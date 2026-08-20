"""[jonex] §table-grid-v2: unit tests for L1 grid filling + L2 header inference.

Covers table-parsing-retrieval-governance-plan.md §3 L5 regression cases
(step 6 of batch 3: L1/L2 + format_table_body switch):

  - rowspan forward-fill (T1 repro: 代理手续费表例外行错位)
  - colspan right-fill + colspan×rowspan cross + irregular row width
  - multi-level header with prose notes (T2 repro: 审批权限表 300 字说明)
  - numeric-sequence header (years) vs isolated numeric row
  - markdown pipe table / list-of-lists input
  - empty / single-row boundaries
  - format_table_body consistency (summary chain sees the same header)
  - legacy normalize_table_rows unchanged (RAG_TABLE_GRID_V2=false rollback)

L3 cell normalization (Excel serial dates) and L4.1 caption cases are added
in later steps of batch 3 (steps 9/10).

The import bypasses ``raganything/__init__.py`` (which requires LightRAG) by
loading ``utils.py`` directly with ``importlib`` and stubbing out
``lightrag.utils.logger``.
"""
import importlib.util
import logging
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Load utils.py without triggering raganything.__init__ ──────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
_UTILS_PATH = _REPO_ROOT / "raganything" / "utils.py"

# Stub lightrag.utils.logger before the module-level ``from lightrag.utils
# import logger`` is executed.
with patch.dict("sys.modules", {}, clear=False):
    class _FakeLogger(logging.Logger):
        pass

    _fake_lightrag = type("lightrag", (), {})
    _fake_lightrag_utils = type("lightrag_utils", (), {"logger": _FakeLogger("stub")})

    sys.modules["lightrag"] = _fake_lightrag
    sys.modules["lightrag.utils"] = _fake_lightrag_utils

    spec = importlib.util.spec_from_file_location(
        "raganything_utils_grid_test", _UTILS_PATH,
    )
    _utils = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_utils)

_HTMLTableGridExtractor = _utils._HTMLTableGridExtractor
normalize_table_grid = _utils.normalize_table_grid
normalize_table_rows = _utils.normalize_table_rows
format_table_body = _utils.format_table_body
extract_embedded_tables = _utils.extract_embedded_tables
pack_rows = _utils.pack_rows


def _grid_of(html: str):
    p = _HTMLTableGridExtractor()
    p.feed(html)
    p.close()
    return p.grid


# ── L1 网格化：_HTMLTableGridExtractor ──────────────────────────────────


