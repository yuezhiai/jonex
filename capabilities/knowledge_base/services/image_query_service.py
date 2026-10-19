#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""以图问知识：图片 → 查询文本（两步式的第一步）。

设计：docs/knowledge-base/image-query-search-execution-plan.md §4-B3

本 service **不检索**，只把图识别成一段可以直接丢给现有检索接口的文本
（两步式：识别与检索各占一次往返，现有检索链路一行不改）。
识别产物随检索历史持久化由 `QueryImageService` 负责（本模块只把字节
**暂存进 Redis**，等保存历史时才落盘 —— 「未检索的图片不留存」）。

⚠️ **只暂存、不落盘**：
    识别成功后字节 + 识别结果写 `kb:imgq:pending:{tenant}:{user}:{image_id}`
    （TTL `IMAGE_QUERY_PENDING_TTL`），返回 `image_id`；前端保存历史时带上它，
    才由 `QueryImageService.bind_for_history` 写对象存储与数据库。
    「识别了但没检索 / 没保存历史」的图只在 Redis 停到 TTL 过期，**两个系统里都不留痕**。

⚠️ **识别结果按图缓存，question 不进缓存键**：
    键是 `kb:imgq:{tenant}:{sha256}:{PROMPT_VERSION}`。同一张图换个问题照样命中缓存，
    代价是 VLM 不会针对问题去关注图中局部 —— 检索本来就只吃文本，这个代价换来的
    缓存命中率与去重复用更值。`PROMPT_VERSION` 一改旧缓存自然失效。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import time
from typing import Any, Optional
from uuid import uuid4

from jonex_core.common.cache import CacheUtil
from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.common import vision_llm

from ..dtos import (
    ImageQueryImageOut,
    ImageQueryRequest,
    ImageQueryResponse,
)
from ..repository import KnowledgeQueryImageRepository

logger = logging.getLogger(__name__)

#: 与 `vision_llm` 共用同一个版本号 —— 它就是识别缓存的键后缀，
#: 也是图片表 `prompt_version` 列的取值来源。
PROMPT_VERSION = vision_llm.PROMPT_VERSION

#: 合并后查询文本里保留的实体数上限。
MAX_MERGED_ENTITIES = 8

#: 无提问、且实体与描述都为空时的最后兜底：OCR 文字拼接后的前 N 字。
_OCR_FALLBACK_CHARS = 200

_WS_RE = re.compile(r"\s+")
#: 句子切分（取描述第一句用）。中英文句末标点都认。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]")


# ── 图片类型判定（magic bytes，不信任上传声明） ────────────────────

def detect_image_mime(raw: bytes) -> Optional[str]:
    """按 magic bytes 判真实类型；不在白名单返回 ``None``。

    ⚠️ 有意的判定口径：
    - **不信任** 报文里的 `Content-Type` / 文件名后缀（都可伪造）；
    - **不含 SVG** —— 它是可执行 XML，同源 inline 渲染有 XSS 面；
    - WEBP 要同时满足前 4 字节 `RIFF` 与第 8-11 字节 `WEBP`（RIFF 容器不止 webp）。
    """
    if len(raw) < 12:
        return None
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] in (b"GIF8",):
        return "image/gif"
    if raw[:2] == b"BM":
        return "image/bmp"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


# ── Redis 键（暂存与缓存） ────────────────────────────────────────
# ⚠️ 三处必须**用同一份实现**：本模块写暂存、`QueryImageService` 读暂存、
# 两边都按 sha 读写识别缓存。各抄一遍迟早抄歪成「写得进、读不出」。

def cache_key(tenant_id: str, sha256: str) -> str:
    """识别文本缓存键（**租户级**：同租户内同图可复用，与落盘去重的用户级不同）。"""
    return f"kb:imgq:{tenant_id}:{sha256}:{PROMPT_VERSION}"


def pending_key(tenant_id: str, user_id: str, image_id: str) -> str:
    """暂存键（**带 user**：别人拿不到我的 image_id，也就读不走字节）。"""
    return f"kb:imgq:pending:{tenant_id}:{user_id}:{image_id}"


