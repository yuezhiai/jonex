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
import os
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

#: 归档时索引清理失败的行（2026-11-20 第二轮审计）：
#: 归档链路在 `_remove_document_indexes` 失败时保留 `rag_doc_ids`、不打
#: `indexes_cleaned_at`，但**没有任何后台会重试**（原补偿器只扫「KB 已删」）。
#: 归档文档的残留切片仍可被召回，「归档 = 下线」失效。这里按同一条清理链补跑。
_ARCHIVED_PENDING_SQL = text("""
    SELECT d.id, d.tenant_id, d.knowledge_base_id, d.rag_doc_ids,
           d.file_name,
           k.kb_type,
           COALESCE(d.extra_metadata->>'ingest_backend', 'rag') AS doc_backend
      FROM knowledge_base.knowledge_documents d
      JOIN knowledge_base.knowledge_info k
        ON k.id = d.knowledge_base_id AND k.tenant_id = d.tenant_id
     WHERE d.is_deleted = 0 AND d.status = 'archived'
       AND d.indexes_cleaned_at IS NULL
       AND k.is_deleted = 0
     ORDER BY d.updated_at
     LIMIT :batch
""")

# Only the Wiki delete path leaves this state after an uncertain engine reply.
# A generous lease keeps the recovery loop away from normally running deletes.
_WIKI_DELETE_STALE_SECONDS = 1800
_STALE_WIKI_DELETES_SQL = text("""
    SELECT d.id, d.tenant_id, d.knowledge_base_id, d.updated_at
      FROM knowledge_base.knowledge_documents d
      JOIN knowledge_base.knowledge_info k
        ON k.id = d.knowledge_base_id AND k.tenant_id = d.tenant_id
     WHERE d.is_deleted = 0 AND d.status = 'deleting'
       AND k.is_deleted = 0 AND k.kb_type = 'jonex_wiki'
       AND d.updated_at < now() - (:stale_seconds * interval '1 second')
     ORDER BY d.updated_at
     LIMIT :batch
""")
_CLAIM_WIKI_DELETE_SQL = text("""
    UPDATE knowledge_base.knowledge_documents
       SET updated_at = now()
     WHERE id = :id AND tenant_id = :tenant_id
       AND knowledge_base_id = :knowledge_base_id
       AND is_deleted = 0 AND status = 'deleting' AND updated_at = :updated_at
    RETURNING id
""")


#: 删除链在第一次事务里提交 DELETING 之后，要跨多个外部调用才能软删；
#: 进程重启/清理抛异常会让文档永久停在 deleting（所有入口都被那道闸挡住）。
#: 这里做超时兜底：超过 30 分钟仍是 deleting 的行回写为 failed，用户可重试。
_STUCK_DELETING_SQL = text("""
    UPDATE knowledge_base.knowledge_documents
       SET status = 'failed',
           error_message = '删除超时（进程中断或索引清理失败），已自动回滚为失败，可重新删除',
           updated_at = now()
     WHERE status = 'deleting'
       AND is_deleted = 0
       AND updated_at < now() - make_interval(secs => 1800)
       -- ⚠️ jonex_wiki 的 deleting 是「删除可恢复」的标记：引擎回报不确定时保留它、
       -- 由 `recover_stale_wiki_deletes` 幂等重试（2026-11-19 审计：sweep 一旦抢先
       -- 把它改成 failed，恢复查询就永远查不到，自动重试从不发生）。
       AND NOT EXISTS (
           SELECT 1 FROM knowledge_base.knowledge_info k
            WHERE k.id = knowledge_base.knowledge_documents.knowledge_base_id
              AND k.tenant_id = knowledge_base.knowledge_documents.tenant_id
              AND k.kb_type = 'jonex_wiki')
    RETURNING id
""")


#: 无主 PENDING 文档兜底（2026-11-19 审计）：upload-only 落库后任务在派活前失败/被停止时，
#: 文档没有任何接管行，也没有巡检会推进它 —— 永久 PENDING、占配额、只能手工删。
#: 超长保留期（默认 72h）后回写 failed，用户可删除或重新上传。
_ORPHAN_PENDING_SQL = text("""
    UPDATE knowledge_base.knowledge_documents d
       SET status = 'failed',
           error_message = '上传后长期未被任何任务执行（超过保留期），已自动回收为失败；可删除或重新上传',
           updated_at = now()
     WHERE d.status = 'pending'
       AND d.is_deleted = 0
       AND d.updated_at < now() - make_interval(hours => :ttl_hours)
       AND NOT EXISTS (SELECT 1 FROM knowledge_base.task_center_documents t
                        WHERE t.document_id = d.id)
    RETURNING d.id
""")