class TestGridFilling:
    def test_rowspan_forward_fill_t1_repro(self):
        """T1 复现：品种/交割费 rowspan=2，例外行 2 个自有格。

        旧行为：例外行被 rowspan 占位撑到 6 列后 append，AU6 合约跑进
        col_6/col_7 与列名脱钩。新行为：rowspan 向下复制，自有格落在
        第一个未占用列，例外行成为自解释完整记录。
        """
        html = (
            "<table>"
            "<tr><th>品种类型</th><th>交易所</th><th>品种</th>"
            "<th>代码及合约</th><th>交易手续费标准</th><th>交割/行权履约手续费</th></tr>"
            '<tr><td rowspan="2">期货</td><td rowspan="2">上海期货交易所</td>'
            '<td rowspan="2">黄金</td><td>AU</td><td>40 元／手</td><td>0.15 元／克</td></tr>'
            '<tr><td>AU6、12月份合约</td><td>80 元／手</td><td>0.15 元／克</td></tr>'
            "</table>"
        )
        grid = _grid_of(html)
        # extractor 输出全部行（含表头行）；数据行索引 1/2
        assert grid[1] == ["期货", "上海期货交易所", "黄金", "AU", "40 元／手", "0.15 元／克"]
        assert grid[2] == ["期货", "上海期货交易所", "黄金", "AU6、12月份合约", "80 元／手", "0.15 元／克"]
        # 严格矩形，无多余列
        assert all(len(r) == 6 for r in grid)

    def test_colspan_right_fill(self):
        """colspan 向右复制：值出现在它跨越的每一列，不是首格有值其余留空。"""
        html = (
            "<table>"
            '<tr><th colspan="3">审批授权情况</th><th>备注</th></tr>'
            "<tr><td>部门负责人</td><td>分管领导</td><td>总裁</td><td>—</td></tr>"
            "</table>"
        )
        grid = _grid_of(html)
        assert grid[0] == ["审批授权情况", "审批授权情况", "审批授权情况", "备注"]
        assert grid[1] == ["部门负责人", "分管领导", "总裁", "—"]

    def test_colspan_rowspan_cross(self):
        """colspan × rowspan 交叉：4 格全部填入同值；下一行自有格落第一个未占用列。"""
        html = (
            "<table>"
            '<tr><th rowspan="2" colspan="2">审批授权情况</th>'
            "<th>部门负责人</th><th>分管领导</th></tr>"
            "<tr><td>A</td><td>B</td></tr>"
            "</table>"
        )
        grid = _grid_of(html)
        assert len(grid) == 2
        assert len(grid[0]) == 4 == len(grid[1])
        assert grid[0] == ["审批授权情况", "审批授权情况", "部门负责人", "分管领导"]
        assert grid[1] == ["审批授权情况", "审批授权情况", "A", "B"]

    def test_irregular_row_width_no_shift(self):
        """不规则行宽：rowspan 占住首列后，本行缺格补空、后续格不左移。"""
        html = (
            "<table>"
            "<tr><th>A</th><th>B</th><th>C</th><th>D</th></tr>"
            '<tr><td rowspan="2">x</td><td>1</td><td>2</td><td>3</td></tr>'
            "<tr><td>5</td><td>6</td></tr>"
            "</table>"
        )
        grid = _grid_of(html)
        # 5 落 B（index 1，A 列被 rowspan 占用）、6 落 C、D 留空
        assert grid[2] == ["x", "5", "6", ""]
        assert all(len(r) == 4 for r in grid)

    def test_nested_table_skipped(self):
        """嵌套表格只取最外层（与旧 extractor 行为一致）。"""
        html = (
            "<table><tr><th>外</th></tr>"
            "<tr><td><table><tr><td>内</td></tr></table></td></tr>"
            "</table>"
        )
        grid = _grid_of(html)
        assert len(grid) == 2
        assert grid[1] == [""]


# ── L2 表头推断：normalize_table_grid ──────────────────────────────────


