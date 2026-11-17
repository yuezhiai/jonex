#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""落点合并守卫与重算语义（设计 §5.5）：Agent 判定 → 抽象 schema 落点。

**这一层是数据安全层**，写错只会静默覆盖人的落点或冻结 agent 的落点，所以规则
按 §5.5 的顺序逐条执行，不做「顺手优化」：

1. 目标 `(ds, entity)` 必须存在于 bundles；列必须是真实列（大小写不敏感，
   写回源里的真实名）。—— 模型输出里任何标识符都不可信。
2. 列映射只落到概念**已有**的属性名（大小写不敏感）。
3. 既有 `included: false` → 整条跳过（**永不复活**：人把删除改成排除是明确意图）。
4. 既有 `origin == "manual"` → 整条跳过（人的落点 agent 绝不碰，含列来源）。
5. 既有 `origin == "agent"` → 更新置信度/依据；**本轮落点列表里没有它**则置
   `included: false`（agent 的落点每次都是可重算的）。这条**只在 `action == "decide"`
   时生效**：`give_up` 是「本轮判不了」（无候选/预算耗尽/协议错误），不是「旧落点错了」，
   照它撤销会让一次预算耗尽冻结该概念下所有 agent 落点。
6. 既有 `origin == "name"` → 本轮判定则升级为 `decision.origin`（B 档为 agent）。
7. 既有但**没有 origin（历史数据）** → 只补 `confidence`/`rationale` 与列来源；
   不写 origin、不改 `included`、不删除（历史落点永久受保护）。
8. 源侧没有样本证据 → `high` 降为 `medium`，rationale 末尾追加「（无样本证据）」。
   **评审裁定（review round 1）**：带至少一条已验证列映射的落点视为有结构依据，
   不算「无证据」；只有「无样本证据 + 无任何列映射」的纯名字落点才降级（见 `_provenance`）。
9. 新写入的 origin 一律取 `decision.origin`（A 档由 runner 构造成 `"name"`），
   绝不在这里硬编码 `"agent"`。
10. 只写 Task 1 归一器认识的键（`origin`/`confidence`/`rationale`），不发明字段。

**只处理 `decisions` 里出现的概念**：没进 decisions 的实体原样不动 —— 这保护了
「本轮没判到」的概念（如 `max_concepts` 截断）不被误当成「agent 不再判定」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..concept_expansion import (
    LANDING_CONFIDENCES,
    LANDING_ORIGINS,
    MAX_RATIONALE_CHARS,
)
from ..evidence_probe import evidence_key

if TYPE_CHECKING:  # 只为类型标注；避免 apply → loop → tools 的运行时导入链
    from .loop import ConceptDecision

#: 无样本证据的降级标记。归一器按字符数截断 rationale，追加前要给它留位置。
NO_EVIDENCE_MARK = "（无样本证据）"


@dataclass
class ApplyStats:
    """本次合并的统计。

    - `entities`：被**实际改动**的实体落点数（新增 / provenance 更新 / 置 excluded）；
      值没变的重复写入不计数，保证重跑同一批决策是幂等的。
    - `attributes`：新增的属性来源条数（已有列映射只升级 origin，不算新增）。
    - `skipped`：被守卫拒绝的落点/列映射条数（未知目标、未知列、manual、已排除...）。
    - `conflicts`：上述拒绝的可读原因，按处理顺序排列（可重复）。
    """

    entities: int = 0
    attributes: int = 0
    skipped: int = 0
    conflicts: list[str] = field(default_factory=list)


def _clean(value) -> str:
    """任意值 → 去空白字符串（缺键/None 回 ""，不留 "None"）。"""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _fold(value) -> str:
    """比较用的规范形式：大小写不敏感。写回时仍用真实名。"""
    return _clean(value).casefold()


def _origin_of(decision) -> str:
    """决策的写入来源；值域外的不写（归一器本来也会丢，这里提前不产生脏键）。

    origin 与名字同口径做大小写折叠后落库（`"Agent"` → `"agent"`）：否则
    `"Manual"` 这类写法会绕过本模块所有 `origin == "manual"` 的守卫。
    """
    origin = _fold(getattr(decision, "origin", ""))
    return origin if origin in LANDING_ORIGINS else ""


