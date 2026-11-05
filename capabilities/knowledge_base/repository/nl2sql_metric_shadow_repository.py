#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Nl2SqlMetricShadow 仓储（append-only；影子对比不参与业务 CRUD）。"""

from jonex_core.common.repository import BaseRepository

from ..models.nl2sql_metric_shadow import Nl2SqlMetricShadow


class Nl2SqlMetricShadowRepository(BaseRepository[Nl2SqlMetricShadow]):
    model = Nl2SqlMetricShadow

    async def add(self, **fields) -> Nl2SqlMetricShadow:
        """追加一行。会话由调用方提供（`get_db_session()` 退出即 commit）。"""
        return await self.create(**fields)


__all__ = ["Nl2SqlMetricShadowRepository"]
