#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""文档版本链仓储（append-only）。"""

from sqlalchemy import func, select

from jonex_core.common.repository import BaseRepository

from ..models.document import KnowledgeDocumentVersion


class KnowledgeDocumentVersionRepository(BaseRepository[KnowledgeDocumentVersion]):
    model = KnowledgeDocumentVersion

    async def list_for_document(
        self, tenant_id: str, document_id: str,
    ) -> list[KnowledgeDocumentVersion]:
        result = await self.session.execute(
            select(KnowledgeDocumentVersion)
            .where(
                KnowledgeDocumentVersion.tenant_id == tenant_id,
                KnowledgeDocumentVersion.document_id == document_id,
            )
            .order_by(KnowledgeDocumentVersion.version_no.desc())
        )
        return list(result.scalars())

    async def get_version(
        self, tenant_id: str, document_id: str, version_no: int,
    ) -> KnowledgeDocumentVersion | None:
        result = await self.session.execute(
            select(KnowledgeDocumentVersion).where(
                KnowledgeDocumentVersion.tenant_id == tenant_id,
                KnowledgeDocumentVersion.document_id == document_id,
                KnowledgeDocumentVersion.version_no == version_no,
            )
        )
        return result.scalars().first()

    async def next_version_no(self, tenant_id: str, document_id: str) -> int:
        result = await self.session.execute(
            select(func.max(KnowledgeDocumentVersion.version_no)).where(
                KnowledgeDocumentVersion.tenant_id == tenant_id,
                KnowledgeDocumentVersion.document_id == document_id,
            )
        )
        current = result.scalar_one_or_none()
        return int(current or 0) + 1
