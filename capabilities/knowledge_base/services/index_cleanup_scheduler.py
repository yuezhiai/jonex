"""删库后的检索索引补偿器。

**问题**：删 KB（`knowledge_info.delete`）只软删 KB 行与文档行
（`cleanup_kb_residue`），不走逐文档删除链路，因此检索侧索引不会清：
LightRAG 切片、OpenKB / jonex_wiki 引擎文档、Neo4j 本体节点都会残留。
文档行一旦被批量软删，就没有「按文档删除」的入口了。

**补偿**：周期性扫描「父 KB 已软删、文档 `is_deleted=1`、`indexes_cleaned_at IS NULL`」
的文档行，用**删除前的粘性快照**调用与 `delete_document` 相同的
`DocumentService._remove_document_indexes`，成功后打标 `indexes_cleaned_at`。

粘性快照来自文档行自身（`rag_doc_ids`）与 KB 行（`kb_type`）—— 注意文档 / KB 已软删，
`_remove_document_indexes` 不能自己反查（会拿到空值），所以显式传入。

单副本假设（与 `RetentionScheduler` 相同）：多副本会重复清理；引擎侧删除是幂等的，
最坏是多打几条 warning。失败按文档隔离，未打标的行下一轮重试。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from sqlalchemy import text

from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session

logger = logging.getLogger(__name__)

_PENDING_SQL = text("""
    SELECT d.id,
           d.tenant_id,
           d.knowledge_base_id,
           d.file_name,
           d.rag_doc_ids,
           k.kb_type
      FROM knowledge_base.knowledge_documents d
      JOIN knowledge_base.knowledge_info k
        ON k.id = d.knowledge_base_id
       AND k.tenant_id = d.tenant_id
     WHERE d.is_deleted = 1
       AND d.indexes_cleaned_at IS NULL
       AND k.is_deleted = 1
     ORDER BY d.updated_at
     LIMIT :batch
""")


def _default_document_service_factory() -> Any:
    from .document_service import DocumentService

    return DocumentService()


class IndexCleanupScheduler:
    """删库索引补偿后台循环。"""

    def __init__(
        self,
        *,
        interval: Optional[float] = None,
        batch: Optional[int] = None,
        session_factory: Optional[Callable] = None,
        document_service_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        config = get_config()
        self._interval = float(
            interval
            if interval is not None
            else getattr(config, "KB_INDEX_CLEANUP_INTERVAL_SECONDS", 600)
        )
        self._batch = int(
            batch
            if batch is not None
            else getattr(config, "KB_INDEX_CLEANUP_BATCH", 100)
        )
        self._session_factory = session_factory or get_db_session
        self._service_factory = (
            document_service_factory or _default_document_service_factory
        )
        self._task: Optional[asyncio.Task] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """起循环；`interval <= 0` 显式关闭。"""
        if self._interval <= 0:
            logger.info(
                "[IndexCleanup] 补偿器已关闭（KB_INDEX_CLEANUP_INTERVAL_SECONDS<=0）"
            )
            return False
        if self.is_running:
            return True
        if self._batch <= 0:
            logger.warning(
                "[IndexCleanup] KB_INDEX_CLEANUP_BATCH=%s（<=0）：补偿器将空转",
                self._batch,
            )
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[IndexCleanup] 删库索引补偿器已启动（间隔 %.1fs，每批最多 %s 条）",
            self._interval,
            self._batch,
        )
        return True

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("[IndexCleanup] 删库索引补偿器已停止")

    async def _loop(self) -> None:
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(self._interval)
                first_run = False
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一轮失败不该让循环死掉
                logger.warning(
                    "[IndexCleanup] 本轮出错，跳过本轮：%s", exc, exc_info=True,
                )

    async def _fetch_pending(self) -> list[dict]:
        if self._batch <= 0:
            return []
        async with self._session_factory() as session:
            rows = (
                await session.execute(_PENDING_SQL, {"batch": self._batch})
            ).mappings().all()
        return [dict(row) for row in rows]

    async def run_once(self) -> dict:
        """跑一轮；单测直接调用。"""
        rows = await self._fetch_pending()
        cleaned = 0
        failed = 0
        service: Any = None

        def _service() -> Any:
            nonlocal service
            if service is None:
                service = self._service_factory()
            return service

        for row in rows:
            try:
                svc = _service()
                rag_doc_ids = list(row.get("rag_doc_ids") or [])
                if not rag_doc_ids:
                    # 与删除链路同口径反查（doc= 锚点优先，不按文件名兜底：
                    # 删库后同 KB 同名文档可能已不在，兜底只会误伤）。
                    rag_doc_ids = await svc._resolve_rag_doc_ids(
                        tenant_id=row["tenant_id"],
                        document_id=row["id"],
                        kb_id=row["knowledge_base_id"],
                        file_name=row.get("file_name") or "",
                        rag_doc_ids=[],
                        allow_filename_fallback=False,
                    )
                await svc._remove_document_indexes(
                    tenant_id=row["tenant_id"],
                    document_id=row["id"],
                    kb_id=row["knowledge_base_id"],
                    file_name=row.get("file_name") or "",
                    rag_doc_ids=rag_doc_ids,
                    kb_type=row.get("kb_type"),
                )
                await svc.mark_indexes_cleaned(
                    row["tenant_id"], row["id"],
                )
                cleaned += 1
            except Exception:
                failed += 1
                logger.warning(
                    "[IndexCleanup] 文档索引补偿失败: doc=%s kb=%s（下一轮重试）",
                    row.get("id"), row.get("knowledge_base_id"), exc_info=True,
                )
        if rows:
            logger.info(
                "[IndexCleanup] 本轮候选 %d，清理 %d，失败 %d",
                len(rows), cleaned, failed,
            )
        return {"pending": len(rows), "cleaned": cleaned, "failed": failed}


# ============================================================
# 进程内单例（能力 initialize / shutdown 用）
# ============================================================

_scheduler: Optional[IndexCleanupScheduler] = None


def get_scheduler() -> IndexCleanupScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = IndexCleanupScheduler()
    return _scheduler


async def start_scheduler() -> Optional[IndexCleanupScheduler]:
    scheduler = get_scheduler()
    return scheduler if await scheduler.start() else None


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None


__all__ = [
    "IndexCleanupScheduler",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
