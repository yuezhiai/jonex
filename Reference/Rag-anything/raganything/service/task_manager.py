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
import gzip
import html
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
    reset_vlm_degraded,
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
# [jonex] 视频后端决策与 full 链（pipeline/stages.py::MultimodalStage）**共用**
# 同一实现，防两条链再次漂移（2026-10-08 事故根因：本体库正常、jonex_wiki 假占位）。
from raganything.video_processor import route_video

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

WORKER_COUNT = int(os.getenv("WORKER_COUNT", "4"))
QUEUE_CAPACITY = int(os.getenv("TASK_QUEUE_CAPACITY", "100"))
RETENTION_HOURS = 24
CLEANUP_INTERVAL_SEC = 30 * 60  # 30 min
IDEMPOTENCY_TTL_SEC = 10 * 60   # 10 min
DEFAULT_WEBHOOK_URL = os.getenv("RAG_WEBHOOK_URL", "")  # global fallback (v1 compat)


def build_mps_video_url(
    preset_name: str, storage_backend: str, storage_key: str,
    cos_bucket: str, cos_region: str = "ap-guangzhou",
) -> str:
    """[jonex] KB 解析器为 video_mps 且对象存储为 cos 时，构造 MPS 输入的 COS URL。

    条件不满足或 bucket 缺失返回空串（调用方忽略 → 视频走本地 VLM 管线）。
    MPS 的 CosInputInfo 实际使用 mps_cos_bucket（回落 COS_BUCKET），此处 URL
    仅作 key 载体与路由信号（_select_backend 按存在性路由）。
    """
    if preset_name != "video_mps":
        return ""
    if (storage_backend or "").strip().lower() != "cos" or not storage_key:
        return ""
    bucket = (cos_bucket or "").strip()
    if not bucket:
        return ""
    region = (cos_region or "ap-guangzhou").strip()
    return f"https://{bucket}.cos.{region}.myqcloud.com/{storage_key}"

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
    # [jonex] P2a：VLM 覆盖相关失败归类清楚（前端据此提示「模型不可用」）
    try:
        from raganything.service.vlm_guard import (
            VlmOverridePrecheckError,
            VlmStrictModeError,
        )

        if isinstance(exc, VlmStrictModeError):
            return ErrorCode.VLM_STRICT_MODE
        if isinstance(exc, VlmOverridePrecheckError):
            return ErrorCode.MODEL_NOT_FOUND
    except ImportError:  # pragma: no cover
        pass
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


def _count_multimodal_items(content_list) -> int:
    """[jonex] 数 content_list 里的**图片**项 —— KB 级 VLM 覆盖护栏的分母。

    只数图片的原因：P2a 的 VLM 覆盖只管图片理解。视频走 MPS/mineru 视频后端、
    音频走 ASR，模型不在这轮范围内；混进来会把比例算歪（一个视频 = 一个 item，
    内部却是几十帧 VLM 调用）。没有图片时返回 0，护栏天然空判。
    """
    total = 0
    for item in content_list or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") == "image":
            total += 1
    return total


def _transcript_cache_key(item_type: str, vlm_override: str) -> str:
    """[jonex] 转写缓存键：**图片按 VLM 模型分桶**，video/audio 不分组。

    图片的 description 是 VLM 的产物 —— 换了 KB 级模型再解析，如果还命中同一个键，
    用户改完模型得到的仍是旧模型的描述，而「改模型」这件事本身就等于「换解析结果」。
    所以有覆盖时图片键是 ``image@<model>``；没有覆盖时仍是 ``image``（键不变 =
    升级前后零成本，也不影响既有缓存）。

    video/audio 的正文来自 MPS/ASR，与 VLM 模型无关，**故意不分组**：分组只会让
    每次改 VLM 都白付一次分钟级转写费。
    """
    tag = (vlm_override or "").strip()
    if item_type == "image" and tag:
        return f"image@{tag}"
    return item_type


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


# ── [jonex] P0-b：表格上下文跨任务回放 ──────────────────────────────
#
# 问题：Row-as-Object 表格对象只从 content_list 的 table item 推导
# （_merge_table_objects 的唯一输入），而 content_list 只存在于 full 入库
# 链路。ontology_only 重抽按设计不本地化原文件、不解析（P0-A），
# ctx.content_list 恒为空 → _merge_table_objects 不跑 → 重抽结果**不含
# 表格对象**；而写图是「delete_by_document 全删 + merge」的全量替换
# （ontology_graph_repository.delete_by_document），首轮由表格对象建立的
# 节点会被整体换掉 = 静默丢数据。
#
# 为什么不让重抽「自己重新解析一份」：解析缓存键含 str(file_path
# .absolute()) 与 st_mtime（cache.ParseCacheManager.generate_cache_key），
# 而 _fetch_cos_object 每次下载都落在带随机 uuid 前缀的新路径上 → key 必
# 不同 → 必然 miss → 每次都要重解一遍原文件。代价与失败面都远大于
# 「首轮把表格上下文存一份」。
#
# 故：full 链路在进入本体阶段时把 content_list 的表格子集落盘（持久卷，
# 跨容器重启/重建存活），ontology_only 重抽读回放进 ctx.content_list——
# 让 table_handled / _merge_table_objects / P10 批 2.4 零产出兜底三条既有
# 逻辑**原样生效**，判据一个都不用改，重抽口径自然与首轮一致。
#
# 存档有三种状态，必须区分（否则「本来就没表格」会被误报成「存档丢失」）：
#   文件不存在      → 无存档（上线前入库的老文档）。**不降级**：由本体阶段
#                    按 chunk 判据决定——本文档确有表格 chunk 就拒绝重抽
#                    （见 _run_ontology_extraction 内的 P0-b 闸门）
#   文件存在、空列表 → 首轮确认无表格 → 重抽同样按无表格处理（正常跑）
#   文件存在、非空   → 回放，口径与首轮一致
#
# 为什么无存档时「拒绝重抽」而不是「降级跑」：降级把表格当普通文本抽，
# 产出一批口径不同的通用实体（无结构化 attributes、无复合主键区分），再经
# delete_by_document 全量替换掉首轮的表格对象——不是「补上缺失的」，是
# 「用差的换掉好的」；代价还是 24212 chunk 全量进 LLM（3027 批 × gleaning
# ≈45 分钟）+ backfill O(实体×chunk) 分钟级 CPU。拒绝则一行不动、首轮结果
# 原样保留，KB 侧最多空转 MAX_ONTOLOGY_RETRIES 次（秒级）后落 FAILED，并在
# error 里指明需 force_reparse 重建存档。
#
# 存档用 gzip（后缀 .json.gz）：表体是高重复结构化文本，实测 XBRL 财报
# 1015 张表 / 29431 行的表体原文 23.72MB → 压缩后 0.57MB（41.5x），磁盘占用
# 降到 1/40。
#
# 上限 _TABLE_CTX_MAX_BYTES_DEFAULT **按解压后（原始 JSON）字节**判定，不是
# 压缩后——真约束不是磁盘而是读回时的内存：_load_table_context 一次性
# gzip.decompress + json.loads，再经 _merge_table_objects 展开成 Python 对象
# （通常为 JSON 文本的 3~5 倍）。压缩比是文档属性、能差两个数量级（同一份
# 8MiB 压缩后存档，低压缩比文档解压后可能只有 24MB，高重复文档可达 348MB），
# 故「压缩后」无法约束内存。64MiB 解压后 ≈ 200~320MB Python 对象，考虑
# WORKER_COUNT=4 的并发读回量级可控；gzip 只负责省磁盘。
#
# 已知局限：
#   ① 存档不随文档删除即时回收——靠 _sweep_table_context 的 TTL 兜底
#      （RAG_TABLE_CTX_TTL_SEC，默认 90 天），不做删除链路钩子。
#   ② 存档是首轮 parse 的产物，而 chunk 可被 update_chunk 端点改写；改写后
#      回放的表格上下文与 chunk 实际内容可能漂移（概率低：改写表格 chunk
#      的场景罕见）。真发生则 Row-as-Object 产出的对象会引用已变更的
#      chunk。要根治需在 update_chunk 时失效存档，未做。
_TABLE_CTX_MAX_BYTES_DEFAULT = 64 * 1024 * 1024
_TABLE_CTX_TTL_SEC_DEFAULT = 90 * 24 * 3600
# 全树遍历随文档数增长，故按此间隔节流（模块级时间戳，进程内有效）
_TABLE_CTX_SWEEP_INTERVAL_SEC = 3600
_last_table_ctx_sweep = 0.0


def _table_context_dir() -> str:
    """表格上下文存档目录。

    与 _cos_cache_dir 同口径（KB_INPUT_DIR / WORKING_DIR 下的子目录），但
    独立成 ``_table_ctx``——``_cos_cache`` 有 TTL 清扫（_sweep_cos_cache，
    默认 24h），存档混进去会被清掉。
    """
    explicit = (os.getenv("RAG_TABLE_CTX_DIR") or "").strip()
    if explicit:
        return explicit
    base = (
        os.getenv("KB_INPUT_DIR")
        or os.getenv("WORKING_DIR")
        or "/app/rag_storage"
    )
    return os.path.join(base, "_table_ctx")


def _table_context_path(task) -> str:
    """存档路径 ``{dir}/{tenant}/{kb}/{document_id}.json.gz``（逐段净化防穿越）。"""

    def _seg(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z_.\-]", "_", str(value or ""))[:128] or "_"

    return os.path.join(
        _table_context_dir(),
        _seg(getattr(task, "tenant_id", "")),
        _seg(getattr(task, "kb_id", "")),
        f"{_seg(getattr(task, 'document_id', ''))}.json.gz",
    )


def _table_context_items(content_list) -> list:
    """抽出 content_list 里的表格子集，规整成扁平条目列表。

    两类来源（与 _merge_table_objects 的消费口径一一对应）：
      ① ``type == "table"`` 的 item（MinerU/表格类 parser）；
      ② text item 上 push 阶段登记的 ``_mapped_tables``（md/内嵌表格路径，
         见 pipeline/stages.py §table-llm-bypass）。

    一律规整为 ``{type, table_body, table_caption, table_idx}``：表体统一
    落 ``table_body`` 键（get_table_body 的首选别名，故读回后取值口径不
    变），且**不保留 _mapped_tables 嵌套**——否则读回后同一个表会被
    _merge_table_objects 当作顶层 item 和内层条目各消费一次。
    """
    from raganything.utils import get_table_body

    def _norm(item: dict) -> dict:
        return {
            "type": "table",
            "table_body": get_table_body(item),
            "table_caption": item.get("table_caption"),
            "table_idx": item.get("table_idx"),
        }

    items: list = []
    for item in content_list or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "table":
            items.append(_norm(item))
        for mapped in (item.get("_mapped_tables") or []):
            if isinstance(mapped, dict):
                items.append(_norm(mapped))
    return items


# ── [jonex] A18：引擎表格片 → 存档条目聚合（spec §4.4）──────────────────
#
# 与引擎 `table_packer.py:19-22` 的渲染常量**同源**；引擎侧改这两个常量时
# 这里必须同步（跨仓契约，见 docs/codes-modified-a18-consume-integration.md）。
_A18_CELL_LEAD = "| "
_A18_CELL_TAIL = " |"
_A18_CELL_SEP = " | "
_A18_SELF_DESC_INFIX = ": "
_TABLE_CAPTION_PREFIX = "表格明细：【表】"
_CONTINUATION_SUFFIX_RE = re.compile(r"（续：[^）]*）\s*$")


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_anchor_kv(file_source: str) -> dict:
    """引擎 file_source 锚串 → {键: 值}（`|` 分段、`=` 拆分）。

    与 `jonex_core.common.file_source_util.parse_file_source` 同构但**不复用**：
    那个实现读掉 `tenant=` 却不回传 tenant 键，而读回校验（T6 前置段）要用它。
    """
    out: dict = {}
    for seg in (file_source or "").split("|"):
        if "=" not in seg:
            continue
        key, _, value = seg.partition("=")
        out[key.strip()] = value
    return out


def _split_cells(line: str, *, strip_cells: bool = True) -> Optional[list]:
    """markdown 表行（`| a | b |`）→ 格列表；非 markdown 行 → None。

    ``strip_cells=False`` 返回**未 strip** 的原始切片——仅供
    `_merge_row_fragments` 拼接用：硬切是纯字符切，缝上的空白属格内容，
    strip 会静默吞掉（I2，缝两侧若是数字 token 会改语义）。表头格等
    既有调用点保持默认 strip 语义。
    """
    if not line.startswith(_A18_CELL_LEAD):
        return None
    body = line[len(_A18_CELL_LEAD):]
    if body.endswith(_A18_CELL_TAIL):
        body = body[: -len(_A18_CELL_TAIL)]
    cells = body.split(_A18_CELL_SEP)
    return [c.strip() for c in cells] if strip_cells else cells


def _strip_selfdesc_cells(line: str) -> list:
    """自描述行（`列名: 值 | 列名: 值`，无前导竖线）→ 逐格剥 `列名: ` 前缀。

    宽松剥：只剥**首个** `: ` 之前的部分；格内无 `: ` 时原样保留（防误剥）。
    """
    out = []
    for part in line.split(_A18_CELL_SEP):
        _, sep, value = part.partition(_A18_SELF_DESC_INFIX)
        out.append((value if sep else part).strip())
    return out


def _merge_row_fragments(frags: list, n_cols: int, sig: str) -> str:
    """一行的碎片 → 单行 markdown（spec N1：同格互斥切片按片序拼接）。

    frags: [(cell_start, cell_end, chunk_index, line_text)]。
    - `cell_start == cursor` ⇒ 连续整格组（软切）⇒ 追加格值；
    - `cell_start == cell_end - 1 == 已收格数 - 1` ⇒ 硬切续片（同一
      `[i, i+1)` 区间）⇒ 与最后一格已有文本拼接；
    - 其余（缺口/重叠）⇒ warning 后确定性降级追加（宁留冗余不丢字）。

    格值一律取**未 strip** 原始切片（I2）：硬切缝上的空白是格内容，
    strip 会把它吞掉、拼出与原文不同的文本。
    """
    cells: list = []
    cursor = 0
    for cell_start, cell_end, chunk_index, line in sorted(
        frags, key=lambda t: (t[0], t[2])
    ):
        values = _split_cells(line, strip_cells=False)
        if values is None:
            values = _strip_selfdesc_cells(line)
        last = len(cells) - 1
        if cell_start == cursor:
            cells.extend(values)
            cursor = cell_end
        elif cell_start == cell_end - 1 and cell_start == last:
            cells[last] = cells[last] + (values[0] if values else "")
        else:
            logger.warning(
                "[jonex][A18] 表格分组 %s 碎片不连续（期望 cell_start=%d，实到 %d，"
                "chunk=%d），确定性降级拼接",
                sig, cursor, cell_start, chunk_index,
            )
            cells.extend(values)
            cursor = cell_end
    if n_cols and len(cells) != n_cols:
        logger.warning(
            "[jonex][A18] 表格分组 %s 行格数不齐（%d != %d；已知有损场景：格值含 "
            "` | `），按现有文本输出",
            sig, len(cells), n_cols,
        )
    return _A18_CELL_LEAD + _A18_CELL_SEP.join(cells) + _A18_CELL_TAIL