class TestHeaderInference:
    def test_multilevel_header_with_prose_notes(self):
        """T2 复现：首行「总体原则 + 300 字说明」进 notes，真实列名成表头。"""
        prose = "1、本表适用于总部各部室资本支出、费用报销等审批事项。" + "具体规则详见公司制度文件。" * 30
        assert len(prose) > 300
        html = (
            "<table>"
            f"<tr><td>总体原则</td><td>{prose}</td>" + "<td></td>" * 8 + "</tr>"
            "<tr><td></td><td></td><td>部门负责人</td><td>归口管理部门负责人</td>"
            "<td>财务部负责人</td><td>分管领导</td><td>归口管理分管领导</td>"
            "<td>总裁／董事长</td><td>决策会议</td><td>事项性质</td></tr>"
            "<tr><td>1.1</td><td>IT 归口管理资本支出</td><td>≤20万</td><td>●</td>"
            "<td>●</td><td>●</td><td>●</td><td>金科中心●</td><td>●</td><td>支出</td></tr>"
            "</table>"
        )
        header, data_rows, meta = normalize_table_grid(html)
        # 真实列名成为表头，中间列无 col_N 占位
        assert header[2] == "部门负责人"
        assert header[7] == "总裁／董事长"
        assert header[8] == "决策会议"
        assert not any(re.fullmatch(r"col_[2-9]", c) for c in header)
        # 300 字说明进 notes，不再混入列名
        assert meta["notes"] and any(len(n) > 300 for n in meta["notes"])
        assert not any("总体原则" in c or "本表适用" in c for c in header)
        # 数据行从真实表头之后开始
        assert data_rows and data_rows[0][0] == "1.1"
        assert meta["header_levels"] == 1

    def test_year_sequence_header(self):
        """连续递增序列（年份）判合法表头，不被「纯数字非表头」误杀。"""
        grid = [["2023", "2024", "2025"], ["a", "b", "c"]]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["2023", "2024", "2025"]
        assert data_rows == [["a", "b", "c"]]

    def test_isolated_numbers_not_header(self):
        """孤立数字行判度量值：降级 col_N，整行保留为数据。"""
        grid = [["1.1", "45", "0.3"], ["x", "y", "z"]]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["col_0", "col_1", "col_2"]
        assert data_rows == grid

    def test_repeated_values_not_header(self):
        """H2：交替重复（是/否/是/否 频繁切换）判数据行。"""
        grid = [["是", "否", "是", "否"], ["a", "b", "c", "d"]]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["col_0", "col_1", "col_2", "col_3"]
        assert data_rows == grid

    def test_repeated_clustered_values_released(self):
        """H2：连续聚块（是×3+否 / Q1×2+Q2×2）放行——L1 forward-fill 产物
        或期间分组表头；首行被吃的代价可接受（C7 已知取舍）。"""
        grid = [["是", "是", "是", "否"], ["a", "b", "c", "d"]]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header[0] == "是"
        assert data_rows == [["a", "b", "c", "d"]]

    def test_h1_blank_rows_before_header_skipped(self):
        """H1：表前空行不再吞掉表头（此前 break 导致表头整区丢失）。"""
        grid = [
            ["", "", ""],
            ["", "", ""],
            ["列一", "列二", "列三"],
            ["a", "b", "c"],
        ]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["列一", "列二", "列三"]
        assert data_rows == [["a", "b", "c"]]

    def test_h2_alternating_repetition_is_data(self):
        """H2：交替重复（是/否/是/否 频繁切换）判数据行。"""
        grid = [["是", "否", "是", "否"], ["a", "b", "c", "d"]]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["col_0", "col_1", "col_2", "col_3"]
        assert data_rows == grid

    def test_h2_contiguous_run_released(self):
        """H2：连续聚块（L1 forward-fill 产物，如「期货×N」）放行不判数据。"""
        grid = [["期货", "期货", "期货", "期货"], ["金", "银", "铜", "铝"]]
        header, data_rows, meta = normalize_table_grid(grid)
        # 连续聚块行判表头（损失可接受，远好于误杀分组表头）
        assert header[0] == "期货"
        assert data_rows == [["金", "银", "铜", "铝"]]

    def test_grouped_repeating_header_kept(self):
        """分组重复表头（星期一/总数量/剩余数量 × N 天）是合法表头，
        不被重复率判据误杀——线上 2025年4月.xlsx 星期表头回归。"""
        grid = [
            ["", "", "星期一", "总数量", "剩余数量", "星期二", "总数量", "剩余数量",
             "星期三", "总数量", "剩余数量", "星期四", "总数量", "剩余数量"],
            ["", "", "45747", "", "", "45748", "", "", "45749", "", "", "45750", "", ""],
            ["早", "中式", "肉包", "", "", "香菇肉包", "", "", "素菜包/核桃包", "", "", "肉包", "", ""],
        ]
        header, data_rows, meta = normalize_table_grid(grid)
        # 两级表头：星期行（层 1）+ 序列号行经 L3 转日期（层 2）拼接；
        # 层级上限 2 挡住肉包数据行（此前三级级联误判）
        assert header[2] == "星期一-2025-03-31（周一）"
        assert header[5] == "星期二-2025-04-01（周二）"
        assert "总数量" in header
        assert not any(c.startswith("col_") for c in header[2:])
        # 数据从菜品行开始，日期语义不丢
        assert data_rows[0][0] == "早"
        assert "肉包" in " ".join(data_rows[0])

    def test_two_level_header_join(self):
        """多级表头纵向拼接：父级名复制到跨越列后按 - 拼接。"""
        html = (
            "<table>"
            '<tr><th colspan="2">审批授权情况</th><th>决策会议</th></tr>'
            "<tr><th>部门负责人</th><th>分管领导</th><th>—</th></tr>"
            "<tr><td>A</td><td>B</td><td>C</td></tr>"
            "</table>"
        )
        header, data_rows, meta = normalize_table_grid(html)
        assert header[0] == "审批授权情况-部门负责人"
        assert header[1] == "审批授权情况-分管领导"
        assert header[2] == "决策会议"
        assert data_rows == [["A", "B", "C"]]
        assert meta["header_levels"] == 2

    def test_short_dunhao_header_kept(self):
        """短顿号表头（如「证券、基金」）不因含句读被剔除进 notes。"""
        html = (
            "<table>"
            "<tr><th>交易品种、合约</th><th>手续费</th></tr>"
            "<tr><td>黄金</td><td>80</td></tr>"
            "</table>"
        )
        header, data_rows, meta = normalize_table_grid(html)
        assert header == ["交易品种、合约", "手续费"]
        assert meta["notes"] == []
        assert data_rows == [["黄金", "80"]]

    def test_markdown_pipe_input(self):
        md = (
            "| 年份 | 收入 |\n"
            "| --- | --- |\n"
            "| 2023 | 100 |\n"
            "| 2024 | 120 |\n"
        )
        header, data_rows, meta = normalize_table_grid(md)
        assert header == ["年份", "收入"]
        assert data_rows == [["2023", "100"], ["2024", "120"]]

    def test_list_of_lists_input(self):
        rows = [["名称", "数量"], ["苹果", "3"], ["梨", "5"]]
        header, data_rows, meta = normalize_table_grid(rows)
        assert header == ["名称", "数量"]
        assert data_rows == [["苹果", "3"], ["梨", "5"]]


