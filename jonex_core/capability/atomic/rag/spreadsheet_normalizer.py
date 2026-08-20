# -*- coding: utf-8 -*-
"""[jonex] §table-grid-v2 L3 主轨：xlsx 前置规范化（openpyxl 精确轨）。

治理方案：docs/table-parsing-retrieval-governance-plan.md §3 L3（改动 6/43）。

背景（T3）：mineru_online 对 xlsx 走 `_parse_via_selfhost`，平台不做前置格式化，
拿不到 `number_format`——线上「2025年4月.xlsx」的表格 chunk 表头是 `45761 / 45762`
（Excel 日期序列号），日期→星期→菜品的三维对应彻底丢失。

主轨职责：用 openpyxl 逐格按 `number_format` 渲染结构化表格（日期/百分比/货币），
产出与 MinerU `content_list` table item 完全对齐的 dict（下游 `get_table_body` /
`_collect_text_chunks` / `TableModalProcessor` 零特殊分支）：

    {
        "type": "table",
        "table_body": "<table data-jonex-native=\"1\">...</table>",
        "table_caption": [sheet_name],      # list[str]，与 MinerU 口径一致
        "table_footnote": [],
        "page_idx": sheet_index,            # sheet 序号占位，保证 file_source page 锚点有值
        "table_idx": table_seq,             # 同 sheet 多表（空行分隔）时递增
        "img_path": "",                     # 无表格截图
    }

合并规则（在 vendored ParseStage 侧执行）：
- openpyxl 产出的 `type=="table"` 项 → **保留**，作为表格唯一来源；
- MinerU 照常调用，其 `type=="table"` 项 → **丢弃**（丢弃数量计入 table_stats）；
- MinerU 的 `type=="image"` / `type=="equation"` 等非表格项 → **保留**（图表/截图
  openpyxl 拿不到，必须走现有多模态链路）。

`table_body` 带 `data-jonex-native="1"` 标记：合并幂等指纹（cache 命中路径重复合并时
靠它识别已合并，不重复插入）；`_HTMLTableRowExtractor` 忽略未知属性。

⚠️ 不支持旧版 .xls（openpyxl 仅 xlsx）——normalize 返回 [] 时调用方回退 MinerU
全量（旧行为），不丢数据。
"""
import html
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

try:
    import openpyxl
except ImportError:  # pragma: no cover — 依赖缺失时主轨整体不可用
    openpyxl = None  # type: ignore[assignment]

# 合并幂等指纹：content_list 中已含此标记的 table 项 → 说明已合并过
NATIVE_TABLE_MARK = "data-jonex-native"

_WEEKDAY_CN = "一二三四五六日"


def _excel_serial_to_date(serial: int) -> str:
    """Excel 1900 日期系统（含 1900 闰年 bug，openpyxl 同口径）。"""
    d = date(1899, 12, 30) + timedelta(days=serial)
    return f"{d:%Y-%m-%d}（周{_WEEKDAY_CN[d.weekday()]}）"


def _render_cell(cell: Any) -> str:
    """按 cell.number_format 渲染单元格值（T3 修复核心）。"""
    v = cell.value
    if v is None:
        return ""
    if isinstance(v, datetime):
        # openpyxl 对日期格式单元格自动转 datetime；纯日期（00:00:00）渲染为
        # 「YYYY-MM-DD（周X）」，带时间的原样输出
        if v.time() == time(0, 0):
            return _excel_serial_to_date(v.toordinal() - date(1899, 12, 30).toordinal())
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return _excel_serial_to_date(v.toordinal() - date(1899, 12, 30).toordinal())
    if isinstance(v, (int, float)):
        fmt = cell.number_format or ""
        if "%" in fmt:
            # 百分比：0.065 存为 6.5% 显示
            return f"{v * 100:g}%"
        if any(s in fmt for s in ("¥", "￥", "$", "€", "£")):
            sym = next(s for s in ("¥", "￥", "$", "€", "£") if s in fmt)
            return f"{sym}{v:,.2f}"
        if fmt and "," in fmt.split(";")[0] and any(c in fmt for c in ("#", "0")):
            return f"{v:,.2f}"
    return str(v)