def _assemble_table_item(sig: str, pieces: list) -> dict:
    """一组表格片 → 一条存档条目 {type, table_body, table_caption, table_idx}。

    表头锚点法（spec §4.4）：片内**首个 `| ` 行**即表头行（引擎每片恒重发表头），
    它上方是 caption（可多行；续段标记在首个 caption 行行尾，逐行剥除）。
    完整片（`cell_start is None`）数据行原样采用；碎片按 (row_start, row_end)
    键控、`_merge_row_fragments` 拼回。
    """
    def _key(item):
        _, kv = item
        return (_as_int(kv.get("row_start")), _as_int(kv.get("cell_start")),
                _as_int(kv.get("chunk")))

    pieces = sorted(pieces, key=_key)
    caption = ""
    header_cells: Optional[list] = None
    rows: dict = {}
    order: list = []

    for chunk, kv in pieces:
        lines = (chunk.get("content") or "").split("\n")
        header_idx = next(
            (i for i, ln in enumerate(lines) if ln.startswith(_A18_CELL_LEAD)), None
        )
        if header_idx is None:
            raise ValueError(
                f"[jonex][A18] 表格分组 {sig} 内 chunk={kv.get('chunk')} 无表头锚点行"
                "（引擎表格片恒含 `| ` 表头行）——产出形态漂移，拒绝静默聚合"
            )
        if header_cells is None:
            header_cells = _split_cells(lines[header_idx]) or []
        if not caption:
            cap_lines = []
            for ln in lines[:header_idx]:
                ln = _CONTINUATION_SUFFIX_RE.sub("", ln)
                if ln.startswith(_TABLE_CAPTION_PREFIX):
                    ln = ln[len(_TABLE_CAPTION_PREFIX):]
                cap_lines.append(ln)
            caption = "\n".join(ln for ln in cap_lines if ln.strip())

        key = (_as_int(kv.get("row_start")), _as_int(kv.get("row_end")))
        if key not in rows:
            rows[key] = {"full": None, "frags": []}
            order.append(key)
        slot = rows[key]
        data_lines = lines[header_idx + 1:]
        if kv.get("cell_start") is None:
            if slot["full"] is None:
                slot["full"] = data_lines
            else:
                logger.warning(
                    "[jonex][A18] 表格分组 %s 同键 %s 出现多个完整片，取序首", sig, key,
                )
        else:
            slot["frags"].append((
                _as_int(kv.get("cell_start")),
                _as_int(kv.get("cell_end")),
                _as_int(kv.get("chunk")),
                data_lines[0] if data_lines else "",
            ))

    n_cols = len(header_cells or [])
    out_rows: list = []
    for key in order:
        slot = rows[key]
        if slot["full"] is not None:
            if slot["frags"]:
                # 行为保持「完整片优先、碎片丢弃」，但绝不允许静默：引擎 240B
                # 降级链第 2 级会整段丢 cell 锚，降级后的碎片在消费侧看起来
                # 就是「完整片」——此共存态可达（I1），静默丢弃等于丢全量文本。
                logger.warning(
                    "[jonex][A18] 表格分组 %s 同键 %s 完整片与碎片共存，"
                    "碎片 %d 片被丢弃（引擎降级链可能把 cell 锚丢成"
                    "『完整片』，请查 file_source_degraded）",
                    sig, key, len(slot["frags"]),
                )
            for ln in slot["full"]:
                if ln.startswith(_A18_CELL_LEAD):
                    out_rows.append(ln)
                elif ln.strip():
                    out_rows.append(
                        _A18_CELL_LEAD + _A18_CELL_SEP.join(_strip_selfdesc_cells(ln))
                        + _A18_CELL_TAIL
                    )
            continue
        out_rows.append(_merge_row_fragments(slot["frags"], n_cols, sig))

    header_line = _A18_CELL_LEAD + _A18_CELL_SEP.join(header_cells or []) + _A18_CELL_TAIL
    return {
        "type": "table",
        "table_body": "\n".join([header_line, *out_rows]),
        "table_caption": caption,
        "table_idx": _as_int(pieces[0][1].get("table_idx")),
    }


def _table_context_items_from_chunks(chunks: list) -> list:
    """[jonex] A18：引擎全量 chunk → `_table_ctx` 存档条目（聚合，spec §4.4）。

    只消费 `ctype == "table_row"` 锚的片；无表格片 ⇒ 返回 []（合法值：
    「确认无表格」，前置段恒写档会把空列表也写下去）。
    """
    grouped: dict = {}
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        kv = _parse_anchor_kv(chunk.get("file_source") or "")
        if kv.get("ctype") != "table_row":
            continue
        sig = kv.get("table_sig")
        if not sig:
            raise ValueError(
                f"[jonex][A18] 表格 chunk（chunk_index={kv.get('chunk')}）缺 table_sig 锚"
                "——引擎产出形态漂移，拒绝静默聚合"
            )
        grouped.setdefault(sig, []).append((chunk, kv))
    items = [_assemble_table_item(sig, pieces) for sig, pieces in grouped.items()]
    items.sort(key=lambda item: item["table_idx"])
    return items


# [jonex] A18：与 LightRAG `sanitize_text_for_encoding`（lightrag/utils.py 的
# `sanitize_text_for_encoding`，其 regex 在两处模块常量）**同源复刻**——灌库
# 去重门②（sanitized 内容 hash）的键形态。T1 实测（2026-10-10）：读回 =
# `html.unescape(raw).strip()` 形态；实测管线 strip → unescape → 去 surrogate
# → 去控制符（\t\n\r 保留）→ strip。LightRAG 侧改口径时这里必须同步。
_DEDUP_SURROGATE_PATTERN = re.compile(r"[\uD800-\uDFFF\uFFFE\uFFFF]")
_DEDUP_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def _sanitize_for_dedupe(text: str, replacement_char: str = "") -> str:
    """LightRAG sanitize_text_for_encoding 的同源复刻（读回比对用）。"""
    if not isinstance(text, str):
        return text
    text = text.strip()
    if not text:
        return text
    text = html.unescape(text)
    text = _DEDUP_SURROGATE_PATTERN.sub(replacement_char, text)
    text = _DEDUP_CONTROL_CHAR_PATTERN.sub(replacement_char, text)
    return text.strip()


# [jonex] A18 读回通道事实（真机实测 172.30.2.57，2026-10-10）：
# · 列表端点 `GET /documents/chunks?doc_id=` 读回时剥除 ns marker 与环绕空白
#   （`Reference/LightRAG/lightrag/api/routers/document_routes.py:3923-3928`
#   的 `\s*<!--yx:[0-9a-f]+-->\s*` 替换）⇒ 本地比对一律用注入前内容。
# · 超限片（>引擎 1200 token）被按窗口 1200 / 步长 1100 token 重切为多条记录，
#   相邻分片重叠约 130 字符；分片 = 全片 sanitize 后连续子串（探针 13/13 逐字
#   复现，`.local-verify/a18supp/`）。
_FRAGMENT_TAIL_RE = re.compile(r"<!--yx:[0-9a-f]{0,8}-{0,2}$")
_FRAGMENT_HEAD_RE = re.compile(r"^(?:yx:)?[0-9a-f]{1,8}-{0,2}-->")


def _fragment_piece_form(raw: str) -> str:
    """重切分片的规范化形态：剥边缘 U+FFFD（多 token 字符压窗口边缘的半截
    token decode 产物，探针实测）与边缘半截 ns marker（窗口切在 marker 内部；
    机制推论未及实测，残余更碎形态方向=loud，记入 T7 文档）。"""
    piece = _sanitize_for_dedupe(raw)
    piece = piece.strip("�")
    piece = _FRAGMENT_HEAD_RE.sub("", piece)
    piece = _FRAGMENT_TAIL_RE.sub("", piece)
    return piece.strip("�").strip()


def _fragments_match_content(
    content_key: str, anchors: set, record_contents: dict,
) -> bool:
    """duplicated 条（如超限片重跑被内容门拒）的分片形态容忍（Important-1）。

    超限片首推时被重切（同锚多条分片）、重跑被拒后「全片」不再逐条在场，
    故补分片判据：该条任一锚存在非空读回记录，逐条（归一后）均为内容
    sanitize 子串，且合计长度覆盖内容长度（防「分片缺失被当全量」掩蔽）。
    不得放宽为「锚在场即过」——同锚异内容（I6）的响亮失败必须保留。"""
    for anchor in anchors:
        pieces = [
            _fragment_piece_form(c) for c in (record_contents.get(anchor) or [])
        ]
        if not pieces:
            continue
        if all(piece in content_key for piece in pieces) and (
            sum(len(piece) for piece in pieces) >= len(content_key)
        ):
            return True
    return False


def _sweep_table_context() -> None:
    """按 TTL 回收存档（兜底磁盘泄漏：文档删除链路没有钩子）。

    仿 _sweep_cos_cache，但存档是 {tenant}/{kb}/{doc} 三层树、文件数随文档
    数增长，故用模块级时间戳节流（进程内有效），避免每次落盘都全树遍历。
    TTL 默认 90 天，远长于 _cos_cache 的 24h——存档是重抽的唯一凭据，删早了
    该文档就只能 force_reparse 重建。
    """
    global _last_table_ctx_sweep
    now = time.time()
    if now - _last_table_ctx_sweep < _TABLE_CTX_SWEEP_INTERVAL_SEC:
        return
    _last_table_ctx_sweep = now
    ttl = int(os.getenv("RAG_TABLE_CTX_TTL_SEC") or _TABLE_CTX_TTL_SEC_DEFAULT)
    if ttl <= 0:  # 显式关停清扫
        return
    removed = 0
    try:
        for root, _dirs, files in os.walk(_table_context_dir()):
            for name in files:
                if not name.endswith(".json.gz"):
                    continue
                fp = os.path.join(root, name)
                try:
                    if now - os.path.getmtime(fp) > ttl:
                        os.remove(fp)
                        removed += 1
                except OSError:  # 被并发删掉/权限问题：跳过
                    continue
    except OSError:
        return
    if removed:
        logger.info(
            "[jonex][P0-b] 表格上下文 TTL 回收 %d 个存档（TTL=%ds）", removed, ttl,
        )


def _write_table_context(task, items: list) -> Optional[int]:
    """表格上下文存档底层写口（[jonex] A18 从 `_persist_table_context` 提炼，共用）。

    **无条件可写**：空列表也是合法值（`_load_table_context` 的「首轮确认无表格」）
    ——「空则跳过」是三个上层调用点的语义（`write_when_empty`），不上移到本层；
    A18 前置段（`_ingest_engine_chunks`）必须能写空档（恒写档）。

    best-effort 语义保持：超限（RAG_TABLE_CTX_MAX_BYTES，按解压后原始 JSON
    字节判定）或任何异常 ⇒ warning + None；成功 ⇒ 返回写入条数。
    """
    try:
        _sweep_table_context()
        raw = json.dumps(items, ensure_ascii=False, default=str).encode("utf-8")
        payload = gzip.compress(raw, 6)
        max_bytes = int(
            os.getenv("RAG_TABLE_CTX_MAX_BYTES") or _TABLE_CTX_MAX_BYTES_DEFAULT
        )
        path = _table_context_path(task)
        if len(raw) > max_bytes:
            logger.warning(
                "[jonex][P0-b] 表格上下文超限不落盘（解压后 %d > %d 字节；"
                "压缩后 %d 字节）: doc=%s ——该文档日后重抽将被 P0-b 闸门拒绝",
                len(raw), max_bytes, len(payload), getattr(task, "document_id", ""),
            )
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 先写临时文件再 replace：避免重抽读到写了一半的 gzip
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, path)
        logger.info(
            "[jonex][P0-b] 表格上下文已落盘 %d 条 %d→%d 字节（%.1fx）: doc=%s → %s",
            len(items), len(raw), len(payload),
            len(raw) / max(1, len(payload)),
            getattr(task, "document_id", ""), path,
        )
        return len(items)
    except Exception as exc:  # noqa: BLE001 — 存档失败不阻断本体链路
        # sweep 与写入同在此 try 内（结构不动）：此文案须同时覆盖「清扫抛错」
        # （如 RAG_TABLE_CTX_TTL_SEC 非法）与「写入失败」，否则归因误导。
        logger.warning(
            "[jonex][P0-b] 表格上下文落盘/清理失败（不影响本次本体抽取）: doc=%s: %s",
            getattr(task, "document_id", ""), exc,
        )
        return None


def _persist_table_context(
    task, content_list, write_when_empty: bool = True,
) -> Optional[int]:
    """把表格子集 gzip 落盘（按 document_id 幂等覆盖），供日后重抽回放。

    [jonex] A18：写入部分已提炼到 `_write_table_context`（前置段共用，需能
    无条件写空档）；本函数保留既有上层语义（含 write_when_empty 闸门与
    「为什么」的解释，见 _write_table_context 与模块注释）。

    Args:
        write_when_empty: 抽到 0 张表时是否仍写一份**空存档**。默认 True，
            即「0 表示首轮确认无表格」的语义（full 链路用，它一定跑到底，
            「无表格」是权威结论，必须与「无存档」区分开）。
            任务中心三步链路的**第一步**置 False —— 那一步的 content_list 里
            只有 ``type=="table"`` 的 item，md / 内嵌表格的表体要等 push 阶段
            登记成 ``_mapped_tables``（见 _table_context_items），所以第一步
            抽到 0 张表**不代表文档没有表格**。此时落空存档会把「未知」伪装成
            「确认无表格」：第三步的 P0-b 闸门是 ``ctx.content_list is None``
            才拒绝，空列表会被静默放过 → 表格知识凭空消失且不报错。
            宁可**不写**（第三步读回 None → 按 chunk 证据拒绝，会响）。

    Returns:
        int: 写入的表格条目数。0 表示首轮确认无表格——仍写空存档，用于
             把「无表格」与「无存档」区分开（write_when_empty=False 时不会
             返回 0）。
        None: 未写。content_list 为空（非 full 链路/解析无产出）、抽到 0 张表
              且 write_when_empty=False、超限或写入失败。best-effort，仅告警，
              绝不阻断本体链路。
    """
    if not content_list:
        return None
    try:
        items = _table_context_items(content_list)
    except Exception as exc:  # noqa: BLE001 — 与既有 best-effort 语义同源
        logger.warning(
            "[jonex][P0-b] 表格上下文抽取失败（不影响本次本体抽取）: doc=%s: %s",
            getattr(task, "document_id", ""), exc,
        )
        return None
    if not items and not write_when_empty:
        # 刻意不写：在「表格还没登记完」的阶段落空存档，会把「未知」写成
        # 「确认无表格」（详见原 docstring / P0-b spec）。
        logger.debug(
            "[jonex][P0-b] 表格上下文抽到 0 条且 write_when_empty=False，"
            "跳过落盘（保留磁盘上原有的存档，若有）: doc=%s",
            getattr(task, "document_id", ""),
        )
        return None
    return _write_table_context(task, items)


def _load_table_context(task) -> Optional[list]:
    """读回表格子集存档（gzip，兼容明文 JSON 便于运维手工排查）。

    Returns:
        list: 存档中的表格条目；**空列表是有效值**，代表首轮确认无表格。
        None: 无存档或存档损坏——调用方据此交本体阶段的 P0-b 闸门裁决。
    """
    try:
        path = _table_context_path(task)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            blob = fh.read()
        # gzip 魔数 1f 8b 才解压；否则按明文 JSON 读（手工塞存档排查用，
        # 不是历史格式兼容——本功能上线前没有存档文件）
        if blob[:2] == b"\x1f\x8b":
            blob = gzip.decompress(blob)
        data = json.loads(blob.decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"存档格式非列表: {type(data).__name__}")
        return [it for it in data if isinstance(it, dict)]
    except Exception as exc:  # noqa: BLE001 — 读档失败按「无存档」处理
        logger.warning(
            "[jonex][P0-b] 表格上下文存档读取失败（按无存档处理）: doc=%s: %s",
            getattr(task, "document_id", ""), exc,
        )
        return None


