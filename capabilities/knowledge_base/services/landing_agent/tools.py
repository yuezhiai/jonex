#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""落点 Agent 的工具层：紧凑证据卡 + 一组确定性、有界的工具。

模型只通过这里看源表 —— 因此这里有两条硬约束：

1. **绝不返回原始行**。表里能给的只有列元数据、已存证据（样本/去重数/数值区间）
   与邻接边；`samples` 一律截到 5 条，避免模型把「样」当「全量」。
2. **绝不抛**。工具层面对的是模型输出（ref 可能编造、参数可能缺），任何问题都
   回 `{"ok": False, "error": ...}` 让循环继续，而不是把整轮落点打断。

预算与缓存都挂在 `ToolContext` 上：同一次概念判定内共享，跨概念互不影响。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..nl2sql.schema_retrieval import TableChunk, tokenize
from ..synonym_normalizer import _normalize_key
from .recall import Candidate, SourceIndex

#: 模型每轮只允许调用这些动作；循环侧先校验再 dispatch。
TOOL_NAMES: tuple[str, ...] = ("inspect_table", "inspect_columns",
                               "search_tables", "probe_column", "decide", "give_up")

#: 证据卡的信息上限：卡片进 prompt，必须紧凑；工具结果可以更完整。
MAX_CARD_COLUMNS = 8
MAX_CARD_RELATIONS = 5
MAX_SEARCH_RESULTS = 10
MAX_SAMPLES = 5

#: `search_tables` 每次概念判定最多**新增**几个候选 ref（spec §5.3：≤3 次/概念）。
#: 召回漏表是 C 档的唯一兜底，但不能让检索无限扩候选面（模型会围着候选库打转）。
MAX_SEARCH_NEW_REFS = 3


@dataclass
class ToolContext:
    """一次概念判定的工具上下文（缓存与预算的作用域就是它）。"""

    index: SourceIndex
    candidates: dict[str, Candidate]      # ref -> candidate（ref 形如 "c1"）
    concept: dict
    probe: Callable[[str, str, str], Awaitable[list[str] | None]] | None = None
    probe_budget: int = 40
    table_cache: dict[str, dict] = field(default_factory=dict)
    probe_calls: int = 0
    #: `search_tables` 已经扩充的候选数（每概念上限 `MAX_SEARCH_NEW_REFS`）。
    search_added: int = 0


class _ToolError(Exception):
    """工具层可预期的失败（未知 ref/参数缺失）→ 结构化错误，不打 traceback。"""


# ---------------------------------------------------------------------------
# 取数与形状
# ---------------------------------------------------------------------------

def _find_entity(index: SourceIndex, ds_id: str, entity_name: str) -> dict:
    """`(ds, 实体名)` → 原始编译实体；找不到回退到同名块的表名。"""
    entity = (index.entities or {}).get((ds_id, entity_name))
    if isinstance(entity, dict):
        return entity
    # 兼容手工构造的索引（只有 chunks、没有 entities）。
    for chunk in index.chunks:
        if chunk.data_source_id == ds_id and chunk.entity_name == entity_name:
            return {"name": chunk.entity_name, "attributes": []}
    return {}


def _attributes(entity: dict) -> list[dict]:
    """编译实体的属性 → `(物理名, 逻辑名, 类型, 描述)`，空的跳过、保序。"""
    entries: list[dict] = []
    for attribute in (entity or {}).get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        logical = str(attribute.get("name") or "").strip()
        physical = str(attribute.get("physical_name") or "").strip() or logical
        if not logical and not physical:
            continue
        entries.append({
            "name": physical,
            "attribute": logical or physical,
            "type": str(attribute.get("type") or "").strip(),
            "description": str(attribute.get("description") or "").strip(),
        })
    return entries


def _evidence_of(index: SourceIndex, ds_id: str, entity_name: str) -> dict:
    raw = (index.evidence or {}).get((ds_id, entity_name)) or {}
    return raw if isinstance(raw, dict) else {}


def _samples(value) -> list:
    """样本一律截到 5 条；非列表/空值不产出。"""
    if not isinstance(value, (list, tuple)):
        return []
    return list(value)[:MAX_SAMPLES]


def _evidence_view(column_evidence: dict) -> dict:
    """单列证据 → `{samples, distinct, numeric_range}`（有才给，避免空键噪声）。"""
    if not isinstance(column_evidence, dict):
        return {}
    view: dict = {}
    samples = _samples(column_evidence.get("samples"))
    if samples:
        view["samples"] = samples
    if column_evidence.get("distinct_in_sample") is not None:
        view["distinct"] = column_evidence.get("distinct_in_sample")
    if column_evidence.get("numeric_range") is not None:
        view["numeric_range"] = column_evidence.get("numeric_range")
    return view


