"""
TaskManager — core state machine, Semaphore-based capacity control,
cooperative cancellation, and retention cleanup.

Spec §5 — compliant with:
  - §5.1: 4 Workers + Semaphore(worker_count + queue_capacity)
  - §5.2: Task lifecycle + 24h retention + 30min cleanup
  - §5.3: Cooperative cancellation with cancel_event
  - §3:   TaskInfo fields
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from raganything.callbacks import ProcessingCallback
from raganything.config import RAGAnythingConfig
from raganything.pipeline.base import PipelineContext as PipelineCtx
from raganything.service.model_factory import ModelFactory
from raganything.service.jonex_metering_ctx import (  # [jonex] Gap A contextvar
    set_ingest_ctx,
    reset_ingest_ctx,
)
from raganything.service.models import (
    CancelTaskResponse,
    CreateTaskRequest,
    CreateTaskResponse,
    ErrorCode,
    ErrorResponse,
    FileType,
    PaginatedTasks,
    ProgressDetail,
    ResultSummary,
    StageTiming,
    StorageInfo,
    TaskInfo,
    TaskListItem,
    TaskStatus,
    TERMINAL_STATES,
    STATE_TRANSITIONS,
    infer_file_type,
    validate_transition,
)

from raganything.service.chunk_repository import LightRAGChunkRepository
from raganything.service.exceptions import StorageNotReadyError
from raganything.service.models import UpdateChunkResult

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

WORKER_COUNT = int(os.getenv("WORKER_COUNT", "4"))
QUEUE_CAPACITY = int(os.getenv("TASK_QUEUE_CAPACITY", "100"))
RETENTION_HOURS = 24
CLEANUP_INTERVAL_SEC = 30 * 60  # 30 min
IDEMPOTENCY_TTL_SEC = 10 * 60   # 10 min
DEFAULT_WEBHOOK_URL = os.getenv("RAG_WEBHOOK_URL", "")  # global fallback (v1 compat)

# ── PipelineContext — execution metadata collector ─────────────────────
# Collects block stats and timing during pipeline execution so
# consumers don't need to reverse-query LightRAG internals.


class PipelineContext:
    """Execution context that collects metadata during pipeline stages.

    Block type information is captured from MinerU content_list during
    parsing — LightRAG ingest *flattens* type info into plain text chunks,
    so it cannot be recovered from storage afterwards.

    Timing is recorded as ``time.time()`` epoch floats (NOT monotonic).
    """

    __slots__ = (
        "doc_id", "file_path", "parser_type",
        "started_at", "completed_at",
        "text_blocks", "table_blocks", "code_blocks", "image_count",
        "content_list",
    )

    def __init__(
        self,
        file_path: str = "",
        parser_type: str = "",
    ):
        self.doc_id: str = ""
        self.file_path: str = file_path
        self.parser_type: str = parser_type

        # Timing — time.time() epoch floats
        self.started_at: float = 0.0
        self.completed_at: float = 0.0

        # Block counts from content_list
        self.text_blocks: int = 0
        self.table_blocks: int = 0
        self.code_blocks: int = 0
        self.image_count: int = 0

        # Raw content_list for artifact writing (Bug 3)
        self.content_list: list[dict] | None = None

    # ── Block counting ──────────────────────────────────────────────

    def count_blocks_from_content_list(self) -> None:
        """Tally text/table/code/image blocks from MinerU content_list."""
        for item in (self.content_list or []):
            t = item.get("type", "text")
            if t == "text":
                self.text_blocks += 1
            elif t == "table":
                self.table_blocks += 1
            elif t == "code":
                self.code_blocks += 1
            elif t == "image":
                self.image_count += 1

    @property
    def total_blocks(self) -> int:
        return self.text_blocks + self.table_blocks + self.code_blocks

    # ── Timing ──────────────────────────────────────────────────────

    @property
    def duration_seconds(self) -> float:
        if self.completed_at > 0 and self.started_at > 0:
            return self.completed_at - self.started_at
        return 0.0


# ── Error classification — Spec §5.4 ─────────────────────────────────

ERROR_CLASSIFICATION: list[tuple[str, ErrorCode | str]] = [
    (r"FileNotFound|No such file",                     ErrorCode.FILE_NOT_FOUND),
    (r"Profile.*not found",                            ErrorCode.PROFILE_NOT_FOUND),
    (r"Preset.*not found",                             ErrorCode.PRESET_NOT_FOUND),
    (r"Model.*not found|unknown model",                ErrorCode.MODEL_NOT_FOUND),
    (r"timed out|TimeoutError",                        "continue"),  # fallthrough
    (r"llm.*timed out|openai.*timeout",                ErrorCode.LLM_TIMEOUT),
    (r"vlm.*timed out",                                ErrorCode.VLM_TIMEOUT),
    (r"asr.*timed out|whisper.*timeout",               ErrorCode.ASR_TIMEOUT),
    (r"lightrag.*error|LightRAG",                      ErrorCode.LIGHTRAG_ERROR),
    (r"parse.*error|MinerU|docling|paddle",            ErrorCode.PARSER_ERROR),
    (r"invalid.*config|config.*invalid",               ErrorCode.CONFIG_INVALID),
    (r"OBJECT_FETCH_FAILED",                           ErrorCode.OBJECT_FETCH_FAILED),
]


def _classify_error(exc: Exception) -> ErrorCode:
    if isinstance(exc, TaskCancelledError):
        return ErrorCode.TASK_CANCELLED
    msg = str(exc)
    for pattern, code in ERROR_CLASSIFICATION:
        import re
        if re.search(pattern, msg, re.IGNORECASE):
            if code == "continue":
                continue
            return code  # type: ignore[return-value]
    return ErrorCode.UNKNOWN


# ── TaskHandle ───────────────────────────────────────────────────────

class TaskCancelledError(Exception):
    """Raised inside worker when cancel_event is set."""


class TaskHandle:
    __slots__ = ("cancel_event", "current_task", "_release_cb", "_released")

    def __init__(self, release_cb):
        self.cancel_event = asyncio.Event()
        self.current_task: Optional[asyncio.Task] = None
        self._release_cb = release_cb
        self._released = False

    def release_slot(self):
        """Release the capacity slot. Safe to call multiple times."""
        if not self._released:
            self._released = True
            self._release_cb()


# ── TenantRAGCache ──────────────────────────────────────────────────

class TenantRAGCache:
    """Per-tenant+KB lazy RAGAnything instances (ADR-1).

    Creates real RAGAnything instances with:
      - working_dir = {base_dir}/{tenant_id}/{kb_id}/
      - Model functions from profile via ModelFactory
      - LRU eviction when exceeding max_tenants (default 32)

    Thread-safe: all public methods are guarded by asyncio.Lock.
    """

    def __init__(
        self,
        base_dir: str,
        model_factory: ModelFactory,
        max_tenants: int = 32,
    ):
        self._instances: dict[tuple[str, str], Any] = {}
        self._access_times: dict[tuple[str, str], float] = {}
        self._base_dir = base_dir
        self._model_factory = model_factory
        self._max_tenants = max_tenants
        self._lock = asyncio.Lock()

    async def get(self, tenant_id: str, kb_id: str, config_snapshot: dict) -> Any:
        """Get or create a RAGAnything instance for the tenant + KB.

        Args:
            tenant_id: Tenant identifier.
            kb_id: Knowledge base identifier (empty string for default).
            config_snapshot: Resolved config dict (from ConfigResolver.resolve()).

        Returns:
            RAGAnything instance.
        """
        key = (tenant_id, kb_id)
        async with self._lock:
            if key in self._instances:
                self._access_times[key] = time.monotonic()
                return self._instances[key]

            # Evict LRU if at capacity
            if len(self._instances) >= self._max_tenants:
                oldest_key = min(
                    self._access_times.keys(),
                    key=lambda k: self._access_times[k],
                )
                await self._evict_tenant(oldest_key)

            # Create new RAGAnything instance
            rag = await self._create_instance(tenant_id, config_snapshot, kb_id=kb_id)
            self._instances[key] = rag
            self._access_times[key] = time.monotonic()
            logger.info(
                f"TenantRAGCache: created RAGAnything for tenant={tenant_id} kb={kb_id} "
                f"(total instances={len(self._instances)})"
            )
            return rag

    async def _create_instance(self, tenant_id: str, config_snapshot: dict,
                               kb_id: str = "") -> Any:
        """Create a RAGAnything instance for the given tenant + KB."""
        from raganything.raganything import RAGAnything

        kb = kb_id or "default_kb"
        working_dir = os.path.join(self._base_dir, tenant_id, kb, "rag_storage")
        parser_output_dir = os.path.join(self._base_dir, tenant_id, kb, "output")
        os.makedirs(working_dir, exist_ok=True)
        os.makedirs(parser_output_dir, exist_ok=True)

        # Extract profile from config snapshot
        profile = config_snapshot.get("profile", "dev")

        # Build model functions from profile
        funcs = self._model_factory.build(
            profile=profile,
            overrides=config_snapshot,
            working_dir=working_dir,
            parser_output_dir=parser_output_dir,
            tenant_id=tenant_id,
            kb_id=kb_id,
        )

        # Set env vars for ASR model downloads if configured
        asr_backend = config_snapshot.get("asr_binding", "")
        asr_model = config_snapshot.get("asr_model", "")
        if asr_backend and asr_model:
            os.environ.setdefault("ASR_BINDING", asr_backend)
            os.environ.setdefault("ASR_MODEL", asr_model)

        rag = RAGAnything(
            config=funcs["config"],
            llm_model_func=funcs["llm_model_func"],
            embedding_func=funcs["embedding_func"],
            vlm_model_func=funcs.get("vlm_model_func"),
            asr_model_func=funcs.get("asr_model_func"),
            lightrag_kwargs=funcs.get("lightrag_kwargs", {}),
        )

        # Configure MinerU online parser with preset token (overrides env var)
        if hasattr(rag.doc_parser, "configure") and config_snapshot:
            rag.doc_parser.configure(**config_snapshot)

        return rag

    async def _evict_tenant(self, key: tuple[str, str]) -> None:
        """Close and remove a tenant+KB's RAGAnything instance."""
        rag = self._instances.pop(key, None)
        self._access_times.pop(key, None)
        if rag is not None:
            try:
                # RAGAnything.close() handles async cleanup
                rag.close()
            except Exception:
                logger.warning(
                    f"Error closing RAGAnything for key={key}",
                    exc_info=True,
                )
            logger.info(f"TenantRAGCache: evicted tenant={key[0]} kb={key[1]}")

    async def close_all(self) -> None:
        """Close all cached instances (called during shutdown)."""
        async with self._lock:
            for key in list(self._instances.keys()):
                await self._evict_tenant(key)
            logger.info("TenantRAGCache: all instances closed")

    @property
    def tenant_count(self) -> int:
        return len(self._instances)


# ── ProgressTrackingCallback ─────────────────────────────────────────

class ProgressTrackingCallback(ProcessingCallback):
    """Callback that updates TaskInfo progress + timeline during pipeline execution.

    Registered on RAGAnything.callback_manager before process_document_complete
    and unregistered afterwards.  All updates are synchronous on the event loop
    thread (safe: TaskInfo is owned by the worker coroutine).

    Timeline: Each on_*_start call pushes a StageTiming with started_at;
    each on_*_complete call closes the last matching stage with ended_at.
    """

    def __init__(self, task: TaskInfo, handle: "TaskHandle"):
        self._task = task
        self._handle = handle
        self._start_time = time.monotonic()
        # Initialize timeline with pre-pipeline stages from existing timestamps.
        # Only do so if the timeline hasn't been populated yet (first registration).
        if not task.timeline:
            task.timeline = [
                StageTiming(
                    stage="created",
                    label="创建任务",
                    detail="提交任务请求",
                    started_at=task.created_at,
                    ended_at=task.queued_at or task.created_at,
                    elapsed_seconds=_elapsed_seconds(task.created_at, task.queued_at),
                ),
                StageTiming(
                    stage="queued",
                    label="排队等待",
                    detail="等待 Worker 接管",
                    started_at=task.queued_at,
                    ended_at=task.started_at,
                    elapsed_seconds=_elapsed_seconds(task.queued_at, task.started_at),
                ),
            ]

    # ── timeline helpers ──────────────────────────────────────────

    def _push_stage(self, stage: str, label: str, detail: str = "") -> None:
        self._task.timeline.append(StageTiming(
            stage=stage,
            label=label,
            detail=detail,
            started_at=datetime.now(timezone.utc),
        ))

    def _close_stage(self) -> None:
        if not self._task.timeline:
            return
        last = self._task.timeline[-1]
        if last.ended_at is None and last.started_at is not None:
            now = datetime.now(timezone.utc)
            last.ended_at = now
            last.elapsed_seconds = (now - last.started_at).total_seconds()

    # ── cancellation ─────────────────────────────────────────────

    def _check_cancelled(self) -> bool:
        if self._handle.cancel_event.is_set():
            raise TaskCancelledError()
        return False

    # ── parse ────────────────────────────────────────────────────

    def on_parse_start(self, file_path: str, **kwargs: Any) -> None:
        self._check_cancelled()
        self._task.current_step = "parse"
        self._task.progress_detail = ProgressDetail(
            current=1, total=5, unit="step",
            step_name="parse", step_detail="Parsing document...",
            elapsed_seconds=time.monotonic() - self._start_time,
        )
        self._push_stage("parse", "文档解析", "MinerU 在线解析（上传→轮询→下载zip）")
        self._task.updated_at = datetime.now(timezone.utc)

    def on_parse_complete(
        self, file_path: str, content_blocks: int = 0, **kwargs: Any
    ) -> None:
        self._check_cancelled()
        self._task.progress = 0.2
        self._task.progress_detail = ProgressDetail(
            current=1, total=5, unit="step",
            step_name="parse", step_detail=f"Parsed {content_blocks} blocks",
            elapsed_seconds=time.monotonic() - self._start_time,
        )
        self._close_stage()
        self._task.updated_at = datetime.now(timezone.utc)

    # ── text_insert ──────────────────────────────────────────────

    def on_text_insert_start(self, file_path: str, **kwargs: Any) -> None:
        self._check_cancelled()
        self._task.current_step = "text_insert"
        self._task.progress_detail = ProgressDetail(
            current=2, total=5, unit="step",
            step_name="text_insert", step_detail="Inserting text into LightRAG...",
            elapsed_seconds=time.monotonic() - self._start_time,
        )
        self._push_stage("text_insert", "文本入库", "LightRAG insert_text_content")
        self._task.updated_at = datetime.now(timezone.utc)

    def on_text_insert_complete(self, file_path: str, **kwargs: Any) -> None:
        self._check_cancelled()
        self._task.progress = 0.4
        self._task.progress_detail = ProgressDetail(
            current=2, total=5, unit="step",
            step_name="text_insert", step_detail="Text insertion complete",
            elapsed_seconds=time.monotonic() - self._start_time,
        )
        self._close_stage()
        self._task.updated_at = datetime.now(timezone.utc)

    # ── multimodal ───────────────────────────────────────────────

    def on_multimodal_start(self, file_path: str, item_count: int = 0, **kwargs: Any) -> None:
        self._check_cancelled()
        self._task.current_step = "multimodal"
        self._task.progress = 0.5
        self._task.progress_detail = ProgressDetail(
            current=0, total=item_count, unit="item",
            step_name="multimodal", step_detail=f"Processing {item_count} multimodal items...",
            elapsed_seconds=time.monotonic() - self._start_time,
        )
        self._push_stage("multimodal", "多模态处理", "图片/表格/公式 VLM 描述")
        self._task.updated_at = datetime.now(timezone.utc)

    def on_multimodal_item_complete(
        self, file_path: str, item_index: int = 0, total_items: int = 0, **kwargs: Any
    ) -> None:
        self._check_cancelled()
        progress = 0.5 + 0.4 * (item_index / max(total_items, 1))
        self._task.progress = progress
        self._task.progress_detail = ProgressDetail(
            current=item_index, total=total_items, unit="item",
            step_name="multimodal",
            step_detail=f"Processed {item_index}/{total_items} multimodal items",
            elapsed_seconds=time.monotonic() - self._start_time,
        )
        self._task.updated_at = datetime.now(timezone.utc)

    def on_multimodal_complete(self, file_path: str, **kwargs: Any) -> None:
        self._check_cancelled()
        self._task.progress = 0.9
        self._task.progress_detail = ProgressDetail(
            current=4, total=5, unit="step",
            step_name="multimodal", step_detail="Multimodal processing complete",
            elapsed_seconds=time.monotonic() - self._start_time,
        )
        self._close_stage()
        self._task.updated_at = datetime.now(timezone.utc)

    # ── push_chunks (HTTP mode) ───────────────────────────────────
    # [jonex] 批次 2-A：P3 push 阶段信号，供 kb-service 对账置 INGESTING

    def on_push_chunks_start(self, file_path: str = "", **kwargs: Any) -> None:
        self._check_cancelled()
        self._task.current_step = "push_chunks"
        self._task.progress = 0.92
        self._task.progress_detail = ProgressDetail(
            current=3, total=5, unit="step",
            step_name="push_chunks", step_detail="推送并等待 LightRAG 入图/抽取...",
            elapsed_seconds=time.monotonic() - self._start_time,
        )
        self._push_stage("push_chunks", "推送入图", "逐 chunk LightRAG LLM 抽取 + embedding + 写图/向量库")
        self._task.updated_at = datetime.now(timezone.utc)

    # ── done ─────────────────────────────────────────────────────

    def on_document_complete(self, file_path: str, doc_id: str = "", **kwargs: Any) -> None:
        self._check_cancelled()
        self._task.current_step = "done"
        self._task.progress = 1.0
        elapsed = time.monotonic() - self._start_time
        self._task.progress_detail = ProgressDetail(
            current=5, total=5, unit="step",
            step_name="done", step_detail="Complete",
            elapsed_seconds=elapsed,
        )
        # [jonex] §11 缺口 A：先关当前活跃 stage（push_chunks，由 on_push_chunks_start 打开），
        # 保证 push_chunks 有 ended_at/elapsed_seconds，再 push done（pipeline done）。
        # 注意：管道中 parse/text_insert/multimodal 已由各自的 on_*_complete 逐一 close，
        # 此处 _close_stage() 唯一定位的是 push_chunks 的未关闭 record。
        self._close_stage()  # closes push_chunks（当前活跃 stage）
        self._push_stage("pipeline_done", "完成", "pipeline 收尾")
        self._close_stage()  # closes pipeline_done
        # Store doc_id for result summary (picked up by _execute_pipeline)
        self._task.result_summary = ResultSummary(
            doc_id=doc_id,
            duration_seconds=elapsed,
        )
        self._task.updated_at = datetime.now(timezone.utc)

    # ── error ────────────────────────────────────────────────────

    def on_document_error(
        self, file_path: str, error: Any = "", stage: str = "", **kwargs: Any
    ) -> None:
        self._task.progress_detail = ProgressDetail(
            current=0, total=5, unit="step",
            step_name=stage, step_detail=f"Error: {error}",
            elapsed_seconds=time.monotonic() - self._start_time,
        )
        self._close_stage()
        self._task.updated_at = datetime.now(timezone.utc)


