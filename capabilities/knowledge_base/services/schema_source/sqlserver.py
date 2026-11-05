#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""SQL Server catalog 读取。

三处与 PostgreSQL 不同（照抄 PG 的查询会挂）：

1. **注释不在 information_schema 里**，要走扩展属性
   `sys.extended_properties where name='MS_Description'`。
   这是 SQL Server 独有的机制，没有它就等于没有中文注释。
2. **外键要三表连**：`table_constraints` ⋈ `key_column_usage` ⋈
   `constraint_column_usage`。MySQL 没这第三个视图，SQL Server 必须有。
3. **默认 schema 是 `dbo`**，不是 `public`。

驱动用 `pymssql` 而非 pyodbc——后者要在镜像里装 Microsoft 的 ODBC 运行时，
pymssql 自带 FreeTDS，纯 pip 可装。它是同步库，用 to_thread 包成异步。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .base import CatalogColumn, CatalogForeignKey, CatalogTable, SchemaCatalog

logger = logging.getLogger(__name__)

# ⚠️ 注释查询**刻意拆成独立语句**，不与结构查询 JOIN。
#
# 原因：本实现**未能在真实 SQL Server 上验证**（Docker 镜像在几个国内源上都拉不到）。
# 而 SQL Server 的注释要走扩展属性 `MS_Description`——`OBJECT_ID` + `COLUMNPROPERTY`
# 这套写法是标准用法，但没跑过就是没跑过。若把它 JOIN 进列查询，一旦写错，
# **整张表的结构都读不出来**；拆开后最坏情况只是「没注释」，而结构（表/列/主键/外键）
# 仍然完整。对中文库而言丢注释代价很大，但不该大到连表都读不出来。
_Q_TABLES = """
SELECT TABLE_NAME AS name
  FROM INFORMATION_SCHEMA.TABLES
 WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA = %s
 ORDER BY TABLE_NAME
"""

_Q_COLUMNS = """
SELECT TABLE_NAME AS table_name,
       COLUMN_NAME AS name,
       DATA_TYPE + CASE
           WHEN CHARACTER_MAXIMUM_LENGTH IS NOT NULL
                THEN '(' + CAST(CHARACTER_MAXIMUM_LENGTH AS VARCHAR(10)) + ')'
           WHEN DATA_TYPE IN ('decimal','numeric') AND NUMERIC_PRECISION IS NOT NULL
                THEN '(' + CAST(NUMERIC_PRECISION AS VARCHAR(10)) + ',' +
                     CAST(ISNULL(NUMERIC_SCALE,0) AS VARCHAR(10)) + ')'
           ELSE '' END AS type,
       IS_NULLABLE AS nullable,
       COLUMN_DEFAULT AS default_expr
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = %s
 ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

# 表注释：扩展属性 major_id = 表对象，minor_id = 0
_Q_TABLE_COMMENTS = """
SELECT t.TABLE_NAME AS name, CAST(ep.value AS NVARCHAR(MAX)) AS comment
  FROM INFORMATION_SCHEMA.TABLES t
  JOIN sys.extended_properties ep
    ON ep.major_id = OBJECT_ID(QUOTENAME(t.TABLE_SCHEMA) + '.' + QUOTENAME(t.TABLE_NAME))
   AND ep.minor_id = 0
   AND ep.name = 'MS_Description'
 WHERE t.TABLE_SCHEMA = %s
"""

# 列注释：minor_id = 该列的 ColumnId
_Q_COLUMN_COMMENTS = """
SELECT c.TABLE_NAME AS table_name, c.COLUMN_NAME AS name,
       CAST(ep.value AS NVARCHAR(MAX)) AS comment
  FROM INFORMATION_SCHEMA.COLUMNS c
  JOIN sys.extended_properties ep
    ON ep.major_id = OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME))
   AND ep.minor_id = COLUMNPROPERTY(
         OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)),
         c.COLUMN_NAME, 'ColumnId')
   AND ep.name = 'MS_Description'
 WHERE c.TABLE_SCHEMA = %s
"""

_Q_PKS = """
SELECT kcu.TABLE_NAME AS table_name, kcu.COLUMN_NAME AS name
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
  JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
    ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
   AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
 WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_SCHEMA = %s
 ORDER BY kcu.TABLE_NAME, kcu.ORDINAL_POSITION
