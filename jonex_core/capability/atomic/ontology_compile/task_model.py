# -*- coding: utf-8 -*-
"""[jonex] 编译服务的任务模型（P2 自带，替代 raganything 宿主 TaskInfo）。

字段集 = `deps.py` 写死的 15 个消费字段 + 落库/失败路径需要的时间戳与错误字段
+ `content_generation`（提交时的文档代次，KB 对账的代次栅栏要读它）。
枚举与 raganything `service/models.py` 的 str-Enum **值逐字一致**（下游按值比较），
此处复制一份是为了满足红线：本包不得 import raganything。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional

from pydantic.v1 import BaseModel, Field


class TaskStatus(str, Enum):
    """对齐 raganything `models.TaskStatus`（值逐字一致）。"""
    CREATED = "created"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCode(str, Enum):
    """本包只用得上 CONFIG_INVALID，其余按需补。值对齐 raganything `models.ErrorCode`。"""
    CONFIG_INVALID = "CONFIG_INVALID"
    LIGHTRAG_ERROR = "LIGHTRAG_ERROR"
    TASK_CANCELLED = "TASK_CANCELLED"


class TaskCancelledError(Exception):
    """cancel_event 置位时抛出。对齐 raganything `TaskCancelledError`。"""


# 便捷常量（`deps.status_completed` / `deps.error_config_invalid` 的包内默认值）
STATUS_COMPLETED = TaskStatus.COMPLETED
ERROR_CONFIG_INVALID = ErrorCode.CONFIG_INVALID


class StageTiming(BaseModel):
    """单个处理阶段的时间线元素。字段与 raganything `models.StageTiming` 逐字一致。"""
    stage: str = ""
    label: str = ""
    detail: str = ""
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    elapsed_seconds: float = 0.0


class CompileTask(BaseModel):
    """编译任务。duck-type 契约（`deps.py`）的显式化。"""

    # ── 契约字段（15 个，一个都不能少）──
    task_id: str
    tenant_id: str
    kb_id: str = ""
    document_id: str = ""
    name: str = ""
    file_path: str = ""
    current_step: str = ""
    progress: float = 0.0
    status: Any = TaskStatus.CREATED          # 允许枚举或字符串（str-Enum 两者相等）
    config_snapshot: dict = Field(default_factory=dict)
    ontology_schema: Optional[dict] = None
    ontology_status: str = ""
    ontology_data: Optional[dict] = None
    ontology_error: str = ""
    # ⚠️ 必须是 List[StageTiming] 而非裸 `list`：`save→get` 往返走 `.dict()` / 构造，
    # 裸 list 会让元素退化成 dict，续跑时 extract.py:353-359（**在 finally 里**）的
    # `last.stage` / `last.ended_at` 会 AttributeError —— 且会替换掉原异常、跳过下一次 save。
    timeline: List[StageTiming] = Field(default_factory=list)

    # ── 失败/落库路径需要（对齐 TaskInfo 语义）──
    error_code: Any = None
    error_message: Optional[str] = None
    # A18 过渡通道：平台搬运 engine_chunks（P4 后删除）
    engine_chunks: Optional[list] = None

    # ── 代次（fencing）──
    # **提交时**记下的文档代次，随 `get_compile_status` 原样回显。KB 对账的
    # `_handle_completed` 第一件事就是代次栅栏：拿 `status_info["content_generation"]`
    # 与 `doc.content_generation` 比，`task < doc` 判「旧结果」——此时接管行在途
    # （编译中恒在途）就 `return "skipped"`，**文档停在 EXTRACTING 永不 READY**。
    # 缺键 ⇒ 0 ⇒ 凡是被重新解析过的文档（`doc.content_generation >= 1`）**必然**命中
    # 这个栅栏 —— 而全新上传的文档（gen=0）看不出来，正是它危险的地方。
    # ⚠️ **不能**在读取侧现取 `doc.content_generation` 顶上：那会让「文档在编译期间又被
    # 重解析」的陈旧结果也判成同代、被照常应用 —— 代次必须冻结在提交那一刻（rag 链的
    # `retry_ontology_extract(content_generation=…)` 就是这个形态）。
    content_generation: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    class Config:
        # 搬迁代码会往 task 上挂额外属性（如 delete_pending_ids），不该被拒
        extra = "allow"
        arbitrary_types_allowed = True
