"""
Pydantic models for RAGAnything Service API.

Covers: TaskInfo, ErrorCode, ProgressDetail, ResultSummary, request/response schemas.
Compliant with spec v1.0 §3, §4, §6.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────


class TaskStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCode(str, Enum):
    """Structured error codes — callers can automate recovery based on these."""

    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    PRESET_NOT_FOUND = "PRESET_NOT_FOUND"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    VLM_TIMEOUT = "VLM_TIMEOUT"
    ASR_TIMEOUT = "ASR_TIMEOUT"
    LIGHTRAG_ERROR = "LIGHTRAG_ERROR"
    PARSER_ERROR = "PARSER_ERROR"
    CONFIG_INVALID = "CONFIG_INVALID"
    # [jonex] R7-2b：COS 对象下载失败（FetchObjectStage）
    OBJECT_FETCH_FAILED = "OBJECT_FETCH_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNKNOWN = "UNKNOWN"


class FileType(str, Enum):
    VIDEO = "video"
    DOCUMENT = "document"
    AUDIO = "audio"
    IMAGE = "image"


class TaskMode(str, Enum):
    INLINE = "inline"
    YAML = "yaml"
    PRESET = "preset"


# ── Progress ────────────────────────────────────────────────────────────


class ProgressDetail(BaseModel):
    current: int = 0
    total: int = 0
    unit: str = ""  # "frame" | "segment" | "chunk" | "document" | "second"
    elapsed_seconds: float = 0.0
    eta_seconds: float = -1.0  # -1 = cannot estimate
    step_name: str = ""  # validate | parse | asr | vlm | extract | merge | done
    step_detail: str = ""  # "VLM frame 5/8 described"


class StageTiming(BaseModel):
    """Wall-clock timing for a single processing stage in the task timeline."""
    stage: str = ""          # "created" | "parse" | "text_insert" | "multimodal" | "done"
    label: str = ""          # human-readable, e.g. "文档解析"
    detail: str = ""         # extra context, e.g. "MinerU 在线解析 (上传→轮询→下载zip)"
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    elapsed_seconds: float = 0.0


# ── ResultSummary ────────────────────────────────────────────────────────


class ResultSummary(BaseModel):
    doc_id: str = ""
    entities: int = 0
    relations: int = 0
    chunks: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0
    frames: int = 0
    asr_segments: int = 0
    duration_seconds: float = 0.0
    tokens_used: int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)
    # ── block-level stats ──
    blocks: int = 0
    text_blocks: int = 0
    table_blocks: int = 0
    code_blocks: int = 0


# ── Storage info ─────────────────────────────────────────────────────────

class StorageInfo(BaseModel):
    """Storage location info for parsed artifacts (surfaced in API/webhook)."""
    root: str = ""
    mineru_dir: Optional[str] = None
    video_dir: Optional[str] = None
    asset_base_url: str = ""
    latest_url: Optional[str] = None
    files_count: int = 0
    total_size_bytes: int = 0


# ── TaskInfo ─────────────────────────────────────────────────────────────


class TaskInfo(BaseModel):
    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    tenant_id: str
    name: str  # display name (file basename)
    file_path: str
    file_type: FileType = FileType.DOCUMENT
    file_size_bytes: int = 0
    status: TaskStatus = TaskStatus.CREATED
    progress: float = 0.0  # 0.0 ~ 1.0
    progress_detail: Optional[ProgressDetail] = None
    current_step: str = ""  # validate | parse | asr | vlm | extract | merge | done
    worker_id: Optional[str] = None  # "worker-{hostname}-{index}"
    attempt: int = 1  # V1: always 1, reserved for retry
    error_code: Optional[ErrorCode] = None
    error_message: Optional[str] = None
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    preset_name: Optional[str] = None
    preset_version: Optional[str] = None
    prompt_ids: list[str] = Field(default_factory=list)
    webhook_url: Optional[str] = None
    webhook_delivered: bool = False
    idempotency_key: Optional[str] = None

    # Timestamps — all UTC, ISO 8601
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    result_summary: Optional[ResultSummary] = None
    timeline: list[StageTiming] = Field(default_factory=list)

    # ── KB context + storage ──
    kb_id: str = ""
    setting_id: str = ""
    file_hash: str = ""
    storage: Optional[StorageInfo] = None

    # ── KB integration fields ──
    document_id: str = ""
    storage_backend: str = "local"
    storage_key: str = ""
    # [jonex] 方案 C：MPS 视频分析专用 COS URL（与本地 file_path 并行保留）
    mps_video_url: str = ""

    # ── Ontology ──
    ontology_schema: Optional[dict] = None
    ontology_status: str = ""       # "" | pending | extracting | completed | failed
    ontology_data: Optional[dict] = None
    ontology_error: str = ""

    # ── HTTP mode track tracking ──
    lightrag_doc_ids: list[str] = Field(default_factory=list)
    pending_track_ids: list[str] = Field(default_factory=list)
    failed_chunk_count: int = 0
    total_chunk_count: int = 0
    # [jonex] #6: timeout / duplicated classification for KB reconciliation
    timeout_chunk_count: int = 0
    duplicated_chunk_count: int = 0
    total_pushed_count: int = 0

    # ── Reparse / recompile execution control ──
    # execution_mode: full（完整解析）| ontology_only（只重抽本体，跳过 parse/push/文件校验）
    #               | reparse_strict（严格全量替换：全部 chunk 成功 + 失败补偿 + 差集删旧）
    execution_mode: str = "full"
    content_generation: int = 0        # 携带 KB 文档 reparse 代次，回传对账做 fencing
    schema_version: int = 0            # 本任务使用的 compiled schema 版本
    schema_hash: str = ""              # 版本号兜底比对
    strict_push: bool = False          # 严格推送模式（全量成功才算成功 + 失败补偿）
    old_rag_doc_ids: list[str] = Field(default_factory=list)       # reparse 旧 doc 快照
    new_rag_doc_ids: list[str] = Field(default_factory=list)       # 本次成功推送的新 doc
    delete_pending_ids: list[str] = Field(default_factory=list)    # cleanup 待删旧 doc（old−new）
    compensate_pending_ids: list[str] = Field(default_factory=list)  # 失败补偿待删新 doc（new−old）
    cleanup_total: int = Field(default=0)  # [jonex] 3-A：进入 cleanup 时的初始待删总量，供 KB 动态超时


# ── Request schemas ──────────────────────────────────────────────────────


class CreateTaskRequest(BaseModel):
    """POST /api/v1/tasks — all three modes share this schema."""

    mode: TaskMode = TaskMode.INLINE
    file_path: str

    # mode=inline fields (all optional for yaml/preset override)
    llm: Optional[str] = None
    embedding: Optional[str] = None
    vision: Optional[str] = None
    vlm: Optional[str] = None
    asr: Optional[str] = None
    parser: Optional[str] = None
    modalities: list[str] = Field(default_factory=lambda: ["video", "audio"])
    output_dir: Optional[str] = None
    profile: Optional[str] = None
    lightrag_url: Optional[str] = None
    webhook_url: Optional[str] = None
    kb_id: str = ""
    setting_id: str = ""

    # Model connection overrides (host / api_key per model type)
    llm_host: Optional[str] = None
    llm_api_key: Optional[str] = None
    vlm_host: Optional[str] = None
    vlm_api_key: Optional[str] = None
    embedding_host: Optional[str] = None
    embedding_api_key: Optional[str] = None

    # mode=yaml
    yaml_path: Optional[str] = None

    # mode=preset
    preset: Optional[str] = None

    # Prompt override — reference prompt config IDs to use
    prompt_ids: list[str] = Field(default_factory=list)

    # Advanced overrides
    video_max_frames: Optional[int] = None
    video_keyframe_interval: Optional[int] = None
    force_reparse: bool = False  # skip parse cache, force full re-parse

    # ── KB integration fields ──
    knowledge_base_id: Optional[str] = None   # 对外字段名 → 映射到 TaskInfo.kb_id
    document_id: Optional[str] = None
    storage_backend: str = "local"
    storage_key: Optional[str] = None
    # [jonex] 方案 C：MPS 视频分析专用 COS URL
    mps_video_url: Optional[str] = None

    # ── Ontology ──
    ontology_schema: Optional[dict] = None

    # ── Reparse / recompile execution control ──
    execution_mode: str = "full"       # full | ontology_only | reparse_strict
    content_generation: int = 0        # reparse 代次
    schema_version: int = 0            # compiled schema 版本
    schema_hash: str = ""              # 版本号兜底
    strict_push: bool = False          # 严格推送模式
    force_retry: bool = False          # 允许对 completed 幂等任务新建 attempt


class CreateTaskResponse(BaseModel):
    task_id: str
    tenant_id: str
    status: TaskStatus
    created_at: datetime


# ── Chunk list / Export schemas ───────────────────────────────────────


class ChunkItem(BaseModel):
    """A single text chunk with positional/timeline metadata."""
    chunk_id: str = ""
    content: str = ""
    full_doc_id: str = ""
    tokens: int = 0
    chunk_order_index: int = 0
    file_path: str = ""
    page_idx: Optional[int] = None
    text_idx: Optional[int] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None


class ChunkListData(BaseModel):
    doc_id: str = ""
    total: int = 0
    chunks: list[ChunkItem] = Field(default_factory=list)


class ExportData(BaseModel):
    """Aggregated export report for a parsed document."""
    doc_id: str = ""
    file_name: str = ""
    file_type: str = ""
    full_text: str = ""
    total_chunks: int = 0
    total_entities: int = 0
    total_relations: int = 0
    chunks: list[dict] = Field(default_factory=list)
    entities: list[dict] = Field(default_factory=list)
    relations: list[dict] = Field(default_factory=list)


class TaskListQuery(BaseModel):
    status: str = "all"      # all | active | created | queued | processing | completed | failed | cancelled
    file_type: str = "all"   # all | video | document | audio | image
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    sort: str = "-created_at"  # created_at | -created_at


class TaskListItem(BaseModel):
    """Reduced task info for list view."""
    task_id: str
    name: str
    file_type: FileType
    status: TaskStatus
    progress: float
    progress_detail: Optional[ProgressDetail] = None
    worker_id: Optional[str] = None
    created_at: datetime


class PaginatedTasks(BaseModel):
    total: int
    page: int
    page_size: int
    tasks: list[TaskListItem]


class CancelTaskResponse(BaseModel):
    task_id: str
    previous_status: TaskStatus
    status: TaskStatus
    http_code: int  # 200 or 202


# ── Preset schemas ───────────────────────────────────────────────────────


class PresetMeta(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0"
    updated_at: Optional[datetime] = None
    updated_by: str = "anonymous"


class PresetConfig(BaseModel):
    description: str = ""
    version: str = "1.0"
    config: dict[str, Any] = Field(default_factory=dict)


class PresetListItem(BaseModel):
    name: str
    description: str
    version: str
    updated_at: Optional[datetime] = None


# ── Error response ───────────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    code: int
    request_id: str
    message: str
    data: Optional[dict[str, Any]] = None


# ── Health ───────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str  # "ok"


class ReadyResponse(BaseModel):
    status: str  # "ready" | "not_ready"
    reason: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────


# Allowed state transitions (Spec §2.2)
STATE_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED:    {TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.QUEUED:     {TaskStatus.PROCESSING, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.PROCESSING: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED:  set(),  # terminal
    TaskStatus.FAILED:     set(),  # terminal
    TaskStatus.CANCELLED:  set(),  # terminal
}

TERMINAL_STATES: set[TaskStatus] = {
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED
}


def validate_transition(current: TaskStatus, target: TaskStatus) -> bool:
    """Check if a state transition is allowed."""
    return target in STATE_TRANSITIONS.get(current, set())


def infer_file_type(file_path: str) -> FileType:
    """Infer FileType from extension."""
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    video_exts = {"mp4", "avi", "mov", "mkv", "webm", "flv", "wmv"}
    audio_exts = {"wav", "mp3", "flac", "aac", "ogg", "m4a", "wma"}
    image_exts = {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"}
    document_exts = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "md"}

    if ext in video_exts:
        return FileType.VIDEO
    if ext in audio_exts:
        return FileType.AUDIO
    if ext in image_exts:
        return FileType.IMAGE
    if ext in document_exts:
        return FileType.DOCUMENT
    return FileType.DOCUMENT  # default fallback


# ── Chunk write-back schemas ────────────────────────────────────────────

from dataclasses import dataclass


@dataclass
class UpdateChunkResult:
    """Result of a chunk content update operation."""

    old_chunk_id: str
    new_chunk_id: str
    doc_id: str
    tokens: int
    content_length: int
    updated_at: str                     # ISO-8601 timestamp
    vector_updated: bool                # True = vdb write succeeded
    partial_failure: str | None = None  # vdb error detail if vector_updated=False
    affected_entities: int = 0          # count of entity/relation_chunks records rewritten

    def to_dict(self) -> dict:
        return {
            "old_chunk_id": self.old_chunk_id,
            "new_chunk_id": self.new_chunk_id,
            "doc_id": self.doc_id,
            "tokens": self.tokens,
            "content_length": self.content_length,
            "updated_at": self.updated_at,
            "vector_updated": self.vector_updated,
        }