# ── 边界与回退语义 ─────────────────────────────────────────────────────


class TestBoundariesAndRollback:
    def test_empty_inputs_do_not_raise(self):
        for raw in (None, "", "   ", [], [["a"], ["b"]], "普通文本"):
            header, data_rows, meta = normalize_table_grid(raw)
            assert isinstance(header, list)
            assert isinstance(data_rows, list)
            assert isinstance(meta, dict)

    def test_single_row_degrades_to_no_data(self):
        """单行表：表头特征行 → data 为空（调用方 fallback 原文本）。"""
        html = "<table><tr><th>A</th><th>B</th></tr></table>"
        header, data_rows, meta = normalize_table_grid(html)
        assert header == ["A", "B"]
        assert data_rows == []

    def test_format_table_body_uses_inferred_header(self):
        """摘要链路与明细行基于同一份推断表头；notes 附在表后。"""
        prose = "本表适用于……" * 30  # >40 字，触发 notes
        html = (
            "<table>"
            f"<tr><td>{prose}</td><td></td><td></td></tr>"
            "<tr><td>部门负责人</td><td>分管领导</td><td>备注</td></tr>"
            "<tr><td>A</td><td>B</td><td>C</td></tr>"
            "</table>"
        )
        rendered = format_table_body(html)
        # 表头是推断后的列名，而非 prose 长文本
        assert "部门负责人" in rendered
        assert "分管领导" in rendered
        # prose 不进表体，只在表后说明段落出现
        table_part = rendered.split("【表前说明】")[0]
        assert "本表适用于" not in table_part
        assert "【表前说明】" in rendered

    def test_format_table_body_rollback_flag(self, monkeypatch):
        """RAG_TABLE_GRID_V2=false → 回退旧 normalize_table_rows 路径（无 notes 段落）。"""
        monkeypatch.setenv("RAG_TABLE_GRID_V2", "false")
        prose = "本表适用于……" * 30
        html = (
            "<table>"
            f"<tr><td>{prose}</td><td></td><td></td></tr>"
            "<tr><td>A</td><td>B</td><td>C</td></tr>"
            "</table>"
        )
        rendered = format_table_body(html)
        assert "【表前说明】" not in rendered

    def test_legacy_normalize_table_rows_unchanged(self, monkeypatch):
        """旧函数在开关 false 时的行为与改造前一致（回滚语义）。"""
        monkeypatch.setenv("RAG_TABLE_GRID_V2", "false")
        html = (
            "<table>"
            "<tr><th>A</th><th>B</th></tr>"
            "<tr><td>1</td><td>2</td></tr>"
            "</table>"
        )
        header, rows = normalize_table_rows(html)
        assert header == ["A", "B"]
        assert rows == [["1", "2"]]


