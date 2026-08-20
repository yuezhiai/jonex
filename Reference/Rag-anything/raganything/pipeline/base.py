"""Pipeline stage abstract base class."""

import asyncio
import dataclasses
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from raganything.pipeline_mode import PipelineMode


# ── Data layer ────────────────────────────────────────────────────────

@dataclass
class PipelineContext:
    """Pure data carrier — stages return StageResult rather than mutate this."""

    file_path: str
    file_name: str = ""
    mode: PipelineMode = PipelineMode.STANDALONE
    content_list: Optional[List[Dict[str, Any]]] = None
    doc_id: Optional[str] = None
    multimodal_items: Optional[List[Dict[str, Any]]] = None
    multimodal_results: Optional[List[Dict[str, Any]]] = None
    chunk_results: Optional[List[Any]] = None
    error: Optional[str] = None

    # Control flags
    cancel_event: Optional[asyncio.Event] = None
    force_reparse: bool = False

    # ── per-task parser / config (preset 链路) ──
    parser_type: str = ""
    config_snapshot: dict = field(default_factory=dict)

    # ── [jonex] 主解析提示词覆盖（逐任务隔离，禁止挂共享 processor 实例）──
    # PromptOverride（by_code dict）；由 task_manager 按 task.prompt_ids 解析后注入，
    # MultimodalStage 显式透传给各 processor 的 generate_description_only。
    prompt_overrides: Optional[Any] = None

    # v2 HTTP mode: per-task context
    tenant_id: str = ""
    kb_id: str = ""
    # [jonex] 方案 C：MPS 视频分析 COS URL（与本地 file_path 并行保留）
    mps_video_url: str = ""
    # KB 侧文档 id（KnowledgeDocument.id）。作为 file_source 的 doc= 锚点，
    # KB 所有按 document_id 的过滤（图谱视图 / 本体抽取 / 删除）都依赖它。
    # 与 doc_id 分开：doc_id 会被 ParseStage 覆盖为解析内容哈希，不能用作锚点。
    document_id: str = ""
    collected_doc_ids: list[str] = field(default_factory=list)
    pending_track_ids: list[str] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: float = 0.0

    # ── [jonex] chunk push outcome counters (§4/§5) ──
    total_chunk_count: int = 0
    failed_chunk_count: int = 0
    timeout_chunk_count: int = 0
    duplicated_chunk_count: int = 0
    total_pushed_count: int = 0

    # ── [jonex] §table-grid-v2 O4: 表格处理可观测统计 ──
    # tables_total / tables_normalized / tables_fallback / rows_total /
    # cols_unnamed / oversize_table_chunks；由 PushChunksStage 收集，
    # task_manager 汇总进 result_summary.extensions["table_stats"]。
    table_stats: dict = field(default_factory=dict)

    # ── [jonex] §image-refs P1-3: 资产上传结果 {image_idx: ext} ──
    # AssetUploadStage 产出，PushChunksStage._collect_multimodal_chunks 据此
    # 在图片 chunk 的 file_source 写 aext=（上传失败的图片不写，检索侧据此
    # 判定无 URL 可取）。
    asset_exts: dict = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        if self.completed_at > 0 and self.started_at > 0:
            return self.completed_at - self.started_at
        return 0.0

    def get_file_reference(self, use_full_path: bool) -> str:
        if use_full_path:
            return str(self.file_path)
        return os.path.basename(self.file_path)


# ── Dependency layer ──────────────────────────────────────────────────

@dataclass
class PipelineServices:
    """Read-only dependencies available to all stages."""

    config: Any
    lightrag: Any
    doc_parser: Any
    modal_processors: Dict[str, Any] = dataclasses.field(default_factory=dict)
    parse_cache: Optional[Any] = None
    doc_status_mgr: Optional[Any] = None
    callback_manager: Optional[Any] = None
    event_bus: Optional[Any] = None
    logger: Any = None
    # ── v2 HTTP mode ──
    http_client: Any = None  # HttpLightRagClient | None


# ── Stage result ──────────────────────────────────────────────────────

@dataclass
class StageResult:
    """Immutable output of a single stage.  Only non-None fields overwrite context."""

    content_list: Optional[List[Dict[str, Any]]] = None
    doc_id: Optional[str] = None
    multimodal_items: Optional[List[Dict[str, Any]]] = None
    multimodal_results: Optional[List[Dict[str, Any]]] = None
    chunk_results: Optional[List[Any]] = None
    error: Optional[str] = None
    # [jonex] §image-refs P1-3: 资产上传结果 {image_idx: ext}（key 为 int 枚举序号）
    asset_exts: Optional[Dict[int, str]] = None


# ── Pipeline result ───────────────────────────────────────────────────

@dataclass
class PipelineResult:
    success: bool
    doc_id: Optional[str] = None
    error: Optional[str] = None
    status: str = "success"
    final_ctx: Optional[Any] = None  # PipelineContext after all stages applied


# ── Stage base ────────────────────────────────────────────────────────

class Stage(ABC):
    can_run_on_error: bool = False

    @abstractmethod
    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        """Execute this stage. Returns a StageResult to merge into context."""


class EmptyStage(Stage):
    """No-op stage — placeholder for PipelineBuilder insert points."""

    def __init__(self, name: str = ""):
        self._name = name

    async def execute(
        self, ctx: PipelineContext, services: PipelineServices
    ) -> StageResult:
        return StageResult()


def merge_context(ctx: PipelineContext, result: StageResult) -> PipelineContext:
    """Return a new PipelineContext with non-None StageResult fields applied."""
    updates = {k: v for k, v in vars(result).items() if v is not None}
    return dataclasses.replace(ctx, **updates)