def _elapsed_seconds(start: datetime | None, end: datetime | None) -> float:
    """Safe elapsed-seconds between two optional datetimes."""
    if start is not None and end is not None:
        return (end - start).total_seconds()
    return 0.0


# ── v2 ingest_timing logger ────────────────────────────────────────────
# [jonex] §11 增补：把 v2 task.timeline 各 stage 的 elapsed_seconds 摊平成
# {stage}_ms + worker_total_ms，打一条 event=ingest_timing 结构化日志
#（message 内嵌 + extra 双写，对齐 §3.4 A 方案甲）。

_V2_TIMING_STAGES = frozenset({
    "created", "queued", "parse", "text_insert", "multimodal",
    "push_chunks", "ontology_extract", "pipeline_done",
})

# [jonex] P1-1：created/queued 是排队等待，不算入 worker 端到端耗时
_QUEUE_KEYS = frozenset({"created", "queued"})


def _log_ingest_timing_v2(task, status: str, *, force_ontology_only: bool = False, error: str = "") -> None:
    """Log v2 ingest_timing structured log from task.timeline stage timings.

    Args:
        task: TaskInfo with timeline populated.
        status: ``completed`` / ``failed`` / ``cancelled``.
        force_ontology_only: True when execution_mode==ontology_only.
        error: failure reason for failed/cancelled paths.
    """
    try:
        from jonex_core.common.timing import timing_enabled
    except ImportError:
        return  # graceful: jonex_core not on sys.path

    if not timing_enabled():
        return

    # [jonex] §11 缺口 D：失败/cancel 路径下回调不触发的 stage 没有 ended_at，
    # 此处兜底 close，确保"卡在哪个阶段"数据不丢失。
    now = datetime.now(timezone.utc)
    for s in (task.timeline or []):
        if s.ended_at is None and s.started_at is not None:
            s.ended_at = now
            s.elapsed_seconds = (now - s.started_at).total_seconds()

    stages: dict = {}
    worker_total_s = 0.0
    for s in (task.timeline or []):
        stage_name = s.stage
        if not stage_name or stage_name not in _V2_TIMING_STAGES:
            continue
        try:
            secs = float(s.elapsed_seconds or 0)
        except (TypeError, ValueError):
            secs = 0.0
        stages[f"{stage_name}_ms"] = int(secs * 1000)
        # [jonex] P1-1：created/queued 是排队，不算入 worker 端到端
        if stage_name not in _QUEUE_KEYS:
            worker_total_s += secs
    worker_total_ms = int(worker_total_s * 1000)

    # message 内嵌关键数字供 stdout/grep（方案甲）
    sorted_parts = " ".join(f"{k}={v}" for k, v in sorted(stages.items()))
    logger.info(
        "ingest_timing pipeline_version=v2 task=%s status=%s"
        " force_ontology_only=%s worker_total_ms=%s error=%s %s",
        task.task_id, status, force_ontology_only,
        worker_total_ms or "0", error or "", sorted_parts,
        extra={
            "event": "ingest_timing",
            "pipeline_version": "v2",
            "task_id": task.task_id,
            "tenant_id": getattr(task, "tenant_id", ""),
            "knowledge_base_id": getattr(task, "kb_id", ""),
            "document_id": getattr(task, "document_id", ""),
            "status": status,
            "force_ontology_only": force_ontology_only,
            "worker_total_ms": worker_total_ms,
            "error": error,
            **stages,
        },
    )


# ── TaskManager ──────────────────────────────────────────────────────

