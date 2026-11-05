"""Parse result query service for Knowledge Base."""

from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..dtos import (
    DocumentParseResultRequest,
    ParseResultDocumentListRequest,
    ParseResultEntityListRequest,
    ParseResultGraphRequest,
    ParseResultRelationshipListRequest,
    ParseResultScopeRequest,
)
from .document_service import _payload
from .kb_type_service import LLM_WIKI_TYPES  # [jonex] llm-wiki 子集 {openkb, jonex_wiki}


class ParseResultService:
    async def get_summary(self, tenant_id: str, request: ParseResultScopeRequest | dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = ParseResultScopeRequest(**_payload(request))
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        if kb_type in LLM_WIKI_TYPES:
            return await self._openkb_summary(tenant_id, req.knowledge_base_id, kb_type)
        return await get_rag_client().get_storage_summary(req.knowledge_base_id, tenant_id)

    async def list_documents(
        self,
        tenant_id: str,
        request: ParseResultDocumentListRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = ParseResultDocumentListRequest(**_payload(request))
        # ── [jonex] llm-wiki 管线：解析文档列表来自 PG（knowledge_documents），非 LightRAG ──
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        if kb_type in LLM_WIKI_TYPES:
            return await self._openkb_documents_page(
                tenant_id, req.knowledge_base_id, req.page, req.page_size,
                req.keyword, req.status, kb_type,
            )
        return await get_rag_client().get_storage_documents(
            knowledge_base_id=req.knowledge_base_id,
            tenant_id=tenant_id,
            page=req.page,
            page_size=req.page_size,
            keyword=req.keyword,
            status=req.status,
        )

    async def list_entities(
        self,
        tenant_id: str,
        request: ParseResultEntityListRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = ParseResultEntityListRequest(**_payload(request))
        # ── [jonex] llm-wiki 管线：实体来自 Wiki entities/concepts，非 LightRAG ──
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        if kb_type in LLM_WIKI_TYPES:
            graph = await self._openkb_graph(
                tenant_id, req.knowledge_base_id, document_id=req.document_id or "",
            )
            return self._openkb_entities_page(
                graph, req.page, req.page_size, req.keyword, req.entity_type, kb_type,
            )
        return await get_rag_client().get_storage_entities(
            knowledge_base_id=req.knowledge_base_id,
            tenant_id=tenant_id,
            page=req.page,
            page_size=req.page_size,
            keyword=req.keyword,
            entity_type=req.entity_type,
            file_path=req.file_path,
            document_id=req.document_id,
        )

    async def list_relationships(
        self,
        tenant_id: str,
        request: ParseResultRelationshipListRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = ParseResultRelationshipListRequest(**_payload(request))
        # ── [jonex] llm-wiki 管线：关系来自 Wiki 页面 [[wikilinks]] ──
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        if kb_type in LLM_WIKI_TYPES:
            graph = await self._openkb_graph(
                tenant_id, req.knowledge_base_id, document_id=req.document_id or "",
            )
            return self._openkb_relationships_page(
                graph, req.page, req.page_size, req.keyword,
                req.source_entity, req.target_entity, kb_type,
            )
        return await get_rag_client().get_storage_relationships(
            knowledge_base_id=req.knowledge_base_id,
            tenant_id=tenant_id,
            page=req.page,
            page_size=req.page_size,
            keyword=req.keyword,
            file_path=req.file_path,
            document_id=req.document_id,
            source_entity=req.source_entity,
            target_entity=req.target_entity,
        )

    async def get_graph_summary(
        self,
        tenant_id: str,
        request: ParseResultScopeRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = ParseResultScopeRequest(**_payload(request))
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        if kb_type in LLM_WIKI_TYPES:
            graph = await self._openkb_graph(tenant_id, req.knowledge_base_id)
            n_ent = len(graph.get("entities", []))
            n_rel = len(graph.get("relationships", []))
            return {"entities_count": n_ent, "relations_count": n_rel,
                    "relationships_count": n_rel, "kb_type": kb_type}
        return await get_rag_client().get_storage_graph_summary(req.knowledge_base_id, tenant_id)

    async def get_graph(self, tenant_id: str, request: ParseResultGraphRequest | dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = ParseResultGraphRequest(**_payload(request))
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        if kb_type in LLM_WIKI_TYPES:
            # [jonex] document_id 可选：给则文档级子图（llm-wiki 容器源头过滤）
            graph = await self._openkb_graph(
                tenant_id, req.knowledge_base_id, document_id=req.document_id or "",
            )
            return self._openkb_graph_view(graph, req.limit, req.keyword, kb_type)
        return await get_rag_client().get_storage_graph(
            knowledge_base_id=req.knowledge_base_id,
            tenant_id=tenant_id,
            limit=req.limit,
            keyword=req.keyword,
            file_path=req.file_path,
            document_id=req.document_id,
        )

    async def get_document_parse_result(
        self,
        tenant_id: str,
        request: DocumentParseResultRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = DocumentParseResultRequest(**_payload(request))
        result = await get_rag_client().get_document_parse_result(
            req.knowledge_base_id,
            tenant_id,
            document_id=req.document_id,
        )
        if isinstance(result, dict):
            result["document_id"] = req.document_id
        return result

    # ── [jonex] OpenKB 管线：实体/关系来自 OpenKB Wiki（非 LightRAG） ──

    async def _get_kb_type(self, tenant_id: str, kb_id: str) -> str:
        """[jonex] 查询 KB 的 kb_type（转调公共 helper）。"""
        from .kb_type_service import get_kb_type
        return await get_kb_type(tenant_id, kb_id)

    @staticmethod
    def _wiki_compiler(kb_type: str):
        """[jonex] 按 kb_type 返回对应的 llm-wiki 编译服务（openkb/jonex_wiki）。"""
        if kb_type == "jonex_wiki":
            from .jonex_wiki_service import JonexWikiService
            return JonexWikiService()
        from .openkb_service import KnowledgeCompilerService
        return KnowledgeCompilerService()

    async def _openkb_graph(self, tenant_id: str, kb_id: str,
                            document_id: str = "") -> dict:
        """拉取 llm-wiki 的 entities/concepts + 关系（文档级过滤可选）。异常降级为空图。

        openkb 走 KnowledgeCompilerService；jonex_wiki 走 JonexWikiService，
        引擎把 Wiki 页面与页间 links 投影为 entities/relationships。
        """
        kb_type = await self._get_kb_type(tenant_id, kb_id)
        try:
            return await self._wiki_compiler(kb_type).get_graph(
                kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
                document_id=document_id,
            ) or {}
        except Exception:
            return {"entities": [], "relationships": []}

    async def get_wiki_page(self, tenant_id: str, kb_id: str, path: str, *, actor: str = "") -> dict:
        """[jonex] llm-wiki 管线：读取 Wiki 页面内容（编译结果阅读模式）。

        页面级 References（溯源到源文档）由引擎适配器给出 document_id 与摘录；
        文档显示名/媒体类型是**平台库**信息，在这里用一次 SELECT 补齐——
        前端据此直接打开原始文档查看器（维基百科 References 的对应物）。
        """
        kb_type = await self._get_kb_type(tenant_id, kb_id)
        if kb_type not in LLM_WIKI_TYPES:
            raise InvalidParameterError(message=translate("err.kb.not_openkb", fallback="仅 LLM Wiki 知识库支持 Wiki 页面读取"))
        compiler = self._wiki_compiler(kb_type) if kb_type != 'jonex_wiki' else None
        if compiler is None:
            from jonex_core.capability.atomic.jonex_wiki.client import get_jonex_wiki_client
            compiler = get_jonex_wiki_client()
        page = await compiler.read_page(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id, path=path,
        ) or {}
        refs = page.get("references")
        if isinstance(refs, list) and refs:
            await self._attach_document_meta(tenant_id, refs)
        from .wiki_media import attach_image_urls
        attach_image_urls(page.get('images') or [], tenant_id, actor, kb_id, page.get('bundle_sha') or '')
        return page

    @staticmethod
    async def _attach_document_meta(tenant_id: str, references: list) -> None:
        """就地补齐 References 的源文档显示信息（file_name / mime_type）。

        引擎只认 document_id；文件名在平台库。查不到（文档已删除）时保留
        document_id —— 前端降级展示短 id，不隐藏这条溯源。
        """
        from sqlalchemy import select

        from jonex_core.common.database import get_db_session

        from ..models import KnowledgeDocument

        doc_ids = [
            str(r.get("document_id"))
            for r in references
            if isinstance(r, dict) and r.get("document_id")
        ]
        if not doc_ids:
            return
        try:
            async with get_db_session() as session:
                rows = (
                    await session.execute(
                        select(
                            KnowledgeDocument.id,
                            KnowledgeDocument.file_name,
                            KnowledgeDocument.mime_type,
                        ).where(
                            KnowledgeDocument.tenant_id == tenant_id,
                            KnowledgeDocument.id.in_(doc_ids),
                        )
                    )
                ).all()
        except Exception:  # noqa: BLE001 — 元数据补齐失败不影响页面阅读
            return
        meta = {row[0]: (row[1], row[2]) for row in rows}
        for ref in references:
            if not isinstance(ref, dict):
                continue
            info = meta.get(str(ref.get("document_id")))
            ref["file_name"] = info[0] if info else ""
            ref["mime_type"] = (info[1] if info else "") or ""

    async def wiki_scenario(self, tenant_id: str, request: dict, *, operation: str, actor: str = "") -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id = str(request.get("knowledge_base_id") or "")
        if await self._get_kb_type(tenant_id, kb_id) != "jonex_wiki":
            raise InvalidParameterError(message="仅 Jonex Wiki 支持场景扩展")
        options = request.get("options", {})
        if not isinstance(options, dict):
            raise InvalidParameterError(message="options 必须为对象")
        from jonex_core.capability.atomic.jonex_wiki.client import get_jonex_wiki_client
        return await get_jonex_wiki_client().scenario(tenant_id, kb_id, operation,
            str(request.get("scenario_id") or ""), options, actor=actor)

    async def query_wiki_resources(self, tenant_id: str, request: dict, *, actor: str = "") -> dict:
        kb_id = str(request.get("knowledge_base_id") or "")
        operation = str(request.get("operation") or "")
        if operation not in {"find_resources", "get_question", "lookup_records", "get_media_segment", "get_evidence", "inspect_visual", "find_images", "get_image", "inspect_image"}:
            raise InvalidParameterError(message="不支持的资源读取操作")
        if await self._get_kb_type(tenant_id, kb_id) != "jonex_wiki":
            raise InvalidParameterError(message="仅 Jonex Wiki 支持资源读取")
        options = request.get("options") or {}
        if not isinstance(options, dict):
            raise InvalidParameterError(message="options 必须为对象")
        # Resource reads use the independent Wiki port directly. Constructing
        # JonexWikiService also eagerly builds an unrelated OpenKB DB helper.
        from jonex_core.capability.atomic.jonex_wiki.client import get_jonex_wiki_client
        result = await get_jonex_wiki_client().query_resources(tenant_id, kb_id, operation, options)
        if operation in {'find_images', 'get_image'}:
            from .wiki_media import attach_image_urls
            images = result.get('items', []) if operation == 'find_images' else [result['image']]
            attach_image_urls(images, tenant_id, actor, kb_id, result['revision'])
        if operation == "get_media_segment":
            from .wiki_media import resource_media_url
            segment = result["segment"]
            result["playback"]["url"] = resource_media_url(tenant_id, kb_id, segment["document_id"],
                result["revision"], segment["object_id"])
        return result

    async def query_wiki_graph(self, tenant_id: str, operation: str, request: dict) -> dict:
        from ..dtos.wiki_graph import GRAPH_FIELDS, WikiGraphRequest
        req = WikiGraphRequest(**request)
        if await self._get_kb_type(tenant_id, req.knowledge_base_id) != "jonex_wiki":
            raise InvalidParameterError(message="仅 Jonex Wiki 知识库支持知识图谱查询")
        if operation == "media":
            from jonex_core.common.database import get_db_session
            from ..repository.document_repository import KnowledgeDocumentRepository
            async with get_db_session() as session:
                doc = await KnowledgeDocumentRepository(session).get_required(req.document_id, tenant_id)
                if doc.knowledge_base_id != req.knowledge_base_id:
                    raise InvalidParameterError(message="图片不属于指定知识库")
        payload = req.dict(exclude_none=True)
        options = {key: payload[key] for key in GRAPH_FIELDS[operation] if key in payload}
        result = await self._wiki_compiler("jonex_wiki").query_graph(
            tenant_id=tenant_id, kb_id=req.knowledge_base_id, operation=operation, **options)
        # File names belong to the platform. Enrich all returned document nodes
        # and exact evidence locations without changing their graph identities.
        records = list(result.get("items") or [])
        if isinstance(result.get("node"), dict):
            records.append(result["node"])
        records += [item["node"] for item in list(records) if isinstance(item.get("node"), dict)]
        refs = [record for record in records if record.get("document_id")]
        if refs:
            await self._attach_document_meta(tenant_id, refs)
            for record in refs:
                if record.get("node_type") == "document" and record.get("file_name"):
                    record["label"] = record["file_name"]
        return result

    async def get_wiki_backlinks(self, tenant_id: str, kb_id: str, path: str,
                                 document_id: str = "") -> dict:
        """[jonex] 「链入本页」：谁链接到了这一页（维基百科 What links here）。

        边来自引擎适配器的全库链接索引（frontmatter links + 正文首次提及，
        识别门槛与前端正文蓝链同口径），按 published mtime 缓存。
        """
        kb_type = await self._get_kb_type(tenant_id, kb_id)
        if kb_type not in LLM_WIKI_TYPES:
            raise InvalidParameterError(message=translate("err.kb.not_openkb", fallback="仅 LLM Wiki 知识库支持 Wiki 页面读取"))
        return await self._wiki_compiler(kb_type).get_wiki_backlinks(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id, path=path,
            document_id=document_id,
        ) or {}

    async def get_wiki_claims(self, tenant_id: str, kb_id: str, path: str = "",
                              document_id: str = "", limit: int = 500) -> dict:
        """[jonex] claim 级溯源明细（列式视图「溯源审计」）。

        与页面级 References 的分工：References 回答「这页证据来自哪几份文档」，
        claim 明细回答「哪一条断言、原文哪一段」，并补上来源文档显示名。
        """
        kb_type = await self._get_kb_type(tenant_id, kb_id)
        if kb_type not in LLM_WIKI_TYPES:
            raise InvalidParameterError(message=translate("err.kb.not_openkb", fallback="仅 LLM Wiki 知识库支持 Wiki 页面读取"))
        result = await self._wiki_compiler(kb_type).get_wiki_claims(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id, path=path,
            document_id=document_id, limit=limit,
        ) or {}
        items = result.get("items")
        if isinstance(items, list) and items:
            await self._attach_claim_document_meta(tenant_id, items)
        return result

    @staticmethod
    async def _attach_claim_document_meta(tenant_id: str, claims: list) -> None:
        """给 claim 明细补来源文档显示名（与 References 同一套平台库信息）。"""
        from sqlalchemy import select

        from jonex_core.common.database import get_db_session

        from ..models import KnowledgeDocument

        doc_ids = sorted({
            str(doc_id)
            for claim in claims if isinstance(claim, dict)
            for doc_id in (claim.get("document_ids") or [])
            if doc_id
        })
        if not doc_ids:
            return
        try:
            async with get_db_session() as session:
                rows = (
                    await session.execute(
                        select(
                            KnowledgeDocument.id,
                            KnowledgeDocument.file_name,
                            KnowledgeDocument.mime_type,
                        ).where(
                            KnowledgeDocument.tenant_id == tenant_id,
                            KnowledgeDocument.id.in_(doc_ids),
                        )
                    )
                ).all()
        except Exception:  # noqa: BLE001 — 元数据补齐失败不影响审计表
            return
        meta = {row[0]: (row[1], row[2]) for row in rows}
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            docs = []
            for doc_id in (claim.get("document_ids") or []):
                name, mime = meta.get(str(doc_id), ("", ""))
                docs.append({
                    "document_id": str(doc_id),
                    "file_name": name or "",
                    "mime_type": mime or "",
                })
            claim["documents"] = docs

    async def get_wiki_contents(self, tenant_id: str, kb_id: str,
                                document_id: str = "", view: str = "") -> dict:
        """[jonex] llm-wiki 管线：Wiki 页面树（文档级过滤可选）。

        view="counts"：只返回三类页数，不返回页面条目（编译资产 tab 用）。
        """
        kb_type = await self._get_kb_type(tenant_id, kb_id)
        if kb_type not in LLM_WIKI_TYPES:
            raise InvalidParameterError(message=translate("err.kb.not_openkb", fallback="仅 LLM Wiki 知识库支持 Wiki 页面树"))
        return await self._wiki_compiler(kb_type).list_wiki_contents(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id, view=view,
        ) or {}

    async def compilation_state(self, tenant_id: str, kb_id: str, paused=None) -> dict:
        if await self._get_kb_type(tenant_id, kb_id) != "jonex_wiki":
            raise InvalidParameterError(message="仅 Jonex Wiki 支持编译暂停和恢复")
        from jonex_core.capability.atomic.jonex_wiki.client import get_jonex_wiki_client
        from sqlalchemy import select, func
        from jonex_core.common.database import get_db_session
        from ..models import KnowledgeDocument
        state = await get_jonex_wiki_client().compilation_state(tenant_id, kb_id, paused)
        async with get_db_session() as session:
            counts = (await session.execute(select(
                KnowledgeDocument.status, KnowledgeDocument.llm_wiki_compile_status, func.count()
            ).where(KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.knowledge_base_id == kb_id,
                    KnowledgeDocument.is_deleted == 0).group_by(
                KnowledgeDocument.status, KnowledgeDocument.llm_wiki_compile_status))).all()
        state["pending_source_count"] = sum(n for status, _, n in counts
                                            if status in {"pending", "parsing", "ingesting"})
        state["waiting_source_count"] = sum(n for status, compile_status, n in counts
                                            if status == "ready" and compile_status in {"stale", "pending", None, ""})
        state["failed_source_count"] = sum(n for status, compile_status, n in counts
                                           if status == "failed" or compile_status == "failed")
        return state

    @staticmethod
    def _page_slice(items: list, page: int, page_size: int) -> tuple[list, int]:
        total = len(items)
        page = max(1, int(page or 1))
        size = max(1, int(page_size or 20))
        start = (page - 1) * size
        return items[start:start + size], total

    def _openkb_entities_page(self, graph, page, page_size, keyword, entity_type, kb_type: str) -> dict:
        from collections import Counter
        ents = graph.get("entities", []) or []
        rels = graph.get("relationships", []) or []
        deg: Counter = Counter()
        for r in rels:
            deg[r.get("source", "")] += 1
            deg[r.get("target", "")] += 1
        kw = (keyword or "").lower()
        mapped = []
        for e in ents:
            name = e.get("name", "")
            if entity_type and e.get("type") != entity_type:
                continue
            if kw and kw not in f"{name} {e.get('description','')}".lower():
                continue
            mapped.append({
                # [jonex] id 保持 slug（关系/图按 id 匹配），name 用显示名（正文 H1 中文标题）
                "id": e.get("id") or name,
                "name": name,
                "type": e.get("type", ""),
                "description": (e.get("description", "") or "")[:300],
                "source_id": "",
                "file_path": "",
                "created_at": None,
                "relations_count": deg.get(e.get("id") or name, 0),
            })
        items, total = self._page_slice(mapped, page, page_size)
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "scope_mode": "knowledge_base", "scope_warning": None, "kb_type": kb_type}

    def _openkb_relationships_page(self, graph, page, page_size, keyword, source_entity, target_entity, kb_type: str) -> dict:
        rels = graph.get("relationships", []) or []
        # [jonex] 语言适配：关系的 source/target 底层是 slug（图按 id 匹配），
        # 列表展示用实体显示名（正文 H1 中文标题），无映射则退回 slug。
        ents = graph.get("entities", []) or []
        name_map = {str(e.get("id") or ""): e.get("name", "") for e in ents}
        kw = (keyword or "").lower()
        mapped = []
        for r in rels:
            src_slug, tgt_slug = r.get("source", ""), r.get("target", "")
            src = name_map.get(src_slug, src_slug)
            tgt = name_map.get(tgt_slug, tgt_slug)
            if source_entity and src != source_entity and src_slug != source_entity:
                continue
            if target_entity and tgt != target_entity and tgt_slug != target_entity:
                continue
            if kw and kw not in f"{src} {tgt} {src_slug} {tgt_slug}".lower():
                continue
            mapped.append({
                "id": f"{src_slug}->{tgt_slug}",
                "source_entity": src,
                "target_entity": tgt,
                "description": "",
                "source_id": "",
                "file_path": "",
                "created_at": None,
            })
        items, total = self._page_slice(mapped, page, page_size)
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "scope_mode": "knowledge_base", "scope_warning": None, "kb_type": kb_type}

    def _openkb_graph_view(self, graph, limit, keyword, kb_type: str) -> dict:
        ents = graph.get("entities", []) or []
        rels = graph.get("relationships", []) or []
        kw = (keyword or "").lower()
        if kw:
            ents = [e for e in ents if kw in f"{e.get('name','')} {e.get('id','')} {e.get('description','')}".lower()]
        lim = int(limit or 100)
        ents = ents[:lim]
        # [jonex] 语言适配：节点 id 保持 slug（G6 边 source/target 按 id 匹配），
        # name 用显示名（正文 H1 中文标题）。
        keep = {e.get("id") or e.get("name", "") for e in ents}
        nodes = [{
            "id": e.get("id") or e.get("name", ""), "name": e.get("name", ""),
            "type": e.get("type", ""), "description": (e.get("description", "") or "")[:300],
        } for e in ents]
        edges = [{
            "id": f"{r.get('source','')}->{r.get('target','')}",
            "source": r.get("source", ""), "target": r.get("target", ""),
            "source_entity": r.get("source", ""), "target_entity": r.get("target", ""),
        } for r in rels if r.get("source") in keep and r.get("target") in keep]
        return {"nodes": nodes, "edges": edges, "entities": nodes, "relationships": edges,
                "kb_type": kb_type}

    async def _openkb_docs(self, tenant_id: str, kb_id: str) -> list[dict]:
        """从 PG 取该 KB 的文档（openkb 文档权威来源）。"""
        from sqlalchemy import select
        from jonex_core.common.database import get_db_session
        from ..models.document import KnowledgeDocument
        async with get_db_session() as session:
            rows = (await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.knowledge_base_id == kb_id,
                    KnowledgeDocument.is_deleted == 0,
                )
            )).scalars().all()
        return [d.to_dict() for d in rows]

    async def _openkb_summary(self, tenant_id: str, kb_id: str, kb_type: str) -> dict:
        docs = await self._openkb_docs(tenant_id, kb_id)
        graph = await self._openkb_graph(tenant_id, kb_id)
        total = len(docs)
        compiled = sum(
            1 for d in docs
            if d.get("llm_wiki_compile_status") == "compiled"
        )
        failed = sum(
            1 for d in docs
            if d.get("status") == "failed"
            or d.get("llm_wiki_compile_status") == "failed"
        )
        # [jonex] stale（已触发重解析、旧 Wiki 过期）与 compiling 归入"处理中"，
        # 否则总览 processed+failed 之和小于 documents_count，看起来像丢了文档。
        processing = sum(
            1 for d in docs
            if d.get("llm_wiki_compile_status") in ("stale", "compiling")
        )
        last = max((d.get("updated_at") or "" for d in docs), default="") or None
        return {
            "knowledge_base_id": kb_id,
            "tenant_id": tenant_id,
            "source": kb_type,
            "scope_mode": "knowledge_base",
            "scope_warning": None,
            "status": "processed",
            "documents_count": total,
            "processed_documents_count": compiled,
            "failed_documents_count": failed,
            "processing_documents_count": processing,
            "chunks_count": 0,
            "entities_count": len(graph.get("entities", []) or []),
            "relationships_count": len(graph.get("relationships", []) or []),
            "compile_versions_count": 0,
            "last_updated_at": last,
            "storage_files": {},
            "kb_type": kb_type,
        }

    async def _openkb_documents_page(self, tenant_id, kb_id, page, page_size, keyword, status, kb_type: str) -> dict:
        docs = await self._openkb_docs(tenant_id, kb_id)
        kw = (keyword or "").lower()
        items = []
        for d in docs:
            meta = d.get("metadata") or {}
            if status and d.get("status") != status:
                continue
            if kw and kw not in (d.get("file_name", "") or "").lower():
                continue
            items.append({
                "id": d.get("id"),
                "business_document_id": d.get("id"),
                "file_name": d.get("file_name", ""),
                "file_path": d.get("file_path", ""),
                "status": d.get("status", "unknown"),
                "chunks_count": 0,
                "content_length": 0,
                "content_summary": "",
                "error_msg": d.get("error_message") or meta.get("openkb_compile_error", "") or "",
                "created_at": d.get("created_at") or "",
                "updated_at": d.get("updated_at") or "",
                "multimodal_processed": False,
                "llm_wiki_compile_status": d.get("llm_wiki_compile_status") or "",
            })
        items.sort(key=lambda x: x.get("updated_at", "") or x.get("created_at", ""), reverse=True)
        page_items, total = self._page_slice(items, page, page_size)
        return {
            "documents": page_items,
            "items": page_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "scope_mode": "knowledge_base",
            "scope_warning": None,
            "kb_type": kb_type,
        }


__all__ = ["ParseResultService"]
