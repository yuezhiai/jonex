# [jonex] Jonex新增文件 — OpenKB 编译服务
"""knowledge_base 侧 OpenKB 编译/查询/删除编排服务。

通过 RemoteOpenKBClient → Sidecar /invoke 调用 OpenKB 容器。
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from jonex_core.capability.atomic.openkb.client import get_openkb_client
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import ResourceConflictError, ResourceNotFoundError
from jonex_core.common.i18n import translate

logger = logging.getLogger(__name__)


# ── [jonex] 模块级辅助函数：产品路径与检测 ──


def openkb_artifact_path(document_id: str) -> Path:
    """[jonex] openkb 解析产物 markdown 的绝对路径。

    路径与 atomic-rag `_write_openkb_artifact` 约定一致（task_manager.py:1172）：
    {KB_INPUT_DIR}/parsed/{document_id}/content.md
    """
    inputs_root = os.getenv("KB_INPUT_DIR", "/app/inputs")
    return Path(inputs_root) / "parsed" / document_id / "content.md"


def openkb_artifact_exists(document_id: str) -> bool:
    """[jonex] 判断 openkb 解析产物是否已在共享卷上。"""
    return openkb_artifact_path(document_id).is_file()


class KnowledgeCompilerService:
    """知识编译服务 — 编排 OpenKB 编译任务。"""

    def __init__(self):
        self._client = get_openkb_client()

    # ── 基础能力（对 OpenKB 容器的薄封装）──

    async def init_kb(self, kb_name: str, tenant_id: str, kb_id: str) -> dict:
        return await self._client.init_kb(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
        )

    async def compile_document(
        self,
        kb_name: str,
        tenant_id: str,
        kb_id: str,
        parsed_artifact: dict,
    ) -> dict:
        """将 Jonex parsed artifact 送入 OpenKB 编译管道。"""
        return await self._client.compile_parsed_document(
            kb_name=kb_name,
            parsed_artifact=parsed_artifact,
            tenant_id=tenant_id,
            kb_id=kb_id,
        )

    async def search_compiled_knowledge(
        self, kb_name: str, tenant_id: str, kb_id: str, question: str,
        return_trace: bool = False,  # [jonex]
    ) -> dict:
        """搜索已编译的 Wiki 知识。"""
        return await self._client.query(
            kb_name=kb_name, question=question, tenant_id=tenant_id, kb_id=kb_id,
            return_trace=return_trace,
        )

    async def remove_document(
        self, kb_name: str, tenant_id: str, kb_id: str, document_id: str,
    ) -> dict:
        """删除 OpenKB 中指定 document_id 的编译产物。"""
        return await self._client.remove_document(
            kb_name=kb_name, document_id=document_id,
            tenant_id=tenant_id, kb_id=kb_id,
        )

    async def delete_kb(self, kb_name: str, tenant_id: str, kb_id: str) -> dict:
        """删除 OpenKB KB 目录。"""
        return await self._client.delete_kb(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
        )

    async def get_graph(self, kb_name: str, tenant_id: str, kb_id: str,
                        document_id: str = "") -> dict:
        """列出已编译 Wiki 的 entities/concepts 及关系（文档级过滤可选）。

        document_id 默认空 = KB 级：knowledge_info_service.get() 的 KB 详情页
        计数调用不传该参，保持现状语义，绝不能缺参报错。
        """
        return await self._client.list_graph(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id,
        )

    async def read_page(self, kb_name: str, tenant_id: str, kb_id: str, path: str) -> dict:
        """读取 Wiki 页面 markdown（供 parse_result_service 透传）。"""
        return await self._client.read_page(
            kb_name=kb_name, path=path, tenant_id=tenant_id, kb_id=kb_id,
        )

    async def list_wiki_contents(self, kb_name: str, tenant_id: str, kb_id: str,
                                 document_id: str = "") -> dict:
        """列出 Wiki 页面树（新 action list_wiki_contents），文档级过滤可选。"""
        return await self._client.list_wiki_contents(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id,
        )

    async def recompile_document(
        self, kb_name: str, tenant_id: str, kb_id: str, document_id: str,
        *, source_file_path: str = "", title: str = "", parser: str = "mineru",
        wiki_schema_version: int | None = None,
    ) -> dict:
        """[jonex] 按共享卷上已有的解析产物重新编译（不重新解析）。

        语义是「以 Jonex parsed artifact 为权威源重编译 Wiki」，因此走
        compile_parsed_document，而不是 OpenKB registry 驱动的 client.recompile()。
        不依赖 atomic-rag task status（内存态、重启即丢）。

        产物不存在时抛 ResourceNotFoundError（调用方已前置校验过，这里是兜底）。

        [jonex] wiki_schema_version：编译绑定的 LLM-Wiki Schema 版本（方案 §6）——
        透传进 parsed_artifact，adapter 写进 task 文件供 patrol 回写 applied；
        None = 未启用 schema fencing（存量 KB 无 schema，与旧行为一致）。
        """
        md_abs = openkb_artifact_path(document_id)
        if not md_abs.is_file():
            raise ResourceNotFoundError(
                message=translate(
                    "err.openkb.no_parsed_artifact",
                    fallback="未找到该文档的解析产物，无法重新编译，请先执行「重新解析」",
                ),
                details={"document_id": document_id, "expected_path": str(md_abs)},
            )

        # 传「相对 inputs 卷根」路径：openkb 挂载点不同（OPENKB_INPUT_ROOT），两端各自解析
        rel_md = f"parsed/{document_id}/content.md"
        rel_assets = (
            f"parsed/{document_id}/assets"
            if (md_abs.parent / "assets").is_dir() else ""
        )

        artifact = {
            "document_id": document_id,
            "source_file_path": source_file_path,
            "parsed_markdown_path": rel_md,
            "assets_dir": rel_assets,
            "parser": parser,
            "metadata": {"pages": 0, "title": title, "recompile": True},
        }
        if wiki_schema_version is not None:
            artifact["wiki_schema_version"] = wiki_schema_version

        return await self.compile_document(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            parsed_artifact=artifact,
        )

    # ── [jonex] 重新编译编排（从 ontology_service.retry_extract 调用）──

    async def recompile_openkb(
        self, tenant_id: str, document_id: str, kb_id: str,
        *, doc_status: str, source_file_path: str = "", title: str = "",
    ) -> dict:
        """[jonex] openkb「重新编译」：按共享卷已有产物重跑 OpenKB 编译。

        任务化语义：claim（列）→ 提交编译任务（容器内后台执行）→ task_id 写列
        → 立即返回。终态由对账巡检 patrol_openkb_compile 轮询回写。
        """
        from ..models import DocStatus

        # ── 忙碌校验：避免与 MinerU 覆盖写 content.md 竞争 ──
        busy = {DocStatus.PARSING.value, DocStatus.PENDING.value,
                DocStatus.INGESTING.value, DocStatus.DELETING.value}
        if doc_status in busy:
            raise ResourceConflictError(
                message=translate("err.openkb.doc_busy",
                                  fallback="文档正在解析/入库/删除中，请等待完成后再重新编译"),
                details={"document_id": document_id, "status": doc_status},
            )

        # ── 产物前置校验：缺失时立即报错，不 claim、不投任务（避免假成功）──
        if not openkb_artifact_exists(document_id):
            raise ResourceNotFoundError(
                message=translate("err.openkb.no_parsed_artifact",
                                  fallback="未找到该文档的解析产物，无法重新编译，请先执行「重新解析」"),
                details={"document_id": document_id},
            )

        # ── [jonex] LLM-Wiki Schema fencing（方案 §6）：读 active 版本 → claim
        # 绑定 target → task 文件带同一版本（防线①：KB 侧按 DB sync 状态核对；
        # 无 schema 返回 None = 未启用 fencing，按旧行为不带版本编译）──
        target_ver = await self.get_active_schema_version(tenant_id, kb_id)

        # ── 原子 claim（列）：状态非 compiling 才抢到（防并发双投）──
        if not await self.claim_compile(tenant_id, document_id,
                                        target_schema_version=target_ver):
            raise ResourceConflictError(
                message=translate("err.openkb.already_compiling",
                                  fallback="该文档正在编译中，请等待完成后再重试"),
                details={"document_id": document_id},
            )

        # ── 提交任务（复用 recompile_document 的产物校验/相对路径拼装）──
        # 投递失败必须回滚 claim，否则永久卡 compiling
        try:
            result = await self.recompile_document(
                kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
                document_id=document_id,
                source_file_path=source_file_path, title=title,
                wiki_schema_version=target_ver,
            )
            await self.set_task_id(tenant_id, document_id, result["task_id"])
        except Exception as exc:
            await self._record_openkb_status(
                tenant_id, document_id, status="failed",
                error=f"OPENKB_RECOMPILE_SUBMIT_FAILED: {exc}"[:1000],
            )
            raise

        from ..repository import KnowledgeDocumentRepository
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            return doc.to_dict()

    async def get_active_schema_version(self, tenant_id: str, kb_id: str) -> int | None:
        """[jonex] 读 KB 当前 active LLM-Wiki Schema 版本（编译绑定用，方案 §6）。

        无 schema（存量 KB 未进过编译设置）返回 None——调用方按「未启用
        schema fencing」处理（不带版本编译，与旧行为一致）。
        """
        from ..repository.llm_wiki_schema_repository import LlmWikiSchemaRepository

        row = await LlmWikiSchemaRepository().get_active(tenant_id, kb_id)
        return row.schema_version if row else None

    async def claim_compile(self, tenant_id: str, document_id: str, *,
                            target_schema_version: int | None = None) -> bool:
        """[jonex] 原子抢占编译权（列化）：状态非 compiling 才置 compiling。

        返回 True 表示抢到。用 DB 条件更新而非「先读后写」，避免并发双投。

        ⚠️ 必须一并清 llm_wiki_task_id / error / warnings——重编译是同一文档的
        第二次尝试，残留的旧 task_id 会让 patrol 读到旧任务 completed 而误回写
        compiled（阻塞三）。requested_at 用 UTC（datetime.utcnow），供 patrol 判超时。

        [jonex] LLM-Wiki Schema fencing（方案 §6）：target_schema_version 传入时
        同 UPDATE 写 llm_wiki_target_schema_version——claim 成功即绑定本次编译的
        目标版本（task 文件写同一版本，patrol 回写 applied 时 fencing 闭环）。
        """
        from sqlalchemy import text

        if target_schema_version is not None:
            sql = text("""
                UPDATE knowledge_base.knowledge_documents
                   SET llm_wiki_compile_status = 'compiling',
                       llm_wiki_compile_requested_at = :now,
                       llm_wiki_task_id = NULL,
                       llm_wiki_compile_error = NULL,
                       llm_wiki_compile_warnings = NULL,
                       llm_wiki_target_schema_version = :target_ver
                 WHERE id = :doc_id
                   AND tenant_id = :tenant_id
                   AND is_deleted = 0
                   AND coalesce(llm_wiki_compile_status, '') <> 'compiling'
            """)
            params: dict = {
                "doc_id": document_id,
                "tenant_id": tenant_id,
                "now": datetime.utcnow(),
                "target_ver": target_schema_version,
            }
        else:
            sql = text("""
                UPDATE knowledge_base.knowledge_documents
                   SET llm_wiki_compile_status = 'compiling',
                       llm_wiki_compile_requested_at = :now,
                       llm_wiki_task_id = NULL,
                       llm_wiki_compile_error = NULL,
                       llm_wiki_compile_warnings = NULL
                 WHERE id = :doc_id
                   AND tenant_id = :tenant_id
                   AND is_deleted = 0
                   AND coalesce(llm_wiki_compile_status, '') <> 'compiling'
            """)
            params = {
                "doc_id": document_id,
                "tenant_id": tenant_id,
                "now": datetime.utcnow(),
            }
        async with get_db_session() as session:
            result = await session.execute(sql, params)
            await session.commit()
            return (result.rowcount or 0) > 0

    async def set_task_id(self, tenant_id: str, document_id: str, task_id: str) -> None:
        """[jonex] 提交任务后把容器 task_id 写列（patrol 按它轮询）。"""
        from ..repository import KnowledgeDocumentRepository
        from sqlalchemy import text

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_required(document_id, tenant_id)
            doc.llm_wiki_task_id = task_id
            session.add(doc)
            await session.commit()

    async def submit_compile(self, kb_name: str, tenant_id: str, kb_id: str,
                             parsed_artifact: dict) -> dict:
        """[jonex] 提交编译任务（OpenKB 容器内后台执行），立即返回 {task_id, status}。"""
        return await self._client.compile_parsed_document(
            kb_name=kb_name, parsed_artifact=parsed_artifact,
            tenant_id=tenant_id, kb_id=kb_id,
        )

    async def get_compile_status(self, kb_name: str, tenant_id: str, kb_id: str,
                                 task_id: str) -> dict:
        """查询单个编译任务状态（排障/手工验证用）。"""
        return await self._client.get_compile_status(
            kb_name=kb_name, task_id=task_id, tenant_id=tenant_id, kb_id=kb_id,
        )

    async def list_compile_tasks(self, kb_name: str, tenant_id: str, kb_id: str,
                                 document_id: str = "") -> dict:
        """列出该 KB 的编译任务（对账巡检批量拉取；document_id 可选过滤）。"""
        return await self._client.list_compile_tasks(
            kb_name=kb_name, tenant_id=tenant_id, kb_id=kb_id,
            document_id=document_id,
        )

    async def _record_openkb_status(self, tenant_id: str, document_id: str, *,
                                    status: str, error: str = "", warnings=None) -> None:
        """[jonex] 回写 LLM-Wiki 编译状态（列化）。语义统一表：
        - compiling/compiled/stale → 清 llm_wiki_compile_error；
        - compiling/stale → 清 llm_wiki_compile_warnings（新尝试开始）；
        - failed → 只写 error，**保留 warnings 不动**（旧警告仍有诊断价值）；
        - compiled → 写 warnings（或 None）、写 compiled_at。
        """
        try:
            from ..repository import KnowledgeDocumentRepository
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                fresh = await repo.get_required(document_id, tenant_id)
                fresh.llm_wiki_compile_status = status
                if status in ("compiling", "compiled", "stale"):
                    fresh.llm_wiki_compile_error = None
                if status in ("compiling", "stale"):
                    fresh.llm_wiki_compile_warnings = None
                if status == "failed" and error:
                    fresh.llm_wiki_compile_error = error[:1000]
                if warnings is not None:
                    fresh.llm_wiki_compile_warnings = warnings or None
                if status == "compiled":
                    fresh.llm_wiki_compiled_at = datetime.utcnow()
                session.add(fresh)
                await session.commit()
        except Exception:
            logger.warning("[jonex] 记录 LLM-Wiki 编译状态失败 doc=%s", document_id, exc_info=True)
