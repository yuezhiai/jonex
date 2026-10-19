#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""以图问知识的查询期视觉识别客户端（图片 → 可检索的文本）。

设计：docs/knowledge-base/image-query-search-execution-plan.md §4-B1（含 B0 实测修正）

**与入库侧的关系**：入库时 VLM 也走 llm-gateway（``deploy/.env`` 的
``VLM_BINDING_HOST`` / ``VLM_MODEL_NAME``），产出 ``detailed_description``
（1-3 句客观描述）后拼进 LightRAG 的「图片描述：…」chunk。查询侧要产出的是
**能被同一条文本检索链路召回的东西**，所以提示词口径与入库对齐
（``Reference/Rag-anything/raganything/prompts_zh.py`` 的 ``vision_prompt``）：
只描述可见内容、用具体名称、禁止推测措辞。本模块的 ``description`` 即对应
入库的 ``detailed_description``；额外多要 ``ocr_text`` 与 ``entities``
—— 前者让纯文字截图能靠 OCR 召回，后者是本体链路要的短实体词。

**为什么单独一个模块**：平台侧没有查询期可用的 vision client
（atomic 的 ``LLMClient`` 只支持纯文本 content 且未接 llm-gateway），
调用模式照抄 ``jonex_core/common/ontology_llm.py``（AsyncOpenAI → llm-gateway
+ ``X-Jonex-*`` 计量头）。

⚠️ **两条实测得来的硬要求（2026-10-09）**，改动前先读：
1. ``image_query_caption`` **必须**登记在部署的 ``LLMGW_DISABLE_THINKING_SCENES``
   里。``deepseek-v4-flash-202605`` 是思考型模型，实测大图（1790KB）在开思考时
   ``reasoning_tokens`` 会把 ``max_tokens`` 整个吃掉、``content`` 恒为空字符串
   —— 识别永远失败，且看起来像"模型不会识图"。
2. ``max_tokens`` 必须**显式**给足（见 ``_MAX_TOKENS``）。关思考后仍实测到
   ``max_tokens=256`` 时大图会被截断（``finish_reason=length``）。
   配合下面「空正文 + length 判失败」的处理，一起构成防线。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Optional

from openai import AsyncOpenAI

from jonex_core.common.exceptions import ServiceTimeoutError, UpstreamServiceError
from jonex_core.common.i18n import translate
from jonex_core.common.model_config import resolve_llm_model

logger = logging.getLogger(__name__)

#: 提示词版本。**提示词一改就递增** —— 它是识别缓存的键后缀
#: （``kb:imgq:{tenant}:{sha256}:{PROMPT_VERSION}``），递增即让旧缓存自然失效；
#: 也是图片表 ``prompt_version`` 列的取值来源（过旧的落盘行会被重新识别）。
PROMPT_VERSION = "v1"

#: 调用 llm-gateway 的 scene 名（计量维度 + 关思考白名单都认它）。
SCENE = "image_query_caption"

#: 单张识别的 ``max_tokens``。**必须显式给足**（见模块 docstring）：
#: 关思考下大图实测正文可达 2356 字符，256 会被截断。1024 留出余量，
#: 上限本身也约束了单张的金额与延迟。
_MAX_TOKENS = 1024

#: 提示词里要求的实体上限。与 ``ImageQueryService.build_query`` 的合并上限一致 ——
#: 单图多要几个，合并时按图片轮流取、去重后截到 8 个。
_MAX_ENTITIES = 8

#: 模型名解析链：用途级变量 → 入库侧同名变量（口径与入库对称）。
_MODEL_ENV_VARS = ("IMAGE_QUERY_VLM_MODEL", "VLM_MODEL_NAME")

#: 网关地址解析链：用途级变量 → 本体链路变量 → 容器内默认。
_HOST_ENV_VARS = ("IMAGE_QUERY_VLM_BINDING_HOST", "ONTOLOGY_LLM_BINDING_HOST")
_API_KEY_ENV_VARS = ("IMAGE_QUERY_VLM_BINDING_API_KEY", "ONTOLOGY_LLM_BINDING_API_KEY")

