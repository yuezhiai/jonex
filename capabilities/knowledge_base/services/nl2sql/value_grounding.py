#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""字面量值接地（P1-1）：把 SQL 里的字符串字面量和库里的真实值对齐。

v1 只做**高置信度**的确定性归一（大小写/首尾空白），不调模型：

1. sqlglot 从最终 SQL 抽 `col = 'x'` / `col IN (...)` / `col LIKE '...'` 里的字符串字面量；
2. 按 `table/column` 在**当前 SQL 白名单**内查 `DISTINCT` 候选（`LOWER(CAST(col)) LIKE`，
   LIMIT 5、3s 超时、失败 fail-open）；
3. 候选里存在**大小写/空白**差异的同一个值时，直接改写 SQL 里的字面量；
4. 其余情况（拼写/缩写，如 USA vs US）v1 不动 —— 留给后续「模型反馈」版本，避免猜错。

AV-SQL 的完整形态是把候选值反馈给模型重生成（见报告）；这里先做确定性版：
零额外 LLM 调用、零延迟放大风险（只在命中时改字符串），适合先 A/B。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from sqlglot import exp

from ..schema_source import normalize_engine
from .sql_ir import parse as parse_ir

#: 单题最多检查几个字面量（防止大 SQL 把库拖慢）。
MAX_LITERALS = 5
#: 单次值查询的上限（行）与超时（秒）。
LOOKUP_LIMIT = 20
LOOKUP_TIMEOUT = 3.0
#: 值查询只接受简单标识符（与 evidence_probe 的白名单同口径）。
_IDENT_RE = re.compile(r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*)?$")


@dataclass(frozen=True)
class LiteralPredicate:
    table: str
    column: str
    literal: str


def _safe_ident(value: str) -> str | None:
    text = str(value or "").strip()
    return text if _IDENT_RE.match(text) else None


def _alias_map(tree: exp.Expression) -> dict[str, str]:
    out: dict[str, str] = {}
    tables = list(tree.find_all(exp.Table))
    for table in tables:
        name = table.name
        if not name:
            continue
        parts = [p for p in (table.catalog, table.db, table.name) if p]
        full = ".".join(parts) or name
        out[(table.alias_or_name or name).casefold()] = full
        out[name.casefold()] = full
    return out


def _column_table(column: exp.Column, aliases: dict[str, str],
                  only_table: str | None) -> str | None:
    if column.table:
        return aliases.get(str(column.table).casefold())
    return only_table


def _literal_text(node) -> str | None:
    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this)
    return None


def extract_literals(sql: str, engine: str = "") -> list[LiteralPredicate]:
    """抽字符串字面量 → `(table, column, literal)`；解析失败/不安全的返回空列表。"""
    ir = parse_ir(sql, engine)
    if not ir.ok:  # 解析失败就跳过（安全闸会兜底）
        return []
    tree = ir.tree
    aliases = _alias_map(tree)
    tables = list(tree.find_all(exp.Table))
    only_table = str(tables[0]) if len(tables) == 1 else None
    found: list[LiteralPredicate] = []
    seen: set[tuple[str, str, str]] = set()

    def add(column, literal, table_hint=None):
        if not isinstance(column, exp.Column):
            return
        literal = _literal_text(literal)
        if literal is None:
            return
        literal = literal.strip()
        if len(literal) < 2 or len(literal) > 120:
            return
        # v1 不处理 LIKE 通配符（替换会丢 %/_，先跳过；留给后续「模型反馈」版）。
        if "%" in literal or "_" in literal:
            return
        table = table_hint or _column_table(column, aliases, only_table)
        table = _safe_ident(str(table or ""))
        col = _safe_ident(column.name)
        if not table or not col:
            return
        key = (table.casefold(), col.casefold(), literal)
        if key in seen:
            return
        seen.add(key)
        found.append(LiteralPredicate(table=table, column=col, literal=literal))

    for node in tree.find_all(exp.EQ):
        left, right = node.this, node.expression
        if isinstance(left, exp.Column):
            add(left, right)
        elif isinstance(right, exp.Column):
            add(right, left)
    for node in tree.find_all(exp.In):
        column = node.this
        for item in node.expressions or []:
            add(column, item)
    for node in tree.find_all(exp.Like):
        add(node.this, node.expression)
    return found[:MAX_LITERALS]


def _probe_prefix(literal: str) -> str:
    """第二路探测的前缀：全大写缩写取首字母，其余取前 4 个字符。"""
    text = str(literal or "").strip()
    if text.isalpha() and text.isupper() and 2 <= len(text) <= 6:
        return text[:1]
    return text[:4]


