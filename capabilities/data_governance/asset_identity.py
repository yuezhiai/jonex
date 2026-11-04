"""派生资产 ID 构造/解析（W2.1）。

格式见 spec §2；一律按**首个冒号**切 scope，其余原样保留。
"""
from __future__ import annotations

from jonex_core.common.exceptions import InvalidParameterError

DERIVED_TYPES = frozenset({"table", "column", "metric"})


def table_asset_id(data_source_id: str, table: str) -> str:
    return f"{data_source_id}:{table}"


def column_asset_id(data_source_id: str, table: str, column: str) -> str:
    return f"{data_source_id}:{table}.{column}"


def metric_asset_id(knowledge_base_id: str, name: str) -> str:
    return f"{knowledge_base_id}:{name}"


def split_asset_id(asset_type: str, asset_id: str) -> tuple[str, str, str]:
    """返回 `(scope_id, object_id, sub_id)`。

    table:  (ds_id, table, "")
    column: (ds_id, table, column) —— 按**最后一个点**切列名（表名可能带 schema 点）
    metric: (kb_id, name, "")

    列名不得含 `.`；含点时按最后一点切分。
    """
    if not isinstance(asset_type, str) or not isinstance(asset_id, str):
        raise InvalidParameterError(
            message="invalid_asset_id",
            details={"asset_type": type(asset_type).__name__, "asset_id": type(asset_id).__name__},
        )
    value = asset_id
    if asset_type not in DERIVED_TYPES or ":" not in value:
        raise InvalidParameterError(
            message="invalid_asset_id", details={"asset_type": asset_type, "asset_id": asset_id},
        )
    scope, rest = value.split(":", 1)
    if not scope or not rest:
        raise InvalidParameterError(
            message="invalid_asset_id", details={"asset_type": asset_type, "asset_id": asset_id},
        )
    if asset_type == "column":
        if "." not in rest:
            raise InvalidParameterError(
                message="invalid_asset_id",
                details={"asset_type": asset_type, "asset_id": asset_id},
            )
        table, column = rest.rsplit(".", 1)
        if not table or not column:
            raise InvalidParameterError(
                message="invalid_asset_id",
                details={"asset_type": asset_type, "asset_id": asset_id},
            )
        return scope, table, column
    return scope, rest, ""
