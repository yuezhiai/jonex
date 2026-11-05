"""
Repository for ontology template bindings and compiled schemas.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import ResourceConflictError
from jonex_core.common.i18n import translate
from jonex_core.common.schema_generation import bump_schema_generation
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
        data_source_id: Optional[str] = None,
    ) -> Optional[OntologyCompiledSchema]:
        """取一份编译 schema。

        `data_source_id=None` → **知识库级**（不绑定具体数据源），与改造前行为一致；
        给了 id → 该数据源自己的那份。
        """
        stmt = select(OntologyCompiledSchema).where(
            OntologyCompiledSchema.tenant_id == require_tenant(tenant_id),
            OntologyCompiledSchema.knowledge_base_id == knowledge_base_id,
            OntologyCompiledSchema.status == "active",
        )
        # NULL 的比较必须走 IS NULL —— `== None` 在 SQL 里恒为 NULL，取不到行
        stmt = stmt.where(
            OntologyCompiledSchema.data_source_id.is_(None)
            if data_source_id is None
            else OntologyCompiledSchema.data_source_id == data_source_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_compiled_schemas(
        self,
        tenant_id: str,
        knowledge_base_id: str,
    ) -> list[OntologyCompiledSchema]:
        """取该知识库下**全部** schema（知识库级 + 各数据源级）。

        检索侧要用它把多份 schema 合并成一份视图——不改这条路径的话，
        加了「按数据源」之后 inverse 映射只会看到知识库级那一份。
        """
        stmt = select(OntologyCompiledSchema).where(
            OntologyCompiledSchema.tenant_id == require_tenant(tenant_id),
            OntologyCompiledSchema.knowledge_base_id == knowledge_base_id,
            OntologyCompiledSchema.status == "active",
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

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
        # NULL = 知识库级（与改造前一致）；给了 id → 该数据源自己的那一份
        data_source_id: Optional[str] = None,
    ) -> OntologyCompiledSchema:
        # 按「份」取旧的：知识库级与数据源级是各自独立的乐观锁
        old = await self.get_compiled_schema(tenant_id, knowledge_base_id, data_source_id)
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
                # Core UPDATE 不会刷新 identity map 中已加载的 old，过期后重取最新对象。
                # ⚠️ `data_source_id` 必须带上：漏了就会取回**知识库级**那一行，
                # 而调用方（`save_compiled_schema`）会拿它去 `_set_cache(..., data_source_id)`
                # —— 于是知识库级那份被写进数据源级的缓存槽，之后所有数据源级读都返回错的那份
                # （画布打开像是「改了没生效」，实际是把 KB 级的显示出来了）。
                self.session.expire(old)
                row = await self.get_compiled_schema(
                    tenant_id, knowledge_base_id, data_source_id)
                # P1 版本化读取：写成功后推进进程内代号（② 的行缓存据此立刻换键）。
                # 冲突分支在上面已经 raise，不走这里。
                bump_schema_generation()
                return row

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
                data_source_id=data_source_id,
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
        # P1 版本化读取：flush 成功（INSERT 或原地 UPDATE 分支）后才推进代号。
        # ⚠️ 仓储不 commit：调用方事务最终失败时这次 bump 只是让 ② 多读一次库，
        # 方向安全（宁可多读，不可读到写前的行）。
        bump_schema_generation()
        return compiled

    async def set_evidence(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        data_source_id: Optional[str],
        evidence: dict,
    ) -> bool:
        """写「生成期探测」的派生证据。

        **不走 `upsert_compiled_schema`**：证据不是语义的一部分 —— 它不该推进 `schema_version`
        （那会让编辑器的乐观锁误以为有人在改 schema），也不该被画布保存覆写
        （`upsert` 的 UPDATE 显式列举列名、天然不碰这一列）。

        返回 `False` 表示那一行不存在（还没生成过 schema）—— 调用方照常继续，不是错误。
        """
        # ⚠️ 这里**不能**照抄别的仓储那条 `is_deleted == 0`：`ontology_compiled_schemas`
        # 根本没有这一列（模型不带 `SoftDeleteMixin`，004 的建表语句里也没有），
        # 写上它只会在运行时炸 `AttributeError` —— 本表的失效语义走 `status`/`sync_status`。
        stmt = (
            update(OntologyCompiledSchema)
            .where(
                OntologyCompiledSchema.tenant_id == tenant_id,
                OntologyCompiledSchema.knowledge_base_id == knowledge_base_id,
                OntologyCompiledSchema.data_source_id == data_source_id,
                # ⚠️ **要带 `status == "active"`**（Task 3 复审、控制器定夺）：姊妹读路径
                # `get_compiled_schema` 正是这么筛的 —— 不带它的话，万一存在非 active 行，
                # 这里会写成功、返回 `True`，而**读者永远看不见那份证据**（返回值会被
                # Task 4 当成「落库了」的依据）。
                OntologyCompiledSchema.status == "active",
            )
            .values(evidence=evidence, updated_at=datetime.utcnow())
        )
        result = await self.session.execute(stmt)
        if result.rowcount > 0:
            # P1 版本化读取：`evidence` 是 ② 缓存行的一部分（`samples_by_column` 提示
            # 从它挑），列变了就要换键 —— 否则最长 TTL 内继续拿旧样本。命中 0 行
            # （行不存在 / 非 active）不算写入，不推进。
            bump_schema_generation()
        return result.rowcount > 0

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
