#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""落点 agent 运行记录 ORM。

对应表：`knowledge_base.abstract_landing_runs`（增量迁移 `update/108`）。

为什么要落表而不是把进度放内存：落点是一次**逐概念调模型**的后台任务，几十秒到
几分钟；前端靠轮询拿进度，进程重启或换副本都不能让进度丢。表是唯一的进度真源。

「同一 KB 同时只有一个在跑的 run」由**部分唯一索引**
`uq_abstract_landing_runs_active` 兜底，而不是应用层自己记：多副本并发点「开始」
时，内存标记各自为政，只有库上的约束是全局的。
"""
import copy

from sqlalchemy import Boolean, Column, DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin

RUN_MODE_FULL = "full"
RUN_MODE_LANDINGS = "landings"
#: `full` = 从源 schema 全量构造；`landings` = 只对现有概念库做一轮落点。
RUN_MODES = (RUN_MODE_FULL, RUN_MODE_LANDINGS)

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUSES = (
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_FAILED,
)
#: 「占用中」的状态集合 —— 部分唯一索引的谓词与 `find_active` 共用这一份，
#: 两边各写一份字面量是最容易漂的地方。
ACTIVE_RUN_STATUSES = (RUN_STATUS_QUEUED, RUN_STATUS_RUNNING)


class AbstractLandingRun(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一次落点 / 构造运行的进度与终态。

    状态机：`queued → running → succeeded|failed`（v1 不提供取消，
    所以没有 cancelled 状态 —— 别在服务层造一个库里不认的值）。
    """

    __tablename__ = "abstract_landing_runs"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    # 不加 `index=True`：索引以 DDL 为准（`idx_alr_kb` 是 (tenant_id,
    # knowledge_base_id, created_at DESC) 的复合索引，已覆盖本列的单列查询）。
    # 若在这里声明单列索引，`metadata.create_all` 会多造一个 migrations 里
    # 并不存在的索引，两条建表路径就漂了。
    knowledge_base_id = Column(String(128), nullable=False)
    run_mode = Column(String(16), nullable=False, default=RUN_MODE_LANDINGS)
    #: `full` 模式是否覆盖已确认（active）的抽象 schema。必须在**建单时**落库：
    #: `execute` 是另一个任务、只拿 run_id，靠行上的持久参数决定 construct 的 force。
    force = Column(Boolean, nullable=False, default=False,
                   server_default=text("false"))
    #: 业务词表（`别名 => 实体名`）。只有 `full`（全部重来）会用它；落库同理，
    #: 后台任务不能依赖调度参数，否则重复投递/进程重启后词表就丢了。
    alias_hints = Column(Text, nullable=False, default="",
                         server_default=text("''"))
    status = Column(String(32), nullable=False, default=RUN_STATUS_QUEUED)
    # 当前阶段名（recall / score / landing / apply …）。空串是「还没开始跑」的
    # 明确取值，不是 None：前端拿到空串就知道该显示「排队中」。
    phase = Column(String(64), nullable=False, default="")
    progress = Column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
    )
    summary = Column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
    )
    error = Column(Text, nullable=True)
    created_by = Column(String(128), nullable=True, default="")
    # `timezone=True`：迁移里是 timestamptz，别让 ORM 按无时区去读。
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        """对外形状：不泄漏 `tenant_id` / `is_deleted`（同其它 KB 资源口径）。

        `or` 兜底不是防御性编程：`default=` 只在 flush 时生效，而 create() 之后
        立刻就要把 run 回给前端，那时 JSONB 列还是 None。

        JSON 列走 `copy.deepcopy`：直接把 ORM 持有的 dict 交出去，调用方改一下
        嵌套层就是**原地改 ORM 实例**，而 SQLAlchemy 的 JSON 跟踪看不到这种改动
        （改得进不了库、也没人知道改过）。浅拷贝挡不住嵌套层。
        """
        return {
            "id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "run_mode": self.run_mode or RUN_MODE_LANDINGS,
            "force": bool(self.force),
            "alias_hints": self.alias_hints or "",
            "status": self.status or RUN_STATUS_QUEUED,
            "phase": self.phase or "",
            "progress": (
                copy.deepcopy(self.progress)
                if isinstance(self.progress, (dict, list))
                else {}
            ),
            "summary": (
                copy.deepcopy(self.summary)
                if isinstance(self.summary, (dict, list))
                else {}
            ),
            "error": self.error,
            "created_by": self.created_by or "",
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "RUN_MODES",
    "RUN_MODE_FULL",
    "RUN_MODE_LANDINGS",
    "RUN_STATUSES",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_QUEUED",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SUCCEEDED",
    "AbstractLandingRun",
]
