#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""按 trace 汇总单次检索 Token 的**只读**仓储。

数据源 = 既有表 ``metering.llm_usage_log``（LLM 网关计量明细，DDL 见
``deploy/postgres/migrations/002_platform.sql``），ORM 复用
``jonex_core/common/monitor_models.py`` 的 :class:`LlmUsageLog`（不新建 ORM、
不复制表定义、不写表）。

口径（与 ``services/trace_token.py`` 的对外契约一一对应）：

* 条件恒含 ``tenant_id = :tenant_id AND trace_id = :trace_id AND is_estimated IS false``
  —— 租户过滤不是可选项；``is_estimated`` 在 DDL 里可空，用 ``IS false``
  （而非 ``= false``）同时排除 ``NULL`` 行，只统计**非估算**的精确用量。
* 一次查询取 ``COALESCE(SUM(total_tokens), 0)`` 与 ``COUNT(*)``：
  行数为 0 表示「本次检索还没有计量落库」，由 service 决定重试；
  行数 > 0 时合计即使为 0 也是有效结果。
* 不选整表列，只取两个聚合列。
"""

from typing import Any, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.monitor_models import LlmUsageLog
from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant


class TraceTokenRepository(BaseRepository[LlmUsageLog]):
    """``metering.llm_usage_log`` 按 trace 的 Token 合计取数（只读）。"""

    model = LlmUsageLog

    def __init__(self, session: AsyncSession, *, tenant_id: str):
        super().__init__(session)
        # 租户必填且在构造期校验：非法租户在 service 的 try 内降级为 None。
        self.tenant_id = require_tenant(tenant_id)

    def _conditions(self, trace_id: str) -> list[Any]:
        """租户 + trace + 非估算三条件（缺一不可）。"""
        return [
            LlmUsageLog.tenant_id == self.tenant_id,
            LlmUsageLog.trace_id == trace_id,
            LlmUsageLog.is_estimated.is_(False),
        ]

    def build_summary_stmt(self, trace_id: str):
        """构造一次查询：``COALESCE(SUM(total_tokens), 0)`` + ``COUNT(*)``。

        暴露为公开方法，便于单测直接编译语句断言过滤条件。
        """
        return select(
            func.coalesce(func.sum(LlmUsageLog.total_tokens), 0).label("total_tokens"),
            func.count().label("row_count"),
        ).where(*self._conditions(trace_id))

    async def summarize(self, trace_id: str) -> Tuple[int, int]:
        """返回 ``(合计 Token, 行数)``；空集为 ``(0, 0)``，由调用方判定是否重试。"""
        result = await self.session.execute(self.build_summary_stmt(trace_id))
        row = result.one()
        return int(getattr(row, "total_tokens", 0) or 0), int(
            getattr(row, "row_count", 0) or 0
        )


__all__ = ["TraceTokenRepository"]
