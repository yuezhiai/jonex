#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Nl2SqlObservation 仓储（本线只提供 append 路径；观测不参与业务 CRUD）。"""

from jonex_core.common.repository import BaseRepository

from ..models.nl2sql_observation import Nl2SqlObservation


class Nl2SqlObservationRepository(BaseRepository[Nl2SqlObservation]):
    model = Nl2SqlObservation

    async def add(self, **fields) -> Nl2SqlObservation:
        """追加一行观测。调用方（observe._write）已做截断与键装配；这里不做业务校验。

        会话由调用方提供（`get_db_session()` 正常退出即 commit —— database.py 语义），
        这里只 `create`（= add + flush），**不自行 commit**。
        """
        return await self.create(**fields)


__all__ = ["Nl2SqlObservationRepository"]
