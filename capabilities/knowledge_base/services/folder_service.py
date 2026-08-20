#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KB-level folder CRUD service."""

import uuid

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import ResourceConflictError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..repository.document_repository import KnowledgeDocumentRepository, UNCLASSIFIED_SENTINEL
from ..repository.folder_repository import FolderRepository
from ..repository.knowledge_info_repository import KnowledgeInfoRepository


class FolderService:
    """知识库文件夹 CRUD 服务。"""

    async def _ensure_kb(self, session, kb_id: str, tenant_id: str) -> None:
        """校验 KB 归属存在（不存在抛 ResourceNotFoundError）。"""
        await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)

    async def list_folders(self, tenant_id: str, knowledge_base_id: str) -> dict:
        """列出 KB 下所有未删文件夹，附带各目录文档数与未分类文档数。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            await self._ensure_kb(session, knowledge_base_id, tenant_id)
            repo = FolderRepository(session)
            items = await repo.list_by_kb(tenant_id, knowledge_base_id)
            counts = await KnowledgeDocumentRepository(session).count_by_folder(
                tenant_id, knowledge_base_id
            )
            uncategorized = counts.pop(UNCLASSIFIED_SENTINEL, 0)
            result_items = []
            for f in items:
                d = f.to_dict()
                d["document_count"] = counts.get(f.id, 0)
                result_items.append(d)
            return {
                "items": result_items,
                "total": len(result_items),
                "uncategorized_count": uncategorized,
            }

    async def create_folder(self, tenant_id: str, data: dict) -> dict:
        """创建文件夹（同 KB 内名称唯一）。"""
        tenant_id = require_tenant(tenant_id)
        kb_id = data["knowledge_base_id"]
        name = data["name"]
        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            repo = FolderRepository(session)

            existing = await repo.get_by_name(tenant_id, kb_id, name)
            if existing:
                raise ResourceConflictError(message=translate("err.folder.name_exists", params={"name": name}, fallback=f"文件夹名称已存在: {name}")  )  # 原消息)

            obj = await repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                name=name,
                is_preset=0,
                sort_order=0,
            )
            await session.commit()
            return obj.to_dict()

    async def rename_folder(
        self, tenant_id: str, folder_id: str, knowledge_base_id: str, new_name: str
    ) -> dict:
        """重命名文件夹（同 KB 内名称唯一）。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = FolderRepository(session)

            # 获取文件夹并校验 KB 归属
            obj = await repo.get_required(folder_id, tenant_id)
            if obj.knowledge_base_id != knowledge_base_id:
                raise ResourceNotFoundError(
                    message=translate("err.folder.not_belong_to_kb", fallback="文件夹不属于该知识库")  ,  # 原消息
                    details={"folder_id": folder_id, "knowledge_base_id": knowledge_base_id},
                )

            # 校验名称唯一性（使用 folder 实际所属 KB）
            existing = await repo.get_by_name(tenant_id, obj.knowledge_base_id, new_name)
            if existing and existing.id != folder_id:
                raise ResourceConflictError(message=translate("err.folder.name_exists", params={"name": new_name}, fallback=f"文件夹名称已存在: {new_name}")  )  # 原消息)

            obj.name = new_name
            await session.commit()
            return obj.to_dict()

    async def delete_folder(
        self, tenant_id: str, folder_id: str, knowledge_base_id: str
    ) -> bool:
        """删除文件夹：先将关联文档的 folder_id 置 NULL，再软删除文件夹。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = FolderRepository(session)
            doc_repo = KnowledgeDocumentRepository(session)

            obj = await repo.get_required(folder_id, tenant_id)
            if obj.knowledge_base_id != knowledge_base_id:
                raise ResourceNotFoundError(
                    message=translate("err.folder.not_belong_to_kb", fallback="文件夹不属于该知识库")  ,  # 原消息
                    details={"folder_id": folder_id, "knowledge_base_id": knowledge_base_id},
                )

            # 解除关联文档
            docs = await doc_repo.list_by_folder(tenant_id, folder_id)
            for doc in docs:
                doc.folder_id = None

            await repo.delete_soft(obj)
            await session.commit()
            return True


__all__ = ["FolderService"]