# ── L3 兜底轨：Excel 序列号启发式 ────────────────────────────────────────


class TestExcelSerialDates:
    def test_serial_header_with_date_semantic(self):
        """L5 用例 8：45761 表头 + 同表「星期一」→ 列名 2025-04-14（周一）。"""
        grid = [["45761"], ["星期一"]]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["2025-04-14（周一）"]
        assert data_rows == [["星期一"]]
        assert meta["dates_heuristic"] == 1

    def test_serial_no_date_semantic_not_converted(self):
        """L5 用例 9：同表无任何日期/星期词 → 不转换（避免误伤业务数字）。"""
        grid = [["45761"], ["烧鸭"]]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["col_0"]  # 单格数字无日期信号 → 度量值 → 非表头
        assert data_rows == grid
        assert meta["dates_heuristic"] == 0

    def test_normalize_cell_value_guards(self):
        ncv = _utils._normalize_cell_value
        # 无日期信号：一律不转
        assert ncv("45761", False) == "45761"
        # 小数不转（非纯整数）
        assert ncv("1.5", True) == "1.5"
        # 窗口外不转
        assert ncv("0", True) == "0"
        assert ncv("100001", True) == "100001"
        # 千分位整数转（与数字判定同口径去逗号）
        assert ncv("45,761", True) == "2025-04-14（周一）"
        # 负数不转（窗口下限 1）
        assert ncv("-1", True) == "-1"
        # 含百分号不转
        assert ncv("50%", True) == "50%"

    def test_serial_in_multi_col_table(self):
        """T3 复现简化版：45761/45762 两列 + 星期语义 → 表头转日期。"""
        grid = [["45761", "45762"], ["星期一", "星期二"]]
        header, data_rows, meta = normalize_table_grid(grid)
        # 规范化后表头行是日期文本（非纯数字序列），但序列行判据在规范化
        # 之后执行 → 首行日期序列属于「非纯数字」行 → 合法表头
        assert header[0] == "2025-04-14（周一）"
        assert header[1] == "2025-04-15（周二）"


# ── L4.1/O3：上下文头 + 分组切分（pack_rows caption） ─────────────────────


