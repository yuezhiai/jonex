"""Search history service for Knowledge Base.

[jonex] 以图问知识（docs/knowledge-base/image-query-search-execution-plan.md §4-B9）：
历史是查询图片的**锚点** —— 图片的存在、可见范围、保留期全部跟随历史。
所以三处删除入口（删单条 / 清空 / trim 超量）都要把「因此失去全部引用的图片」
交出去释放，列表接口则要批量带出图。
"""

import logging
from typing import Any, Optional

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models import build_query_hash
from ..repository import KnowledgeQueryImageRepository, KnowledgeSearchHistoryRepository
from ..repository.search_history_repository import MAX_HISTORY_PER_USER, _strip_raw_urls
from ..dtos import (
    SearchHistoryCreateRequest,
    SearchHistoryDeleteRequest,
    SearchHistoryListRequest,
    SearchOverviewRequest,
)
from .document_service import _payload
from .query_image_service import QueryImageService

logger = logging.getLogger(__name__)


def _reference_doc_ids(references: Any) -> list[str]:
    """历史快照引用里的 doc_id 列表（去重保序；非 dict / 缺 doc_id 一律跳过）。"""
    seen: set[str] = set()
    out: list[str] = []
    for ref in references or []:
        if not isinstance(ref, dict):
            continue
        doc_id = ref.get("doc_id")
        if not doc_id:
            continue
        value = str(doc_id)
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _history_kb_ids(history: Any) -> list[str]:
    """All KBs that contributed to a snapshot, including multi-KB searches."""
    metadata = getattr(history, "extra_metadata", None)
    scope = metadata.get("knowledge_base_ids", []) if isinstance(metadata, dict) else []
    if not isinstance(scope, (list, tuple)):
        scope = []
    values = [getattr(history, "knowledge_base_id", ""), *scope]
    return list(dict.fromkeys(str(value).strip() for value in values if value and str(value).strip()))


async def _accessible_history_kbs(session, tenant_id: str, user_id: str, kb_ids: list[str]) -> set[str]:
    """Check existing KB, active space, and membership or KB grant."""
    if not kb_ids:
        return set()
    from sqlalchemy import text
    from .space_permission_service import get_active_space_ids, get_visible_space_ids
    from .kb_permission_service import get_granted_kb_ids

    rows = (await session.execute(text(
        "SELECT ki.id, ki.space_id FROM knowledge_base.knowledge_info ki "
        "WHERE ki.id = ANY(:ids) AND ki.tenant_id=:t AND ki.is_deleted=0"
    ), {"ids": kb_ids, "t": tenant_id})).all()
    active = set(await get_active_space_ids(tenant_id))
    visible = await get_visible_space_ids(tenant_id, user_id)
    granted = set(await get_granted_kb_ids(tenant_id, user_id)) if visible is not None else set()
    return {str(kb_id) for kb_id, space_id in rows
            if space_id in active and (visible is None or space_id in visible or kb_id in granted)}


async def _active_history_documents(session, tenant_id: str, kb_ids: list[str], doc_ids: list[str]) -> set[str]:
    """A saved citation must still belong to an active document in its scope."""
    if not doc_ids:
        return set()
    from sqlalchemy import text
    rows = (await session.execute(text(
        "SELECT id FROM knowledge_base.knowledge_documents "
        "WHERE tenant_id=:t AND knowledge_base_id = ANY(:kb_ids) "
        "AND id = ANY(:doc_ids) AND is_deleted=0 AND status NOT IN ('archived', 'deleting', 'deleted')"
    ), {"t": tenant_id, "kb_ids": kb_ids, "doc_ids": doc_ids})).all()
    return {str(row[0]) for row in rows}


async def _wiki_history_kbs(session, tenant_id: str, kb_ids: list[str]) -> set[str]:
    """Determine history types from the current tenant's KB rows, not client metadata."""
    if not kb_ids:
        return set()
    from sqlalchemy import text
    rows = (await session.execute(text(
        "SELECT id FROM knowledge_base.knowledge_info WHERE tenant_id=:t "
        "AND id = ANY(:ids) AND is_deleted=0 AND kb_type='jonex_wiki'"
    ), {"t": tenant_id, "ids": kb_ids})).all()
    return {str(row[0]) for row in rows}