def _confidence_of(decision) -> str:
    """置信度只认三档；值域外一律 `low`（与 loop 的归一化同口径：宁降不升）。"""
    level = _clean(getattr(decision, "confidence", "")).lower()
    return level if level in LANDING_CONFIDENCES else "low"


def _has_sample_evidence(bundle: dict, source_entity: dict) -> bool:
    """该源实体是否有**样本证据**（任一列带非空 `samples`）。

    判定到实体级而不是列级：落点的置信度是实体级判断（「这张表是不是这个概念」），
    列里有没有样本只影响「凭什么这么判」，不该让单个无样本列把整条落点降级。
    表键必须走 `evidence_key`（与写入方共用），否则空白 `source_object_id` 会让
    读取静默落空。
    """
    evidence = bundle.get("evidence") if isinstance(bundle, dict) else None
    if not isinstance(evidence, dict):
        return False
    tables = evidence.get("tables")
    if not isinstance(tables, dict):
        return False
    entry = tables.get(evidence_key(source_entity))
    if not isinstance(entry, dict):
        return False
    columns = entry.get("columns")
    if not isinstance(columns, dict):
        return False
    return any(isinstance(value, dict) and bool(value.get("samples"))
               for value in columns.values())


def _provenance(decision, bundle: dict, source_entity: dict, *,
                has_mappings: bool) -> tuple[str, str]:
    """决策 → 落库用的 `(confidence, rationale)`（规则 8 在此收口）。

    「源无证据」的判定包含**结构性依据**：落点带至少一条已验证的列映射时，它
    至少落在源的真实列上，不因缺样本就把模型给的 high 降掉；只有「无样本证据 +
    无任何列映射」的纯名字落点才降级并标记 —— brief 的
    `test_adds_entity_and_attribute_landings` 与
    `test_high_confidence_without_evidence_downgraded` 只有按这个口径才能同时成立。
    """
    confidence = _confidence_of(decision)
    rationale = _clean(getattr(decision, "rationale", ""))
    if _has_sample_evidence(bundle, source_entity):
        return confidence, rationale[:MAX_RATIONALE_CHARS]
    # 无样本证据时**标记一律追加**（哪怕有列映射、置信度不降），让读者一眼知道
    # 这条落点没有数据支撑；列映射只豁免「降级」，不豁免「透明」。
    if not has_mappings and confidence == "high":
        confidence = "medium"
    # 标记必须活到落库：先给标记留位置再截断，否则 200 字的 rationale 会把标记切掉。
    budget = max(0, MAX_RATIONALE_CHARS - len(NO_EVIDENCE_MARK))
    return confidence, rationale[:budget] + NO_EVIDENCE_MARK


def _concepts_by_name(entities) -> dict[str, list[dict]]:
    """概念名 → 抽象实体（精确匹配；重名时全部命中，保持输入顺序）。"""
    out: dict[str, list[dict]] = {}
    for entity in entities if isinstance(entities, list) else []:
        if not isinstance(entity, dict):
            continue
        name = _clean(entity.get("name"))
        if name:
            out.setdefault(name, []).append(entity)
    return out


def _real_entity_name(bundle: dict, wanted: str) -> str:
    """bundle 里的实体真实键名；大小写不敏感兜底。找不到回 ""。"""
    entities = bundle.get("entities")
    if not isinstance(entities, dict) or not wanted:
        return ""
    if wanted in entities:
        return wanted
    folded = _fold(wanted)
    for name in entities:
        if _fold(name) == folded:
            return str(name)
    return ""


