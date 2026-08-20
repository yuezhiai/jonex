"""[jonex] §table-chunking: unit tests for normalize_table_rows / pack_rows /
format_table_body / _HTMLTableRowExtractor.

The import bypasses ``raganything/__init__.py`` (which requires LightRAG) by
loading ``utils.py`` directly with ``importlib`` and stubbing out
``lightrag.utils.logger``.
"""
import html.parser
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import patch

import pytest

# ── Load utils.py without triggering raganything.__init__ ──────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
_UTILS_PATH = _REPO_ROOT / "raganything" / "utils.py"

# Stub lightrag.utils.logger before the module-level ``from lightrag.utils
# import logger`` is executed.
with patch.dict("sys.modules", {}, clear=False):
    # Prevent ``lightrag`` and sub-packages from being imported
    class _FakeLogger(logging.Logger):
        pass

    _fake_lightrag = type("lightrag", (), {})
    _fake_lightrag_utils = type("lightrag_utils", (), {"logger": _FakeLogger("stub")})

    sys.modules["lightrag"] = _fake_lightrag
    sys.modules["lightrag.utils"] = _fake_lightrag_utils

    spec = importlib.util.spec_from_file_location(
        "raganything_utils_test", _UTILS_PATH,
    )
    _utils = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_utils)

_HTMLTableRowExtractor = _utils._HTMLTableRowExtractor
normalize_table_rows = _utils.normalize_table_rows
pack_rows = _utils.pack_rows
format_table_body = _utils.format_table_body
fmt_row = _utils.fmt_row
make_header_block = _utils.make_header_block
split_row_by_cells = _utils.split_row_by_cells


# ── _HTMLTableRowExtractor ────────────────────────────────────────────────


class TestHTMLTableRowExtractor:
    def test_basic_th_header(self):
        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        p = _HTMLTableRowExtractor()
        p.feed(html)
        p.close()
        assert p.header == ["A", "B"]
        assert p.rows == [["1", "2"]]

    def test_mineru_online_all_td(self):
        """MinerU Online uses <td> for all cells — first row is header."""
        html = (
            "<table>"
            "<tr><td><p>银行代码</p></td><td><p>银行名称</p></td></tr>"
            "<tr><td><p>000501</p></td><td><p>招商银行</p></td></tr>"
            "</table>"
        )
        p = _HTMLTableRowExtractor()
        p.feed(html)
        p.close()
        assert p.header == ["银行代码", "银行名称"]
        assert p.rows == [["000501", "招商银行"]]

    def test_single_row_is_header(self):
        html = "<table><tr><td>Only</td><td>Row</td></tr></table>"
        p = _HTMLTableRowExtractor()
        p.feed(html)
        p.close()
        assert p.header == ["Only", "Row"]
        assert p.rows == []

    def test_nested_table_skipped(self):
        html = (
            "<table><tr><td>Outer</td><td><table><tr><td>Inner</td></tr></table></td></tr></table>"
        )
        p = _HTMLTableRowExtractor()
        p.feed(html)
        p.close()
        # First row: ['Outer', ''] (nested table's empty <td> → empty cell)
        assert p.header == ["Outer", ""]
        assert p.rows == []

    def test_th_row_not_first(self):
        """First-row-wins: even with a later <th> row, the first row is header."""
        html = (
            "<table>"
            "<tr><td>data1</td><td>data2</td></tr>"
            "<tr><th>H1</th><th>H2</th></tr>"
            "<tr><td>v1</td><td>v2</td></tr>"
            "</table>"
        )
        p = _HTMLTableRowExtractor()
        p.feed(html)
        p.close()
        # First row wins as header
        assert p.header == ["data1", "data2"]
        # Remaining rows are data
        assert p.rows == [["H1", "H2"], ["v1", "v2"]]

    # ── colspan / rowspan ──

    def test_colspan_basic(self):
        html = (
            "<table>"
            "<tr><td>A</td><td>B</td><td>C</td></tr>"
            "<tr><td colspan='2'>merged</td><td>c1</td></tr>"
            "</table>"
        )
        p = _HTMLTableRowExtractor()
        p.feed(html)
        p.close()
        assert p.header == ["A", "B", "C"]
        assert p.rows == [["merged", "", "c1"]]

    def test_rowspan_basic(self):
        html = (
            "<table>"
            "<tr><td>A</td><td>B</td><td>C</td></tr>"
            "<tr><td rowspan='2'>r</td><td>b2</td><td>c2</td></tr>"
            "<tr><td>b3</td><td>c3</td></tr>"
            "</table>"
        )
        p = _HTMLTableRowExtractor()
        p.feed(html)
        p.close()
        assert p.header == ["A", "B", "C"]
        # Row 1: [r, b2, c2]
        assert p.rows[0] == ["r", "b2", "c2"]
        # Row 2: rowspan fills col 0 with "r", then [b3, c3]
        assert p.rows[1] == ["r", "b3", "c3"]

    def test_colspan_and_rowspan_combined(self):
        html = (
            "<table>"
            "<tr><td>A</td><td>B</td></tr>"
            "<tr><td colspan='2'>full-width</td></tr>"
            "<tr><td rowspan='2'>r</td><td>v1</td></tr>"
            "<tr><td>v2</td></tr>"
            "</table>"
        )
        p = _HTMLTableRowExtractor()
        p.feed(html)
        p.close()
        assert p.header == ["A", "B"]
        # row 0: colspan → ['full-width', '']
        assert p.rows[0] == ["full-width", ""]
        # row 1: ['r', 'v1']
        assert p.rows[1] == ["r", "v1"]
        # row 2: rowspan → ['r', 'v2']
        assert p.rows[2] == ["r", "v2"]


