# -*- coding: utf-8 -*-
"""指标编译影子对比（W3.3 M4）：命中、差异口径与落库。

只做**纯函数 + append-only 写**；调度与执行编排在 `nl2sql/service.py`
（那里才有数据源与 schema 读取依赖）。本模块不读 schema、不执行 SQL。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from jonex_core.common.database import get_db_session

from ...repository.nl2sql_metric_shadow_repository import (
    Nl2SqlMetricShadowRepository,
)

logger = logging.getLogger(__name__)

#: `pick_shadow_metric` 的三态。
PICK_OK = "ok"
PICK_NOT_EXECUTABLE = "not_executable"
PICK_NONE = "no_metric"


@dataclass(frozen=True)
class ShadowPick:
    status: str
    metric: dict | None = None
    name: str = ""
    match_kind: str = ""
    matched_term: str = ""


def pick_shadow_metric(question: str, metrics) -> ShadowPick:
    """按现有指标匹配管道取**第一条带 executable** 的命中。

    没有命中 → `no_metric`；有命中但都没定义 → `not_executable`（带上第一条
    命中的名字，报告里能看出「认出来了但没定义」与「根本没认出」的差别）。
    """
    from ..concept_expansion import match_metrics

    hits = match_metrics(question, metrics)
    if not hits:
        return ShadowPick(status=PICK_NONE)
    first = hits[0]
    first_metric = first["metric"]
    if isinstance(first_metric, dict) and isinstance(
            first_metric.get("executable"), dict):
        return ShadowPick(
            status=PICK_OK, metric=first_metric,
            name=str(first.get("name") or ""),
            match_kind=str(first.get("match_kind") or ""),
            matched_term=str(first.get("matched_term") or ""),
        )
    for hit in hits:
        metric = hit.get("metric")
        if isinstance(metric, dict) and isinstance(metric.get("executable"), dict):
            return ShadowPick(
                status=PICK_OK, metric=metric,
                name=str(hit.get("name") or ""),
                match_kind=str(hit.get("match_kind") or ""),
                matched_term=str(hit.get("matched_term") or ""),
            )
    return ShadowPick(
        status=PICK_NOT_EXECUTABLE,
        name=str(first.get("name") or ""),
        match_kind=str(first.get("match_kind") or ""),
        matched_term=str(first.get("matched_term") or ""),
    )


def compare_results(
    *,
    main_outcome: str,
    main_row_count: int | None,
    main_cell: tuple[bool, object] = (False, None),
    shadow_row_count: int | None,
    shadow_cell: tuple[bool, object] = (False, None),
) -> str:
    """两侧结果的差异口径（不比较原始值是否落库，只回标签）。

    `*_cell` 是 `(是否存在单元格, 值)`：只有 1×1 结果才存在。用显式布尔而不是
    `None` 判缺失 —— 单元格本身可能就是 NULL。
    """
    shadow_ok = shadow_row_count is not None
    if main_outcome != "success":
        return "main_failed_shadow_ok" if shadow_ok else "both_failed"
    if not shadow_ok:
        return "not_comparable"
    if (main_row_count or 0) == 0 and int(shadow_row_count or 0) == 0:
        return "both_empty"
    if int(main_row_count or 0) != int(shadow_row_count or 0):
        return "row_count_mismatch"
    if main_cell[0] and shadow_cell[0]:
        return ("equal_scalar"
                if _norm_scalar(main_cell[1]) == _norm_scalar(shadow_cell[1])
                else "value_mismatch")
    return "unknown"


def _norm_scalar(value) -> str:
    """标量归一：NULL → 空串；数字按 Decimal 规范化（10 / 10.0 / 3.00 相等）。

    只做「看起来是数字」的归一，非数字原样 strip —— 不能把文本 `001` 和 `1`
    当成相等（那会制造假 `equal_scalar`）。
    """
    if value is None:
        return ""
    text = str(value).strip()
    try:
        return str(Decimal(text).normalize())
    except (InvalidOperation, ValueError):
        return text


def column_names(result: dict | None) -> list[str]:
    """适配器结果 → 列名列表（Postgres/MySQL/MSSQL 是 `list[str]`，OLAP 是 `list[dict]`）。"""
    out: list[str] = []
    for column in (result or {}).get("columns") or []:
        if isinstance(column, dict):
            name = column.get("name")
        else:
            name = column
        out.append(str(name or ""))
    return out


def first_cell(result: dict | None) -> tuple[bool, object]:
    """1×1 结果的首单元格；否则 `(False, None)`。"""
    if not isinstance(result, dict):
        return False, None
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    if len(columns) != 1 or len(rows) != 1:
        return False, None
    row = rows[0]
    if not isinstance(row, (list, tuple)) or len(row) != 1:
        return False, None
    return True, row[0]


async def record(entry: dict) -> None:
    """落一行影子对比；**任何异常只 warning、绝不冒泡**（旁路）。"""
    try:
        async with get_db_session() as session:
            await Nl2SqlMetricShadowRepository(session).add(**entry)
    except Exception as exc:  # noqa: BLE001 —— 影子绝不冒泡
        logger.warning("[metric-shadow] 写入失败（已吞）: %s", exc)


__all__ = [
    "PICK_NONE",
    "PICK_NOT_EXECUTABLE",
    "PICK_OK",
    "ShadowPick",
    "column_names",
    "compare_results",
    "first_cell",
    "pick_shadow_metric",
    "record",
]
