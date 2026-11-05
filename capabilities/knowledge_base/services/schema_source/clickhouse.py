#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""W1.2 ClickHouse 适配器（HTTP 接口，不依赖厂商 SDK）。

- 连接：`http://{host}:{port}/`，Basic auth（user/password），默认 8123；
- catalog：`system.tables` + `system.columns`（**无外键**；主键取 sorting_key 再退
  primary_key，两者都是逗号分隔的表达式串）；
- 查询：`default_format=JSONCompact`，响应 `{meta:[{name,type}], data:[[...]]}`；
- 安全判定不在这一层（guard 在上层，沿用现状）。
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from .base import (
    CatalogColumn, CatalogTable, SchemaCatalog, SourceQueryError,
)

_Q_TABLES = """
SELECT name, comment, engine, sorting_key, primary_key
  FROM system.tables
 WHERE database = {db:String}
 ORDER BY name
"""

_Q_COLUMNS = """
SELECT table, name, type, comment, default_kind, is_in_primary_key
  FROM system.columns
 WHERE database = {db:String}
 ORDER BY table, position
"""


class ClickHouseSchemaSource:
    engine = "clickhouse"

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._transport = transport

    @staticmethod
    def _settings(cfg: dict[str, Any]) -> dict[str, Any]:
        return {
            "host": str(cfg.get("host") or "127.0.0.1"),
            "port": int(cfg.get("port") or 8123),
            "user": str(cfg.get("user") or "default"),
            "password": str(cfg.get("password") or ""),
            "database": str(cfg.get("database") or "default"),
        }

    def _client(self, cfg: dict[str, Any], timeout: float) -> httpx.AsyncClient:
        s = self._settings(cfg)
        return httpx.AsyncClient(
            base_url=f"http://{s['host']}:{s['port']}",
            auth=(s["user"], s["password"]) if s["user"] else None,
            timeout=httpx.Timeout(timeout),
            transport=self._transport,
        )

    async def _post(self, cfg: dict[str, Any], sql: str, params: dict,
                    timeout: float) -> dict:
        try:
            async with self._client(cfg, timeout) as client:
                resp = await client.post(
                    "/", content=sql,
                    params={**params, "default_format": "JSONCompact",
                            "database": self._settings(cfg)["database"],
                            "output_format_json_quote_64bit_integers": "0"})
        except Exception as exc:  # noqa: BLE001 —— 网络层错误统一成可读异常
            raise SourceQueryError(f"ClickHouse 连接失败：{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise SourceQueryError(
                f"ClickHouse 查询失败（HTTP {resp.status_code}）：{resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceQueryError("ClickHouse 返回不是 JSON") from exc

    async def test_connection(self, cfg: dict[str, Any]) -> dict:
        try:
            data = await self._post(cfg, "SELECT version() AS version", {}, 10.0)
        except Exception as exc:  # noqa: BLE001 —— 探测失败回可读信息，不抛
            return {"ok": False, "message": str(exc)[:300]}
        rows = data.get("data") or []
        version = str(rows[0][0]) if rows and rows[0] else ""
        return {"ok": True, "message": "连接成功",
                "database": self._settings(cfg)["database"], "version": version}

    async def read_catalog(self, cfg: dict[str, Any]) -> SchemaCatalog:
        db = self._settings(cfg)["database"]
        tables_data = await self._post(cfg, _Q_TABLES, {"param_db": db}, 15.0)
        cols_data = await self._post(cfg, _Q_COLUMNS, {"param_db": db}, 15.0)

        by_name: dict[str, CatalogTable] = {}
        for name, comment, _engine, sorting_key, primary_key in tables_data.get("data") or []:
            key = str(sorting_key or "").strip() or str(primary_key or "").strip()
            pk = [p.strip() for p in key.split(",") if p.strip()]
            by_name[str(name)] = CatalogTable(
                name=str(name), schema=db, comment=_clean(comment),
                primary_key=pk, foreign_keys=[],
            )
        for table, name, typ, comment, default_kind, is_pk in cols_data.get("data") or []:
            t = by_name.get(str(table))
            if t is None:
                continue
            t.columns.append(CatalogColumn(
                name=str(name), type=str(typ),
                nullable="Nullable(" in str(typ),
                default=None if not default_kind or str(default_kind) == "" else str(default_kind),
                comment=_clean(comment),
                is_pk=bool(is_pk),
            ))
        return SchemaCatalog(engine=self.engine, database=db,
                             tables=list(by_name.values()))

    async def run_query(self, cfg: dict[str, Any], sql: str, *, timeout: float) -> dict:
        data = await self._post(cfg, sql, {}, timeout)
        meta = data.get("meta") or []
        rows = data.get("data") or []
        columns = [{"name": str(m.get("name") or ""), "type": str(m.get("type") or "")}
                   for m in meta]
        return {"columns": columns, "rows": [list(r) for r in rows],
                "row_count": len(rows)}


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


__all__ = ["ClickHouseSchemaSource"]
