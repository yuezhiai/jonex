"""治理 P1：② 生成后 SQL 的掩码 / 行过滤 / 敏感列拒绝（sqlglot）。

P1 规则：
- 直接投影的敏感列 → 掩码（`NULL AS col` 或 `'***' AS col`）；
- 敏感列出现在 WHERE/JOIN/GROUP/HAVING/ORDER 或聚合/表达式里 → 拒绝；
- 行过滤 → 把表引用包成 `(SELECT * FROM t WHERE <filter>) AS alias`。

本模块不连库；决策由调用方注入 `decide_fn(table, column, action) -> Decision`。
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from ..schema_source import normalize_engine
from .pdp import Decision

#: W2.7c：治理改写覆盖 OLAP；时序（Influx v3）无 sqlglot 方言 → 显式拒绝。
_DIALECTS = {"postgres": "postgres", "mysql": "mysql",
             "sqlserver": "tsql", "clickhouse": "clickhouse"}


@dataclass(frozen=True)
class RewriteResult:
    ok: bool
    sql: str
    reason: str = ""
    decisions: tuple = ()
    #: 拒绝细分（观测口径，D1）：`deny` = 密级/策略拒绝；
    #: `mask_position` = 需掩码的列出现在谓词/聚合；`parse_error` 等 = 改写面故障。
    kind: str = ""


def engine_to_dialect(engine: str) -> str:
    # 数据源登记常见 `postgresql`，而 sqlglot 的方言键是 `postgres`；不归一就会
    # `read=None` 自动探测，遇到 `~` 这类 Postgres 专有语法直接 ParseError，
    # 治理改写把合法查询误判成「解析失败」拒掉。
    key = normalize_engine(engine) or str(engine or "").strip().lower()
    return _DIALECTS.get(key, "")


def _table_name(table: exp.Table) -> str:
    parts = [str(p) for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts)


def _schema_for(table_name: str, schema: dict[str, set[str]]) -> set[str] | None:
    """精确匹配优先；否则按末段唯一匹配；多义返回 None。"""
    if table_name in schema:
        return schema[table_name]
    last = table_name.split(".")[-1].lower()
    hits = [cols for name, cols in schema.items()
            if name.split(".")[-1].lower() == last]
    if len(hits) == 1:
        return hits[0]
    return None


def _column_action(node: exp.Column, select: exp.Select | None) -> str:
    if node.find_ancestor(exp.AggFunc) is not None:
        return "aggregate"
    if select is not None:
        for item in select.expressions:
            target = item.this if isinstance(item, exp.Alias) else item
            if target is node:
                return "select"
    return "predicate"


def _mask_expr(mask_type: str | None):
    if str(mask_type or "null") == "redact":
        return exp.Literal.string("***")
    return exp.Null()


def _apply_mask(node: exp.Column, mask_type: str | None) -> None:
    expr = _mask_expr(mask_type)
    parent = node.parent
    if isinstance(parent, exp.Alias):
        parent.set("this", expr)
        return
    if isinstance(parent, exp.Select):
        idx = parent.expressions.index(node)
        parent.expressions[idx] = exp.alias_(expr, node.name)
        return
    # 保守：无法安全替换的位置按拒绝处理（调用方已在前面判过 select 直接投影）
    raise ValueError("mask position is not a direct projection")


def apply_governance(sql: str, *, engine: str, decide_fn,
                     schema: dict[str, set[str]] | None = None) -> RewriteResult:
    """解析并改写 SQL；返回可直接执行的 SQL 或拒绝原因。"""
    engine_key = normalize_engine(engine) or str(engine or "").strip().lower()
    if engine_key == "influx_v3":
        return RewriteResult(
            False, sql,
            "InfluxDB v3 没有 sqlglot 方言，治理层拒绝改写（不执行未审查 SQL）",
            kind="unsupported_dialect")
    dialect = engine_to_dialect(engine)
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception as exc:  # noqa: BLE001 —— 解析失败按拒绝处理（不执行未审查 SQL）
        return RewriteResult(False, sql, f"SQL 解析失败：{type(exc).__name__}",
                             kind="parse_error")
    if tree is None:
        return RewriteResult(False, sql, "SQL 解析失败", kind="parse_error")

    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    tables = [t for t in tree.find_all(exp.Table)
              if not (t.name in ctes and not t.db)]
    if not tables:
        return RewriteResult(True, sql)

    # alias / 末段 → 限定名
    table_map: dict[str, str] = {}
    for t in tables:
        name = _table_name(t)
        table_map[t.alias_or_name] = name
        table_map[t.name] = name
        table_map[name] = name

    decisions: list[dict] = []
    masks: list[tuple[exp.Column, str | None]] = []
    row_filters: dict[str, list[str]] = {}
    schema = dict(schema or {})

    # 表级判定：COUNT(*) / SELECT * 不一定有 Column 节点，表级 deny 与行过滤
    # 必须在表引用上单独评估一次（2026-10-18 真机发现）。
    for t in tables:
        name = _table_name(t)
        d = decide_fn(name, None, "select")
        decisions.append({"table": name, "column": None, "action": "select",
                          "effect": d.effect, "policy_id": d.policy_id,
                          "reason": d.reason, "row_filters": list(d.row_filters)})
        if d.effect == "deny":
            return RewriteResult(False, sql, f"敏感数据：表 {name}（{d.reason or '拒绝'}）",
                                 tuple(decisions), kind="deny")
        for rf in d.row_filters:
            if rf not in row_filters.get(name, []):
                row_filters.setdefault(name, []).append(rf)

    for node in list(tree.find_all(exp.Column)):
        if node.table:
            table = table_map.get(node.table)
            if table is None:
                # 不认识限定名：按原样尝试精确匹配
                table = node.table
            candidates = [table]
        else:
            candidates = [t for t in table_map.values()
                          if node.name in (_schema_for(t, schema) or set())]
            if not candidates:
                # 没有 schema 信息：退化为所有引用表（保守取最严）
                candidates = sorted(set(table_map.values()))
        select = node.find_ancestor(exp.Select)
        action = _column_action(node, select)
        worst: Decision | None = None
        for table in candidates:
            d = decide_fn(table, node.name, action)
            decisions.append({"table": table, "column": node.name,
                              "action": action, "effect": d.effect,
                              "policy_id": d.policy_id, "reason": d.reason,
                              "row_filters": list(d.row_filters)})
            if worst is None or _severity(d.effect) > _severity(worst.effect):
                worst = d
            for rf in d.row_filters:
                # 同一策略会在每个列引用上重复返回；表级判定已去重，列级也必须
                # 去重，否则 10 个列 = 同一行过滤嵌套 10 层（语义对、SQL 丑且膨胀）。
                bucket = row_filters.setdefault(table, [])
                if rf not in bucket:
                    bucket.append(rf)
        if worst is None:
            continue
        if worst.effect == "deny":
            return RewriteResult(
                False, sql,
                f"敏感数据：{node.name}（{worst.reason or '拒绝'}）",
                tuple(decisions), kind="deny")
        if worst.effect == "mask":
            if action != "select":
                return RewriteResult(
                    False, sql,
                    f"敏感列 {node.name} 出现在 {action} 中，禁止使用"
                    f"（{worst.reason or '需要更高密级'}）",
                    tuple(decisions), kind="mask_position")
            masks.append((node, worst.mask_type))

    for node, mask_type in masks:
        _apply_mask(node, mask_type)

    for table in tables:
        filters = row_filters.get(_table_name(table))
        if not filters:
            continue
        inner = table.copy()
        sub = exp.select("*").from_(inner)
        for raw in filters:
            try:
                sub = sub.where(sqlglot.parse_one(raw, read=dialect or None))
            except Exception:  # noqa: BLE001 —— 策略表达式坏了：拒绝，不执行
                return RewriteResult(False, sql, f"行过滤表达式非法：{raw}",
                                     tuple(decisions), kind="row_filter_error")
        alias = table.alias_or_name
        table.replace(exp.Subquery(
            this=sub,
            alias=exp.TableAlias(this=exp.to_identifier(alias)),
        ))

    try:
        out = tree.sql(dialect=dialect or None)
    except Exception as exc:  # noqa: BLE001
        return RewriteResult(False, sql, f"SQL 序列化失败：{type(exc).__name__}",
                             tuple(decisions), kind="serialize_error")
    return RewriteResult(True, out, "", tuple(decisions))


def _severity(effect: str) -> int:
    return {"allow": 0, "mask": 1, "deny": 2}.get(str(effect), 0)