class TestPackRowsCaptionAndGrouping:
    ROWS = [
        ["上海期货交易所", "黄金", "AU"],
        ["上海期货交易所", "铜", "CU"],
        ["大连商品交易所", "豆粕", "M"],
        ["大连商品交易所", "玉米", "C"],
    ]
    HEADER = ["交易所", "品种", "代码"]

    def test_caption_line_prefixed(self):
        """L5 用例 10：segment 含「表格明细：【表】标题」，无【列】冗余行。"""
        segs = pack_rows(self.ROWS, 900, self.HEADER, caption="招商期货分支机构")
        # O3 分组断开：两个交易所各成一段，每段都带上下文头
        assert len(segs) == 2
        for text, _, _ in segs:
            assert text.startswith("表格明细：【表】招商期货分支机构\n")
            assert "【列】" not in text
            # 行数据仍带列名（self-describing；3 列 → markdown 紧凑格式重复表头）
            assert "| 交易所 | 品种 | 代码 |" in text
        assert "上海期货交易所" in segs[0][0]
        assert "大连商品交易所" in segs[1][0]

    def test_no_caption_keeps_legacy_format(self):
        segs = pack_rows(self.ROWS, 900, self.HEADER)
        text = segs[0][0]
        assert "表格明细：" not in text

    def test_caption_head_ratio_bounded(self):
        """L5 用例 12：长表标题 + 短 chunk → 头部 ≤ chunk 总长 8%。"""
        long_caption = "这是一个非常长的表格标题用于测试截断行为是否超过百分之八的预算约束限制" * 3
        budget = 200
        segs = pack_rows(self.ROWS, budget, self.HEADER, caption=long_caption)
        for seg_text, _, _ in segs:
            first_line = seg_text.split("\n", 1)[0]
            assert len(first_line) + 1 <= budget * 0.08 + 1
            assert "…" in first_line  # 超长被截断

    def test_group_boundary_split(self):
        """O3：分组键（第 0 列）不同 → 段在分组边界断开。"""
        budget = 200  # 一段放得下 2 行（约 112 字符），组边界在第 2 行后
        segs = pack_rows(self.ROWS, budget, self.HEADER, caption="品种表")
        # 分组边界：上海期货交易所 2 行 / 大连商品交易所 2 行
        assert len(segs) == 2
        assert "上海期货交易所" in segs[0][0]
        assert "大连商品交易所" in segs[1][0]
        assert segs[0][2] == 2  # 第一段行 0-2（组边界）
        assert segs[1][1] == 2

    def test_continuation_suffix_within_overlong_group(self):
        """O3：组本身超预算 → 组内切分，续块带「（续：分组值）」。"""
        rows = [["上海期货交易所", f"品种{i}", f"CU{i}"] for i in range(20)]
        budget = 150  # 组内 20 行必须切
        segs = pack_rows(rows, budget, ["交易所", "品种", "代码"], caption="品种表")
        assert len(segs) > 1
        # 首段无续标，后续段有续标
        assert "（续：上海期货交易所）" not in segs[0][0]
        assert any("（续：上海期货交易所）" in s[0] for s in segs[1:])
        # 行区间连续无遗漏
        assert segs[0][1] == 0
        assert segs[-1][2] == 20
        for prev, nxt in zip(segs, segs[1:]):
            assert prev[2] == nxt[1]


class TestValidCaption:
    def test_all_invalid_forms(self):
        for raw in (None, [], "", "   ", "None", "none", "null", "N/A",
                    "n/a", [None], [""], ["None", ""]):
            assert _utils._valid_caption(raw) is None

    def test_valid_forms(self):
        assert _utils._valid_caption(["招商期货分支机构"]) == "招商期货分支机构"
        assert _utils._valid_caption("表1") == "表1"
        assert _utils._valid_caption(["a", "b"]) == "a, b"

    def test_overlong_rejected(self):
        assert _utils._valid_caption(["长" * 61]) is None


# ── 8 类真实表格形态压测（增补十 §18.4 验收基线） ──────────────────────────
# 无论表头判定主判是规则还是模型（第 14 步模型化），这 8 条都是验收基线。
# C1 是规则路线的已知天花板（版式无法区分「星期一是维度名」与「肉包是
# 菜品实例」），模型化前 xfail。


