#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""指标构建 Agent 的工具层：只读、有界、绝不给原始全量数据。

与落点 agent 的工具层同一纪律：

1. **绝不抛**。模型会编实体名、漏参数、写错 SQL —— 一律回结构化错误让它下一轮改，
   而不是把整次构建打断（工具错误是**正常的一轮**，不是失败）。
2. **绝不返回原始行**。`recall_columns` 只回列名/类型/描述与已存证据（样本截 3 条）；
   `execute_preview` 是**显式预览**，行数封顶、由安全闸限定表作用域。
3. **预算挂在 ToolContext 上**。预览次数有上限：一次构建里模型最多看 N 次结果集，
   防止它把「预览」当成随意查库的通道。

数据来源由调用方注入（`rows` + `run_sql`），本模块不碰数据库、不读配置，
因此可以脱离平台单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#: 模型每轮只允许调用这些动作；循环侧先校验再 dispatch。
TOOL_NAMES: tuple[str, ...] = (
    "recall_columns", "inspect_entity", "inspect_column",
    "check_join_overlap", "execute_preview", "propose_metric", "give_up",
)

#: 证据样本条数上限（模型容易把「样本」当「全量」）。
MAX_SAMPLES = 3
#: 一次召回返回的列数上限。
MAX_RECALL = 40
#: `execute_preview` 的行数上限。
MAX_PREVIEW_ROWS = 20
#: 一次构建里允许的预览次数（`check_join_overlap` 内部两次查询算一次预览）。
DEFAULT_PREVIEW_BUDGET = 16
#: 合理化的长度上限（进提案，进 UI）。
MAX_RATIONALE_CHARS = 2000


@dataclass
class ToolContext:
    """一次指标构建的工具上下文（预算与缓存的作用域就是它）。"""

    #: 编译 schema 行（dict）：`{"data_source_id": ..., "entity_types": [...],
    #: "relation_types": [...]}`。调用方从库里读好传进来。
    rows: list[dict] = field(default_factory=list)
    #: `run_sql(data_source_id, sql, limit) -> {"ok"/"columns"/"rows"/"error"}`。
    #: 安全闸与连接都由调用方负责；None 表示该环境不能跑 SQL（只有元数据可用）。
    run_sql: Callable[[str, str, int], Awaitable[dict]] | None = None
    preview_budget: int = DEFAULT_PREVIEW_BUDGET
    preview_calls: int = 0
    #: 默认数据源（模型没点名时用它）。
    default_source: str = ""

    def spend_preview(self, count: int = 1) -> bool:
        if self.run_sql is None:
            return False
        if self.preview_calls + count > self.preview_budget:
            return False
        self.preview_calls += count
        return True


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _source_rows(ctx: ToolContext, data_source_id: str = "") -> list[dict]:
    wanted = _text(data_source_id) or ctx.default_source
    out = []
    for row in ctx.rows:
        if not isinstance(row, dict):
            continue
        ds_id = _text(row.get("data_source_id"))
        if not ds_id:
            continue
        if wanted and ds_id != wanted:
            continue
        out.append(row)
    return out


def _entities(row: dict) -> list[dict]:
    return [item for item in _as_list(row.get("entity_types"))
            if isinstance(item, dict)]


def _attributes(entity: dict) -> list[dict]:
    return [item for item in _as_list(entity.get("attributes"))
            if isinstance(item, dict)]


def _table_name(entity: dict) -> str:
    """物理表名。编译 schema 里是 `source_object_id`；没有就退回实体名。"""
    return _text(entity.get("source_object_id")) or _text(entity.get("name"))


def _column_evidence(row: dict, entity: dict, column: str) -> dict:
    evidence = row.get("evidence")
    tables = evidence.get("tables") if isinstance(evidence, dict) else None
    if not isinstance(tables, dict):
        return {}
    key = _text(entity.get("source_object_id")) or _text(entity.get("name"))
    entry = tables.get(key) or tables.get(_text(entity.get("name"))) or {}
    columns = entry.get("columns") if isinstance(entry, dict) else None
    if not isinstance(columns, dict):
        return {}
    for name, value in columns.items():
        if _text(name).casefold() == _text(column).casefold():
            return value if isinstance(value, dict) else {}
    return {}


