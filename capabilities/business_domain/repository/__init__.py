"""
业务领域 — Repository 层。
"""
from capabilities.business_domain.repository.base import BaseRepository
from capabilities.business_domain.models import (
    Adapter, DataAccessMethod, ModelProvider,
    ParserConfig, SkillCatalog, TenantSkill, TemplateAttribute, TemplateConstraint,
    TemplateDomain, TemplateObject, TemplateRelation, TemplateScenario,
    TemplateScenarioVersion, TemplatePublishRequest,
)


class DataAccessMethodRepository(BaseRepository[DataAccessMethod]):
    model = DataAccessMethod


class ParserConfigRepository(BaseRepository[ParserConfig]):
    model = ParserConfig


class ModelProviderRepository(BaseRepository[ModelProvider]):
    model = ModelProvider


class AdapterRepository(BaseRepository[Adapter]):
    model = Adapter


class SkillCatalogRepository(BaseRepository[SkillCatalog]):
    """平台共享 Skill 目录仓库（不传租户）"""
    model = SkillCatalog


class TenantSkillRepository(BaseRepository[TenantSkill]):
    """租户技能启用状态仓库（必须传租户）"""
    model = TenantSkill


class TemplateDomainRepository(BaseRepository[TemplateDomain]):
    model = TemplateDomain


class TemplateScenarioRepository(BaseRepository[TemplateScenario]):
    model = TemplateScenario


class TemplateObjectRepository(BaseRepository[TemplateObject]):
    model = TemplateObject


class TemplateAttributeRepository(BaseRepository[TemplateAttribute]):
    model = TemplateAttribute


class TemplateRelationRepository(BaseRepository[TemplateRelation]):
    model = TemplateRelation


from capabilities.business_domain.models.prompt_template import PromptTemplate


class PromptTemplateRepository(BaseRepository[PromptTemplate]):
    """提示词模板仓储 — 实际实现在 prompt_template_repository.py。
    此处注册基础 BaseRepository 子类供简单操作；复杂查询使用独立文件中的专用方法。"""
    model = PromptTemplate
class TemplateConstraintRepository(BaseRepository[TemplateConstraint]):
    model = TemplateConstraint


class TemplateScenarioVersionRepository(BaseRepository[TemplateScenarioVersion]):
    """模板场景发布快照（append-only）。"""

    model = TemplateScenarioVersion

    async def list_for_scenario(
        self, tenant_id: str, scenario_id: str, offset: int = 0, limit: int = 20,
    ) -> list[TemplateScenarioVersion]:
        return await self.list_all(
            tenant_id, offset, limit,
            extra_conditions=[TemplateScenarioVersion.scenario_id == scenario_id],
        )

    async def get_version(
        self, tenant_id: str, scenario_id: str, version: int,
    ) -> TemplateScenarioVersion | None:
        from sqlalchemy import select

        result = await self.session.execute(
            select(TemplateScenarioVersion).where(
                TemplateScenarioVersion.tenant_id == tenant_id,
                TemplateScenarioVersion.scenario_id == scenario_id,
                TemplateScenarioVersion.version == version,
            )
        )
        return result.scalars().first()


class TemplatePublishRequestRepository(BaseRepository[TemplatePublishRequest]):
    """模板场景发布申请（审批流）。"""

    model = TemplatePublishRequest

    async def list_for_scenario(
        self,
        tenant_id: str,
        scenario_id: str,
        *,
        submitted_by: str | None = None,
        limit: int = 100,
    ) -> list[TemplatePublishRequest]:
        from sqlalchemy import select

        conditions = [
            TemplatePublishRequest.tenant_id == tenant_id,
            TemplatePublishRequest.scenario_id == scenario_id,
        ]
        if submitted_by:
            conditions.append(TemplatePublishRequest.submitted_by == submitted_by)
        result = await self.session.execute(
            select(TemplatePublishRequest)
            .where(*conditions)
            .order_by(TemplatePublishRequest.submitted_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def get_pending(
        self, tenant_id: str, scenario_id: str,
    ) -> TemplatePublishRequest | None:
        from sqlalchemy import select

        result = await self.session.execute(
            select(TemplatePublishRequest).where(
                TemplatePublishRequest.tenant_id == tenant_id,
                TemplatePublishRequest.scenario_id == scenario_id,
                TemplatePublishRequest.status == "pending",
            )
        )
        return result.scalars().first()

    async def get_for_tenant(
        self, request_id: str, tenant_id: str,
    ) -> TemplatePublishRequest | None:
        from sqlalchemy import select

        result = await self.session.execute(
            select(TemplatePublishRequest).where(
                TemplatePublishRequest.id == request_id,
                TemplatePublishRequest.tenant_id == tenant_id,
            )
        )
        return result.scalars().first()