def _column_views(index: SourceIndex, ds_id: str, entity_name: str, *,
                  with_evidence: bool) -> list[dict]:
    """列视图；`with_evidence=True` 时把该列证据平铺进列对象（工具结果用）。"""
    entity = _find_entity(index, ds_id, entity_name)
    evidence = _evidence_of(index, ds_id, entity_name)
    # 证据表的键可能是物理名，也可能是逻辑名 —— 两把尺子都归一后再查。
    by_key = {_normalize_key(str(key)): value for key, value in evidence.items()}

    views: list[dict] = []
    for entry in _attributes(entity):
        view = {"name": entry["name"]}
        if entry["attribute"] and entry["attribute"] != entry["name"]:
            view["attribute"] = entry["attribute"]
        if entry["type"]:
            view["type"] = entry["type"]
        if entry["description"]:
            view["description"] = entry["description"]
        if with_evidence:
            raw = (by_key.get(_normalize_key(entry["name"]))
                   or by_key.get(_normalize_key(entry["attribute"]))
                   or {})
            view.update(_evidence_view(raw))
        views.append(view)
    return views


def _relations(index: SourceIndex, candidate: Candidate) -> list[dict]:
    """无向遍历邻接边，两端各给一条（列名按方向对调）。

    边只带实体名、不带 ds_id（跨源同名即同节点，见 `recall.build_index` 的已知
    局限）；这里按实体名/表名任一相等判定「这条边贴着当前表」。
    """
    mine = {_normalize_key(candidate.entity_name), _normalize_key(candidate.table)}
    mine.discard("")
    edges: list[dict] = []
    seen: set[tuple] = set()

    def _add(name, target, source_column, target_column):
        key = (name, target, source_column, target_column)
        if key in seen:
            return
        seen.add(key)
        edges.append({"name": name, "target": target,
                      "source_column": source_column,
                      "target_column": target_column})

    for relation in index.relation_types or []:
        if not isinstance(relation, dict):
            continue
        name = str(relation.get("name") or "").strip()
        source = str(relation.get("source") or "").strip()
        target = str(relation.get("target") or "").strip()
        source_column = relation.get("source_column")
        target_column = relation.get("target_column")
        if _normalize_key(source) in mine:
            _add(name, target, source_column, target_column)
        if _normalize_key(target) in mine:
            # 反向：对端是 source，列名随之对调。
            _add(name, source, target_column, source_column)
    return edges


# ---------------------------------------------------------------------------
# 候选卡（进初始 prompt）
# ---------------------------------------------------------------------------

def build_cards(candidates: list[Candidate], index: SourceIndex) -> list[dict]:
    """候选 → 紧凑证据卡（ref 与 `ToolContext.candidates` 的键同序，c1 起）。

    钉住的旧落点（`signals["pinned"]`）额外带 `pinned: true`：卡片形状与真实召回
    一致时模型会把旧结论当新证据，标记让提示词能要求它「证据仍成立才保留」。
    """
    cards: list[dict] = []
    for position, candidate in enumerate(candidates or [], start=1):
        entity = _find_entity(index, candidate.ds_id, candidate.entity_name)
        evidence = _evidence_of(index, candidate.ds_id, candidate.entity_name)
        # 卡片里的证据单独成表：列清单保持短，样本只在这里出现一次。
        evidence_cards: dict = {}
        for column, value in evidence.items():
            view = _evidence_view(value)
            if view:
                evidence_cards[str(column)] = view
        card = {
            "ref": f"c{position}",
            "ds": candidate.ds_id,
            "table": candidate.table,
            "entity": candidate.entity_name,
            "description": str((entity or {}).get("description") or "").strip(),
            "columns": _column_views(index, candidate.ds_id,
                                     candidate.entity_name, with_evidence=False)
                       [:MAX_CARD_COLUMNS],
            "evidence": evidence_cards,
            "relations": _relations(index, candidate)[:MAX_CARD_RELATIONS],
        }
        if (candidate.signals or {}).get("pinned"):
            # 只在钉住时出现：真实召回卡逐字节保持原形状。
            card["pinned"] = True
        cards.append(card)
    return cards


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def _resolve_ref(args: dict, ctx: ToolContext) -> tuple[str, Candidate]:
    ref = str((args or {}).get("ref") or "").strip()
    candidate = (ctx.candidates or {}).get(ref)
    if candidate is None:
        raise _ToolError(f"未知 ref：{ref or '(缺失)'}")
    return ref, candidate