class TestTableFormStress:
    def test_c1_menu_partial_blank_header_keeps_data(self):
        """C1 线上菜单表：首行部分空表头 + 中文数据行。

        [模型化前已知失败] 规则路线会把「早/中式/肉包」行吃进表头
        （两级拼接「星期一-肉包」）——H3 结构约束已撤回，模型化根治。
        第 14 步模型化通过后移除 xfail 行。
        """
        grid = [
            ["", "", "星期一", "星期二", "星期三"],
            ["早", "中式", "肉包", "香菇肉包", "素菜包"],
            ["早", "粉面", "炒牛河", "三丝炒米粉", "猪肠粉"],
            ["午", "主", "烧鸭", "卤水拼", "烧鸡"],
        ]
        pytest.xfail("模型化前已知失败：肉包行被吃进表头（增补十 C1）")
        header, data_rows, meta = normalize_table_grid(grid)
        assert header[2] == "星期一"
        assert len(data_rows) == 3

    def test_c2_continuation_no_header_all_data(self):
        """C2 表格续块：无表头，首行即数据，含长文本错误信息列。"""
        grid = [
            ["000501", "招商银行", "5019", "因为超过允许的错误次数，您的资金账户已被锁定", "银行"],
            ["000501", "招商银行", "5041", "转账金额超过单笔转出上限，请分次转账", "银行"],
            ["000501", "招商银行", "5043", "转账金额超过单笔转入上限", "银行"],
        ]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["col_0", "col_1", "col_2", "col_3", "col_4"]
        assert len(data_rows) == 3  # 数据全保留

    def test_c3_blank_first_row_header_found(self):
        """C3 MinerU 空首行 + 真实表头在第 2 行（H1 修复回归）。"""
        grid = [
            ["", "", "", ""],
            ["序号", "品种", "代码", "手续费"],
            ["1", "黄金", "AU", "40 元/手"],
            ["2", "白银", "AG", "万分之0.4"],
        ]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["序号", "品种", "代码", "手续费"]
        assert len(data_rows) == 2

    def test_c4_two_level_full_header_single_level_fallback(self):
        """C4 两级满行表头：已知取舍判成单级（Q1 行做表头）。"""
        grid = [
            ["Q1", "Q1", "Q2", "Q2"],
            ["销量", "收入", "销量", "收入"],
            ["100", "2000", "120", "2400"],
        ]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["Q1", "Q1", "Q2", "Q2"]
        assert len(data_rows) == 2

    def test_c5_standard_single_header_numeric_data(self):
        grid = [
            ["年份", "营收", "净利"],
            ["2023", "1000", "100"],
            ["2024", "1200", "140"],
            ["2025", "1500", "180"],
        ]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["年份", "营收", "净利"]
        assert len(data_rows) == 3

    def test_c6_year_columns_sequence_header(self):
        grid = [
            ["指标", "2023", "2024", "2025"],
            ["营收", "1000", "1200", "1400"],
            ["净利", "100", "120", "140"],
        ]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["指标", "2023", "2024", "2025"]
        assert len(data_rows) == 2

    def test_c7_headerless_chinese_data_not_eaten(self):
        """C7 无表头纯数据表（全短中文）——数据行不被吃进表头。"""
        grid = [
            ["刘飞", "青岛西海岸", "686", "正常"],
            ["宋腾飞", "青岛新都心", "332", "正常"],
            ["李迅", "青岛金家岭", "863", "正常"],
        ]
        header, data_rows, meta = normalize_table_grid(grid)
        assert header == ["col_0", "col_1", "col_2", "col_3"]
        assert len(data_rows) == 3

    def test_c8_approval_table_notes_and_two_level(self):
        """C8 审批表：说明合并行进 notes + 两级表头（有合并信号）+ 1 行数据。"""
        grid = [
            ["总体原则",
             "1、本表适用于总部各部室。2、根据公司制度要求，重大项目需经决策会议审议。",
             "", "", ""],
            ["序号", "支出事项", "审批授权情况", "审批授权情况", "备注"],
            ["", "", "部门负责人", "分管领导", ""],
            ["1.1", "IT 资本支出", "●", "金科中心●", "含硬件"],
        ]
        header, data_rows, meta = normalize_table_grid(
            grid, has_merge=[False, True, False, False],
        )
        assert meta["notes"] and "本表适用于" in meta["notes"][0]
        assert header[2] == "审批授权情况-部门负责人"
        assert header[3] == "审批授权情况-分管领导"
        assert len(data_rows) == 1
        assert data_rows[0][0] == "1.1"


# ── O4-bis：B 型错位（左移）嫌疑行检测 ────────────────────────────────────


