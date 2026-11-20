"""[jonex] Dependency probes for the `/health` endpoint.

Why this lives in its own module: `/health` is whitelisted (no auth in
``WHITELIST_PATHS``), so the probe must be cheap to import and cheap to test
without pulling in the whole FastAPI app or the storage layer.

Design decisions behind the current shape (2026-11-20 review):

1. **One-shot connections, not the shared pool.** Probing through
   ``pool.acquire(timeout=...)`` reports *this process's pool pressure*, not
   whether the dependency is reachable: when heavy ingestion saturates the 25
   connections, the probe gets 503 while PG is perfectly healthy. It also pays
   a second timeout on release — ``Pool.release`` reuses the acquire timeout
   for the cancel handshake, so a 1s probe takes ~2s. A one-shot
   ``asyncpg.connect`` has neither problem; the parameter set is taken from the
   same ``db`` object that built the pool.

2. **Bounded and non-raising.** Each probe returns a state string; nothing
   propagates. A hung dependency must never hang ``/health``.

3. **Cache + single-flight.** The endpoint is unauthenticated; without a cache
   N concurrent callers become N connections. Only one probe runs at a time and
   the result is reused for a short TTL (shorter when degraded, so recovery is
   noticed quickly).

4. **Probe only what is configured.** A JSON doc-status storage has no PG to
   probe; that dependency is reported as ``skipped`` and never degrades the
   response. Otherwise switching to a dev storage config would make the service
   permanently unhealthy.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Upper bound for a single dependency probe. Kept well below the compose
#: healthcheck timeout (10s) so a hung dependency never eats the healthcheck.
HEALTH_PROBE_TIMEOUT = 1.0

#: Result reuse windows. Up results are stable, so they can be cached longer;
#: a degraded result expires faster so recovery shows up quickly.
HEALTH_CACHE_TTL_UP = 5.0
HEALTH_CACHE_TTL_DOWN = 2.0

STATE_UP = "up"
STATE_UNAVAILABLE = "unavailable"
STATE_NOT_INITIALIZED = "not_initialized"
STATE_SKIPPED = "skipped"
STATE_ERROR = "error"

#: States that downgrade the overall response to 503. ``skipped`` never does.
_DEGRADED_STATES = frozenset(
    {STATE_UNAVAILABLE, STATE_NOT_INITIALIZED, STATE_ERROR}
)


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------


def _pg_configured() -> bool:
    """True when the doc-status backend is PostgreSQL (so there is a DB to probe)."""
    return "PGDocStatusStorage" in os.environ.get(
        "LIGHTRAG_DOC_STATUS_STORAGE", ""
    )


def _connect_kwargs(db: Any) -> Dict[str, Any]:
    """Rebuild the connect arguments the pool was created with.

    Mirrors ``PostgreSQLDB.initdb``: custom SSL context first, then the simple
    ``require``/``prefer``/``disable`` mappings. Getting this wrong would
    silently downgrade TLS, so the two branches are kept identical.
    """
    kwargs: Dict[str, Any] = {
        "host": db.host,
        "port": db.port,
        "user": db.user,
        "password": db.password,
        "database": db.database,
    }
    ssl_context = None
    factory = getattr(db, "_create_ssl_context", None)
    if callable(factory):
        try:
            ssl_context = factory()
        except Exception:  # noqa: BLE001 - probe must not raise
            ssl_context = None
    if ssl_context is not None:
        kwargs["ssl"] = ssl_context
    else:
        mode = (getattr(db, "ssl_mode", None) or "").lower()
        if mode in ("require", "prefer"):
            kwargs["ssl"] = True
        elif mode == "disable":
            kwargs["ssl"] = False
    return kwargs


async def _default_asyncpg_connect(db: Any):
    """Open a one-shot asyncpg connection (imported lazily for testability)."""
    import asyncpg  # local import: keeps this module importable without asyncpg

    return await asyncpg.connect(**_connect_kwargs(db))


async def probe_postgres(
    manager: Any,
    *,
    timeout: float = HEALTH_PROBE_TIMEOUT,
    connect: Optional[Callable[[Any], Awaitable[Any]]] = None,
) -> str:
    """Return the PG probe state without ever raising.

    Uses a dedicated short connection rather than the shared pool: the answer
    must describe PG, not this process's pool occupancy.
    """
    rag = manager.get_default() if manager is not None else None
    if rag is None:
        return STATE_NOT_INITIALIZED if _pg_configured() else STATE_SKIPPED

    db = getattr(getattr(rag, "doc_status", None), "db", None)
    if db is None:
        return STATE_NOT_INITIALIZED if _pg_configured() else STATE_SKIPPED

    connect = connect or _default_asyncpg_connect
    conn = None
    try:
        conn = await asyncio.wait_for(connect(db), timeout=timeout)
        await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=timeout)
        return STATE_UP
    except asyncio.TimeoutError:
        logger.warning("health probe: postgres did not answer within %.1fs", timeout)
        return STATE_UNAVAILABLE
    except Exception as exc:  # noqa: BLE001 - probe must not propagate
        logger.warning(
            "health probe: postgres unreachable (%s)", type(exc).__name__
        )
        return STATE_UNAVAILABLE
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 - closing a dead conn is best-effort
                pass


# --------------------------------------------------------------------------
# Milvus / Neo4j (TCP reachability)
# --------------------------------------------------------------------------


def _parse_host_port(uri: str, default_port: int) -> Tuple[str, int]:
    """Extract ``(host, port)`` from a URI like ``http://milvus:19530``."""
    if not uri:
        return "", 0
    parsed = urlparse(uri if "://" in uri else f"//{uri}")
    return parsed.hostname or "", parsed.port or default_port