def _score(query: str, *values: Any) -> int:
    """命中词数。列级命中在调用方加权（只命中实体名不该把整表倒出来）。"""
    needle = _text(query).casefold()
    if not needle:
        return 1
    tokens = [token for token in re.split(r"[\s,，/]+", needle) if token]
    text = " ".join(_text(value) for value in values).casefold()
    return sum(1 for token in tokens if token in text)


def _find_entity(row: dict, name: str) -> dict | None:
    wanted = _text(name).casefold()
    if not wanted:
        return None
    for entity in _entities(row):
        if _text(entity.get("name")).casefold() == wanted:
            return entity
    return None


def _find_column(entity: dict, column: str) -> dict | None:
    wanted = _text(column).casefold()
    for attribute in _attributes(entity):
        if _text(attribute.get("name")).casefold() == wanted:
            return attribute
    return None


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def recall_columns(ctx: ToolContext, args: dict) -> dict:
    """按关键词召回列（度量列/维度列的第一步）。"""
    query = _text(args.get("query"))
    if not query:
        # 模型偶尔把关键词写成 `keywords: [...]`：容忍这一种别名（多一轮修复
        # 只是白烧 token），其余未知键仍然忽略。
        keywords = args.get("keywords")
        if isinstance(keywords, list):
            query = " ".join(_text(item) for item in keywords if _text(item))
        elif keywords is not None:
            query = _text(keywords)
    limit = int(args.get("limit") or 30)
    data_source_id = _text(args.get("data_source_id"))
    limit = max(1, min(limit, MAX_RECALL))

    out = []
    for row in _source_rows(ctx, data_source_id):
        ds_id = _text(row.get("data_source_id"))
        for entity in _entities(row):
            entity_name = _text(entity.get("name"))
            table = _table_name(entity)
            entity_hit = _score(query, entity_name, table,
                                entity.get("description"))
            for attribute in _attributes(entity):
                column = _text(attribute.get("name"))
                if not column:
                    continue
                # 列级命中权重更高：只命中实体名时整表同分，等于把 40 列
                # 一次性倒给模型；命中列名/描述才是它真正在找的列。
                score = (_score(query, column, attribute.get("description"),
                                attribute.get("display_name")) * 2
                         + entity_hit)
                if score <= 0:
                    continue
                # 召回只回「有哪些列」：样本/区间留给 inspect_column。一次召回
                # 30 列各带 3 条样本 ≈ 2k tokens，而历史每轮都要重发 —— 这是
                # token 预算被吃光的主因（2026-11-14 实测 5 步 24k）。
                out.append({
                    "score": score,
                    "data_source_id": ds_id,
                    "entity": entity_name,
                    "table": table,
                    "column": column,
                    "type": _text(attribute.get("type")),
                    "description": _text(attribute.get("description")),
                })
    out.sort(key=lambda item: (-item["score"], item["entity"], item["column"]))
    return {"ok": True, "columns": out[:limit], "total": len(out)}


def inspect_entity(ctx: ToolContext, args: dict) -> dict:
    """看一个实体的全部列（类型/描述/证据区间）+ 关系边。

    为什么需要它：`recall_columns` 是关键词召回，模型想「先把这张表的列列全」
    时只能反复换关键词（实测两次召回都没列全）。一次给全，既省步数也省 token。
    """
    entity_name = _text(args.get("entity"))
    data_source_id = _text(args.get("data_source_id"))
    for row in _source_rows(ctx, data_source_id):
        entity = _find_entity(row, entity_name)
        if entity is None:
            continue
        columns = []
        for attribute in _attributes(entity):
            name = _text(attribute.get("name"))
            if not name:
                continue
            evidence = _column_evidence(row, entity, name)
            columns.append({
                "column": name,
                "type": _text(attribute.get("type")),
                "description": _text(attribute.get("description")),
                "samples": _as_list(evidence.get("samples"))[:MAX_SAMPLES],
                "distinct_in_sample": evidence.get("distinct_in_sample"),
                "numeric_range": evidence.get("numeric_range"),
            })
        return {
            "ok": True,
            "data_source_id": _text(row.get("data_source_id")),
            "entity": entity_name,
            "table": _table_name(entity),
            "entity_description": _text(entity.get("description")),
            "columns": columns[:60],
            "column_count": len(columns),
            "relations": [
                {"direction": "out" if _text(rel.get("source")) == entity_name else "in",
                 "name": rel.get("name"),
                 "target": rel.get("target") if _text(rel.get("source")) == entity_name else rel.get("source"),
                 "source_column": rel.get("source_column"),
                 "target_column": rel.get("target_column")}
                for rel in _as_list(row.get("relation_types"))
                if isinstance(rel, dict)
                and entity_name in (_text(rel.get("source")), _text(rel.get("target")))
            ][:10],
        }
    return {"ok": False, "error": f"实体 {entity_name} 不在该数据源的编译 schema 里"}


