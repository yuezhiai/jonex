"""
提示词模板 Repository。

查询策略：
- scope='system' 的模板（tenant_id=NULL）：走共享查询（不过滤 tenant），对所有租户只读可见
- scope='domain' 的模板（tenant_id!=NULL）：走标准租户隔离查询
"""
from typing import Sequence

from sqlalchemy import func, or_, select

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from capabilities.business_domain.models.prompt_template import PromptTemplate


class PromptTemplateRepository(BaseRepository[PromptTemplate]):
    model = PromptTemplate

    # ── 共享查询（system 模板） ──

    async def list_system_templates(
        self,
        offset: int = 0,
        limit: int = 20,
        extra_conditions: Sequence | None = None,
    ) -> list[PromptTemplate]:
        """查询系统全局模板（tenant_id=NULL, scope='system'），不按 tenant 过滤"""
        conditions = [
            PromptTemplate.tenant_id.is_(None),
            PromptTemplate.scope == "system",
            PromptTemplate.is_deleted == 0,
        ]
        if extra_conditions:
            conditions.extend(extra_conditions)
        stmt = (
            select(PromptTemplate)
            .where(*conditions)
            .order_by(PromptTemplate.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_system_templates(
        self,
        extra_conditions: Sequence | None = None,
    ) -> int:
        from sqlalchemy import func
        conditions = [
            PromptTemplate.tenant_id.is_(None),
            PromptTemplate.scope == "system",
            PromptTemplate.is_deleted == 0,
        ]
        if extra_conditions:
            conditions.extend(extra_conditions)
        result = await self.session.execute(
            select(func.count()).select_from(PromptTemplate).where(*conditions)
        )
        return result.scalar_one()

    async def get_system_template(self, template_id: str) -> PromptTemplate | None:
        """按 ID 获取系统模板"""
        result = await self.session.execute(
            select(PromptTemplate).where(
                PromptTemplate.id == template_id,
                PromptTemplate.tenant_id.is_(None),
                PromptTemplate.scope == "system",
                PromptTemplate.is_deleted == 0,
            )
        )
        return result.scalar_one_or_none()

    # ── 领域模板查询 ──

    async def list_domain_templates(
        self,
        tenant_id: str,
        offset: int = 0,
        limit: int = 20,
        space_id: str | None = None,
        extra_conditions: Sequence | None = None,
    ) -> list[PromptTemplate]:
        """查询领域空间模板（租户隔离 + 可选的 space_id 过滤）"""
        tenant_id = require_tenant(tenant_id)
        conditions = [
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.scope == "domain",
            PromptTemplate.is_deleted == 0,
        ]
        if space_id is not None:
            conditions.append(PromptTemplate.space_id == space_id)
        if extra_conditions:
            conditions.extend(extra_conditions)
        stmt = (
            select(PromptTemplate)
            .where(*conditions)
            .order_by(PromptTemplate.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_domain_templates(
        self,
        tenant_id: str,
        space_id: str | None = None,
        extra_conditions: Sequence | None = None,
    ) -> int:
        from sqlalchemy import func
        tenant_id = require_tenant(tenant_id)
        conditions = [
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.scope == "domain",
            PromptTemplate.is_deleted == 0,
        ]
        if space_id is not None:
            conditions.append(PromptTemplate.space_id == space_id)
        if extra_conditions:
            conditions.extend(extra_conditions)
        result = await self.session.execute(
            select(func.count()).select_from(PromptTemplate).where(*conditions)
        )
        return result.scalar_one()

    async def get_domain_template(
        self, template_id: str, tenant_id: str, space_id: str | None = None
    ) -> PromptTemplate | None:
        """按 ID 获取领域模板（校验租户归属 + 可选的 space_id）"""
        tenant_id = require_tenant(tenant_id)
        conditions = [
            PromptTemplate.id == template_id,
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.scope == "domain",
            PromptTemplate.is_deleted == 0,
        ]
        if space_id is not None:
            conditions.append(PromptTemplate.space_id == space_id)
        result = await self.session.execute(
            select(PromptTemplate).where(*conditions)
        )
        return result.scalar_one_or_none()

    async def get_required_domain_template(
        self, template_id: str, tenant_id: str, space_id: str | None = None
    ) -> PromptTemplate:
        """按 ID 获取领域模板，不存在则抛异常"""
        from jonex_core.common.exceptions import ResourceNotFoundError
        from jonex_core.common.i18n import translate

        obj = await self.get_domain_template(template_id, tenant_id, space_id=space_id)
        if obj is None:
            raise ResourceNotFoundError(
                message=translate("err.prompt.not_found", params={"template_id": template_id}, fallback=f"提示词模板不存在: {template_id}"),  # 原消息
                details={"id": template_id, "tenant_id": tenant_id},
            )
        return obj

      # ── 名称查重 ──

    async def name_exists(
        self,
        tenant_id: str,
        name: str,
        space_id: str | None = None,
        exclude_id: str | None = None,
    ) -> bool:
        """判断同领域空间内是否已存在同名 domain 模板。

        - 区分大小写（== 精确匹配，与 DB 部分唯一索引语义一致）
        - 排除软删（is_deleted=0）
        - space_id=None 表示精确匹配 space_id IS NULL 的模板（NULL 也是明确空间，
        不按「不过滤」处理 —— 与 list_domain_templates 的 space_id=None 语义不同）
        - exclude_id 用于编辑场景排除自身
        """
        tenant_id = require_tenant(tenant_id)
        conditions = [
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.scope == "domain",
            PromptTemplate.is_deleted == 0,
            PromptTemplate.name == name,
        ]
        if space_id is not None:
            conditions.append(PromptTemplate.space_id == space_id)
        else:
            conditions.append(PromptTemplate.space_id.is_(None))
        if exclude_id is not None:
            conditions.append(PromptTemplate.id != exclude_id)
        result = await self.session.execute(
            select(func.count()).select_from(PromptTemplate).where(*conditions)
        )
        return result.scalar_one() > 0

    # ── 通用查询（任意模板，不校验租户归属） ──

    async def get_any(self, template_id: str) -> PromptTemplate | None:
        """获取任意模板（不校验租户），用于内部判断 scope"""
        result = await self.session.execute(
            select(PromptTemplate).where(
                PromptTemplate.id == template_id,
                PromptTemplate.is_deleted == 0,
            )
        )
        return result.scalar_one_or_none()

    async def search_domain_templates(
        self,
        tenant_id: str,
        keyword: str,
        offset: int = 0,
        limit: int = 20,
        space_id: str | None = None,
    ) -> list[PromptTemplate]:
        """关键词搜索领域模板（名称 + 描述 + 提示词内容，可选的 space_id 过滤）"""
        tenant_id = require_tenant(tenant_id)
        pattern = f"%{keyword}%"
        conditions = [
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.scope == "domain",
            PromptTemplate.is_deleted == 0,
        ]
        if space_id is not None:
            conditions.append(PromptTemplate.space_id == space_id)
        conditions.append(
            or_(
                PromptTemplate.name.ilike(pattern),
                PromptTemplate.description.ilike(pattern),
                # versions_json[0]->>'content' 的搜索在 service 层处理
            ),
        )
        stmt = (
            select(PromptTemplate)
            .where(*conditions)
            .order_by(PromptTemplate.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())
