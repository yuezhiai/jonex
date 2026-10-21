#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""上报收件箱 ORM：运行方上报的落库暂存（§4.2 ①）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §4.2 ①、§7.5。
建表 DDL 见 `deploy/postgres/migrations/008_task_center.sql`。

运行方调 `report_status` 时，管理方**先把这条上报存下来就回「受理」**（同步阶段做两件事：
报文级校验 —— `status` 取值、条件必填项；以及**身份一致性核对** —— 报文里的任务 / 空间 /
凭证得互相对得上。后者要读一眼库，但**不拿锁、不写库**，读到的是旧值、只用来拒明显
自相矛盾的报文），后台循环再逐条消化（定位 / 判当前运行 / 判终态 / 拿锁 / 落库 + 填下游 +
收尾）。这样上报能不能落库**不再取决于运行方愿不愿意重发**。

**假删语义跟别的表不一样，别照别的表类推**：这里 `is_deleted=1` 表示「**这条上报
已经处理完了**」，不是「被删掉了」—— 处理掉的记录永远留着，供事后对账
「管理方到底收到没」。

「处理完了」分两种，由 `dead_lettered` 区分：

- `is_deleted=1` + `dead_lettered=0` → **正常消化**（状态已推进、下游已填）；
- `is_deleted=1` + `dead_lettered=1` → **判了死信**（连续失败达 `TASK_CENTER_REPORT_MAX_ATTEMPTS`，
  放弃重试；`attempts` / `last_error` 留着供人工排查）。

**死信也出队**（同样置 `is_deleted=1`）不是图省事：超时巡检的「让路」判据只看
`is_deleted = 0`，死信若留在队列里，那条子任务就**永远**被让路、永远判不了超时。

由此三点：

- 「未消化」就是 `is_deleted = 0`（**死信不在其中**，它已经出队了），**没有单独的
  `state` 列** —— 三态是「一列两值 + 一个正交的标志位」，不是三个投票互相打架的状态；
- 「什么时候收到的」记在 `reported_at` —— 超时巡检「让路」要用它跟超时点比
  （§7.4：挂着「上报时间早于超时点、且尚未消化」的记录的子任务，这一轮先不判超时）；
- **不设 `created_at`**：`reported_at` 就是它，且语义更准。

**不加任何唯一约束**：同一张凭证**允许**重复上报（重复 / 迟到的上报本来就靠
`_absorb` 的终态闸吸收），所以这里不能像 `uq_tc_subtask_exec` 那样加唯一索引。
也因此 §3.1 里「假删表加唯一约束必须用部分索引」那条规矩对本表不适用。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)

from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base

from .base import iso


