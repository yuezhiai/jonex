# -*- coding:utf-8 -*-
"""演化补丁的纯函数：题面归一 / 草稿转换 / 采纳与回滚变换。

以下函数**逐字复制**自 `tests/eval/nl2sql/evolve.py`（2026-10-18 服务化迁移）：
`normalize_question`、`match_target_cases`、`alias_entries_from_suggestions`、
`constraint_entries_from_candidates`、`entry_key`、`apply_alias_entries`、
`rollback_alias_entries`、`apply_constraint_entries`、`rollback_constraint_entries`。

`apply_constraint_entries` 依赖的 `build_query_rule` / `MAX_EXPRESSION_CHARS` 原在
`tests/eval/nl2sql/constraint_adopt.py` —— 服务层不得 import tests 包，故同样逐字
复制到本模块。本模块不读写任何外部状态。

P1-4 追加（同一模块内的纯函数）：`plan_constraint_ops` / `apply_constraint_ops` /
`rollback_constraint_ops` —— 对**已有**约束做 add / update / disable / enable，
按 name 定位并记录 before，供采纳回滚逐项复原。
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import logging

from jonex_core.common.exceptions import InvalidParameterError

from ..nl2sql.diagnosis import add_concept_alias
from .corpus_service import corpus_ds, load_corpus
from .locking import advisory_key, chain_lock, scope_for_kind

logger = logging.getLogger(__name__)

#: 评测阶段心跳间隔（秒）。父本/候选评测可能跑几分钟；`recover_stale` 以
#: `updated_at` 判断运行是否中断，期间没有任何状态推进就会把活着的运行误判
#: 成「服务重启中断」。心跳只刷 `updated_at`，不推进 stage/status。
#: 模块常量（而非函数内字面量）：测试用 monkeypatch 把它压到毫秒级。
HEARTBEAT_SECONDS = 60

#: 单条口径文本的上限（2026-10-12 口径线）：与 constraints.py 的 MAX_TOTAL_CHARS
#: 预算相容的单条护栏；也是演化台「条目人工调整」写回（evolve.py edit）的同款校验值。
MAX_EXPRESSION_CHARS = 600


def build_query_rule(entities: list[dict], constraints: list[dict],
                     target_code: str, expression: str) -> dict:
    """纯函数：三道校验 → 新约束行（或 `changed=False` + why）。

    - `target_code` 形如 `Entity.attr`，两端都要在册（实体 + 属性）；
    - 该列**不能已有 query_rule**（`Jobs.status` 那条就在——重复写只会有两条打架）；
    - `expression` 非空（空话一条对模型零价值，P1-2 判据 2 同款）。
    """
    code = str(target_code or "").strip()
    expr = str(expression or "").strip()
    if not expr:
        return {"changed": False, "why": "expression 为空"}
    if len(expr) > MAX_EXPRESSION_CHARS:
        return {"changed": False,
                "why": f"expression 超长（{len(expr)} > {MAX_EXPRESSION_CHARS} 字）"}
    if "." not in code:
        return {"changed": False, "why": f"target_code 要写成 Entity.attribute：{code!r}"}
    ent_name, attr_name = code.rsplit(".", 1)
    ent = next((e for e in entities or []
                if isinstance(e, dict) and str(e.get("name")) == ent_name), None)
    if ent is None:
        return {"changed": False, "why": f"实体不在编译 schema 里：{ent_name}"}
    if not any(isinstance(a, dict) and str(a.get("name")) == attr_name
               for a in (ent.get("attributes") or [])):
        return {"changed": False, "why": f"{ent_name} 没有列 {attr_name}"}
    for c in constraints or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("constraint_type") or "") == "query_rule" \
                and str(c.get("target_code") or "") == code:
            return {"changed": False,
                    "why": f"该列已有 query_rule（{c.get('name')}）——先决定是否改写它"}
    rule = {
        "name": f"query_rule_{ent_name}_{attr_name}",
        "target_type": "attribute",
        "target_code": code,
        "target_label": attr_name,
        "constraint_type": "query_rule",
        "expression": expr,
        "suggestion": "",
        # 新采纳的口径默认启用（与 P1-4 的编辑开关同口径；显式写出来，
        # 避免依赖读取侧「缺字段 = 启用」的隐式约定）。
        "enabled": True,
    }
    return {"changed": True, "why": "ok", "rule": rule}


#: 归一化要剥掉的字符（中英全半角标点 + 空白）。**保守口径**：只做「换个写法就不认」，
#: 不引入模糊匹配 —— 门的题必须与语料逐字同源（spec §四）。
_PUNCT = " \t\r\n，。！？、；：,.!?;:（）()【】[]《》〈〉「」『』\"'‘’“”…—-－～~·•"


def normalize_question(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch not in _PUNCT)


def match_target_cases(questions: list[str], corpus: dict) -> list[str]:
    """失败问句 → 语料 case 名（归一化后完全一致；按题去重、保序）。"""
    index: dict[str, str] = {}
    for case in (corpus or {}).get("cases") or []:
        norm = normalize_question(case.get("question") or "")
        if norm and case.get("name"):
            index[norm] = str(case["name"])
    out: list[str] = []
    for q in questions or []:
        name = index.get(normalize_question(q))
        if name and name not in out:
            out.append(name)
    return out


def alias_entries_from_suggestions(suggestions: list[dict], questions: list[str],
                                   entities: list[dict],
                                   proposal_by_q: dict[int, str]) -> tuple[list, list]:
    """scene 输出 → (entries, skipped)。逐条走 `add_concept_alias`（与人审 --adopt 同一判据）；
    批内叠加（后一条看得到前一条的写入），天然去重 + 挡批内冲突。"""
    working = [dict(e) for e in entities or []]
    entries: list[dict] = []
    skipped: list[dict] = []
    for s in suggestions or []:
        if not isinstance(s, dict):
            continue
        q = s.get("q")
        concept = str(s.get("concept") or "").strip()
        alias = str(s.get("alias") or "").strip()
        question = questions[q] if isinstance(q, int) and not isinstance(q, bool) \
            and 0 <= q < len(questions) else ""
        res = add_concept_alias(working, concept, alias)
        if res["changed"]:
            working = res["concepts"]
            entries.append({"concept": concept, "alias": alias,
                            "proposal_id": (proposal_by_q or {}).get(q, "") if isinstance(q, int) else "",
                            "question": question})
        else:
            skipped.append({"concept": concept, "alias": alias, "why": res["why"]})
    return entries, skipped


def constraint_entries_from_candidates(candidates: list[dict],
                                       picks: list[str] | None = None) -> tuple[list, list]:
    """constraint_candidates 产物 → (entries, skipped)。`picks` 指定子集；缺省全取（按 target_code 排序）。"""
    by_code = {str(c.get("target_code")): c for c in candidates or [] if isinstance(c, dict)}
    wanted = [str(p) for p in picks] if picks else sorted(by_code)
    entries: list[dict] = []
    skipped: list[dict] = []
    for code in wanted:
        c = by_code.get(code)
        if c is None:
            skipped.append({"target_code": code, "why": "候选文件里没有这个 target_code"})
            continue
        entries.append({"target_code": code,
                        "expression": str(c.get("draft_expression") or ""),
                        "raw_description": str(c.get("raw_description") or "")[:200]})
    return entries, skipped


_RELATION_FIELDS = frozenset(
    {"source", "source_column", "target", "target_column"})


def entry_key(entry: dict, kind: str | None = None) -> str:
    """人审 --pick 用的稳定条目键。

    有 `kind` 时**只按 kind 取字段**（不嗅探任意 source/target 键）：

    - `alias` → `Concept:别名`
    - `query_rule` → `Entity.attr`（缺 target_code 时回退 expression）
    - `relation` → 边名
    - `constraint` → 约束 name（add 条目在规划后也带生成名）
    - `mapping` → `target_code|ds|entity|column`（同一抽象属性的多个落点不能共用
      `target_code` 互相串选）
    - `tool_schema` → 工具 name（`TOOL_SCHEMAS` 里的标识符；同批同一工具可能
      被 update / disable 多次，人审 --pick 按 name 整组选中）
    - `synonym` → `词1/词2:canonical`（同批 term 不重复；canonical 进键让
      「同词换名」的候选与旧组可区分）

    `kind` 缺省时保留旧调用口径（concept → alias，target_code → query_rule；
    relation 需要**四个端点字段齐全**才认，避免把普通条目的 source/target
    形状误判成 join 边）。
    """
    entry = entry or {}
    kind = str(kind or "").strip()
    if kind == "alias":
        return f"{entry.get('concept')}:{entry.get('alias')}"
    if kind == "query_rule":
        return str(entry.get("target_code") or entry.get("expression") or "")
    if kind in ("relation", "constraint", "tool_schema"):
        return str(entry.get("name") or "")
    if kind == "mapping":
        # 同一抽象属性可以有多个落点（不同源/实体/列），只按 target_code 取键会
        # 让它们互相串选 —— P1-5 用「抽象锚点 + 物理三元组」的复合键。
        return mapping_key(entry.get("target_code"), entry.get("data_source_id"),
                           entry.get("entity_name"), entry.get("attribute_name"))
    if kind == "synonym":
        terms = [str(term).strip() for term in entry.get("terms") or []
                 if str(term).strip()]
        return f"{'/'.join(terms)}:{str(entry.get('canonical') or '').strip()}"
    if "concept" in entry:
        return f"{entry.get('concept')}:{entry.get('alias')}"
    if "target_code" in entry:
        return str(entry.get("target_code") or "")
    if _RELATION_FIELDS <= set(entry):
        return str(entry.get("name") or "")
    return ""


def apply_alias_entries(schema_json: dict, entries: list[dict]) -> tuple[dict, list, list]:
    """抽象 schema（`{"entities":[...], ...}`）→ (新 schema, added, skipped)。不改入参。"""
    working = [dict(e) for e in (schema_json or {}).get("entities") or []]
    added: list[dict] = []
    skipped: list[dict] = []
    for e in entries or []:
        res = add_concept_alias(working, e.get("concept"), e.get("alias"))
        if res["changed"]:
            working = res["concepts"]
            added.append({"concept": str(e.get("concept")).strip(),
                          "alias": str(e.get("alias")).strip()})
        else:
            skipped.append({"concept": e.get("concept"), "alias": e.get("alias"),
                            "why": res["why"]})
    return {**(schema_json or {}), "entities": working}, added, skipped


def rollback_alias_entries(schema_json: dict, added: list[dict]) -> dict:
    """移除 added 的 (concept, alias)（strip 比较）。找不到的忽略（幂等）。不改入参。"""
    targets = {(str(a.get("concept") or "").strip(), str(a.get("alias") or "").strip())
               for a in added or []}
    out = []
    for c in (schema_json or {}).get("entities") or []:
        if not isinstance(c, dict):
            out.append(c)
            continue
        name = str(c.get("name") or "").strip()
        aliases = [str(a) for a in (c.get("aliases") or [])]
        kept = [a for a in aliases if (name, a.strip()) not in targets]
        out.append({**c, "aliases": kept} if len(kept) != len(aliases) else c)
    return {**(schema_json or {}), "entities": out}


def apply_constraint_entries(row_json: dict, entries: list[dict]) -> tuple[dict, list, list]:
    """编译行（`entity_types`/`constraints`/…）→ (新行, added, skipped)。逐条走 `build_query_rule`。"""
    entities = list((row_json or {}).get("entity_types") or [])
    constraints = list((row_json or {}).get("constraints") or [])
    added: list[dict] = []
    skipped: list[dict] = []
    for e in entries or []:
        res = build_query_rule(entities, constraints, e.get("target_code"), e.get("expression"))
        if res["changed"]:
            rule = res["rule"]
            constraints.append(rule)
            added.append({"name": rule["name"], "target_code": rule["target_code"]})
        else:
            skipped.append({"target_code": e.get("target_code"), "why": res["why"]})
    return {**(row_json or {}), "constraints": constraints}, added, skipped


def rollback_constraint_entries(row_json: dict, added: list[dict]) -> dict:
    """按 name 移除本批新增的约束。找不到的忽略（幂等）。不改入参。"""
    names = {str(a.get("name")) for a in added or []}
    kept = [c for c in (row_json or {}).get("constraints") or []
            if not (isinstance(c, dict) and str(c.get("name")) in names)]
    return {**(row_json or {}), "constraints": kept}


# ------------------------------------------------------------------ relation 补边

#: 单批 relation 条目上限（brief R4）。
RELATION_MAX_ENTRIES = 50
#: relation 支持的两种操作（P1 收尾）：`add` 补边、`remove` 按边名删除。
#: `op` 缺省视为 `add`；同一批只允许一种 op（混批是结构性错误 → 1001）。
RELATION_OPS = frozenset({"add", "remove"})
#: 生成边名的上限（超长截断；显式 name 超长 → skipped）。
_RELATION_NAME_MAX = 64
def normalize_relation_cardinality(value) -> str:
    """基数归一：**复用编译器的 `CARDINALITY_MAP`**（含中文别名与规范值）；
    空/未知 → `custom`（与编译器同口径，但不抛错）。

    复用映射而不是自己维护一份枚举，避免「预览/台账写 `一对多`、落库行被
    编译器归一成 `one_to_many`」这种前后不一致。
    """
    # 函数内 import：engine 是纯函数模块，避免模块加载期拉起编译器依赖图。
    from ..ontology_compiler import CARDINALITY_MAP

    return CARDINALITY_MAP.get(str(value or "").strip(), "custom")


def relation_edge_name(source: str, source_column: str,
                       target: str, target_column: str) -> str:
    """缺省边名：`REL_<SOURCE>_<COL>_<TARGET>_<COL>`，全大写、非字母数字→`_`、≤64。"""
    raw = f"REL_{source}_{source_column}_{target}_{target_column}"
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in raw).upper()
    return cleaned[:_RELATION_NAME_MAX]


def _entity_index(row_json: dict) -> dict[str, dict]:
    return {str(e.get("name")): e
            for e in (row_json or {}).get("entity_types") or []
            if isinstance(e, dict) and str(e.get("name") or "").strip()}


def _resolve_attribute(entity: dict, column) -> str | None:
    """列引用 → 实体属性上的**逻辑名**；`name` 或 `physical_name` 都认。"""
    wanted = str(column or "").strip()
    if not wanted:
        return None
    for attr in entity.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        name = str(attr.get("name") or "").strip()
        physical = str(attr.get("physical_name") or "").strip()
        if wanted == name or (physical and wanted == physical):
            return name or None
    return None


def _relation_pairs(relations: list[dict]) -> set[tuple[str, str, str, str]]:
    """已有边 → `{(source, source_col, target, target_col)}`（列空串归一）。"""
    return {(str(r.get("source") or ""), str(r.get("source_column") or ""),
             str(r.get("target") or ""), str(r.get("target_column") or ""))
            for r in relations or [] if isinstance(r, dict)}


def _relation_names(relations: list[dict]) -> set[str]:
    return {str(r.get("name")) for r in relations or []
            if isinstance(r, dict) and r.get("name")}


def _relation_pos(relations: list, name: str) -> int | None:
    """按边名定位 relation 在列表中的位置（None = 不存在）。"""
    return next((i for i, r in enumerate(relations)
                 if isinstance(r, dict) and str(r.get("name")) == name), None)


def _relation_batch_op(entries: list) -> str:
    """整批 relation 的 op：缺省 add；未知 op / 混批 → 1001（结构性错误）。

    **先于任何逐条跳过**判定：混 op 不是「某一条不合法」，而是这个批次本身
    不成立（add 与 remove 的端点校验、target_cases 与回滚记账都无法共用）。
    非对象条目留给主循环的 `条目必须是对象`；它们不贡献 op。
    """
    seen: set[str] = set()
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("op") or "").strip() or "add"
        if op not in RELATION_OPS:
            raise InvalidParameterError(
                message=f"未知的 relation 操作: {op!r}（支持 add/remove）")
        seen.add(op)
    if len(seen) > 1:
        raise InvalidParameterError(
            message="同一 relation 批次只能使用一种 op（add 或 remove），不能混批")
    return next(iter(seen)) if seen else "add"


def _plan_relation_removals(relations: list[dict],
                            entries: list) -> tuple[list[dict], list[dict]]:
    """remove 分支：只校验 `name` 与存在性；批内按顺序模拟删除。

    planned 记录形状 `{op, name, source, source_column, target, target_column,
    cardinality, description, before, index}`：`before` 是原边对象的浅拷贝
    （回滚按它复原），`index` 是弹出时在**当前列表**中的位置。端点与边的
    属性（cardinality/description）从原边抄到顶层，前端/台账无需再查 schema。
    """
    working = list(relations)
    planned: list[dict] = []
    skipped: list[dict] = []

    for index, raw in enumerate(entries or []):
        if index >= RELATION_MAX_ENTRIES:
            skipped.append({"index": index,
                            "why": f"超过单批条目上限 {RELATION_MAX_ENTRIES} 条"})
            continue
        if not isinstance(raw, dict):
            skipped.append({"index": index, "why": "条目必须是对象"})
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            skipped.append({"index": index, "why": "缺少字段：name"})
            continue
        pos = _relation_pos(working, name)
        if pos is None:
            skipped.append({"index": index, "name": name,
                            "why": "edge_not_found"})
            continue
        edge = working.pop(pos)
        planned.append({
            "op": "remove", "name": name,
            "source": edge.get("source"),
            "source_column": edge.get("source_column"),
            "target": edge.get("target"),
            "target_column": edge.get("target_column"),
            # 边的属性也抄到顶层：RunDetail 的 remove 分支与采纳台账不再为空。
            "cardinality": edge.get("cardinality"),
            "description": edge.get("description"),
            "before": dict(edge),
            "index": pos,
        })
    return planned, skipped


def plan_relation_entries(row_json: dict,
                          entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """relation 条目 **dry-run 校验** → (planned, skipped)。不写行、不改入参。

    planned 已归一：实体/列 resolved 成编译行的逻辑名、缺省边名已生成。
    `build_patch` 用它把「全被跳过」变成无条目（empty-draft）。
    `op` 缺省为 `add`（原路径逐字节不变）；`remove` 只按边名定位。
    """
    batch_op = _relation_batch_op(entries)
    relations = [r for r in (row_json or {}).get("relation_types") or []
                 if isinstance(r, dict)]
    if batch_op == "remove":
        return _plan_relation_removals(relations, entries)

    entities = _entity_index(row_json)
    pairs = _relation_pairs(relations)
    names = _relation_names(relations)
    planned: list[dict] = []
    skipped: list[dict] = []

    for index, raw in enumerate(entries or []):
        if index >= RELATION_MAX_ENTRIES:
            skipped.append({"index": index,
                            "why": f"超过单批条目上限 {RELATION_MAX_ENTRIES} 条"})
            continue
        if not isinstance(raw, dict):
            skipped.append({"index": index, "why": "条目必须是对象"})
            continue
        source = str(raw.get("source") or "").strip()
        source_column_raw = str(raw.get("source_column") or "").strip()
        target = str(raw.get("target") or "").strip()
        target_column_raw = str(raw.get("target_column") or "").strip()
        missing = [field for field, value in (
            ("source", source), ("source_column", source_column_raw),
            ("target", target), ("target_column", target_column_raw)) if not value]
        if missing:
            skipped.append({"index": index, "source": source,
                            "target": target,
                            "why": f"缺少字段：{', '.join(missing)}"})
            continue

        source_entity = entities.get(source)
        if source_entity is None:
            skipped.append({"index": index, "source": source,
                            "why": f"实体不在编译 schema 里：{source}"})
            continue
        target_entity = entities.get(target)
        if target_entity is None:
            skipped.append({"index": index, "target": target,
                            "why": f"实体不在编译 schema 里：{target}"})
            continue
        if source == target:
            skipped.append({"index": index, "source": source, "target": target,
                            "why": "source 与 target 是同一实体（不退化为自连边）"})
            continue

        source_column = _resolve_attribute(source_entity, source_column_raw)
        if source_column is None:
            skipped.append({"index": index, "source": source,
                            "source_column": source_column_raw,
                            "why": f"{source} 没有列 {source_column_raw}"})
            continue
        target_column = _resolve_attribute(target_entity, target_column_raw)
        if target_column is None:
            skipped.append({"index": index, "target": target,
                            "target_column": target_column_raw,
                            "why": f"{target} 没有列 {target_column_raw}"})
            continue

        name = str(raw.get("name") or "").strip()
        if name and len(name) > _RELATION_NAME_MAX:
            skipped.append({"index": index, "name": name,
                            "why": f"边名超长（{len(name)} > {_RELATION_NAME_MAX}）"})
            continue
        name = name or relation_edge_name(
            source, source_column, target, target_column)

        forward = (source, source_column, target, target_column)
        reverse = (target, target_column, source, source_column)
        if forward in pairs:
            skipped.append({"index": index, "source": source,
                            "source_column": source_column, "target": target,
                            "target_column": target_column,
                            "why": "同向 join 边已存在"})
            continue
        if reverse in pairs:
            skipped.append({"index": index, "source": source,
                            "source_column": source_column, "target": target,
                            "target_column": target_column,
                            "why": "反向 join 边已存在"})
            continue
        if name in names:
            skipped.append({"index": index, "name": name,
                            "why": f"关系编码已存在：{name}"})
            continue

        cardinality = normalize_relation_cardinality(raw.get("cardinality"))
        description = str(raw.get("description") or "").strip() or (
            f"{source}.{source_column} → {target}.{target_column}（演化补边）")
        planned.append({
            "source": source, "source_column": source_column,
            "target": target, "target_column": target_column,
            "name": name, "cardinality": cardinality,
            "description": description,
        })
        # 批内叠加：后一条看得到前一条（与 alias 的批内去重同姿态）。
        pairs.add(forward)
        pairs.add(reverse)
        names.add(name)

    return planned, skipped


def _relation_edge(entry: dict) -> dict:
    """planned entry → compiled `relation_types` 元素（与生成器同形）。"""
    name = str(entry["name"])
    return {
        "name": name,
        "origin": "evolved",
        "source": entry["source"],
        "target": entry["target"],
        "source_column": entry["source_column"],
        "target_column": entry["target_column"],
        "cardinality": normalize_relation_cardinality(
            entry.get("cardinality")),
        "description": entry.get("description") or "",
        "status": "active",
        "display_name": name,
        "aliases": [],
        "inverse_of": None,
        "source_types": [],
        "target_types": [],
        "source_relation_id": None,
    }


def apply_relation_entries(row_json: dict,
                           entries: list[dict]) -> tuple[dict, list, list]:
    """编译行（`entity_types`/`relation_types`/`constraints`）→ (新行, added, skipped)。

    add 逐条校验（实体/列/重复/反向/边名），不合法的进 skipped 且不写；
    `added` 带边名与两端（`{name, source, source_column, target,
    target_column}`），前端预览/台账可渲染；回滚仍只按 `name` 匹配。
    remove（P1 收尾）按 name 在当前列表定位并 `pop`，`added` 用**实际弹出**
    的对象与 index 记账（`{op, name, ..., cardinality, description, before,
    index}`），供回滚按原位插回、前端/台账读边属性。
    """
    planned, skipped = plan_relation_entries(row_json, entries)
    relations = list((row_json or {}).get("relation_types") or [])
    added: list[dict] = []
    for entry in planned:
        if str(entry.get("op") or "") == "remove":
            name = str(entry.get("name") or "")
            pos = _relation_pos(relations, name)
            if pos is None:
                # 计划与应用的间隙里被并发删掉：降级成 skipped，不当成写成功。
                skipped.append({"index": entry.get("index"), "name": name,
                                "why": "edge_not_found"})
                continue
            popped = relations.pop(pos)
            added.append({
                "op": "remove", "name": name,
                "source": popped.get("source"),
                "source_column": popped.get("source_column"),
                "target": popped.get("target"),
                "target_column": popped.get("target_column"),
                "cardinality": popped.get("cardinality"),
                "description": popped.get("description"),
                "before": dict(popped),
                "index": pos,
            })
            continue
        edge = _relation_edge(entry)
        relations.append(edge)
        added.append({
            "name": edge["name"],
            "source": edge["source"],
            "source_column": edge["source_column"],
            "target": edge["target"],
            "target_column": edge["target_column"],
        })
    return {**(row_json or {}), "relation_types": relations}, added, skipped


def rollback_relation_entries(row_json: dict, added: list[dict]) -> dict:
    """逆操作本批 relation 改动（找不到的忽略 = 幂等）。不改入参。

    - `add` → 按 name 移除（原行为）；
    - `remove`（P1 收尾）→ 把 `before` 按原 `index` 插回。**逆序**处理，
      保证同批多删后顺序与操作前一致（内容指纹可复原）；`before` 已在列表
      中说明回滚过（或已被并发补回），跳过而不是插第二条。
    """
    relations = list((row_json or {}).get("relation_types") or [])
    for record in reversed(list(added or [])):
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "")
        if str(record.get("op") or "") == "remove":
            before = record.get("before")
            if not isinstance(before, dict):
                continue
            before_name = str(before.get("name") or name)
            if _relation_pos(relations, before_name) is not None:
                continue
            index = record.get("index")
            pos = (len(relations)
                   if not isinstance(index, int) or isinstance(index, bool)
                   else min(max(index, 0), len(relations)))
            relations.insert(pos, dict(before))
            continue
        relations = [r for r in relations
                     if not (isinstance(r, dict)
                             and str(r.get("name")) == name)]
    return {**(row_json or {}), "relation_types": relations}


# ------------------------------------------------------- constraint 可编辑（P1-4）

#: 单批 constraint 条目上限（与 relation 同口径）。
CONSTRAINT_MAX_ENTRIES = 50
#: constraint 支持的四类操作（R3：**不做**物理删除 `remove`，人审只停用）。
CONSTRAINT_OPS = frozenset({"add", "update", "disable", "enable"})
#: add 条目唯一允许的约束类型（R2：不新建 unique_attribute 生成逻辑）。
_CONSTRAINT_ADD_TYPES = frozenset({"query_rule"})


def _constraint_enabled(item: dict) -> bool:
    """约束是否启用：与归一化/注入同口径（R1 fail-open，只有显式 False 停用）。"""
    return item.get("enabled") is not False


def _constraint_pos(constraints: list, name: str) -> int | None:
    """按 name 找约束在列表中的位置（None = 不存在）。"""
    return next((i for i, c in enumerate(constraints)
                 if isinstance(c, dict) and str(c.get("name")) == name), None)


def plan_constraint_ops(row_json: dict,
                        entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """constraint 条目 **dry-run 校验** → (planned, skipped)。不写行、不改入参。

    planned 已归一：`add` 带上生成名（`query_rule_Entity_attr`）与
    `constraint_type`，`update` 带新 `expression`；update/disable/enable 都带
    `target_code` / `constraint_type`（从当前行抄，人审 adjust/edit 据此定位
    靶点）。**非 add 条目另带 `before`**（与回滚记账同一份：update →
    `{expression, constraint_type}`，disable/enable → `{enabled}`；前态本身
    缺 `enabled` 键时为空对象，表示回滚要删键而不是写回），前端补丁预览
    据此显示「旧值 → 新值」；`add` 没有旧值，不带该键。
    批内按顺序模拟（后一条看得到前一条），与 relation/alias 同姿态 ——
    `build_patch` 据此把「全被跳过」收口成 empty-draft。

    结构性问题（未知 op / add 用 unique_attribute）抛 1001；单条业务问题
    （重复列 / name 不存在 / 表达式缺失或超长 / 已停用或已启用）进 skipped。
    """
    entities = [e for e in (row_json or {}).get("entity_types") or []
                if isinstance(e, dict)]
    working = [dict(c) for c in (row_json or {}).get("constraints") or []
               if isinstance(c, dict)]
    planned: list[dict] = []
    skipped: list[dict] = []
    for index, raw in enumerate(entries or []):
        if index >= CONSTRAINT_MAX_ENTRIES:
            skipped.append({"index": index,
                            "why": f"超过单批条目上限 {CONSTRAINT_MAX_ENTRIES} 条"})
            continue
        if not isinstance(raw, dict):
            skipped.append({"index": index, "why": "条目必须是对象"})
            continue
        op = str(raw.get("op") or "").strip()
        if op not in CONSTRAINT_OPS:
            raise InvalidParameterError(
                message=(f"未知的约束操作: {op or '(空)'}"
                         "（支持 add/update/disable/enable）"))
        if op == "add":
            # R2：add 只生成 query_rule；unique_attribute 等已有类型只能编辑。
            ctype = str(raw.get("constraint_type") or "").strip() or "query_rule"
            if ctype not in _CONSTRAINT_ADD_TYPES:
                raise InvalidParameterError(
                    message=("constraint add 只支持 constraint_type=query_rule"
                             f"（收到 {ctype!r}）"))
            res = build_query_rule(entities, working,
                                   raw.get("target_code"), raw.get("expression"))
            if not res["changed"]:
                skipped.append({"index": index, "op": op,
                                "target_code": raw.get("target_code"),
                                "why": res["why"]})
                continue
            rule = res["rule"]
            working.append(rule)
            planned.append({"op": "add", "name": rule["name"],
                            "target_code": rule["target_code"],
                            "constraint_type": "query_rule",
                            "expression": rule["expression"]})
            continue

        name = str(raw.get("name") or "").strip()
        pos = _constraint_pos(working, name)
        if not name or pos is None:
            skipped.append({"index": index, "op": op, "name": name,
                            "why": "name_not_found"})
            continue
        if op == "update":
            expression = str(raw.get("expression") or "").strip()
            if not expression:
                skipped.append({"index": index, "op": op, "name": name,
                                "why": "expression_required"})
                continue
            if len(expression) > MAX_EXPRESSION_CHARS:
                skipped.append({"index": index, "op": op, "name": name,
                                "why": "expression_too_long"})
                continue
            # update 只改 expression；enabled / constraint_type 原样保留。
            before = {"expression": working[pos].get("expression"),
                      "constraint_type": working[pos].get("constraint_type")}
            working[pos] = {**working[pos], "expression": expression}
            planned.append({"op": "update", "name": name,
                            "target_code": working[pos].get("target_code"),
                            "constraint_type": working[pos].get("constraint_type"),
                            "expression": expression, "before": before})
            continue

        want = op == "enable"
        old_enabled = _constraint_enabled(working[pos])
        if old_enabled == want:
            skipped.append({"index": index, "op": op, "name": name,
                            "why": "already_enabled" if want else "already_disabled"})
            continue
        # 前态**有没有这个键**是形状的一部分：存量 7 键行停用后回滚必须
        # 回到「缺键」（读取侧 fail-open = 启用），不能写出 `enabled: true`
        # 把内容指纹永久改掉（老版本快照就再也恢复不回来了）。
        before = ({"enabled": old_enabled}
                  if "enabled" in working[pos] else {})
        working[pos] = {**working[pos], "enabled": want}
        planned.append({"op": op, "name": name,
                        "target_code": working[pos].get("target_code"),
                        "constraint_type": working[pos].get("constraint_type"),
                        "before": before})
    return planned, skipped


def apply_constraint_ops(row_json: dict,
                         entries: list[dict]) -> tuple[dict, list, list]:
    """编译行 → (新行, added, skipped)：add / update / disable / enable。

    `added` 记 `{op, name, before}`（add 无 before，回滚按 name 移除）：
    - `update` → `before={"expression", "constraint_type"}`；
    - `disable` / `enable` → `before={"enabled": 旧布尔}`；前态缺该键时
      `before={}`（回滚删键，恢复存量 7 键形状）。
    找不到目标的条目进 skipped（幂等），绝不静默改写别的约束。
    """
    planned, skipped = plan_constraint_ops(row_json, entries)
    entities = [e for e in (row_json or {}).get("entity_types") or []
                if isinstance(e, dict)]
    constraints = list((row_json or {}).get("constraints") or [])
    added: list[dict] = []
    for entry in planned:
        op = str(entry["op"])
        name = str(entry["name"])
        if op == "add":
            # 与 plan 同一判据（批内叠加也一致）；行在两阶段间被并发改过时
            # 这一条会降级成 skipped，而不是覆盖别人刚写进去的口径。
            res = build_query_rule(
                entities, [c for c in constraints if isinstance(c, dict)],
                entry.get("target_code"), entry.get("expression"))
            if not res["changed"]:
                skipped.append({"op": op, "name": name,
                                "target_code": entry.get("target_code"),
                                "why": res["why"]})
                continue
            rule = res["rule"]
            constraints.append(rule)
            added.append({"op": "add", "name": rule["name"]})
            continue
        pos = _constraint_pos(constraints, name)
        if pos is None:
            skipped.append({"op": op, "name": name, "why": "name_not_found"})
            continue
        item = constraints[pos]
        if op == "update":
            before = {"expression": item.get("expression"),
                      "constraint_type": item.get("constraint_type")}
            constraints[pos] = {**item, "expression": entry["expression"]}
            added.append({"op": "update", "name": name, "before": before})
        else:
            # 与 plan 同口径：前态缺键 → before 不带键，回滚时把键删掉。
            before = ({"enabled": _constraint_enabled(item)}
                      if "enabled" in item else {})
            constraints[pos] = {**item, "enabled": op == "enable"}
            added.append({"op": op, "name": name, "before": before})
    return {**(row_json or {}), "constraints": constraints}, added, skipped


def _restore_key(item: dict, key: str, value, *, present: bool) -> dict:
    """回滚单个键：前态**有**该键才写回；前态没有就删除，恢复 7 键形状。

    内容指纹是版本快照的一部分：存量行缺 `enabled` 时，回滚若写一个
    `enabled: true`，这次「停用 → 撤销」会把 7 键行永久变成 8 键，
    之后老版本快照的 hash 就再也对不上。
    """
    if present:
        return {**item, key: value}
    return {k: v for k, v in item.items() if k != key}


def rollback_constraint_ops(row_json: dict, added: list[dict]) -> dict:
    """逆操作本批约束改动（按 name 定位；找不到的忽略 = 幂等）。不改入参。

    逆序处理，保证同一条约束在一批里被多次改（update → disable）时逐项复原：
    `add` 移除、`update` 恢复 before.expression、`disable`/`enable` 恢复
    before.enabled。**形状复原**：前态缺某个键时（存量 7 键行）回滚把键删掉，
    而不是写一个默认值 —— 否则内容指纹被永久改写。
    """
    constraints = list((row_json or {}).get("constraints") or [])
    for record in reversed(list(added or [])):
        if not isinstance(record, dict):
            continue
        op = str(record.get("op") or "")
        name = str(record.get("name") or "")
        pos = _constraint_pos(constraints, name)
        if pos is None:
            continue
        if op == "add":
            constraints.pop(pos)
        elif op == "update":
            before = record.get("before") or {}
            constraints[pos] = _restore_key(
                constraints[pos], "expression", before.get("expression"),
                present="expression" in before)
        elif op in ("disable", "enable"):
            before = record.get("before") or {}
            constraints[pos] = _restore_key(
                constraints[pos], "enabled",
                before.get("enabled") is not False,
                present="enabled" in before)
    return {**(row_json or {}), "constraints": constraints}


# ------------------------------------------------------- mapping 对象化（P1-5）

#: 单批 mapping 条目上限（与 relation / constraint 同口径）。
MAPPING_MAX_ENTRIES = 50
#: mapping 支持的四类操作（与 constraint 同口径：不做物理删除 `remove`）。
MAPPING_OPS = frozenset({"add", "update", "disable", "enable"})
#: 落点上的语义槽。`enabled` 不在其中：它由 disable / enable 单独维护，
#: update 只改「口径」类槽。
MAPPING_SLOT_KEYS = ("role", "filter", "aggregation", "grain")
#: 单个槽文本上限（与 `concept_expansion.MAX_LANDING_SLOT_CHARS` 同口径）。
MAX_MAPPING_SLOT_CHARS = 240


def mapping_key(target_code, data_source_id, entity_name, attribute_name) -> str:
    """mapping 条目的复合身份键：`target_code|ds|entity|column`（None → 空串）。

    抽象锚点（`Entity.attr`）只回答「哪个概念字段」，物理三元组才回答「哪个源的
    哪张表的哪一列」。同一个物理列可以合法地同时是两个抽象属性的落点，同一个抽象
    属性也可以有多个源落点 —— 只用 `target_code` 会让人审 --pick 互相串选。
    """
    return "|".join(_mapping_text(value) for value in (
        target_code, data_source_id, entity_name, attribute_name))


def _mapping_text(value) -> str:
    return "" if value is None else str(value).strip()


def _mapping_entity(schema_json: dict, name: str) -> dict | None:
    """按实体名找抽象实体（大小写不敏感）。"""
    wanted = _mapping_text(name).casefold()
    if not wanted:
        return None
    for entity in (schema_json or {}).get("entities") or []:
        if isinstance(entity, dict) \
                and _mapping_text(entity.get("name")).casefold() == wanted:
            return entity
    return None


def _mapping_attribute(entity: dict, name: str) -> dict | None:
    """按属性名找抽象属性（大小写不敏感）。"""
    wanted = _mapping_text(name).casefold()
    if not wanted:
        return None
    for attr in entity.get("attributes") or []:
        if isinstance(attr, dict) \
                and _mapping_text(attr.get("name")).casefold() == wanted:
            return attr
    return None


def _mapping_landing_pos(sources: list, data_source_id, entity_name,
                        attribute_name) -> int | None:
    """在该属性的 `sources[]` 里按物理三元组定位落点（大小写不敏感）。"""
    ds = _mapping_text(data_source_id).casefold()
    entity = _mapping_text(entity_name).casefold()
    attr = _mapping_text(attribute_name).casefold()
    for pos, landing in enumerate(sources or []):
        if not isinstance(landing, dict):
            continue
        if (_mapping_text(landing.get("data_source_id")).casefold() == ds
                and _mapping_text(landing.get("entity_name")).casefold() == entity
                and _mapping_text(landing.get("attribute_name")).casefold() == attr):
            return pos
    return None


def _mapping_entity_landed(entity: dict, data_source_id,
                           entity_name) -> str | None:
    """抽象实体在该源上是否有 `included is not False` 的实体级落点。

    命中时返回落点上**规范大小写**的 `entity_name`（入参大小写不敏感），
    没有则 None —— `add` 必须把规范名写进落点：② 的注入按 `entity_name`
    精确匹配，写进大小写变体（`circuits`）会让这条 mapping 永远不被注入。
    ② 的候选扩展只认实体级落点（`concept_expansion`）：没有它，这条 mapping
    永远不会被选中 —— 与画布「字段落点必须依附实体落点」同一条硬门槛。
    """
    ds = _mapping_text(data_source_id).casefold()
    ent = _mapping_text(entity_name).casefold()
    for src in entity.get("sources") or []:
        if not isinstance(src, dict):
            continue
        if (_mapping_text(src.get("data_source_id")).casefold() == ds
                and _mapping_text(src.get("entity_name")).casefold() == ent
                and src.get("included") is not False):
            return _mapping_text(src.get("entity_name")) or entity_name
    return None


def _mapping_slots(landing: dict) -> dict:
    """落点的语义槽快照：`role/filter/aggregation/grain`（有才带）+ `enabled`
    （**只在该落点确实有这个键时**才带，原值照抄）。"""
    out: dict = {}
    for slot in MAPPING_SLOT_KEYS:
        if slot in landing and landing[slot] is not None:
            out[slot] = landing[slot]
    if "enabled" in landing:
        out["enabled"] = landing["enabled"]
    return out


def _mapping_slot_value(raw: dict, slot: str) -> tuple[bool, str | None]:
    """归一单个槽：→ `(ok, value)`。`value is None` = 不落键 / 删键。

    - 非字符串且非 None → 不合法（`invalid_slot`）；
    - strip 后空串 → 视同没有（update 里表示显式清空）；
    - strip 后超 `MAX_MAPPING_SLOT_CHARS` → 不合法。
    """
    value = raw.get(slot)
    if value is None:
        return True, None
    if not isinstance(value, str):
        return False, None
    cleaned = value.strip()
    if not cleaned:
        return True, None
    if len(cleaned) > MAX_MAPPING_SLOT_CHARS:
        return False, None
    return True, cleaned


def _mapping_identity(target_code: str, data_source_id: str, entity_name: str,
                      attribute_name: str) -> dict:
    return {
        "target_code": target_code,
        "data_source_id": data_source_id,
        "entity_name": entity_name,
        "attribute_name": attribute_name,
    }


def plan_mapping_ops(schema_json: dict, entries: list[dict], *,
                     columns_by_source: dict | None = None
                     ) -> tuple[list[dict], list[dict]]:
    """mapping 条目 **dry-run 校验** → (planned, skipped)。不写行、不改入参。

    批内模拟（后一条看得到前一条），与 relation / constraint 同姿态。
    planned 已归一（实体/属性/落点大小写不敏感定位，规范名写回），带：
    add → 身份 + 输入槽；update → 身份 + 输入槽 + `changed` / `before`；
    disable / enable → 身份 + `before`。**不写 `after`**（那是 apply 的产物）。

    结构性错误（未知 `op` / `target_type` 非空且不是 `"attribute"` /
    update 无任何槽键）抛 1001；单条业务问题进 skipped：
    `too_many` / `missing_field` / `target_not_found` / `landing_exists` /
    `source_not_found` / `column_not_found` / `entity_not_landed` /
    `invalid_slot` / `no_change` / `landing_not_found` /
    `already_disabled` / `already_enabled`。
    """
    working = copy.deepcopy(schema_json or {})
    planned: list[dict] = []
    skipped: list[dict] = []

    for index, raw in enumerate(entries or []):
        if index >= MAPPING_MAX_ENTRIES:
            skipped.append({"index": index, "why": "too_many"})
            continue
        if not isinstance(raw, dict):
            skipped.append({"index": index, "why": "missing_field"})
            continue
        op = _mapping_text(raw.get("op"))
        if op not in MAPPING_OPS:
            raise InvalidParameterError(
                message=(f"未知的 mapping 操作: {op or '(空)'}"
                         "（支持 add/update/disable/enable）"))
        target_type = _mapping_text(raw.get("target_type"))
        if target_type and target_type != "attribute":
            raise InvalidParameterError(
                message=(f"mapping 只支持 target_type=attribute"
                         f"（收到 {target_type!r}）"))

        target_code = _mapping_text(raw.get("target_code"))
        data_source_id = _mapping_text(raw.get("data_source_id"))
        entity_name = _mapping_text(raw.get("entity_name"))
        attribute_name = _mapping_text(raw.get("attribute_name"))
        ent_part, _, attr_part = target_code.partition(".")
        ent_part, attr_part = ent_part.strip(), attr_part.strip()
        if not (ent_part and attr_part and data_source_id and entity_name
                and attribute_name):
            skipped.append({"index": index, "op": op,
                            "target_code": target_code,
                            "why": "missing_field"})
            continue

        entity = _mapping_entity(working, ent_part)
        attr = _mapping_attribute(entity, attr_part) if entity is not None else None
        if entity is None or attr is None:
            skipped.append({"index": index, "op": op,
                            "target_code": target_code,
                            "why": "target_not_found"})
            continue
        canonical_target = (f"{_mapping_text(entity.get('name'))}"
                            f".{_mapping_text(attr.get('name'))}")
        sources = attr.get("sources")
        if not isinstance(sources, list):
            sources = []
            attr["sources"] = sources
        # 列名归一（物理名 / 大小写变体 → compiled slug）必须**先于**落点定位：
        # 否则构建期用物理名找不到落点、按 slug 计划 add，应用期重规划按 slug
        # 命中已有落点 → added 为空 → apply_patch 抛「零条生效」。先按原始名
        # 定位（兼容存量按物理名存的落点），找不到再用 slug 兜底。
        colmap = None
        if columns_by_source is not None:
            colmap = columns_by_source.get(
                (data_source_id, entity_name.casefold()))
        pos = _mapping_landing_pos(sources, data_source_id, entity_name,
                                   attribute_name)
        if pos is None and colmap is not None:
            slug = colmap.get(attribute_name.casefold())
            if slug:
                attribute_name = _mapping_text(slug) or attribute_name
                pos = _mapping_landing_pos(sources, data_source_id,
                                           entity_name, attribute_name)
        landing = sources[pos] if pos is not None else None
        if landing is not None:
            canonical_ds = _mapping_text(landing.get("data_source_id"))
            canonical_entity = _mapping_text(landing.get("entity_name"))
            canonical_attribute = _mapping_text(landing.get("attribute_name"))
        else:
            canonical_ds, canonical_entity, canonical_attribute = (
                data_source_id, entity_name, attribute_name)
        identity = _mapping_identity(canonical_target, canonical_ds,
                                     canonical_entity, canonical_attribute)

        if op == "add":
            if pos is not None:
                skipped.append({"index": index, "op": op, **identity,
                                "why": "landing_exists"})
                continue
            if columns_by_source is not None:
                if colmap is None:
                    skipped.append({"index": index, "op": op, **identity,
                                    "why": "source_not_found"})
                    continue
                slug = colmap.get(attribute_name.casefold())
                if slug is None:
                    skipped.append({"index": index, "op": op, **identity,
                                    "why": "column_not_found"})
                    continue
                # 写回编译 schema 的规范列名（slug）
                canonical_attribute = _mapping_text(slug) or attribute_name
                identity["attribute_name"] = canonical_attribute
            landed_entity = _mapping_entity_landed(
                entity, data_source_id, entity_name)
            if landed_entity is None:
                skipped.append({"index": index, "op": op, **identity,
                                "why": "entity_not_landed"})
                continue
            # 实体级落点的规范大小写：② 的注入是按 `entity_name` 精确匹配的。
            canonical_entity = landed_entity
            identity["entity_name"] = canonical_entity
            slots: dict = {}
            invalid = False
            for slot in MAPPING_SLOT_KEYS:
                if slot not in raw:
                    continue
                ok, value = _mapping_slot_value(raw, slot)
                if not ok:
                    invalid = True
                    break
                if value is not None:
                    slots[slot] = value
            if invalid:
                skipped.append({"index": index, "op": op, **identity,
                                "why": "invalid_slot"})
                continue
            record = {"op": "add", **identity, **slots}
            if "enabled" in raw:
                record["enabled"] = raw.get("enabled") is not False
            planned.append(record)
            new_landing = {
                "data_source_id": canonical_ds,
                "entity_name": canonical_entity,
                "attribute_name": canonical_attribute,
            }
            new_landing.update(slots)
            if "enabled" in record:
                new_landing["enabled"] = record["enabled"]
            sources.append(new_landing)
            continue

        if op == "update":
            if not any(slot in raw for slot in MAPPING_SLOT_KEYS):
                raise InvalidParameterError(
                    message=("mapping update 至少需要一个槽："
                             "role/filter/aggregation/grain"))
            if landing is None:
                skipped.append({"index": index, "op": op, **identity,
                                "why": "landing_not_found"})
                continue
            changed: dict = {}
            before: dict = {}
            carried: dict = {}
            invalid = False
            for slot in MAPPING_SLOT_KEYS:
                if slot not in raw:
                    continue
                ok, value = _mapping_slot_value(raw, slot)
                if not ok:
                    invalid = True
                    break
                # planned 必须能原样再喂回 apply：显式清空保留空串
                carried[slot] = value or ""
                if value is None:
                    if slot in landing:
                        changed[slot] = None
                        before[slot] = landing.get(slot)
                elif landing.get(slot) != value:
                    changed[slot] = value
                    if slot in landing:
                        before[slot] = landing.get(slot)
            if invalid:
                skipped.append({"index": index, "op": op, **identity,
                                "why": "invalid_slot"})
                continue
            if not changed:
                skipped.append({"index": index, "op": op, **identity,
                                "why": "no_change"})
                continue
            planned.append({"op": "update", **identity, **carried,
                            "changed": changed, "before": before})
            for slot, value in changed.items():
                if value is None:
                    landing.pop(slot, None)
                else:
                    landing[slot] = value
            continue

        # disable / enable：幂等开关，enable 的落库形状是**删键**
        if landing is None:
            skipped.append({"index": index, "op": op, **identity,
                            "why": "landing_not_found"})
            continue
        if op == "disable":
            if landing.get("enabled") is False:
                skipped.append({"index": index, "op": op, **identity,
                                "why": "already_disabled"})
                continue
            before = ({"enabled": landing["enabled"]}
                      if "enabled" in landing else {})
            landing["enabled"] = False
        else:
            if landing.get("enabled") is not False:
                skipped.append({"index": index, "op": op, **identity,
                                "why": "already_enabled"})
                continue
            before = {"enabled": False}
            landing.pop("enabled", None)
        planned.append({"op": op, **identity, "before": before})

    return planned, skipped


def apply_mapping_ops(schema_json: dict, entries: list[dict], *,
                      columns_by_source: dict | None = None
                      ) -> tuple[dict, list, list]:
    """抽象 schema → (新 schema, added, skipped)：mapping 的 add/update/disable/enable。

    `added` 记 `{op, target_code, data_source_id, entity_name, attribute_name,
    before?, after}`（update 另带 `changed` 供逐键回滚）：
    - `after` = 应用后该落点**存在的**槽 + `enabled`（只含存在的键）；
    - `add` 无 `before`；`update` 的 `before` 只含被改且前态存在的槽；
    - `disable` / `enable` 的 `before` 记旧 `enabled`，前态缺键时为空对象
      （回滚删键，绝不写默认值）。
    落点在两阶段间被并发改掉时该条降级为 skipped，不覆盖别人刚写的口径。
    """
    planned, skipped = plan_mapping_ops(
        schema_json, entries, columns_by_source=columns_by_source)
    working = copy.deepcopy(schema_json or {})
    added: list[dict] = []

    for entry in planned:
        op = _mapping_text(entry.get("op"))
        target_code = _mapping_text(entry.get("target_code"))
        ent_part, _, attr_part = target_code.partition(".")
        entity = _mapping_entity(working, ent_part.strip())
        attr = _mapping_attribute(entity, attr_part.strip()) \
            if entity is not None else None
        if attr is None:
            skipped.append({"op": op, "target_code": target_code,
                            "why": "target_not_found"})
            continue
        identity = _mapping_identity(
            target_code, _mapping_text(entry.get("data_source_id")),
            _mapping_text(entry.get("entity_name")),
            _mapping_text(entry.get("attribute_name")))
        sources = attr.get("sources")
        if not isinstance(sources, list):
            sources = []
            attr["sources"] = sources
        pos = _mapping_landing_pos(
            sources, identity["data_source_id"], identity["entity_name"],
            identity["attribute_name"])

        if op == "add":
            if pos is not None:
                skipped.append({"op": op, **identity, "why": "landing_exists"})
                continue
            landing = {
                "data_source_id": identity["data_source_id"],
                "entity_name": identity["entity_name"],
                "attribute_name": identity["attribute_name"],
            }
            for slot in MAPPING_SLOT_KEYS:
                if slot in entry and entry[slot] not in (None, ""):
                    landing[slot] = entry[slot]
            if "enabled" in entry:
                landing["enabled"] = entry.get("enabled") is not False
            sources.append(landing)
            added.append({"op": "add", **identity,
                          "after": _mapping_slots(landing)})
            continue

        if pos is None:
            skipped.append({"op": op, **identity, "why": "landing_not_found"})
            continue
        landing = sources[pos]
        if op == "update":
            changed = dict(entry.get("changed") or {})
            before = dict(entry.get("before") or {})
            for slot, value in changed.items():
                if value is None:
                    landing.pop(slot, None)
                else:
                    landing[slot] = value
            added.append({"op": "update", **identity, "before": before,
                          "changed": changed,
                          "after": _mapping_slots(landing)})
            continue
        before = dict(entry.get("before") or {})
        if op == "disable":
            landing["enabled"] = False
        else:
            landing.pop("enabled", None)
        added.append({"op": op, **identity, "before": before,
                      "after": _mapping_slots(landing)})

    return working, added, skipped


def rollback_mapping_ops(schema_json: dict, added: list[dict]) -> dict:
    """逆操作本批 mapping 改动（按复合身份定位；找不到的忽略 = 幂等）。

    **逆序**处理，保证同一条落点在一批里被多次改（update → disable）时逐项复原：
    `add` 移除落点、`update` 按 `changed` 逐键恢复（`before` 缺该键 = 删键）、
    `disable` / `enable` 按 `before` 恢复 `enabled`（缺键 = 删键）。
    """
    working = copy.deepcopy(schema_json or {})
    for record in reversed(list(added or [])):
        if not isinstance(record, dict):
            continue
        op = _mapping_text(record.get("op"))
        target_code = _mapping_text(record.get("target_code"))
        ent_part, _, attr_part = target_code.partition(".")
        entity = _mapping_entity(working, ent_part.strip())
        attr = _mapping_attribute(entity, attr_part.strip()) \
            if entity is not None else None
        if attr is None:
            continue
        sources = attr.get("sources")
        if not isinstance(sources, list):
            continue
        pos = _mapping_landing_pos(sources, record.get("data_source_id"),
                                   record.get("entity_name"),
                                   record.get("attribute_name"))
        if op == "add":
            if pos is not None:
                sources.pop(pos)
            continue
        if pos is None:
            continue
        landing = sources[pos]
        if op == "update":
            before = record.get("before") or {}
            for slot in (record.get("changed") or {}):
                if slot in before:
                    landing[slot] = before[slot]
                else:
                    landing.pop(slot, None)
        elif op in ("disable", "enable"):
            before = record.get("before") or {}
            if "enabled" in before:
                landing["enabled"] = before["enabled"]
            else:
                landing.pop("enabled", None)
    return working


def mapping_state_matches(schema_json: dict, record: dict) -> bool:
    """回读核对：`added` 记录描述的目标状态是否已在 schema 里（纯函数）。

    - `add` / `update`：把落点按**同一套** `_mapping_slots` 投影后与记录的
      `after` 逐键比较 —— 不拿原始 JSON 比，否则「None 槽不落键」的形状差异
      会被误判成没落库；
    - `disable`：落点 `enabled` 必须**恰好是** `False`；
    - `enable`：落点必须**没有** `enabled` 键（enable 的落库形状是删键）。

    落点按复合身份（target_code + 物理三元组）大小写不敏感定位；找不到 → False。
    """
    record = record or {}
    target_code = _mapping_text(record.get("target_code"))
    ent_part, _, attr_part = target_code.partition(".")
    entity = _mapping_entity(schema_json or {}, ent_part.strip())
    attr = _mapping_attribute(entity, attr_part.strip()) \
        if entity is not None else None
    if attr is None:
        return False
    sources = attr.get("sources")
    if not isinstance(sources, list):
        return False
    pos = _mapping_landing_pos(sources, record.get("data_source_id"),
                               record.get("entity_name"),
                               record.get("attribute_name"))
    if pos is None:
        return False
    landing = sources[pos]
    op = _mapping_text(record.get("op"))
    if op == "disable":
        return landing.get("enabled") is False
    if op == "enable":
        return "enabled" not in landing
    after = record.get("after")
    if not isinstance(after, dict):
        return False
    return _mapping_slots(landing) == after


def mapping_added_verified(schema_json: dict, added: list[dict]) -> bool:
    """整批 mapping 的回读核对：**每个落点只看最后一条记录**（纯函数）。

    `plan_mapping_ops` / `apply_mapping_ops` 支持批内模拟（「后一条看得到前一
    条」），同一落点可能在一个批次里被操作多次（add → update、add → disable、
    disable → enable、update → update），每条 `added` 记的都是**当时那一步**的
    `after`。写库后 schema 是整批终态，拿中间态逐条比必然失败（合法批次会被
    误报成「回读核对失败」）。这里按 `mapping_key` 的复合身份（大小写不敏感）
    分组，只调用 `mapping_state_matches` 核对每组的**最后一条**。
    """
    terminal: dict[str, dict] = {}
    for record in added or []:
        if not isinstance(record, dict):
            continue
        key = mapping_key(record.get("target_code"),
                          record.get("data_source_id"),
                          record.get("entity_name"),
                          record.get("attribute_name")).casefold()
        # dict 保持首次插入位置；同键后写覆盖 → 每组只留最后一条。
        terminal[key] = record
    return all(mapping_state_matches(schema_json, record)
               for record in terminal.values())


# ------------------------------------------------------- tool_schema（工具口径）

#: 单批 tool_schema 条目上限（与 `concept_expansion.TOOLS_MAX` 同口径）。
TOOL_SCHEMA_MAX_ENTRIES = 20
#: 工具口径**只允许**三种 op：不提供 add / remove —— 工具本身由
#: `nl2sql.multiturn.TOOL_SCHEMAS` 定义，演化只改 description / enabled。
TOOL_SCHEMA_OPS = frozenset({"update", "disable", "enable"})


def tool_name_whitelist() -> frozenset[str]:
    """`nl2sql.multiturn.TOOL_SCHEMAS` 的工具 name 白名单。

    **延迟 import**：engine 是纯函数模块，模块加载期不拉起多轮生成的依赖图
    （multiturn 会经 llm client 反向依赖一批东西）。白名单是演化条目唯一的
    name 存在性判据：工具由代码定义，不在这三个里的 name 一律 skipped。
    """
    from ..nl2sql.multiturn import TOOL_SCHEMAS

    names: set[str] = set()
    for item in TOOL_SCHEMAS or []:
        function = item.get("function") if isinstance(item, dict) else None
        name = (function or {}).get("name") if isinstance(function, dict) else None
        if name:
            names.add(str(name))
    return frozenset(names)


def _tool_desc_max() -> int:
    """描述截断上限：**复用** `concept_expansion.TOOL_DESC_MAX`（256）。

    落库归一（`_normalize_tools`）会把描述截到这个长度；计划阶段不先截，
    回读核对拿完整串与截断后的存量比较必然失败。
    """
    from ..concept_expansion import TOOL_DESC_MAX

    return int(TOOL_DESC_MAX)


def _tools_max() -> int:
    """`tools[]` 总条数上限：**复用** `concept_expansion.TOOLS_MAX`（20）。

    写时归一（`_normalize_tools`）会截断到这个条数；计划阶段不先挡，
    20 条满时追加的覆盖项会被截掉、回读核对必然失败。
    """
    from ..concept_expansion import TOOLS_MAX

    return int(TOOLS_MAX)


def _tool_pos(tools: list, name: str) -> int | None:
    """按 name 定位工具覆盖项在 `tools[]` 里的位置（None = 没有覆盖项）。"""
    return next((i for i, item in enumerate(tools)
                 if isinstance(item, dict)
                 and str(item.get("name")) == name), None)


def _tool_item(entry: dict | None, name: str) -> dict:
    """覆盖项基底：有原条目就浅拷贝（保留 description / enabled），无则只带 name。"""
    item = dict(entry) if isinstance(entry, dict) else {"name": name}
    item["name"] = name
    return item


def plan_tool_schema_entries(schema_json: dict,
                             entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """tool_schema 条目 **dry-run 校验** → (planned, skipped)。不写行、不改入参。

    条目只允许 `update` / `disable` / `enable`（结构性错误：受理层 / builder
    抛 1001；这里按 skipped 处理，供直接调用与预览的宽容口径）。单条业务问题
    进 skipped：

    - `missing_field`：缺 name（非空字符串）；
    - `unknown_op`：op 不在三 op 里；
    - `unknown_tool`：name 不在 `TOOL_SCHEMAS` 白名单；
    - `description_required`：update 没给**非空字符串** description；
    - `no_change`：update 的新 description 与当前覆盖项一致；
    - `already_disabled` / `already_enabled`：disable / enable 的幂等跳过
      （没有覆盖项 = 默认启用）；
    - `tools_full`：`tools[]` 已到总条数上限，且该 name 还没有覆盖项。

    planned 记录 `{op, name, before, description?}`：`before` 是原覆盖项
    （浅拷贝）或 None（批内叠加时后一条看得到前一条）。`description` 只在
    update 上带 —— 回读核对拿它比落库后的实际描述。
    """
    whitelist = tool_name_whitelist()
    desc_max = _tool_desc_max()
    working = copy.deepcopy((schema_json or {}).get("tools") or [])
    planned: list[dict] = []
    skipped: list[dict] = []

    for index, raw in enumerate(entries or []):
        if index >= TOOL_SCHEMA_MAX_ENTRIES:
            skipped.append({"index": index,
                            "why": f"超过单批条目上限 {TOOL_SCHEMA_MAX_ENTRIES} 条"})
            continue
        if not isinstance(raw, dict):
            skipped.append({"index": index, "why": "条目必须是对象"})
            continue
        op = str(raw.get("op") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not name:
            skipped.append({"index": index, "op": op, "why": "missing_field"})
            continue
        if op not in TOOL_SCHEMA_OPS:
            skipped.append({"index": index, "op": op, "name": name,
                            "why": "unknown_op"})
            continue
        if name not in whitelist:
            skipped.append({"index": index, "op": op, "name": name,
                            "why": "unknown_tool"})
            continue

        pos = _tool_pos(working, name)
        current = working[pos] if pos is not None else None
        before = dict(current) if isinstance(current, dict) else None

        if pos is None and len(working) >= _tools_max():
            skipped.append({"index": index, "op": op, "name": name,
                            "why": "tools_full"})
            continue

        if op == "update":
            description = raw.get("description")
            if not isinstance(description, str) or not description.strip():
                skipped.append({"index": index, "op": op, "name": name,
                                "why": "description_required"})
                continue
            description = description.strip()[:desc_max]
            if current is not None \
                    and str(current.get("description") or "") == description:
                skipped.append({"index": index, "op": op, "name": name,
                                "why": "no_change"})
                continue
            item = {**_tool_item(current, name),
                    "description": description}
            planned.append({"op": "update", "name": name,
                            "description": description, "before": before})
        elif op == "disable":
            if current is not None and current.get("enabled") is False:
                skipped.append({"index": index, "op": op, "name": name,
                                "why": "already_disabled"})
                continue
            item = {**_tool_item(current, name), "enabled": False}
            planned.append({"op": "disable", "name": name, "before": before})
        else:  # enable
            if current is None or current.get("enabled") is not False:
                skipped.append({"index": index, "op": op, "name": name,
                                "why": "already_enabled"})
                continue
            # enable 的落库形状是**删键**（不是写 true）：读取侧缺键 = 启用。
            item = {key: value for key, value in _tool_item(current, name).items()
                    if key != "enabled"}
            planned.append({"op": "enable", "name": name, "before": before})

        # 批内叠加：后一条看得到前一条（与 alias / relation / constraint 同姿态）。
        if pos is None:
            working.append(item)
        else:
            working[pos] = item

    return planned, skipped


def _with_tools(schema_json: dict, tools: list[dict]) -> dict:
    """把 `tools[]` 装回 schema：**空数组不留键**（与 Task 1 的权威写同口径）。"""
    out = {key: value for key, value in (schema_json or {}).items()
           if key != "tools"}
    if tools:
        out["tools"] = tools
    return out


def apply_tool_schema_entries(schema_json: dict,
                              entries: list[dict]) -> tuple[dict, list, list]:
    """抽象 schema → (新 schema, added, skipped)：update / disable / enable。

    `added` 记 `{op, name, before, description?}`（`before` 为原覆盖项或
    None；update 另带目标 description 供回读核对）。无覆盖项时按 name
    append 一条覆盖项；enable 删 `enabled` 键、其余字段保留。
    """
    planned, skipped = plan_tool_schema_entries(schema_json, entries)
    tools = copy.deepcopy((schema_json or {}).get("tools") or [])
    added: list[dict] = []

    for entry in planned:
        op = str(entry["op"])
        name = str(entry["name"])
        pos = _tool_pos(tools, name)
        current = tools[pos] if pos is not None else None
        before = entry.get("before")
        before = dict(before) if isinstance(before, dict) else None
        if op == "update":
            item = {**_tool_item(current, name),
                    "description": entry["description"]}
        elif op == "disable":
            item = {**_tool_item(current, name), "enabled": False}
        else:
            item = {key: value for key, value in _tool_item(current, name).items()
                    if key != "enabled"}
        if pos is None:
            tools.append(item)
        else:
            tools[pos] = item
        record = {"op": op, "name": name, "before": before}
        if op == "update":
            record["description"] = entry["description"]
        added.append(record)

    return _with_tools(schema_json, tools), added, skipped


def rollback_tool_schema_entries(schema_json: dict, added: list[dict]) -> dict:
    """逆操作本批 tool_schema 改动（按 name 定位；找不到的忽略 = 幂等）。

    **逆序**处理，保证同一个工具在一批里被多次改（update → disable）时逐项复原：
    - `before is None` → 删除该 name 的覆盖项（本批新建的）；
    - 否则把原覆盖项写回（位置已在就替换，被别人删掉就 append 补回）。
    空 `tools[]` 不留键（与权威写「省略 tools = 清除」同口径）。
    """
    tools = copy.deepcopy((schema_json or {}).get("tools") or [])
    for record in reversed(list(added or [])):
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "")
        if not name:
            continue
        before = record.get("before")
        pos = _tool_pos(tools, name)
        if before is None:
            if pos is not None:
                tools.pop(pos)
            continue
        if not isinstance(before, dict):
            continue
        if pos is None:
            tools.append(copy.deepcopy(before))
        else:
            tools[pos] = copy.deepcopy(before)
    return _with_tools(schema_json, tools)


def tool_schema_state_matches(schema_json: dict, added: list[dict]) -> bool:
    """回读核对：`added` 记录描述的**目标状态**是否已在 schema 里（纯函数）。

    - `update`：覆盖项的 description 与记录的目标一致；
    - `disable`：覆盖项 `enabled` 必须**恰好是** `False`；
    - `enable`：覆盖项**没有** `enabled` 键或值非 False（enable 落库是删键）。

    同一 name 在一批里被操作多次时只看**最后一条**（写库后是整批终态，
    拿中间态逐条比会把合法批次误报成「回读核对失败」）。找不到覆盖项 → False。
    """
    terminal: dict[str, dict] = {}
    for record in added or []:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "")
        if name:
            terminal[name] = record
    tools = {str(item.get("name")): item
             for item in (schema_json or {}).get("tools") or []
             if isinstance(item, dict)}
    for name, record in terminal.items():
        item = tools.get(name)
        if item is None:
            return False
        op = str(record.get("op") or "")
        if op == "update":
            if str(item.get("description") or "") \
                    != str(record.get("description") or ""):
                return False
        elif op == "disable":
            if item.get("enabled") is not False:
                return False
        elif op == "enable":
            if item.get("enabled") is False:
                return False
    return True


# ---------------------------------------------------------------- 运行编排

#: CAS 未命中（该行已被 `recover_stale` 回收或已到终态）时的统一失败原因。
RECLAIMED_ERROR = "运行已被回收/结束"


async def _heartbeat(store, run_id: str) -> None:
    """评测期间定期刷新 `updated_at`（CAS running）；CAS 未命中即停止。

    运行已被 `recover_stale` 回收（failed）或已到终态时，再刷 `updated_at`
    只会掩盖真实状态、并让后续 `_advance` 误以为仍活着 —— 命中 0 行就退出。
    心跳自身的库写失败只记 warning：它是旁路，不能把评测拖垮（会话中毒的
    兜底在 `RunStore.log`，这里不碰日志表）。
    """
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            ok = await store.update(run_id, expect_statuses={"running"})
        except Exception as exc:  # noqa: BLE001 —— 心跳失败不推翻主流程
            logger.warning("演化运行心跳写入失败 run=%s: %s", run_id, exc)
            return
        if not ok:
            return


async def _eval_with_heartbeat(store, run_id: str, factory):
    """在 `run_eval` 周围起/停心跳任务；正常结束与异常都 cancel。"""
    task = asyncio.create_task(_heartbeat(store, run_id))
    try:
        return await factory()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _advance(store, run_id: str, **fields) -> None:
    """带状态 CAS 的状态推进：只有仍是 `running` 的行才写。

    `recover_stale` 会把超时/重启中断的 `running` 置 `failed` 释放活跃槽；
    被回收后仍在跑的线程若继续无条件写库，就会把 `failed` 覆盖成
    `evaluated`（旧的 Important 缺陷）。这里命中 0 行就立刻中止运行。
    """
    ok = await store.update(run_id, expect_statuses={"running"}, **fields)
    if not ok:
        raise RuntimeError(RECLAIMED_ERROR)


async def _load_run_corpus(run: dict, tenant_id: str, kb: str, ds: str | None) -> dict:
    """读本次运行的语料。`run["_session"]` 可注入（runner / 测试）；未注入时自开会话。

    `ds=None`（alias 演化）原样下传：`load_corpus` 会回退到该 KB 的第一个数据源，
    **不能**改写成空串——那会精确匹配 `ds_id == ''`、拿到 0 条用例。
    """
    session = run.get("_session")
    if session is not None:
        return await _load_with_auto_corpus(session, tenant_id, kb, ds)
    from jonex_core.common.database import get_db_session

    async with get_db_session() as owned:
        return await _load_with_auto_corpus(owned, tenant_id, kb, ds)


async def _load_with_auto_corpus(session, tenant_id: str, kb: str,
                                 ds: str | None) -> dict:
    """语料为空时先试「从租户内借一份」（重建 KB 后 gate 空报告的兜底）。"""
    from .corpus_service import ensure_corpus_for_kb

    corpus = await load_corpus(session, tenant_id, kb, ds)
    if corpus.get("cases"):
        return corpus
    auto = await ensure_corpus_for_kb(session, tenant_id, kb, ds)
    if auto.get("filled") or auto.get("moved"):
        corpus = await load_corpus(session, tenant_id, kb, ds)
    corpus["_auto_corpus"] = auto
    return corpus


async def _record_corpus_provenance(store, tenant_id: str, run_id: str,
                                    kb: str, eval_ds: str) -> None:
    """把本次运行实际评测的那条语料链的版本/hash 记到 run 行（旁路）。

    hash 必须走 `snapshot_for_chain`（全链、含停用题），**不是** `load_corpus`
    的 cases —— 后者只含启用题、缺 `enabled` 键，写进 run 行会与版本链永远
    对不上。没有 active 版本时 `corpus_version_id` 写 NULL（不自动补版本），
    hash 仍是该链当前内容的 hash。

    provenance 不是关键路径：读/写失败只 warning，绝不推翻本次运行；
    这里也**不**用状态 CAS（brief：CAS 不在这条旁路上）。
    """
    session = getattr(store, "session", None)
    if session is None:
        return
    from .corpus_versions import active as corpus_active, snapshot_for_chain

    try:
        version = await corpus_active(session, tenant_id, kb, eval_ds)
        _snapshot, chain_hash = await snapshot_for_chain(
            session, tenant_id, kb, eval_ds)
        await store.update(
            run_id, tenant_id=tenant_id,
            corpus_version_id=(version or {}).get("version_id"),
            corpus_hash=chain_hash)
    except Exception as exc:  # noqa: BLE001 —— 来源记录是旁路
        logger.warning("演化运行记录语料来源失败 run=%s: %s", run_id, exc)
        # `RunStore.update` 只在 **commit** 失败时回滚；模板/列表查询本身失败
        # （迁移缺表缺列、连接断）会把共享 session 留成 aborted 事务，紧接着的
        # `build_patch` / `_advance` 会连锁 PendingRollbackError —— 那就不叫旁路了。
        # 前一步 `store.log` 已提交、run session 上无待提交内容，这里回滚安全。
        try:
            await session.rollback()
        except Exception as rollback_exc:  # noqa: BLE001 —— 清理失败也不能掀翻 run
            logger.warning("演化运行记录语料来源失败后回滚未完成 run=%s: %s",
                           run_id, rollback_exc)


async def run_pipeline(tenant_id: str, run: dict, *, store, on_stage=None) -> dict:
    """一次运行的全流程；异常由 runner 统一收口为 failed。

    返回 `{"status": <终态>}`：`evaluated` / `dry-run` / `empty-draft`。
    终态由本函数写库，runner 据此避免把 dry-run/empty-draft 覆盖成 evaluated。

    `on_stage` 在每次阶段推进后回调，runner 用它记录最后的**合法** stage，
    失败时收口成 `error=f"{stage}: {exc}"`（迁移 CHECK 不接受 `failed@*`）。

    所有状态推进都走 `_advance`（`expect_statuses={"running"}` 的状态 CAS）：
    被 `recover_stale` 回收（置 `failed`）后，任何迟到写入都命中 0 行并抛
    `RuntimeError("运行已被回收/结束")`，绝不把终态覆盖成 `evaluated`。

    依赖全部在函数内 import：Task 7b 的 builders/upstream 尚未实现（延迟到
    调用时），同时让测试可以 monkeypatch 各源模块的同名属性。
    """
    from .builders import build_patch, probe_targets
    from .eval import run_eval
    from .gate import decide
    from .schema_io import apply_patch, get_row, rollback_patch
    from .upstream import asker_for, truth_runner_for

    def mark(stage: str) -> None:
        if on_stage is not None:
            on_stage(stage)

    async def log(line: str) -> None:
        """阶段日志（也是心跳）：写失败只记 warning，绝不推翻主流程。"""
        try:
            await store.log(tenant_id, run_id, line)
        except Exception as exc:  # noqa: BLE001 —— 日志不是关键路径
            logger.warning("演化运行日志写入失败 run=%s: %s", run_id, exc)

    req = run.get("request") or {}
    kind, kb, ds = run["kind"], run["kb"], run["ds"]
    run_id = run["run_id"]
    repeat = int(req.get("repeat") or 1)

    # KB 级 run（alias / mapping）自身 `ds=None`：mapping 的语料源由开库校验
    # 写进 `request.bench_ds`，经 `corpus_ds` 解析；alias 仍是 None（回落该 KB
    # 的第一个数据源）。
    corpus = await _load_run_corpus(run, tenant_id, kb, corpus_ds(run, ds))
    # 语料自动补齐的痕迹写进运行日志（报告空的时候用户能直接看到原因）
    auto_corpus = corpus.pop("_auto_corpus", None) or {}
    if auto_corpus.get("filled") or auto_corpus.get("moved"):
        names = [*(auto_corpus.get("filled") or []),
                 *(auto_corpus.get("moved") or [])]
        await log(f"corpus 自动补齐 {len(names)} 条：{', '.join(names[:5])}")
    elif auto_corpus.get("reason") in ("no_donor", "no_match"):
        await log(f"corpus 为空且租户内没有可借语料（{auto_corpus['reason']}）")
    # 评测依赖跟语料实际落在哪个数据源：alias 运行 `ds=None`，`load_corpus` 已把
    # 该 KB 的第一个数据源解析进 `target.data_source_id` —— 继续传 None，
    # probe/asker/truth 就拿不到真实数据源。
    eval_ds = ds or (corpus.get("target") or {}).get("data_source_id")
    # 语料来源（Task 4）：记的是**本次实际评测的那条链**。alias 类 run 自身
    # ds=None，eval_ds 已解析成该 KB 的语料数据源。
    if eval_ds:
        await _record_corpus_provenance(store, tenant_id, run_id, kb, eval_ds)
    patch = await build_patch(tenant_id, kind, kb, ds, req, corpus, store,
                              run=run)
    await _advance(store, run_id, stage="drafted", patch=patch,
                   parent={"kind": kind, "kb": kb, "ds": ds})
    mark("drafted")
    await log(f"drafted: {len(patch.get('entries') or [])} entries")
    if patch.get("note"):
        await log(f"note: {patch['note']}")

    # 候选独立落库（spec §3.2）：证据/预期变化要能跨运行详情读取。与日志/审计
    # 同姿态 —— 失败只记 warning，绝不把一次候选写入升级成整个运行失败。
    candidate_record = patch.get("candidate")
    if candidate_record:
        try:
            from .candidate_service import CandidateService
            await CandidateService(getattr(store, "session", None)).persist(
                tenant_id, run_id, candidate_record)
        except Exception as exc:  # noqa: BLE001 —— 候选不是关键路径
            logger.warning("演化候选持久化失败 run=%s: %s", run_id, exc)

    if not patch.get("entries"):
        await _advance(store, run_id, status="empty-draft")
        await log("empty-draft: 补丁没有可应用条目，运行结束")
        return {"status": "empty-draft"}
    if req.get("dry_run"):
        await _advance(store, run_id, status="dry-run")
        await log("dry-run: 按要求只生成补丁，不应用")
        return {"status": "dry-run"}

    probe: dict = {}
    if patch.get("target_cases") and not req.get("skip_probe"):
        probe = await probe_targets(tenant_id, kb, eval_ds, corpus,
                                    patch["target_cases"],
                                    evolution_run_id=run_id)
        await _advance(store, run_id, stage="probed")
        mark("probed")
        await log(f"probed: {len(probe or {})} 个目标用例")

    asker = asker_for(tenant_id, kb, eval_ds, evolution_run_id=run_id)
    truth = truth_runner_for(tenant_id, eval_ds)
    parent = await _eval_with_heartbeat(
        store, run_id,
        lambda: run_eval(tenant_id, corpus["target"], corpus, repeat,
                         asker=asker, truth_runner=truth))
    await _advance(store, run_id, stage="parent-ran", report={"parent": parent})
    mark("parent-ran")
    parent_count = len(parent.get("results") or []) if isinstance(parent, dict) else 0
    await log(f"parent-ran: 父本评测完成（{parent_count} 条）")

    # 先持久化 patch（含 build 时的 rollback 初值）再应用：apply 可能「已落库
    # 但回执丢了」，回滚账不能只活在进程内存里。顺带把 apply 前的 schema 版本
    # 记进 parent（读不到就不写这一项，不能因此让运行失败）。
    parent_ref = {"kind": kind, "kb": kb, "ds": ds}
    try:
        current = await get_row(tenant_id, kind, kb, ds)
    except Exception as exc:  # noqa: BLE001 —— 仅作记账，读不到不阻塞
        logger.warning("演化运行读取 schema_version 失败 run=%s: %s", run_id, exc)
    else:
        parent_ref["schema_version"] = (current or {}).get("schema_version")
    await _advance(store, run_id, patch=patch, parent=parent_ref)

    async def _persist_before_write(backup: dict, staged: dict) -> None:
        """PUT 之前先把 backup 与带 added 的 patch 落库（CAS running）。

        `_advance` 在运行已被回收/结束后抛 `RuntimeError`：此时**不再写 schema**，
        异常的收口交给 runner。重启恢复时库里仍有备份与回滚账，可据此逆操作。
        """
        await _advance(store, run_id, backup=backup, patch=staged)

    # 与 adopt 共用同一把版本链锁：覆盖「apply → 候选评测 → rollback」整个
    # 窗口，避免人工采纳看到 runner 的半应用状态（spec §4.1 step 1 / 决策 6）。
    # 锁用独立会话（`chain_lock` 内建），不会阻塞 runner 的心跳/日志写入。
    lock_key = advisory_key(tenant_id, kb, scope_for_kind(kind), ds)
    async with chain_lock(lock_key):
        try:
            applied = await apply_patch(
                tenant_id, patch, before_write=_persist_before_write)
        except Exception as exc:  # noqa: BLE001 —— 收口前决定是否补偿
            # schema_io 在写库前就把本批 added 记进 patch。只有「PUT 已成功、
            # 回读核对失败」才能确定写入发生过；版本冲突/回收中止等路径 PUT
            # 未落地，补偿反而可能删掉并发赢家刚写入的同名条目（与 adopt 同口径）。
            if ((patch.get("rollback") or {}).get("added")
                    and "回读核对失败" in str(exc)):
                await rollback_patch(tenant_id, patch)
            raise
        # 应用成功后的任何一步失败（含持久化 backup 失败）都必须撤销本批改动。
        try:
            # 此时 patch 带 backup：先落库再评测候选。
            await _advance(store, run_id, stage="applied",
                           backup=applied["backup"], patch=patch)
            mark("applied")
            await log("applied: 补丁已应用")
            candidate = await _eval_with_heartbeat(
                store, run_id,
                lambda: run_eval(tenant_id, corpus["target"], corpus, repeat,
                                 asker=asker, truth_runner=truth))
            await _advance(store, run_id, stage="candidate-ran")
            mark("candidate-ran")
            await log("candidate-ran: 候选评测完成")
        finally:
            await rollback_patch(tenant_id, patch)
    await _advance(store, run_id, stage="rolled-back")
    mark("rolled-back")
    await log("rolled-back: 已撤销本批改动")

    gate = decide(parent, candidate, patch.get("target_cases") or [],
                  margin=int(req.get("margin") or 0), probe=probe)
    await _advance(store, run_id, stage="gated", status="evaluated",
                   report={"parent": parent, "candidate": candidate},
                   gate=gate)
    # I6：门禁出结论后把候选推进到 evaluated 并回写 gate_decision/gate_reasons；
    # 后续 adopt 置 adopted、reject/smoke 失败置 rejected。候选是旁路，失败只
    # 记 warning，绝不推翻已完成的运行。
    if candidate_record:
        try:
            from .candidate_service import CandidateService
            await CandidateService(getattr(store, "session", None)).update_status(
                tenant_id, run_id, "evaluated", gate=gate)
        except Exception as exc:  # noqa: BLE001 —— 候选不是关键路径
            logger.warning("演化候选状态回写失败 run=%s: %s", run_id, exc)
    mark("gated")
    await log(f"gated: decision={gate.get('decision')} delta={gate.get('delta')}")
    return {"status": "evaluated"}