# ── normalize_table_rows ──────────────────────────────────────────────────


class TestNormalizeTableRows:
    def test_html_table(self):
        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        h, rows = normalize_table_rows(html)
        assert h == ["A", "B"]
        assert rows == [["1", "2"]]

    def test_html_no_header(self):
        """Single-row table: the first (and only) row is the header."""
        html = "<table><tr><td>v1</td><td>v2</td></tr></table>"
        h, rows = normalize_table_rows(html)
        # First-row-wins: goes to header, no data rows
        assert rows == []
        # The row itself is correctly extracted as header
        assert h == ["v1", "v2"]

    def test_list_of_lists(self):
        data = [["A", "B"], ["1", "2"], ["3", "4"]]
        h, rows = normalize_table_rows(data)
        assert h == ["A", "B"]
        assert rows == [["1", "2"], ["3", "4"]]

    def test_list_single_row(self):
        data = [["Only"]]
        h, rows = normalize_table_rows(data)
        assert h == []
        assert rows == [["Only"]]

    def test_markdown_table(self):
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
        h, rows = normalize_table_rows(md)
        assert h == ["A", "B"]
        assert rows == [["1", "2"], ["3", "4"]]

    def test_markdown_no_separator(self):
        md = "| A | B |\n| 1 | 2 |"
        h, rows = normalize_table_rows(md)
        assert h == ["A", "B"]
        assert rows == [["1", "2"]]

    def test_plain_text_returns_empty(self):
        h, rows = normalize_table_rows("just some text")
        assert h == []
        assert rows == []

    def test_none_returns_empty(self):
        h, rows = normalize_table_rows(None)
        assert h == []
        assert rows == []

    def test_empty_list_returns_empty(self):
        h, rows = normalize_table_rows([])
        assert h == []
        assert rows == []


# ── pack_rows ──────────────────────────────────────────────────────────────


