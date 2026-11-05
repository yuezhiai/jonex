"""
Ontology compiler and KB-side ontology editor service.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from jonex_core.common.cache import CacheUtil
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..repository.knowledge_info_repository import KnowledgeInfoRepository
from ..repository.ontology_abstract_schema_repository import (
    OntologyAbstractSchemaRepository,
)
from ..repository.ontology_data_alignment_repository import (
    OntologyDataAlignmentRepository,
)
from ..repository.ontology_schema_repository import OntologySchemaRepository
from .template_schema_provider import ObjectDef, ScenarioDef, TemplateSchemaProvider

logger = logging.getLogger(__name__)

ONTOLOGY_AUTO_COMPILE = os.getenv("ONTOLOGY_AUTO_COMPILE", "true").lower() == "true"
ONTOLOGY_COMPILED_SCHEMA_TTL = int(os.getenv("ONTOLOGY_COMPILED_SCHEMA_TTL", "3600"))
ONTOLOGY_SCHEMA_FALLBACK_PATH = os.getenv(
    "ONTOLOGY_SCHEMA_FALLBACK_PATH",
    "deploy/config/ontology/default.yaml",
)

VALID_ATTR_TYPES = {"string", "text", "number", "date", "enum", "boolean"}
VALID_CARDINALITY = {"one_to_one", "one_to_many", "many_to_many", "custom"}

CARDINALITY_MAP = {
    "一对多": "one_to_many",
    "多对多": "many_to_many",
    "一对一": "one_to_one",
    "自定义": "custom",
    "one_to_many": "one_to_many",
    "many_to_many": "many_to_many",
    "one_to_one": "one_to_one",
    "custom": "custom",
}


def _ds_slot(data_source_id: Optional[str]) -> str:
    """缓存键里的数据源槽位。

    知识库级（None）用固定占位符而不是省略——否则 `{tenant}:{kb}` 会和
    「某数据源」的键前缀撞上，两类 schema 互相覆盖。
    """
    return data_source_id or "-"


def _compiled_key(tenant_id: str, knowledge_base_id: str, data_source_id: Optional[str] = None) -> str:
    return f"ontology:compiled:{tenant_id}:{knowledge_base_id}:{_ds_slot(data_source_id)}"


def _hash_key(tenant_id: str, knowledge_base_id: str, data_source_id: Optional[str] = None) -> str:
    return f"ontology:compiled:hash:{tenant_id}:{knowledge_base_id}:{_ds_slot(data_source_id)}"


def abstract_to_compiled_types(schema: dict) -> tuple[list[dict], list[dict]]:
    """v2 抽象 schema → compiled 形状的 (entity_types, relation_types)。

    - 实体带扩展字段 `sources`（**仅 included** 落点）与编译形状属性
      （`primary_key` → `is_primary_key`）；
    - 关系带扩展字段 `backings`（原样，含 join 键与 origin）；
    - 这里**不做**标脏、不读库：纯映射，供投影与测试共用。
    """
    entity_types: list[dict] = []
    for e in (schema or {}).get("entities") or []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        entity: dict = {
            "name": e["name"],
            "description": e.get("description") or "",
            "aliases": list(e.get("aliases") or []),
            "attributes": [
                {
                    "name": a.get("name"),
                    "type": a.get("type") or "string",
                    "required": bool(a.get("required")),
                    "is_primary_key": bool(a.get("primary_key")),
                    **({"display_name": a["display_name"]} if a.get("display_name") else {}),
                    **({"description": a["description"]} if a.get("description") else {}),
                    **({"enum_values": list(a["enum_values"])} if a.get("enum_values") else {}),
                }
                for a in e.get("attributes") or []
                if isinstance(a, dict) and a.get("name")
            ],
            "sources": [
                {"data_source_id": s.get("data_source_id"), "entity_name": s.get("entity_name")}
                for s in e.get("sources") or []
                if isinstance(s, dict) and s.get("included") is not False
            ],
        }
        if e.get("display_name"):
            entity["display_name"] = e["display_name"]
        entity_types.append(entity)

    relation_types: list[dict] = []
    for r in (schema or {}).get("relations") or []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        relation: dict = {
            "name": r["name"],
            "source": r.get("source"),
            "target": r.get("target"),
            "description": r.get("description") or "",
            "backings": [dict(b) for b in r.get("backings") or [] if isinstance(b, dict)],
        }
        if r.get("cardinality"):
            relation["cardinality"] = r["cardinality"]
        relation_types.append(relation)
    return entity_types, relation_types


def merge_schema_parts(parts: list[dict], tenant_id: str, knowledge_base_id: str) -> dict:
    """把若干「schema 形状 dict」（抽象投影在前 + 各 ds 行）合并成一份视图。

    合并规则与 `get_merged_schema` 原实现一致（按 name 去重、先到先得、
    disambiguation 取第一份非空），本条只是把「行列表」换成「dict 列表」以便
    投影与 ds 行同路装配、纯函数可测。
    """
    merged: dict = {
        "tenant_id": tenant_id,
        "knowledge_base_id": knowledge_base_id,
        "data_source_id": None,
        "schema_version": max((int(p.get("schema_version") or 1) for p in parts), default=1),
        "source_type": "merged",
        "entity_types": [],
        "relation_types": [],
        "constraints": [],
        "disambiguation": None,
        "sources": [
            {"data_source_id": p.get("data_source_id"), "schema_version": p.get("schema_version")}
            for p in parts
        ],
    }
    seen: dict[str, set] = {"entity_types": set(), "relation_types": set(), "constraints": set()}
    for part in parts:
        for key in ("entity_types", "relation_types", "constraints"):
            for item in part.get(key) or []:
                name = (item or {}).get("name")
                if name and name in seen[key]:
                    continue
                if name:
                    seen[key].add(name)
                merged[key].append(item)
        if merged["disambiguation"] is None and part.get("disambiguation"):
            merged["disambiguation"] = part["disambiguation"]
    if merged["disambiguation"] is None:
        merged["disambiguation"] = {"case_insensitive": True, "alias_merge": True}
    return merged


def _normalize_cardinality(raw: str) -> str:
    return CARDINALITY_MAP.get((raw or "").strip(), "custom")


def _normalize_editor_status(raw: Optional[str]) -> str:
    value = (raw or "").strip().lower()
    if value in {"inactive", "disabled", "draft", "off"}:
        return "inactive"
    return "active"


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = (item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def resolve_freshness(
    stored,
    schema: Optional[dict],
    expected_hash: Optional[str] = None,
) -> str:
    """KB 级行新鲜度（纯函数，读时计算）。

    - 有存储行：manual_edited 优先，其次显式 outdated，再其次 **来源 hash 比对**
      （`expected_hash` 非空且与 `stored.source_hash` 不等 → outdated），
      否则 seeded；
    - 无存储行：抽象投影 → abstract_projection；否则有可用 schema → seeded；
      两者都没有 → missing。

    `expected_hash` 是当前模板场景的 `structure_hash`（YAML 兜底没有可比对象，
    调用方传 None ⇒ 保持 seeded）。比较口径只有这一处定义：状态页与跨模态对齐
    必须共用，否则同一个 KB 会按读取入口给出两种新鲜度。
    """
    if stored is not None:
        if (getattr(stored, "schema_mode", None) or "") == "manual_edited":
            return "manual_edited"
        if (getattr(stored, "sync_status", None) or "") == "outdated":
            return "outdated"
        if expected_hash and (getattr(stored, "source_hash", None) != expected_hash):
            return "outdated"
        return "seeded"
    if schema is None:
        return "missing"
    if (schema.get("source_type") or "") == "abstract":
        return "abstract_projection"
    return "seeded"


def stored_entity_type_names(stored) -> list[str]:
    """存储的 KB 级行上的实体名（统一口径，Task 3 复用）。

    **只认行对象自带的 `entity_types`**：抽象投影 / YAML 兜底 / 无行
    都没有「存储的 KB 级行实体名集合」，返回空列表。
    """
    names: list[str] = []
    for entity in getattr(stored, "entity_types", None) or []:
        if isinstance(entity, dict):
            name = str(entity.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def diff_entity_types(current: list[dict], candidate: list[dict]) -> dict:
    """实体类型差异：优先按 `source_object_id` 识别 rename，其余按名字。"""
    cur = [e for e in current or [] if isinstance(e, dict)]
    cand = [e for e in candidate or [] if isinstance(e, dict)]
    cur_by_name = {str(e.get("name") or "").strip(): e for e in cur
                   if str(e.get("name") or "").strip()}
    cand_by_name = {str(e.get("name") or "").strip(): e for e in cand
                    if str(e.get("name") or "").strip()}
    cur_by_obj = {str(e.get("source_object_id") or "").strip(): e for e in cur
                  if str(e.get("source_object_id") or "").strip()}
    renamed: list[dict] = []
    consumed_cur: set[str] = set()
    consumed_cand: set[str] = set()
    for name, entity in cand_by_name.items():
        obj = str(entity.get("source_object_id") or "").strip()
        old = cur_by_obj.get(obj) if obj else None
        old_name = str(old.get("name") or "").strip() if old else ""
        # 同一旧实体（重复 source_object_id）只产出一次 rename：后出现的新名
        # 不能再认领它（否则 `from` 重复、新名还会误入 added）。
        if (
            old_name
            and old_name != name
            and old_name not in cand_by_name
            and old_name not in consumed_cur
        ):
            renamed.append({"from": old_name, "to": name})
            consumed_cur.add(old_name)
            consumed_cand.add(name)
    return {
        "added": sorted(n for n in cand_by_name
                        if n not in cur_by_name and n not in consumed_cand),
        "removed": sorted(n for n in cur_by_name
                          if n not in cand_by_name and n not in consumed_cur),
        "renamed": renamed,
    }


def _filter_mirror_constraints(
    constraints, entity_types: list[dict], relation_types: list[dict],
) -> list[dict]:
    """镜像回写时保留的约束：只留目标仍存在的。

    抽象 schema 不建模 constraints，镜像源的约束来自**原 KB 级行**。实体/关系
    被抽象层改名或删除后，原约束会指空；`save_compiled_schema` 的
    `_normalize_constraints` 会因此拒绝整次写入。这里先过滤，写回只丢失效约束。
    """
    attrs = {
        str(e.get("name")): {
            str(a.get("name")) for a in (e.get("attributes") or [])
            if isinstance(a, dict) and a.get("name")
        }
        for e in entity_types if isinstance(e, dict) and e.get("name")
    }
    relations = {str(r.get("name")) for r in relation_types if isinstance(r, dict) and r.get("name")}
    kept: list[dict] = []
    for constraint in constraints or []:
        if not isinstance(constraint, dict):
            continue
        kind = constraint.get("type")
        if kind == "relation_cardinality":
            if str(constraint.get("relation") or "") in relations:
                kept.append(constraint)
        elif kind in ("required_attribute", "unique_attribute", "value_range"):
            entity = str(constraint.get("entity") or "")
            if entity in attrs and str(constraint.get("attribute") or "") in attrs[entity]:
                kept.append(constraint)
        else:
            # 未知类型交给 save 的 _normalize_constraints 判（保持原口径）。
            kept.append(constraint)
    return kept


class OntologyCompiler:
    """Compile template schema to KB snapshots and manage KB-side edits."""

    async def bind_template(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        template_domain_id: Optional[str] = None,
        template_scenario_id: Optional[str] = None,
        source_type: str = "business_template",
    ) -> dict:
        tenant_id = require_tenant(tenant_id)

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            binding = await repo.upsert_binding(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                template_domain_id=template_domain_id,
                template_scenario_id=template_scenario_id,
                source_type=source_type,
            )
            await session.commit()

        if ONTOLOGY_AUTO_COMPILE:
            try:
                await self.compile_for_knowledge_base(tenant_id, knowledge_base_id, force=True)
            except Exception as e:
                logger.warning("Auto-compile failed after binding: %s", e)

        return binding.to_dict()

    async def compile_for_knowledge_base(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        force: bool = False,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            binding = await repo.get_binding(tenant_id, knowledge_base_id)

            if binding and binding.template_scenario_id:
                provider = TemplateSchemaProvider(session)
                scenario = await provider.load_scenario(tenant_id, binding.template_scenario_id)
                if scenario is None:
                    raise ResourceNotFoundError(
                        message=translate("err.template.scenario_not_available", params={"scenario_id": binding.template_scenario_id}, fallback=f"模板场景不存在或不可用: {binding.template_scenario_id}")  ,  # 原消息
                    )
                compiled = self._compile_from_scenario(scenario, source_type=binding.source_type or "business_template")
            else:
                compiled = await self._compile_from_yaml_fallback()
                compiled["template_domain_id"] = None
                compiled["template_scenario_id"] = None
                compiled["source_type"] = "yaml_default"

            compiled["tenant_id"] = tenant_id
            compiled["knowledge_base_id"] = knowledge_base_id

            # [jonex] P2-H：force=False 且来源 hash 未变（且非人工编辑）→ 短路复用，
            # 不 bump schema_version、不覆盖快照，返回 unchanged=True。
            if not force:
                existing = await repo.get_compiled_schema(tenant_id, knowledge_base_id)
                if (
                    existing is not None
                    and existing.schema_mode != "manual_edited"
                    and existing.source_hash
                    and existing.source_hash == compiled.get("source_hash")
                ):
                    result = existing.to_dict()
                    result["unchanged"] = True
                    await self._set_cache(tenant_id, knowledge_base_id, existing.to_dict())
                    return result

            cs = await repo.upsert_compiled_schema(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                template_domain_id=compiled.get("template_domain_id"),
                template_scenario_id=compiled.get("template_scenario_id"),
                source_type=compiled["source_type"],
                source_version=compiled.get("source_version", 1),
                source_hash=compiled.get("source_hash"),
                entity_types=compiled["entity_types"],
                relation_types=compiled["relation_types"],
                constraints=compiled.get("constraints", []),
                disambiguation=compiled.get("disambiguation", {}),
                prompt_schema=compiled["prompt_schema"],
                schema_mode="template_seeded",
                sync_status="synced",
            )
            await session.commit()

        await self._set_cache(tenant_id, knowledge_base_id, cs.to_dict())
        return cs.to_dict()

    async def recompile_for_knowledge_base(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        force: bool = False,
        apply_to_documents: bool = False,
    ) -> dict:
        """[jonex] P2-H：从模板/yaml 重新编译 compiled schema（生产链路入口）。

        - 遇 `schema_mode == manual_edited` → 409（ResourceConflictError），提示改用 reseed；
          即 recompile 永不覆盖人工编辑，丢弃人工编辑的唯一入口是 reseed。
        - `force=True` 仅对非人工编辑跳过 source_hash 短路、无条件重生成并覆盖快照。
        - `apply_to_documents=True` 时，编译成功后顺带把该 KB 文档置 PENDING（对账被动重抽）。

        返回分步契约：{schema_saved, schema_version, unchanged, documents_matched,
        documents_queued, apply_error?}。编译与批量重置任一失败独立报告。
        """
        tenant_id = require_tenant(tenant_id)

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            current = await repo.get_compiled_schema(tenant_id, knowledge_base_id)

        if current is not None and current.schema_mode == "manual_edited":
            raise ResourceConflictError(
                message=translate("err.ontology.manual_schema_no_recompile", fallback="该知识库 schema 为人工编辑，recompile 不会覆盖；如需从模板重置请使用 reseed")  ,  # 原消息
                details={"knowledge_base_id": knowledge_base_id, "schema_mode": "manual_edited"},
            )

        compiled = await self.compile_for_knowledge_base(tenant_id, knowledge_base_id, force=force)
        schema_version = compiled.get("schema_version")

        result = {
            "schema_saved": True,
            "schema_version": schema_version,
            "unchanged": bool(compiled.get("unchanged", False)),
            "documents_matched": 0,
            "documents_queued": 0,
        }

        if apply_to_documents:
            try:
                from ..repository import KnowledgeDocumentRepository

                async with get_db_session() as session:
                    doc_repo = KnowledgeDocumentRepository(session)
                    stats = await doc_repo.reset_ontology_for_kb(
                        tenant_id, knowledge_base_id,
                        target_schema_version=schema_version,
                    )
                    await session.commit()
                result["documents_matched"] = stats.get("matched", 0)
                result["documents_queued"] = stats.get("reset", 0)
            except Exception as e:
                logger.warning(
                    "recompile apply_to_documents failed kb=%s: %s", knowledge_base_id, e,
                )
                result["apply_error"] = str(e)[:500]

        return result

    async def get_compiled_schema(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        data_source_id: Optional[str] = None,
        auto_compile: bool = True,
    ) -> Optional[dict]:
        """取一份编译 schema。

        `data_source_id=None` → **知识库级**（与改造前完全一致，既有调用方零改动）；
        给了 id → 该数据源自己的那份。
        """
        tenant_id = require_tenant(tenant_id)

        # active 抽象 schema 存在 → 知识库级就是它的投影。放在缓存之前是有意的：
        # 遗留行写进缓存的旧值不该盖过已生效的抽象内容（投影本身不缓存）。
        if data_source_id is None:
            projected = await self._abstract_projection(tenant_id, knowledge_base_id)
            if projected is not None:
                return projected

        cached = await self._get_cache(tenant_id, knowledge_base_id, data_source_id)
        if cached is not None:
            return cached

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            schema = await repo.get_compiled_schema(tenant_id, knowledge_base_id, data_source_id)
            if schema is not None:
                result = schema.to_dict()
                await self._set_cache(tenant_id, knowledge_base_id, result, data_source_id)
                return result

        # 兜底路径（自动编译 / YAML 默认）产出的是知识库级语义，
        # 只对「要知识库级」的调用返回；否则会把 KB 级那份错当成数据源的
        if data_source_id is not None:
            return None

        if auto_compile and ONTOLOGY_AUTO_COMPILE:
            try:
                return await self.compile_for_knowledge_base(tenant_id, knowledge_base_id)
            except Exception as e:
                logger.warning("Auto-compile failed for %s/%s: %s", tenant_id, knowledge_base_id, e)

        try:
            return await self._compile_from_yaml_fallback()
        except Exception as e:
            logger.error(
                "Compiled schema unavailable for %s/%s: %s",
                tenant_id,
                knowledge_base_id,
                e,
            )
            return None

    async def _expected_source_hash(
        self, session, tenant_id: str, binding,
    ) -> Optional[str]:
        """当前绑定的模板场景 structure_hash（只读，不编译、不落库）。

        只取**模板来源**的期望 hash：无绑定 / 无 `template_scenario_id` /
        场景不可用 → None（YAML 兜底没有可比的期望 hash）。
        场景不可用时留 warning，便于定位「绑定还在、模板没了」。
        """
        if binding is None or not binding.template_scenario_id:
            return None
        scenario = await TemplateSchemaProvider(session).load_scenario(
            tenant_id, binding.template_scenario_id)
        if scenario is None:
            logger.warning(
                "KB schema status: template scenario unavailable tenant=%s scenario=%s",
                tenant_id, binding.template_scenario_id,
            )
            return None
        return getattr(scenario, "structure_hash", None)

    async def _fallback_schema_for_status(
        self, tenant_id: str, knowledge_base_id: str,
    ) -> Optional[dict]:
        projected = await self._abstract_projection(tenant_id, knowledge_base_id)
        if projected is not None:
            return projected
        try:
            return await self._compile_from_yaml_fallback()
        except Exception as exc:  # YAML 不可用 = 没有可用 TBox
            logger.warning(
                "KB schema status: yaml fallback unavailable kb=%s: %s",
                knowledge_base_id, exc,
            )
            return None

    async def get_kb_schema_status(
        self, tenant_id: str, knowledge_base_id: str,
    ) -> dict:
        """KB 级 compiled schema 的读取契约（只读，不触发自动编译）。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            await KnowledgeInfoRepository(session).get_required(
                knowledge_base_id, tenant_id)
            repo = OntologySchemaRepository(session)
            binding = await repo.get_binding(tenant_id, knowledge_base_id)
            stored = await repo.get_compiled_schema(tenant_id, knowledge_base_id)
            expected_hash = await self._expected_source_hash(
                session, tenant_id, binding)
            alignments = await OntologyDataAlignmentRepository(session).list_by_kb(
                tenant_id, knowledge_base_id)

            # 基于 ORM 行的读取全部落在 session 块内：退出后实例可能已过期，
            # 不依赖懒加载 / expire_on_commit 行为。
            has_stored = stored is not None
            stored_values = {
                "source_type": getattr(stored, "source_type", None),
                "source_version": getattr(stored, "source_version", None),
                "source_hash": getattr(stored, "source_hash", None),
                "schema_version": getattr(stored, "schema_version", None),
                "compiled_at": getattr(stored, "compiled_at", None),
                "edited_at": getattr(stored, "edited_at", None),
                "edited_by": getattr(stored, "edited_by", None),
            } if has_stored else {}
            template_domain_id = getattr(binding, "template_domain_id", None)
            template_scenario_id = getattr(binding, "template_scenario_id", None)
            active_rows = [
                row for row in alignments
                if (getattr(row, "status", None) or "") == "active"
            ]

            if has_stored:
                schema = stored.to_dict()
                # doc_types 只认**存储的 KB 级行**（不是抽象投影、不是 YAML 兜底）。
                doc_types = set(stored_entity_type_names(stored))
                # M-2：只对 active/proposed 行判陈旧。rejected 是人已否决的
                # 候选，既不代表当前文档侧事实，也不应计入影响面；行本身保留
                # （不删除、不翻状态）。
                stale = sorted({
                    str(row.doc_type).strip() for row in alignments
                    if (getattr(row, "status", None) or "") in ("active", "proposed")
                    and str(getattr(row, "doc_type", "") or "").strip()
                    and str(row.doc_type).strip() not in doc_types
                })
                # 来源 hash 与当前模板不一致 ⇒ seeded 之上再判 outdated（I-3）。
                freshness = resolve_freshness(
                    stored, schema, expected_hash=expected_hash)
            else:
                # 无存储行时没有可比对的实体名集合：抽象投影 / YAML 兜底 /
                # missing 一律不判 stale（不删除对齐行，也不虚构过期）。
                schema = None
                doc_types = set()
                stale = []
                freshness = "missing"

            # `projected_maps_to` 是**估算值**（下界）：只统计 doc_type 能落到
            # 当前存储行实体名集合里的 active 行，并按 (doc_type, data_source_id)
            # 去重。精确值需复用对齐投影器（实例级 MAPS_TO 边数），这里不复算投影。
            projected_maps_to = len({
                (str(getattr(row, "doc_type", "") or "").strip(),
                 getattr(row, "data_source_id", None))
                for row in active_rows
                if str(getattr(row, "doc_type", "") or "").strip() in doc_types
            })

        if not has_stored:
            schema = await self._fallback_schema_for_status(tenant_id, knowledge_base_id)
            freshness = resolve_freshness(None, schema)

        # M-1：状态读取留一行可观测日志（只记计数，不落完整 schema）。
        logger.info(
            "KB schema status: kb=%s freshness=%s stored=%s stale_doc_types=%d "
            "active_alignments=%d projected_maps_to=%d",
            knowledge_base_id, freshness, has_stored, len(stale),
            len(active_rows), projected_maps_to,
        )

        return {
            "schema": schema,
            "freshness": freshness,
            "source_type": stored_values.get("source_type") or (schema or {}).get("source_type"),
            "source_version": stored_values.get("source_version") or (schema or {}).get("source_version"),
            "source_hash": stored_values.get("source_hash") or (schema or {}).get("source_hash"),
            "schema_version": stored_values.get("schema_version") or (schema or {}).get("schema_version"),
            "compiled_at": stored_values.get("compiled_at"),
            "edited_at": stored_values.get("edited_at"),
            "edited_by": stored_values.get("edited_by"),
            "expected_source_hash": expected_hash,
            "template_domain_id": template_domain_id,
            "template_scenario_id": template_scenario_id,
            "stale_doc_types": stale,
            "alignment_impact": {
                "active_alignments": len(active_rows),
                "stale_doc_types": len(stale),
                "projected_maps_to": projected_maps_to,
            },
        }

    async def preview_recompile(
        self, tenant_id: str, knowledge_base_id: str,
    ) -> dict:
        """预览一次 recompile 的差异与影响面（只读：不 upsert、不重置文档）。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            await KnowledgeInfoRepository(session).get_required(
                knowledge_base_id, tenant_id)
            repo = OntologySchemaRepository(session)
            binding = await repo.get_binding(tenant_id, knowledge_base_id)
            current = await repo.get_compiled_schema(tenant_id, knowledge_base_id)
            if binding and binding.template_scenario_id:
                scenario = await TemplateSchemaProvider(session).load_scenario(
                    tenant_id, binding.template_scenario_id)
                if scenario is None:
                    raise ResourceNotFoundError(
                        message=translate(
                            "err.template.scenario_not_available",
                            params={"scenario_id": binding.template_scenario_id},
                            fallback=f"模板场景不存在或不可用: {binding.template_scenario_id}"),
                    )
                candidate = self._compile_from_scenario(
                    scenario, source_type=binding.source_type or "business_template")
            else:
                candidate = await self._compile_from_yaml_fallback()
            alignments = await OntologyDataAlignmentRepository(session).list_by_kb(
                tenant_id, knowledge_base_id)

            # 基于 ORM 行的读取全部落在 session 块内（与状态页同口径）：
            # 退出后实例可能已过期，不依赖懒加载 / expire_on_commit 行为。
            current_entity_types = (
                [dict(e) for e in (getattr(current, "entity_types", None) or [])
                 if isinstance(e, dict)]
                if current is not None else []
            )
            current_hash = (
                getattr(current, "source_hash", None) if current is not None else None)
            current_mode = (
                (getattr(current, "schema_mode", None) or "")
                if current is not None else "")
            candidate_entity_types = [
                dict(e) for e in (candidate.get("entity_types") or [])
                if isinstance(e, dict)]
            candidate_hash = candidate.get("source_hash")
            alignment_pairs = [
                (
                    str(getattr(row, "doc_type", "") or "").strip(),
                    (getattr(row, "status", None) or ""),
                    getattr(row, "data_source_id", None),
                )
                for row in alignments
            ]

        diff = diff_entity_types(current_entity_types, candidate_entity_types)
        # 与 `compile_for_knowledge_base(force=False)` 的短路条件同口径：只有
        # 「有行 + 来源 hash 非空 + hash 未变 + 非人工编辑」才复用既有快照。
        # YAML 兜底来源的 source_hash 恒为 None ⇒ 必然 upsert，预览不能显示无变化。
        would_short_circuit = bool(
            current is not None
            and current_hash
            and current_hash == candidate_hash
            and current_mode != "manual_edited"
        )
        # `stale_after` 语义 = 本次重编译后**新**失效的 doc_type
        # （removed ∪ renamed.from），不是「当前已 stale 的集合」。
        stale_after = sorted(
            set(diff["removed"]) | {r["from"] for r in diff["renamed"]})
        stale_set = set(stale_after)
        # 影响面与 M-2 同口径：rejected 行是人已否决的候选，不计入
        # `affected_alignments` / `affected_maps_to`（行本身保留）。
        affected = [
            pair for pair in alignment_pairs
            if pair[0] in stale_set and pair[1] in ("active", "proposed")
        ]
        # `affected_maps_to` 是估算值（下界），与状态页 `alignment_impact`
        # 同口径：只统计 active 行，并按 (doc_type, data_source_id) 去重。
        # 精确值需复用对齐投影器（实例级 MAPS_TO 边数）。
        affected_maps_to = len({
            (doc_type, data_source_id)
            for doc_type, status, data_source_id in affected
            if status == "active"
        })
        # M-1：预览只读，留一行影响面摘要（计数即可，不落完整 diff/schema）。
        logger.info(
            "KB schema recompile preview: kb=%s changed=%s source_changed=%s "
            "manual_edited=%s added=%d removed=%d renamed=%d affected_alignments=%d",
            knowledge_base_id,
            (not would_short_circuit) or bool(
                diff["added"] or diff["removed"] or diff["renamed"]),
            current is None or current_hash != candidate_hash,
            current_mode == "manual_edited",
            len(diff["added"]), len(diff["removed"]), len(diff["renamed"]),
            len(affected),
        )
        return {
            "changed": (not would_short_circuit) or bool(
                diff["added"] or diff["removed"] or diff["renamed"]),
            "source_changed": current is None or current_hash != candidate_hash,
            "manual_edited": current_mode == "manual_edited",
            "added_entity_types": diff["added"],
            "removed_entity_types": diff["removed"],
            "renamed_entity_types": diff["renamed"],
            "affected_alignments": len(affected),
            "stale_after": stale_after,
            "affected_maps_to": affected_maps_to,
        }

    async def inject_synonyms(
        self, tenant_id: str, knowledge_base_id: str, schema: dict
    ) -> dict:
        """[jonex] 运行时把 KB 级同义词组注入 compiled schema（不落库）。

        写入顶层 ``synonyms`` 与 ``prompt_schema.synonyms``（[{canonical, terms}]），
        供 atomic-rag 抽取 prompt 提示 LLM 统一使用 canonical 表述（效果 Y 的软提示层）。
        受开关 ONTOLOGY_SYNONYM_PROMPT_ENABLED（默认 true）控制；异常降级返回原 schema。
        """
        if not schema:
            return schema
        if os.getenv("ONTOLOGY_SYNONYM_PROMPT_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
            return schema
        try:
            from ..repository.ontology_synonym_repository import OntologySynonymRepository

            async with get_db_session() as session:
                groups = await OntologySynonymRepository(session).list_all_by_kb(
                    tenant_id, knowledge_base_id
                )
            syn = [
                {"canonical": (g.canonical or (g.terms or [""])[0]), "terms": g.terms or []}
                for g in groups
                if g.terms
            ]
            if not syn:
                return schema
            result = dict(schema)
            result["synonyms"] = syn
            prompt_schema = dict(result.get("prompt_schema") or {})
            prompt_schema["synonyms"] = syn
            result["prompt_schema"] = prompt_schema
            return result
        except Exception as e:
            logger.warning(
                "Synonym inject skipped (degraded): kb=%s err=%s", knowledge_base_id, e
            )
            return schema

    async def get_editor_state(
        self,
        tenant_id: str,
        knowledge_base_id: str,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            binding = await repo.get_binding(tenant_id, knowledge_base_id)
            schema = await repo.get_compiled_schema(tenant_id, knowledge_base_id)

            current_template = None
            if binding and binding.template_scenario_id:
                provider = TemplateSchemaProvider(session)
                scenario = await provider.load_scenario(tenant_id, binding.template_scenario_id)
                if scenario is not None:
                    current_template = {
                        "domain_id": scenario.domain_id,
                        "domain_name": scenario.domain_name,
                        "scenario_id": scenario.scenario_id,
                        "scenario_name": scenario.scenario_name,
                        "source_version": scenario.version,
                        "source_hash": scenario.structure_hash,
                    }

        return {
            "knowledge_base_id": knowledge_base_id,
            "binding": binding.to_dict() if binding else None,
            "compiled_schema": schema.to_dict() if schema else None,
            "current_template": current_template,
        }

    async def get_merged_schema(self, tenant_id: str, knowledge_base_id: str) -> Optional[dict]:
        """把该知识库下的**全部** schema 合成一份视图。

        检索侧（`_build_inverse_map`）要用这个。schema 粒度改成「按数据源」之后，
        仍按知识库级取一份会漏掉各数据源里声明的关系——inverse 映射会静默变小，
        而且是那种「不报错、只是答得差一点」的漏。

        合并规则：
          - entity_types / relation_types / constraints 按 `name` 去重求并集
            （先出现的胜出：知识库级在前，其后按数据源 id 排序，保证结果稳定）
          - disambiguation 取第一份非空的
        合并视图**不缓存**：它随任一数据源变化而失效，缓存收益不抵失效逻辑的复杂度。

        改造（抽象 schema v2）：KB 出现 active 抽象时，**抽象投影取代知识库级那份、
        且遗留的知识库级行不再并入**——否则它的同名实体会在「先到先得」里去压
        已生效的抽象内容；各数据源级行照旧并入。
        """
        tenant_id = require_tenant(tenant_id)
        projected = await self._abstract_projection(tenant_id, knowledge_base_id)
        async with get_db_session() as session:
            rows = await OntologySchemaRepository(session).list_compiled_schemas(tenant_id, knowledge_base_id)

        # 知识库级放最前，其余按数据源 id 排——顺序稳定，合并结果才可复现。
        # 排序必须落在分支**之前**（两个分支共用）：`list_compiled_schemas` 没有
        # ORDER BY，投影分支若不排，「先到先得」的胜者就取决于数据库的返回顺序。
        rows.sort(key=lambda r: (r.data_source_id is not None, r.data_source_id or ""))

        parts: list[dict] = []
        if projected is not None:
            # active 抽象存在：投影取代 KB 级行，且**遗留行不再并入**——
            # 否则它的同名实体会在「先到先得」里去压已生效的抽象内容。
            parts.append(projected)
            parts.extend(r.to_dict() for r in rows if r.data_source_id is not None)
        else:
            parts.extend(r.to_dict() for r in rows)

        if not parts:
            return None
        return merge_schema_parts(parts, tenant_id, knowledge_base_id)

    async def _abstract_projection(self, tenant_id: str, knowledge_base_id: str) -> Optional[dict]:
        """active 抽象 schema → compiled 形状投影；没有/未确认/空 → None。

        **不缓存**：抽象行一变（保存/确认）投影就变，而 KB 级缓存键没有跟着它
        失效的接线——现算省掉这条失效链（先例：合并视图也不缓存）。调用点有三处：
        `get_compiled_schema(ds=None)`、`get_merged_schema`，以及
        `save_compiled_schema` 的防洗行护栏（KB 级写入前先问一句「这个 KB 还有
        没有可写的知识库级 schema」）——都不在热路径。
        """
        async with get_db_session() as session:
            row = await OntologyAbstractSchemaRepository(session).get_by_kb(tenant_id, knowledge_base_id)
        if row is None or row.status != "active":
            return None
        schema = row.schema if isinstance(row.schema, dict) else None
        if not schema or not (schema.get("entities") or []):
            return None
        entity_types, relation_types = abstract_to_compiled_types(schema)
        return {
            "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id,
            "data_source_id": None,
            "schema_version": row.schema_version,
            "source_type": "abstract",
            "entity_types": entity_types,
            "relation_types": relation_types,
            "constraints": [],
            "disambiguation": {"case_insensitive": True, "alias_merge": True},
            "prompt_schema": self._build_prompt_schema(entity_types, relation_types),
        }

    async def save_compiled_schema(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        entity_types: list[dict],
        relation_types: list[dict],
        edited_by: Optional[str] = None,
        constraints: Optional[list[dict]] = None,
        expected_schema_version: Optional[int] = None,
        data_source_id: Optional[str] = None,
        _mirror_from_abstract: bool = False,
        _carry_enabled: bool = True,
    ) -> dict:
        """保存某一份编译 schema。

        `data_source_id=None` → 存知识库级那份；但该 KB 有 active 抽象 schema
        时直接 1003 拒绝（防洗行护栏，见下方注释）；
        给了 id → 存该数据源自己的那份。乐观锁是**按份**的
        （每份 schema 各有自己的 schema_version）。
        """
        tenant_id = require_tenant(tenant_id)

        # 编辑器保存必须携带版本，做乐观锁；缺失直接 400（避免无版本请求绕过并发保护）
        if expected_schema_version is None:
            raise InvalidParameterError(message=translate("err.ontology.missing_schema_version", fallback="缺少 expected_schema_version")  )  # 原消息)

        # ── 防洗行护栏（p12 §三.1 收口；决策 = 防洗护栏 + 15 条维持）──
        # KB 有 active 抽象 schema 时，`data_source_id IS NULL` 的读取已被投影
        # 接管（`get_compiled_schema(ds=None)` 返回 `source_type="abstract"` 的
        # 投影、`get_merged_schema` 也不再并入遗留行）——此时**没有任何可编辑的
        # 「知识库级 schema」**。但画布回落路径恰好会这么写：读到的正是投影、
        # 保存时不带 `data_source_id`。现状仅靠乐观锁「版本不等」碰巧挡下
        # （投影版本 = 抽象行版本，通常 ≠ 遗留行版本）；一旦两者巧合相等，
        # CAS 放行、投影内容就**整行覆写遗留行**（assets_r2 id=7，v15，含
        # 15 条 unique_attribute —— 对 ② 零价值但属人工数据，洗掉不可逆）。
        # 这里把「碰巧」变成「必然」：先于乐观锁与一切写入，语义拒绝。
        if data_source_id is None and not _mirror_from_abstract:
            projected = await self._abstract_projection(tenant_id, knowledge_base_id)
            if projected is not None:
                raise ResourceConflictError(
                    message=translate(
                        "err.ontology.kb_level_managed_by_abstract",
                        fallback="该知识库已启用抽象 schema，知识库级 schema 不可再直接保存；请到抽象 schema 编辑器中修改",
                    ),
                    details={
                        "knowledge_base_id": knowledge_base_id,
                        "reason": "abstract_schema_active",
                    },
                )

        normalized_entities = self._normalize_entity_types(entity_types)
        normalized_relations = self._normalize_relation_types(relation_types, normalized_entities)
        prompt_schema = self._build_prompt_schema(normalized_entities, normalized_relations)
        edited_at = datetime.now(timezone.utc)

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            binding = await repo.get_binding(tenant_id, knowledge_base_id)
            # 乐观锁与继承字段都按**份**取——数据源级那份不该继承知识库级的模板来源
            current = await repo.get_compiled_schema(tenant_id, knowledge_base_id, data_source_id)

            source_type = current.source_type if current else "manual"
            template_domain_id = current.template_domain_id if current else None
            template_scenario_id = current.template_scenario_id if current else None
            source_version = current.source_version if current else 1
            source_hash = current.source_hash if current else None
            disambiguation = current.disambiguation if current else {"case_insensitive": True, "alias_merge": True}

            # 约束三态：None=保留现有（顺带清理历史 stub）；[]=清空；非空=替换（校验目标存在性）
            if constraints is None:
                final_constraints = self._strip_legacy_stub(current.constraints if current else [])
            else:
                # 跨层防护（P1-4）：旧客户端（如画布）会把约束重建成不含
                # `enabled` 的固定 7 键对象；归一化的 fail-open 会把它当启用，
                # 一次与约束无关的保存就静默撤销人工 disable。先按 name 从
                # **本份**已存行补回（current 已为本函数读出，无额外读/写，
                # 乐观锁口径不变）；显式给了 enabled 的条目永远以入参为准。
                # `_carry_enabled=False`（内部写路径：版本恢复）关闭继承 ——
                # 恢复载荷是**权威内容**，不得被当前行「补」出新键，否则
                # 恢复后内容指纹与版本 snapshot_hash 对不上（SnapshotRestoreError）。
                incoming = constraints
                if _carry_enabled:
                    incoming = self._carry_over_constraint_enabled(
                        constraints, current.constraints if current else None)
                final_constraints = self._normalize_constraints(
                    incoming, normalized_entities, normalized_relations
                )

            if binding and binding.template_scenario_id:
                source_type = binding.source_type or source_type
                if not current or not source_hash:
                    provider = TemplateSchemaProvider(session)
                    scenario = await provider.load_scenario(tenant_id, binding.template_scenario_id)
                    if scenario is not None:
                        template_domain_id = scenario.domain_id
                        template_scenario_id = scenario.scenario_id
                        source_version = scenario.version
                        source_hash = scenario.structure_hash

            # 抽象镜像不是模板派生，也不该被当成人工编辑：
            # - source_type 让状态页/后续维护能认出这份行的来源；
            # - schema_mode 不能是 manual_edited（否则 freshness 会报 manual_edited，
            #   而这份内容其实是抽象层确认后的派生结果）。
            if _mirror_from_abstract:
                source_type = "abstract_mirror"

            compiled = await repo.upsert_compiled_schema(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                data_source_id=data_source_id,
                template_domain_id=template_domain_id,
                template_scenario_id=template_scenario_id,
                source_type=source_type,
                source_version=source_version,
                source_hash=source_hash,
                entity_types=normalized_entities,
                relation_types=normalized_relations,
                constraints=final_constraints,
                disambiguation=disambiguation or {"case_insensitive": True, "alias_merge": True},
                prompt_schema=prompt_schema,
                schema_mode=("abstract_mirror" if _mirror_from_abstract else "manual_edited"),
                sync_status="synced",
                edited_at=edited_at,
                edited_by=edited_by,
                expected_version=expected_schema_version,
            )
            await session.commit()

        await self._set_cache(tenant_id, knowledge_base_id, compiled.to_dict(), data_source_id)
        # 数据源级 schema 被人工编辑后，图里的 `DataSourceEntity` 子图（表/列）
        # 还是旧形状 —— 投影只在编译/导入等流程触发，直连本方法的编辑入口会漏。
        # best-effort：投影失败不回滚已提交的 schema（权威是 PG）。
        if data_source_id:
            from .ontology_data_projection import project_source_best_effort
            await project_source_best_effort(
                tenant_id, knowledge_base_id, str(data_source_id))
        return compiled.to_dict()

    async def mirror_from_abstract(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        abstract_schema: dict,
        *,
        edited_by: Optional[str] = None,
    ) -> dict:
        """把 active 抽象 schema 镜像写回 KB 级 compiled schema（P2）。

        唯一合法的「抽象 → KB 级」写入方向：防洗行护栏在
        `_mirror_from_abstract=True` 下放行；其它 KB 级写入仍被拒绝。
        约束沿用原 KB 级行，但只保留目标仍存在的（见 `_filter_mirror_constraints`）。
        指标（metrics）不在 compiled schema 形状里，天然不写。
        """
        tenant_id = require_tenant(tenant_id)
        entity_types, relation_types = abstract_to_compiled_types(abstract_schema or {})
        async with get_db_session() as session:
            current = await OntologySchemaRepository(session).get_compiled_schema(
                tenant_id, knowledge_base_id,
            )
            constraints = _filter_mirror_constraints(
                getattr(current, "constraints", None), entity_types, relation_types,
            )
            expected_version = int(getattr(current, "schema_version", 0) or 0)

        result = await self.save_compiled_schema(
            tenant_id,
            knowledge_base_id,
            entity_types,
            relation_types,
            edited_by=edited_by,
            constraints=constraints,
            expected_schema_version=expected_version,
            data_source_id=None,
            _mirror_from_abstract=True,
        )
        logger.info(
            "抽象 schema 已镜像回 KB 级 compiled schema: kb=%s entities=%d rels=%d v=%s",
            knowledge_base_id, len(entity_types), len(relation_types),
            result.get("schema_version"),
        )
        return result

    async def set_evidence(
        self, tenant_id: str, knowledge_base_id: str, data_source_id: Optional[str], evidence: dict,
    ) -> bool:
        """写「生成期探测」的证据。**转发给仓储**。

        ⚠️ 这层转发不是装饰：`generate_schema` 手里的 `compiler` 是**能力层传进来的 `OntologyCompiler`**
        （`capability.py` 里 `compiler = self.service.compiler`），而 `set_evidence` 只定义在
        `OntologySchemaRepository` 上 —— 不转发的话生产链路上会 `AttributeError`，
        再被 `generate_schema` 那段宽 `except` 吞成一行 warning：
        **付了探测的全部成本（真连库、逐表采样、最长 300s），一条证据都没落库**（评审查出的 Critical）。
        （`get_db_session()` 退出时自动 commit，这里不需要显式提交。）
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            return await OntologySchemaRepository(session).set_evidence(
                tenant_id, knowledge_base_id, data_source_id, evidence,
            )

    async def export_compiled_schema_yaml(
        self,
        knowledge_base_id: str,
        tenant_id: str,
    ) -> dict:
        """[jonex] Export compiled schema as YAML text.

        Reads the current compiled schema from DB, maps entity_types/relation_types/constraints
        to ontology_yaml data structures, strips internal tracking fields
        (source_object_id, source_attribute_id, source_relation_id), and serializes to YAML.

        Returns:
            dict with keys: filename, yaml_text, warnings
        """
        from jonex_core.common.ontology_yaml import (
            dump_yaml, OntologyYamlDocument, YamlAttribute,
            YamlConstraint, YamlEntity, YamlRelation, to_yaml_attr_type,
            ONTOLOGY_YAML_FORMAT_VERSION,
        )

        tenant_id = require_tenant(tenant_id)

        schema = await self.get_compiled_schema(tenant_id, knowledge_base_id, auto_compile=False)
        if not schema:
            raise ResourceNotFoundError(
                message=translate(
                    "err.ontology.compiled_schema_not_found",
                    fallback=f"知识库 {knowledge_base_id} 的编译 schema 不存在，请先绑定模板或初始化 schema",
                ),
            )

        warnings: list[str] = []

        # [jonex] Map entity_types → YamlEntity, strip source_object_id and source_attribute_id
        entities: list[YamlEntity] = []
        for et in schema.get("entity_types", []):
            attrs = []
            for attr in et.get("attributes", []):
                db_type = (attr.get("type") or "string").strip()
                yaml_type = to_yaml_attr_type(db_type)
                if yaml_type != db_type:
                    warnings.append(
                        f"entity {et['name']}.{attr['name']}: "
                        f"attr_type={db_type} → YAML type={yaml_type}"
                    )
                attrs.append(YamlAttribute(
                    name=attr["name"],
                    display_name=attr.get("display_name"),
                    description=attr.get("description"),
                    type=yaml_type,
                    required=bool(attr.get("required", False)),
                    is_primary_key=bool(attr.get("is_primary_key", False)),
                ))
            entities.append(YamlEntity(
                name=et["name"],
                display_name=et.get("display_name"),
                description=et.get("description"),
                aliases=et.get("aliases", []),
                attributes=attrs,
                # TBox 构建技能声明：漏传会让「导出 → 导入」往返清空库里的技能链
                extraction_skills=et.get("extraction_skills") or None,
            ))

        # [jonex] Map relation_types → YamlRelation, strip source_relation_id
        relations: list[YamlRelation] = []
        for rt in schema.get("relation_types", []):
            relations.append(YamlRelation(
                name=rt["name"],
                source=rt["source"],
                target=rt["target"],
                display_name=rt.get("display_name"),
                description=rt.get("description"),
                aliases=rt.get("aliases", []),
                cardinality=rt.get("cardinality", "custom"),
                inverse_of=rt.get("inverse_of") or None,
                source_types=list(rt.get("source_types") or []),
                target_types=list(rt.get("target_types") or []),
            ))

        # [jonex] Map constraints → YamlConstraint, decompose target_code.
        # 跳过无法分解出 entity/attribute/relation 的约束（纯元数据，无 YAML 语义）。
        constraints: list[YamlConstraint] = []
        for ct in schema.get("constraints", []):
            target_type = ct.get("target_type", "")
            target_code = ct.get("target_code", "")
            if not target_type or not target_code:
                continue
            entity = None
            attribute = None
            relation = None
            if target_type == "entity":
                entity = target_code
            elif target_type == "attribute":
                parts = target_code.split(".", 1)
                entity = parts[0] if parts else None
                attribute = parts[1] if len(parts) > 1 else None
            elif target_type == "relation":
                relation = target_code
            else:
                continue  # unknown target_type, skip

            constraints.append(YamlConstraint(
                type=ct.get("constraint_type", "custom"),
                entity=entity,
                attribute=attribute,
                relation=relation,
                expression=ct.get("expression"),
                suggestion=ct.get("suggestion"),
            ))

        disambiguation = schema.get("disambiguation") or {"case_insensitive": True, "alias_merge": True}
        domain = schema.get("template_domain_id") or schema.get("source_type") or "default"

        doc = OntologyYamlDocument(
            # YAML 格式版本固定为 1；schema["schema_version"] 是乐观锁版本（每次编辑自增），
            # 写进 version 会让 ontology builder 等消费方按 "Schema version 1" 校验时整份拒绝。
            version=ONTOLOGY_YAML_FORMAT_VERSION,
            domain=domain,
            entity_types=entities,
            relation_types=relations,
            constraints=constraints,
            disambiguation=disambiguation,
        )

        yaml_text = dump_yaml(doc)
        filename = f"{knowledge_base_id}_ontology_schema.yaml"

        return {
            "filename": filename,
            "yaml_text": yaml_text,
            "warnings": warnings,
        }

    async def import_compiled_schema_yaml(
        self,
        knowledge_base_id: str,
        tenant_id: str,
        yaml_text: str,
        expected_schema_version: int,
        dry_run: bool = True,
    ) -> dict:
        """[jonex] Import compiled schema from YAML text.

        Parses YAML, converts to compiled schema DTOs, and either returns a summary
        (dry_run=True) or persists via save_compiled_schema (dry_run=False).

        Returns:
            dict with keys: dry_run, summary, warnings, errors
        """
        from jonex_core.common.ontology_yaml import parse_yaml

        tenant_id = require_tenant(tenant_id)

        # 1. Parse YAML
        doc, validation = parse_yaml(yaml_text)
        if doc is None:
            return {
                "dry_run": dry_run,
                "summary": None,
                "warnings": validation.warnings,
                "errors": validation.errors,
            }

        warnings = list(validation.warnings)
        errors = list(validation.errors)

        # [jonex] Check raw YAML for constraints key presence (distinguishes
        # "no constraints section" → keep existing vs "empty constraints" → clear)
        import yaml as _yaml_parser
        raw_yaml = _yaml_parser.safe_load(yaml_text)
        has_constraints_key = isinstance(raw_yaml, dict) and "constraints" in raw_yaml

        # [jonex] Build entity index for constraint target_label resolution
        entity_index: dict[str, dict] = {}
        for ye in doc.entity_types:
            entity_index[ye.name] = {
                "display_name": ye.display_name,
                "attributes": {a.name: a.display_name for a in ye.attributes},
            }

        # 2. Convert entities
        entity_types: list[dict] = []
        for ye in doc.entity_types:
            attrs = []
            for ya in ye.attributes:
                attrs.append({
                    "name": ya.name,
                    "display_name": ya.display_name or ya.name,
                    "description": ya.description or "",
                    "type": ya.type,
                    "required": ya.required,
                    "is_primary_key": ya.is_primary_key,
                    "source_attribute_id": None,  # [jonex] strip tracking field
                })
            entity_types.append({
                "name": ye.name,
                "display_name": ye.display_name or ye.name,
                "description": ye.description or "",
                "requirement": "",  # [jonex] default
                "status": "active",  # [jonex] default
                "aliases": ye.aliases,
                "source_object_id": None,  # [jonex] strip tracking field
                "attributes": attrs,
                # 整份交给 save_compiled_schema 落库：不带就会把库里已有的技能链清空
                "extraction_skills": ye.extraction_skills,
            })

        # 3. Convert relations
        relation_types: list[dict] = []
        for yr in doc.relation_types:
            relation_types.append({
                "name": yr.name,
                "display_name": yr.display_name or yr.name,
                "description": yr.description or "",
                "status": "active",  # [jonex] default
                "aliases": yr.aliases,
                "source": yr.source,
                "target": yr.target,
                "source_relation_id": None,  # [jonex] strip tracking field
                "cardinality": yr.cardinality,
                # ⚠️ 必须显式写进来。`parse_yaml` 已把 inverse_of 解析到
                # `yr.inverse_of`，但这里若不带上，下游 `_normalize_relation_types`
                # 的 `item.get("inverse_of")` 只会读到 None——
                # 于是导入的 TBox 里每条关系的 inverse_of 都成 null，
                # 检索侧 `_build_inverse_map` 建出空映射，inverse 语义翻译静默失效。
                # （`_normalize_relation_types` 本身就带 inverse_of 的互指校验，
                #  说明这个键是它预期收到的——只是导入这条路上漏传了。）
                "inverse_of": yr.inverse_of,
                # 同上：inverse_of / 多态端点不带就会在落库时被清空
                "inverse_of": yr.inverse_of,
                "source_types": list(yr.source_types or []),
                "target_types": list(yr.target_types or []),
            })

        # 4. Convert constraints
        constraints: list[dict] = []
        for yc in doc.constraints:
            target_type, target_code = _yaml_constraint_target(yc)
            if not target_type or not target_code:
                warnings.append(f"跳过无法解析 target 的约束: type={yc.type}")
                continue
            target_label = _resolve_constraint_label(yc, entity_index, target_type, target_code)
            constraint_name = _derive_constraint_name(yc, target_code)
            constraints.append({
                "name": constraint_name,
                "target_type": target_type,
                "target_code": target_code,
                "target_label": target_label,
                "constraint_type": yc.type,
                "expression": yc.expression or "",
                "suggestion": yc.suggestion or "",
            })

        # [jonex] Constraint handling: no section → keep existing; section present → replace
        final_constraints: Optional[list[dict]] = constraints if has_constraints_key else None

        # Build summary
        if final_constraints is None:
            constraint_mode = "keep_existing"
        elif not constraints:
            constraint_mode = "clear"
        else:
            constraint_mode = "replace"

        summary = {
            "entity_types_count": len(entity_types),
            "relation_types_count": len(relation_types),
            "constraints_count": len(constraints),
            "constraints_mode": constraint_mode,
            "schema_version": doc.version,
            "domain": doc.domain,
        }

        if dry_run:
            return {
                "dry_run": True,
                "summary": summary,
                "warnings": warnings,
                "errors": errors,
            }

        # 5. Persist via existing save_compiled_schema
        result = await self.save_compiled_schema(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            entity_types=entity_types,
            relation_types=relation_types,
            constraints=final_constraints,
            expected_schema_version=expected_schema_version,
            edited_by=None,
        )

        return {
            "dry_run": False,
            "summary": summary,
            "warnings": warnings,
            "errors": errors,
            "schema_version": result.get("schema_version"),
        }

    async def reseed_from_template(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        template_scenario_id: str,
        template_domain_id: Optional[str] = None,
        source_type: str = "business_template",
        apply_to_documents: bool = False,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            await repo.upsert_binding(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                template_domain_id=template_domain_id,
                template_scenario_id=template_scenario_id,
                source_type=source_type,
            )
            await session.commit()

        # reseed 是丢弃人工编辑的唯一入口：强制重编译（force=True）
        compiled = await self.compile_for_knowledge_base(tenant_id, knowledge_base_id, force=True)

        # [jonex] 阶段2：可选顺带把该 KB 文档置 PENDING（对账被动按新 schema 重抽）
        if apply_to_documents:
            try:
                from ..repository import KnowledgeDocumentRepository

                async with get_db_session() as session:
                    doc_repo = KnowledgeDocumentRepository(session)
                    stats = await doc_repo.reset_ontology_for_kb(
                        tenant_id, knowledge_base_id,
                        target_schema_version=compiled.get("schema_version"),
                    )
                    await session.commit()
                compiled = {
                    **compiled,
                    "documents_matched": stats.get("matched", 0),
                    "documents_queued": stats.get("reset", 0),
                }
            except Exception as e:
                logger.warning("reseed apply_to_documents failed kb=%s: %s", knowledge_base_id, e)
                compiled = {**compiled, "apply_error": str(e)[:500]}

        return compiled

    async def list_impacted_knowledge_bases(
        self,
        tenant_id: str,
        template_scenario_id: str,
    ) -> list[dict]:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            bindings = await repo.list_by_template_scenario(tenant_id, template_scenario_id)
            items = []
            for binding in bindings:
                schema = await repo.get_compiled_schema(tenant_id, binding.knowledge_base_id)
                items.append({
                    "tenant_id": binding.tenant_id,
                    "knowledge_base_id": binding.knowledge_base_id,
                    "schema_outdated": schema is None or schema.sync_status == "outdated",
                })
            return items

    async def react_to_publish(self, tenant_id: str, template_scenario_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            bindings = await repo.list_by_template_scenario(tenant_id, template_scenario_id)
            outdated = []
            for binding in bindings:
                changed = await repo.mark_schema_outdated(tenant_id, binding.knowledge_base_id)
                if changed:
                    outdated.append(binding.knowledge_base_id)
            await session.commit()

        for knowledge_base_id in outdated:
            await self.invalidate_cache(tenant_id, knowledge_base_id)

        logger.info(
            "react_to_publish scenario=%s marked %d KB schemas outdated",
            template_scenario_id,
            len(outdated),
        )
        return {"outdated_kbs": outdated, "recompiled_kbs": [], "reset_documents": 0}

    def _compile_from_scenario(
        self,
        scenario: ScenarioDef,
        source_type: str = "business_template",
    ) -> dict:
        entity_types: list[dict] = []
        for obj in scenario.objects:
            code = obj.ontology_code or self._slugify(obj.name)
            entity_types.append({
                "name": code,
                "display_name": obj.name,
                "description": obj.description or "",
                "requirement": "",
                "status": _normalize_editor_status(obj.status),
                "aliases": obj.aliases or [],
                "source_object_id": obj.id,
                "attributes": [
                    {
                        "name": attr.ontology_code or self._slugify(attr.attr_name),
                        "display_name": attr.attr_name,
                        "description": attr.description or "",
                        "type": attr.attr_type,
                        "required": attr.is_required,
                        "is_primary_key": bool(attr.is_primary_key),
                        "source_attribute_id": attr.id,
                    }
                    for attr in obj.attributes
                ],
            })

        relation_types: list[dict] = []
        for rel in scenario.relations:
            source_name = self._resolve_entity_code(rel.source_object_id, scenario.objects)
            target_name = self._resolve_entity_code(rel.target_object_id, scenario.objects)
            if not source_name or not target_name:
                logger.warning("Skip relation %s due to unresolved endpoints", rel.id)
                continue
            relation_types.append({
                "name": rel.ontology_code or self._slugify(rel.name),
                "display_name": rel.name,
                "description": rel.description or "",
                "status": _normalize_editor_status(rel.status),
                "aliases": rel.aliases or [],
                "source": source_name,
                "target": target_name,
                "source_relation_id": rel.id,
                "cardinality": _normalize_cardinality(rel.relation_type),
                # [jonex] P1：模板场景侧无 inverse_of/多态端点概念，显式缺省（与 yaml 路径结构一致）
                "inverse_of": None,
                "source_types": [],
                "target_types": [],
            })

        return {
            "template_domain_id": scenario.domain_id,
            "template_scenario_id": scenario.scenario_id,
            "source_type": source_type,
            "source_version": scenario.version,
            "source_hash": scenario.structure_hash,
            "entity_types": entity_types,
            "relation_types": relation_types,
            "constraints": [],
            "disambiguation": {"case_insensitive": True, "alias_merge": True},
            "prompt_schema": self._build_prompt_schema(entity_types, relation_types),
            "compiled_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _compile_from_yaml_fallback(self) -> dict:
        try:
            from jonex_core.capability.atomic.ontology.registry import OntologyRegistry

            registry = OntologyRegistry()
            schema = registry.load(ONTOLOGY_SCHEMA_FALLBACK_PATH)

            entity_types = [
                {
                    "name": entity_type.name,
                    "display_name": entity_type.name,
                    "description": "",
                    "requirement": "",
                    "status": "active",
                    "aliases": entity_type.aliases,
                    "source_object_id": None,
                    "extraction_skills": entity_type.extraction_skills,
                    "attributes": [
                        {
                            "name": attr.name,
                            "display_name": attr.name,
                            # [jonex] P0-fix：透传 YAML 语义说明（此前硬编码 ""，
                            # Redis 编译档属性说明全空 → 抽取 prompt 属性表
                            # 只剩属性名，LLM 不填 attributes）。
                            "description": attr.description or "",
                            "type": attr.type,
                            "required": attr.required,
                            "is_primary_key": False,
                            "source_attribute_id": None,
                        }
                        for attr in entity_type.attributes
                    ],
                }
                for entity_type in schema.entity_types
            ]
            relation_types = [
                {
                    "name": relation_type.name,
                    "display_name": relation_type.name,
                    # [jonex] P0-fix：透传 YAML 语义说明（此前硬编码 ""，
                    # _writeback_custom_kg 的 rel_desc 读完整 compiled 档 →
                    # LightRAG 图里 TBox 内关系描述恒空）
                    "description": relation_type.description or "",
                    "status": "active",
                    "aliases": [],
                    "source": relation_type.source,
                    "target": relation_type.target,
                    "source_relation_id": None,
                    "cardinality": relation_type.cardinality or "custom",
                    "inverse_of": relation_type.inverse_of,
                    "source_types": list(relation_type.source_types or []),
                    "target_types": list(relation_type.target_types or []),
                }
                for relation_type in schema.relation_types
            ]

            return {
                "template_domain_id": None,
                "template_scenario_id": None,
                "source_type": "yaml_default",
                "source_version": 1,
                "source_hash": None,
                "entity_types": entity_types,
                "relation_types": relation_types,
                "constraints": [],
                "disambiguation": {"case_insensitive": True, "alias_merge": True},
                # [jonex] P10 批 2.9：无条件走 _build_prompt_schema——旧实现
                # `prompt or ...` 被 to_prompt_json 非空输出短路，而
                # to_prompt_json 不含 extraction_skills（P9 遗留 bug），
                # yaml_default KB 的 compiled 档 prompt_schema 恒缺技能声明
                # → NativeOntologyExtractor 的技能链 decl_map 恒空、静默跳过
                # （Redis 实测确认）。_build_prompt_schema 字段集是
                # to_prompt_json 的超集（+extraction_skills），prompt 渲染
                # 只取 name 列表，行为零变化。
                "prompt_schema": self._build_prompt_schema(entity_types, relation_types),
                "compiled_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.error("YAML fallback compile failed: %s", e)
            raise

    def _normalize_entity_types(self, entity_types: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        names_seen: set[str] = set()
        display_names_seen: set[str] = set()

        for index, item in enumerate(entity_types, start=1):
            name = self._require_name(item.get("name"), f"entity_types[{index}].name")
            if name in names_seen:
                raise InvalidParameterError(message=translate("err.ontology.entity_code_duplicate", params={"name": name}, fallback=f"实体编码重复: {name}")  )  # 原消息)
            names_seen.add(name)

            # 显示名（对象名称）在同 KB 内唯一，与 name 编码唯一性分开校验
            display_name = (item.get("display_name") or name).strip()
            if display_name in display_names_seen:
                raise InvalidParameterError(message=translate("err.ontology.entity_name_duplicate", params={"name": display_name}, fallback=f"对象名称重复: {display_name}"))
            display_names_seen.add(display_name)

            attr_names_seen: set[str] = set()
            attributes = []
            for attr_index, attr in enumerate(item.get("attributes") or [], start=1):
                attr_name = self._require_name(attr.get("name"), f"entity_types[{index}].attributes[{attr_index}].name")
                if attr_name in attr_names_seen:
                    raise InvalidParameterError(message=translate("err.ontology.attr_code_duplicate", params={"name": name, "attr_name": attr_name}, fallback=f"实体 {name} 下属性编码重复: {attr_name}")  )  # 原消息)
                attr_names_seen.add(attr_name)
                attr_type = (attr.get("type") or "string").strip().lower()
                if attr_type not in VALID_ATTR_TYPES:
                    raise InvalidParameterError(message=translate("err.ontology.invalid_attr_type", params={"name": name, "attr_name": attr_name, "attr_type": attr_type}, fallback=f"属性类型不合法: {name}.{attr_name}={attr_type}")  )  # 原消息)
                attributes.append({
                    "name": attr_name,
                    "display_name": (attr.get("display_name") or attr_name).strip(),
                    "description": (attr.get("description") or "").strip(),
                    "type": attr_type,
                    "required": bool(attr.get("required")),
                    "is_primary_key": bool(attr.get("is_primary_key")),
                    "source_attribute_id": attr.get("source_attribute_id"),
                    # 物理列名（catalog 原名，可能带空格/大写）。属性名是归一后的 slug，
                    # ② NL→SQL 生成 SQL 时必须用物理名 —— 白名单丢了就会拿 slug 去查
                    # 一张没有该列的表（2026-10-23 BIRD 实测）。
                    **({"physical_name": str(attr["physical_name"])}
                       if attr.get("physical_name") else {}),
                })

            normalized.append({
                "name": name,
                "display_name": display_name,
                "description": (item.get("description") or "").strip(),
                "requirement": (item.get("requirement") or "").strip(),
                "status": _normalize_editor_status(item.get("status")),
                "aliases": _dedupe_strings(item.get("aliases") or []),
                "source_object_id": item.get("source_object_id"),
                "attributes": attributes,
                # [jonex] P1：TBox 构建技能声明透传（否则保存路径静默丢弃）
                "extraction_skills": item.get("extraction_skills"),
            })

        return normalized

    def _normalize_relation_types(self, relation_types: list[dict], entity_types: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        names_seen: set[str] = set()
        entity_names = {item["name"] for item in entity_types}

        # [jonex] P1（M1）：预处理收集全部 relation name，供 inverse_of 冲突/互指校验
        all_names: set[str] = set()
        for index, item in enumerate(relation_types, start=1):
            all_names.add(self._require_name(item.get("name"), f"relation_types[{index}].name"))

        # name → inverse_of 映射（互指检测用）
        inverse_by_name: dict[str, str] = {}
        for item in relation_types:
            raw_inverse = (item.get("inverse_of") or "").strip()
            item_name = (item.get("name") or "").strip()
            if item_name and raw_inverse:
                inverse_by_name[item_name] = raw_inverse

        for index, item in enumerate(relation_types, start=1):
            name = self._require_name(item.get("name"), f"relation_types[{index}].name")
            if name in names_seen:
                raise InvalidParameterError(message=translate("err.ontology.relation_code_duplicate", params={"name": name}, fallback=f"关系编码重复: {name}")  )  # 原消息)
            names_seen.add(name)

            source = self._require_name(item.get("source"), f"relation_types[{index}].source")
            target = self._require_name(item.get("target"), f"relation_types[{index}].target")
            if source not in entity_names:
                raise InvalidParameterError(message=translate("err.ontology.relation_source_missing", params={"name": name, "source": source}, fallback=f"关系 {name} 的 source 不存在: {source}")  )  # 原消息)
            if target not in entity_names:
                raise InvalidParameterError(message=translate("err.ontology.relation_target_missing", params={"name": name, "target": target}, fallback=f"关系 {name} 的 target 不存在: {target}")  )  # 原消息)

            cardinality = _normalize_cardinality(item.get("cardinality") or "custom")
            if cardinality not in VALID_CARDINALITY:
                raise InvalidParameterError(message=translate("err.ontology.invalid_cardinality", params={"name": name, "cardinality": cardinality}, fallback=f"关系基数不合法: {name}={cardinality}")  )  # 原消息)

            # [jonex] P1（M1）：inverse_of / source_types / target_types 校验
            inverse_of = (item.get("inverse_of") or "").strip() or None
            if inverse_of:
                if inverse_of == name:
                    raise InvalidParameterError(message=translate("err.ontology.inverse_of_self", params={"name": name}, fallback=f"关系 {name} 的 inverse_of 不能指向自身")  )  # 原消息)
                # 互指（A.inverse_of=B 且 B.inverse_of=A）→ 更明确的报错优先于冲突
                if inverse_of in all_names and inverse_by_name.get(inverse_of) == name:
                    raise InvalidParameterError(message=translate("err.ontology.inverse_of_mutual", params={"name": name, "inverse_of": inverse_of}, fallback=f"关系 {name} 与 {inverse_of} 互指 inverse_of，只允许单侧声明")  )  # 原消息)
                # 逆名是虚拟名，不得与任何 relation_types[].name 冲突
                if inverse_of in all_names:
                    raise InvalidParameterError(message=translate("err.ontology.inverse_of_conflict", params={"name": name, "inverse_of": inverse_of}, fallback=f"关系 {name} 的逆名 {inverse_of} 与已有关系名冲突")  )  # 原消息)

            source_types = _dedupe_strings(item.get("source_types") or [])
            target_types = _dedupe_strings(item.get("target_types") or [])
            for type_name in source_types:
                if type_name not in entity_names:
                    raise InvalidParameterError(message=translate("err.ontology.source_type_missing", params={"name": name, "type": type_name}, fallback=f"关系 {name} 的 source_types 越界: {type_name}")  )  # 原消息)
            for type_name in target_types:
                if type_name not in entity_names:
                    raise InvalidParameterError(message=translate("err.ontology.target_type_missing", params={"name": name, "type": type_name}, fallback=f"关系 {name} 的 target_types 越界: {type_name}")  )  # 原消息)
            # 数组是单值超集，须包含 source/target 单值
            if source_types and source not in source_types:
                raise InvalidParameterError(message=translate("err.ontology.source_types_missing_single", params={"name": name, "source": source}, fallback=f"关系 {name} 的 source_types 必须包含 source 单值: {source}")  )  # 原消息)
            if target_types and target not in target_types:
                raise InvalidParameterError(message=translate("err.ontology.target_types_missing_single", params={"name": name, "target": target}, fallback=f"关系 {name} 的 target_types 必须包含 target 单值: {target}")  )  # 原消息)

            normalized.append({
                "name": name,
                "display_name": (item.get("display_name") or name).strip(),
                "description": (item.get("description") or "").strip(),
                "status": _normalize_editor_status(item.get("status")),
                "aliases": _dedupe_strings(item.get("aliases") or []),
                "source": source,
                "target": target,
                "source_relation_id": item.get("source_relation_id"),
                "cardinality": cardinality,
                "inverse_of": inverse_of,
                "source_types": source_types,
                "target_types": target_types,
                # join 键（P0-2）：生成器算出来的两个端点列 + 来源档。
                # **缺省是 None** —— 存量行、YAML 导入、旧版前端保存都走这条，
                # 不能因为它们没带就让整个保存失败。
                "source_column": (item.get("source_column") or None),
                "target_column": (item.get("target_column") or None),
                "origin": (item.get("origin") or None),
            })

        return normalized

    @staticmethod
    def _strip_legacy_stub(constraints: Optional[list[dict]]) -> list[dict]:
        """过滤掉无 target_code 的历史 stub 约束（如 {"type":"entity","severity":"warning"}）。

        用于 constraints=None（保留）路径，避免旧 stub 永久驻留。
        """
        result: list[dict] = []
        for item in constraints or []:
            if isinstance(item, dict) and (item.get("target_code") or "").strip():
                result.append(item)
        return result

    @staticmethod
    def _carry_over_constraint_enabled(
        constraints: list[dict],
        stored: list[dict] | None,
    ) -> list[dict]:
        """入参缺 `enabled` 时，按 name 继承**已存行**的启用状态。

        P1-4 的跨层防护：画布等旧客户端把约束重建成固定 7 键对象（没有
        `enabled`），`_normalize_constraints` 的 fail-open（缺字段 = 启用）
        会让一次与约束无关的保存把人工 `disable` 静默翻回启用。这里在
        归一化前按约束 `name` 从同 (kb, ds) 的已存行补回：

        - 入参显式带 `enabled` → 原样保留（入参永远赢）；
        - 存储行有该 name 且带 `enabled` → 复制其布尔语义（只有显式 False
          是停用，与读取侧同口径）；
        - 新 name / 存储行本身缺键 / 没有存储行 → 不带键，交由归一化默认
          True（与今天逐字节相同的行为）。

        纯函数：不读库、不改入参。
        """
        if not constraints:
            return constraints
        by_name = {
            str(item.get("name")): item
            for item in stored or []
            if isinstance(item, dict) and item.get("name")
        }
        out: list[dict] = []
        for item in constraints:
            if not isinstance(item, dict) or "enabled" in item:
                out.append(item)
                continue
            old = by_name.get(str(item.get("name")))
            if isinstance(old, dict) and "enabled" in old:
                out.append({**item, "enabled": old.get("enabled") is not False})
            else:
                out.append(item)
        return out

    def _normalize_constraints(
        self,
        constraints: list[dict],
        entity_types: list[dict],
        relation_types: list[dict],
    ) -> list[dict]:
        """规整并校验约束：name 唯一、target_type 合法、target_code 必须存在于本次提交集合。"""
        entity_codes = {item["name"] for item in entity_types}
        attribute_codes = {
            f'{item["name"]}.{attr["name"]}'
            for item in entity_types
            for attr in item.get("attributes", [])
        }
        relation_codes = {item["name"] for item in relation_types}

        normalized: list[dict] = []
        names_seen: set[str] = set()

        for index, item in enumerate(constraints, start=1):
            name = self._require_name(item.get("name"), f"constraints[{index}].name")
            if name in names_seen:
                raise InvalidParameterError(message=translate("err.ontology.constraint_name_duplicate", params={"name": name}, fallback=f"约束名称重复: {name}")  )  # 原消息)
            names_seen.add(name)

            target_type = (item.get("target_type") or "").strip().lower()
            if target_type not in {"entity", "attribute", "relation"}:
                raise InvalidParameterError(message=translate("err.ontology.invalid_constraint_target", params={"name": name, "target_type": target_type}, fallback=f"约束目标类型不合法: {name}={target_type}")  )  # 原消息)

            target_code = self._require_name(
                item.get("target_code"), f"constraints[{index}].target_code"
            )
            valid_targets = {
                "entity": entity_codes,
                "attribute": attribute_codes,
                "relation": relation_codes,
            }[target_type]
            if target_code not in valid_targets:
                raise InvalidParameterError(
                    message=translate("err.ontology.constraint_target_missing", params={"target_code": target_code}, fallback=f"约束目标不存在: {target_code}")  ,  # 原消息
                    details={"name": name, "target_type": target_type},
                )

            row = {
                "name": name,
                "target_type": target_type,
                "target_code": target_code,
                "target_label": (item.get("target_label") or "").strip() or None,
                "constraint_type": (item.get("constraint_type") or "custom").strip(),
                "expression": (item.get("expression") or "").strip(),
                "suggestion": (item.get("suggestion") or "").strip(),
            }
            # 停用开关（R1）：**只有显式 False 是停用**——缺字段 / "false" / 其它
            # 一律启用（fail-open）。这里只在入参**本来带键**时才写回：存量约束
            # 是 7 键（没有 enabled），若无条件补键，任何一次保存都会改变内容
            # 指纹 —— 老版本快照的 hash 对不上（restore_snapshot 拒绝回滚）、
            # P1-1 编译漂移首次保存误报。缺键的语义继续由读取侧 fail-open 承担。
            if "enabled" in item:
                row["enabled"] = item.get("enabled") is not False
            normalized.append(row)

        return normalized

    def _build_prompt_schema(self, entity_types: list[dict], relation_types: list[dict]) -> dict:
        # [jonex] P1（批 1 第一项）：attribute description 与 relation description/cardinality
        # 进 prompt——此前 LLM 看不到任何属性语义与基数约束（skill-action §2 前置说明）
        # [jonex] P9（修复 P7 遗留）：extraction_skills 必须透传进 prompt_schema——
        # task_manager 把 prompt_schema 作为 tbox 传给 NativeOntologyExtractor.extract()，
        # 技能链执行器按 schema.entity_types[].extraction_skills 构建声明链，
        # 不透传则 decl_map 恒空、技能链静默跳过（e2e 观测 P9 技能链日志缺失时发现）。
        # 只增加字段：LLM prompt 渲染仅取 name 列表，不改变 prompt 文本。
        return {
            "entity_types": [
                {
                    "name": entity_type["name"],
                    "aliases": entity_type.get("aliases", []),
                    "extraction_skills": entity_type.get("extraction_skills"),
                    "attributes": [
                        {
                            "name": attr["name"],
                            "type": attr["type"],
                            "required": attr["required"],
                            "description": attr.get("description") or "",
                        }
                        for attr in entity_type.get("attributes", [])
                    ],
                }
                for entity_type in entity_types
            ],
            "relation_types": [
                {
                    "name": relation_type["name"],
                    "source": relation_type["source"],
                    "target": relation_type["target"],
                    "description": relation_type.get("description") or "",
                    "cardinality": relation_type.get("cardinality") or "custom",
                    # [jonex] P0-fix：补齐 _post_validate 依赖的三个约束字段
                    # （此前 prompt_schema 不含，多态扩展失效、逆名关系全丢）
                    "inverse_of": relation_type.get("inverse_of"),
                    "source_types": relation_type.get("source_types") or [],
                    "target_types": relation_type.get("target_types") or [],
                }
                for relation_type in relation_types
            ],
        }

    async def _set_cache(
        self, tenant_id: str, knowledge_base_id: str, data: dict, data_source_id: Optional[str] = None
    ) -> None:
        try:
            key = _compiled_key(tenant_id, knowledge_base_id, data_source_id)
            raw = json.dumps(data, ensure_ascii=False, default=str)
            await CacheUtil.set(key, raw, expire=ONTOLOGY_COMPILED_SCHEMA_TTL)
        except Exception as e:
            logger.warning("Redis cache write failed: %s", e)

    async def _get_cache(
        self, tenant_id: str, knowledge_base_id: str, data_source_id: Optional[str] = None
    ) -> Optional[dict]:
        try:
            raw = await CacheUtil.get(_compiled_key(tenant_id, knowledge_base_id, data_source_id))
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("Redis cache read failed: %s", e)
        return None

    async def invalidate_cache(
        self, tenant_id: str, knowledge_base_id: str, data_source_id: Optional[str] = None
    ) -> None:
        """失效某一份 schema 的缓存。

        **同时清掉合并视图**——合并结果是把 KB 级与各数据源级叠起来，
        任何一份变了，合并视图都不再成立。只清单份会让检索侧一直读到旧合并结果。
        """
        try:
            await CacheUtil.delete(_compiled_key(tenant_id, knowledge_base_id, data_source_id))
            await CacheUtil.delete(_hash_key(tenant_id, knowledge_base_id, data_source_id))
            if data_source_id is not None:
                # 单份变更 → 合并视图必须一并失效
                await CacheUtil.delete(_compiled_key(tenant_id, knowledge_base_id, None))
        except Exception as e:
            logger.warning("Redis cache invalidation failed: %s", e)

    @staticmethod
    def _slugify(name: str) -> str:
        import re

        value = (name or "").strip()
        value = re.sub(r"[\s/\\]+", "_", value)
        value = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "", value)
        return value or "unknown"

    @staticmethod
    def _resolve_entity_code(object_id: str, objects: list[ObjectDef]) -> Optional[str]:
        for obj in objects:
            if obj.id == object_id:
                return obj.ontology_code or OntologyCompiler._slugify(obj.name)
        return None

    @staticmethod
    def _require_name(value: Optional[str], field_name: str) -> str:
        name = (value or "").strip()
        if not name:
            raise InvalidParameterError(message=translate("err.ontology.field_required", params={"field_name": field_name}, fallback=f"{field_name} 不能为空")  )  # 原消息)
        return name


# ── YAML import helpers ──────────────────────────────────────────────────
# [jonex] Convert YamlConstraint → (target_type, target_code) for DB storage


def _yaml_constraint_target(yc) -> tuple[str, str]:
    """[jonex] Derive DB constraint (target_type, target_code) from YamlConstraint."""
    from jonex_core.common.ontology_yaml import YamlConstraint as _YamlConstraint

    if yc.relation:
        return ("relation", yc.relation)
    if yc.entity and yc.attribute:
        return ("attribute", f"{yc.entity}.{yc.attribute}")
    if yc.entity:
        return ("entity", yc.entity)
    # 无 entity/attribute/relation → 约束缺少 target，不能映射
    return ("", "")


def _resolve_constraint_label(
    yc, entity_index: dict[str, dict], target_type: str, target_code: str,
) -> Optional[str]:
    """[jonex] Resolve a human-readable label for a constraint target."""
    if target_type == "entity":
        info = entity_index.get(target_code)
        return info["display_name"] if info else None
    if target_type == "attribute":
        parts = target_code.split(".", 1)
        if len(parts) == 2:
            entity_info = entity_index.get(parts[0])
            if entity_info:
                return entity_info.get("attributes", {}).get(parts[1]) or parts[1]
        return None
    if target_type == "relation":
        return target_code
    return None


def _derive_constraint_name(yc, target_code: str) -> str:
    """[jonex] Derive a unique DB constraint name from type and target."""
    safe_code = target_code.replace(".", "_")
    return f"{yc.type}_{safe_code}"


__all__ = [
    "OntologyCompiler",
    "diff_entity_types",
    "resolve_freshness",
    "stored_entity_type_names",
    "_yaml_constraint_target",
    "_resolve_constraint_label",
    "_derive_constraint_name",
]