def inspect_column(ctx: ToolContext, args: dict) -> dict:
    """看一列的完整证据与该实体的关系边。"""
    entity_name = _text(args.get("entity"))
    column = _text(args.get("column"))
    data_source_id = _text(args.get("data_source_id"))
    for row in _source_rows(ctx, data_source_id):
        entity = _find_entity(row, entity_name)
        if entity is None:
            continue
        attribute = _find_column(entity, column)
        if attribute is None:
            continue
        edges = []
        for relation in _as_list(row.get("relation_types")):
            if not isinstance(relation, dict):
                continue
            if _text(relation.get("source")) == entity_name:
                edges.append({"direction": "out", "name": relation.get("name"),
                              "target": relation.get("target"),
                              "source_column": relation.get("source_column"),
                              "target_column": relation.get("target_column")})
            elif _text(relation.get("target")) == entity_name:
                edges.append({"direction": "in", "name": relation.get("name"),
                              "target": relation.get("source"),
                              "source_column": relation.get("target_column"),
                              "target_column": relation.get("source_column")})
        return {
            "ok": True,
            "data_source_id": _text(row.get("data_source_id")),
            "entity": entity_name,
            "table": _table_name(entity),
            "entity_description": _text(entity.get("description")),
            "column": _text(attribute.get("name")),
            "type": _text(attribute.get("type")),
            "description": _text(attribute.get("description")),
            "evidence": _column_evidence(row, entity,
                                         _text(attribute.get("name"))),
            "relations": edges[:10],
        }
    return {"ok": False,
            "error": f"未找到列 {entity_name}.{column}（先用 recall_columns 找候选）"}


def _first_column(payload: dict) -> list:
    return [row[0] for row in (payload.get("rows") or []) if row]


async def check_join_overlap(ctx: ToolContext, args: dict) -> dict:
    """用真实数据回答「靠这列能连上吗」：各取前 N 个 distinct 值算重叠率。"""
    entity_a = _text(args.get("entity_a"))
    column_a = _text(args.get("column_a"))
    entity_b = _text(args.get("entity_b"))
    column_b = _text(args.get("column_b"))
    data_source_id = _text(args.get("data_source_id"))
    limit = max(1, min(int(args.get("limit") or 200), 1000))

    rows = _source_rows(ctx, data_source_id)
    if not rows:
        return {"ok": False, "error": "该数据源没有编译 schema"}
    if not ctx.spend_preview(2):
        return {"ok": False,
                "error": "预览预算已用尽（或本环境不能执行 SQL）"}
    ds_id = _text(rows[0].get("data_source_id"))

    table_a = _table_for(rows, entity_a, column_a)
    if isinstance(table_a, dict):
        return table_a
    table_b = _table_for(rows, entity_b, column_b)
    if isinstance(table_b, dict):
        return table_b

    def _values_sql(table: str, column: str) -> str:
        return (f"SELECT DISTINCT {column} FROM {table} "
                f"WHERE {column} IS NOT NULL")

    left = await ctx.run_sql(ds_id, _values_sql(table_a, column_a), limit)
    right = await ctx.run_sql(ds_id, _values_sql(table_b, column_b), limit)
    if not left.get("ok") or not right.get("ok"):
        return {"ok": False, "error": "取值失败",
                "left": left, "right": right}

    left_values = {_text(v) for v in _first_column(left) if v is not None}
    right_values = {_text(v) for v in _first_column(right) if v is not None}
    overlap = left_values & right_values
    denominator = min(len(left_values), len(right_values)) or 1
    return {
        "ok": True,
        "left_values": len(left_values),
        "right_values": len(right_values),
        "overlap": len(overlap),
        "overlap_ratio": round(len(overlap) / denominator, 4),
        "samples": sorted(overlap)[:5],
    }