def _inspect_table(args: dict, ctx: ToolContext) -> dict:
    ref, candidate = _resolve_ref(args, ctx)
    cache_key = f"{candidate.ds_id}::{candidate.entity_name}"
    cached = ctx.table_cache.get(cache_key)
    if cached is None:
        entity = _find_entity(ctx.index, candidate.ds_id, candidate.entity_name)
        # 缓存的是**与 ref 无关**的表载荷：同一张表可能被多个 ref 指向
        # （跨源候选或调用方重排），ref 必须每次按入参盖上去，否则第二
        # 个 ref 会拿到第一个 ref 的标签。
        cached = {
            "ds": candidate.ds_id,
            "table": candidate.table,
            "entity": candidate.entity_name,
            "description": str((entity or {}).get("description") or "").strip(),
            # 完整列（含证据）；绝不把原始行放进结果。
            "columns": _column_views(ctx.index, candidate.ds_id,
                                     candidate.entity_name, with_evidence=True),
            "relations": _relations(ctx.index, candidate),
        }
        ctx.table_cache[cache_key] = cached

    # 深拷贝：缓存对象不能被调用方就地修改污染后续调用。
    result = copy.deepcopy(cached)
    result["ref"] = ref
    return result


def _inspect_columns(args: dict, ctx: ToolContext) -> dict:
    ref, candidate = _resolve_ref(args, ctx)
    raw = args.get("columns")
    if raw is None:
        raw = args.get("column")
    if isinstance(raw, str):
        wanted = [raw]
    elif isinstance(raw, (list, tuple)):
        wanted = [str(item) for item in raw]
    else:
        wanted = []
    wanted = [item.strip() for item in wanted if str(item).strip()]
    if not wanted:
        raise _ToolError("inspect_columns 需要 columns（列名列表）")

    wanted_keys = {_normalize_key(item) for item in wanted}
    columns = [view for view in _column_views(ctx.index, candidate.ds_id,
                                              candidate.entity_name, with_evidence=True)
               if _normalize_key(view["name"]) in wanted_keys
               or _normalize_key(view.get("attribute") or "") in wanted_keys]
    found = {_normalize_key(view["name"]) for view in columns}
    found.update(_normalize_key(view.get("attribute") or "") for view in columns)

    result = {
        "ref": ref,
        "ds": candidate.ds_id,
        "table": candidate.table,
        "columns": columns,
        # 找不到的列名照实回：让模型知道是拼错了，而不是「这列没证据」。
        "missing": [item for item in wanted if _normalize_key(item) not in found],
    }
    return result


def _search_tables(args: dict, ctx: ToolContext) -> dict:
    query = str((args or {}).get("query") or "").strip()
    if not query:
        raise _ToolError("search_tables 需要 query")
    query_tokens = tokenize(query)
    query_key = _normalize_key(query)

    scored: list[tuple[int, str, str, dict]] = []
    new_refs: list[str] = []
    for position, chunk in enumerate(ctx.index.chunks or []):
        ds_id = (ctx.index.chunk_ds[position]
                 if position < len(ctx.index.chunk_ds) else chunk.data_source_id)
        ds_id = str(ds_id or chunk.data_source_id or "").strip()
        # 块 token 已含名字/表名/描述/列名（见 build_chunks），交集即词面命中；
        # 再加一次整串包含，覆盖「列名是一个长串」这类分词切不开的关键词。
        hits = len(query_tokens & set(chunk.tokens)) if query_tokens else 0
        if query_key and any(
                query_key in _normalize_key(text)
                for text in (chunk.entity_name, chunk.table, chunk.text)):
            hits += 1
        if hits <= 0:
            continue
        item = {"ds": ds_id, "table": chunk.table, "entity": chunk.entity_name}
        ref, created = _ref_for(ctx, ds_id, chunk.entity_name, chunk)
        if ref:
            item["ref"] = ref
        if created:
            new_refs.append(ref)
        entity = _find_entity(ctx.index, ds_id, chunk.entity_name)
        description = str((entity or {}).get("description") or "").strip()
        if description:
            item["description"] = description
        scored.append((hits, ds_id, chunk.entity_name, item))

    # 稳定排序：命中数降序，再按 (ds, entity) 全序，跨进程逐字节可比。
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    return {"query": query,
            "tables": [row[3] for row in scored[:MAX_SEARCH_RESULTS]],
            # 新增的 ref 必须显式回给模型：这些表不在初始候选卡里，模型只有拿到
            # ref 才能在下一轮 inspect/decide —— 这正是 C 档兜底的意义。
            "new_refs": new_refs}


def _next_ref(ctx: ToolContext) -> str:
    """下一个候选 ref（`c{N}`）；按现有最大编号递增，跨多次 search 不重复。"""
    highest = 0
    for key in (ctx.candidates or {}):
        if key.startswith("c") and key[1:].isdigit():
            highest = max(highest, int(key[1:]))
    return f"c{highest + 1}"


