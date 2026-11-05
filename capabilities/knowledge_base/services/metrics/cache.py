# -*- coding: utf-8 -*-
"""指标结果进程内缓存（W4.2）。

- 默认关（`METRIC_ASK_CACHE_TTL_SECONDS=0`）；
- 键含 **schema 写代号**：任何 schema 行写入都会推进代号，命中立即作废
  （与 `nl2sql/schema_read` 的缓存同一纪律）；
- 只缓存成功结果；失败/拒绝永不缓存。
"""
from __future__ import annotations

import copy
import threading
import time

from jonex_core.common.config import config
from jonex_core.common.schema_generation import schema_generation

_cache: dict[tuple, tuple[dict, float]] = {}
_lock = threading.Lock()


def make_key(*, tenant_id: str, kb_id: str, data_source_id: str, sql: str,
             max_rows: int, offset: int) -> tuple:
    return (str(tenant_id), str(kb_id), str(data_source_id), str(sql),
            int(max_rows), int(offset), int(schema_generation()))


def get(key: tuple) -> dict | None:
    ttl = int(config.METRIC_ASK_CACHE_TTL_SECONDS or 0)
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _lock:
        item = _cache.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at <= now:
            _cache.pop(key, None)
            return None
        return copy.deepcopy(value)


def put(key: tuple, value: dict) -> None:
    ttl = int(config.METRIC_ASK_CACHE_TTL_SECONDS or 0)
    if ttl <= 0 or not isinstance(value, dict):
        return
    limit = max(1, int(config.METRIC_ASK_CACHE_MAX_ENTRIES or 128))
    with _lock:
        if len(_cache) >= limit:
            # 先清过期，再丢最早到期的条目；不引入 LRU。
            now = time.monotonic()
            for stale in [k for k, (_, exp) in _cache.items() if exp <= now]:
                _cache.pop(stale, None)
            if len(_cache) >= limit:
                oldest = min(_cache, key=lambda k: _cache[k][1])
                _cache.pop(oldest, None)
        _cache[key] = (copy.deepcopy(value), time.monotonic() + ttl)


def clear() -> None:
    with _lock:
        _cache.clear()


__all__ = ["clear", "get", "make_key", "put"]