# ══════════════════════════════════════════════════════════════════════
# [jonex] 任务中心三步拆分：解析产物落盘（prepare_only → push_only）
# ══════════════════════════════════════════════════════════════════════
#
# 为什么需要：P1/P2（解析 + 多模态）与 P3（推切片）原先在同一次 full 作业里
# 连续跑完，中间产物只活在进程内存（ctx.content_list / ctx.multimodal_results）。
# 任务中心把三步拆成三条独立子任务后，第二步必须能拿回第一步的产物。
#
# 为什么**不**落 content.md 再反推：_write_openkb_artifact 把 page_idx / bbox /
# text_level / 行字符偏移 / table_idx / _mapped_tables 全丢了，type=="code"
# 无分支直接丢，表格被 "\t".join 压平——锚点层、结构层、分词层都不可复原。
#
# 为什么**连 multimodal_results 一起落**（而不是第二步从 content_list 重建）：
# multimodal_results 的 index 是 multimodal_items 里的枚举序号，而
# multimodal_items 由 separate_content 派生、含 table/equation 在内的**全部**
# 非文本类型；_collect_multimodal_chunks 对每一类都产出一条摘要 chunk（表格
# 那条带「表格概览：」前缀 + ctype=table_summary，供检索侧双路召回）。重建
# 一旦漏掉任何一类，对应摘要 chunk 就凭空消失。原样落盘 = 与 full 链路逐条
# 等价，且逐帧 VLM（_video_keyframes）与逐段 ASR（_audio_segments）天然带上
# —— 这两个结构只存在于 original 副本上，不落盘就永久丢失。
#
# 目录：{inputs}/parsed/{document_id}/ —— jonex-rag-inputs 是 atomic-rag /
# knowledge-base / openkb 同时挂载的唯一共享卷，跨任务、跨容器都能读到。
# 与 _write_openkb_artifact 共用该目录（content.md + assets/），文件名不冲突。
#
# 上限按**解压后**字节判定，与 _TABLE_CTX_MAX_BYTES 同因：真约束不是磁盘而是
# 读回时一次性 decompress + json.loads 的内存峰值，而压缩比是文档属性、能差
# 两个数量级，用「压缩后」约束不住内存。
#
# 不做 TTL 清扫：核对决策「删任务时向量库/图库/原文件全保留」——产物是重推
# 切片的唯一凭据，删早了该文档只能 force_reparse 重建。
_INGEST_ARTIFACT_MAX_BYTES_DEFAULT = 256 * 1024 * 1024


def _ingest_artifact_enabled() -> bool:
    """产物落盘总开关（默认开）。关闭则三步拆分链路不可用。"""
    return os.getenv("RAG_INGEST_ARTIFACT_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _ingest_artifact_max_bytes() -> int:
    """解压后字节上限；<=0 表示不限制。"""
    raw = (os.getenv("RAG_INGEST_ARTIFACT_MAX_BYTES") or "").strip()
    if not raw:
        return _INGEST_ARTIFACT_MAX_BYTES_DEFAULT
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "[jonex][ingest-artifact] RAG_INGEST_ARTIFACT_MAX_BYTES 非整数(%r)，回退默认", raw,
        )
        return _INGEST_ARTIFACT_MAX_BYTES_DEFAULT


def _ingest_artifact_dir(task) -> str:
    """解析产物目录 ``{inputs}/parsed/{document_id}``。

    与 _write_openkb_artifact 的 out_dir 同一表达式——两条链路的产物必须落在
    同一处，否则第二步读到的 assets 与 content.md 引用的不是同一份。
    """
    inputs_root = (
        os.getenv("RAG_INPUTS_DIR") or os.getenv("KB_INPUT_DIR") or "/app/inputs"
    )
    return os.path.join(inputs_root, "parsed", str(getattr(task, "document_id", "") or ""))


def _ingest_artifact_path(task, name: str) -> str:
    return os.path.join(_ingest_artifact_dir(task), name)


def _persist_ingest_artifacts(
    task, content_list, multimodal_results, asset_exts
) -> tuple[bool, str]:
    """落盘解析产物，供第二步 push_only 读回。

    落三个文件：
      content_list.json       —— 文本 chunk 的唯一来源（separate_content 的输入）
      multimodal_results.json —— 多模态摘要 chunk 的唯一来源。**不能**从
                                 content_list 重建：_collect_multimodal_chunks 产出的
                                 table_summary / equation / video_frame / audio_segment
                                 都依赖 MultimodalStage 跑出来的 description + entity_info，
                                 这些字段在 content_list 里根本不存在。
      asset_exts.json         —— {content_list 下标: 图片扩展名}。图片资产本身已在
                                 第一步由 AssetUploadStage 传上对象存储，但推切片时
                                 _collect_multimodal_chunks 还要靠它拼 `aext=` 锚点，
                                 丢了检索侧就取不到图片 URL。

    为什么不落 content.md 再反推：_write_openkb_artifact 丢掉了 page_idx / bbox /
    text_level / 行字符偏移 / table_idx / _mapped_tables / img_footnote，
    type=="code" 整块无分支（直接丢），表格被 "\\t".join 压平 —— 锚点层、结构层、
    分词层三层全不可复原。

    为什么不用改写 img_path：切分点放在 office_preview 之后，AssetUploadStage 在
    图片还在本地时就跑完了；push 阶段不碰任何图片文件，所以 JSON 里那份指向
    /app/output（未挂卷的容器内临时目录）的 img_path 过期也无所谓。
    （_extract_image_anchor 靠 page_idx + bbox 定位，本就不读 img_path。）

    Returns:
        (ok, message)：ok=False 时调用方必须 _fail_task —— 产物是第二步的**唯一**
        输入，落不下去就没有「部分成功」可言，静默降级会让文档永远不可检索。
    """
    if not _ingest_artifact_enabled():
        return False, "RAG_INGEST_ARTIFACT_ENABLED 已关闭，无法落盘解析产物（三步拆分链路不可用）"
    document_id = str(getattr(task, "document_id", "") or "")
    if not document_id:
        return False, "缺少 document_id，无法落盘解析产物"
    if not content_list:
        return False, "解析结果为空（content_list 空），无法落盘解析产物"

    out_dir = _ingest_artifact_dir(task)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"解析产物目录创建失败：{exc}"

    max_bytes = _ingest_artifact_max_bytes()
    for name, payload in (
        ("content_list.json", content_list),
        ("multimodal_results.json", multimodal_results or []),
        # JSON 的键只能是字符串；读回时 _load_ingest_artifacts 会强转回 int。
        ("asset_exts.json", {str(k): v for k, v in (asset_exts or {}).items()}),
    ):
        try:
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            # 三个文件各自判定：分开读回，任一超限都说明峰值内存不可控
            if max_bytes > 0 and len(raw) > max_bytes:
                return False, (
                    f"解析产物 {name} 解压后 {len(raw)} 字节，超过上限 {max_bytes}"
                    f"（RAG_INGEST_ARTIFACT_MAX_BYTES）"
                )
            path = _ingest_artifact_path(task, name)
            tmp = f"{path}.tmp"
            # 先写临时文件再 replace：避免重推读到写了一半的 gzip（与
            # _persist_table_context 同因）
            with open(tmp, "wb") as fh:
                fh.write(gzip.compress(raw, 6))
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[jonex][ingest-artifact] 落盘失败 %s: doc=%s: %s", name, document_id, exc,
            )
            return False, f"解析产物 {name} 落盘失败：{exc}"

    logger.info(
        "[jonex][ingest-artifact] 已落盘: doc=%s content_list=%d multimodal_results=%d "
        "asset_exts=%d dir=%s",
        document_id, len(content_list), len(multimodal_results or []),
        len(asset_exts or {}), out_dir,
    )
    return True, ""