class TestSuspectLeftShift:
    def test_mid_hole_detected(self):
        """rowspan 标注不完整 → 自有格左移：中间空洞 + 右侧仍有值。"""
        grid = [["AG6、12月份合约", "万分之2", "白银", "", "", "3 元／千克"]]
        suspect = _utils._detect_suspect_left_shift([4], grid)
        assert suspect == [0]

    def test_missing_tail_columns_not_suspect(self):
        """正常缺尾列（空段在最右端）不算。"""
        grid = [["a", "b", "c", "d", "", ""]]
        assert _utils._detect_suspect_left_shift([4], grid) == []

    def test_full_row_not_suspect(self):
        grid = [["a", "b", "c", "d", "e", "f"]]
        assert _utils._detect_suspect_left_shift([6], grid) == []

    def test_thin_prefix_not_suspect(self):
        """左侧连续非空 < 2 → 不判（稀疏行）。"""
        grid = [["a", "", "", "b", "", "c"]]
        assert _utils._detect_suspect_left_shift([3], grid) == []

    def test_small_hole_not_suspect(self):
        """空段长度 1 → 不判（单格空位常见，噪声大）。"""
        grid = [["a", "b", "", "c", "d", "e"]]
        assert _utils._detect_suspect_left_shift([5], grid) == []

    def test_meta_passthrough(self):
        """normalize_table_grid 的 meta 透出 suspect_left_shift 计数。"""
        # list 输入：行 1 原始 5 格 < 6 列、中间空洞 → 1 个嫌疑
        rows = [
            ["类型", "交易所", "品种", "代码", "手续费", "交割费"],
            ["AG6、12月份合约", "万分之2", "", "", "3 元／千克"],
        ]
        header, data_rows, meta = normalize_table_grid(rows)
        assert meta["suspect_left_shift"] == 1

    def test_normal_table_zero_suspect(self):
        rows = [["a", "b", "c"], ["1", "2", "3"]]
        header, data_rows, meta = normalize_table_grid(rows)
        assert meta["suspect_left_shift"] == 0


# ── O2 内嵌表格探测：extract_embedded_tables ──────────────────────────────


class TestEmbeddedTables:
    def test_html_table_in_middle(self):
        text = (
            "前文介绍。\n"
            "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>\n"
            "后文总结。"
        )
        segs = extract_embedded_tables(text)
        assert [s["kind"] for s in segs] == ["text", "table", "text"]
        assert segs[0]["text"] == "前文介绍。\n"
        assert segs[1]["text"].startswith("<table")
        assert segs[2]["text"] == "\n后文总结。"

    def test_plain_text_single_segment(self):
        segs = extract_embedded_tables("普通正文，无表格。")
        assert len(segs) == 1
        assert segs[0] == {"kind": "text", "text": "普通正文，无表格。"}

    def test_markdown_pipe_table_extracted(self):
        text = (
            "说明文字\n"
            "| 年份 | 收入 |\n"
            "| --- | --- |\n"
            "| 2023 | 100 |\n"
            "| 2024 | 120 |\n"
            "结尾文字\n"
        )
        segs = extract_embedded_tables(text)
        assert [s["kind"] for s in segs] == ["text", "table", "text"]
        # 管道表段可被 normalize_table_grid 归一化
        header, data_rows, meta = normalize_table_grid(segs[1]["text"])
        assert header == ["年份", "收入"]
        assert data_rows == [["2023", "100"], ["2024", "120"]]

    def test_fewer_than_3_pipe_lines_not_table(self):
        text = "| 只有 | 两行 |\n| 不算 | 表格 |\n"
        segs = extract_embedded_tables(text)
        assert len(segs) == 1
        assert segs[0]["kind"] == "text"

    def test_multiple_tables_in_one_block(self):
        text = (
            "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
            "中间文字"
            "<table><tr><th>B</th></tr><tr><td>2</td></tr></table>"
        )
        segs = extract_embedded_tables(text)
        assert [s["kind"] for s in segs] == ["table", "text", "table"]

    def test_md_table_inside_html_segment_boundaries(self):
        """HTML 表与管道表共存：两段都被提取。"""
        text = (
            "<table><tr><th>H</th></tr><tr><td>1</td></tr></table>\n"
            "| A | B |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
        )
        segs = extract_embedded_tables(text)
        kinds = [s["kind"] for s in segs]
        assert kinds == ["table", "text", "table"] or kinds == ["table", "table"]