def _column_index(source_entity) -> dict[str, str]:
    """源实体 → `{fold(可请求名): 真实列名}`。

    `physical_name`（若有）优先：编译形状通常只有 `name`，但历史/引擎输入可能带
    物理名；按 `tools._attributes` 的同一口径把两者都映射到真实列名。
    """
    index: dict[str, str] = {}
    attributes = (source_entity or {}).get("attributes") \
        if isinstance(source_entity, dict) else None
    for attribute in attributes if isinstance(attributes, list) else []:
        if not isinstance(attribute, dict):
            continue
        logical = _clean(attribute.get("name"))
        physical = _clean(attribute.get("physical_name"))
        real = physical or logical
        if not real:
            continue
        index.setdefault(_fold(real), real)
        if logical:
            index.setdefault(_fold(logical), real)
    return index


def _attribute_index(entity: dict) -> dict[str, dict]:
    """概念属性名 → 属性对象（大小写不敏感；重名先到者胜）。"""
    out: dict[str, dict] = {}
    attributes = entity.get("attributes")
    for attribute in attributes if isinstance(attributes, list) else []:
        if not isinstance(attribute, dict):
            continue
        name = _clean(attribute.get("name"))
        if name:
            out.setdefault(_fold(name), attribute)
    return out


def _source_key(source: dict) -> tuple[str, str]:
    """落点身份：`(ds_id, fold(entity_name))`。"""
    return (_clean(source.get("data_source_id")), _fold(source.get("entity_name")))


def _plan_landings(attribute_index: dict[str, dict], concept_name: str,
                   decision, bundles: dict, stats: ApplyStats) -> tuple[set, dict]:
    """校验一个概念的落点列表，返回 `(attempted, decided)`。

    - `attempted`：模型给出的所有目标键（**含校验失败者**）。规则 5 的「本轮未
      判定」按落点列表定义，而不是按校验结果 —— 模型写了目标但列名拼错，不该
      让上一条 agent 落点被误冻结。
    - `decided`：通过规则 1/2 的目标 → 可写信息（含待补的属性来源）。
    """
    attempted: set[tuple[str, str]] = set()
    decided: dict[tuple[str, str], dict] = {}
    landings = getattr(decision, "landings", None)
    for raw in landings if isinstance(landings, list) else []:
        if not isinstance(raw, dict):
            stats.skipped += 1
            stats.conflicts.append(f"落点形状非法：{concept_name}")
            continue
        ds_id = _clean(raw.get("ds_id"))
        entity_name = _clean(raw.get("entity_name"))
        if ds_id and entity_name:
            attempted.add((ds_id, _fold(entity_name)))

        bundle = (bundles or {}).get(ds_id) if ds_id else None
        if not isinstance(bundle, dict):
            stats.skipped += 1
            stats.conflicts.append(f"未知数据源：{ds_id or '(缺失)'}")
            continue
        real_entity = _real_entity_name(bundle, entity_name)
        if not real_entity:
            stats.skipped += 1
            stats.conflicts.append(f"未知实体：{ds_id}/{entity_name or '(缺失)'}")
            continue

        source_entity = (bundle.get("entities") or {}).get(real_entity) or {}
        target_key = (ds_id, _fold(real_entity))
        target = decided.setdefault(target_key, {
            "ds_id": ds_id,
            "entity_name": real_entity,
            "source_entity": source_entity,
            "bundle": bundle,
            "attributes": [],
            "_pairs": set(),
        })

        columns = _column_index(source_entity)
        attributes = raw.get("attributes")
        for item in attributes if isinstance(attributes, list) else []:
            if not isinstance(item, dict):
                stats.skipped += 1
                continue
            attribute_name = _clean(item.get("attribute"))
            column = _clean(item.get("column"))
            concept_attribute = attribute_index.get(_fold(attribute_name)) \
                if attribute_name else None
            if concept_attribute is None:
                stats.skipped += 1
                stats.conflicts.append(
                    f"概念缺少属性：{concept_name}.{attribute_name or '(缺失)'}")
                continue
            real_column = columns.get(_fold(column))
            if not real_column:
                stats.skipped += 1
                stats.conflicts.append(
                    f"列不存在：{ds_id}/{real_entity}.{column or '(缺失)'}")
                continue
            pair = (_fold(concept_attribute.get("name")), _fold(real_column))
            if pair in target["_pairs"]:
                continue
            target["_pairs"].add(pair)
            target["attributes"].append((concept_attribute, real_column))
    return attempted, decided


