#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""触发表 ORM（空间级：tenant_id + space_id）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.3。

描述「什么时候、用什么参数把任务跑起来」。删除走状态位 `status='deleted'`，不真删。

**触发器只等于定时**（§3.3）：曾经有 `manual`（手动点一下）和 `cron` 两种类型，
现在 `manual` 已砍、`trigger_type` 这一列整个删掉 —— 因为**定时是触发器的唯一刚需**
（`cron_expr` / `next_fire_at` 只有这张表能存），而「手动跑一次」走的是 `task.fire`，
**不经触发器**（`tasks.trigger_id` 记 NULL）。所以 `cron_expr` / `next_fire_at` /
`misfire_policy` 三列现在是**无条件必填**（`NOT NULL`），不再靠 `ck_tc_trig_cron`
去按类型分流。

`space_id` 是「这个触发器在哪个空间下引用定义」——它触发出来的任务就落那个空间，
同时也是它能否引用某个定义（尤其 `scope=space` 的）的判据。
"""

from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TimestampMixin

from .base import iso
from .enums import TriggerStatus


class TaskTrigger(Base, TimestampMixin):
    """触发：什么时候、用什么参数把任务跑起来。"""

    __tablename__ = "task_triggers"
    __table_args__ = (
        # 触发名在「租户 + 空间」内不重名，但删掉的（status='deleted'）不算数——
        # 删了之后同名可以再建，所以是**部分**唯一索引（§3.3）。
        Index(
            "uq_tc_trigger_key",
            "tenant_id",
            "space_id",
            "trigger_key",
            unique=True,
            postgresql_where=text("status <> 'deleted'"),
        ),
        # 后台循环「扫到期触发器」用：只扫 active（§7.5 ⓪）。
        # 从前谓词里还有个 `trigger_type = 'cron'` —— `trigger_type` 列已删（触发器只等于定时），
        # 那一半条件随之消失，剩下的 `status = 'active'` 才是它真正在管的事。
        Index(
            "idx_tc_triggers_due",
            "next_fire_at",
            postgresql_where=text("status = 'active'"),
        ),
        # 下面两条 CHECK 的出处是 DDL（`deploy/postgres/migrations/005_task_center.sql`）：
        # 这里重复声明一遍，是为了 `Base.metadata.create_all` 建出来的表**跟 DDL 一样**
        # （本模块的规矩，见 `TaskDefinition.__table_args__` 里那段）。生产建表只走 DDL，
        # 这两条不参与；但少了它们，本地 `create_all` 出的库会**少两道生产有的闸**，
        # 于是本地能过、线上一插就炸。
        CheckConstraint(
            "status IN ('active','paused','deleted')", name="ck_tc_trig_status"
        ),
        CheckConstraint(
            "misfire_policy IN ('skip','catch_up')", name="ck_tc_trig_misfire"
        ),
        # ⚠️ 本类**不走 `TenantMixin`**：它带的 `index=True` 会让 `create_all` 多建一条
        # `ix_task_center_task_triggers_tenant_id`，而 DDL 那边 `tenant_id` 已被上面那条
        # 唯一索引（`(tenant_id, space_id, trigger_key)`）的前缀覆盖、不必单开一条 ——
        # 于是同一个模型在两条建表路径上形状不同。原因与取舍见
        # `Task.__table_args__`（`execution.py`）那段；`tenant_id` 改为在下面直接声明，
        # 定义照抄 mixin。
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    # 见上面 `__table_args__` 里那段：这一列**不走 mixin**（DDL 不给它单独建索引），
    # 声明照抄 `TenantMixin` 的定义。
    tenant_id = Column(String(64), nullable=False)
    # 这个触发器在哪个空间下引用定义；触发出来的任务就落这个空间（§3.3）
    space_id = Column(String(64), nullable=False)
    definition_id = Column(String(64), nullable=False)
    trigger_key = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False, default=TriggerStatus.ACTIVE.value)
    # 5 段表达式「分 时 日 月 周」。**必填** —— 触发器只等于定时，没有「不定时」的触发器了
    cron_expr = Column(String(128), nullable=False)
    # 下次该触发的时间（秒一律为 00）；后台循环按它扫（<= 现在 就是要触发）。
    # **必填** —— 算不出来时不许写 NULL（列约束直接拒绝），
    # 处置是「告警 + 置 paused」或「拒绝」（§3.3.2 ⑤）
    next_fire_at = Column(DateTime, nullable=False)
    # 错过点的处置：skip=跳过已错过的 / catch_up=补跑一次（§7.5 ⓪）。**必填**
    misfire_policy = Column(String(16), nullable=False)
    # 触发时用哪一版编排。**一列就够了**（§3.3）：空 = 用最新已发布版；给了号 = 钉住那一版。
    # 从前还有一列 `version_mode`（latest / fixed），已删 —— 它一个字的信息都没多给：
    # `fixed_version` 是不是空已经表达了同一件事，而那一列还养出「`latest` 却填了号」这条
    # 必然要检查的矛盾，以及「从 fixed 切回 latest 时这一列置不回 NULL」那条死路。
    fixed_version = Column(Integer, nullable=True)
    # 触发时带的入参，触发时被当作 fire 的 inputs 传下去（§3.3.1）
    inputs = Column(JSONB, nullable=True)
    # 【语义】上一次「到点处理」的时间 —— **不代表真的起过任务**：misfire_policy=skip
    # 那次只推 next_fire_at、没调 trig.fire，也照样写它（抢占 CAS 一次写入，§7.5 ⓪）。
    # 要「上次真正触发任务的时间」请看 trig.runs 最后一条任务的 created_at。
    last_fired_at = Column(DateTime, nullable=True)
    # 触发抢占的 CAS 版本号（§7.5 ⓪：防多副本同时触发同一条）
    state_version = Column(Integer, nullable=False, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "space_id": self.space_id,
            "definition_id": self.definition_id,
            "trigger_key": self.trigger_key,
            "name": self.name,
            "status": self.status,
            "cron_expr": self.cron_expr,
            "next_fire_at": iso(self.next_fire_at),
            "misfire_policy": self.misfire_policy,
            "fixed_version": self.fixed_version,
            "inputs": self.inputs or {},
            "last_fired_at": iso(self.last_fired_at),
            "state_version": self.state_version,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


__all__ = ["TaskTrigger"]
