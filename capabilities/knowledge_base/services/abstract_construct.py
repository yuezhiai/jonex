#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""「agent 自动构造」的确定性管线（纯函数为主 + 名字档运行器）。

语义出处：`docs/superpowers/specs/2026-10-03-abstract-schema-agent-construct-design.md` §四。
三条铁律：

1. **本模块不碰库、不调模型、不改输入**：产出 raw 形状交给调用方拼进
   `normalize_schema` 收口（它是形状与上限的唯一出处）。
2. **补齐式、只增不删**：调用方把现有草稿排在 raw **前面**喂 normalize
   （同名合并先到者胜）——本模块只负责「从源里造出缺的那部分」。
3. **确定性优先**：同样的输入必须产出同样的输出（无随机、无时间）；
   唯一带时钟的是 `run_name_stage` 的预算（可注入 `now` 便于测试）。
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Optional

from .evidence_probe import evidence_key

#: 枚举取样判据：样本 distinct 超过它就不当「取值域小」（与 `nl2sql/evidence.pick_samples`
#: 的 `MAX_DISTINCT_FOR_SAMPLES` 同口径）。
ENUM_MAX_DISTINCT = 10

#: 每个属性的 enum_values 上限（证据里每列最多存 5 个样本，所以实际 ≤5）。
ENUM_MAX_VALUES = 10


def _as_list(value) -> list:
    """「本该是 list」的容器字段收敛成 list，绝不抛（输入是编译 schema 的 JSONB）。"""
    return value if isinstance(value, list) else []


def build_source_bundles(rows, ds_names: dict) -> dict[str, dict]:
    """compiled 行 → `{ds_id: {"name", "entities", "relations", "evidence"}}`。

    - 只收形状正常的实体/关系（一个坏元素不该毁掉整次构造）；
      同名实体**先到者胜**（行内不应有重名；真出现时保持确定性而不是让后者覆盖）。
    - `evidence` 非 dict 一律当空（它是可选增益）。
    """
    bundles: dict[str, dict] = {}
    for row in rows or []:
        ds_id = getattr(row, "data_source_id", None)
        if not ds_id:
            continue
        entities: dict[str, dict] = {}
        for e in _as_list(getattr(row, "entity_types", None)):
            if not isinstance(e, dict):
                continue
            name = str(e.get("name") or "").strip()
            if name and name not in entities:
                entities[name] = e
        relations = [
            r for r in _as_list(getattr(row, "relation_types", None))
            if isinstance(r, dict)
        ]
        evidence = getattr(row, "evidence", None)
        bundles[ds_id] = {
            "name": ds_names.get(ds_id, ""),
            "entities": entities,
            "relations": relations,
            "evidence": evidence if isinstance(evidence, dict) else {},
        }
    return bundles


def build_landing_map(abstract_entities) -> dict[tuple[str, str], str]:
    """`(ds_id, 源实体名) -> 抽象实体名`（**只认 `included` 落点**；重复落点先到者胜）。

    「先到」= 抽象实体数组顺序：一个落点理论上只属于一个实体，真出现重复时
    取先到的，保证确定性。
    """
    out: dict[tuple[str, str], str] = {}
    for ent in _as_list(abstract_entities):
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        for s in _as_list(ent.get("sources")):
            if not isinstance(s, dict) or s.get("included") is False:
                continue
            ds = str(s.get("data_source_id") or "").strip()
            src = str(s.get("entity_name") or "").strip()
            if ds and src:
                out.setdefault((ds, src), name)
    return out


def _enum_values_from_evidence(evidence: dict, source_entity: dict, column: str) -> list[str]:
    """证据里该列的样本 → `enum_values`（只在「取值域小」时给）。

    - 表键走 `evidence_key(source_entity)`（与写入侧共用同一份归一化）；
    - `distinct_in_sample` 缺失/为 0/超过 `ENUM_MAX_DISTINCT` → 不给；
    - 列名先精确匹配、再不区分大小写兜底（两边都从同一库来，但编译期可能改过大小写）。
    """
    table_key = evidence_key(source_entity)
    entry = ((evidence or {}).get("tables") or {}).get(table_key) or {}
    ev_columns = entry.get("columns") or {}
    col = ev_columns.get(column)
    if col is None:
        lowered = column.casefold()
        for key, value in ev_columns.items():
            if str(key).casefold() == lowered:
                col = value
                break
    if not isinstance(col, dict):
        return []
    try:
        distinct = int(col.get("distinct_in_sample") or 0)
    except (TypeError, ValueError):
        return []
    if distinct <= 0 or distinct > ENUM_MAX_DISTINCT:
        return []
    samples = [str(v) for v in (col.get("samples") or []) if str(v).strip()]
    return samples[:ENUM_MAX_VALUES]