def _set(existing: dict, key: str, value: str) -> bool:
    """写入非空值；返回是否真的变了（幂等重跑不计数）。"""
    if existing.get(key) == value:
        return False
    existing[key] = value
    return True


def _upgrade_provenance(existing: dict, decision, confidence: str, rationale: str,
                        *, allow_origin: bool) -> bool:
    """agent / name 落点的 provenance 更新；返回是否变更。

    agent 行不写 origin（规则 5 只说更新置信度/依据）：A 档的 `"name"` 不能把
    agent 判定降级回名字匹配。name 行按规则 6 升级为 `decision.origin`。
    """
    changed = False
    if allow_origin:
        origin = _origin_of(decision)
        if origin:
            changed |= _set(existing, "origin", origin)
    changed |= _set(existing, "confidence", confidence)
    # 只在有依据时覆盖：空 rationale 不抹掉已存的文本（模型偶尔不写依据，
    # 不代表上一轮的判断依据失效）。
    if rationale:
        changed |= _set(existing, "rationale", rationale)
    return changed


def _fill_legacy(existing: dict, confidence: str, rationale: str) -> bool:
    """历史落点（无 origin）：只补缺失值，不改 `included`、不写 origin。"""
    changed = False
    if not _clean(existing.get("confidence")) and confidence:
        existing["confidence"] = confidence
        changed = True
    if not _clean(existing.get("rationale")) and rationale:
        existing["rationale"] = rationale
        changed = True
    return changed


def _attach_attributes(target: dict, decision, stats: ApplyStats) -> None:
    """把已校验的列映射补到概念属性上（只增；已有映射最多升级 origin）。"""
    ds_id = target["ds_id"]
    entity_name = target["entity_name"]
    origin = _origin_of(decision)
    for concept_attribute, real_column in target["attributes"]:
        links = concept_attribute.get("sources")
        if not isinstance(links, list):
            links = []
            concept_attribute["sources"] = links
        existing = next(
            (link for link in links
             if isinstance(link, dict)
             and _clean(link.get("data_source_id")) == ds_id
             and _fold(link.get("entity_name")) == _fold(entity_name)
             and _fold(link.get("attribute_name")) == _fold(real_column)),
            None,
        )
        if existing is not None:
            if existing.get("enabled") is False:
                continue  # 停用的列映射不复活（与 included:false 同口径）
            current = _fold(existing.get("origin"))
            if current == "manual":
                continue  # 人手工挂的列映射不动
            # name → agent 是升级；反向不降级；其它值（无 origin 的历史数据）不补。
            if origin and current and current != origin \
                    and not (current == "agent" and origin == "name"):
                existing["origin"] = origin
                stats.attributes += 1  # origin 升级也是属性来源的一次变更
            continue
        link = {"data_source_id": ds_id, "entity_name": entity_name,
                "attribute_name": real_column}
        if origin:
            link["origin"] = origin
        links.append(link)
        stats.attributes += 1