class SubtaskReport(Base):
    """一条待消化 / 已消化的运行方上报。一级字段全部平铺，跟 `report_status` 的入参一一对应。"""

    __tablename__ = "subtask_reports"
    __table_args__ = (
        # 后台循环按「还没消化的、先来先消化」扫（部分索引：已消化的记录不必进索引）
        Index(
            "idx_tc_report_pending",
            "reported_at",
            postgresql_where=text("is_deleted = 0"),
        ),
        # 超时巡检「让路」那个 EXISTS 用：按这三者找「这条子任务还挂着未消化的上报吗」
        Index(
            "idx_tc_report_exec",
            "tenant_id",
            "space_id",
            "execution_id",
            "is_deleted",
        ),
        # 同步阶段已经校验过 status，这里只是把口径落在库里（`create_all` 建表也跟着走）
        CheckConstraint("status IN ('success','failed')", name="ck_tc_report_status"),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    # 索引由上面的 idx_tc_report_exec（tenant_id 打头）覆盖，不另开 `index=True`
    tenant_id = Column(String(64), nullable=False)
    # 执行侧一律空间级，跟 `subtasks` 一个口径（§3.5）
    space_id = Column(String(64), nullable=False)
    # 受理凭证：消化时按 tenant_id + space_id + 它定位到那条 `subtasks`（§4.2 ①）
    execution_id = Column(String(128), nullable=False)
    # 报文里那条任务 id。**不参与定位**（定位靠上面的三元组 + 调度侧的 `task_attempt`），
    # 它只做两件事：① 同步阶段的**交叉校验**（这张凭证是不是真属于这条任务，
    # 见 `TaskService._require_report_identity`）；② 排障时一眼看出「这条上报是哪条任务的」。
    # 因此**不建索引**：没有任何查询按它筛。它**不是**能拿来反查子任务的外键 ——
    # 「查得到才核对」意味着「任务已删 / 凭证已被重试清掉」的报文照样会落库，那些行
    # 里的 `task_id` 在 `subtasks` 里已经找不到了（这正是幂等吸收该有的样子）。
    task_id = Column(String(64), nullable=False)
    # 只可能是 success / failed —— 同步阶段已校验，运行方报不了 running（§4.2 ①）
    status = Column(String(16), nullable=False)
    # status=success 的产出（同步阶段已保证非空）
    outputs = Column(JSONB, nullable=True)
    # 产出「去哪儿看」的引用（展示用，**不参与下游取参**）。它**跟着报文走**，所以收件箱
    # 也要存一列 —— 否则后台消化那一刻拿不到；消化时原样落到 `subtasks.output_refs`。
    output_refs = Column(JSONB, nullable=True)
    # 输入「**从哪儿来**」的引用（展示用，**不参与下游取参**，2026-10-16 加）：与
    # `output_refs` 同一条理由必须存 —— 它也**跟着报文走**，消化是另一趟，那一刻
    # 只有收件箱这一份报文；消化时原样落到 `subtasks.input_refs`。可空（老运行方不带）。
    input_refs = Column(JSONB, nullable=True)
    # status=failed 的失败原因（同步阶段已保证非空）
    error_message = Column(Text, nullable=True)
    # 失败原因的代号 + 参数（2026-10-10 加）。收件箱是**来源**：消化时要把它们原样搬到
    # `subtasks` / `subtask_attempts`，所以这里必须存 —— 否则后台消化那一刻拿不到，
    # 又多一处「渲染后冻住」的文本。语义同 `execution.py` 里那两列的注释。
    error_code = Column(String(128), nullable=True)
    error_params = Column(JSONB, nullable=True)
    # 运行方侧的事件时间（13 位毫秒）；**只落库留痕、不参与任何裁决**（运行方的钟不可信）。
    # 注意它**不进日志**——管理方侧没有任何一条 logger 带它，唯一的落点就是这一列。
    ts = Column(BigInteger, nullable=True)
    # 管理方**收到**这条上报的时刻 —— 跟超时点比的就是它，不是 `ts`
    reported_at = Column(DateTime, nullable=False, default=datetime.now)
    # 消化尝试次数 / 上次失败原因：排障时看「它卡了几轮、卡在哪」。
    # **`attempts` 现在不只是观测了** —— 后台循环拿它跟
    # `TASK_CENTER_REPORT_MAX_ATTEMPTS` 比，达阈值就判死信（见下面的 `dead_lettered`）。
    # `last_error` 是 `TEXT`、**不截断** —— 存进来的是「异常类名: 异常消息」，不含 traceback，
    # 完整留下才够排障。
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    processed_at = Column(DateTime, nullable=True)
    # 1 = 已判死信：消化连续失败达上限，**放弃重试**（记录留着供人工排查，不是删掉）。
    # 与 `is_deleted` 正交但**只可能出现在 `is_deleted=1` 的行上** —— 判死信那一步是把
    # 两个标志 + `processed_at` 在同一条 UPDATE 里一起写的（出队）。
    # 判死是**按"上报行"算**的，不是按子任务算：同一张凭证重发一条新的上报，它的 `attempts`
    # 从 0 重新起算 —— 运行方自己的重发预算是 3 次，所以最多再多几行，不会无限堆。
    dead_lettered = Column(SmallInteger, nullable=False, default=0)
    # 1 = 已处理完（**正常消化或判死信**，看 `dead_lettered`；**不是「被删」**，见模块 docstring）
    is_deleted = Column(SmallInteger, nullable=False, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "space_id": self.space_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "status": self.status,
            "outputs": self.outputs,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "error_params": self.error_params,
            "ts": self.ts,
            "reported_at": iso(self.reported_at),
            "attempts": self.attempts,
            "last_error": self.last_error,
            "processed_at": iso(self.processed_at),
            "dead_lettered": self.dead_lettered or 0,
            "is_deleted": self.is_deleted or 0,
        }


__all__ = ["SubtaskReport"]
