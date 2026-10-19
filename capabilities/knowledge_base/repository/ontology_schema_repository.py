"""
Repository for ontology template bindings and compiled schemas.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import ResourceConflictError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models.ontology_schema import OntologyCompiledSchema, OntologyTemplateBinding


class OntologySchemaRepository:
    """Tenant-scoped CRUD for ontology bindings and compiled schema snapshots."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_binding(
        self,
        tenant_id: str,
        knowledge_base_id: str,
    ) -> Optional[OntologyTemplateBinding]:
        stmt = select(OntologyTemplateBinding).where(
            OntologyTemplateBinding.tenant_id == require_tenant(tenant_id),
            OntologyTemplateBinding.knowledge_base_id == knowledge_base_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_binding(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        template_domain_id: Optional[str] = None,
        template_scenario_id: Optional[str] = None,
        source_type: str = "business_template",
    ) -> OntologyTemplateBinding:
        binding = await self.get_binding(tenant_id, knowledge_base_id)
        if binding:
            binding.template_domain_id = template_domain_id
            binding.template_scenario_id = template_scenario_id
            binding.source_type = source_type
            # 同 `mark_schema_outdated`：naive 列不能赋 aware datetime。
            binding.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            binding = OntologyTemplateBinding(
                tenant_id=require_tenant(tenant_id),
                knowledge_base_id=knowledge_base_id,
                template_domain_id=template_domain_id,
                template_scenario_id=template_scenario_id,
                source_type=source_type,
            )
            self.session.add(binding)
        await self.session.flush()
        return binding

    async def delete_binding(self, tenant_id: str, knowledge_base_id: str) -> bool:
        binding = await self.get_binding(tenant_id, knowledge_base_id)
        if binding is None:
            return False
        await self.session.delete(binding)
        await self.session.flush()
        return True

    async def list_by_template_scenario(
        self,
        tenant_id: str,
        template_scenario_id: str,
    ) -> list[OntologyTemplateBinding]:
        stmt = select(OntologyTemplateBinding).where(
            OntologyTemplateBinding.tenant_id == require_tenant(tenant_id),
            OntologyTemplateBinding.template_scenario_id == template_scenario_id,
            OntologyTemplateBinding.status == "active",
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def get_compiled_schema(
        self,
        tenant_id: str,
        knowledge_base_id: str,
    ) -> Optional[OntologyCompiledSchema]:
        stmt = select(OntologyCompiledSchema).where(
            OntologyCompiledSchema.tenant_id == require_tenant(tenant_id),
            OntologyCompiledSchema.knowledge_base_id == knowledge_base_id,
            OntologyCompiledSchema.status == "active",
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_compiled_schema(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        template_domain_id: Optional[str],
        template_scenario_id: Optional[str],
        source_type: str,
        source_version: int,
        source_hash: Optional[str],
        entity_types: list,
        relation_types: list,
        constraints: list,
        disambiguation: dict,
        prompt_schema: dict,
        schema_mode: str = "template_seeded",
        sync_status: str = "synced",
        edited_at: Optional[datetime] = None,
        edited_by: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> OntologyCompiledSchema:
        old = await self.get_compiled_schema(tenant_id, knowledge_base_id)
        now = datetime.now(timezone.utc)
        now_naive = datetime.utcnow()
        if old:
            if expected_version is not None:
                # 原子乐观锁：仅当 DB 版本仍等于 expected 时更新并自增；否则并发冲突。
                # 用 Core UPDATE 判 rowcount，避免 service 层"读-比对-写"竞态。
                stmt = (
                    update(OntologyCompiledSchema)
                    .where(
                        OntologyCompiledSchema.id == old.id,
                        OntologyCompiledSchema.schema_version == expected_version,
                    )
                    .values(
                        template_domain_id=template_domain_id,
                        template_scenario_id=template_scenario_id,
                        source_type=source_type,
                        source_version=source_version,
                        source_hash=source_hash,
                        schema_version=expected_version + 1,
                        entity_types=entity_types,
                        relation_types=relation_types,
                        constraints=constraints,
                        disambiguation=disambiguation,
                        prompt_schema=prompt_schema,
                        status="active",
                        schema_mode=schema_mode,
                        sync_status=sync_status,
                        edited_at=edited_at,
                        edited_by=edited_by,
                        compiled_at=now,
                        updated_at=now_naive,
                    )
                    .returning(OntologyCompiledSchema.id)
                )
                result = await self.session.execute(stmt)
                if result.first() is None:
                    raise ResourceConflictError(
                        message=translate("err.ontology.schema_stale", fallback="schema 已被更新，请刷新后重试"),
                        details={"expected_schema_version": expected_version},
                    )
                # Core UPDATE 不会刷新 identity map 中已加载的 old，过期后重取最新对象
                self.session.expire(old)
                return await self.get_compiled_schema(tenant_id, knowledge_base_id)

            old.template_domain_id = template_domain_id
            old.template_scenario_id = template_scenario_id
            old.source_type = source_type
            old.source_version = source_version
            old.source_hash = source_hash
            old.schema_version = (old.schema_version or 0) + 1
            old.entity_types = entity_types
            old.relation_types = relation_types
            old.constraints = constraints
            old.disambiguation = disambiguation
            old.prompt_schema = prompt_schema
            old.status = "active"
            old.schema_mode = schema_mode
            old.sync_status = sync_status
            old.edited_at = edited_at
            old.edited_by = edited_by
            old.compiled_at = now
            old.updated_at = now_naive
            compiled = old
        else:
            compiled = OntologyCompiledSchema(
                tenant_id=require_tenant(tenant_id),
                knowledge_base_id=knowledge_base_id,
                template_domain_id=template_domain_id,
                template_scenario_id=template_scenario_id,
                source_type=source_type,
                source_version=source_version,
                source_hash=source_hash,
                schema_version=1,
                entity_types=entity_types,
                relation_types=relation_types,
                constraints=constraints,
                disambiguation=disambiguation,
                prompt_schema=prompt_schema,
                status="active",
                schema_mode=schema_mode,
                sync_status=sync_status,
                edited_at=edited_at,
                edited_by=edited_by,
                compiled_at=now,
            )
            self.session.add(compiled)
        await self.session.flush()
        return compiled

    async def mark_schema_outdated(
        self,
        tenant_id: str,
        knowledge_base_id: str,
    ) -> bool:
        schema = await self.get_compiled_schema(tenant_id, knowledge_base_id)
        if schema is None:
            return False
        schema.sync_status = "outdated"
        # 列是 TIMESTAMP WITHOUT TIME ZONE（`TimestampMixin` 的 DateTime），
        # 赋 aware datetime 会让 asyncpg 抛「can't subtract offset-naive and
        # offset-aware datetimes」，表现为发布联动「执行失败」。
        schema.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await self.session.flush()
        return True


__all__ = ["OntologySchemaRepository"]
