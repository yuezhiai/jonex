"""P2-4b：小 schema 采样行（`NL2SQL_SAMPLE_ROWS`）。

AV-SQL 的 schema 上下文里除了列示例值，还有每表 ≤3 行真实数据。示例值能回答
「这一列长什么样」，采样行能补「多列放在一起长什么样」（日期格式、枚举组合、
金额精度）。本模块只做**纯函数**：拼方言 SQL、裁剪返回行；执行与超时由调用方
（`service` 的只读执行器）负责。

安全边界：
- 表/列只来自编译 schema（平台认识的标识），这里仍按方言**引用**并对控制字符/超长
  标识符直接拒绝（返回 `None` = 不采样）；
- 只 SELECT 指定列 + LIMIT，绝不 `SELECT *`；
- 单元格截断 40 字符、总预算 2000 字符（按表序→行序→列序确定性截断）。
"""
from __future__ import annotations

MAX_SAMPLE_TABLES = 10
SAMPLE_ROWS_LIMIT = 3
SAMPLE_ROWS_BUDGET = 2000
SAMPLE_CELL_CHARS = 40
MAX_IDENT_CHARS = 128


def _quote_ident(name, engine: str) -> str:
    raw = str(name or "").strip()
    if not raw or len(raw) > MAX_IDENT_CHARS:
        return ""
    if any(ord(ch) < 32 for ch in raw):
        return ""
    if engine == "mysql":
        return "`" + raw.replace("`", "``") + "`"
    if engine == "sqlserver":
        return "[" + raw.replace("]", "]]") + "]"
    return '"' + raw.replace('"', '""') + '"'


def _quote_table(table, engine: str) -> str:
    parts = [p.strip() for p in str(table or "").split(".") if p.strip()]
    quoted = [_quote_ident(p, engine) for p in parts]
    return ".".join(quoted) if quoted and all(quoted) else ""


_ENGINE_ALIASES = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "mysql": "mysql",
    "mssql": "sqlserver",
    "sqlserver": "sqlserver",
}


def build_sample_sql(engine: str, table, columns, *,
                     limit: int = SAMPLE_ROWS_LIMIT) -> str | None:
    """按方言拼采样 SQL；任何标识符不合格 → `None`（调用方跳过该表）。"""
    eng = _ENGINE_ALIASES.get(str(engine or "").strip().lower(), "")
    if not eng:
        return None
    quoted = [_quote_ident(c, eng) for c in (columns or [])]
    quoted = [c for c in quoted if c]
    target = _quote_table(table, eng)
    if not target or not quoted:
        return None
    n = max(1, int(limit))
    cols = ", ".join(quoted)
    if eng == "sqlserver":
        return f"SELECT TOP {n} {cols} FROM {target}"
    return f"SELECT {cols} FROM {target} LIMIT {n}"


def normalize_rows(result: dict | None, *, columns: list, limit: int = SAMPLE_ROWS_LIMIT,
                   budget: int = SAMPLE_ROWS_BUDGET,
                   cell_chars: int = SAMPLE_CELL_CHARS) -> tuple[list[list], int]:
    """裁剪返回行：`(rows, used_chars)`。放不下的行**整行丢**，不留半行。"""
    max_cols = len(columns or [])
    out: list[list] = []
    used = 0
    for row in list((result or {}).get("rows") or [])[:max(1, int(limit))]:
        values = list(row) if isinstance(row, (list, tuple)) else [row]
        cells: list = []
        row_used = 0
        for value in values[:max_cols]:
            if value is None:
                text = None
                cost = 4                     # JSON 里的 `null`
            else:
                text = str(value).strip().replace("\n", " ")[:cell_chars]
                cost = len(text) + 2         # JSON 里一对引号
            if used + row_used + cost > max(0, int(budget)):
                return out, used
            cells.append(text)
            row_used += cost
        if cells:
            out.append(cells)
            used += row_used
    return out, used


__all__ = ["build_sample_sql", "normalize_rows", "MAX_SAMPLE_TABLES",
           "SAMPLE_ROWS_LIMIT", "SAMPLE_ROWS_BUDGET", "SAMPLE_CELL_CHARS"]
