"""
业务领域 — 业务模板服务
"""
import uuid
from typing import Any

from sqlalchemy import or_, select

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.ontology_yaml import (  # [jonex]
    OntologyYamlDocument,
    YamlAttribute,
    YamlConstraint,
    YamlEntity,
    YamlRelation,
    dump_yaml,
    parse_yaml,
    template_relation_type_to_card,
    to_attr_code,
    to_entity_code,
    to_relation_code,
    to_yaml_attr_type,
)

from capabilities.business_domain.models.template import (
    TemplateAttribute,
    TemplateConstraint,
    TemplateDomain,
    TemplateObject,
    TemplateRelation,
    TemplateScenario,
)
from capabilities.business_domain.repository import (
    TemplateAttributeRepository,
    TemplateConstraintRepository,
    TemplateDomainRepository,
    TemplateObjectRepository,
    TemplateRelationRepository,
    TemplateScenarioRepository,
)
from capabilities.business_domain.services import _check_tenant


class TemplateService:
    """业务领域模板：模板领域 -> 场景 -> 对象/属性/关系。"""

    # Template Domains
    async def list_domains(self, tenant_id: str, offset: int = 0, limit: int = 20, status: str | None = None) -> dict:
        tenant_id = _check_tenant(tenant_id)
        extra_conditions = [TemplateDomain.status == status] if status else []
        async with get_db_session() as session:
            repo = TemplateDomainRepository(session)
            items = await repo.list_all(tenant_id, offset, limit, extra_conditions=extra_conditions)
            total = await repo.count(tenant_id, extra_conditions=extra_conditions)
            domain_ids = [o.id for o in items]
            scenario_counts: dict[str, int] = {}
            if domain_ids:
                from sqlalchemy import func
                count_rows = await session.execute(
                    select(TemplateScenario.domain_id, func.count(TemplateScenario.id))
                    .where(
                        TemplateScenario.tenant_id == tenant_id,
                        TemplateScenario.is_deleted == 0,
                        TemplateScenario.domain_id.in_(domain_ids),
                    )
                    .group_by(TemplateScenario.domain_id)
                )
                scenario_counts = {row[0]: row[1] for row in count_rows}
            result_items = []
            for o in items:
                d = o.to_dict()
                d["scenario_count"] = scenario_counts.get(o.id, 0)
                result_items.append(d)
            return {"items": result_items, "total": total, "offset": offset, "limit": limit}

    async def get_domain(self, domain_id: str, tenant_id: str) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            obj = await TemplateDomainRepository(session).get_required(domain_id, tenant_id)
            return obj.to_dict()

    async def create_domain(self, tenant_id: str, data: dict) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            repo = TemplateDomainRepository(session)
            obj = await repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                name=data["name"],
                name_en=data.get("name_en"),
                description=data.get("description"),
                status=data.get("status", "inactive"),
            )
            await session.commit()
            return obj.to_dict()

    async def update_domain(self, domain_id: str, tenant_id: str, data: dict) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            repo = TemplateDomainRepository(session)
            obj = await repo.update(domain_id, tenant_id, **{
                k: v for k, v in data.items()
                if k in ("name", "name_en", "description", "status") and v is not None
            })
            if obj is None:
                raise ResourceNotFoundError(message=translate("err.template.domain_not_found", params={"domain_id": domain_id}, fallback=f"模板领域不存在: {domain_id}"))  # 原消息
            await session.commit()
            return obj.to_dict()

    async def delete_domain(self, domain_id: str, tenant_id: str) -> bool:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            domain_repo = TemplateDomainRepository(session)
            scenario_repo = TemplateScenarioRepository(session)
            object_repo = TemplateObjectRepository(session)
            attribute_repo = TemplateAttributeRepository(session)
            relation_repo = TemplateRelationRepository(session)
            constraint_repo = TemplateConstraintRepository(session)

            # 校验领域存在（保留原错误码与文案）
            domain = await domain_repo.get_by_id(domain_id, tenant_id)
            if domain is None:
                raise ResourceNotFoundError(message=translate("err.template.domain_not_found", params={"domain_id": domain_id}, fallback=f"模板领域不存在: {domain_id}"))  # 原消息

            # 级联软删：领域 -> 场景 -> 对象 -> 属性 / 关系 / 约束
            scenarios = await scenario_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[TemplateScenario.domain_id == domain_id],
            )
            scenario_ids = [s.id for s in scenarios]
            if scenario_ids:
                objects = await object_repo.list_all(
                    tenant_id, 0, 10000,
                    extra_conditions=[TemplateObject.scenario_id.in_(scenario_ids)],
                )
                object_ids = [o.id for o in objects]
                relations = await relation_repo.list_all(
                    tenant_id, 0, 10000,
                    extra_conditions=[TemplateRelation.scenario_id.in_(scenario_ids)],
                )
                constraints = await constraint_repo.list_all(
                    tenant_id, 0, 10000,
                    extra_conditions=[TemplateConstraint.scenario_id.in_(scenario_ids)],
                )
                attributes: list = []
                if object_ids:
                    attributes = await attribute_repo.list_all(
                        tenant_id, 0, 10000,
                        extra_conditions=[TemplateAttribute.template_object_id.in_(object_ids)],
                    )

                for attr in attributes:
                    await attribute_repo.delete_soft(attr, tenant_id)
                for relation in relations:
                    await relation_repo.delete_soft(relation, tenant_id)
                for constraint in constraints:
                    await constraint_repo.delete_soft(constraint, tenant_id)
                for obj in objects:
                    await object_repo.delete_soft(obj, tenant_id)
                for scenario in scenarios:
                    await scenario_repo.delete_soft(scenario, tenant_id)

            await domain_repo.delete_soft(domain, tenant_id)
            await session.commit()
            return True

    # Template Scenarios
    async def list_scenarios(
        self,
        tenant_id: str,
        domain_id: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            if domain_id:
                await TemplateDomainRepository(session).get_required(domain_id, tenant_id)
            conditions = [TemplateScenario.domain_id == domain_id] if domain_id else []
            repo = TemplateScenarioRepository(session)
            items = await repo.list_all(tenant_id, offset, limit, extra_conditions=conditions)
            total = await repo.count(tenant_id, extra_conditions=conditions)
            return {"items": [o.to_dict() for o in items], "total": total, "offset": offset, "limit": limit}

    async def create_scenario(self, tenant_id: str, data: dict) -> dict:
        tenant_id = _check_tenant(tenant_id)
        domain_id = data.get("domain_id")
        if not domain_id:
            raise InvalidParameterError(message=translate("err.template.scenario_needs_domain", fallback="模板场景必须关联模板领域"))  # 原消息

        async with get_db_session() as session:
            await TemplateDomainRepository(session).get_required(domain_id, tenant_id)
            obj = await TemplateScenarioRepository(session).create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                domain_id=domain_id,
                name=data["name"],
                description=data.get("description"),
                config_json=data.get("config_json", {}),
            )
            await session.commit()
            return obj.to_dict()

    async def get_scenario(self, scenario_id: str, tenant_id: str) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            obj = await TemplateScenarioRepository(session).get_required(scenario_id, tenant_id)
            return obj.to_dict()

    async def update_scenario(self, scenario_id: str, tenant_id: str, data: dict) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            repo = TemplateScenarioRepository(session)
            obj = await repo.get_required(scenario_id, tenant_id)
            old_domain_id = obj.domain_id
            if "domain_id" in data and data["domain_id"] and data["domain_id"] != obj.domain_id:
                await TemplateDomainRepository(session).get_required(data["domain_id"], tenant_id)
            update_values = {
                k: v for k, v in data.items()
                if k in ("name", "description", "domain_id", "config_json") and v is not None
            }
            obj = await repo.update(obj, tenant_id, **update_values)
            if obj.domain_id != old_domain_id:
                object_repo = TemplateObjectRepository(session)
                relation_repo = TemplateRelationRepository(session)
                constraint_repo = TemplateConstraintRepository(session)
                objects = await object_repo.list_all(
                    tenant_id,
                    0,
                    10000,
                    extra_conditions=[TemplateObject.scenario_id == obj.id],
                )
                relations = await relation_repo.list_all(
                    tenant_id,
                    0,
                    10000,
                    extra_conditions=[TemplateRelation.scenario_id == obj.id],
                )
                constraints = await constraint_repo.list_all(
                    tenant_id,
                    0,
                    10000,
                    extra_conditions=[TemplateConstraint.scenario_id == obj.id],
                )
                for template_object in objects:
                    await object_repo.update(template_object, tenant_id, domain_id=obj.domain_id)
                for relation in relations:
                    await relation_repo.update(relation, tenant_id, domain_id=obj.domain_id)
                for constraint in constraints:
                    await constraint_repo.update(constraint, tenant_id, domain_id=obj.domain_id)
            await session.commit()
            return obj.to_dict()

    async def delete_scenario(self, scenario_id: str, tenant_id: str) -> bool:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            scenario_repo = TemplateScenarioRepository(session)
            scenario = await scenario_repo.get_required(scenario_id, tenant_id)
            object_repo = TemplateObjectRepository(session)
            relation_repo = TemplateRelationRepository(session)
            attribute_repo = TemplateAttributeRepository(session)
            constraint_repo = TemplateConstraintRepository(session)

            objects = await object_repo.list_all(
                tenant_id,
                0,
                10000,
                extra_conditions=[TemplateObject.scenario_id == scenario_id],
            )
            relations = await relation_repo.list_all(
                tenant_id,
                0,
                10000,
                extra_conditions=[TemplateRelation.scenario_id == scenario_id],
            )
            constraints = await constraint_repo.list_all(
                tenant_id,
                0,
                10000,
                extra_conditions=[TemplateConstraint.scenario_id == scenario_id],
            )
            for obj in objects:
                attrs = await attribute_repo.list_all(
                    tenant_id,
                    0,
                    10000,
                    extra_conditions=[TemplateAttribute.template_object_id == obj.id],
                )
                for attr in attrs:
                    await attribute_repo.delete_soft(attr, tenant_id)
                await object_repo.delete_soft(obj, tenant_id)
            for relation in relations:
                await relation_repo.delete_soft(relation, tenant_id)
            for constraint in constraints:
                await constraint_repo.delete_soft(constraint, tenant_id)
            await scenario_repo.delete_soft(scenario, tenant_id)
            await session.commit()
            return True

    # Template Objects
    async def list_objects(self, tenant_id: str, scenario_id: str, offset: int = 0, limit: int = 20) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            scenario = await TemplateScenarioRepository(session).get_required(scenario_id, tenant_id)
            repo = TemplateObjectRepository(session)
            items = await repo.list_all(
                tenant_id,
                offset,
                limit,
                extra_conditions=[TemplateObject.scenario_id == scenario.id],
            )
            total = await repo.count(tenant_id, extra_conditions=[TemplateObject.scenario_id == scenario.id])
            attribute_map = await self._load_attributes(session, tenant_id, [o.id for o in items])
            return {
                "items": [self._object_to_dict(o, attribute_map.get(o.id, [])) for o in items],
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    async def create_object(self, tenant_id: str, scenario_id: str, data: dict) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            scenario = await TemplateScenarioRepository(session).get_required(scenario_id, tenant_id)
            obj = await TemplateObjectRepository(session).create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                domain_id=scenario.domain_id,
                scenario_id=scenario.id,
                name=data["name"],
                description=data.get("description"),
                status=data.get("status", "draft"),
                ontology_code=data.get("ontology_code"),
                aliases=data.get("aliases", []),
            )
            attrs = await self._replace_attributes(session, tenant_id, obj.id, data.get("attributes", []))
            await session.commit()
            return self._object_to_dict(obj, attrs)

    async def update_object(self, object_id: str, tenant_id: str, data: dict) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            object_repo = TemplateObjectRepository(session)
            obj = await object_repo.get_required(object_id, tenant_id)
            await TemplateScenarioRepository(session).get_required(obj.scenario_id, tenant_id)
            old_name = obj.name
            obj = await object_repo.update(obj, tenant_id, **{
                k: v for k, v in data.items()
                if k in ("name", "description", "status", "ontology_code", "aliases") and v is not None
            })
            # 对象改名 → 同步对象级约束 target_label
            new_name = data.get("name")
            if new_name and new_name != old_name:
                await self._sync_constraint_labels_by_target(
                    session, tenant_id, "object", object_id, new_name,
                )
            attrs = await self._load_attributes(session, tenant_id, [obj.id])
            if "attributes" in data:
                attrs[obj.id] = await self._replace_attributes(session, tenant_id, obj.id, data.get("attributes") or [])
            await session.commit()
            return self._object_to_dict(obj, attrs.get(obj.id, []))

    async def delete_object(self, object_id: str, tenant_id: str) -> bool:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            object_repo = TemplateObjectRepository(session)
            attribute_repo = TemplateAttributeRepository(session)
            relation_repo = TemplateRelationRepository(session)
            constraint_repo = TemplateConstraintRepository(session)
            obj = await object_repo.get_required(object_id, tenant_id)

            attrs = await attribute_repo.list_all(
                tenant_id,
                0,
                10000,
                extra_conditions=[TemplateAttribute.template_object_id == obj.id],
            )
            attr_ids = [a.id for a in attrs]
            relations = await relation_repo.list_all(
                tenant_id,
                0,
                10000,
                extra_conditions=[
                    TemplateRelation.scenario_id == obj.scenario_id,
                    or_(
                        TemplateRelation.source_object_id == obj.id,
                        TemplateRelation.target_object_id == obj.id,
                    ),
                ],
            )
            # 级联软删对象级约束
            object_constraints = await constraint_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[
                    TemplateConstraint.scenario_id == obj.scenario_id,
                    TemplateConstraint.target_type == "object",
                    TemplateConstraint.target_id == obj.id,
                ],
            )
            # 级联软删属性级约束
            attr_constraints: list = []
            if attr_ids:
                attr_constraints = await constraint_repo.list_all(
                    tenant_id, 0, 10000,
                    extra_conditions=[
                        TemplateConstraint.scenario_id == obj.scenario_id,
                        TemplateConstraint.target_type == "attribute",
                        TemplateConstraint.target_id.in_(attr_ids),
                    ],
                )
            for attr in attrs:
                await attribute_repo.delete_soft(attr, tenant_id)
            for relation in relations:
                await relation_repo.delete_soft(relation, tenant_id)
            for constraint in object_constraints + attr_constraints:
                await constraint_repo.delete_soft(constraint, tenant_id)
            await object_repo.delete_soft(obj, tenant_id)
            await session.commit()
            return True

    # Template Relations
    async def list_relations(self, tenant_id: str, scenario_id: str, offset: int = 0, limit: int = 20) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            scenario = await TemplateScenarioRepository(session).get_required(scenario_id, tenant_id)
            repo = TemplateRelationRepository(session)
            items = await repo.list_all(
                tenant_id,
                offset,
                limit,
                extra_conditions=[TemplateRelation.scenario_id == scenario.id],
            )
            total = await repo.count(tenant_id, extra_conditions=[TemplateRelation.scenario_id == scenario.id])
            object_names = await self._load_object_names(session, tenant_id, scenario.id)
            return {
                "items": [self._relation_to_dict(o, object_names) for o in items],
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    async def create_relation(self, tenant_id: str, scenario_id: str, data: dict) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            scenario = await TemplateScenarioRepository(session).get_required(scenario_id, tenant_id)
            await self._validate_relation_objects(
                session,
                tenant_id,
                scenario.id,
                data.get("source_object_id"),
                data.get("target_object_id"),
            )
            obj = await TemplateRelationRepository(session).create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                domain_id=scenario.domain_id,
                scenario_id=scenario.id,
                name=data["name"],
                description=data.get("description"),
                source_object_id=data["source_object_id"],
                target_object_id=data["target_object_id"],
                relation_type=data.get("relation_type", "custom"),
                status=data.get("status", "draft"),
                ontology_code=data.get("ontology_code"),
                aliases=data.get("aliases", []),
            )
            object_names = await self._load_object_names(session, tenant_id, scenario.id)
            await session.commit()
            return self._relation_to_dict(obj, object_names)

    async def update_relation(self, relation_id: str, tenant_id: str, data: dict) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            repo = TemplateRelationRepository(session)
            obj = await repo.get_required(relation_id, tenant_id)
            scenario = await TemplateScenarioRepository(session).get_required(obj.scenario_id, tenant_id)
            source_id = data.get("source_object_id", obj.source_object_id)
            target_id = data.get("target_object_id", obj.target_object_id)
            await self._validate_relation_objects(session, tenant_id, scenario.id, source_id, target_id)
            old_name = obj.name
            obj = await repo.update(obj, tenant_id, **{
                k: v for k, v in data.items()
                if k in (
                    "name",
                    "description",
                    "source_object_id",
                    "target_object_id",
                    "relation_type",
                    "status",
                    "ontology_code",
                    "aliases",
                ) and v is not None
            })
            # 关系改名 → 同步关系级约束 target_label
            new_name = data.get("name")
            if new_name and new_name != old_name:
                await self._sync_constraint_labels_by_target(
                    session, tenant_id, "relation", relation_id, new_name,
                )
            object_names = await self._load_object_names(session, tenant_id, scenario.id)
            await session.commit()
            return self._relation_to_dict(obj, object_names)

    async def delete_relation(self, relation_id: str, tenant_id: str) -> bool:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            repo = TemplateRelationRepository(session)
            obj = await repo.get_required(relation_id, tenant_id)
            constraint_repo = TemplateConstraintRepository(session)
            constraints = await constraint_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[
                    TemplateConstraint.scenario_id == obj.scenario_id,
                    TemplateConstraint.target_type == "relation",
                    TemplateConstraint.target_id == relation_id,
                ],
            )
            for constraint in constraints:
                await constraint_repo.delete_soft(constraint, tenant_id)
            await repo.delete_soft(obj, tenant_id)
            await session.commit()
            return True

    async def _load_attributes(self, session, tenant_id: str, object_ids: list[str]) -> dict[str, list[TemplateAttribute]]:
        if not object_ids:
            return {}
        result = await session.execute(
            select(TemplateAttribute)
            .where(
                TemplateAttribute.tenant_id == tenant_id,
                TemplateAttribute.is_deleted == 0,
                TemplateAttribute.template_object_id.in_(object_ids),
            )
            .order_by(TemplateAttribute.sort_order.asc(), TemplateAttribute.created_at.asc())
        )
        attrs_by_object: dict[str, list[TemplateAttribute]] = {object_id: [] for object_id in object_ids}
        for attr in result.scalars():
            attrs_by_object.setdefault(attr.template_object_id, []).append(attr)
        return attrs_by_object

    async def _replace_attributes(
        self,
        session,
        tenant_id: str,
        object_id: str,
        attributes: list[dict[str, Any]],
    ) -> list[TemplateAttribute]:
        """按 id upsert 属性：保留传入带 id 的属性（原地更新），新增无 id 的，软删不在列表中的。
        保持属性 id 稳定，使属性级约束 target_id 可靠。"""
        repo = TemplateAttributeRepository(session)
        constraint_repo = TemplateConstraintRepository(session)
        old_attrs = await repo.list_all(
            tenant_id,
            0,
            10000,
            extra_conditions=[TemplateAttribute.template_object_id == object_id],
        )
        old_by_id = {a.id: a for a in old_attrs}

        incoming_ids = {
            attr_data.get("id") for attr_data in attributes
            if attr_data.get("id")
        }
        # 软删不在列表中的旧属性 + 其约束
        removed_ids = set(old_by_id.keys()) - incoming_ids
        if removed_ids:
            removed_attrs = [old_by_id[aid] for aid in removed_ids]
            removed_attr_constraints = await constraint_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[
                    TemplateConstraint.target_type == "attribute",
                    TemplateConstraint.target_id.in_(list(removed_ids)),
                ],
            )
            for constraint in removed_attr_constraints:
                await constraint_repo.delete_soft(constraint, tenant_id)
            for attr in removed_attrs:
                await repo.delete_soft(attr, tenant_id)

        result: list[TemplateAttribute] = []
        for index, attr_data in enumerate(attributes):
            attr_name = attr_data.get("attr_name") or attr_data.get("name")
            if not attr_name:
                raise InvalidParameterError(message=translate("err.template.attr_name_required", fallback="对象属性名称不能为空"))  # 原消息
            attr_id = attr_data.get("id")
            if attr_id and attr_id in old_by_id:
                # 原地更新保留属性（改名时同步 target_label）
                old_attr = old_by_id[attr_id]
                old_name = old_attr.attr_name
                updated = await repo.update(old_attr, tenant_id, **{
                    k: v for k, v in {
                        "attr_name": attr_name,
                        "description": attr_data.get("description") or attr_data.get("desc"),
                        "attr_type": attr_data.get("attr_type") or attr_data.get("type") or "string",
                        "is_primary_key": 1 if attr_data.get("is_primary_key") or attr_data.get("isPrimary") else 0,
                        "constraints_json": attr_data.get("constraints_json", {}),
                        "sort_order": attr_data.get("sort_order", index),
                        "ontology_code": attr_data.get("ontology_code"),
                        "is_required": 1 if attr_data.get("is_required") else 0,
                    }.items() if v is not None
                })
                # 改名时同步属性级约束 target_label
                if attr_name != old_name:
                    await self._sync_constraint_labels_by_target(
                        session, tenant_id, "attribute", attr_id, attr_name,
                    )
                result.append(updated)
            else:
                # 新建属性
                attr = await repo.create(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    template_object_id=object_id,
                    attr_name=attr_name,
                    description=attr_data.get("description") or attr_data.get("desc"),
                    attr_type=attr_data.get("attr_type") or attr_data.get("type") or "string",
                    is_primary_key=1 if attr_data.get("is_primary_key") or attr_data.get("isPrimary") else 0,
                    constraints_json=attr_data.get("constraints_json", {}),
                    sort_order=attr_data.get("sort_order", index),
                    ontology_code=attr_data.get("ontology_code"),
                    is_required=1 if attr_data.get("is_required") else 0,
                )
                result.append(attr)
        return result

    async def _load_object_names(self, session, tenant_id: str, scenario_id: str) -> dict[str, str]:
        result = await session.execute(
            select(TemplateObject)
            .where(
                TemplateObject.tenant_id == tenant_id,
                TemplateObject.is_deleted == 0,
                TemplateObject.scenario_id == scenario_id,
            )
        )
        return {obj.id: obj.name for obj in result.scalars()}

    async def _validate_relation_objects(
        self,
        session,
        tenant_id: str,
        scenario_id: str,
        source_object_id: str | None,
        target_object_id: str | None,
    ) -> None:
        if not source_object_id or not target_object_id:
            raise InvalidParameterError(message=translate("err.template.relation_needs_both", fallback="关系必须指定源对象和目标对象"))  # 原消息
        objects = await TemplateObjectRepository(session).list_all(
            tenant_id,
            0,
            2,
            extra_conditions=[
                TemplateObject.scenario_id == scenario_id,
                TemplateObject.id.in_([source_object_id, target_object_id]),
            ],
        )
        found_ids = {obj.id for obj in objects}
        if source_object_id not in found_ids or target_object_id not in found_ids:
            raise InvalidParameterError(message=translate("err.template.relation_same_scenario", fallback="关系对象必须属于当前模板场景"))  # 原消息

    @staticmethod
    def _object_to_dict(obj: TemplateObject, attributes: list[TemplateAttribute]) -> dict:
        data = obj.to_dict()
        data["attributes"] = [attr.to_dict() for attr in attributes]
        return data

    @staticmethod
    def _relation_to_dict(obj: TemplateRelation, object_names: dict[str, str]) -> dict:
        data = obj.to_dict()
        data["source_object_name"] = object_names.get(obj.source_object_id)
        data["target_object_name"] = object_names.get(obj.target_object_id)
        return data

    # ── Template Constraints ──────────────────────────────────

    async def list_constraints(
        self, tenant_id: str, scenario_id: str, offset: int = 0, limit: int = 20,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            await TemplateScenarioRepository(session).get_required(scenario_id, tenant_id)
            repo = TemplateConstraintRepository(session)
            items = await repo.list_all(
                tenant_id, offset, limit,
                extra_conditions=[TemplateConstraint.scenario_id == scenario_id],
            )
            total = await repo.count(
                tenant_id,
                extra_conditions=[TemplateConstraint.scenario_id == scenario_id],
            )
            return {
                "items": [o.to_dict() for o in items],
                "total": total, "offset": offset, "limit": limit,
            }

    async def create_constraint(
        self, tenant_id: str, scenario_id: str, data: dict,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            scenario = await TemplateScenarioRepository(session).get_required(scenario_id, tenant_id)
            target_type = data["target_type"]
            target_id = data["target_id"]
            constraint_type = data["constraint_type"]
            # 表达式必填规则
            if constraint_type in ("conditional", "range") and not data.get("expression"):
                raise InvalidParameterError(
                    message=translate("err.template.constraint_needs_expression", params={"constraint_type": constraint_type}, fallback=f"约束类型 {constraint_type} 必须填写表达式"),  # 原消息
                )
            target_label = await self._validate_constraint_target(
                session, tenant_id, scenario.id, target_type, target_id,
            )
            obj = await TemplateConstraintRepository(session).create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                domain_id=scenario.domain_id,
                scenario_id=scenario.id,
                name=data["name"],
                target_type=target_type,
                target_id=target_id,
                target_label=target_label,
                constraint_type=constraint_type,
                expression=data.get("expression"),
                suggestion=data.get("suggestion"),
            )
            await session.commit()
            return obj.to_dict()

    async def update_constraint(
        self, constraint_id: str, tenant_id: str, data: dict,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            repo = TemplateConstraintRepository(session)
            obj = await repo.get_required(constraint_id, tenant_id)
            await TemplateScenarioRepository(session).get_required(obj.scenario_id, tenant_id)

            # PATCH 合并：现值 + patch → 最终值
            merged_target_type = data.get("target_type", obj.target_type)
            merged_target_id = data.get("target_id", obj.target_id)
            merged_constraint_type = data.get("constraint_type", obj.constraint_type)
            merged_expression = data.get("expression", obj.expression) if "expression" in data else obj.expression

            # 表达式必填规则（基于合并后的 constraint_type）
            if merged_constraint_type in ("conditional", "range") and not merged_expression:
                raise InvalidParameterError(
                    message=translate("err.template.constraint_needs_expression", params={"constraint_type": merged_constraint_type}, fallback=f"约束类型 {merged_constraint_type} 必须填写表达式"),  # 原消息
                )

            # 检查目标变化
            target_changed = (
                merged_target_type != obj.target_type
                or merged_target_id != obj.target_id
            )
            target_label = obj.target_label
            if target_changed:
                target_label = await self._validate_constraint_target(
                    session, tenant_id, obj.scenario_id,
                    merged_target_type, merged_target_id,
                )

            update_values = {
                k: v for k, v in {
                    "name": data.get("name"),
                    "target_type": data.get("target_type"),
                    "target_id": data.get("target_id"),
                    "target_label": target_label if target_changed else None,
                    "constraint_type": data.get("constraint_type"),
                    "expression": data.get("expression"),
                    "suggestion": data.get("suggestion"),
                }.items() if v is not None
            }
            obj = await repo.update(obj, tenant_id, **update_values)
            await session.commit()
            return obj.to_dict()

    async def delete_constraint(self, constraint_id: str, tenant_id: str) -> bool:
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            repo = TemplateConstraintRepository(session)
            deleted = await repo.delete_soft(constraint_id, tenant_id)
            if not deleted:
                raise ResourceNotFoundError(message=translate("err.template.constraint_not_found", params={"constraint_id": constraint_id}, fallback=f"模板约束不存在: {constraint_id}"))  # 原消息
            await session.commit()
            return True

    async def _validate_constraint_target(
        self, session, tenant_id: str, scenario_id: str,
        target_type: str, target_id: str,
    ) -> str:
        """校验 target_id 归属当前 scenario，返回 target_label 快照。"""
        valid_types = {"object", "attribute", "relation"}
        if target_type not in valid_types:
            raise InvalidParameterError(
                message=translate("err.template.invalid_constraint_target_type", params={"target_type": target_type, "valid": ', '.join(sorted(valid_types))}, fallback=f"无效的约束目标类型: {target_type}，有效值: {', '.join(sorted(valid_types))}"),  # 原消息
            )
        if target_type == "object":
            objects = await TemplateObjectRepository(session).list_all(
                tenant_id, 0, 1,
                extra_conditions=[
                    TemplateObject.scenario_id == scenario_id,
                    TemplateObject.id == target_id,
                ],
            )
            if not objects:
                raise InvalidParameterError(message=translate("err.template.constraint_same_scenario", fallback="约束目标对象不属于当前模板场景"))  # 原消息
            return objects[0].name
        if target_type == "relation":
            relations = await TemplateRelationRepository(session).list_all(
                tenant_id, 0, 1,
                extra_conditions=[
                    TemplateRelation.scenario_id == scenario_id,
                    TemplateRelation.id == target_id,
                ],
            )
            if not relations:
                raise InvalidParameterError(message=translate("err.template.constraint_rel_same_scenario", fallback="约束目标关系不属于当前模板场景"))  # 原消息
            return relations[0].name
        # target_type == "attribute": 两级校验
        if target_type == "attribute":
            result = await session.execute(
                select(TemplateAttribute, TemplateObject)
                .join(TemplateObject, TemplateAttribute.template_object_id == TemplateObject.id)
                .where(
                    TemplateAttribute.tenant_id == tenant_id,
                    TemplateAttribute.is_deleted == 0,
                    TemplateAttribute.id == target_id,
                    TemplateObject.tenant_id == tenant_id,
                    TemplateObject.is_deleted == 0,
                    TemplateObject.scenario_id == scenario_id,
                )
                .limit(1)
            )
            row = result.first()
            if not row:
                raise InvalidParameterError(
                    message=translate("err.template.constraint_target_invalid", fallback="约束目标属性不存在或所属对象不属于当前模板场景"),  # 原消息
                )
            return row[0].attr_name
        return ""

    async def _sync_constraint_labels_by_target(
        self, session, tenant_id: str,
        target_type: str, target_id: str, new_label: str,
    ) -> None:
        """更新指定 target 的所有约束的 target_label 快照。"""
        constraints = await TemplateConstraintRepository(session).list_all(
            tenant_id, 0, 10000,
            extra_conditions=[
                TemplateConstraint.target_type == target_type,
                TemplateConstraint.target_id == target_id,
            ],
        )
        for constraint in constraints:
            await TemplateConstraintRepository(session).update(
                constraint, tenant_id, target_label=new_label,
            )

    # ── Ontology YAML 导入导出 ──────────────────────────────────  # [jonex]

    async def export_ontology_yaml(self, tenant_id: str, scenario_id: str) -> dict:
        """导出场景的本体数据为 YAML。"""
        tenant_id = _check_tenant(tenant_id)
        async with get_db_session() as session:
            scenario = await TemplateScenarioRepository(session).get_required(scenario_id, tenant_id)

            object_repo = TemplateObjectRepository(session)
            relation_repo = TemplateRelationRepository(session)
            constraint_repo = TemplateConstraintRepository(session)

            objects = await object_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[TemplateObject.scenario_id == scenario_id],
            )
            relations = await relation_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[TemplateRelation.scenario_id == scenario_id],
            )
            constraints = await constraint_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[TemplateConstraint.scenario_id == scenario_id],
            )

            attr_map = await self._load_attributes(session, tenant_id, [o.id for o in objects])
            obj_name_map = {o.id: o.name for o in objects}
            obj_ontology_map: dict[str, str] = {}
            for o in objects:
                if o.ontology_code:
                    obj_ontology_map[o.id] = o.ontology_code

            warnings: list[str] = []

            # ── 实体 ──
            yaml_entities: list[YamlEntity] = []
            for obj in objects:
                yaml_attrs: list[YamlAttribute] = []
                for attr in attr_map.get(obj.id, []):
                    yaml_attr_type = to_yaml_attr_type(attr.attr_type)
                    if yaml_attr_type == "string" and attr.attr_type and attr.attr_type not in (
                        "string", "text", "number", "date", "enum", "boolean",
                        "字符串", "文本", "数值", "数字", "日期", "枚举", "布尔", "布尔值",
                    ):
                        warnings.append(
                            f"object {obj.name}.{attr.attr_name}: "
                            f"attr_type '{attr.attr_type}' mapped to '{yaml_attr_type}'"
                        )
                    yaml_attrs.append(YamlAttribute(
                        name=to_attr_code(attr.ontology_code, attr.attr_name),
                        display_name=attr.attr_name,
                        description=attr.description,
                        type=yaml_attr_type,
                        required=bool(attr.is_required),
                        is_primary_key=bool(attr.is_primary_key),
                    ))
                yaml_entities.append(YamlEntity(
                    name=to_entity_code(obj.ontology_code, obj.name),
                    display_name=obj.name,
                    description=obj.description,
                    aliases=list(obj.aliases or []),
                    attributes=yaml_attrs,
                ))

            # ── 关系 ──
            yaml_relations: list[YamlRelation] = []
            for rel in relations:
                src_name = to_entity_code(
                    obj_ontology_map.get(rel.source_object_id),
                    obj_name_map.get(rel.source_object_id, ""),
                )
                tgt_name = to_entity_code(
                    obj_ontology_map.get(rel.target_object_id),
                    obj_name_map.get(rel.target_object_id, ""),
                )
                cardinality = template_relation_type_to_card(rel.relation_type)
                yaml_relations.append(YamlRelation(
                    name=to_relation_code(rel.ontology_code, rel.name),
                    source=src_name,
                    target=tgt_name,
                    display_name=rel.name,
                    description=rel.description,
                    aliases=list(rel.aliases or []),
                    cardinality=cardinality,
                ))

            # ── 约束 ──
            # 构建 attribute id → (parent_object_name, attr_code) 索引
            attr_parent: dict[str, tuple[str, str]] = {}
            for obj in objects:
                for attr in attr_map.get(obj.id, []):
                    attr_parent[attr.id] = (obj.name, to_attr_code(attr.ontology_code, attr.attr_name))
            # 构建 relation id → relation_code 索引
            rel_code_map = {r.id: to_relation_code(r.ontology_code, r.name) for r in relations}

            yaml_constraints: list[YamlConstraint] = []
            for c in constraints:
                entity_name: str | None = None
                attr_name: str | None = None
                rel_name: str | None = None

                if c.target_type == "object":
                    entity_name = obj_name_map.get(c.target_id, c.target_label)
                elif c.target_type == "attribute":
                    parent_info = attr_parent.get(c.target_id)
                    if parent_info:
                        entity_name, attr_name = parent_info
                    else:
                        warnings.append(
                            f"constraint {c.name}: attribute target {c.target_id} not found"
                        )
                        entity_name = c.target_label
                elif c.target_type == "relation":
                    rel_name = rel_code_map.get(c.target_id, c.target_label)

                yaml_constraints.append(YamlConstraint(
                    type=c.constraint_type,
                    entity=entity_name,
                    attribute=attr_name,
                    relation=rel_name,
                    severity=c.name,
                    expression=c.expression,
                    suggestion=c.suggestion,
                    raw={},
                ))

            doc = OntologyYamlDocument(
                version=1,
                domain=scenario.name,
                entity_types=yaml_entities,
                relation_types=yaml_relations,
                constraints=yaml_constraints,
            )
            yaml_text = dump_yaml(doc)
            safe_name = scenario.name.replace(" ", "_").replace("/", "_")
            filename = f"{safe_name}_ontology.yaml"

            return {
                "filename": filename,
                "yaml_text": yaml_text,
                "warnings": warnings,
            }

    async def import_ontology_yaml(
        self, tenant_id: str, scenario_id: str, yaml_text: str,
        dry_run: bool = True, mode: str = "merge",
    ) -> dict:
        """从 YAML 导入本体数据到场景。

        dry_run=True  仅返回变更摘要，不写入数据。
        dry_run=False 在事务中 upsert entities → attributes → relations → constraints。
        """
        tenant_id = _check_tenant(tenant_id)

        # ── 解析 YAML ──
        doc, validation = parse_yaml(yaml_text)
        if doc is None:
            return {
                "dry_run": dry_run, "mode": mode,
                "summary": {},
                "warnings": validation.warnings,
                "errors": validation.errors,
            }

        async with get_db_session() as session:
            scenario = await TemplateScenarioRepository(session).get_required(scenario_id, tenant_id)
            domain_id = scenario.domain_id  # [jonex] 从场景获取 domain_id,不从 YAML

            object_repo = TemplateObjectRepository(session)
            attr_repo = TemplateAttributeRepository(session)
            relation_repo = TemplateRelationRepository(session)
            constraint_repo = TemplateConstraintRepository(session)

            # ── 加载现有数据 ──
            existing_objects = await object_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[TemplateObject.scenario_id == scenario_id],
            )
            existing_relations = await relation_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[TemplateRelation.scenario_id == scenario_id],
            )
            existing_constraints = await constraint_repo.list_all(
                tenant_id, 0, 10000,
                extra_conditions=[TemplateConstraint.scenario_id == scenario_id],
            )
            existing_attrs_map = await self._load_attributes(
                session, tenant_id, [o.id for o in existing_objects],
            )

            # ── 查找映射 ──
            # object: name -> obj, ontology_code -> obj
            obj_by_name: dict[str, TemplateObject] = {o.name: o for o in existing_objects}
            obj_by_onto: dict[str, TemplateObject] = {}
            for o in existing_objects:
                if o.ontology_code:
                    obj_by_onto[o.ontology_code] = o

            # relation: name -> rel, ontology_code -> rel
            rel_by_name: dict[str, TemplateRelation] = {r.name: r for r in existing_relations}
            rel_by_onto: dict[str, TemplateRelation] = {}
            for r in existing_relations:
                if r.ontology_code:
                    rel_by_onto[r.ontology_code] = r

            # attribute: (object_id, attr_name) -> attr
            attr_by_obj_name: dict[tuple[str, str], TemplateAttribute] = {}
            for obj_id, attrs in list(existing_attrs_map.items()):
                for a in attrs:
                    attr_by_obj_name[(obj_id, a.attr_name)] = a

            # ── 计算摘要 ──
            def _match_object(yaml_e: YamlEntity):
                """匹配已有对象：先按 ontology_code，再按 display_name。"""
                entity_code = yaml_e.name
                display_name = yaml_e.display_name or entity_code
                matched = obj_by_onto.get(entity_code) or obj_by_name.get(display_name)
                return matched

            def _calc_summary():
                summary = {
                    "entities": {"create": 0, "update": 0, "skip": 0},
                    "attributes": {"create": 0, "update": 0, "skip": 0},
                    "relations": {"create": 0, "update": 0, "skip": 0},
                    "constraints": {"create": 0, "update": 0, "skip": 0},
                }
                for yaml_e in doc.entity_types:
                    matched = _match_object(yaml_e)
                    if matched:
                        summary["entities"]["update"] += 1
                    else:
                        summary["entities"]["create"] += 1
                # existing entities not in YAML are skipped in merge mode
                yaml_entity_codes = {e.name for e in doc.entity_types}
                for o in existing_objects:
                    if o.ontology_code not in yaml_entity_codes and o.name not in {
                        e.display_name or e.name for e in doc.entity_types
                    }:
                        summary["entities"]["skip"] += 1

                # attributes — 每个 YAML entity 下的每个 attribute 判断 match
                for yaml_e in doc.entity_types:
                    matched_obj = _match_object(yaml_e)
                    for yaml_attr in yaml_e.attributes:
                        if matched_obj:
                            existing_attr = attr_by_obj_name.get(
                                (matched_obj.id, yaml_attr.display_name or yaml_attr.name),
                            )
                            if existing_attr:
                                summary["attributes"]["update"] += 1
                            else:
                                summary["attributes"]["create"] += 1
                        else:
                            summary["attributes"]["create"] += 1

                # relations
                yaml_rel_names = set()
                for yaml_rel in doc.relation_types:
                    rel_code = yaml_rel.name
                    display = yaml_rel.display_name or rel_code
                    matched = rel_by_onto.get(rel_code) or rel_by_name.get(display)
                    yaml_rel_names.add(rel_code)
                    yaml_rel_names.add(display)
                    if matched:
                        summary["relations"]["update"] += 1
                    else:
                        summary["relations"]["create"] += 1
                for r in existing_relations:
                    if r.ontology_code not in yaml_rel_names and r.name not in yaml_rel_names:
                        summary["relations"]["skip"] += 1

                # constraints
                # match by (constraint_type, target_type, target_label)
                existing_ct_keys: set[tuple[str, str, str]] = set()
                for c in existing_constraints:
                    existing_ct_keys.add((c.constraint_type, c.target_type, c.target_label))
                for yc in doc.constraints:
                    # determine target_type and label
                    if yc.relation:
                        ct_key = (yc.type, "relation", yc.relation)
                    elif yc.attribute:
                        ct_key = (yc.type, "attribute", yc.attribute)
                    elif yc.entity:
                        ct_key = (yc.type, "object", yc.entity)
                    else:
                        ct_key = (yc.type, "", "")
                    if ct_key in existing_ct_keys:
                        summary["constraints"]["update"] += 1
                    else:
                        summary["constraints"]["create"] += 1
                # existing constraints not in YAML
                yaml_ct_keys: set[tuple[str, str, str]] = set()
                for yc in doc.constraints:
                    if yc.relation:
                        yaml_ct_keys.add((yc.type, "relation", yc.relation))
                    elif yc.attribute:
                        yaml_ct_keys.add((yc.type, "attribute", yc.attribute))
                    elif yc.entity:
                        yaml_ct_keys.add((yc.type, "object", yc.entity))
                for c in existing_constraints:
                    if (c.constraint_type, c.target_type, c.target_label) not in yaml_ct_keys:
                        summary["constraints"]["skip"] += 1

                return summary

            summary = _calc_summary()

            if dry_run:
                return {
                    "dry_run": True,
                    "mode": mode,
                    "summary": summary,
                    "warnings": validation.warnings,
                    "errors": validation.errors,
                }

            # ── 非 dry_run: 事务性导入 ──
            # 重置计数器
            for key, counters in list(summary.items()):
                for sub in counters:
                    counters[sub] = 0

            # Phase 1: upsert 实体
            new_obj_map: dict[str, TemplateObject] = {}  # yaml entity name -> DB object
            for yaml_e in doc.entity_types:
                entity_code = yaml_e.name
                display_name = yaml_e.display_name or entity_code
                matched = obj_by_onto.get(entity_code) or obj_by_name.get(display_name)

                if matched:
                    await object_repo.update(matched, tenant_id,
                        name=display_name,
                        description=yaml_e.description,
                        ontology_code=entity_code if entity_code != display_name else (
                            matched.ontology_code or entity_code
                        ),
                        aliases=list(yaml_e.aliases),
                    )
                    new_obj_map[entity_code] = matched
                    summary["entities"]["update"] += 1
                else:
                    obj = await object_repo.create(
                        id=uuid.uuid4().hex,
                        tenant_id=tenant_id,
                        domain_id=domain_id,
                        scenario_id=scenario_id,
                        name=display_name,
                        description=yaml_e.description,
                        ontology_code=entity_code if entity_code != display_name else None,
                        aliases=list(yaml_e.aliases),
                    )
                    new_obj_map[entity_code] = obj
                    summary["entities"]["create"] += 1

            # Phase 2: upsert 属性
            # 收集待删属性（merge 模式：删掉不在 YAML 中的旧属性）
            new_attr_map: dict[str, TemplateAttribute] = {}  # yaml entity.attr -> DB attribute
            for obj_id in new_obj_map.values():
                # 按 entity_code 收集这次 YAML 里有的 attribute codes
                pass  # we handle this per-entity below

            for yaml_e in doc.entity_types:
                obj = new_obj_map.get(yaml_e.name)
                if obj is None:
                    continue
                existing_attrs = existing_attrs_map.get(obj.id, [])
                existing_by_name = {a.attr_name: a for a in existing_attrs}
                existing_by_onto: dict[str, TemplateAttribute] = {}
                for a in existing_attrs:
                    if a.ontology_code:
                        existing_by_onto[a.ontology_code] = a

                yaml_attr_codes = set()
                for idx, yaml_attr in enumerate(yaml_e.attributes):
                    attr_code = yaml_attr.name
                    attr_display = yaml_attr.display_name or attr_code
                    yaml_attr_codes.add(attr_code)

                    matched_attr = existing_by_onto.get(attr_code) or existing_by_name.get(attr_display)
                    if matched_attr:
                        await attr_repo.update(matched_attr, tenant_id,
                            attr_name=attr_display,
                            description=yaml_attr.description,
                            attr_type=yaml_attr.type,
                            is_primary_key=1 if yaml_attr.is_primary_key else 0,
                            is_required=1 if yaml_attr.required else 0,
                            ontology_code=attr_code if attr_code != attr_display else (
                                matched_attr.ontology_code or attr_code
                            ),
                            sort_order=idx,
                        )
                        new_attr_map[f"{yaml_e.name}.{attr_code}"] = matched_attr
                        summary["attributes"]["update"] += 1
                    else:
                        new_attr = await attr_repo.create(
                            id=uuid.uuid4().hex,
                            tenant_id=tenant_id,
                            template_object_id=obj.id,
                            attr_name=attr_display,
                            description=yaml_attr.description,
                            attr_type=yaml_attr.type,
                            is_primary_key=1 if yaml_attr.is_primary_key else 0,
                            is_required=1 if yaml_attr.required else 0,
                            ontology_code=attr_code if attr_code != attr_display else None,
                            sort_order=idx,
                            constraints_json={},
                        )
                        new_attr_map[f"{yaml_e.name}.{attr_code}"] = new_attr
                        summary["attributes"]["create"] += 1

                # 软删不在 YAML 中的旧属性及其约束（merge 模式）
                yaml_attr_display_names = {
                    a.display_name or a.name for a in yaml_e.attributes
                }
                for old_attr in existing_attrs:
                    keep_by_name = old_attr.attr_name in yaml_attr_display_names
                    keep_by_onto = (
                        old_attr.ontology_code and old_attr.ontology_code in yaml_attr_codes
                    )
                    if not keep_by_name and not keep_by_onto:
                        attr_constraints = await constraint_repo.list_all(
                            tenant_id, 0, 10000,
                            extra_conditions=[
                                TemplateConstraint.target_type == "attribute",
                                TemplateConstraint.target_id == old_attr.id,
                            ],
                        )
                        for ac in attr_constraints:
                            await constraint_repo.delete_soft(ac, tenant_id)
                        await attr_repo.delete_soft(old_attr, tenant_id)

            # Phase 3: upsert 关系
            new_rel_map: dict[str, TemplateRelation] = {}  # yaml relation name -> DB relation
            for yaml_rel in doc.relation_types:
                rel_code = yaml_rel.name
                display = yaml_rel.display_name or rel_code
                src_obj = new_obj_map.get(yaml_rel.source)
                tgt_obj = new_obj_map.get(yaml_rel.target)

                if not src_obj or not tgt_obj:
                    validation.warnings.append(
                        f"relation {rel_code}: source={yaml_rel.source} or "
                        f"target={yaml_rel.target} not found, skipping"
                    )
                    continue

                _CARD_TO_REL_TYPE = {  # [jonex] cardinality → 模板 relation_type 反向映射
                    "custom": "custom",
                    "one_to_one": "一对一",
                    "one_to_many": "一对多",
                    "many_to_many": "多对多",
                }
                relation_type = _CARD_TO_REL_TYPE.get(yaml_rel.cardinality, yaml_rel.cardinality)

                matched_rel = rel_by_onto.get(rel_code) or rel_by_name.get(display)
                if matched_rel:
                    await relation_repo.update(matched_rel, tenant_id,
                        name=display,
                        description=yaml_rel.description,
                        source_object_id=src_obj.id,
                        target_object_id=tgt_obj.id,
                        relation_type=relation_type,
                        ontology_code=rel_code if rel_code != display else (
                            matched_rel.ontology_code or rel_code
                        ),
                        aliases=list(yaml_rel.aliases),
                    )
                    new_rel_map[rel_code] = matched_rel
                    summary["relations"]["update"] += 1
                else:
                    new_rel = await relation_repo.create(
                        id=uuid.uuid4().hex,
                        tenant_id=tenant_id,
                        domain_id=domain_id,
                        scenario_id=scenario_id,
                        name=display,
                        description=yaml_rel.description,
                        source_object_id=src_obj.id,
                        target_object_id=tgt_obj.id,
                        relation_type=relation_type,
                        ontology_code=rel_code if rel_code != display else None,
                        aliases=list(yaml_rel.aliases),
                    )
                    new_rel_map[rel_code] = new_rel
                    summary["relations"]["create"] += 1

            # 软删不在 YAML 中的旧关系（merge 模式）
            yaml_rel_display_names = {
                r.display_name or r.name for r in doc.relation_types
            }
            for old_rel in existing_relations:
                keep_by_name = old_rel.name in yaml_rel_display_names
                keep_by_onto = old_rel.ontology_code and old_rel.ontology_code in {
                    r.name for r in doc.relation_types
                }
                if not keep_by_name and not keep_by_onto:
                    rel_constraints = await constraint_repo.list_all(
                        tenant_id, 0, 10000,
                        extra_conditions=[
                            TemplateConstraint.target_type == "relation",
                            TemplateConstraint.target_id == old_rel.id,
                        ],
                    )
                    for rc in rel_constraints:
                        await constraint_repo.delete_soft(rc, tenant_id)
                    await relation_repo.delete_soft(old_rel, tenant_id)

            # Phase 4: upsert 约束
            # 构建最新的 target 查找
            final_obj_name_map: dict[str, TemplateObject] = {}
            for code, obj in list(new_obj_map.items()):
                final_obj_name_map[code] = obj
                final_obj_name_map[obj.name] = obj

            # attribute: entity_code.attr_code -> TemplateAttribute
            final_attr_map: dict[str, TemplateAttribute] = new_attr_map

            # relation: code -> TemplateRelation
            final_rel_code_map: dict[str, TemplateRelation] = dict(new_rel_map)
            for rel in list(new_rel_map.values()):
                final_rel_code_map[rel.name] = rel

            existing_ct_by_key: dict[tuple[str, str, str], TemplateConstraint] = {}
            for c in existing_constraints:
                existing_ct_by_key[(c.constraint_type, c.target_type, c.target_label)] = c

            for yc in doc.constraints:
                target_type = ""
                target_id = ""
                target_label = ""

                if yc.relation:
                    resolved_rel = final_rel_code_map.get(yc.relation)
                    if resolved_rel:
                        target_type = "relation"
                        target_id = resolved_rel.id
                        target_label = resolved_rel.name
                    else:
                        validation.warnings.append(
                            f"constraint type={yc.type}: relation '{yc.relation}' not found, skipping"
                        )
                        continue
                elif yc.attribute and yc.entity:
                    # attribute 目标: entity_code.attr_code
                    attr_key = f"{yc.entity}.{yc.attribute}"
                    resolved_attr = final_attr_map.get(attr_key)
                    if resolved_attr:
                        target_type = "attribute"
                        target_id = resolved_attr.id
                        target_label = resolved_attr.attr_name
                    else:
                        validation.warnings.append(
                            f"constraint type={yc.type}: attribute '{yc.entity}.{yc.attribute}' "
                            f"not found, skipping"
                        )
                        continue
                elif yc.entity:
                    resolved_obj = final_obj_name_map.get(yc.entity)
                    if resolved_obj:
                        target_type = "object"
                        target_id = resolved_obj.id
                        target_label = resolved_obj.name
                    else:
                        validation.warnings.append(
                            f"constraint type={yc.type}: entity '{yc.entity}' not found, skipping"
                        )
                        continue
                else:
                    validation.warnings.append(
                        f"constraint type={yc.type}: no target (entity/attribute/relation), skipping"
                    )
                    continue

                ct_key = (yc.type, target_type, target_label)
                matched_ct = existing_ct_by_key.get(ct_key)
                if matched_ct:
                    await constraint_repo.update(matched_ct, tenant_id,
                        name=yc.severity or yc.type,
                        constraint_type=yc.type,
                        target_type=target_type,
                        target_id=target_id,
                        target_label=target_label,
                        expression=yc.expression,
                        suggestion=yc.suggestion,
                    )
                    summary["constraints"]["update"] += 1
                else:
                    await constraint_repo.create(
                        id=uuid.uuid4().hex,
                        tenant_id=tenant_id,
                        domain_id=domain_id,
                        scenario_id=scenario_id,
                        name=yc.severity or yc.type,
                        target_type=target_type,
                        target_id=target_id,
                        target_label=target_label,
                        constraint_type=yc.type,
                        expression=yc.expression,
                        suggestion=yc.suggestion,
                    )
                    summary["constraints"]["create"] += 1

            # 软删不在 YAML 中的旧约束（merge 模式）
            yaml_ct_keys: set[tuple[str, str, str]] = set()
            for yc in doc.constraints:
                if yc.relation:
                    resolved_rel = final_rel_code_map.get(yc.relation)
                    if resolved_rel:
                        yaml_ct_keys.add((yc.type, "relation", resolved_rel.name))
                elif yc.attribute and yc.entity:
                    attr_key = f"{yc.entity}.{yc.attribute}"
                    resolved_attr = final_attr_map.get(attr_key)
                    if resolved_attr:
                        yaml_ct_keys.add((yc.type, "attribute", resolved_attr.attr_name))
                elif yc.entity:
                    resolved_obj = final_obj_name_map.get(yc.entity)
                    if resolved_obj:
                        yaml_ct_keys.add((yc.type, "object", resolved_obj.name))
            for c in existing_constraints:
                if (c.constraint_type, c.target_type, c.target_label) not in yaml_ct_keys:
                    await constraint_repo.delete_soft(c, tenant_id)
                    summary["constraints"]["skip"] += 1

            await session.commit()

            return {
                "dry_run": False,
                "mode": mode,
                "summary": summary,
                "warnings": validation.warnings,
                "errors": validation.errors,
            }
