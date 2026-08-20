"""
Utility functions for RAGAnything

Contains helper functions for content separation, text insertion, and other utilities
"""

import base64
import html.parser
import inspect
import os
import re
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from lightrag.utils import logger


def normalize_caption_list(value: Any) -> List[str]:
    """Return captions and footnotes as a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def get_table_body(item: Dict[str, Any]) -> Any:
    """Read table content across common content-list alias fields."""
    if item.get("table_body") not in (None, ""):
        return item.get("table_body")
    if item.get("table_data") not in (None, ""):
        return item.get("table_data")
    return item.get("text", "")


def format_table_body(table_body: Any) -> str:
    """Serialize table content for prompts and chunks without dropping aliases.

    Strings are passed through unchanged — except HTML table markup, which is
    auto-detected and converted to a compact Markdown table via
    :func:`normalize_table_rows`.

    List-of-lists (the common ``table_data`` shape from non-MinerU parsers) are
    rendered as a simple Markdown table so the LLM sees structured rows instead
    of a Python repr.  Other shapes fall back to a newline-joined string of
    ``str(...)`` items.
    """
    # [jonex] §table-chunking: detect HTML table → normalize to markdown
    # so that TableModalProcessor prompts get compact table text instead
    # of verbose HTML markup (also prevents prompt token explosion).
    if isinstance(table_body, str) and table_body.strip().startswith("<"):
        # [jonex] §table-grid-v2 L2/§13.5: 摘要链路与明细行必须基于同一份
        # 推断表头，否则「明细对、摘要错」（摘要 LLM 把说明文字当列名）。
        # 开关 false 时回退旧 normalize_table_rows 路径。
        if _table_grid_v2_enabled():
            header, data_rows, meta = normalize_table_grid(table_body)
            if data_rows:
                if header:
                    rendered = format_table_body([header] + data_rows)
                else:
                    rendered = format_table_body(data_rows)
                notes = meta.get("notes") or []
                if notes:
                    rendered += "\n\n【表前说明】\n" + "\n".join(notes)
                return rendered
            # Not a recognizable HTML table — pass through unchanged
            return table_body

        header, data_rows = normalize_table_rows(table_body)
        if data_rows:
            if header:
                return format_table_body([header] + data_rows)
            else:
                return format_table_body(data_rows)
        # Not a recognizable HTML table — pass through unchanged
        return table_body

    if isinstance(table_body, str):
        return table_body
    if isinstance(table_body, list):
        if not table_body:
            return ""
        if all(isinstance(row, (list, tuple)) for row in table_body):
            rendered_rows = [
                "| " + " | ".join(str(cell) for cell in row) + " |"
                for row in table_body
            ]
            if len(rendered_rows) >= 1:
                column_count = max(len(row) for row in table_body)
                separator = "| " + " | ".join(["---"] * column_count) + " |"
                rendered_rows.insert(1, separator)
            return "\n".join(rendered_rows)
        return "\n".join(str(row) for row in table_body)
    return str(table_body)


# ── [jonex] §table-chunking: HTML table row extraction + budget packing ──


class _HTMLTableRowExtractor(html.parser.HTMLParser):
    """Extract rows and cells from a minimal HTML ``<table>`` fragment.

    Only collects from the outermost ``<table>``; nested tables are skipped.
    The **first row** is always treated as the header (MinerU Online renders
    all cells as ``<td>`` even for header rows; well-formed ``<th>`` tables
    put the header row first, so first-row-wins covers both cases).

    ``colspan`` and ``rowspan`` are handled:

    - ``colspan``: the cell value is placed in its column and *colspan-1*
      empty strings are appended immediately after it to keep subsequent
      cells in the correct column positions.
    - ``rowspan``: the cell value is remembered and re-inserted at the
      same column position in the following *rowspan-1* rows.  Rowspan
      cells are always placed before the current row's own cells.
    """

    def __init__(self):
        super().__init__()
        self.header: List[str] = []
        self.rows: List[List[str]] = []
        self._current_row: List[str] = []
        self._current_cell: str = ""
        self._in_cell = False
        self._current_colspan: int = 1
        self._current_rowspan: int = 1
        self._table_depth = 0
        self._header_found = False
        # [(col_idx, rows_remaining, cell_value), ...]
        self._rowspan_pending: List[List] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        t = tag.lower()
        if t == "table":
            self._table_depth += 1
        if self._table_depth != 1:
            return

        if t == "tr":
            # ── Start of a new row: insert pending rowspan cells
            # before this row's own cells arrive ──
            self._current_row = []
            for pending in self._rowspan_pending:
                col_idx = pending[0]
                val = pending[2]
                while len(self._current_row) <= col_idx:
                    self._current_row.append("")
                self._current_row[col_idx] = val
                pending[1] -= 1  # consumed one more row
            # Remove expired entries
            self._rowspan_pending = [
                p for p in self._rowspan_pending if p[1] > 0
            ]

        if t in ("td", "th"):
            self._in_cell = True
            self._current_cell = ""
            self._current_colspan = 1
            self._current_rowspan = 1
            attrs_dict = dict(attrs)
            if "colspan" in attrs_dict:
                try:
                    self._current_colspan = int(attrs_dict["colspan"])
                except (ValueError, TypeError):
                    pass
            if "rowspan" in attrs_dict:
                try:
                    self._current_rowspan = int(attrs_dict["rowspan"])
                except (ValueError, TypeError):
                    pass

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "table":
            self._table_depth -= 1
        if self._table_depth != 1:
            return
        if t in ("td", "th"):
            self._in_cell = False
            cell_val = self._current_cell.strip()
            # colspan: append cell + (colspan-1) empty placeholders
            self._current_row.append(cell_val)
            for _ in range(self._current_colspan - 1):
                self._current_row.append("")
            # rowspan: remember for *subsequent* rows
            if self._current_rowspan > 1:
                col_idx = len(self._current_row) - self._current_colspan
                self._rowspan_pending.append(
                    [col_idx, self._current_rowspan - 1, cell_val]
                )
        if t == "tr":
            if self._current_row:
                if not self._header_found:
                    self.header = list(self._current_row)
                    self._header_found = True
                else:
                    self.rows.append(list(self._current_row))

    def handle_data(self, data: str) -> None:
        if self._table_depth == 1 and self._in_cell:
            self._current_cell += data


def _extract_markdown_rows(raw_str: str) -> List[List[str]]:
    """[jonex] §table-grid-v2: extract pipe-table rows from markdown text.

    Skips ``|---|---|`` separator lines.  Shared by ``normalize_table_rows``
    (legacy first-row-wins) and ``normalize_table_grid`` (L2 header
    inference).
    """
    rows: List[List[str]] = []
    for line in raw_str.split("\n"):
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.fullmatch(r"[\s\-:]+", c) for c in cells):
            continue  # separator row
        rows.append(cells)
    return rows


def normalize_table_rows(raw: Any) -> Tuple[List[str], List[List[str]]]:
    """Normalize table content from HTML / list-of-lists / markdown into
    uniform ``(header_row, data_rows)``.

    ``header_row`` may be empty (``[]``) if no header row is detected.
    Both return empty lists when the input cannot be recognized — callers
    should fall back to the raw string.

    Returns:
        ``(header_row, data_rows)`` where each is ``List[str]`` /
        ``List[List[str]]``.
    """
    if raw is None:
        return [], []

    # ── list[list[str]] ──
    if isinstance(raw, list) and raw and all(
        isinstance(r, (list, tuple)) for r in raw
    ):
        if len(raw) == 0:
            return [], []
        if len(raw) == 1:
            return [], [[str(c) for c in raw[0]]]
        return [str(c) for c in raw[0]], [
            [str(c) for c in r] for r in raw[1:]
        ]

    # ── string ──
    if not isinstance(raw, str) or not raw.strip():
        return [], []

    raw_str = raw.strip()

    # ── HTML ──
    if raw_str.startswith("<"):
        try:
            parser = _HTMLTableRowExtractor()
            parser.feed(raw_str)
            parser.close()
            if parser.rows or parser.header:
                return parser.header, parser.rows
        except Exception:
            pass  # Fall through to empty return

    # ── Markdown table ──
    rows = _extract_markdown_rows(raw_str)
    if rows:
        header = [str(c) for c in rows[0]]
        data_rows = [[str(c) for c in r] for r in rows[1:]]
        if header and data_rows:
            return header, data_rows
        if data_rows:
            return [], data_rows

    return [], []


# ── [jonex] §table-grid-v2 L1/L2: 网格填充 + 表头推断
#   (table-parsing-retrieval-governance-plan.md §3 L1/L2) ──


def _table_grid_v2_enabled() -> bool:
    """[jonex] §table-grid-v2: read the RAG_TABLE_GRID_V2 switch.

    Unset → new grid path (default true); explicit ``false`` → roll back to
    the legacy ``normalize_table_rows`` path (see plan §5 灰度表).
    """
    return os.getenv("RAG_TABLE_GRID_V2", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _detect_suspect_left_shift(
    raw_row_lengths: List[int], grid: List[List[str]],
) -> List[int]:
    """[jonex] §table-grid-v2 O4-bis: 检测「B 型错位」（左移）嫌疑行。

    MinerU 的 rowspan 标注不完整（只标部分列）时，自有格落到第一个
    未占用列——按 HTML 语义正确解析，但业务语义错位（如合约代码落进
    「品种类型」列）。它比旧实现更隐蔽：列数正确、无 col_N，看不出异常。

    指纹：原始 td 数 < n_cols（该行缺格）+ 左侧密集填充（前部连续
    非空 ≥2）+ 行内存在长度 ≥2 的**中间空洞**（空段右侧仍有非空值）。
    正常的「缺尾列」行（空段在最右端）不算。命中数透出到
    ``table_stats.suspect_left_shift``，纳入步 13 验收观察项。
    """
    n_cols = max(len(r) for r in grid) if grid else 0
    suspect: List[int] = []
    for r in range(min(len(raw_row_lengths), len(grid))):
        if raw_row_lengths[r] >= n_cols:
            continue
        row = grid[r]
        # 左侧密集：前部连续非空 ≥2
        prefix = 0
        for v in row:
            if v.strip():
                prefix += 1
            else:
                break
        if prefix < 2:
            continue
        # 中间空洞：长度 ≥2 的连续空段且其后还有非空
        blank_run = 0
        for v in row:
            if v.strip():
                if blank_run >= 2:
                    suspect.append(r)
                    break
                blank_run = 0
            else:
                blank_run += 1
    return suspect


class _HTMLTableGridExtractor(html.parser.HTMLParser):
    """[jonex] §table-grid-v2 L1: extract a strict ``n_cols``-wide grid.

    Two-pass grid filling:
      pass 1 (feed): collect raw cells per row as ``(value, colspan,
      rowspan)``;
      pass 2 (close): compute ``n_cols = max(Σcolspan per row)`` then fill a
      rectangular grid where ``colspan`` copies the value **rightwards** and
      ``rowspan`` copies it **downwards** — every covered cell gets the
      value (the legacy extractor put the value in the first cell only and
      padded with empty strings, which broke column alignment once a
      rowspan-occupied row appended its own cells).

    A cell in row *r* is placed at the **first column not yet occupied**
    (by an earlier cell of the same row or a rowspan from above).  This
    replaces append-only placement and keeps subsequent cells aligned with
    their visual columns.
    """

    def __init__(self):
        super().__init__()
        self.grid: List[List[str]] = []
        # 每行是否存在 colspan>1 / rowspan>1 的合并格——L2 用它区分
        # 「多级表头」（上一级有合并格，下一级可能是列名行）与
        # 「表头 + 数据」（满行无合并，下一行是数据）。
        self.has_merge: List[bool] = []
        # B 型错位（左移）嫌疑行索引（close 时计算，O4-bis）
        self.suspect_left_shift_rows: List[int] = []
        self._raw_rows: List[List[Tuple[str, int, int]]] = []
        self._current_row: List[Tuple[str, int, int]] = []
        self._current_cell: str = ""
        self._in_cell = False
        self._colspan = 1
        self._rowspan = 1
        self._row_has_merge = False
        self._table_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        t = tag.lower()
        if t == "table":
            self._table_depth += 1
        if self._table_depth != 1:
            return
        if t == "tr":
            self._current_row = []
        if t in ("td", "th"):
            self._in_cell = True
            self._current_cell = ""
            self._colspan = 1
            self._rowspan = 1
            for key, value in attrs:
                if key == "colspan":
                    try:
                        self._colspan = max(1, int(value))
                    except (ValueError, TypeError):
                        pass
                elif key == "rowspan":
                    try:
                        self._rowspan = max(1, int(value))
                    except (ValueError, TypeError):
                        pass
            if self._colspan > 1 or self._rowspan > 1:
                self._row_has_merge = True

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "table":
            self._table_depth -= 1
        if self._table_depth != 1:
            return
        if t in ("td", "th"):
            self._in_cell = False
            self._current_row.append(
                (self._current_cell.strip(), self._colspan, self._rowspan)
            )
        if t == "tr" and self._current_row:
            self._raw_rows.append(self._current_row)
            self.has_merge.append(self._row_has_merge)
            self._current_row = []
            self._row_has_merge = False

    def handle_data(self, data: str) -> None:
        if self._table_depth == 1 and self._in_cell:
            self._current_cell += data

    def close(self) -> None:
        super().close()
        self.grid = self._fill_grid()
        self.suspect_left_shift_rows = self._detect_suspect_rows(self.grid)

    def _fill_grid(self) -> List[List[str]]:
        n_rows = len(self._raw_rows)
        n_cols = max(
            (sum(cs for _, cs, _ in row) for row in self._raw_rows), default=0
        )
        grid: List[List[Optional[str]]] = [
            [None] * n_cols for _ in range(n_rows)
        ]
        for r, row in enumerate(self._raw_rows):
            for val, cs, rs in row:
                # 下一个未被占用的列（rowspan 自上而下、本行左至右）
                c = 0
                while c < n_cols and grid[r][c] is not None:
                    c += 1
                if c >= n_cols:
                    break
                for dr in range(rs):
                    for dc in range(cs):
                        rr, cc = r + dr, c + dc
                        if rr < n_rows and cc < n_cols:
                            grid[rr][cc] = val
        return [
            [cell if cell is not None else "" for cell in row]
            for row in grid
        ]

    def _detect_suspect_rows(self, grid: List[List[str]]) -> List[int]:
        """[jonex] §table-grid-v2 O4-bis: B 型错位（左移）嫌疑行检测。

        See :func:`_detect_suspect_left_shift`.
        """
        raw_lengths = [len(row) for row in self._raw_rows]
        return _detect_suspect_left_shift(raw_lengths, grid)


# ── L2 表头推断参数（TABLE_HEADER_MAX_LEN 可在 env 覆盖）──
_HEADER_CELL_MAX_LEN = 24            # 候选表头单元格短阈值
_HEADER_SEQ_MIN_LEN = 3              # 数字序列判定最小长度
_HEADER_DUP_RATIO = 0.5              # 行内非空值重复率上限（超过 → 数据行）
# 多级表头拼接时跳过的占位符（「—」等表示沿用上一级列名）
_HEADER_PLACEHOLDERS = {"—", "-", "–", "--", "/", "\\"}
_DATE_SEMANTIC_RE = re.compile(r"星期[一二三四五六日天]|周[一二三四五六日天]")
# 句末符：出现即判表前说明；列举符（顿号/逗号）须叠加超长才判，
# 避免误杀「交易品种、合约」这类短顿号表头。
_SENTENCE_END_PUNCT = "。；！？"
_LIST_PUNCT = "、，,"

# ── L3 兜底轨：Excel 日期序列号启发式 ──
_EXCEL_SERIAL_MIN = 1          # 1900-01-01
_EXCEL_SERIAL_MAX = 100000     # ~2173 年；窗口刻意放宽（§3 L3），守门靠日期信号
_WEEKDAY_CN = "一二三四五六日"


def _excel_serial_to_date(serial: int) -> str:
    """Excel 1900 日期系统（含 1900 闰年 bug，openpyxl 同口径）。"""
    from datetime import date as _date
    from datetime import timedelta

    d = _date(1899, 12, 30) + timedelta(days=serial)
    return f"{d:%Y-%m-%d}（周{_WEEKDAY_CN[d.weekday()]}）"


def _normalize_cell_value(value: str, date_semantic: bool) -> str:
    """[jonex] §table-grid-v2 L3 兜底轨：纯整数落在 ``[1, 100000]`` 且同表
    存在日期/星期语义信号时，转为 Excel 序列号日期。

    守门条件刻意只用「同表日期信号」而非数值区间——窗口放宽到
    ``[1, 100000]``（覆盖历史档案年份），避免误伤业务数字；无日期信号
    一律不转。
    """
    if not date_semantic or not value or not _is_pure_number(value):
        return value
    stripped = value.replace(",", "").replace("，", "").replace(" ", "").lstrip("+")
    try:
        n = int(float(stripped))
    except ValueError:
        return value
    if not (_EXCEL_SERIAL_MIN <= n <= _EXCEL_SERIAL_MAX):
        return value
    if str(n) != stripped:
        # 仅纯整数（无小数点/百分号）才可能是序列号
        return value
    return _excel_serial_to_date(n)


def _table_header_max_len() -> int:
    return int(os.getenv("TABLE_HEADER_MAX_LEN", "40"))


def _is_pure_number(value: str) -> bool:
    try:
        float(value.replace(",", "").replace("，", "").replace(" ", ""))
        return True
    except ValueError:
        return False


def _forms_numeric_sequence(nums: List[float]) -> bool:
    """Strictly monotonic, constant-step sequence (len ≥ 3).

    ``2023 | 2024 | 2025`` → True (legal year/period header);
    ``1.1 | 45 | 0.3`` → False (measure values → data row).
    """
    if len(nums) < _HEADER_SEQ_MIN_LEN:
        return False
    step = nums[1] - nums[0]
    if abs(step) < 1e-9:
        return False
    return all(
        abs((nums[i + 1] - nums[i]) - step) < 1e-9
        for i in range(1, len(nums) - 1)
    )


def _table_has_date_semantic(grid: List[List[str]]) -> bool:
    """Any cell carries a weekday/date semantic (星期/周X) — the guard for
    Excel-serial-number header cells (single numeric cell in a row)."""
    return any(
        _DATE_SEMANTIC_RE.search(cell)
        for row in grid
        for cell in row
        if cell
    )


def _is_prose_cell(cell: str) -> bool:
    """Cell reads like a pre-table note (说明) instead of a column name."""
    if len(cell) > _table_header_max_len():
        return True
    if any(p in cell for p in _SENTENCE_END_PUNCT):
        return True
    if any(p in cell for p in _LIST_PUNCT) and len(cell) > _HEADER_CELL_MAX_LEN:
        return True
    return False


def _is_header_row(
    row: List[str], date_semantic: bool, header_exists: bool = False,
    row_has_merge: bool = False,
) -> bool:
    """Header-zone feature test (L2): short cells, non-measure values,
    low in-row repetition."""
    values = [v.strip() for v in row if v and v.strip()]
    if not values:
        return False
    if any(len(v) > _HEADER_CELL_MAX_LEN for v in values):
        return False
    nums = [
        float(v.replace(",", "").replace("，", "").replace(" ", ""))
        for v in values
        if _is_pure_number(v)
    ]
    if nums:
        if len(nums) == 1:
            # 单格纯数字：仅当整表存在日期/星期语义信号时才视为合法表头
            # （Excel 序列号单列形态）；否则判度量值，避免误伤业务数字。
            if not date_semantic:
                return False
        elif not _forms_numeric_sequence(nums):
            return False
        elif header_exists:
            # 数字序列行只做首层表头——「2023|2024|2025 表头 + 逐年数字
            # 数据（100|120|130 也是序列）」是常见形态，第二层序列行判数据。
            return False
    # [jonex] H2 重复判据：区分「交替重复」与「连续聚块」。
    # L1 网格化的 rowspan/colspan forward-fill 会复制出**连续同值段**
    # （分组表头「审批授权情况×2」、分组列「期货×N」）——这是 L1 的
    # 正常产物，不是数据特征；而「是/否/是/否」**频繁切换**才是数据行
    # 特征。仅对无合并格信号的行生效（有合并格的行由合并信号担保）。
    if (
        not row_has_merge
        and len(values) >= 3
        and len(set(values)) <= 2
    ):
        runs = 1 + sum(
            1 for i in range(1, len(values))
            if values[i] != values[i - 1]
        )
        # 「交替」只认几乎每格都切换的形态（A,B,A,B → runs=len-1）。
        # 连续聚块（Q1,Q1,Q2,Q2 / 期货×N）是 L1 forward-fill 的产物或
        # 期间分组表头，放行——否则 C4 压测形态连单级表头都保不住。
        if runs >= len(values) - 1:
            return False  # 交替重复 → 数据行
        # 连续聚块 → 放行
    return True


def _row_is_full(row: List[str]) -> bool:
    return all(c and c.strip() for c in row)


def _compose_header(header_rows: List[List[str]], n_cols: int) -> List[str]:
    """[jonex] §table-grid-v2 L2: join multi-level header rows per column.

    ``审批授权情况 / 部门负责人`` → ``审批授权情况-部门负责人``; still-empty
    columns keep the ``col_N`` fallback.
    """
    header: List[str] = []
    for c in range(n_cols):
        parts: List[str] = []
        seen: set = set()
        for row in header_rows:
            v = row[c].strip() if c < len(row) else ""
            if v and v not in seen and v not in _HEADER_PLACEHOLDERS:
                parts.append(v)
                seen.add(v)
        header.append("-".join(parts) if parts else f"col_{c}")
    return header


def _infer_table_header(
    grid: List[List[str]],
    has_merge: Optional[List[bool]] = None,
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """[jonex] §table-grid-v2 L2: replace "first row wins" with header-zone
    inference.

    Scans from row 0 while rows satisfy header features.  Rows with long
    prose cells are removed into ``meta["notes"]`` and the scan continues
    (the real header may follow a merged note row).

    Multi-level headers require a structural signal: a second header row is
    only admitted when the previous header row has merged cells or empty
    cells (a group row spanning partial columns).  When the previous header
    row is full and unmerged, the next row is data — this keeps text-only
    data rows (e.g. 姓名/部门 tables) from being eaten into the header.

    Returns ``(header, data_rows, meta)`` with
    ``meta = {header_levels, notes, n_cols}``.
    """
    n_cols = len(grid[0]) if grid else 0
    has_merge = list(has_merge) if has_merge else [False] * len(grid)
    date_semantic = _table_has_date_semantic(grid)
    notes: List[str] = []
    header_rows: List[List[str]] = []
    last_header_idx = -1
    data_start = 0
    for r, row in enumerate(grid):
        if not any(c and c.strip() for c in row):
            # [jonex] H1：全空行跳过继续扫描（此前 break 会把「表前空行」之后
            # 的表头整区丢掉——表头完全丢失）。空行无语义，continue 安全；
            # 数据区中间的空行由 data_rows 过滤兜底。
            continue
        if any(_is_prose_cell(cell) for cell in row):
            prose = " ".join(c.strip() for c in row if c and c.strip())
            if prose:
                notes.append(prose)
            data_start = r + 1
            continue
        if header_rows:
            # 上一级满行且无合并格 → 本行是数据（防止文本数据行被吃进表头）
            if _row_is_full(header_rows[-1]) and not has_merge[last_header_idx]:
                break
            # 表头最多两层：多级表头现实中罕见超过 2 级，且无上限时「部分空
            # 的二级表头行 + 文本数据行」会级联误判（线上菜单表三级拼接回归）
            if len(header_rows) >= 2:
                break
        if _is_header_row(
            row, date_semantic, header_exists=bool(header_rows),
            row_has_merge=has_merge[r] if r < len(has_merge) else False,
        ):
            header_rows.append(row)
            last_header_idx = r
            data_start = r + 1
            continue
        break
    header = _compose_header(header_rows, n_cols)
    data_rows = [
        row for row in grid[data_start:] if any(c and c.strip() for c in row)
    ]
    meta: Dict[str, Any] = {
        "header_levels": len(header_rows),
        "notes": notes,
        "n_cols": n_cols,
    }
    return header, data_rows, meta


_HTML_TABLE_RE = re.compile(
    r"<table[\s>].*?</table>", re.IGNORECASE | re.DOTALL
)
_MD_PIPE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_MIN_LINES = 3  # 连续 ≥3 行管道行才判为表格（文档 O2 口径）


def extract_embedded_tables(text: str) -> List[Dict[str, Any]]:
    """[jonex] §table-grid-v2 O2: split a text block into alternating
    text / table segments.

    Detects HTML ``<table>…</table>`` fragments and runs of ≥3 consecutive
    markdown pipe lines inside a ``type == "text"`` block (md documents with
    embedded tables are never routed through the table branch otherwise, and
    their HTML/pipe markup gets hard-cut by LightRAG token chunking).

    Returns a list of ``{"kind": "text"|"table", "text": str}`` segments in
    original order.  A block without embedded tables returns a single
    ``{"kind": "text"}`` segment.  Table segments feed
    :func:`normalize_table_grid` via the table branch of
    ``_collect_text_chunks``; surrounding text segments become ordinary text
    chunks and serve as caption candidates for L4.1.
    """
    segments: List[Dict[str, Any]] = []

    # ── pass 1: HTML <table>…</table> fragments ──
    pos = 0
    for m in _HTML_TABLE_RE.finditer(text):
        if m.start() > pos:
            segments.append({"kind": "text", "text": text[pos:m.start()]})
        segments.append({"kind": "table", "text": m.group(0)})
        pos = m.end()
    if pos < len(text):
        segments.append({"kind": "text", "text": text[pos:]})
    if not segments:
        segments.append({"kind": "text", "text": text})

    # ── pass 2: markdown pipe-table runs inside text segments ──
    refined: List[Dict[str, Any]] = []
    for seg in segments:
        if seg["kind"] != "text":
            refined.append(seg)
            continue
        seg_text = seg["text"]
        lines = seg_text.split("\n")
        out_lines: List[str] = []
        i = 0
        n = len(lines)
        while i < n:
            if _MD_PIPE_LINE_RE.match(lines[i]):
                j = i
                while j < n and _MD_PIPE_LINE_RE.match(lines[j]):
                    j += 1
                if j - i >= _MD_TABLE_MIN_LINES:
                    if out_lines:
                        refined.append({
                            "kind": "text", "text": "\n".join(out_lines),
                        })
                        out_lines = []
                    refined.append({
                        "kind": "table", "text": "\n".join(lines[i:j]),
                    })
                    i = j
                    continue
            out_lines.append(lines[i])
            i += 1
        if out_lines:
            refined.append({"kind": "text", "text": "\n".join(out_lines)})

    # 纯空白 text 段（表格间的换行等）不产出，避免空 chunk
    refined = [
        s for s in refined
        if s["kind"] != "text" or s["text"].strip()
    ]
    return refined or [{"kind": "text", "text": text}]


def normalize_table_grid(
    raw: Any,
    has_merge: Optional[List[bool]] = None,
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """[jonex] §table-grid-v2 L1/L2: normalize tables into
    ``(header, data_rows, meta)`` with grid filling + header inference.

    L1: ``_HTMLTableGridExtractor`` fills a strict ``n_cols`` rectangular
    grid (colspan right-fill, rowspan down-fill, cross cells); list /
    markdown inputs are rectangularized the same way.

    L2: ``_infer_table_header`` replaces first-row-wins (see its docstring).

    *has_merge* 可显式传入「每行是否存在合并格」信号（list 输入无法从
    markup 推导；HTML 输入缺省时由 extractor 产出）。测试与未来调用方用。

    Empty result (``data_rows == []``) signals "not a recognizable table" —
    callers fall back to the raw string, same contract as
    :func:`normalize_table_rows`.
    """
    if raw is None:
        return [], [], {"header_levels": 0, "notes": [], "n_cols": 0}

    grid: List[List[str]] = []
    has_merge_derived: List[bool] = []
    suspect_rows: List[int] = []
    if isinstance(raw, list) and raw and all(
        isinstance(r, (list, tuple)) for r in raw
    ):
        grid = [[str(c) for c in r] for r in raw]
        has_merge_derived = [False] * len(grid)
        suspect_rows = _detect_suspect_left_shift(
            [len(r) for r in raw], grid,
        )
    elif isinstance(raw, str) and raw.strip():
        raw_str = raw.strip()
        if raw_str.startswith("<"):
            try:
                parser = _HTMLTableGridExtractor()
                parser.feed(raw_str)
                parser.close()
                grid = parser.grid
                has_merge_derived = parser.has_merge
                suspect_rows = parser.suspect_left_shift_rows
            except Exception:
                grid = []
                has_merge_derived = []
                suspect_rows = []
        else:
            grid = _extract_markdown_rows(raw_str)
            has_merge_derived = [False] * len(grid)
            suspect_rows = _detect_suspect_left_shift(
                [len(r) for r in grid], grid,
            )

    if has_merge is None:
        has_merge = has_merge_derived

    if not grid:
        return [], [], {"header_levels": 0, "notes": [], "n_cols": 0}

    # L1: 矩形化——缺格补空不左移
    n_cols = max(len(r) for r in grid)
    grid = [list(r) + [""] * (n_cols - len(r)) for r in grid]

    # L3 兜底轨: cell 规范化（Excel 序列号 → 日期）。守门条件 = 同表日期/
    # 星期语义信号；命中数计入 meta["dates_heuristic"]（透出到 table_stats）。
    date_semantic = _table_has_date_semantic(grid)
    dates_heuristic = 0
    if date_semantic:
        for r in range(len(grid)):
            for c in range(len(grid[r])):
                normalized = _normalize_cell_value(grid[r][c], True)
                if normalized != grid[r][c]:
                    grid[r][c] = normalized
                    dates_heuristic += 1

    header, data_rows, meta = _infer_table_header(grid, has_merge)
    meta["dates_heuristic"] = dates_heuristic
    # O4-bis: B 型错位（左移）嫌疑行数（col_ 归零后仍需观察该指标）
    meta["suspect_left_shift"] = len(suspect_rows)
    return header, data_rows, meta


_CAPTION_PREFIX = "表格明细：【表】"   # L4.2 明细行正文前缀 + 单行表标题
_CAPTION_MAX_HEAD_RATIO = 0.08        # 上下文头 ≤ chunk 总长 8%（L4.1 定量约束）
_CAPTION_TRUNCATE_KEEP = 30           # 超长标题截断保留前 N 字 + …
_CAPTION_BLACKLIST = {"none", "null", "n/a"}


def _valid_caption(raw: Any) -> Optional[str]:
    """[jonex] §table-grid-v2 L4.1: caption 有效性校验。

    判定无效并返回 ``None`` 的情形：``None`` / ``[]`` / ``""`` / 纯空白 /
    字面量 ``"None"``、``"none"``、``"null"``、``"N/A"``（大小写不敏感）/
    list 展平后为空 / 长度 > 60 字。否则返回展平 join 后的字符串。
    """
    flat = normalize_caption_list(raw)
    if not flat:
        return None
    cleaned = [
        s for s in flat
        if s.strip().lower() not in _CAPTION_BLACKLIST
    ]
    if not cleaned:
        return None
    joined = ", ".join(cleaned)
    if len(joined) > 60:
        return None
    return joined


def _detect_group_column(
    rows: List[List[str]], n_cols: int,
) -> Optional[int]:
    """[jonex] §table-grid-v2 O3: 分组键检测。

    rowspan forward-fill 的产物——某列在连续多行中同值（如「交易所」列）。
    选变化次数最少且 < 行数一半的列（只看前 8 列，分组键通常靠左）。
    """
    best_col: Optional[int] = None
    best_changes = len(rows)
    for c in range(min(n_cols, 8)):
        changes = 0
        prev: Optional[str] = None
        for row in rows:
            v = row[c].strip() if c < len(row) else ""
            if v != prev:
                changes += 1
                prev = v
        # 变化次数 ≤ 行数一半 → 存在连续同值段（rowspan forward-fill 产物）
        if changes < best_changes and changes <= len(rows) * 0.5:
            best_changes = changes
            best_col = c
    return best_col


def _group_value(row: List[str], group_col: Optional[int]) -> str:
    if group_col is None:
        return ""
    return row[group_col].strip() if group_col < len(row) else ""


def _render_caption_line(
    caption: str, budget: int, continuation: str = "",
) -> str:
    """L4.1: 单行上下文头「表格明细：【表】标题」，≤ 8% 预算，超长截断。

    组内续块（O3）追加「（续：分组值）」标注。

    [jonex] §C1-bis: *continuation* 承载两类语义、由调用点区分——
    ① O3 组内续块传**分组值**；② 行内切分（split_row_by_cells）传
    **已格式化的行号字符串**（如 ``f"第 {row_start + 1} 行"``，1-based）。
    本函数只做「传什么渲染什么」，不做任何格式化。
    """
    suffix = f"（续：{continuation}）" if continuation else ""
    max_head = max(16, int(budget * _CAPTION_MAX_HEAD_RATIO))
    cap_budget = max(1, max_head - len(_CAPTION_PREFIX) - len(suffix) - 1)
    cap = str(caption).strip()
    if len(cap) > cap_budget:
        cap = cap[:min(_CAPTION_TRUNCATE_KEEP, cap_budget)] + "…"
    if len(cap) > cap_budget:
        cap = cap[:max(1, cap_budget - 1)] + "…"
    return f"{_CAPTION_PREFIX}{cap}{suffix}\n"


# [jonex] §C1-bis: markdown 格式的列数阈值（pack_rows 与行内切分共用，
# 两处口径必须一致——行内切分需要知道该行是否走了 markdown 分支）。
DEFAULT_COLUMN_THRESHOLD = 6


def fmt_row(
    row: List[str],
    padded_header: List[str],
    n_cols: int,
    use_markdown: bool,
) -> str:
    """[jonex] §C1-bis: pack_rows 的行渲染逻辑提为模块级函数。

    行内切分（split_row_by_cells）需要复用同一渲染口径，才能用 `` | `` join
    出的单元格分隔符作为切分边界；两种格式都不产生换行（单元格值自带换行
    除外，见 table-parsing-retrieval-governance-plan.md §19.2）。
    """
    padded = list(row) + [""] * (n_cols - len(row))
    if use_markdown:
        return "| " + " | ".join(str(c) for c in padded) + " |"
    else:
        return " | ".join(
            f"{padded_header[i]}: {str(v)}" for i, v in enumerate(padded)
        )


def make_header_block(
    padded_header: List[str],
    n_cols: int,
    use_markdown: bool,
) -> str:
    """[jonex] §C1-bis: pack_rows 的表头块逻辑提为模块级函数。

    markdown 分支返回「列名行 + ``---`` 分隔行」，self-describing 分支返回
    空串（表头由每格的 ``列名:`` 前缀隐含）。
    """
    if use_markdown:
        sep = "| " + " | ".join(["---"] * n_cols) + " |"
        return (
            "| " + " | ".join(padded_header) + " |\n" + sep
        )
    else:
        # Self-describing format — header is implied by row labels
        return ""


# [jonex] §block-packing 文本块级打包（docs/text-block-packing-chunk-governance-plan.md §3.2/§4 改动 1）
# 判据与表格链路 caption L4.1 复用同一套「句末符」常量——utils.py 不能
# import stages.py（循环依赖），故在此定义同值常量，与
# stages.py:1131 的 _CAPTION_SENTENCE_END 保持同源同步。
_HEADING_SENTENCE_END = "。！？；."   # 句末符——正文结尾而非标题

_HEADING_PATTERNS = (
    r"^第[一二三四五六七八九十百零〇\d]+[章节条款部分篇]",   # 第一节 / 第3章
    r"^[\d]+(\.[\d]+)*[\s、.]",                      # 1. / 1.2.3 / 3、
    r"^[（(][一二三四五六七八九十\d]+[)）]",              # （一） / (3)
    r"^(图|表|附录|附表|附图)\s*[\d一二三四五六七八九十]+",  # 图1 / 表 3 / 附录A
    r"^[A-Z][.、]\s",                                   # A. / B、
)

_NOISE_NUM_RE = re.compile(r"^[\d]{1,4}$")
_NOISE_ROMAN_RE = re.compile(r"^[ivxlcdmIVXLCDM]{1,4}$")
_NOISE_SYMBOL_RE = re.compile(r"^[\W_]{1,4}$")


def is_noise_block(
    text: str,
    repeat_index: Optional[Dict[str, int]] = None,
) -> bool:
    """§block-packing C2/C3: 纯数字/纯符号页码、高重复页眉页脚判定。

    C2：strip 后长度 ≤ 4 且通篇只有数字 / 罗马数字 / 符号——这类块不可能
    承载语义；C3：长度 ≤ 20 且全文出现 ≥ 3 次（页眉页脚形态），阈值取 3
    是为了避开「章节名恰好重复一次」的正常情况（见 §3.2）。

    Args:
        repeat_index: 全文档短块（≤ 20 字）出现次数预扫结果（key 为 strip
            后文本）。为 None 时跳过 C3，仅做 C2 判定。
    """
    t = text.strip()
    if not t:
        return True
    if len(t) <= 4 and (
        _NOISE_NUM_RE.match(t)
        or _NOISE_ROMAN_RE.match(t)
        or _NOISE_SYMBOL_RE.match(t)
    ):
        return True
    if repeat_index is not None and len(t) <= 20:
        return repeat_index.get(t, 0) >= 3
    return False


def is_heading_like(text: str, max_len: int = 40) -> bool:
    """§block-packing C4/C5: 短 + 无句末符 + 无换行 → 视作标题。

    C4 编号模式命中仍受长度闸门约束：仅当 strip 后长度 ≤
    ``max(max_len * 2, 80)`` 才判 heading。编号样式的长正文段（如
    「1. 政策出台背景是……」数百字）绝不能判标题——否则包头渲染截断
    会真丢正文（review P0-1）。C5 三条同时满足才判定：strip 后长度 ≤
    *max_len*、末字符不在句末符集合、无换行。误判后果仅是「该句从正文
    位置挪到包头首行」，零信息损失。
    """
    t = text.strip()
    if not t:
        return False
    if any(re.match(p, t) for p in _HEADING_PATTERNS):
        return len(t) <= max(max_len * 2, 80)
    if len(t) > max_len:
        return False
    if "\n" in t:
        return False
    return t[-1] not in _HEADING_SENTENCE_END


def _render_pack_head(heads: List[str], budget: int) -> Tuple[str, str]:
    """渲染包头 ``【A / B】\n``，返回 ``(head, overflow)``。

    总长超过预算 8% 时截断显示，被截掉的完整包头作为 *overflow* 回传、
    由调用方转为正文首行（review P0-1：截断不得丢正文）。与 pack_rows 的
    caption 行同约定（≤ 8% 预算），见 §3.2。
    """
    if not heads:
        return "", ""
    max_head = max(16, int(budget * 0.08))
    full = "【" + " / ".join(heads) + "】"
    if len(full) <= max_head:
        return full + "\n", ""
    return full[: max(1, max_head - 1)] + "…\n", full


def pack_text_blocks(
    blocks: List[Dict[str, Any]],
    budget: int,
    *,
    heading_max_len: int = 40,
    heading_levels: int = 2,
    drop_noise: bool = True,
    repeat_index: Optional[Dict[str, int]] = None,
    carry: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """§block-packing: 相邻文本块打包（pack_rows 的文本链路同构实现）。

    单遍顺序扫描 + 一个累积缓冲（§3.2.1 状态机）。输入是 stages 侧分派后
    的纯文本块序列（硬边界 table/image/equation 已由调用方在进本函数前
    冲刷），块内允许缺省字符范围锚点（O2 内嵌表格分支，见 §5.2）。

    标题页计入包跨度：新收标题的首包，包头渲染在包内 offset 0，标题页
    ≠ 正文页时写出 ``pspans``（首 entry 为 ``0@{标题页}``）；续包（标题
    跨预算/跨硬边界继承）page_start 取首个正文块页，``pspans`` 首 entry
    为 ``0@{首正文块页}``（review P0-2：不得把续包页码拉回标题页）。

    Args:
        blocks: [{"text","page_idx","char_start","char_end","line_start",
                  "line_end"}]，page_idx 之外的字段可缺省。
        budget: 字符预算（含包头；stages 侧按 RAG_TEXT_PACK_CHARS 传入）。
        heading_max_len: C5 判定长度上限（env RAG_TEXT_PACK_HEADING_MAX_LEN）。
        heading_levels: 包头保留的最近标题级数（默认 2）。
        drop_noise: False 时噪声块不丢弃、按正文吸附（零信息损失模式，
            env RAG_TEXT_PACK_DROP_NOISE，保守上线可先用该模式）。
        repeat_index: C3 用全文档短块出现次数预扫（stages 侧打包前一次
            O(n) 预扫，见 §8）。
        carry: 跨调用包头状态（review P1：标题须跨 table/image 硬边界
            继承）。传入 dict 时以其 ``heads`` / ``head_pages`` /
            ``heads_fresh`` 键为初值，结束时原地更新回传；None 则每次
            调用从头初始化（纯函数模式，既有测试用）。

    Returns:
        ``(packs, dropped)``。
        packs: [{"text","page_start","page_end","pspans","char_start",
                 "char_end","line_start","line_end","block_count","heading"}]。
        ``pspans`` 仅跨页包非 None（格式 ``offset@page;…``），``page_end``
        仅当 ≠ page_start 时非 None；字符范围锚点全缺省时对应字段为 None。
        dropped: [(text, reason), ...]，供采样日志与 blocks_dropped_noise
        计数透出（reason 目前恒为 "noise"）。
    """
    packs: List[Dict[str, Any]] = []
    dropped: List[Tuple[str, str]] = []

    if carry is not None:
        cur_heads = list(carry.get("heads") or [])
        head_pages = list(carry.get("head_pages") or [])
        heads_fresh = bool(carry.get("heads_fresh", True))
    else:
        cur_heads: List[str] = []
        head_pages: List[Optional[int]] = []
        heads_fresh = True
    cur_lines: List[str] = []
    body_chars = 0                      # 正文累计字符（含换行）
    cur_pspans: List[Tuple[int, Optional[int]]] = []   # (正文起点 offset, page)
    page_start: Optional[int] = None    # 包首块页号（新收标题包=标题页）
    cur_page: Optional[int] = None      # 已收最后一块的页号
    block_count = 0
    first_char_start = first_line_start = None
    last_char_end = last_line_end = None

    def head_chars() -> int:
        return len(_render_pack_head(cur_heads, budget)[0]) if cur_heads else 0

    def flush() -> None:
        nonlocal cur_lines, body_chars, cur_pspans, page_start, cur_page
        nonlocal block_count, first_char_start, first_line_start
        nonlocal last_char_end, last_line_end, heads_fresh
        if not cur_lines:
            # 空包无输出；cur_heads 跨包保留（长小节拆多包时每个包
            # 都带同一【章 / 节】包头，见 §3.2.1）。
            return
        head, head_overflow = _render_pack_head(cur_heads, budget)
        lines = list(cur_lines)
        if head_overflow:
            # 包头截断的溢出部分转为正文首行（review P0-1，零信息损失）
            lines.insert(0, head_overflow)
        text = head + "\n".join(lines)
        entries = [(0, page_start)] + cur_pspans
        pspans_str = None
        if len(entries) > 1 and entries[0][1] != entries[-1][1]:
            pspans_str = ";".join(f"{off}@{pg}" for off, pg in entries)
        packs.append({
            "text": text,
            "page_start": page_start,
            "page_end": cur_page if cur_page != page_start else None,
            "pspans": pspans_str,
            "char_start": first_char_start,
            "char_end": last_char_end,
            "line_start": first_line_start,
            "line_end": last_line_end,
            "block_count": block_count,
            "heading": head.rstrip("\n") or None,
        })
        # 本包已渲染过包头：此后 cur_heads 为「继承」态，续包 page_start
        # 取首个正文块页（review P0-2）。
        heads_fresh = False
        cur_lines = []
        body_chars = 0
        cur_pspans = []
        page_start = None
        cur_page = None
        block_count = 0
        first_char_start = first_line_start = None
        last_char_end = last_line_end = None

    def append_body(block: Dict[str, Any]) -> None:
        nonlocal body_chars, cur_pspans, page_start, cur_page
        nonlocal block_count, first_char_start, first_line_start
        nonlocal last_char_end, last_line_end
        text = block.get("text") or ""
        if not text:
            return
        page = block.get("page_idx")
        if page_start is None:
            # 新收标题（heads_fresh）→ 包首页取标题页；继承标题（续包）
            # → 取首个正文块页，避免页码被拉回标题页（review P0-2）。
            page_start = (
                head_pages[0]
                if (head_pages and heads_fresh)
                else page
            )
        # 页边界记录：与「上一个有效页」比较。首正文块时 cur_page 尚为
        # None，比较对象退化为 page_start（标题页 ≠ 正文页同样记一条，
        # 走查 chunk#0 的 22@2 即由此产生）。
        prev_page = cur_page if cur_page is not None else page_start
        if page is not None and prev_page is not None and page != prev_page:
            cur_pspans.append((head_chars() + body_chars, page))
        cur_lines.append(text)
        body_chars += len(text) + 1
        cur_page = page
        block_count += 1
        if first_char_start is None and block.get("char_start") is not None:
            first_char_start = block["char_start"]
        if first_line_start is None and block.get("line_start") is not None:
            first_line_start = block["line_start"]
        if block.get("char_end") is not None:
            last_char_end = block["char_end"]
        if block.get("line_end") is not None:
            last_line_end = block["line_end"]

    for block in blocks:
        text = block.get("text") or ""
        if not text:
            continue
        if is_noise_block(text, repeat_index):
            if drop_noise:
                dropped.append((text, "noise"))
                continue
            # DROP_NOISE=false：噪声按正文吸附（零信息损失模式）。
            # 注意 elif 结构：吸附路径必须跳过 heading 判定，否则
            # 页码「1」会被 C5 误吸进包头。
        elif is_heading_like(text, heading_max_len):
            flush()                      # 标题归属「下一包」而不是当前包
            cur_heads = (cur_heads + [text])[-heading_levels:]
            head_pages = (head_pages + [block.get("page_idx")])[-heading_levels:]
            heads_fresh = True           # 新收标题，下一包以标题页计页
            continue
        if body_chars + len(text) + 1 + head_chars() > budget and cur_lines:
            flush()                      # 预算满，先冲刷再装
        append_body(block)

    flush()                              # 收尾

    if carry is not None:
        # 跨硬边界继承的包头状态原地回传（review P1）
        carry["heads"] = cur_heads
        carry["head_pages"] = head_pages
        carry["heads_fresh"] = heads_fresh

    return packs, dropped


def pack_rows(
    rows: List[List[str]],
    budget: int,
    header: Optional[List[str]] = None,
    column_threshold: int = DEFAULT_COLUMN_THRESHOLD,
    caption: Optional[str] = None,
) -> List[Tuple[str, int, int]]:
    """Split table rows into budget-sized chunks with self-describing format.

    Each segment repeats the header and encodes each row as::

        col_name: value | col_name: value | ...

    For tables with few columns (≤ *column_threshold*), uses compact
    Markdown table format instead for better human readability.

    [jonex] §table-grid-v2 L4.1/O3:
      - *caption* adds a one-line context header ``表格明细：【表】标题`` to
        every segment (≤ 8% of the budget, truncated when over-long) — the
        subject-disambiguation header, and the only repeated metadata in the
        body (column list / signature / notes go to ``file_source``);
      - group-aware splitting: when a column keeps the same value across
        consecutive rows (rowspan forward-fill output), segments break at
        group boundaries first; continuation segments inside an over-budget
        group get a ``（续：分组值）`` suffix.

    Returns:
        ``[(segment_text, row_start_idx, row_end_idx), ...]``.
        Indices are 0-based within *rows*, exclusive end.
    """
    if not rows:
        return []

    header = header or []
    n_cols = max(len(r) for r in rows) if rows else 0
    if header:
        n_cols = max(n_cols, len(header))

    # Pad header to n_cols with generic column names
    padded_header = list(header) + [
        f"col_{i}" for i in range(len(header), n_cols)
    ]

    use_markdown = n_cols <= column_threshold

    hl = make_header_block(padded_header, n_cols, use_markdown)

    group_col = _detect_group_column(rows, n_cols)

    segments: List[Tuple[str, int, int]] = []
    i = 0
    n = len(rows)
    first_segment = True
    prev_flushed_group: Optional[str] = None

    while i < n:
        group_val = _group_value(rows[i], group_col)
        continuation = ""
        if (
            caption and group_col is not None and group_val
            and not first_segment and group_val == prev_flushed_group
        ):
            continuation = group_val  # 组内续块（O3）
        cap_text = _render_caption_line(caption, budget, continuation) if caption else ""

        seg_lines: List[str] = []
        seg_len = len(cap_text) + len(hl) + (1 if hl else 0)
        seg_start = i

        while i < n:
            row_str = fmt_row(rows[i], padded_header, n_cols, use_markdown)
            row_len = len(row_str) + 1  # +1 for newline
            g = _group_value(rows[i], group_col)
            if seg_lines and g and group_val and g != group_val:
                break  # O3: 分组边界优先断开（空分组值不参与）
            if seg_len + row_len > budget and seg_lines:
                break
            seg_lines.append(row_str)
            seg_len += row_len
            i += 1

        body = "\n".join(seg_lines)
        seg_text = cap_text + (hl + "\n" + body if hl else body)
        segments.append((seg_text, seg_start, i))
        prev_flushed_group = group_val
        first_segment = False

    return segments


def split_row_by_cells(
    row_text: str,
    budget: int,
    *,
    header_block: str = "",
    caption_line: str = "",
) -> List[Tuple[str, int, int]]:
    """[jonex] §C1-bis: 单行超预算时按单元格边界切分（table-parsing-retrieval-governance-plan.md §19.3）。

    切点取「不超过 budget 的最后一个 `` | ``」，而非精确 budget 处硬切——
    `` | `` 是 fmt_row 两种格式（markdown / self-describing）共用的单元格
    分隔符，也是行内唯一的语义边界。header_block / caption_line 计入每段
    预算，并统一作为前缀附在每个子段上（§19.4：列名有条件补、表标题
    无条件补）。

    仅当单个单元格自身超预算时才退化为字符硬切，并打 WARNING（cells_hard_cut
    计数由调用方通过「非尾段段末不含 `` | ``」特征完成，见 §19.3）。

    Returns:
        ``[(seg_text, cell_start, cell_end), ...]``。
        单元格序号 0-based 右开（与 pack_rows 的 row 区间口径一致，§19.5 的
        file_source 锚点直接使用，不做 1-based 转换）。seg_text 已含
        caption_line + header_block 前缀。
    """
    if not row_text:
        return []

    prefix = caption_line
    if header_block:
        prefix = prefix + header_block + "\n"
    body_budget = max(1, budget - len(prefix))

    n = len(row_text)
    if n <= body_budget:
        return [(prefix + row_text, 0, 1)]

    # 所有 " | " 分隔符的起始位置；cell 总数 = 分隔符数 + 1
    seps: List[int] = []
    hit = row_text.find(" | ")
    while hit != -1:
        seps.append(hit)
        hit = row_text.find(" | ", hit + 1)
    n_cells = len(seps) + 1

    segments: List[Tuple[str, int, int]] = []
    pos = 0
    cell_start = 0
    while pos < n:
        end = pos + body_budget
        if end >= n:
            segments.append((prefix + row_text[pos:], cell_start, n_cells))
            break
        # 找最后一个完整落在 [pos, end] 内的分隔符；切点 = 其后下一格的起点。
        # 切在 seps[k]+3 保证前段以 " | " 结尾、后段从新格开始。
        cut_k = -1
        for k, s in enumerate(seps):
            if s < pos:
                continue
            if s + 3 <= end:
                cut_k = k
            else:
                break
        if cut_k >= 0:
            cut = seps[cut_k] + 3
            segments.append((prefix + row_text[pos:cut], cell_start, cut_k + 1))
            pos = cut
            cell_start = cut_k + 1
            continue
        # 无完整分隔符落在窗口内：某个单元格自身超预算 → 字符硬切（可观测退化）。
        # 前段覆盖「起点落在窗口内」的所有格：m = 第一个不完整分隔符的全局序号，
        # 格 m 的结束边界在窗口外、部分内容在前段，故区间为 [cell_start, m+1)。
        # 下一段从格 m 继续（它还没结束）——硬切使该格出现在两段区间中，
        # 重叠是硬切的本质，正常按边界切时不发生。
        m = len(seps)
        for k, s in enumerate(seps):
            if s >= pos and s + 3 > end:
                m = k
                break
        logger.warning(
            "[jonex] §C1-bis: single cell exceeds body budget "
            "(cells=%d:%d, row_len=%d > budget=%d), "
            "falling back to hard char cut",
            cell_start, cell_start + m + 1, n, body_budget,
        )
        segments.append(
            (prefix + row_text[pos:end], cell_start, cell_start + m + 1)
        )
        pos = end
        cell_start = cell_start + m

    return segments


def get_equation_text_and_format(item: Dict[str, Any]) -> Tuple[str, str]:
    """Read equation content while preserving LaTeX aliases from content lists.

    Field priority follows MinerU first (``text`` + ``text_format``), then
    falls back to ``latex`` and ``equation`` aliases used by other parsers.
    The textual description is intentionally NOT concatenated into the
    equation body: the ``equation_chunk`` template has a separate
    ``enhanced_caption`` slot for that.
    """
    text = str(item.get("text", "") or "").strip()
    latex = str(item.get("latex", "") or "").strip()
    equation = str(item.get("equation", "") or "").strip()
    equation_format = str(item.get("text_format", "") or "").strip()

    if text:
        equation_text = text
    elif latex:
        equation_text = latex
        if not equation_format:
            equation_format = "latex"
    elif equation:
        equation_text = equation
    else:
        equation_text = ""

    return equation_text, equation_format


# ── [jonex] P1-4 结构感知切分已废弃（O5，2026-08-13）──
#
# structure_aware_chunk / _enrich_table_row_text / _classify_block_type /
# _LIST_ENTRY_PATTERNS / _STRUCTURE_AWARE_ENABLED 及 separate_content 中的
# 调用点已删除：该路径不在推送链路上（只被 separate_content 调用，而推送走
# _collect_text_chunks），是死代码；且只认 markdown `#` 标题，认不出 docx/xlsx
# 的普通文本标题。表标题回填能力由 §table-grid-v2 L4.1 的
# `_resolve_table_caption` + pack_rows caption 接管（来源不限 markdown heading）。
# 详见 docs/table-parsing-retrieval-governance-plan.md §11.4 O5 与 JONEX_CHANGES §21。


def separate_content(
    content_list: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Separate text content and multimodal content

    For each text block, computes positional metadata:
      - page_idx:   page number from parser (0-based)
      - text_idx:   position index in content_list
      - line_start: starting line in the concatenated full text (1-based)
      - line_end:   ending line in the concatenated full text (1-based)

    Args:
        content_list: Content list from MinerU parsing

    Returns:
        (text_content, multimodal_items): Pure text content and multimodal items list.
        The last element of multimodal_items is a sentinel ``{"_text_meta": [...]}``
        carrying per-block metadata for downstream use.
    """
    text_parts = []
    text_meta_list: List[Dict[str, Any]] = []
    multimodal_items = []
    line_cursor = 1  # 1-based document-level line counter

    for index, item in enumerate(content_list):
        content_type = item.get("type", "text")

        if content_type == "text":
            # Text content — preserve positional metadata
            text = item.get("text", "")
            if text.strip():
                lines_in_block = text.count("\n") + 1
                text_parts.append(text)
                # Record per-block metadata for downstream chunk tracking
                text_meta_list.append({
                    "content": text,
                    "page_idx": item.get("page_idx", 0),
                    "text_idx": index,  # original position in content_list
                    "line_start": line_cursor,
                    "line_end": line_cursor + lines_in_block - 1,
                })
                # Advance cursor: this block's lines + 1 for the "\n\n" join
                # separator between blocks (adds one empty line)
                line_cursor += lines_in_block + 1
        else:
            # Multimodal content (image, table, equation, etc.)
            multimodal_item = dict(item)
            multimodal_item.setdefault("_content_list_index", index)
            multimodal_items.append(multimodal_item)

    # Merge all text content for backward-compatible full-text reference
    text_content = "\n\n".join(text_parts)

    # Attach metadata list to the return value for callers that need it.
    # Stored as an attribute on the string is not Pythonic, so we return
    # the metadata list separately via a sentinel key in multimodal_items.
    # Backward-compatible callers only use text_content and multimodal_items.
    multimodal_items.append({"_text_meta": text_meta_list})

    logger.info("Content separation complete:")
    logger.info(f"  - Text content length: {len(text_content)} characters")
    logger.info(f"  - Text blocks: {len(text_meta_list)}")
    logger.info(f"  - Multimodal items count: {len(multimodal_items) - 1}")

    # Count multimodal types
    modal_types = {}
    for item in multimodal_items:
        modal_type = item.get("type", "unknown")
        modal_types[modal_type] = modal_types.get(modal_type, 0) + 1

    if modal_types:
        logger.info(f"  - Multimodal type distribution: {modal_types}")

    return text_content, multimodal_items


