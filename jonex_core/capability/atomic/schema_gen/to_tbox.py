"""nexonto ExportSchemaV1 → Jonex TBox。

两者的 YAML 形状**几乎一样**（`name/display_name/description/aliases/attributes`，
关系还都有 `inverse_of`），所以这不是翻译，只是一层薄适配。真正要做的事只有一件：

## 类型词表对不上

nexonto 的属性类型有 8 种：`string/text/number/integer/boolean/date/datetime/enum`
Jonex 的 TBox 只有 6 种：`string/text/number/date/enum/boolean`
（`jonex_core/common/ontology_yaml.py` 的 `_ATTR_TYPE_TO_YAML`）

`integer` 和 `datetime` **不在表里**，导入时按 `normalize_attr_type()` 的兜底
**静默退化成 `string`**。本仓实测：1411 个属性里 **435 个 integer + 196 个 datetime
全部中招**，导入返回 631 条归一化警告。

这里做映射，而**不是**反过来去改 nexonto 的词汇——nexonto 的 8 类型是对的，
是 Jonex 的词表窄。改上游去迁就下游会把语义丢掉。

## 已知损失

- `enum_values`：Jonex 的 `YamlAttribute` **没有这个字段**（只有 `enum` 这个类型名），
  所以我们从注释里解析出来的枚举取值到 TBox 就没了。如实记进损失清单。
- 其余字段（`display_name` / `aliases` / `extraction_skills`）两边同名，直接透传。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# nexonto 类型 → Jonex TBox 类型。
# 不在表里的原样透传（Jonex 侧会自己再兜底一次，但那不该由我们触发）。
_TYPE_MAP = {
    "integer": "number",   # Jonex 无 integer；number 是最接近的
    "datetime": "date",    # Jonex 无 datetime；date 是可表达的最接近值
}


@dataclass
class TboxResult:
    document: dict
    stats: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def convert(doc: dict) -> TboxResult:
    """nexonto 文档 → TBox 文档。"""
    converted: dict[str, int] = {}
    enum_dropped = 0
    attrs_total = 0

    entity_types: list[dict[str, Any]] = []
    for e in doc.get("entity_types", []):
        attrs: list[dict[str, Any]] = []
        for a in e.get("attributes", []):
            attrs_total += 1
            t = a.get("type", "string")
            if t in _TYPE_MAP:
                converted[t] = converted.get(t, 0) + 1
                t = _TYPE_MAP[t]

            out: dict[str, Any] = {
                "name": a["name"],
                "type": t,
                "required": bool(a.get("required")),
                "is_primary_key": bool(a.get("is_primary_key")),
            }
            # 物理列名（catalog 原名）：属性名可能被归一成 slug，② NL→SQL
            # 生成 SQL 时必须回落到物理名，不能在这一层白名单式丢掉。
            if a.get("physical_name"):
                out["physical_name"] = a["physical_name"]
            if a.get("description"):
                out["description"] = a["description"]
            # enum_values 有意不写：Jonex 的 YamlAttribute 没有这个字段，
            # 写了也会被解析器丢掉。与其让它静默消失，不如在这里计数报出来。
            if a.get("enum_values"):
                enum_dropped += 1
            attrs.append(out)

        ent: dict[str, Any] = {
            "name": e["name"],
            "aliases": list(e.get("aliases") or []),
            "attributes": attrs,
        }
        # 物理表限定名（`<schema>.<table>`）：这一层是白名单式重拼，不显式透传就会
        # 在 TBox 落地前被静默丢掉——② NL→SQL 正是从这份文档里取表名的。
        if e.get("source_object_id"):
            ent["source_object_id"] = e["source_object_id"]
        if e.get("display_name"):
            ent["display_name"] = e["display_name"]
        if e.get("description"):
            ent["description"] = e["description"]
        if e.get("extraction_skills"):
            ent["extraction_skills"] = e["extraction_skills"]
        entity_types.append(ent)

    relation_types: list[dict[str, Any]] = []
    n_inverse = 0
    for r in doc.get("relation_types", []):
        out = {"name": r["name"], "source": r["source"], "target": r["target"]}
        if r.get("description"):
            out["description"] = r["description"]
        if r.get("cardinality"):
            out["cardinality"] = r["cardinality"]
        if r.get("inverse_of"):
            # 这条是路 A 的目的所在——Jonex 的 `_build_inverse_map` 靠它
            # 给 incoming 边换读法。丢了它整个 TBox 就只是个装饰。
            out["inverse_of"] = r["inverse_of"]
            n_inverse += 1
        # join 键：Jonex 侧 `_normalize_relation_types` 是逐字段重建 dict 的，
        # 这里不显式透传就会在 TBox 落地前被静默丢掉 —— ② NL→SQL 于是看不到任何 join，
        # 只能写单表 SQL（库内跨表当前结构上不可能，就是这个原因）。
        for key in ("source_column", "target_column", "origin"):
            if r.get(key) is not None:
                out[key] = r[key]
        if r.get("source_types"):
            out["source_types"] = r["source_types"]
        if r.get("target_types"):
            out["target_types"] = r["target_types"]
        relation_types.append(out)

    document = {
        "version": doc.get("version", 1),
        "domain": doc.get("domain", ""),
        "entity_types": entity_types,
        "relation_types": relation_types,
        "constraints": doc.get("constraints", []),
        "disambiguation": doc.get("disambiguation", {"case_insensitive": True, "alias_merge": True}),
    }

    res = TboxResult(document=document)
    res.stats = {
        "entity_types": len(entity_types),
        "relations": len(relation_types),
        "relations_with_inverse": n_inverse,
        "attributes": attrs_total,
        "type_mapped": sum(converted.values()),
        "type_map_detail": converted,
        "enum_values_dropped": enum_dropped,
    }
    if converted:
        res.notes.append(
            "类型映射：" + "、".join(f"{k}→{v}" for k, v in sorted(converted.items()))
            + f"（Jonex 的 TBox 词表无 {'/'.join(_TYPE_MAP)}）"
        )
    if enum_dropped:
        res.notes.append(
            f"{enum_dropped} 个属性的 enum_values 未写入——Jonex 的 YamlAttribute 无此字段，"
            "从注释解析出的枚举取值在 TBox 这一层丢失（实例层不受影响）"
        )
    return res


def run(nexonto_yaml: str | Path, out_path: str | Path) -> TboxResult:
    doc = yaml.safe_load(Path(nexonto_yaml).read_text(encoding="utf-8"))
    res = convert(doc)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(
        yaml.safe_dump(res.document, allow_unicode=True, sort_keys=False,
                       default_flow_style=False, width=4096),
        encoding="utf-8",
    )
    return res
