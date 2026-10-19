#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for Knowledge Base search history."""

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from jonex_core.common.repository import BaseRepository

from ..models import KnowledgeSearchHistory, build_query_hash

# [jonex] 检索历史单用户保留上限：超过后自动软清理最早记录
MAX_HISTORY_PER_USER = 200

# [jonex] 列表视图列：**不含快照三件套**（answer / references / reasoning）。
# 这三列是 TOAST 存储的大对象——实测单用户 18 条历史的 references 原始 JSON 就
# 102 KB（其中 97% 是 locations[].text，即整段 chunk 正文）。列表把它们排除在
# SELECT 之外有两个好处：响应体积降下来；这些大字段不从 TOAST 读进 shared
# buffers（一读就会把缓冲区占用抬上去，挤掉本该缓存的索引页）。
# ⚠️ 与 `KnowledgeSearchHistory.to_dict(include_snapshot=False)` 成对使用。
_LIGHT_COLUMNS = (
    KnowledgeSearchHistory.id,
    KnowledgeSearchHistory.tenant_id,
    KnowledgeSearchHistory.user_id,
    KnowledgeSearchHistory.domain_space_id,
    KnowledgeSearchHistory.query,
    KnowledgeSearchHistory.query_hash,
    KnowledgeSearchHistory.knowledge_base_id,
    KnowledgeSearchHistory.mode,
    KnowledgeSearchHistory.top_k,
    KnowledgeSearchHistory.status,
    KnowledgeSearchHistory.reference_count,
    KnowledgeSearchHistory.result_count,
    KnowledgeSearchHistory.duration_ms,
    KnowledgeSearchHistory.extra_metadata,
    KnowledgeSearchHistory.searched_at,
    KnowledgeSearchHistory.created_at,
    KnowledgeSearchHistory.updated_at,
)


def _strip_raw_urls(refs: list) -> list:
    """剥离引用快照中会过期的预签名 URL（COS 约 1 小时），仅保留持久标识。

    点击历史展示时经 resolve 端点按 doc_id/locations 重新生成 raw_url。
    [jonex] §image-refs P0-6：locations[].asset_url 同为预签名，一并剥离；
    历史展示经 resolve 按 image_idx + asset_ext 重新推导生成。
    """
    out = []
    for r in refs or []:
        if isinstance(r, dict):
            item = dict(r)
            item.pop("raw_url", None)
            for loc in item.get("locations") or []:
                if isinstance(loc, dict):
                    loc.pop("asset_url", None)
            out.append(item)
        else:
            out.append(r)
    return out


