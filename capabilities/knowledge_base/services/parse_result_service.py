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


class ParseResultService:
    async def get_summary(self, tenant_id: str, request: ParseResultScopeRequest | dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = ParseResultScopeRequest(**_payload(request))
        if await self._get_kb_type(tenant_id, req.knowledge_base_id) == "openkb":
            return await self._openkb_summary(tenant_id, req.knowledge_base_id)
        return await get_rag_client().get_storage_summary(req.knowledge_base_id, tenant_id)

    async def list_documents(
        self,
        tenant_id: str,
        request: ParseResultDocumentListRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = ParseResultDocumentListRequest(**_payload(request))
        # ── [jonex] OpenKB 管线：解析文档列表来自 PG（knowledge_documents），非 LightRAG ──
        if await self._get_kb_type(tenant_id, req.knowledge_base_id) == "openkb":
            return await self._openkb_documents_page(
                tenant_id, req.knowledge_base_id, req.page, req.page_size,
                req.keyword, req.status,
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
        # ── [jonex] OpenKB 管线：实体来自 Wiki entities/concepts，非 LightRAG ──
        if await self._get_kb_type(tenant_id, req.knowledge_base_id) == "openkb":
            graph = await self._openkb_graph(
                tenant_id, req.knowledge_base_id, document_id=req.document_id or "",
            )
            return self._openkb_entities_page(
                graph, req.page, req.page_size, req.keyword, req.entity_type,
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
        # ── [jonex] OpenKB 管线：关系来自 Wiki 页面 [[wikilinks]] ──
        if await self._get_kb_type(tenant_id, req.knowledge_base_id) == "openkb":
            graph = await self._openkb_graph(
                tenant_id, req.knowledge_base_id, document_id=req.document_id or "",
            )
            return self._openkb_relationships_page(
                graph, req.page, req.page_size, req.keyword,
                req.source_entity, req.target_entity,
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
        if await self._get_kb_type(tenant_id, req.knowledge_base_id) == "openkb":
            graph = await self._openkb_graph(tenant_id, req.knowledge_base_id)
            n_ent = len(graph.get("entities", []))
            n_rel = len(graph.get("relationships", []))
            return {"entities_count": n_ent, "relations_count": n_rel,
                    "relationships_count": n_rel, "kb_type": "openkb"}
        return await get_rag_client().get_storage_graph_summary(req.knowledge_base_id, tenant_id)

    async def get_graph(self, tenant_id: str, request: ParseResultGraphRequest | dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = ParseResultGraphRequest(**_payload(request))
        if await self._get_kb_type(tenant_id, req.knowledge_base_id) == "openkb":
            # [jonex] document_id 可选：给则文档级子图（OpenKB 容器源头过滤）
            graph = await self._openkb_graph(
                tenant_id, req.knowledge_base_id, document_id=req.document_id or "",
            )
            return self._openkb_graph_view(graph, req.limit, req.keyword)
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

    async def _openkb_graph(self, tenant_id: str, kb_id: str,
                            document_id: str = "") -> dict:
        """拉取 OpenKB Wiki 的 entities/concepts + 关系（文档级过滤可选）。异常降级为空图。"""
        from .openkb_service import KnowledgeCompilerService
        try:
            return await KnowledgeCompilerService().get_graph(
                kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
                document_id=document_id,
            ) or {}
        except Exception:
            return {"entities": [], "relationships": []}

    async def get_wiki_page(self, tenant_id: str, kb_id: str, path: str) -> dict:
        """[jonex] openkb 管线：读取 Wiki 页面内容（编译结果阅读模式）。"""
        if await self._get_kb_type(tenant_id, kb_id) != "openkb":
            raise InvalidParameterError(message=translate("err.kb.not_openkb", fallback="仅 LLM Wiki 知识库支持 Wiki 页面读取"))
        from .openkb_service import KnowledgeCompilerService
        return await KnowledgeCompilerService().read_page(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id, path=path,
        ) or {}

    async def get_wiki_contents(self, tenant_id: str, kb_id: str,
                                document_id: str = "") -> dict:
        """[jonex] openkb 管线：Wiki 页面树（文档级过滤可选）。"""
        if await self._get_kb_type(tenant_id, kb_id) != "openkb":
            raise InvalidParameterError(message=translate("err.kb.not_openkb", fallback="仅 LLM Wiki 知识库支持 Wiki 页面树"))
        from .openkb_service import KnowledgeCompilerService
        return await KnowledgeCompilerService().list_wiki_contents(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id,
        ) or {}

    @staticmethod
    def _page_slice(items: list, page: int, page_size: int) -> tuple[list, int]:
        total = len(items)
        page = max(1, int(page or 1))
        size = max(1, int(page_size or 20))
        start = (page - 1) * size
        return items[start:start + size], total

    def _openkb_entities_page(self, graph, page, page_size, keyword, entity_type) -> dict:
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
                "scope_mode": "knowledge_base", "scope_warning": None, "kb_type": "openkb"}

    def _openkb_relationships_page(self, graph, page, page_size, keyword, source_entity, target_entity) -> dict:
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
                "scope_mode": "knowledge_base", "scope_warning": None, "kb_type": "openkb"}

    def _openkb_graph_view(self, graph, limit, keyword) -> dict:
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
                "kb_type": "openkb"}

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

    async def _openkb_summary(self, tenant_id: str, kb_id: str) -> dict:
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
            "source": "openkb",
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
            "kb_type": "openkb",
        }

    async def _openkb_documents_page(self, tenant_id, kb_id, page, page_size, keyword, status) -> dict:
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
            "kb_type": "openkb",
        }


__all__ = ["ParseResultService"]
