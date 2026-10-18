"""LightRAG Server (:9621) HTTP 客户端 —— P2 剪裁面。

[jonex] P2 宿主解耦：自 `raganything/service/http_lightrag_client.py` 剪裁，
只保留本体编译链路实际用到的那部分，外加它们背后的隐性依赖
（`__init__` / `close` / `_headers` / `_post` / `_get` / `_get_json` /
`track_status` / `_parse_track_status` / `_poll_one_track`）。

保留的操作：
  - 写入：upload_text（灌 chunk）、insert_custom_kg（本体抽取结果回注）
  - 轮询：track_status、batch_track_status
  - 读回：get_document_chunks

被剪掉的方法（查询 / 删除 / 图读 / 单文档状态探针）由宿主侧服务各自持有，
不在本包内 —— 本包只需要「灌进去、等它跑完、读回 chunk、回注图」这条闭环。

`CircuitBreaker` 自 `raganything/service/circuit_breaker.py` **内联**（原文 42 行，
只依赖标准库 `time`）：源文件顶部的
`from raganything.service.circuit_breaker import CircuitBreaker` 是本包
唯一无法保留的依赖（KB 容器不含 raganything，见 tests/unit/ontology_compile/
test_no_raganything_dependency.py 的红线守卫）。

迁移纪律：除上述内联与 docstring 重写外，搬入的代码**逐字不动** ——
`_get` 里 `{"params": params} if params else {}` 的写法与
`batch_track_status` 的并发信号量都是真实生产故障的止损点，不许顺手清理。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from jonex_core.common.file_source_util import lightrag_workspace

logger = logging.getLogger(__name__)

# ── Inlined dependency: CircuitBreaker ──────────────────────────────────
# 逐字搬自 raganything/service/circuit_breaker.py（唯一改动：顶部 import time
# 并入本文件已有的 import 区）。


class CircuitBreaker:
    """Circuit breaker with closed → open → half_open state transitions.

    After ``fail_threshold`` consecutive failures, the breaker opens.
    It stays open for ``cooldown_seconds``, then transitions to half_open
    allowing one probe request through. Success resets to closed.
    """

    def __init__(self, fail_threshold: int = 5, cooldown_seconds: float = 30.0):
        self._fail_threshold = fail_threshold
        self._cooldown = cooldown_seconds
        self._fail_count = 0
        self._opened_at: float = 0.0
        self._state = "closed"

    @property
    def state(self) -> str:
        return self._state

    def allow(self) -> bool:
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self._cooldown:
                self._state = "half_open"
                return True
            return False
        return True

    def record_success(self):
        self._fail_count = 0
        self._state = "closed"

    def record_failure(self):
        self._fail_count += 1
        if self._fail_count >= self._fail_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()


# ── Data types ──────────────────────────────────────────────────────────


@dataclass
class TrackStatus:
    """track_status three-state result.

    state='completed' → doc_ids is non-empty.
    state='failed'    → error may carry the failure reason.
    state in {pending, processing} → the chunk is still being handled.

    [jonex] P0-1.2: doc_metadata carries per-document metadata from the LightRAG
    track_status response (e.g. is_duplicate, original_doc_id).  Set on failed
    tracks so the pipeline polling layer can structurally identify dup-failed
    chunks without relying solely on error-string heuristics.
    """

    state: Literal["pending", "processing", "completed", "failed", "timeout"]
    doc_ids: list[str] = field(default_factory=list)
    error: str | None = None
    doc_metadata: dict | None = None  # [jonex] P0-1.2: per-document metadata for dup detection
    empty: bool = False  # [jonex] orphan detection: true when track has 0 documents


@dataclass
class UploadResult:
    """Return value of upload_text().

    track_id  — assigned by :9621; used for subsequent track_status polling.
    status    — "success" for new chunks, "duplicated" for idempotent re-uploads.
    doc_ids   — populated when :9621 is in synchronous mode; empty for async.
    """

    track_id: str
    status: str  # "success" | "duplicated"
    doc_ids: list[str] = field(default_factory=list)


# ── Exceptions ──────────────────────────────────────────────────────────


class LightRAGUnavailableError(Exception):
    """Raised when the circuit breaker is open."""

    pass


class LightRAGTimeoutError(Exception):
    """Raised when a LightRAG request times out."""

    pass


class LightRAGError(Exception):
    """Raised for non-2xx LightRAG responses with mapped v2 error codes."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# ── Error mapping ───────────────────────────────────────────────────────


