#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MySQL / MariaDB catalog 读取。

与 PostgreSQL 的两处关键差异（踩过才知道）：

1. **外键的目标表就在 `key_column_usage` 上**（`REFERENCED_TABLE_NAME` /
   `REFERENCED_COLUMN_NAME`）。MySQL 没有 `constraint_column_usage` 这个视图，
   照抄 PG 的查询会直接报表不存在。
2. **注释在 information_schema 里**（`TABLE_COMMENT` / `COLUMN_COMMENT`），
   不像 PG 要走 `obj_description()` / `col_description()`。

同样只查 information_schema，不碰业务表。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import CatalogColumn, CatalogForeignKey, CatalogTable, SchemaCatalog

logger = logging.getLogger(__name__)

# 建表时没写注释的列，MySQL 会给空串而不是 NULL
_Q_TABLES = """
SELECT TABLE_NAME AS name, TABLE_COMMENT AS comment
  FROM information_schema.TABLES
 WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
 ORDER BY TABLE_NAME
"""

_Q_COLUMNS = """
SELECT TABLE_NAME AS table_name,
       COLUMN_NAME AS name,
       COLUMN_TYPE AS type,
       IS_NULLABLE AS nullable,
       COLUMN_DEFAULT AS default_expr,
       COLUMN_COMMENT AS comment
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = %s
 ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

_Q_PKS = """
SELECT TABLE_NAME AS table_name, COLUMN_NAME AS name
  FROM information_schema.KEY_COLUMN_USAGE
 WHERE TABLE_SCHEMA = %s AND CONSTRAINT_NAME = 'PRIMARY'
 ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

# 目标表列就在同一张视图上——MySQL 没有 constraint_column_usage
_Q_FKS = """
SELECT TABLE_NAME AS table_name,
       COLUMN_NAME AS column_name,
       REFERENCED_TABLE_NAME AS ref_table,
       REFERENCED_COLUMN_NAME AS ref_column
  FROM information_schema.KEY_COLUMN_USAGE
 WHERE TABLE_SCHEMA = %s
   AND REFERENCED_TABLE_NAME IS NOT NULL
 ORDER BY TABLE_NAME, ORDINAL_POSITION
"""


def _clean(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return " ".join(str(s).split()).strip() or None


class MySQLSchemaSource:
    engine = "mysql"

    @staticmethod
    def _database(cfg: dict[str, Any]) -> str:
        return str(cfg.get("database") or "").strip()

    async def _connect(self, cfg: dict[str, Any]):
        import aiomysql

        return await aiomysql.connect(
            host=cfg.get("host"),
            port=int(cfg.get("port") or 3306),
            user=cfg.get("user"),
            password=cfg.get("password") or "",
            db=self._database(cfg),
            connect_timeout=int(cfg.get("timeout") or 10),
            charset="utf8mb4",
        )

    async def _fetch(self, cur, sql: str, db: str) -> list[dict]:
        await cur.execute(sql, (db,))
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row)) for row in await cur.fetchall()]

    async def test_connection(self, cfg: dict[str, Any]) -> dict:
        try:
            conn = await self._connect(cfg)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT DATABASE() AS db, VERSION() AS ver")
                row = await cur.fetchone()
                return {"ok": True, "message": "连接成功", "database": row[0], "version": row[1]}
        finally:
            conn.close()

    async def run_query(self, cfg: dict[str, Any], sql: str, *, timeout: float) -> dict:
        """跑一条**只读** SQL 并取回列名与行。

        安全关（单语句 / 只读 / LIMIT）已在 `services/nl2sql/guard.py` 把过，
        这里不再判 —— 重复判据只会分叉。超时走连接参数。
        """
        conn = await self._connect({**cfg, "timeout": timeout})
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql)
                columns = [d[0] for d in (cur.description or [])]
                rows = [list(r) for r in await cur.fetchall()]
            return {"columns": columns, "rows": rows, "row_count": len(rows)}
        finally:
            conn.close()

    async def read_catalog(self, cfg: dict[str, Any]) -> SchemaCatalog:
        db = self._database(cfg)
        conn = await self._connect(cfg)
        try:
            async with conn.cursor() as cur:
                tables: dict[str, CatalogTable] = {}
                for r in await self._fetch(cur, _Q_TABLES, db):
                    tables[r["name"]] = CatalogTable(
                        name=r["name"], schema=db, comment=_clean(r.get("comment"))
                    )

                for r in await self._fetch(cur, _Q_COLUMNS, db):
                    t = tables.get(r["table_name"])
                    if t is None:
                        continue
                    t.columns.append(
                        CatalogColumn(
                            name=r["name"],
                            type=r["type"],
                            nullable=(str(r.get("nullable") or "YES").upper() == "YES"),
                            default=r.get("default_expr"),
                            comment=_clean(r.get("comment")),
                        )
                    )

                for r in await self._fetch(cur, _Q_PKS, db):
                    if (t := tables.get(r["table_name"])) is not None:
                        t.primary_key.append(r["name"])

                for r in await self._fetch(cur, _Q_FKS, db):
                    if (t := tables.get(r["table_name"])) is not None:
                        t.foreign_keys.append(
                            CatalogForeignKey(
                                column=r["column_name"],
                                ref_table=r["ref_table"],
                                ref_column=r["ref_column"],
                            )
                        )

                for t in tables.values():
                    for c in t.columns:
                        if c.name in t.primary_key:
                            c.is_pk = True
                            c.nullable = False

                return SchemaCatalog(engine=self.engine, database=db, tables=list(tables.values()))
        finally:
            conn.close()