_DEFAULT_HOST = "http://llm-gateway:8787/v1"

#: 模型返回里的代码围栏（```json ... ``` 或 ``` ... ```）。提示词已要求裸 JSON，
#: 但模型偶尔仍会加；解析容错第二档就去掉它。
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

#: 从**被截断的 JSON** 里抢救 ``description`` 字段。
#:
#: 截断几乎总是发生在 ``ocr_text`` 那一段（图里文字多时它最长），
#: 所以 ``description`` 通常已经完整写入、只是后面的内容没了。
#: 抢救它对检索**很有价值** —— 那才是描述图片内容的字段；
#: 而把整段残缺 JSON（含半截 OCR、引号、大括号）当查询文本送检索则是**有害的**。
_TRUNCATED_DESCRIPTION_RE = re.compile(r'"description"\s*:\s*"(.*?)(?<!\\)"', re.DOTALL)

_SYSTEM_PROMPT = (
    "你是图片内容识别助手，为知识库检索提供 Query。请客观描述图片内容，"
    "并**只输出 JSON**，不要输出任何解释或 Markdown 代码围栏。\n"
    "JSON 结构：\n"
    '{"description": "用 1-3 句话客观描述图片内容要点", '
    '"ocr_text": "图中文字原样抄录", '
    '"entities": ["图中出现的关键名称"]}\n'
    "description 的写法：\n"
    "- 只描述图片中**可见**的内容，不推测、不解读创作意图\n"
    "- 识别主要对象、人物、文字和视觉元素（有把握才写）\n"
    "- 如涉及图表、图解等，包含关键数据或结构信息\n"
    "- 始终使用**具体名称**而非代词\n"
    "- 禁止使用「可能/看似/或许/似乎/大概」等推测措辞\n"
    "ocr_text：图中文字**原样**抄录，没有文字则为空字符串；"
    "文字较多时**最多抄录 300 字**，超出部分以「…」省略"
    "（整段抄录会把输出预算吃光、导致 JSON 被截断）。\n"
    f"entities：图中出现的关键名称（人名/作品名/机构名/技术名词等），最多 {_MAX_ENTITIES} 个；"
    "没有则为空数组。**不要**把「图片」「图表」这类通用词当实体。"
)

#: 客户端缓存。**按 (base_url, api_key) 分键**，不按「第一次调用时的 env」定死 ——
#: 与 ``model_config`` 同一条理由：改了 env 不该要重启进程才生效。
_clients: dict[tuple[str, str], AsyncOpenAI] = {}


def _read_env(names: tuple[str, ...]) -> str:
    """按顺序取第一个非空白的环境变量值；都没有返回空串。"""
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _resolve_model() -> str:
    """解析 VLM 模型名：``IMAGE_QUERY_VLM_MODEL`` → ``VLM_MODEL_NAME`` → 响亮失败。

    最后一步走 ``resolve_llm_model`` 是为了复用既有约定（模型名**不留字面量**，
    解析不出来就抛 ``ModelNotConfiguredError`` 并指明该配哪个变量）—— 宁可拦在
    配置层，也不让一个可能早已失效的名字流到上游才炸。
    """
    explicit = _read_env(_MODEL_ENV_VARS)
    if explicit:
        return explicit
    return resolve_llm_model("IMAGE_QUERY_VLM_MODEL")


def _get_client() -> AsyncOpenAI:
    base_url = _read_env(_HOST_ENV_VARS) or _DEFAULT_HOST
    api_key = _read_env(_API_KEY_ENV_VARS)
    key = (base_url, api_key)
    client = _clients.get(key)
    if client is None:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        _clients[key] = client
    return client