def merged_attributes_for_entity(entity: dict, bundles: dict) -> list[dict]:
    """一个抽象实体的「落点合并属性」（raw 形状，供 `normalize_schema` 收口）。

    - 遍历 **included** 落点（落点源缺失/实体不在源里 → 跳过该落点）；
    - 按属性名去重（大小写不敏感；**先到先得** = 落点数组顺序 × 源内属性顺序）；
    - 标量逐字段先到先得；`sources` 三键并集去重（attribute_name 记**该源里**的真实列名）；
    - `enum_values`：只在首次收编该属性名时按证据给（后到来源不覆盖）。
    """
    out: list[dict] = []
    by_key: dict[str, dict] = {}
    for s in _as_list(entity.get("sources")):
        if not isinstance(s, dict) or s.get("included") is False:
            continue
        ds = str(s.get("data_source_id") or "").strip()
        src_name = str(s.get("entity_name") or "").strip()
        bundle = bundles.get(ds)
        if not bundle or not src_name:
            continue
        source_entity = bundle["entities"].get(src_name)
        if not isinstance(source_entity, dict):
            continue
        for raw in _as_list(source_entity.get("attributes")):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            triple = {"data_source_id": ds, "entity_name": src_name, "attribute_name": name}
            current = by_key.get(key)
            if current is None:
                item: dict = {"name": name}
                for field in ("type", "description", "display_name"):
                    value = str(raw.get(field) or "").strip()
                    if value:
                        item[field] = value
                item["primary_key"] = (
                    raw.get("is_primary_key") is True or raw.get("primary_key") is True
                )
                item["required"] = raw.get("required") is True
                enum_values = _enum_values_from_evidence(
                    bundle.get("evidence") or {}, source_entity, name,
                )
                if enum_values:
                    item["enum_values"] = enum_values
                item["sources"] = [triple]
                by_key[key] = item
                out.append(item)
            else:
                seen = {
                    (t["data_source_id"], t["entity_name"], t["attribute_name"])
                    for t in current["sources"]
                }
                if (ds, src_name, name) not in seen:
                    current["sources"].append(triple)
    return out


def lifted_relations(bundles: dict, landing_map: dict,
                     *, reserved=None) -> tuple[list[dict], dict]:
    """源关系 → 抽象关系 raw（背书照搬：ds / 关系名 / 两列 / origin）。

    - 只提升「两端都能映射到抽象实体」的关系（`landing_map` 只含 included 落点，
      所以「该 ds 落点被 excluded」在这里天然跳过）；映射不上跳过并计数。
    - **抽象关系身份 = (source, target, name)**，合并交给调用方的 `normalize_schema`
      （同名同端点 → 背书并集 + origin 信任序）。
    - `reserved`（= 现有草稿关系）：同名但端点不同 → **提升方改名**（`_r` 后缀循环，
      确定性）+ 计数；绝不改现有内容。
    """
    stats = {"lifted": 0, "unmapped": 0, "renamed": 0}
    claims: dict[str, tuple[str, str]] = {}
    for rel in _as_list(reserved):
        if not isinstance(rel, dict):
            continue
        name = str(rel.get("name") or "").strip()
        source = str(rel.get("source") or "").strip()
        target = str(rel.get("target") or "").strip()
        if name and source and target:
            claims[name] = (source, target)

    out: list[dict] = []
    for ds_id, bundle in (bundles or {}).items():
        for rel in bundle.get("relations") or []:
            src = str(rel.get("source") or "").strip()
            tgt = str(rel.get("target") or "").strip()
            name = str(rel.get("name") or "").strip()
            if not (src and tgt and name):
                stats["unmapped"] += 1
                continue
            abs_src = (landing_map or {}).get((ds_id, src))
            abs_tgt = (landing_map or {}).get((ds_id, tgt))
            if not (abs_src and abs_tgt):
                stats["unmapped"] += 1
                continue
            while name in claims and claims[name] != (abs_src, abs_tgt):
                stats["renamed"] += 1
                name = f"{name}_r"
            claims[name] = (abs_src, abs_tgt)
            item: dict = {
                "name": name,
                "source": abs_src,
                "target": abs_tgt,
                "backings": [{
                    "data_source_id": ds_id,
                    "relation_name": str(rel.get("name") or "").strip(),
                    "source_column": str(rel.get("source_column") or "").strip() or None,
                    "target_column": str(rel.get("target_column") or "").strip() or None,
                    "origin": str(rel.get("origin") or "").strip() or None,
                }],
            }
            description = str(rel.get("description") or "").strip()
            if description:
                item["description"] = description
            cardinality = str(rel.get("cardinality") or "").strip()
            if cardinality:
                item["cardinality"] = cardinality
            out.append(item)
            stats["lifted"] += 1
    return out, stats


#: 跨源补边候选上限（名字相容档排序后截断；超出的候选没有确定性依据，宁可不出）。
MAX_EDGE_CANDIDATES = 20

#: 值包含判据：样本 distinct 下限与包含率门槛（P2-3 口径：样本不足 = 不可测，不是否决）。
EDGE_MIN_DISTINCT = 10
EDGE_MIN_CONTAINMENT = 0.8

#: 合成关系名的字符上限（与 `concept_expansion.MAX_TERM_LENGTH` 同值）。
MAX_RELATION_NAME_CHARS = 128

#: 类型族：跨源值包含只要求同族（文本域 / 数值域 / 时间域 / 布尔）。
_TYPE_FAMILY = {
    "string": "text", "text": "text", "enum": "text",
    "number": "numeric", "integer": "numeric",
    "date": "temporal", "datetime": "temporal",
    "boolean": "bool",
}


def _family(attr_type) -> str:
    return _TYPE_FAMILY.get(str(attr_type or "").strip().casefold(), "text")


