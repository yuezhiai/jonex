"""HttpLightRagClient — unified LightRAG Server :9621 HTTP client (read + write).

Replaces the v1 split:
  - StorageReaderService  (read-only HTTP client)
  - Embedded LightRAG     (write via local storage)

All read/write operations go through a single httpx.AsyncClient connection pool
with circuit breaker, workspace header isolation, and retry logic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from raganything.service.circuit_breaker import CircuitBreaker
from jonex_core.common.file_source_util import lightrag_workspace

logger = logging.getLogger(__name__)

# ── Workspace ID validation ─────────────────────────────────────────────

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-\.]{1,64}$")


def _validate_workspace_id(value: str, label: str) -> str:
    """Validate tenant_id / kb_id for workspace header injection safety."""
    if not value or not _SAFE_ID.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


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
    """Unified HTTP client for LightRAG Server (:9621).

    Covers all read + write operations:
      - Write: upload_text, track_status, batch_track_status, delete_doc
      - Read:  query, get_summary, get_documents, get_entities,
               get_relationships, get_graph_summary, get_graph,
               get_document_parse_result
      - Probe: doc_exists

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

    def _query_headers(
        self, tenant_id: str, kb_id: str, trace_id: str = "",
        user_id: str = "", scene: str = "lightrag_query",  # [jonex] user_id 对齐 v1 计量头
    ) -> dict[str, str]:
        """[jonex] Query metering headers + workspace isolation.

        Mirrors the retired v1 _jonex_query_headers(): injects X-Jonex-*
        dimensions so LightRAG Server middleware can propagate
        tenant/kb/scene/trace/user to the llm-gateway for per-tenant
        token metering.
        """
        h = self._headers(tenant_id, kb_id)
        h["X-Jonex-Scene"] = scene
        if tenant_id:
            h["X-Jonex-Tenant-Id"] = tenant_id
        if kb_id:
            h["X-Jonex-Kb-Id"] = kb_id
        if trace_id:
            h["X-Jonex-Trace-Id"] = trace_id
        if user_id:
            h["X-Jonex-User-Id"] = user_id  # [jonex] 补全 v1 六头计量维度
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

    async def _delete(
        self, path: str, tenant_id: str, kb_id: str, body: dict | None = None,
        *, document_id: str = "", trace_id: str = "",
    ) -> dict:
        """DELETE to :9621 with circuit breaker, retry, and error mapping.

        httpx 的 post/get 不支持 DELETE，需用 request("DELETE", ...) 才能带 body。
        LightRAG 的 /documents/delete_document 要求 DELETE 方法 + doc_ids 列表 body，
        用 POST 会被拒 405（对齐 v1 LightRAGServerClient._client.request("DELETE", ...)）。

        [jonex] R4：携带 X-Jonex-* 计量头（scene=lightrag_rebuild），使 LightRAG
        中间件在删除/重建路径能正确归因 LLM 调用（不再误记为 lightrag_query）。
        """
        if not self._breaker.allow():
            raise LightRAGUnavailableError("LightRAG temporarily unavailable (circuit breaker open)")

        headers = self._headers(tenant_id, kb_id)
        # [jonex] R4：delete/rebuild 计量头 —— scene=lightrag_rebuild 区分于查询/入库
        headers["X-Jonex-Scene"] = "lightrag_rebuild"
        if tenant_id:
            headers["X-Jonex-Tenant-Id"] = tenant_id
        if kb_id:
            headers["X-Jonex-Kb-Id"] = kb_id
        if document_id:
            headers["X-Jonex-Doc-Id"] = document_id
        if trace_id:
            headers["X-Jonex-Trace-Id"] = trace_id
        url = f"{self.base_url}{path}"

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._client.request(
                    "DELETE", url, headers=headers, json=body or {},
                )
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
                    f"LightRAG DELETE {path} attempt {attempt}/{self._max_retries} "
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
                resp = await self._client.get(url, headers=headers, params=params or {})
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

    async def delete_orphan_chunk(
        self, chunk_id: str, *, tenant_id: str, kb_id: str,
    ) -> bool:
        """[jonex] O7: DELETE /documents/chunks/{chunk_id} — 孤儿 chunk 强制清理。

        doc_status 无记录的残留 chunk（reparse 删除未收敛），doc 级删除会
        not found 直接返回。本端点按 chunk_id 直删 text_chunks/向量/full_docs，
        不依赖 doc_status。
        """
        try:
            data = await self._delete(
                f"/documents/chunks/{chunk_id}", tenant_id, kb_id, body=None,
            )
            return bool(data and data.get("status") == "success")
        except LightRAGError as e:
            if e.code == 404:
                return True  # 已不存在视为清理完成
            logger.warning(
                "LightRAG delete_orphan_chunk(%s) failed: %s", chunk_id, e,
            )
            return False

    async def delete_doc(
        self, doc_id: str, *, tenant_id: str, kb_id: str,
    ) -> bool:
        """DELETE /documents/delete_document — doc_id is server chunk doc_id.

        对齐 v1 LightRAGServerClient._delete_doc_with_retry：
          - 用 DELETE 方法（POST 会被 LightRAG 拒 405）；
          - body 传 doc_ids 列表 + delete_file + delete_llm_cache（连带删上传文件与 LLM 缓存）；
          - LightRAG 后台异步删除返回 status=deletion_started；
          - status=busy（服务忙于其他任务）时带退避重试；
          - 404（workspace 内查无此 doc）视为已删除，返回 False。
        """
        try:
            busy_retries = max(1, int(os.getenv("RAG_DELETE_BUSY_RETRIES", "5")))
        except ValueError:
            busy_retries = 5
        try:
            busy_base_delay = float(os.getenv("RAG_DELETE_BUSY_DELAY", "2"))
        except ValueError:
            busy_base_delay = 2.0

        body = {
            "doc_ids": [doc_id],
            "delete_file": True,
            "delete_llm_cache": True,
        }
        for attempt in range(busy_retries):
            try:
                data = await self._delete(
                    "/documents/delete_document", tenant_id, kb_id, body=body,
                )  # [jonex] R4: single-doc delete, tenant/kb in headers for metering
            except LightRAGError as e:
                if e.code == 404:
                    return False
                raise

            status = str(data.get("status", "")).lower()

            if status == "deletion_started":
                logger.info(f"LightRAG delete_doc({doc_id}) 已启动后台删除")
                return True

            if status == "busy":
                # LightRAG 单管道：并发索引其他文档时删除返回 busy，指数退避重试
                # （对齐 v1 _delete_doc_with_retry：delay = base_delay * 2^attempt）。
                if attempt < busy_retries - 1:
                    delay = busy_base_delay * (2 ** attempt)
                    logger.warning(
                        f"LightRAG 正忙于其他任务，delete_doc({doc_id}) "
                        f"(attempt {attempt + 1}/{busy_retries})，{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LightRAGError(
                    503,
                    f"LightRAG delete failed: 服务持续繁忙，已重试 {busy_retries} 次",
                )

            # 未知 status → 当作成功处理（对齐 v1）
            logger.warning(
                f"LightRAG delete_doc({doc_id}) 返回未知状态 status={status or 'ok'}，视为成功"
            )
            return True

        raise LightRAGError(
            503, f"LightRAG delete failed: 服务繁忙，已重试 {busy_retries} 次"
        )

    async def delete_docs(
        self, doc_ids: list[str], *, tenant_id: str, kb_id: str,
        document_id: str = "", trace_id: str = "",
    ) -> dict:
        """[jonex] 2-A + R4：批量删除——一次 DELETE /documents/delete_document 传全部 doc_ids。

        LightRAG 的 background_delete_documents 本就 loop doc_ids，在**单个 busy 会话**内
        连删全部；相比逐个 id 各自抢 busy 锁被拒，大幅减少 busy 拒绝与抢锁开销。

        返回 {"status": "deletion_started"|..., "accepted": [...受理的 doc_ids...]}：
          - deletion_started → 整批受理进入后台删除，accepted=全部 doc_ids；
          - 404（workspace 内查无）→ 视为已删除，accepted=全部；
          - busy 重试耗尽 → 抛 LightRAGError(503)，调用方保留 pending 待下次/兜底。
        真正的完成确认由上层 _converge_delete 收敛循环负责（每轮重新发起 delete +
        轮询读一致性，而非旧 _poll_old_ids_gone 的 poll-only 模式）。

        [jonex] R4：透传 document_id + trace_id 到 _delete，注入 X-Jonex-* 计量头。
        """
        ids = [d for d in (doc_ids or []) if d]
        if not ids:
            return {"status": "deletion_started", "accepted": []}

        try:
            busy_retries = max(1, int(os.getenv("RAG_DELETE_BUSY_RETRIES", "5")))
        except ValueError:
            busy_retries = 5
        try:
            busy_base_delay = float(os.getenv("RAG_DELETE_BUSY_DELAY", "2"))
        except ValueError:
            busy_base_delay = 2.0

        body = {
            "doc_ids": list(ids),
            "delete_file": True,
            "delete_llm_cache": True,
        }
        for attempt in range(busy_retries):
            try:
                data = await self._delete(
                    "/documents/delete_document", tenant_id, kb_id, body=body,
                    document_id=document_id, trace_id=trace_id,
                )
            except LightRAGError as e:
                if e.code == 404:
                    return {"status": "deletion_started", "accepted": list(ids)}
                raise

            status = str(data.get("status", "")).lower()

            if status == "deletion_started":
                logger.info(
                    f"LightRAG delete_docs 已启动后台批量删除：count={len(ids)}"
                )
                return {"status": "deletion_started", "accepted": list(ids)}

            if status == "busy":
                if attempt < busy_retries - 1:
                    delay = busy_base_delay * (2 ** attempt)
                    logger.warning(
                        f"LightRAG 正忙于其他任务，delete_docs(count={len(ids)}) "
                        f"(attempt {attempt + 1}/{busy_retries})，{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LightRAGError(
                    503,
                    f"LightRAG delete_docs failed: 服务持续繁忙，已重试 {busy_retries} 次",
                )

            # 未知 status → 视为受理（对齐 delete_doc 单条语义）
            logger.warning(
                f"LightRAG delete_docs(count={len(ids)}) 返回未知状态 status={status or 'ok'}，视为已受理"
            )
            return {"status": status or "deletion_started", "accepted": list(ids)}

        raise LightRAGError(
            503, f"LightRAG delete_docs failed: 服务繁忙，已重试 {busy_retries} 次"
        )

    async def update_chunk(
        self, old_chunk_id: str, new_content: str, *,
        tenant_id: str, kb_id: str,
        file_source: str = "",
        expected_content_hash: str | None = None,
    ) -> dict:
        """POST /documents/chunks/update — 更新单个 chunk（含重向量化+引用重写）。

        服务端在单个事务中完成：
        upsert new chunk → delete old chunk → rewrite entity/relation references。
        """
        body: dict[str, Any] = {
            "old_chunk_id": old_chunk_id,
            "new_content": new_content,
            "file_source": file_source,
        }
        if expected_content_hash:
            body["expected_content_hash"] = expected_content_hash
        # 如果 old_chunk_id 是 doc-xxx 格式，同时传 doc_status_id 让 LightRAG 回退查找
        if old_chunk_id and not old_chunk_id.startswith("chunk-"):
            body["doc_status_id"] = old_chunk_id
        return await self._post("/documents/chunks/update", tenant_id, kb_id, body)

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

    async def get_chunk_by_id(
        self, chunk_id: str, *, tenant_id: str, kb_id: str,
    ) -> dict:
        """GET /documents/chunks/{chunk_id} — 按 chunk_id 直查单个 chunk 内容。"""
        return await self._get_json(
            f"/documents/chunks/{chunk_id}", tenant_id, kb_id,
        )

    # ── Query ──────────────────────────────────────────────────────────

    async def query(
        self, q: str, *, mode: str = "hybrid", top_k: int = 5,
        tenant_id: str, kb_id: str, trace_id: str = "",
        user_id: str = "", only_need_context: bool = False,  # [jonex] 方案 A 透传
    ) -> dict:
        """POST /query → {response, references}."""
        body: dict[str, Any] = {
            "query": q,
            "mode": mode,
            "top_k": top_k,
            "include_references": True,
            "include_chunk_content": True,
            "only_need_context": only_need_context,  # [jonex] 方案 A 透传（对齐 v1）
        }
        if trace_id:
            body["trace_id"] = trace_id

        if not self._breaker.allow():
            raise LightRAGUnavailableError("LightRAG temporarily unavailable (circuit breaker open)")

        headers = self._query_headers(
            tenant_id=tenant_id, kb_id=kb_id, trace_id=trace_id, user_id=user_id,
        )
        url = f"{self.base_url}/query"

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._client.post(url, headers=headers, json=body)
                self._breaker.record_success()
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                self._breaker.record_failure()
                last_exc = LightRAGTimeoutError(
                    f"LightRAG timeout after {self._client.timeout.read}s"
                )
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
                logger.warning(
                    f"LightRAG query attempt {attempt}/{self._max_retries} "
                    f"failed: {last_exc}. Retrying in {backoff}s"
                )
                await asyncio.sleep(backoff)

        raise last_exc  # type: ignore[misc]

    # ── Storage read ───────────────────────────────────────────────────

    async def get_summary(self, tenant_id: str, kb_id: str) -> dict:
        """GET /graph/summary."""
        return await self._get_json("/graph/summary", tenant_id, kb_id)

    async def get_documents(
        self, tenant_id: str, kb_id: str, *,
        page: int = 1, page_size: int = 20,
        keyword: str = "", status: str = "",
        document_id: str = "", file_path: str = "",
    ) -> dict:
        """POST /documents/paginated."""
        body: dict[str, Any] = {"page": page, "page_size": page_size}
        if keyword:
            body["keyword"] = keyword
        if status:
            body["status"] = status
        if document_id:
            body["doc_id"] = document_id
        if file_path:
            body["file_path"] = file_path
        return await self._post("/documents/paginated", tenant_id, kb_id, body)

    async def get_entities(
        self, tenant_id: str, kb_id: str, *,
        page: int = 1, page_size: int = 20,
        keyword: str = "", entity_type: str = "",
        document_id: str = "", file_path: str = "",
    ) -> dict:
        """GET /graph/entities."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if keyword:
            params["keyword"] = keyword
        if entity_type:
            params["entity_type"] = entity_type
        if document_id:
            params["doc_id"] = document_id
        if file_path:
            params["file_path"] = file_path
        return await self._get_json("/graph/entities", tenant_id, kb_id, params=params)

    async def get_relationships(
        self, tenant_id: str, kb_id: str, *,
        page: int = 1, page_size: int = 20,
        keyword: str = "", document_id: str = "", file_path: str = "",
        source_entity: str = "", target_entity: str = "",
    ) -> dict:
        """GET /graph/relationships."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if keyword:
            params["keyword"] = keyword
        if document_id:
            params["doc_id"] = document_id
        if file_path:
            params["file_path"] = file_path
        if source_entity:
            params["source_entity"] = source_entity
        if target_entity:
            params["target_entity"] = target_entity
        result = await self._get_json(
            "/graph/relationships", tenant_id, kb_id, params=params
        )
        # ── [jonex] v2 关系响应归一化 ─────────────────────────────
        # LightRAG 图端点返回 src_id/tgt_id，而平台 v1 关系契约和
        # OntologyExtractor 使用 source_entity/target_entity。v2 在 HTTP
        # 客户端边界统一补齐规范字段，同时保留原字段，避免破坏现有调用方。
        items = result.get("items")
        if isinstance(items, list):
            result = {
                **result,
                "items": [
                    {
                        **item,
                        "source_entity": item.get("source_entity")
                        or item.get("src_id", ""),
                        "target_entity": item.get("target_entity")
                        or item.get("tgt_id", ""),
                    }
                    if isinstance(item, dict)
                    else item
                    for item in items
                ],
            }
        # ── [jonex] end ─────────────────────────────────────────────
        return result

    async def get_graph_summary(
        self, tenant_id: str, kb_id: str, *,
        document_id: str = "", file_path: str = "",
    ) -> dict:
        """GET /graph/counts."""
        params: dict[str, Any] = {}
        if document_id:
            params["doc_id"] = document_id
        if file_path:
            params["file_path"] = file_path
        return await self._get_json("/graph/counts", tenant_id, kb_id, params=params)

    async def get_graph(
        self, tenant_id: str, kb_id: str, *,
        limit: int = 200, keyword: str = "",
        document_id: str = "", file_path: str = "",
    ) -> dict:
        """GET /graph/entities (full graph view)."""
        params: dict[str, Any] = {"limit": limit}
        if keyword:
            params["keyword"] = keyword
        if document_id:
            params["doc_id"] = document_id
        if file_path:
            params["file_path"] = file_path
        return await self._get_json("/graph/entities", tenant_id, kb_id, params=params)

    async def get_document_parse_result(
        self, tenant_id: str, kb_id: str, *,
        document_id: str,
    ) -> dict:
        """Aggregate documents + entities + relationships for one document.

        document_id is the client-side document_id (from KB's KnowledgeDocument.id).
        Filtered by file_source prefix matching on :9621 side.
        """
        filter_params = {"doc_id": document_id}
        filter_body = {"doc_id": document_id}
        docs = await self._post("/documents/paginated", tenant_id, kb_id, filter_body)
        entities = await self._get_json("/graph/entities", tenant_id, kb_id, params=filter_params)
        relations = await self._get_json("/graph/relationships", tenant_id, kb_id, params=filter_params)
        return {
            "documents": docs,
            "entities": entities,
            "relationships": relations,
        }

    # ── Probe ──────────────────────────────────────────────────────────

    async def doc_exists(
        self, doc_id: str, *, tenant_id: str, kb_id: str,
    ) -> bool:
        """GET /documents/{doc_id} → True if 200, False if 404.

        doc_id is server-side chunk doc_id (not client document_id).

        [jonex] P0-1: 改用 GET /documents/{doc_id}（LightRAG vendored 新增端点），
        原 HEAD 同一路径不存在。
        """
        try:
            await self._get_json(
                f"/documents/{doc_id}", tenant_id, kb_id,
            )
            return True
        except LightRAGError as e:
            if e.code == 404:
                return False
            raise

    # ── Probe: single document status ─────────────────────────────────

    # [jonex] P0-1: query a single document's current status from LightRAG.
    # Used by the pipeline polling layer to perform three-state dup adjudication
    # (processed → benign success, pending/processing → wait, failed → hard_failed).
    # Requires the vendored GET /documents/{doc_id} route in LightRAG
    # (Reference/LightRAG/lightrag/api/routers/document_routes.py).
    async def get_document_status(
        self, doc_id: str, *, tenant_id: str, kb_id: str,
    ) -> dict | None:
        """GET /documents/{doc_id} → document info dict, or None if not found.

        doc_id is the LightRAG-internal chunk doc_id (e.g. ``doc-2a4d6557...``).
        Returns the full document record (including ``status``) on success,
        ``None`` on 404.

        [jonex] P0-1: 依赖 LightRAG vendored GET /documents/{doc_id} 端点。
        None 返回会打印 WARNING 日志，避免静默 no-op 瞒过排查。
        """
        try:
            return await self._get_json(
                f"/documents/{doc_id}", tenant_id, kb_id,
            )
        except LightRAGError as e:
            if e.code == 404:
                logger.warning(
                    "get_document_status(%s) → 404: doc 不存在或端点未部署。"
                    " 若 LightRAG 镜像未包含 [jonex] GET /documents/{doc_id}，"
                    " P0-1 dup 三态判定将静默退化为 hard_failed。",
                    doc_id,
                )
                return None
            raise

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def breaker_state(self) -> str:
        """Expose circuit breaker state for health checks."""
        return self._breaker.state