def _salvage_truncated(text: str) -> Optional[dict[str, Any]]:
    """从被截断的 JSON 里抢救 ``description``；救不出来返回 ``None``。"""
    match = _TRUNCATED_DESCRIPTION_RE.search(text or "")
    if not match:
        return None
    desc = match.group(1).replace('\\"', '"').replace("\\n", " ").strip()
    return {"description": desc, "ocr_text": "", "entities": []} if desc else None


def _parse_payload(raw: str, *, truncated: bool = False) -> Optional[dict[str, Any]]:
    """把模型回包解析成 ``{description, ocr_text, entities}``。

    解析顺序：
    1. 直接 ``json.loads``；
    2. 去掉 Markdown 代码围栏再试（提示词已禁，但模型偶尔仍加）；
    3. 仍失败：
       - **未被截断**（``finish_reason != length``）→ **整段文本当 description**。
         这是有意的降级：模型给了人能读懂的一段话，只是没按 JSON 来，
         拿它去检索**仍然有用**，为此整条检索失败不划算。
       - **被截断** → 先试着抢救 ``description`` 字段；**救不出来就返回 ``None``**
         （调用方转成失败）。
         ⚠️ 这一档绝不能退回第 3 档的"整段当描述"：截断后的文本里混着
         ``{"description": "...", "ocr_text": "半截话…`` 这类 JSON 语法与半截内容，
         把它当查询文本送检索，检索到的就是**一堆语法噪声** ——
         而症状极难归因（用户看到的是"检索不准"，不是"识别失败"）。
    """
    text = (raw or "").strip()
    if not text:
        return {"description": "", "ocr_text": "", "entities": []}

    candidate = text
    fenced = _FENCE_RE.match(text)
    if fenced:
        candidate = fenced.group(1).strip()

    data: Any = None
    for attempt in (candidate, text):
        try:
            data = json.loads(attempt)
            break
        except (ValueError, TypeError):
            continue

    if not isinstance(data, dict):
        if truncated:
            return _salvage_truncated(text)
        return {"description": candidate or text, "ocr_text": "", "entities": []}

    description = str(data.get("description") or "").strip()
    ocr_text = str(data.get("ocr_text") or "").strip()

    raw_entities = data.get("entities")
    entities: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_entities, list):
        for item in raw_entities:
            name = str(item or "").strip()
            if name and name not in seen:
                seen.add(name)
                entities.append(name)
            if len(entities) >= _MAX_ENTITIES:
                break

    return {
        "description": description,
        "ocr_text": ocr_text,
        "entities": entities,
    }