def column_pair_kind(left_col: str, right_col: str,
                     left_entity: str = "", right_entity: str = "") -> str | None:
    """列名相容判据（大小写不敏感）。返回 None = 不相容。实体名传**源实体名**。

    - `"same"`：同名；
    - `"id_suffix"`：一侧是另一侧 + `_id`（`company_id` ↔ `company`）；
    - `"entity_fk"`：一侧是 `<对方源实体名>_id`（`Orders` 的 `company_id` ↔ `Company` 的任意列）
      —— 外键指向对侧实体表的**最常见形态**（`orders.company_id → company.id` 里
      被引用列就叫 `id`，纯列名模式永远匹配不上，必须借实体名）。
    """
    l, r = left_col.casefold(), right_col.casefold()
    if l == r:
        return "same"
    if l.endswith("_id") and l[:-3] == r:
        return "id_suffix"
    if r.endswith("_id") and r[:-3] == l:
        return "id_suffix"
    left_ent, right_ent = left_entity.casefold(), right_entity.casefold()
    if l.endswith("_id") and left_ent and l[:-3] == right_ent:
        return "entity_fk"
    if r.endswith("_id") and right_ent and r[:-3] == left_ent:
        return "entity_fk"
    return None


def _landing_pairs(entity, bundles):
    """实体的 included 落点 → 可用的 (ds, 源实体 dict) 列表（源/实体缺失跳过）。"""
    out = []
    for s in _as_list(entity.get("sources")):
        if not isinstance(s, dict) or s.get("included") is False:
            continue
        ds = str(s.get("data_source_id") or "").strip()
        src = str(s.get("entity_name") or "").strip()
        bundle = bundles.get(ds)
        if not bundle or not src:
            continue
        source_entity = bundle["entities"].get(src)
        if isinstance(source_entity, dict):
            out.append((ds, src, source_entity))
    return out


def cross_source_candidates(abstract_entities, abstract_relations, bundles: dict,
                            landing_map: dict, *, max_pairs: int = MAX_EDGE_CANDIDATES) -> list[dict]:
    """跨源实体对的列级候选（待证据验证）。

    - 枚举抽象实体对（i<j 保证唯一）：E1 的 included 落点（ds A）× E2 的（ds B），
      A≠B（跨源）、且两者间**无现存关系**（任一方向）；
    - 列对：列名相容（`column_pair_kind`，含 `entity_fk` 档）+ 类型同族；每对实体取
      **最优一档**（entity_fk > id_suffix > same，其次列名字符更长者）；
    - 排序截断 `max_pairs`（确定性）；候选自带探针表键（`evidence_key` 口径）。
    """
    linked: set[frozenset] = set()
    for rel in _as_list(abstract_relations):
        if isinstance(rel, dict):
            s = str(rel.get("source") or "").strip()
            t = str(rel.get("target") or "").strip()
            if s and t and s != t:
                linked.add(frozenset((s, t)))

    ents = [
        e for e in _as_list(abstract_entities)
        if isinstance(e, dict) and str(e.get("name") or "").strip()
    ]
    picked: list[dict] = []
    for i, e1 in enumerate(ents):
        n1 = str(e1["name"]).strip()
        for e2 in ents[i + 1:]:
            n2 = str(e2["name"]).strip()
            if n1 == n2 or frozenset((n1, n2)) in linked:
                continue
            best: dict | None = None
            for ds1, src1, se1 in _landing_pairs(e1, bundles):
                for ds2, src2, se2 in _landing_pairs(e2, bundles):
                    if ds1 == ds2:
                        continue  # 同源不算跨源
                    for a1 in _as_list(se1.get("attributes")):
                        if not isinstance(a1, dict):
                            continue
                        name1 = str(a1.get("name") or "").strip()
                        if not name1:
                            continue
                        for a2 in _as_list(se2.get("attributes")):
                            if not isinstance(a2, dict):
                                continue
                            name2 = str(a2.get("name") or "").strip()
                            if not name2:
                                continue
                            kind = column_pair_kind(name1, name2, src1, src2)
                            if kind is None or _family(a1.get("type")) != _family(a2.get("type")):
                                continue
                            cand = {
                                "left": {"ds": ds1, "table": evidence_key(se1), "entity": src1,
                                         "abstract": n1, "column": name1, "type": str(a1.get("type") or "")},
                                "right": {"ds": ds2, "table": evidence_key(se2), "entity": src2,
                                          "abstract": n2, "column": name2, "type": str(a2.get("type") or "")},
                                "kind": kind,
                            }
                            if best is None or _candidate_rank(cand) < _candidate_rank(best):
                                best = cand
            if best is not None:
                picked.append(best)
    picked.sort(key=lambda c: (
        _candidate_rank(c),
        c["left"]["abstract"], c["right"]["abstract"],
        c["left"]["column"], c["right"]["column"],
    ))
    return picked[:max_pairs]


#: 候选相容档的排序权重：entity_fk（外键直指对方表）> id_suffix > same。
_KIND_RANK = {"entity_fk": 0, "id_suffix": 1, "same": 2}


def _candidate_rank(cand: dict) -> tuple:
    """候选排序键：相容档优先，其次列名字符更长者（信息量更大）。"""
    base = max(len(cand["left"]["column"]), len(cand["right"]["column"]))
    return (_KIND_RANK.get(cand["kind"], 3), -base)


