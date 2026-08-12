"""Internal Knowledge Base reconciliation service.

Follows the same core logic as the previous flat-file architecture:
- Per-doc task status polling with completed/failed/not_found handling
- LightRAG storage fallback (`_verify_via_storage`) when task state is lost
- Ontology retry with max 3 attempts
- Neo4j ontology write before PG status update (consistency design)
- Compiled schema check before ontology retry (Phase 4.2)
"""

import logging
import os
import time
from datetime import datetime

from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.database import get_db_session
from jonex_core.common.neo4j_client import get_neo4j_driver

from ..models import DocStatus, OntologyStatus
from ..repository import KnowledgeDocumentRepository, OntologyGraphRepository

logger = logging.getLogger(__name__)


# [jonex] §11 v2 stage key 白名单：过滤未知阶段、避免噪音泄露到聚合字段
_V2_STAGE_KEYS = frozenset({
    "created", "queued", "parse", "text_insert", "multimodal",
    "push_chunks", "ontology_extract", "pipeline_done",
})

# [jonex] P1-1：created/queued 是排队等待，不算入 worker 端到端耗时
_QUEUE_KEYS = frozenset({"created", "queued"})


def _normalize_stage_timings(raw):
    """归一 worker 分阶段耗时，兼容两种形态：
    - v1：dict，含 `worker_total_ms` + 各阶段键
    - v2：list[{stage,label,started_at,ended_at,elapsed_seconds}]
    返回 (worker_total_ms: int|None, per_stage_ms: dict)。
    v2 list 形态仅取白名单内 stage key，其余静默跳过。
    """
    if isinstance(raw, dict):
        total = raw.get("worker_total_ms")
        per = {k: v for k, v in raw.items() if k != "worker_total_ms"}
        return total, per
    if isinstance(raw, list):
        per: dict = {}
        total_s = 0.0
        for s in raw:
            if not isinstance(s, dict):
                continue
            stage = s.get("stage") or "unknown"
            # [jonex] §11 白名单过滤
            if stage not in _V2_STAGE_KEYS:
                continue
            try:
                secs = float(s.get("elapsed_seconds") or 0)
            except (TypeError, ValueError):
                secs = 0.0
            per[f"{stage}_ms"] = int(secs * 1000)
            # [jonex] P1-1：created/queued 是排队，不算入 worker 端到端
            if stage not in _QUEUE_KEYS:
                total_s += secs
        return (int(total_s * 1000) if raw else None), per
    return None, {}


def _detect_pipeline_version(raw) -> str | None:
    """从 stage_timings 形态推断 pipeline 版本。
    - list → v2 (raganything pipeline)
    - dict → v1 (lightrag_adapter legacy)
    - None/empty → None
    """
    if isinstance(raw, list):
        return "v2"
    if isinstance(raw, dict):
        return "v1"
    return None

MAX_ONTOLOGY_RETRIES = 3