def vlm_request_id(tenant_id: str, trace_id: Optional[str], index: int) -> str:
    """发给 llm-gateway 的**计量幂等键**（`X-Jonex-Request-Id`）。

    ⚠️ **必须每次逻辑调用都不同，绝不能只由图片内容派生。**
    网关写计量表用的是 `INSERT ... ON CONFLICT (request_id) DO NOTHING`，
    所以键一旦能被两次不同的调用复现，后一次的计量就被**静默丢掉**。
    按内容取哈希（如 `{sha256}`）正好踩这个坑：同一张图的第二次真实调用
    （缓存过期 / 提示词升级 / 另一个用户、乃至**另一个租户**传了同一张图）
    会撞上同一个键 —— 跨租户那档等于把两家的账并成一行。

    幂等键要防的是「同一次调用的重复投递」，不是「同内容的两次调用」，
    所以这里用 `租户 + 请求 trace + 图序`：同一次请求里重发幂等，
    不同请求（不同租户、不同时刻）天然分开。

    用显式键还有个附带好处：跳过网关对整个请求体（含 base64）做
    `json.dumps` + sha1 派生（大图实测约 7.5ms 与等量内存）。
    """
    return f"imgq:{tenant_id}:{trace_id or uuid4().hex}:{index}"


# ── 查询文本拼装（纯函数，单独可测） ──────────────────────────────

def _round_robin_entities(recognitions: list[dict], limit: int = MAX_MERGED_ENTITIES) -> list[str]:
    """按图片顺序**轮流**取实体，去重后截到 ``limit``。

    轮流取（而不是「第一张图的全拿完再拿第二张」）是为了保证**每张图都有代表** ——
    否则第一张图实体多的时候，后面几张图在查询文本里一个字都不出现。
    """
    columns: list[list[str]] = []
    for item in recognitions:
        names: list[str] = []
        for raw in item.get("entities") or []:
            name = _WS_RE.sub(" ", str(raw or "")).strip()
            if name:
                names.append(name)
        columns.append(names)

    out: list[str] = []
    seen: set[str] = set()
    depth = 0
    while len(out) < limit:
        progressed = False
        for names in columns:
            if depth >= len(names):
                continue
            progressed = True
            name = names[depth]
            if name not in seen:
                seen.add(name)
                out.append(name)
                if len(out) >= limit:
                    break
        if not progressed:
            break
        depth += 1
    return out


def _first_sentence(text: str) -> str:
    """描述的第一句（拼无提问时的查询文本用）。"""
    cleaned = _WS_RE.sub(" ", (text or "")).strip()
    if not cleaned:
        return ""
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(cleaned) if p.strip()]
    return parts[0] if parts else cleaned


