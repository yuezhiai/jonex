#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""重复概念合并：同一张表被不同文档抽成「英文标识符 + 中文业务名」两条时并成一条。

为什么需要它（2026-11-14 真机，KB `273e4ac5…`）：

同一批 MES 数据被两类文档各抽了一遍 —— DDL/表文档给出
`process_card_test_record`，业务文档给出「流程卡测试记录」。两条共享别名
「测试记录」，且后者的别名 `ProcessCardTestRecord` 与前者的身份键相同；但
编译只按 `canonical_name` 合并，抽象构造也只按「名字相等」合并，于是两条
并列保留，落点只挂在其中一条上，界面上看起来就是「又新建了一个实体」。

判据（全部确定性、不调模型，纯函数）：

1. **身份键相等**：名字/别名去掉分隔符与大小写后相同
   （`ProcessCardTestRecord` ↔ `process_card_test_record`）；
2. **去系统前缀后相等**：`MesQcRecord` ↔ `qc_record`（`Mes`/`Sys`/`Tb`/`T_`，
   与设计 §5.4 的命名风格规则同一口径）；
3. 过泛别名（`记录`/`信息`…）不参与聚类 —— 否则「测试记录」会把流程卡、
   QC、AOI 三条并成一条。

合并动作：保留「有 included 落点 → 属性多 → 列表序靠前」选出的那条；
被并条的名字与别名并入别名（检索仍能命中），落点/属性取并集，关系端点与
指标落点改写到保留名。**不改输入**（内部 deepcopy）。
"""
from __future__ import annotations

import copy
import re

from .synonym_normalizer import _normalize_key

#: 过泛别名不参与聚类：并了它们等于把 QC/流程卡/AOI 全并成一个「记录」。
_GENERIC_ALIASES = frozenset({
    "记录", "信息", "数据", "表", "明细", "详情", "列表", "档案", "信息表",
})

#: 系统前缀：`MesQcRecord` / `mes_qc_record` / `tb_order` / `t_order`。
_SNAKE_PREFIX = re.compile(r"^(?:mes|sys|tb|t)[_-]+(?=.)", re.IGNORECASE)
_CAMEL_PREFIX = re.compile(r"^(?:Mes|Sys|Tb)(?=[A-Z])")


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _identity(value: str) -> str:
    """标识符身份键：归一后去掉所有非字母数字（`WorkOrder` == `work_order`）。"""
    folded = _normalize_key(str(value or ""))
    return re.sub(r"[^0-9a-z㐀-鿿]", "", folded)


def _without_prefix(value: str) -> str:
    """去掉系统前缀后的名字；没有前缀时原样返回。"""
    raw = str(value or "").strip()
    return _SNAKE_PREFIX.sub("", _CAMEL_PREFIX.sub("", raw))


def _concept_keys(entity: dict) -> set[str]:
    """一个实体的聚类键：名字与别名的身份键（含去系统前缀后的形态）。"""
    keys: set[str] = set()
    name = str(entity.get("name") or "").strip()
    if name:
        keys.add(_identity(name))
        keys.add(_identity(_without_prefix(name)))
    for alias in _as_list(entity.get("aliases")):
        raw = str(alias or "").strip()
        if not raw or raw in _GENERIC_ALIASES:
            continue
        keys.add(_identity(raw))
        keys.add(_identity(_without_prefix(raw)))
    keys.discard("")
    return keys


def _merge_sources(target: list[dict], incoming) -> list[dict]:
    """落点并集：按 `(data_source_id, entity_name)` 去重，先到者胜（含 included）。"""
    seen = {
        (str(s.get("data_source_id") or ""), str(s.get("entity_name") or ""))
        for s in target if isinstance(s, dict)
    }
    for source in _as_list(incoming):
        if not isinstance(source, dict):
            continue
        key = (str(source.get("data_source_id") or ""),
               str(source.get("entity_name") or ""))
        if key in seen:
            continue
        seen.add(key)
        target.append(copy.deepcopy(source))
    return target


def _merge_attributes(target: list[dict], incoming) -> list[dict]:
    """属性并集：同名（大小写不敏感）只保留一条、来源取并集；标量先到者胜。"""
    by_name = {
        str(a.get("name") or "").strip().casefold(): a
        for a in target if isinstance(a, dict) and str(a.get("name") or "").strip()
    }
    for raw in _as_list(incoming):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        current = by_name.get(name.casefold())
        if current is None:
            item = copy.deepcopy(raw)
            item["sources"] = _merge_sources([], raw.get("sources"))
            target.append(item)
            by_name[name.casefold()] = item
            continue
        current["sources"] = _merge_sources(
            list(_as_list(current.get("sources"))), raw.get("sources"))
    return target


def _survivor_index(group: list[int], entities: list[dict]) -> int:
    """保留哪条：有 included 落点优先 → 属性多优先 → 列表序靠前（全确定性）。"""
    def score(index: int) -> tuple:
        entity = entities[index]
        included = sum(
            1 for s in _as_list(entity.get("sources"))
            if isinstance(s, dict) and s.get("included") is not False
        )
        return (included, len(_as_list(entity.get("attributes"))), -index)

    return max(group, key=score)


def merge_duplicate_concepts(
    entities: list[dict],
    relations: list[dict] | None = None,
    metrics: list[dict] | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """合并同一概念的重复实体。

    Returns:
        `(entities, relations, metrics, merges)`；`merges` 形如
        `[{"kept": "流程卡测试记录", "merged": ["process_card_test_record"]}]`，
        调用方据此回 warning（合并必须可见，不能静默改概念）。
    """
    source_entities = [e for e in _as_list(entities) if isinstance(e, dict)]
    if len(source_entities) < 2:
        return (copy.deepcopy(source_entities),
                copy.deepcopy(_as_list(relations)),
                copy.deepcopy(_as_list(metrics)), [])

    parent = list(range(len(source_entities)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[max(root_left, root_right)] = min(root_left, root_right)

    seen_keys: dict[str, int] = {}
    for index, entity in enumerate(source_entities):
        for key in _concept_keys(entity):
            if key in seen_keys:
                union(index, seen_keys[key])
            else:
                seen_keys[key] = index

    groups: dict[int, list[int]] = {}
    for index in range(len(source_entities)):
        groups.setdefault(find(index), []).append(index)

    merged: list[dict] = []
    name_map: dict[str, str] = {}
    merges: list[dict] = []
    dropped_self = 0
    for root in sorted(groups):
        group = sorted(groups[root])
        if len(group) == 1:
            merged.append(copy.deepcopy(source_entities[group[0]]))
            continue
        keep = _survivor_index(group, source_entities)
        keeper = copy.deepcopy(source_entities[keep])
        keeper["sources"] = _merge_sources([], keeper.get("sources"))
        keeper["attributes"] = _merge_attributes([], keeper.get("attributes"))
        keeper["aliases"] = list(_as_list(keeper.get("aliases")))
        kept_name = str(keeper.get("name") or "").strip()
        dropped_names: list[str] = []
        for index in group:
            if index == keep:
                continue
            donor = source_entities[index]
            donor_name = str(donor.get("name") or "").strip()
            if donor_name:
                dropped_names.append(donor_name)
                if donor_name != kept_name:
                    name_map[donor_name] = kept_name
            for value in [donor_name, *_as_list(donor.get("aliases"))]:
                value = str(value or "").strip()
                if value and value != kept_name and value not in keeper["aliases"]:
                    keeper["aliases"].append(value)
            _merge_sources(keeper["sources"], donor.get("sources"))
            _merge_attributes(keeper["attributes"], donor.get("attributes"))
            if not keeper.get("description") and donor.get("description"):
                keeper["description"] = donor["description"]
            if not keeper.get("display_name") and donor.get("display_name"):
                keeper["display_name"] = donor["display_name"]
        merged.append(keeper)
        merges.append({"kept": kept_name, "merged": dropped_names})

    out_relations: list[dict] = []
    for relation in _as_list(relations):
        if not isinstance(relation, dict):
            continue
        item = copy.deepcopy(relation)
        item["source"] = name_map.get(str(item.get("source") or ""), item.get("source"))
        item["target"] = name_map.get(str(item.get("target") or ""), item.get("target"))
        if item.get("source") == item.get("target"):
            # 两条概念并成一条后，端点相同的边无法在图上表达（也不再有意义）。
            dropped_self += 1
            continue
        for key in ("source_types", "target_types"):
            if isinstance(item.get(key), list):
                mapped: list[str] = []
                for value in item[key]:
                    mapped_value = name_map.get(str(value), value)
                    if mapped_value not in mapped:
                        mapped.append(mapped_value)
                item[key] = mapped
        out_relations.append(item)

    out_metrics: list[dict] = []
    for metric in _as_list(metrics):
        if not isinstance(metric, dict):
            continue
        item = copy.deepcopy(metric)
        for landing in _as_list(item.get("landings")):
            if isinstance(landing, dict):
                landing["entity_name"] = name_map.get(
                    str(landing.get("entity_name") or ""), landing.get("entity_name"))
        out_metrics.append(item)

    if dropped_self:
        if merges:
            merges[0]["dropped_self_relations"] = (
                merges[0].get("dropped_self_relations", 0) + dropped_self)
        else:
            merges.append({"kept": "", "merged": [],
                           "dropped_self_relations": dropped_self})
    return merged, out_relations, out_metrics, merges


__all__ = ["merge_duplicate_concepts"]
