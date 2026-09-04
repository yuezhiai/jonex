#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Answer feedback service（回答级反馈）.

按问答记录（history_id）维护反馈主记录与变更事件：
- 提交：反查问答记录快照、校验归属、幂等 + 乱序保护、原子 upsert 主记录 + 追加事件。
- 回显：按 history_id 查询当前反馈状态（无反馈返回 null）。
"""

from sqlalchemy import select

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models import KnowledgeInfo, ServiceKnowledgeBase
from ..repository import (
    KnowledgeAnswerFeedbackEventRepository,
    KnowledgeAnswerFeedbackRepository,
    KnowledgeSearchHistoryRepository,
)

VALID_FEEDBACK_TYPES = ("like", "dislike")
VALID_FEEDBACK_REASONS = frozenset(
    {
        "inaccurate",
        "not_answered",
        "incomplete",
        "wrong_reference",
        "missing_knowledge",
        "other",
    }
)
MAX_COMMENT_LEN = 300


class AnswerFeedbackService:
    """回答级反馈服务。"""

    async def submit(
        self,
        tenant_id: str,
        user_id: str,
        request: dict,
    ) -> dict:
        """提交/更新反馈（同一 operation_id 幂等，version 单调递增）。"""
        tenant_id = require_tenant(tenant_id)
        history_id = request["history_id"]
        operation_id = request["operation_id"]
        version = int(request["version"])
        feedback_type = request["feedback_type"]
        feedback_reason = request.get("feedback_reason")
        feedback_comment = request.get("feedback_comment")

        if feedback_type not in VALID_FEEDBACK_TYPES:
            raise InvalidParameterError(
                message=translate("err.answer_feedback.invalid_type", fallback="非法的反馈类型")
            )

        if feedback_type == "like":
            # like 强制清空 reason/comment（旧值仅保留在事件表）
            feedback_reason = None
            feedback_comment = None
        else:
            if not feedback_reason:
                raise InvalidParameterError(
                    message=translate(
                        "err.answer_feedback.reason_required", fallback="点踩时请选择原因"
                    )
                )
            if feedback_reason not in VALID_FEEDBACK_REASONS:
                raise InvalidParameterError(
                    message=translate("err.answer_feedback.invalid_reason", fallback="非法的点踩原因")
                )

        if feedback_comment is not None and len(feedback_comment) > MAX_COMMENT_LEN:
            raise InvalidParameterError(
                message=translate(
                    "err.answer_feedback.comment_too_long",
                    params={"max": str(MAX_COMMENT_LEN)},
                    fallback="补充说明不能超过 300 字",
                )
            )

        async with get_db_session() as session:
            main_repo = KnowledgeAnswerFeedbackRepository(session)
            event_repo = KnowledgeAnswerFeedbackEventRepository(session)
            history_repo = KnowledgeSearchHistoryRepository(session)

            # 1. 幂等：operation_id 已处理过 → 直接返回当前主记录
            existing_event = await event_repo.get_by_operation(tenant_id, operation_id)
            if existing_event is not None:
                main = await main_repo.get_by_history(tenant_id, history_id)
                return self._result(main, superseded=False, idempotent=True)

            # 2. 反查问答记录并校验归属（tenant + user；软删后的 history 查不到 → 404）
            history = await history_repo.get_by_id(history_id, tenant_id)
            if history is None or history.user_id != user_id:
                raise ResourceNotFoundError(
                    message=translate(
                        "err.answer_feedback.history_not_found", fallback="问答记录不存在或无权访问"
                    )
                )

            # 3. 乱序保护：version 不递增则丢弃，返回当前状态
            main = await main_repo.get_by_history(tenant_id, history_id)
            if main is not None and version <= main.version:
                return self._result(main, superseded=True, idempotent=False)

            # 4. 上下文快照（服务端按问答记录反查，缺 kb_ids 时从领域维度兜底反查）
            kb_ids = await self._resolve_kb_ids(session, tenant_id, history)
            snapshot = self._snapshot(history, kb_ids)

            # 5. 同事务原子：upsert 主记录 + 追加事件
            main = await main_repo.upsert_main(
                tenant_id,
                {
                    "user_id": user_id,
                    "history_id": history_id,
                    "query": snapshot["query"],
                    "answer": snapshot["answer"],
                    "feedback_type": feedback_type,
                    "feedback_reason": feedback_reason,
                    "feedback_comment": feedback_comment,
                    "domain_space_id": snapshot["domain_space_id"],
                    "knowledge_base_ids": snapshot["knowledge_base_ids"],
                    "source": snapshot["source"],
                    "mode": snapshot["mode"],
                    "source_missing_reason": snapshot["source_missing_reason"],
                    "version": version,
                },
            )
            await event_repo.insert_event(
                tenant_id,
                {
                    "history_id": history_id,
                    "feedback_id": main.id,
                    "user_id": user_id,
                    "operation_id": operation_id,
                    "version": version,
                    "feedback_type": feedback_type,
                    "feedback_reason": feedback_reason,
                    "feedback_comment": feedback_comment,
                },
            )

        return self._result(main, superseded=False, idempotent=False)

    async def get_status(
        self,
        tenant_id: str,
        user_id: str,
        request: dict,
    ) -> dict:
        """回显当前反馈状态（无反馈或非本人返回 null）。"""
        tenant_id = require_tenant(tenant_id)
        history_id = request["history_id"]
        async with get_db_session() as session:
            repo = KnowledgeAnswerFeedbackRepository(session)
            main = await repo.get_by_history(tenant_id, history_id)
        if main is None or main.user_id != user_id:
            return {"feedback": None}
        return {"feedback": main.to_dict()}

    async def list_feedback(
        self,
        tenant_id: str,
        request: dict,
    ) -> dict:
        """按知识库聚合查询回答反馈列表，含统计。"""
        tenant_id = require_tenant(tenant_id)
        kb_id = request["knowledge_base_id"]
        feedback_type = request.get("feedback_type")
        page = request.get("page", 1)
        page_size = request.get("page_size", 50)
        offset = (page - 1) * page_size

        async with get_db_session() as session:
            repo = KnowledgeAnswerFeedbackRepository(session)
            items = await repo.list_by_knowledge_base(
                tenant_id, kb_id, feedback_type=feedback_type,
                offset=offset, limit=page_size,
            )
            total = await repo.count_by_knowledge_base(
                tenant_id, kb_id, feedback_type=feedback_type,
            )
            stats = await repo.get_stats(tenant_id, kb_id)

        return {
            "items": [item.to_dict() for item in items],
            "total": total,
            "like_count": stats["like_count"],
            "dislike_count": stats["dislike_count"],
            "page": page,
            "page_size": page_size,
        }

    async def get_stats(
        self,
        tenant_id: str,
        request: dict,
    ) -> dict:
        """按知识库聚合统计回答反馈。"""
        tenant_id = require_tenant(tenant_id)
        kb_id = request["knowledge_base_id"]

        async with get_db_session() as session:
            repo = KnowledgeAnswerFeedbackRepository(session)
            stats = await repo.get_stats(tenant_id, kb_id)
        return stats

    async def toggle_adopted(
        self,
        tenant_id: str,
        request: dict,
    ) -> dict:
        """切换反馈采纳状态。"""
        tenant_id = require_tenant(tenant_id)
        feedback_id = request["feedback_id"]

        async with get_db_session() as session:
            repo = KnowledgeAnswerFeedbackRepository(session)
            record = await repo.toggle_adopted(tenant_id, feedback_id)
            if not record:
                raise ResourceNotFoundError(
                    message=translate("err.feedback.not_found", fallback="反馈记录不存在")
                )
        return record.to_dict()

    async def delete(
        self,
        tenant_id: str,
        request: dict,
    ) -> dict:
        """软删除单条回答反馈。"""
        tenant_id = require_tenant(tenant_id)
        feedback_id = request["feedback_id"]

        async with get_db_session() as session:
            repo = KnowledgeAnswerFeedbackRepository(session)
            deleted = await repo.delete_soft(feedback_id, tenant_id)
        return {"deleted": deleted}

    async def _resolve_kb_ids(self, session, tenant_id: str, history) -> list[str]:
        """从检索历史反查命中的知识库列表。

        优先级：
        1. extra_metadata.knowledge_base_ids（后端检索自动落库时写入）
        2. knowledge_base_id 单值（旧维度）
        3. extra_metadata.domain_id（领域服务 id，非 "all"）→ service_knowledge_bases 反查
        4. domain_space_id（领域空间 id）→ knowledge_info 反查空间下全部 kb
        """
        metadata = history.extra_metadata or {}
        kb_ids = [k for k in (metadata.get("knowledge_base_ids") or []) if k and str(k).strip()]
        if kb_ids:
            return kb_ids

        single_kb = (getattr(history, "knowledge_base_id", "") or "").strip()
        if single_kb:
            return [single_kb]

        # 兜底 1：领域服务 → 关联知识库
        domain_id = (metadata.get("domain_id") or "").strip()
        if domain_id and domain_id != "all":
            rows = await session.execute(
                select(ServiceKnowledgeBase.kb_id)
                .join(
                    KnowledgeInfo,
                    (ServiceKnowledgeBase.kb_id == KnowledgeInfo.id)
                    & (KnowledgeInfo.is_deleted == 0),
                )
                .where(
                    ServiceKnowledgeBase.service_id == domain_id,
                    ServiceKnowledgeBase.tenant_id == tenant_id,
                    ServiceKnowledgeBase.is_deleted == 0,
                )
            )
            svc_ids = [r[0] for r in rows.all()]
            if svc_ids:
                return svc_ids

        # 兜底 2：领域空间 → 空间下全部知识库
        space_id = (history.domain_space_id or "").strip()
        if space_id:
            rows = await session.execute(
                select(KnowledgeInfo.id).where(
                    KnowledgeInfo.space_id == space_id,
                    KnowledgeInfo.tenant_id == tenant_id,
                    KnowledgeInfo.is_deleted == 0,
                )
            )
            space_ids = [r[0] for r in rows.all()]
            if space_ids:
                return space_ids

        return []

    @staticmethod
    def _snapshot(history, kb_ids: list[str]) -> dict:
        """从检索历史记录反查上下文快照。"""
        metadata = history.extra_metadata or {}
        source = metadata.get("source") or metadata.get("pipeline")
        source_missing_reason = "history_no_kb" if not kb_ids else None
        return {
            "query": history.query,
            "answer": history.answer or history.answer_preview,
            "domain_space_id": history.domain_space_id,
            "knowledge_base_ids": kb_ids,
            "source": source,
            "mode": history.mode,
            "source_missing_reason": source_missing_reason,
        }

    @staticmethod
    def _result(main, superseded: bool, idempotent: bool) -> dict:
        return {
            "feedback": main.to_dict() if main else None,
            "superseded": superseded,
            "idempotent": idempotent,
        }


__all__ = ["AnswerFeedbackService"]
