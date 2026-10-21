"""平台监控 · 外部调用明细实体（monitor schema）

两张 append-only 事件表（对齐「数据运营」范式）：

- ``monitor.call_log``     N1 逻辑调用 —— 一行 = 一次逻辑调用，``request_id`` 本表内唯一
- ``monitor.call_attempt`` N2 物理 Attempt —— 一行 = 一次物理尝试，``request_id`` 可重复

⚠️ **本文件住在共享内核（`jonex_core/common/`）而非 platform 能力内，是刻意的**：
采集点分散在 Sidecar / llm-gateway / 各 capability **多个进程**，写法是**各进程各自实例化
采集器、直写同一个 PG 的 monitor schema**。
若模型留在 `capabilities/platform/models/`，别的进程 import 它就构成跨能力依赖
（`no-cross-capability-imports`）。同形先例：`metering.llm_usage_log` 的 DDL 由 platform
迁移建立，写入方却是 llm-gateway 进程（`recorder.py`）。

**表的所有权不变**：DDL 仍由 platform 的迁移（`002_platform.sql` 基线 + `update/048` 增量）
建立；对外读口（C1–C9 九个接口）仍在 platform。共享内核只共享**写入所需的结构**。

时间列名为 ``logical_started_at`` / ``final_ended_at`` / ``attempt_no``；两表均**不建** ``space_id``。

**不套用的 mixin 及理由**：

- ``SoftDeleteMixin`` → 不套：调用明细是 append-only 事件流，无软删语义。
- ``AuditMixin`` → 不套：明细由采集链路（系统进程）写入，无操作者身份；人工操作审计归属
  ``platform.audit_logs``，与调用明细不是同一件事。

**时间列**：DDL 用 ``TIMESTAMPTZ``，故 ORM 侧一律 ``DateTime(timezone=True)``，配套写入
tz-aware 的 ``datetime.now(timezone.utc)``（朴素值与 TIMESTAMPTZ 混用会踩 asyncpg 编解码坑）。
``created_at`` / ``updated_at`` 交给 DB 的 ``DEFAULT NOW()``，采集侧不必赋值。
"""

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin


def _utcnow() -> datetime:
    """tz-aware 当前时刻；async ORM 里禁 func.now()（MissingGreenlet 陷阱）。"""
    return datetime.now(timezone.utc)


class CallDirection(str, Enum):
    """调用方向。**取值集待评审**，当前仅 outbound/inbound。"""

    OUTBOUND = "outbound"
    INBOUND = "inbound"


class CallFinalStatus(str, Enum):
    """逻辑调用终态（五态）。**与既有枚举的映射待评审**。"""

    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RUNNING = "running"
    UNKNOWN = "unknown"


#: N2 物理 Attempt 状态 —— **与 N1 复用同一套五态**。
#:
#: 直接复用 N1 的 :class:`CallFinalStatus`：**一个枚举承载两表状态**，杜绝两边
#: 取值漂移；是别名而非新类 —— ``AttemptStatus.SUCCESS is CallFinalStatus.SUCCESS``。
#:
#: DDL 侧 ``monitor.call_attempt.status`` **有意不写 CHECK**，故本次加值**不需要迁移**。
AttemptStatus = CallFinalStatus


class Protocol(str, Enum):
    """调用方式 / 协议。**取值集未定** —— 仅列出已知取值，DDL 侧不写 CHECK。"""

    INVOKE = "invoke"
    MCP = "mcp"
    HTTP = "http"
    # 平台调大模型走 OpenAI 标准协议（llm-gateway 出站调用）。
    # 与「能力接口 / MCP / 网页接口」并列的第四种叫法。
    OPENAI = "openai"


class CallTrigger(str, Enum):
    """调用触发方 —— 「用户直接发起」还是「系统后台发起」。

    「调用记录」列表按此维度筛选（两类**都记**，靠前端筛选区分）。
    判定由采集侧按场景标签自动分类（见
    :func:`monitor_collector.classify_trigger_by_scene`），
    不要求每个调用点自己声明。DDL 侧写 CHECK（取值集已定，防脏数据混入历史）。
    """

    USER = "user"
    SYSTEM = "system"