def _wiki_history_documents(history: Any, wiki_kbs: set[str], kb_ids: list[str]) -> list[str]:
    """Return conservative Bundle source scopes, or reject unknowable old scope.

    New histories include all document ids in the pinned Bundle, not only
    successfully verified references. Old snapshots without any identifiable
    Wiki reference cannot safely be rechecked and must be searched again.
    """
    metadata = getattr(history, 'extra_metadata', None)
    outcomes = metadata.get('query_outcomes', []) if isinstance(metadata, dict) else []
    outcomes = outcomes if isinstance(outcomes, list) else []
    refs = getattr(history, 'references', None) or []
    documents = set()
    for kb_id in wiki_kbs:
        wiki_outcomes = [item for item in outcomes if isinstance(item, dict) and item.get('kb_id') == kb_id]
        relevant = [item for item in wiki_outcomes if item.get('stop_reason') not in ('empty_kb', 'engine_error')]
        if relevant and all('source_document_ids' in item for item in relevant):
            for item in relevant:
                ids = item['source_document_ids']
                if not isinstance(ids, list) or len(ids) > 10000 or any(not isinstance(v, str) or not v for v in ids):
                    raise ResourceNotFoundError(message="历史来源范围无法核验，请重新检索")
                documents.update(ids)
            continue
        if relevant and any('source_document_ids' in item for item in relevant):
            raise ResourceNotFoundError(message="历史来源范围无法核验，请重新检索")
        if wiki_outcomes and not relevant:
            # This Wiki returned no answer (e.g. empty or unavailable); it did
            # not contribute documents to the saved answer.
            continue
        legacy = {str(ref['doc_id']) for ref in refs if isinstance(ref, dict) and ref.get('doc_id')
                  and (ref.get('kb_id') == kb_id or (len(kb_ids) == 1 and not ref.get('kb_id')))}
        if not legacy:
            raise ResourceNotFoundError(message="历史来源范围无法核验，请重新检索")
        documents.update(legacy)
    return sorted(documents)


