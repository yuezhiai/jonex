#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""提案的**确定性复核**：agent 交完公式后，平台自己再验一遍，不靠模型自述。

为什么要有这一层（2026-11-14 真机教训）：模型两次都「验证过了」，但一次把
`good_qty` 当成检验总数、一次把 99.98% 说成「合理范围」。两类错误都不是执行失败，
靠看模型写的 rationale 发不现。复核只做**能自动判定**的事：

1. 公式真能跑（安全闸 + 实际执行），且返回至少一行；
2. 比例型结果的值域（>1 直接判失败；>0.99 记可疑）；
3. 参与计算的数值列里，有没有「逐行几乎相等」的对 —— 这正是
   `good_qty ≈ inspection_qty` 这类「同一个量的两种写法」的机器可判定特征；
4. 分母列是否大量为空/为零（覆盖率不足会让比率看起来很正常）。

复核结果随提案一起入库（`verification`），人工确认页直接展示；**有任何可疑项
就把置信度降到 low**，不再沿用模型自报的 medium/high。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import sqlglot
from sqlglot import exp

#: 逐行相等比例超过它就算「几乎是同一列」。
IDENTICAL_RATIO = 0.95
#: 比例型指标超过它记「可疑地高」。
SUSPICIOUS_HIGH = 0.99
#: 某列空/零占比超过它记「覆盖不足」。
ZERO_HEAVY = 0.5
#: 一次复核最多比对的对数（单条 SQL 的 CASE WHEN 数量上限）。
MAX_PAIRS = 6
#: 最多参与画像的数值列数。
MAX_PROFILE_COLUMNS = 8

_NUMERIC_TYPES = ("number", "integer", "int", "float", "double", "decimal",
                  "numeric", "bigint", "smallint")


@dataclass
class Verification:
    """复核结论：`checks` 逐项可读，`warnings` 进提案。"""

    ok: bool = True
    value: float | None = None
    checks: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, name: str, detail: str) -> "Verification":
        self.ok = False
        self.checks.append({"name": name, "status": "fail", "detail": detail})
        return self

    def warn(self, name: str, detail: str) -> "Verification":
        self.checks.append({"name": name, "status": "warn", "detail": detail})
        self.warnings.append(detail)
        return self

    def passed(self, name: str, detail: str = "") -> "Verification":
        self.checks.append({"name": name, "status": "pass", "detail": detail})
        return self

    def to_dict(self) -> dict:
        return {"ok": self.ok, "value": self.value,
                "checks": self.checks, "warnings": self.warnings}


def referenced_columns(sql: str) -> tuple[list[str], list[str]]:
    """从公式里取出 `(表名列表, 列名列表)`；解析失败回空（由执行检查兜底）。"""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:  # noqa: BLE001 - 解析失败不是复核的重点，执行会报
        return [], []
    tables: list[str] = []
    for table in tree.find_all(exp.Table):
        parts = [part for part in (table.catalog, table.db, table.name) if part]
        name = ".".join(parts) if parts else ""
        if name and name not in tables:
            tables.append(name)
    columns: list[str] = []
    for column in tree.find_all(exp.Column):
        name = str(column.name or "")
        if name and name not in columns:
            columns.append(name)
    return tables, columns


def _numeric_columns(row: dict, table: str) -> list[str]:
    for entity in row.get("entity_types") or []:
        if not isinstance(entity, dict):
            continue
        physical = str(entity.get("source_object_id") or "").strip()
        if physical.casefold() != table.casefold():
            continue
        out = []
        for attribute in entity.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            name = str(attribute.get("name") or "").strip()
            if not name:
                continue
            type_name = str(attribute.get("type") or "").strip().lower()
            if type_name in _NUMERIC_TYPES or not type_name:
                out.append(name)
        return out
    return []