class ReconciliationService:
    """Background-only cross-tenant scans. Do not use from request handlers."""

    # ── public entry points (called by the 30s loop) ───────────

    async def reconcile_documents(self, limit: int = 50) -> dict:
        """Scan PARSING + INGESTING docs and reconcile each one."""
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            docs = await repo.list_by_status_for_reconciliation(
                [DocStatus.PARSING, DocStatus.INGESTING], limit=limit,
            )

        updated, failed, skipped = 0, 0, 0
        for doc in docs:
            try:
                result = await self._reconcile_one(doc)
                if result == "updated":
                    updated += 1
                elif result == "skipped":
                    skipped += 1
            except Exception:
                failed += 1
                logger.warning("Failed to reconcile document %s", doc.id, exc_info=True)

        return {"checked": len(docs), "updated": updated, "skipped": skipped, "failed": failed}

    async def reconcile_ontology(self, limit: int = 50) -> dict:
        """Scan ontology pending/failed docs and retry extraction.

        [jonex] P1-F：最大在途 ontology task 数限流（ONTOLOGY_MAX_INFLIGHT，默认 100）——
        在途（EXTRACTING）过多时本轮少领/不领，避免打满上游 LLM。SKIP LOCKED 领取避免多实例重复。
        """
        max_inflight = int(os.getenv("ONTOLOGY_MAX_INFLIGHT", "100"))
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            inflight = await repo.count_by_ontology_status([OntologyStatus.EXTRACTING])
            # 预留额度：本轮最多新领 (max_inflight - inflight) 个 PENDING/FAILED；
            # EXTRACTING 文档仍需扫描以轮询收尾，故 fetch limit 不缩减，仅在提交新任务时受限。
            effective_limit = limit
            docs = await repo.list_by_ontology_status_for_reconciliation(
                [OntologyStatus.PENDING, OntologyStatus.FAILED, OntologyStatus.EXTRACTING],
                limit=effective_limit,
                skip_locked=True,
            )

        budget = max(0, max_inflight - inflight)  # 本轮可新提交的 ontology-only 任务数
        queued = 0
        for doc in docs:
            try:
                # EXTRACTING 文档只做轮询收尾（不占用新提交预算）；
                # PENDING/FAILED 需要新提交，受 budget 限制。
                is_new_submit = doc.ontology_status != OntologyStatus.EXTRACTING.value
                if is_new_submit and budget <= 0:
                    continue
                if await self._retry_ontology_one(doc):
                    if is_new_submit:
                        budget -= 1
                    queued += 1
            except Exception:
                logger.warning("Ontology retry failed for doc %s", doc.id, exc_info=True)

        return {"checked": len(docs), "queued": queued, "inflight": inflight}

    async def patrol_parsing_timeout(self, limit: int = 50) -> dict:
        """扫描 PARSING/INGESTING 超时的文档并处置。

        [jonex] R1 探活优化：
        - SOFT 超时（RAG_TASK_SOFT_TIMEOUT_SEC，默认 3600s）到点先探活，任务仍在处理/排队
          则继续等待，绝不无脑重推；
        - HARD 超时（RAG_TASK_HARD_TIMEOUT_SEC，默认 21600s=6h）兜底，超限即使 alive 也判死；
        - 探测失败不即判死（RAG_TASK_PROBE_FAIL_MAX，默认 3 次连续失败才判 dead）；
        - INGESTING 文档绝不 re-insert（已过 P1/P2，重推会丢弃进度并加重 LightRAG 负载）；
        - INGESTING 失败不动 parsing_retry_count（该字段语义仅代表 P1/P2 解析重试）。
        集成在 _reconcile_loop() 30 秒循环中，不额外引入 APScheduler。
        """
        SOFT = int(os.getenv("RAG_TASK_SOFT_TIMEOUT_SEC", "3600"))
        HARD = int(os.getenv("RAG_TASK_HARD_TIMEOUT_SEC", "21600"))
        PROBE_FAIL_MAX = int(os.getenv("RAG_TASK_PROBE_FAIL_MAX", "3"))

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            docs = await repo.list_by_status_for_reconciliation(
                [DocStatus.PARSING, DocStatus.INGESTING], limit=limit,
            )

        now = datetime.now()  # [jonex] P0-2: naive 本地时间，与 DB 写入口径一致
        timed_out = 0
        retried = 0
        exhausted = 0
        waiting = 0

        for doc in docs:
            # [jonex] P0-3: 计时基准优先用 parsing_started_at（进入 PARSING/INGESTING 时打点），
            # 兜底用 updated_at。解耦 TimestampMixin.updated_at onupdate 心跳污染：
            # _death_candidate 每 30s 写 DB → onupdate 刷新 updated_at → 旧实现永远到不了 SOFT。
            _pm = dict(doc.extra_metadata or {})
            started_at_str = _pm.get("parsing_started_at")
            if started_at_str:
                try:
                    started_at = datetime.fromisoformat(str(started_at_str))
                    if started_at.tzinfo is not None:
                        started_at = started_at.astimezone().replace(tzinfo=None)
                    elapsed_seconds = (now - started_at).total_seconds()
                except (ValueError, TypeError, OSError):
                    started_at = None
            else:
                started_at = None

            if started_at is None:
                # 兜底：旧文档没有 parsing_started_at，回退到 updated_at
                updated_at = doc.updated_at
                if updated_at is None:
                    continue
                if updated_at.tzinfo is not None:
                    updated_at = updated_at.astimezone().replace(tzinfo=None)
                elapsed_seconds = (now - updated_at).total_seconds()

            if elapsed_seconds < SOFT:
                continue

            timed_out += 1

            meta = dict(doc.extra_metadata or {})
            retry_count = meta.get("parsing_retry_count", 0)

            # ── R1-E 探活：查任务真实状态 ──
            alive = False
            rag_status = ""
            current_step = ""
            probe_failures = meta.get("consecutive_probe_failures", 0)

            if doc.rag_task_id:
                try:
                    st = await get_rag_client().get_task_status(
                        task_id=doc.rag_task_id, tenant_id=doc.tenant_id,
                    )
                    rag_status = st.get("status") or st.get("state") or ""
                    current_step = st.get("current_step") or ""
                    alive = rag_status in ("pending", "queued", "processing", "running", "extracting")
                    # 探测成功 → 清零失败计数
                    if probe_failures > 0:
                        meta["consecutive_probe_failures"] = 0
                except Exception as exc:
                    logger.warning("Patrol probe get_task_status failed doc=%s: %s", doc.id, exc)
                    probe_failures += 1
                    meta["consecutive_probe_failures"] = probe_failures
                    if probe_failures < PROBE_FAIL_MAX:
                        # 单次探测失败 → 保守跳过本轮，不判死
                        waiting += 1
                        logger.info(
                            "Patrol→probe failed, skip this round: doc_id=%s waited=%.0fmin failures=%d/%d",
                            doc.id, elapsed_seconds / 60, probe_failures, PROBE_FAIL_MAX,
                        )
                        async with get_db_session() as session:
                            repo_s = KnowledgeDocumentRepository(session)
                            doc.extra_metadata = meta
                            session.add(doc)
                            await session.commit()
                        continue
                    # 连续失败达上限 → 判 dead
                    alive = False
            else:
                alive = False

            # ── 未到硬上限且任务还活着 → 只是排队/慢，继续等 ──
            if alive and elapsed_seconds < HARD:
                # [jonex] R5-a：processing + cleanup 卡死兜底 —— 不再等 6h HARD，
                # 按删除量动态超时判死（与 _handle_failed 同口径），覆盖 patrol
                # 对 processing 态盲区。reparse 进入 cleanup 后任务 status 为
                # processing，旧 patrol 只看 alive 就继续等 → 永久卡 ingesting。
                if current_step == "cleanup":
                    stuck_timeout = self._cleanup_stuck_timeout(st)
                    if elapsed_seconds >= stuck_timeout:
                        logger.error(
                            "Patrol PARSING+cleanup stuck→FAILED: doc_id=%s task_id=%s "
                            "waited=%.0fmin current_step=%s",
                            doc.id, doc.rag_task_id, elapsed_seconds / 60, current_step,
                        )
                        async with get_db_session() as session:
                            repo_s = KnowledgeDocumentRepository(session)
                            await repo_s.set_status(
                                doc, DocStatus.FAILED,
                                error_message=f"文档清理超时（cleanup 卡死 {elapsed_seconds/60:.0f} 分钟），请重试",
                            )
                        continue
                waiting += 1
                logger.info(
                    "Patrol→仍在处理/排队，继续等待: doc_id=%s waited=%.0fmin rag_status=%s current_step=%s",
                    doc.id, elapsed_seconds / 60, rag_status, current_step,
                )
                # 持久化可能已清零的 probe 计数
                if meta.get("consecutive_probe_failures", 0) == 0 and probe_failures > 0:
                    async with get_db_session() as session:
                        repo_s = KnowledgeDocumentRepository(session)
                        doc.extra_metadata = meta
                        session.add(doc)
                        await session.commit()
                continue

            # ── R1-C：INGESTING 绝不 re-insert ──
            is_ingesting = doc.status == "ingesting"  # forward-compatible with Batch 2-B
            if is_ingesting:
                # [jonex] 问题2：判死前探活。INGESTING 阶段 LightRAG 抽取可能
                # 极慢（3~18s/chunk × N千 chunk），健康任务在 HARD 内未完成
                # 是正常的。alive 且未超绝对上限则继续等待，避免误杀（如本例
                # 1878 chunk 文档在 6h HARD 时实际仍在正常入库）。
                INGESTING_CEIL = HARD * 2
                if alive and elapsed_seconds < INGESTING_CEIL:
                    waiting += 1
                    logger.info(
                        "Patrol→INGESTING 仍在入库，继续等待: doc_id=%s waited=%.0fmin "
                        "rag_status=%s current_step=%s",
                        doc.id, elapsed_seconds / 60, rag_status, current_step,
                    )
                    continue

                error_msg = (
                    f"知识入库超时（已 INGESTING {elapsed_seconds/60:.0f} 分钟），"
                    f"任务状态={rag_status or 'unknown'}"
                )
                if elapsed_seconds >= HARD:
                    logger.error(
                        "Patrol HARD timeout INGESTING→FAILED: doc_id=%s task_id=%s "
                        "waited=%.0fmin rag_status=%s current_step=%s",
                        doc.id, doc.rag_task_id, elapsed_seconds / 60, rag_status, current_step,
                    )
                async with get_db_session() as session:
                    repo_s = KnowledgeDocumentRepository(session)
                    fresh = await repo_s.set_status(doc, DocStatus.FAILED, error_message=error_msg)
                    # #6 计数隔离：INGESTING 失败用独立标记，不动 parsing_retry_count
                    # [jonex] P0-新: N2 规范 — set_status 返回 fresh 实例（已装进 session），
                    # 在 fresh 上写 metadata；禁止 session.add(detached doc)。
                    fm = dict(fresh.extra_metadata or {})
                    fm["ingesting_failed"] = True
                    fresh.extra_metadata = fm
                logger.warning(
                    "Patrol INGESTING→FAILED (no re-insert): doc_id=%s waited=%.0fmin",
                    doc.id, elapsed_seconds / 60,
                )
                continue

            # ── PARSING：原有 FAILED + re-insert 路径 ──

            if retry_count >= 3:
                exhausted += 1
                if elapsed_seconds >= HARD:
                    logger.error(
                        "Patrol HARD timeout PARSING→FAILED(exhausted): doc_id=%s task_id=%s "
                        "waited=%.0fmin rag_status=%s current_step=%s retries=%d",
                        doc.id, doc.rag_task_id, elapsed_seconds / 60,
                        rag_status, current_step, retry_count,
                    )
                async with get_db_session() as session:
                    repo_s = KnowledgeDocumentRepository(session)
                    await repo_s.set_status(
                        doc, DocStatus.FAILED,
                        error_message=f"解析超时（已 PARSING {elapsed_seconds/60:.0f} 分钟），重试次数已用尽",
                    )
                logger.warning(
                    "Patrol timeout exhausted: doc_id=%s, waited=%.0fmin, retries=%d",
                    doc.id, elapsed_seconds / 60, retry_count,
                )
                continue

            # 决策 C：HARD 超限置 FAILED 时打 ERROR 告警日志
            if elapsed_seconds >= HARD:
                logger.error(
                    "Patrol HARD timeout PARSING→re-insert: doc_id=%s task_id=%s "
                    "waited=%.0fmin rag_status=%s current_step=%s retry=%d/3",
                    doc.id, doc.rag_task_id, elapsed_seconds / 60,
                    rag_status, current_step, retry_count + 1,
                )

            # Mark as FAILED first, then retry
            async with get_db_session() as session:
                repo_s = KnowledgeDocumentRepository(session)
                await repo_s.set_status(
                    doc, DocStatus.FAILED,
                    error_message=f"解析超时（已 PARSING {elapsed_seconds/60:.0f} 分钟），自动重试第 {retry_count + 1} 次",
                )

            # Re-submit to RAG
            try:
                from .ontology_compiler import OntologyCompiler
                schema = await OntologyCompiler().get_compiled_schema(
                    doc.tenant_id, doc.knowledge_base_id, auto_compile=True,
                )
            except Exception as exc:
                logger.warning(
                    "Patrol schema check failed for doc %s, proceeding: %s", doc.id, exc,
                )
                schema = None

            try:
                rag_result = await get_rag_client().insert(
                    file_path=doc.file_path,
                    tenant_id=doc.tenant_id,
                    knowledge_base_id=doc.knowledge_base_id,
                    document_id=doc.id,
                    ontology_schema=schema,
                    storage_backend=doc.storage_backend or "local",
                    storage_key=doc.storage_key,
                )

                new_status = DocStatus.READY if not rag_result.get("task_id") else DocStatus.PARSING

                async with get_db_session() as session:
                    repo_s = KnowledgeDocumentRepository(session)
                    fresh = await repo_s.set_status(
                        doc, new_status,
                        rag_task_id=rag_result.get("task_id"),
                        rag_doc_ids=rag_result.get("doc_ids") or rag_result.get("document_ids") or [],
                    )
                    # [jonex] P0-新: N2 规范 — set_status 返回 fresh 实例（已装进 session），
                    # 在 fresh 上写 metadata；禁止 session.add(detached doc)。
                    fm = dict(fresh.extra_metadata or {})
                    fm["parsing_retry_count"] = retry_count + 1
                    # [jonex] P0-b: 重推成功时重置计时锚点，每次 attempt 重新计时。
                    # 否则 elapsed 早已越过 SOFT/HARD，下一个 patrol 周期立即判超时，
                    # 3 次重试在 90 秒内烧完，健康大文档被误杀。
                    fm["parsing_started_at"] = datetime.now().isoformat()
                    fresh.extra_metadata = fm

                retried += 1
                logger.info(
                    "Patrol retry queued: doc_id=%s, retry=%d/%d, status=%s",
                    doc.id, retry_count + 1, 3, new_status.value,
                )

            except Exception as exc:
                logger.warning(
                    "Patrol re-insert failed for doc %s, left as FAILED: %s", doc.id, exc,
                )

        return {
            "checked": len(docs),
            "timed_out": timed_out,
            "retried": retried,
            "exhausted": exhausted,
            "waiting": waiting,
        }

    # ── per-doc reconciliation ─────────────────────────────────

    async def _reconcile_one(self, doc) -> str:
        """Reconcile a single PARSING document. Returns 'updated' | 'skipped'."""
        # [jonex] R2: task_id 为空 → 走查证链（不再直接 _handle_not_found 一拍即死）
        if not doc.rag_task_id:
            return await self._verify_chain(doc, entry="null_task_id")

        try:
            status_info = await get_rag_client().get_task_status(
                task_id=doc.rag_task_id,
                tenant_id=doc.tenant_id,
            )
        except Exception as e:
            logger.warning("Reconcile query failed for doc %s: %s", doc.id, e)
            return "skipped"

        rag_status = status_info.get("status") or status_info.get("state", "")

        # [jonex] P0-2: 白名单清零 —— 只对「在途/健康」状态清零判死计数。
        # failed / not_found / 未知状态不清零，让 _death_candidate 连续多拍累积到 MAX。
        # 原实现 rag_status != "not_found" 会把 failed 也清零，导致 task_failed 判死
        # 计数每 30s 被清零→写1，永远到不了 MAX=3，文档永久卡 PARSING。
        # not_found 不走这里（进入 _verify_chain → _death_candidate），例外不参与计数。
        # [jonex] P1-c: TaskStatus 合法值: created / queued / processing / completed
        # / failed / cancelled
        _ALIVE_STATES = frozenset({"created", "queued", "processing", "completed"})
        if rag_status in _ALIVE_STATES:
            await self._clear_death_verdict(doc)

        if rag_status == "completed":
            return await self._handle_completed(doc, status_info)
        elif rag_status == "failed":
            return await self._handle_failed(doc, status_info)
        elif rag_status == "not_found":
            # [jonex] R2: not_found → 也走查证链（不再直接 _handle_not_found）
            return await self._verify_chain(doc, entry="not_found")
        elif rag_status == "cancelled":
            # [jonex] P1-c: cancelled 任务直接落 FAILED。用户主动取消等同于确认放弃，
            # 不应无限期 keep PARSING 等 patrol 兜底。
            # 代次 fencing：旧代次 cancelled 不覆盖新代次 doc 状态。
            task_generation = int(status_info.get("content_generation", 0) or 0)
            if task_generation < int(getattr(doc, "content_generation", 0) or 0):
                logger.info(
                    "Reconcile→skip cancelled(stale generation): doc_id=%s task_gen=%s doc_gen=%s",
                    doc.id, task_generation, getattr(doc, "content_generation", 0),
                )
                return "skipped"
            logger.info("Reconcile→FAILED(task cancelled): doc_id=%s", doc.id)
            return await self._finalize_failure(
                doc, verdict="task_cancelled",
                error_msg="任务已被取消",
            )
        else:
            # [jonex] 细粒度落库（设计 §9.5）：task 进入本体抽取（ontology_status=extracting）
            # ⟹ parse+push 已完成 ⟹ 文档已可搜索。把仍处于 PARSING 的文档提前落成
            # READY+EXTRACTING，使线性状态「编译中」在首次上传时真实可见。
            # 只提前置 EXTRACTING，绝不置 PENDING——否则 reconcile_ontology 会对 pending
            # 文档直接 claim+提交新的 ontology-only 重抽，与 insert 任务内抽取重复触发。
            # 收尾交给 reconcile_ontology 的 EXTRACTING 分支轮询同一 rag_task_id（insert 任务）。
            if (
                os.getenv("RECONCILE_REFLECT_COMPILING", "true").lower() in ("1", "true", "yes", "on")
                and doc.status in (DocStatus.PARSING.value, DocStatus.INGESTING.value)
                and status_info.get("ontology_status") == "extracting"
            ):
                # 代次 fencing：与 _handle_completed 一致，旧代次任务不提前置位
                task_gen = int(status_info.get("content_generation", 0) or 0)
                doc_gen = int(getattr(doc, "content_generation", 0) or 0)
                if task_gen < doc_gen:
                    logger.debug(
                        "Reconcile→skip reflect(stale gen): doc_id=%s task_gen=%s doc_gen=%s",
                        doc.id, task_gen, doc_gen,
                    )
                    return "skipped"
                doc_ids = status_info.get("lightrag_doc_ids") or status_info.get("doc_ids") or []
                async with get_db_session() as session:
                    repo = KnowledgeDocumentRepository(session)
                    # 保留 rag_task_id（仍指向 insert 任务）；仅更新 status + rag_doc_ids
                    # [jonex] N3: 恢复 READY 时清零判死计数（与 set_status 同 session）
                    fresh = await repo.set_status(doc, DocStatus.READY, rag_doc_ids=doc_ids)
                    fm = dict(fresh.extra_metadata or {})
                    fm.pop("death_verdict_count", None)
                    fm.pop("death_verdict_reason", None)
                    fm.pop("death_verdict_error", None)
                    fresh.extra_metadata = fm
                    await repo.set_ontology_status(doc, OntologyStatus.EXTRACTING)
                    await session.commit()
                logger.info(
                    "Reconcile→READY+EXTRACTING (compiling visible): doc_id=%s", doc.id,
                )
                return "updated"
            # ── [jonex] R1-B：INGESTING 判定 ──
            # openkb KB（parse_only）不推 LightRAG，跳过 INGESTING 判定
            kb_type = (doc.extra_metadata or {}).get("kb_type")
            if not kb_type:
                from .kb_type_service import get_kb_type
                kb_type = await get_kb_type(tenant_id, doc.knowledge_base_id)

            if kb_type != "openkb" and doc.status in (DocStatus.PARSING.value, DocStatus.INGESTING.value):
                current_step = status_info.get("current_step") or ""
                progress = status_info.get("progress", 0) or 0

                if current_step == "push_chunks":
                    async with get_db_session() as session:
                        repo = KnowledgeDocumentRepository(session)
                        await repo.set_status(doc, DocStatus.INGESTING)
                        await session.commit()
                    logger.info(
                        "Reconcile→INGESTING (push_chunks): doc_id=%s, progress=%s",
                        doc.id, progress,
                    )
                    return "updated"

                # 兜底：processing + progress>0 但 current_step 无法精确判定（已过解析）
                if rag_status == "processing" and progress > 0:
                    async with get_db_session() as session:
                        repo = KnowledgeDocumentRepository(session)
                        await repo.set_status(doc, DocStatus.INGESTING)
                        await session.commit()
                    logger.info(
                        "Reconcile→INGESTING (fallback, progress>0): doc_id=%s "
                        "rag_status=%s current_step=%s progress=%s",
                        doc.id, rag_status, current_step, progress,
                    )
                    return "updated"

            # processing / pending — keep current status
            logger.debug(
                "Reconcile→keep %s: doc_id=%s, rag_status=%s, progress=%s",
                doc.status, doc.id, rag_status, status_info.get("progress"),
            )
            return "skipped"

    async def _apply_synonyms(self, tenant_id: str, kb_id: str, ont_data: dict) -> dict:
        """[jonex] 写图前 KB 级同义词归一（效果 Y）。

        直查同义词表构建索引，对 ont_data 的 entities/relations 做归一 + 合并。
        受环境变量 ONTOLOGY_SYNONYM_MERGE_ENABLED（默认 true）总开关控制；
        任何异常降级返回原 ont_data，不阻塞写图。
        """
        if os.getenv("ONTOLOGY_SYNONYM_MERGE_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
            return ont_data
        try:
            from ..repository.ontology_synonym_repository import OntologySynonymRepository
            from .synonym_normalizer import build_synonym_index, normalize_ont_data

            async with get_db_session() as session:
                groups = await OntologySynonymRepository(session).list_all_by_kb(tenant_id, kb_id)
            index = build_synonym_index(groups)
            if not index:
                return ont_data
            before = len(ont_data.get("entities", []) or [])
            result = normalize_ont_data(ont_data, index)
            after = len(result.get("entities", []) or [])
            logger.info(
                "Synonym normalize: kb=%s groups=%d entities %d→%d",
                kb_id, len(groups), before, after,
            )
            return result
        except Exception as e:
            logger.warning("Synonym normalize skipped (degraded): kb=%s err=%s", kb_id, e)
            return ont_data

    async def _handle_completed(self, doc, status_info: dict, reconcile_source: str = "first") -> str:
        """Handle completed task: write Neo4j ontology (if any), then mark READY.

        reconcile_source: "first"（首次入库收尾）或 "ontology_retry"（本体重试收尾），
        用于区分 e2e_ready_ms 口径（重试路径含多轮等待，不可与首次比较）。
        """
        # [jonex] P0-I 代次 fencing：任务代次早于文档当前 content_generation → 整体作废，
        # 不改状态 / 不删数据 / 不覆盖 rag_doc_ids（交给最新代次任务收敛）。
        task_generation = int(status_info.get("content_generation", 0) or 0)
        doc_generation = int(getattr(doc, "content_generation", 0) or 0)
        if task_generation < doc_generation:
            logger.info(
                "Reconcile→skip(stale generation): doc_id=%s task_gen=%s doc_gen=%s",
                doc.id, task_generation, doc_generation,
            )
            return "skipped"

        ont_status = status_info.get("ontology_status", "pending")
        ont_data = status_info.get("ontology_data")
        ont_error = status_info.get("ontology_error")
        kb_id = (doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", ""))
        # worker 透传的分阶段耗时（见 docs/ingestion-timing-metrics-design.md §3.4 C）
        # 兼容 v1(dict) 与 v2(list) 两种 stage_timings 形态
        stage_timings_raw = status_info.get("stage_timings")
        worker_total_ms, worker_timings = _normalize_stage_timings(stage_timings_raw)
        pipeline_version = _detect_pipeline_version(stage_timings_raw)

        # [jonex] P1-E schema 版本 fencing：任务 schema 版本低于文档目标 → 旧结果，丢弃。
        # 任务版本 >= 目标版本 → 正常写入（版本升级时 task_ver 可大于 target_ver）。
        task_schema_version = int(status_info.get("schema_version", 0) or 0)
        task_schema_hash = status_info.get("schema_hash") or None
        target_schema_version = getattr(doc, "ontology_target_schema_version", None)
        schema_fenced = bool(
            target_schema_version is not None
            and task_schema_version
            and task_schema_version < target_schema_version  # only fence OLDER results
        )
        if schema_fenced and ont_status == "completed" and ont_data:
            logger.info(
                "Ontology result fenced (stale schema): doc_id=%s task_ver=%s target_ver=%s",
                doc.id, task_schema_version, target_schema_version,
            )

        # Step 1: Neo4j ontology write (before PG, consistency design)
        neo4j_ok = True
        neo4j_write_ms = None
        if not schema_fenced and ont_status == "completed" and ont_data:
            _t_neo4j = time.perf_counter()
            try:
                gdao = OntologyGraphRepository(get_neo4j_driver())
                # 先清掉本文档此前写入的本体贡献，再重写。
                # 否则实体重新归类（entity_type 变化，如 unknown→Product）会因 MERGE 主键含
                # entity_type 而新建节点、旧节点残留，导致重复。
                await gdao.delete_by_document(doc.tenant_id, doc.id)
                # P1：本文档写入前清空端点解析缓存，避免命中上一文档/旧状态的解析结果
                gdao.reset_endpoint_cache()
                # [jonex] KB 级同义词归一（效果 Y：实体归并）——写图前把同义词组内不同表述
                # 归一到 canonical 并统一 entity_type，使 MERGE 主键相同的节点自然合并。
                # 直查 DB（强一致，正确性锚点）；失败降级不阻塞写图。
                ont_data = await self._apply_synonyms(doc.tenant_id, kb_id, ont_data)
                # 批量预取 embedding_hash，消除 merge_entity 循环内的 N+1 查询
                hash_cache = await gdao.get_embedding_hashes(doc.tenant_id, kb_id)
                for ent in ont_data.get("entities", []):
                    await gdao.merge_entity(doc.tenant_id, kb_id, doc.id, ent, hash_cache=hash_cache)
                for rel in ont_data.get("relations", []):
                    await gdao.merge_relation(doc.tenant_id, kb_id, doc.id, rel)
                neo4j_write_ms = int((time.perf_counter() - _t_neo4j) * 1000)
                logger.info(
                    "Ontology written: doc_id=%s, entities=%d, relations=%d",
                    doc.id,
                    len(ont_data.get("entities", [])),
                    len(ont_data.get("relations", [])),
                )
            except Exception as e:
                neo4j_write_ms = int((time.perf_counter() - _t_neo4j) * 1000)
                logger.error("Neo4j ontology write failed for doc %s: %s", doc.id, e)
                neo4j_ok = False

        # [jonex] kb_type：openkb 管线只解析、不做本体抽取
        # 优先读 extra_metadata 快照（缓存），再查 knowledge_info 权威源兜底
        kb_type = (doc.extra_metadata or {}).get("kb_type")
        if not kb_type:
            from .kb_type_service import get_kb_type
            kb_type = await get_kb_type(tenant_id, doc.knowledge_base_id)

        # Step 2: PG status update
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            # [jonex] N3: 恢复 READY 时清零判死计数
            fresh = await repo.set_status(
                doc,
                DocStatus.READY,
                rag_doc_ids=status_info.get("lightrag_doc_ids") or status_info.get("doc_ids") or [],
            )
            fm = dict(fresh.extra_metadata or {})
            fm.pop("death_verdict_count", None)
            fm.pop("death_verdict_reason", None)
            fm.pop("death_verdict_error", None)
            fresh.extra_metadata = fm

            if kb_type == "openkb":
                # openkb 无本体抽取，直接标 READY，避免 reconcile_ontology 无谓重试后置 FAILED
                await repo.set_ontology_status(doc, OntologyStatus.READY)
            elif schema_fenced and ont_status == "completed":
                # 本体结果按目标版本作废：保持 PENDING 等目标版本任务重抽，不写 applied
                await repo.set_ontology_status(doc, OntologyStatus.PENDING)
            elif ont_status == "completed" and neo4j_ok:
                # [jonex] P1-E：写图成功后记录已应用的 schema 版本/hash，
                # 同时将 target 版本同步到任务版本（编译 schema 升级后 task_ver > target_ver 为正常态）。
                await repo.set_ontology_status(
                    doc, OntologyStatus.READY,
                    applied_schema_version=(task_schema_version or None),
                    applied_schema_hash=task_schema_hash,
                )
                if task_schema_version and (
                    target_schema_version is None or task_schema_version > target_schema_version
                ):
                    doc.ontology_target_schema_version = task_schema_version
            elif ont_status == "failed":
                await repo.set_ontology_status(doc, OntologyStatus.FAILED, error=ont_error or "本体抽取失败")
            elif not neo4j_ok:
                await repo.set_ontology_status(doc, OntologyStatus.FAILED, error="Neo4j 本体写入失败")

            await session.commit()

        # ── [jonex] openkb 管线：解析完成 → 推送 OpenKB Wiki 编译 ──
        if kb_type == "openkb":
            await self._dispatch_openkb_compile(doc, status_info)

        # 端到端墙钟：created_at 为 naive UTC（TimestampMixin: default=datetime.utcnow），
        # 必须用 datetime.utcnow() 作差（详见设计文档 §3.1 #9）。
        # 端到端墙钟：TimestampMixin 实为 datetime.now（naive 本地时间，见 jonex_core/common/entity.py:24），
        # 用 datetime.now() 作差保持同口径，避免 utcnow() 导致的 -8h 偏差（P0-2 同类 bug）。
        e2e_ready_ms = None
        if doc.created_at is not None:
            e2e_ready_ms = int((datetime.now() - doc.created_at).total_seconds() * 1000)

        logger.info("Reconcile→READY: doc_id=%s, chunks=%d", doc.id,
                     len(status_info.get("lightrag_doc_ids") or status_info.get("doc_ids") or []))
        logger.info(
            "reconcile_timing doc_id=%s source=%s pipeline_version=%s neo4j_write_ms=%s e2e_ready_ms=%s worker_total_ms=%s",
            doc.id,
            reconcile_source,
            pipeline_version or "unknown",
            neo4j_write_ms,
            e2e_ready_ms,
            worker_total_ms,
            extra={
                "event": "reconcile_timing",
                "tenant_id": doc.tenant_id,
                "knowledge_base_id": kb_id,
                "document_id": str(doc.id),
                "status": "ready",
                "reconcile_source": reconcile_source,
                "pipeline_version": pipeline_version,
                "neo4j_write_ms": neo4j_write_ms,
                "e2e_ready_ms": e2e_ready_ms,
                "worker_total_ms": worker_total_ms,
                **worker_timings,
            },
        )
        schedule_emit({
            "tenant_id": doc.tenant_id,
            "log_type": "TASK",
            "action": "document.parse_done",
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(doc.id),
            "duration_ms": worker_total_ms,
            "request_params": {
                "reconcile_source": reconcile_source,
                "neo4j_write_ms": neo4j_write_ms,
                "e2e_ready_ms": e2e_ready_ms,
            },
        })
        return "updated"

    @staticmethod
    def _cleanup_stuck_timeout(status_info: dict) -> float:
        """[jonex] R5-a：cleanup 动态超时公式 = BASE + PER_DOC × cleanup_total，套 CEIL 上限。

        供 patrol（processing+cleanup 盲区）与 _handle_failed（failed+cleanup 已覆盖）
        共享同一口径。
        """
        base = int(os.getenv("RECONCILE_CLEANUP_BASE_SEC", "600"))
        per_doc = int(os.getenv("RECONCILE_CLEANUP_PER_DOC_SEC", "15"))
        ceil = int(os.getenv("RECONCILE_CLEANUP_CEIL_SEC", "21600"))
        cleanup_total = int(status_info.get("cleanup_total", 0) or 0)
        return min(ceil, base + per_doc * cleanup_total)

    async def _handle_failed(self, doc, status_info: dict) -> str:
        """Handle failed task. Try storage fallback first before finalizing failure."""
        # [jonex] P0-J：cleanup 进行中的任务对 storage fallback 免疫——保持 PARSING，
        # 不因 LightRAG 里仍有（旧）数据就恢复 READY，避免绕过未完成的清理。
        if status_info.get("current_step") == "cleanup":
            # [jonex] 3-A + R5-a：cleanup 不再无限期 skip，按删除量动态超时判死。
            stuck_timeout = self._cleanup_stuck_timeout(status_info)
            cleanup_total = int(status_info.get("cleanup_total", 0) or 0)
            pending = int(status_info.get("cleanup_pending_count", 0) or 0)

            # 计时基准：doc.updated_at（进入 cleanup 后对账 skip 不再刷新，可作卡死时长代理）
            now = datetime.now()  # [jonex] P0-2: naive 本地时间，与 DB 写入口径一致
            updated_at = doc.updated_at
            elapsed = 0.0
            if updated_at is not None:
                if updated_at.tzinfo is not None:
                    updated_at = updated_at.astimezone().replace(tzinfo=None)
                elapsed = (now - updated_at).total_seconds()

            if elapsed >= stuck_timeout:
                err = (
                    f"补偿清理未收敛（cleanup 卡死 {elapsed/60:.0f} 分钟，"
                    f"待删 {pending}/{cleanup_total}），请删除文档后重新上传"
                )
                async with get_db_session() as session:
                    repo = KnowledgeDocumentRepository(session)
                    await repo.set_status(doc, DocStatus.FAILED, error_message=err)
                    await session.commit()
                logger.warning(
                    "Reconcile→FAILED(cleanup stuck): doc_id=%s elapsed=%.0fmin "
                    "pending=%d/%d timeout=%ds",
                    doc.id, elapsed / 60, pending, cleanup_total, stuck_timeout,
                )
                return "updated"

            logger.info(
                "Reconcile→keep %s (cleanup in progress): doc_id=%s elapsed=%.0fmin "
                "pending=%d/%d timeout=%ds",
                doc.status, doc.id, elapsed / 60, pending, cleanup_total, stuck_timeout,
            )
            return "skipped"
        # 代次 fencing：旧代次任务的失败结果不改文档状态（交给最新代次收敛）
        task_generation = int(status_info.get("content_generation", 0) or 0)
        if task_generation < int(getattr(doc, "content_generation", 0) or 0):
            logger.info(
                "Reconcile→skip failed(stale generation): doc_id=%s task_gen=%s doc_gen=%s",
                doc.id, task_generation, getattr(doc, "content_generation", 0),
            )
            return "skipped"

        # If LightRAG has the data, recover to READY instead of FAILED
        rag_doc_ids = await self._verify_via_storage(doc)
        if rag_doc_ids:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                # [jonex] N3: 恢复 READY 时清零判死计数（与 set_status 同 session）
                fresh = await repo.set_status(doc, DocStatus.READY, rag_doc_ids=rag_doc_ids)
                fm = dict(fresh.extra_metadata or {})
                fm.pop("death_verdict_count", None)
                fm.pop("death_verdict_reason", None)
                fm.pop("death_verdict_error", None)
                fresh.extra_metadata = fm
                await session.commit()
            logger.info("Reconcile→READY(via failed-task storage fallback): doc_id=%s", doc.id)
            return "updated"

        # [jonex] P1-5: 确定性终态跳过 3 拍确认，直接落 FAILED。
        # 连续确认是为对抗瞬时不可达（LightRAG 繁忙等），对确定性失败只是白拖 90s。
        # [jonex] P0-a: error_code 匹配 + error_msg 文本前缀兜底（兼容镜像未更新）。
        error_msg = status_info.get("error") or status_info.get("message") or "RAG 解析失败"
        error_code = status_info.get("error_code") or ""

        _DETERMINISTIC_TEXT_PREFIXES = (
            "File not found:", "OBJECT_FETCH_FAILED",
        )
        _DETERMINISTIC_TERMINAL_CODES = frozenset({
            "FILE_NOT_FOUND", "OBJECT_FETCH_FAILED",
            "PROFILE_NOT_FOUND", "PRESET_NOT_FOUND",
            "MODEL_NOT_FOUND", "CONFIG_INVALID",
        })
        if (error_code in _DETERMINISTIC_TERMINAL_CODES
                or error_msg.startswith(_DETERMINISTIC_TEXT_PREFIXES)):
            logger.info(
                "Reconcile→FAILED(deterministic terminal): doc_id=%s error_code=%s error=%s",
                doc.id, error_code or "(text match)", error_msg,
            )
            return await self._finalize_failure(doc, verdict="task_failed", error_msg=error_msg)

        return await self._death_candidate(doc, verdict="task_failed", error_msg=error_msg)

    # ── [jonex] R2 查证链 + R3 连续确认判死器 ──────────────────────

    @staticmethod
    def _build_idempotency_key(doc) -> str | None:
        """按 upload_document / reparse_document 同口径生成幂等键，供 R2-b 反查。"""
        kb_id = doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", "")
        gen = getattr(doc, "content_generation", 0) or 0
        if not kb_id:
            return None
        return f"insert:{doc.tenant_id}:{kb_id}:{doc.id}:{gen}"

    @staticmethod
    async def _clear_death_verdict(doc) -> None:
        """[jonex] P1-d: 恢复/成功路径清零判死计数+原因+错误。

        只在有残留时才写 DB。调用时机：
        - _reconcile_one 拿到健康 task 状态（created/queued/processing/completed）
        - _verify_chain R2-a/b/c 任一路命中
        - _handle_completed 成功收尾
        """
        meta = dict(doc.extra_metadata or {})
        if "death_verdict_count" not in meta and "death_verdict_reason" not in meta and "death_verdict_error" not in meta:
            return
        meta.pop("death_verdict_count", None)
        meta.pop("death_verdict_reason", None)
        meta.pop("death_verdict_error", None)
        async with get_db_session() as session:
            doc.extra_metadata = meta
            session.add(doc)
            await session.commit()

    async def _verify_chain(self, doc, *, entry: str) -> str:
        """R2 查证链：a（宽限期）→ b（幂等键反查）→ c（storage fallback）→ d（判死候选）。

        入口：entry="null_task_id"（task_id 为空，upload/reparse 空窗期）
              entry="not_found"（task_id 查询返回 not_found，容器重启丢状态）
        任一路命中即 skip/updated；全未命中进入 R3 _death_candidate。
        """
        doc_id = doc.id
        meta = dict(doc.extra_metadata or {})

        # ── R2-a: 提交宽限期（submit_started_at 在 GRACE 窗口内 → in-flight skip）──
        # [jonex] P1-1: 统一用 datetime.now()（naive 本地时间），与写入侧的 datetime.now().isoformat() 口径一致
        submit_at = meta.get("submit_started_at")
        if submit_at:
            try:
                submitted = datetime.fromisoformat(str(submit_at))
                grace = int(os.getenv("RECONCILE_SUBMIT_GRACE_SEC", "180"))
                if (datetime.now() - submitted).total_seconds() < grace:
                    # [jonex] N3: 任务 in-flight 正常态 → 清零判死计数
                    # [jonex] N4: elapsed 用 datetime.now()（与 submitted 同口径）
                    elapsed = (datetime.now() - submitted).total_seconds()
                    logger.info(
                        "Reconcile→skip(in-flight grace): doc_id=%s entry=%s elapsed=%.0fs",
                        doc_id, entry, elapsed,
                    )
                    await self._clear_death_verdict(doc)
                    return "skipped"
            except (ValueError, TypeError, OSError):
                pass  # 损坏的时间戳 → 穿透继续

        # ── R2-b: 幂等键反查 atomic-rag 在途任务 → 回填 rag_task_id ──
        if os.getenv("RECONCILE_IDEMPOTENCY_LOOKUP_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
            ik = self._build_idempotency_key(doc)
            if ik:
                try:
                    result = await get_rag_client().get_task_by_idempotency_key(ik, doc.tenant_id)
                    if result.get("found"):
                        async with get_db_session() as session:
                            repo = KnowledgeDocumentRepository(session)
                            # [jonex] P1-2: 回填 task_id 后清除锚点
                            # [jonex] N2: 通过 fresh 实例持久化 metadata 变更
                            fresh = await repo.set_status(
                                doc, doc.status, rag_task_id=result["task_id"],
                            )
                            # [jonex] P1-2: 清除锚点 + [jonex] N3: 反查到在途任务 → 清零判死计数
                            fm = dict(fresh.extra_metadata or {})
                            had_anchor = fm.pop("submit_started_at", None) is not None
                            had_count = fm.pop("death_verdict_count", None) is not None
                            if had_anchor or had_count:
                                fm.pop("death_verdict_reason", None)
                                fm.pop("death_verdict_error", None)
                                fresh.extra_metadata = fm
                            await session.commit()
                        logger.info(
                            "Reconcile→backfill task_id(idempotency lookup hit): doc_id=%s "
                            "task_id=%s entry=%s",
                            doc_id, result["task_id"], entry,
                        )
                        return "updated"
                except Exception:
                    pass  # RAG 不可达 → 穿透继续

        # ── R2-c: storage fallback（现有 _verify_via_storage，原样保留）──
        _t0 = time.perf_counter()
        rag_doc_ids = await self._verify_via_storage(doc)
        storage_fallback_ms = int((time.perf_counter() - _t0) * 1000)
        if rag_doc_ids is not None:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                # [jonex] N3: 恢复 READY 时清零判死计数（与 set_status 同 session）
                fresh = await repo.set_status(doc, DocStatus.READY, rag_doc_ids=rag_doc_ids)
                fm = dict(fresh.extra_metadata or {})
                fm.pop("death_verdict_count", None)
                fm.pop("death_verdict_reason", None)
                fm.pop("death_verdict_error", None)
                fresh.extra_metadata = fm
                await session.commit()
            logger.info("Reconcile→READY(via storage fallback): doc_id=%s entry=%s", doc_id, entry)
            schedule_emit({
                "tenant_id": doc.tenant_id,
                "log_type": "TASK",
                "action": "document.parse_recover",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.DOCUMENT.value,
                "resource_id": str(doc_id),
                "duration_ms": storage_fallback_ms,
            })
            return "updated"

        # ── R2-d: 全未命中 → 进入判死候选（R3 连续确认）──
        return await self._death_candidate(doc, verdict=f"task_{entry}")

    async def _death_candidate(self, doc, *, verdict: str, error_msg: str | None = None) -> str:
        """R3: 连续 N 拍同一结论才判死。计数持久化在 extra_metadata。

        error_msg: 仅 task_failed 路径传入真实失败原因；null_task_id/not_found 路径
        不传 → _finalize_failure 回退 death_verdict_error（跨 verdict 保留）。
        """
        MAX = int(os.getenv("RAG_TASK_PROBE_FAIL_MAX", "3"))

        meta = dict(doc.extra_metadata or {})
        count = meta.get("death_verdict_count", 0)
        reason = meta.get("death_verdict_reason", "")

        if reason == verdict:
            count += 1
        else:
            # [jonex] P1-d: verdict 切换时清旧 death_verdict_error，
            # 防止上次 task_failed 的 FILE_NOT_FOUND 串台到这次 task_not_found 文案。
            count = 1
            reason = verdict
            meta.pop("death_verdict_error", None)

        meta["death_verdict_count"] = count
        meta["death_verdict_reason"] = reason

        # [jonex] P1-4: 保留首个真实错误跨 verdict 切换不丢失。
        if error_msg:
            meta["death_verdict_error"] = error_msg

        async with get_db_session() as session:
            doc.extra_metadata = meta
            session.add(doc)
            await session.commit()

        if count >= MAX:
            return await self._finalize_failure(doc, verdict, error_msg=error_msg)

        logger.info(
            "Reconcile→death candidate: doc_id=%s verdict=%s count=%d/%d",
            doc.id, verdict, count, MAX,
        )
        return "skipped"

    async def _finalize_failure(self, doc, verdict: str, *, error_msg: str | None = None) -> str:
        """落定 FAILED：清锚点/判死计数，写入 error_message。

        error_msg 优先级: 传入 > death_verdict_error（跨 verdict 保留）> verdict 分类。
        """
        meta = dict(doc.extra_metadata or {})

        # [jonex] P1-4: 优先取传入 error_msg，其次取 death_verdict_error（跨 verdict 保留），
        # 兜底生成通用文案。防止 verdict 从 task_failed 切到 task_not_found 时真实错误被抹掉。
        effective_error = error_msg or meta.get("death_verdict_error", "")
        if effective_error:
            err = f"{effective_error}（{verdict}）"
        else:
            err = f"RAG 任务确认丢失（{verdict}），原 task_id={doc.rag_task_id}，请重新上传"

        meta.pop("submit_started_at", None)
        meta.pop("death_verdict_count", None)
        meta.pop("death_verdict_reason", None)
        meta.pop("death_verdict_error", None)

        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            # [jonex] N2: set_status 返回 fresh 实例（get_by_id），需在 fresh 上写 metadata
            # 直接改 doc.extra_metadata 是 detached 操作，不会被持久化
            fresh = await repo.set_status(doc, DocStatus.FAILED, error_message=err)
            fresh.extra_metadata = meta
            await session.commit()

        logger.info("Reconcile→FAILED(death confirmed): doc_id=%s verdict=%s", doc.id, verdict)
        schedule_emit({
            "tenant_id": doc.tenant_id,
            "log_type": "TASK",
            "action": "document.parse_recover",
            "outcome": "FAILED",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(doc.id),
            "error_message": err[:1000],
        })
        return "updated"

    # ── ontology retry ─────────────────────────────────────────

    async def _retry_ontology_one(self, doc) -> bool:
        """Retry ontology extraction for a single doc. Returns True if a new task was queued."""
        kb_id = (doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", ""))

        # Step 0: Ensure compiled schema exists before retry (Phase 4.2)
        schema = None
        schema_version = 0
        try:
            from .ontology_compiler import OntologyCompiler
            schema = await OntologyCompiler().get_compiled_schema(doc.tenant_id, kb_id, auto_compile=True)
            if schema is None:
                logger.warning(
                    "Ontology retry skipped - no compiled schema for doc %s (KB %s)",
                    doc.id, kb_id,
                )
                return False
            schema_version = int(schema.get("schema_version", 0) or 0)
        except Exception as e:
            logger.warning("Compiled schema check failed for doc %s, proceeding anyway: %s", doc.id, e)
            if schema is None:
                return False

        # Check retry limit
        retry_count = doc.ontology_retry_count or 0
        if retry_count >= MAX_ONTOLOGY_RETRIES:
            logger.warning(
                "Ontology retry limit reached (%d/%d) for doc %s, marking failed",
                retry_count, MAX_ONTOLOGY_RETRIES, doc.id,
            )
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                await repo.set_ontology_status(
                    doc, OntologyStatus.FAILED,
                    error=f"本体重试次数已达上限 ({retry_count}/{MAX_ONTOLOGY_RETRIES})",
                )
                await session.commit()
            return False

        # 仅当文档已处于 EXTRACTING 时，才轮询既有任务并据其结果收尾。
        # EXTRACTING ⟹ 本对账循环此前已发起过一次 ontology-only 重抽，且彼时已把
        # rag_task_id 改写为该重抽任务，因此此处 rag_task_id 指向的是“重抽任务”，可安全收尾。
        #
        # 对 PENDING / FAILED 文档：rag_task_id 可能仍是最初的“入库任务”，其 Redis 缓存里
        # 带着旧的 ontology_data。若用它收尾，会把旧的（无描述 / 旧 schema）结果回写 Neo4j，
        # 且 retry_count 不增、永不触发新抽取（历史 bug）。故 PENDING/FAILED 一律跳过收尾，
        # 直接走下方“触发全新重抽”，不再依赖运维手动清空 rag_task_id。
        if doc.ontology_status == OntologyStatus.EXTRACTING.value and doc.rag_task_id:
            try:
                status_info = await get_rag_client().get_task_status(
                    task_id=doc.rag_task_id,
                    tenant_id=doc.tenant_id,
                )
                rag_status = status_info.get("status") or status_info.get("state", "")
                if rag_status in ("processing", "pending"):
                    logger.debug("Ontology retry task still running: doc_id=%s, task_id=%s", doc.id, doc.rag_task_id)
                    return False
                if rag_status == "completed":
                    ont_status = status_info.get("ontology_status")
                    if ont_status == "completed" and status_info.get("ontology_data"):
                        # 在途重抽任务已完成 — 收尾写入 Neo4j + 置 READY
                        await self._handle_completed(doc, status_info, reconcile_source="ontology_retry")
                        return True
                    # 已完成但无 ontology_data（例如无候选实体）
                    ont_error = status_info.get("ontology_error") or status_info.get("error") or "无候选实体"
                    if retry_count + 1 >= MAX_ONTOLOGY_RETRIES:
                        async with get_db_session() as session:
                            repo = KnowledgeDocumentRepository(session)
                            await repo.set_ontology_status(doc, OntologyStatus.FAILED, error=ont_error)
                            await session.commit()
                        logger.warning("Ontology retry exhausted for doc %s: %s", doc.id, ont_error)
                        return False
                    # 落空 → 继续往下触发新一轮重抽
            except Exception as e:
                logger.warning("Task status query failed for doc %s, skipping: %s", doc.id, e)
                return False

        # [jonex] P1-F 领取协议：CAS 领取（当前状态 → EXTRACTING 占位）→ 提交 atomic-rag →
        # 拿到 task_id 后写 rag_task_id + ++retry；提交失败回退 PENDING、不 ++retry。
        prev_status = doc.ontology_status
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            claimed = await repo.claim_ontology_for_retry(doc)
            await session.commit()
        if not claimed:
            # 其它实例已领取该文档 → 跳过（多实例只领一次）
            logger.debug("Ontology retry skipped (claimed by another instance): doc_id=%s", doc.id)
            return False

        # Trigger new ontology retry (ontology-only, 携带 schema + 版本 + 代次)
        try:
            result = await get_rag_client().retry_ontology_extract(
                document_id=doc.id,
                knowledge_base_id=kb_id,
                tenant_id=doc.tenant_id,
                file_path=doc.file_path,
                ontology_schema=schema,
                schema_version=schema_version,
                content_generation=int(getattr(doc, "content_generation", 0) or 0),
            )
        except Exception as e:
            logger.warning("Retry ontology extract failed for doc %s: %s", doc.id, e)
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                await repo.revert_ontology_status(doc, prev_status)
                await session.commit()
            return False

        retry_task_id = (result or {}).get("task_id", "")
        if not retry_task_id:
            logger.warning("Retry ontology did not return task_id for doc %s", doc.id)
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                await repo.revert_ontology_status(doc, prev_status)
                await session.commit()
            return False

        # 提交成功：保持 EXTRACTING（已由 claim 置位），写 rag_task_id + ++retry
        async with get_db_session() as session:
            repo = KnowledgeDocumentRepository(session)
            await repo.set_ontology_status(doc, OntologyStatus.EXTRACTING, increment_retry=True)
            await repo.set_status(doc, doc.status, rag_task_id=retry_task_id)
            await session.commit()

        logger.info("Ontology retry queued: doc_id=%s, new_task_id=%s", doc.id, retry_task_id)
        return True

    # ── storage fallback ───────────────────────────────────────

    async def _verify_via_storage(self, doc) -> list[str] | None:
        """Confirm document existence via LightRAG storage reader.

        Used when atomic-rag task state is lost (Redis expired / container restarted)
        but the document may have been successfully ingested into LightRAG.

        Matching strategy: extract doc=<id> from LightRAG's file_path field and
        compare with doc.id.  This is more reliable than file_name matching
        because LightRAG prepends a UUID prefix and may change underscores
        (e.g. "年报" → "___").

        Returns:
            rag_doc_ids list if confirmed, None otherwise.
        """
        import re

        kb_id = (doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", ""))

        try:
            result = await get_rag_client().get_storage_documents(
                knowledge_base_id=kb_id,
                tenant_id=doc.tenant_id,
                keyword=doc.file_name,
                page=1,
                page_size=500,
            )
        except Exception as e:
            logger.warning("Storage lookup failed for doc %s: %s", doc.id, e)
            return None

        items = result.get("items", [])
        total = result.get("total", len(items))
        if total > len(items):
            logger.warning(
                "Storage fallback: total %d exceeds page size for doc %s, may miss rag_doc_ids",
                total, doc.id,
            )

        # Match by document_id in file_path (doc=<uuid>), then fall back to
        # file_name for backward compatibility.
        matched = []
        for item in items:
            fp = item.get("file_path") or ""
            m = re.search(r'doc=([a-f0-9-]+)\|', fp)
            if m and m.group(1) == doc.id:
                matched.append(item)
        if not matched:
            # Fallback: file_name match (legacy)
            matched = [
                item for item in items
                if (
                    item.get("file_name") == doc.file_name
                    or (item.get("file_name") or "").endswith("_" + doc.file_name)
                )
            ]

        if not matched:
            raw_items = [
                {
                    "file_name": i.get("file_name"),
                    "file_path": (i.get("file_path") or "")[:120],
                    "status": i.get("status"),
                }
                for i in items[:5]
            ]
            logger.info(
                "Storage fallback miss: doc_id=%s, file_name=%s, total=%d, sample(5)=%s",
                doc.id, doc.file_name, len(items), raw_items,
            )
            return None

        rag_doc_ids = [item["id"] for item in matched if item.get("id")]
        logger.info("Storage fallback hit: doc_id=%s, rag_doc_ids=%s", doc.id, rag_doc_ids)
        return rag_doc_ids

    # ── [jonex] OpenKB compile dispatch ──

    async def _dispatch_openkb_compile(self, doc, status_info: dict):
        """解析完成后调用 OpenKB compile_parsed_document。

        parsed markdown/assets 由解析任务在共享卷 inputs 上产出，经 task status 透传
        （parsed_markdown_path / assets_dir，绝对路径）。这里换算为「相对 inputs 卷根」
        的路径交给 OpenKB，做到跨容器挂载点无关。

        重要：拿不到 parsed markdown 时如实标记失败，绝不把原始二进制文件（PDF/docx）
        当 markdown 送入编译；编译结果（compiled/failed）写回文档，避免静默成功/失败。
        """
        from .openkb_service import KnowledgeCompilerService

        kb_id = (doc.knowledge_base_id or (doc.extra_metadata or {}).get("knowledge_base_id", ""))

        # 解析任务透传的产物路径（绝对路径，位于共享卷 inputs 挂载点内）
        abs_md = status_info.get("parsed_markdown_path") or ""
        abs_assets = status_info.get("assets_dir") or ""

        rel_md = self._relativize_input_path(abs_md)
        rel_assets = self._relativize_input_path(abs_assets)

        if not rel_md:
            # 没有可用 parsed markdown：如实标记失败，等待解析器把 markdown 落到共享卷并暴露路径
            await self._record_openkb_status(
                doc, status="failed",
                error="OPENKB_NO_PARSED_MARKDOWN: 解析任务未提供 parsed_markdown_path"
                      "（需解析器把 markdown 落到共享卷 inputs 并经 task status 暴露）",
            )
            logger.error(
                "[jonex] OpenKB compile 跳过：doc=%s 无 parsed_markdown_path（task 未暴露产物路径）",
                doc.id,
            )
            return

        # [jonex] 任务化：claim（列）→ 提交编译任务（容器内后台执行）→ task_id 写列
        # → 立即返回（不等待编译）。终态（compiled/failed）由 patrol_openkb_compile
        # 轮询任务状态回写，sidecar 同步超时不再能打断编译。
        compiler = KnowledgeCompilerService()
        if not await compiler.claim_compile(doc.tenant_id, doc.id):
            logger.info("[jonex] OpenKB compile claim 未抢到（已 compiling 或并发双投）doc=%s", doc.id)
            return
        try:
            result = await compiler.submit_compile(
                kb_name=kb_id,
                tenant_id=doc.tenant_id,
                kb_id=kb_id,
                parsed_artifact={
                    "document_id": doc.id,
                    "source_file_path": doc.file_path or "",
                    "parsed_markdown_path": rel_md,       # 相对 inputs 卷根
                    "assets_dir": rel_assets,             # 相对 inputs 卷根（可空）
                    "parser": status_info.get("parser", "mineru"),
                    "metadata": {
                        "pages": status_info.get("pages", 0),
                        "title": doc.file_name or "",
                        "rag_task_id": doc.rag_task_id,
                    },
                },
            )
            await compiler.set_task_id(doc.tenant_id, doc.id, result["task_id"])
            logger.info(
                "[jonex] OpenKB compile 已提交（任务化）: kb=%s doc=%s task=%s",
                kb_id, doc.id, result.get("task_id"),
            )
        except Exception as exc:
            # 投递失败必须回滚 claim，否则永久卡 compiling
            await self._record_openkb_status(doc, status="failed", error=str(exc)[:1000])
            logger.exception(
                "[jonex] OpenKB compile 提交失败: kb=%s doc=%s error=%s", kb_id, doc.id, exc,
            )

    @staticmethod
    def _relativize_input_path(path: str) -> str:
        """把共享卷 inputs 上的绝对路径换算为「相对卷根」的路径（跨容器挂载点无关）。

        knowledge_base/atomic-rag 的 inputs 挂载点由 KB_INPUT_DIR 指定（默认 /app/inputs），
        openkb 挂在 OPENKB_INPUT_ROOT（默认 /app/data/inputs）。传相对路径让两端各自解析。
        空值返回空串；已是相对路径原样返回。
        """
        if not path:
            return ""
        root = os.getenv("KB_INPUT_DIR", "/app/inputs").rstrip("/")
        p = str(path)
        if p.startswith(root + "/"):
            return p[len(root) + 1:]
        return p if not p.startswith("/") else p.lstrip("/")

    async def _record_openkb_status(self, doc, *, status: str, error: str = "", warnings=None):
        """把 LLM-Wiki 编译状态写回文档（列化），避免静默成功/失败。

        [jonex] 统一语义（与 openkb_service 版一致）：
        - compiling/compiled/stale → 清除 llm_wiki_compile_error；
        - compiling/stale → 清除 llm_wiki_compile_warnings；
        - failed → 写 error（截断 1000），**保留 warnings 不动**；
        - compiled → 写 warnings（或 None）+ compiled_at（UTC）。
        """
        try:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                fresh = await repo.get_required(doc.id, doc.tenant_id)
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
                # 保持内存态一致（doc.extra_metadata 仍兼容旧 JSONB 读点）
                meta = dict(fresh.extra_metadata or {})
                meta["openkb_compile_status"] = status
                doc.extra_metadata = meta
        except Exception:
            logger.warning("记录 LLM-Wiki 编译状态失败 doc=%s", doc.id, exc_info=True)

    async def patrol_openkb_compile(self, limit: int = 50) -> dict:
        """[jonex] 轮询 OpenKB 编译任务状态并回写 PG；stale 文档补提交；超时判死。

        任务化后编译终态只由这里回写（30s 巡检）：
        - completed → llm_wiki_compile_status='compiled'（含 warnings / compiled_at）
        - failed → 'failed'（error 取任务）
        - running → 按任务 started_at 起算超时（排队时间不算）
        - pending → 还在 mutation 锁队列排队，不判死（批量上传队尾任务）
        - task 未知（容器重启清理）→ 判死 failed（error='task lost'）
        - task_id IS NULL → 只按 requested_at 超时（claim 后 submit 未回写的窗口）
        env 兼容：OPENKB_COMPILE_TIMEOUT_SEC 优先，回退 OPENKB_RECOMPILE_TIMEOUT_SEC。
        """
        TIMEOUT = int(os.getenv("OPENKB_COMPILE_TIMEOUT_SEC",
                     os.getenv("OPENKB_RECOMPILE_TIMEOUT_SEC", "1800")))
        from sqlalchemy import text

        # ① compiling 文档（索引查询；带 knowledge_base_id 供分组 invoke）
        sql = text("""
            SELECT id, tenant_id, knowledge_base_id, llm_wiki_task_id,
                   llm_wiki_compile_requested_at
              FROM knowledge_base.knowledge_documents
             WHERE is_deleted = 0 AND llm_wiki_compile_status = 'compiling'
             LIMIT :limit
        """)
        async with get_db_session() as session:
            rows = (await session.execute(sql, {"limit": limit})).all()

        now = datetime.utcnow()
        compiled = failed = timed_out = 0

        # ② 按 (tenant_id, kb_id) 分组，一组一次 list_compile_tasks
        by_kb: dict[tuple[str, str], list] = {}
        for r in rows:
            by_kb.setdefault((r.tenant_id, r.knowledge_base_id or ""), []).append(r)

        from .openkb_service import KnowledgeCompilerService
        compiler = KnowledgeCompilerService()
        for (tenant_id, kb_id), group in by_kb.items():
            task_map: dict[str, dict] = {}
            try:
                res = await compiler.list_compile_tasks(
                    kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id)
                for t in (res or {}).get("tasks", []):
                    task_map[t.get("task_id")] = t
            except Exception as exc:
                # 容器不可达：保持现状不判死（对账降级，不误杀）
                logger.warning("patrol_openkb_compile list_compile_tasks 失败 kb=%s: %s", kb_id, exc)
                continue

            for r in group:
                doc_id, task_id = r.id, r.llm_wiki_task_id
                req_at = r.llm_wiki_compile_requested_at
                if not task_id:
                    # claim 后 submit 尚未回写 task_id 的正常窗口：只按 requested_at 超时
                    if req_at and (now - req_at).total_seconds() > TIMEOUT:
                        await self._record_openkb_status_by_id(
                            doc_id, tenant_id, status="failed",
                            error="OPENKB_COMPILE_TIMEOUT: 编译超时（submit 未回写 task_id），请重试",
                        )
                        timed_out += 1
                    continue
                task = task_map.get(task_id)
                if task is None:
                    # 任务丢失（容器重启 initialize 判死）
                    await self._record_openkb_status_by_id(
                        doc_id, tenant_id, status="failed",
                        error="OPENKB_TASK_LOST: 编译任务丢失（容器重启），请重新编译",
                    )
                    failed += 1
                    continue
                tstatus = task.get("status")
                if tstatus == "completed":
                    await self._record_openkb_status_by_id(
                        doc_id, tenant_id, status="compiled",
                        warnings=task.get("warnings") or [],
                    )
                    compiled += 1
                elif tstatus == "failed":
                    await self._record_openkb_status_by_id(
                        doc_id, tenant_id, status="failed",
                        error=task.get("error") or "compile failed",
                    )
                    failed += 1
                elif tstatus == "running":
                    # ⚠️ 排队时间不算超时：running 才从 started_at 起算
                    started = task.get("started_at")
                    try:
                        started_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                        if started_dt.tzinfo:
                            started_dt = started_dt.replace(tzinfo=None)
                        elapsed = (now - started_dt).total_seconds()
                    except (ValueError, TypeError):
                        elapsed = 0   # started_at 缺失不判死（保守）
                    if elapsed > TIMEOUT:
                        await self._record_openkb_status_by_id(
                            doc_id, tenant_id, status="failed",
                            error="OPENKB_COMPILE_TIMEOUT: 编译超时，请重试",
                        )
                        timed_out += 1
                # pending：排队中不判死（批量上传队尾任务，mutation 锁队列）

        # ③ stale 且 ready 的文档补提交（reparse 停在 stale 的兜底）
        stale_submitted = await self._patrol_stale_recompile(limit=limit)

        return {"checked": len(rows), "compiled": compiled, "failed": failed,
                "timed_out": timed_out, "stale_submitted": stale_submitted}

    async def _patrol_stale_recompile(self, limit: int = 50) -> int:
        """[jonex] stale 且 ready 的文档重新提交编译。

        reparse 常规路径会推回 parsing→ready 再触发钩子，但 stale 若由其他路径
        写入、或 reparse 中途失败停在 ready，文档会永久停在 stale——此处兜底。
        """
        from sqlalchemy import text

        sql = text("""
            SELECT id, tenant_id, knowledge_base_id
              FROM knowledge_base.knowledge_documents
             WHERE is_deleted = 0 AND llm_wiki_compile_status = 'stale'
               AND status = 'ready'
             LIMIT :limit
        """)
        async with get_db_session() as session:
            rows = (await session.execute(sql, {"limit": limit})).all()

        from .openkb_service import KnowledgeCompilerService, openkb_artifact_path
        compiler = KnowledgeCompilerService()
        submitted = 0
        for r in rows:
            md = openkb_artifact_path(r.id)
            if not md.is_file():
                continue
            if not await compiler.claim_compile(r.tenant_id, r.id):
                continue
            try:
                result = await compiler.submit_compile(
                    kb_name=r.knowledge_base_id, tenant_id=r.tenant_id,
                    kb_id=r.knowledge_base_id,
                    parsed_artifact={
                        "document_id": r.id,
                        "source_file_path": "",
                        "parsed_markdown_path": f"parsed/{r.id}/content.md",
                        "assets_dir": f"parsed/{r.id}/assets"
                        if (md.parent / "assets").is_dir() else "",
                        "parser": "mineru",
                        "metadata": {"pages": 0, "title": "", "recompile": True},
                    },
                )
                await compiler.set_task_id(r.tenant_id, r.id, result["task_id"])
                submitted += 1
            except Exception as exc:
                await self._record_openkb_status_by_id(
                    r.id, r.tenant_id, status="failed",
                    error=f"OPENKB_STALE_SUBMIT_FAILED: {exc}"[:1000],
                )
        return submitted

    async def _record_openkb_status_by_id(self, document_id: str, tenant_id: str, *,
                                          status: str, error: str = "", warnings=None) -> None:
        """[jonex] 按 id+tenant 回写 LLM-Wiki 编译状态（列化；巡检无 ORM 对象场景）。"""
        try:
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
            logger.warning("[jonex] patrol 回写 LLM-Wiki 状态失败 doc=%s", document_id, exc_info=True)


__all__ = ["ReconciliationService"]