class TestPackRows:
    def test_self_describing_format(self):
        """>6 columns → self-describing row format."""
        rows = [["000501", "招商银行", "002", "余额不足", "券商", "银行返回", "1341"]]
        header = ["银行代码", "银行名称", "柜台错误码", "错误信息", "发起方", "信息返回方", "银行错误码"]
        segs = pack_rows(rows, 2000, header)
        assert len(segs) == 1
        text, r0, r1 = segs[0]
        assert r0 == 0
        assert r1 == 1
        assert "银行代码: 000501" in text
        assert "银行名称: 招商银行" in text

    def test_markdown_format_small_table(self):
        """≤6 columns → markdown table format."""
        rows = [["000501", "招商银行", "002"]]
        header = ["银行代码", "银行名称", "柜台错误码"]
        segs = pack_rows(rows, 2000, header)
        text, r0, r1 = segs[0]
        assert "| 银行代码 | 银行名称 | 柜台错误码 |" in text
        assert "| --- | --- | --- |" in text
        assert "| 000501 | 招商银行 | 002 |" in text

    def test_budget_split(self):
        """Rows that exceed budget are split into multiple segments."""
        rows = [
            ["000501", "招商银行", "002", "活期账户余额不足", "券商", "银行返回", "1341"],
            ["000501", "招商银行", "020", "非转帐日期", "银行", "券商返回", ""],
            ["000502", "工商银行", "5049", "未找到冲帐对应原记录", "银行", "券商返回", "5049"],
        ]
        header = ["银行代码", "银行名称", "柜台错误码", "错误信息", "发起方", "信息返回方", "银行错误码"]
        segs = pack_rows(rows, 250, header)
        assert len(segs) > 1, f"Expected multiple segments, got {len(segs)}"
        total_rows = sum(r1 - r0 for _, r0, r1 in segs)
        assert total_rows == len(rows)

    def test_header_repeats_in_each_segment(self):
        """Header appears in every segment."""
        rows = [
            ["000501", "招商银行", "002", "错误A", "券商", "银行返回", "1341"],
            ["000502", "工商银行", "5049", "错误B", "银行", "券商返回", "5049"],
        ]
        header = ["银行代码", "银行名称", "柜台错误码", "错误信息", "发起方", "信息返回方", "银行错误码"]
        segs = pack_rows(rows, 50, header)
        assert len(segs) >= 2
        for text, _, _ in segs:
            assert "银行代码: " in text, f"Header missing in segment: {text[:100]}"

    def test_no_header_col_fallback(self):
        """When no header provided, generic col_N names are used."""
        rows = [["v1", "v2"]]
        segs = pack_rows(rows, 500, header=None)
        text, _, _ = segs[0]
        # ≤6 cols → markdown format with generic col_0/col_1 headers
        assert "col_0" in text
        assert "v1" in text

    def test_empty_rows(self):
        segs = pack_rows([], 500)
        assert segs == []

    def test_single_row_exceeds_budget(self):
        """A single row longer than budget becomes its own segment.
        The caller (PushChunksStage) is responsible for running
        _split_long_text on the output if the segment exceeds budget.
        """
        long_row = ["x" * 100 for _ in range(10)]
        segs = pack_rows([long_row], 100, header=None)
        assert len(segs) == 1, f"Expected 1 segment for single oversize row, got {len(segs)}"
        # The segment exceeds budget — documented/pre-existing behavior.
        assert len(segs[0][0]) > 100


# ── format_table_body ─────────────────────────────────────────────────────


class TestFormatTableBodyHTML:
    def test_html_auto_converted_to_markdown(self):
        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        result = format_table_body(html)
        assert "| A | B |" in result
        assert "| --- | --- |" in result
        assert "| 1 | 2 |" in result
        assert "<table>" not in result

    def test_normal_string_passes_through(self):
        result = format_table_body("just a string")
        assert result == "just a string"

    def test_list_preserved(self):
        result = format_table_body([["A", "B"], ["1", "2"]])
        assert "| A | B |" in result

    def test_non_table_html_passes_through(self):
        """HTML that doesn't contain a recognizable table stays as-is."""
        result = format_table_body("<div>not a table</div>")
        assert result == "<div>not a table</div>"


# ── split_row_by_cells (§C1-bis) ─────────────────────────────────────────


def _self_describing_row(n_cols: int, cell_len: int) -> str:
    """构造 self-describing 行文本：每格 ``列i: xxxx``，格长约 cell_len + 4。"""
    header = [f"列{i}" for i in range(n_cols)]
    padded = list(header) + [f"col_{i}" for i in range(len(header), n_cols)]
    value = "值" * cell_len
    row = [value] * n_cols
    return fmt_row(row, padded, n_cols, use_markdown=False)