def score_containment(left_values, right_values) -> dict:
    """样本值包含率（左 ⊆ 右）。**样本不足判 `testable=False`**，不是否决。

    P2-3 的教训原样搬：0/0 采样 ≠ 没关联（首跑 44 条假 veto）；只有「测得动且
    比率不够」才算没通过。
    """
    left = {str(v) for v in (left_values or []) if str(v).strip()}
    right = {str(v) for v in (right_values or []) if str(v).strip()}
    info = {"n_left": len(left), "n_right": len(right)}
    if len(left) < EDGE_MIN_DISTINCT or len(right) < EDGE_MIN_DISTINCT:
        return {**info, "testable": False, "ratio": None, "passed": False}
    ratio = round(len(left & right) / len(left), 4)
    return {**info, "testable": True, "ratio": ratio, "passed": ratio >= EDGE_MIN_CONTAINMENT}


def build_edge_suggestion(candidate: dict, left_score: dict, right_score: dict) -> dict | None:
    """候选 + 双向评分 → raw 抽象关系（`backings: []` = 逻辑关系；描述带证据口径）。

    方向：外键侧值 ⊆ 主键侧值 ⇒ 关系从外键侧指向主键侧。双向都过（同域）：
    取 distinct 少的一侧当外键侧（更像多值侧）。都不够 → None（调用方记账）。
    """
    if left_score["passed"] and right_score["passed"]:
        prefer_left = left_score["n_left"] <= left_score["n_right"]
    elif left_score["passed"]:
        prefer_left = True
    elif right_score["passed"]:
        prefer_left = False
    else:
        return None
    if prefer_left:
        src, tgt, score = candidate["left"], candidate["right"], left_score
    else:
        src, tgt, score = candidate["right"], candidate["left"], right_score
    name = f"{src['abstract']}_{tgt['abstract']}"[:MAX_RELATION_NAME_CHARS]
    return {
        "name": name,
        "source": src["abstract"],
        "target": tgt["abstract"],
        "description": (
            f"【跨源建议】{src['ds']}.{src['table']}.{src['column']} 的样本值被 "
            f"{tgt['ds']}.{tgt['table']}.{tgt['column']} 包含"
            f"（样本包含率 {score['ratio']:.0%}，n={score['n_left']}/{score['n_right']}）。"
            "保留或删除由人工核对决定。"
        ),
        "backings": [],
    }


async def collect_edge_scores(candidates, probe, *, budget_s: float, max_queries: int,
                              now: Callable[[], float] | None = None) -> tuple[list[dict], dict]:
    """对候选两侧做定向探针并算双向评分。`probe(ds_id, table, column) -> list[str] | None`。

    - 每列最多探一次（跨候选缓存）；
    - 超过次数上限或时间预算 → 剩余候选记 `skipped`（丢弃，不硬猜）；
    - 单列探失败（异常或 None）→ 该侧样本为 None → 评分判「不可测」，候选照常返回
      （让上层如实计数），不抛。
    - `now` 只为测试注入时钟；生产传 None（取 `time.monotonic`）。
    """
    import time

    clock = now or time.monotonic
    started = clock()
    cache: dict[tuple, Optional[list]] = {}
    stats = {"queries": 0, "failed": 0, "skipped": 0}
    rows: list[dict] = []

    for cand in candidates or []:
        side_values: dict[str, Optional[list]] = {}
        exhausted = False
        for side_key in ("left", "right"):
            side = cand[side_key]
            key = (side["ds"], side["table"], side["column"])
            if key in cache:
                side_values[side_key] = cache[key]
                continue
            if stats["queries"] >= max_queries or (clock() - started) >= budget_s:
                exhausted = True
                break
            stats["queries"] += 1
            try:
                values = await probe(side["ds"], side["table"], side["column"])
            except Exception:  # noqa: BLE001 —— 单列探失败 = 该候选不可测，不是否决
                values = None
                stats["failed"] += 1
            else:
                # None 返回也是失败信号（docstring：异常**或 None**）——探针闭包把
                # 「源不可用 / 单列失败」都收敛成 None，不在这里计数就等于没记。
                # 放在 else 分支：异常路径已经记过一次，不重复计。
                if values is None:
                    stats["failed"] += 1
            cache[key] = values
            side_values[side_key] = values
        if exhausted:
            stats["skipped"] += 1
            continue
        left_values = side_values.get("left")
        right_values = side_values.get("right")
        rows.append({**cand,
                     "left_score": score_containment(left_values, right_values),
                     "right_score": score_containment(right_values, left_values)})
    return rows, stats


#: 名字档批大小（每批实体数）。
NAME_BATCH_ENTITIES = 10

#: 中文名长度上限（字符）。超过的补丁直接丢（模型偶尔会写一句解释进来）。
NAME_MAX_CHARS = 32


def needs_display_name(attr: dict) -> bool:
    """该属性要不要中文名：`display_name` 空 / 等于 name（大小写不敏感）/ 不含 CJK。"""
    if not isinstance(attr, dict):
        return False
    display = str(attr.get("display_name") or "").strip()
    name = str(attr.get("name") or "").strip()
    if not display or display.casefold() == name.casefold():
        return True
    return not any("一" <= ch <= "鿿" for ch in display)


