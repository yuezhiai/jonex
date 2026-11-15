#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""N1-1 verified query 快路径的纯函数层：匹配 / 参数抽取 / 模板渲染 / 表名解析。

口径（设计 `docs/superpowers/specs/2026-11-07-nl2sql-verified-query-design.md`）：

- **只做词面匹配**：归一化后精确相等 = 1.0，否则词元 Jaccard；低于 `floor` 不命中，
  最高与次高差 < `AMBIGUITY_MARGIN` 判歧义（宁可回退正常链路，也不猜）。
- **参数只从登记的正则抽**：每个参数一条带捕获组的正则；抽不到 / 不合法一律 `None`，
  由调用方回退。v1 所有参数都按必填处理（`required` 字段留给 UI 表达，不改变行为）。
- **渲染只在值位置落值**：模板占位符 `{{name}}`；字符串/日期/枚举单引号包裹并转义，
  数字/布尔原样。出现未声明占位符或渲染后仍残留 `{{` 一律 `ValueError`。
- 本模块不读库、不调模型、不执行 SQL；执行与安全链在 `service.py`。
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Iterable, Optional

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from ..schema_source import normalize_engine

__all__ = [
    "AMBIGUITY_MARGIN",
    "PARAM_TYPES",
    "extract_params",
    "match_query",
    "normalize_question",
    "rank_query",
    "render_template",
    "score_question",
    "template_tables",
    "validate_param_value",
]

#: 最高分与次高分小于该差值 ⇒ 判歧义（两个模板同样像，选错比回退更贵）。
AMBIGUITY_MARGIN = 0.02

#: 支持的参数类型；新增类型必须同时补 `validate_param_value` 与渲染引号策略。
PARAM_TYPES = ("string", "number", "date", "boolean", "enum")

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_CJK_RUN = re.compile(r"[一-鿿]+")
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_DIALECT = {"postgres": "postgres", "mysql": "mysql", "sqlserver": "tsql"}

#: 需要加引号并转义的参数类型（其余原样落值）。
_QUOTED_TYPES = frozenset({"string", "date", "enum"})


def normalize_question(text: str) -> str:
    """casefold + 非词字符压成单空格 + 去首尾空白（中英文同一口径）。"""
    folded = str(text or "").casefold()
    return " ".join(_NON_WORD.sub(" ", folded).split())


def _tokens(text: str) -> set[str]:
    """词元集合：非中文按空白分词，中文按二元组（中文没有空格，整串当一词会稀释重合）。

    含中文的 token **不再**把整串收进去：否则「华东环保…」与「华南环保…」的差异被
    整串 token 放大，Jaccard 被压到 0.5 以下，正常的近似问句反而匹配不上。
    """
    norm = normalize_question(text)
    if not norm:
        return set()
    tokens: set[str] = set()
    for token in norm.split():
        runs = _CJK_RUN.findall(token)
        if not runs:
            tokens.add(token)
            continue
        for run in runs:
            if len(run) == 1:
                tokens.add(run)
            else:
                for index in range(len(run) - 1):
                    tokens.add(run[index:index + 2])
        # 拉丁/数字片段（如 `abc华东`）仍按原 token 收一次，避免丢信息
        if not _CJK_RUN.sub("", token).strip():
            continue
        tokens.add(token)
    return tokens


def score_question(question: str, candidate: str) -> float:
    """一条问句与一条登记问句的相似度：精确相等 1.0，否则词元 Jaccard。"""
    if normalize_question(question) == normalize_question(candidate):
        return 1.0 if normalize_question(question) else 0.0
    left, right = _tokens(question), _tokens(candidate)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _mask_params(text: str, params: Iterable[dict]) -> str:
    """把参数正则命中的片段替换成占位词 —— 参数**取值**不该影响模板的匹配。

    `top 5 jobs` 与 `top 9 jobs` 指同一条模板：不掩掉取值，Jaccard 只有 0.5，
    带参模板会因为「用户输入了别的数字」而永远命不中。
    """
    out = str(text or "")
    for param in params or []:
        pattern = str((param or {}).get("extract") or "").strip()
        if not pattern:
            continue
        try:
            out = re.sub(pattern, " __p__ ", out, flags=re.IGNORECASE)
        except re.error:
            continue
    return out