def _table_for(rows: list[dict], entity_name: str, column: str) -> str | dict:
    """实体+列 → 物理表名；不合法时回结构化错误 dict。"""
    for row in rows:
        entity = _find_entity(row, entity_name)
        if entity is None:
            continue
        if _find_column(entity, column) is None:
            return {"ok": False,
                    "error": f"列 {column} 不在实体 {entity_name} 里"}
        table = _table_name(entity)
        if not table:
            return {"ok": False, "error": f"实体 {entity_name} 没有物理表名"}
        return table
    return {"ok": False, "error": f"实体 {entity_name} 不在该数据源的编译 schema 里"}


async def execute_preview(ctx: ToolContext, args: dict) -> dict:
    """执行一段只读 SELECT 的预览（安全闸在调用方，行数封顶）。"""
    sql = _text(args.get("sql"))
    data_source_id = _text(args.get("data_source_id"))
    limit = max(1, min(int(args.get("limit") or 10), MAX_PREVIEW_ROWS))
    if not sql:
        return {"ok": False, "error": "sql 不能为空"}
    if not ctx.spend_preview(1):
        return {"ok": False, "error": "预览预算已用尽（或本环境不能执行 SQL）"}
    rows = _source_rows(ctx, data_source_id)
    if not rows:
        return {"ok": False, "error": "该数据源没有编译 schema"}
    return await ctx.run_sql(_text(rows[0].get("data_source_id")), sql, limit)


def propose_metric(ctx: ToolContext, args: dict) -> dict:
    """终止动作：提交指标提案。形状校验在 runner（入库前），这里只归字段。"""
    return {"ok": True, "terminal": "propose", "proposal": {
        "name": _text(args.get("name")),
        "formula_sql": _text(args.get("formula_sql")),
        "grain": _text(args.get("grain")),
        "filters": _text(args.get("filters")),
        "rationale": _text(args.get("rationale"))[:MAX_RATIONALE_CHARS],
        "confidence": _confidence(args.get("confidence")),
        "data_source_id": _text(args.get("data_source_id"))
                          or ctx.default_source,
    }}


_CONFIDENCE_LEVELS = ("high", "medium", "low")


def _confidence(value) -> str:
    """枚举外一律降为 low（宁降不升）。"""
    level = _text(value).lower()
    return level if level in _CONFIDENCE_LEVELS else "low"


#: 动作名 → 实现。**终止动作也在里面**：循环对两类动作的调度路径完全一致。
_HANDLERS: dict[str, Callable[[ToolContext, dict], Any]] = {
    "recall_columns": lambda ctx, args: recall_columns(ctx, args),
    "inspect_entity": lambda ctx, args: inspect_entity(ctx, args),
    "inspect_column": lambda ctx, args: inspect_column(ctx, args),
    "check_join_overlap": check_join_overlap,
    "execute_preview": execute_preview,
    "propose_metric": lambda ctx, args: propose_metric(ctx, args),
}


async def dispatch(action: str, args: dict, ctx: ToolContext) -> dict:
    """模型动作 → 工具结果；**任何情况都不抛**。

    普通工具：`{"ok": True, "columns"|...}`
    终止动作：`{"ok": True, "terminal": "propose"|"give_up", ...}`
    错误：    `{"ok": False, "error": "..."}`
    """
    name = _text(action)
    payload = args if isinstance(args, dict) else {}

    if name not in TOOL_NAMES:
        return {"ok": False,
                "error": f"未知动作：{name or '(缺失)'}；可用：{', '.join(TOOL_NAMES)}"}

    # 终止动作的参数原样透传，但 `ok`/`terminal` 是协议字段：模型若在 args 里
    # 塞了同名键，不能覆盖（否则成功终止会被当成失败）。
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
    except Exception as exc:  # noqa: BLE001 - 工具层绝不打断循环
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "error": "工具返回了非对象结果"}
    return result


__all__ = [
    "DEFAULT_PREVIEW_BUDGET", "MAX_PREVIEW_ROWS", "TOOL_NAMES",
    "ToolContext", "dispatch",
]