def _trim_row(row: List[str]) -> List[str]:
    """去掉行右端连续空列（避免 max_column 虚高把 HTML 撑爆）。"""
    end = len(row)
    while end > 0 and not row[end - 1].strip():
        end -= 1
    return row[:end]


def _rows_to_html(rows: List[List[str]]) -> str:
    parts = [f'<table {NATIVE_TABLE_MARK}="1">']
    for row in rows:
        parts.append("<tr>")
        for cell in row:
            parts.append(f"<td>{html.escape(cell)}</td>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _sheet_to_items(ws: Any, sheet_name: str, page_idx: int) -> List[dict]:
    """一个 sheet → 0..N 个 table item（全空行分隔多表，table_idx 递增）。"""
    items: List[dict] = []
    current: List[List[str]] = []
    table_seq = 0

    def _flush() -> None:
        nonlocal table_seq
        if not current:
            return
        items.append({
            "type": "table",
            "table_body": _rows_to_html(current),
            "table_caption": [sheet_name],
            "table_footnote": [],
            "page_idx": page_idx,
            "table_idx": table_seq,
            "img_path": "",
        })
        table_seq += 1

    for row in ws.iter_rows(values_only=False):
        cells = _trim_row([_render_cell(c) for c in row])
        if not cells:  # 全空行 → 表分隔
            _flush()
            current = []
            continue
        current.append(cells)
    _flush()
    return items


def normalize_xlsx(file_path: str) -> List[dict]:
    """[jonex] §table-grid-v2 L3 主轨：产出与 MinerU content_list 对齐的 table items。

    任何失败（文件打不开 / openpyxl 缺失 / 非 xlsx）返回 ``[]`` —— 调用方回退
    MinerU 全量（旧行为），表格结构化能力降级但不丢数据。
    """
    if openpyxl is None:
        logger.warning(
            "[jonex] §table-grid-v2 L3: openpyxl 不可用，xlsx 主轨跳过 "
            "（回退 MinerU 全量）"
        )
        return []
    try:
        wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — 主轨失败必须回退
        logger.warning(
            "[jonex] §table-grid-v2 L3: xlsx 主轨打开失败（%s），回退 MinerU 全量",
            exc,
        )
        return []
    items: List[dict] = []
    try:
        for sheet_idx, ws in enumerate(wb.worksheets):
            try:
                items.extend(_sheet_to_items(ws, ws.title, sheet_idx))
            except Exception as exc:  # noqa: BLE001 — 单 sheet 失败不拖垮全文档
                logger.warning(
                    "[jonex] §table-grid-v2 L3: sheet「%s」规范化失败（%s），跳过该 sheet",
                    ws.title, exc,
                )
    finally:
        wb.close()
    return items


def merge_native_tables(
    content_list: List[dict], file_path: str,
) -> tuple[List[dict], int]:
    """[jonex] §table-grid-v2 L3: 双路合并（ParseStage 调用，幂等）。

    Returns:
        ``(merged_content_list, mineru_table_dropped)``。已合并过（含
        ``NATIVE_TABLE_MARK`` 指纹）时原样返回 ``(content_list, 0)``。
    """
    if any(
        it.get("type") == "table"
        and NATIVE_TABLE_MARK in str(it.get("table_body", ""))
        for it in content_list
    ):
        return content_list, 0  # 幂等：cache 命中路径重复合并时直接放行

    native_tables = normalize_xlsx(file_path)
    if not native_tables:
        return content_list, 0  # 主轨失败 → MinerU 全量（旧行为）

    kept = [it for it in content_list if it.get("type") != "table"]
    dropped = len(content_list) - len(kept)
    merged = kept + native_tables
    # 按 page_idx 稳定排序（MinerU 图像项保留原相对顺序，openpyxl 表按 sheet 序）
    merged.sort(key=lambda it: it.get("page_idx") if it.get("page_idx") is not None else 0)
    logger.info(
        "[jonex] §table-grid-v2 L3: xlsx 双路合并完成——openpyxl 表 %d 张、"
        "MinerU 非表格项保留 %d、MinerU 表格项丢弃 %d",
        len(native_tables), len(kept), dropped,
    )
    return merged, dropped
