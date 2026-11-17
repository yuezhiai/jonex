#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""join 边复核 Agent 的工具层。

分工写死：**候选由确定性发现器产出**（`evolution/relation_discovery.py`：
命名短名单 + 值包含采样 + 判词），agent 只做「复核 + 定夺」：

- `list_candidates`：看这一轮有哪些候选、各自的判词与重叠率（含 ambiguous/weak）；
- `inspect_candidate`：看两端的列类型/注释/样本，以及源端已有的关系边；
- `check_overlap`：用**完整 LEFT JOIN 计数**（不是采样）再验一次重叠率；
- `propose_join`：提交 0..N 条边（终止动作）；
- `give_up`：放弃（终止动作）。

与指标 agent 同一纪律：绝不抛、绝不返回原始全量行、预览有预算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

TOOL_NAMES: tuple[str, ...] = (
    "list_candidates", "inspect_candidate", "check_overlap",
    "propose_join", "give_up",
)

MAX_CANDIDATES = 40
MAX_EDGES = 8
MAX_SAMPLES = 3
DEFAULT_CHECK_BUDGET = 12

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class JoinContext:
    """一次复核的上下文：候选 + 编译 schema + 只读执行器。"""

    candidates: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    run_sql: Callable[[str, str, int], Awaitable[dict]] | None = None
    default_source: str = ""
    check_budget: int = DEFAULT_CHECK_BUDGET
    checks: int = 0

    def spend(self) -> bool:
        if self.run_sql is None:
            return False
        if self.checks + 1 > self.check_budget:
            return False
        self.checks += 1
        return True


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _entity(row: dict, name: str) -> dict | None:
    wanted = _text(name).casefold()
    for entity in row.get("entity_types") or []:
        if isinstance(entity, dict) and _text(entity.get("name")).casefold() == wanted:
            return entity
    return None


def _attribute(entity: dict, column: str) -> dict | None:
    wanted = _text(column).casefold()
    for attr in entity.get("attributes") or []:
        if isinstance(attr, dict) and _text(attr.get("name")).casefold() == wanted:
            return attr
    return None


def _table(entity: dict) -> str:
    return _text(entity.get("source_object_id")) or _text(entity.get("name"))


def _rows_for(ctx: JoinContext, data_source_id: str) -> list[dict]:
    wanted = _text(data_source_id) or ctx.default_source
    return [row for row in ctx.rows
            if isinstance(row, dict)
            and (not wanted or _text(row.get("data_source_id")) == wanted)]


def list_candidates(ctx: JoinContext, args: dict) -> dict:
    """列出本轮候选（含判词）；`index` 是后续工具引用的稳定编号。"""
    limit = max(1, min(int(args.get("limit") or 20), MAX_CANDIDATES))
    out = []
    for index, item in enumerate(ctx.candidates[:limit]):
        out.append({
            "index": index,
            "source_entity": _text(item.get("source")),
            "source_column": _text(item.get("source_column")),
            "target_entity": _text(item.get("target")),
            "target_column": _text(item.get("target_column")),
            "verdict": _text(item.get("verdict")),
            "ratio": item.get("ratio"),
            "sampled": item.get("sampled"),
            "confidence": _text(item.get("confidence")),
            "why": _text(item.get("why")),
        })
    return {"ok": True, "candidates": out, "total": len(ctx.candidates)}


def _candidate(ctx: JoinContext, index: Any) -> dict | None:
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(ctx.candidates):
        return ctx.candidates[idx]
    return None


def inspect_candidate(ctx: JoinContext, args: dict) -> dict:
    """看候选两端的列元数据、样本与源端已有关系边。"""
    item = _candidate(ctx, args.get("index"))
    if item is None:
        return {"ok": False, "error": "index 不在候选列表里（先 list_candidates）"}
    rows = _rows_for(ctx, _text(args.get("data_source_id")))
    if not rows:
        return {"ok": False, "error": "该数据源没有编译 schema"}
    row = rows[0]
    source = _entity(row, _text(item.get("source")))
    target = _entity(row, _text(item.get("target")))
    if source is None or target is None:
        return {"ok": False, "error": "候选里的实体不在编译 schema 中"}
    source_attr = _attribute(source, _text(item.get("source_column"))) or {}
    target_attr = _attribute(target, _text(item.get("target_column"))) or {}
    relations = [
        {"name": rel.get("name"), "source": rel.get("source"),
         "target": rel.get("target"), "source_column": rel.get("source_column"),
         "target_column": rel.get("target_column")}
        for rel in row.get("relation_types") or []
        if isinstance(rel, dict) and _text(rel.get("source")) in (
            _text(item.get("source")), _text(item.get("target")))
    ][:10]
    return {
        "ok": True,
        "candidate": item,
        "source": {"entity": _text(source.get("name")),
                   "table": _table(source),
                   "column": _text(source_attr.get("name")),
                   "type": _text(source_attr.get("type")),
                   "description": _text(source_attr.get("description")),
                   "is_primary_key": bool(source_attr.get("is_primary_key"))},
        "target": {"entity": _text(target.get("name")),
                   "table": _table(target),
                   "column": _text(target_attr.get("name")),
                   "type": _text(target_attr.get("type")),
                   "description": _text(target_attr.get("description")),
                   "is_primary_key": bool(target_attr.get("is_primary_key"))},
        "existing_relations": relations,
    }


