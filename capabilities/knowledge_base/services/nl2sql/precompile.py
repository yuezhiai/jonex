#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行前可构建性校验（A1b）：用 compiled schema 校验 SQL 的列/别名可解析。

**这不是安全闸**（`guard.py` 仍是唯一安全闸）。它只回答一个质量问题：
「模型写的列，在这个数据源的真实 schema 里存在吗？」——答不上来时**一律放行**
（解析失败、方言不支持、查询里出现了 schema map 没有的表，都返回 `None`），
因为把「我们不知道」当成「列不存在」会制造比原问题更贵的误伤。

只在开关 `NL2SQL_PRECOMPILE_CHECK` 打开时被调用；关档时本模块不进任何路径。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from ..schema_source import normalize_engine

#: 平台引擎键 → sqlglot 方言名（`normalize_engine` 的取值只有 postgres/mysql/sqlserver）。
_DIALECT = {"postgres": "postgres", "mysql": "mysql", "sqlserver": "tsql"}

_COLUMN_MSG = re.compile(r"Column '([^']+)' could not be resolved", re.IGNORECASE)
_UNKNOWN_MSG = re.compile(r"Unknown column:\s*(\S+)", re.IGNORECASE)
_QUOTES = "\"[]`"

__all__ = [
    "PrecompileViolation",
    "build_schema_map",
    "check_precompile",
    "format_precompile_note",
]


@dataclass(frozen=True)
class PrecompileViolation:
    """一条可回给模型的 schema 解析违规。"""

    code: str          # unknown_column | ambiguous_column
    detail: str        # 英文短句（进 retry_note）
    table: str = ""
    column: str = ""


def _last_segment(name: str) -> str:
    """限定名/引号名 → 最后一段的 casefold（sqlglot 按表名末段查 schema）。"""
    text = str(name or "").strip().strip(_QUOTES)
    if not text:
        return ""
    return text.split(".")[-1].strip(_QUOTES).strip().casefold()


def build_schema_map(entities: list[dict] | None) -> dict[str, set[str]]:
    """compiled `entity_types` → `{表名末段: {列名}}`。

    列同时收逻辑名（`name`）与物理名（`physical_name`）：② 的 SQL 用物理列名，
    抽象 schema 的落点用逻辑名，两边都可能出现在模型输出里。同键合并（并集）。
    """
    out: dict[str, set[str]] = {}
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        table = (str(entity.get("source_object_id") or "").strip()
                 or str(entity.get("name") or "").strip())
        if not table:
            continue
        key = _last_segment(table)
        if not key:
            continue
        columns = out.setdefault(key, set())
        seen = {c.casefold() for c in columns}
        for attr in entity.get("attributes") or []:
            if not isinstance(attr, dict):
                continue
            for field in ("name", "physical_name"):
                column = str(attr.get(field) or "").strip()
                if column and column.casefold() not in seen:
                    columns.add(column)
                    seen.add(column.casefold())
    return out


def _referenced_tables(tree: exp.Expression) -> set[str]:
    """SQL 里引用的真实表名（用 scope 区分物理表与 CTE 别名）。

    不能简单地「排除所有 CTE 同名节点」：`WITH Jobs AS (SELECT ... FROM Jobs)` 里
    CTE 体中的 `Jobs` 是**真实表**，按名字全局排除会把整条查询放行。`scope.sources`
    里 CTE 源是 `exp.CTE`、物理表源是 `exp.Table`，按类型分即可。
    """
    tables: set[str] = set()
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                name = str(source.name or "").strip()
                if name:
                    tables.add(name)
    return tables


def check_precompile(
    sql: str,
    *,
    engine: str,
    schema_map: dict[str, set[str]],
) -> Optional[PrecompileViolation]:
    """校验 SQL 的列/别名能否在 `schema_map` 里解析；不能确定时返回 `None`。"""
    text = str(sql or "").strip()
    if not text or not schema_map:
        return None
    dialect = _DIALECT.get(normalize_engine(engine))
    if not dialect:
        return None
    try:
        tree = sqlglot.parse_one(text, dialect=dialect)
    except Exception:  # noqa: BLE001 —— 解析不了交给 guard/数据库，不在这一层制造失败
        return None
    if tree is None:
        return None
    tables = _referenced_tables(tree)
    if not tables:
        return None
    # 任何一张表不在 map 里（多跳/血缘路径表、预览表、表值函数…）→ 整体放行：
    # sqlglot 对未知表会把它的列判成 unresolved，继续校验就是误伤。
    if any(_last_segment(t) not in schema_map for t in tables):
        return None
    # schema 的键必须用 **SQL 里的实际拼写**：sqlglot 的 MySQL/Postgres 方言对
    # schema dict 做大小写/引号精确匹配，用 casefold 后的键会让 `FROM Jobs` 整张表
    # 解析失败，再叠加全库 owners 就把正常列误判成 ambiguous（复审 Critical #1）。
    referenced = {t: schema_map[_last_segment(t)] for t in tables}
    schema = {
        table: {column: "TEXT" for column in columns}
        for table, columns in referenced.items()
    }
    try:
        qualify(tree, schema=schema, validate_qualify_columns=True, dialect=dialect)
    except OptimizeError as exc:
        return _classify(str(exc), referenced)
    except Exception:  # noqa: BLE001 —— 方言/作用域边缘，放行
        return None
    return None


def _classify(
    message: str,
    referenced: dict[str, set[str]],
) -> Optional[PrecompileViolation]:
    """把 sqlglot 的列解析错误转成可行动的类型；认不出就放行。

    `owners` **只在被引用的表里找**：列只存在于别的表，真实问题是「这里的列写错了」，
    不是「歧义」；在整库范围找会把 unknown 漏报/误标成 ambiguous（复审 Important #4）。
    """
    match = _COLUMN_MSG.search(message) or _UNKNOWN_MSG.search(message)
    if not match:
        return None
    raw = match.group(1).strip().strip(_QUOTES)
    column = _last_segment(raw)
    if not column:
        return None
    owners = [
        table for table, columns in referenced.items()
        if column in {c.casefold() for c in columns}
    ]
    if not owners:
        return PrecompileViolation(
            "unknown_column",
            f"column '{raw}' does not exist in the referenced table(s)",
            table=next(iter(referenced)) if len(referenced) == 1 else "",
            column=column,
        )
    if len(owners) > 1 and "." not in raw:
        return PrecompileViolation(
            "ambiguous_column",
            f"column '{raw}' is ambiguous across tables; qualify it with a table alias",
            column=column,
        )
    # 列在被引用表里存在、但 sqlglot 仍解析失败：通常是作用域边缘 → 放行。
    return None


def format_precompile_note(
    violation: PrecompileViolation,
    schema_map: dict[str, set[str]],
    *,
    max_chars: int = 1500,
) -> str:
    """给模型的英文重写提示：违规原因 + 有界可用列清单（确定性排序）。"""
    head = (
        "The SQL cannot be validated against the database schema: "
        f"{violation.detail}. Rewrite it using only the tables and columns below."
    )
    lines = [head, "Available columns:"]
    used = len(head) + len("Available columns:") + 2
    for table in sorted(schema_map):
        columns = sorted(schema_map[table])[:40]
        if not columns:
            continue
        line = f"- {table}: {', '.join(columns)}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)[:max_chars]