class CallLog(TenantMixin, Base):
    """N1 逻辑调用表。"""

    __tablename__ = "call_log"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_monitor_call_log_request_id"),
        {"schema": "monitor"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    request_id = Column(String(64), nullable=False)
    trace_id = Column(String(64))
    direction = Column(String(16), nullable=False)
    source = Column(String(128))
    target = Column(String(128))
    protocol = Column(String(32))
    # 触发方：user（用户直接发起）/ system（系统后台批量，如入库向量化）。
    # 「调用记录」列表按此列筛选；采集侧按场景标签自动分类，DDL 侧写 CHECK。
    trigger_type = Column(
        String(16), nullable=False, default="user", server_default=text("'user'")
    )

    # 逻辑调用起止（列名已冻结，采集与查询两侧共用）
    logical_started_at = Column(DateTime(timezone=True))
    final_ended_at = Column(DateTime(timezone=True))

    final_status = Column(String(16), nullable=False)
    error_code = Column(String(64))
    error_message = Column(Text)

    # 物理尝试次数；供 retry_count = max(attempt_count - 1, 0) 推导。
    # 采集侧按 N2 行数补正（单调收敛，见 monitor_collector）。
    # ⚠️ **本表事后允许更新的列不止这一列**：同 request_id 的重复投递走 UPSERT，
    # 会把 final_status / final_ended_at / error_code / error_message / updated_at
    # 一并覆盖成**后到那次**的结局（跨进程重试的终态收敛，见 monitor_collector._sync_write）。
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=text("NOW()"),
    )


class CallAttempt(TenantMixin, Base):
    """N2 物理 Attempt 表（N1 的 1:N 明细，降级模式取数表）。"""

    __tablename__ = "call_attempt"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_monitor_call_attempt_id"),
        UniqueConstraint("request_id", "attempt_no", name="uq_monitor_call_attempt_req_no"),
        {"schema": "monitor"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id = Column(String(64), nullable=False)
    request_id = Column(String(64), nullable=False, index=True)
    # 尝试序号（列名已冻结为 attempt_no，不采用 attempt）
    attempt_no = Column(Integer, nullable=False)

    # Attempt 级起止（列名已冻结，采集与查询两侧共用）
    attempt_started_at = Column(DateTime(timezone=True))
    attempt_ended_at = Column(DateTime(timezone=True))

    status = Column(String(16), nullable=False)
    error_code = Column(String(64))
    error_message = Column(Text)

    is_final = Column(Boolean, nullable=False, default=False, server_default=text("FALSE"))
    wait_before_ms = Column(Integer)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=text("NOW()"),
    )


class LlmUsageLog(TenantMixin, Base):
    """LLM 网关计量明细表（``metering.llm_usage_log``）只读映射。

    列定义逐条对齐 ``deploy/postgres/migrations/002_platform.sql:377-402`` 的基线 DDL：

    * ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens`` / ``is_stream`` /
      ``is_estimated`` / ``created_at`` 在 DDL 里**都没有 NOT NULL**，故 ORM 侧
      一律**不写** ``nullable=False``（写了会与库内真实可空性不符）。
    * ``call_count`` 是 ``NOT NULL DEFAULT 1``（明细行 = 1；embedding 聚合行累加为 N）。

    写入方是 **llm-gateway 进程**（``jonex_core/llm_gateway/recorder.py``，走原生 SQL），
    本类只服务 platform 监控侧的**只读**聚合查询 —— 因此本类不做任何写路径适配。

    租户：``metering`` 表带 ``tenant_id``，但监控族**刻意不按租户过滤**（可见性只凭权限码
    ``platform:monitor:read``）。查询侧只选聚合列 / ``model`` 列，绝不 ``select(LlmUsageLog)``
    整表选列，避免把 ``tenant_id`` 带进 SQL。
    """

    __tablename__ = "llm_usage_log"
    __table_args__ = {"schema": "metering"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    # 计量幂等键：每次逻辑 LLM 调用唯一、重试稳定（embedding 聚合行是合成键）
    request_id = Column(String(64))
    trace_id = Column(String(64))
    user_id = Column(String(64))
    scene = Column(String(64), nullable=False)
    # 模型标识：库内**只有这一列**，故 model_id / model_name 本期同取本列
    model = Column(String(128), nullable=False)
    kb_id = Column(String(64))
    doc_id = Column(String(64))

    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    latency_ms = Column(Integer)
    is_stream = Column(Boolean, default=False)
    is_estimated = Column(Boolean, default=False)
    call_count = Column(Integer, nullable=False, default=1, server_default=text("1"))

    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))


__all__ = [
    "CallLog",
    "CallAttempt",
    "LlmUsageLog",
    "CallDirection",
    "CallFinalStatus",
    "AttemptStatus",
    "Protocol",
    "CallTrigger",
]
