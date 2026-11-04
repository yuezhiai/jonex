"""目录服务：LLM 起草 + LLM 调整 + 读取（目录功能下沉，引擎侧）。

平移自平台 ``WikiOutlineService``，按引擎约定改造：
- 依赖注入 ``session_factory``/``gw``/``layout``/``settings``（不延迟构造客户端）；
- 页面清单直接读引擎 ``bundle_reader``（不经过平台 adapter 的展示投影）；
- 错误用引擎 ``kernel.errors`` 表达（ValidationError/NotFoundError/DomainError）；
- LLM 走引擎 ``GwClient.complete``（``json_mode=True`` + ``_extract_json_object`` 兜底）；
- 决策 5：目录 prompt 不写「知识库：{kb_name}」，请求不依赖平台 ``KnowledgeInfo``。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from jonex_wiki.domain.outline.outline import (
    ADJUST_COVERAGE_FLOOR,
    MAX_DESCRIPTION_LEN,
    MAX_PAGES_FOR_LLM,
    SOURCE_ADJUST,
    SOURCE_DRAFT,
    _ADJUST_SYSTEM_PROMPT,
    _DRAFT_SYSTEM_PROMPT,
    _catalog_snapshot,
    _clean_title,
    _clip,
    _covered_paths,
    _draft_max_tokens,
    _extract_json_object,
    _fingerprint,
    _outline_for_llm,
    build_topic_edges,
    sanitize_outline,
    scope_outline_to_pages,
)
from jonex_wiki.kernel.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from jonex_wiki.storage.repos import OutlineRepo

log = logging.getLogger(__name__)

# draft/adjust 共用 scene（决策：不按 action 拆分，便于计量/白名单统一）
_SCENE = "jonex_wiki_outline"
_KB_TYPE = "jonex_wiki"


class OutlineService:
    """Wiki 阅读目录读写服务（引擎侧）。"""

    def __init__(self, *, session_factory, gw, layout, settings):
        self.session_factory = session_factory
        self.gw = gw
        self.layout = layout
        self.settings = settings
        self._repo = OutlineRepo()

    # ────────────────────────────────────────────────────────────
    # 页面清单
    # ────────────────────────────────────────────────────────────
    def _published_dir(self, tenant_id: str, kb_id: str):
        d = self.layout.resolve_published(tenant_id, kb_id)
        if d is None:
            raise NotFoundError(f"知识库 {kb_id} 尚未编译发布")
        return d

    async def _load_catalog(self, tenant_id: str, kb_id: str,
                            document_id: str = "") -> list[dict]:
        """读引擎页面清单并归一化为目录/前端共用的扁平结构（可按文档过滤）。"""
        from jonex_wiki.domain.reading import list_page_documents as _list_docs
        from jonex_wiki.domain.reading import list_pages as _list_pages

        pub = self._published_dir(tenant_id, kb_id)
        pages, doc_pages = await asyncio.to_thread(lambda: (_list_pages(pub), _list_docs(pub)))
        doc_slug = (
            hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:16]
            if document_id else None
        )

        items: list[dict] = []
        seen: set[str] = set()
        for p in pages:
            path = str(p.get("path") or "")
            if not path or path in seen:
                continue
            ptype = str(p.get("type") or "concept")
            if document_id and not self._belongs_to_doc(doc_pages, path, ptype, document_id, doc_slug):
                continue
            seen.add(path)
            items.append({
                "path": path,
                "title": _clean_title(p.get("title"), path),
                "type": ptype,
                "category": path.split("/", 1)[0] if "/" in path else "",
                "description": _clip(p.get("description"), MAX_DESCRIPTION_LEN),
                "aliases": [str(a) for a in (p.get("aliases") or [])][:10],
                "tags": [str(t) for t in (p.get("tags") or [])][:10],
                "why_visit": _clip(p.get("why_visit"), MAX_DESCRIPTION_LEN),
                "fields": [str(f) for f in (p.get("fields") or [])][:40],
                "scope": p.get("scope") if isinstance(p.get("scope"), dict) else {},
                "reference_count": len(doc_pages.get(path) or ()),
            })
        items.sort(key=lambda p: (p.get("category") or "", p.get("title") or ""))
        return items

    @staticmethod
    def _belongs_to_doc(doc_pages: dict, path: str, ptype: str,
                        document_id: str, doc_slug: Optional[str]) -> bool:
        """文档级过滤：source 页按 slug 匹配；table/concept 页查 claims 归属映射。"""
        if ptype == "source":
            return path == f"sources/source-{doc_slug}.md"
        return document_id in (doc_pages.get(path) or ())

    # ────────────────────────────────────────────────────────────
    # 对外三个入口：读 / 起草 / 调整
    # ────────────────────────────────────────────────────────────
    async def get_view(self, tenant_id: str, kb_id: str, document_id: str = "") -> dict:
        """读目录 + 页面清单（阅读页首屏一次拿全）。

        未生成过目录时 outline=None（前端显示「生成目录」引导），但 pages 一定
        返回。``document_id`` 非空 = 文档级阅读视图：页面清单按该文档过滤，落库
        的知识库级目录做**过滤投影**（不改库里的目录）。
        """
        if self.layout.resolve_published(tenant_id, kb_id) is None:
            return self._view_payload(kb_id, [], None)
        pages = await self._load_catalog(tenant_id, kb_id, document_id)
        async with self.session_factory() as s:
            row = await self._repo.get_active(s, tenant_id, kb_id)
        payload = await asyncio.to_thread(self._project, tenant_id, kb_id, pages, row)
        if document_id:
            scoped = scope_outline_to_pages(payload.get("outline"), {p["path"] for p in pages})
            payload["outline"] = scoped
            payload["topic_edges"] = build_topic_edges(scoped, pages)
            payload["document_id"] = document_id
            # 文档级：目录是知识库级的投影，过期/重新生成都只在知识库级有意义
            payload["stale"] = False
        return payload

    async def draft(self, tenant_id: str, kb_id: str, request: dict,
                    *, user_id: Optional[str] = None) -> dict:
        """LLM 起草目录并落库（覆盖旧 active 行，决策 4：人工「重新生成」无条件覆盖）。

        ``request["model"]`` = 平台在发起时给的编译模型（方案 B：不读引擎侧配置）。
        空/缺失 ⇒ 不传 model，由 GwClient 走部署默认链；自动起草由 outline 任务把
        编译任务里那份快照原样带进来（见 `domain/outline/task.py`）。
        """
        model = (request or {}).get("model")
        extra_instruction = (request or {}).get("extra_instruction")
        unclassified_title = (request or {}).get("unclassified_title")

        pages = await self._load_catalog(tenant_id, kb_id)
        if not pages:
            raise ValidationError("该知识库还没有编译产物，无法生成目录")

        raw = await self._call_llm_json(
            tenant_id=tenant_id, kb_id=kb_id,
            system_prompt=_DRAFT_SYSTEM_PROMPT,
            user_prompt=self._catalog_prompt(pages, extra_instruction),
            model=model, max_tokens=_draft_max_tokens(),
        )
        outline = sanitize_outline(
            raw, pages,
            unclassified_title=unclassified_title or "未归类",
            overview=_clip((raw or {}).get("overview"), MAX_DESCRIPTION_LEN),
        )
        try:
            async with self.session_factory() as s:
                row = await self._repo.save_new_active(
                    s, tenant_id=tenant_id, kb_id=kb_id,
                    source=SOURCE_DRAFT, model=model or self.settings.llm_model,
                    instruction=_clip(extra_instruction, 2000) or None,
                    page_count=len(pages),
                    catalog_fingerprint=_fingerprint(pages),
                    outline=outline,
                    catalog_snapshot=_catalog_snapshot(pages),
                    edited_by=user_id,
                )
                await s.commit()
        except IntegrityError as exc:
            raise ConflictError("目录生成并发冲突，请重试") from exc
        log.info(
            "[outline] 起草完成 kb=%s pages=%d topics=%d version=%s",
            kb_id, len(pages), len(outline.get("topics") or []), row.outline_version,
        )
        return await asyncio.to_thread(self._project, tenant_id, kb_id, pages, row)

    async def adjust(self, tenant_id: str, kb_id: str, request: dict,
                     *, user_id: Optional[str] = None) -> dict:
        """按自然语言指令调整现有目录（覆盖率护栏不过则整次失败）。"""
        instruction = (request or {}).get("instruction")
        model = (request or {}).get("model")
        unclassified_title = (request or {}).get("unclassified_title")
        if not instruction:
            raise ValidationError("调整要求（instruction）不能为空")

        pages = await self._load_catalog(tenant_id, kb_id)
        async with self.session_factory() as s:
            row = await self._repo.get_active(s, tenant_id, kb_id)
        if row is None or not (row.outline or {}).get("topics"):
            raise ValidationError("还没有生成目录，请先生成目录再调整")

        raw = await self._call_llm_json(
            tenant_id=tenant_id, kb_id=kb_id,
            system_prompt=_ADJUST_SYSTEM_PROMPT,
            user_prompt=self._adjust_prompt(pages, row.outline, instruction),
            model=model, max_tokens=_draft_max_tokens(),
        )
        outline = sanitize_outline(
            raw, pages,
            unclassified_title=unclassified_title or "未归类",
            overview=_clip((raw or {}).get("overview"), MAX_DESCRIPTION_LEN)
            or _clip((row.outline or {}).get("overview"), MAX_DESCRIPTION_LEN),
        )

        before = _covered_paths(row.outline or {})
        after = _covered_paths(outline)
        if before and len(after) < max(1, int(len(before) * ADJUST_COVERAGE_FLOOR)):
            log.warning(
                "[outline] 调整结果覆盖率异常 kb=%s before=%d after=%d，拒绝落库",
                kb_id, len(before), len(after),
            )
            raise ValidationError("调整结果丢失了过多页面，请换一种说法重试，或重新生成目录")

        try:
            async with self.session_factory() as s:
                new_row = await self._repo.save_new_active(
                    s, tenant_id=tenant_id, kb_id=kb_id,
                    source=SOURCE_ADJUST, model=model or self.settings.llm_model,
                    instruction=_clip(instruction, 2000) or None,
                    page_count=len(pages),
                    catalog_fingerprint=_fingerprint(pages),
                    outline=outline,
                    catalog_snapshot=_catalog_snapshot(pages),
                    edited_by=user_id,
                )
                await s.commit()
        except IntegrityError as exc:
            raise ConflictError("目录调整并发冲突，请重试") from exc
        log.info(
            "[outline] 调整完成 kb=%s pages=%d version=%s",
            kb_id, len(pages), new_row.outline_version,
        )
        return await asyncio.to_thread(self._project, tenant_id, kb_id, pages, new_row)

    # ────────────────────────────────────────────────────────────
    # 内部：视图 / LLM / prompt
    # ────────────────────────────────────────────────────────────
    def _merge_map(self, tenant_id: str, kb_id: str):
        """展示侧同名归并映射（无归并时为空映射）。

        目录落在 ``wiki_outlines`` 表里、**不在 bundle 内**，所以归并不能在编译
        产物里改到它；这里在读取时把指向 loser 的条目重定向到 winner。不写库，
        下一次「重新生成目录」基于归并后的页面清单起草完，重定向自然失效。
        """
        from jonex_wiki.domain.reading import merge_map
        return merge_map(self._published_dir(tenant_id, kb_id))

    def _project(self, tenant_id: str, kb_id: str, pages: list[dict], row: Any) -> dict:
        """``_view_payload`` + 同名归并投影（目录路径重定向、stale 口径、主题边）。"""
        from jonex_wiki.domain.reading import remap_outline_paths

        payload = self._view_payload(kb_id, pages, row)
        merge = self._merge_map(tenant_id, kb_id)
        if merge.is_identity():
            return payload

        payload["outline"] = remap_outline_paths(payload.get("outline"), merge.redirects)
        payload["topic_edges"] = build_topic_edges(payload.get("outline"), pages)
        payload["merged_pages"] = merge.merged_count

        # ``stale`` 必须用**归并前**的页面清单重算：归并只是展示投影，页面集合与
        # 目录的内容/顺序都没变（条目路径已被重定向），不该判「目录过期」——
        # 否则每个知识库都会白弹一次「重新生成目录」，白白花一次 LLM 起草。
        stored = getattr(row, "catalog_fingerprint", None) if row is not None else None
        if stored and payload["stale"]:
            # 新目录已按归并后的清单保存指纹；匹配时无需重建旧格式基准。
            # 基准必须复刻 ``_load_catalog`` 的口径：title 走 ``_clean_title``，
            # 否则指纹对不上，stale 会恒为 True。
            basis: list[dict] = []
            for page in pages:
                path = str(page.get("path") or "")
                basis.append({"path": path, "title": page.get("title") or ""})
                for loser in merge.cluster(path):
                    if loser != path:
                        basis.append({
                            "path": loser,
                            "title": _clean_title(merge.titles.get(loser, ""), loser),
                        })
            payload["stale"] = _fingerprint(basis) != stored
        return payload

    def _view_payload(self, kb_id: str, pages: list[dict], row: Any) -> dict:
        fingerprint = _fingerprint(pages)
        outline = (row.outline or None) if row is not None else None
        generated_at = None
        if row is not None and row.generated_at is not None:
            generated_at = row.generated_at.isoformat()
        return {
            "knowledge_base_id": kb_id,
            "kb_type": _KB_TYPE,
            "page_count": len(pages),
            "pages": pages,
            "outline": outline,
            # 主题地图的边（LLM 目录 + 页面分类/标签派生；无目录时为空数组）
            "topic_edges": build_topic_edges(outline, pages),
            "outline_version": int(getattr(row, "outline_version", 0) or 0),
            "source": getattr(row, "source", None),
            "model": getattr(row, "model", None),
            "instruction": getattr(row, "instruction", None),
            "generated_at": generated_at,
            "catalog_fingerprint": fingerprint,
            # 页面集合变了（重编译/新增文档）→ 目录过期，前端提示「重新生成」
            "stale": bool(row is not None and row.catalog_fingerprint
                          and row.catalog_fingerprint != fingerprint),
        }

    async def _call_llm_json(
        self, *, tenant_id: str, kb_id: str,
        system_prompt: str, user_prompt: str,
        model: Optional[str], max_tokens: int,
    ) -> dict:
        """调引擎 GwClient 并解析 JSON；失败抛 DomainError(503)（前端可重试）。"""
        trace_id = uuid.uuid4().hex
        try:
            resp = await self.gw.complete(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                scene=_SCENE, tenant_id=tenant_id, kb_id=kb_id,
                trace_id=trace_id, json_mode=True,
                max_tokens=max_tokens, model=model,
            )
        except Exception as exc:  # noqa: BLE001 — 上游形态多，统一转可重试错误
            log.warning("[outline] LLM 调用失败 kb=%s: %s", kb_id, exc)
            raise DomainError(
                "outline_llm_failed", "目录生成服务暂时不可用，请稍后重试", 503,
            ) from exc

        text = str(resp.get("content") or "").strip()
        parsed = _extract_json_object(text)
        if not isinstance(parsed, dict):
            log.warning(
                "[outline] LLM 返回不可解析 JSON kb=%s finish=%s 正文=%d 字符: %s",
                kb_id, resp.get("finish_reason"), len(text), text[:200],
            )
            raise DomainError(
                "outline_llm_bad_output", "目录生成结果无法解析，请重试", 503,
            )
        return parsed

    def _catalog_prompt(self, pages: list[dict],
                        extra_instruction: Optional[str]) -> str:
        lines = [
            f"页面总数：{len(pages)}",
            "",
            "页面清单（path | 标题 | 类型 | 分类 | 摘要）：",
        ]
        for page in pages[:MAX_PAGES_FOR_LLM]:
            lines.append(
                "- {path} | {title} | {role} | {category} | {desc}".format(
                    path=page.get("path", ""),
                    title=page.get("title", ""),
                    role=page.get("type", ""),
                    category=page.get("category", ""),
                    desc=_clip(page.get("description"), 60),
                )
            )
        if len(pages) > MAX_PAGES_FOR_LLM:
            lines.append(
                f"（清单已截断，仅列出前 {MAX_PAGES_FOR_LLM} 个页面；"
                "未能列出的页面将被自动归入「未归类」，不影响本次生成）"
            )
        if extra_instruction:
            lines += ["", f"额外要求：{_clip(extra_instruction, 2000)}"]
        lines += ["", "请输出目录 JSON。"]
        return "\n".join(lines)

    def _adjust_prompt(self, pages: list[dict], current: dict, instruction: str) -> str:
        lines = [
            "当前目录（JSON）：",
            json.dumps(_outline_for_llm(current), ensure_ascii=False),
            "",
            "可用页面清单（path | 标题 | 类型 | 分类）：",
        ]
        for page in pages[:MAX_PAGES_FOR_LLM]:
            lines.append(
                "- {path} | {title} | {role} | {category}".format(
                    path=page.get("path", ""),
                    title=page.get("title", ""),
                    role=page.get("type", ""),
                    category=page.get("category", ""),
                )
            )
        lines += [
            "",
            f"读者的调整要求：{_clip(instruction, 2000)}",
            "",
            "请输出调整后的完整目录 JSON（保持同样的 JSON 形状）。",
        ]
        return "\n".join(lines)