async def probe_tcp(
    uri: str,
    default_port: int,
    *,
    timeout: float = HEALTH_PROBE_TIMEOUT,
    open_connection: Optional[Callable[..., Awaitable[Any]]] = None,
) -> str:
    """Check that a TCP endpoint accepts connections.

    Deliberately a transport-level check: it answers "is the server accepting
    work at all", not "is this specific collection/database valid". That is the
    right granularity for a liveness probe and keeps it free of the storage
    client's synchronous RPCs.
    """
    host, port = _parse_host_port(uri, default_port)
    if not host:
        return STATE_SKIPPED

    opener = open_connection or asyncio.open_connection
    try:
        reader, writer = await asyncio.wait_for(
            opener(host, port), timeout=timeout
        )
        del reader
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - best-effort close
            pass
        return STATE_UP
    except asyncio.TimeoutError:
        logger.warning(
            "health probe: %s:%s did not accept within %.1fs", host, port, timeout
        )
        return STATE_UNAVAILABLE
    except Exception as exc:  # noqa: BLE001 - probe must not propagate
        logger.warning(
            "health probe: %s:%s unreachable (%s)", host, port, type(exc).__name__
        )
        return STATE_UNAVAILABLE


# --------------------------------------------------------------------------
# Aggregation: run every configured probe concurrently, with timing.
# --------------------------------------------------------------------------


async def probe_dependencies(
    manager: Any,
    args: Any = None,
    *,
    timeout: float = HEALTH_PROBE_TIMEOUT,
) -> Dict[str, Dict[str, Any]]:
    """Run every configured dependency probe and return ``{name: {state, ms}}``.

    Unconfigured backends are omitted entirely rather than reported as down.
    """
    probes: Dict[str, Callable[[], Awaitable[str]]] = {}

    if _pg_configured():
        probes["postgres"] = lambda: probe_postgres(manager, timeout=timeout)

    vector_storage = getattr(args, "vector_storage", "") or ""
    if "Milvus" in vector_storage:
        milvus_uri = os.environ.get("MILVUS_URI", "")
        probes["milvus"] = lambda: probe_tcp(
            milvus_uri, 19530, timeout=timeout
        )

    graph_storage = getattr(args, "graph_storage", "") or ""
    if "Neo4J" in graph_storage:
        neo4j_uri = os.environ.get("NEO4J_URI", "")
        probes["neo4j"] = lambda: probe_tcp(neo4j_uri, 7687, timeout=timeout)

    if not probes:
        return {}

    async def run(
        name: str, factory: Callable[[], Awaitable[str]]
    ) -> Tuple[str, Dict[str, Any]]:
        start = time.perf_counter()
        try:
            state = await factory()
        except Exception as exc:  # noqa: BLE001 - defensive: probes shouldn't raise
            logger.warning("health probe %s raised (%s)", name, type(exc).__name__)
            state = STATE_ERROR
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return name, {"state": state, "ms": elapsed_ms}

    results = await asyncio.gather(
        *(run(name, factory) for name, factory in probes.items())
    )
    return dict(results)


# --------------------------------------------------------------------------
# Cache + single-flight
# --------------------------------------------------------------------------

_cache: Dict[str, Any] = {"expires": 0.0, "value": {}}
_cache_lock: Optional[asyncio.Lock] = None


def _get_cache_lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def reset_cache() -> None:
    """Clear the cache and drop a lock bound to a stale event loop (tests)."""
    global _cache, _cache_lock
    _cache = {"expires": 0.0, "value": {}}
    _cache_lock = None


async def get_dependencies(
    manager: Any,
    args: Any = None,
    *,
    timeout: float = HEALTH_PROBE_TIMEOUT,
) -> Dict[str, Dict[str, Any]]:
    """Return cached probes, refreshing under a single-flight lock.

    At most one probe batch runs at a time; concurrent callers wait for it and
    then read the cached result instead of opening their own connections.
    """
    lock = _get_cache_lock()
    async with lock:
        now = time.monotonic()
        if now < _cache["expires"]:
            return _cache["value"]

        value = await probe_dependencies(manager, args, timeout=timeout)
        degraded = any(
            item.get("state") in _DEGRADED_STATES for item in value.values()
        )
        ttl = HEALTH_CACHE_TTL_DOWN if degraded else HEALTH_CACHE_TTL_UP
        _cache["value"] = value
        _cache["expires"] = now + ttl
        return value


# --------------------------------------------------------------------------
# Response shaping
# --------------------------------------------------------------------------


def apply_probe(
    payload: Dict[str, Any], dependencies: Dict[str, Dict[str, Any]]
) -> Tuple[Dict[str, Any], int]:
    """Attach probe results and decide the HTTP status code.

    ``200`` keeps every existing field untouched and only adds ``dependencies``.
    Any degraded state returns ``503``: the compose healthcheck only inspects
    the status code, so a 200 carrying ``degraded`` would leave the container
    marked healthy.
    """
    payload["dependencies"] = dependencies
    degraded = any(
        item.get("state") in _DEGRADED_STATES for item in dependencies.values()
    )
    if degraded:
        payload["status"] = "degraded"
        return payload, 503
    return payload, 200
