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


def _compiled_key(tenant_id: str, knowledge_base_id: str) -> str:
    return f"ontology:compiled:{tenant_id}:{knowledge_base_id}"


def _hash_key(tenant_id: str, knowledge_base_id: str) -> str:
    return f"ontology:compiled:hash:{tenant_id}:{knowledge_base_id}"


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
        auto_compile: bool = True,
    ) -> Optional[dict]:
        tenant_id = require_tenant(tenant_id)

        cached = await self._get_cache(tenant_id, knowledge_base_id)
        if cached is not None:
            return cached

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            schema = await repo.get_compiled_schema(tenant_id, knowledge_base_id)
            if schema is not None:
                result = schema.to_dict()
                await self._set_cache(tenant_id, knowledge_base_id, result)
                return result

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

    async def save_compiled_schema(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        entity_types: list[dict],
        relation_types: list[dict],
        edited_by: Optional[str] = None,
        constraints: Optional[list[dict]] = None,
        expected_schema_version: Optional[int] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)

        # 编辑器保存必须携带版本，做乐观锁；缺失直接 400（避免无版本请求绕过并发保护）
        if expected_schema_version is None:
            raise InvalidParameterError(message=translate("err.ontology.missing_schema_version", fallback="缺少 expected_schema_version")  )  # 原消息)

        normalized_entities = self._normalize_entity_types(entity_types)
        normalized_relations = self._normalize_relation_types(relation_types, normalized_entities)
        prompt_schema = self._build_prompt_schema(normalized_entities, normalized_relations)
        edited_at = datetime.now(timezone.utc)

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            binding = await repo.get_binding(tenant_id, knowledge_base_id)
            current = await repo.get_compiled_schema(tenant_id, knowledge_base_id)

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
                final_constraints = self._normalize_constraints(
                    constraints, normalized_entities, normalized_relations
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

            compiled = await repo.upsert_compiled_schema(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
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
                schema_mode="manual_edited",
                sync_status="synced",
                edited_at=edited_at,
                edited_by=edited_by,
                expected_version=expected_schema_version,
            )
            await session.commit()

        await self._set_cache(tenant_id, knowledge_base_id, compiled.to_dict())
        return compiled.to_dict()

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
            version=schema.get("schema_version", 1),
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
            prompt = json.loads(registry.to_prompt_json(schema.domain))

            entity_types = [
                {
                    "name": entity_type.name,
                    "display_name": entity_type.name,
                    "description": "",
                    "requirement": "",
                    "status": "active",
                    "aliases": entity_type.aliases,
                    "source_object_id": None,
                    "attributes": [
                        {
                            "name": attr.name,
                            "display_name": attr.name,
                            "description": "",
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
                    "description": "",
                    "status": "active",
                    "aliases": [],
                    "source": relation_type.source,
                    "target": relation_type.target,
                    "source_relation_id": None,
                    "cardinality": "custom",
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
                "prompt_schema": prompt or self._build_prompt_schema(entity_types, relation_types),
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
            })

        return normalized

    def _normalize_relation_types(self, relation_types: list[dict], entity_types: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        names_seen: set[str] = set()
        entity_names = {item["name"] for item in entity_types}

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

            normalized.append({
                "name": name,
                "target_type": target_type,
                "target_code": target_code,
                "target_label": (item.get("target_label") or "").strip() or None,
                "constraint_type": (item.get("constraint_type") or "custom").strip(),
                "expression": (item.get("expression") or "").strip(),
                "suggestion": (item.get("suggestion") or "").strip(),
            })

        return normalized

    def _build_prompt_schema(self, entity_types: list[dict], relation_types: list[dict]) -> dict:
        return {
            "entity_types": [
                {
                    "name": entity_type["name"],
                    "aliases": entity_type.get("aliases", []),
                    "attributes": [
                        {
                            "name": attr["name"],
                            "type": attr["type"],
                            "required": attr["required"],
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
                }
                for relation_type in relation_types
            ],
        }

    async def _set_cache(self, tenant_id: str, knowledge_base_id: str, data: dict) -> None:
        try:
            key = _compiled_key(tenant_id, knowledge_base_id)
            raw = json.dumps(data, ensure_ascii=False, default=str)
            await CacheUtil.set(key, raw, expire=ONTOLOGY_COMPILED_SCHEMA_TTL)
        except Exception as e:
            logger.warning("Redis cache write failed: %s", e)

    async def _get_cache(self, tenant_id: str, knowledge_base_id: str) -> Optional[dict]:
        try:
            raw = await CacheUtil.get(_compiled_key(tenant_id, knowledge_base_id))
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("Redis cache read failed: %s", e)
        return None

    async def invalidate_cache(self, tenant_id: str, knowledge_base_id: str) -> None:
        try:
            await CacheUtil.delete(_compiled_key(tenant_id, knowledge_base_id))
            await CacheUtil.delete(_hash_key(tenant_id, knowledge_base_id))
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
    "_yaml_constraint_target",
    "_resolve_constraint_label",
    "_derive_constraint_name",
]