def build_query(
    question: Optional[str],
    recognitions: list[dict],
    max_chars: int,
) -> str:
    """把各图识别结果拼成一段检索查询文本（纯函数）。

    Args:
        question: 用户键入的提问（可空）。
        recognitions: **识别成功的**图片结果，按图片顺序；每项含
            ``description / ocr_text / entities``。
        max_chars: 结果长度上限（``IMAGE_QUERY_MAX_QUERY_CHARS``）。

    Returns:
        可直接丢给检索接口的查询文本。

    Raises:
        InvalidParameterError: 拼不出任何可检索内容
            （``err.image_query.empty_result``）。**在这里抛而不是返回空串**，
            是为了让「识别出来了、但等于什么都没识别到」这件事无法被调用方漏检。

    ── 组装与截断──

    优先级从高到低：**实体 → 提问 → 描述 → OCR**。前面的完整保留，最后的按剩余
    预算截断（提问超长时从尾部截，问句的关键信息通常在前半句）。

    两种形态（**描述在两种形态里都参与**，只是位置与优先级不同）：

    - 无提问：``实体 + 各图描述首句``（描述为空退 OCR）；
    - 有提问：``实体 + 提问 + 描述`` —— 描述排在最后、超长时**先被截掉**。

    ⚠️ 「有提问时也要带描述」是**实测逼出来的**，见下面 `entity_part` 分支里的注释：
    查询侧抽出的实体未必出现在索引里，只靠它们检索会把图片本身的信息丢掉。

    ⚠️ **实体为空时必须回落，不能只返回提问。** 这是踩过的坑：VLM 抽不出实体
    一点都不罕见（实测里装饰性图片就是 ``entities: []``），而原先「有提问」
    分支只取 实体+提问、**从不回落**，于是 query 退化成用户输入的原话 ——
    图片对检索的贡献归零，而且现象极具迷惑性（卡片上「识别为：」把用户自己打的字
    原样回显，看起来像显示 bug，实则是检索压根没用上图）。
    回落时图片内容改由**描述 / OCR** 承担，且仍排在提问**之后** ——
    因为截断优先级里提问高于描述，描述是被裁的那一个。

    ⚠️ **实体必须打头且不被裁掉** —— 本体的 exact/prefix 匹配拿整条 query 去比实体名，
    实体词被长提问挤掉就等于这两级彻底失效。
    """
    max_chars = max(1, int(max_chars))

    entity_part = _WS_RE.sub(" ", " ".join(_round_robin_entities(recognitions))).strip()
    desc_part = _WS_RE.sub(
        " ",
        "；".join(
            s for s in (_first_sentence(item.get("description") or "") for item in recognitions) if s
        ),
    ).strip()
    ocr_part = _WS_RE.sub(
        " ", "".join((item.get("ocr_text") or "") for item in recognitions)
    ).strip()[:_OCR_FALLBACK_CHARS]
    question_part = _WS_RE.sub(" ", question).strip() if question else ""

    # 图片侧内容：实体 → 描述 → OCR（**提问不参与这一级**）
    image_part = entity_part or desc_part or ocr_part

    def _empty() -> InvalidParameterError:
        return InvalidParameterError(
            message=translate(
                "err.image_query.empty_result",
                fallback="未识别出可检索的内容",
            )
        )

    # ── 形态一：无提问 → 实体 + 各图描述首句（实体空则退描述、再退 OCR）──
    if not question_part:
        if not image_part:
            raise _empty()
        if entity_part and desc_part:
            # 实体打头，描述跟在后面（描述被裁、实体不裁）
            budget = max_chars - len(entity_part) - 1
            if budget <= 0:
                return entity_part[:max_chars]
            return f"{entity_part} {desc_part[:budget]}"
        return image_part[:max_chars]

    # ── 形态二：有提问 ──
    if not image_part:
        # 图里什么都没读出来、只有一句提问 —— 按「识别不出内容」报错，
        # **不**拿用户原话去检索：那样用户会看到「识别为：<自己刚打的字>」，
        # 以为图片参与了检索，实际一个图像词都没有（失败处置：
        # 有文字时给「仅用文字检索」让用户**自己选**，而不是替他悄悄降级）。
        raise _empty()

    if entity_part:
        # 标准形态：实体打头且完整保留（本体 exact/prefix 要的就是它们），提问其后
        head = entity_part
        budget = max_chars - len(head) - 1
        if budget <= 0:
            return head[:max_chars]
        if len(question_part) >= budget:
            # 提问自己就把预算吃满了：从尾部截提问，描述不再放
            return f"{head} {question_part[:budget]}"
        head = f"{head} {question_part}"

        # ⚠️ **描述以最低优先级追加在最后**（截断先截它）。
        #
        # 这一条是实测逼出来的，不是设计推演：查询侧 VLM 抽出的"实体"往往只是几个短词，
        # 而且**未必出现在索引里**——实测一张图识别出 `监护仪 / 呼吸机 / 自测题`，
        # 而全库 69 条图片描述里 `监护仪`、`自测题` 各命中 **0** 次。
        # 光靠这几个词检索，模型只能答"无法确认该图片的出处"；
        # 把描述（自由散文，与入库侧的图片描述同语言同风格，字面重合度高得多）追加进去，
        # 同一个问题立刻答出「出自 …v2.3.pdf 的**第 15 页**」。
        #
        # 优先级仍是 实体 → 提问 → 描述：实体与提问在前、超长时先裁描述，
        # 所以本体 exact/prefix 那两级不会被长描述影响。
        budget = max_chars - len(head) - 1
        if budget > 0 and desc_part:
            return f"{head} {desc_part[:budget]}"
        return head

    # 回落形态：图片内容改由描述/OCR 承担。截断优先级里提问高于描述 →
    # **留提问、裁图片内容**（否则回落形态下提问反而先被裁掉，与优先级自相矛盾）
    budget = max_chars - len(question_part) - 1
    if budget <= 0:
        return question_part[:max_chars]
    return f"{image_part[:budget]} {question_part}"


