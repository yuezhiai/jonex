#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""共享本体 TBox（YAML）→ 抽象 schema 的导入器（纯函数族，无 IO、不碰 DB）。

设计：`docs/superpowers/specs/2026-10-13-abstract-tbox-import-landing-design.md` §三/§四。

三段流水：

- `parse_tbox_yaml` —— PyYAML 安全解析 + 顶层形状闸（失败一律抛 `InvalidParameterError`）；
- `map_tbox_to_schema` —— YAML dict → 抽象 schema 形状 + 告警（上限**前置检查**、未知键
  忽略、`source_types`/`target_types` 语义校验全在这里）；
- `merge_imported` —— **反序**喂 `normalize_schema`（imported 在前、existing 在后）：
  「先到者胜 + 并集」天然给出「导入方标量优先、existing 落点/别名/backings 并入保留、
  existing 独有实体保留、零落点实体保留」；
- `build_diff_summary` —— 给用户/前端的摘要（新增实体 / 更新 / 新增属性）。

**口径（写死，勿改）**：本体骨架标量的真值 = 导入的 YAML——**导入方没写 = 覆盖为空**
（含 description/display_name；`normalize_schema` 的 `_clean_str` 恒写这些键）。要改成
「有值才覆盖」就在 `merge_imported` 这一层集中改，别散到别处。
"""
from __future__ import annotations

from typing import NoReturn

import yaml

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate

from .concept_expansion import (
    MAX_ATTRIBUTES_PER_ENTITY,
    MAX_ENTITIES,
    MAX_RELATIONS,
    MAX_TERM_LENGTH,
    _ATTRIBUTE_TYPES,
    _as_list,
    _clean_str,
    normalize_schema,
)

#: YAML 文本上限（字符）——防呆闸：正常 TBox 远小于此。
IMPORT_MAX_CHARS = 2_000_000

#: 各层「已知键」——其余键一律「宽松忽略 + 告警」（与 nexonto 导入的宽容口径对称）。
_TOP_KNOWN = frozenset({"version", "domain", "entity_types", "relation_types",
                        "constraints", "disambiguation"})
_ENTITY_KNOWN = frozenset({"name", "display_name", "description", "aliases", "attributes"})
_ATTR_KNOWN = frozenset({"name", "display_name", "description", "type", "required",
                         "is_primary_key"})
_REL_KNOWN = frozenset({"name", "source", "target", "description", "cardinality",
                        "inverse_of", "source_types", "target_types"})

#: 逐条告警的封顶（超出折一条合计；照 `_append_cap_warnings` 先例）。
_MAX_WARNINGS = 12


def _cap_warnings(lines: list[str]) -> list[str]:
    """告警封顶：逐条最多 `_MAX_WARNINGS` 条，其余折一条合计（防刷屏）。"""
    if len(lines) <= _MAX_WARNINGS:
        return lines
    return [*lines[:_MAX_WARNINGS], f"另有 {len(lines) - _MAX_WARNINGS} 条提示从略"]


def _unknown_keys(raw: dict, known: frozenset) -> list[str]:
    return [str(k) for k in raw if str(k) not in known]


def _raise_invalid(err_text: str) -> NoReturn:
    raise InvalidParameterError(message=translate(
        "err.abstract_schema.import_invalid_yaml",
        params={"err": err_text},
        fallback=f"TBox YAML 无效：{err_text}",
    ))


def parse_tbox_yaml(yaml_text: str) -> dict:
    """PyYAML 安全解析 + 顶层形状闸。**失败一律抛**（1001）：导入是写操作，
    坏输入必须显式拒绝，不能带半份理解往下走。"""
    text = str(yaml_text or "")
    if not text.strip():
        _raise_invalid("文本为空")
    if len(text) > IMPORT_MAX_CHARS:
        raise InvalidParameterError(message=translate(
            "err.abstract_schema.import_too_large",
            params={"max": IMPORT_MAX_CHARS},
            fallback=f"YAML 文本过大（上限 {IMPORT_MAX_CHARS} 字符）",
        ))
    try:
        raw = yaml.safe_load(text)
    # 深度嵌套/递归展开达到解释器栈上限时 PyYAML 抛 RecursionError，
    # 同属「坏输入」（不能逃逸成 500）。
    except (yaml.YAMLError, RecursionError) as exc:
        _raise_invalid(f"解析失败（{str(exc)[:200]}）")
    if not isinstance(raw, dict):
        _raise_invalid("顶层必须是对象")
    return raw


def _endpoint_types(raw_types, single: str, known: set[str], *,
                    rel: str, field: str, warnings: list[str]) -> list[str] | None:
    """`source_types` / `target_types` 的语义校验（对齐编译侧 `_normalize_relation_types`
    口径、取**宽容档**——不整份拒绝）：

    - 非 list → 未设置（None）；未知实体名 → 剔除 + 告警；重复/空串 → 去重保序丢弃；
    - 过滤后为空 → 未设置；
    - **集合必含 source/target**：不含 → 整键忽略 + 告警；
    - **单值 = 未设置**：仅剩一个（必是 single 自身）→ 不落该键。
    """
    if not isinstance(raw_types, list):
        return None
    kept: list[str] = []
    for item in raw_types:
        t = _clean_str(item)
        if not t:
            continue
        if t not in known:
            warnings.append(f"关系 {rel} 的 {field} 含未知实体 {t}，已剔除")
            continue
        if t not in kept:
            kept.append(t)
    if not kept:
        return None
    if single not in kept:
        warnings.append(f"关系 {rel} 的 {field} 不含 {single}（须含 source/target 单值），整键忽略")
        return None
    if len(kept) <= 1:
        return None
    return kept


def _map_attributes(raw, entity_name: str, warnings: list[str]) -> list[dict]:
    """实体的 attributes 段 → 属性 dict 列表（`is_primary_key` → `primary_key`）。"""
    if raw is not None and not isinstance(raw, list):
        warnings.append(f"实体 {entity_name} 的 attributes 不是列表，已忽略")
        return []
    raw = raw if isinstance(raw, list) else []
    if len(raw) > MAX_ATTRIBUTES_PER_ENTITY:
        raise InvalidParameterError(message=translate(
            "err.abstract_schema.import_over_limit",
            params={"what": f"实体 {entity_name} 的属性",
                    "count": len(raw), "max": MAX_ATTRIBUTES_PER_ENTITY},
            fallback=f"实体 {entity_name} 的属性数量 {len(raw)} 超出上限 "
                     f"{MAX_ATTRIBUTES_PER_ENTITY}；请精简后再导入",
        ))
    out: list[dict] = []
    for j, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"{entity_name}.attributes[{j}] 不是对象，已跳过")
            continue
        name = _clean_str(item.get("name"))
        if not name or len(name) > MAX_TERM_LENGTH:
            warnings.append(f"{entity_name}.attributes[{j}] 的名称缺失或超长，已跳过")
            continue
        for k in _unknown_keys(item, _ATTR_KNOWN):
            warnings.append(f"{entity_name}.{name} 的未知键 {k} 已忽略")
        typ = _clean_str(item.get("type"))
        if typ and typ not in _ATTRIBUTE_TYPES:
            warnings.append(f"{entity_name}.{name} 的类型 {typ} 不在词表，按 string 处理")
        attr: dict = {
            "name": name,
            "type": typ if typ in _ATTRIBUTE_TYPES else "string",
            "required": item.get("required") is True,
            "primary_key": item.get("is_primary_key") is True,
        }
        if _clean_str(item.get("description")):
            attr["description"] = _clean_str(item.get("description"))
        if _clean_str(item.get("display_name")):
            attr["display_name"] = _clean_str(item.get("display_name"))
        out.append(attr)
    return out


def map_tbox_to_schema(raw: dict) -> tuple[dict, list[str], list[str]]:
    """YAML dict → `(抽象 schema 形状, warnings, ignored_sections)`。

    上限**前置检查**（不靠 normalize 静默截断，spec §八）：实体数/关系数/单实体属性数
    超限 → 抛 1001（拒绝导入）；名字缺失/超长条目 → 丢弃 + 告警。
    `constraints` / `disambiguation` 无对应段 → 忽略并进 `ignored_sections`。
    """
    warnings: list[str] = []
    ignored: list[str] = []
    for k in _unknown_keys(raw, _TOP_KNOWN):
        warnings.append(f"顶层未知键 {k} 已忽略")
    for section in ("constraints", "disambiguation"):
        if section in raw:
            value = raw.get(section)
            n = len(value) if isinstance(value, list) else 1
            ignored.append(f"{section}({n})" if isinstance(value, list) else section)

    raw_entities = raw.get("entity_types")
    if raw_entities is not None and not isinstance(raw_entities, list):
        _raise_invalid("entity_types 必须是列表")
    raw_entities = raw_entities if isinstance(raw_entities, list) else []
    if len(raw_entities) > MAX_ENTITIES:
        raise InvalidParameterError(message=translate(
            "err.abstract_schema.import_over_limit",
            params={"what": "实体", "count": len(raw_entities), "max": MAX_ENTITIES},
            fallback=f"实体数量 {len(raw_entities)} 超出上限 {MAX_ENTITIES}；请精简后再导入",
        ))

    entities: list[dict] = []
    names: set[str] = set()
    for i, item in enumerate(raw_entities):
        if not isinstance(item, dict):
            warnings.append(f"entity_types[{i}] 不是对象，已跳过")
            continue
        name = _clean_str(item.get("name"))
        if not name or len(name) > MAX_TERM_LENGTH:
            warnings.append(f"entity_types[{i}] 的名称缺失或超长，已跳过")
            continue
        if name in names:
            warnings.append(f"实体 {name} 重复出现（同名合并）")
        names.add(name)
        for k in _unknown_keys(item, _ENTITY_KNOWN):
            warnings.append(f"实体 {name} 的未知键 {k} 已忽略")
        ent: dict = {
            "name": name,
            "aliases": [str(a).strip() for a in _as_list(item.get("aliases"))
                        if isinstance(a, str) and str(a).strip()],
            "attributes": _map_attributes(item.get("attributes"), name, warnings),
        }
        if _clean_str(item.get("description")):
            ent["description"] = _clean_str(item.get("description"))
        if _clean_str(item.get("display_name")) and _clean_str(item.get("display_name")) != name:
            ent["display_name"] = _clean_str(item.get("display_name"))
        entities.append(ent)

    raw_relations = raw.get("relation_types")
    if raw_relations is not None and not isinstance(raw_relations, list):
        _raise_invalid("relation_types 必须是列表")
    raw_relations = raw_relations if isinstance(raw_relations, list) else []
    if len(raw_relations) > MAX_RELATIONS:
        raise InvalidParameterError(message=translate(
            "err.abstract_schema.import_over_limit",
            params={"what": "关系", "count": len(raw_relations), "max": MAX_RELATIONS},
            fallback=f"关系数量 {len(raw_relations)} 超出上限 {MAX_RELATIONS}；请精简后再导入",
        ))

    relations: list[dict] = []
    for i, item in enumerate(raw_relations):
        if not isinstance(item, dict):
            warnings.append(f"relation_types[{i}] 不是对象，已跳过")
            continue
        name = _clean_str(item.get("name"))
        if not name or len(name) > MAX_TERM_LENGTH:
            warnings.append(f"relation_types[{i}] 的名称缺失或超长，已跳过")
            continue
        source, target = _clean_str(item.get("source")), _clean_str(item.get("target"))
        if not source or not target:
            warnings.append(f"关系 {name} 缺 source/target，已跳过")
            continue
        # 端点是否存在于实体集合：**不在这里判**（本函数只见 YAML；引用 existing
        # 实体的关系是合法场景）。normalize 会丢真正悬空的关系，由 `merge_imported`
        # 在「YAML ∪ existing」口径下补告警——normalize 本身无告警通道。
        for k in _unknown_keys(item, _REL_KNOWN):
            warnings.append(f"关系 {name} 的未知键 {k} 已忽略")
        rel: dict = {"name": name, "source": source, "target": target}
        if _clean_str(item.get("description")):
            rel["description"] = _clean_str(item.get("description"))
        if _clean_str(item.get("cardinality")):
            rel["cardinality"] = _clean_str(item.get("cardinality"))
        if _clean_str(item.get("inverse_of")):
            rel["inverse_of"] = _clean_str(item.get("inverse_of"))
        st = _endpoint_types(item.get("source_types"), source, names,
                             rel=name, field="source_types", warnings=warnings)
        if st:
            rel["source_types"] = st
        tt = _endpoint_types(item.get("target_types"), target, names,
                             rel=name, field="target_types", warnings=warnings)
        if tt:
            rel["target_types"] = tt
        relations.append(rel)

    return {"entities": entities, "relations": relations}, _cap_warnings(warnings), ignored


def _check_merged_limits(imported: dict, existing: dict) -> None:
    """合并上限预检（spec §八：**不靠 normalize 静默截断**）。

    反序合并里 imported 在前——normalize 的 `>= MAX` 截断会把**排在后面的
    existing 条目**静默丢掉（实体/关系/属性三处同构），那等于「导入静默删已有
    数据」，违反「只增不删」。所以在这里按**并集口径**先算一遍：超限 → 抛 1001
    （与 map 侧单侧上限同一档：拒绝导入，让用户先清理，而不是静默丢）。
    计量口径：实体/关系按名字并集（= 合并后条数）；属性按「实体名下属性名并集」。
    """
    ent_names: set[str] = set()
    attr_names: dict[str, set[str]] = {}
    for side in (imported, existing):
        for ent in side.get("entities") or []:
            if not isinstance(ent, dict):
                continue
            name = _clean_str(ent.get("name"))
            if not name:
                continue
            ent_names.add(name)
            bucket = attr_names.setdefault(name, set())
            for a in ent.get("attributes") or []:
                if isinstance(a, dict) and _clean_str(a.get("name")):
                    bucket.add(_clean_str(a.get("name")))
    if len(ent_names) > MAX_ENTITIES:
        raise InvalidParameterError(message=translate(
            "err.abstract_schema.import_over_limit",
            params={"what": "实体（含已有）", "count": len(ent_names), "max": MAX_ENTITIES},
            fallback=f"实体数量（含已有）{len(ent_names)} 超出上限 {MAX_ENTITIES}；请精简后再导入",
        ))
    for name, attrs in attr_names.items():
        if len(attrs) > MAX_ATTRIBUTES_PER_ENTITY:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.import_over_limit",
                params={"what": f"实体 {name} 的属性（含已有）",
                        "count": len(attrs), "max": MAX_ATTRIBUTES_PER_ENTITY},
                fallback=f"实体 {name} 的属性数量（含已有）{len(attrs)} 超出上限 "
                         f"{MAX_ATTRIBUTES_PER_ENTITY}；请精简后再导入",
            ))
    rel_names: set[str] = set()
    for side in (imported, existing):
        for r in side.get("relations") or []:
            if isinstance(r, dict) and _clean_str(r.get("name")):
                rel_names.add(_clean_str(r.get("name")))
    if len(rel_names) > MAX_RELATIONS:
        raise InvalidParameterError(message=translate(
            "err.abstract_schema.import_over_limit",
            params={"what": "关系（含已合并）", "count": len(rel_names), "max": MAX_RELATIONS},
            fallback=f"关系数量（含已有）{len(rel_names)} 超出上限 {MAX_RELATIONS}；请精简后再导入",
        ))


def merge_imported(existing: dict, imported: dict) -> tuple[dict, list[str]]:
    """**反序**合并（spec §3.3 的实现取巧）：imported 在前、existing 在后，一起过
    `normalize_schema`——「先到者胜 + 并集」语义天然给出：

    - 导入方标量优先（description/display_name；**导入方没写 → 覆盖为空**，
      即「本体骨架的真值 = nexonto 导出的 YAML」）；
    - existing 的落点 / 别名 / 属性来源 / backings 并入保留；existing 独有实体保留；
    - 零落点实体保留；同名关系端点冲突取导入方端点；
    - `metrics` 段原样保留（导入不碰人工登记物）。

    返回 `(merged, warnings)`：warnings 把 normalize 的**静默丢弃**显形——
    导入方关系两端（并入 existing 后仍）不在实体集里 → 整条被丢，补一条告警
    （过 `_cap_warnings` 封顶，与 map 侧同一口径）。
    """
    _check_merged_limits(imported, existing)
    merged = normalize_schema({
        "entities": [*(imported.get("entities") or []), *(existing.get("entities") or [])],
        "relations": [*(imported.get("relations") or []), *(existing.get("relations") or [])],
        "metrics": [*(existing.get("metrics") or [])],
    })
    known = {str(e.get("name") or "") for e in merged.get("entities") or []}
    warnings: list[str] = []
    for r in imported.get("relations") or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("source") or "") not in known or str(r.get("target") or "") not in known:
            warnings.append(
                f"关系 {r.get('name')} 的端点不在实体集里（含已有实体），已跳过")
    return merged, _cap_warnings(warnings)


def build_diff_summary(existing: dict, imported: dict) -> dict:
    """「这次导入会带来什么」（spec §3.3 的 diff 摘要）。判据取导入方视角：

    - `entities_added`：导入的实体名不在现有里；
    - `entities_updated`：同名且 description/display_name **标量有变**（含「导入方为空 →
      覆盖为空」，与反序合并的最终语义一致；别名并集与属性新增不计入这里）；
    - `attributes_added`：导入方带来的属性总数——**新实体自带的 + 同名实体的新增**
      （同名实体的新增 = 导入的属性名不在现有属性集里的条数）。
    """
    old_entities: dict[str, dict] = {}
    for e in existing.get("entities") or []:
        if isinstance(e, dict) and _clean_str(e.get("name")):
            old_entities.setdefault(_clean_str(e.get("name")), e)
    added = updated = attrs_added = 0
    for e in imported.get("entities") or []:
        if not isinstance(e, dict):
            continue
        name = _clean_str(e.get("name"))
        if not name:
            continue
        old = old_entities.get(name)
        if old is None:
            added += 1
            attrs_added += sum(
                1 for a in e.get("attributes") or []
                if isinstance(a, dict) and _clean_str(a.get("name"))
            )
            continue
        for key in ("description", "display_name"):
            if _clean_str(old.get(key)) != _clean_str(e.get(key)):
                updated += 1
                break
        old_attrs = {_clean_str(a.get("name")) for a in old.get("attributes") or []
                     if isinstance(a, dict)}
        attrs_added += sum(
            1 for a in e.get("attributes") or []
            if isinstance(a, dict) and _clean_str(a.get("name")) not in old_attrs
        )
    return {"entities_added": added, "entities_updated": updated,
            "attributes_added": attrs_added}


__all__ = [
    "IMPORT_MAX_CHARS",
    "build_diff_summary",
    "map_tbox_to_schema",
    "merge_imported",
    "parse_tbox_yaml",
]
