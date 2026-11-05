#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""W1.3 InfluxDB v3 适配器（SQL over HTTP，不依赖厂商 SDK）。

- 连接：`POST {url}/api/v3/query_sql`，body `{db, q, format:"json"}`，Bearer token；
- catalog：`SHOW TABLES` + `SHOW COLUMNS FROM "<measurement>"`；
- 时序能力位：`requires_time_filter=True`（护栏在 W4 落地，本层只声明）；
- 已知限制：空结果集拿不到列名（JSON 行对象没有键），此时 `columns=[]`。
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from .base import (
    CatalogColumn, CatalogTable, SchemaCatalog, SourceQueryError,
)


class InfluxV3SchemaSource:
    engine = "influx_v3"

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._transport = transport

    @staticmethod
    def _settings(cfg: dict[str, Any]) -> dict[str, Any]:
        url = str(cfg.get("url") or "").strip()
        if not url:
            host = str(cfg.get("host") or "127.0.0.1")
            port = int(cfg.get("port") or 8181)
            url = f"http://{host}:{port}"
        return {
            "url": url.rstrip("/"),
            "db": str(cfg.get("database") or ""),
            "token": str(cfg.get("token") or cfg.get("password") or ""),
        }

    async def _query(self, cfg: dict[str, Any], sql: str, timeout: float) -> list[dict]:
        s = self._settings(cfg)
        headers = {"Authorization": f"Bearer {s['token']}"} if s["token"] else {}
        try:
            async with httpx.AsyncClient(
                base_url=s["url"], timeout=httpx.Timeout(timeout),
                transport=self._transport,
            ) as client:
                resp = await client.post(
                    "/api/v3/query_sql",
                    json={"db": s["db"], "q": sql, "format": "json"},
                    headers=headers,
                )
        except Exception as exc:  # noqa: BLE001
            raise SourceQueryError(
                f"InfluxDB 连接失败：{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise SourceQueryError(
                f"InfluxDB 查询失败（HTTP {resp.status_code}）：{resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceQueryError("InfluxDB 返回不是 JSON") from exc
        if not isinstance(data, list):
            raise SourceQueryError("InfluxDB 返回形状不是行数组")
        return data

    async def test_connection(self, cfg: dict[str, Any]) -> dict:
        try:
            await self._query(cfg, "SELECT 1", 10.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)[:300]}
        return {"ok": True, "message": "连接成功",
                "database": self._settings(cfg)["db"], "version": ""}

    async def read_catalog(self, cfg: dict[str, Any]) -> SchemaCatalog:
        db = self._settings(cfg)["db"]
        tables = await self._query(cfg, "SHOW TABLES", 15.0)
        out: list[CatalogTable] = []
        for row in tables:
            # v3 的 SHOW TABLES 会把 system schema 的内部表（tables/columns/queries…）
            # 一起返回；只收用户 measurement（table_schema=iox，非 system）。
            if str(row.get("table_schema") or "").lower() in ("system", "information_schema"):
                continue
            name = str(row.get("table_name") or row.get("name") or "").strip()
            if not name:
                continue
            table = CatalogTable(name=name, schema=str(row.get("table_schema") or db),
                                 comment=None, primary_key=[], foreign_keys=[])
            try:
                cols = await self._query(cfg, f'SHOW COLUMNS FROM "{name}"', 15.0)
            except SourceQueryError:
                cols = []
            for col in cols:
                col_name = str(col.get("column_name") or "").strip()
                if not col_name:
                    continue
                table.columns.append(CatalogColumn(
                    name=col_name,
                    type=str(col.get("data_type") or ""),
                    nullable=str(col.get("is_nullable") or "").upper() != "NO",
                    comment=None, is_pk=col_name == "time",
                ))
            out.append(table)
        return SchemaCatalog(engine=self.engine, database=db, tables=out)

    async def run_query(self, cfg: dict[str, Any], sql: str, *, timeout: float) -> dict:
        rows = await self._query(cfg, sql, timeout)
        columns = []
        if rows:
            columns = [{"name": str(k), "type": ""} for k in rows[0].keys()]
        return {
            "columns": columns,
            "rows": [[_jsonable(r.get(c["name"])) for c in columns] for r in rows],
            "row_count": len(rows),
        }


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = ["InfluxV3SchemaSource"]