def _merge_target(entity: dict, target: dict, decision, stats: ApplyStats) -> None:
    """按现有落点的 origin 走规则 3-7 的矩阵。"""
    ds_id = target["ds_id"]
    entity_name = target["entity_name"]
    confidence, rationale = _provenance(decision, target["bundle"],
                                        target["source_entity"],
                                        has_mappings=bool(target["attributes"]))
    sources = entity.get("sources")
    if not isinstance(sources, list):
        sources = []

    key = (ds_id, _fold(entity_name))
    existing = next(
        (source for source in sources
         if isinstance(source, dict) and _source_key(source) == key),
        None,
    )
    if existing is None:
        # 新落点：included 显式 True；origin 取决策（A 档 name / B 档 agent）。
        if not isinstance(entity.get("sources"), list):
            entity["sources"] = sources
        row: dict = {"data_source_id": ds_id, "entity_name": entity_name,
                     "included": True}
        origin = _origin_of(decision)
        if origin:
            row["origin"] = origin
        row["confidence"] = confidence
        if rationale:
            row["rationale"] = rationale
        sources.append(row)
        stats.entities += 1
        _attach_attributes(target, decision, stats)
        return

    # 规则 3：已排除的落点永不复活（含 origin 为 agent/name 的行）。
    if existing.get("included") is False:
        stats.skipped += 1
        return

    current_origin = _fold(existing.get("origin"))
    if current_origin == "manual":
        # 规则 4：人的落点整条不碰 —— 连列来源也不补，否则等于改写了这条落点。
        stats.skipped += 1
        stats.conflicts.append(f"manual 落点跳过：{ds_id}/{entity_name}")
        return

    if current_origin == "agent":
        # 规则 5：本轮仍判定 → 更新置信度/依据（origin 保持 agent，不降级）。
        if _upgrade_provenance(existing, decision, confidence, rationale,
                               allow_origin=False):
            stats.entities += 1
    elif current_origin == "name":
        # 规则 6：本轮判定则升级为 decision.origin。
        if _upgrade_provenance(existing, decision, confidence, rationale,
                               allow_origin=True):
            stats.entities += 1
    else:
        # 规则 7：无 origin 的历史落点只补缺，不写 origin、不碰 included。
        if _fill_legacy(existing, confidence, rationale):
            stats.entities += 1

    _attach_attributes(target, decision, stats)


def _exclude_undecided_agent_landings(entity: dict, attempted: set,
                                      stats: ApplyStats) -> None:
    """规则 5 的另一半：agent 落点本轮落点列表里没有 → 置 `included: false`。

    只排除 agent：manual 是人的决定、name 是确定性匹配、无 origin 是历史数据，
    它们都不受重算矩阵的删除语义约束。
    """
    sources = entity.get("sources")
    if not isinstance(sources, list):
        return
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("included") is False:
            continue
        if _fold(source.get("origin")) != "agent":
            continue
        key = _source_key(source)
        if not all(key):
            continue  # 身份不完整的坏落点宁可不碰
        if key in attempted:
            continue
        source["included"] = False
        stats.entities += 1


def apply_decisions(
        entities: list[dict],
        decisions: dict[str, "ConceptDecision"],
        bundles: dict[str, dict],
) -> ApplyStats:
    """把概念判定合并进抽象 schema 的落点（原地修改 `entities`）。

    `decisions` 的键是概念名；**只有出现在这里的概念会被重算**。返回改动统计。
    任何形状异常都按「跳过」处理，绝不因为一条坏落点打断整批合并。
    """
    stats = ApplyStats()
    by_name = _concepts_by_name(entities)
    for concept_name, decision in (decisions or {}).items():
        name = _clean(concept_name)
        # 规则 5 只在「本轮确实做了判定」时生效。`give_up` 表示这一轮判不了
        # （无候选 / 预算耗尽 / 协议错误），不是「上一条落点错了」；若照样撤销，
        # 一次预算耗尽就会把该概念下所有 agent 落点冻结 —— 那是数据事故，不是重算。
        if _clean(getattr(decision, "action", "")) != "decide":
            continue
        concept_entities = by_name.get(name) or []
        if not concept_entities:
            stats.skipped += 1
            stats.conflicts.append(f"概念不存在：{name}")
            continue
        # 决策形状坏掉时**整条不处理**：此时无法区分「本轮不判定」与「调用方传错」，
        # 若照样跑规则 5，会把该概念所有 agent 落点误冻结。
        if not isinstance(getattr(decision, "landings", None), list):
            stats.skipped += 1
            stats.conflicts.append(f"决策形状非法：{name}")
            continue
        for entity in concept_entities:
            attribute_index = _attribute_index(entity)
            attempted, decided = _plan_landings(attribute_index, name, decision,
                                                bundles, stats)
            for target in decided.values():
                _merge_target(entity, target, decision, stats)
            _exclude_undecided_agent_landings(entity, attempted, stats)
    return stats


__all__ = ["ApplyStats", "NO_EVIDENCE_MARK", "apply_decisions"]