def rank_query(question: str, rows: Iterable[dict],
               *, floor: float) -> Optional[tuple[dict, float]]:
    """在 active 行里找唯一最佳匹配；返回 `(row, score)`；低于 floor 或歧义 `None`。

    打分前两边都按该行的参数正则做掩码：参数取值不同不影响命中（见 `_mask_params`）。
    """
    scored: list[tuple[float, dict]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "active") != "active":
            continue
        params = row.get("params") or []
        masked_question = _mask_params(question, params)
        questions = row.get("questions") or []
        best = 0.0
        for candidate in questions:
            best = max(best, score_question(
                masked_question, _mask_params(str(candidate), params)))
        if best > 0.0:
            scored.append((best, row))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_row = scored[0]
    if best_score < floor:
        return None
    if len(scored) > 1:
        second_score = scored[1][0]
        if second_score >= floor and best_score - second_score < AMBIGUITY_MARGIN:
            return None
    return best_row, best_score


def match_query(question: str, rows: Iterable[dict],
                *, floor: float) -> Optional[dict]:
    """在 active 行里找唯一最佳匹配；只要行（观测里需要 score 时用 `rank_query`）。"""
    hit = rank_query(question, rows, floor=floor)
    return hit[0] if hit else None


def validate_param_value(value: Any, param: dict) -> tuple[bool, str]:
    """按声明类型校验参数值；返回 `(ok, 失败原因)`（原因只进日志/审计，不回给模型）。"""
    if not isinstance(value, str):
        return False, "value is not a string"
    kind = str((param or {}).get("type") or "string").strip().lower()
    if kind == "string":
        if not value or len(value) > 200:
            return False, "string must be 1..200 chars"
        return True, ""
    if kind == "number":
        return (True, "") if _NUMBER.match(value) else (False, "not a number")
    if kind == "date":
        if not _DATE.match(value):
            return False, "date must be YYYY-MM-DD"
        try:
            _dt.date.fromisoformat(value)
        except ValueError:
            return False, "invalid date"
        return True, ""
    if kind == "boolean":
        return (True, "") if value.casefold() in ("true", "false") else (
            False, "boolean must be true/false")
    if kind == "enum":
        allowed = [str(v) for v in (param or {}).get("enum") or []]
        return (True, "") if value in allowed else (False, "not in enum")
    return False, f"unsupported type: {kind}"


def extract_params(question: str, params: Iterable[dict]) -> Optional[dict]:
    """按每个参数登记的 `extract` 正则取第 1 组；任何一步失败返回 `None`（回退）。"""
    values: dict[str, str] = {}
    for param in params or []:
        if not isinstance(param, dict):
            return None
        name = str(param.get("name") or "").strip()
        pattern = str(param.get("extract") or "").strip()
        if not name or not pattern:
            return None
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return None
        match = regex.search(str(question or ""))
        if match is None or match.lastindex is None:
            return None
        value = str(match.group(1) or "").strip()
        ok, _ = validate_param_value(value, param)
        if not ok:
            return None
        values[name] = value
    return values


def _render_value(value: str, param: dict) -> str:
    ok, reason = validate_param_value(value, param)
    if not ok:
        raise ValueError(f"invalid value for {param.get('name')}: {reason}")
    kind = str((param or {}).get("type") or "string").strip().lower()
    if kind in _QUOTED_TYPES:
        return "'" + value.replace("'", "''") + "'"
    return value


def render_template(template: str, params: Iterable[dict],
                    values: dict) -> str:
    """把 `{{name}}` 替换成安全字面量；未知占位符 / 缺值 / 残留花括号都 `ValueError`。"""
    text = str(template or "")
    defs = {str(p.get("name") or ""): p for p in params or []
            if isinstance(p, dict) and str(p.get("name") or "")}
    declared = set(defs)
    used = set(_PLACEHOLDER.findall(text))
    if used - declared:
        raise ValueError(f"undeclared placeholders: {sorted(used - declared)}")
    missing = declared - set(values)
    if missing:
        raise ValueError(f"missing values: {sorted(missing)}")

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        raw = values[name]
        if not isinstance(raw, str):  # 拒绝隐式 str() 强转（5 → "5" 会绕过类型校验）
            raise ValueError(f"value for {name} is not a string")
        return _render_value(raw, defs[name])

    out = _PLACEHOLDER.sub(_sub, text)
    if "{{" in out:
        raise ValueError("unbalanced placeholder left in template")
    return out


def template_tables(sql: str, engine: str) -> Optional[set[str]]:
    """模板引用的物理表名（排除 CTE 名）；解析失败返回 `None`（调用方应回退）。"""
    dialect = _DIALECT.get(normalize_engine(engine))
    if not dialect:
        return None
    try:
        tree = sqlglot.parse_one(str(sql or ""), dialect=dialect)
    except Exception:  # noqa: BLE001 —— 解析不了就让调用方回退
        return None
    if tree is None:
        return None
    tables: set[str] = set()
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                parts = [p for p in (source.catalog, source.db, source.name) if p]
                if parts:
                    tables.add(".".join(parts))
    return tables