#: 长期 released 的接管行清理（默认保留 7 天供排查）：行本身是终态残留，
#: 任务仍在跑（pending/running）的一律不动。
_RELEASED_TAKEOVER_SWEEP_SQL = text("""
    DELETE FROM knowledge_base.task_center_documents t
     WHERE t.state = 'released'
       AND t.updated_at < now() - make_interval(days => :days)
       -- ⚠️ **只清「文档已经删掉」的行**（2026-11-19 第二轮审计）：
       -- released 接管行是「重新解析 / 重新编译」两个按钮唯一的锚点
       -- （`rearm_for_rerun` 按它取 task_id）；活文档的行一旦被删，这两个按钮
       -- 会永久报 no_task，只能整份重传。活文档的行保留，删文档时随文档一起清。
       AND NOT EXISTS (
           SELECT 1 FROM knowledge_base.knowledge_documents d
            WHERE d.id = t.document_id AND d.is_deleted = 0)
       AND (t.task_id IS NULL OR NOT EXISTS (
             SELECT 1 FROM task_center.tasks tk
              WHERE tk.id = t.task_id AND tk.is_deleted = 0
                AND tk.status IN ('pending', 'running')))
    RETURNING t.document_id
""")

#: 已释放的执行登记清理（默认保留 7 天）：每次派活一行，删文档/删任务后不会自己消失。
_REGISTRY_SWEEP_SQL = text("""
    DELETE FROM knowledge_base.task_execution_registry
     WHERE (released_at IS NOT NULL
            AND released_at < now() - make_interval(days => :days))
        -- 成功受理的登记 runner 不会 release（released_at 恒 NULL），只靠上面那条
        -- 永远清不掉（2026-11-19 第二轮审计）。按「领取时间」再加一个长保留期兜底。
        OR claimed_at < now() - make_interval(days => 30)
    RETURNING execution_id
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
                # 先恢复、再 sweep：恢复的候选（deleting + 窗口）是 sweep 候选的子集，
                # 反过来顺序会让 sweep 先把行改成 failed、恢复永远查不到（2026-11-19 审计）。
                # 恢复内部有 claim（刷新 updated_at），sweep 随后不会再碰这批行。
                await self.recover_stale_wiki_deletes()
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

    async def _sweep_stuck_deleting(self) -> int:
        """把超时的 deleting 回写为 failed（删除链崩溃/异常后的兜底）。"""
        async with self._session_factory() as session:
            result = await session.execute(_STUCK_DELETING_SQL)
            swept = list(result.scalars().all())
            await session.commit()
        if swept:
            logger.warning(
                "[IndexCleanup] deleting 超时回滚为 failed: %s", swept
            )
        return len(swept)

    async def _sweep_orphans(self) -> dict:
        """无主残留清理（2026-11-19 审计）：孤儿 PENDING 文档、长期 released 接管行、
        已释放的执行登记。全部按长保留期兜底（超期才动），不碰在途任务。"""
        # 默认 30 天：upload-only 是「先上传、过几天再执行」的设计，72h 回收会误伤
        # 正常用法（2026-11-19 第二轮审计）。
        ttl_hours = int(os.getenv("KB_ORPHAN_PENDING_TTL_HOURS", "720"))
        days = int(os.getenv("KB_ORPHAN_ROW_RETENTION_DAYS", "7"))
        async with self._session_factory() as session:
            orphan_docs = list((await session.execute(
                _ORPHAN_PENDING_SQL, {"ttl_hours": ttl_hours},
            )).scalars().all())
            takeovers = list((await session.execute(
                _RELEASED_TAKEOVER_SWEEP_SQL, {"days": days},
            )).scalars().all())
            registry = list((await session.execute(
                _REGISTRY_SWEEP_SQL, {"days": days},
            )).scalars().all())
            await session.commit()
        if orphan_docs:
            logger.warning(
                "[IndexCleanup] 孤儿 PENDING 文档回收为 failed（超 %sh）: %s",
                ttl_hours, orphan_docs[:20],
            )
        if takeovers:
            logger.info(
                "[IndexCleanup] 清理长期 released 接管行 %d 条: %s",
                len(takeovers), takeovers[:20],
            )
        if registry:
            logger.info(
                "[IndexCleanup] 清理已释放执行登记 %d 条", len(registry),
            )
        return {
            "orphan_pending": len(orphan_docs),
            "released_takeovers": len(takeovers),
            "released_registry": len(registry),
        }

    async def run_once(self) -> dict:
        """跑一轮；单测直接调用。"""
        await self._sweep_stuck_deleting()
        try:
            await self._sweep_orphans()
        except Exception:
            # 清理是旁路：它失败不能把本轮的索引补偿一起停掉（审计 #30）
            logger.warning("[IndexCleanup] 无主残留清理失败，本轮跳过", exc_info=True)
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
                all_cleaned = await svc._remove_document_indexes(
                    tenant_id=row["tenant_id"],
                    document_id=row["id"],
                    kb_id=row["knowledge_base_id"],
                    file_name=row.get("file_name") or "",
                    rag_doc_ids=rag_doc_ids,
                    kb_type=row.get("kb_type"),
                )
                # ⚠️ 引擎侧报告有失败时**绝不能打标**：打标就等于宣布「这文档清过了」，
                # 而残留还留在检索侧、下一轮也不会再被选中 —— 永久泄漏。
                # 抛出走进下面的失败分支（不写 mark、计入 failed、下一轮重试）。
                if all_cleaned is False:
                    raise RuntimeError(
                        f"引擎索引未全部清成功: doc={row['id']} kb={row['knowledge_base_id']}"
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
        # ── 归档清理失败的补偿（2026-11-20 第二轮审计）──
        archived_rows: list[dict] = []
        try:
            async with self._session_factory() as session:
                archived_rows = [
                    dict(r) for r in (
                        await session.execute(_ARCHIVED_PENDING_SQL, {"batch": self._batch})
                    ).mappings().all()
                ]
        except Exception:
            logger.warning("[IndexCleanup] 归档补偿扫描失败", exc_info=True)
        for row in archived_rows:
            try:
                svc = _service()
                rag_doc_ids = list(row.get("rag_doc_ids") or [])
                all_cleaned = await svc._remove_document_indexes(
                    tenant_id=row["tenant_id"],
                    document_id=row["id"],
                    kb_id=row["knowledge_base_id"],
                    file_name=row.get("file_name") or "",
                    rag_doc_ids=rag_doc_ids,
                    doc_backend=row.get("doc_backend") or "rag",
                    kb_type=row.get("kb_type"),
                )
                if all_cleaned is False:
                    raise RuntimeError(
                        f"归档文档索引未全部清成功: doc={row['id']} kb={row['knowledge_base_id']}"
                    )
                await svc.mark_indexes_cleaned(row["tenant_id"], row["id"])
                cleaned += 1
            except Exception:
                failed += 1
                logger.warning(
                    "[IndexCleanup] 归档文档索引补偿失败: doc=%s kb=%s（下一轮重试）",
                    row.get("id"), row.get("knowledge_base_id"), exc_info=True,
                )
        if rows or archived_rows:
            logger.info(
                "[IndexCleanup] 本轮候选 %d（归档补偿 %d），清理 %d，失败 %d",
                len(rows), len(archived_rows), cleaned, failed,
            )
        return {
            "pending": len(rows) + len(archived_rows),
            "archived_pending": len(archived_rows),
            "cleaned": cleaned,
            "failed": failed,
        }

    async def recover_stale_wiki_deletes(self) -> dict:
        """Resume old Wiki deletions after a crash or ambiguous engine reply.

        The compare-and-set claim prevents two platform workers from taking the
        same stale row concurrently. A crash after claiming is retried after the
        lease expires; engine deletion and object removal are idempotent.
        """
        if self._batch <= 0:
            return {"pending": 0, "resumed": 0, "failed": 0}
        async with self._session_factory() as session:
            rows = (await session.execute(_STALE_WIKI_DELETES_SQL, {
                "batch": self._batch, "stale_seconds": _WIKI_DELETE_STALE_SECONDS,
            })).mappings().all()
        resumed = failed = 0
        service: Any = None
        for raw_row in rows:
            row = dict(raw_row)
            try:
                async with self._session_factory() as session:
                    claimed = (await session.execute(_CLAIM_WIKI_DELETE_SQL, row)).first()
                if not claimed:
                    continue
                if service is None:
                    service = self._service_factory()
                await service.delete_document(
                    row["tenant_id"], row["id"],
                    {"knowledge_base_id": row["knowledge_base_id"]},
                    allow_filename_fallback=False,
                    audit_action="recover_delete_document",
                    _resume_deleting=True,
                )
                resumed += 1
            except Exception:
                failed += 1
                logger.warning(
                    "[IndexCleanup] Wiki 文档删除恢复失败: doc=%s kb=%s（租约过期后重试）",
                    row.get("id"), row.get("knowledge_base_id"), exc_info=True,
                )
        if rows:
            logger.info("[IndexCleanup] Wiki 删除恢复候选 %d，完成 %d，失败 %d",
                        len(rows), resumed, failed)
        return {"pending": len(rows), "resumed": resumed, "failed": failed}


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