def _ref_for(ctx: ToolContext, ds_id: str, entity_name: str,
             chunk=None) -> tuple[str, bool]:
    """块 → `(ref, 是否本次新增)`。

    - 已在候选里：回原 ref、不新增；
    - 不在候选里且未到 `MAX_SEARCH_NEW_REFS`：注册成新候选（`score=0`、
      `verbatim=False`、`signals={"search": 1}`），下一轮 inspect/decide 可用；
    - 到上限后不再扩面（回空 ref，结果里仍保留表名/实体名供模型参考）。
    """
    for ref, candidate in (ctx.candidates or {}).items():
        if candidate.ds_id == ds_id and candidate.entity_name == entity_name:
            return ref, False
    if chunk is None or ctx.search_added >= MAX_SEARCH_NEW_REFS:
        return "", False
    ref = _next_ref(ctx)
    ctx.candidates[ref] = Candidate(
        ds_id=ds_id,
        entity_name=chunk.entity_name,
        table=chunk.table,
        score=0.0,
        verbatim=False,
        coverage=0.0,
        # 显式标记：排障时能区分「检索兜底扩出来的候选」与真实召回。
        signals={"search": 1.0},
    )
    ctx.search_added += 1
    return ref, True


async def _probe_column(args: dict, ctx: ToolContext) -> dict:
    ref, candidate = _resolve_ref(args, ctx)
    column = str((args or {}).get("column") or "").strip()
    if not column:
        raise _ToolError("probe_column 需要 column")
    if ctx.probe is None:
        # 没有只读凭据就是不可用，不能靠「静默返回空」让模型误以为表里没值。
        raise _ToolError("未配置只读凭据，probe_column 不可用")

    # 列名尽量按源侧真实物理名调用；编造的列在探测前就拒掉（不消耗预算）。
    columns = _attributes(_find_entity(ctx.index, candidate.ds_id, candidate.entity_name))
    matched = next(
        (item for item in columns
         if _normalize_key(column) in {_normalize_key(item["name"]),
                                       _normalize_key(item["attribute"])}),
        None,
    )
    if columns and matched is None:
        raise _ToolError(f"列不存在：{column}")
    probe_column = (matched or {}).get("name") or column

    budget = max(0, int(ctx.probe_budget or 0))
    if ctx.probe_calls >= budget:
        raise _ToolError(f"probe 预算已用尽（{budget} 次）")

    # 先记账再调用：失败/超时同样消耗一次预算，避免坏凭据被无限重试。
    ctx.probe_calls += 1

    try:
        samples = await ctx.probe(candidate.ds_id, candidate.table, probe_column)
    except Exception as exc:  # noqa: BLE001 - 探测是增益，失败只回结构化错误
        return {"ref": ref, "column": probe_column,
                "error": f"probe 失败：{exc}"}
    return {"ref": ref, "column": probe_column,
            "samples": _samples(samples or [])}


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

async def dispatch(action: str, args: dict, ctx: ToolContext) -> dict:
    """模型动作 → 工具结果；**任何情况都不抛**。

    普通工具：`{"ok": True, "result": ...}`
    终止动作：`{"ok": True, "terminal": "decide"|"give_up", ...args}`
    错误：    `{"ok": False, "error": "..."}`
    """
    name = str(action or "").strip()
    payload = args if isinstance(args, dict) else {}

    if name not in TOOL_NAMES:
        return {"ok": False,
                "error": f"未知动作：{name or '(缺失)'}；可用：{', '.join(TOOL_NAMES)}"}

    # 终止动作原样透传参数（landings/confidence/rationale 或 reason），但
    # `ok`/`terminal` 是协议字段：模型若在 args 里塞了同名键（如 `{"ok": false}`）
    # 不能覆盖，否则调用方会把成功终止误判为失败。
    if name in ("decide", "give_up"):
        result = {key: value for key, value in payload.items()
                  if key not in ("ok", "terminal")}
        result["ok"] = True
        result["terminal"] = name
        return result

    try:
        if name == "inspect_table":
            return {"ok": True, "result": _inspect_table(payload, ctx)}
        if name == "inspect_columns":
            return {"ok": True, "result": _inspect_columns(payload, ctx)}
        if name == "search_tables":
            return {"ok": True, "result": _search_tables(payload, ctx)}
        if name == "probe_column":
            probed = await _probe_column(payload, ctx)
            if "error" in probed:
                return {"ok": False, "error": probed["error"]}
            return {"ok": True, "result": probed}
    except _ToolError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 工具层绝不打断判定循环
        return {"ok": False, "error": f"工具执行失败：{exc}"}

    return {"ok": False, "error": f"未知动作：{name}"}


__all__ = ["MAX_CARD_COLUMNS", "MAX_CARD_RELATIONS", "MAX_SAMPLES",
           "MAX_SEARCH_NEW_REFS", "MAX_SEARCH_RESULTS", "TOOL_NAMES",
           "ToolContext", "build_cards", "dispatch"]