def build_lookup_sql(engine: str, predicate: LiteralPredicate, *,
                     limit: int = LOOKUP_LIMIT) -> str | None:
    """按方言拼「包含该字面量 **或** 以探测前缀开头」的真实值；标识符不合法返回 None。

    两路是为了覆盖拼写/缩写：`Netherlandic` 用前 4 字符 `Neth%` 能命中 `Netherlands`；
    `USA` 用首字母 `U%` 能命中 `United States of America`（再由 initialism 精确判定）。
    """
    table = _safe_ident(predicate.table)
    column = _safe_ident(predicate.column)
    if not table or not column:
        return None
    literal = str(predicate.literal).replace("'", "''")
    prefix = _probe_prefix(predicate.literal).replace("'", "''")
    key = normalize_engine(engine)
    if key == "postgres":
        return (f"SELECT DISTINCT {column} FROM {table} "
                f"WHERE LOWER(CAST({column} AS TEXT)) LIKE LOWER('%{literal}%') "
                f"OR LOWER(CAST({column} AS TEXT)) LIKE LOWER('{prefix}%') "
                f"LIMIT {int(limit)}")
    if key == "mysql":
        return (f"SELECT DISTINCT {column} FROM {table} "
                f"WHERE LOWER(CAST({column} AS CHAR)) LIKE LOWER('%{literal}%') "
                f"OR LOWER(CAST({column} AS CHAR)) LIKE LOWER('{prefix}%') "
                f"LIMIT {int(limit)}")
    if key == "sqlserver":
        return (f"SELECT DISTINCT TOP {int(limit)} {column} FROM {table} "
                f"WHERE LOWER(CONVERT(NVARCHAR(MAX), {column})) LIKE LOWER('%{literal}%') "
                f"OR LOWER(CONVERT(NVARCHAR(MAX), {column})) LIKE LOWER('{prefix}%')")
    return None


def pick_case_match(literal: str, candidates: list) -> str | None:
    """候选里与 literal **仅大小写/首尾空白不同**的值（唯一时返回）。"""
    literal = str(literal or "").strip()
    if not literal:
        return None
    matches: list[str] = []
    for candidate in candidates or []:
        text = str(candidate or "").strip()
        if not text or text == literal:
            continue
        if text.casefold() == literal.casefold():
            if text not in matches:
                matches.append(text)
    return matches[0] if len(matches) == 1 else None


_INITIALISM_STOPWORDS = {"of", "the", "and", "for", "de", "la", "van", "von", "der"}


def _initialism(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", str(text or ""))
    return "".join(w[0].upper() for w in words
                   if w.casefold() not in _INITIALISM_STOPWORDS)


def pick_value_match(literal: str, candidates: list) -> str | None:
    """高置信度值归一：大小写/空白、唯一 initialism、唯一前缀扩展、唯一 fuzzy。

    只在**恰好一个**候选满足时返回；多个候选/低相似度一律不猜。
    """
    literal = str(literal or "").strip()
    if not literal:
        return None
    cleaned: list[str] = []
    for candidate in candidates or []:
        text = str(candidate or "").strip()
        if text and text != literal and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        return None
    # 1) 仅大小写/空白不同
    case = [c for c in cleaned if c.casefold() == literal.casefold()]
    if len(case) == 1:
        return case[0]
    # 2) 唯一 initialism（United States of America -> USA）
    literal_initial = re.sub(r"[^A-Za-z0-9]", "", literal).upper()
    initials = [c for c in cleaned
                if len(_initialism(c)) >= 2 and _initialism(c) == literal_initial]
    if len(initials) == 1:
        return initials[0]
    # 3) 唯一前缀扩展（Netherlandic -> Netherlands；差 ≤3 字符）
    lit_fold = literal.casefold()
    prefix = [c for c in cleaned
              if c.casefold().startswith(lit_fold)
              and 0 < len(c) - len(literal) <= 3]
    if len(prefix) == 1:
        return prefix[0]
    # 4) 唯一 fuzzy（编辑相似度 ≥0.85，且比第二名高 0.05）
    scored = sorted(
        ((difflib.SequenceMatcher(None, lit_fold, c.casefold()).ratio(), c)
         for c in cleaned),
        reverse=True)
    if (scored and scored[0][0] >= 0.85 and len(scored[0][1]) >= 4
            and len(literal) >= 4
            and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05)):
        return scored[0][1]
    return None


def replace_literals(sql: str, replacements: dict[str, str], engine: str = "") -> str:
    """把 SQL 里**字符串字面量节点**的值替换掉；解析失败返回原 SQL。"""
    if not replacements:
        return sql
    ir = parse_ir(sql, engine)
    if not ir.ok:
        return sql
    tree = ir.tree
    changed = False
    for node in tree.find_all(exp.Literal):
        if not node.is_string:
            continue
        old = str(node.this)
        if old in replacements:
            node.replace(exp.Literal.string(replacements[old]))
            changed = True
    return tree.sql(dialect=ir.dialect) if changed else sql


__all__ = ["LiteralPredicate", "extract_literals", "build_lookup_sql",
           "pick_case_match", "pick_value_match", "replace_literals",
           "MAX_LITERALS", "LOOKUP_LIMIT", "LOOKUP_TIMEOUT"]
