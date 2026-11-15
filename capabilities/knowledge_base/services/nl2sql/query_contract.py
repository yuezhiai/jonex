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
 "next_cursor": null,
 "elapsed_ms": 3, "cached": false, "result_format": "json",
 "sql_sha256": "…"}
```

- 分页在 `guard_sql` 上做（`offset` 追加，SQL Server 明确拒绝）；`next_cursor`
  是 **offset 编码**（`base64url("offset:<n>")`），只作为不透明令牌回传，**写入后
  会漂**——要稳定快照请用键集分页（未实现）；
- 取消用 `asyncio.Event`：置位即取消执行任务（适配器不保证服务端查询已终止，
  但调用方不再等待）；
- 缓存**默认关**（`NL2SQL_QUERY_CACHE_TTL_SECONDS=0`），键含 engine + 实际执行 SQL
  + 输出格式 + 是否取总数；
- `include_total=True` 时额外执行 `SELECT COUNT(*) FROM (<原始 SQL>) AS _q`
  （显式成本开关；失败只回 `total=None`，不阻断主查询）；
- `output="arrow"` 走 **JSON 信封内 base64 Arrow IPC**：pyarrow 惰性 import，
  未安装时 400（`kind="format"`），不新增硬依赖。
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import logging
import time

logger = logging.getLogger(__name__)

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


_MISSING = object()


def encode_cursor(offset: int) -> str:
    """offset → 不透明游标（base64url，无填充）。"""
    raw = f"offset:{int(offset)}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> int:
    """游标 → offset；格式不对或越界抛 `QueryRejected(kind="params")`。"""
    try:
        padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        prefix, _, value = raw.partition(":")
        offset = int(value)
    except (ValueError, UnicodeError):
        raise QueryRejected("分页游标无效", kind="params") from None
    if prefix != "offset" or not 0 <= offset <= 1_000_000:
        raise QueryRejected("分页游标无效", kind="params")
    return offset


def _to_arrow_ipc(columns: list[dict], rows: list[list]) -> bytes:
    """列 + 行 → Arrow IPC stream 字节；pyarrow 缺失时 400（不伪造）。"""
    try:
        import pyarrow as pa
    except ImportError:
        raise QueryRejected(
            "arrow 输出需要安装 pyarrow（pip install pyarrow）", kind="format"
        ) from None
    arrays = [pa.array([row[index] for row in rows])
              for index in range(len(columns))]
    table = pa.Table.from_arrays(
        arrays, names=[column["name"] for column in columns])
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _arrow_envelope(payload: bytes) -> dict:
    return {
        "encoding": "base64",
        "content_type": "application/vnd.apache.arrow.stream",
        "byte_length": len(payload),
        "data": base64.b64encode(payload).decode("ascii"),
    }


def normalize_result(result: dict, *, sql: str, limit: int | None,
                     offset: int = 0, elapsed_ms: int | None = None,
                     cached: bool = False, result_format: str = "json",
                     total=_MISSING) -> dict:
    raw_columns = list(result.get("columns") or [])
    columns = [{"name": _column_name(column), "type": _column_type(column)}
               for column in raw_columns]
    rows = [list(row or []) if not isinstance(row, dict)
            else [row.get(column["name"]) for column in columns]
            for row in (result.get("rows") or [])]
    returned = len(rows)
    truncated = bool(limit is not None and returned >= int(limit))
    out = {
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
        "next_cursor": (encode_cursor(int(offset or 0) + returned)
                        if truncated else None),
        "elapsed_ms": elapsed_ms,
        "cached": bool(cached),
        "result_format": result_format,
        "sql_sha256": hashlib.sha256(str(sql or "").encode("utf-8")).hexdigest(),
    }
    if total is not _MISSING:
        out["total"] = total
    return out


class QueryCache:
    """进程内 TTL 缓存；0 = 关闭（`get` 恒 miss、`put` 不写）。"""

    def __init__(self, ttl: int | None = None, max_entries: int | None = None):
        self.ttl = int(ttl if ttl is not None
                       else config.NL2SQL_QUERY_CACHE_TTL_SECONDS)
        self.max_entries = int(max_entries if max_entries is not None
                               else config.NL2SQL_QUERY_CACHE_MAX_ENTRIES)
        self._store: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def make_key(*, engine: str, sql: str, limit, offset: int,
                 output: str = "json", include_total: bool = False) -> str:
        # 输出格式与是否取总数必须进键：否则同一 SQL 的 json 结果会被
        # arrow 请求命中（反之亦然），total 也会被不带 total 的缓存污染。
        payload = (f"{engine}\x00{sql}\x00{limit}\x00{offset}"
                   f"\x00{output}\x00{int(bool(include_total))}")
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


async def _count_total(source, cfg: dict, sql: str, *, engine: str,
                       timeout: float | None) -> int | None:
    """`SELECT COUNT(*) FROM (<原始 SQL>) AS _q`；任何失败都回 None。

    计数是**显式成本开关**，不能让主查询跟着失败：安全闸拒绝、方言不支持子查询、
    连接抖动都只丢 `total`。计数 SQL 也过一遍安全闸（max_rows=1），不绕过只读判定。
    """
    count_sql = f"SELECT COUNT(*) FROM (\n{str(sql or '').strip()}\n) AS _q"
    guarded = guard_sql(count_sql, engine=engine, max_rows=1)
    if not guarded.ok:
        return None
    try:
        result = await run_readonly(source, cfg, guarded.sql, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 —— 计数失败不阻断主查询
        logger.info("总数查询失败（已降级 total=null）: %s: %s",
                    type(exc).__name__, exc)
        return None
    rows = result.get("rows") or []
    if not rows or not rows[0]:
        return None
    try:
        return int(rows[0][0])
    except (TypeError, ValueError):
        return None


async def run_query_contract(
    source, cfg: dict, sql: str, *, engine: str, max_rows: int,
    offset: int = 0, timeout: float | None = None,
    cancel_event: asyncio.Event | None = None, output: str = "json",
    include_total: bool = False, cursor: str | None = None,
    use_cache: bool = True, cache: QueryCache | None = None,
) -> dict:
    """安全闸 → 分页 → 执行（可取消）→ 统一信封（可选缓存）。"""
    fmt = str(output or "json").lower()
    if fmt not in ("json", "arrow"):
        raise QueryRejected("只支持 json / arrow 输出", kind="format")
    if cursor is not None and str(cursor) != "":
        if int(offset or 0) > 0:
            raise QueryRejected("cursor 与 offset 不能同时使用", kind="params")
        offset = decode_cursor(str(cursor))
    guarded = guard_sql(
        sql, engine=engine, max_rows=max_rows, offset=offset)
    if not guarded.ok:
        raise QueryRejected(guarded.reason, guarded.kind or "guard")
    if cancel_event is not None and cancel_event.is_set():
        raise QueryCancelled("查询已取消")
    store = cache if cache is not None else _DEFAULT_CACHE
    key = store.make_key(engine=str(engine or ""), sql=guarded.sql,
                         limit=max_rows, offset=offset,
                         output=fmt, include_total=include_total)
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
    extra: dict = {"total": _MISSING}
    if include_total:
        extra["total"] = await _count_total(
            source, cfg, sql, engine=engine, timeout=timeout)
    if fmt == "arrow":
        envelope = normalize_result(
            result, sql=guarded.sql, limit=max_rows, offset=offset,
            elapsed_ms=elapsed_ms, result_format="arrow", **extra,
        )
        payload = _to_arrow_ipc(envelope["columns"], envelope["rows"])
        envelope["rows"] = []
        envelope["arrow"] = _arrow_envelope(payload)
    else:
        envelope = normalize_result(
            result, sql=guarded.sql, limit=max_rows, offset=offset,
            elapsed_ms=elapsed_ms, **extra,
        )
    if use_cache:
        store.put(key, envelope)
    return envelope


__all__ = [
    "CONTRACT_VERSION", "QueryCache", "QueryCancelled", "QueryRejected",
    "decode_cursor", "encode_cursor", "get_default_cache", "normalize_result",
    "run_query_contract",
]
