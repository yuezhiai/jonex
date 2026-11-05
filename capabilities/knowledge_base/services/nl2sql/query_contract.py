# -*- coding: utf-8 -*-
"""W1.6 查询结果契约（MVP）：统一信封 + 分页 + 取消 + 缓存 + JSON。

设计：docs/superpowers/specs/2026-11-02-w1.6-query-result-contract-design.md（工作区文档，不入库）。

只服务**只读查询执行**，不改变任何既有调用方：

```json
{"contract_version": "1",
 "columns": [{"name": "id", "type": ""}],
 "column_names": ["id"],
 "rows": [[1]], "row_count": 1, "returned": 1,
 "limit": 10, "offset": 5, "truncated": false, "next_offset": null,
 "elapsed_ms": 3, "cached": false, "result_format": "json",
 "sql_sha256": "…"}
```

- 分页在 `guard_sql` 上做（`offset` 追加，SQL Server 明确拒绝）；
- 取消用 `asyncio.Event`：置位即取消执行任务（适配器不保证服务端查询已终止，
  但调用方不再等待）；
- 缓存**默认关**（`NL2SQL_QUERY_CACHE_TTL_SECONDS=0`），键含 engine + 实际执行 SQL；
- `arrow` 明确未支持（不伪造）：需要 pyarrow + 适配器 Arrow 通道。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import time

from jonex_core.common.config import config

from .guard import guard_sql
from .runner import run_readonly

CONTRACT_VERSION = "1"


class QueryRejected(Exception):
    """查询未执行（安全闸 / 参数 / 不支持的输出格式）。"""

    def __init__(self, reason: str, kind: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


class QueryCancelled(Exception):
    """调用方在查询完成前取消。"""


def _column_name(column) -> str:
    if isinstance(column, dict):
        return str(column.get("name") or column.get("field") or "")
    return str(column or "")


def _column_type(column) -> str:
    return str(column.get("type") or "") if isinstance(column, dict) else ""


def normalize_result(result: dict, *, sql: str, limit: int | None,
                     offset: int = 0, elapsed_ms: int | None = None,
                     cached: bool = False) -> dict:
    raw_columns = list(result.get("columns") or [])
    columns = [{"name": _column_name(column), "type": _column_type(column)}
               for column in raw_columns]
    rows = [list(row or []) if not isinstance(row, dict)
            else [row.get(column["name"]) for column in columns]
            for row in (result.get("rows") or [])]
    returned = len(rows)
    truncated = bool(limit is not None and returned >= int(limit))
    return {
        "contract_version": CONTRACT_VERSION,
        "columns": columns,
        "column_names": [column["name"] for column in columns],
        "rows": rows,
        "row_count": int(result.get("row_count")
                         if result.get("row_count") is not None else returned),
        "returned": returned,
        "limit": int(limit) if limit is not None else None,
        "offset": int(offset or 0),
        "truncated": truncated,
        "next_offset": (int(offset or 0) + returned) if truncated else None,
        "elapsed_ms": elapsed_ms,
        "cached": bool(cached),
        "result_format": "json",
        "sql_sha256": hashlib.sha256(str(sql or "").encode("utf-8")).hexdigest(),
    }


class QueryCache:
    """进程内 TTL 缓存；0 = 关闭（`get` 恒 miss、`put` 不写）。"""

    def __init__(self, ttl: int | None = None, max_entries: int | None = None):
        self.ttl = int(ttl if ttl is not None
                       else config.NL2SQL_QUERY_CACHE_TTL_SECONDS)
        self.max_entries = int(max_entries if max_entries is not None
                               else config.NL2SQL_QUERY_CACHE_MAX_ENTRIES)
        self._store: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def make_key(*, engine: str, sql: str, limit, offset: int) -> str:
        payload = f"{engine}\x00{sql}\x00{limit}\x00{offset}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict | None:
        if self.ttl <= 0:
            return None
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._store.pop(key, None)
            return None
        return copy.deepcopy(value)

    def put(self, key: str, value: dict) -> None:
        if self.ttl <= 0:
            return
        if self.max_entries > 0 and len(self._store) >= self.max_entries:
            oldest = min(self._store,
                         key=lambda item: self._store[item][0])
            self._store.pop(oldest, None)
        self._store[key] = (time.monotonic() + self.ttl, copy.deepcopy(value))

    def clear(self) -> None:
        self._store.clear()


_DEFAULT_CACHE = QueryCache()


def get_default_cache() -> QueryCache:
    return _DEFAULT_CACHE


async def _execute_with_cancel(coro, cancel_event: asyncio.Event | None):
    task = asyncio.create_task(coro)
    if cancel_event is None:
        return await task
    watcher = asyncio.create_task(cancel_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        raise QueryCancelled("查询已取消")
    finally:
        watcher.cancel()


async def run_query_contract(
    source, cfg: dict, sql: str, *, engine: str, max_rows: int,
    offset: int = 0, timeout: float | None = None,
    cancel_event: asyncio.Event | None = None, output: str = "json",
    use_cache: bool = True, cache: QueryCache | None = None,
) -> dict:
    """安全闸 → 分页 → 执行（可取消）→ 统一信封（可选缓存）。"""
    if str(output or "json").lower() != "json":
        raise QueryRejected(
            "只支持 json 输出；arrow 需要 pyarrow 与适配器 Arrow 通道",
            kind="format")
    guarded = guard_sql(
        sql, engine=engine, max_rows=max_rows, offset=offset)
    if not guarded.ok:
        raise QueryRejected(guarded.reason, guarded.kind or "guard")
    if cancel_event is not None and cancel_event.is_set():
        raise QueryCancelled("查询已取消")
    store = cache if cache is not None else _DEFAULT_CACHE
    key = store.make_key(engine=str(engine or ""), sql=guarded.sql,
                         limit=max_rows, offset=offset)
    if use_cache:
        cached = store.get(key)
        if cached is not None:
            cached["cached"] = True
            return cached
    started = time.perf_counter()
    result = await _execute_with_cancel(
        run_readonly(source, cfg, guarded.sql, timeout=timeout),
        cancel_event,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    envelope = normalize_result(
        result, sql=guarded.sql, limit=max_rows, offset=offset,
        elapsed_ms=elapsed_ms,
    )
    if use_cache:
        store.put(key, envelope)
    return envelope


__all__ = [
    "CONTRACT_VERSION", "QueryCache", "QueryCancelled", "QueryRejected",
    "get_default_cache", "normalize_result", "run_query_contract",
]
