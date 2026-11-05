"""
本体数据模型 — TBox 层级。

定义本体 schema 的结构化类型（实体类型定义、属性定义、关系类型定义），
以及顶层 OntologySchema 容器，对应 ontology YAML 文件的格式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AttributeDef:
    """实体类型的属性定义。"""

    name: str
    type: str = "string"
    required: bool = False
    description: Optional[str] = None
    # 四个核心字段之外的键（如将来的 values / min / max / pattern 等属性约束键）原样收在这里。
    # 旧实现 AttributeDef(**a) 直接展开，YAML 属性多一个键就 TypeError，整个 OntologyRegistry.load 崩。
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "AttributeDef":
        known = {"name", "type", "required", "description"}
        return cls(
            name=data["name"],
            type=data.get("type", "string"),
            required=bool(data.get("required", False)),
            description=data.get("description"),
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass
class EntityTypeDef:
    """实体类型定义（TBox 中的概念类）。"""

    name: str
    aliases: List[str] = field(default_factory=list)
    attributes: List[AttributeDef] = field(default_factory=list)
    # [jonex] P1（skill-action 批 1）：TBox 构建技能声明
    # {refine: [{skill, description}], constrain: [{skill, description}]}，缺省 None
    extraction_skills: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: dict) -> "EntityTypeDef":
        return cls(
            name=data["name"],
            aliases=data.get("aliases", []),
            attributes=[AttributeDef.from_dict(a) for a in data.get("attributes", [])],
            extraction_skills=data.get("extraction_skills"),
        )


@dataclass
class RelationTypeDef:
    """关系类型定义（TBox 中的角色/属性）。"""

    name: str
    source: Optional[str] = None  # 源实体类型约束
    target: Optional[str] = None  # 目标实体类型约束
    description: Optional[str] = None
    cardinality: str = "custom"  # [jonex] P1：打通已有字段（from_dict 此前丢 cardinality）
    inverse_of: Optional[str] = None  # [jonex] P1：逆关系名（纯声明，不建反向边）
    source_types: List[str] = field(default_factory=list)  # 为空则回落到 source 单值
    target_types: List[str] = field(default_factory=list)  # 为空则回落到 target 单值

    @classmethod
    def from_dict(cls, data: dict) -> "RelationTypeDef":
        return cls(
            name=data["name"],
            source=data.get("source"),
            target=data.get("target"),
            description=data.get("description"),
            cardinality=data.get("cardinality") or "custom",
            inverse_of=data.get("inverse_of"),
            source_types=list(data.get("source_types") or []),
            target_types=list(data.get("target_types") or []),
        )


@dataclass
class ConstraintDef:
    """本体约束定义。"""

    type: str
    entity: Optional[str] = None
    attribute: Optional[str] = None
    severity: str = "warning"

    @classmethod
    def from_dict(cls, data: dict) -> "ConstraintDef":
        return cls(
            type=data["type"],
            entity=data.get("entity"),
            attribute=data.get("attribute"),
            severity=data.get("severity", "warning"),
        )


@dataclass
class DisambiguationConfig:
    """消歧配置。"""

    case_insensitive: bool = True
    alias_merge: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "DisambiguationConfig":
        return cls(
            case_insensitive=data.get("case_insensitive", True),
            alias_merge=data.get("alias_merge", True),
        )


@dataclass
class OntologySchema:
    """顶层本体 Schema 定义，对应一个 ontology YAML 文件。"""

    version: int = 1
    domain: str = "default"
    entity_types: List[EntityTypeDef] = field(default_factory=list)
    relation_types: List[RelationTypeDef] = field(default_factory=list)
    constraints: List[ConstraintDef] = field(default_factory=list)
    disambiguation: DisambiguationConfig = field(default_factory=DisambiguationConfig)
    raw: Optional[Dict[str, Any]] = None  # 原始字典，供需要直接访问 YAML 字段的代码使用

    @classmethod
    def from_dict(cls, data: dict) -> "OntologySchema":
        return cls(
            version=data.get("version", 1),
            domain=data.get("domain", "default"),
            entity_types=[EntityTypeDef.from_dict(e) for e in data.get("entity_types", [])],
            relation_types=[RelationTypeDef.from_dict(r) for r in data.get("relation_types", [])],
            constraints=[ConstraintDef.from_dict(c) for c in data.get("constraints", [])],
            disambiguation=DisambiguationConfig.from_dict(data.get("disambiguation", {})),
            raw=data,
        )

    def get_entity_type_names(self) -> List[str]:
        return [et.name for et in self.entity_types]

    def get_relation_type_names(self) -> List[str]:
        return [rt.name for rt in self.relation_types]

    def find_entity_type(self, name: str) -> Optional[EntityTypeDef]:
        """按名称（含别名匹配）查找实体类型定义。"""
        for et in self.entity_types:
            if et.name.lower() == name.lower():
                return et
            if name.lower() in [a.lower() for a in et.aliases]:
                return et
        return None

    def find_relation_type(self, name: str) -> Optional[RelationTypeDef]:
        for rt in self.relation_types:
            if rt.name == name:
                return rt
        return None

    @classmethod
    def from_compiled_dict(cls, data: dict) -> "OntologySchema":
        """从 compiled schema JSON dict 构建 OntologySchema。

        compiled schema 的 entity_types/relation_types 比 TBox YAML 更丰富
        （含 display_name、aliases、cardinality 等），这里只提取与 TBox
        兼容的核心字段。
        """
        entity_types = []
        for et in data.get("entity_types", []):
            attrs = []
            for a in et.get("attributes", []):
                attrs.append(AttributeDef(
                    name=a.get("name") or a.get("ontology_code", ""),
                    type=a.get("type", "string"),
                    required=a.get("required", False),
                    # [jonex] P0-fix：优先真实语义说明；display_name 恒非空
                    # （fallback=name），旧顺序 description 永不被采用。
                    description=a.get("description") or a.get("display_name"),
                ))
            entity_types.append(EntityTypeDef(
                name=et.get("name", ""),
                aliases=et.get("aliases", []),
                attributes=attrs,
                extraction_skills=et.get("extraction_skills"),
            ))

        relation_types = []
        for rt in data.get("relation_types", []):
            relation_types.append(RelationTypeDef(
                name=rt.get("name", ""),
                source=rt.get("source"),
                target=rt.get("target"),
                description=rt.get("description") or rt.get("display_name"),
                cardinality=rt.get("cardinality") or "custom",
                inverse_of=rt.get("inverse_of"),
                source_types=list(rt.get("source_types") or []),
                target_types=list(rt.get("target_types") or []),
            ))

        disamb = data.get("disambiguation", {})
        return cls(
            version=data.get("schema_version", 1),
            domain=data.get("template_scenario_id") or data.get("domain", "compiled"),
            entity_types=entity_types,
            relation_types=relation_types,
            constraints=[ConstraintDef.from_dict(c) for c in data.get("constraints", [])],
            disambiguation=DisambiguationConfig.from_dict(disamb),
            raw=data,
        )