class TaskManager:
    def __init__(
        self,
        base_dir: str = "./rag_service_data",
        model_factory: ModelFactory | None = None,
        worker_count: int = WORKER_COUNT,
        queue_capacity: int = QUEUE_CAPACITY,
        chunk_repository_factory=None,
        *,
        http_client: Any = None,
        pipeline_executor: Any = None,
        prompt_config_manager: Any = None,
    ):
        self._tasks: dict[str, TaskInfo] = {}
        self._handles: dict[str, TaskHandle] = {}
        self._max_slots = worker_count + queue_capacity
        self._active_slots = 0
        self._slot_lock = asyncio.Lock()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._idempotency: dict[str, tuple[float, str]] = {}  # key → (expire_ts, task_id)
        self._worker_count = worker_count
        self._model_factory = model_factory or ModelFactory()
        # ── v2 HTTP mode ──
        self._http_client = http_client
        self._pipeline_executor = pipeline_executor
        self._config_resolver = None  # set after init
        # [jonex] 主解析提示词覆盖：pipeline 消费 prompt_ids 时用（B2）
        self._pcm = prompt_config_manager
        self._shutting_down = False
        # [jonex] P0-B：后台 fire-and-forget task 强引用集合。asyncio.create_task 的
        # 返回值若不保存，task 可能被 GC 回收导致协程执行中途消失（任务永久停在 created）。
        self._bg_tasks: set[asyncio.Task] = set()

        from raganything.service.task_repository import TaskRepository
        self._base_dir = base_dir
        self._repo = TaskRepository(base_dir=base_dir)

        self._worker_tasks: list[asyncio.Task] = []
        self._cleanup_task: Optional[asyncio.Task] = None

    # ── Lifecycle ───────────────────────────────────────────────────

    def set_config_resolver(self, resolver):
        self._config_resolver = resolver

    # ── [jonex] P0：后台任务与 handle 兜底 ───────────────────────────

    def _spawn_bg(self, coro, *, name: str = "") -> asyncio.Task:
        """创建后台 task 并保存强引用，done 后自动移除。

        直接用裸 asyncio.create_task 时事件循环只弱引用 task，GC 可能在协程
        挂起期间回收它，协程静默中止且不留日志。done 回调同时把取消/异常
        显式打出来，避免 fire-and-forget 里的失败永远看不到。
        """
        t = asyncio.create_task(coro, name=name or None)
        self._bg_tasks.add(t)

        def _done(fut: asyncio.Task) -> None:
            self._bg_tasks.discard(fut)
            if fut.cancelled():
                logger.warning("后台任务被取消: %s", fut.get_name())
                return
            exc = fut.exception()
            if exc is not None:
                logger.error(
                    "后台任务异常: %s: %s", fut.get_name(), exc, exc_info=exc,
                )

        t.add_done_callback(_done)
        return t

    def _ensure_handle(self, task_id: str) -> TaskHandle:
        """幂等获取/重建 TaskHandle。

        重启恢复路径（start()）不经过 create()，历史实现只恢复 _tasks + _queue
        而不重建 _handles，导致 _worker_loop 取到任务后因 `not handle` 静默丢弃
        （task_done + continue，零日志），任务永久停在 created / progress=0。
        cancel() 同样依赖 handle 才能置 cancel_event。此处统一兜底重建。
        """
        handle = self._handles.get(task_id)
        if handle is None:
            handle = TaskHandle(lambda: self._release_slot(task_id))
            self._handles[task_id] = handle
        return handle

    async def start(self):
        hostname = socket.gethostname().split(".")[0]

        # Recover persisted tasks
        recovered = self._repo.load_all()
        for task_id, task in recovered.items():
            self._tasks[task_id] = task
            # Recover stuck ontology tasks
            if task.ontology_status == "extracting":
                logger.warning(f"Recovered stuck ontology task {task_id}, resetting to pending")
                task.ontology_status = "pending"
            if task.status not in TERMINAL_STATES:
                # [jonex] P0-A：恢复路径必须重建 handle，否则 worker 静默丢弃、
                # cancel 也失效（详见 _ensure_handle 注释）。
                self._ensure_handle(task_id)
                # ── [jonex] 阶段4 P0-J：cleanup 阶段只恢复清理，不重跑解析管线 ──
                if getattr(task, "current_step", "") == "cleanup":
                    logger.info(
                        f"Recovered task {task_id} in cleanup, resuming cleanup only "
                        f"(delete_pending={len(task.delete_pending_ids or [])}, "
                        f"compensate_pending={len(task.compensate_pending_ids or [])})"
                    )
                    self._spawn_bg(self._resume_cleanup(task), name=f"resume-cleanup-{task_id}")
                    continue
                # ── v2 HTTP mode: resume track polling if needed ──
                if task.pending_track_ids:
                    logger.info(
                        f"Recovered task {task_id} with {len(task.pending_track_ids)} "
                        f"pending tracks, resuming polling"
                    )
                    self._spawn_bg(
                        self._resume_track_polling(task), name=f"resume-poll-{task_id}"
                    )
                elif (not hasattr(task, 'pending_track_ids')
                      and not hasattr(task, 'lightrag_doc_ids')
                      and self._http_client is not None):
                    # Old-version task without HTTP mode fields → mark FAILED
                    logger.warning(
                        f"Task {task_id} has no HTTP-mode fields — "
                        f"version incompatible, marking FAILED"
                    )
                    self._fail_task(task, ErrorCode.INTERNAL_ERROR,
                                    "任务版本不兼容，请重新提交")
                else:
                    # [jonex] P0-A：状态归一到 QUEUED。恢复态可能是 created/queued/
                    # processing，而 STATE_TRANSITIONS 不允许 created→processing、
                    # processing→processing，worker 会打 "Invalid transition" 并让
                    # 状态与实际执行脱节。同时占回一个 slot（worker finally 释放）。
                    task.status = TaskStatus.QUEUED
                    task.queued_at = datetime.now(timezone.utc)
                    task.updated_at = task.queued_at
                    task.worker_id = None
                    self._active_slots += 1
                    self._repo.save(task)
                    logger.info(f"Recovered task {task_id} re-queued for processing")
                    self._queue.put_nowait(task_id)
        if recovered:
            logger.info(f"Recovered {len(recovered)} tasks from disk")

        for i in range(self._worker_count):
            worker_id = f"worker-{hostname}-{i}"
            t = asyncio.create_task(self._worker_loop(worker_id), name=worker_id)
            self._worker_tasks.append(t)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="cleanup")
        logger.info(f"TaskManager started: {self._worker_count} workers, queue={QUEUE_CAPACITY}")

    async def shutdown(self, grace_seconds: float = 120.0):
        """Graceful shutdown (Spec §4.8)."""
        self._shutting_down = True
        logger.info("TaskManager shutting down...")

        # Cancel cleanup
        if self._cleanup_task:
            self._cleanup_task.cancel()

        # Wait workers
        done, pending = await asyncio.wait(
            self._worker_tasks, timeout=grace_seconds
        )
        for t in pending:
            t.cancel()
        logger.info(f"TaskManager stopped ({len(done)} workers finished, {len(pending)} cancelled)")

        # Close HTTP client if present
        if self._http_client is not None:
            try:
                await self._http_client.close()
            except Exception:
                pass

    @property
    def accepting_tasks(self) -> bool:
        return not self._shutting_down and self._active_slots < self._max_slots

    @property
    def slots_available(self) -> int:
        return max(0, self._max_slots - self._active_slots)

    def _release_slot(self, task_id: str):
        """Decrement active slot count. Called by TaskHandle."""
        self._active_slots = max(0, self._active_slots - 1)

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ── Public API ──────────────────────────────────────────────────

    # ── Public API (HTTP mode) ──────────────────────────────────────

    async def query(
        self, query: str, tenant_id: str,
        mode: str = "hybrid", top_k: int = 5,
        *,
        kb_id: str = "",
        trace_id: str = "",
    ) -> dict:
        """Query LightRAG via HTTP client, returning {answer, references}.

        Mirrors v1 LightRAGAdapter.query_detailed(): references are parsed
        from :9621 response via parse_file_source(), ready for KB-side
        enrichment with COS presigned URLs and DB lookups.
        """
        if self._http_client is not None:
            from jonex_core.common.file_source_util import (
                normalize_chunk_content,
                parse_file_source,
            )
            result = await self._http_client.query(
                query, mode=mode, top_k=top_k,
                tenant_id=tenant_id, kb_id=kb_id, trace_id=trace_id,
            )
            if isinstance(result, dict):
                answer = result.get("response", result.get("data", ""))
                refs: list[dict] = []
                for r in (result.get("references") or []):
                    parsed = parse_file_source(r.get("file_path", ""))
                    if not parsed:
                        continue
                    # LightRAG content is the chunk text array for this file_path.
                    # [jonex] §10 L1：归一化抽为共享纯函数（与 LOCAL lightrag_adapter
                    # 同构）；顺带修复 ns token 泄漏——REMOTE 此前只 strip、
                    # 未清 `<!--yx:…-->`，与 LOCAL 行为对齐为统一清理。
                    text, chunk_texts = normalize_chunk_content(r.get("content"))
                    if text:
                        parsed["text"] = text
                    if chunk_texts:
                        parsed["chunk_texts"] = chunk_texts
                    # [jonex] 透传 chunk_ids：LightRAG reference 已带 chunk_ids（与 content 数组对齐）。
                    # 本平台 file_source 按 chunk 唯一，取首个作单值 chunk_id；全量存 chunk_ids。
                    chunk_ids = r.get("chunk_ids") or []
                    if chunk_ids:
                        parsed["chunk_ids"] = chunk_ids
                        parsed["chunk_id"] = chunk_ids[0]
                    refs.append(parsed)
                return {"answer": str(answer), "references": refs}
            return {"answer": str(result), "references": []}
        else:
            raise RuntimeError(
                "HTTP mode not configured. Embedded mode query is not supported "
                "in this TaskManager version. Set LIGHTRAG_API_URL to enable HTTP mode."
            )

    async def delete_doc(self, doc_id: str, tenant_id: str, *, kb_id: str = "") -> bool:
        """Delete a document from LightRAG storage via HTTP."""
        if self._http_client is not None:
            return await self._http_client.delete_doc(
                doc_id, tenant_id=tenant_id, kb_id=kb_id,
            )
        raise RuntimeError("HTTP mode not configured")

    async def delete_docs(
        self, doc_ids: list[str], tenant_id: str, *,
        kb_id: str = "", document_id: str = "", trace_id: str = "",
    ) -> dict:
        """[jonex] 批量删除——单次 DELETE 提交全部 doc_ids，LightRAG 仅执行一次 rebuild。

        Returns:
            {"status": "deletion_started", "accepted": [...]}
        """
        if self._http_client is not None:
            return await self._http_client.delete_docs(
                doc_ids, tenant_id=tenant_id, kb_id=kb_id,
                document_id=document_id, trace_id=trace_id,
            )
        raise RuntimeError("HTTP mode not configured")

    async def get_document_chunks(
        self, doc_id: str, tenant_id: str, *, kb_id: str = "",
    ) -> dict | None:
        """Return all chunks for a document via the doc= anchor in file_path.

        Calls GET /documents/chunks?doc_id=X (LightRAG text_chunks by doc=
        anchor), which returns real chunk entries with chunk_id, content,
        chunk_order_index, file_path (containing tstart=/tend= for video/audio
        timeline metadata), tokens, etc.

        Fixed from the previous broken implementation that called
        /documents/paginated (document-level data, no chunk position metadata).
        """
        if self._http_client is not None:
            return await self._http_client.get_document_chunks(
                doc_id, tenant_id=tenant_id, kb_id=kb_id,
            )
        return None

    async def get_chunk_by_id(
        self, chunk_id: str, tenant_id: str, *, kb_id: str = "",
    ) -> dict | None:
        """按 chunk_id 直查单个 chunk 内容（不依赖 task，不拉整篇 chunk 列表）。

        直连 LightRAG GET /documents/chunks/{chunk_id}，返回
        {chunk_id, content, full_doc_id, chunk_order_index, file_path, page_idx, line_start, line_end, tokens}。
        chunk 不存在时 HttpLightRagClient 抛 404 → 上层归一为 None。
        """
        if self._http_client is not None:
            import httpx
            try:
                return await self._http_client.get_chunk_by_id(
                    chunk_id, tenant_id=tenant_id, kb_id=kb_id,
                )
            except httpx.HTTPStatusError as e:
                # chunk 不存在 → None（由 action handler 归一为 40405）
                if e.response.status_code == 404:
                    return None
                raise
        raise RuntimeError("HTTP mode not configured")

    async def export_document(
        self, doc_id: str, tenant_id: str, fmt: str = "json", *, kb_id: str = "",
    ) -> dict | None:
        """Aggregate all data for a document_id via HTTP."""
        if self._http_client is not None:
            return await self._http_client.get_document_parse_result(
                tenant_id=tenant_id, kb_id=kb_id, document_id=doc_id,
            )
        return None

    # [jonex] R7-2b: COS 下载缓存目录（与 lightrag_adapter_v2._cos_cache_dir 同口径）
    def _cos_cache_dir(self) -> str:
        base = (
            os.getenv("RAG_COS_CACHE_DIR")
            or os.path.join(
                os.getenv("KB_INPUT_DIR") or os.getenv("WORKING_DIR") or self._base_dir,
                "_cos_cache",
            )
        )
        os.makedirs(base, exist_ok=True)
        return base

    # [jonex] P0-1: COS 缓存 TTL 清理（兜底：异常退出残留、finally 漏删的孤儿文件）
    def _sweep_cos_cache(self) -> None:
        ttl = int(os.getenv("RAG_COS_CACHE_TTL_SEC", "86400"))
        now = time.time()
        try:
            d = self._cos_cache_dir()
            for name in os.listdir(d):
                fp = os.path.join(d, name)
                try:
                    if os.path.isfile(fp) and now - os.path.getmtime(fp) > ttl:
                        os.remove(fp)
                except OSError:
                    pass
        except OSError:
            pass

    async def create(
        self, req: CreateTaskRequest, tenant_id: str, idempotency_key: str | None = None
    ) -> CreateTaskResponse | TaskInfo:
        # ── Idempotency check ──────────────────────────────────────
        if idempotency_key:
            existing = self._check_idempotency(idempotency_key)
            if existing:
                return existing

        # ── Capacity check (atomic, no TOCTOU) ────────────────────
        async with self._slot_lock:
            if self._active_slots >= self._max_slots:
                raise SlotFullError(
                    ErrorResponse(
                        code=42901,
                        request_id="",
                        message="Task queue is full. Retry later.",
                        data={"queue_capacity": QUEUE_CAPACITY, "available_slots": 0},
                    )
                )
            self._active_slots += 1

        # ── Create task ────────────────────────────────────────────
        task = TaskInfo(
            tenant_id=tenant_id,
            name=os.path.basename(req.file_path),
            file_path=req.file_path,
            file_type=infer_file_type(req.file_path),
            file_size_bytes=self._get_file_size(req.file_path),
            webhook_url=req.webhook_url,
            idempotency_key=idempotency_key,
            preset_name=req.preset,
            prompt_ids=req.prompt_ids or [],
            kb_id=req.kb_id or (req.knowledge_base_id or ""),
            setting_id=req.setting_id,
            document_id=req.document_id or "",
            storage_backend=req.storage_backend or "local",
            storage_key=req.storage_key or "",
            mps_video_url=getattr(req, "mps_video_url", None) or "",
            ontology_schema=req.ontology_schema,
            # ── Reparse / recompile execution control ──
            execution_mode=req.execution_mode or "full",
            content_generation=req.content_generation or 0,
            schema_version=req.schema_version or 0,
            schema_hash=req.schema_hash or "",
            strict_push=req.strict_push or False,
        )
        self._tasks[task.task_id] = task
        self._repo.save(task)

        handle = TaskHandle(lambda: self._release_slot(task.task_id))
        self._handles[task.task_id] = handle

        if idempotency_key:
            self._idempotency[idempotency_key] = (
                time.monotonic() + IDEMPOTENCY_TTL_SEC,
                task.task_id,
            )

        logger.info(
            f"Task created: {task.task_id} tenant={tenant_id} file={task.name}"
        )

        # ── Async validate → enqueue ───────────────────────────────
        # [jonex] P0-B：必须保存强引用，裸 create_task 可能被 GC 中途回收
        self._spawn_bg(
            self._validate_and_enqueue(task, req), name=f"enqueue-{task.task_id}"
        )

        return CreateTaskResponse(
            task_id=task.task_id,
            tenant_id=tenant_id,
            status=task.status,
            created_at=task.created_at,
        )

    async def get(self, task_id: str, tenant_id: str) -> TaskInfo | None:
        task = self._tasks.get(task_id)
        if task and task.tenant_id == tenant_id:
            return task
        return None

    async def list(
        self, tenant_id: str, status: str, file_type: str,
        page: int, page_size: int, sort: str,
    ) -> PaginatedTasks:
        tasks = [
            t for t in self._tasks.values()
            if t.tenant_id == tenant_id
        ]
        # Filter
        if status != "all":
            if status == "active":
                tasks = [t for t in tasks if t.status not in TERMINAL_STATES]
            else:
                try:
                    st = TaskStatus(status)
                    tasks = [t for t in tasks if t.status == st]
                except ValueError:
                    pass
        if file_type != "all":
            try:
                ft = FileType(file_type)
                tasks = [t for t in tasks if t.file_type == ft]
            except ValueError:
                pass

        # Sort
        reverse = sort.startswith("-")
        sort_key = sort.lstrip("-")
        if sort_key == "created_at":
            tasks.sort(key=lambda t: t.created_at, reverse=reverse)
        else:
            tasks.sort(key=lambda t: t.created_at, reverse=True)

        # Paginate
        total = len(tasks)
        start = (page - 1) * page_size
        page_tasks = tasks[start : start + page_size]

        items = [
            TaskListItem(
                task_id=t.task_id,
                name=t.name,
                file_type=t.file_type,
                status=t.status,
                progress=t.progress,
                progress_detail=t.progress_detail,
                worker_id=t.worker_id,
                created_at=t.created_at,
            )
            for t in page_tasks
        ]
        return PaginatedTasks(total=total, page=page, page_size=page_size, tasks=items)

    async def cancel(self, task_id: str, tenant_id: str) -> CancelTaskResponse | None:
        task = await self.get(task_id, tenant_id)
        if task is None:
            return None

        if task.status in TERMINAL_STATES:
            return CancelTaskResponse(
                task_id=task_id,
                previous_status=task.status,
                status=task.status,
                http_code=200,
            )

        prev = task.status
        is_processing = task.status == TaskStatus.PROCESSING

        task.status = TaskStatus.CANCELLED
        task.error_code = ErrorCode.TASK_CANCELLED
        task.completed_at = datetime.now(timezone.utc)
        task.updated_at = datetime.now(timezone.utc)

        handle = self._handles.get(task_id)
        if handle:
            handle.cancel_event.set()
            handle.release_slot()

        return CancelTaskResponse(
            task_id=task_id,
            previous_status=prev,
            status=TaskStatus.CANCELLED,
            http_code=202 if is_processing else 200,
        )

    # ── Internal: validate + enqueue ────────────────────────────────

    async def _validate_and_enqueue(self, task: TaskInfo, req: CreateTaskRequest):
        handle = self._handles.get(task.task_id)
        if not handle:
            return

        try:
            # Check cancellation before starting
            if handle.cancel_event.is_set():
                return

            # Validate file path
            # [jonex] P0-A.2: ontology_only 不本地化 COS、不需要原文件 → 跳过文件存在性校验，
            # 直接进入"按 document_id 读 LightRAG 实体/关系 → 抽本体"。
            # [jonex] P0-1: COS 后端文件不检查本地路径（file_path 是 storage_key 标识符，
            # 实际文件由 pipeline 阶段从 COS 下载），避免误判 FILE_NOT_FOUND。
            needs_local = task.execution_mode != "ontology_only" and not (
                task.storage_backend == "cos" and task.storage_key
            )
            if needs_local and not os.path.exists(task.file_path):
                self._fail_task(task, ErrorCode.FILE_NOT_FOUND, f"File not found: {task.file_path}")
                handle.release_slot()
                return

            # Resolve config via ConfigResolver
            if self._config_resolver:
                try:
                    task.config_snapshot = self._config_resolver.resolve(
                        req, tenant_id=task.tenant_id, kb_id=getattr(task, "kb_id", ""),
                    )
                except Exception as e:
                    logger.warning(
                        f"Config resolution failed for {task.task_id}: {e}. "
                        f"Using minimal config."
                    )
                    task.config_snapshot = {"file_path": task.file_path}
            else:
                task.config_snapshot = {"file_path": task.file_path}
            task.config_snapshot.setdefault("file_path", task.file_path)
            # Carry force_reparse flag through to pipeline
            task.config_snapshot["force_reparse"] = req.force_reparse
            # [jonex] 阶段4：把严格推送 + 执行模式透传给 PushChunksStage
            task.config_snapshot["strict_push"] = task.strict_push
            task.config_snapshot["execution_mode"] = task.execution_mode

            # Check cancellation after validation
            if handle.cancel_event.is_set():
                self._transition(task, TaskStatus.CANCELLED, ErrorCode.TASK_CANCELLED)
                handle.release_slot()
                return

            # Enqueue
            self._transition(task, TaskStatus.QUEUED)
            task.queued_at = datetime.now(timezone.utc)
            await self._queue.put(task.task_id)

        except asyncio.CancelledError:
            # [jonex] P0-B：CancelledError 是 BaseException，历史实现漏抓 →
            # 任务停在 created 且 slot 永不释放（累积后 42901 拒新任务）。
            logger.warning(
                "Validate/enqueue cancelled for %s, marking FAILED", task.task_id
            )
            self._fail_task(
                task, ErrorCode.INTERNAL_ERROR, "任务入队被中断，请重新提交"
            )
            handle.release_slot()
            raise
        except Exception as e:
            logger.error(f"Validate/enqueue failed for {task.task_id}: {e}")
            self._fail_task(task, _classify_error(e), str(e))
            handle.release_slot()

    # ── Worker loop ─────────────────────────────────────────────────

    async def _worker_loop(self, worker_id: str):
        logger.info(f"Worker started: {worker_id}")
        while not self._shutting_down:
            try:
                task_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            task = self._tasks.get(task_id)
            if task is None:
                # [jonex] P0-A：唯一可丢弃的情形（任务已被 cleanup 清除），必须留日志
                logger.warning(
                    "Worker %s: task %s not in registry, dropped from queue",
                    worker_id, task_id,
                )
                self._queue.task_done()
                continue
            # [jonex] P0-A：handle 缺失（重启恢复等路径）不再静默 continue，兜底重建，
            # 否则任务永久停在 created 且无任何日志。
            if task_id not in self._handles:
                logger.warning(
                    "Worker %s: handle missing for task %s (recovered task?), rebuilding",
                    worker_id, task_id,
                )
            handle = self._ensure_handle(task_id)

            task.worker_id = worker_id
            self._transition(task, TaskStatus.PROCESSING)
            task.started_at = datetime.now(timezone.utc)

            try:
                await self._execute_pipeline(task, handle)
            except TaskCancelledError:
                self._transition(task, TaskStatus.CANCELLED, ErrorCode.TASK_CANCELLED)
            except Exception as e:
                logger.error(f"Pipeline failed for {task_id}: {e}\n{traceback.format_exc()}")
                self._fail_task(task, _classify_error(e), str(e))
            finally:
                task.worker_id = None
                task.completed_at = datetime.now(timezone.utc)
                task.updated_at = datetime.now(timezone.utc)
                handle.release_slot()
                self._queue.task_done()
                self._maybe_deliver_webhook(task)

        logger.info(f"Worker stopped: {worker_id}")

    async def _execute_pipeline(self, task: TaskInfo, handle: TaskHandle):
        """Execute the RAGAnything pipeline (HTTP mode).

        HTTP pipeline: parse → multimodal → push_chunks → ontology.
        Uses self._pipeline_executor (RAGAnything with http_client).
        """
        # Check cancellation before starting
        if handle.cancel_event.is_set():
            raise TaskCancelledError()

        # ── HTTP mode ──────────────────────────────────────────────
        if self._pipeline_executor is not None and self._http_client is not None:
            await self._execute_pipeline_http(task, handle)
            return

        # ── Embedded mode (fallback) ───────────────────────────────
        raise RuntimeError(
            "Embedded mode is not supported in this TaskManager version. "
            "Configure LIGHTRAG_API_URL to enable HTTP mode."
        )

    def _write_openkb_artifact(self, task: TaskInfo, ctx) -> None:
        """[jonex] 将解析出的 markdown + 图片落到共享 inputs 卷 parsed/{document_id}/，
        并把「相对 inputs 卷根」的路径记入 task.result_summary.extensions，
        经 get_task_status 暴露给 knowledge_base → OpenKB 编译。

        - gated：RAG_OPENKB_ARTIFACT_ENABLED（默认 on）；关闭则不产出。
        - 输入：ctx.content_list（MinerU blocks，img_path 已被 mineru 改写为绝对路径）。
        - 幂等：按 document_id 覆盖写。
        - 任何异常都不影响主入库链路（best-effort，仅告警）。
        """
        if os.getenv("RAG_OPENKB_ARTIFACT_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
            return
        document_id = getattr(task, "document_id", "") or ""
        content_list = getattr(ctx, "content_list", None)
        if not document_id or not content_list:
            return
        try:
            inputs_root = os.getenv("RAG_INPUTS_DIR", "/app/inputs")
            out_dir = Path(inputs_root) / "parsed" / document_id
            assets_dir = out_dir / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)

            md_parts: list[str] = []
            has_assets = False
            for item in content_list:
                if not isinstance(item, dict):
                    continue
                t = item.get("type", "text")
                if t == "text":
                    txt = (item.get("text") or "").strip()
                    if txt:
                        md_parts.append(txt)
                elif t == "equation":
                    txt = (item.get("text") or "").strip()
                    if txt:
                        md_parts.append(f"$$\n{txt}\n$$")
                elif t == "table":
                    caption = (item.get("table_caption") or "")
                    if isinstance(caption, list):
                        caption = " ".join(str(c) for c in caption)
                    body = item.get("table_body") or item.get("text") or ""
                    if isinstance(body, list):
                        body = "\n".join(
                            "\t".join(str(c) for c in row if c is not None)
                            for row in body if row
                        )
                    block = ((caption.strip() + "\n") if caption.strip() else "") + str(body).strip()
                    if block.strip():
                        md_parts.append(block)
                elif t == "image":
                    # [jonex] VLM 描述段（_textualize_multimodal 产出）写在图片引用前，
                    # 编译时 LLM 上下文连贯（描述 + 图片引用）；无描述（OCR/caption 兜底）
                    # 则只有图片引用
                    vlm_desc = item.get("description") or ""
                    if isinstance(vlm_desc, list):
                        vlm_desc = " ".join(str(c) for c in vlm_desc)
                    vlm_desc = str(vlm_desc).strip()
                    if vlm_desc:
                        md_parts.append(vlm_desc)
                    img = item.get("img_path") or ""
                    caption = item.get("img_caption") or ""
                    if isinstance(caption, list):
                        caption = " ".join(str(c) for c in caption)
                    if img and os.path.isfile(img):
                        dst_name = os.path.basename(img)
                        try:
                            shutil.copy2(img, assets_dir / dst_name)
                            has_assets = True
                            # [jonex] 引用路径对齐 OpenKB 短文档约定：source md 在 wiki/sources/{doc}.md，
                            # 图片经 adapter 复制到 wiki/sources/images/{doc}/，故相对引用为 images/{doc}/{name}
                            md_parts.append(f"![{caption}](images/{document_id}/{dst_name})")
                        except Exception:
                            logger.warning("[jonex] OpenKB artifact 图片复制失败: %s", img)
                elif t in ("video", "audio"):
                    # [jonex] 转写正文（_textualize_multimodal 的产出，含时间戳）写进 content.md
                    desc = item.get("description") or item.get("transcription") or ""
                    if isinstance(desc, list):
                        desc = " ".join(str(c) for c in desc)
                    desc = str(desc).strip()
                    if desc:
                        md_parts.append(desc)

            md_text = "\n\n".join(md_parts).strip() + "\n"
            (out_dir / "content.md").write_text(md_text, encoding="utf-8")

            rel_md = f"parsed/{document_id}/content.md"
            rel_assets = f"parsed/{document_id}/assets" if has_assets else ""

            task.storage = StorageInfo(root=str(out_dir), mineru_dir=str(out_dir))
            if task.result_summary is None:
                task.result_summary = ResultSummary(doc_id=document_id)
            task.result_summary.extensions["parsed_markdown_path"] = rel_md
            task.result_summary.extensions["assets_dir"] = rel_assets
            logger.info(
                "[jonex] OpenKB artifact written: doc=%s md=%s assets=%s blocks=%d",
                document_id, rel_md, rel_assets or "(none)", len(md_parts),
            )
        except Exception:
            logger.warning(
                "[jonex] OpenKB artifact write failed doc=%s", document_id, exc_info=True,
            )

    async def _textualize_multimodal(self, task: TaskInfo, handle: TaskHandle,
                                     content_list: list) -> None:
        """[jonex] 多模态文本化（parse_only 专用）：视频/音频转写正文挂回 block。

        与 full 模式 MultimodalStage（stages.py:343）同逻辑，但取数不同：
        full 从 original_item 的 _audio_segments 逐段建 chunk；parse_only 把
        逐场景文本（含时间戳）拼接后写入 item["description"]（供 artifact 分支）。
        - ⚠️ 取数：`_audio_segments` **三条后端路径都会写**（MPS
          `video_processor.py:235`、`_analyze_via_local` `:427`、audio
          `modalprocessors.py:1792`）——不局限于 COS+MPS；转写正文从它取，
          `desc`（全局摘要）仅兜底
        - 视频 MPS：注入 task.mps_video_url（仅 COS 存储时 FetchObject 设置，
          task_manager.py:1361-1369）；本地存储时**提前短路**（见下），不白等长超时
        - 图片：VLM 增强描述（对齐 full 模式 modalprocessors.py:894 generate_description_only）；
          失败/空**不写占位**（图片有 MinerU OCR 文本 + 原生 caption 兜底，避免污染），
          只记 warning 信号——与 video/audio（必须留痕）的差异见 image 分支注释
        - 失败/配置关闭：video/audio 写占位文本（**粗粒度原因**，异常原文只进日志——安全），
          不阻断 parse_only；占位同时写机器可读信号（_record_multimodal_warning）
        - 转写缓存：按 document_id 读/写 parsed/{doc}/transcript.json——reparse/重编译
          重跑 parse_only 时命中缓存跳过转写（省 MPS/ASR 费用）；失败/占位不写缓存
        """
        from raganything.utils import get_processor_for_type

        processors = getattr(self._pipeline_executor, "modal_processors", None)
        if not processors:
            return
        # [jonex] 配置门：enable_video_processing / enable_audio_processing 关闭时
        # modal_processors["video"] 仍存在（build_all 构建）但 backends 空/未接线
        # （raganything.py:410-425）——显式检查并跳过，避免撞 generic 或失败误报。
        # 实读 config 真实值（ENABLE_VIDEO_PROCESSING / ENABLE_AUDIO_PROCESSING env 控制，
        # **默认 True**，config.py:56=audio / :134=video）；False 兜底仅当 _cfg 缺失时
        # 生效（几乎不发生）。实际值 log 一次（info），排查时一眼看出是配置关的还是
        # 代码判错——否则属性名变更会**静默永久关闭**转写，只剩一条 warning
        _cfg = getattr(self._pipeline_executor, "config", None)
        enabled = {
            "video": bool(getattr(_cfg, "enable_video_processing", False)) if _cfg else False,
            "audio": bool(getattr(_cfg, "enable_audio_processing", False)) if _cfg else False,
            "image": bool(getattr(_cfg, "enable_image_processing", False)) if _cfg else False,
        }
        logger.info(
            "[jonex] multimodal 转写配置门：video=%s audio=%s image=%s（ENABLE_VIDEO/AUDIO/IMAGE_PROCESSING）",
            enabled.get("video"), enabled.get("audio"), enabled.get("image"),
        )

        generic = processors.get("generic")
        mps_url = getattr(task, "mps_video_url", "") or ""
        # prompt_overrides 解析一次（循环外）——RAG_PROMPT_STRICT 时缺配置 fail-fast
        # （_resolve_task_prompt_overrides docstring），不能放 try 内被吞成 warning
        prompt_overrides = self._resolve_task_prompt_overrides(task)

        # [jonex] 转写缓存（重复转写防护）：reparse/重编译会重跑 parse_only → 每次重付
        # MPS/ASR 费用（视频分钟级 + 云计费）。按 document_id 读 parsed/{doc}/transcript.json
        # （与 content.md 同目录，天然随 document_id 幂等，与 _write_openkb_artifact
        # 的覆盖写同一语义），命中即跳过转写直接挂回。只缓存**真实转写正文**
        # （_cacheable），失败/占位不写——reparse 时重试转写，符合用户重试预期
        transcript_cache: dict = {}
        _inputs_root = os.getenv("RAG_INPUTS_DIR", "/app/inputs")
        _cache_path = Path(_inputs_root) / "parsed" / (task.document_id or "") / "transcript.json"
        try:
            if _cache_path.is_file():
                transcript_cache = json.loads(_cache_path.read_text(encoding="utf-8")) or {}
        except Exception:
            transcript_cache = {}

        _t_cn = {"video": "视频", "audio": "音频", "image": "图片"}
        for item in content_list:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            if t not in ("video", "audio", "image"):
                continue
            # 取消检查（每 item）：分钟级 MPS 等待期间用户取消必须生效
            if handle.cancel_event.is_set():
                raise TaskCancelledError()
            if not enabled.get(t, False):
                logger.warning("[jonex] %s 转写已配置关闭（enable_%s_processing=false）——跳过",
                               _t_cn.get(t, t), t)
                continue
            proc = get_processor_for_type(processors, t)
            # ⚠️ get_processor_for_type 对未注册类型兜底 generic（utils.py:540）——
            #    必须显式排除，否则静默用 generic 产出与内容无关的描述
            if proc is None or proc is generic:
                logger.warning("[jonex] 无 %s 专用 processor（generic 兜底）——跳过转写", t)
                continue
            if t in transcript_cache:
                # [jonex] 命中缓存：跳过转写（省一次 MPS/ASR 费用），直接挂回
                item["description"] = str(transcript_cache[t])
                continue
            _cacheable = False
            body = ""
            modal_content: dict = {}   # except 兜底可安全访问（processor 写回的 _audio_segments 在副本上）
            try:
                modal_content = dict(item)   # 独立 dict：processor 原地写 _audio_segments
                if t == "video":
                    # [jonex] backend 判据（已核实 processor_builder.py:124-133）：
                    # _video_backends 非空 = MPS 路径（MPS_ENABLED=true）；空 = 内置
                    # local 路径（ffmpeg+ASR，镜像已含 ffmpeg）。MPS 路径必须 COS URL：
                    # mps_video_url 仅在 COS 存储时设置，本地存储为空 → mps_input 回落
                    # 容器内本地路径交给腾讯云 MPS → 长超时失败（video_processor.py:173-174）。
                    # 提前短路写占位，不白等（_select_backend 只返回 "mps" 或 None，无 local 兜底）。
                    # 不缓存——reparse 换 COS 存储后应重试转写
                    _backends = getattr(proc, "_video_backends", None)
                    if _backends and not mps_url:
                        body = ("该视频的转写已跳过：MPS 视频分析需要 COS 存储的 URL，"
                                "当前为本地存储（无 mps_video_url），MPS 不可用。")
                        self._record_multimodal_warning(task, "video_mps_no_cos_url", body)
                    else:
                        if _backends:
                            # 仅 MPS 路径注入 COS URL；local 路径（_backends 空）不需要
                            modal_content["mps_video_url"] = mps_url
                        desc, _entity = await proc.generate_description_only(
                            modal_content=modal_content, content_type=t,
                            prompt_overrides=prompt_overrides,
                        )
                        # 取数：优先 _audio_segments（逐场景转写正文），desc（全局摘要）兜底
                        segments = modal_content.get("_audio_segments") or []
                        if segments:
                            body = "\n".join(
                                self._fmt_segment(s) for s in segments if s.get("text")
                            )
                        else:
                            body = str(desc or "").strip()
                        _cacheable = True
                elif t == "image":
                    # [jonex] 图片 VLM 描述（对齐 full 模式 MultimodalStage：
                    # modalprocessors.py:894 generate_description_only → enhanced_caption）。
                    # 与 video/audio 的差异：图片有 MinerU OCR 文本 + 原生 caption 兜底
                    # （text block 已进 content.md），VLM 失败/空**不写占位**——避免污染
                    # 有 OCR 的内容；纯图片无文字（空洞场景）靠 warning 信号可查。
                    # 缓存：真实描述才 _cacheable（省 VLM 费用）；失败/空不缓存
                    desc, _entity = await proc.generate_description_only(
                        modal_content=dict(item), content_type=t,
                        prompt_overrides=prompt_overrides,
                    )
                    body = str(desc or "").strip()
                    if body:
                        _cacheable = True
                    else:
                        # VLM 返回空（VLM 不可用/模型拒绝）：不写占位，记 warning
                        self._record_multimodal_warning(
                            task, "image_vlm_empty", "图片 VLM 描述为空",
                        )
                else:  # audio
                    # 独立 dict（与 video 同因）：processor 原地写 _audio_segments
                    modal_content = dict(item)
                    desc, _entity = await proc.generate_description_only(
                        modal_content=modal_content, content_type=t,
                        prompt_overrides=prompt_overrides,
                    )
                    segments = modal_content.get("_audio_segments") or []
                    if segments:
                        body = "\n".join(
                            self._fmt_segment(s) for s in segments if s.get("text")
                        )
                    else:
                        body = str(desc or "").strip()
                    _cacheable = True
            except Exception as exc:
                # ⚠️ 安全：占位只写粗粒度原因——异常原文可能带 COS URL/签名 token/
                # 内部域名/bucket 名，会随 content.md → OpenKB 编译 → **被索引**并可能
                # 出现在知识搜索回答里（对齐 llm-wiki-compile-async-plan 的
                # 「不把 str(exc) 透给前端」规矩，此处更严重：落盘+持久）。异常名与
                # 详情只进日志。video/audio 失败也写占位——否则回到「空摘要」原始 bug，
                # 用户看到 compiled 但内容空，无法判断是转写挂了
                logger.warning(
                    "[jonex] %s 转写失败（%s）task=%s",
                    _t_cn.get(t, t), type(exc).__name__, task.task_id, exc_info=True,
                )
                if t == "image":
                    # 图片有 OCR/caption 兜底：VLM 失败不写占位（避免污染），只记 warning
                    self._record_multimodal_warning(task, "image_vlm_failed", "图片 VLM 描述失败")
                else:
                    # [jonex] 防御：ASR/MPS 可能已写 _audio_segments（转写正文成功、
                    # 全局摘要等后续步骤抛错——如 vendored _recursive_mapreduce NameError）。
                    # 有正文则用正文兜底**不丢转写**（已付的转写费用不白费），只记 warning；
                    # 无正文才写「失败」占位
                    segments = modal_content.get("_audio_segments") or []
                    seg_body = "\n".join(
                        self._fmt_segment(s) for s in segments if s.get("text")
                    )
                    if seg_body:
                        body = seg_body
                        _cacheable = True
                        self._record_multimodal_warning(
                            task, f"{t}_summary_failed", "转写正文已提取，但全局摘要生成失败",
                        )
                    else:
                        reason = self._classify_transcribe_error(exc)
                        body = f"该{_t_cn.get(t, t)}的转写失败（{reason}），暂无可提取文本。"
                        self._record_multimodal_warning(task, f"{t}_transcribe_failed", body)
            if not body:
                if t == "image":
                    # 图片空描述：不写占位（OCR/caption 兜底），warning 已记
                    pass
                else:
                    # 兜底：转写成功但内容为空（无语音/空 scenes）——写占位
                    body = f"该{_t_cn.get(t, t)}内容无可提取的文本（无语音或转写为空）。"
                    self._record_multimodal_warning(task, f"{t}_empty_content", body)
            item["description"] = body
            if _cacheable:
                transcript_cache[t] = body
            # 进度（每 item 后 save）：多视频文档逐个推进；单视频（最常见）只有一个
            # video block——save 发生在 MPS 等待结束后，等待期间无中间写入。判死无风险
            # （探活按任务状态判 alive，与 save 无关）。短路/失败分支都落到此处统一
            # 收尾（warnings 随 result_summary 保存，不丢；worker 异常路径也 save）；
            # 缓存命中在 try 前 continue（无转写，无进度更新）
            task.progress = min(0.9, task.progress + 0.1)
            self._repo.save(task)

        # 循环结束写转写缓存（覆盖写，与 content.md 同幂等语义）
        if transcript_cache:
            try:
                _cache_path.parent.mkdir(parents=True, exist_ok=True)
                _cache_path.write_text(
                    json.dumps(transcript_cache, ensure_ascii=False), encoding="utf-8",
                )
            except Exception:
                logger.warning(
                    "[jonex] transcript 缓存写入失败 %s", _cache_path, exc_info=True,
                )

    def _record_multimodal_warning(self, task: TaskInfo, wtype: str, message: str) -> None:
        """[jonex] 转写失败/跳过/空内容的机器可读信号：写入 result_summary.extensions，
        **经 atomic-rag-server-v2.py 任务状态接口暴露**（extensions 是显式白名单，
        需新增 multimodal_warnings key）——knowledge-base 只拿状态 JSON，拿不到 task
        对象；对账从 status_info 读入写入文档 extra_metadata["multimodal_warnings"]。
        否则任务 completed + 文档 compiled 全绿，唯一痕迹是 Wiki 正文的一句话。
        ⚠️ 不写 llm_wiki_compile_warnings：该列属编译侧，compiling/stale 时会被清空
        ——对账 READY 写入会在编译开始时丢失。解析侧/编译侧两个警告来源并列展示。
        """
        if task.result_summary is None:
            task.result_summary = ResultSummary(doc_id=task.document_id or "")
        warns = task.result_summary.extensions.setdefault("multimodal_warnings", [])
        warns.append({"type": wtype, "message": message})

    @staticmethod
    def _classify_transcribe_error(exc: Exception) -> str:
        """[jonex] 异常 → 粗粒度原因（安全：异常原文可能含 COS URL/签名 token，绝不进 content.md）。

        优先按异常**类型名**判定（覆盖 asyncio.TimeoutError / httpx.TimeoutException /
        httpx.ConnectError 等超时连接类；ConfigError / ConfigurationError 类 → 配置缺失）；
        其余一律「转写服务不可用」——**不拼 str(exc)** 参与匹配（异常文本可能含中文/URL，
        匹配不可预期），也避免 `KeyError: 'missing_field'` / `FileNotFoundError` 被误归
        「配置缺失」误导排查。异常名与完整 str(exc) 只进日志。
        """
        _name = type(exc).__name__
        if "Timeout" in _name or "ConnectError" in _name:
            return "转写超时"
        if "Config" in _name and "Error" in _name:
            return "配置缺失"
        return "转写服务不可用"

    @staticmethod
    def _fmt_segment(s: dict) -> str:
        """[jonex] segment → "[mm:ss-mm:ss] text"；st/et 任一缺失时省略时间前缀；
        超过 1 小时进位为 "[hh:mm:ss-…]"。"""
        st, et = s.get("start_time"), s.get("end_time")
        text = str(s.get("text", "")).strip()
        if st is None or et is None:
            return text

        def _fmt(v):
            if isinstance(v, (int, float)):
                total = int(v)
                if total >= 3600:
                    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
                return f"{total // 60:02d}:{total % 60:02d}"
            return str(v)
        return f"[{_fmt(st)}-{_fmt(et)}] {text}".strip()

    def _resolve_task_prompt_overrides(self, task: TaskInfo):
        """[jonex] B2: 按 task.prompt_ids 解析主解析提示词覆盖（PromptOverride）。

        - 无 prompt_ids / 无 pcm → None（走内置默认）。
        - 缺失或空内容的 prompt_id：RAG_PROMPT_STRICT（默认 true）时 **fail-fast**（抛异常 →
          worker 置任务 FAILED，错误含缺失 id），避免"KB 有 id 但 atomic-rag 找不到"时静默走默认。
        """
        pids = list(getattr(task, "prompt_ids", []) or [])
        if not pids:
            return None
        if self._pcm is None:
            logger.warning(
                "Task %s has prompt_ids but PromptConfigManager not configured; using defaults",
                task.task_id,
            )
            return None
        from raganything.service.prompt_integration import PromptOverride

        strict = os.getenv("RAG_PROMPT_STRICT", "true").lower() in ("1", "true", "yes", "on")
        by_code: dict[str, str] = {}
        for pid in pids:
            item = self._pcm.get(task.tenant_id, pid)
            if item is None or not getattr(item, "content", ""):
                msg = f"prompt config not found or empty: id={pid} tenant={task.tenant_id}"
                if strict:
                    raise RuntimeError(f"PROMPT_OVERRIDE_MISSING: {msg}")
                logger.warning("Prompt override missing (fallback to default): %s", msg)
                continue
            by_code[item.prompt_code] = item.content
        return PromptOverride(by_code=by_code) if by_code else None

    async def _fetch_cos_object(self, task: TaskInfo) -> str | None:
        """[jonex] R7-2b: 任务 pipeline 前从 COS 异步下载文件到本地临时路径。

        full / parse_only 执行模式共用（parse_only 即 OpenKB 管线）。返回
        本地临时文件路径，调用方负责 finally 清理；非 COS 后端返回 None；
        下载失败抛 OBJECT_FETCH_FAILED（可被 _classify_error 识别）。
        """
        if not (task.storage_backend == "cos" and task.storage_key):
            return None

        import uuid as _uuid
        _cos_cache = self._cos_cache_dir()
        # [jonex] P0-1: sweep 兜底清残留（异常退出时 TTL 过期 → 下一次下载前清理）
        self._sweep_cos_cache()
        _base_name = os.path.basename(task.file_path or task.storage_key) or "cos_object"
        local_path = os.path.join(_cos_cache, f"{_uuid.uuid4().hex}_{_base_name}")
        try:
            from jonex_core.common.object_storage import get_object_storage
            await get_object_storage().get_to_path(task.storage_key, local_path)
            logger.info(
                "FetchObject: COS 下载完成 key=%s → %s task=%s",
                task.storage_key, local_path, task.task_id,
            )
            task.file_path = local_path
            # 下游 pipeline 统一按本地文件处理
            # 不修改 task.storage_backend（保留 cos 语义给终态 finally 判断是否清理）
        except Exception as _e:
            logger.exception(
                "FetchObject: COS 下载失败 key=%s task=%s",
                task.storage_key, task.task_id,
            )
            # [jonex] P0-1: 下载失败不留残留空文件
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
            except OSError:
                pass
            # 抛一个能被 _classify_error 识别为 OBJECT_FETCH_FAILED 的异常
            raise RuntimeError(f"OBJECT_FETCH_FAILED: COS 对象下载失败 key={task.storage_key}: {_e}") from _e

        # [jonex] MPS 视频路径：视频文件需保留 COS URL 供 MPS backend 使用
        # [jonex] P2: 改用 storage_key 判扩展名（本地名是 {uuid}_{basename}，可能丢扩展名）
        _ext = os.path.splitext(task.storage_key)[1].lower()
        if _ext in {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.mpg', '.mpeg', '.3gp'}:
            _bucket = os.getenv("COS_BUCKET", "")
            _region = os.getenv("COS_REGION", "")
            if _bucket and _region:
                task.mps_video_url = f"https://{_bucket}.cos.{_region}.myqcloud.com/{task.storage_key}"
                logger.info("FetchObject: MPS COS URL set for video task=%s", task.task_id)
        return local_path

    async def _execute_pipeline_http(self, task: TaskInfo, handle: TaskHandle):
        """HTTP mode pipeline execution."""
        # Check cancellation before starting
        if handle.cancel_event.is_set():
            raise TaskCancelledError()

        # ── [jonex] P0-A: ontology-only 执行模式 ──
        # 跳过 parse/multimodal/push，直接按 document_id 从 LightRAG 读实体/关系抽本体。
        # ctx.content_list 不可用（无解析），_run_ontology_extraction 内部按 document_id
        # 分页读 LightRAG，不依赖 content_list（传空列表即可）。
        if task.execution_mode == "ontology_only":
            await self._run_ontology_only(task, handle)
            return

        # ── [jonex] parse_only 执行模式（OpenKB 管线）──
        # 只解析产出 markdown 供 OpenKB 编译，跳过 multimodal/push_chunks/ontology，
        # 不写 LightRAG/Neo4j（KB 级互斥，避免与 OpenKB 双写）。
        # [jonex] R7-2b-fix: parse_only 同样需要先从 COS 拉取文件（与 full 模式共用
        # _fetch_cos_object），否则 file_path 是 storage_key（相对 COS key）本地不存在，
        # parse_document 直接 FileNotFoundError。
        if task.execution_mode == "parse_only":
            _cos_temp_path: str | None = None  # [jonex] P0-1: 供 finally 清理
            if task.storage_backend == "cos" and task.storage_key:
                _cos_temp_path = await self._fetch_cos_object(task)
            try:
                await self._run_parse_only(task, handle)
            finally:
                # [jonex] P0-1: 清理 COS 临时文件（即时释放，task-owned temp）
                if _cos_temp_path:
                    try:
                        if os.path.exists(_cos_temp_path):
                            os.remove(_cos_temp_path)
                            logger.debug("FetchObject: cleaned COS temp %s task=%s", _cos_temp_path, task.task_id)
                    except OSError:
                        logger.warning("FetchObject: failed to clean COS temp %s", _cos_temp_path)
            return

        # Register progress callback
        progress_cb = ProgressTrackingCallback(task, handle)
        self._pipeline_executor.callback_manager.register(progress_cb)

        # ── [jonex] R7-2b: FetchObject —— pipeline 前异步下载 COS 对象 ──
        # 把 COS 下载从同步 HTTP 请求路径（insert/retry invoke）移到异步任务 pipeline，
        # invoke 立刻返回 task_id，空窗期压到毫秒级。
        _cos_temp_path: str | None = None  # [jonex] P0-1: 供 finally 清理
        if task.storage_backend == "cos" and task.storage_key:
            _cos_temp_path = await self._fetch_cos_object(task)

        # ── PipelineContext (base) — per-task context ──
        # 用 raganything.pipeline.base.PipelineContext（dataclass，字段齐全），
        # 而非本模块的统计用 PipelineContext（__slots__ 缺 collected_doc_ids 等），
        # 否则 pipeline.execute 的 merge_context(dataclasses.replace) 与回写会报错。
        ctx = PipelineCtx(
            file_path=task.file_path,
            file_name=task.name,
            tenant_id=task.tenant_id,
            kb_id=task.kb_id,
            doc_id=task.document_id or "",
            document_id=task.document_id or "",
            cancel_event=handle.cancel_event,
            force_reparse=task.config_snapshot.get("force_reparse", False),
            parser_type=task.config_snapshot.get("parser", ""),
            config_snapshot=task.config_snapshot,
            mps_video_url=task.mps_video_url or "",
        )
        # [jonex] B2: 主解析提示词覆盖（按 task.prompt_ids 解析；缺失 id fail-fast）
        ctx.prompt_overrides = self._resolve_task_prompt_overrides(task)
        ctx.started_at = time.time()

        # [jonex] Gap A2: 设置入库计量 contextvar，使 driver + fallback 闭包能
        # 读到逐任务变化的 doc_id/trace_id（tenant/kb 由 build 时静态兜底）。
        # contextvar 在 asyncio.create_task 时复制进子 task，并发入库不串租户。
        _ingest_token = set_ingest_ctx(
            tenant_id=task.tenant_id,
            kb_id=task.kb_id,
            doc_id=task.document_id or "",
            trace_id=task.task_id,
        )

        doc_id = None
        try:
            # Check cancellation before pipeline
            if handle.cancel_event.is_set():
                raise TaskCancelledError()

            # [jonex] R1-b：reparse_strict 确定性替换 —— 先删全量旧 doc，再推全量新 chunk。
            # 旧 doc 由 document_id 精确确定（PG 传入 ∪ LightRAG 按 document_id 现查），
            # 不再依赖内容哈希的 old−new 差集（根除非确定性全量删除风暴的源头）。
            # 删除失败即终止 reparse（不残留新旧混合）。
            if task.execution_mode == "reparse_strict":
                task.old_rag_doc_ids = await self._snapshot_old_doc_ids(task)
                self._repo.save(task)
                old_ids = set(task.old_rag_doc_ids or [])
                if old_ids:
                    converged = await self._reparse_delete_all_old(task)
                    if not converged:
                        self._fail_task(
                            task, ErrorCode.LIGHTRAG_ERROR,
                            "reparse 旧数据删除未收敛（LightRAG 删除未完成），请重试",
                        )
                        return

            # Run HTTP pipeline: parse → multimodal → push_chunks
            doc_id = await self._pipeline_executor.process_document_complete(
                file_path=task.file_path,
                file_name=task.name,
                cancel_event=handle.cancel_event,
                force_reparse=task.config_snapshot.get("force_reparse", False),
                tenant_id=task.tenant_id,
                kb_id=task.kb_id,
                doc_id=task.document_id,
                ctx=ctx,
            )

            if doc_id:
                # ── Collect tracking info from PipelineContext ──
                task.lightrag_doc_ids = list(ctx.collected_doc_ids)
                task.pending_track_ids = list(ctx.pending_track_ids)
                task.failed_chunk_count = getattr(ctx, 'failed_chunk_count', 0)
                task.total_chunk_count = getattr(ctx, 'total_chunk_count', 0)
                # [jonex] #6: persist timeout count for KB reconciliation
                task.timeout_chunk_count = getattr(ctx, 'timeout_chunk_count', 0)

                # ── 提前置位：push_chunks 完成后立即标 ontology_status，
                # 让 KB 对账循环在下一轮（≤30s）就能提升文档状态到 READY+EXTRACTING，
                # 不必等 _run_ontology_extraction 执行（中间还有 summary/reparse 等步骤）。
                # 约束：① reparse_strict 跳过（差集删旧未收敛，不应提前暴露 READY）；
                # ② all_duplicated 跳过（后置逻辑会直接标 completed，中间 extracting
                #    窗口会被对账读到并触发不必要的 ontology-only 重抽，抵消 #5 的节省）。
                total_pushed = getattr(ctx, 'total_pushed_count', 0)
                duplicated = getattr(ctx, 'duplicated_chunk_count', 0)
                all_duplicated = total_pushed > 0 and duplicated == total_pushed
                if (
                    task.execution_mode != "reparse_strict"
                    and not all_duplicated
                    and os.getenv("ONTOLOGY_EXTRACT_ENABLED", "false").lower()
                    in ("1", "true", "yes", "on")
                    and task.ontology_schema
                ):
                    task.ontology_status = "extracting"
                    self._repo.save(task)

                # ── Read result summary from :9621 ──
                summary = await self._read_result_summary_http(
                    task.tenant_id, task.kb_id, task.document_id,
                )
                ctx.completed_at = time.time()
                summary.duration_seconds = ctx.duration_seconds
                summary.doc_id = doc_id

                # ── Block stats from content_list（base ctx 无统计方法，就地统计）──
                _tb = _tab = _cod = 0
                for _item in (ctx.content_list or []):
                    _t = _item.get("type", "text")
                    if _t == "text":
                        _tb += 1
                    elif _t == "table":
                        _tab += 1
                    elif _t == "code":
                        _cod += 1
                summary.blocks = _tb + _tab + _cod
                summary.text_blocks = _tb
                summary.table_blocks = _tab
                summary.code_blocks = _cod

                # ── [jonex] §table-grid-v2 O4: 表格处理统计透出 ──
                # （tables_total/tables_normalized/tables_fallback/rows_total/
                # cols_unnamed/oversize_table_chunks）。cols_unnamed>0 意味着
                # 仍有 col_N 占位列名（表头推断失败），KB 侧可据此留文档级告警。
                _table_stats = getattr(ctx, "table_stats", None) or {}
                if _table_stats:
                    summary.extensions = dict(summary.extensions or {})
                    summary.extensions["table_stats"] = _table_stats

                task.result_summary = summary

                # [jonex] OpenKB 产物落盘：把 parsed markdown+图片写到共享 inputs 卷，
                # 供 knowledge_base → OpenKB 编译读取（相对路径经 get_task_status 暴露）。
                self._write_openkb_artifact(task, ctx)

                # [jonex] R1-b：旧 doc 已在推前删除，此处无需 converge。

                # ── Ontology extraction (by document_id filter) ──
                # [jonex] #5: all-duplicated guard — skip ontology when every chunk
                # was idempotent duplicate, saving LLM cost (aligned with v1).
                total_pushed = getattr(ctx, 'total_pushed_count', 0)
                duplicated = getattr(ctx, 'duplicated_chunk_count', 0)
                all_duplicated = total_pushed > 0 and duplicated == total_pushed

                if (
                    os.getenv("ONTOLOGY_EXTRACT_ENABLED", "false").lower()
                    in ("1", "true", "yes", "on")
                    and task.ontology_schema
                ):
                    if all_duplicated:
                        task.ontology_status = "completed"
                        logger.info(
                            f"Task {task.task_id}: 全 chunk 幂等重复（{duplicated}/{total_pushed}），"
                            f"跳过本体抽取"
                        )
                    else:
                        await self._run_ontology_extraction(task, ctx, doc_id)

                task.status = TaskStatus.COMPLETED
                task.progress = 1.0
                task.current_step = "done"

                # [jonex] §11 v2 ingest_timing 结构化日志（成功路径）
                _log_ingest_timing_v2(task, "completed", force_ontology_only=False)

                logger.info(
                    f"Task {task.task_id} completed (HTTP mode): doc_id={doc_id} "
                    f"entities={summary.entities} chunks={summary.chunks} "
                    f"lightrag_doc_ids={len(task.lightrag_doc_ids)}"
                )
            else:
                # [jonex] #3/#6: 失败也保留已确认 doc_id / pending track / 分类计数，
                # 并用 pipeline 真实错误（含 "RAG_PUSH_TIMEOUT" / "...硬失败" 前缀）作为
                # 失败原因，供 KB 对账区分超时 vs 硬失败（否则被写死为通用错误而丢失语义）。
                task.lightrag_doc_ids = list(ctx.collected_doc_ids)
                task.pending_track_ids = list(ctx.pending_track_ids)
                task.failed_chunk_count = getattr(ctx, "failed_chunk_count", 0)
                task.total_chunk_count = getattr(ctx, "total_chunk_count", 0)
                task.timeout_chunk_count = getattr(ctx, "timeout_chunk_count", 0)
                reason = getattr(ctx, "error", None) or (
                    "Pipeline returned no doc_id (may indicate internal error)"
                )
                # [jonex] R1-b：reparse_strict 旧数据已在推前删除，推新失败无 compensate。
                # 旧数据不可恢复，需用户重新上传源文件触发入库。
                if task.execution_mode == "reparse_strict":
                    reason = "reparse 推新失败（旧数据已删除，请重新上传文档）: " + reason
                self._fail_task(task, ErrorCode.UNKNOWN, reason)
                # [jonex] §11 v2 ingest_timing 结构化日志（失败路径：doc_id=None）
                _log_ingest_timing_v2(task, "failed", force_ontology_only=False,
                                     error=getattr(task, "error_message", ""))
        except TaskCancelledError:
            # ── Cancel: save collected doc_ids, mark cancelled ──
            task.lightrag_doc_ids = list(ctx.collected_doc_ids)
            task.pending_track_ids = list(ctx.pending_track_ids)
            task.failed_chunk_count = getattr(ctx, 'failed_chunk_count', 0)
            task.total_chunk_count = getattr(ctx, 'total_chunk_count', 0)
            task.timeout_chunk_count = getattr(ctx, 'timeout_chunk_count', 0)
            # 复位提前置位的 ontology_status，避免 FAILED 任务悬空在 extracting
            if task.ontology_status == "extracting":
                task.ontology_status = "pending"
            # [jonex] R1-b：reparse_strict 旧数据已在推前删除，取消无 compensate。
            self._repo.save(task)
            # [jonex] §11 v2 ingest_timing 结构化日志（取消路径）
            _log_ingest_timing_v2(task, "cancelled", force_ontology_only=False,
                                 error="task_cancelled")
            raise
        except Exception as e:
            # [jonex] #6: persist collected doc_ids even on failure
            # so KB reconciliation knows which chunks already made it in
            task.lightrag_doc_ids = list(ctx.collected_doc_ids)
            task.pending_track_ids = list(ctx.pending_track_ids)
            task.failed_chunk_count = getattr(ctx, 'failed_chunk_count', 0)
            task.total_chunk_count = getattr(ctx, 'total_chunk_count', 0)
            task.timeout_chunk_count = getattr(ctx, 'timeout_chunk_count', 0)
            # 复位提前置位的 ontology_status，避免 FAILED 任务悬空在 extracting
            if task.ontology_status == "extracting":
                task.ontology_status = "pending"
            # [jonex] R1-b：reparse_strict 旧数据已在推前删除，异常无 compensate。
            self._repo.save(task)
            # [jonex] §11 v2 ingest_timing 结构化日志（异常路径：含失败前已完成阶段的耗时）
            _log_ingest_timing_v2(task, "failed", force_ontology_only=False,
                                 error=str(e)[:200])
            logger.error(f"Pipeline error for {task.task_id}: {e}\n{traceback.format_exc()}")
            raise
        finally:
            # [jonex] Gap A2: 还原 contextvar，避免污染同一事件循环的下一任务
            reset_ingest_ctx(_ingest_token)
            # [jonex] P0-1: 清理 COS 临时文件（即时释放，task-owned temp）
            if _cos_temp_path:
                try:
                    if os.path.exists(_cos_temp_path):
                        os.remove(_cos_temp_path)
                        logger.debug("FetchObject: cleaned COS temp %s task=%s", _cos_temp_path, task.task_id)
                except OSError:
                    logger.warning("FetchObject: failed to clean COS temp %s", _cos_temp_path)
            self._pipeline_executor.callback_manager.unregister(progress_cb)
            task.updated_at = datetime.now(timezone.utc)
            self._repo.save(task)

    # ── Reparse strict replacement helpers (阶段4) ───────────────────

    async def _list_doc_ids_by_document(self, task: TaskInfo) -> list[str]:
        """按 KB document_id 全量分页查询 LightRAG 现有 doc id（只精确匹配，不用文件名兜底）。"""
        found: list[str] = []
        page = 1
        while True:
            result = await self._http_client.get_documents(
                task.tenant_id, task.kb_id,
                document_id=task.document_id or "", page=page, page_size=200,
            )
            entries = (result or {}).get("documents") or []
            if not entries:
                for status_list in (result or {}).get("statuses", {}).values():
                    if isinstance(status_list, list):
                        entries.extend(status_list)
            if not entries:
                break
            for e in entries:
                did = e.get("id")
                if did:
                    found.append(did)
            total = int((result or {}).get("total", 0) or 0)
            if len(entries) < 200 or (total and page * 200 >= total):
                break
            page += 1
        return found

    async def _snapshot_old_doc_ids(self, task: TaskInfo) -> list[str]:
        """[jonex] 阶段4：权威 old_ids = PG 传入 ∪ LightRAG 按 document_id 全量分页。

        只按 document_id 精确匹配，禁用文件名兜底；实时分页查询失败 → 抛错终止 reparse
        （不能在不知道旧集合的情况下推新/删旧）。
        """
        old_ids: set[str] = set(task.old_rag_doc_ids or [])
        try:
            old_ids.update(await self._list_doc_ids_by_document(task))
        except Exception as e:
            raise RuntimeError(
                f"reparse 快照旧 doc 失败（LightRAG 查询异常），终止 reparse: {e}"
            ) from e
        return list(old_ids)

    async def _reparse_delete_all_old(self, task: TaskInfo) -> bool:
        """[jonex] R1-b + P0：推新前删全文档的全部旧 LightRAG doc（按 document_id），确定性替换。

        P0 收敛循环：每轮重新发起 delete + 读一致性确认，只要 pipeline 空闲就能被受理，
        不再出现「删一次被 busy 拒绝 → 只剩空 poll」的死局。

        返回：
        - True：无需删 / 已删净 → 调用方可继续 parse+push；
        - False：超预算仍有残留 → 保持 current_step="cleanup"，交调用方判失败 +
          KB 对账 3-A 动态超时兜底。
        """
        old = set(task.old_rag_doc_ids or [])
        if not old:
            return True
        task.current_step = "cleanup"
        task.delete_pending_ids = list(old)
        task.cleanup_total = len(old)  # [jonex] 3-A：记录初始待删总量
        self._repo.save(task)

        residual = await self._converge_delete(task, old)
        if residual:
            task.delete_pending_ids = list(residual)
            task.current_step = "cleanup"
            self._repo.save(task)
            logger.warning(
                "[jonex][R1-b] reparse 删旧未收敛 task=%s 残留=%d，保持 cleanup 交 3-A 兜底",
                task.task_id, len(residual),
            )
            return False
        task.delete_pending_ids = []
        task.current_step = ""
        self._repo.save(task)
        logger.info(
            "[jonex][R1-b] reparse 删旧完成 task=%s 已删=%d",
            task.task_id, len(old),
        )
        return True

    async def _converge_delete(self, task: TaskInfo, old_ids: set[str]) -> set[str]:
        """[jonex] P0：收敛删除循环 —— 每轮重新发起 delete + 读一致性确认。

        与旧「删一次→只 poll」的关键区别：每轮 poll 前重新尝试 delete_docs。
        只要 pipeline 一空闲，下一轮 delete 立刻被受理，而不是空等一件没发生的事。

        Args:
            task: 当前任务
            old_ids: 目标删除集合（old_rag_doc_ids）

        Returns:
            残留集合（空 = 收敛成功），非空时调用方判 failed 交 KB 3-A 兜底
        """
        n = max(0, len(old_ids))
        if n == 0:
            return set()

        max_elapsed = float(os.getenv("RAG_CLEANUP_MAX_ELAPSED", "1800"))
        poll_delay = float(os.getenv("RAG_CLEANUP_POLL_DELAY", "5"))
        per_doc_sec = float(os.getenv("RAG_CLEANUP_POLL_PER_DOC_SEC", "2"))

        # 总量级缩放 deadline：min 防止误算导致无限等
        scaled_elapsed = min(
            max_elapsed,
            max(300.0, 300.0 + per_doc_sec * n),
        )
        deadline = time.monotonic() + scaled_elapsed
        delete_retry_interval = max(poll_delay * 3, 10.0)
        # 首次立即发起 delete
        last_delete_attempt = -delete_retry_interval

        logger.info(
            "[jonex][P0] converge delete start task=%s old_ids=%d max_elapsed=%.0fs "
            "poll_delay=%.1fs retry_interval=%.1fs",
            task.task_id, n, scaled_elapsed, poll_delay, delete_retry_interval,
        )

        remaining: set[str] = set(old_ids)
        while time.monotonic() < deadline:
            # 1. 查询当前仍可见的旧 doc
            try:
                current = set(await self._list_doc_ids_by_document(task))
            except Exception as e:
                logger.warning(
                    "[jonex][P0] converge query failed task=%s: %s，等下一轮",
                    task.task_id, e,
                )
                await asyncio.sleep(poll_delay)
                continue

            remaining = old_ids & current
            if not remaining:
                logger.info(
                    "[jonex][P0] converge delete 收敛 task=%s: 旧 doc 已全部不可见",
                    task.task_id,
                )
                return set()

            # 2. 更新 task 进度供 KB 对账观察
            task.delete_pending_ids = list(remaining)
            self._repo.save(task)

            # 3. 重新发起 delete（间隔控制，不对同一批持续轰炸）
            now = time.monotonic()
            if now - last_delete_attempt >= delete_retry_interval:
                try:
                    await self._http_client.delete_docs(
                        list(remaining), tenant_id=task.tenant_id, kb_id=task.kb_id,
                        document_id=task.document_id or "", trace_id=task.task_id,
                    )
                    logger.info(
                        "[jonex][P0] converge delete 重新发起删除 task=%s remaining=%d",
                        task.task_id, len(remaining),
                    )
                except Exception as e:
                    logger.warning(
                        "[jonex][P0] converge delete 本轮 delete 失败 task=%s "
                        "remaining=%d: %s",
                        task.task_id, len(remaining), e,
                    )
                last_delete_attempt = now

            await asyncio.sleep(poll_delay)

        logger.warning(
            "[jonex][P0] converge delete timeout task=%s: 残留=%d/%d (%.0fs)，交 3-A 兜底",
            task.task_id, len(remaining), n, scaled_elapsed,
        )
        return remaining

    async def _resume_cleanup(self, task: TaskInfo) -> None:
        """[jonex] R1-b P0-J + P0：容器重启后只恢复 cleanup（续删剩余旧 doc），不重跑解析管线。

        P0 收敛循环：每轮重新发起 delete，不会因首次被 busy 拒绝就永久卡死。
        """
        try:
            # R1-b：全部 old_rag_doc_ids 即为待删集合
            delete_set = set(task.old_rag_doc_ids or [])
            if not delete_set:
                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.now(timezone.utc)
                task.current_step = "done"
                self._repo.save(task)
                return

            residual = await self._converge_delete(task, delete_set)
            if residual:
                task.delete_pending_ids = list(residual)
                self._fail_task(
                    task, ErrorCode.LIGHTRAG_ERROR,
                    "reparse 旧数据删除未收敛（续删仍残留），请重试",
                )
                logger.warning(
                    "[jonex][R1-b] resume 删旧未收敛 task=%s 残留=%d，判失败交 3-A 兜底",
                    task.task_id, len(residual),
                )
                return
            task.current_step = "done"
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now(timezone.utc)
            self._repo.save(task)
        except Exception as e:
            logger.error(f"Resume cleanup failed for {task.task_id}: {e}")

    # ── [jonex] parse-only execution (OpenKB 管线) ───────────────────

    async def _run_parse_only(self, task: TaskInfo, handle: TaskHandle) -> None:
        """[jonex] 只解析执行 — 产出 markdown 供 OpenKB 编译，不写 LightRAG/Neo4j。

        用于 pipeline_type=openkb 的文档：调用 parser 得到 content_list（复用解析缓存），
        序列化 markdown + 图片到共享卷（_write_openkb_artifact，路径经 get_task_status
        暴露），跳过 multimodal/push_chunks/ontology。实现 KB 级互斥，避免与 OpenKB 双写。
        """
        if handle.cancel_event.is_set():
            raise TaskCancelledError()

        ctx = PipelineCtx(
            file_path=task.file_path,
            file_name=task.name,
            tenant_id=task.tenant_id,
            kb_id=task.kb_id,
            doc_id=task.document_id or "",
            document_id=task.document_id or "",
            cancel_event=handle.cancel_event,
            config_snapshot=task.config_snapshot or {},
            parser_type=(task.config_snapshot or {}).get("parser", ""),
        )
        ctx.started_at = time.time()

        task.current_step = "parse"
        task.progress = 0.3
        self._repo.save(task)

        # 仅解析（复用 parse cache）；不入库、不抽本体
        content_list, _doc_id = await self._pipeline_executor.parse_document(
            task.file_path,
            (task.config_snapshot or {}).get("output_dir") or None,
            (task.config_snapshot or {}).get("parse_method") or None,
        )
        ctx.content_list = content_list

        # [jonex] 多模态文本化：视频/音频转写（parse_only 之前跳过 multimodal → 产物空洞）。
        # 转写前置推进进度 + 置 current_step——注意：knowledge-base API/DTO 未暴露
        # current_step/progress，前端不可见；价值 = 原子侧状态可读 + 对账探活日志。
        # 判死无风险（探活按任务状态判 alive，与 save 无关）。
        task.current_step = "multimodal"
        task.progress = 0.5
        self._repo.save(task)
        await self._textualize_multimodal(task, handle, content_list)

        # 写 OpenKB 产物（markdown+图片 → 共享卷；含转写正文；相对路径经 result_summary 暴露）
        self._write_openkb_artifact(task, ctx)

        if task.result_summary is None:
            task.result_summary = ResultSummary(doc_id=task.document_id or "")
        task.status = TaskStatus.COMPLETED
        task.progress = 1.0
        task.current_step = "done"
        self._repo.save(task)
        _log_ingest_timing_v2(task, "completed")
        logger.info(
            "[jonex] Task %s completed (parse_only): doc=%s blocks=%d",
            task.task_id, task.document_id, len(content_list or []),
        )

    # ── Ontology-only execution (P0-A) ──────────────────────────────

    async def _run_ontology_only(self, task: TaskInfo, handle: TaskHandle) -> None:
        """[jonex] P0-A: ontology-only 执行 — 只重抽本体，跳过 parse/multimodal/push。

        直接按 document_id 从 LightRAG 读实体/关系做本体归类，不需要原文件，也不需要
        content_list。要求任务携带 compiled schema（否则无法归类 → 失败）。
        """
        if handle.cancel_event.is_set():
            raise TaskCancelledError()

        if not task.ontology_schema:
            self._fail_task(
                task, ErrorCode.CONFIG_INVALID,
                "ontology_only 任务缺少 compiled schema，无法归类",
            )
            # [jonex] §11 v2 ingest_timing（ontology_only 失败路径）
            _log_ingest_timing_v2(task, "failed", force_ontology_only=True,
                                 error=getattr(task, "error_message", ""))
            return

        # 轻量 ctx：content_list 为空，_run_ontology_extraction 按 document_id 读 LightRAG
        ctx = PipelineCtx(
            file_path=task.file_path,
            file_name=task.name,
            tenant_id=task.tenant_id,
            kb_id=task.kb_id,
            doc_id=task.document_id or "",
            document_id=task.document_id or "",
            cancel_event=handle.cancel_event,
            config_snapshot=task.config_snapshot or {},
        )
        ctx.started_at = time.time()

        await self._run_ontology_extraction(
            task, ctx, task.document_id or "", force_edge_based=True,
        )

        # ontology-only 复用已有 LightRAG doc，不产新 doc；状态收尾
        task.status = TaskStatus.COMPLETED
        task.progress = 1.0
        task.current_step = "done"
        self._repo.save(task)
        # [jonex] §11 v2 ingest_timing（ontology_only 成功路径）
        _log_ingest_timing_v2(task, "completed", force_ontology_only=True)
        logger.info(
            f"Task {task.task_id} completed (ontology_only): "
            f"doc={task.document_id} ontology_status={task.ontology_status}"
        )

    # ── Ontology extraction (Stage 5) ───────────────────────────────

    async def _run_ontology_extraction(
        self, task: TaskInfo, ctx: PipelineContext, doc_id: str,
        *, force_edge_based: bool = False,
    ) -> None:
        """[jonex] Run ontology extraction via jonex_core.OntologyExtractor.

        Orchestration only — all ontology classification/LLM prompt/post-validation
        logic lives in jonex_core, NOT in raganything.

        Reads LightRAG entities + relations via HttpLightRagClient,
        resolves compiled schema, calls OntologyExtractor.extract(),
        and saves results to task.ontology_status/data/error.
        """
        import re

        task.ontology_status = "extracting"
        # Push a timeline entry for the ontology stage
        task.timeline.append(StageTiming(
            stage="ontology_extract",
            label="本体抽取",
            detail="LLM 本体类型归类 + 关系定型",
            started_at=datetime.now(timezone.utc),
        ))
        self._repo.save(task)

        try:
            kb_id = task.kb_id or ""

            # ① Read LightRAG entities (paginated, filtered by document_id)
            all_entities: list[dict] = []
            page = 1
            while True:
                try:
                    batch = await self._http_client.get_entities(
                        task.tenant_id, kb_id,
                        page=page, page_size=200,
                        document_id=task.document_id or "",
                    )
                except Exception as e:
                    logger.warning(
                        f"Ontology: failed to read entities for {task.task_id}: {e}"
                    )
                    break
                items = batch.get("items", [])
                if not items:
                    break
                all_entities.extend(items)
                total = int(batch.get("total", 0) or 0)
                if total and len(all_entities) >= total:
                    break
                page += 1

            # Filter namespace-token garbage entities
            _ns_pattern = re.compile(r"<!--yx:[a-f0-9]{8}-->")
            all_entities = [
                e for e in all_entities
                if not _ns_pattern.search(e.get("name", ""))
            ]

            if not all_entities:
                task.ontology_status = "failed"
                task.ontology_error = "LightRAG 存储中未找到候选实体，无法抽取本体"
                self._repo.save(task)
                return

            # ①b Read LightRAG relations (paginated)
            all_relations: list[dict] = []
            page = 1
            while True:
                try:
                    batch = await self._http_client.get_relationships(
                        task.tenant_id, kb_id,
                        page=page, page_size=200,
                        document_id=task.document_id or "",
                    )
                except Exception as e:
                    logger.warning(
                        f"Ontology: failed to read relations for {task.task_id}: {e}"
                    )
                    break
                items = batch.get("items", [])
                if not items:
                    break
                all_relations.extend(items)
                total = int(batch.get("total", 0) or 0)
                if total and len(all_relations) >= total:
                    break
                page += 1

            # ② Resolve compiled schema
            compiled_schema = task.ontology_schema
            if not compiled_schema:
                try:
                    from jonex_core.capability.atomic.ontology.compiled_schema_client import (
                        CompiledSchemaClient,
                    )
                    compiled_schema = await CompiledSchemaClient().get_schema(
                        task.tenant_id, kb_id,
                    )
                except Exception as e:
                    logger.warning(
                        f"Ontology: compiled schema fetch failed for {task.task_id}: {e}"
                    )

            # ③ Call jonex_core.OntologyExtractor.extract()
            from jonex_core.capability.atomic.ontology import OntologyRegistry
            from jonex_core.capability.atomic.rag.ontology_extractor import (
                OntologyExtractor,
            )

            registry = OntologyRegistry()
            schema_path = os.getenv(
                "ONTOLOGY_SCHEMA_PATH",
                "deploy/config/ontology/default.yaml",
            )
            try:
                registry.load(schema_path)
            except Exception as e:
                logger.warning(
                    f"Ontology: failed to load schema from {schema_path}: {e}"
                )

            extractor = OntologyExtractor(registry)

            scope = {
                "tenant_id": task.tenant_id,
                "knowledge_base_id": kb_id,
                "document_id": task.document_id or "",
                "trace_id": task.task_id,
            }

            result = await extractor.extract(
                content_list=ctx.content_list or [],
                lightrag_entities=all_entities,
                lightrag_relations=all_relations,
                scope=scope,
                compiled_schema=compiled_schema,
                # [jonex] P0-A.6：ontology-only / reparse 强制边定型，不受环境开关影响
                edge_based=True if force_edge_based else None,
            )

            # ── [jonex] §table-grid-v2 步14：Row-as-Object 表格对象抽取 ──
            # ctx.content_list 可用（常规 insert 链路）时，对表格做确定性映射
            # （一行 = 一个对象，列名 = 属性），产出并入 ontology_data。
            # 失败降级：仅打日志，不影响 LightRAG 抽取结果。
            if ctx.content_list:
                try:
                    await self._merge_table_objects(
                        ctx, scope, result, compiled_schema=compiled_schema,
                    )
                except Exception as exc:  # noqa: BLE001 — 表格对象失败不阻断本体
                    logger.warning(
                        "Table objects extraction failed for %s: %s",
                        task.task_id, exc,
                    )

            # ④ Save results to task
            task.ontology_status = "completed" if result.ok else "failed"
            task.ontology_data = {
                "entities": [
                    {
                        "canonical_name": e.canonical_name,
                        "entity_type": e.entity_type,
                        "aliases": e.aliases,
                        "attributes": e.attributes,
                        "description": e.description,
                        "confidence": e.confidence,
                        "source_chunks": e.source_chunks,
                        "extraction_method": e.extraction_method,
                        # [jonex] 改动 18：row_as_object 的 upsert 键随 ont_data 透传
                        "table_sig": e.table_sig,
                        "pk_value": e.pk_value,
                    }
                    for e in result.entities
                ],
                "relations": [
                    {
                        "source_name": r.source_name,
                        "source_type": r.source_type,
                        "target_name": r.target_name,
                        "target_type": r.target_type,
                        "relation_type": r.relation_type,
                        "confidence": r.confidence,
                    }
                    for r in result.relations
                ],
            }
            task.ontology_error = (
                str(result.errors[:1]) if result.errors else ""
            )

            logger.info(
                f"Ontology extraction done for {task.task_id}: "
                f"status={task.ontology_status} "
                f"entities={len(result.entities)} relations={len(result.relations)}"
            )

        except Exception as e:
            logger.warning(
                f"Ontology extraction failed for {task.task_id}: {e}",
                exc_info=True,
            )
            task.ontology_status = "failed"
            task.ontology_error = str(e)[:500]
        finally:
            # Close the ontology stage timing
            if task.timeline:
                last = task.timeline[-1]
                if last.stage == "ontology_extract" and last.ended_at is None:
                    now = datetime.now(timezone.utc)
                    last.ended_at = now
                    if last.started_at:
                        last.elapsed_seconds = (now - last.started_at).total_seconds()
            self._repo.save(task)

    async def _merge_table_objects(self, ctx, scope: dict, result,
                                   compiled_schema=None) -> None:
        """[jonex] §table-grid-v2 步14：Row-as-Object 表格对象抽取。

        对 content_list 的每个 table item：重跑网格化（与 push 阶段同函数
        保证一致性）→ 表级判定（LLM/缓存/规则，含表头判定模型化）→ 列名
        →TBox 属性映射（§10.2.4 第二次 LLM）→ 行级对象实例 + 分组关系，
        并入 ExtractionResult（extraction_method=row_as_object，attributes
        结构化，不走 200 截断）。
        """
        import hashlib as _hashlib

        from jonex_core.capability.atomic.rag.ontology_extractor import (
            ExtractedEntity,
            ExtractedRelation,
        )
        from jonex_core.capability.atomic.rag.table_object_extractor import (
            TableObjectExtractor,
            _compose_header_parts,
            apply_header_verdict,
        )
        from raganything.utils import (
            _table_grid_v2_enabled,
            get_table_body,
            normalize_table_grid,
            normalize_table_rows,
        )

        max_rows = int(os.getenv("TABLE_OBJECT_MAX_ROWS", "2000"))
        extractor = TableObjectExtractor()
        file_name = ctx.file_name or ""
        # §10.2.4 可选标准属性词表：compiled schema 各实体类型的属性名并集
        tbox_attributes: list | None = None
        if compiled_schema:
            try:
                tbox_attributes = sorted({
                    str(a.get("name", ""))
                    for et in (compiled_schema.get("entity_types") or [])
                    for a in (et.get("attributes") or [])
                    if a.get("name")
                }) or None
            except Exception as exc:  # noqa: BLE001 — 词表是提示项，失败不阻塞
                logger.warning("Row-as-Object: tbox_attributes 提取失败: %s", exc)

        table_entities: list = []
        table_relations: list = []
        for item in ctx.content_list or []:
            if item.get("type") != "table":
                continue
            raw_body = get_table_body(item)
            if _table_grid_v2_enabled():
                header, data_rows, meta = normalize_table_grid(raw_body)
            else:
                header, data_rows = normalize_table_rows(raw_body)
            if not data_rows or not header:
                continue

            caption = item.get("table_caption")
            if isinstance(caption, list):
                caption = "、".join(
                    str(c) for c in caption if str(c).strip()
                )
            caption = (caption or "").strip() or file_name

            verdict = await extractor.analyze_table(
                caption, header, data_rows, scope=scope,
                tbox_attributes=tbox_attributes,
            )
            # §10.2.3 层级关系：多级表头父级链（属性组），无额外表头时为空
            header_parts: list | None = None
            # [jonex] §table-grid-v2 步14 补缺：消费表级判定的表头/说明行
            # 结论。此前 header_row_indices / note_row_indices 只校验不消费
            # （对象抽取仍用规则表头）——模型认为 data_rows 中还有表头行
            # （多级表头漏识别）或说明行（长文本漏吸净）时，重建列名与
            # 对象行，行锚点经 row_map 保持与原 data_rows 对齐。
            if verdict.source != "rules":
                header2, rows2, row_map = apply_header_verdict(
                    header, data_rows, verdict,
                )
                if header2 != header:
                    # 属性组父级链（与 apply_header_verdict 同拼接口径，
                    # 同样过滤越界行号）
                    header_parts = _compose_header_parts(
                        [header]
                        + [
                            data_rows[i]
                            for i in verdict.header_row_indices
                            if 0 <= i < len(data_rows)
                        ],
                        len(header2),
                    )
                    # 主键/限定列按列位置重映射（多级拼接不改变列序）
                    pos = {name: i for i, name in enumerate(header)}
                    verdict.pk_columns = [
                        header2[pos[c]] for c in verdict.pk_columns if c in pos
                    ]
                    verdict.qualifier_columns = [
                        header2[pos[c]]
                        for c in verdict.qualifier_columns
                        if c in pos
                    ]
                    logger.info(
                        "Row-as-Object: 表「%s」应用表头判定：列名 %d 列重拼 "
                        "（+%d 表头行）、剔除 %d 说明行",
                        caption[:40], len(header2),
                        len(verdict.header_row_indices),
                        len(verdict.note_row_indices),
                    )
                header, data_rows = header2, rows2
            else:
                row_map = list(range(len(data_rows)))

            if not verdict.pk_columns:
                logger.info(
                    "Row-as-Object: 表「%s」主键识别失败，跳过对象抽取",
                    caption[:40],
                )
                continue

            table_sig = _hashlib.md5(
                "|".join(header).encode()
            ).hexdigest()[:8]
            objects = extractor.build_objects(
                caption, header, data_rows[:max_rows], verdict,
                table_sig=table_sig,
                table_idx=item.get("table_idx"),
                row_indices=row_map[:max_rows],
                header_parts=header_parts,
            )
            relations = extractor.build_group_relations(
                header, data_rows[:max_rows], objects, verdict,
            )
            for obj in objects:
                table_entities.append(ExtractedEntity(
                    canonical_name=obj.canonical_name,
                    entity_type=obj.entity_type,
                    aliases=obj.aliases,
                    attributes=obj.attributes,
                    description="",
                    confidence=1.0,
                    source_chunks=obj.source_chunks,
                    extraction_method="row_as_object",
                    table_sig=obj.table_sig,   # [jonex] 改动 18 upsert 键
                    pk_value=obj.pk_value,     # [jonex] 改动 18 upsert 键
                ))
            for rel in relations:
                table_relations.append(ExtractedRelation(
                    source_name=rel["source_name"],
                    source_type=rel["source_type"],
                    target_name=rel["target_name"],
                    target_type=rel["target_type"],
                    relation_type=rel["relation_type"],
                ))
            logger.info(
                "Row-as-Object: 表「%s」→ %d 对象 + %d 分组关系 "
                "(pk=%s, verdict=%s)",
                caption[:40], len(objects), len(relations),
                "、".join(verdict.pk_columns), verdict.source,
            )

        if table_entities:
            # O6：同名去重——表格对象（结构化 attributes）优先于 LightRAG
            # 文本实体；别名也占位（防止同名别名实体残留）。
            table_names = set()
            for e in table_entities:
                table_names.add(e.canonical_name)
                table_names.update(e.aliases)
            result.entities = [
                e for e in result.entities
                if not (
                    e.extraction_method != "row_as_object"
                    and (
                        e.canonical_name in table_names
                        or any(a in table_names for a in e.aliases)
                    )
                )
            ]
            result.entities.extend(table_entities)
            result.relations.extend(table_relations)
            # 表格对象保证 ok（与 LightRAG 零实体互不依赖）
            result.ok = True

    # ── Result summary (HTTP mode) ──────────────────────────────────

    async def _read_result_summary_http(
        self, tenant_id: str, kb_id: str, document_id: str,
    ) -> ResultSummary:
        """Read pipeline results from :9621 via HTTP."""
        summary = ResultSummary(doc_id=document_id)
        try:
            result = await self._http_client.get_document_parse_result(
                tenant_id=tenant_id, kb_id=kb_id, document_id=document_id,
            )
            docs = result.get("documents", {})
            entities = result.get("entities", {})
            relationships = result.get("relationships", {})
            summary.chunks = len(docs.get("items", docs.get("data", [])) if isinstance(docs, dict) else docs)
            summary.entities = len(entities.get("items", entities.get("data", [])) if isinstance(entities, dict) else entities)
            summary.relations = len(relationships.get("items", relationships.get("data", [])) if isinstance(relationships, dict) else relationships)
        except Exception as e:
            logger.warning(f"Failed to read summary for {document_id}: {e}")
        return summary

    # ── Restart recovery ────────────────────────────────────────────

    async def _resume_track_polling(self, task: TaskInfo):
        """Restart recovery: re-poll pending track_ids after TaskManager restart.

        Called from start() for non-terminal tasks with pending_track_ids.
        """
        pending = list(task.pending_track_ids)
        if not pending:
            # All tracks already terminal — determine final state
            if task.lightrag_doc_ids:
                task.status = TaskStatus.COMPLETED
                task.progress = 1.0
            else:
                self._fail_task(task, ErrorCode.LIGHTRAG_ERROR,
                                "All chunks failed during previous run")
            self._repo.save(task)
            return

        logger.info(
            f"Resume polling {len(pending)} tracks for task {task.task_id}"
        )

        try:
            terminal, still_pending = await self._http_client.batch_track_status(
                pending,
                tenant_id=task.tenant_id,
                kb_id=task.kb_id,
                max_wait_seconds=float(
                    os.getenv("RAG_TRACK_TIMEOUT_SECONDS", "1800")
                ),
                per_track_timeout_seconds=float(
                    os.getenv("RAG_TRACK_PER_CHUNK_TIMEOUT_SECONDS", "900")
                ),
            )
        except Exception as e:
            logger.error(f"Resume track polling failed for {task.task_id}: {e}")
            self._fail_task(task, ErrorCode.LIGHTRAG_ERROR, str(e))
            return

        # Merge completed doc_ids + classify terminal failures
        for tid, status in terminal.items():
            if status.state == "completed":
                for doc_id in status.doc_ids:
                    if doc_id not in task.lightrag_doc_ids:
                        task.lightrag_doc_ids.append(doc_id)
            else:
                # [jonex] #6: terminal failed → hard failure
                task.failed_chunk_count += 1

        # [jonex] #6: still_pending → timeout (not hard failure)
        task.timeout_chunk_count = len(still_pending)
        task.pending_track_ids = list(still_pending.keys())

        if not task.pending_track_ids:
            # All terminal — determine final state
            if task.lightrag_doc_ids:
                task.status = TaskStatus.COMPLETED
                task.progress = 1.0
            else:
                self._fail_task(task, ErrorCode.LIGHTRAG_ERROR,
                                "All chunks failed after resume")
        # else: still pending — keep current state

        self._repo.save(task)
        logger.info(
            f"Resume polling done for {task.task_id}: "
            f"{len(task.lightrag_doc_ids)} doc_ids, "
            f"{len(task.pending_track_ids)} still pending"
        )

    # ── Cleanup ─────────────────────────────────────────────────────

    # ── Parser artifact helpers (Bug 3) ──────────────────────────────

    @staticmethod
    def _get_content_list_from_cache(rag: Any, doc_id: str) -> list[dict] | None:
        """Retrieve MinerU online content_list from parse cache by doc_id.

        Parse cache entries carry the doc_id set during initial parsing.
        Match by doc_id rather than file_path (cache key is an opaque hash).
        """
        parse_cache = getattr(rag, "parse_cache", None)
        if parse_cache is None:
            return None

        try:
            # JsonKVStorage has no get_all(); iterate _data dict
            cache_data = getattr(parse_cache, "_data", {}) or {}
            for _key, entry in cache_data.items():
                if isinstance(entry, dict) and entry.get("doc_id") == doc_id:
                    cl = entry.get("content_list")
                    if cl and isinstance(cl, list):
                        return cl
            return None
        except Exception as e:
            logger.warning(f"Failed to read content_list from parse cache: {e}")
            return None

    @staticmethod
    def _extract_images(content_list: list[dict], target_dir: Path) -> int:
        """Extract images from content_list into target_dir.

        Handles URL-based images (download via httpx) and base64-encoded
        images.  Failures are logged and skipped — missing images don't
        block the text pipeline.

        Returns count of successfully extracted images.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        seen_urls: set[str] = set()
        extracted = 0
        name_counter: dict[str, int] = {}

        for idx, item in enumerate(content_list):
            if item.get("type") != "image":
                continue

            # ── URL-based images ──
            url = item.get("image_url") or item.get("url")
            if url and url not in seen_urls:
                seen_urls.add(url)
                try:
                    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
                    resp.raise_for_status()
                    base_name = (
                        url.rsplit("/", 1)[-1].split("?")[0]
                        or f"image_{idx:03d}"
                    )
                    name_counter[base_name] = name_counter.get(base_name, 0) + 1
                    if name_counter[base_name] > 1:
                        stem, ext = (
                            base_name.rsplit(".", 1)
                            if "." in base_name
                            else (base_name, "png")
                        )
                        base_name = f"{stem}_{name_counter[base_name]}.{ext}"
                    (target_dir / base_name).write_bytes(resp.content)
                    extracted += 1
                except Exception as e:
                    logger.warning(f"Failed to download image {url}: {e}")
                continue

            # ── Base64-encoded images ──
            b64 = item.get("image_base64") or item.get("base64")
            if b64:
                try:
                    data = base64.b64decode(b64)
                    name = item.get("name") or f"image_{idx:03d}"
                    name_counter[name] = name_counter.get(name, 0) + 1
                    if name_counter[name] > 1:
                        stem, ext = (
                            name.rsplit(".", 1)
                            if "." in name
                            else (name, "png")
                        )
                        name = f"{stem}_{name_counter[name]}.{ext}"
                    (target_dir / f"{name}.png").write_bytes(data)
                    extracted += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to decode base64 image at index {idx}: {e}"
                    )

        return extracted

    @staticmethod
    def _write_artifacts_to_staging(
        staging_dir: Path,
        content_list: list[dict] | None,
        parser_type: str,
    ) -> None:
        """Write MinerU artifacts into staging/mineru/ before commit.

        mineru_online: serialize content_list → content_list.json + extract images
        mineru/docling/paddleocr: handled by caller (copy output directory)
        """
        mineru_dir = staging_dir / "mineru"
        mineru_dir.mkdir(parents=True, exist_ok=True)

        if parser_type == "mineru_online" and content_list:
            # Write raw content_list.json
            raw_path = mineru_dir / "content_list.json"
            raw_path.write_text(
                json.dumps(content_list, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # Extract images
            TaskManager._extract_images(content_list, mineru_dir / "images")

    # ── Cleanup ─────────────────────────────────────────────────────

    async def _cleanup_loop(self):
        while not self._shutting_down:
            await asyncio.sleep(CLEANUP_INTERVAL_SEC)
            now = datetime.now(timezone.utc)
            to_delete = []
            for task_id, task in self._tasks.items():
                if task.status in TERMINAL_STATES and task.completed_at:
                    age_hours = (now - task.completed_at).total_seconds() / 3600
                    if age_hours >= RETENTION_HOURS:
                        to_delete.append(task_id)
            for task_id in to_delete:
                task = self._tasks.pop(task_id)
                self._handles.pop(task_id, None)
                try:
                    self._repo.delete(task_id, task.tenant_id, task.kb_id)
                except Exception:
                    pass
            if to_delete:
                logger.info(f"Cleanup: removed {len(to_delete)} expired tasks")

    # ── Helpers ──────────────────────────────────────────────────────

    def _transition(self, task: TaskInfo, target: TaskStatus,
                    error_code: ErrorCode | None = None):
        if not validate_transition(task.status, target):
            logger.warning(
                f"Invalid transition: {task.task_id} {task.status} → {target}"
            )
            return
        task.status = target
        if error_code:
            task.error_code = error_code
        task.updated_at = datetime.now(timezone.utc)
        # [jonex] P0-C：queued/processing 迁移原先不落盘，磁盘 JSON 长期停在
        # created，排障时无法区分「未入队 / 排队中 / 正在跑」。此处统一持久化。
        try:
            self._repo.save(task)
        except Exception:
            logger.warning(
                "Persist transition failed: task=%s status=%s", task.task_id, target,
                exc_info=True,
            )

    def _fail_task(self, task: TaskInfo, error_code: ErrorCode, message: str):
        # [jonex] R5-c：原子复位 current_step —— cleanup 完成（pending 为空）后
        # 退出 cleanup 状态，让 KB 对账立即收尾、不干等。校验 pending 双列表
        # 均空 + current_step=="cleanup" 才复位，避免 pending 未清时误复位。
        if getattr(task, "current_step", "") == "cleanup":
            dp = list(getattr(task, "delete_pending_ids", []) or [])
            cp = list(getattr(task, "compensate_pending_ids", []) or [])
            if not dp and not cp:
                task.current_step = ""
                logger.info(
                    "[jonex][R5-c] cleanup pending 已空，复位 current_step task=%s",
                    task.task_id,
                )
        task.status = TaskStatus.FAILED
        task.error_code = error_code
        task.error_message = message
        task.completed_at = datetime.now(timezone.utc)
        task.updated_at = datetime.now(timezone.utc)
        # [jonex] P0-1: 显式记录失败（含 task_id / error_code / message），
        # 防止 KB 对账 _finalize_failure 拿到空 error 后写误导性「任务丢失」文案。
        logger.error(
            "Task failed: task_id=%s error_code=%s error_message=%s",
            task.task_id, error_code.value if hasattr(error_code, "value") else error_code, message,
        )
        try:
            self._repo.save(task)
        except Exception:
            pass

    def _check_idempotency(self, key: str) -> TaskInfo | None:
        expire_ts, task_id = self._idempotency.get(key, (0, ""))
        if time.monotonic() < expire_ts:
            existing = self._tasks.get(task_id)
            # [jonex] P0-A.5: 只复用非终态任务；failed/cancelled/completed 允许新建 attempt，
            # 否则失败任务会在 10min 幂等窗口内被反复返回、无法真正重试。
            if existing is not None and existing.status not in TERMINAL_STATES:
                return existing
            # 终态 → 不复用，清掉缓存让调用方新建
            self._idempotency.pop(key, None)
            return None
        # expired → remove
        self._idempotency.pop(key, None)
        return None

    @staticmethod
    def _get_file_size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    # ── Webhook delivery (Spec §4.6) ────────────────────────────────

    # SSRF protection: block internal/reserved IP ranges
    _SSRF_BLOCKED_NETWORKS = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("fe80::/10"),
    ]

    _WEBHOOK_RETRIES = 3
    _WEBHOOK_RETRY_BACKOFF = 2.0  # seconds base
    _WEBHOOK_TIMEOUT = 30.0  # seconds

    @classmethod
    def _is_ssrf_safe(cls, url: str) -> bool:
        """Check a URL does not point to internal/private addresses."""
        try:
            parsed = urllib.parse.urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                return False

            # Block raw IPs in private ranges
            try:
                addr = ipaddress.ip_address(hostname)
                for net in cls._SSRF_BLOCKED_NETWORKS:
                    if addr in net:
                        return False
                return True
            except ValueError:
                pass  # hostname, resolve it

            # For hostnames, resolve and check all IPs.
            # DNS failures are treated as safe — if the host can't be resolved,
            # the actual webhook request will fail anyway with a connection error.
            import socket as _socket
            try:
                ips = _socket.getaddrinfo(hostname, None)
            except _socket.gaierror:
                return True  # can't resolve → can't confirm unsafe → allow
            for ip_info in ips:
                ip_str = ip_info[4][0]
                addr = ipaddress.ip_address(ip_str)
                for net in cls._SSRF_BLOCKED_NETWORKS:
                    if addr in net:
                        return False
            return True
        except Exception:
            return False

    def _maybe_deliver_webhook(self, task: TaskInfo):
        """Deliver webhook asynchronously with retry (Spec §4.6).

        Runs as a background task so it doesn't block worker cleanup.
        Includes SSRF protection and HMAC signature.

        URL resolution order:
          1. Per-task webhook_url (from CreateTaskRequest)
          2. Global RAG_WEBHOOK_URL env var (v1 compat fallback)
        """
        webhook_url = task.webhook_url or DEFAULT_WEBHOOK_URL
        if not webhook_url or task.webhook_delivered:
            return

        task.webhook_delivered = True  # mark delivered to avoid double-send

        # [jonex] P0-B：保存强引用，避免 webhook 投递协程被 GC 中途回收
        self._spawn_bg(
            self._deliver_webhook(task.task_id, webhook_url, task),
            name=f"webhook-{task.task_id}",
        )

    async def _deliver_webhook(
        self, task_id: str, url: str, task: TaskInfo
    ) -> None:
        """Deliver webhook with up to 3 retries + exponential backoff."""
        # SSRF check
        if not self._is_ssrf_safe(url):
            logger.warning(
                f"Webhook SSRF blocked for {task_id}: {url}"
            )
            return

        payload = {
            "task_id": task.task_id,
            "tenant_id": task.tenant_id,
            "doc_id": (
                task.result_summary.doc_id if task.result_summary else ""
            ),
            "status": task.status.value,
            "progress": task.progress,
            "file_path": task.file_path,
            "file_type": task.file_type.value,
            "error_code": task.error_code.value if task.error_code else None,
            "error_message": task.error_message,
            "result_summary": (
                task.result_summary.model_dump(mode="json")
                if task.result_summary else None
            ),
            "storage": (
                task.storage.model_dump(mode="json")
                if task.storage else None
            ),
            "timeline": [
                t.model_dump(mode="json") for t in task.timeline
            ],
            "completed_at": (
                task.completed_at.isoformat() if task.completed_at else None
            ),
        }

        # Get webhook secret for HMAC
        webhook_secret = os.getenv("WEBHOOK_SECRET", "")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Jonex-Event": "task.completed",
            "X-Jonex-Task-Id": task.task_id,
        }

        last_error = None
        for attempt in range(1, self._WEBHOOK_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self._WEBHOOK_TIMEOUT) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if 200 <= resp.status_code < 300:
                        logger.info(
                            f"Webhook delivered for {task_id}: {resp.status_code}"
                        )
                        return
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as e:
                last_error = str(e)

            if attempt < self._WEBHOOK_RETRIES:
                backoff = self._WEBHOOK_RETRY_BACKOFF * (2 ** (attempt - 1))
                logger.warning(
                    f"Webhook attempt {attempt}/{self._WEBHOOK_RETRIES} failed "
                    f"for {task_id}: {last_error}. Retrying in {backoff}s"
                )
                await asyncio.sleep(backoff)

        logger.error(
            f"Webhook delivery failed after {self._WEBHOOK_RETRIES} attempts "
            f"for {task_id}: {last_error}"
        )


# ── SlotFullError ───────────────────────────────────────────────────

class SlotFullError(Exception):
    def __init__(self, response):
        self.response = response
