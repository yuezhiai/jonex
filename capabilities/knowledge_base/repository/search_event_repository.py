#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""数据运营事件仓储 — 检索/处理链路埋点落库。

一期只交付**写入**方法（供二期埋点链路调用）。二期的聚合查询**不在本仓储**：
AnalyticsService 直接按事件表写 SQL 聚合，本仓储只管 append-only 落库。
所有写入均为 append-only：检索四张表逐条插入；处理任务表按
(tenant_id, processing_task_id) 唯一键 upsert（同一任务状态覆盖）。
"""

import logging

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.tenant import require_tenant

from ..models import (
    AssetStatusEvent,
    ProcessingTaskEvent,
    SearchAdoptionEvent,
    SearchHitEvent,
    SearchRequestEvent,
    SearchStepEvent,
)

logger = logging.getLogger(__name__)


class SearchEventRepository:
    """数据运营事件写入仓储（append-only）。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record_request_event(
        self,
        tenant_id: str,
        *,
        user_id: str | None = None,
        request_id: str,
        entry_service_id: str | None = None,
        domain_space_id: str | None = None,
        query: str | None = None,
        query_hash: str | None = None,
        knowledge_base_ids: list | None = None,
    ) -> SearchRequestEvent:
        tenant_id = require_tenant(tenant_id)
        stmt = (
            insert(SearchRequestEvent)
            .values(
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                entry_service_id=entry_service_id,
                domain_space_id=domain_space_id,
                query=query,
                query_hash=query_hash,
                knowledge_base_ids=knowledge_base_ids or [],
                is_deleted=0,
            )
            .returning(SearchRequestEvent)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def record_step_event(
        self,
        tenant_id: str,
        *,
        request_id: str,
        stage: str,
        title: str | None,
        status: str,
        summary: str | None = None,
        detail: dict | None = None,
        duration_ms: int | None = None,
        knowledge_base_ids: list | None = None,
        service_id: str | None = None,
    ) -> SearchStepEvent:
        tenant_id = require_tenant(tenant_id)
        stmt = (
            insert(SearchStepEvent)
            .values(
                tenant_id=tenant_id,
                request_id=request_id,
                stage=stage,
                title=title,
                status=status,
                summary=summary,
                detail=detail,
                duration_ms=duration_ms,
                knowledge_base_ids=knowledge_base_ids or [],
                service_id=service_id,
                is_deleted=0,
            )
            .returning(SearchStepEvent)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def record_hit_event(
        self,
        tenant_id: str,
        *,
        request_id: str,
        target_type: str,
        target_id: str,
        entity_type: str | None = None,
        canonical_name: str | None = None,
        kb_id: str | None = None,
    ) -> SearchHitEvent:
        tenant_id = require_tenant(tenant_id)
        stmt = (
            insert(SearchHitEvent)
            .values(
                tenant_id=tenant_id,
                request_id=request_id,
                target_type=target_type,
                target_id=target_id,
                entity_type=entity_type,
                canonical_name=canonical_name,
                kb_id=kb_id,
                is_deleted=0,
            )
            .returning(SearchHitEvent)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def record_adoption_event(
        self,
        tenant_id: str,
        *,
        answer_id: str,
        request_id: str | None = None,
        target_type: str,
        target_id: str,
        entity_type: str | None = None,
        canonical_name: str | None = None,
        kb_id: str | None = None,
    ) -> SearchAdoptionEvent:
        tenant_id = require_tenant(tenant_id)
        stmt = (
            insert(SearchAdoptionEvent)
            .values(
                tenant_id=tenant_id,
                answer_id=answer_id,
                request_id=request_id,
                target_type=target_type,
                target_id=target_id,
                entity_type=entity_type,
                canonical_name=canonical_name,
                kb_id=kb_id,
                is_deleted=0,
            )
            .returning(SearchAdoptionEvent)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def record_asset_status_event(
        self,
        tenant_id: str,
        *,
        asset_id: str,
        status: str,
        previous_status: str | None = None,
        reason: str | None = None,
        kb_id: str | None = None,
    ) -> AssetStatusEvent | None:
        # 门禁：缺租户 / 缺资产 / 缺状态一律不写（绝不落脏行）。
        # 必须在 `require_tenant` **之前**判 —— 空租户在那里是抛异常，而埋点
        # 的契约是「写不了就跳过」，不该由一条坏埋点把调用方带崩。
        if not tenant_id or not asset_id or not status:
            logger.warning(
                "asset_status_event 缺关键字段，已跳过：tenant=%r asset=%r status=%r",
                tenant_id, asset_id, status,
            )
            return None
        tenant_id = require_tenant(tenant_id)
        stmt = (
            insert(AssetStatusEvent)
            .values(
                tenant_id=tenant_id,
                asset_id=asset_id,
                status=status,
                previous_status=previous_status,
                reason=reason,
                kb_id=kb_id,
                is_deleted=0,
            )
            .returning(AssetStatusEvent)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def upsert_processing_task_event(
        self,
        tenant_id: str,
        *,
        processing_task_id: str,
        asset_id: str,
        task_type: str,
        status: str,
        error: str | None = None,
        started_at=None,
        finished_at=None,
        kb_id: str | None = None,
    ) -> ProcessingTaskEvent:
        """同一 (tenant_id, processing_task_id) 状态覆盖。

        提交时落 started；完成/失败回调再 upsert 覆盖 status/finished_at/error，
        不新增任务行。

        ⚠️ `started_at` 用 `coalesce(excluded, 现值)` 而**不是直接覆盖**：
        第一次写 running 时带上开工时间，第二次写终态（success/failed）不会带
        started_at —— 直接覆盖会把它抹成 NULL，`duration_ms` 就全算不出来了。
        `finished_at` 相反，终态就该盖掉（重跑时新终态覆盖旧终态）。
        """
        tenant_id = require_tenant(tenant_id)
        base = insert(ProcessingTaskEvent).values(
            tenant_id=tenant_id,
            processing_task_id=processing_task_id,
            asset_id=asset_id,
            task_type=task_type,
            status=status,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            kb_id=kb_id,
            is_deleted=0,
        )
        stmt = base.on_conflict_do_update(
            index_elements=["tenant_id", "processing_task_id"],
            set_={
                "status": status,
                "error": error,
                "started_at": func.coalesce(
                    base.excluded.started_at, ProcessingTaskEvent.started_at
                ),
                "finished_at": finished_at,
                "asset_id": asset_id,
                "task_type": task_type,
                "kb_id": kb_id,
                "is_deleted": 0,
            },
        ).returning(ProcessingTaskEvent)
        result = await self.session.execute(stmt)
        return result.scalar_one()


async def safe_record_asset_status_event(
    session: AsyncSession,
    tenant_id: str,
    *,
    asset_id: str,
    status: str,
    previous_status: str | None = None,
    reason: str | None = None,
    kb_id: str | None = None,
) -> AssetStatusEvent | None:
    """写一条状态事件，**写入失败不得污染调用方的主事务**。

    ── 为什么需要 SAVEPOINT（这不是过度设计）──

    PostgreSQL 里一条语句失败后，**整个事务**立刻进入 aborted 状态：后面的任何
    语句（包括调用方自己的 `flush` / `commit`）都会收到
    `current transaction is aborted`。所以「`except` 吞掉异常」在 PG 上是假的：
    异常吞了，事务已经废了，主流程照样崩 —— 只是崩在更远的地方、报一个与埋点
    毫无关系的错。

    `begin_nested()` = `SAVEPOINT`：事件写入失败只回滚到保存点，主事务继续可用。
    这与 `record_processing_task` 的「埋点失败不影响主流程」是**同一条契约**，
    那边只是因为唯一键 upsert 极少失败、且没有 savepoint 兜底，属于已知缺口；
    状态事件是纯 INSERT，撞约束/类型错误的概率更高，必须按正确形状写。

    返回事件行；被门禁挡下 / 写入失败返回 `None`（调用方不需要关心）。
    """
    try:
        async with session.begin_nested():
            return await SearchEventRepository(session).record_asset_status_event(
                tenant_id,
                asset_id=asset_id,
                status=status,
                previous_status=previous_status,
                reason=reason,
                kb_id=kb_id,
            )
    except Exception:  # noqa: BLE001 — 埋点失败不得影响主流程，但必须留日志
        logger.warning(
            "asset_status_event write failed (tenant=%s asset=%s %s -> %s)",
            tenant_id, asset_id, previous_status, status, exc_info=True,
        )
        return None


__all__ = ["SearchEventRepository", "safe_record_asset_status_event"]
