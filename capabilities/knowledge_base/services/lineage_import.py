"""血缘文件导入：JSON/CSV 解析 + 行归一（纯函数，无 DB）。

JSON 形状与 `nl2sql/lineage_source.py` 的文件加载器一致（`{"edges": [...]}` 或裸 list）；
CSV 表头固定 `from_table,from_column,to_table,to_column[,note]`。
"""
from __future__ import annotations

import csv
import io
import json

from jonex_core.common.exceptions import InvalidParameterError

_EDGE_FIELDS = ("from_table", "from_column", "to_table", "to_column")
_CSV_HEADERS = (*_EDGE_FIELDS, "note")

# 与迁移 010 的 DDL 对齐：端点四列 VARCHAR(255)、note VARCHAR(500)。
# 超长若留到 commit 才炸，撞的是数据库错误 ⇒ 整批 500（不是单行 errors）。
# 人工录入（`lineage_edge_service.normalize_edge_payload`）复用同一对常量，
# 两条写入口的长度口径只有一处。
MAX_ENDPOINT_LEN = 255
MAX_NOTE_LEN = 500


def _parse_json(text: str) -> list[dict]:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise InvalidParameterError(f"文件不是合法 JSON：{exc}") from exc
    rows = raw.get("edges") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise InvalidParameterError("JSON 顶层必须是数组或 {edges: [...]}")
    return [r for r in rows if isinstance(r, dict)]


def _parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or any(h not in reader.fieldnames for h in _EDGE_FIELDS):
        raise InvalidParameterError(
            f"CSV 表头必须包含 {','.join(_EDGE_FIELDS)}（可选 note）")
    return [dict(r) for r in reader]


def parse_edges(filename: str, text: str) -> list[dict]:
    """按扩展名解析 → 只保留 dict 行；不在这里校验列存在性（那要连库）。"""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return _parse_csv(text)
    if name.endswith(".json"):
        return _parse_json(text)
    raise InvalidParameterError("只支持 .json / .csv 血缘文件")


def normalize_import_payload(data: dict, data_source_id: str) -> dict:
    """导入行的归一：四列 strip 后非空；**origin 一律 platform**（ETL 观测，0.6）。

    与人工录入的 `normalize_edge_payload` 刻意分开：那边压成 manual（人的声明），
    这边压成 platform（机器来源），两者都不是 UI 可选项。

    **长度在逐行这里挡**（审查 M-L 级：DDL 是端点 `VARCHAR(255)` / `note`
    `VARCHAR(500)`；超长留到 commit 才炸 ⇒ `IntegrityError`/`DataError` 把整批
    打成 500，而不是「单行坏、其余照常」）。抛 `InvalidParameterError` 由批量
    循环计入 `errors`。
    """
    got = {f: str((data or {}).get(f) or "").strip() for f in _EDGE_FIELDS}
    missing = [f for f, v in got.items() if not v]
    if missing:
        raise InvalidParameterError(f"血缘边的端点字段不能为空：{'、'.join(missing)}")
    for f, v in got.items():
        if len(v) > MAX_ENDPOINT_LEN:
            raise InvalidParameterError(
                f"{f} 超长（最多 {MAX_ENDPOINT_LEN} 字符，本次 {len(v)}）")
    note = (str((data or {}).get("note") or "").strip() or None)
    if note is not None and len(note) > MAX_NOTE_LEN:
        raise InvalidParameterError(
            f"note 超长（最多 {MAX_NOTE_LEN} 字符，本次 {len(note)}）")
    got["origin"] = "platform"
    got["note"] = note
    got["data_source_id"] = str(data_source_id or "").strip()
    return got


__all__ = ["parse_edges", "normalize_import_payload"]