class SearchHistoryService:
    def __init__(self, query_image: Optional[QueryImageService] = None) -> None:
        # 图片服务由 `KnowledgeBaseService` 注入（与其它 service 的组装方式一致）；
        # 这里给默认值只是为了让单测能直接构造。
        self.query_image = query_image or QueryImageService()

    async def list_history(
        self,
        tenant_id: str,
        user_id: str,
        request: SearchHistoryListRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchHistoryListRequest(**_payload(request))
        # [jonex] 两种模式共用一个端点 ——
        #   列表（默认）：不带快照，供侧栏渲染（只用 query / result_count / searched_at）；
        #   单条（传 history_id）：带快照，供点击历史时回放（answer / references / reasoning）。
        # 快照三件套占列表响应体积的 83%（实测 222 KB 中的 185 KB），没必要每次进页面都背。
        want_snapshot = bool(req.history_id)
        offset = 0 if want_snapshot else (req.page - 1) * req.page_size
        limit = 1 if want_snapshot else req.page_size
        async with get_db_session() as session:
            repo = KnowledgeSearchHistoryRepository(session)
            if want_snapshot:
                items = await repo.list_by_user(
                    tenant_id,
                    user_id,
                    knowledge_base_id=req.knowledge_base_id,
                    offset=offset,
                    limit=limit,
                    domain_space_id=req.domain_space_id,
                    history_id=req.history_id,
                    with_snapshot=want_snapshot,
                )
                # 快照里可能有历史答案/引用；读取前按**当前**用户密级复核。
                if items:
                    await self._assert_history_readable(
                        session, tenant_id, user_id, items[0]
                    )
                # 单条取：命中数就是 total，省掉一次 count 往返
                total = len(items)
            else:
                user_rank = await self._user_max_rank(session, tenant_id, user_id)
                # Even administrators must not see snapshots from disabled or
                # deleted KBs. Apply all current scope checks before paging.
                rows = await repo.list_by_user(
                    tenant_id, user_id, knowledge_base_id=req.knowledge_base_id,
                    offset=0, limit=MAX_HISTORY_PER_USER,
                    domain_space_id=req.domain_space_id, history_id=req.history_id,
                    with_snapshot=False,
                )
                visible = await self._filter_visible_history(
                    session, tenant_id, rows, user_rank, user_id=user_id
                )
                total = len(visible)
                items = visible[offset:offset + limit]
            # [jonex] 一次查出本页所有历史的图（`history_id IN (...)` join 图片表），
            # 不是逐条查 —— 否则 20 条历史就是 20 次往返。
            image_repo = KnowledgeQueryImageRepository(session)
            images = await image_repo.list_for_histories(
                tenant_id, user_id, [item.id for item in items]
            )
        return {
            "items": [
                {
                    **item.to_dict(include_snapshot=want_snapshot),
                    "query_images": _refs(images.get(item.id)),
                }
                for item in items
            ],
            "total": total,
            "page": req.page,
            "page_size": req.page_size,
        }

    # ------------------------------------------------------------------
    # B6：检索历史读取侧的密级复核
    #
    # 历史是**快照**：保存时的答案/引用可能来自今天已经超密级的文档。
    # 读取侧按当前密级处理（B6 spec §8 风险 2）：
    #   - 列表：按历史所属 KB 的最高生效 rank 过滤（与检索侧 D8 KB 级预拦截同口径），
    #     列表只含 query/计数，不含快照大字段；
    #   - 单条（带快照）：KB 与引用里的 doc_id 逐个复核，命中即 404，不返回答案。
    # 管理员全见；匿名不会有历史行。
    # ------------------------------------------------------------------

    async def _user_max_rank(self, session, tenant_id: str, user_id: str) -> int | None:
        """None = 不限（管理员）；否则用户可读最高 rank。"""
        from jonex_core.security import classification_access as policy
        from .space_permission_service import is_tenant_admin

        if user_id and user_id != "anonymous":
            if await is_tenant_admin(tenant_id, user_id):
                return None
        return await policy.get_user_max_rank(
            session, tenant_id, user_id, is_tenant_admin=False,
        )

    async def _filter_visible_history(
        self, session, tenant_id: str, rows: list, user_rank: int | None,
        *, user_id: str | None = None,
    ) -> list:
        """Recheck every KB in each snapshot's scope before exposing its query."""
        from jonex_core.security import classification_access as policy

        kb_ids = list(dict.fromkeys(k for r in rows for k in _history_kb_ids(r)))
        actor = user_id or (str(rows[0].user_id) if rows else "")
        accessible = await _accessible_history_kbs(session, tenant_id, actor, kb_ids)
        max_ranks = (
            await policy.kb_max_level_rank(session, tenant_id, kb_ids)
            if kb_ids and user_rank is not None else {}
        )
        visible = [
            r for r in rows
            if _history_kb_ids(r)
            and all(k in accessible and (user_rank is None or max_ranks.get(k, 0) <= user_rank)
                    for k in _history_kb_ids(r))
        ]
        hidden = len(rows) - len(visible)
        if hidden:
            logger.info(
                "检索历史隐藏：%d 条超密级（user=%s）", hidden, rows[0].user_id
            )
        return visible

    async def _assert_history_readable(
        self, session, tenant_id: str, user_id: str, history,
    ) -> None:
        """单条历史带快照：KB 或任一引用文档超密级 ⇒ 404（不泄露存在性）。"""
        from jonex_core.security import classification_access as policy

        kb_ids = _history_kb_ids(history)
        accessible = await _accessible_history_kbs(session, tenant_id, user_id, kb_ids)
        if not kb_ids or any(k not in accessible for k in kb_ids):
            raise ResourceNotFoundError(message="检索历史不存在")
        wiki_kbs = await _wiki_history_kbs(session, tenant_id, kb_ids)
        doc_ids = list(dict.fromkeys([
            *_reference_doc_ids(getattr(history, "references", None)),
            *_wiki_history_documents(history, wiki_kbs, kb_ids),
        ]))
        if doc_ids:
            active_docs = await _active_history_documents(session, tenant_id, kb_ids, doc_ids)
            if any(doc not in active_docs for doc in doc_ids):
                raise ResourceNotFoundError(message="检索历史不存在")
        user_rank = await self._user_max_rank(session, tenant_id, user_id)
        if user_rank is None:
            return
        max_ranks = await policy.kb_max_level_rank(session, tenant_id, kb_ids)
        if any(max_ranks.get(k, 0) > user_rank for k in kb_ids):
            raise ResourceNotFoundError(message="检索历史不存在")

        if doc_ids:
            ranks = await policy.document_level_ranks(session, tenant_id, doc_ids)
            if any(rank > user_rank for rank in ranks.values()):
                raise ResourceNotFoundError(message="检索历史不存在")

    async def save_history(
        self,
        tenant_id: str,
        user_id: str,
        request: SearchHistoryCreateRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchHistoryCreateRequest(**_payload(request))
        data: dict[str, Any] = _payload(req)
        # 前端搜索结束后补存同一条历史：带 history_id 时更新后端刚写的那一条，
        # 不再插入重复行（否则列表顶部那条会缺 pending_actions/action_results）。
        target_history_id = data.pop("history_id", None)
        metadata = dict(data.pop("metadata", {}) or {})
        domain = data.pop("domain", None)
        domain_id = data.pop("domain_id", None)
        domain_space_id = data.pop("domain_space_id", None)
        strict_config = data.pop("strict_config", None)
        # ⚠️ F17：本方法是**手动 pop 已知字段**后组 data 交给 upsert_for_user 的，
        # 而 upsert_for_user 只从它显式列出的 values 取值 —— DTO 上加了字段、
        # 这里不显式处理，新字段就会被**静默丢掉**（不报错）。所以下面两个必须显式取。
        image_ids = data.pop("query_image_ids", None) or []
        image_question = data.pop("image_question", None)
        if domain:
            metadata["domain"] = domain
        if domain_id:
            metadata["domain_id"] = domain_id
        if strict_config:
            metadata["strict_config"] = strict_config
        if image_question:
            # 用户原话存进 extra_metadata（不加列）；`to_dict()` 已把它输出为 `metadata`
            metadata["image_question"] = image_question
        if domain_space_id:
            data["domain_space_id"] = domain_space_id
        data["extra_metadata"] = metadata
        async with get_db_session() as session:
            repo = KnowledgeSearchHistoryRepository(session)
            existing = (
                await repo.get_by_id_for_user(tenant_id, user_id, target_history_id)
                if target_history_id else None
            )
            if existing is None:
                history = await repo.upsert_for_user(tenant_id, user_id, data)
            else:
                # Updating a snapshot returns its retained metadata. Recheck
                # access first so this write path cannot bypass the read gate.
                await self._assert_history_readable(session, tenant_id, user_id, existing)
                # 前端补存：更新同一条历史，并保留后端写入的卡片/工具结果等元数据
                old_meta = dict(existing.extra_metadata or {})
                merged_meta = {**old_meta, **metadata}
                # A later client-side update must not shrink the original scope
                # and thereby bypass the current authorization checks.
                old_scope = _history_kb_ids(existing)
                new_scope = metadata.get("knowledge_base_ids")
                if not isinstance(new_scope, (list, tuple)):
                    new_scope = []
                merged_meta["knowledge_base_ids"] = list(dict.fromkeys(
                    [*old_scope, *(str(k) for k in new_scope if k)]))
                for key in ("pending_actions", "tool_results", "action_results", "query_outcomes", "pipeline", "pipeline_groups"):
                    if key in old_meta:
                        merged_meta[key] = old_meta[key]
                existing.extra_metadata = merged_meta
                existing.query = data.get("query") or existing.query
                existing.query_hash = build_query_hash(existing.query)
                if data.get("knowledge_base_id") and not existing.knowledge_base_id:
                    existing.knowledge_base_id = data["knowledge_base_id"]
                if data.get("domain_space_id") is not None:
                    existing.domain_space_id = data["domain_space_id"]
                existing.mode = data.get("mode") or existing.mode
                existing.top_k = data.get("top_k") or existing.top_k
                existing.status = data.get("status") or existing.status
                existing.answer_preview = data.get("answer_preview")
                existing.reference_count = data.get("reference_count") or 0
                existing.result_count = data.get("result_count") or 0
                existing.duration_ms = data.get("duration_ms")
                existing.answer = data.get("answer")
                existing.references = _strip_raw_urls(data.get("references") or [])
                existing.reasoning = data.get("reasoning")
                await session.flush()
                history = existing
            history_dict = history.to_dict()

        # [jonex] 图片落盘与关联在**独立事务**里（B9 四步），历史行已在上面的
        # `get_db_session()` 退出时提交 —— 图片环节失败不影响历史保存（D11）。
        query_images: list[dict] = []
        if image_ids:
            try:
                query_images = await self.query_image.bind_for_history(
                    tenant_id, user_id, history_dict["id"], image_ids
                )
            except Exception as exc:  # noqa: BLE001
                # 兜尽一切：图片是附属于历史的，不能因为它让历史保存失败
                logger.warning(
                    "以图问知识：历史关联图片失败（历史已保存）history=%s err=%s",
                    history_dict["id"], exc,
                )
                query_images = []
        history_dict["query_images"] = query_images

        # [jonex] 超过 MAX_HISTORY_PER_USER 自动清理最早记录；被清掉的历史
        # 同样要释放它们独占的图片（D9 —— trim 是一种删除）。
        async with get_db_session() as session:
            repo = KnowledgeSearchHistoryRepository(session)
            trimmed = await repo.trim_for_user(tenant_id, user_id)
        if trimmed:
            await self._release(tenant_id, user_id, trimmed)
        return history_dict

    async def append_action_result(
        self, tenant_id: str, user_id: str, history_id: str, item: dict,
    ) -> bool:
        """服务端回写一条问答动作执行结果（只供执行器内部调用）。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            ok = await KnowledgeSearchHistoryRepository(session).append_action_result(
                tenant_id, user_id, history_id, item,
            )
            if ok:
                await session.commit()
            return ok

    async def delete_history(
        self,
        tenant_id: str,
        user_id: str,
        history_id: str,
        request: SearchHistoryDeleteRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchHistoryDeleteRequest(**_payload(request))
        async with get_db_session() as session:
            repo = KnowledgeSearchHistoryRepository(session)
            deleted = await repo.delete_for_user(
                tenant_id,
                user_id,
                req.knowledge_base_id,
                history_id,
                domain_space_id=req.domain_space_id,
            )
            if not deleted:
                raise ResourceNotFoundError(message=translate("err.search_history.not_found", fallback="搜索历史不存在")  )  # 原消息)
        # 被删的就是入参这一个 id（F22：该方法签名不变）
        await self._release(tenant_id, user_id, [history_id])
        return {"id": history_id, "deleted": True}

    async def clear_history(
        self, tenant_id: str, user_id: str, request: SearchOverviewRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchOverviewRequest(**_payload(request))
        async with get_db_session() as session:
            repo = KnowledgeSearchHistoryRepository(session)
            deleted_ids = await repo.clear_for_user(
                tenant_id, user_id, req.knowledge_base_id,
                domain_space_id=req.domain_space_id,
            )
        await self._release(tenant_id, user_id, deleted_ids)
        # 响应结构不变：仍是条数
        return {"deleted_count": len(deleted_ids)}

    async def get_overview(
        self, tenant_id: str, user_id: str, request: SearchOverviewRequest | dict,
    ) -> dict:
        req = SearchOverviewRequest(**_payload(request))
        history = await self.list_history(
            tenant_id,
            user_id,
            SearchHistoryListRequest(
                knowledge_base_id=req.knowledge_base_id or "",
                domain_space_id=req.domain_space_id,
                page=1,
                page_size=10,
            ),
        )
        return {
            "total_history": history["total"],
            "recent_items": history["items"],
        }

    async def _release(self, tenant_id: str, user_id: str, history_ids: list[str]) -> None:
        """释放这些历史独占的查询图片。**best-effort**：失败只 warning（D9）。"""
        if not history_ids:
            return
        try:
            await self.query_image.release_for_histories(tenant_id, user_id, history_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "以图问知识：释放查询图片失败（不影响历史删除；兜底扫描会补）: %s", exc
            )


def _refs(rows) -> list[dict]:
    """图片行 → 历史条目上的引用（``view_url`` 由 Gateway 补签）。"""
    return [row.to_ref() for row in (rows or [])]


__all__ = ["SearchHistoryService"]