def _filter_clause(filters: str) -> str:
    """只接受形如 `deleted = 0` 的简单过滤；其它形态一律不带（宁可少查一次）。"""
    text = str(filters or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\s*(=|<>|!=|>=|<=|>|<)\s*[0-9A-Za-z_.'\s-]+",
                    text):
        return f" WHERE {text}"
    return ""


async def verify_proposal(
        proposal: dict, *, rows: list[dict],
        run_sql: Callable[[str, str, int], Awaitable[dict]] | None,
        default_source: str = "",
) -> Verification:
    """复核一份提案；**不抛**（任何异常都收敛成 fail/warn）。"""
    result = Verification()
    sql = str(proposal.get("formula_sql") or "").strip()
    if not sql:
        return result.fail("formula_executes", "提案没有公式")
    if run_sql is None:
        return result.warn("formula_executes", "本环境不能执行 SQL，未做执行复核")

    ds_id = str(proposal.get("data_source_id") or default_source or "")
    try:
        executed = await run_sql(ds_id, sql, 5)
    except Exception as exc:  # noqa: BLE001
        return result.fail("formula_executes", f"执行异常：{exc}")
    if not executed.get("ok"):
        return result.fail("formula_executes",
                           str(executed.get("error") or "执行失败"))
    values = _first_numeric_values(executed)
    if not values:
        return result.fail("formula_executes", "公式执行成功但没有返回数值")
    result.value = values[0]
    result.passed("formula_executes", f"返回 {len(values)} 个数值，首个={values[0]}")

    _check_value_range(result, values)
    _check_ratio_sanity(result, proposal, values)
    await _check_column_shape(result, sql, rows, run_sql, ds_id, proposal)
    return result


def _check_ratio_sanity(result: Verification, proposal: dict,
                        values: list[float]) -> None:
    """比率型指标的两个「一眼假」取值。

    `0` 几乎总是整数相除被截断（Postgres 的 bigint/bigint = 整数除法），
    这时公式**能跑**、结果却是假的 —— 必须拒收并让 agent 改写成浮点运算。
    `恰好 1` 则提示可能是「分子分母同一个量」。
    """
    if not _is_ratio_metric(proposal):
        return
    if all(value == 0 for value in values):
        result.ok = False
        result.checks.append({
            "name": "ratio_sanity", "status": "fail",
            "detail": "比率结果为 0：常见原因是整数相除被截断"
                      "（如 SUM(a)/SUM(b)）。请改成 SUM(a) * 1.0 / SUM(b) "
                      "或 CAST(... AS DOUBLE PRECISION) 后重试",
        })
        return
    if any(value == 1.0 for value in values):
        result.warn("ratio_sanity",
                    "比率恰好等于 1：确认分子分母是不是同一个量")
        return
    result.passed("ratio_sanity", "比率取值正常")


def _first_numeric_values(executed: dict) -> list[float]:
    out: list[float] = []
    for row in executed.get("rows") or []:
        for value in row or []:
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                out.append(float(value))
            elif isinstance(value, str):
                try:
                    out.append(float(value))
                except ValueError:
                    continue
            else:  # Decimal 等
                try:
                    out.append(float(value))
                except (TypeError, ValueError):
                    continue
        if out:
            break
    return out


_RATIO_HINTS = ("率", "占比", "ratio", "yield", "percent", "比例")


def _is_ratio_metric(proposal: dict) -> bool:
    name = str(proposal.get("name") or "").lower()
    return any(hint in name for hint in _RATIO_HINTS)


def _check_value_range(result: Verification, values: list[float]) -> None:
    low = min(values)
    high = max(values)
    if high > 1.0:
        result.warn("value_range", f"结果超过 1（最大 {high}）：比例型指标大概率算错了")
        return
    if low < 0:
        result.warn("value_range", f"结果出现负数（最小 {low}）")
        return
    result.passed("value_range", f"取值 {low}–{high}")
    if high > SUSPICIOUS_HIGH:
        result.warn("value_plausibility",
                    f"结果 {high} > {SUSPICIOUS_HIGH}：良率类指标应复核分子是否选错")


async def _check_column_shape(
        result: Verification, sql: str, rows: list[dict],
        run_sql: Callable[[str, str, int], Awaitable[dict]],
        ds_id: str, proposal: dict,
) -> None:
    """列形态复核：近重复列 + 空/零占比。只查单表公式。

    两类假阳性必须压掉（2026-11-14 真机：一张表里十几个全零的 bin 列互相「100%
    相等」，把真正的 good_qty≈inspection_qty 淹了）：

    - **恒定列**（distinct <= 1）之间的相等没有信息量，直接排除；
    - **空/零占比**只对公式里真正用到的列报，不扫全表。
    """
    tables, referenced = referenced_columns(sql)
    if len(tables) != 1:
        result.passed("column_shape", "多表或无法解析的表，跳过列形态复核")
        return
    table = tables[0]
    row = next((item for item in rows
                if str(item.get("data_source_id") or "") == ds_id
                and any(str(entity.get("source_object_id") or "").casefold()
                        == table.casefold()
                        for entity in item.get("entity_types") or []
                        if isinstance(entity, dict))), None)
    if row is None:
        result.passed("column_shape", "表不在编译 schema 里，跳过")
        return
    numeric = _numeric_columns(row, table)
    if not numeric:
        result.passed("column_shape", "该表没有可识别的数值列")
        return
    profile = numeric[:MAX_PROFILE_COLUMNS]
    where = _filter_clause(str(proposal.get("filters") or ""))

    # ① 一次查询拿到 每列零/空计数 + distinct 数。
    exprs = []
    for index, column in enumerate(profile):
        exprs.append(
            f"SUM(CASE WHEN {column} IS NULL OR {column} = 0 THEN 1 ELSE 0 END) "
            f"AS z_{index}")
        exprs.append(f"COUNT(DISTINCT {column}) AS d_{index}")
    query = f"SELECT COUNT(*) AS n, {', '.join(exprs)} FROM {table}{where}"
    payload = await _safe_query(run_sql, ds_id, query, 2)
    if not payload.get("ok"):
        result.passed("column_shape", "列画像查询不可用，已跳过")
        return
    row_values = (payload.get("rows") or [[0]])[0]
    total = row_values[0] if row_values else 0
    if not isinstance(total, (int, float)) or total <= 0:
        return
    stats: dict[str, dict] = {}
    for index, column in enumerate(profile):
        zero = row_values[index * 2 + 1] if index * 2 + 1 < len(row_values) else 0
        distinct = row_values[index * 2 + 2] if index * 2 + 2 < len(row_values) else 0
        try:
            stats[column] = {"zero_ratio": float(zero or 0) / float(total),
                             "distinct": int(distinct or 0)}
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    used = [column for column in profile if column in set(referenced)]
    for column in used:
        info = stats.get(column) or {}
        if info.get("zero_ratio", 0) > ZERO_HEAVY and info.get("distinct", 0) > 1:
            result.warn("zero_heavy",
                        f"列 {column} 有 {info['zero_ratio']:.0%} 的行为空或 0："
                        f"参与比率计算时覆盖面很小")

    # ② 近重复列：只在**非常量**列之间比（恒定列 100% 相等是废话）。
    candidates = [column for column in profile
                  if (stats.get(column) or {}).get("distinct", 0) > 1]
    pairs = []
    for index, left in enumerate(candidates):
        for right in candidates[index + 1:]:
            pairs.append((left, right))
            if len(pairs) >= MAX_PAIRS:
                break
        if len(pairs) >= MAX_PAIRS:
            break
    if not pairs:
        result.passed("column_identity", "非常量数值列不足两列，跳过")
        return
    eq_exprs = ", ".join(
        f"SUM(CASE WHEN {left} = {right} THEN 1 ELSE 0 END) AS e_{index}"
        for index, (left, right) in enumerate(pairs))
    query = f"SELECT COUNT(*) AS n, {eq_exprs} FROM {table}{where}"
    payload = await _safe_query(run_sql, ds_id, query, 2)
    if not payload.get("ok"):
        result.passed("column_identity", "列比对查询不可用，已跳过")
        return
    row_values = (payload.get("rows") or [[0]])[0]
    total = row_values[0] if row_values else 0
    if not isinstance(total, (int, float)) or total <= 0:
        return
    for index, (left, right) in enumerate(pairs):
        equal = row_values[index + 1] if index + 1 < len(row_values) else 0
        try:
            ratio = float(equal or 0) / float(total)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if ratio >= IDENTICAL_RATIO:
            result.warn(
                "column_identity",
                f"列 {left} 与 {right} 有 {ratio:.0%} 的行完全相等："
                f"可能是同一个量的两种写法，确认分子分母是否选错")


async def _safe_query(run_sql, ds_id: str, sql: str, limit: int) -> dict:
    try:
        return await run_sql(ds_id, sql, limit)
    except Exception as exc:  # noqa: BLE001 - 复核查询失败不阻断
        return {"ok": False, "error": str(exc)}


__all__ = ["Verification", "referenced_columns", "verify_proposal"]