class KnowledgeSearchHistoryRepository(BaseRepository[KnowledgeSearchHistory]):
    model = KnowledgeSearchHistory

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_user(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_id: str,
        offset: int = 0,
        limit: int = 20,
        domain_space_id: str | None = None,
        history_id: str | None = None,
        with_snapshot: bool = True,
    ) -> list[KnowledgeSearchHistory]:
        """按 tenant/user 列历史。

        - `history_id` 非空 → 只取这一条（主键条件），忽略 offset/limit；
        - `with_snapshot=False` → **不 SELECT 快照三件套**（见 `_LIGHT_COLUMNS`），
          调用方必须用 `to_dict(include_snapshot=False)` 取值，否则会触发懒加载。
        """
        conditions = [
            KnowledgeSearchHistory.tenant_id == self._tenant_id(tenant_id),
            KnowledgeSearchHistory.user_id == user_id,
            KnowledgeSearchHistory.is_deleted == 0,
        ]
        if history_id:
            conditions.append(KnowledgeSearchHistory.id == history_id)
        if knowledge_base_id:
            conditions.append(KnowledgeSearchHistory.knowledge_base_id == knowledge_base_id)
        if domain_space_id is not None:
            conditions.append(KnowledgeSearchHistory.domain_space_id == domain_space_id)
        stmt = select(KnowledgeSearchHistory)
        if not with_snapshot:
            stmt = stmt.options(load_only(*_LIGHT_COLUMNS))
        stmt = (
            stmt.where(*conditions)
            .order_by(KnowledgeSearchHistory.searched_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_by_user(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_id: str,
        domain_space_id: str | None = None,
    ) -> int:
        conditions = [
            KnowledgeSearchHistory.tenant_id == self._tenant_id(tenant_id),
            KnowledgeSearchHistory.user_id == user_id,
            KnowledgeSearchHistory.is_deleted == 0,
        ]
        if knowledge_base_id:
            conditions.append(KnowledgeSearchHistory.knowledge_base_id == knowledge_base_id)
        if domain_space_id is not None:
            conditions.append(KnowledgeSearchHistory.domain_space_id == domain_space_id)
        result = await self.session.execute(
            select(func.count())
            .select_from(KnowledgeSearchHistory)
            .where(*conditions)
        )
        return result.scalar_one()

    async def upsert_for_user(
        self,
        tenant_id: str,
        user_id: str,
        data: dict,
    ) -> KnowledgeSearchHistory:
        normalized_tenant = self._tenant_id(tenant_id)
        query = data["query"]
        values = {
            "tenant_id": normalized_tenant,
            "user_id": user_id,
            "query": query,
            "query_hash": build_query_hash(query),
            "knowledge_base_id": data["knowledge_base_id"],
            "domain_space_id": data.get("domain_space_id"),
            "mode": data.get("mode") or "hybrid",
            "top_k": data.get("top_k") or 5,
            "status": data.get("status") or "done",
            "answer_preview": data.get("answer_preview"),
            "reference_count": data.get("reference_count") or 0,
            "result_count": data.get("result_count") or 0,
            "duration_ms": data.get("duration_ms"),
            "extra_metadata": data.get("extra_metadata") or {},
            "answer": data.get("answer"),
            "references": _strip_raw_urls(data.get("references") or []),
            "reasoning": data.get("reasoning"),
            "is_deleted": 0,
        }
        # [jonex] 每次搜索插入一条新记录，不覆盖旧记录（需求：新结果形成新的历史记录）。
        # 原有唯一约束 uq_knowledge_search_history_query_kb 已由 update/012 迁移移除，
        # 同一问题可保留多条历史；超出 MAX_HISTORY_PER_USER 由 trim_for_user 清理。
        stmt = (
            insert(KnowledgeSearchHistory)
            .values(**values)
            .returning(KnowledgeSearchHistory)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def trim_for_user(
        self,
        tenant_id: str,
        user_id: str,
        keep: int = MAX_HISTORY_PER_USER,
    ) -> list[str]:
        """该用户检索历史超过 keep 条时，软删除最早记录，返回**被删的 id 列表**。

        在 save 后调用：保留最新 keep 条，其余（含同一问题的更早记录）标记 is_deleted=1。

        [jonex] 以图问知识：返回值由「删除条数」改成「被删 id」——
        调用方要用这些 id 去释放它们独占的查询图片（D9）。
        `RETURNING` 而不是先查后删：多一次往返，且中间可能被别的写入插队。
        """
        conditions = [
            KnowledgeSearchHistory.tenant_id == self._tenant_id(tenant_id),
            KnowledgeSearchHistory.user_id == user_id,
            KnowledgeSearchHistory.is_deleted == 0,
        ]
        stmt_ids = (
            select(KnowledgeSearchHistory.id)
            .where(*conditions)
            .order_by(KnowledgeSearchHistory.searched_at.desc())
            .limit(keep)
        )
        keep_ids = set((await self.session.execute(stmt_ids)).scalars())
        if not keep_ids:
            return []
        stmt = (
            update(KnowledgeSearchHistory)
            .where(*conditions, KnowledgeSearchHistory.id.not_in(keep_ids))
            .values(is_deleted=1, updated_at=func.now())
            .returning(KnowledgeSearchHistory.id)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return list(result.scalars())

    async def delete_for_user(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_id: str,
        history_id: str,
        domain_space_id: str | None = None,
    ) -> bool:
        """软删单条。**签名不变** —— 被删的 id 就是入参 `history_id`，
        调用方不需要额外返回值（F22：三处方法改造量不同，这里最小）。
        """
        history = await self.get_by_id(history_id, tenant_id)
        if (
            not history
            or history.user_id != user_id
            or (knowledge_base_id and history.knowledge_base_id != knowledge_base_id)
            or (domain_space_id is not None and history.domain_space_id != domain_space_id)
        ):
            return False
        history.is_deleted = 1
        await self.session.flush()
        return True

    async def clear_for_user(
        self, tenant_id: str, user_id: str, knowledge_base_id: str,
        domain_space_id: str | None = None,
    ) -> list[str]:
        """清空该用户的历史，返回**被删的 id 列表**。

        [jonex] 2026-10-09 重写（F22 存量问题 + D17）：
        - 原实现是 `list_by_user(limit=1000)` 再逐行 ORM 置删 —— **超过 1000 条时
          一次清空清不干净**（每用户上限 200 条时不会触发，但那是巧合不是保证）。
          改成一条批量 UPDATE，天然没有条数上限；
        - 返回 id 列表（不再是条数），调用方据此释放独占的查询图片（D9）；
        - 筛选条件与 `list_by_user` **逐条对齐**（tenant / user / is_deleted，
          以及可选的 kb / space），否则会删到别的空间的历史。
        """
        conditions = [
            KnowledgeSearchHistory.tenant_id == self._tenant_id(tenant_id),
            KnowledgeSearchHistory.user_id == user_id,
            KnowledgeSearchHistory.is_deleted == 0,
        ]
        if knowledge_base_id:
            conditions.append(KnowledgeSearchHistory.knowledge_base_id == knowledge_base_id)
        if domain_space_id is not None:
            conditions.append(KnowledgeSearchHistory.domain_space_id == domain_space_id)
        stmt = (
            update(KnowledgeSearchHistory)
            .where(*conditions)
            .values(is_deleted=1, updated_at=func.now())
            .returning(KnowledgeSearchHistory.id)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return list(result.scalars())


__all__ = ["KnowledgeSearchHistoryRepository"]