def build_name_batches(entities, *, batch_size: int = NAME_BATCH_ENTITIES) -> list[list[dict]]:
    """按实体分批构造名字档载荷；只收「有缺名属性」的实体（全有名的不进批）。"""
    groups: list[dict] = []
    for ent in _as_list(entities):
        if not isinstance(ent, dict):
            continue
        attrs = [
            a for a in _as_list(ent.get("attributes"))
            if isinstance(a, dict) and needs_display_name(a)
        ]
        if not attrs:
            continue
        groups.append({
            "entity_name": str(ent.get("name") or ""),
            "entity_display_name": str(ent.get("display_name") or ""),
            "entity_description": str(ent.get("description") or ""),
            "attributes": [
                {
                    "name": str(a.get("name") or ""),
                    "type": str(a.get("type") or ""),
                    "description": str(a.get("description") or ""),
                }
                for a in attrs
            ],
        })
    return [groups[i:i + batch_size] for i in range(0, len(groups), batch_size)]


def apply_name_patches(entities, patches) -> dict:
    """把 LLM 名字补丁打到实体上（**原地修改**；只填「缺中文名」的属性）。

    校验（任一不过 → 丢弃计数 `rejected`）：
    - `entity_name` / `attribute_name` 必须命中（大小写不敏感）；
    - `display_name` 非空、含 CJK、≤ `NAME_MAX_CHARS`；
    - 目标属性**当前确实缺中文名**（防把人工已填的盖掉）。
    """
    stats = {"patched": 0, "rejected": 0}
    index: dict[str, dict[str, dict]] = {}
    for ent in _as_list(entities):
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        attrs = {
            str(a.get("name") or "").strip().casefold(): a
            for a in _as_list(ent.get("attributes"))
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        }
        index.setdefault(name.casefold(), attrs)
    for patch in _as_list(patches):
        if not isinstance(patch, dict):
            stats["rejected"] += 1
            continue
        ent_key = str(patch.get("entity_name") or "").strip().casefold()
        attr_key = str(patch.get("attribute_name") or "").strip().casefold()
        display = str(patch.get("display_name") or "").strip()
        attr = (index.get(ent_key) or {}).get(attr_key)
        ok = (
            attr is not None
            and display
            and len(display) <= NAME_MAX_CHARS
            and any("一" <= ch <= "鿿" for ch in display)
            and needs_display_name(attr)
        )
        if not ok:
            stats["rejected"] += 1
            continue
        attr["display_name"] = display
        stats["patched"] += 1
    return stats