"""

# 三表连：constraint_column_usage 给目标表，key_column_usage 给源列
_Q_FKS = """
SELECT kcu.TABLE_NAME AS table_name,
       kcu.COLUMN_NAME AS column_name,
       ccu.TABLE_NAME AS ref_table,
       ccu.COLUMN_NAME AS ref_column
  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
  JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
    ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
  JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu
    ON ccu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME AND ccu.TABLE_SCHEMA = tc.TABLE_SCHEMA
 WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY' AND tc.TABLE_SCHEMA = %s
 ORDER BY kcu.TABLE_NAME, kcu.ORDINAL_POSITION
"""


def _clean(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return " ".join(str(s).split()).strip() or None


#: FreeTDS「TDS 版本谈不拢」的错误码。
_TDS_VERSION_MISMATCH_ERRNO = 20002

#: 退回的 TDS 版本。实测（2026-09-28，一台老库或它前面的代理）**只认这一个档位**：
#: 默认档与 7.1 / 7.2 / 7.3 / 7.4 / 8.0 全在握手段就挂，7.0 能一路走到登录那一步
#: （回的是「用户登录失败」，即握手确实成了）。
_TDS_FALLBACK_VERSION = "7.0"


def _is_tds_handshake_failure(exc: Exception) -> bool:
    """这是不是「TDS 版本谈不拢」那一类失败？

    **只认它**，是因为只有这一类值得换个版本重试。凭据错的形态长得很像、成因完全不同：
    `18456 用户 'xxx' 登录失败` 说明**握手已经成功**，拿错误密码再试一次 7.0 只会把
    一次清晰的认证失败拖成两次连接。

    DB-Lib 的文案按服务器语言本地化（实测见过中文），所以错误码与固定英文短语两个都认
    —— 实测不同档位吐出来的形态并不一致：默认档 errno=20002 且文案里也带 20002，
    而 7.4 档 errno 却是 18456、只有文案里带着 20002。
    """
    args = getattr(exc, "args", ()) or ()
    if args and args[0] == _TDS_VERSION_MISMATCH_ERRNO:
        return True
    text = str(exc)
    return (
        str(_TDS_VERSION_MISMATCH_ERRNO) in text
        or "TDS server connection failed" in text
    )


class SQLServerSchemaSource:
    engine = "sqlserver"

    @staticmethod
    def _schema(cfg: dict[str, Any]) -> str:
        return str(cfg.get("schema") or cfg.get("database_schema") or "dbo").strip() or "dbo"

    @staticmethod
    def _connect(cfg: dict[str, Any]):
        """建一条 TDS 连接（同步库，调用方用 `to_thread` 包成异步）。

        `tds_version` 三条规矩：显式配了就用配的（不做任何猜测重试）；没配先按
        FreeTDS 默认来；**只在握手失败时**退回 7.0 再试一次。现代 SQL Server 走不到
        兜底那一步（默认档就握手成功），所以这条兜底不会把它们的协议降级。
        """
        import pymssql

        kwargs = dict(
            server=cfg.get("host"),
            port=str(int(cfg.get("port") or 1433)),
            user=cfg.get("user"),
            password=cfg.get("password") or "",
            database=cfg.get("database"),
            login_timeout=int(cfg.get("timeout") or 10),
        )
        configured = cfg.get("tds_version")
        if configured:
            return pymssql.connect(**kwargs, tds_version=str(configured))
        try:
            return pymssql.connect(**kwargs)
        except Exception as exc:  # noqa: BLE001 —— 只吞「该换版本」那类，其余原样抛
            if not _is_tds_handshake_failure(exc):
                raise
            logger.warning(
                "TDS 默认版本握不上，退回 %s 重试: host=%s err=%s",
                _TDS_FALLBACK_VERSION, cfg.get("host"), str(exc)[:160],
            )
            return pymssql.connect(**kwargs, tds_version=_TDS_FALLBACK_VERSION)

    def _fetch_sync(self, cfg: dict[str, Any], sql: str, param: str) -> list[dict]:
        conn = self._connect(cfg)
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute(sql, (param,))
            return [
                {(k.lower() if isinstance(k, str) else k): v for k, v in row.items()}
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    async def test_connection(self, cfg: dict[str, Any]) -> dict:
        def _probe() -> dict:
            conn = self._connect(cfg)
            try:
                cur = conn.cursor()
                cur.execute("SELECT DB_NAME(), @@VERSION")
                row = cur.fetchone()
                return {"ok": True, "message": "连接成功", "database": row[0],
                        "version": str(row[1]).split("\n")[0][:80]}
            finally:
                conn.close()

        try:
            return await asyncio.to_thread(_probe)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}

    def _run_sync(self, cfg: dict[str, Any], sql: str) -> dict:
        conn = self._connect(cfg)
        try:
            # 按**位置**取值（`as_dict=False`），不用字典：模型生成的 JOIN 很容易写成
            # `SELECT a.id, b.id`（不给别名），而字典会把同名列折叠成一个键 ——
            # `len(row) < len(columns)`，下游按位置对齐表头时就**静默错位**（值整体挪一格）。
            # 位置元组与 `cur.description` 天然一一对应，列名也照旧从 description 取。
            cur = conn.cursor(as_dict=False)
            cur.execute(sql)
            rows = [list(r) for r in cur.fetchall()]
            columns = [str(d[0]) for d in (cur.description or [])]
            return {"columns": columns, "rows": rows, "row_count": len(rows)}
        finally:
            conn.close()

    async def run_query(self, cfg: dict[str, Any], sql: str, *, timeout: float) -> dict:
        """跑一条**只读** SQL 并取回列名与行。

        安全关（单语句 / 只读 / LIMIT）已在 `services/nl2sql/guard.py` 把过，
        这里不再判 —— 重复判据只会分叉。超时走连接参数。
        `pymssql` 是同步库，沿用本模块既有口径包 `to_thread`。
        """
        return await asyncio.to_thread(self._run_sync, {**cfg, "timeout": timeout}, sql)

    async def _comments(self, cfg: dict[str, Any], sql: str, sch: str) -> list[dict]:
        """注释查询 —— **失败不算整体失败**。

        本实现未在真实 SQL Server 上验证过（镜像拉不到），而这段用的是 SQL Server
        独有的扩展属性机制。宁可丢注释也不要整张表读不出来，所以单独 try。
        """
        try:
            return await asyncio.to_thread(self._fetch_sync, cfg, sql, sch)
        except Exception as e:  # noqa: BLE001
            logger.warning("[sqlserver] 读注释失败（结构不受影响）: %s", e)
            return []

    async def read_catalog(self, cfg: dict[str, Any]) -> SchemaCatalog:
        sch = self._schema(cfg)
        rows_t = await asyncio.to_thread(self._fetch_sync, cfg, _Q_TABLES, sch)
        rows_c = await asyncio.to_thread(self._fetch_sync, cfg, _Q_COLUMNS, sch)
        rows_p = await asyncio.to_thread(self._fetch_sync, cfg, _Q_PKS, sch)
        rows_f = await asyncio.to_thread(self._fetch_sync, cfg, _Q_FKS, sch)
        # 注释在结构之后单独取，失败就降级为「无注释」
        t_comments = {
            r["name"]: _clean(r.get("comment"))
            for r in await self._comments(cfg, _Q_TABLE_COMMENTS, sch)
        }
        c_comments = {
            (r["table_name"], r["name"]): _clean(r.get("comment"))
            for r in await self._comments(cfg, _Q_COLUMN_COMMENTS, sch)
        }

        tables: dict[str, CatalogTable] = {}
        for r in rows_t:
            tables[r["name"]] = CatalogTable(
                name=r["name"], schema=sch, comment=t_comments.get(r["name"])
            )
        for r in rows_c:
            t = tables.get(r["table_name"])
            if t is None:
                continue
            t.columns.append(
                CatalogColumn(
                    name=r["name"],
                    type=str(r["type"]),
                    nullable=(str(r.get("nullable") or "YES").upper() == "YES"),
                    default=r.get("default_expr"),
                    comment=c_comments.get((r["table_name"], r["name"])),
                )
            )
        for r in rows_p:
            if (t := tables.get(r["table_name"])) is not None:
                t.primary_key.append(r["name"])
        for r in rows_f:
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

        return SchemaCatalog(
            engine=self.engine,
            database=str(cfg.get("database") or ""),
            tables=list(tables.values()),
        )