async def check_overlap(ctx: JoinContext, args: dict) -> dict:
    """完整 LEFT JOIN 计数：源表里有多少行的该列能对上目标列。"""
    entity_a = _text(args.get("source_entity"))
    column_a = _text(args.get("source_column"))
    entity_b = _text(args.get("target_entity"))
    column_b = _text(args.get("target_column"))
    rows = _rows_for(ctx, _text(args.get("data_source_id")))
    if not rows:
        return {"ok": False, "error": "该数据源没有编译 schema"}
    row = rows[0]
    a = _entity(row, entity_a)
    b = _entity(row, entity_b)
    if a is None or b is None:
        return {"ok": False, "error": "实体不在编译 schema 中"}
    if _attribute(a, column_a) is None or _attribute(b, column_b) is None:
        return {"ok": False, "error": "列不在对应实体里"}
    table_a, table_b = _table(a), _table(b)
    phys_a = _text((_attribute(a, column_a) or {}).get("physical_name")) or column_a
    phys_b = _text((_attribute(b, column_b) or {}).get("physical_name")) or column_b
    for ident in (table_a.split(".")[-1], table_b.split(".")[-1], phys_a, phys_b):
        if not _IDENT.match(ident):
            return {"ok": False, "error": "标识符不合规，拒绝拼接 SQL"}
    if not ctx.spend():
        return {"ok": False, "error": "校验预算已用尽（或本环境不能执行 SQL）"}

    sql = (
        "SELECT COUNT(*) AS total, "
        f"COUNT(t.{phys_b}) AS matched "
        f"FROM {table_a} s LEFT JOIN {table_b} t "
        f"ON s.{phys_a} = t.{phys_b}"
    )
    payload = await ctx.run_sql(_text(row.get("data_source_id")), sql, 5)
    if not payload.get("ok"):
        return {"ok": False, "error": payload.get("error") or "查询失败"}
    values = (payload.get("rows") or [[0, 0]])[0]
    total = float(values[0] or 0)
    matched = float(values[1] or 0)
    return {
        "ok": True,
        "total": int(total),
        "matched": int(matched),
        "ratio": round(matched / total, 4) if total else 0.0,
    }


def propose_join(ctx: JoinContext, args: dict) -> dict:
    """终止动作：提交 0..N 条 join 边。形状校验在 runner（入库前）。"""
    raw = args.get("edges")
    edges = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        edges.append({
            "source_entity": _text(item.get("source_entity")),
            "source_column": _text(item.get("source_column")),
            "target_entity": _text(item.get("target_entity")),
            "target_column": _text(item.get("target_column")),
            "rationale": _text(item.get("rationale"))[:1000],
            "confidence": _confidence(item.get("confidence")),
        })
        if len(edges) >= MAX_EDGES:
            break
    return {"ok": True, "terminal": "propose", "edges": edges,
            "rationale": _text(args.get("rationale"))[:1000]}


_CONFIDENCE = ("high", "medium", "low")


def _confidence(value) -> str:
    level = _text(value).lower()
    return level if level in _CONFIDENCE else "low"


_HANDLERS: dict[str, Callable[[JoinContext, dict], Any]] = {
    "list_candidates": list_candidates,
    "inspect_candidate": inspect_candidate,
    "check_overlap": check_overlap,
    "propose_join": propose_join,
}


async def dispatch(action: str, args: dict, ctx: JoinContext) -> dict:
    """动作 → 结果；**任何情况都不抛**。"""
    name = _text(action)
    payload = args if isinstance(args, dict) else {}
    if name not in TOOL_NAMES:
        return {"ok": False,
                "error": f"未知动作：{name or '(缺失)'}；可用：{', '.join(TOOL_NAMES)}"}
    if name == "give_up":
        result = {key: value for key, value in payload.items()
                  if key not in ("ok", "terminal")}
        result["ok"] = True
        result["terminal"] = "give_up"
        result["reason"] = _text(payload.get("reason")) or "no_reason"
        return result
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"动作未实现：{name}"}
    try:
        result = handler(ctx, payload)
        if hasattr(result, "__await__"):
            result = await result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "error": "工具返回了非对象结果"}
    return result


__all__ = ["DEFAULT_CHECK_BUDGET", "MAX_EDGES", "TOOL_NAMES", "JoinContext",
           "dispatch"]