async def run_name_stage(entities, llm_call, *, budget_s: float, concurrency: int,
                         call_timeout_s: float = 60.0,
                         batch_size: int = NAME_BATCH_ENTITIES,
                         now: Callable[[], float] | None = None) -> dict:
    """并发跑名字档：分批调 `llm_call(batch)`，预算/并发有界，补丁即时应用（原地）。

    - 到预算（`now() - start >= budget_s`）→ 不再起新批，剩余记 `batches_skipped`；
    - 单批异常/超时 → 记首条 `error`，继续其余批（**降级不抛**：名字是增益）；
    - `batch_size` 透传给 `build_name_batches`（测试用小批次钉预算行为）。
    - 返回 `{"patched", "rejected", "batches_done", "batches_skipped", "error"}`。
    """
    import time

    clock = now or time.monotonic
    started = clock()
    batches = build_name_batches(entities, batch_size=batch_size)
    stats: dict[str, Any] = {"patched": 0, "rejected": 0, "batches_done": 0,
                             "batches_skipped": 0, "error": None}
    if not batches:
        return stats

    pending = list(batches)
    inflight: dict[asyncio.Task, None] = {}

    def _budget_left() -> bool:
        return (clock() - started) < budget_s

    while pending or inflight:
        while pending and len(inflight) < max(1, concurrency) and _budget_left():
            task = asyncio.create_task(
                asyncio.wait_for(llm_call(pending.pop(0)), timeout=call_timeout_s),
            )
            inflight[task] = None
        if not inflight:
            break
        done, _ = await asyncio.wait(set(inflight), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            inflight.pop(task, None)
            try:
                patches = task.result()
                result = apply_name_patches(entities, patches if isinstance(patches, list) else [])
                stats["patched"] += result["patched"]
                stats["rejected"] += result["rejected"]
            except Exception as exc:  # noqa: BLE001 —— 单批失败降级，不炸全流程
                if stats["error"] is None:
                    stats["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            stats["batches_done"] += 1
    stats["batches_skipped"] += len(pending)
    return stats


# ── P1：以文档本体为种子 ──────────────────────────────────────────────
#
# 文档侧 KB 级 compiled schema 是 `{entity_types, relation_types, constraints}`；
# 抽象 schema 是 `{entities, relations, metrics}`。这里只做形状搬运：
# 文档 canonical `name` 原样保留（ABox / doc_types / MAPS_TO 依赖它），
# 中文名进 `display_name`，落点先留空（由 `attach_landings_by_name` 再补）。
# **constraints 不进入抽象层**（抽象 schema 没有这个字段），以 warning 显形。


def document_seed_from_compiled(
    schema: dict,
) -> tuple[list[dict], list[dict], list[str]]:
    """KB 级 compiled schema → 抽象 v2 的 `(entities, relations, warnings)`。"""
    warnings: list[str] = []
    raw_constraints = _as_list((schema or {}).get("constraints"))
    if raw_constraints:
        warnings.append(
            f"文档 schema 的 {len(raw_constraints)} 条约束不会进入抽象层"
            "（抽象 schema 不建模 constraints）"
        )

    entities: list[dict] = []
    for raw in _as_list((schema or {}).get("entity_types")):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        attributes: list[dict] = []
        for raw_attr in _as_list(raw.get("attributes")):
            if not isinstance(raw_attr, dict):
                continue
            attr_name = str(raw_attr.get("name") or "").strip()
            if not attr_name:
                continue
            attr: dict = {
                "name": attr_name,
                "type": str(raw_attr.get("type") or "string").strip() or "string",
                "primary_key": (
                    raw_attr.get("is_primary_key") is True
                    or raw_attr.get("primary_key") is True
                ),
                "required": raw_attr.get("required") is True,
                "sources": [],
            }
            for field in ("display_name", "description"):
                value = str(raw_attr.get(field) or "").strip()
                if value:
                    attr[field] = value
            enum_values = raw_attr.get("enum_values")
            if isinstance(enum_values, list):
                cleaned = [str(v).strip() for v in enum_values if str(v).strip()]
                if cleaned:
                    attr["enum_values"] = cleaned
            attributes.append(attr)

        entity: dict = {
            "name": name,
            "description": str(raw.get("description") or ""),
            "aliases": [
                str(a).strip() for a in _as_list(raw.get("aliases")) if str(a).strip()
            ],
            "sources": [],
            "attributes": attributes,
        }
        display_name = str(raw.get("display_name") or "").strip()
        if display_name:
            entity["display_name"] = display_name
        entities.append(entity)

    relations: list[dict] = []
    for raw in _as_list((schema or {}).get("relation_types")):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        source = str(raw.get("source") or "").strip()
        target = str(raw.get("target") or "").strip()
        if not (name and source and target):
            continue
        relation: dict = {
            "name": name,
            "source": source,
            "target": target,
            "description": str(raw.get("description") or ""),
            "backings": [],
        }
        cardinality = str(raw.get("cardinality") or "").strip()
        if cardinality:
            relation["cardinality"] = cardinality
        relations.append(relation)

    return entities, relations, warnings


def attach_landings_by_name(entities, bundles: dict) -> int:
    """给**没有落点**的抽象实体按名称/别名匹配数据源实体并挂上落点。

    匹配口径：`name` / `display_name` / `aliases` 与源实体名大小写不敏感精确相等；
    同一实体可能命中多个源的同一名称（跨源概念），全部挂上。已有落点的实体不动。
    返回匹配到落点的实体数。
    """
    index: dict[str, list[tuple[str, str]]] = {}
    for ds_id, bundle in (bundles or {}).items():
        for source_name in (bundle.get("entities") or {}):
            key = str(source_name).casefold()
            if key:
                index.setdefault(key, []).append((ds_id, str(source_name)))

    matched = 0
    for entity in _as_list(entities):
        if not isinstance(entity, dict) or entity.get("sources"):
            continue
        keys = [entity.get("name"), entity.get("display_name")]
        keys.extend(_as_list(entity.get("aliases")))
        hits: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for key in keys:
            for ds_id, source_name in index.get(str(key or "").casefold(), []):
                triple = (ds_id, source_name)
                if triple in seen:
                    continue
                seen.add(triple)
                hits.append({
                    "data_source_id": ds_id,
                    "entity_name": source_name,
                    "included": True,
                })
        if hits:
            entity["sources"] = hits
            matched += 1
    return matched


def apply_llm_landings(entities, alignments, bundles: dict) -> dict:
    """把 LLM 对齐结果落到抽象实体（实体级 ≥ 属性级），只接受真实存在的目标。

    - `(data_source_id, source_entity)` 必须存在于 `bundles`；
    - `attribute` 必须是该抽象实体已有的属性名（大小写不敏感）；
    - `source_attribute` 必须是该源实体真实列名（大小写不敏感），落库时写回
      **源里的真实列名**；
    - 重复落点/属性来源去重。
    返回 `{"entities": 新增实体落点数, "attributes": 新增属性来源数}`。
    """
    by_name: dict[str, list[dict]] = {}
    for entity in _as_list(entities):
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        if name:
            by_name.setdefault(name.casefold(), []).append(entity)

    source_index: dict[tuple[str, str], dict[str, str]] = {}
    for ds_id, bundle in (bundles or {}).items():
        for source_name, source_entity in (bundle.get("entities") or {}).items():
            columns = {
                str(attr.get("name") or "").strip().casefold(): str(attr.get("name") or "").strip()
                for attr in _as_list(source_entity.get("attributes"))
                if isinstance(attr, dict) and str(attr.get("name") or "").strip()
            }
            source_index[(str(ds_id), str(source_name).casefold())] = columns

    entity_added = 0
    attribute_added = 0
    for alignment in _as_list(alignments):
        if not isinstance(alignment, dict):
            continue
        targets = by_name.get(str(alignment.get("entity") or "").strip().casefold(), [])
        if not targets:
            continue
        for mapping in _as_list(alignment.get("mappings")):
            if not isinstance(mapping, dict):
                continue
            ds_id = str(mapping.get("data_source_id") or "").strip()
            source_name = str(mapping.get("source_entity") or "").strip()
            columns = source_index.get((ds_id, source_name.casefold()))
            if columns is None:
                continue
            for entity in targets:
                sources = entity.setdefault("sources", [])
                existing = {
                    (str(s.get("data_source_id") or ""), str(s.get("entity_name") or ""))
                    for s in sources if isinstance(s, dict)
                }
                if (ds_id, source_name) not in existing:
                    sources.append({
                        "data_source_id": ds_id,
                        "entity_name": source_name,
                        "included": True,
                    })
                    entity_added += 1

                for attr_map in _as_list(mapping.get("attributes")):
                    if not isinstance(attr_map, dict):
                        continue
                    attr_name = str(attr_map.get("attribute") or "").strip().casefold()
                    source_attr = columns.get(
                        str(attr_map.get("source_attribute") or "").strip().casefold()
                    )
                    if not attr_name or not source_attr:
                        continue
                    for attr in _as_list(entity.get("attributes")):
                        if not isinstance(attr, dict):
                            continue
                        if str(attr.get("name") or "").strip().casefold() != attr_name:
                            continue
                        links = attr.setdefault("sources", [])
                        triple = (ds_id, source_name, source_attr)
                        if not any(
                            isinstance(link, dict)
                            and (
                                str(link.get("data_source_id") or ""),
                                str(link.get("entity_name") or ""),
                                str(link.get("attribute_name") or ""),
                            ) == triple
                            for link in links
                        ):
                            links.append({
                                "data_source_id": ds_id,
                                "entity_name": source_name,
                                "attribute_name": source_attr,
                            })
                            attribute_added += 1
    return {"entities": entity_added, "attributes": attribute_added}


# ── 业务词表（alias_hints）：人给的「客户会怎么说」→ 抽象实体别名 ──────────

#: 每个实体可追加的别名上限（与 concept_expansion.MAX_ALIASES_PER_CONCEPT 同值；
#: 这里不复用导入，避免两个模块将来各自演进时把上限悄悄改成两个数）。
ALIAS_HINTS_MAX_ALIASES = 50
#: 单个别名的长度上限（与 concept_expansion.MAX_TERM_LENGTH 同值）。
ALIAS_HINTS_MAX_LEN = 128
_ALIAS_SPLIT_RE = re.compile(r"[,，/、;；]")


def _alias_hint_pairs(bundles: dict | None, target: str) -> list[tuple[str, str]]:
    """词表目标 → 数据源里的 (data_source_id, entity_name)（大小写不敏感，全部命中）。"""
    key = target.strip().casefold()
    pairs: list[tuple[str, str]] = []
    for ds_id, bundle in (bundles or {}).items():
        if not isinstance(bundle, dict):
            continue
        for name in bundle.get("entities") or {}:
            if str(name).strip().casefold() == key:
                pairs.append((str(ds_id), str(name)))
    return pairs


def apply_alias_hints_to_entities(
    entities: list[dict], hints: str, bundles: dict | None = None,
) -> tuple[int, int, list[str]]:
    """词表 → 实体别名；目标不存在但数据源里有 → **自动补建实体**。

    返回 ``(applied, created, warnings)``：``applied`` = 被挂上别名的既有实体数，
    ``created`` = 新建实体数（不计入 applied）；坏行/未知目标只进 warnings。
    新建实体带 `sources`（该源里同名实体），属性由调用方随后的
    `merged_attributes_for_entity` 补齐 —— 这是「词表里写了就一定有这个概念」的保证。
    """
    applied: set[int] = set()
    created = 0
    warnings: list[str] = []
    if not isinstance(hints, str) or not hints.strip():
        return 0, 0, warnings
    if not isinstance(entities, list):
        return 0, 0, warnings

    index: dict[str, list[dict]] = {}

    def _index_entity(ent: dict) -> None:
        names = {str(ent.get("name") or ""), str(ent.get("display_name") or "")}
        for src in _as_list(ent.get("sources")):
            if isinstance(src, dict):
                names.add(str(src.get("entity_name") or ""))
        for name in names:
            key = name.strip().casefold()
            if key:
                index.setdefault(key, []).append(ent)

    for ent in entities:
        if isinstance(ent, dict):
            _index_entity(ent)

    for lineno, raw in enumerate(hints.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" not in line:
            warnings.append(f"词表第 {lineno} 行缺少 '=>'，已忽略：{line[:40]}")
            continue
        left, right = line.split("=>", 1)
        aliases = [
            a.strip() for a in _ALIAS_SPLIT_RE.split(left)
            if a.strip() and len(a.strip()) <= ALIAS_HINTS_MAX_LEN
        ]
        target = right.strip()
        if not aliases or not target:
            warnings.append(f"词表第 {lineno} 行缺少别名或目标，已忽略：{line[:40]}")
            continue
        matched = index.get(target.casefold())
        if not matched:
            pairs = _alias_hint_pairs(bundles, target)
            if not pairs:
                warnings.append(f"词表目标既不在抽象实体里、也不在数据源 schema 里，已忽略：{target}")
                continue
            ent = {
                "name": pairs[0][1],
                "display_name": pairs[0][1],
                "aliases": list(aliases),
                "sources": [
                    {"included": True, "entity_name": name, "data_source_id": ds}
                    for ds, name in pairs
                ],
            }
            entities.append(ent)
            _index_entity(ent)
            created += 1
            continue
        for ent in matched:
            existing = [str(a) for a in _as_list(ent.get("aliases")) if str(a).strip()]
            merged = list(dict.fromkeys([*existing, *aliases]))[:ALIAS_HINTS_MAX_ALIASES]
            ent["aliases"] = merged
            applied.add(id(ent))
    return len(applied), created, warnings


def apply_alias_hints(schema: dict, hints: str) -> tuple[int, list[str]]:
    """兼容入口：只挂别名、不补建实体（schema 形状的纯函数，供旧调用方/测试）。"""
    if not isinstance(schema, dict):
        return 0, []
    applied, _created, warnings = apply_alias_hints_to_entities(
        _as_list(schema.get("entities")), hints)
    return applied, warnings


# ── 同源重复合并（文档种子 vs 结构化聚类）────────────────────────────

def _included_source_key(ent: dict) -> frozenset[tuple[str, str]] | None:
    pairs: set[tuple[str, str]] = set()
    for src in _as_list(ent.get("sources")):
        if not isinstance(src, dict) or src.get("included") is False:
            continue
        ds = str(src.get("data_source_id") or "").strip()
        name = str(src.get("entity_name") or "").strip()
        if ds and name:
            pairs.add((ds, name))
    return frozenset(pairs) or None


def merge_duplicate_source_entities(
    entities: list[dict], split_index: int,
) -> tuple[list[dict], int]:
    """把 split_index 之后的「聚类实体」并入此前同落点的实体（通常是文档种子）。

    只合并**跨组**：文档种子之间即使同源也保留（可能是两个刻意的概念）；
    合并时别名并集、属性按名去重（先到者胜）；描述/显示名保留先出现者。
    返回 (合并后的列表, 被并掉的条数)。
    """
    if not isinstance(entities, list) or not isinstance(split_index, int):
        return entities, 0
    if split_index <= 0 or split_index >= len(entities):
        return entities, 0
    prior: dict[frozenset, dict] = {}
    for ent in entities[:split_index]:
        if not isinstance(ent, dict):
            continue
        key = _included_source_key(ent)
        if key:
            prior.setdefault(key, ent)

    out: list[dict] = list(entities[:split_index])
    dropped = 0
    for ent in entities[split_index:]:
        key = _included_source_key(ent) if isinstance(ent, dict) else None
        target = prior.get(key) if key else None
        if target is None:
            out.append(ent)
            if key and isinstance(ent, dict):
                prior.setdefault(key, ent)
            continue
        aliases = [str(a) for a in _as_list(target.get("aliases")) if str(a).strip()]
        for alias in _as_list(ent.get("aliases")):
            alias = str(alias).strip()
            if alias and alias not in aliases:
                aliases.append(alias)
        target["aliases"] = aliases[:ALIAS_HINTS_MAX_ALIASES]
        attrs = [a for a in _as_list(target.get("attributes")) if isinstance(a, dict)]
        seen = {str(a.get("name") or "").casefold() for a in attrs}
        for attr in _as_list(ent.get("attributes")):
            if not isinstance(attr, dict):
                continue
            name = str(attr.get("name") or "").strip()
            if name and name.casefold() not in seen:
                attrs.append(attr)
                seen.add(name.casefold())
        target["attributes"] = attrs
        dropped += 1
    return out, dropped


# ── 指标词表（[metric] 行）────────────────────────────────────────────
_METRIC_HINT_PREFIX = "[metric]"


def parse_metric_hints(hints: str, bundles: dict | None = None,
                       ) -> tuple[list[dict], list[str]]:
    """业务词表里的 ``[metric] 名称 | 别名 | 目标实体 | 描述`` → 指标定义。

    指标不是从文档/库结构自动生成的（它们是人登记的「口径」）；把定义写进生成用的
    业务词表，生成时一并落 ``schema.metrics``，重建库后就不用再手工进指标面板补。

    落点复用别名目标的解析（实体名/物理表名 → 数据源里的同名实体，全部命中）；
    目标找不到只 warning。同名指标**首条胜**（与 normalize 的合并口径一致）。
    """
    metrics: list[dict] = []
    warnings: list[str] = []
    if not isinstance(hints, str) or not hints.strip():
        return metrics, warnings
    seen: set[str] = set()
    for lineno, raw in enumerate(hints.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.lower().startswith(_METRIC_HINT_PREFIX):
            continue
        body = line[len(_METRIC_HINT_PREFIX):].strip()
        parts = [p.strip() for p in body.split("|")]
        if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
            warnings.append(
                f"指标词表第 {lineno} 行格式应为 [metric] 名称 | 别名 | 目标实体 | 描述，"
                f"已忽略：{line[:60]}")
            continue
        name, alias_text, target = parts[0], parts[1], parts[2]
        description = parts[3] if len(parts) > 3 else ""
        aliases = [
            a.strip() for a in _ALIAS_SPLIT_RE.split(alias_text)
            if a.strip() and len(a.strip()) <= ALIAS_HINTS_MAX_LEN
        ]
        pairs = _alias_hint_pairs(bundles, target)
        if not pairs:
            warnings.append(f"指标「{name}」的落点不在数据源 schema 里，已忽略：{target}")
            continue
        if name in seen:
            continue
        seen.add(name)
        metrics.append({
            "name": name,
            "display_name": name,
            "aliases": aliases,
            "landings": [
                {"entity_name": ent, "data_source_id": ds} for ds, ent in pairs
            ],
            "additivity": "non_additive",
            "description": description,
        })
    return metrics, warnings
