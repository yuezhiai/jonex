"""Ontology extraction service for Knowledge Base."""

import logging

from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models import OntologyStatus
from ..repository import KnowledgeDocumentRepository

logger = logging.getLogger(__name__)


class OntologyService:
    async def retry_extract(
        self,
        tenant_id: str,
        document_id: str,
        knowledge_base_id: str,
    ) -> dict:
        """手动重抽本体（ontology-only）/ OpenKB 重新编译。

        [jonex] P0-B：统一走「领取 → 提交 → 拿到有效 task_id → EXTRACTING + retry_count++」协议：
        - 不再凭 queued 就置 READY（会导致对账不再扫、新结果永不落 Neo4j）；
        - 提交成功才写回新 `rag_task_id`、置 EXTRACTING、++retry，由对账 EXTRACTING 分支收尾；
        - 提交失败 / 未拿到 task_id：不改状态、不 ++retry，不遗留无 rag_task_id 的 EXTRACTING。

        [jonex] openkb 管线：先校验文档归属，再按 doc 上的真实 KB 查 pipeline_type。
        若为 openkb → 委托 KnowledgeCompilerService.recompile_openkb（原子 claim + 异步投递，立即返回）。
        """
        tenant_id = require_tenant(tenant_id)

        # ── [jonex] 先校验文档归属，再按 doc 上的真实 KB 查 pipeline_type ──
        # 不能直接信请求里的 knowledge_base_id：传错 KB id 会走错分支，或返回
        # 误导性的「无可用 compiled schema」。
        # 本块同时取出 lightrag 分支需要的 file_path / content_generation，
        # 下游复用，替代原来第二次 doc 查询。
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is None or doc.knowledge_base_id != knowledge_base_id:
                raise ResourceNotFoundError(
                    message=translate("err.doc.not_found", fallback="知识文档不存在"),
                )
            real_kb_id = doc.knowledge_base_id
            file_path = doc.file_path or ""                                  # lightrag 用
            content_generation = int(getattr(doc, "content_generation", 0) or 0)  # lightrag 用（fencing）
            doc_status = doc.status                                          # openkb 互斥用
            title = doc.file_name or ""                                      # openkb 用

        # openkb 的「重新编译」= 重跑 OpenKB Wiki 编译，不抽本体、不写 Neo4j
        if await self._get_kb_type(tenant_id, real_kb_id) == "openkb":
            from .openkb_service import KnowledgeCompilerService
            return await KnowledgeCompilerService().recompile_openkb(
                tenant_id, document_id, real_kb_id,
                doc_status=doc_status, source_file_path=file_path, title=title,
            )

        # ── 以下为 lightrag 原有逻辑 ──

        # [jonex] 手动重抽也应尊重重试上限，避免 retry_count 被无限顶高。
        # 已达上限的文档应走 force reparse（reparse 会重置计数），
        # 而非继续点「重试本体抽取」。
        from .reconciliation_service import MAX_ONTOLOGY_RETRIES

        if (doc.ontology_retry_count or 0) >= MAX_ONTOLOGY_RETRIES:
            raise InvalidParameterError(
                message=translate(
                    "err.ontology.retry_limit_reached",
                    fallback=f"本体重试次数已达上限 ({doc.ontology_retry_count}/{MAX_ONTOLOGY_RETRIES})，请对该文档执行 force reparse 以重置计数后重试",
                ),
                details={
                    "document_id": document_id,
                    "retry_count": doc.ontology_retry_count,
                    "max_retries": MAX_ONTOLOGY_RETRIES,
                },
            )

        # ontology-only 必须携带 compiled schema，否则 atomic-rag 无法归类
        schema = None
        schema_version = 0
        try:
            from .ontology_compiler import OntologyCompiler
            schema = await OntologyCompiler().get_compiled_schema(
                tenant_id, knowledge_base_id, auto_compile=True,
            )
            if schema:
                schema_version = int(schema.get("schema_version", 0) or 0)
        except Exception as e:
            logger.warning("Compiled schema check failed for doc %s: %s", document_id, e)

        if schema is None:
            raise InvalidParameterError(
                message=translate("err.ontology.no_compiled_schema", fallback="该知识库无可用 compiled schema，请先编译本体 schema 后再重试"),
                details={"knowledge_base_id": knowledge_base_id, "document_id": document_id},
            )

        # 提交 ontology-only 任务（不在此处 ++retry / 置 EXTRACTING）
        try:
            result = await get_rag_client().retry_ontology_extract(
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
                tenant_id=tenant_id,
                file_path=file_path,
                ontology_schema=schema,
                schema_version=schema_version,
                content_generation=content_generation,
            )
        except Exception as e:
            logger.warning("Ontology retry submit failed for doc %s: %s", document_id, e)
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                doc = await repo.get_required(document_id, tenant_id)
                return doc.to_dict()

        task_id = (result or {}).get("task_id", "")
        if not task_id:
            logger.warning("Ontology retry did not return task_id for doc %s", document_id)
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                doc = await repo.get_required(document_id, tenant_id)
                return doc.to_dict()

        # 提交成功：写回新 rag_task_id + EXTRACTING + ++retry，由对账收尾后才 READY
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            await repo.set_ontology_status(doc, OntologyStatus.EXTRACTING, increment_retry=True)
            await repo.set_status(doc, doc.status, rag_task_id=task_id)
            doc.extra_metadata = {**(doc.extra_metadata or {}), "ontology_retry_task_id": task_id}
            await session.commit()
            return doc.to_dict()

    async def reextract_kb_documents(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        *,
        document_ids: list[str] | None = None,
        only_outdated: bool = False,
        only_ready: bool = True,
    ) -> dict:
        """KB 级「按新 schema 重抽本体」（C→B 联动，走对账被动）。

        [jonex] 阶段2：只做状态重置（置 PENDING + 重置计数 + 写目标 schema 版本）；
        实际重抽交给 30s 对账 reconcile_ontology 逐个 ontology-only 跑（不重解析文件）。
        返回 {matched, reset, skipped, schema_version}。
        """
        tenant_id = require_tenant(tenant_id)

        schema = None
        schema_version = 0
        try:
            from .ontology_compiler import OntologyCompiler
            schema = await OntologyCompiler().get_compiled_schema(
                tenant_id, knowledge_base_id, auto_compile=True,
            )
            if schema:
                schema_version = int(schema.get("schema_version", 0) or 0)
        except Exception as e:
            logger.warning("Compiled schema check failed for KB %s: %s", knowledge_base_id, e)

        if schema is None:
            raise InvalidParameterError(
                message=translate("err.ontology.no_compiled_schema", fallback="该知识库无可用 compiled schema，请先编译本体 schema 后再重试"),
                details={"knowledge_base_id": knowledge_base_id},
            )

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            stats = await repo.reset_ontology_for_kb(
                tenant_id, knowledge_base_id,
                document_ids=document_ids,
                only_ready=only_ready,
                only_outdated=only_outdated,
                target_schema_version=schema_version,
            )
            await session.commit()

        return {**stats, "schema_version": schema_version}

    # ── [jonex] kb_type 查询 ──

    async def _get_kb_type(self, tenant_id: str, kb_id: str) -> str:
        """[jonex] 查询 KB 的 kb_type（转调公共 helper）。"""
        from .kb_type_service import get_kb_type
        return await get_kb_type(tenant_id, kb_id)


__all__ = ["OntologyService"]
