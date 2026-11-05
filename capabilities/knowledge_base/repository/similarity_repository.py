"""相似检测：签名表 + 合并建议表仓储。"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.similarity import (
    STATUS_DISMISSED,
    STATUS_INVALID,
    STATUS_MERGED,
    STATUS_PENDING,
    STATUS_SUPERSEDED,
    KnowledgeDocumentSignature,
    KnowledgeDocumentSimilaritySuggestion,
)

_SET_STATUS_ALLOWED = frozenset({
    STATUS_PENDING,
    STATUS_MERGED,
    STATUS_DISMISSED,
    STATUS_SUPERSEDED,
    STATUS_INVALID,
})


class KnowledgeSimilarityRepository(BaseRepository[KnowledgeDocumentSimilaritySuggestion]):
    model = KnowledgeDocumentSimilaritySuggestion

    # ── 签名 ──
    async def upsert_signature(
        self, tenant_id: str, knowledge_base_id: str, document_id: str,
        *, simhash: int | None, text_chars: int, text_source: str,
    ) -> None:
        tenant_id = require_tenant(tenant_id)
        stmt = pg_insert(KnowledgeDocumentSignature).values(
            document_id=document_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            simhash=simhash,
            text_chars=text_chars,
            text_source=text_source,
            # 与重试 SQL 的 now() 同源（DB 时钟），避免容器时钟偏移让 TTL 判早/判晚。
            updated_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[KnowledgeDocumentSignature.document_id],
            set_={
                "simhash": stmt.excluded.simhash,
                "text_chars": stmt.excluded.text_chars,
                "text_source": stmt.excluded.text_source,
                "updated_at": func.now(),
            },
            # 只有同租户的既有行才允许覆盖（跨租户同 document_id 不得被改写）；
            # 且 NULL 哨兵只能写新行或覆盖既有 NULL 行，绝不覆盖有效签名。
            where=and_(
                KnowledgeDocumentSignature.tenant_id == tenant_id,
                or_(
                    stmt.excluded.simhash.isnot(None),
                    KnowledgeDocumentSignature.simhash.is_(None),
                ),
            ),
        )
        result = await self.session.execute(
            stmt.returning(KnowledgeDocumentSignature)
            .execution_options(populate_existing=True)
        )
        result.scalars().all()  # 触发 populate_existing，刷新已加载的 ORM 实例

    async def get_signature(
        self, tenant_id: str, document_id: str,
    ) -> KnowledgeDocumentSignature | None:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(KnowledgeDocumentSignature).where(
                KnowledgeDocumentSignature.document_id == document_id,
                KnowledgeDocumentSignature.tenant_id == tenant_id,
            )
        )
        return result.scalars().first()

    async def list_signatures(
        self, tenant_id: str, knowledge_base_id: str,
    ) -> list[KnowledgeDocumentSignature]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(KnowledgeDocumentSignature).where(
                KnowledgeDocumentSignature.tenant_id == tenant_id,
                KnowledgeDocumentSignature.knowledge_base_id == knowledge_base_id,
                # 只返回真实签名；simhash IS NULL 是「无可用文本」哨兵行。
                KnowledgeDocumentSignature.simhash.isnot(None),
            )
        )
        return list(result.scalars())

    async def delete_signature(self, tenant_id: str, document_id: str) -> None:
        tenant_id = require_tenant(tenant_id)
        await self.session.execute(
            delete(KnowledgeDocumentSignature).where(
                KnowledgeDocumentSignature.document_id == document_id,
                KnowledgeDocumentSignature.tenant_id == tenant_id,
            )
        )

    # ── 建议 ──
    async def upsert_suggestion(
        self, tenant_id: str, knowledge_base_id: str,
        source_document_id: str, target_document_id: str,
        similarity: float, method: str = "text_lines",
        reset_dismissed: bool = True,
    ) -> KnowledgeDocumentSimilaritySuggestion:
        tenant_id = require_tenant(tenant_id)
        stmt = pg_insert(KnowledgeDocumentSimilaritySuggestion).values(
            id=uuid4().hex,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            source_document_id=source_document_id,
            target_document_id=target_document_id,
            similarity=similarity,
            method=method,
            status=STATUS_PENDING,
        )
        update_values = {
            "similarity": stmt.excluded.similarity,
            "method": stmt.excluded.method,
            "updated_at": func.now(),
            # 非源侧重解析：dismissed 保持 dismissed（用户忽略是人的决定），
            # 其余状态（pending/superseded/invalid）都回 pending。
            "status": case(
                (
                    KnowledgeDocumentSimilaritySuggestion.status == STATUS_DISMISSED,
                    STATUS_DISMISSED,
                ),
                else_=STATUS_PENDING,
            ),
        }
        if reset_dismissed:
            # 仅源文档重解析（本轮扫描文档）才允许把已忽略的建议复活。
            update_values.update(
                status=STATUS_PENDING, dismissed_by=None, dismissed_at=None,
            )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_kb_doc_sim_pair",
            set_=update_values,
        ).returning(KnowledgeDocumentSimilaritySuggestion).execution_options(
            populate_existing=True,
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_pending_for_document(
        self, tenant_id: str, document_id: str,
    ) -> list[KnowledgeDocumentSimilaritySuggestion]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(KnowledgeDocumentSimilaritySuggestion).where(
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
                KnowledgeDocumentSimilaritySuggestion.source_document_id == document_id,
                KnowledgeDocumentSimilaritySuggestion.status == STATUS_PENDING,
            ).order_by(KnowledgeDocumentSimilaritySuggestion.similarity.desc())
        )
        return list(result.scalars())

    async def list_pending_for_documents(
        self, tenant_id: str, document_ids: list[str],
    ) -> list[KnowledgeDocumentSimilaritySuggestion]:
        tenant_id = require_tenant(tenant_id)
        if not document_ids:
            return []
        result = await self.session.execute(
            select(KnowledgeDocumentSimilaritySuggestion).where(
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
                KnowledgeDocumentSimilaritySuggestion.source_document_id.in_(document_ids),
                KnowledgeDocumentSimilaritySuggestion.status == STATUS_PENDING,
            )
        )
        return list(result.scalars())

    async def get_suggestion(
        self, tenant_id: str, suggestion_id: str,
    ) -> KnowledgeDocumentSimilaritySuggestion | None:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(KnowledgeDocumentSimilaritySuggestion).where(
                KnowledgeDocumentSimilaritySuggestion.id == suggestion_id,
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
            )
        )
        return result.scalars().first()

    async def set_status(
        self, tenant_id: str, suggestion_id: str, status: str,
        *, dismissed_by: str | None = None,
    ) -> None:
        tenant_id = require_tenant(tenant_id)
        if status not in _SET_STATUS_ALLOWED:
            raise InvalidParameterError(
                message=f"invalid suggestion status: {status}",
                details={"status": status},
            )
        values = {"status": status, "updated_at": func.now()}
        if status == STATUS_DISMISSED:
            values["dismissed_by"] = dismissed_by
            values["dismissed_at"] = func.now()
        await self.session.execute(
            update(KnowledgeDocumentSimilaritySuggestion)
            .where(
                KnowledgeDocumentSimilaritySuggestion.id == suggestion_id,
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
            )
            .values(**values)
        )

    async def dismiss_if_pending(
        self, tenant_id: str, suggestion_id: str,
        *, dismissed_by: str | None = None,
    ) -> bool:
        """原子地把 pending 建议置为 dismissed；与合并竞态只有一方返回 True。"""
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            update(KnowledgeDocumentSimilaritySuggestion)
            .where(
                KnowledgeDocumentSimilaritySuggestion.id == suggestion_id,
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
                KnowledgeDocumentSimilaritySuggestion.status == STATUS_PENDING,
            )
            .values(
                status=STATUS_DISMISSED,
                dismissed_by=dismissed_by,
                dismissed_at=func.now(),
                updated_at=func.now(),
            )
        )
        return (result.rowcount or 0) > 0

    async def merge_if_pending(
        self, tenant_id: str, suggestion_id: str,
    ) -> bool:
        """原子地把 pending 建议置为 merged；并发双击只有一方返回 True。"""
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            update(KnowledgeDocumentSimilaritySuggestion)
            .where(
                KnowledgeDocumentSimilaritySuggestion.id == suggestion_id,
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
                KnowledgeDocumentSimilaritySuggestion.status == STATUS_PENDING,
            )
            .values(status=STATUS_MERGED, updated_at=func.now())
        )
        return (result.rowcount or 0) > 0

    async def supersede_pending_for_source(self, tenant_id: str, document_id: str) -> None:
        tenant_id = require_tenant(tenant_id)
        await self.session.execute(
            update(KnowledgeDocumentSimilaritySuggestion)
            .where(
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
                KnowledgeDocumentSimilaritySuggestion.source_document_id == document_id,
                KnowledgeDocumentSimilaritySuggestion.status == STATUS_PENDING,
            )
            .values(status="superseded", updated_at=func.now())
        )

    async def supersede_pending_for_document(
        self, tenant_id: str, document_id: str,
    ) -> None:
        """清掉所有涉及本文档的 pending（无论 source 还是 target 侧）。"""
        tenant_id = require_tenant(tenant_id)
        await self.session.execute(
            update(KnowledgeDocumentSimilaritySuggestion)
            .where(
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
                KnowledgeDocumentSimilaritySuggestion.status == STATUS_PENDING,
                or_(
                    KnowledgeDocumentSimilaritySuggestion.source_document_id == document_id,
                    KnowledgeDocumentSimilaritySuggestion.target_document_id == document_id,
                ),
            )
            .values(status="superseded", updated_at=func.now())
        )

    async def supersede_other_pending(
        self, tenant_id: str, document_id: str, keep_id: str,
    ) -> None:
        tenant_id = require_tenant(tenant_id)
        await self.session.execute(
            update(KnowledgeDocumentSimilaritySuggestion)
            .where(
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
                KnowledgeDocumentSimilaritySuggestion.source_document_id == document_id,
                KnowledgeDocumentSimilaritySuggestion.status == STATUS_PENDING,
                KnowledgeDocumentSimilaritySuggestion.id != keep_id,
            )
            .values(status="superseded", updated_at=func.now())
        )

    async def invalidate_for_document(self, tenant_id: str, document_id: str) -> None:
        tenant_id = require_tenant(tenant_id)
        await self.session.execute(
            update(KnowledgeDocumentSimilaritySuggestion)
            .where(
                KnowledgeDocumentSimilaritySuggestion.tenant_id == tenant_id,
                KnowledgeDocumentSimilaritySuggestion.status == STATUS_PENDING,
                or_(
                    KnowledgeDocumentSimilaritySuggestion.source_document_id == document_id,
                    KnowledgeDocumentSimilaritySuggestion.target_document_id == document_id,
                ),
            )
            .values(status="invalid", updated_at=func.now())
        )

    async def list_documents_missing_signature(
        self, tenant_id: str, knowledge_base_id: str, limit: int,
    ) -> list[str]:
        tenant_id = require_tenant(tenant_id)
        from sqlalchemy import text

        result = await self.session.execute(
            text("""
                SELECT d.id
                  FROM knowledge_base.knowledge_documents d
                  LEFT JOIN knowledge_base.knowledge_document_signatures s
                    ON s.document_id = d.id
                 WHERE d.tenant_id = :t AND d.knowledge_base_id = :kb
                   AND d.is_deleted = 0 AND d.status = 'ready'
                   AND s.document_id IS NULL
                 ORDER BY d.created_at ASC, d.id
                 LIMIT :lim
            """),
            {"t": tenant_id, "kb": knowledge_base_id, "lim": limit},
        )
        return [row[0] for row in result]

    async def list_documents_retry_unavailable(
        self, tenant_id: str, knowledge_base_id: str, limit: int,
        older_than_seconds: int,
    ) -> list[str]:
        """unavailable 哨兵超过 TTL 的文档（用于重算，避免一次性故障被永久固化）。"""
        tenant_id = require_tenant(tenant_id)
        from sqlalchemy import text

        result = await self.session.execute(
            text("""
                SELECT s.document_id
                  FROM knowledge_base.knowledge_document_signatures s
                  JOIN knowledge_base.knowledge_documents d
                    ON d.id = s.document_id AND d.tenant_id = s.tenant_id
                 WHERE s.tenant_id = :t AND s.knowledge_base_id = :kb
                   AND s.simhash IS NULL
                   AND s.text_source LIKE 'unavailable:%'
                   AND s.updated_at <= now() - make_interval(secs => :age)
                   AND d.is_deleted = 0 AND d.status = 'ready'
                 ORDER BY s.updated_at ASC
                 LIMIT :lim
            """),
            {"t": tenant_id, "kb": knowledge_base_id,
             "age": older_than_seconds, "lim": limit},
        )
        return [row[0] for row in result]
