#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""内置步骤清单 ORM（平台预置、只读，不带租户）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.4。

`task_catalog_steps` 回答「平台支持哪些步骤可选」；步骤的固有形状
（跑哪个运行方 `runner`、输入输出 schema、默认超时、并发上限）定义在这里，编排里只引用不重定义。

⚠️ **2026-10-23 起不再有 `task_catalog` 表**：以前每个步骤挂在一个「内置任务」下，
定义必须先选一个内置任务；为了让任务中心可以自由编排，任务类型这一层被拿掉，
步骤清单变成**全局一张表**（按 `step_code` 唯一），编排时直接从这里选节点。

表里的 created_at / updated_at 由数据库 DEFAULT CURRENT_TIMESTAMP 兜底，应用不写。
"""

from uuid import uuid4

from sqlalchemy import Column, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base


class TaskCatalogStep(Base):
    """内置步骤清单：编排里能选哪些节点（步骤的固有形状都在这）。"""

    __tablename__ = "task_catalog_steps"
    __table_args__ = (
        # 索引名 / 列 / 唯一性逐条对齐 DDL（`deploy/postgres/migrations/008_task_center.sql`）。
        # 2026-10-23 起 `step_code` 全局唯一（以前是 `(task_catalog_code, step_code)`）。
        Index("uq_tc_cs_step_code", "step_code", unique=True),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    step_code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    runner_code = Column(String(64), nullable=False)
    # 这一步最多同时跑几个（空 = 不限）；派活时管理方数「这类活全局有多少个在跑」到上限就跳过（§3.4、§6.3）
    max_concurrency = Column(Integer, nullable=True)
    timeout_seconds = Column(Integer, nullable=False, default=300)
    input_schema = Column(JSONB, nullable=True)
    output_schema = Column(JSONB, nullable=True)
    created_at = Column(DateTime, nullable=True, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=True, server_default=text("CURRENT_TIMESTAMP"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "step_code": self.step_code,
            "name": self.name,
            "runner_code": self.runner_code,
            "max_concurrency": self.max_concurrency,
            "timeout_seconds": self.timeout_seconds,
            "input_schema": self.input_schema or {},
            "output_schema": self.output_schema or {},
        }


__all__ = ["TaskCatalogStep"]