class TestSplitRowByCells:
    def test_empty_and_fit_rows(self):
        assert split_row_by_cells("", 100) == []
        segs = split_row_by_cells("短行", 100)
        assert segs == [("短行", 0, 1)]

    def test_cuts_at_cell_boundaries(self):
        """§19.8 按单元格切：每格 50 字符 × 10 列，切点都落在 `` | `` 边界。"""
        row_text = _self_describing_row(10, cell_len=50)
        assert len(row_text) > 500  # 超长行（§19.8 的 3000 字符场景同构缩小）
        segs = split_row_by_cells(row_text, budget=200)
        assert len(segs) > 1, "超预算行必须被切成多段"
        for seg_text, _, _ in segs:
            assert len(seg_text) <= 200, f"段超预算: {len(seg_text)}"
        # 非尾段切点都落在单元格边界（body 以 " | " 结尾）
        for seg_text, _, _ in segs[:-1]:
            assert seg_text.endswith(" | "), f"切点不在单元格边界: {seg_text[-20:]!r}"
        # 每段可完整解析出「列名: 值」对（无单元格被切碎）：
        # self-describing 格式下按 " | " 拆分出的格数 == cell 区间长度
        # （非尾段末尾的空串来自边界切点，过滤掉）
        for seg_text, cell_start, cell_end in segs:
            cells = [c for c in seg_text.split(" | ") if c]
            assert len(cells) == cell_end - cell_start, (
                f"格数 {len(cells)} != 区间长度 {cell_end - cell_start}"
            )
        # cell 区间互不重叠且连续，覆盖 0..n_cells
        assert segs[0][1] == 0
        assert segs[-1][2] == 10
        for prev, nxt in zip(segs, segs[1:]):
            assert prev[2] == nxt[1], f"cell 区间断裂: {prev[2]} != {nxt[1]}"

    def test_self_describing_no_column_names_added(self):
        """§19.8 self-describing 不补列名：header_block 传空时续段无列名块。"""
        row_text = _self_describing_row(10, cell_len=50)
        segs = split_row_by_cells(row_text, budget=200)
        # 续段不能以 markdown 表头（"| 列名 |"）开头——不补列名
        for seg_text, _, _ in segs[1:]:
            assert not seg_text.startswith("| 列0 |")
        # 每格仍带自己的「列名: 」前缀（self-describing 天然自描述）
        first_cells = [c for c in segs[0][0].split(" | ") if c]
        assert all(": " in c for c in first_cells)

    def test_markdown_adds_header_block(self):
        """§19.8 markdown 补列名：每个子段都含 header_block。"""
        header = [f"列{i}" for i in range(5)]
        hb = make_header_block(header, 5, use_markdown=True)
        row = ["值" * 400, "v2", "v3", "v4", "v5"]  # 第一格 400 字符
        row_text = fmt_row(row, header, 5, use_markdown=True)
        # 整行 422 字符、hb 62 字符 → budget=480 时 body_budget=417 < 422，
        # 且第一格边界（405）完整落在窗口内 → 按格切、不硬切。
        segs = split_row_by_cells(row_text, budget=480, header_block=hb)
        assert len(segs) > 1
        for seg_text, _, _ in segs:
            assert seg_text.startswith(hb + "\n"), "续段缺失 markdown 表头块"
            assert len(seg_text) <= 480

    def test_caption_unconditionally_prefixed(self):
        """§19.8 caption 无条件补：每段首行含表标题 + 续段标记。"""
        row_text = _self_describing_row(10, cell_len=50)
        caption_line = "表格明细：【测试表】（续：第 1 行）\n"
        segs = split_row_by_cells(
            row_text, budget=300, caption_line=caption_line,
        )
        assert len(segs) > 1
        for seg_text, _, _ in segs:
            assert seg_text.startswith(caption_line), "段缺失表标题"
            assert len(seg_text) <= 300

    def test_single_cell_exceeds_budget_hard_cut(self):
        """§19.8 单个单元格超预算：退化字符硬切（非尾段段末不含 `` | ``）。"""
        row_text = "列A: " + "x" * 1500
        segs = split_row_by_cells(row_text, budget=400)
        assert len(segs) > 1
        for seg_text, _, _ in segs[:-1]:
            assert not seg_text.endswith(" | "), "硬切段不可能以单元格边界结尾"
        # 单格被劈：所有段 cell 区间都是 [0, 1)
        assert all((s, e) == (0, 1) for _, s, e in segs)
        for seg_text, _, _ in segs:
            assert len(seg_text) <= 400

    def test_cell_ranges_contiguous(self):
        """§19.8 cell 区间锚点：多段切分时区间互不重叠且连续。"""
        row_text = _self_describing_row(10, cell_len=50)
        # 每格 54 字符（"列i: " + 50），每段装 3 格 → 区间 [0,3) [3,6) [6,9) [9,10)
        segs = split_row_by_cells(row_text, budget=170)
        assert len(segs) >= 3
        assert segs[0][1] == 0
        assert segs[-1][2] == 10
        for prev, nxt in zip(segs, segs[1:]):
            assert prev[2] == nxt[1]
            assert prev[1] < prev[2] <= nxt[1]  # 单调推进且无重叠

    def test_prefix_accounted_in_budget(self):
        """header_block + caption_line 计入每段预算（§19.4）。"""
        row_text = _self_describing_row(10, cell_len=50)
        header = [f"列{i}" for i in range(5)]
        hb = make_header_block(header, 5, use_markdown=True)
        caption_line = "表格明细：【测试表】（续：第 1 行）\n"
        prefix_len = len(caption_line) + len(hb) + 1
        segs = split_row_by_cells(
            row_text, budget=prefix_len + 180,
            header_block=hb, caption_line=caption_line,
        )
        for seg_text, _, _ in segs:
            assert len(seg_text) <= prefix_len + 180, (
                f"段超预算: {len(seg_text)} > {prefix_len + 180}"
            )
            assert seg_text.startswith(caption_line + hb + "\n")
