#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""PostgreSQL catalog 读取。

只查 pg_catalog，**不 SELECT 任何业务表**。取数思路移植自
`onto-intelligent/src/introspect.py`（那版在 assets_r2 的 89 张表上跑过），
并在注释上加了保险：中文库里 COMMENT 是关系与枚举的真值来源，
丢了它下游就只能看到 8 条声明外键。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import CatalogColumn, CatalogForeignKey, CatalogTable, SchemaCatalog

logger = logging.getLogger(__name__)

_Q_TABLES = """
SELECT c.relname AS name,
       obj_description(c.oid, 'pg_class') AS comment
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'r'
   AND n.nspname = ANY($1::text[])
 ORDER BY c.relname
"""

_Q_COLUMNS = """
SELECT c.relname AS table_name,
       a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS type,
       NOT a.attnotnull AS nullable,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr,
       col_description(a.attrelid, a.attnum) AS comment
  FROM pg_attribute a
  JOIN pg_class c     ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE c.relkind = 'r'
   AND n.nspname = ANY($1::text[])
   AND a.attnum > 0
   AND NOT a.attisdropped
 ORDER BY c.relname, a.attnum
"""

_Q_PKS = """
SELECT c.relname AS table_name, a.attname AS name
  FROM pg_index i
  JOIN pg_class c     ON c.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
 WHERE i.indisprimary
   AND n.nspname = ANY($1::text[])
 ORDER BY c.relname, array_position(i.indkey, a.attnum)
"""

# 用 pg_constraint 而非 information_schema：后者在非默认 schema 或权限受限时容易漏
_Q_FKS = """
SELECT c.relname  AS table_name,
       fa.attname AS column_name,
       fc.relname AS ref_table,
       fna.attname AS ref_column
  FROM pg_constraint con
  JOIN pg_class c       ON c.oid = con.conrelid
  JOIN pg_namespace n   ON n.oid = c.relnamespace
  JOIN pg_class fc      ON fc.oid = con.confrelid
  JOIN pg_attribute fa  ON fa.attrelid = con.conrelid  AND fa.attnum = con.conkey[1]
  JOIN pg_attribute fna ON fna.attrelid = con.confrelid AND fna.attnum = con.confkey[1]
 WHERE con.contype = 'f'
   AND n.nspname = ANY($1::text[])
 ORDER BY c.relname, fa.attname
"""


def _clean(s: Optional[str]) -> Optional[str]:
    """注释压成单行。

    换行会让 YAML 里的描述难读，也会把下游 BM25 的短语匹配打散——
    本 POC 实测过这个代价。
    """
    if not s:
        return None
    return " ".join(str(s).split()).strip() or None


class PostgresSchemaSource:
    engine = "postgresql"

    @staticmethod
    def _schemas(cfg: dict[str, Any]) -> list[str]:
        raw = cfg.get("schemas") or cfg.get("schema") or ["public"]
        if isinstance(raw, str):
            raw = [raw]
        return [s for s in (str(x).strip() for x in raw) if s] or ["public"]

    async def _connect(self, cfg: dict[str, Any]):
        import asyncpg

        return await asyncpg.connect(
            host=cfg.get("host"),
            port=int(cfg.get("port") or 5432),
            user=cfg.get("user"),
            password=cfg.get("password") or "",
            database=cfg.get("database"),
            timeout=float(cfg.get("timeout") or 10),
        )

    async def test_connection(self, cfg: dict[str, Any]) -> dict:
        try:
            conn = await self._connect(cfg)
        except Exception as e:  # noqa: BLE001 — 探测失败要回可读信息，不该抛
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}
        try:
            row = await conn.fetchrow("SELECT current_database() AS db, version() AS ver")
            return {
                "ok": True,
                "message": "连接成功",
                "database": row["db"],
                "version": str(row["ver"]).split(",")[0],
            }
        finally:
            await conn.close()

    async def run_query(self, cfg: dict[str, Any], sql: str, *, timeout: float) -> dict:
        """跑一条**只读** SQL 并取回列名与行。

        安全关（单语句 / 只读 / LIMIT）已在 `services/nl2sql/guard.py` 把过，
        这里不再判 —— 重复判据只会分叉。超时走连接参数。

        走 `prepare` 而不是 `conn.fetch`：`fetch` 在**空结果集**时没有任何 Row 可以读
        `keys()`，列名随之丢失；prepared statement 的 `get_attributes()` 恒有列名，
        与 mysql / sqlserver 的 `cur.description` 对齐（下游按 `columns` 拼表头）。
        代价与 `conn.fetch` 相同：都一样是 Parse+Describe 之后 Bind/Execute，不多一次往返。
        """
        conn = await self._connect({**cfg, "timeout": timeout})
        try:
            stmt = await conn.prepare(sql)
            records = await stmt.fetch()
            return {
                "columns": [a.name for a in stmt.get_attributes()],
                "rows": [list(r.values()) for r in records],
                "row_count": len(records),
            }
        finally:
            await conn.close()

    async def read_catalog(self, cfg: dict[str, Any]) -> SchemaCatalog:
        schemas = self._schemas(cfg)
        conn = await self._connect(cfg)
        try:
            tables: dict[str, CatalogTable] = {}
            for r in await conn.fetch(_Q_TABLES, schemas):
                tables[r["name"]] = CatalogTable(
                    name=r["name"], schema=schemas[0], comment=_clean(r["comment"])
                )

            for r in await conn.fetch(_Q_COLUMNS, schemas):
                t = tables.get(r["table_name"])
                if t is None:
                    continue
                t.columns.append(
                    CatalogColumn(
                        name=r["name"],
                        type=r["type"],
                        nullable=bool(r["nullable"]),
                        default=r["default_expr"],
                        comment=_clean(r["comment"]),
                    )
                )

            for r in await conn.fetch(_Q_PKS, schemas):
                if (t := tables.get(r["table_name"])) is not None:
                    t.primary_key.append(r["name"])

            for r in await conn.fetch(_Q_FKS, schemas):
                if (t := tables.get(r["table_name"])) is not None:
                    t.foreign_keys.append(
                        CatalogForeignKey(
                            column=r["column_name"],
                            ref_table=r["ref_table"],
                            ref_column=r["ref_column"],
                        )
                    )

            # 主键列补 is_pk 并强制非空（以主键语义为准，不信 catalog 的历史遗留）
            for t in tables.values():
                for c in t.columns:
                    if c.name in t.primary_key:
                        c.is_pk = True
                        c.nullable = False

            db = await conn.fetchval("SELECT current_database()")
            return SchemaCatalog(engine=self.engine, database=db, tables=list(tables.values()))
        finally:
            await conn.close()
