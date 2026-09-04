#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
RAG Client 抽象 + 工厂

业务/领域代码统一通过 `get_rag_client()` 获取 RAGClient，不再 new 具体适配器。
- LOCAL：进程内直接调用本地 LightRAG 适配器
- REMOTE：通过 Sidecar 反代调用独立 atomic-rag 服务
- MOCK：离线/测试桩，不依赖任何外部资源

替换实现（LightRAG → Milvus + FAISS）只需扩展 LocalRAGClient.factory 或修改清单 endpoint。
"""

from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from jonex_core.capability.locator import CapabilityMode, get_locator
from jonex_core.common import (
    CapabilityInvokeError,
    InvalidParameterError,
    get_config,
    get_logger,
    require_tenant,
)
from jonex_core.common.i18n import translate

logger = get_logger("capability.client.rag")

# 当前使用的 capability_id（清单中可覆盖）
RAG_CAPABILITY_ID = "atomic.rag.lightrag.v1"


def require_knowledge_base(knowledge_base_id: Optional[str]) -> str:
    return (knowledge_base_id or "").strip()


class RAGClient(ABC):
    """RAG 客户端契约：领域/业务代码只依赖此接口"""

    @abstractmethod
    async def insert(
        self,
        file_path: str,
        tenant_id: str,
        output_dir: Optional[str] = None,
        *,
        knowledge_base_id: str,
        document_id: Optional[str] = None,
        ontology_schema: Optional[dict] = None,
        storage_backend: str = "local",
        storage_key: Optional[str] = None,
        preset: Optional[str] = None,
        prompt_ids: Optional[list] = None,
        execution_mode: str = "full",
    ) -> dict:
        """插入文档到 RAG 索引，立即返回 task_id

        Args:
            ontology_schema: per-KB compiled schema（可选，push 模式）
            storage_backend: 存储后端（"local" | "cos" | "s3"），P3
            storage_key: 对象存储键（storage_backend 为 "cos"/"s3" 时使用）

        Returns:
            {"task_id": str, "status": "pending", "file_path": str}
        """
        pass

    @abstractmethod
    async def query(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",          # [jonex] 计量链路追踪
        user_id: str = "",           # [jonex] 计量上下文
        only_need_context: bool = False,  # [jonex] 方案 A：只召回不生成
    ) -> str:
        """RAG 查询，返回回答字符串（不含引用信息）

        若需引用信息请使用 query_detailed()。
        """
        pass

    @abstractmethod
    async def query_detailed(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",
        user_id: str = "",           # [jonex] 计量上下文
        only_need_context: bool = False,  # [jonex] 方案 A：只召回不生成
    ) -> dict:
        """RAG 查询，返回 {"answer": str, "references": list[dict]} 详细结果。

        references 中的每项为 parse_file_source 解析后的结构化引用片段。

        only_need_context=True 时 LightRAG 提前返回 chunk 上下文、不调生成 LLM，
        此时 answer 字段是上下文字符串而非答案（调用方需自行作答）。
        """
        pass

    @abstractmethod
    async def delete(
        self,
        doc_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        """删除文档，返回是否成功。knowledge_base_id 用于定位 LightRAG workspace。"""
        pass

    @abstractmethod
    async def delete_orphan_chunk(
        self,
        chunk_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        """[jonex] O7：按 chunk_id 强制清理孤儿 chunk（doc_status 无记录的
        reparse 残留），不依赖 doc 级删除。"""
        pass

    @abstractmethod
    async def delete_batch(
        self,
        doc_ids: list[str],
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
        document_id: str = "",
        trace_id: str = "",
    ) -> dict:
        """[jonex] 批量删除文档——一次请求提交全部 doc_ids。

        LightRAG 在单个 background_delete_documents 任务内逐 doc 删除，
        批末仅执行一次 rebuild_knowledge_from_chunks，将 N×rebuild 降为 1×rebuild，
        避免逐条删除时每个 doc 各自触发 LLM summarization。

        Args:
            document_id: [jonex] R4 计量归因——透传 KB 侧文档 ID，
                         使删除/rebuild 的 LLM 调用能标注到具体文档（X-Jonex-Doc-Id）。
            trace_id: [jonex] R4 链路追踪——透传调用链 trace_id（X-Jonex-Trace-Id）。

        Returns:
            {"success": bool, "accepted": [...], "failed": [...]}
        """
        pass

    @abstractmethod
    async def get_task_status(
        self,
        task_id: str,
        tenant_id: str,
    ) -> dict:
        """查询异步任务状态

        Returns:
            {
                "task_id": str,
                "status": "pending" | "processing" | "completed" | "failed",
                "progress": float,
                "error": str | None
            }
        """
        pass

    # ── storage reader methods ──────────────────────────────────

    @abstractmethod
    async def get_storage_summary(
        self,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        """获取知识库存储摘要统计"""
        pass

    @abstractmethod
    async def get_storage_documents(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        """获取知识库文档列表（分页）"""
        pass

    @abstractmethod
    async def get_storage_entities(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        entity_type: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        """获取知识库实体列表（分页）"""
        pass

    @abstractmethod
    async def get_storage_relationships(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
        source_entity: Optional[str] = None,
        target_entity: Optional[str] = None,
    ) -> dict:
        """获取知识库关系列表（分页）"""
        pass

    @abstractmethod
    async def get_storage_graph_summary(
        self,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        """获取知识图谱摘要统计"""
        pass

    @abstractmethod
    async def get_storage_graph(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        limit: int = 200,
        keyword: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        """获取知识图谱节点与边数据"""
        pass

    @abstractmethod
    async def get_document_parse_result(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        document_id: Optional[str] = None,
    ) -> dict:
        """获取文档解析完整结果（聚合 summary + documents + entities + relationships）"""
        pass

    @abstractmethod
    async def retry_ontology_extract(
        self,
        document_id: str,
        knowledge_base_id: str,
        tenant_id: str = "default",
        file_path: str = "",
        *,
        ontology_schema: Optional[dict] = None,
        schema_version: int = 0,
        schema_hash: str = "",
        force_retry: bool = False,
        content_generation: int = 0,
    ) -> dict:
        """重新触发文档的本体抽取（ontology-only 模式）"""
        pass

    # ── 提示词配置 CRUD（KB 主解析提示词联动）──
    @abstractmethod
    async def create_prompt(
        self,
        tenant_id: str,
        *,
        prompt_code: str,
        content: str,
        preset_name: str = "",
        display_name: str = "",
        description: str = "",
        category: str = "analysis",
        language: str = "zh",
    ) -> dict:
        """创建租户级 prompt 配置，返回含 id 的记录。"""
        pass

    @abstractmethod
    async def update_prompt(
        self, tenant_id: str, prompt_id: str, *, content: Optional[str] = None, **fields
    ) -> dict:
        """更新 prompt 配置（一般只传 content）。"""
        pass

    @abstractmethod
    async def delete_prompt(self, tenant_id: str, prompt_id: str) -> dict:
        """删除 prompt 配置（幂等，不存在也成功）。"""
        pass

    @abstractmethod
    async def get_prompt(self, tenant_id: str, prompt_id: str) -> Optional[dict]:
        """按 id 查 prompt 配置；不存在返回 None。"""
        pass


# ============================================================
# Local：直连进程内适配器
# ============================================================
class LocalRAGClient(RAGClient):
    """直连本地 LightRAG 适配器（仅 atomic-rag 容器内可用）"""

    def __init__(self, options: Optional[Dict[str, Any]] = None) -> None:
        # 延迟导入避免在 REMOTE/MOCK 模式下加载重依赖
        # [jonex] v1 退役：唯一 adapter 即 v2（ATOMIC_RAG_VERSION 开关已删除）
        from jonex_core.capability.atomic.rag.lightrag_adapter_v2 import LightRAGAdapterV2

        self._adapter = LightRAGAdapterV2()
        self._options = options or {}
        logger.info("RAG Client 初始化：LOCAL 模式（v2 adapter）")

    async def _ensure_initialized(self):
        """延迟初始化适配器；v2 的 initialize() 自带幂等（_started 标志）"""
        await self._adapter.initialize()

    async def insert(
        self,
        file_path: str,
        tenant_id: str,
        output_dir: Optional[str] = None,
        *,
        knowledge_base_id: str,
        document_id: Optional[str] = None,
        ontology_schema: Optional[dict] = None,
        storage_backend: str = "local",
        storage_key: Optional[str] = None,
        preset: Optional[str] = None,
        prompt_ids: Optional[list] = None,
        execution_mode: str = "full",
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        await self._ensure_initialized()
        _extra = {"preset": preset} if preset is not None else {}
        if prompt_ids:
            _extra["prompt_ids"] = prompt_ids
        if execution_mode and execution_mode != "full":
            _extra["execution_mode"] = execution_mode
        return await self._adapter.insert(
            file_path=file_path,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            output_dir=output_dir,
            document_id=document_id,
            ontology_schema=ontology_schema,
            storage_backend=storage_backend,
            storage_key=storage_key or file_path,
            **_extra,
        )

    async def query(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",          # [jonex] 计量链路追踪
        user_id: str = "",           # [jonex] 计量上下文
        only_need_context: bool = False,  # [jonex] 方案 A：只召回不生成
    ) -> str:
        tenant_id = require_tenant(tenant_id)
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        await self._ensure_initialized()
        return await self._adapter.query(
            query,
            tenant_id,
            mode,
            top_k,
            knowledge_base_id=knowledge_base_id,
            trace_id=trace_id,          # [jonex] 计量链路追踪
            user_id=user_id,            # [jonex] 计量上下文
            only_need_context=only_need_context,
        )

    async def query_detailed(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",
        user_id: str = "",           # [jonex] 计量上下文
        only_need_context: bool = False,  # [jonex] 方案 A：只召回不生成
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        await self._ensure_initialized()
        return await self._adapter.query_detailed(
            query,
            tenant_id,
            mode,
            top_k,
            knowledge_base_id=knowledge_base_id,
            trace_id=trace_id,
            user_id=user_id,            # [jonex] 计量上下文
            only_need_context=only_need_context,
        )

    async def delete(
        self,
        doc_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        tenant_id = require_tenant(tenant_id)
        await self._ensure_initialized()
        return await self._adapter.delete(
            doc_id, tenant_id, knowledge_base_id=knowledge_base_id
        )

    async def delete_orphan_chunk(
        self,
        chunk_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        tenant_id = require_tenant(tenant_id)
        await self._ensure_initialized()
        return await self._adapter.delete_orphan_chunk(
            chunk_id, tenant_id, knowledge_base_id=knowledge_base_id
        )

    async def delete_batch(
        self,
        doc_ids: list[str],
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
        document_id: str = "",
        trace_id: str = "",
    ) -> dict:
        """[jonex] 批量删除——v1 退役后 LOCAL 面优先走 v2 adapter 的 delete_batch
        （单次 rebuild）；adapter 不可用/调用失败时逐条 delete 回退。"""
        tenant_id = require_tenant(tenant_id)
        await self._ensure_initialized()
        try:
            return await self._adapter.delete_batch(
                doc_ids, tenant_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id, trace_id=trace_id,
            )
        except Exception:
            logger.warning("LocalRAGClient delete_batch 批量调用失败，回退逐条删除", exc_info=True)
        accepted = []
        failed = []
        for doc_id in doc_ids:
            try:
                ok = await self._adapter.delete(
                    doc_id, tenant_id, knowledge_base_id=knowledge_base_id
                )
                if ok:
                    accepted.append(doc_id)
                else:
                    failed.append(doc_id)
            except Exception:
                logger.warning("LocalRAGClient delete_batch failed for %s", doc_id, exc_info=True)
                failed.append(doc_id)
        return {"success": len(failed) == 0, "accepted": accepted, "failed": failed}

    async def get_task_status(
        self,
        task_id: str,
        tenant_id: str,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        return await self._adapter.get_task_status(task_id, tenant_id)

    # ── storage reader delegations ──────────────────────────

    def _build_scope(self, knowledge_base_id: str, tenant_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        return {
            "knowledge_base_id": knowledge_base_id,
            "tenant_id": tenant_id,
            "scope_mode": "knowledge_base",
        }

    # [jonex] v1 退役：storage 委托从 _reader() 改为直调 v2 adapter 同名方法。
    # v2 侧无 _reader 子对象（读操作经 dispatch 到 vendored storage handler）。

    async def get_storage_summary(
        self,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        await self._ensure_initialized()
        return await self._adapter.get_storage_summary(
            tenant_id, knowledge_base_id=knowledge_base_id
        ) or {}

    async def get_storage_documents(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        await self._ensure_initialized()
        return await self._adapter.get_storage_documents(
            tenant_id, knowledge_base_id=knowledge_base_id,
            page=page, page_size=page_size, keyword=keyword or "", status=status or "",
        ) or {}

    async def get_storage_entities(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        entity_type: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        await self._ensure_initialized()
        return await self._adapter.get_storage_entities(
            tenant_id, knowledge_base_id=knowledge_base_id,
            page=page, page_size=page_size, keyword=keyword or "",
            entity_type=entity_type or "", doc_id=document_id or "",
            file_path=file_path or "",
        ) or {}

    async def get_storage_relationships(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
        source_entity: Optional[str] = None,
        target_entity: Optional[str] = None,
    ) -> dict:
        # [jonex] v1 退役：vendored storage handler 暂不支持 source/target 实体筛选，
        # LOCAL 面忽略这两个参数（开发环境专用，生产走 REMOTE）。
        await self._ensure_initialized()
        return await self._adapter.get_storage_relationships(
            tenant_id, knowledge_base_id=knowledge_base_id,
            page=page, page_size=page_size, keyword=keyword or "",
            file_path=file_path or "", doc_id=document_id or "",
        ) or {}

    async def get_storage_graph_summary(
        self,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        await self._ensure_initialized()
        return await self._adapter.get_storage_graph_summary(
            tenant_id, knowledge_base_id=knowledge_base_id
        ) or {}

    async def get_storage_graph(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        limit: int = 200,
        keyword: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        await self._ensure_initialized()
        return await self._adapter.get_storage_graph(
            tenant_id, knowledge_base_id=knowledge_base_id,
            limit=limit, keyword=keyword or "",
            doc_id=document_id or "", file_path=file_path or "",
        ) or {}

    async def get_document_parse_result(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        document_id: Optional[str] = None,
    ) -> dict:
        await self._ensure_initialized()
        return await self._adapter.get_document_parse_result(
            tenant_id, knowledge_base_id=knowledge_base_id,
            document_id=document_id or "",
        ) or {}

    async def retry_ontology_extract(
        self,
        document_id: str,
        knowledge_base_id: str,
        tenant_id: str = "default",
        file_path: str = "",
        *,
        ontology_schema: Optional[dict] = None,
        schema_version: int = 0,
        schema_hash: str = "",
        force_retry: bool = False,
        content_generation: int = 0,
    ) -> dict:
        await self._ensure_initialized()
        # [jonex] v1 退役：v2 adapter 形参序为 (document_id, tenant_id, *, knowledge_base_id)，
        # 位置传参会把 knowledge_base_id 错位到 tenant_id —— 必须关键字传参。
        # schema_version / schema_hash / force_retry / content_generation 为 v1 独有控制项，
        # v2 无对应透传，LOCAL 面忽略（开发环境专用，生产走 REMOTE）。
        return await self._adapter.retry_ontology_extract(
            document_id, tenant_id,
            knowledge_base_id=knowledge_base_id, file_path=file_path,
        ) or {}

    # ── 提示词配置 CRUD：[jonex] v1 退役后 LOCAL 面委托 v2 adapter 薄转发 ──
    async def create_prompt(self, tenant_id: str, *, prompt_code: str, content: str,
                            preset_name: str = "", display_name: str = "",
                            description: str = "", category: str = "analysis",
                            language: str = "zh") -> dict:
        await self._ensure_initialized()
        return await self._adapter.create_prompt(
            tenant_id, prompt_code=prompt_code, content=content,
            preset_name=preset_name, display_name=display_name,
            description=description, category=category, language=language,
        )

    async def update_prompt(self, tenant_id: str, prompt_id: str, *,
                            content: Optional[str] = None, **fields) -> dict:
        await self._ensure_initialized()
        return await self._adapter.update_prompt(
            tenant_id, prompt_id, content=content, **fields,
        )

    async def delete_prompt(self, tenant_id: str, prompt_id: str) -> dict:
        await self._ensure_initialized()
        return await self._adapter.delete_prompt(tenant_id, prompt_id)

    async def get_prompt(self, tenant_id: str, prompt_id: str) -> Optional[dict]:
        await self._ensure_initialized()
        return await self._adapter.get_prompt(tenant_id, prompt_id)


# ============================================================
# Remote：通过 Sidecar 反代
# ============================================================
class RemoteRAGClient(RAGClient):
    """通过 Sidecar 反代调用 atomic-rag 服务（业务层用这个）"""

    def __init__(
        self,
        endpoint: str,
        capability_id: str = RAG_CAPABILITY_ID,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        import httpx

        from jonex_core.common.config import get_config

        timeout = float(os.getenv("RAG_CLIENT_TIMEOUT", "120"))
        self._client = httpx.AsyncClient(
            base_url=endpoint.rstrip("/"),
            headers={"X-API-Key": get_config().GATEWAY_API_KEY},
            timeout=(options or {}).get("timeout", timeout),
        )
        self._capability_id = capability_id
        logger.info(f"RAG Client 初始化：REMOTE 模式，endpoint={endpoint}")

    def _build_scope(self, knowledge_base_id: str, tenant_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        return {
            "knowledge_base_id": knowledge_base_id,
            "tenant_id": tenant_id,
            "scope_mode": "knowledge_base",
        }

    async def insert(
        self,
        file_path: str,
        tenant_id: str,
        output_dir: Optional[str] = None,
        *,
        knowledge_base_id: str,
        document_id: Optional[str] = None,
        ontology_schema: Optional[dict] = None,
        storage_backend: str = "local",
        storage_key: Optional[str] = None,
        preset: Optional[str] = None,
        prompt_ids: Optional[list] = None,
        schema_version: int = 0,
        schema_hash: str = "",
        execution_mode: str = "full",
        idempotency_key: Optional[str] = None,  # [jonex] R1
    ) -> dict:
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        payload: dict = {
            "action": "insert",
            "file_path": file_path,
            "output_dir": output_dir,
            "knowledge_base_id": knowledge_base_id,
            "storage_backend": storage_backend,
            "storage_key": storage_key or file_path,
            # [jonex] P1-E：携带 schema 版本，供对账写图前 fencing
            "schema_version": schema_version,
            # [jonex] OpenKB：parse_only 让 atomic-rag 只解析产出 markdown，不写 LightRAG/Neo4j
            "execution_mode": execution_mode,
        }
        if schema_hash:
            payload["schema_hash"] = schema_hash
        if document_id:
            payload["document_id"] = document_id
        if ontology_schema:
            payload["ontology_schema"] = ontology_schema
        if preset:
            payload["preset"] = preset
        if prompt_ids:
            payload["prompt_ids"] = prompt_ids
        if idempotency_key:                        # [jonex] R1
            payload["idempotency_key"] = idempotency_key
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def retry(
        self,
        file_path: str,
        tenant_id: str,
        output_dir: Optional[str] = None,
        *,
        knowledge_base_id: str,
        document_id: Optional[str] = None,
        ontology_schema: Optional[dict] = None,
        storage_backend: str = "local",
        storage_key: Optional[str] = None,
        preset: Optional[str] = None,
        prompt_ids: Optional[list] = None,
        execution_mode: str = "full",
        content_generation: int = 0,
        strict_push: bool = False,
        schema_version: int = 0,
        schema_hash: str = "",
        old_rag_doc_ids: Optional[list] = None,
        idempotency_key: Optional[str] = None,  # [jonex] R1
    ) -> dict:
        """以 force_reparse=true 重新解析同一文件（atomic-rag action `retry`）。

        参数与 insert 一致；服务端 handler 强制 force_reparse=True。
        [jonex] 阶段4：支持 execution_mode=reparse_strict + strict_push + content_generation
        代次，用于严格全量替换与旧任务 fencing。
        """
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        payload: dict = {
            "action": "retry",
            "file_path": file_path,
            "output_dir": output_dir,
            "knowledge_base_id": knowledge_base_id,
            "storage_backend": storage_backend,
            "storage_key": storage_key or file_path,
            "execution_mode": execution_mode,
            "content_generation": content_generation,
            "strict_push": strict_push,
            "schema_version": schema_version,
        }
        if schema_hash:
            payload["schema_hash"] = schema_hash
        if old_rag_doc_ids:
            payload["old_rag_doc_ids"] = old_rag_doc_ids
        if document_id:
            payload["document_id"] = document_id
        if ontology_schema:
            payload["ontology_schema"] = ontology_schema
        if preset:
            payload["preset"] = preset
        if prompt_ids:
            payload["prompt_ids"] = prompt_ids
        if idempotency_key:                        # [jonex] R1
            payload["idempotency_key"] = idempotency_key
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    # [jonex] R1：幂等键反查 —— 供 R2-b 查证链使用
    async def get_task_by_idempotency_key(
        self, idempotency_key: str, tenant_id: str,
    ) -> dict:
        resp = await self._invoke({
            "action": "get_task_by_idempotency_key",
            "idempotency_key": idempotency_key,
        }, tenant_id)
        return resp["data"]

    async def query(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",          # [jonex] 计量链路追踪
        user_id: str = "",           # [jonex] 计量上下文
        only_need_context: bool = False,  # [jonex] 方案 A：只召回不生成
    ) -> str:
        result = await self.query_detailed(
            query=query, tenant_id=tenant_id, mode=mode, top_k=top_k,
            knowledge_base_id=knowledge_base_id, trace_id=trace_id,
            user_id=user_id, only_need_context=only_need_context,
        )
        return result["answer"]

    async def query_detailed(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",
        user_id: str = "",           # [jonex] 计量上下文
        only_need_context: bool = False,  # [jonex] 方案 A：只召回不生成
    ) -> dict:
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        payload: dict = {
            "action": "query",
            "query": query,
            "mode": mode,
            "top_k": top_k,
            "knowledge_base_id": knowledge_base_id,
            "trace_id": trace_id,
            "user_id": user_id,
            "only_need_context": only_need_context,   # [jonex] 方案 A 透传
        }
        resp = await self._invoke(payload, tenant_id)
        data = resp["data"]
        return {
            "answer": data.get("answer", ""),
            "references": data.get("references", []),
        }

    async def delete(
        self,
        doc_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        payload = {
            "action": "delete",
            "doc_id": doc_id,
            "knowledge_base_id": knowledge_base_id,
        }
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]["success"]

    async def delete_orphan_chunk(
        self,
        chunk_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        payload = {
            "action": "delete_orphan_chunk",
            "chunk_id": chunk_id,
            "knowledge_base_id": knowledge_base_id,
        }
        resp = await self._invoke(payload, tenant_id)
        return bool((resp.get("data") or {}).get("deleted"))

    async def delete_batch(
        self,
        doc_ids: list[str],
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
        document_id: str = "",
        trace_id: str = "",
    ) -> dict:
        """[jonex] 批量删除——单次 Sidecar invoke 提交全部 doc_ids，
        atomic-rag 批量受理后 LightRAG 仅执行一次 rebuild。

        [jonex] R4：透传 document_id / trace_id，使删除/rebuild 的 LLM
        调用能正确归因到具体文档与调用链（X-Jonex-Doc-Id / X-Jonex-Trace-Id）。
        """
        if not doc_ids:
            return {"success": True, "accepted": [], "failed": []}
        payload: dict = {
            "action": "delete_batch",
            "doc_ids": list(doc_ids),
            "knowledge_base_id": knowledge_base_id,
        }
        if document_id:
            payload["document_id"] = document_id
        if trace_id:
            payload["trace_id"] = trace_id
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def get_task_status(
        self,
        task_id: str,
        tenant_id: str,
    ) -> dict:
        payload = {
            "action": "get_task_status",
            "task_id": task_id,
        }
        # v2 语义对齐：task 不存在时返回 success=false / code=40401 / data.status="not_found"。
        # 直接归一为 v1 的 {status:"not_found"}，不抛异常——否则 reconcile 会把 not_found
        # 当作 RAG 调用失败吞成 skipped，文档永远卡 PARSING。
        result = await self._post_invoke(payload, tenant_id)
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and data.get("status") == "not_found":
            return data
        if isinstance(result, dict) and result.get("code") == 40401:
            return {"task_id": task_id, "status": "not_found"}
        return self._check_invoke_result(result, "get_task_status")["data"]

    # ── storage reader (via sidecar → atomic-rag) ──────────

    async def get_storage_summary(
        self,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        payload = {
            "action": "get_storage_summary",
            "knowledge_base_id": knowledge_base_id,
            "scope": self._build_scope(knowledge_base_id, tenant_id),
        }
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def get_storage_documents(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        payload: dict = {
            "action": "get_storage_documents",
            "knowledge_base_id": knowledge_base_id,
            "scope": self._build_scope(knowledge_base_id, tenant_id),
            "page": page,
            "page_size": page_size,
        }
        if keyword:
            payload["keyword"] = keyword
        if status:
            payload["status"] = status
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def get_storage_entities(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        entity_type: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        payload: dict = {
            "action": "get_storage_entities",
            "knowledge_base_id": knowledge_base_id,
            "scope": self._build_scope(knowledge_base_id, tenant_id),
            "page": page,
            "page_size": page_size,
        }
        if keyword:
            payload["keyword"] = keyword
        if entity_type:
            payload["entity_type"] = entity_type
        if file_path:
            payload["file_path"] = file_path
        if document_id:
            payload["doc_id"] = document_id  # v2 storage handler 读 doc_id
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def get_storage_relationships(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
        source_entity: Optional[str] = None,
        target_entity: Optional[str] = None,
    ) -> dict:
        payload: dict = {
            "action": "get_storage_relationships",
            "knowledge_base_id": knowledge_base_id,
            "scope": self._build_scope(knowledge_base_id, tenant_id),
            "page": page,
            "page_size": page_size,
        }
        if keyword:
            payload["keyword"] = keyword
        if file_path:
            payload["file_path"] = file_path
        if document_id:
            payload["doc_id"] = document_id  # v2 storage handler 读 doc_id
        if source_entity:
            payload["source_entity"] = source_entity
        if target_entity:
            payload["target_entity"] = target_entity
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def get_storage_graph_summary(
        self,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        payload = {
            "action": "get_storage_graph_summary",
            "knowledge_base_id": knowledge_base_id,
            "scope": self._build_scope(knowledge_base_id, tenant_id),
        }
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def get_storage_graph(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        limit: int = 200,
        keyword: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        payload: dict = {
            "action": "get_storage_graph",
            "knowledge_base_id": knowledge_base_id,
            "scope": self._build_scope(knowledge_base_id, tenant_id),
            "limit": limit,
        }
        if keyword:
            payload["keyword"] = keyword
        if file_path:
            payload["file_path"] = file_path
        if document_id:
            payload["doc_id"] = document_id  # v2 storage handler 读 doc_id
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def get_document_parse_result(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        document_id: Optional[str] = None,
    ) -> dict:
        payload = {
            "action": "get_document_parse_result",
            "knowledge_base_id": knowledge_base_id,
            "scope": self._build_scope(knowledge_base_id, tenant_id),
        }
        if document_id:
            payload["document_id"] = document_id
            payload["scope"]["document_ids"] = [document_id]
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def retry_ontology_extract(
        self,
        document_id: str,
        knowledge_base_id: str,
        tenant_id: str = "default",
        file_path: str = "",
        *,
        ontology_schema: Optional[dict] = None,
        schema_version: int = 0,
        schema_hash: str = "",
        force_retry: bool = False,
        content_generation: int = 0,
    ) -> dict:
        payload: dict = {
            "action": "retry_ontology_extract",
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "file_path": file_path,
            "schema_version": schema_version,
            "force_retry": force_retry,
            "content_generation": content_generation,
        }
        # [jonex] P0-A.4：必传 compiled schema，否则 atomic-rag 本体归类无法进行
        if ontology_schema:
            payload["ontology_schema"] = ontology_schema
        if schema_hash:
            payload["schema_hash"] = schema_hash
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def get_doc_chunks(
        self,
        document_id: str,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        """按 document_id（= KB knowledge_documents.id）查 chunk 列表。

        对应 atomic-rag v2 action `get_doc_chunks`（含行号/时间轴位置元数据）。
        doc_id 即 KB 文档 id，匹配 file_source 的 doc= 锚点。
        not_found（code=40405）归一为空结果、不抛异常，便于"仍在解析/无 chunk"场景。
        返回 {"doc_id", "total", "chunks": [...]}。
        """
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        payload = {
            "action": "get_doc_chunks",
            "doc_id": document_id,
            "knowledge_base_id": knowledge_base_id,
        }
        result = await self._post_invoke(payload, tenant_id)
        if isinstance(result, dict) and result.get("code") == 40405:
            return result.get("data") or {"doc_id": document_id, "total": 0, "chunks": []}
        return self._check_invoke_result(result, "get_doc_chunks")["data"]

    async def get_chunk_by_id(
        self,
        chunk_id: str,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> Optional[dict]:
        """按 chunk_id 直查单个 chunk 内容（对应 atomic-rag action `get_chunk`）。

        直连 LightRAG text_chunks，不拉整篇 chunk 列表。chunk 不存在时（40405）返回 None。
        返回 {chunk_id, content, full_doc_id, chunk_order_index, file_path, page_idx, line_start, line_end, tokens}。
        """
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        payload = {
            "action": "get_chunk",
            "chunk_id": chunk_id,
            "knowledge_base_id": knowledge_base_id,
        }
        result = await self._post_invoke(payload, tenant_id)
        if isinstance(result, dict) and result.get("code") == 40405:
            return None
        return self._check_invoke_result(result, "get_chunk")["data"]

    async def _post_invoke(self, payload: dict, tenant_id: str) -> dict:
        """POST Sidecar /invoke，返回**原始** result（不做 success 校验）。

        供需要感知 success=false 语义的调用方（如 get_task_status 的 not_found）使用。
        """
        tenant_id = require_tenant(tenant_id)
        payload = dict(payload)
        payload["tenant_id"] = tenant_id
        body = {
            "capability_id": self._capability_id,
            "payload": payload,
            "tenant_id": tenant_id,
        }
        resp = await self._client.post(
            "/invoke",
            json=body,
            headers={"X-Tenant-ID": tenant_id},
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _check_invoke_result(result: dict, action: str) -> dict:
        """业务层校验：Sidecar 可能返回 HTTP 200 但 success=false / data=null，
        若不拦截，下游 resp["data"][...] 会抛出误导性的 NoneType 下标错误。
        统一抛出携带原始 message 的依赖异常。"""
        if not isinstance(result, dict) or not result.get("success", False):
            message = (
                result.get("message") if isinstance(result, dict) else None
            ) or "RAG 能力调用失败"
            raise CapabilityInvokeError(
                message=translate("err.rag.invoke_failed", params={"action": action}, fallback=f"RAG[{action}] 调用失败"),
                details={"action": action, "upstream_message": message},
            )
        if result.get("data") is None:
            raise CapabilityInvokeError(
                message=translate("err.rag.empty_data", params={"action": action}, fallback=f"RAG[{action}] 返回空数据（data=null）"),
                details={"action": action},
            )
        return result

    async def _invoke(self, payload: dict, tenant_id: str) -> dict:
        """统一调用 Sidecar /invoke 接口（strict：success=false 即抛异常）"""
        result = await self._post_invoke(payload, tenant_id)
        return self._check_invoke_result(result, payload.get("action", "unknown"))

    # ── 提示词配置 CRUD（KB 主解析提示词联动）──
    async def create_prompt(
        self,
        tenant_id: str,
        *,
        prompt_code: str,
        content: str,
        preset_name: str = "",
        display_name: str = "",
        description: str = "",
        category: str = "analysis",
        language: str = "zh",
    ) -> dict:
        payload = {
            "action": "create_prompt",
            "prompt_code": prompt_code,
            "content": content,
            "preset_name": preset_name,
            "display_name": display_name,
            "description": description,
            "category": category,
            "language": language,
        }
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def update_prompt(
        self, tenant_id: str, prompt_id: str, *, content: Optional[str] = None, **fields
    ) -> dict:
        payload: dict = {"action": "update_prompt", "prompt_id": prompt_id}
        if content is not None:
            payload["content"] = content
        for k in ("prompt_code", "preset_name", "display_name", "description", "category", "language"):
            if fields.get(k) is not None:
                payload[k] = fields[k]
        resp = await self._invoke(payload, tenant_id)
        return resp["data"]

    async def delete_prompt(self, tenant_id: str, prompt_id: str) -> dict:
        # handler 幂等：不存在也 success=true；用 strict _invoke 安全
        resp = await self._invoke(
            {"action": "delete_prompt", "prompt_id": prompt_id}, tenant_id
        )
        return resp["data"]

    async def get_prompt(self, tenant_id: str, prompt_id: str) -> Optional[dict]:
        # 未找到时 handler 返回 success=false/40404 → 用 _post_invoke 取原始结果，归一为 None
        result = await self._post_invoke(
            {"action": "get_prompt", "prompt_id": prompt_id}, tenant_id
        )
        if isinstance(result, dict) and result.get("success"):
            return result.get("data")
        return None


# ============================================================
# Mock：测试 / 离线桩
# ============================================================
class MockRAGClient(RAGClient):
    """内存桩实现，用于单测和离线开发"""

    def __init__(self, options: Optional[Dict[str, Any]] = None) -> None:
        self._tasks: Dict[str, dict] = {}
        self._docs: set[str] = set()
        logger.info("RAG Client 初始化：MOCK 模式")

    async def insert(
        self,
        file_path: str,
        tenant_id: str,
        output_dir: Optional[str] = None,
        *,
        knowledge_base_id: str,
        document_id: Optional[str] = None,
        ontology_schema: Optional[dict] = None,
        storage_backend: str = "local",
        storage_key: Optional[str] = None,
        preset: Optional[str] = None,
        prompt_ids: Optional[list] = None,
        execution_mode: str = "full",
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        task_id = str(uuid.uuid4())
        self._tasks[task_id] = {
            "task_id": task_id,
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "status": "completed",
            "progress": 1.0,
            "error": None,
        }
        self._docs.add(f"{tenant_id}:{file_path}")
        return {
            "task_id": task_id,
            "status": "pending",
            "file_path": file_path,
        }

    async def query(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",          # [jonex] 计量链路追踪（Mock 忽略）
        user_id: str = "",           # [jonex] 计量上下文（Mock 忽略）
        only_need_context: bool = False,  # [jonex] 方案 A（Mock 吸收形参，行为不变）
    ) -> str:
        require_tenant(tenant_id)
        require_knowledge_base(knowledge_base_id)
        return f"[MOCK RAG 回答] 关于'{query}'的回答：这是来自 Mock 的测试回复。"

    async def query_detailed(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        *,
        knowledge_base_id: str,
        trace_id: str = "",
        user_id: str = "",           # [jonex] 计量上下文（Mock 忽略）
        only_need_context: bool = False,  # [jonex] 方案 A（Mock 吸收形参，行为不变）
    ) -> dict:
        require_tenant(tenant_id)
        require_knowledge_base(knowledge_base_id)
        return {
            "answer": f"[MOCK RAG 回答] 关于'{query}'的回答：这是来自 Mock 的测试回复。",
            "references": [],
        }

    async def delete(
        self,
        doc_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        tenant_id = require_tenant(tenant_id)
        key = f"{tenant_id}:{doc_id}"
        if key in self._docs:
            self._docs.remove(key)
            return True
        return False

    async def delete_orphan_chunk(
        self,
        chunk_id: str,
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
    ) -> bool:
        # Mock：与 delete 同口径（按 id 移除即视为清理成功）
        return await self.delete(
            chunk_id, tenant_id, knowledge_base_id=knowledge_base_id,
        )

    async def delete_batch(
        self,
        doc_ids: list[str],
        tenant_id: str,
        *,
        knowledge_base_id: str = "",
        document_id: str = "",
        trace_id: str = "",
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        accepted = []
        failed = []
        for doc_id in doc_ids:
            key = f"{tenant_id}:{doc_id}"
            if key in self._docs:
                self._docs.remove(key)
                accepted.append(doc_id)
            else:
                failed.append(doc_id)
        return {"success": len(failed) == 0, "accepted": accepted, "failed": failed}

    async def get_task_status(
        self,
        task_id: str,
        tenant_id: str,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        task = self._tasks.get(task_id)
        if task and task.get("tenant_id") != tenant_id:
            return {
                "task_id": task_id,
                "status": "not_found",
                "progress": 0.0,
                "error": "task not found",
            }
        return task or {
            "task_id": task_id,
            "status": "not_found",
            "progress": 0.0,
            "error": "task not found",
        }

    # ── storage reader (mock: empty data) ──────────────────

    async def get_storage_summary(
        self,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        knowledge_base_id = require_knowledge_base(knowledge_base_id)
        return {
            "knowledge_base_id": knowledge_base_id,
            "tenant_id": tenant_id,
            "source": "mock",
            "scope_mode": "knowledge_base",
            "status": "storage_missing",
            "documents_count": 0,
            "processed_documents_count": 0,
            "failed_documents_count": 0,
            "chunks_count": 0,
            "entities_count": 0,
            "relationships_count": 0,
            "compile_versions_count": 0,
            "last_updated_at": None,
            "storage_files": {},
        }

    async def get_storage_documents(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        require_tenant(tenant_id)
        require_knowledge_base(knowledge_base_id)
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    async def get_storage_entities(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        entity_type: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        require_tenant(tenant_id)
        require_knowledge_base(knowledge_base_id)
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    async def get_storage_relationships(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
        source_entity: Optional[str] = None,
        target_entity: Optional[str] = None,
    ) -> dict:
        require_tenant(tenant_id)
        require_knowledge_base(knowledge_base_id)
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    async def get_storage_graph_summary(
        self,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        require_tenant(tenant_id)
        require_knowledge_base(knowledge_base_id)
        return {
            "nodes_count": 0, "edges_count": 0,
            "entity_type_count": 0, "relation_type_count": 0,
            "avg_degree": 0, "entity_type_distribution": [], "relation_distribution": [],
        }

    async def get_storage_graph(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        limit: int = 200,
        keyword: Optional[str] = None,
        file_path: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        require_tenant(tenant_id)
        require_knowledge_base(knowledge_base_id)
        return {"nodes": [], "edges": []}

    async def get_document_parse_result(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        document_id: Optional[str] = None,
    ) -> dict:
        require_tenant(tenant_id)
        require_knowledge_base(knowledge_base_id)
        return {
            "document_id": document_id,
            "summary": {},
            "documents": [],
            "entities": [],
            "relationships": [],
        }

    async def retry_ontology_extract(
        self,
        document_id: str,
        knowledge_base_id: str,
        tenant_id: str = "default",
        file_path: str = "",
        *,
        ontology_schema: Optional[dict] = None,
        schema_version: int = 0,
        schema_hash: str = "",
        force_retry: bool = False,
        content_generation: int = 0,
    ) -> dict:
        require_tenant(tenant_id)
        require_knowledge_base(knowledge_base_id)
        return {"status": "completed", "task_id": str(uuid.uuid4())}

    # ── 提示词配置 CRUD（内存桩）──
    async def create_prompt(self, tenant_id: str, *, prompt_code: str, content: str,
                            preset_name: str = "", display_name: str = "",
                            description: str = "", category: str = "analysis",
                            language: str = "zh") -> dict:
        require_tenant(tenant_id)
        return {
            "id": uuid.uuid4().hex[:12], "tenant_id": tenant_id,
            "prompt_code": prompt_code, "content": content,
            "preset_name": preset_name, "category": category, "language": language,
        }

    async def update_prompt(self, tenant_id: str, prompt_id: str, *,
                            content: Optional[str] = None, **fields) -> dict:
        require_tenant(tenant_id)
        return {"id": prompt_id, "tenant_id": tenant_id, "content": content or "", **fields}

    async def delete_prompt(self, tenant_id: str, prompt_id: str) -> dict:
        require_tenant(tenant_id)
        return {"deleted": True}

    async def get_prompt(self, tenant_id: str, prompt_id: str) -> Optional[dict]:
        require_tenant(tenant_id)
        return None


# ============================================================
# 工厂：根据运行时清单返回对应 Client
# ============================================================
def get_rag_client(
    capability_id: str = RAG_CAPABILITY_ID,
) -> RAGClient:
    """获取 RAG Client（业务/领域代码统一调用此入口）"""
    spec = get_locator().get_spec(capability_id)

    if spec.mode == CapabilityMode.MOCK:
        return MockRAGClient()

    if spec.mode == CapabilityMode.LOCAL:
        return LocalRAGClient()

    if spec.mode == CapabilityMode.REMOTE:
        cfg = get_config()
        return RemoteRAGClient(
            endpoint=spec.endpoint or cfg.SIDECAR_URL or "http://sidecar:8000",
            capability_id=capability_id,
        )

    raise ValueError(f"不支持的 RAG 能力模式：{spec.mode}")