def _load_ingest_artifacts(
    task,
) -> tuple[Optional[list], Optional[list], Optional[dict]]:
    """读回 (content_list, multimodal_results, asset_exts)。

    Returns:
        (list, list, dict): content_list 必然非空（落盘时校验过）。
        (None, None, None): 无产物或产物损坏 —— 调用方据此判失败，不可降级
                            （没有 content_list 就一条文本 chunk 都推不出去）。
    """
    def _read(path: str):
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            blob = fh.read()
        # gzip 魔数 1f 8b 才解压；否则按明文 JSON 读（手工排查用）
        if blob[:2] == b"\x1f\x8b":
            blob = gzip.decompress(blob)
        return json.loads(blob.decode("utf-8"))

    try:
        raw_cl = _read(_ingest_artifact_path(task, "content_list.json"))
        content_list = raw_cl if isinstance(raw_cl, list) else None
        if not content_list:
            return None, None, None
        # multimodal_results 允许是空列表（纯文本文档），缺席按空处理
        raw_mm = _read(_ingest_artifact_path(task, "multimodal_results.json"))
        results = raw_mm if isinstance(raw_mm, list) else []
        # asset_exts 的键经 JSON 往返变成了字符串，而推切片侧是按 int 下标查的
        # （multimodal_results 里 image_idx 是 int）。不转回来的话**全部查不到**，
        # 图片锚点整体消失且不报错 —— 静默退化，很难从日志里发现。
        raw_ae = _read(_ingest_artifact_path(task, "asset_exts.json"))
        asset_exts: dict = {}
        if isinstance(raw_ae, dict):
            for k, v in raw_ae.items():
                try:
                    asset_exts[int(k)] = v
                except (TypeError, ValueError):
                    continue
        return content_list, results, asset_exts
    except Exception as exc:  # noqa: BLE001 — 读档失败按「无产物」处理
        logger.warning(
            "[jonex][ingest-artifact] 产物读取失败（按无产物处理）: doc=%s: %s",
            getattr(task, "document_id", ""), exc,
        )
        return None, None, None


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
                # [jonex] P0-2：护栏计数必须**先自增并持久化**再判——task 由
                # _repo.load_all() 读出、改动经 _repo.save() 落盘，若只在内存里
                # 计数，每次重启都归零，护栏等于没有。
                task.recovery_attempts = (task.recovery_attempts or 0) + 1
                # [jonex] P0-2：立即落盘，不等分支。原先只在「重新入队」分支
                # （task.status 非终态）才 save，若磁盘上 status 已是终态而
                # ontology_status 仍是 extracting（抽取期 _fail_task 失败、
                # 或 status 先落盘而 ontology 字段未落盘），计数就永远丢失，
                # 护栏形同虚设。此处先无条件持久化，保证每次重启计数必增。
                try:
                    self._repo.save(task)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("持久化 recovery_attempts 失败: %s", exc)
                try:
                    max_rec = max(
                        1, int(os.getenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", "2")),
                    )
                except ValueError:
                    max_rec = 2
                if task.recovery_attempts > max_rec:
                    # 线上实例：financial_metric.md（46290 chunk）抽取把宿主内存
                    # 吃到 23.7GB 被 oom-killer 杀，灌盘前进程即死，磁盘上恒为
                    # extracting → 每次重启都被判「卡住」重跑，白跑 4.6 小时/轮。
                    # 超限落终态，把无限循环收敛成明确失败，交由人工拆分文档。
                    logger.error(
                        "[jonex][P0-2] 本体任务 %s 连续 %d 次在恢复扫描中被重置"
                        "（上限 %d），疑似抽取期 OOM/崩溃循环，落 FAILED 停止自动重跑",
                        task_id, task.recovery_attempts, max_rec,
                    )
                    task.ontology_status = "failed"
                    task.ontology_error = (
                        f"本体抽取连续 {task.recovery_attempts} 次在进程恢复后被重置，"
                        "疑似内存不足导致进程被杀；已停止自动重跑，"
                        "请拆分文档或调高 ONTOLOGY_RECOVERY_MAX_ATTEMPTS 后重新提交"
                    )
                    self._fail_task(
                        task, ErrorCode.INTERNAL_ERROR, task.ontology_error,
                    )
                    continue
                logger.warning(
                    f"Recovered stuck ontology task {task_id}, resetting to pending "
                    f"(recovery {task.recovery_attempts}/{max_rec})"
                )
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
        user_id: str = "",  # [jonex] v1 退役：查询计量维度对齐 v1 _jonex_query_headers
        only_need_context: bool = False,  # [jonex] 方案 A：多 KB 只召回、平台侧作答
    ) -> dict:
        """Query LightRAG via HTTP client, returning {answer, references}.

        Mirrors the retired v1 LightRAGAdapter.query_detailed() contract:
        references are parsed from :9621 response via parse_file_source(),
        ready for KB-side enrichment with COS presigned URLs and DB lookups.
        """
        if self._http_client is not None:
            from jonex_core.common.file_source_util import (
                normalize_chunk_content,
                parse_file_source,
            )
            result = await self._http_client.query(
                query, mode=mode, top_k=top_k,
                tenant_id=tenant_id, kb_id=kb_id, trace_id=trace_id,
                user_id=user_id, only_need_context=only_need_context,  # [jonex] 透传
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
            kb_type=getattr(req, "kb_type", "") or "",
            content_generation=req.content_generation or 0,
            # [jonex] A18：透传引擎全量 chunk（缺省空列表 = 非引擎链/未携带）
            engine_chunks=req.engine_chunks or [],
            schema_version=req.schema_version or 0,
            schema_hash=req.schema_hash or "",
            strict_push=req.strict_push or False,
            # [jonex] KB 级 VLM 覆盖（空 = 不覆盖，行为与改造前一致）
            vlm_override=getattr(req, "vlm_override", "") or "",
            # [jonex] KB 级本体抽取模型覆盖（空 = 不覆盖，用 ONTOLOGY_LLM_MODEL）
            ontology_model=getattr(req, "ontology_model", "") or "",
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
            # [jonex] 任务中心三步拆分：push_only（第二步，只推切片）同样不碰原文件——
            # content_list / multimodal_results / asset_exts 全部从共享卷的解析产物读回，
            # 图片资产已在第一步传完。这一步的价值就在于此：跨任务不依赖任何本地文件。
            # [jonex] P0-1: 对象存储后端（cos/s3）文件不检查本地路径（file_path 是 storage_key
            # 标识符，实际文件由 pipeline 阶段从对象存储下载），避免误判 FILE_NOT_FOUND。
            needs_local = task.execution_mode not in ("ontology_only", "push_only") and not (
                (task.storage_backend or "").strip().lower() in ("cos", "s3")
                and task.storage_key
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
            # [jonex] llm-wiki 产物分支：openkb→content.md / jonex_wiki→content_list.json
            task.config_snapshot["kb_type"] = task.kb_type
            # [jonex] KB 级 VLM 覆盖（P2a）：**在 resolve 之后无条件写**，与上面的
            # kb_type/strict_push/execution_mode 同一手法。原因两条：
            #   ① INLINE 的 config_resolver 只认 _FIELD_MAP 里的键
            #      （config_resolver.py:121-133），新字段进不来快照；
            #   ② resolve 抛异常时快照会被降级成 {file_path: ...}，走 resolver 的
            #      字段会一起丢 —— 覆盖值必须比它更硬。
            # 用专用键名，不碰 preset 深合并出的 vlm/vlm_model_name（那两个键装着
            # preset 的 vlm: 值，今天不生效，读它就会突然生效）。
            _kb_vlm = (getattr(req, "vlm_override", "") or "").strip()
            if not _kb_vlm:
                _kb_vlm = str(
                    (task.config_snapshot or {}).get("kb_vlm_override") or ""
                ).strip()
            if _kb_vlm:
                task.config_snapshot["kb_vlm_override"] = _kb_vlm

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
        # [jonex] kb_type 分支：jonex_wiki 拿 MinerU 结构化 content_list.json，
        # openkb/默认拿重组版 content.md
        kb_type = getattr(task, "kb_type", "") or (task.config_snapshot or {}).get("kb_type", "")
        try:
            inputs_root = os.getenv("RAG_INPUTS_DIR", "/app/inputs")
            out_dir = Path(inputs_root) / "parsed" / document_id
            if kb_type == "jonex_wiki" and content_list:
                self._write_jonex_wiki_raw_artifact(task, document_id, content_list, out_dir)
                return
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

    def _write_jonex_wiki_raw_artifact(self, task: TaskInfo, document_id: str,
                                       content_list: list, out_dir: Path) -> None:
        """[jonex] jonex_wiki 产物：MinerU 结构化 content_list.json 直写 + 图片复制。

        - content_list.json = MinerU {stem}_content_list.json 的结构化数组（含
          type/text/table_body/image_caption/bbox 等），不做 openkb 的 markdown 重组、
          VLM 描述、图片路径改写。img_path 改写为相对 images/{name}，引擎 ingest
          走 MINERU 路由（normalize_inline_content_list），与独立部署 parse task
          的 MinerU 分支一致。
        - 图片：从 content_list 的 img_path（绝对路径）复制到 parsed/{doc}/images/。
        - parsed_markdown_path 指到 content_list.json，knowledge_base 派发
          jonex_wiki 时读它。
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        images_dir = out_dir / "images"
        has_images = False

        items: list[dict] = []
        for item in content_list:
            if not isinstance(item, dict):
                continue
            # 去掉 stash 字段(_jonex_raw_markdown 仅存 MinerU markdown 原文,不属
            # content_list 结构,写进 json 会污染引擎 normalize 的 type 判定)
            it = {k: v for k, v in item.items() if k != "_jonex_raw_markdown"}
            # 图片路径相对化:绝对路径 → 复制到 images/ → images/{name}
            for field in ("img_path", "table_img_path", "equation_img_path", "image_path"):
                img = it.get(field)
                if not isinstance(img, str) or not img:
                    continue
                if img.startswith(("http://", "https://", "s3://")):
                    continue
                if os.path.isfile(img):
                    dst_name = os.path.basename(img)
                    try:
                        images_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(img, images_dir / dst_name)
                        it[field] = f"images/{dst_name}"
                        has_images = True
                    except Exception:
                        logger.warning("[jonex] jonex_wiki raw artifact 图片复制失败: %s", img)
                        it[field] = ""
                else:
                    # 非本地文件且非 URL:置空,避免引擎 _safe_item 越界丢弃警告
                    it[field] = ""
            if it:
                items.append(it)

        (out_dir / "content_list.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8")

        rel_md = f"parsed/{document_id}/content_list.json"
        rel_assets = f"parsed/{document_id}/images" if has_images else ""

        task.storage = StorageInfo(root=str(out_dir), mineru_dir=str(out_dir))
        if task.result_summary is None:
            task.result_summary = ResultSummary(doc_id=document_id)
        task.result_summary.extensions["parsed_markdown_path"] = rel_md
        task.result_summary.extensions["assets_dir"] = rel_assets
        logger.info(
            "[jonex] jonex_wiki raw artifact written: doc=%s json=%s assets=%s items=%d",
            document_id, rel_md, rel_assets or "(none)", len(items),
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
        - 视频后端：按 `_select_backend` **实际选中的后端**分流（不是「后端池非空」——
          mineru 绑定也入池）。选中 mps 时注入 task.mps_video_url（仅 COS 存储时
          FetchObject 设置，task_manager.py:1361-1369），拿不到则**提前短路**写占位、
          不白等长超时；选中 mineru / 未入池（local）时原样交给后端，mineru 读本地
          video_path（塞 COS URL 会被它当本地路径打开 → FileNotFoundError）
        - 图片：VLM 增强描述（对齐 full 模式 modalprocessors.py:894 generate_description_only）；
          失败/空**不写占位**（图片有 MinerU OCR 文本 + 原生 caption 兜底，避免污染），
          只记 warning 信号——与 video/audio（必须留痕）的差异见 image 分支注释
        - 失败/配置关闭：video/audio 写占位文本（**粗粒度原因**，异常原文只进日志——安全），
          不阻断 parse_only；占位同时写机器可读信号（_record_multimodal_warning）
        - 转写缓存：按 document_id 读/写 parsed/{doc}/transcript.json——reparse/重编译
          重跑 parse_only 时命中缓存跳过转写（省 MPS/ASR 费用）；失败/占位不写缓存。
          **图片条目按 VLM 模型分桶**（`image@<model>`，见 `_transcript_cache_key`）：
          换模型后必须重算描述，否则用户改完模型拿到的还是旧模型的产出
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

        # [jonex] 图片缓存按模型分桶（见 _transcript_cache_key）：读出来的键与写回去的
        # 键必须同源，所以在这里取一次，循环里不重复读 contextvar。
        try:
            from raganything.service.jonex_metering_ctx import get_vlm_override

            _vlm_tag = (get_vlm_override() or "").strip()
        except Exception:  # pragma: no cover - ctx 导入失败时退回旧键
            _vlm_tag = ""

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
            _ck = _transcript_cache_key(t, _vlm_tag)
            if _ck in transcript_cache:
                # [jonex] 命中缓存：跳过转写（省一次 MPS/ASR 费用），直接挂回
                item["description"] = str(transcript_cache[_ck])
                continue
            _cacheable = False
            body = ""
            modal_content: dict = {}   # except 兜底可安全访问（processor 写回的 _audio_segments 在副本上）
            try:
                modal_content = dict(item)   # 独立 dict：processor 原地写 _audio_segments
                if t == "video":
                    # [jonex] backend 判据：**这次到底会走哪个后端**，而不是「后端池非不非空」。
                    # 池里装什么由两处决定（processor_builder.py:114-167）：MPS_ENABLED=true
                    # 入池 "mps"；config.video_analysis_binding（env VIDEO_ANALYSIS_BINDING）
                    # 入池 "mineru" 等。绑定接活之后「池非空」就再≠走 MPS —— mineru 同样入池，
                    # 而它读的是本地 video_path、不需要 COS URL。旧的粗判据会把 mineru 绑定的
                    # 视频当成 MPS 直接跳过（2026-10-08：MPS_ENABLED=false、绑定 mineru，
                    # 却产出「MPS 需要 COS 存储的 URL」占位，转写一次都没发生）。
                    # ⚠ 判据走 `route_video`（与 full 链 MultimodalStage._process_one
                    # **同一个函数**）—— 两条链各写一份正是那次事故的根因，别再拆开。
                    # 不缓存——reparse 换 COS 存储后应重试转写
                    _routing = route_video(proc, modal_content, mps_url)
                    if _routing.needs_cos_url:
                        body = ("该视频的转写已跳过：MPS 视频分析需要 COS 存储的 URL，"
                                "当前为本地存储（无 mps_video_url），MPS 不可用。")
                        self._record_multimodal_warning(task, "video_mps_no_cos_url", body)
                    else:
                        if _routing.use_mps:
                            # 仅 MPS 路径注入 COS URL；mineru / local 都不需要
                            modal_content["mps_video_url"] = mps_url
                        elif "mps" in (getattr(proc, "_video_backends", None) or {}):
                            # MPS 已入池但这条视频用不上它（无 COS URL 且未绑定 mps）：
                            # 按 _select_backend 的契约回落内置本地管线，不是静默丢弃
                            logger.info(
                                "[jonex] 视频未走 MPS（无 COS URL 且未绑定 mps）→ 内置本地管线"
                            )
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
                    # [jonex] P2a：图片 VLM 失败计入降级比例（护栏判据）
                    try:
                        from raganything.service.jonex_metering_ctx import (
                            record_vlm_degraded,
                        )

                        record_vlm_degraded("image_vlm_failed")
                    except Exception:  # pragma: no cover
                        pass
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
                    # [jonex] P2a：空描述同样计入降级（VLM 返回空 = 没拿到描述）
                    try:
                        from raganything.service.jonex_metering_ctx import (
                            record_vlm_degraded,
                        )

                        record_vlm_degraded("image_empty_description")
                    except Exception:  # pragma: no cover
                        pass
                else:
                    # 兜底：转写成功但内容为空（无语音/空 scenes）——写占位
                    body = f"该{_t_cn.get(t, t)}内容无可提取的文本（无语音或转写为空）。"
                    self._record_multimodal_warning(task, f"{t}_empty_content", body)
            item["description"] = body
            if _cacheable:
                transcript_cache[_ck] = body
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

        判序（bd ``jonex-platform-977`` 起）：

        ① **优先读抛错方带的结构化 ``reason``** —— MinerU 传输层 / 任务体失败由
           ``parsers.mineru`` 在**拿得到状态码的地方**打好标签（HTTP 400/401/402/404/
           429/5xx、连不上、连不上超时，以及任务体里写的 402 …）。此前传输层把
           urllib 的异常一律重抛成 ``RuntimeError``，下面那套按类型名的判定拿到的
           信息是**零**，于是全部塌缩成同一句「转写服务不可用」——2026-10-09 线上
           上游欠费（402）排查绕远路，就是因为用户和运维看到的都是那一句。
        ② 没有 ``reason`` 的（httpx / asyncio 等其它生产者）仍按**类型名**判定。
        ③ 其余一律「转写服务不可用」。

        ⚠ **不拼 ``str(exc)`` 参与匹配**：异常文本可能含中文/URL，匹配不可预期，
        也会让 ``KeyError: 'missing_field'`` / ``FileNotFoundError`` 被误归
        「配置缺失」误导排查。异常名与完整 ``str(exc)`` 只进日志。
        """
        _structured = getattr(exc, "reason", None)
        if isinstance(_structured, str) and _structured:
            return _structured
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
        """[jonex] R7-2b: 任务 pipeline 前从对象存储（cos/s3）异步下载文件到本地临时路径。

        full / parse_only 执行模式共用（parse_only 即 OpenKB 管线）。返回
        本地临时文件路径，调用方负责 finally 清理；非对象存储后端返回 None；
        下载失败抛 OBJECT_FETCH_FAILED（可被 _classify_error 识别）。

        [jonex] S3 兼容：按 task.storage_backend 选客户端（get_object_storage_for），
        而非全局单例——混合数据（部分文档 cos、部分 s3）时全局单例会用错后端。
        """
        _backend = (task.storage_backend or "").strip().lower()
        if not (_backend in ("cos", "s3") and task.storage_key):
            return None

        import uuid as _uuid
        _cos_cache = self._cos_cache_dir()
        # [jonex] P0-1: sweep 兜底清残留（异常退出时 TTL 过期 → 下一次下载前清理）
        self._sweep_cos_cache()
        _base_name = os.path.basename(task.file_path or task.storage_key) or "cos_object"
        local_path = os.path.join(_cos_cache, f"{_uuid.uuid4().hex}_{_base_name}")
        try:
            from jonex_core.common.object_storage import get_object_storage_for
            await get_object_storage_for(_backend).get_to_path(task.storage_key, local_path)
            logger.info(
                "FetchObject: %s 下载完成 key=%s → %s task=%s",
                _backend, task.storage_key, local_path, task.task_id,
            )
            task.file_path = local_path
            # 下游 pipeline 统一按本地文件处理
            # 不修改 task.storage_backend（保留 cos 语义给终态 finally 判断是否清理）
        except Exception as _e:
            logger.exception(
                "FetchObject: %s 下载失败 key=%s task=%s",
                _backend, task.storage_key, task.task_id,
            )
            # [jonex] P0-1: 下载失败不留残留空文件
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
            except OSError:
                pass
            # 抛一个能被 _classify_error 识别为 OBJECT_FETCH_FAILED 的异常
            raise RuntimeError(
                f"OBJECT_FETCH_FAILED: {_backend} 对象下载失败 key={task.storage_key}: {_e}"
            ) from _e

        # [jonex] MPS 视频路径：仅 KB 解析器选择 video_mps 时才保留 COS URL 供
        # MPS backend 使用（per-KB 路由设计 2026-09-01）。其余解析器（本地 VLM
        # 管线）不设置——_select_backend 按 mps_video_url 存在性路由，无条件设置
        # 会把 KB 配置为 VLM 的视频也强行送进 MPS。
        # [jonex] P2: 改用 storage_key 判扩展名（本地名是 {uuid}_{basename}，可能丢扩展名）
        _ext = os.path.splitext(task.storage_key)[1].lower()
        if (task.preset_name == "video_mps"
                and _ext in {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.mpg', '.mpeg', '.3gp'}):
            _bucket = os.getenv("COS_BUCKET", "")
            _region = os.getenv("COS_REGION", "")
            if _bucket and _region:
                task.mps_video_url = f"https://{_bucket}.cos.{_region}.myqcloud.com/{task.storage_key}"
                logger.info("FetchObject: MPS COS URL set for video task=%s", task.task_id)
        return local_path

    async def _execute_pipeline_http(self, task: TaskInfo, handle: TaskHandle):
        """[jonex] 解析入口：为**所有执行模式**设置入库 contextvar，然后跑 inner。

        为什么要在最外层设置（P2a）：改造前只有 full 主链调 set_ingest_ctx，
        parse_only / prepare_only / ontology_only / push_only 都在它之前 return ——
        这些模式里的 VLM 调用既读不到 KB 级覆盖，计量也缺 doc_id（wiki/openkb
        解析全中）。覆盖是按任务的语义：必须在任何 VLM 调用之前设置、结束时还原。

        strict_mode 由执行模式推导，平台不需要多传开关：
        - reparse_strict → "fail"：旧数据已删，运行期降级只留痕（靠预检兜底）；
        - 其余解析模式 → "record"：降级比例超阈值即判失败；
        - 无覆盖时两者都不介入（guard 先判 override）。
        """
        _override = (
            getattr(task, "vlm_override", "")
            or str((task.config_snapshot or {}).get("kb_vlm_override") or "")
        ).strip()
        _strict_mode = "fail" if task.execution_mode == "reparse_strict" else "record"
        _ingest_token = set_ingest_ctx(
            tenant_id=task.tenant_id,
            kb_id=task.kb_id,
            doc_id=task.document_id or "",
            trace_id=task.task_id,
            vlm_override=_override,
            vlm_strict_mode=_strict_mode if _override else "",
        )
        try:
            await self._execute_pipeline_http_inner(task, handle)
        finally:
            reset_ingest_ctx(_ingest_token)
            reset_vlm_degraded()

    async def _execute_pipeline_http_inner(self, task: TaskInfo, handle: TaskHandle):
        """HTTP mode pipeline execution."""
        # Check cancellation before starting
        if handle.cancel_event.is_set():
            raise TaskCancelledError()

        # [jonex] KB 级 VLM 覆盖 P2a：**预检**。有覆盖时先打一发 1x1 PNG 的真实
        # VLM 请求，模型名错/无权限/端点不通在这里就失败，而不是等多模态写占位
        # （modalprocessors 对 VLM 失败是宽容的：写占位并让任务成功）。
        # 时机关键：reparse_strict 会在此后卸掉旧数据，预检必须在它之前。
        _kb_vlm_override = (
            getattr(task, "vlm_override", "")
            or str((task.config_snapshot or {}).get("kb_vlm_override") or "")
        ).strip()
        if _kb_vlm_override and task.execution_mode not in (
            "ontology_only", "push_only"
        ):
            from raganything.service.vlm_guard import precheck_vlm_override

            await precheck_vlm_override(
                getattr(self._pipeline_executor, "vlm_bound", None),
                stage=task.execution_mode,
            )

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
            # [jonex] S3 兼容：cos/s3 均需 pipeline 前下载到本地
            if (task.storage_backend or "").strip().lower() in ("cos", "s3") and task.storage_key:
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

        # ── [jonex] 任务中心三步拆分：第二步 push_only ──
        # 只推切片。**不下载原文件、不校验 file_path**：输入全部来自第一步落在共享卷的
        # 解析产物（content_list / multimodal_results / asset_exts），图片资产也已在
        # 第一步传完。所以这里既不 fetch COS 也没有 finally 清理。
        if task.execution_mode == "push_only":
            await self._run_push_only(task, handle)
            return

        # ── [jonex] 任务中心三步拆分：第一步 prepare_only ──
        # 解析 + 多模态 + 图片资产上传 + office 预览上传，但**不推切片**。产物落共享卷
        # 后，第二步（另一个任务、可能另一个容器）才读得到。
        # 与 parse_only 一样需要先下载原文件（file_path 是 COS storage_key 时本地不存在）。
        if task.execution_mode == "prepare_only":
            _cos_temp_path: str | None = None
            if (task.storage_backend or "").strip().lower() in ("cos", "s3") and task.storage_key:
                _cos_temp_path = await self._fetch_cos_object(task)
            try:
                await self._run_prepare_only(task, handle)
            finally:
                if _cos_temp_path:
                    try:
                        if os.path.exists(_cos_temp_path):
                            os.remove(_cos_temp_path)
                            logger.debug(
                                "FetchObject: cleaned COS temp %s task=%s",
                                _cos_temp_path, task.task_id,
                            )
                    except OSError:
                        logger.warning("FetchObject: failed to clean COS temp %s", _cos_temp_path)
            return

        # Register progress callback
        progress_cb = ProgressTrackingCallback(task, handle)
        self._pipeline_executor.callback_manager.register(progress_cb)

        # ── [jonex] R7-2b: FetchObject —— pipeline 前异步下载对象存储对象（cos/s3）──
        # 把下载从同步 HTTP 请求路径（insert/retry invoke）移到异步任务 pipeline，
        # invoke 立刻返回 task_id，空窗期压到毫秒级。
        _cos_temp_path: str | None = None  # [jonex] P0-1: 供 finally 清理
        # [jonex] S3 兼容：cos/s3 均需 pipeline 前下载到本地
        if (task.storage_backend or "").strip().lower() in ("cos", "s3") and task.storage_key:
            _cos_temp_path = await self._fetch_cos_object(task)

        # ── PipelineContext (base) — per-task context ──
        # 用 raganything.pipeline.base.PipelineContext（dataclass，字段齐全），
        # 而非本模块的统计用 PipelineContext（__slots__ 缺 collected_doc_ids 等），
        # 否则 pipeline.execute 的 merge_context(dataclasses.replace) 与回写会报错。
        # [jonex] MPS 输入 URL：外部直调传入优先（方案 C）；否则按 KB 解析器
        # （preset=video_mps）+ cos 存储内部构造（per-KB 路由设计 2026-09-01）
        _mps_url = task.mps_video_url or ""
        if not _mps_url:
            _mps_url = build_mps_video_url(
                task.preset_name, task.storage_backend, task.storage_key,
                os.getenv("COS_BUCKET", ""), os.getenv("COS_REGION", "ap-guangzhou"),
            )
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
            mps_video_url=_mps_url,
        )
        # [jonex] B2: 主解析提示词覆盖（按 task.prompt_ids 解析；缺失 id fail-fast）
        ctx.prompt_overrides = self._resolve_task_prompt_overrides(task)
        ctx.started_at = time.time()

        # [jonex] Gap A2 → P2a：入库 contextvar 已上移到 _execute_pipeline_http 统一
        # 设置，使 parse_only / prepare_only 等提前 return 的模式也带上覆盖与 doc_id。
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
                # [jonex] P4 §8：判重计数落 task，本体阶段 chunk 读回缺口告警用
                task.duplicated_chunk_count = getattr(ctx, 'duplicated_chunk_count', 0)

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

                # [jonex] 表格上下文存档上移到「推完切片」处落盘（原先只落在本体抽取
                # 阶段 _run_ontology_extraction 内）：full 不带 ontology_schema 时不抽
                # 本体 → 存档不落 → 后续单独的 ontology_only 回放不到
                # （_load_table_context 返回 None）会命中 P0-b 闸门，对含表格文档直接判
                # FAILED。
                # 语义不变：仍然落在「chunk 可能被改写之前」，且早于本体阶段全部早退。
                # best-effort，落盘失败只告警（详见 _table_context_dir 上方注释）。
                #
                # [jonex] 任务中心三步拆分走的**不是这条路**：prepare_only 有它自己的
                # 同名调用（_run_prepare_only 内），两边 execution_mode 互斥，不会重复落盘。
                _persist_table_context(task, ctx.content_list)

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
                        # [jonex] P0-c：本体抽取阶段（_merge_table_objects）
                        # 写回的表格主键质量指标，晚于上方 `task.result_summary
                        # = summary` 的快照点（后者在抽取之前）。此处补合并一次，
                        # 否则 pk_recognition 类指标永远到不了
                        # result_summary.extensions.table_stats。
                        _late_stats = getattr(ctx, "table_stats", None)
                        if _late_stats:
                            summary.extensions = dict(summary.extensions or {})
                            summary.extensions["table_stats"] = _late_stats
                            task.result_summary = summary

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

        # [jonex] KB 级 VLM 覆盖 P2a：parse_only 走的是自己的多模态文本化（不经
        # MultimodalStage），所以护栏要单独判一次。判定点同样是「多模态之后、
        # 产物落盘之前」。
        try:
            from raganything.service.vlm_guard import guard_vlm_degraded

            guard_vlm_degraded(
                _count_multimodal_items(content_list), stage="parse_only"
            )
        except ImportError:  # pragma: no cover
            pass

        # 写 OpenKB 产物（markdown+图片 → 共享卷；含转写正文；相对路径经 result_summary 暴露）
        self._write_openkb_artifact(task, ctx)

        # [jonex] office 查看原文定位：本函数不走 stage pipeline（见 docstring：
        # 跳过 multimodal/push_chunks/ontology），而 OfficePreviewUploadStage 只挂在
        # HTTP/full pipeline 上 —— 结果就是走 parse_only 的 KB 永远没有 preview.pdf，
        # 查看原文只能回退下载原始 office 文件（iframe 里 xlsx 被浏览器直接下载，
        # 看不到任何预览）。这里显式补跑同一实现（best-effort，失败不阻塞主链路）。
        await self._upload_office_preview(task)

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

    # ── 任务中心三步拆分：prepare / push ─────────────────────────────

    async def _run_prepare_only(self, task: TaskInfo, handle: TaskHandle) -> None:
        """[jonex] 三步拆分**第一步**：解析 + 多模态 + 资产/office 预览上传，不推切片。

        跑 create_prepare_only_pipeline（= create_http_pipeline 去掉 push_chunks）。跑完
        把 final_ctx 的三样产物（content_list / multimodal_results / asset_exts）落共享卷，
        第二步 push_only 只读这些产物就能推切片 —— 跨任务、跨容器可用。

        与 _run_parse_only 的区别（别混）：parse_only 是 OpenKB 链路的手工解析（自己调
        parse_document + _textualize_multimodal，只产 markdown 给 OpenKB 编译，不产
        asset_exts / multimodal_results 原件）；prepare_only 走完整 pipeline 前缀，产的三样
        产物正好是 LightRAG 推切片所需的全部输入。
        """
        if handle.cancel_event.is_set():
            raise TaskCancelledError()

        # [jonex] MPS 输入 URL 与主解析提示词覆盖：与 full 链路同口径。解析质量相关的
        # 参数必须一致，否则「第一步解析」与「单跑 full」产出的 content_list 会有差异。
        _mps_url = task.mps_video_url or ""
        if not _mps_url:
            _mps_url = build_mps_video_url(
                task.preset_name, task.storage_backend, task.storage_key,
                os.getenv("COS_BUCKET", ""), os.getenv("COS_REGION", "ap-guangzhou"),
            )
        _snap = task.config_snapshot or {}
        ctx = PipelineCtx(
            file_path=task.file_path,
            file_name=task.name,
            tenant_id=task.tenant_id,
            kb_id=task.kb_id,
            doc_id=task.document_id or "",
            document_id=task.document_id or "",
            cancel_event=handle.cancel_event,
            force_reparse=_snap.get("force_reparse", False),
            parser_type=_snap.get("parser", ""),
            config_snapshot=_snap,
            mps_video_url=_mps_url,
        )
        ctx.prompt_overrides = self._resolve_task_prompt_overrides(task)
        ctx.started_at = time.time()

        task.current_step = "prepare"
        task.progress = 0.3
        self._repo.save(task)

        result = await self._pipeline_executor.prepare_only(ctx)

        if not result.success:
            # 解析没成，第二步就没有输入可用 → 直接判失败，不留半成品状态
            self._fail_task(
                task, ErrorCode.PARSER_ERROR,
                f"prepare_only 失败：{ctx.error or result.error or '未知错误'}",
            )
            _log_ingest_timing_v2(task, "failed", error=getattr(task, "error_message", ""))
            return

        # ① 产物落盘 —— 第二步的**唯一**输入。落不下去就没有「部分成功」可言：
        #    静默降级会让文档永远到不了「知识抽取入库中」且没人知道为什么。
        ok, msg = _persist_ingest_artifacts(
            task, ctx.content_list, ctx.multimodal_results, ctx.asset_exts,
        )
        if not ok:
            self._fail_task(task, ErrorCode.PARSER_ERROR, msg)
            _log_ingest_timing_v2(task, "failed", error=getattr(task, "error_message", ""))
            return

        # ② 表格上下文存档（**兜底的一份**，第二步 push 完还会落一份覆盖它）。
        #    这一步存的只有 content_list 里 type=="table" 的 item —— MinerU / 表格类
        #    解析器的表格在这一步就是全的。缺失的是 md / 内嵌表格那条路：它们的表体
        #    由 push 阶段的 _emit_table_chunks 登记成 text item 上的 _mapped_tables，
        #    此刻还不存在（见 _table_context_items 的两类来源）。
        #    所以这份是**兜底**：万一第二步从没跑过，磁盘上至少有一份可回放的存档，
        #    不至于让第三步的 P0-b 闸门因「无存档」把文档直接判 FAILED。
        #    write_when_empty=False 是关键：这一步抽到 0 张表**不代表文档没表格**
        #    （md 类文档此刻必然抽到 0 张），落空存档会把「未知」写成「确认无表格」，
        #    第三步静默放过、表格知识凭空消失。宁可这步不写。
        #    best-effort，失败只告警（详见 _table_context_dir 上方注释）。
        _persist_table_context(task, ctx.content_list, write_when_empty=False)

        # ③ 结果摘要：只填块统计。entities/chunks 由 :9621 在推切片后才产出，这一步
        #    还没有，第二步 push_only 才回读 —— 不要在这里假装读到。
        ctx.completed_at = time.time()
        summary = ResultSummary(doc_id=task.document_id or "")
        summary.duration_seconds = ctx.duration_seconds
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
        task.result_summary = summary

        task.status = TaskStatus.COMPLETED
        task.progress = 1.0
        task.current_step = "done"
        self._repo.save(task)
        _log_ingest_timing_v2(task, "completed")
        logger.info(
            "[jonex] Task %s completed (prepare_only): doc=%s blocks=%d "
            "multimodal_results=%d asset_exts=%d",
            task.task_id, task.document_id, len(ctx.content_list or []),
            len(ctx.multimodal_results or []), len(ctx.asset_exts or {}),
        )

    async def _run_push_only(self, task: TaskInfo, handle: TaskHandle) -> None:
        """[jonex] 三步拆分**第二步**：只推切片（P3，用户看到的「知识抽取入库中」）。

        输入全部来自第一步落在共享卷的解析产物，**不下载原文件、不重新解析**。推完之后
        才置 lightrag_doc_ids / 四个计数器，KB 对账循环据此把文档提升到 READY。
        """
        if handle.cancel_event.is_set():
            raise TaskCancelledError()

        content_list, multimodal_results, asset_exts = _load_ingest_artifacts(task)
        if content_list is None:
            # 没有产物就一条 chunk 都推不出去。这里不重试也不降级：重试要重新解析，
            # 那是第一步的事；静默成功会让文档卡在「知识抽取入库中」的假象里。
            self._fail_task(
                task, ErrorCode.PARSER_ERROR,
                "解析产物缺失或损坏（读不到 content_list.json），无法推切片；请重新解析该文档",
            )
            _log_ingest_timing_v2(task, "failed", error=getattr(task, "error_message", ""))
            return

        _snap = task.config_snapshot or {}
        ctx = PipelineCtx(
            file_path=task.file_path,
            file_name=task.name,
            tenant_id=task.tenant_id,
            kb_id=task.kb_id,
            doc_id=task.document_id or "",
            document_id=task.document_id or "",
            cancel_event=handle.cancel_event,
            config_snapshot=_snap,
            parser_type=_snap.get("parser", ""),
        )
        ctx.content_list = content_list
        ctx.multimodal_results = multimodal_results
        ctx.asset_exts = asset_exts
        ctx.started_at = time.time()

        task.current_step = "push_chunks"
        task.progress = 0.3
        self._repo.save(task)

        result = await self._pipeline_executor.push_chunks_only(ctx)

        if not result.success:
            self._fail_task(
                task, ErrorCode.LIGHTRAG_ERROR,
                f"push_only 失败：{ctx.error or result.error or '未知错误'}",
            )
            _log_ingest_timing_v2(task, "failed", error=getattr(task, "error_message", ""))
            return

        # ── 表格上下文存档（**第二次**，覆盖第一步那份）──
        # 必须在 push **之后**：md / 内嵌表格的表体是 push 阶段 _emit_table_chunks
        # 登记到 content_list text item 上的 _mapped_tables，push 之前不存在。
        # 第三步 ontology_only 手上没有 content_list（它按 document_id 读 LightRAG），
        # 表格子集全靠回放这份存档；漏了 _mapped_tables → table_handled 判 False →
        # 行对象一个不产、连「零产出兜底」那段（if table_handled and ...）都进不去，
        # 表格知识静默丢失且不报错。
        # ctx.content_list 与 PushChunksStage 看到的是**同一个 list 对象**
        # （DocumentPipeline 用 current=merge_context(...) 递进，而 merge_context 是
        #  dataclasses.replace —— 只换外层，字段仍是引用），所以这里读得到 stage 刚
        # 登记进去的 _mapped_tables。
        # 落点与 full 链路（_execute_pipeline_http 内、push 之后）完全一致。
        # best-effort，失败只告警。
        _persist_table_context(task, ctx.content_list)

        # ── 计数收回：与 full 链路同口径（KB 对账靠这些判 READY 与对账告警）──
        task.lightrag_doc_ids = list(ctx.collected_doc_ids)
        task.pending_track_ids = list(ctx.pending_track_ids)
        task.failed_chunk_count = getattr(ctx, "failed_chunk_count", 0)
        task.total_chunk_count = getattr(ctx, "total_chunk_count", 0)
        task.timeout_chunk_count = getattr(ctx, "timeout_chunk_count", 0)
        task.duplicated_chunk_count = getattr(ctx, "duplicated_chunk_count", 0)

        # ── 回读 :9621 的结果摘要（entities/chunks 到这一步才存在）──
        summary = await self._read_result_summary_http(
            task.tenant_id, task.kb_id, task.document_id,
        )
        ctx.completed_at = time.time()
        summary.duration_seconds = ctx.duration_seconds
        summary.doc_id = task.document_id or ""
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
        # ctx.table_stats 由 PushChunksStage 产出（push_chunks_only 单独回灌了它 ——
        # _run_http_pipeline 的通用回灌列表为保持 full 链路行为不变没列这个字段）。
        _table_stats = getattr(ctx, "table_stats", None) or {}
        if _table_stats:
            summary.extensions = dict(summary.extensions or {})
            summary.extensions["table_stats"] = _table_stats
        task.result_summary = summary

        task.status = TaskStatus.COMPLETED
        task.progress = 1.0
        task.current_step = "done"
        self._repo.save(task)
        _log_ingest_timing_v2(task, "completed")
        logger.info(
            "[jonex] Task %s completed (push_only): doc=%s chunks=%d lightrag_doc_ids=%d "
            "duplicated=%d",
            task.task_id, task.document_id, task.total_chunk_count,
            len(task.lightrag_doc_ids), task.duplicated_chunk_count,
        )

    async def _upload_office_preview(self, task: TaskInfo) -> None:
        """[jonex] parse_only 链路的 office preview.pdf 补传（best-effort）。

        与 full 链路的 OfficePreviewUploadStage 共用
        'raganything.pipeline.stages.upload_office_preview'：
        - 仅 office 扩展名生效，其余直接跳过；
        - 转换 PDF 复用 '{output_dir}/{stem}.pdf'（本地 mineru 解析产物），
          缺失时才调 LibreOffice 现转；
        - 上传失败 / 对象存储不可用只 WARNING，不影响 task.status。

        放在 task.status = COMPLETED 之前：与 full 链路「preview 赶在 chunk 写入前
        就位」同序，避免文档已 READY 但预览仍缺窗口期。
        """
        try:
            from raganything.pipeline.stages import upload_office_preview

            output_dir = (
                (task.config_snapshot or {}).get("output_dir")
                or os.path.join(self._base_dir, task.tenant_id, task.kb_id, "output")
            )
            await upload_office_preview(
                file_path=task.file_path,
                file_name=task.name,
                tenant_id=task.tenant_id,
                kb_id=task.kb_id,
                doc_anchor=task.document_id or "",
                output_dir=output_dir,
                logger=logger,
            )
        except Exception as exc:  # noqa: BLE001
            # 延迟 import 失败 / 目录不可用等一律降级为 WARNING
            logger.warning("[jonex] office-preview: parse_only 补传跳过: %s", exc)

    # ── Ontology-only execution (P0-A) ──────────────────────────────

    # ── [jonex] A18：引擎前置段（灌库 + 等终态 + 读回校验，spec §4.3）────────

    async def _ingest_engine_chunks(self, task: TaskInfo, chunks: list) -> None:
        """把平台随任务带来的 引擎全量 chunk 灌入 LightRAG 并做读回校验。

        步骤（顺序重要）：
          1. 锚校验：`doc=/tenant=/kb=` 三元与任务一致；不符 ⇒ 抛（错锚会静默读回空）。
          2. **先合成表格存档**（用**原始** content、`_inject_ns_token` 之前）；
             **恒写档**：无表格片 ⇒ 覆盖写空列表（消「旧档回放 + P0-b 闸门误杀」）。
          3. 逐条灌库：空 content 跳过计数；`_inject_ns_token` 注入 → `upload_text`
             （条级重试 3 次指数退避，env `RAG_ONTOLOGY_INGEST_UPLOAD_RETRIES`）。
          4. 等全量终态：`batch_track_status` 只作**结束时机**——orphan 释放
             （empty=True）与 failed 轨**都不构成放行/判死**（真伪交读回裁决）；
             等待异常也只是 warning。
          5. 读回校验（唯一裁决；**按条 status 分流**，补充波定稿）：
             读回通道（列表端点）剥 ns marker ⇒ 全部比对用**注入前内容**；
             `success` ⇒ 该锚必须落地（重切场景锚继承，成立）；
             `duplicated` ⇒ 该内容必须可读——内容门保证「同 sanitize 内容在
             工作区仅一条记录、锚=首推锚」，跨代旧锚场景锚可能漂移，故按
             内容面容忍，另补分片形态（超限片重切后「全片」不再逐条在场）；
             若该内容不可读且无合格分片（同锚异内容被拒 ⇒ 静默陈旧）⇒ 抛；
             未知状态 ⇒ 锚或内容任一在场。canary 条件比对保留：锚恰 1 条
             记录者内容须等于本地提交内容（sanitize 后）。

        任何失败一律 raise（worker 的 `_fail_task` 落 FAILED + error_message）。
        """
        document_id = task.document_id or ""
        if not chunks or not document_id:
            raise RuntimeError(
                f"[jonex][A18] engine_chunks 前置段输入不完整: "
                f"doc={document_id!r} chunks={len(chunks or [])}"
            )

        # 1. 锚校验
        for chunk in chunks:
            kv = _parse_anchor_kv(chunk.get("file_source") or "")
            if (
                kv.get("doc") != document_id
                or kv.get("tenant") != task.tenant_id
                or kv.get("kb") != task.kb_id
            ):
                raise RuntimeError(
                    "[jonex][A18] engine_chunks 锚校验失败（doc/tenant/kb 与任务不一致）: "
                    f"chunk={kv.get('chunk')} doc={kv.get('doc')!r} "
                    f"tenant={kv.get('tenant')!r} kb={kv.get('kb')!r}；任务 "
                    f"doc={document_id!r} tenant={task.tenant_id!r} kb={task.kb_id!r}"
                )

        # 2. 合成表格存档（原始 content）并恒写档
        table_items = _table_context_items_from_chunks(chunks)
        if _write_table_context(task, table_items) is None:
            raise RuntimeError(
                f"[jonex][A18] 表格上下文存档写入失败（doc={document_id}）"
                "——含表格片文档无档会被 P0-b 闸门硬失败，拒绝带缺档继续"
            )
        logger.info(
            "[jonex][A18] 表格上下文存档已合成 %d 条（含空档）: doc=%s",
            len(table_items), document_id,
        )

        # 3. 逐条灌库（条级重试，指数退避）
        from raganything.pipeline.stages import _inject_ns_token  # noqa: PLC0415
        # ↑ 函数内 import：顶层导入 pipeline.stages 有循环依赖风险（本文件既有先例）。

        upload_retries = max(1, int(os.getenv("RAG_ONTOLOGY_INGEST_UPLOAD_RETRIES", "3")))
        track_ids: set = set()
        original_by_anchor: dict = {}  # 锚 → 原始内容（读回通道剥 ns marker，比对一律用注入前内容）
        # 读回裁决期望（按条 status 分流的三个桶；补充波定稿）：
        require_anchor: dict = {}     # success 条 ⇒ 锚必须落地（值=原始内容，报错抽样用）
        require_content: dict = {}    # duplicated 条 ⇒ sanitize 内容可读或该条锚为分片形态（值=锚集合）
        require_either: dict = {}     # 未知状态兜底 ⇒ 锚或内容任一在场（值=内容键）
        skipped_empty = 0
        for chunk in chunks:
            content = chunk.get("content") or ""
            file_source = chunk.get("file_source") or ""
            if not content.strip():
                skipped_empty += 1
                continue
            anchor = _parse_anchor_kv(file_source).get("chunk") or ""
            pushed = _inject_ns_token(content, task.tenant_id, task.kb_id, document_id)
            content_key = _sanitize_for_dedupe(content)  # 读回剥 marker ⇒ 键用注入前内容
            original_by_anchor.setdefault(anchor, content)

            last_err: Optional[Exception] = None
            res = None
            for attempt in range(1, upload_retries + 1):
                try:
                    res = await self._http_client.upload_text(
                        pushed, file_source, tenant_id=task.tenant_id, kb_id=task.kb_id,
                    )
                    if getattr(res, "track_id", ""):
                        track_ids.add(res.track_id)
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if attempt < upload_retries:
                        await asyncio.sleep(2 ** (attempt - 1))
            if last_err is not None:
                raise RuntimeError(
                    f"[jonex][A18] chunk 灌库失败（doc={document_id} chunk={anchor}，"
                    f"重试 {upload_retries} 次）: {last_err}"
                ) from last_err

            status = (getattr(res, "status", "") or "") if res is not None else ""
            if status == "duplicated":
                require_content.setdefault(content_key, set()).add(anchor)
            elif status == "success":
                require_anchor[anchor] = content
            else:
                require_either[anchor] = content_key
        logger.info(
            "[jonex][A18] 灌库完成: doc=%s pushed=%d skipped_empty=%d tracks=%d"
            "（require_anchor=%d require_content=%d require_either=%d）",
            document_id, len(original_by_anchor), skipped_empty, len(track_ids),
            len(require_anchor), len(require_content), len(require_either),
        )

        # 4. 等全量终态（batch 底座；empty/failed 不作真伪结论，只影响结束时机）
        if track_ids:
            try:
                terminal, pending = await self._http_client.batch_track_status(
                    sorted(track_ids),
                    tenant_id=task.tenant_id, kb_id=task.kb_id,
                    max_wait_seconds=float(os.getenv("RAG_ONTOLOGY_INGEST_WAIT_SEC", "1800")),
                    per_track_timeout_seconds=float(
                        os.getenv("RAG_ONTOLOGY_INGEST_PER_TRACK_SEC", "900")
                    ),
                )
                logger.info(
                    "[jonex][A18] 轨道等待结束: doc=%s terminal=%d pending=%d"
                    "（orphan 释放与 failed 均不作真伪结论，交读回裁决）",
                    document_id, len(terminal), len(pending),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[jonex][A18] 轨道等待异常，直接进读回校验: doc=%s err=%s",
                    document_id, e,
                )

        # 5. 读回校验（唯一裁决）
        readback = await self._http_client.get_document_chunks(
            document_id, tenant_id=task.tenant_id, kb_id=task.kb_id,
        )
        records = [r for r in ((readback or {}).get("chunks") or []) if isinstance(r, dict)]
        record_anchors: set = set()
        record_sanitized: set = set()
        record_contents: dict = {}
        for rec in records:
            anchor = _parse_anchor_kv(rec.get("file_path") or "").get("chunk")
            content = rec.get("content") or ""
            # 无锚记录也计入 record_sanitized（内容门全局语义，内容在场即证据）
            record_sanitized.add(_sanitize_for_dedupe(content))
            if not anchor:
                continue
            record_anchors.add(anchor)
            record_contents.setdefault(anchor, []).append(content)

        missing_anchors = sorted(a for a in require_anchor if a not in record_anchors)
        missing_contents = sorted(
            key[:48] for key, anchors in require_content.items()
            if key not in record_sanitized
            and not _fragments_match_content(key, anchors, record_contents)
        )
        missing_either = sorted(
            a for a, key in require_either.items()
            if a not in record_anchors and key not in record_sanitized
        )
        if missing_anchors or missing_contents or missing_either:
            raise RuntimeError(
                f"[jonex][A18] 读回校验失败：doc={document_id} "
                f"success 锚缺席 {len(missing_anchors)}/{len(require_anchor)}"
                f"（示例 {missing_anchors[:5]}）；"
                f"duplicated 内容不可读 {len(missing_contents)}/{len(require_content)}"
                f"（示例 {missing_contents[:3]}）；"
                f"未知状态未落地 {len(missing_either)}/{len(require_either)}"
                f"（示例 {missing_either[:5]}）；读回记录数={len(records)}"
            )

        # 5b. canary（T1-④ 实测「sanitize 后保真」⇒ 采纳；两侧均按注入前内容的
        # sanitize 后 strip 比对——读回通道剥 ns marker）
        canary_failures = []
        for anchor, contents in record_contents.items():
            expected = original_by_anchor.get(anchor)
            if expected is None or len(contents) != 1:
                continue
            if _sanitize_for_dedupe(contents[0]).strip() != _sanitize_for_dedupe(expected).strip():
                canary_failures.append(anchor)
        if canary_failures:
            raise RuntimeError(
                f"[jonex][A18] canary 校验失败：锚恰 1 条记录但内容与本地注入不一致，"
                f"doc={document_id} anchors={canary_failures[:5]}"
                "（疑似 rebuild 重叠锚旧内容残留）"
            )

    async def _run_ontology_only(self, task: TaskInfo, handle: TaskHandle) -> None:
        """[jonex] P0-A: ontology-only 执行 — 只重抽本体，跳过 parse/multimodal/push。

        直接按 document_id 从 LightRAG 读实体/关系做本体归类，不需要原文件，也不
        重新解析。要求任务携带 compiled schema（否则无法归类 → 失败）。

        [jonex] P0-b：表格上下文不重新解析而是回放首轮存档——重新解析既慢、又
        必然 miss 解析缓存（下载路径含随机 uuid、缓存键含绝对路径+mtime，见
        _table_context_dir 上方注释）；只有回放才能保证重抽与首轮同口径
        （即同样产出 Row-as-Object 表格对象）。
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

        # ── [jonex] A18：引擎前置段 —— 灌引擎全量 chunk + 等 + 读回校验（spec §4.3）──
        # 平台在 compile 步读全量随任务携带（engine_chunks）；这里灌入 LightRAG、
        # 等轨道终态、以读回为唯一裁决，通过后才继续既有流程（含存档回放）。
        # 任何失败一律 raise —— N6：补发 ingest_timing 后由 worker 的
        # `except Exception: _fail_task(...)` 落 FAILED + str(e)。
        engine_chunks = list(getattr(task, "engine_chunks", None) or [])
        if engine_chunks:
            try:
                await self._ingest_engine_chunks(task, engine_chunks)
            except Exception as e:
                _log_ingest_timing_v2(
                    task, "failed", force_ontology_only=True, error=str(e)[:500],
                )
                raise

        # 轻量 ctx：不解析原文件，_run_ontology_extraction 按 document_id 读
        # LightRAG chunk 原文；content_list 的表格子集由首轮存档回放（P0-b）。
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
        # [jonex] P0-b：标记重抽语义——_run_ontology_extraction 的 P0-b 闸门据
        # 此把「无表格上下文存档 + 本文档确有表格 chunk」判为拒绝（而非降级）。
        ctx.ontology_only = True

        # [jonex] P0-b：回放首轮落盘的表格上下文（详见 _table_context_dir 上方
        # 注释）。命中 → ctx.content_list 非空 → table_handled /
        # _merge_table_objects / P10 批 2.4 零产出兜底三条既有逻辑按与首轮相同
        # 的口径生效，重抽结果同样含 Row-as-Object 表格对象。
        # 空列表是有效值（首轮确认无表格），正常跑；None（无存档）不在这里
        # 裁决——交 _run_ontology_extraction 的 P0-b 闸门按 chunk 判据处理
        # （本文档确有表格 chunk → 拒绝重抽并落 FAILED；无表格 → 正常跑）。
        restored = _load_table_context(task)
        if restored is None:
            logger.info(
                "[jonex][P0-b] ontology_only 无表格上下文存档（上线前入库的老文档），"
                "交本体阶段按 chunk 判据裁决: doc=%s",
                task.document_id,
            )
        else:
            ctx.content_list = restored
            logger.info(
                "[jonex][P0-b] ontology_only 回放表格上下文 %d 条（首轮口径）: doc=%s",
                len(restored), task.document_id,
            )

        await self._run_ontology_extraction(
            task, ctx, task.document_id or "",
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
    ) -> None:
        """[jonex] P4：独立本体抽取——chunk 读回 + NativeOntologyExtractor。

        读取 LightRAG chunk 存储原文（§5.6 权威 chunk_id，禁止按 content
        重算）→ NativeOntologyExtractor 双层类型抽取（TBox 档 + 兜底粗类型
        档，§5.8）→ 两档全集 custom-kg 回写 LightRAG 图（§5.7）→ TBox 档
        写 Redis task 供 reconcile 落 PG/Neo4j（下游零改动）。

        旧链路（读 LightRAG 候选实体/关系 → 归类/定型）已被本函数替换（P5 清理）。
        """
        task.ontology_status = "extracting"
        # Push a timeline entry for the ontology stage
        task.timeline.append(StageTiming(
            stage="ontology_extract",
            label="本体抽取",
            detail="chunk 原文独立抽取 + 双层类型归类",
            started_at=datetime.now(timezone.utc),
        ))
        self._repo.save(task)

        try:
            kb_id = task.kb_id or ""

            # [jonex] P0-b：表格子集落盘已上移到「推完切片」处，见
            # _execute_pipeline_http。原因：解析与编译拆成两次任务后，首次任务
            # 可能不抽本体，落盘点必须早于本体阶段才不会被整段跳过
            # （否则 ontology_only 回放不到存档，含表格文档会被 P0-b 闸门判 FAILED）。

            # ① 读回 LightRAG chunk 存储原文（§5.6）。时序前提：常规链路
            # PushChunksStage 等 track 全部 processed 后才进本体阶段，
            # ontology_only 复用已入库 chunk——两种模式读回口径一致。
            try:
                chunks_resp = await self._http_client.get_document_chunks(
                    task.document_id or "",
                    tenant_id=task.tenant_id, kb_id=kb_id,
                )
            except Exception as e:
                logger.warning(
                    f"Ontology: chunk readback failed for {task.task_id}: {e}"
                )
                task.ontology_status = "failed"
                task.ontology_error = f"chunk 读回失败: {e}"[:500]
                return

            chunks = (
                chunks_resp.get("chunks") or []
                if isinstance(chunks_resp, dict)
                else []
            )
            if not chunks:
                task.ontology_status = "failed"
                task.ontology_error = (
                    "LightRAG chunk 存储中未找到该文档 chunk，无法抽取本体"
                )
                return

            # [jonex] P4 §8 跨文档判重缺口暴露：内容与先入文档完全相同的
            # chunk 判重后不重复入库（file_path 带先入文档锚点），按 doc_id
            # 读回时当前文档缺这些段。PushChunksStage 只透传判重计数、未透传
            # duplicated_indices，此处至少记 warning 暴露缺口（§8 风险表
            # 最后一行），不静默。
            dup_count = int(getattr(task, "duplicated_chunk_count", 0) or 0)
            if dup_count > 0:
                logger.warning(
                    "Ontology: doc=%s 有 %d 个 chunk 因跨文档判重未入库，"
                    "按 doc_id 读回缺这些段（读回 %d 个）——抽取输入可能缺段",
                    task.document_id, dup_count, len(chunks),
                )

            # ── [jonex] P0-b 闸门：无表格上下文存档 + 本文档确有表格 chunk
            # → **拒绝抽取**，不降级。──
            #
            # 为什么不降级跑：降级把表格当普通文本抽，产出通用实体（无结构化
            # attributes、无复合主键区分），而写图是 delete_by_document 全量替换
            # ——不是补齐缺失，是用差的换掉首轮的好结果；代价还是全部 chunk 进
            # LLM（本例 24212 chunk / 3027 批 ≈45 分钟）+ backfill 分钟级 CPU。
            # 拒绝则首轮结果一行不动：KB 侧见 ontology_status=failed 走
            # reconciliation 的「已完成但无 ontology_data」分支，最多重试
            # MAX_ONTOLOGY_RETRIES 次（秒级空转）后落 FAILED、把 ontology_error
            # 透到文档 error，**全程不调 _handle_completed、不碰 Neo4j**。
            #
            # 判据三重收敛，缺一不可：
            #   ctx.ontology_only——只有重抽才存在「全量替换掉首轮好结果」的
            #     问题；full 首轮入库 content_list 为 None 也无所谓（首次写图，
            #     没有可替换的东西），故不能误伤；
            #   ``is None`` 而非 falsy——空列表是「首轮确认无表格」的有效存档；
            #   chunk 侧检测——避免误伤纯文本文档（它没有表格 chunk，重抽本就
            #     不需要表格上下文）。
            from jonex_core.capability.atomic.rag.native_ontology_extractor import (
                NativeOntologyExtractor,
            )

            if ctx.ontology_only and ctx.content_list is None:
                table_chunk_count = sum(
                    1 for c in chunks if NativeOntologyExtractor._is_table_chunk(c)
                )
                if table_chunk_count:
                    task.ontology_status = "failed"
                    task.ontology_error = (
                        f"表格上下文存档缺失，本文档含 {table_chunk_count} 个表格 "
                        "chunk：重抽会以文本口径全量替换首轮由 Row-as-Object 建立"
                        "的表格对象，已拒绝。需 force_reparse 重建存档后重抽"
                    )[:500]
                    logger.error(
                        "[jonex][P0-b] 拒绝 ontology_only 重抽（无表格上下文存档）: "
                        "doc=%s 表格 chunk=%d ——首轮表格对象保留，需 force_reparse",
                        task.document_id, table_chunk_count,
                    )
                    return

            # ② Resolve compiled schema（native 抽取的 TBox 用 prompt_schema）
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
            tbox = (compiled_schema or {}).get("prompt_schema")

            # ③ NativeOntologyExtractor 独立抽取（§5.3 七步流程）。
            # tbox 不可解析 → extract 返回 ok=False 整体不抽取（§5.8 P1-4，
            # 不得「空集全丢弃」静默清空本体）。该 import 已上移到 P0-b 闸门处。
            # [jonex] P3a：KB 级本体抽取模型挂在 scope 上 —— 抽取的**四个** LLM
            # 出口（主抽取 / 表格补跑 / 表级判定 / 列名映射）都拿得到同一个值，
            # 其中后两个在 table_object_extractor 里、签名只有 scope 这一个通道。
            # 空串 = 没覆盖 ⇒ _openai_chat 回落 ONTOLOGY_LLM_MODEL。
            scope = {
                "tenant_id": task.tenant_id,
                "knowledge_base_id": kb_id,
                "document_id": task.document_id or "",
                "trace_id": task.task_id,
                "model": getattr(task, "ontology_model", "") or "",
            }

            # [jonex] P7 M2：procedural 每文档触发条件 = schema 含 Action
            # （总闸 ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED 在 extractor 侧按 env
            # AND 判定，§5 P2-12 gate 关系）。未声明 Action 的 KB 传 null 零开销。
            procedural_opt = None
            if tbox and any(
                (et.get("name") == "Action") for et in (tbox.get("entity_types") or [])
            ):
                procedural_opt = {}

            # [jonex] P5-var（方案 B）：仅当 Row-as-Object 已接管表格
            # （content_list 含 type=="table" item）时剔除表格 chunk；
            # 无接管（content_list 丢失）时表格 chunk 进 native 文本抽取
            # ——修本体召回，不重复映射。
            # [jonex] §table-llm-bypass：md 文档的表格不产出 type=="table"
            # item（parse_text 只发 text item），但 push 阶段已确定性识别并
            # 登记到 content_list item 的 _mapped_tables；两处任一命中即接管。
            # [jonex] P0-b：ontology_only 的 content_list 由首轮存档回放
            # （_load_table_context），故重抽同样落到「已接管」分支、口径与
            # 首轮一致。存档缺失（content_list is None）时不会走到这里——上面
            # 的 P0-b 闸门已按 chunk 判据裁决（有表格 chunk 即拒绝 return）；
            # 能到这里的 None 只可能是「无表格 chunk 的纯文本文档」，走「无
            # 接管」这一支本就正确。
            table_handled = any(
                (item or {}).get("type") == "table"
                or (item or {}).get("_mapped_tables")
                for item in (ctx.content_list or [])
            )
            result = await NativeOntologyExtractor().extract(
                chunks=chunks, tbox=tbox, scope=scope,
                options={
                    "procedural": procedural_opt,
                    "table_objects_handled": table_handled,
                },
            )
            # [jonex] P0-b（2026-09-13 OOM 复盘）：主抽取的返回值此前**完全未被
            # 检查**——ok=False（TBox 不可用、无可用 chunk 等）被静默吞掉，直接
            # 落到下面的表格映射与零产出兜底，日志上看不出「主抽取其实什么都没
            # 跑」。此处补一条带 task/doc 关联的告警（提取器内部另有一条无关联
            # 的明细告警，两条配合即可定位到具体任务）。
            if not result.ok:
                logger.warning(
                    "[jonex][P0-b] 主抽取未产出（ok=False）: task=%s doc=%s "
                    "chunks=%d errors=%s",
                    task.task_id, task.document_id, len(chunks),
                    str(result.errors[:1])[:200],
                )

            # ── [jonex] §table-grid-v2 步14：Row-as-Object 表格对象抽取 ──
            # ctx.content_list 可用（常规 insert 链路，或 P0-b 重抽回放）时，
            # 对表格做确定性映射（一行 = 一个对象，列名 = 属性），产出并入
            # 本体存储档（TBox 档）。content_list 为 None（上线前入库的老文档
            # 重抽且无存档）时走不到这里——上面 P0-b 闸门已拒绝或确认无表格
            # chunk，故此处的空值只代表「本文档确实没有表格」。
            # 失败降级：仅打日志，不影响 native 抽取结果。
            table_produced = False
            if ctx.content_list:
                try:
                    table_produced = await self._merge_table_objects(
                        ctx, scope, result, compiled_schema=compiled_schema,
                        # [jonex] S1b：本函数已把整 doc 的 chunk 读进内存
                        # （上方 ①，此处仍在同一作用域内、中间无重绑定），
                        # 传给行对象做「区间包含」证据回挂，零额外 IO。
                        chunks=chunks,
                    )
                except Exception as exc:  # noqa: BLE001 — 表格对象失败不阻断本体
                    logger.warning(
                        "Table objects extraction failed for %s: %s",
                        task.task_id, exc,
                    )

            # [jonex] P10 批 2.4：Row-as-Object 零产出兜底——table_handled=True
            # 时 native 主抽取已剔除表格 chunk；若确定性映射也零产出（无 pk、
            # 表头判定失败等），表格信息整体丢失。对被剔除的表格 chunk 补跑
            # 一次 native 抽取（table_objects_handled=False 使表格 chunk 进
            # 文本抽取），产出并入两档全集，随后 ⑤ 一并回写 LightRAG。
            # 补跑失败仅告警不阻断。
            #
            # [jonex] P0-a（2026-09-13 线上 OOM 复盘）：兜底必须设规模闸门。
            # 实证（financial_metric.md）：1438/1438 张表主键识别失败 →
            # table_produced=False；而该文档**全部** 46290 个 chunk 都是表格
            # chunk，于是「兜底集合 == 全量集合」，兜底退化成「把整篇文档原样
            # 喂回 LLM」——单次 46290 chunk / 5787 批 ≈ 4.6 小时，产出 19647
            # 实体 + 34522 关系，backfill 阶段把宿主内存吃到 23.7GB 被
            # oom-killer 杀掉（容器当时无 mem_limit，故 OOMKilled=false）。
            # 兜底的定位是「补回少量被误剔除的表格 chunk」，一旦逼近全量就不再
            # 是兜底，而是最坏情况的全量 LLM 抽取，必须显式拒绝。
            # 拒绝而非降级：写图是 delete_by_document 全量替换，放它跑完等于用
            # 「表格当普通文本」的差结果换掉首轮的好结果；拒绝则图一行不动
            # （与上方 P0-b 闸门同款收尾：ontology_status=failed + return，
            # KB 侧走 reconciliation「已完成但无 ontology_data」分支）。
            # 判据双闸，任一命中即拒绝：
            #   count——兜底集合绝对规模（本次 46290 > 2000，真正防 OOM 的那道）；
            #   ratio——兜底集合占全量 chunk 的比例（本次 1.0，最极端形态），
            #          需叠加规模下限才生效，避免误杀「整篇都是表」的小文档。
            # 判据与默认值集中在 _table_fallback_gate，单测见
            # tests/test_service/test_table_fallback_gate.py。
            if table_handled and not table_produced and chunks:
                table_chunks = [
                    c for c in chunks
                    if NativeOntologyExtractor._is_table_chunk(c)
                ]
                if table_chunks:
                    gate_ok, gate_detail = self._table_fallback_gate(
                        len(table_chunks), len(chunks),
                    )
                    if not gate_ok:
                        task.ontology_status = "failed"
                        task.ontology_error = (
                            f"表格确定性映射零产出，且待回补的表格 chunk 规模超闸门"
                            f"（{gate_detail}）：零产出兜底会退化为全量 LLM 抽取，"
                            "已拒绝。请拆分文档，或修正表格主键识别（per-KB 配置"
                            "主键列 / 整行 hash 兜底）后重抽"
                        )[:500]
                        logger.error(
                            "[jonex][P0-a] 拒绝零产出兜底（规模超闸门）: task=%s "
                            "doc=%s 表格 chunk=%s ——表格对象保留不动，"
                            "避免全量 LLM 抽取",
                            task.task_id, task.document_id, gate_detail,
                        )
                        return
                    try:
                        supplement = await NativeOntologyExtractor().extract(
                            chunks=table_chunks, tbox=tbox, scope=scope,
                            options={
                                "procedural": procedural_opt,
                                "table_objects_handled": False,
                            },
                        )
                        if supplement.ok:
                            result.entities.extend(supplement.entities)
                            result.relations.extend(supplement.relations)
                            result.ontology_entities.extend(
                                supplement.ontology_entities
                            )
                            result.ontology_relations.extend(
                                supplement.ontology_relations
                            )
                            result.ok = True
                            logger.info(
                                "Row-as-Object 零产出，表格 chunk 回补 "
                                "native 抽取 %d 实体 / %d 关系",
                                len(supplement.ontology_entities),
                                len(supplement.ontology_relations),
                            )
                        else:
                            logger.warning(
                                "表格 chunk 回补 native 抽取失败: %s",
                                supplement.errors,
                            )
                    except Exception as exc:  # noqa: BLE001 — 兜底失败不阻断
                        logger.warning(
                            "表格 chunk 回补 native 抽取异常: %s", exc,
                        )

            # ⑤ custom-kg 回写：两档全集回写 LightRAG 图（§5.7）。
            # 失败重试 1 次，仍失败本体链路照常成功（本体存储已落），
            # 由 retry_ontology_extract 重抽时再次回写。
            if result.entities or result.relations:
                await self._writeback_custom_kg(
                    task, kb_id, result, compiled_schema or {},
                )

            # ⑥ Redis task 落盘：本体存储侧只落 TBox 档（ontology_* 字段，
            # 与旧链路 _post_validate 后口径一致，下游 reconcile 零改动）。
            task.ontology_status = "completed" if result.ok else "failed"
            task.ontology_data = {
                "entities": [
                    self._serialize_entity(e) for e in result.ontology_entities
                ],
                "relations": [
                    self._serialize_relation(r)
                    for r in result.ontology_relations
                ],
            }
            task.ontology_error = (
                str(result.errors[:1]) if result.errors else ""
            )

            logger.info(
                f"Ontology extraction done for {task.task_id}: "
                f"status={task.ontology_status} "
                f"entities={len(result.ontology_entities)} "
                f"relations={len(result.ontology_relations)} "
                f"kg_entities={len(result.entities)} "
                f"kg_relations={len(result.relations)}"
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

    @staticmethod
    def _serialize_entity(e) -> dict:
        """[jonex] P4：本体实体 → Redis task dict（与旧链路字段逐一对齐）。"""
        return {
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

    @staticmethod
    def _serialize_relation(r) -> dict:
        """[jonex] P4：本体关系 → Redis task dict。

        缺陷修复：补齐 attributes / source_chunks（reconcile→Neo4j ONT_REL
        边出处与关系属性依赖它们，旧序列化两字段缺失）；keywords 一并透传
        （M3-e 观测）。
        """
        return {
            "source_name": r.source_name,
            "source_type": r.source_type,
            "target_name": r.target_name,
            "target_type": r.target_type,
            "relation_type": r.relation_type,
            "confidence": r.confidence,
            "source_chunks": r.source_chunks,
            "keywords": r.keywords,
            "attributes": r.attributes,
        }

    async def _writeback_custom_kg(
        self, task: TaskInfo, kb_id: str, result, compiled_schema: dict,
    ) -> bool:
        """[jonex] P4 §5.7：两档全集回写 LightRAG 图（POST /documents/custom-kg）。

        映射契约：source_id = §5.6 权威 chunk_id（多 chunk 用 ``<SEP>`` 拼接，
        与 LightRAG 自抽路径同构，禁止按 content 重算）；keywords = relation_type
        （TBox 内）或自由关键词（TBox 外）；description = compiled schema 该关系
        类型中文描述，TBox 外关系取 keywords 首项。失败重试 1 次；仍失败返回
        False（本体链路不受影响，retry_ontology_extract 重抽时再次回写）。
        """
        rel_desc = {
            str(rt.get("name") or ""): str(rt.get("description") or "")
            for rt in (compiled_schema.get("relation_types") or [])
            if rt.get("name")
        }

        def _source_of(source_chunks) -> tuple[str, str]:
            ids: list[str] = []
            fp = ""
            for sc in source_chunks or []:
                sid = str(sc.get("source_id") or "").strip()
                if sid:
                    ids.append(sid)
                    if not fp:
                        fp = str(sc.get("file_path") or "")
            # 保持插入序去重
            return "<SEP>".join(dict.fromkeys(ids)), fp

        entities = []
        for e in result.entities:
            sid, fp = _source_of(e.source_chunks)
            entities.append({
                "entity_name": e.canonical_name,
                "entity_type": e.entity_type,
                "description": e.description,
                "source_id": sid,
                "file_path": fp,
            })

        relationships = []
        for r in result.relations:
            sid, fp = _source_of(r.source_chunks)
            keywords = (r.relation_type or "").strip() or (r.keywords or "").strip()
            description = rel_desc.get(r.relation_type, "")
            if not description and r.keywords:
                description = str(r.keywords).split(",")[0].strip()
            relationships.append({
                "src_id": r.source_name,
                "tgt_id": r.target_name,
                "description": description,
                "keywords": keywords,
                "weight": r.confidence,
                "source_id": sid,
                "file_path": fp,
            })

        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                await self._http_client.insert_custom_kg(
                    entities, relationships,
                    tenant_id=task.tenant_id, kb_id=kb_id,
                )
                logger.info(
                    "custom-kg 回写成功 %s: entities=%d relationships=%d",
                    task.task_id, len(entities), len(relationships),
                )
                return True
            except Exception as exc:  # noqa: BLE001 — 回写失败不阻断本体
                last_exc = exc
                logger.warning(
                    "custom-kg 回写失败 %s (attempt %d/2): %s",
                    task.task_id, attempt + 1, exc,
                )
                if attempt == 0:
                    await asyncio.sleep(2)
        logger.warning(
            "custom-kg 回写最终失败 %s（本体链路不受影响，重抽时再回写）: %s",
            task.task_id, last_exc,
        )
        return False

    @staticmethod
    def _row_evidence_enabled() -> bool:
        """[jonex] S1b 开关：行对象 chunk 证据回挂（默认关，灰度）。

        关 = 改造前行为（行对象在图上贡献 0 引用）。
        """
        return os.getenv(
            "ONTOLOGY_ROW_EVIDENCE_ENABLED", "false"
        ).strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _row_evidence_cap() -> int:
        """行对象 ``source_chunks`` 总量上限（0 = 不限）。

        与 native 抽取的 ``ONTOLOGY_SOURCE_CHUNKS_MAX`` 同口径、同默认值：
        同一起因（2026-09-13 无上限的「命中即 append」把宿主内存吃到
        23.7GB 被 oom-killer 杀）——行对象同样是「一行命中多个 chunk」的
        放大点，必须受同一个配额约束。
        """
        try:
            return max(0, int(os.getenv("ONTOLOGY_SOURCE_CHUNKS_MAX", "32")))
        except (TypeError, ValueError):
            return 32

    @staticmethod
    def _table_fallback_gate(table_count: int, total_count: int) -> tuple[bool, str]:
        """[jonex] P0-a：零产出兜底的规模闸门。

        兜底的定位是「补回**少量**被误剔除的表格 chunk」。当待回补集合逼近全量
        （线上实证 46290/46290）时，兜底已等价于「整篇文档全量进 LLM」，必须
        拒绝——判据与实证详见调用处注释。

        双闸，任一命中即拒绝：
          - 绝对闸 ``ONTOLOGY_TABLE_FALLBACK_MAX_CHUNKS``（默认 2000）——
            兜底规模的硬上限，独立生效，是真正防 OOM 的那道；
          - 比例闸 ``ONTOLOGY_TABLE_FALLBACK_MAX_RATIO``（默认 0.5）——
            兜底占文档主导即失去兜底语义。**必须叠加规模下限**
            ``ONTOLOGY_TABLE_FALLBACK_RATIO_MIN_CHUNKS``（默认 200）才生效：
            小文档「整篇都是表」是常态（既有单测里就有 1 chunk 的表格文档），
            1 个 chunk 占 100% 也花不掉几个 LLM —— 只有规模大到值得警惕时，
            占比才有判据意义。无下限时比例闸会把这类正常小文档一并误杀。

        Returns:
            (allowed, detail)：allowed=False 时 detail 为超限明细，直接用于日志
            与 ``ontology_error``；True 时为当前占比，供日志记录。
        """
        ratio = table_count / max(1, total_count)
        try:
            max_ratio = float(
                os.getenv("ONTOLOGY_TABLE_FALLBACK_MAX_RATIO", "0.5")
            )
        except ValueError:
            max_ratio = 0.5
        try:
            max_chunks = int(
                os.getenv("ONTOLOGY_TABLE_FALLBACK_MAX_CHUNKS", "2000")
            )
        except ValueError:
            max_chunks = 2000
        try:
            ratio_min_chunks = int(
                os.getenv("ONTOLOGY_TABLE_FALLBACK_RATIO_MIN_CHUNKS", "200")
            )
        except ValueError:
            ratio_min_chunks = 200
        over_count = table_count > max_chunks
        over_ratio = ratio > max_ratio and table_count > ratio_min_chunks
        detail = (
            f"{table_count}/{total_count} = {ratio:.0%}"
            f"（上限 {max_ratio:.0%} / {max_chunks} 个，"
            f"比例闸下限 {ratio_min_chunks} 个）"
        )
        if over_count or over_ratio:
            return False, detail
        return True, detail

    async def _merge_table_objects(self, ctx, scope: dict, result,
                                   compiled_schema=None, chunks=None) -> bool:
        """[jonex] §table-grid-v2 步14：Row-as-Object 表格对象抽取。

        对 content_list 的每个 table item：重跑网格化（与 push 阶段同函数
        保证一致性）→ 表级判定（LLM/缓存/规则，含表头判定模型化）→ 列名
        →TBox 属性映射（§10.2.4 第二次 LLM）→ 行级对象实例 + 分组关系，
        并入本体存储档 ontology_entities/ontology_relations
        （extraction_method=row_as_object，attributes 结构化，不走 200
        截断）。表格对象不回写 LightRAG（方案 C 通道已入库）。

        Args:
            chunks: 本 doc 的 LightRAG chunk 列表（与上方 ① **同一批**物理
                对象）。S1b 用它给行对象回挂 chunk 引用（§6 S1b / §12.5）；
                缺省 None 表示不开启该步骤（回挂全部计入 miss）。

        Returns:
            bool: 是否有表格对象产出（P10 批 2.4 零产出兜底判定依据）。
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
            attach_row_chunk_evidence,
            index_row_chunks,
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
        # [jonex] P0-c 观测指标：主键质量。与 stages.py 的 tables_fallback
        # 并列写回 ctx.table_stats（`_execute_pipeline_http` 在抽取阶段之后
        # 会二次合并，见该方法内注释）。
        #   tables_pk_empty     —— 重判后仍无主键、走整行 hash 命名的表数
        #   tables_pk_from_llm  —— 主键由 LLM 重判得到的表数
        #   tables_pk_from_rules—— 主键由规则兜底得到的表数
        #   rows_row_hash       —— 整行 hash 命名的对象数
        tables_seen = 0
        objects_total = 0
        relations_total = 0
        pk_failed = 0
        pk_from_llm = 0
        pk_from_rules = 0
        rows_row_hash = 0
        # [jonex] 分组聚合节点实体化去重集：同一分组（列名·值）在多个表片段
        # 里会重复出现，只在首次登记实体，关系仍逐条保留。
        group_nodes: set = set()
        # [jonex] S1b（§6 S1b / §1.1 例外 / §12.5）：行对象 chunk 证据回挂。
        # 索引按 table_sig 归集本 doc 的 ctype=table_row chunk（含行区间），
        # 建一次、全部表片段复用。索引为空 = 降级场景（chunks 缺失，或
        # RAG_TABLE_GRID_V2=false 使 chunk 侧不写 table_sig → join 必然全
        # miss），按 §12.5 要求**打点降级**：记 warning + 计数，行对象退回
        # 无 chunk 引用，不报错、不阻断抽取。
        row_chunk_index: dict = {}
        if self._row_evidence_enabled():
            row_chunk_index = index_row_chunks(chunks or [])
            if not row_chunk_index:
                logger.warning(
                    "Row-as-Object S1b: 行级证据 join 无可用索引"
                    "（chunks=%d；chunk 侧 table_sig 仅在 RAG_TABLE_GRID_V2"
                    "=true 时写入）——行对象本轮退回无 chunk 引用，抽取继续",
                    len(chunks or []),
                )
        row_ev_matched = 0
        row_ev_added = 0
        row_ev_missed = 0
        # [jonex] §table-llm-bypass：除 MinerU/表格类 content_list 的
        # type=="table" item 外，一并消费 push 阶段登记的 _mapped_tables
        # （md/内嵌表格路径；只登记归一化成功的表，失败块已退回文本链路）。
        table_items: list = []
        for item in ctx.content_list or []:
            if (item or {}).get("type") == "table":
                table_items.append(item)
            table_items.extend((item or {}).get("_mapped_tables") or [])
        for item in table_items:
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

            # [jonex] P0-c（2026-09-13 线上 OOM 复盘）：主键为空时用**纠正后的
            # 表头**重判一次（LLM 主判 + 规则兜底），不再直接整表放弃。
            # 原实现中 `analyze_table` 只校验 header_row_indices，LLM 给出合法
            # 表头判定却给不出主键（pk_columns=[]）时照样按 source="llm" 放行
            # → 规则兜底被跳过 → 整表零对象 → 零产出兜底全量 LLM。
            # 重判后仍为空则退化为整行 hash 兜底命名（build_objects 内建），
            # 对象照落——「永不因主键缺失退回 LLM 全量抽取」。
            pk_source = ""
            if not verdict.pk_columns:
                pk_cols, pk_source = await extractor.resolve_pk_columns(
                    caption, header, data_rows, scope=scope,
                )
                if pk_cols:
                    verdict.pk_columns = pk_cols
                    logger.info(
                        "Row-as-Object: 表「%s」主键重判成功 → %s（source=%s）",
                        caption[:40], "、".join(pk_cols), pk_source,
                    )
                else:
                    pk_failed += 1
                    logger.warning(
                        "Row-as-Object: 表「%s」主键重判失败（LLM 与规则均无"
                        "可用主键），退化整行 hash 兜底命名，对象照常产出",
                        caption[:40],
                    )

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
            # [jonex] S1b：按「区间包含」把命中 chunk 追加进行对象
            # source_chunks（不是三元组相等——行对象单行、chunk 多行段）。
            if row_chunk_index:
                _m, _a, _mi = attach_row_chunk_evidence(
                    objects, row_chunk_index, max_chunks=self._row_evidence_cap(),
                )
                row_ev_matched += _m
                row_ev_added += _a
                row_ev_missed += _mi
            relations = extractor.build_group_relations(
                header, data_rows[:max_rows], objects, verdict,
            )
            # [jonex] row-to-table：每个行对象再连一条到所属表的 `包含` 边。
            # 与分组关系**并存**——没有分组列的表（实测大量参数表/方案表/字段
            # 说明表，`detect_group_column` 正确地返回 None）靠这条不再孤立。
            # 实测：本体作答时命中实体的 63% 度≤1，其中孤立 39 个里 79% 是行对象。
            # 表节点名只用 table_sig（不带 caption）——确定性优先；实机实测同一张表
            # 会出现带/不带 caption 两种条目，按 caption 命名必然分裂。
            relations += extractor.build_table_relations(
                objects, table_sig,
            )
            tables_seen += 1
            objects_total += len(objects)
            relations_total += len(relations)
            if pk_source == "llm":
                pk_from_llm += 1
            elif pk_source == "rules":
                pk_from_rules += 1
            rows_row_hash += sum(
                1 for o in objects if o.name_source == "row_hash"
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
                # [jonex] §group-node 分组聚合节点实体化：此前只产出边，源端点
                # 无实体 → 写图时 merge_relation 兜底建 stub（stub=True）→
                # count_relations 要求两端非 stub，分组关系永远统计为 0。
                key = (rel["source_name"], rel["source_type"])
                if key not in group_nodes:
                    group_nodes.add(key)
                    table_entities.append(ExtractedEntity(
                        canonical_name=rel["source_name"],
                        entity_type=rel["source_type"],
                        description="",
                        confidence=1.0,
                        # 不设 table_sig/pk_value：分组节点不是行对象，
                        # 按名走普通 upsert 键（同名跨表片段自然合并）
                        extraction_method="row_as_object",
                    ))
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

        # [jonex] P0-c：主键质量指标写回 ctx.table_stats（与 stages.py 的
        # tables_fallback / cols_unnamed 同表），并落一条汇总日志——指标是
        # 「留文档级告警」的判据：tables_pk_empty > 0 说明该文档仍有表格
        # 走整行 hash 命名（对象名不可读），需要补主键配置或拆分文档。
        if tables_seen:
            stats = getattr(ctx, "table_stats", None)
            if not isinstance(stats, dict):
                stats = {}
                try:
                    ctx.table_stats = stats
                except Exception:  # noqa: BLE001 — 观测指标不阻塞入库
                    pass
            stats["tables_pk_empty"] = stats.get("tables_pk_empty", 0) + pk_failed
            stats["tables_pk_from_llm"] = (
                stats.get("tables_pk_from_llm", 0) + pk_from_llm
            )
            stats["tables_pk_from_rules"] = (
                stats.get("tables_pk_from_rules", 0) + pk_from_rules
            )
            stats["rows_row_hash"] = stats.get("rows_row_hash", 0) + rows_row_hash
            # [jonex] S1b：行级证据回挂指标（§7 第 6 条验收 + §12.5 降级打点）。
            #   row_evidence_matched —— 至少回挂到 1 条 chunk 引用的行对象数；
            #   row_evidence_refs    —— 追加的 chunk 引用总条数；
            #   row_evidence_missed  —— 一条都没命中的行对象数。miss 恒等于
            #     对象总数说明 join 整体失效（table_sig 口径分叉 / grid-v2
            #     关闭），是排障的第一信号，故单列而非并入 matched。
            if self._row_evidence_enabled():
                stats["row_evidence_matched"] = (
                    stats.get("row_evidence_matched", 0) + row_ev_matched
                )
                stats["row_evidence_refs"] = (
                    stats.get("row_evidence_refs", 0) + row_ev_added
                )
                stats["row_evidence_missed"] = (
                    stats.get("row_evidence_missed", 0) + row_ev_missed
                )
            logger.info(
                "Row-as-Object 汇总: 表 %d 张 → 对象 %d 个 + 分组关系 %d 条；"
                "主键来源 llm=%d / rules=%d / 仍失败=%d（整行 hash 兜底 %d 行）",
                tables_seen, objects_total, relations_total,
                pk_from_llm, pk_from_rules, pk_failed, rows_row_hash,
            )
            if self._row_evidence_enabled():
                logger.info(
                    "Row-as-Object S1b 行级证据: 命中对象 %d / 追加引用 %d / "
                    "未命中对象 %d（共 %d 个对象）",
                    row_ev_matched, row_ev_added, row_ev_missed, objects_total,
                )

        if table_entities:
            # O6：同名去重——表格对象（结构化 attributes）优先于 native
            # 抽取文本实体；别名也占位（防止同名别名实体残留）。只作用于
            # 本体存储档（TBox 档）：LightRAG 侧表格实体已由方案 C 通道在
            # push 阶段入库，custom-kg 回写不含表格对象，同名合并由
            # LightRAG upsert 语义处理。
            table_names = set()
            for e in table_entities:
                table_names.add(e.canonical_name)
                table_names.update(e.aliases)
            result.ontology_entities = [
                e for e in result.ontology_entities
                if not (
                    e.extraction_method != "row_as_object"
                    and (
                        e.canonical_name in table_names
                        or any(a in table_names for a in e.aliases)
                    )
                )
            ]
            result.ontology_entities.extend(table_entities)
            result.ontology_relations.extend(table_relations)
            # 表格对象保证 ok（与 native 零实体互不依赖）
            result.ok = True
        return bool(table_entities)

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