def _map_lightrag_error(exc: httpx.HTTPStatusError) -> Exception:
    """Map httpx HTTPStatusError to domain-specific LightRAGError."""
    status = exc.response.status_code
    detail = ""
    try:
        detail = exc.response.json().get("detail", "")
    except Exception:
        detail = exc.response.text[:200]

    if 500 <= status < 600:
        return LightRAGError(502, f"LightRAG upstream error (HTTP {status})")
    elif status == 429:
        return LightRAGError(429, "LightRAG rate limited")
    elif status == 404:
        return LightRAGError(404, "Resource not found in LightRAG workspace")
    return LightRAGError(status, f"LightRAG error: {detail}")


# ── HttpLightRagClient ──────────────────────────────────────────────────


class HttpLightRagClient:
    """HTTP client for the LightRAG Server (:9621) — P2 剪裁面。

    [jonex] P2：本类只覆盖本体编译链路用到的那几段：灌 chunk（upload_text）、
    等入库跑完（track_status / batch_track_status）、读回 chunk
    （get_document_chunks）、把抽取结果回注图（insert_custom_kg）。

    All methods carry tenant_id + kb_id → LIGHTRAG-WORKSPACE header.
    Circuit breaker guards every outbound call.
    """

    # ── Init ──────────────────────────────────────────────────────────

    def __init__(self):
        self.base_url = os.getenv("LIGHTRAG_API_URL", "http://lightrag:9621").rstrip("/")
        self.api_key = os.getenv("LIGHTRAG_API_KEY", "")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                float(os.getenv("RAG_HTTP_TIMEOUT", "300")),
                connect=float(os.getenv("RAG_HTTP_CONNECT_TIMEOUT", "5")),
            ),
            limits=httpx.Limits(
                max_connections=int(os.getenv("RAG_HTTP_MAX_CONNECTIONS", "100")),
                max_keepalive_connections=int(os.getenv("RAG_HTTP_MAX_KEEPALIVE", "20")),
            ),
        )
        self._breaker = CircuitBreaker(
            fail_threshold=int(os.getenv("RAG_HTTP_CB_FAIL_THRESHOLD", "5")),
            cooldown_seconds=float(os.getenv("RAG_HTTP_CB_COOLDOWN_SECONDS", "30")),
        )
        # Per-request retry for transient errors
        self._max_retries = int(os.getenv("RAG_HTTP_RETRIES", "3"))
        # [jonex] track_status 轮询并发度，避免大文档数百 chunk 同时建 HTTP 连接
        try:
            self._track_poll_concurrency = int(os.getenv("RAG_TRACK_POLL_CONCURRENCY", "8"))
        except ValueError:
            self._track_poll_concurrency = 8
        if self._track_poll_concurrency <= 0:
            self._track_poll_concurrency = 8

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    # ── Internal: HTTP helpers ────────────────────────────────────────

    def _headers(self, tenant_id: str, kb_id: str) -> dict[str, str]:
        """Build request headers with workspace + API key."""
        h: dict[str, str] = {}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        ws = lightrag_workspace(tenant_id, kb_id)
        if ws:
            h["LIGHTRAG-WORKSPACE"] = ws
        return h

    async def _post(
        self, path: str, tenant_id: str, kb_id: str, body: dict | None = None,
    ) -> dict:
        """POST to :9621 with circuit breaker, retry, and error mapping."""
        if not self._breaker.allow():
            raise LightRAGUnavailableError("LightRAG temporarily unavailable (circuit breaker open)")

        headers = self._headers(tenant_id, kb_id)
        url = f"{self.base_url}{path}"

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._client.post(url, headers=headers, json=body or {})
                self._breaker.record_success()
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                self._breaker.record_failure()
                last_exc = LightRAGTimeoutError(
                    f"LightRAG timeout after {self._client.timeout.read}s"
                )
            except httpx.HTTPStatusError as e:
                # 4xx → not a server fault, don't retry
                if 400 <= e.response.status_code < 500:
                    self._breaker.record_success()
                    raise _map_lightrag_error(e) from e
                # 5xx → server fault, retry
                self._breaker.record_failure()
                last_exc = _map_lightrag_error(e)
            except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
                self._breaker.record_failure()
                last_exc = LightRAGError(502, f"LightRAG connection error: {e}")

            if attempt < self._max_retries:
                backoff = 2.0 ** attempt  # 2s, 4s, 8s
                logger.warning(
                    f"LightRAG POST {path} attempt {attempt}/{self._max_retries} "
                    f"failed: {last_exc}. Retrying in {backoff}s"
                )
                await asyncio.sleep(backoff)

        raise last_exc  # type: ignore[misc]

    async def _get(
        self, path: str, tenant_id: str, kb_id: str, params: dict | None = None,
    ) -> httpx.Response:
        """GET from :9621 with circuit breaker (returns raw response for HEAD etc.)."""
        if not self._breaker.allow():
            raise LightRAGUnavailableError("LightRAG temporarily unavailable (circuit breaker open)")

        headers = self._headers(tenant_id, kb_id)
        url = f"{self.base_url}{path}"

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                # 这里**不能**写 `params or {}`：httpx 0.28 在拿到一个（哪怕是空的）
                # `params` 字典时会用它**重建** query，于是 path 里自带的 query 被整个
                # 抹掉 —— 例如 `get_document_chunks` 拼的 `/documents/chunks?doc_id=X`
                # 会变成光秃秃的 `/documents/chunks`，LightRAG 那边 `doc_id` 是
                # `Query(...)` 必填，直接回 422，本体抽取在「读回 chunk」这一步就死了。
                # 传 `None` 时不 merge，URL 原样发出。
                _req_params = {"params": params} if params else {}
                resp = await self._client.get(url, headers=headers, **_req_params)
                self._breaker.record_success()
                return resp
            except httpx.TimeoutException:
                self._breaker.record_failure()
                last_exc = LightRAGTimeoutError("LightRAG GET timeout")
            except httpx.HTTPStatusError as e:
                if 400 <= e.response.status_code < 500:
                    self._breaker.record_success()
                    raise _map_lightrag_error(e) from e
                self._breaker.record_failure()
                last_exc = _map_lightrag_error(e)
            except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
                self._breaker.record_failure()
                last_exc = LightRAGError(502, f"LightRAG connection error: {e}")

            if attempt < self._max_retries:
                backoff = 2.0 ** attempt
                await asyncio.sleep(backoff)

        raise last_exc  # type: ignore[misc]

    async def _get_json(
        self, path: str, tenant_id: str, kb_id: str, params: dict | None = None,
    ) -> dict:
        """GET → parsed JSON with same retry/breaker semantics as _post."""
        resp = await self._get(path, tenant_id, kb_id, params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Write ──────────────────────────────────────────────────────────

    async def upload_text(
        self, text: str, file_source: str, *,
        tenant_id: str, kb_id: str,
    ) -> UploadResult:
        """POST /documents/text → push a single text chunk.

        Returns UploadResult with track_id for subsequent status polling.
        """
        body: dict[str, Any] = {
            "text": text,
            "file_source": file_source,
        }
        data = await self._post("/documents/text", tenant_id, kb_id, body)
        return UploadResult(
            track_id=data.get("track_id", ""),
            status=data.get("status", "success"),
            doc_ids=data.get("doc_ids", []) or [],
        )

    async def track_status(
        self, track_id: str, *, tenant_id: str, kb_id: str,
    ) -> TrackStatus:
        """GET /documents/track_status/{track_id} → TrackStatus (3 retries).

        LightRAG Server exposes ``GET /documents/track_status/{track_id}`` and
        returns ``{track_id, documents:[{id,status,error_msg,...}], total_count,
        status_summary}`` (see LightRAG document_routes.get_track_status).

        We derive the v2 three-state from the per-document statuses:
          - any document ``failed``               → state="failed"
          - documents present & all ``processed``  → state="completed"
          - documents present but not all done     → state="processing"
          - no documents yet (async not registered)→ state="pending"
        """
        for attempt in range(1, 4):
            try:
                data = await self._get_json(
                    f"/documents/track_status/{track_id}", tenant_id, kb_id,
                )
                return self._parse_track_status(data)
            except httpx.HTTPStatusError as e:
                mapped = _map_lightrag_error(e)
                if attempt == 3:
                    raise mapped from e
                await asyncio.sleep(2.0 ** attempt)
            except (LightRAGTimeoutError, LightRAGError):
                if attempt == 3:
                    raise
                await asyncio.sleep(2.0 ** attempt)

        # Should not reach here, but satisfy type checker
        raise LightRAGError(500, "track_status exhausted retries")

    @staticmethod
    def _parse_track_status(data: dict) -> TrackStatus:
        """Map a LightRAG track_status response to the v2 three-state TrackStatus.

        Pure function, does not raise. ``processed`` is the terminal success
        state (``preprocessed`` still needs multimodal processing → processing).

        [jonex] P0-1.2: extracts ``metadata`` from the first failed/dup document
        so the pipeline polling layer can structurally identify dup-failed chunks
        (is_duplicate, original_doc_id) without relying solely on error-string
        heuristics.
        """
        documents = data.get("documents", []) or []
        if not documents:
            # [jonex] orphan detection: track_id not yet registered / no docs
            # recorded → keep polling, but flag as empty so batch_track_status
            # can detect orphan tracks after consecutive empty rounds.
            return TrackStatus(state="pending", empty=True)

        statuses = [
            {
                "id": doc.get("id"),
                "status": str(doc.get("status", "")).lower(),
                "error": doc.get("error_msg") or doc.get("error"),
                "metadata": doc.get("metadata") if isinstance(doc, dict) else None,
            }
            for doc in documents
        ]

        failed = [s for s in statuses if s["status"] == "failed"]
        if failed:
            reason = "; ".join(
                f"{s['id']}: {s['error']}" for s in failed if s["error"]
            ) or f"{len(failed)} document(s) failed"
            # [jonex] P0-1.2: carry metadata from the first failed doc for dup detection
            first_failed_meta = failed[0].get("metadata") if failed else None
            return TrackStatus(
                state="failed", error=reason,
                doc_metadata=first_failed_meta if isinstance(first_failed_meta, dict) else None,
            )

        doc_ids = [s["id"] for s in statuses if s["id"]]
        processed = [s["id"] for s in statuses if s["id"] and s["status"] == "processed"]
        has_status = any(s["status"] for s in statuses)

        if processed and len(processed) == len(doc_ids):
            return TrackStatus(state="completed", doc_ids=processed)

        # 兼容旧版 track_status：documents 有 id 但没有 status 字段
        if doc_ids and not has_status:
            return TrackStatus(state="completed", doc_ids=doc_ids)

        return TrackStatus(state="processing", doc_ids=processed)

    async def batch_track_status(
        self, track_ids: list[str], *, tenant_id: str, kb_id: str,
        max_wait_seconds: float = 1800,
        per_track_timeout_seconds: float | None = None,
    ) -> tuple[dict[str, TrackStatus], dict[str, TrackStatus]]:
        """Batch poll track_ids until terminal or max_wait_seconds.

        Returns:
            (terminal_map, pending_map)
              - terminal_map: track_id → TrackStatus (state in {completed, failed})
              - pending_map:  track_id → TrackStatus (state in {pending, processing})

        [jonex] orphan detection (§4.3): tracks that return total_count=0 for
        consecutive rounds are detected as orphans and released as benign
        (state=completed, empty=True) instead of waiting for per-chunk timeout.
        """
        if not track_ids:
            return {}, {}

        remaining = set(track_ids)
        terminal: dict[str, TrackStatus] = {}
        pending: dict[str, TrackStatus] = {}
        poll_interval = 2.0
        started = time.monotonic()

        # [jonex] Semaphore 限流：大文档数百 chunk 时避免同时建数百 HTTP 连接
        poll_sem = asyncio.Semaphore(self._track_poll_concurrency)

        # [jonex] orphan detection: consecutive zero-doc rounds per track_id
        orphan_rounds = int(os.getenv("RAG_ORPHAN_DETECTION_ROUNDS", "15"))
        _zero_doc_counter: dict[str, int] = {}

        async def _poll_one(tid: str) -> TrackStatus:
            async with poll_sem:
                return await self._poll_one_track(tid, tenant_id, kb_id)

        while remaining and (time.monotonic() - started) < max_wait_seconds:
            tasks = {
                tid: asyncio.create_task(_poll_one(tid))
                for tid in remaining
            }
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)

            still_remaining: set[str] = set()
            elapsed = time.monotonic() - started
            over_per_track = (
                per_track_timeout_seconds is not None
                and elapsed > per_track_timeout_seconds
            )
            for tid, result in zip(tasks.keys(), results):
                if isinstance(result, Exception):
                    logger.warning(f"batch_track_status: poll {tid} error: {result}")
                    # [jonex] orphan detection: errors don't reset counter, keep tracking
                    if over_per_track:
                        pending[tid] = TrackStatus(state="timeout", error="Per-chunk polling timeout")
                        _zero_doc_counter.pop(tid, None)
                    else:
                        still_remaining.add(tid)
                    continue
                # ── [jonex] orphan detection: tracks with 0 documents for N
                # consecutive rounds are orphan track_ids.  Release as benign
                # (completed with empty doc_ids) instead of holding until timeout.
                if getattr(result, 'empty', False):
                    _zero_doc_counter[tid] = _zero_doc_counter.get(tid, 0) + 1
                    if _zero_doc_counter[tid] >= orphan_rounds:
                        logger.info(
                            "batch_track_status: %s orphan after %d empty rounds → releasing",
                            tid, orphan_rounds,
                        )
                        terminal[tid] = TrackStatus(state="completed", empty=True)
                        pending.pop(tid, None)
                        _zero_doc_counter.pop(tid, None)
                        continue
                else:
                    _zero_doc_counter.pop(tid, None)
                # ── [jonex] orphan detection end ────────────────────────────

                if result.state in ("completed", "failed"):
                    terminal[tid] = result
                    pending.pop(tid, None)  # [jonex] 终态 track 从 pending 剔除，避免脏残留误判 timeout
                elif over_per_track:
                    pending[tid] = TrackStatus(state="timeout", error="Per-chunk polling timeout")
                else:
                    still_remaining.add(tid)
                    pending[tid] = result

            remaining = still_remaining
            if remaining:
                await asyncio.sleep(poll_interval)

        # Remaining after timeout → mark as pending
        for tid in remaining:
            pending[tid] = TrackStatus(state="processing", error="Polling timeout")

        return terminal, pending

    async def _poll_one_track(
        self, track_id: str, tenant_id: str, kb_id: str,
    ) -> TrackStatus:
        """Single track poll (no retry — retry is at batch level)."""
        try:
            return await self.track_status(
                track_id, tenant_id=tenant_id, kb_id=kb_id,
            )
        except Exception as e:
            logger.warning(f"Poll track {track_id} failed: {e}")
            return TrackStatus(state="processing", error=str(e))

    # ── [jonex] Document Chunks ───────────────────────────────────────

    async def get_document_chunks(
        self, doc_id: str, *, tenant_id: str, kb_id: str,
    ) -> dict:
        """GET /documents/chunks?doc_id=X — 按 doc_id 锚点列出文档所有 chunks。

        返回 {doc_id, total, chunks: [{chunk_id, content, file_path, ...}]}。
        file_path 含 tstart=/tend= 视频/音频时间轴信息。
        """
        return await self._get_json(
            f"/documents/chunks?doc_id={doc_id}", tenant_id, kb_id,
        )

    async def insert_custom_kg(
        self, entities: list[dict], relationships: list[dict], *,
        tenant_id: str, kb_id: str,
    ) -> dict:
        """POST /documents/custom-kg — 本体抽取结果回注 LightRAG 图（§5.7）。

        chunks 传空（chunk 已由 /documents/text 入库，无需也不应重传）；
        source_id 使用 §5.6 读回的权威 chunk_id（多 chunk 用 ``<SEP>`` 拼接，
        与 LightRAG 自抽路径同构）；full_doc_id 不传（平台文档与 LightRAG
        doc key 无一对一映射）。重试/熔断语义由 _post 统一承担。
        """
        body: dict[str, Any] = {
            "chunks": [],
            "entities": entities,
            "relationships": relationships,
        }
        return await self._post("/documents/custom-kg", tenant_id, kb_id, body)
