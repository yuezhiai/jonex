#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""数据库数据源的 catalog 读取（SchemaSource）。

与 `services/ingestion/` 的区别：那一套是**文档导向**的
（`list_remote_files` → `fetch_bytes` → 喂给解析链路），
而数据库数据源**不列文件、不取字节**——它读 catalog 产出 schema 定义。

所以这里另立一套协议，而不是把数据库硬套进文件语义里。
两者共用 `knowledge_data_sources` 表与列表页，靠 `access_type` 分流。
"""

from __future__ import annotations

from typing import Any, Optional

from .base import CatalogColumn, CatalogForeignKey, CatalogTable, SchemaCatalog, SchemaSource

# access_type == "database" 时，按 engine 选实现
_ENGINES: dict[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "pg": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlserver": "sqlserver",
    "mssql": "sqlserver",
    # W1：OLAP / 时序
    "clickhouse": "clickhouse",
    "ch": "clickhouse",
    "influx_v3": "influx_v3",
    "influxdb": "influx_v3",
    "influx": "influx_v3",
}


def normalize_engine(engine: str) -> str:
    """把用户填的引擎名归一化到实现键；**未知引擎返回空串**（`""` = 方言不明）。

    ⚠️ **不要**「修」成返回原值。空串是一个**有意义的档位**，不是「没归一化成功」的
    残渣 —— 它是 `guard_sql` 那个 fail-closed 的支点：

      - `guard_sql` 拿归一化后的键判「这个写法是不是这个方言的」，判不出来时按
        `_UNKNOWN_CAP_FORMS` 兜底（追加 `LIMIT n`）；而 `#` 那道闸的 `_HASH_REJECT_REASONS`
        里，**只有空串这一档代表「方言不明 → 也拒」**（`_HASH_REJECT_REASONS.get(key)`
        取不到就是放行）。
      - 未知引擎若原样透出（`"sqlite"` / 拼错的 `"postgress"` / 管理员随手填的别名），
        `.get("sqlite")` 回 None ⇒ `#` 直接放行。而追加的 `LIMIT n` 落在 `#` 行注释里，
        就是「报有上限、实际没上限」—— 安全层最不能有的形状。

    取不到实现（`get_schema_source` 回 None）由**调用方**报 `InvalidParameterError`
    （见 `db_config.catalog_source_for`），错误信息里带的是用户填的**原值**。
    """
    return _ENGINES.get((engine or "").strip().lower(), "")


def get_schema_source(engine: str) -> Optional[SchemaSource]:
    """按引擎取实现。未知引擎返回 None，由调用方给出可读错误。"""
    key = normalize_engine(engine)
    if key == "postgres":
        from .postgres import PostgresSchemaSource

        return PostgresSchemaSource()
    if key == "mysql":
        from .mysql import MySQLSchemaSource

        return MySQLSchemaSource()
    if key == "sqlserver":
        from .sqlserver import SQLServerSchemaSource

        return SQLServerSchemaSource()
    if key == "clickhouse":
        from .clickhouse import ClickHouseSchemaSource

        return ClickHouseSchemaSource()
    if key == "influx_v3":
        from .influx import InfluxV3SchemaSource

        return InfluxV3SchemaSource()
    return None


def supported_engines() -> list[str]:
    return ["postgresql", "mysql", "sqlserver", "clickhouse", "influx_v3"]


__all__ = [
    "CatalogColumn",
    "CatalogForeignKey",
    "CatalogTable",
    "SchemaCatalog",
    "SchemaSource",
    "get_schema_source",
    "normalize_engine",
    "supported_engines",
]
