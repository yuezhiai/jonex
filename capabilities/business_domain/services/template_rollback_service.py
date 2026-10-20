"""版本回滚：把工作副本恢复成某个发布快照，并立即发布为新版本。

语义（用户裁定「版本回滚」）：
1. 读取目标版本的 snapshot；
2. 结构校验（码唯一、关系端点/约束目标存在）；
3. 在同一事务内把工作副本**恢复成快照**：
   - 快照里的对象/属性/关系/约束 upsert（含把曾软删的行复活）；
   - 当前存在但快照里没有的行软删；
   - 场景名称/描述/config_json 同步；
4. 事务提交后调用 `publish_scenario` 发布（生成新版本号 + 新快照）。

权限：仅租户管理员（capability 层复用发布的管理员闸门）。
失败语义：恢复事务失败 ⇒ 全部回滚、工作副本不变；恢复成功但发布失败 ⇒
工作副本已回到目标版本，管理员可手动再次发布（日志/返回里明确提示）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceNotFoundError,
)

from ..repository import TemplateScenarioRepository, TemplateScenarioVersionRepository
from . import _check_tenant
from .template_publish_service import TemplatePublishService

logger = logging.getLogger(__name__)


def _validate_snapshot(snapshot: dict) -> None:
    errors: list[str] = []
    objects = [o for o in (snapshot or {}).get("objects") or [] if isinstance(o, dict)]
    object_ids = [str(o.get("id")) for o in objects if o.get("id")]
    if len(object_ids) != len(set(object_ids)):
        errors.append("快照对象 ID 重复")
    codes = [o.get("ontology_code") for o in objects if o.get("ontology_code")]
    if len(codes) != len(set(codes)):
        errors.append("快照对象 ontology_code 重复")

    attribute_ids: set[str] = set()
    for obj in objects:
        attr_codes: list[str] = []
        for attr in obj.get("attributes") or []:
            if not isinstance(attr, dict):
                continue
            attr_id = attr.get("id")
            if not attr_id:
                errors.append(f"对象 {obj.get('name')} 存在无 ID 的属性")
                continue
            if str(attr_id) in attribute_ids:
                errors.append(f"快照属性 ID 重复: {attr_id}")
            attribute_ids.add(str(attr_id))
            code = attr.get("ontology_code")
            if code:
                attr_codes.append(code)
        if len(attr_codes) != len(set(attr_codes)):
            errors.append(f"对象 {obj.get('name')} 下属性 ontology_code 重复")

    object_id_set = set(object_ids)
    relations = [r for r in (snapshot or {}).get("relations") or [] if isinstance(r, dict)]
    for rel in relations:
        if str(rel.get("source_object_id")) not in object_id_set:
            errors.append(f"关系 {rel.get('name')} 的源对象不在快照中")
        if str(rel.get("target_object_id")) not in object_id_set:
            errors.append(f"关系 {rel.get('name')} 的目标对象不在快照中")

    for constraint in (snapshot or {}).get("constraints") or []:
        if not isinstance(constraint, dict):
            continue
        target_type = constraint.get("target_type")
        target_id = str(constraint.get("target_id"))
        if target_type == "object" and target_id not in object_id_set:
            errors.append(f"约束 {constraint.get('name')} 的目标对象不在快照中")
        if target_type == "attribute" and target_id not in attribute_ids:
            errors.append(f"约束 {constraint.get('name')} 的目标属性不在快照中")

    if errors:
        raise InvalidParameterError(
            message="发布快照无法回滚（结构不合法）",
            details={"errors": errors},
        )


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else None, ensure_ascii=False)


async def _upsert_objects(session, tenant_id: str, scenario, snapshot: dict) -> list[str]:
    ids: list[str] = []
    sql = text("""
        INSERT INTO business_domain.template_objects
            (id, tenant_id, domain_id, scenario_id, name, description, status,
             ontology_code, aliases, is_deleted, updated_at)
        VALUES (:id, :tenant, :domain, :scenario, :name, :description, :status,
                :ontology_code, CAST(:aliases AS jsonb), 0, now())
        ON CONFLICT (id) DO UPDATE SET
            domain_id = EXCLUDED.domain_id,
            scenario_id = EXCLUDED.scenario_id,
            name = EXCLUDED.name,
            description = EXCLUDED.description,
            status = EXCLUDED.status,
            ontology_code = EXCLUDED.ontology_code,
            aliases = EXCLUDED.aliases,
            is_deleted = 0,
            updated_at = now()
        WHERE business_domain.template_objects.tenant_id = EXCLUDED.tenant_id
    """)
    for obj in snapshot.get("objects") or []:
        obj_id = str(obj.get("id"))
        ids.append(obj_id)
        await session.execute(sql, {
            "id": obj_id,
            "tenant": tenant_id,
            "domain": scenario.domain_id,
            "scenario": scenario.id,
            "name": obj.get("name") or obj_id,
            "description": obj.get("description"),
            "status": obj.get("status") or "active",
            "ontology_code": obj.get("ontology_code"),
            "aliases": _json(obj.get("aliases") or []),
        })
    return ids


async def _upsert_attributes(session, tenant_id: str, snapshot: dict) -> list[str]:
    ids: list[str] = []
    sql = text("""
        INSERT INTO business_domain.template_attributes
            (id, tenant_id, template_object_id, attr_name, description, attr_type,
             is_primary_key, constraints_json, sort_order, ontology_code, is_required,
             is_deleted, updated_at)
        VALUES (:id, :tenant, :object_id, :attr_name, :description, :attr_type,
                :is_primary_key, CAST(:constraints_json AS jsonb), :sort_order,
                :ontology_code, :is_required, 0, now())
        ON CONFLICT (id) DO UPDATE SET
            template_object_id = EXCLUDED.template_object_id,
            attr_name = EXCLUDED.attr_name,
            description = EXCLUDED.description,
            attr_type = EXCLUDED.attr_type,
            is_primary_key = EXCLUDED.is_primary_key,
            constraints_json = EXCLUDED.constraints_json,
            sort_order = EXCLUDED.sort_order,
            ontology_code = EXCLUDED.ontology_code,
            is_required = EXCLUDED.is_required,
            is_deleted = 0,
            updated_at = now()
        WHERE business_domain.template_attributes.tenant_id = EXCLUDED.tenant_id
    """)
    for obj in snapshot.get("objects") or []:
        for attr in obj.get("attributes") or []:
            attr_id = str(attr.get("id"))
            ids.append(attr_id)
            await session.execute(sql, {
                "id": attr_id,
                "tenant": tenant_id,
                "object_id": str(obj.get("id")),
                "attr_name": attr.get("attr_name") or attr_id,
                "description": attr.get("description"),
                "attr_type": attr.get("attr_type") or "string",
                "is_primary_key": int(attr.get("is_primary_key") or 0),
                "constraints_json": _json(attr.get("constraints_json") or {}),
                "sort_order": int(attr.get("sort_order") or 0),
                "ontology_code": attr.get("ontology_code"),
                "is_required": int(attr.get("is_required") or 0),
            })
    return ids


async def _upsert_relations(session, tenant_id: str, scenario, snapshot: dict) -> list[str]:
    ids: list[str] = []
    sql = text("""
        INSERT INTO business_domain.template_relations
            (id, tenant_id, domain_id, scenario_id, name, description,
             source_object_id, target_object_id, relation_type, status,
             ontology_code, aliases, is_deleted, updated_at)
        VALUES (:id, :tenant, :domain, :scenario, :name, :description,
                :source, :target, :relation_type, :status,
                :ontology_code, CAST(:aliases AS jsonb), 0, now())
        ON CONFLICT (id) DO UPDATE SET
            domain_id = EXCLUDED.domain_id,
            scenario_id = EXCLUDED.scenario_id,
            name = EXCLUDED.name,
            description = EXCLUDED.description,
            source_object_id = EXCLUDED.source_object_id,
            target_object_id = EXCLUDED.target_object_id,
            relation_type = EXCLUDED.relation_type,
            status = EXCLUDED.status,
            ontology_code = EXCLUDED.ontology_code,
            aliases = EXCLUDED.aliases,
            is_deleted = 0,
            updated_at = now()
        WHERE business_domain.template_relations.tenant_id = EXCLUDED.tenant_id
    """)
    for rel in snapshot.get("relations") or []:
        rel_id = str(rel.get("id"))
        ids.append(rel_id)
        await session.execute(sql, {
            "id": rel_id,
            "tenant": tenant_id,
            "domain": scenario.domain_id,
            "scenario": scenario.id,
            "name": rel.get("name") or rel_id,
            "description": rel.get("description"),
            "source": str(rel.get("source_object_id")),
            "target": str(rel.get("target_object_id")),
            "relation_type": rel.get("relation_type") or "custom",
            "status": rel.get("status") or "active",
            "ontology_code": rel.get("ontology_code"),
            "aliases": _json(rel.get("aliases") or []),
        })
    return ids


async def _upsert_constraints(session, tenant_id: str, scenario, snapshot: dict) -> list[str]:
    ids: list[str] = []
    sql = text("""
        INSERT INTO business_domain.template_constraints
            (id, tenant_id, domain_id, scenario_id, name, target_type, target_id,
             target_label, constraint_type, expression, suggestion, is_deleted, updated_at)
        VALUES (:id, :tenant, :domain, :scenario, :name, :target_type, :target_id,
                :target_label, :constraint_type, :expression, :suggestion, 0, now())
        ON CONFLICT (id) DO UPDATE SET
            domain_id = EXCLUDED.domain_id,
            scenario_id = EXCLUDED.scenario_id,
            name = EXCLUDED.name,
            target_type = EXCLUDED.target_type,
            target_id = EXCLUDED.target_id,
            target_label = EXCLUDED.target_label,
            constraint_type = EXCLUDED.constraint_type,
            expression = EXCLUDED.expression,
            suggestion = EXCLUDED.suggestion,
            is_deleted = 0,
            updated_at = now()
        WHERE business_domain.template_constraints.tenant_id = EXCLUDED.tenant_id
    """)
    for item in snapshot.get("constraints") or []:
        item_id = str(item.get("id"))
        ids.append(item_id)
        await session.execute(sql, {
            "id": item_id,
            "tenant": tenant_id,
            "domain": scenario.domain_id,
            "scenario": scenario.id,
            "name": item.get("name") or item_id,
            "target_type": item.get("target_type") or "object",
            "target_id": str(item.get("target_id")),
            "target_label": item.get("target_label") or "",
            "constraint_type": item.get("constraint_type") or "custom",
            "expression": item.get("expression"),
            "suggestion": item.get("suggestion"),
        })
    return ids


async def _soft_delete_missing(
    session, table: str, tenant_id: str, scenario_id: str, ids: list[str],
    *, via_objects: bool = False,
) -> int:
    if via_objects:
        sql = text(f"""
            UPDATE business_domain.{table} a
               SET is_deleted = 1, updated_at = now()
              FROM business_domain.template_objects o
             WHERE a.template_object_id = o.id
               AND o.tenant_id = :tenant
               AND o.scenario_id = :scenario
               AND a.tenant_id = :tenant
               AND a.is_deleted = 0
               AND NOT (a.id = ANY(:ids))
        """)
    else:
        sql = text(f"""
            UPDATE business_domain.{table}
               SET is_deleted = 1, updated_at = now()
             WHERE tenant_id = :tenant
               AND scenario_id = :scenario
               AND is_deleted = 0
               AND NOT (id = ANY(:ids))
        """)
    result = await session.execute(
        sql, {"tenant": tenant_id, "scenario": scenario_id, "ids": ids},
    )
    return result.rowcount or 0


class TemplateRollbackService:
    """按发布快照回滚工作副本并发布。"""

    @staticmethod
    async def rollback(
        tenant_id: str,
        scenario_id: str,
        version: int,
        published_by: str | None = None,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        if not scenario_id:
            raise InvalidParameterError(message="scenario_id 不能为空")
        try:
            version_no = int(version)
        except (TypeError, ValueError):
            raise InvalidParameterError(message="version 必须是正整数")
        if version_no < 1:
            raise InvalidParameterError(message="version 必须是正整数")

        from jonex_core.common.database import get_db_session

        async with get_db_session() as session:
            scenario_repo = TemplateScenarioRepository(session)
            scenario = await scenario_repo.get_required(scenario_id, tenant_id)
            version_repo = TemplateScenarioVersionRepository(session)
            row = await version_repo.get_version(tenant_id, scenario_id, version_no)
            if row is None:
                raise ResourceNotFoundError(
                    message="模板版本不存在",
                    details={"scenario_id": scenario_id, "version": version_no},
                )
            snapshot = row.snapshot or {}
            _validate_snapshot(snapshot)

            scenario_snapshot = snapshot.get("scenario") or {}
            await session.execute(
                text("""
                    UPDATE business_domain.template_scenarios
                       SET name = :name,
                           description = :description,
                           config_json = CAST(:config AS jsonb),
                           updated_at = now()
                     WHERE id = :scenario AND tenant_id = :tenant
                """),
                {
                    "name": scenario_snapshot.get("name") or scenario.name,
                    "description": scenario_snapshot.get("description"),
                    "config": _json(scenario_snapshot.get("config_json") or {}),
                    "scenario": scenario_id,
                    "tenant": tenant_id,
                },
            )

            object_ids = await _upsert_objects(session, tenant_id, scenario, snapshot)
            attribute_ids = await _upsert_attributes(session, tenant_id, snapshot)
            relation_ids = await _upsert_relations(session, tenant_id, scenario, snapshot)
            constraint_ids = await _upsert_constraints(session, tenant_id, scenario, snapshot)

            deleted = {
                "objects": await _soft_delete_missing(
                    session, "template_objects", tenant_id, scenario_id, object_ids,
                ),
                "attributes": await _soft_delete_missing(
                    session, "template_attributes", tenant_id, scenario_id,
                    attribute_ids, via_objects=True,
                ),
                "relations": await _soft_delete_missing(
                    session, "template_relations", tenant_id, scenario_id, relation_ids,
                ),
                "constraints": await _soft_delete_missing(
                    session, "template_constraints", tenant_id, scenario_id,
                    constraint_ids,
                ),
            }
            await session.commit()

        counts = {
            "objects": len(object_ids),
            "attributes": len(attribute_ids),
            "relations": len(relation_ids),
            "constraints": len(constraint_ids),
            "soft_deleted": deleted,
        }

        try:
            published = await TemplatePublishService.publish_scenario(
                tenant_id, scenario_id, published_by=published_by,
            )
        except Exception:
            logger.error(
                "回滚已恢复工作副本，但发布失败（场景 %s，目标版本 %s）；"
                "请人工检查后重新发布",
                scenario_id, version_no, exc_info=True,
            )
            raise

        return {
            "scenario_id": scenario_id,
            "restored_from_version": version_no,
            "counts": counts,
            "published": published,
        }


__all__ = ["TemplateRollbackService"]
