#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""本体 YAML 导入导出共享模块

提供 YAML 解析、校验、归一化、序列化，供 business_domain（模板场景）和
knowledge_base（compiled schema）两个 capability 共用。

仅使用 yaml.safe_load / yaml.safe_dump，禁止任意代码执行。
"""

from __future__ import annotations

import dataclasses
import io
from typing import Any, Optional

import yaml

# ── YAML 格式版本（跨系统契约） ────────────────────────────────────
# 固定为 1；勿与 DB 的 schema_version（乐观锁版本，每次编辑自增）混用。
ONTOLOGY_YAML_FORMAT_VERSION = 1

# ── 属性类型 → YAML 英文类型（导出用） ──────────────────────────────
_ATTR_TYPE_TO_YAML = {
    "string": "string",
    "text": "text",
    "number": "number",
    "date": "date",
    "enum": "enum",
    "boolean": "boolean",
    "字符串": "string",
    "文本": "text",
    "数值": "number",
    "数字": "number",
    "日期": "date",
    "枚举": "enum",
    "布尔": "boolean",
    "布尔值": "boolean",
}

# ── cardinality → 模板 relation_type 映射 ─────────────────────────
_CARDINALITY_TO_TEMPLATE_RELATION_TYPE = {
    "custom": "custom",
    "one_to_one": "一对一",
    "one_to_many": "一对多",
    "many_to_many": "多对多",
    "many_to_one": "多对一",
}

_TEMPLATE_RELATION_TYPE_TO_CARDINALITY = {
    "custom": "custom",
    "一对一": "one_to_one",
    "一对多": "one_to_many",
    "多对多": "many_to_many",
    "多对一": "many_to_one",  # [jonex] P1：补缺，此前「多对一」静默降级 custom
}

# DB/历史导出中可能出现的非契约写法 → YAML 契约词表。
# ontology builder（nexonto）只认 one_to_one/one_to_many/many_to_one/many_to_many/custom，
# 其它值会被静默丢弃，故导出前必须归一化。
_YAML_CARDINALITY_ALIASES = {
    "1:1": "one_to_one",
    "1:N": "one_to_many",
    "N:1": "many_to_one",
    "N:N": "many_to_many",
    "一对一": "one_to_one",
    "一对多": "one_to_many",
    "多对多": "many_to_many",
    "多对一": "many_to_one",
}


# ── Data structures ───────────────────────────────────────────────────


@dataclasses.dataclass
class YamlAttribute:
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    type: str = "string"
    required: bool = False
    is_primary_key: bool = False


@dataclasses.dataclass
class YamlEntity:
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    aliases: list[str] = dataclasses.field(default_factory=list)
    attributes: list[YamlAttribute] = dataclasses.field(default_factory=list)
    # [jonex] P1：TBox 构建技能声明（refine/constrain 两组），缺省 None
    extraction_skills: Optional[dict[str, Any]] = None


@dataclasses.dataclass
class YamlRelation:
    name: str
    source: str
    target: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    aliases: list[str] = dataclasses.field(default_factory=list)
    cardinality: str = "custom"
    inverse_of: Optional[str] = None  # [jonex] P1：逆关系名（纯声明，不建反向边）
    source_types: list[str] = dataclasses.field(default_factory=list)  # 为空回落 source 单值
    target_types: list[str] = dataclasses.field(default_factory=list)  # 为空回落 target 单值


@dataclasses.dataclass
class YamlConstraint:
    type: str
    entity: Optional[str] = None
    attribute: Optional[str] = None
    relation: Optional[str] = None
    severity: Optional[str] = None
    expression: Optional[str] = None
    suggestion: Optional[str] = None
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class OntologyYamlDocument:
    version: int
    domain: str
    entity_types: list[YamlEntity] = dataclasses.field(default_factory=list)
    relation_types: list[YamlRelation] = dataclasses.field(default_factory=list)
    constraints: list[YamlConstraint] = dataclasses.field(default_factory=list)
    disambiguation: dict[str, bool] = dataclasses.field(default_factory=lambda: {
        "case_insensitive": True,
        "alias_merge": True,
    })


# ── Validation result ──────────────────────────────────────────────────

@dataclasses.dataclass
class YamlValidation:
    warnings: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


# ── Parse ──────────────────────────────────────────────────────────────


def parse_yaml(yaml_text: str) -> tuple[Optional[OntologyYamlDocument], YamlValidation]:
    """解析 YAML 文本，归一化为 OntologyYamlDocument。

    Returns:
        (doc, validation): doc 为 None 表示解析失败；validation 含 warnings/errors。
    """
    validation = YamlValidation()

    # 1. 安全解析
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        validation.errors.append(f"YAML 语法错误: {exc}")
        return None, validation

    if not isinstance(raw, dict):
        validation.errors.append("YAML 顶层必须是对象")
        return None, validation

    # 2. 顶层字段
    version = raw.get("version", 1)
    if not isinstance(version, int) or version < 1:
        validation.errors.append(f"YAML version 不兼容: {version}")
        return None, validation

    domain = raw.get("domain", "default")
    if not isinstance(domain, str) or not domain.strip():
        domain = "default"

    # 3. entity_types
    entities: list[YamlEntity] = []
    raw_entities = raw.get("entity_types")
    if isinstance(raw_entities, list):
        entities = _parse_entities(raw_entities, validation)
    elif raw_entities is not None:
        validation.errors.append("entity_types 必须是列表")

    # 4. 校验 entity code 唯一性
    entity_codes: set[str] = set()
    for e in entities:
        if not e.name:
            continue
        if e.name in entity_codes:
            validation.errors.append(f"entity code 重复: {e.name}")
        entity_codes.add(e.name)

    # 5. relation_types
    relations: list[YamlRelation] = []
    raw_relations = raw.get("relation_types")
    if isinstance(raw_relations, list):
        relations = _parse_relations(raw_relations, entity_codes, validation)
    elif raw_relations is not None:
        validation.errors.append("relation_types 必须是列表")

    # 6. constraints
    constraints: list[YamlConstraint] = []
    raw_constraints = raw.get("constraints")
    if isinstance(raw_constraints, list):
        constraints = _parse_constraints(raw_constraints, entity_codes, entities, validation)
    elif raw_constraints is not None:
        validation.errors.append("constraints 必须是列表")

    # 7. disambiguation
    dis = raw.get("disambiguation")
    if isinstance(dis, dict):
        disambiguation = {
            "case_insensitive": bool(dis.get("case_insensitive", True)),
            "alias_merge": bool(dis.get("alias_merge", True)),
        }
    else:
        disambiguation = {"case_insensitive": True, "alias_merge": True}

    doc = OntologyYamlDocument(
        version=version,
        domain=domain,
        entity_types=entities,
        relation_types=relations,
        constraints=constraints,
        disambiguation=disambiguation,
    )
    return doc, validation


def _parse_entities(raw: list[dict], validation: YamlValidation) -> list[YamlEntity]:
    entities: list[YamlEntity] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            validation.errors.append(f"entity_types[{i}] 必须是对象")
            continue
        name = _str_or(item.get("name"), "")
        if not name:
            validation.errors.append(f"entity_types[{i}].name 不能为空")
            continue

        attrs = _parse_attributes(item.get("attributes"), name, validation)
        raw_skills = item.get("extraction_skills")
        if raw_skills is not None and not isinstance(raw_skills, dict):
            validation.errors.append(f"{name}.extraction_skills 必须是对象")
            raw_skills = None
        entities.append(YamlEntity(
            name=name,
            display_name=_str_or(item.get("display_name"), None) or name,
            description=_str_or(item.get("description"), None),
            aliases=_normalize_string_list(item.get("aliases")),
            attributes=attrs,
            extraction_skills=raw_skills,
        ))
    return entities


def _parse_attributes(
    raw: Any, entity_code: str, validation: YamlValidation,
) -> list[YamlAttribute]:
    if not isinstance(raw, list):
        return []
    attrs: list[YamlAttribute] = []
    attr_codes: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            validation.errors.append(f"{entity_code}.attributes[{i}] 必须是对象")
            continue
        code = _str_or(item.get("name"), "")
        if not code:
            code = f"attr_{i}"
            validation.warnings.append(f"{entity_code}.attributes[{i}].name 为空，自动补充")
        if code in attr_codes:
            validation.errors.append(f"{entity_code} 下 attribute code 重复: {code}")
        attr_codes.add(code)

        raw_type = _str_or(item.get("type"), "string")
        normalized_type = _normalize_attr_type(raw_type)
        if normalized_type != raw_type:
            validation.warnings.append(
                f"{entity_code}.{code}.type={raw_type} → 归一化为 {normalized_type}"
            )

        attrs.append(YamlAttribute(
            name=code,
            display_name=_str_or(item.get("display_name"), None) or code,
            description=_str_or(item.get("description"), None),
            type=normalized_type,
            required=bool(item.get("required", False)),
            is_primary_key=bool(item.get("is_primary_key", False)),
        ))
    return attrs


def _parse_relations(
    raw: list[dict], entity_codes: set[str], validation: YamlValidation,
) -> list[YamlRelation]:
    relations: list[YamlRelation] = []
    rel_codes: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            validation.errors.append(f"relation_types[{i}] 必须是对象")
            continue
        name = _str_or(item.get("name"), "")
        if not name:
            validation.errors.append(f"relation_types[{i}].name 不能为空")
            continue
        if name in rel_codes:
            validation.errors.append(f"relation code 重复: {name}")
        rel_codes.add(name)

        source = _str_or(item.get("source"), "")
        target = _str_or(item.get("target"), "")
        if source and entity_codes and source not in entity_codes:
            validation.errors.append(f"relation {name}.source={source} 引用了不存在的 entity")
        if target and entity_codes and target not in entity_codes:
            validation.errors.append(f"relation {name}.target={target} 引用了不存在的 entity")

        cardinality = _str_or(item.get("cardinality"), "custom")
        if cardinality not in _CARDINALITY_TO_TEMPLATE_RELATION_TYPE:
            validation.warnings.append(f"relation {name} 的 cardinality={cardinality} 无法映射到模板 relation_type，将使用 custom")
            cardinality = "custom"

        relations.append(YamlRelation(
            name=name,
            source=source,
            target=target,
            display_name=_str_or(item.get("display_name"), None) or name,
            description=_str_or(item.get("description"), None),
            aliases=_normalize_string_list(item.get("aliases")),
            cardinality=cardinality,
            inverse_of=_str_or(item.get("inverse_of"), None),
            source_types=_normalize_string_list(item.get("source_types")),
            target_types=_normalize_string_list(item.get("target_types")),
        ))
    return relations


def _parse_constraints(
    raw: list[dict], entity_codes: set[str], entities: list[YamlEntity],
    validation: YamlValidation,
) -> list[YamlConstraint]:
    """解析约束。校验 constraint target 能解析到 entity/attribute/relation。"""
    constraints: list[YamlConstraint] = []
    # 构建 entity.attr → entity_code 的索引
    attr_code_set: set[str] = set()
    for e in entities:
        for a in e.attributes:
            attr_code_set.add(f"{e.name}.{a.name}")

    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            validation.errors.append(f"constraint[{i}] 必须是对象")
            continue
        ctype = _str_or(item.get("type"), "")
        if not ctype:
            validation.errors.append(f"constraint[{i}].type 不能为空")
            continue

        entity = _str_or(item.get("entity"), None)
        attr = _str_or(item.get("attribute"), None)
        rel = _str_or(item.get("relation"), None)

        # 校验 target
        if entity and entity_codes and entity not in entity_codes:
            validation.errors.append(f"constraint[{i}] entity={entity} 引用了不存在的 entity")
        if attr:
            target = f"{entity or '?'}.{attr}"
            if attr_code_set and target not in attr_code_set:
                validation.warnings.append(f"constraint[{i}] 引用 {target} 不匹配已知 entity.attribute")

        constraints.append(YamlConstraint(
            type=ctype,
            entity=entity,
            attribute=attr,
            relation=rel,
            severity=_str_or(item.get("severity"), None),
            expression=_str_or(item.get("expression"), None),
            suggestion=_str_or(item.get("suggestion"), None),
            raw=item,
        ))
    return constraints


# ── Dump (export) ──────────────────────────────────────────────────────


def dump_yaml(doc: OntologyYamlDocument) -> str:
    """将 OntologyYamlDocument 序列化为 YAML 字符串。

    统一 YAML 字段顺序，避免两个后端各自拼格式。
    """

    def _attr_to_dict(a: YamlAttribute) -> dict:
        d: dict[str, Any] = {"name": a.name}
        if a.display_name and a.display_name != a.name:
            d["display_name"] = a.display_name
        if a.description:
            d["description"] = a.description
        d["type"] = to_yaml_attr_type(a.type)
        # 契约要求 required 为必填布尔字段：false 也必须显式输出。
        # 省略时 Jonex 自身能按默认 false 解析，但跨系统导入（ontology builder 的
        # Schema v1 zod 校验）会因 "expected boolean, received undefined" 整份拒绝。
        d["required"] = bool(a.required)
        if a.is_primary_key:
            d["is_primary_key"] = True
        return d

    def _entity_to_dict(e: YamlEntity) -> dict:
        d: dict[str, Any] = {"name": e.name}
        if e.display_name and e.display_name != e.name:
            d["display_name"] = e.display_name
        if e.description:
            d["description"] = e.description
        if e.attributes:
            d["attributes"] = [_attr_to_dict(a) for a in e.attributes]
        if e.aliases:
            d["aliases"] = [a for a in e.aliases]
        if e.extraction_skills:
            d["extraction_skills"] = e.extraction_skills
        return d

    def _rel_to_dict(r: YamlRelation) -> dict:
        d: dict[str, Any] = {"name": r.name, "source": r.source, "target": r.target}
        if r.display_name and r.display_name != r.name:
            d["display_name"] = r.display_name
        if r.description:
            d["description"] = r.description
        if r.aliases:
            d["aliases"] = [a for a in r.aliases]
        cardinality = to_yaml_cardinality(r.cardinality)
        if cardinality != "custom":
            d["cardinality"] = cardinality
        if r.inverse_of:
            d["inverse_of"] = r.inverse_of
        if r.source_types:
            d["source_types"] = [s for s in r.source_types]
        if r.target_types:
            d["target_types"] = [t for t in r.target_types]
        return d

    def _constraint_to_dict(c: YamlConstraint) -> dict:
        d: dict[str, Any] = {"type": c.type}
        if c.entity:
            d["entity"] = c.entity
        if c.attribute:
            d["attribute"] = c.attribute
        if c.relation:
            d["relation"] = c.relation
        if c.severity:
            d["severity"] = c.severity
        if c.expression:
            d["expression"] = c.expression  # [jonex] P1：导出不再丢约束表达式
        if c.suggestion:
            d["suggestion"] = c.suggestion
        return d

    # 四个集合键始终输出（即使是空数组）：ontology builder 的 Schema v1 契约
    # 把 entity_types/relation_types/constraints 都定义为必填数组，缺键会整份导入失败。
    data: dict[str, Any] = {
        "version": doc.version,
        "domain": doc.domain,
        "entity_types": [_entity_to_dict(e) for e in doc.entity_types],
        "relation_types": [_rel_to_dict(r) for r in doc.relation_types],
        "constraints": [_constraint_to_dict(c) for c in doc.constraints],
        "disambiguation": {
            "case_insensitive": doc.disambiguation.get("case_insensitive", True),
            "alias_merge": doc.disambiguation.get("alias_merge", True),
        },
    }

    buf = io.StringIO()
    class _IndentDumper(yaml.Dumper):
        """强制数组元素也缩进，避免 `-` 与父属性对齐。"""
        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)

    yaml.dump(
        data, buf,
        Dumper=_IndentDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        indent=2,
    )
    return buf.getvalue()


# ── Public helpers ─────────────────────────────────────────────────────


def to_entity_code(ontology_code: Optional[str], name: str) -> str:
    """稳定 entity code：优先 ontology_code，否则 name。"""
    return (ontology_code or name or "").strip()


def to_relation_code(ontology_code: Optional[str], name: str) -> str:
    """稳定 relation code。"""
    return (ontology_code or name or "").strip()


def to_attr_code(ontology_code: Optional[str], attr_name: str) -> str:
    """稳定 attribute code。"""
    return (ontology_code or attr_name or "").strip()


def card_to_template_relation_type(cardinality: str) -> str:
    """YAML cardinality → 模板 relation_type。"""
    return _CARDINALITY_TO_TEMPLATE_RELATION_TYPE.get(cardinality, "custom")


def template_relation_type_to_card(relation_type: str) -> str:
    """模板 relation_type → YAML cardinality。"""
    return _TEMPLATE_RELATION_TYPE_TO_CARDINALITY.get(relation_type, "custom")


def to_yaml_attr_type(db_type: str) -> str:
    """DB attr_type → YAML 英文类型。未知类型返回 string 并应在调用方记 warning。"""
    return _ATTR_TYPE_TO_YAML.get(db_type.strip(), "string")


def to_yaml_cardinality(cardinality: Optional[str]) -> str:
    """DB/历史基线写法 → YAML 契约基数词表（未知值回落 custom）。

    ontology builder 只认 one_to_one / one_to_many / many_to_one / many_to_many /
    custom；N:1 这类写法在对方侧会被静默丢弃，因此导出前统一归一化。
    """
    value = (cardinality or "").strip()
    if value in _CARDINALITY_TO_TEMPLATE_RELATION_TYPE:
        return value
    return _YAML_CARDINALITY_ALIASES.get(value, "custom")


def normalize_attr_type(raw: str) -> str:
    """归一化属性类型：中文/未知类型 → 英文标准类型。"""
    return _ATTR_TYPE_TO_YAML.get(raw.strip(), "string")


# ── Internal helpers ───────────────────────────────────────────────────


def _str_or(val: Any, default: Optional[str]) -> Optional[str]:
    if val is None:
        return default
    if isinstance(val, str):
        s = val.strip()
        return s if s else default
    return default


def _normalize_string_list(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(v).strip() for v in val if isinstance(v, str) and str(v).strip()]


def _normalize_attr_type(raw: str) -> str:
    return normalize_attr_type(raw)
