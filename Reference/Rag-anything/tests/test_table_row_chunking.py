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
