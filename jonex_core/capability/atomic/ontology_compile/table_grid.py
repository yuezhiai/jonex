# -*- coding: utf-8 -*-
"""[jonex] 表格网格化闭包（P2 自 raganything.utils 迁入；宿主解耦）。

自 Reference/Rag-anything/raganything/utils.py 逐字搬迁（锚点定位切片，非行号
carve）：`normalize_caption_list` … `normalize_table_grid` 的整段闭包，含其间
全部私有 helper（`_extract_markdown_rows` / `_detect_suspect_left_shift` /
`_HTMLTableGridExtractor` / L2/L3 判据族）与模块常量。

变换**只有三处**，其余一个字符都不许改：
  1. 顶部 `from lightrag.utils import logger` → 标准库 `logging`
     （红线：新容器不得有 lightrag；`logger` 仅为对齐宿主模块而保留，
     本段闭包代码自身不调用它）；
  2. 开关访问器改名 `_table_grid_v2_enabled` → `table_grid_v2_enabled`
     （对外公开名，Task 6 装进 `TableGrid.enabled`）；
  3. 文件顶部补现代 import 头（`from __future__ import annotations` + 标准库
     与 `typing` import，与包内其他模块同构）。

`raganything.utils` 侧保留同符号转发，P4 宿主退役时连同转发段一起删。
"""
from __future__ import annotations

import html.parser
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


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
        if table_grid_v2_enabled():
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


def table_grid_v2_enabled() -> bool:
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
_HEADER_LONG_CELL_RATIO = 0.5        # 超长格占比超过该值才否决整行（env 可覆盖）
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


def _header_long_cell_ratio() -> float:
    try:
        return float(
            os.getenv("TABLE_HEADER_LONG_CELL_RATIO", str(_HEADER_LONG_CELL_RATIO))
        )
    except ValueError:
        return _HEADER_LONG_CELL_RATIO


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
    # [jonex] P0-c（2026-09-13 线上 OOM 复盘）：超长单元格原先**一票否决整行**
    # 表头判定。实测 financial_metric.md 的 21 列真表头里只有
    # `duplicate_count_in_period`（25 字符）> 24，整行被判非表头 → 真表头降级
    # 成 data_rows[0]、列名退化成 `col_0…col_20` → LLM 拿不到可用列名 → 主键
    # 识别为空 → 1438 张表零对象 → 兜底全量 LLM → 23.7GB OOM。
    # 改为比例判据：单个离群长格（如 snake_case 列名）不再否决，多数格超长
    # （典型「说明/备注」整行）才否决。
    if any(len(v) > _table_header_max_len() for v in values):
        # 长度已超出 prose 硬阈值（40）→ 该行就是说明文字，不是表头。
        return False
    long_cells = [v for v in values if len(v) > _HEADER_CELL_MAX_LEN]
    if long_cells and len(long_cells) >= len(values) * _header_long_cell_ratio():
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


# [jonex] §table-llm-bypass 守卫 B 的前扫窗口：超长表被盲切后，中段块没有
# 表头，长文数据行会连续出现——窗口有界把逐行前扫从 O(n²) 压回 O(n·W)。
_HEADER_LOOKAHEAD = 50


def _header_exists_ahead(
    grid: List[List[str]],
    start: int,
    date_semantic: bool,
    has_merge: List[bool],
    limit: int = _HEADER_LOOKAHEAD,
) -> bool:
    """窗口内是否还存在表头行（守卫 B 用）。

    跳过空行与含长文单元格的行（它们本身也是被猜测的对象），
    命中第一条普通行即按表头判据给出结论。
    """
    end = min(len(grid), start + max(1, limit))
    for r in range(start, end):
        row = grid[r]
        if not any(c and c.strip() for c in row):
            continue
        if any(_is_prose_cell(c) for c in row):
            continue
        return _is_header_row(
            row, date_semantic, header_exists=False,
            row_has_merge=has_merge[r] if r < len(has_merge) else False,
        )
    return False


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
            # [jonex] §table-llm-bypass 守卫 A：表头已定后遇到长文单元格的行
            # = 数据行（“备注/说明”列），不是表前说明。此前无条件 continue
            # 会把整表吃进 notes、data_start 推到表尾 → data_rows 为空 →
            # normalize_table_grid 判定失败 → 整块退回 12000 字符纯文本
            # （sdn.md 实测 503 块中 342 块如此）。
            if header_rows:
                break
            # 守卫 B：表头之前的长文行，只有在「后面确实还有表头」时才算是
            # 表前说明；否则本区段就没有表头（多为超长表被盲切后的中段块），
            # 应就地转数据行，至少把行恢复出来。扫描窗口有界（O(n·W)），
            # 避免整格逐行前扫退化成 O(n²)。
            if not _header_exists_ahead(
                grid, r + 1, date_semantic, has_merge, _HEADER_LOOKAHEAD,
            ):
                break
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
