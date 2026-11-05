#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""SQL 约束 IR（C2，2026-11-01）。

把「解析一次、多处消费」落成一个明确的数据结构：生成后的 SQL → `SqlIR`
（方言 + AST + 逻辑计划），供投影检查 / 方言归一 / 治理 / 观测共用，
避免每个模块各调一次 `sqlglot.parse_one` 并各自维护方言映射。

**定位**：这是工程件，不承诺提分；默认路径的行为必须与解析分散时**完全一致**
（解析失败仍然「不判 / 交给安全闸」，不新增失败出口）。

当前消费方：`projection.check_projection`。后续（未做）：guard 表作用域、
值接地谓词抽取、治理改写的 AST 复用。

逻辑计划（`logical_plan`）只收**结构**：表/列名与形状，不含字面量值 ——
它用于观测与诊断，不能被当成真值或回传给模型。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

#: 规范化引擎键 → sqlglot 方言名；方言不明（空串）走通用解析。
DIALECT_BY_ENGINE = {"postgres": "postgres", "mysql": "mysql", "sqlserver": "tsql"}


@dataclass(frozen=True)
class SqlIR:
    """一条生成 SQL 的解析结果。`tree is None` 表示解析失败（调用方各自 fail-open）。"""

    sql: str
    engine: str
    dialect: str | None
    tree: exp.Expression | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.tree is not None


def normalize_engine(engine: str | None) -> str:
    return str(engine or "").strip().lower()


def parse(sql: str, engine: str | None = "") -> SqlIR:
    """解析 SQL；失败不抛，返回 `tree=None` 并带上错误文本（截 200 字）。"""
    text = str(sql or "").strip()
    key = normalize_engine(engine)
    dialect = DIALECT_BY_ENGINE.get(key)
    if not text:
        return SqlIR(sql=text, engine=key, dialect=dialect, tree=None, error="empty sql")
    try:
        tree = sqlglot.parse_one(text, read=dialect)
    except Exception as exc:  # noqa: BLE001 —— 解析失败交给调用方决定，不在这里抛
        return SqlIR(sql=text, engine=key, dialect=dialect, tree=None,
                     error=f"{type(exc).__name__}: {str(exc)[:200]}")
    if tree is None:
        return SqlIR(sql=text, engine=key, dialect=dialect, tree=None, error="empty tree")
    return SqlIR(sql=text, engine=key, dialect=dialect, tree=tree)


def outer_select(tree: exp.Expression | None) -> exp.Select | None:
    """最外层 SELECT；UNION 取第一支。子查询不参与形状判定。"""
    if tree is None:
        return None
    if isinstance(tree, exp.Select):
        return tree
    if isinstance(tree, exp.Union):
        left = tree.this
        if isinstance(left, exp.Select):
            return left
        return left.find(exp.Select) if left is not None else None
    return tree.find(exp.Select)


def projection_shape(ir: SqlIR) -> dict | None:
    """`{"star": bool, "width": int}`；解析不出最外层 SELECT 返回 None。"""
    select = outer_select(ir.tree)
    if select is None:
        return None
    star = False
    for expression in select.expressions:
        if isinstance(expression, exp.Star):
            star = True
            break
        if isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star):
            star = True
            break
    return {"star": star, "width": len(select.expressions)}


def _table_name(table: exp.Table) -> str:
    name = str(table.name or "").strip()
    if not name:
        return ""
    schema = str(table.db or "").strip()
    return f"{schema}.{name}" if schema else name


def logical_plan(ir: SqlIR) -> dict | None:
    """结构化的逻辑计划（只含形状与标识符，不含字面量值）。"""
    select = outer_select(ir.tree)
    if select is None:
        return None
    tree = ir.tree
    tables: list[str] = []
    joins: list[dict] = []
    for source in tree.find_all(exp.From):
        this = source.this
        if isinstance(this, exp.Table):
            tables.append(_table_name(this))
    for join in tree.find_all(exp.Join):
        this = join.this
        joins.append({
            "table": _table_name(this) if isinstance(this, exp.Table) else "",
            "on": join.args.get("on") is not None,
        })
    projections = []
    for expression in select.expressions:
        if isinstance(expression, exp.Star):
            projections.append("*")
            continue
        alias = str(expression.alias or "").strip()
        if isinstance(expression, exp.Column):
            projections.append(alias or str(expression.name or "").strip())
            continue
        projections.append(alias or type(expression).__name__)
    group = select.args.get("group")
    order = select.args.get("order")
    limit = select.args.get("limit")
    return {
        "ctes": [str(c.alias_or_name or "") for c in tree.find_all(exp.CTE)],
        "union": isinstance(tree, exp.Union),
        "distinct": bool(select.args.get("distinct")),
        "star": any(p == "*" for p in projections),
        "width": len(projections),
        "projections": projections[:50],
        "tables": tables,
        "joins": joins[:50],
        "where": select.args.get("where") is not None,
        "group_by": len(group.expressions) if group is not None else 0,
        "order_by": len(order.expressions) if order is not None else 0,
        "limit": limit is not None,
    }


def plan_digest(plan: dict | None) -> str:
    """逻辑计划的稳定摘要（观测用）；空计划返回空串。"""
    if not plan:
        return ""
    blob = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = [
    "DIALECT_BY_ENGINE", "SqlIR", "normalize_engine", "parse", "outer_select",
    "projection_shape", "logical_plan", "plan_digest",
]
