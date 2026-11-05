"""OpenLineage RunEvent → 列级五元组（纯函数，无 DB）。

只认 `eventType == "COMPLETE"`：START/FAIL/ABORT 不产生边（重复到达是安全的）。
列级来源是 `outputs[].facets.columnLineage.fields`（OpenLineage 标准 facet）；
没有该 facet 的事件**不产生任何边**（表级血缘不进图）。
"""
from __future__ import annotations

from jonex_core.common.exceptions import InvalidParameterError


def _as_dict(value, path: str) -> dict:
    """容器类型闸门：缺省（None）视为空 dict；类型不符 ⇒ 1001。

    事件来自公开客户端，形状不可信。若不显式校验，`facets: "x"` 这类形状会以
    AttributeError/TypeError 冒成能力 500（1000），客户端看不出是自己发错了。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidParameterError(f"OpenLineage 事件字段类型错误: {path}")
    return value


def _as_list(value, path: str) -> list:
    """容器类型闸门：缺省（None）视为空列表；类型不符 ⇒ 1001。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidParameterError(f"OpenLineage 事件字段类型错误: {path}")
    return value


def _dataset_table(dataset: dict) -> str:
    """dataset → 物理表名。

    规则（确定性，宁可错配被校验拦下，也不做模糊猜测）：
    - `name` 已带 ``schema.table`` ⇒ 原样；
    - `name` 不带点但 namespace 有值 ⇒ 取 namespace 最后一段作 schema 前缀，
      **仅当这一段是纯标识符**（不含 ``:`` / ``/``）。``postgres://host:5432``
      的末段是 ``host:5432``——把端口当 schema 会拼出 ``5432.orders``，
      这种 namespace 没有 schema 信息，返回裸表名让校验如实拒绝。
    """
    name = str((dataset or {}).get("name") or "").strip()
    ns = str((dataset or {}).get("namespace") or "").strip()
    if name and "." not in name and ns:
        tail = ns.rstrip("/").rsplit("/", 1)[-1]
        if tail and ":" not in tail and "/" not in tail:
            return f"{tail}.{name}"
    return name


def parse_openlineage_event(event: dict, *, allowed_namespace: str | None = None) -> list[dict]:
    if not isinstance(event, dict):
        raise InvalidParameterError("OpenLineage 事件必须是 JSON 对象")
    if str(event.get("eventType") or "").upper() != "COMPLETE":
        return []
    rows: list[dict] = []
    for idx, out in enumerate(_as_list(event.get("outputs"), "outputs")):
        if not isinstance(out, dict):
            continue  # 列表里的坏条目（不是对象）⇒ 跳过这一条，不影响其余
        base = f"outputs[{idx}]"
        out_ns = str(out.get("namespace") or "")
        if allowed_namespace and out_ns and out_ns != allowed_namespace:
            continue
        facets = _as_dict(out.get("facets"), f"{base}.facets")
        column_lineage = _as_dict(
            facets.get("columnLineage"), f"{base}.facets.columnLineage")
        fields = _as_dict(
            column_lineage.get("fields"), f"{base}.facets.columnLineage.fields")
        out_table = _dataset_table(out)
        for out_field, spec in fields.items():
            field_path = f"{base}.facets.columnLineage.fields[{out_field!r}]"
            spec = _as_dict(spec, field_path)
            input_fields = _as_list(spec.get("inputFields"), f"{field_path}.inputFields")
            for inp in input_fields:
                if not isinstance(inp, dict):
                    continue  # 同上：列表里的坏条目跳过
                in_ns = str(inp.get("namespace") or "")
                if allowed_namespace and in_ns and in_ns != allowed_namespace:
                    continue
                rows.append({
                    "from_table": _dataset_table(inp),
                    "from_column": str(inp.get("field") or "").strip(),
                    "to_table": out_table,
                    "to_column": str(out_field or "").strip(),
                })
    return [r for r in rows if r["from_table"] and r["from_column"]
            and r["to_table"] and r["to_column"]]
