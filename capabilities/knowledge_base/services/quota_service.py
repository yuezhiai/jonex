#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""系统级配额引擎。

职责：
- get_quota_view：返回 12 项配额 used/reserved/limit/unit（前端展示）。
- check_upload_static：上传「文件类型 + 单文件大小」静态校验（事务外，先挡最便宜的错误）。
- check_upload_counts：上传「单 KB 文档数 / 租户文档数 / 租户容量」计数校验（事务内，advisory lock 原子化）。
- check_kb_creation：新建知识库「单租户 KB 数量」校验。

统计口径：全部 ``is_deleted=0``（未软删除）；存储只算 ``file_size`` 源文件（Byte）。
一期不做时长校验、不做独立预占表，reserved 恒为 0。
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.quota import (
    QUOTA_KEYS,
    QuotaConfigError,
    SystemQuota,
    classify_quota_category,
    get_system_quota,
    size_limit_key,
)
from jonex_core.common.exceptions import InvalidParameterError, QuotaExceededError
from jonex_core.common.i18n import translate

from ..models import KnowledgeDocument, KnowledgeInfo

logger = logging.getLogger(__name__)


def _quota_or_none() -> SystemQuota | None:
    """获取系统配额；配置缺失返回 None（运行时兜底，配额校验降级为放行）。

    启动阶段 capability.initialize() 已调用 get_system_quota() 做 fail-fast 校验，
    正常运行时此处命中 lru_cache；仅单元测试直连 service（绕过启动）时可能返回 None，
    此时不该把「启动配置错误」泄漏成运行时 500。
    """
    try:
        return get_system_quota()
    except QuotaConfigError as exc:
        logger.warning("配额配置不可用，本次配额校验降级为放行：%s", exc)
        return None


def _raise_quota(
    q: SystemQuota, key: str, used: int, limit: int, reserved: int = 0
) -> None:
    """抛出配额超限异常：details 携带 quotaKey/used/reserved/limit/unit，errorCode 由异常 code（4004）承载。"""
    raise QuotaExceededError(
        message=translate(QuotaExceededError.code, fallback="配额已用尽"),
        details={
            "quotaKey": key,
            "used": used,
            "reserved": reserved,
            "limit": limit,
            "unit": q.unit(key),
        },
    )


async def _count_kb(session: AsyncSession, tenant_id: str) -> int:
    stmt = (
        select(func.count())
        .select_from(KnowledgeInfo)
        .where(
            KnowledgeInfo.tenant_id == tenant_id,
            KnowledgeInfo.is_deleted == 0,
        )
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _count_docs(
    session: AsyncSession, tenant_id: str, kb_id: str | None = None
) -> int:
    conds = [
        KnowledgeDocument.tenant_id == tenant_id,
        KnowledgeDocument.is_deleted == 0,
    ]
    if kb_id:
        conds.append(KnowledgeDocument.knowledge_base_id == kb_id)
    stmt = select(func.count()).select_from(KnowledgeDocument).where(*conds)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _sum_storage(session: AsyncSession, tenant_id: str) -> int:
    stmt = (
        select(func.coalesce(func.sum(KnowledgeDocument.file_size), 0))
        .select_from(KnowledgeDocument)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.is_deleted == 0,
        )
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


class QuotaService:
    """系统配额校验与查询。"""

    def check_upload_static(self, category: str, file_size: int) -> None:
        """上传静态校验：文件类型 + 单文件大小（无需 DB，事务外调用）。

        需求校验顺序（「单次上传数量」由前端约束）。
        """
        if category == "other":
            raise InvalidParameterError(
                message=translate(
                    "err.doc.unsupported_file_type",
                    fallback="不支持的文件类型，请上传文档、表格、图片、音频或视频文件",
                )
            )
        key = size_limit_key(category)
        if key is None:
            return
        q = _quota_or_none()
        if q is None:
            return
        limit = q.to_bytes(key)
        if file_size > limit:
            _raise_quota(q, key, used=file_size, limit=limit)

    async def check_upload_counts(
        self, session: AsyncSession, tenant_id: str, kb_id: str, file_size: int
    ) -> None:
        """上传计数校验：单 KB 文档数 → 租户文档数 → 租户容量。

        在调用方同一事务内执行：advisory lock 串行化并发，校验通过后调用方 INSERT 占额。
        """
        q = _quota_or_none()
        if q is None:
            return
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"quota:doc:{tenant_id}"},
        )

        kb_count = await _count_docs(session, tenant_id, kb_id)
        kb_limit = q.get("knowledgeBaseDocumentLimit")
        if kb_count + 1 > kb_limit:
            _raise_quota(q, "knowledgeBaseDocumentLimit", used=kb_count, limit=kb_limit)

        tenant_count = await _count_docs(session, tenant_id)
        tenant_limit = q.get("tenantDocumentLimit")
        if tenant_count + 1 > tenant_limit:
            _raise_quota(q, "tenantDocumentLimit", used=tenant_count, limit=tenant_limit)

        storage_used = await _sum_storage(session, tenant_id)
        storage_limit = q.to_bytes("tenantStorageLimitMiB")
        if storage_used + file_size > storage_limit:
            _raise_quota(q, "tenantStorageLimitMiB", used=storage_used, limit=storage_limit)

    async def check_kb_creation(self, session: AsyncSession, tenant_id: str) -> None:
        """新建知识库：单租户 KB 数量校验。"""
        q = _quota_or_none()
        if q is None:
            return
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"quota:kb:{tenant_id}"},
        )
        used = await _count_kb(session, tenant_id)
        limit = q.get("tenantKnowledgeBaseLimit")
        if used + 1 > limit:
            _raise_quota(q, "tenantKnowledgeBaseLimit", used=used, limit=limit)

    async def get_quota_view(self, tenant_id: str, kb_id: str | None = None) -> dict:
        """返回 12 项配额 used/reserved/limit/unit（前端展示）。

        存储类 limit/used 为 Byte（unit 仅作展示标签）；配置类（数量/大小/时长）used 恒 0。
        """
        q = _quota_or_none()
        if q is None:
            return {"quotas": []}
        from jonex_core.common import get_db_session

        async with get_db_session() as session:
            kb_used = await _count_kb(session, tenant_id)
            tenant_doc_used = await _count_docs(session, tenant_id)
            storage_used = await _sum_storage(session, tenant_id)
            kb_doc_used = await _count_docs(session, tenant_id, kb_id) if kb_id else 0

        used_map = {
            "tenantKnowledgeBaseLimit": kb_used,
            "tenantDocumentLimit": tenant_doc_used,
            "tenantStorageLimitMiB": storage_used,
            "knowledgeBaseDocumentLimit": kb_doc_used,
        }
        quotas = [
            {
                "quotaKey": key,
                "used": used_map.get(key, 0),
                "reserved": 0,
                "limit": q.limit(key),
                "unit": q.unit(key),
            }
            for key in QUOTA_KEYS
        ]
        return {"quotas": quotas}


__all__ = ["QuotaService", "classify_quota_category"]
