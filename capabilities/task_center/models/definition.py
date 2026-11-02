#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务定义与版本 ORM。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.2。

`TaskDefinition` 是一类任务的「身份证」（基于某个内置任务建的），自己不存编排，
只存 `current_version_no` 指向最新已发布版本；`TaskDefinitionVersion` 才是真正存编排
的地方，走 draft → published 单向状态机。

两张表都按 `scope` 分三级（§3.1）：`system` 无租户无空间、`tenant` 有租户无空间、
`space` 两个都有；所以 **不能用 `TenantMixin`**（那会把 `tenant_id` 钉成 NOT NULL），
租户/空间列在这里显式声明为可空，配套 `CHECK` 约束由 DDL 兜住。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TimestampMixin

from .base import iso
from .enums import DefinitionScope, VersionStatus


class TaskDefinition(Base, TimestampMixin):
    """任务定义：一类任务的身份证（按 scope 分系统级 / 租户级 / 空间级）。"""

    __tablename__ = "task_definitions"
    __table_args__ = (
        # 按租户查定义（def.list）用；索引名与 DDL 一致
        Index("idx_tc_def_tenant", "tenant_id"),
        # 下面两条 CHECK 的出处是 DDL（`deploy/postgres/migrations/008_task_center.sql`）：
        # 这里重复声明一遍，是为了 `Base.metadata.create_all` 建出来的表**跟 DDL 一样**
        # （本模块的规矩，见 `catalog.py` 里"唯一约束必须跟着走"那句）。生产建表只走 DDL，
        # 这两条不参与；但少了它们，本地 `create_all` 出的库会**少一道生产有的闸**，
        # 于是本地能过、线上一插就炸。
        CheckConstraint("scope IN ('system','tenant','space')", name="ck_tc_def_scope"),
        CheckConstraint(
            # scope 与 tenant_id / space_id 必须配套（§3.1）：
            # 系统级两个都空、租户级只有租户、空间级两个都有
            "(scope = 'system' AND tenant_id IS NULL     AND space_id IS NULL)"
            " OR (scope = 'tenant' AND tenant_id IS NOT NULL AND space_id IS NULL)"
            " OR (scope = 'space'  AND tenant_id IS NOT NULL AND space_id IS NOT NULL)",
            name="ck_tc_def_scope_fields",
        ),
        # ⚠️ 本类**不走 `SoftDeleteMixin`**：它带的 `index=True` 会让 `create_all` 多建
        # 一条 `ix_task_center_task_definitions_is_deleted`，而 DDL 不给软删列单独建索引
        # —— 于是同一个模型在两条建表路径上形状不同。原因与取舍见
        # `Task.__table_args__`（`execution.py`）那段；`is_deleted` 改为在下面直接声明，
        # 定义照抄 mixin。（`tenant_id` 同理没走 `TenantMixin`，理由见列上那句注释。）
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    # system(系统级) / tenant(租户级) / space(空间级)
    scope = Column(String(16), nullable=False, default=DefinitionScope.SPACE.value)
    # scope=tenant / space 时必有；scope=system 时为空
    # （索引走上面的 idx_tc_def_tenant，不另开 `index=True`，免得 `create_all` 建出两条重复的）
    tenant_id = Column(String(64), nullable=True)
    # scope=space 时必有；其余为空
    space_id = Column(String(64), nullable=True)
    name = Column(String(128), nullable=False)
    description = Column(String(512), nullable=True)
    # 最新已发布版本号；0 = 还没发过版
    current_version_no = Column(Integer, nullable=False, default=0)
    # 见上面 `__table_args__` 里那段：这一列**不走 mixin**（DDL 不给软删列单独建索引），
    # 声明照抄 `SoftDeleteMixin` 的定义。
    is_deleted = Column(SmallInteger, default=0, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "tenant_id": self.tenant_id,
            "space_id": self.space_id,
            "name": self.name,
            "description": self.description,
            "current_version_no": self.current_version_no,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


class TaskDefinitionVersion(Base):
    """任务定义版本：一版编排；已发布版本永久固化，不可改、不可删、不可逆回 draft。"""

    __tablename__ = "task_definition_versions"
    __table_args__ = (
        # 同一个定义的版本号不重复
        Index("uq_tc_ver_no", "definition_id", "version_no", unique=True),
        # 同一个定义同一时间只能有一个草稿（§3.2 第 3 条）——
        # 发布后 status 变 'published' 就出了这个部分索引，下一个草稿号才建得起来
        Index(
            "uq_tc_ver_draft",
            "definition_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
        Index("idx_tc_ver_tenant", "tenant_id"),
        # 三条 CHECK 同 `TaskDefinition`：出处是 DDL（§3.2 的建表块明文要求版本表也建
        # `ck_tc_ver_scope_fields`），这里重复一份只为让 `create_all` 跟 DDL 对齐。
        CheckConstraint(
            "status IN ('draft','published')", name="ck_tc_ver_status"
        ),
        CheckConstraint(
            "scope IN ('system','tenant','space')", name="ck_tc_ver_scope"
        ),
        CheckConstraint(
            # 版本的 scope / tenant_id / space_id 必须跟所属定义一致（§3.2）
            "(scope = 'system' AND tenant_id IS NULL     AND space_id IS NULL)"
            " OR (scope = 'tenant' AND tenant_id IS NOT NULL AND space_id IS NULL)"
            " OR (scope = 'space'  AND tenant_id IS NOT NULL AND space_id IS NOT NULL)",
            name="ck_tc_ver_scope_fields",
        ),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    # scope / tenant_id / space_id 与所属定义一致（§3.2）
    scope = Column(String(16), nullable=False, default=DefinitionScope.SPACE.value)
    # 索引走上面的 idx_tc_ver_tenant，不另开 `index=True`
    # （同 TaskDefinition.tenant_id 的注：`index=True` 会让 `create_all` 多建一条
    # `ix_task_center_*`，与 DDL 里的显式索引重名不同、实为同一条）
    tenant_id = Column(String(64), nullable=True)
    definition_id = Column(String(64), nullable=False)
    # scope=space 时必有；任务执行哪一版，就用哪一版的 space_id
    space_id = Column(String(64), nullable=True)
    version_no = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default=VersionStatus.DRAFT.value)
    config = Column(JSONB, nullable=False)
    published_at = Column(DateTime, nullable=True)
    # 假删除标记：版本自己不可单独删，只由「删定义」带出来（§3.2）。
    # 这列不带索引（文档未要求），所以不走 SoftDeleteMixin。
    is_deleted = Column(SmallInteger, default=0, nullable=False)
    # 带 Python 侧默认值：`BaseRepository.create` 只 flush 不 refresh，没有默认值的话
    # 插入后直接 `**row.to_dict()` 返回，`created_at` 会是 None（与 TimestampMixin 口径一致）。
    created_at = Column(DateTime, default=datetime.now, nullable=True)
    # 表结构只有 created_at，没有 updated_at，所以不用 TimestampMixin。

    def to_dict(self, *, with_config: bool = True) -> dict:
        data = {
            "id": self.id,
            "scope": self.scope,
            "tenant_id": self.tenant_id,
            "definition_id": self.definition_id,
            "space_id": self.space_id,
            "version_no": self.version_no,
            "status": self.status,
            "published_at": iso(self.published_at),
            "created_at": iso(self.created_at),
        }
        if with_config:
            data["config"] = self.config or {}
        return data


__all__ = ["TaskDefinition", "TaskDefinitionVersion"]