def extract_text_metadata(multimodal_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract text block metadata hidden in multimodal_items by separate_content.

    Returns:
        List of dicts with keys: content, page_idx, text_idx
    """
    for item in multimodal_items:
        if "_text_meta" in item:
            return item["_text_meta"]
    return []


def encode_image_to_base64(image_path: str) -> str:
    """
    Encode image file to base64 string

    Args:
        image_path: Path to the image file

    Returns:
        str: Base64 encoded string, empty string if encoding fails
    """
    try:
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
        return encoded_string
    except Exception as e:
        logger.error(f"Failed to encode image {image_path}: {e}")
        return ""


def validate_image_file(image_path: str, max_size_mb: int = 50) -> bool:
    """
    Validate if a file is a valid image file

    Args:
        image_path: Path to the image file
        max_size_mb: Maximum file size in MB

    Returns:
        bool: True if valid, False otherwise
    """
    try:
        path = Path(image_path)

        logger.debug(f"Validating image path: {image_path}")
        logger.debug(f"Resolved path object: {path}")
        logger.debug(f"Path exists check: {path.exists()}")

        # Check if file exists and is not a symlink (for security)
        if not path.exists():
            logger.warning(f"Image file not found: {image_path}")
            return False

        if path.is_symlink():
            logger.warning(f"Blocking symlink for security: {image_path}")
            return False

        # Check file extension
        image_extensions = [
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".bmp",
            ".webp",
            ".tiff",
            ".tif",
        ]

        path_lower = str(path).lower()
        has_valid_extension = any(path_lower.endswith(ext) for ext in image_extensions)
        logger.debug(
            f"File extension check - path: {path_lower}, valid: {has_valid_extension}"
        )

        if not has_valid_extension:
            logger.warning(f"File does not appear to be an image: {image_path}")
            return False

        # Check file size
        file_size = path.stat().st_size
        max_size = max_size_mb * 1024 * 1024
        logger.debug(
            f"File size check - size: {file_size} bytes, max: {max_size} bytes"
        )

        if file_size > max_size:
            logger.warning(f"Image file too large ({file_size} bytes): {image_path}")
            return False

        logger.debug(f"Image validation successful: {image_path}")
        return True

    except Exception as e:
        logger.error(f"Error validating image file {image_path}: {e}")
        return False


async def insert_text_content(
    lightrag,
    input: str | list[str] | list[dict],
    file_paths: str | list[str] | None = None,
    split_by_character: str | None = None,
    split_by_character_only: bool = False,
    ids: str | list[str] | None = None,
):
    """
    Insert text content into LightRAG with optional per-chunk metadata.

    When *input* is a ``list[dict]``, each dict must have a ``"content"`` key.
    Extra keys (``page_idx``, ``text_idx``, etc.) are forwarded as chunk metadata
    via ``lightrag.ainsert_custom_chunks``, bypassing LightRAG's internal
    token-based splitting.  This preserves positional information through the
    ingestion pipeline.

    When *input* is a plain ``str`` or ``list[str]``, behaviour is identical to
    the previous implementation (delegates to ``lightrag.ainsert``).

    Args:
        lightrag: LightRAG instance.
        input: Text to insert — plain string(s) or pre-chunked metadata dicts.
        file_paths: File path(s) for reference citation.
        split_by_character: Character to split on (plain-text mode only).
        split_by_character_only: Split by character only (plain-text mode only).
        ids: Document ID(s).
    """
    logger.info("Starting text content insertion into LightRAG...")

    # ── Metadata-aware path: list[dict] → ainsert_custom_chunks ─────
    if isinstance(input, list) and len(input) > 0 and isinstance(input[0], dict):
        # Build full text from all chunks for document storage
        full_text = "\n\n".join(
            item.get("content", "") if isinstance(item, dict) else str(item)
            for item in input
        )
        # Normalize file_paths to a single string for the doc entry
        fp = file_paths
        if isinstance(fp, list):
            fp = fp[0] if fp else ""
        fp = fp or ""

        doc_id = None
        if ids:
            doc_id = ids if isinstance(ids, str) else ids[0] if ids else None

        await lightrag.ainsert_custom_chunks(
            full_text=full_text,
            text_chunks=input,
            doc_id=doc_id,
            file_path=fp,
        )
        logger.info(
            f"Text content insertion complete "
            f"({len(input)} chunks with metadata)"
        )
        return

    # ── Plain-text path: str | list[str] → ainsert (backward compat) ─
    await lightrag.ainsert(
        input=input,
        file_paths=file_paths,
        split_by_character=split_by_character,
        split_by_character_only=split_by_character_only,
        ids=ids,
    )

    logger.info("Text content insertion complete")


async def insert_text_content_with_multimodal_content(
    lightrag,
    input: str | list[str],
    multimodal_content: list[dict[str, any]] | None = None,
    split_by_character: str | None = None,
    split_by_character_only: bool = False,
    ids: str | list[str] | None = None,
    file_paths: str | list[str] | None = None,
    scheme_name: str | None = None,
):
    """
    Insert pure text content into LightRAG

    Args:
        lightrag: LightRAG instance
        input: Single document string or list of document strings
        multimodal_content: Multimodal content list (optional)
        split_by_character: if split_by_character is not None, split the string by character, if chunk longer than
        chunk_token_size, it will be split again by token size.
        split_by_character_only: if split_by_character_only is True, split the string by character only, when
        split_by_character is None, this parameter is ignored.
        ids: single string of the document ID or list of unique document IDs, if not provided, MD5 hash IDs will be generated
        file_paths: single string of the file path or list of file paths, used for citation
        scheme_name: scheme name (optional)
    """
    logger.info("Starting text content insertion into LightRAG...")

    insert_kwargs = {
        "input": input,
        "file_paths": file_paths,
        "split_by_character": split_by_character,
        "split_by_character_only": split_by_character_only,
        "ids": ids,
    }

    try:
        insert_signature = inspect.signature(lightrag.ainsert)
        supported_params = insert_signature.parameters
        accepts_any_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in supported_params.values()
        )
    except (TypeError, ValueError):
        supported_params = {}
        accepts_any_kwargs = True

    if multimodal_content is not None and (
        accepts_any_kwargs or "multimodal_content" in supported_params
    ):
        insert_kwargs["multimodal_content"] = multimodal_content
    elif multimodal_content is not None:
        logger.warning(
            "LightRAG ainsert() does not accept multimodal_content; "
            "retrying with text-only insertion so doc_status is still created"
        )

    if scheme_name is not None and (
        accepts_any_kwargs or "scheme_name" in supported_params
    ):
        insert_kwargs["scheme_name"] = scheme_name
    elif scheme_name is not None:
        logger.warning(
            "LightRAG ainsert() does not accept scheme_name; "
            "continuing without it for compatibility"
        )

    await lightrag.ainsert(**insert_kwargs)

    logger.info("Text content insertion complete")


_PROCESSOR_KEY_MAP: Dict[str, str] = {
    "image": "image",
    "table": "table",
    "equation": "equation",
    "audio": "audio",
    "video": "video",
}


def get_processor_for_type(modal_processors: Dict[str, Any], content_type: str):
    """
    Get appropriate processor based on content type using registry.

    Args:
        modal_processors: Dictionary of available processors
        content_type: Content type

    Returns:
        Corresponding processor instance
    """
    key = _PROCESSOR_KEY_MAP.get(content_type)
    if key:
        proc = modal_processors.get(key)
        if proc is not None:
            return proc
    return modal_processors.get("generic")


def get_processor_supports(proc_type: str) -> List[str]:
    """Get processor supported features"""
    supports_map = {
        "image": [
            "Image content analysis",
            "Visual understanding",
            "Image description generation",
            "Image entity extraction",
        ],
        "table": [
            "Table structure analysis",
            "Data statistics",
            "Trend identification",
            "Table entity extraction",
        ],
        "equation": [
            "Mathematical formula parsing",
            "Variable identification",
            "Formula meaning explanation",
            "Formula entity extraction",
        ],
        "audio": [
            "Audio transcription via ASR",
            "Transcript segment chunking",
            "Audio content summarization",
            "Audio entity extraction with time metadata",
        ],
        "generic": [
            "General content analysis",
            "Structured processing",
            "Entity extraction",
        ],
    }
    return supports_map.get(proc_type, ["Basic processing"])


# ── Parser kwarg filtering ───────────────────────────────────────────

_PARSER_KWARG_KEYS = frozenset({
    "lang", "device", "start_page", "end_page",
    "formula", "table", "backend", "source",
})


def filter_parser_kwargs(kwargs):
    """Return only kwargs that are relevant parser configuration keys."""
    return {k: v for k, v in kwargs.items() if k in _PARSER_KWARG_KEYS}
