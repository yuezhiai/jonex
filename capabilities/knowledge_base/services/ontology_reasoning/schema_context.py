"""公理/规则引用解析视图。

来源是 `OntologyCompiler.get_merged_schema()`（抽象投影 + 各数据源行）；
该视图只读、不缓存，随 schema 变化即时反映。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..ontology_compiler import OntologyCompiler


def _norm(value) -> str:
    return str(value or "").strip()


@dataclass
class SchemaContext:
    schema: dict = field(default_factory=dict)
    entities: dict[str, dict] = field(default_factory=dict)
    entity_alias: dict[str, str] = field(default_factory=dict)
    relations: dict[str, dict] = field(default_factory=dict)
    relation_alias: dict[str, str] = field(default_factory=dict)
    inverse_names: dict[str, str] = field(default_factory=dict)
    attributes: dict[tuple[str, str], dict] = field(default_factory=dict)
    attribute_alias: dict[tuple[str, str], str] = field(default_factory=dict)
    case_insensitive: bool = True
    deprecated_entities: set[str] = field(default_factory=set)
    deprecated_relations: set[str] = field(default_factory=set)

    @classmethod
    def from_schema(cls, schema: Optional[dict]) -> "SchemaContext":
        schema = schema if isinstance(schema, dict) else {}
        dis = schema.get("disambiguation") if isinstance(schema.get("disambiguation"), dict) else {}
        ctx = cls(
            schema=schema,
            case_insensitive=bool(dis.get("case_insensitive", True)),
        )

        def key(name: str) -> str:
            name = _norm(name)
            return name.lower() if ctx.case_insensitive else name

        for entity in schema.get("entity_types") or []:
            if not isinstance(entity, dict):
                continue
            name = _norm(entity.get("name"))
            if not name or key(name) in ctx.entities:
                continue
            ctx.entities[key(name)] = entity
            if _norm(entity.get("status")) == "deprecated":
                ctx.deprecated_entities.add(key(name))
            owners: dict[str, str] = {}
            for alias in entity.get("aliases") or []:
                alias_key = key(alias)
                if not alias_key or alias_key == key(name):
                    continue
                # 同名别名属于多个实体时不猜（与 Studio 的 alias 冲突口径一致）。
                owner = owners.get(alias_key)
                if owner is not None and owner != key(name):
                    ctx.entity_alias.pop(alias_key, None)
                    continue
                owners[alias_key] = key(name)
                ctx.entity_alias[alias_key] = key(name)
            for attr in entity.get("attributes") or []:
                if not isinstance(attr, dict):
                    continue
                attr_name = _norm(attr.get("name"))
                if not attr_name:
                    continue
                attr_key = key(attr_name)
                ctx.attributes[(key(name), attr_key)] = attr
                physical = _norm(attr.get("physical_name"))
                if physical:
                    ctx.attribute_alias[(key(name), key(physical))] = attr_key

        for relation in schema.get("relation_types") or []:
            if not isinstance(relation, dict):
                continue
            name = _norm(relation.get("name"))
            if not name or key(name) in ctx.relations:
                continue
            ctx.relations[key(name)] = relation
            if _norm(relation.get("status")) == "deprecated":
                ctx.deprecated_relations.add(key(name))
            for alias in relation.get("aliases") or []:
                alias_key = key(alias)
                if alias_key and alias_key != key(name):
                    ctx.relation_alias.setdefault(alias_key, key(name))
            inverse = _norm(relation.get("inverse_of"))
            if inverse and key(inverse) not in ctx.relations:
                ctx.inverse_names.setdefault(key(inverse), key(name))
        return ctx

    # ── 解析 ──────────────────────────────────────────────────────

    def _key(self, value) -> str:
        value = _norm(value)
        return value.lower() if self.case_insensitive else value

    def resolve_entity(self, ref) -> Optional[str]:
        raw = _norm(ref)
        if not raw:
            return None
        key = self._key(raw)
        if key in self.entities:
            return key
        return self.entity_alias.get(key)

    def resolve_relation(self, ref) -> Optional[str]:
        """只解析 canonical 名/别名；`inverse_of` 是虚拟名，另由调用方判断。"""
        raw = _norm(ref)
        if not raw:
            return None
        key = self._key(raw)
        if key in self.relations:
            return key
        return self.relation_alias.get(key)

    def resolve_attribute(self, owner, ref) -> Optional[tuple[str, str]]:
        owner_key = self.resolve_entity(owner)
        attr_key = self._key(ref)
        if not owner_key or not attr_key:
            return None
        if (owner_key, attr_key) in self.attributes:
            return owner_key, attr_key
        physical = self.attribute_alias.get((owner_key, attr_key))
        if physical:
            return owner_key, physical
        return None

    def attribute_type(self, owner: str, attr: str) -> Optional[str]:
        found = self.resolve_attribute(owner, attr)
        if not found:
            return None
        item = self.attributes.get(found) or {}
        return _norm(item.get("type")).lower() or "string"

    def relation_endpoints(self, relation: str, side: str) -> set[str]:
        key = self.resolve_relation(relation)
        rel = self.relations.get(key or "") or {}
        values = rel.get(f"{side}_types") or []
        names = {n for n in (_norm(v) for v in values) if n}
        if names:
            return names
        single = _norm(rel.get(side))
        return {single} if single else set()


async def load_schema_context(tenant_id: str, knowledge_base_id: str) -> SchemaContext:
    """取合并视图；没有行时回落到 KB 级（含自动编译/YAML 兜底）。"""
    compiler = OntologyCompiler()
    # 公理/规则引用解析用声明视图，不需要（也不应触发）读时展开。
    schema = await compiler._get_merged_schema_raw(tenant_id, knowledge_base_id)
    if not schema:
        schema = await compiler._get_compiled_schema_raw(
            tenant_id, knowledge_base_id, auto_compile=True,
        )
    return SchemaContext.from_schema(schema)


__all__ = ["SchemaContext", "load_schema_context"]
