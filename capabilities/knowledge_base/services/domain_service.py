"""
知识库能力 — 领域服务管理
"""
from __future__ import annotations

import asyncio
import httpx
import logging
import uuid

from sqlalchemy import select

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import JonexException
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..repository import (
    DomainServiceRepository,
    ServiceKnowledgeBaseRepository,
    ServiceApiKeyRepository,
)
from ..models.domain_service import (
    DomainService,
    ServiceKnowledgeBase,
    ServiceApiKey,
    ServicePermission,
)

logger = logging.getLogger(__name__)


class DomainServiceService:
    """领域服务 CRUD + 知识库关联 + API Key + 配置 + 权限"""

    async def create(self, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = DomainServiceRepository(session)
            service_id = uuid.uuid4().hex
            obj = await repo.create(
                id=service_id,
                tenant_id=tenant_id,
                space_id=data["space_id"],
                name=data["name"],
                description=data.get("description"),
                domain_type=data.get("domain_type"),
                enabled=data.get("enabled", 1),
            )
            # Handle KB associations
            kb_ids = data.get("kb_ids", [])
            if kb_ids:
                kb_repo = ServiceKnowledgeBaseRepository(session)
                for kb_id in kb_ids:
                    await kb_repo.create(
                        id=uuid.uuid4().hex,
                        tenant_id=tenant_id,
                        service_id=service_id,
                        kb_id=kb_id,
                    )
            await session.commit()
            return obj.to_dict(include_kb_ids=kb_ids)

    async def get(self, service_id: str, tenant_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            from ..models.space import Space
            from ..models.knowledge_info import KnowledgeInfo

            repo = DomainServiceRepository(session)
            obj = await repo.get_required(service_id, tenant_id)
            kb_ids = await self._get_kb_ids(session, service_id, tenant_id)

            # Fetch space name
            space_name = ""
            space_row = await session.execute(
                select(Space.name).where(
                    Space.id == obj.space_id,
                    Space.tenant_id == tenant_id,
                    Space.is_deleted == 0,
                )
            )
            space_result = space_row.scalar()
            if space_result:
                space_name = space_result

            # Fetch KB names
            kb_name_map: dict[str, str] = {}
            if kb_ids:
                kb_rows = await session.execute(
                    select(KnowledgeInfo.id, KnowledgeInfo.name).where(
                        KnowledgeInfo.id.in_(kb_ids),
                        KnowledgeInfo.tenant_id == tenant_id,
                        KnowledgeInfo.is_deleted == 0,
                    )
                )
                kb_name_map = {row[0]: row[1] for row in kb_rows.all()}

            return obj.to_dict(include_kb_ids=kb_ids, space_name=space_name, kb_names=kb_name_map)

    @staticmethod
    async def _get_kb_ids(session, service_id: str, tenant_id: str) -> list[str]:
        result = await session.execute(
            select(ServiceKnowledgeBase.kb_id).where(
                ServiceKnowledgeBase.service_id == service_id,
                ServiceKnowledgeBase.tenant_id == tenant_id,
                ServiceKnowledgeBase.is_deleted == 0,
            )
        )
        return [row[0] for row in result.all()]

    async def list(self, tenant_id: str, space_id: str = None, offset: int = 0, limit: int = 20) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            from ..models.space import Space
            from ..models.knowledge_info import KnowledgeInfo

            repo = DomainServiceRepository(session)
            conditions = [DomainService.space_id == space_id] if space_id else []
            items = await repo.list_all(tenant_id, offset, limit, extra_conditions=conditions)
            total = await repo.count(tenant_id, extra_conditions=conditions)

            # Fetch KB associations for all services
            service_ids = [o.id for o in items]
            kb_map: dict[str, list[str]] = {}
            if service_ids:
                result = await session.execute(
                    select(ServiceKnowledgeBase.service_id, ServiceKnowledgeBase.kb_id).where(
                        ServiceKnowledgeBase.service_id.in_(service_ids),
                        ServiceKnowledgeBase.tenant_id == tenant_id,
                        ServiceKnowledgeBase.is_deleted == 0,
                    )
                )
                for service_id, kb_id in result.all():
                    kb_map.setdefault(service_id, []).append(kb_id)

            # Batch fetch space names
            space_ids = list({o.space_id for o in items})
            space_name_map: dict[str, str] = {}
            if space_ids:
                space_rows = await session.execute(
                    select(Space.id, Space.name).where(
                        Space.id.in_(space_ids),
                        Space.tenant_id == tenant_id,
                        Space.is_deleted == 0,
                    )
                )
                space_name_map = {row[0]: row[1] for row in space_rows.all()}

            # Batch fetch knowledge base names
            all_kb_ids: list[str] = []
            for kbs in kb_map.values():
                all_kb_ids.extend(kbs)
            kb_name_map: dict[str, str] = {}
            if all_kb_ids:
                kb_rows = await session.execute(
                    select(KnowledgeInfo.id, KnowledgeInfo.name).where(
                        KnowledgeInfo.id.in_(all_kb_ids),
                        KnowledgeInfo.tenant_id == tenant_id,
                        KnowledgeInfo.is_deleted == 0,
                    )
                )
                kb_name_map = {row[0]: row[1] for row in kb_rows.all()}

            return {
                "items": [
                    o.to_dict(
                        include_kb_ids=kb_map.get(o.id, []),
                        space_name=space_name_map.get(o.space_id, ""),
                        kb_names=kb_name_map,
                    )
                    for o in items
                ],
                "total": total, "offset": offset, "limit": limit,
            }

    async def update(self, service_id: str, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = DomainServiceRepository(session)
            obj = await repo.get_required(service_id, tenant_id)
            updatable = {"name", "description", "domain_type", "status", "enabled"}
            values = {k: v for k, v in data.items() if k in updatable and v is not None}
            if values:
                obj = await repo.update(service_id, tenant_id, **values)

            # Handle KB associations update
            if "kb_ids" in data:
                kb_repo = ServiceKnowledgeBaseRepository(session)
                # Remove existing KB associations
                existing = await session.execute(
                    select(ServiceKnowledgeBase).where(
                        ServiceKnowledgeBase.service_id == service_id,
                        ServiceKnowledgeBase.tenant_id == tenant_id,
                        ServiceKnowledgeBase.is_deleted == 0,
                    )
                )
                for skb in existing.scalars().all():
                    await session.delete(skb)

                # Add new KB associations
                new_kb_ids = data.get("kb_ids", [])
                for kb_id in new_kb_ids:
                    await kb_repo.create(
                        id=uuid.uuid4().hex,
                        tenant_id=tenant_id,
                        service_id=service_id,
                        kb_id=kb_id,
                    )

            await session.commit()
            kb_ids = await self._get_kb_ids(session, service_id, tenant_id)
            return obj.to_dict(include_kb_ids=kb_ids)

    async def delete(self, service_id: str, tenant_id: str) -> bool:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = DomainServiceRepository(session)
            await repo.get_required(service_id, tenant_id)
            await repo.delete_soft(service_id, tenant_id)

            # Cascade delete KB associations
            existing_kb = await session.execute(
                select(ServiceKnowledgeBase).where(
                    ServiceKnowledgeBase.service_id == service_id,
                    ServiceKnowledgeBase.tenant_id == tenant_id,
                    ServiceKnowledgeBase.is_deleted == 0,
                )
            )
            for skb in existing_kb.scalars().all():
                await session.delete(skb)

            # Cascade delete permissions
            existing_perm = await session.execute(
                select(ServicePermission).where(
                    ServicePermission.service_id == service_id,
                    ServicePermission.tenant_id == tenant_id,
                    ServicePermission.is_deleted == 0,
                )
            )
            for sp in existing_perm.scalars().all():
                await session.delete(sp)

            await session.commit()
            return True

    async def enable(self, service_id: str, tenant_id: str) -> dict:
        """启用领域服务"""
        return await self.update(service_id, tenant_id, {"enabled": 1})

    async def disable(self, service_id: str, tenant_id: str) -> dict:
        """停用领域服务"""
        return await self.update(service_id, tenant_id, {"enabled": 0})

    async def rotate_api_key(self, service_id: str, tenant_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = DomainServiceRepository(session)
            await repo.get_required(service_id, tenant_id)
            new_key = uuid.uuid4().hex
            await repo.update(service_id, tenant_id, api_key_encrypted=new_key)
            await session.commit()
            return {"api_key": new_key}

    # ── API Key multi-key management ──

    async def list_api_keys(self, service_id: str, tenant_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            result = await session.execute(
                select(ServiceApiKey).where(
                    ServiceApiKey.service_id == service_id,
                    ServiceApiKey.tenant_id == tenant_id,
                    ServiceApiKey.is_deleted == 0,
                ).order_by(ServiceApiKey.created_at.desc())
            )
            items = [row.to_dict() for row in result.scalars().all()]
            return {"items": items, "total": len(items)}

    async def create_api_key(self, service_id: str, tenant_id: str, data: dict) -> dict:
        from datetime import datetime, timedelta

        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = DomainServiceRepository(session)
            await repo.get_required(service_id, tenant_id)

            key_repo = ServiceApiKeyRepository(session)
            new_key = uuid.uuid4().hex
            prefix = data.get("key_prefix", "sk")
            expires_in_days = data.get("expires_in_days", 365)
            expires_at = datetime.utcnow() + timedelta(days=int(expires_in_days))

            obj = await key_repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                service_id=service_id,
                key_prefix=prefix,
                key_encrypted=new_key,
                expires_at=expires_at,
                is_active=1,
            )
            await session.commit()
            return obj.to_dict()

    async def delete_api_key(self, key_id: str, service_id: str, tenant_id: str) -> bool:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            key_repo = ServiceApiKeyRepository(session)
            await key_repo.get_required(key_id, tenant_id)
            # Ensure the key belongs to the given service
            key_obj = await session.get(ServiceApiKey, key_id)
            if not key_obj or key_obj.service_id != service_id:
                raise JonexException(message=translate("err.service.api_key_not_belong", fallback="API Key 不属于该服务"))
            await key_repo.delete_soft(key_id, tenant_id)
            await session.commit()
            return True

    async def get_configs(self, service_id: str, tenant_id: str) -> dict:
        require_tenant(tenant_id)
        return {"items": [], "total": 0}

    async def update_configs(self, service_id: str, tenant_id: str, configs: dict) -> bool:
        require_tenant(tenant_id)
        return True

    async def get_permissions(self, service_id: str, tenant_id: str) -> list:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            result = await session.execute(
                select(ServicePermission).where(
                    ServicePermission.service_id == service_id,
                    ServicePermission.tenant_id == tenant_id,
                    ServicePermission.is_deleted == 0,
                )
            )
            return [
                {
                    "id": row.id,
                    "user_id": row.user_id,
                    "role": row.role,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in result.scalars().all()
            ]

    async def set_permissions(self, service_id: str, tenant_id: str, permissions: list) -> bool:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            # Remove existing permissions
            existing = await session.execute(
                select(ServicePermission).where(
                    ServicePermission.service_id == service_id,
                    ServicePermission.tenant_id == tenant_id,
                    ServicePermission.is_deleted == 0,
                )
            )
            for sp in existing.scalars().all():
                await session.delete(sp)

            # Add new permissions
            for perm in permissions:
                perm_obj = ServicePermission(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    user_id=perm["user_id"],
                    role=perm.get("role", "viewer"),
                )
                session.add(perm_obj)

            await session.commit()
            return True

    async def search(self, service_id: str, tenant_id: str, query: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = DomainServiceRepository(session)
            await repo.get_required(service_id, tenant_id)
            kb_ids = await self._get_kb_ids(session, service_id, tenant_id)

        if not kb_ids:
            return {
                "service_id": service_id,
                "query": query,
                "items": [],
                "total": 0,
                "references": [],
                "kb_count": 0,
            }

        from ..services.search_service import SearchService

        async def _search_kb(kb_id: str) -> dict | None:
            try:
                return await SearchService().search(
                    tenant_id=tenant_id,
                    user_id="mcp",
                    request={
                        "knowledge_base_id": kb_id,
                        "query": query,
                        "mode": "hybrid",
                        "top_k": 5,
                    },
                    trace_id="",
                )
            except (JonexException, httpx.HTTPError, asyncio.TimeoutError) as e:
                logger.warning(
                    "DomainService search: KB %s search failed for service %s: %s",
                    kb_id, service_id, e,
                )
                return None

        tasks = [_search_kb(kb_id) for kb_id in kb_ids]
        results = await asyncio.gather(*tasks)

        all_refs: dict[str, dict] = {}
        all_items: list = []
        total: int = 0
        answers: list[str] = []

        for result in results:
            if result is None:
                continue
            for ref in result.get("references", []):
                ref_id = ref.get("id")
                if ref_id and ref_id not in all_refs:
                    all_refs[ref_id] = ref
            items = result.get("items", [])
            if items:
                all_items.extend(items)
            total += result.get("total", 0)
            answer = result.get("answer")
            if answer:
                answers.append(str(answer))

        result_dict: dict = {
            "service_id": service_id,
            "query": query,
            "references": list(all_refs.values()),
            "items": all_items,
            "total": total,
            "kb_count": len(kb_ids),
        }
        if answers:
            result_dict["answer"] = "\n\n".join(answers)

        return result_dict