async def describe_query_image(
    image_base64: str,
    mime_type: str,
    *,
    tenant_id: str,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    request_id: Optional[str] = None,
    timeout: float = 40.0,
) -> dict[str, Any]:
    """识别一张图片，返回 ``{description, ocr_text, entities, model}``。

    **不接收 question**：结果按图缓存（同一张图换个问题也能命中缓存），
    问题只在拼检索 query 时用（见 ``ImageQueryService.build_query``）。
    代价是 VLM 不会针对问题去关注图中局部，换来的是缓存命中率与去重复用。

    Args:
        image_base64: 纯 base64（**不带** ``data:`` 前缀）。
        mime_type: 真实 MIME（调用方按 magic bytes 判定，不信任上传声明）。
        request_id: 透传为 ``X-Jonex-Request-Id``，即网关的**计量幂等键**。
            显式给出有两个好处（实测）：① 跳过网关对整个请求体（含 base64）
            的 ``json.dumps`` + sha1（2388KB 体实测 7.5ms、12MB 量级约 50ms 与等量内存）；
            ② 幂等键由调用方自控，重发同一张图不会重复计量。

    Returns:
        ``{"description": str, "ocr_text": str, "entities": list[str], "model": str}``
        —— **不含 base64，也不回传原始回包**。

    Raises:
        ServiceTimeoutError: 超过 ``timeout``（5004）。
        UpstreamServiceError: 上游 4xx/5xx、连接失败、或**识别不出内容**
            （空正文，含「大图 + 开思考被 max_tokens 截空」那种，5003）。
    """
    model = _resolve_model()
    client = _get_client()

    extra_headers: dict[str, str] = {"X-Jonex-Scene": SCENE}
    # 租户头**只在有真值时才发**：调用方（`ImageQueryService.resolve`）已经过
    # `require_tenant`，正常路径必然是合法租户；这里再写一个 `or "unknown"`
    # 等于在客户端伪造一个租户值、把一个"不该发生"的情况悄悄糊过去。
    # 不发则由网关按它自己的默认处理（`llm_gateway/context.py:parse_ctx`），
    # 结果一样，但少一处伪造。
    if tenant_id:
        extra_headers["X-Jonex-Tenant-Id"] = tenant_id
    # 有意不带 X-Jonex-Kb-Id：识别步骤不读任何 KB 数据，也不接收 KB 范围，
    # 带了只会让计量里的 kb 维度出现一个与本次检索无关的值。
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id
    if trace_id:
        extra_headers["X-Jonex-Trace-Id"] = trace_id
    if request_id:
        extra_headers["X-Jonex-Request-Id"] = request_id

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "识别这张图片。"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                },
            ],
        },
    ]

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0,
                max_tokens=_MAX_TOKENS,
                response_format={"type": "json_object"},
                extra_headers=extra_headers,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise ServiceTimeoutError(
            message=translate(
                "err.image_query.vlm_timeout",
                fallback="图片识别超时",
            ),
            details={"model": model, "timeout": timeout},
        ) from exc
    except Exception as exc:  # noqa: BLE001 —— 上游各式异常统一收口成 5003
        logger.warning(
            "以图问知识：VLM 调用失败 model=%s mime=%s err=%s: %s",
            model, mime_type, type(exc).__name__, exc,
        )
        raise UpstreamServiceError(
            message=translate(
                "err.image_query.vlm_failed",
                fallback="图片识别服务不可用",
            ),
            details={"model": model},
        ) from exc

    choice = (response.choices or [None])[0]
    message = getattr(choice, "message", None)
    content = (getattr(message, "content", None) or "").strip()
    finish_reason = getattr(choice, "finish_reason", None)

    if not content:
        # 空正文。最常见的成因是**思考型模型的 reasoning 吃光了 max_tokens**
        # （实测：大图 + 未登记 scene 时 finish_reason=length、content=""）——
        # 那是识别失败，绝不能当成「识别出空内容」静默放过去，
        # 否则用户会拿到一个凭空捏造的检索结果。
        logger.warning(
            "以图问知识：VLM 返回空正文 model=%s mime=%s finish_reason=%s（检查 "
            "LLMGW_DISABLE_THINKING_SCENES 是否含 %s，以及 max_tokens 是否够）",
            model, mime_type, finish_reason, SCENE,
        )
        raise UpstreamServiceError(
            message=translate(
                "err.image_query.vlm_failed",
                fallback="图片识别服务不可用",
            ),
            details={"model": model, "finish_reason": finish_reason, "empty_content": True},
        )

    parsed = _parse_payload(content, truncated=(finish_reason == "length"))
    if parsed is None:
        # 被截断、且连 description 都救不出来 —— **宁可失败**，也不拿一段残缺 JSON 去检索。
        # 成因几乎总是「关思考没生效 / max_tokens 太小」，日志里直接点出来。
        logger.warning(
            "以图问知识：VLM 输出被截断且无法抢救 description model=%s mime=%s "
            "finish_reason=%s usage=%s（检查 LLMGW_DISABLE_THINKING_SCENES 是否含 %s、"
            "以及 max_tokens 是否够）",
            model, mime_type, finish_reason, getattr(response, "usage", None), SCENE,
        )
        raise UpstreamServiceError(
            message=translate(
                "err.image_query.vlm_failed",
                fallback="图片识别服务不可用",
            ),
            details={"model": model, "finish_reason": finish_reason, "truncated": True},
        )
    parsed["model"] = model
    return parsed


__all__ = [
    "PROMPT_VERSION",
    "SCENE",
    "describe_query_image",
]
