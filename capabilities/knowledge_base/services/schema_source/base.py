#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""SchemaSource 协议与 catalog 数据结构。

结构与本仓 `onto-intelligent` 的 Schema IR 对齐（表/列/主键/外键/注释），
因为那是下游生成本体与语义模型时真正消费的形状。

**注释是重点**：中文库的关系真值活在 COMMENT 里，不在声明外键里。
本 POC 实测的 assets_r2：声明外键只有 8 条，而注释里自称关联的有 68 列。
所以这里必须把 comment 原样带出来，不能只当展示字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class CatalogColumn:
    name: str
    # 引擎原始类型串（如 `character varying(128)` / `varchar(128)` / `nvarchar(50)`），
    # 不做归一化——类型映射是下游的事，这里失真了就补不回来
    type: str
    nullable: bool = True
    default: Optional[str] = None
    comment: Optional[str] = None
    is_pk: bool = False


@dataclass
class CatalogForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class CatalogTable:
    name: str
    schema: str = "public"
    comment: Optional[str] = None
    columns: list[CatalogColumn] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[CatalogForeignKey] = field(default_factory=list)


@dataclass
class SchemaCatalog:
    engine: str
    database: str
    tables: list[CatalogTable] = field(default_factory=list)

    def stats(self) -> dict[str, int]:
        cols = sum(len(t.columns) for t in self.tables)
        return {
            "tables": len(self.tables),
            "columns": cols,
            "tables_with_comment": sum(1 for t in self.tables if t.comment),
            "columns_with_comment": sum(
                1 for t in self.tables for c in t.columns if c.comment
            ),
            "declared_fks": sum(len(t.foreign_keys) for t in self.tables),
        }

    def to_dict(self) -> dict[str, Any]:
        """catalog → 可 JSON 化的 dict。

        形状 = 同仓 `jonex_core.capability.atomic.schema_gen` 要的 `catalog` 段
        （原 HTTP `/schema-gen` 契约；下游 `to_tbox` 认的就是这套键名），
        所以**改这里等于改生成器输入契约**。
        读取端与生成端共用它，免得两处序列化慢慢漂移。
        """
        return {
            "engine": self.engine,
            "database": self.database,
            "stats": self.stats(),
            "tables": [
                {
                    "name": t.name,
                    "schema": t.schema,
                    "comment": t.comment,
                    "primary_key": t.primary_key,
                    "foreign_keys": [
                        {"column": f.column, "ref_table": f.ref_table, "ref_column": f.ref_column}
                        for f in t.foreign_keys
                    ],
                    "columns": [
                        {
                            "name": c.name,
                            "type": c.type,
                            "nullable": c.nullable,
                            "default": c.default,
                            "comment": c.comment,
                            "is_pk": c.is_pk,
                        }
                        for c in t.columns
                    ],
                }
                for t in self.tables
            ],
        }


class SourceConnectionError(Exception):
    """连接/认证失败（适配器层统一异常）。"""


class SourceQueryError(Exception):
    """查询/目录读取失败；消息里只放可读原文，不含凭据。"""


@runtime_checkable
class SchemaSource(Protocol):
    """数据库数据源的 catalog 读取协议。

    只有两个方法——比 `IngestionAdapter` 少一个 `fetch_bytes`，
    因为这里**从不读业务数据**：只查 information_schema / pg_catalog。
    """

    engine: str

    async def test_connection(self, cfg: dict[str, Any]) -> dict:
        """连通性探测。返回 {ok, message, database?, version?}。"""
        ...

    async def read_catalog(self, cfg: dict[str, Any]) -> SchemaCatalog:
        """读表/列/主键/外键/注释。**不读业务数据行。**"""
        ...

    async def run_query(self, cfg: dict[str, Any], sql: str, *, timeout: float) -> dict:
        """跑一条**只读** SQL（安全判定在 `services/nl2sql/guard.py`，不在这一层）。"""
        ...


__all__ = [
    "CatalogColumn",
    "CatalogForeignKey",
    "CatalogTable",
    "SchemaCatalog",
    "SchemaSource",
    "SourceConnectionError",
    "SourceQueryError",
]