class ImageQueryService:
    """图片 → 查询文本。"""

    def __init__(self) -> None:
        self._config = get_config()

    # ── 主流程 ────────────────────────────────────────────────

    async def resolve(
        self,
        tenant_id: str,
        user_id: Optional[str],
        data: dict,
        trace_id: Optional[str] = None,
    ) -> dict:
        """识别一批图片，返回合并后的查询文本与逐张状态。

        Raises:
            InvalidParameterError: 参数 / 上限 / 格式问题，以及**全部图片识别失败**。
            单张失败不抛 —— 记在该张的 ``status=failed`` 上，其余照常。
        """
        started = time.perf_counter()
        tenant_id = require_tenant(tenant_id)

        # 图片按本人隔离（可见范围跟随历史、暂存键带 user），匿名身份既没有
        # 「本人」可挂、也是同租户所有人共用的一个坑位，所以直接拒绝。
        if not user_id or user_id == "anonymous":
            raise InvalidParameterError(
                message=translate(
                    "err.query_image.anonymous_forbidden",
                    fallback="以图检索需要登录",
                )
            )

        cfg = self._config
        try:
            req = ImageQueryRequest(**(data or {}))
        except Exception:
            # pydantic ValidationError 原样上抛：capability 的 execute() 统一转 400
            # 「参数校验失败」（既有惯例，见 dtos/image_query.py 的模块注释）。
            raise

        # 张数 / 总大小的**请求级**复核（Gateway 已按同一份配置挡过一次；
        # 这里再核是因为能力服务可能被直连调用，且配置以本服务的为准）。
        if len(req.images) > cfg.IMAGE_QUERY_MAX_IMAGES:
            raise InvalidParameterError(
                message=translate(
                    "err.image_query.too_many",
                    params={"max_images": cfg.IMAGE_QUERY_MAX_IMAGES},
                    fallback=f"最多支持 {cfg.IMAGE_QUERY_MAX_IMAGES} 张图片",
                ),
                details={"max_images": cfg.IMAGE_QUERY_MAX_IMAGES},
            )

        prepared = self._decode_all(tenant_id, req.images)
        if sum(item["size"] for item in prepared) > cfg.IMAGE_QUERY_MAX_TOTAL_BYTES:
            raise InvalidParameterError(
                message=translate(
                    "err.image_query.too_large_total",
                    fallback="图片总大小超过限制",
                ),
                details={"max_total_bytes": cfg.IMAGE_QUERY_MAX_TOTAL_BYTES},
            )

        semaphore = asyncio.Semaphore(max(1, cfg.IMAGE_QUERY_VLM_CONCURRENCY))

        async def _one(index: int, item: dict) -> dict:
            async with semaphore:
                return await self._resolve_one(
                    index=index,
                    item=item,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    trace_id=trace_id,
                )

        # ⚠️ 请求内 sha256 相同的张**只识别一次**：只对「首现」那张发起识别，重复项直接复用结果。
        #
        # 不这么做的话，**光靠识别缓存挡不住**：真实 Redis / DB 调用之间有挂起点，
        # 两张重复图会同时发现缓存未命中 → 并发调两次 VLM —— 多花一次钱、多一条计量，
        # 还白占一个并发名额。（`_decode_all` 早就把重复张的 `reuse_index` 指好了，
        # 只是一直没人读它。）
        primaries = [item for item in prepared if item["reuse_index"] is None]
        primary_results = await asyncio.gather(
            *(_one(item["index"], item) for item in primaries)
        )
        by_index = {result["index"]: result for result in primary_results}
        results: list[dict] = []
        for item in prepared:
            if item["reuse_index"] is None:
                results.append(by_index[item["index"]])
            else:
                # 重复张沿用首现张的结果，只把 `index` 换回自己的位次（前端按它对位预览）
                results.append({**by_index[item["reuse_index"]], "index": item["index"]})

        ok_results = [r for r in results if r["status"] == "ok"]
        if not ok_results:
            # 全部失败：把第一张的异常原样抛出（它是「这次为什么不行」的最直接答案）
            first_error = next((r.get("_error") for r in results if r.get("_error")), None)
            if first_error is not None:
                raise first_error
            raise InvalidParameterError(
                message=translate(
                    "err.image_query.empty_result",
                    fallback="未识别出可检索的内容",
                )
            )

        # 拼不出可检索内容时由 build_query 抛 empty_result（见它的 Raises）
        query_text = build_query(req.question, ok_results, cfg.IMAGE_QUERY_MAX_QUERY_CHARS)

        duration_ms = int((time.perf_counter() - started) * 1000)
        entities = _round_robin_entities(ok_results)
        model = next((r.get("model") for r in ok_results if r.get("model")), "")

        response = ImageQueryResponse(
            query=query_text,
            images=[
                ImageQueryImageOut(
                    index=r["index"],
                    status=r["status"],
                    error_code=r.get("error_code"),
                    image_id=r.get("image_id"),
                    description=r.get("description") or "",
                    ocr_text=r.get("ocr_text") or "",
                    entities=list(r.get("entities") or []),
                    cached=bool(r.get("cached")),
                    persisted=bool(r.get("persisted")),
                )
                for r in results
            ],
            entities=entities,
            model=model,
            partial=any(r["status"] != "ok" for r in results),
            duration_ms=duration_ms,
            limits={
                "max_images": cfg.IMAGE_QUERY_MAX_IMAGES,
                "max_bytes": cfg.IMAGE_QUERY_MAX_BYTES,
                "max_total_bytes": cfg.IMAGE_QUERY_MAX_TOTAL_BYTES,
            },
        )
        logger.info(
            "以图问知识：识别完成 tenant=%s 张数=%d 成功=%d 耗时=%dms 实体=%d 模型=%s",
            tenant_id, len(results), len(ok_results), duration_ms, len(entities), model,
        )
        return response.dict()

    # ── 请求级预处理 ──────────────────────────────────────────

    def _decode_all(self, tenant_id: str, images: list) -> list[dict]:
        """逐张 base64 解码 + magic bytes 复核 + 请求内去重。

        **请求级错误**（解码失败 / 超单张上限 / 格式不支持）在这里直接抛 ——
        它们不是「这张图识别不出来」，而是报文本身有问题。

        请求内 sha256 相同的张只留第一张的字节（后一张标记 ``reuse_index``），
        识别时直接复用前一张的结果（同一请求里的重复图只识别一次、只算一张）。
        """
        cfg = self._config
        out: list[dict] = []
        seen: dict[str, int] = {}
        for idx, image in enumerate(images):
            try:
                raw = base64.b64decode(image.image_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise InvalidParameterError(
                    message=translate(
                        "err.image_query.unsupported_type",
                        fallback="不支持的图片格式",
                    ),
                    details={"index": idx},
                ) from exc

            if len(raw) > cfg.IMAGE_QUERY_MAX_BYTES:
                raise InvalidParameterError(
                    message=translate(
                        "err.image_query.too_large",
                        fallback="图片超过大小限制",
                    ),
                    details={"index": idx, "max_bytes": cfg.IMAGE_QUERY_MAX_BYTES},
                )

            real_mime = detect_image_mime(raw)
            if real_mime is None:
                raise InvalidParameterError(
                    message=translate(
                        "err.image_query.unsupported_type",
                        fallback="不支持的图片格式",
                    ),
                    details={"index": idx, "declared": image.mime_type},
                )

            digest = hashlib.sha256(raw).hexdigest()
            reuse_index = seen.get(digest)
            if reuse_index is None:
                seen[digest] = idx
            out.append(
                {
                    "index": idx,
                    "raw": raw,
                    "base64": image.image_base64,
                    "mime": real_mime,          # 真实类型，覆盖声明值
                    "declared_mime": image.mime_type,
                    "size": len(raw),
                    "sha256": digest,
                    "reuse_index": reuse_index,
                }
            )
        return out

    # ── 单张 ──────────────────────────────────────────────────

    async def _resolve_one(
        self,
        *,
        index: int,
        item: dict,
        tenant_id: str,
        user_id: str,
        trace_id: Optional[str],
    ) -> dict:
        """单张识别。**业务异常转成该张的 ``status=failed``，不中断其它张**。"""
        cfg = self._config
        base = {
            "index": index,
            "description": "",
            "ocr_text": "",
            "entities": [],
            "cached": False,
            "persisted": False,
            "model": "",
        }
        try:
            return await self._recognize(item, tenant_id=tenant_id, user_id=user_id, trace_id=trace_id, index=index)
        except Exception as exc:  # noqa: BLE001
            error_code = getattr(exc, "code", None)
            logger.warning(
                "以图问知识：第 %d 张识别失败 tenant=%s sha=%s size=%d mime=%s err=%s",
                index, tenant_id, item["sha256"][:12], item["size"], item["mime"],
                type(exc).__name__,
            )
            return {**base, "status": "failed", "error_code": error_code, "_error": exc}

    async def _recognize(
        self,
        item: dict,
        *,
        tenant_id: str,
        user_id: str,
        trace_id: Optional[str],
        index: int,
    ) -> dict:
        cfg = self._config
        sha256 = item["sha256"]

        # ── 有提问也没关系：识别只按图（question 不进这里，也不进缓存键）──
        persisted = await self._find_persisted(tenant_id, user_id, sha256)
        if persisted is not None and (persisted.prompt_version or "") == PROMPT_VERSION:
            logger.info(
                "以图问知识：命中已持久化图片 tenant=%s sha=%s image_id=%s（不调 VLM、不暂存）",
                tenant_id, sha256[:12], persisted.id,
            )
            return {
                "index": index,
                "status": "ok",
                "description": persisted.description or "",
                "ocr_text": persisted.ocr_text or "",
                "entities": list(persisted.entities or []),
                "cached": True,
                "persisted": True,
                "image_id": persisted.id,
                "model": persisted.vlm_model or "",
            }

        # ── 识别（缓存优先）──
        cached_payload = await self._read_cache(tenant_id, sha256)
        if cached_payload is not None:
            recognition = cached_payload
            from_cache = True
        else:
            recognition = await vision_llm.describe_query_image(
                item["base64"],
                item["mime"],
                tenant_id=tenant_id,
                user_id=user_id,
                trace_id=trace_id,
                request_id=vlm_request_id(tenant_id, trace_id, index),
                timeout=float(cfg.IMAGE_QUERY_VLM_TIMEOUT),
            )
            from_cache = False
            await self._write_cache(tenant_id, sha256, recognition)

        payload = {
            "description": recognition.get("description") or "",
            "ocr_text": recognition.get("ocr_text") or "",
            "entities": list(recognition.get("entities") or []),
            "model": recognition.get("model") or "",
        }

        if persisted is not None:
            # 提示词版本过旧：重新识别后**更新该行**，沿用同一个 image_id
            await self._refresh_persisted(tenant_id, persisted.id, payload)
            logger.info(
                "以图问知识：提示词版本过旧，已刷新已持久化图片 tenant=%s sha=%s image_id=%s",
                tenant_id, sha256[:12], persisted.id,
            )
            return {
                "index": index, "status": "ok", **payload,
                "cached": from_cache, "persisted": True, "image_id": persisted.id,
            }

        # ── 未持久化：暂存到 Redis，等保存历史时落盘──
        image_id = None
        if cfg.IMAGE_QUERY_PERSIST_ENABLED:
            image_id = await self._stage_pending(
                tenant_id=tenant_id, user_id=user_id, item=item, payload=payload
            )

        logger.info(
            "以图问知识：识别 tenant=%s sha=%s size=%d mime=%s 缓存=%s 暂存=%s 实体=%d",
            tenant_id, sha256[:12], item["size"], item["mime"],
            from_cache, bool(image_id), len(payload["entities"]),
        )
        return {
            "index": index, "status": "ok", **payload,
            "cached": from_cache, "persisted": False, "image_id": image_id,
        }

    # ── 持久化行读写（短会话；VLM 调用不在会话里） ─────────────

    async def _find_persisted(self, tenant_id: str, user_id: str, sha256: str):
        async with get_db_session() as session:
            repo = KnowledgeQueryImageRepository(session)
            return await repo.find_by_sha(tenant_id, user_id, sha256)

    async def _refresh_persisted(self, tenant_id: str, image_id: str, payload: dict) -> None:
        async with get_db_session() as session:
            repo = KnowledgeQueryImageRepository(session)
            await repo.update_recognition(
                image_id,
                tenant_id=tenant_id,
                recognized_query=None,
                description=payload["description"],
                ocr_text=payload["ocr_text"],
                entities=payload["entities"],
                prompt_version=PROMPT_VERSION,
                vlm_model=payload["model"] or None,
            )

    # ── 识别缓存（Redis 不可用时只 warning，不影响主流程） ──────

    async def _read_cache(self, tenant_id: str, sha256: str) -> Optional[dict]:
        try:
            raw = await CacheUtil.get(cache_key(tenant_id, sha256))
        except Exception as exc:  # noqa: BLE001
            logger.warning("以图问知识：识别缓存读取失败，按未命中处理: %s", exc)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except (ValueError, AttributeError):
            return None
        return data if isinstance(data, dict) else None

    async def _write_cache(self, tenant_id: str, sha256: str, recognition: dict) -> None:
        try:
            await CacheUtil.set(
                cache_key(tenant_id, sha256),
                json.dumps(
                    {
                        "description": recognition.get("description") or "",
                        "ocr_text": recognition.get("ocr_text") or "",
                        "entities": list(recognition.get("entities") or []),
                        "model": recognition.get("model") or "",
                    },
                    ensure_ascii=False,
                ),
                expire=int(self._config.IMAGE_QUERY_CACHE_TTL),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("以图问知识：识别缓存写入失败（不影响本次识别）: %s", exc)

    # ── 暂存 ──────────────────────────────────────

    async def _stage_pending(
        self, *, tenant_id: str, user_id: str, item: dict, payload: dict
    ) -> Optional[str]:
        """把字节 + 识别结果写进 Redis 暂存，返回 ``image_id``；失败返回 ``None``。

        暂存失败**不影响本次检索** —— 识别结果照常返回，只是 `image_id=null`，
        该次检索的历史不带图。历史文本是主数据，不能因为图片丢失而失败。
        """
        image_id = uuid4().hex
        body = json.dumps(
            {
                "base64": item["base64"],
                "mime_type": item["mime"],
                "file_size": item["size"],
                "sha256": item["sha256"],
                "description": payload["description"],
                "ocr_text": payload["ocr_text"],
                "entities": payload["entities"],
                "model": payload["model"],
                "prompt_version": PROMPT_VERSION,
            },
            ensure_ascii=False,
        )
        try:
            ok = await CacheUtil.set(
                pending_key(tenant_id, user_id, image_id),
                body,
                expire=int(self._config.IMAGE_QUERY_PENDING_TTL),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("以图问知识：暂存写入失败，该图不进历史: %s", exc)
            return None
        if not ok:
            logger.warning("以图问知识：暂存写入未确认，该图不进历史")
            return None
        return image_id


__all__ = [
    "ImageQueryService",
    "vlm_request_id",
    "build_query",
    "cache_key",
    "detect_image_mime",
    "pending_key",
    "MAX_MERGED_ENTITIES",
    "PROMPT_VERSION",
]
