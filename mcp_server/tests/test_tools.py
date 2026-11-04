#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP Server tool handler tests — HTTP contract + Auth integration + tenant isolation.

Organized into four test categories:

- **TestCallGateway** (8 tests): URL / headers / body / data contract for ``call_gateway()``.
- **TestCallGatewayErrors** (5 tests): HTTP errors, sanitization, transport failures.
- **TestToolAuthIntegration** (7 tests): Auth guard integration for tool handlers.

All tests mock ``tools.get_http_client`` and ``tools.call_gateway`` because the
real implementations are created by the parallel plan 04-02.  The test shapes
validate the *interface contract* defined in 04-CONTEXT.md.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult

# Ensure project root is on sys.path.
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _tool_error(result):
    """工具 handler 的 McpToolError 现在由 ``_inject_mcp_tool`` 翻译成
    ``CallToolResult(isError=True)``（码值在 ``structuredContent``）。

    断言线上形态并还原出 ``value.code / value.message / value.data``，
    让既有断言语义保持不变，同时顺便锁住「错误必须带码值」这一契约。
    """
    from mcp_server.tools import McpToolError

    assert isinstance(result, CallToolResult), f"expected CallToolResult, got {type(result)!r}"
    assert result.isError is True
    sc = result.structuredContent or {}
    data = sc.get("data") or {}
    err = McpToolError(sc["code"], sc["message"], data)
    # McpAuthError 路径：码值统一 -32002，原始 HTTP 状态码在 data.status_code
    if "status_code" in data:
        err.status_code = data["status_code"]
    return SimpleNamespace(value=err)


# ============================================================================
# Fixture helpers
# ============================================================================


@pytest.fixture
def patched_call_gateway():
    """Patch ``mcp_server.tools.call_gateway`` with a real-like implementation.

    Because the real ``call_gateway`` is a stub (created by parallel plan 04-02),
    this fixture provides a contract-compliant version that actually makes
    HTTP calls through a mock client.

    Returns the mock client so tests can inspect request details.
    """
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"success": True, "data": {"items": [], "total": 0}}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    async def _call_gateway(action, tenant_id, mcp_key_id, data=None):
        client = mock_client
        resp = await client.post(
            "/internal/kb/invoke",
            json={
                "action": action,
                "tenant_id": tenant_id,
                "mcp_key_id": mcp_key_id,
                "data": data or {},
            },
            headers={
                "X-Internal-API-Key": "test-api-key",
                "X-Tenant-ID": tenant_id,
            },
        )
        resp.raise_for_status()
        result = resp.json()
        if not result.get("success", False):
            raise Exception(result.get("message", "Unknown error"))
        return result.get("data") or {}

    with patch("mcp_server.tools.call_gateway", side_effect=_call_gateway):
        yield mock_client


# ============================================================================
# TestCallGateway — HTTP contract verification (8 tests)
# ============================================================================


class TestCallGateway:
    """Verify the HTTP contract of ``call_gateway()``.

    Each test sets up a mock HTTP client and calls ``call_gateway`` with
    known parameters, then inspects the HTTP request that was made.
    """

    @pytest.mark.asyncio
    async def test_url_is_kb_invoke(self, patched_call_gateway):
        """call_gateway() MUST POST to ``/internal/kb/invoke``."""
        from mcp_server.tools import call_gateway

        await call_gateway("list_services", "tenant_t", "key-1")

        call_args = patched_call_gateway.post.call_args
        url = call_args[0][0] if call_args and call_args[0] else None
        assert url == "/internal/kb/invoke", f"Expected /internal/kb/invoke, got {url}"

    @pytest.mark.asyncio
    async def test_headers_contain_internal_api_key(self, patched_call_gateway):
        """call_gateway() MUST send ``X-Internal-API-Key`` in headers."""
        from mcp_server.tools import call_gateway

        await call_gateway("search", "tenant_t", "key-1")

        call_kwargs = patched_call_gateway.post.call_args
        headers = call_kwargs[1].get("headers", {})
        assert "X-Internal-API-Key" in headers, "Missing X-Internal-API-Key header"

    @pytest.mark.asyncio
    async def test_headers_contain_x_tenant_id(self, patched_call_gateway):
        """call_gateway() MUST send ``X-Tenant-ID`` in headers matching ``tenant_id``."""
        from mcp_server.tools import call_gateway

        await call_gateway("search", "tenant_jonex_demo", "key-1")

        call_kwargs = patched_call_gateway.post.call_args
        headers = call_kwargs[1].get("headers", {})
        assert headers.get("X-Tenant-ID") == "tenant_jonex_demo", (
            f"Expected X-Tenant-ID=tenant_jonex_demo, got {headers.get('X-Tenant-ID')}"
        )

    @pytest.mark.asyncio
    async def test_body_contains_action(self, patched_call_gateway):
        """call_gateway() body MUST contain ``action`` field."""
        from mcp_server.tools import call_gateway

        await call_gateway("list_services", "tenant_t", "key-1")

        call_kwargs = patched_call_gateway.post.call_args
        body = call_kwargs[1].get("json", {})
        assert body.get("action") == "list_services", (
            f"Expected action=list_services, got {body.get('action')}"
        )

    @pytest.mark.asyncio
    async def test_body_contains_tenant_id(self, patched_call_gateway):
        """call_gateway() body MUST contain ``tenant_id`` field."""
        from mcp_server.tools import call_gateway

        await call_gateway("search", "tenant_x", "key-1")

        call_kwargs = patched_call_gateway.post.call_args
        body = call_kwargs[1].get("json", {})
        assert body.get("tenant_id") == "tenant_x"

    @pytest.mark.asyncio
    async def test_body_contains_mcp_key_id(self, patched_call_gateway):
        """call_gateway() body MUST contain ``mcp_key_id`` field."""
        from mcp_server.tools import call_gateway

        await call_gateway("search", "tenant_t", "mcp-key-002")

        call_kwargs = patched_call_gateway.post.call_args
        body = call_kwargs[1].get("json", {})
        assert body.get("mcp_key_id") == "mcp-key-002"

    @pytest.mark.asyncio
    async def test_body_contains_data_field(self, patched_call_gateway):
        """call_gateway() body MUST contain ``data`` field when provided."""
        from mcp_server.tools import call_gateway

        payload = {"query": "test", "top_k": 10}
        await call_gateway("search", "tenant_t", "key-1", data=payload)

        call_kwargs = patched_call_gateway.post.call_args
        body = call_kwargs[1].get("json", {})
        assert body.get("data") == payload

    @pytest.mark.asyncio
    async def test_data_none_defaults_to_empty_dict(self, patched_call_gateway):
        """call_gateway() with ``data=None`` MUST send ``data: {}`` (not null)."""
        from mcp_server.tools import call_gateway

        await call_gateway("list_services", "tenant_t", "key-1", data=None)

        call_kwargs = patched_call_gateway.post.call_args
        body = call_kwargs[1].get("json", {})
        assert body.get("data") == {}, (
            f"Expected data={{}}, got {body.get('data')}"
        )


# ============================================================================
# TestCallGatewayErrors — error handling and sanitization (5 tests)
# ============================================================================


class TestCallGatewayErrors:
    """Verify error handling of ``call_gateway()``:

    - HTTP 500 / 503 → exception
    - Error messages containing SQL / stack traces / internal URLs → sanitized
    - Transport errors → exception
    """

    @pytest.mark.asyncio
    async def test_http_500_raises(self):
        """call_gateway() MUST raise on HTTP 500."""
        from mcp_server.tools import call_gateway

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"success": False, "message": "Internal error"}
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("mcp_server.tools.get_http_client", return_value=mock_client):
            with patch("mcp_server.config.settings.INTERNAL_API_KEY", "test-key"):
                with pytest.raises(Exception):
                    await call_gateway("search", "tenant_t", "key-1")

    @pytest.mark.asyncio
    async def test_http_503_raises(self):
        """call_gateway() MUST raise on HTTP 503 (service unavailable)."""
        from mcp_server.tools import call_gateway

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.json.return_value = {"success": False, "message": "Service unavailable"}
        mock_resp.raise_for_status.side_effect = Exception("503 Service Unavailable")
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("mcp_server.tools.get_http_client", return_value=mock_client):
            with patch("mcp_server.config.settings.INTERNAL_API_KEY", "test-key"):
                with pytest.raises(Exception):
                    await call_gateway("search", "tenant_t", "key-1")

    @pytest.mark.asyncio
    async def test_sql_in_error_is_sanitized(self):
        """call_gateway() error messages MUST NOT contain SQL statements (SEC-05)."""
        from mcp_server.tools import call_gateway

        sql_error_msg = (
            "ERROR: SELECT * FROM platform.mcp_keys WHERE key_hash = 'abc';"
        )
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": False,
            "message": sql_error_msg,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("mcp_server.tools.get_http_client", return_value=mock_client):
            with patch("mcp_server.config.settings.INTERNAL_API_KEY", "test-key"):
                try:
                    await call_gateway("search", "tenant_t", "key-1")
                except Exception as exc:
                    error_str = str(exc)
                    # SQL keywords should NOT appear in the exposed error.
                    assert "SELECT" not in error_str.upper(), (
                        f"SQL leaked in error: {error_str}"
                    )

    @pytest.mark.asyncio
    async def test_stack_trace_in_error_is_sanitized(self):
        """call_gateway() error messages MUST NOT contain stack traces (SEC-05)."""
        from mcp_server.tools import call_gateway

        stack_error_msg = (
            'File "/app/mcp_server/tools.py", line 42, in call_gateway\n'
            "    raise RuntimeError('boom')\n"
            "RuntimeError: boom"
        )
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": False,
            "message": stack_error_msg,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("mcp_server.tools.get_http_client", return_value=mock_client):
            with patch("mcp_server.config.settings.INTERNAL_API_KEY", "test-key"):
                try:
                    await call_gateway("search", "tenant_t", "key-1")
                except Exception as exc:
                    error_str = str(exc)
                    # "File ... line ..." stack frames should not leak.
                    assert 'File "' not in error_str, (
                        f"Stack trace leaked in error: {error_str}"
                    )

    @pytest.mark.asyncio
    async def test_transport_error_raises(self):
        """call_gateway() MUST raise on TransportError (connection refused etc)."""
        from mcp_server.tools import call_gateway

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("Connection refused"))

        with patch("mcp_server.tools.get_http_client", return_value=mock_client):
            with patch("mcp_server.config.settings.INTERNAL_API_KEY", "test-key"):
                with pytest.raises(Exception):
                    await call_gateway("search", "tenant_t", "key-1")


# ============================================================================
# TestCallGateway429 — 429 rate limit detection (2 tests)
# ============================================================================


class TestCallGateway429:
    """Verify that ``call_gateway()`` correctly handles HTTP 429 responses.

    When Sidecar returns 429, call_gateway MUST:
    - Convert it to a ``McpToolError`` with JSON-RPC error code -32000
    - Extract ``retry_after`` from the ``Retry-After`` header
    - Default to 60 seconds when ``Retry-After`` is missing
    """

    @pytest.mark.asyncio
    async def test_call_gateway_429_converts_to_mcp_tool_error(self):
        """HTTP 429 response raises McpToolError with code=-32000."""
        import httpx
        from mcp_server.tools import call_gateway, McpToolError

        mock_request = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "30"}

        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "429 Too Many Requests",
                request=mock_request,
                response=mock_response,
            )
        )

        with patch("mcp_server.tools.get_http_client", return_value=mock_client):
            with patch("mcp_server.config.settings.INTERNAL_API_KEY", "test-key"):
                with pytest.raises(McpToolError) as exc_info:
                    await call_gateway("search", "tenant_t", "key-1")

        assert exc_info.value.code == -32001, (
            f"Expected code=-32001, got {exc_info.value.code}"
        )
        assert exc_info.value.data is not None
        assert exc_info.value.data["retry_after"] == 30, (
            f"Expected retry_after=30, got {exc_info.value.data}"
        )

    @pytest.mark.asyncio
    async def test_call_gateway_429_missing_retry_after_defaults_to_60(self):
        """Missing Retry-After header → defaults to 60 seconds."""
        import httpx
        from mcp_server.tools import call_gateway, McpToolError

        mock_request = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}  # No Retry-After header

        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "429 Too Many Requests",
                request=mock_request,
                response=mock_response,
            )
        )

        with patch("mcp_server.tools.get_http_client", return_value=mock_client):
            with patch("mcp_server.config.settings.INTERNAL_API_KEY", "test-key"):
                with pytest.raises(McpToolError) as exc_info:
                    await call_gateway("search", "tenant_t", "key-1")

        assert exc_info.value.code == -32001
        assert exc_info.value.data == {"retry_after": 60}, (
            f"Expected default retry_after=60, got {exc_info.value.data}"
        )


# ============================================================================
# TestToolAuthIntegration — Auth guard + tenant isolation (7 tests)
# ============================================================================


class TestToolAuthIntegration:
    """Verify that tool handlers correctly integrate with auth guards.

    Tools MUST call ``require_mcp_auth`` to extract tenant/key context,
    and ``require_kb_scope`` for scoped tools.
    """

    @pytest.mark.asyncio
    async def test_missing_auth_raises_401(self):
        """Calling a tool without valid auth context MUST raise McpAuthError(401)."""
        from mcp_server.auth import McpAuthError

        # Simulate: contextvar is empty → require_mcp_auth raises
        with patch("mcp_server.tools.require_mcp_auth") as mock_guard:
            mock_guard.side_effect = McpAuthError(401, "需要认证")

            with pytest.raises(McpAuthError) as exc_info:
                mock_guard("read")
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_key_tool_runs(self, mock_auth_context, patched_call_gateway):
        """With valid auth context, tool MUST execute and return result."""
        from mcp_server.tools import call_gateway

        result = await call_gateway(
            "list_services",
            mock_auth_context.tenant_id,
            mock_auth_context.key_id,
        )
        assert result is not None
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_kb_scope_denied_raises_401(self):
        """KB out of scope MUST raise McpAuthError(401)."""
        from mcp_server.auth import McpAuthError, McpAuthContext

        ctx = McpAuthContext(
            key_id="key-1",
            tenant_id="tenant_t",
            service_permissions={"svc-1": "call"},
            allowed_kb_ids=["kb_001"],  # only kb_001 allowed
        )
        token = McpAuthContext.set(ctx)
        try:
            # require_kb_scope should deny kb_999
            with patch("mcp_server.auth.require_kb_scope") as mock_scope:
                mock_scope.side_effect = McpAuthError(401, "认证失败")

                with pytest.raises(McpAuthError) as exc_info:
                    mock_scope(ctx, "kb_999")
                assert exc_info.value.status_code == 401
        finally:
            McpAuthContext.reset(token)

    @pytest.mark.asyncio
    async def test_read_source_scope_denied_raises_401(self):
        """read_source with denied KB scope MUST raise McpAuthError(401)."""
        from mcp_server.auth import McpAuthError, McpAuthContext

        ctx = McpAuthContext(
            key_id="key-2",
            tenant_id="tenant_t",
            service_permissions={"svc-1": "call"},
            allowed_kb_ids=["kb_001"],
        )
        token = McpAuthContext.set(ctx)
        try:
            with patch("mcp_server.auth.require_kb_scope") as mock_scope:
                mock_scope.side_effect = McpAuthError(401, "认证失败")

                with pytest.raises(McpAuthError):
                    mock_scope(ctx, "kb_other")
        finally:
            McpAuthContext.reset(token)

    @pytest.mark.asyncio
    async def test_empty_allowed_kb_ids_allows_all(self):
        """allowed_kb_ids=[] MUST allow all KBs (no scope restriction)."""
        from mcp_server.auth import require_kb_scope, McpAuthContext

        ctx = McpAuthContext(
            key_id="key-3",
            tenant_id="tenant_t",
            service_permissions={"svc-1": "call"},
            allowed_kb_ids=[],  # empty = all KBs
        )
        # require_kb_scope should NOT raise for any kb_id
        require_kb_scope(ctx, "kb_any")
        require_kb_scope(ctx, "kb_another")
        # No exception = pass

    @pytest.mark.asyncio
    async def test_tenant_id_from_auth_context_not_hardcoded(self, patched_call_gateway):
        """Tool MUST use tenant_id from auth context, NOT a hardcoded value."""
        from mcp_server.tools import call_gateway

        custom_tenant = "tenant_custom_123"
        await call_gateway("list_services", custom_tenant, "key-1")

        call_kwargs = patched_call_gateway.post.call_args
        body = call_kwargs[1].get("json", {})
        assert body.get("tenant_id") == custom_tenant, (
            f"Expected tenant_id={custom_tenant}, got {body.get('tenant_id')} "
            "(tenant_id must come from auth context, not hardcoded)"
        )

    @pytest.mark.asyncio
    async def test_list_domain_services_does_not_call_kb_scope(self):
        """list_domain_services does NOT call require_kb_scope (no KB ID needed)."""
        import mcp_server.auth as auth_mod
        from mcp_server.auth import McpAuthContext

        ctx = McpAuthContext(
            key_id="key-1",
            tenant_id="tenant_t",
            service_permissions={"svc-1": "call"},
            allowed_kb_ids=["kb_001"],
        )
        token = McpAuthContext.set(ctx)
        try:
            with patch.object(auth_mod, "require_mcp_auth", return_value=ctx) as mock_auth:
                with patch.object(auth_mod, "require_kb_scope") as mock_scope:
                    result = auth_mod.require_mcp_auth("read")
                    assert result == ctx
                    mock_auth.assert_called_once_with("read")
                    mock_scope.assert_not_called()
        finally:
            McpAuthContext.reset(token)

    @pytest.mark.asyncio
    async def test_list_domain_services_passes_through_kb_types(self):
        """list_domain_services 原样透传 items 中的 kb_types 字段。"""
        from mcp_server.auth import McpAuthContext
        from mcp_server.tools import list_domain_services

        auth_ctx = McpAuthContext(
            key_id="key-1",
            tenant_id="tenant_t",
            service_permissions={"svc-1": "call"},
        )
        items = [
            {
                "id": "svc-1",
                "kb_ids": ["kb_a", "kb_b"],
                "kb_types": ["lightrag", "openkb"],
            }
        ]
        with patch("mcp_server.tools.require_mcp_auth", return_value=auth_ctx):
            with patch(
                "mcp_server.tools.call_gateway",
                new=AsyncMock(return_value={"items": items, "total": 1}),
            ):
                result = await list_domain_services()

        assert result["items"][0]["kb_ids"] == ["kb_a", "kb_b"]
        assert result["items"][0]["kb_types"] == ["lightrag", "openkb"]
        assert result["total"] == 1


# ============================================================================
# TestConfirmUpload — confirm_upload tool handler tests（直传确认，base64 已移除）
# ============================================================================


class TestConfirmUpload:
    """confirm_upload tool handler — validation + auth + Gateway contract tests."""

    # ── Test 1: Parameter validation (CF-01) ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_validation_empty_params(self):
        """All required params empty → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, confirm_upload

        # service_id empty
        exc_info = _tool_error(await confirm_upload(service_id="", file_name="test.pdf", storage_key="s3://x", knowledge_base_id="kb-1"))
        assert exc_info.value.code == -32602

        # file_name empty
        exc_info = _tool_error(await confirm_upload(service_id="svc-1", file_name="", storage_key="s3://x", knowledge_base_id="kb-1"))
        assert exc_info.value.code == -32602

        # storage_key empty
        exc_info = _tool_error(await confirm_upload(service_id="svc-1", file_name="test.pdf", storage_key="", knowledge_base_id="kb-1"))
        assert exc_info.value.code == -32602

        # knowledge_base_id empty
        exc_info = _tool_error(await confirm_upload(service_id="svc-1", file_name="test.pdf", storage_key="s3://x", knowledge_base_id=""))
        assert exc_info.value.code == -32602

    # ── Test 2: Unauthorized (CF-02) ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_unauthorized(self):
        """require_mcp_auth('write') raises McpAuthError(401) when no auth."""
        import pytest as _pytest
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import confirm_upload
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "需要认证"),
        ):
            exc_info = _tool_error(await confirm_upload(service_id="svc-1", file_name="test.pdf", storage_key="s3://x", knowledge_base_id="kb-1"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401

    # ── Test 3: No write permission (CF-03) ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_write_permission(self):
        """require_mcp_auth('write') raises McpAuthError(401) without write perm."""
        import pytest as _pytest
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import confirm_upload
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "权限不足"),
        ):
            exc_info = _tool_error(await confirm_upload(service_id="svc-1", file_name="test.pdf", storage_key="s3://x", knowledge_base_id="kb-1"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401

    # ── Test 3b: write 授权只看 write_grants ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_service_key_type_denied_for_write(self):
        """service key 即便 permissions=["write"] 也不放行 confirm_upload（真实 require_mcp_auth）。"""
        import pytest as _pytest
        from mcp_server.auth import McpAuthContext, McpAuthError
        from mcp_server.tools import confirm_upload

        ctx = McpAuthContext(
            key_id="k", tenant_id="tenant_jonex_demo",
            service_permissions={"svc-1": "call"},
        )
        token = McpAuthContext.set(ctx)
        try:
            exc_info = _tool_error(await confirm_upload(service_id="svc-1", file_name="test.pdf", storage_key="s3://x", knowledge_base_id="kb-1"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401
        finally:
            McpAuthContext.reset(token)

    @pytest.mark.asyncio
    async def test_write_key_type_allowed(self):
        """write key（write_grants 非空）放行 confirm_upload（真实 require_mcp_auth + call_gateway）。"""
        from mcp_server.auth import McpAuthContext
        from mcp_server.tools import confirm_upload
        from unittest.mock import AsyncMock, patch

        expected = {"doc_id": "test-doc-001", "status": "PARSING"}
        mock_call = AsyncMock(return_value=expected)

        ctx = McpAuthContext(
            key_id="wk1", tenant_id="tenant_jonex_demo",
            write_grants=[{"kb": "kb-1", "mode": "all", "directories": []}],
            allowed_kb_ids=["kb-1"],
        )
        token = McpAuthContext.set(ctx)
        try:
            with patch("mcp_server.tools.call_gateway", mock_call):
                result = await confirm_upload(
                    service_id="svc-1",
                    file_name="report.pdf",
                    storage_key="jonex/kb/tenant_jonex_demo/kb-1/doc-1/report.pdf",
                    knowledge_base_id="kb-1",
                )
        finally:
            McpAuthContext.reset(token)

        assert result == expected

    # ── Test 5: Path traversal in file_name ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_filename_path_traversal(self):
        """file_name with ../ path traversal → McpToolError(-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, confirm_upload

        exc_info = _tool_error(await confirm_upload(service_id="svc-1", file_name="../../../etc/passwd", storage_key="s3://x", knowledge_base_id="kb-1"))
        assert exc_info.value.code == -32602
        assert "非法" in exc_info.value.message

    # ── Test 6: Empty file_name ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_filename_empty(self):
        """file_name empty or all whitespace → McpToolError(-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, confirm_upload

        # empty string
        exc_info = _tool_error(await confirm_upload(service_id="svc-1", file_name="", storage_key="s3://x", knowledge_base_id="kb-1"))
        assert exc_info.value.code == -32602
        assert "不能为空" in exc_info.value.message

        # all whitespace
        exc_info = _tool_error(await confirm_upload(service_id="svc-1", file_name="   ", storage_key="s3://x", knowledge_base_id="kb-1"))
        assert exc_info.value.code == -32602
        assert "不能为空" in exc_info.value.message

    # ── Test 9: KB not in scope ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_kb_not_in_scope(self, mock_auth_context):
        """knowledge_base_id not in scope → McpAuthError(401)."""
        import pytest as _pytest
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import confirm_upload
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth", return_value=mock_auth_context
        ):
            with patch(
                "mcp_server.tools.require_write_scope",
                side_effect=McpAuthError(401, "不在授权范围内"),
            ):
                exc_info = _tool_error(
                    await confirm_upload(
                        service_id="svc-1", file_name="test.pdf", storage_key="s3://x", knowledge_base_id="kb-1"
                    )
                )
                assert exc_info.value.code == -32002
                assert exc_info.value.status_code == 401
                assert "不在授权范围内" in exc_info.value.message

    # ── Test 11: storage_key 确认入库（直传闭环） ────────────────────────────

    @pytest.mark.asyncio
    async def test_storage_key_confirm(self):
        """storage_key 模式 → call_gateway(action=upload_document) 传 storage_key/doc_id 确认入库。"""
        from mcp_server.auth import McpAuthContext
        from mcp_server.tools import confirm_upload
        from unittest.mock import AsyncMock, patch

        expected = {"doc_id": "doc-123", "status": "PARSING"}
        mock_call = AsyncMock(return_value=expected)

        ctx = McpAuthContext(
            key_id="wk1",
            tenant_id="tenant_jonex_demo",
            write_grants=[{"kb": "kb-1", "mode": "all", "directories": []}],
        )
        token = McpAuthContext.set(ctx)
        try:
            with patch("mcp_server.tools.call_gateway", mock_call):
                result = await confirm_upload(
                    service_id="svc-1",
                    file_name="test.pdf",
                    knowledge_base_id="kb-1",
                    storage_key="jonex/kb/tenant_jonex_demo/kb-1/doc-123/doc-123_test.pdf",
                    doc_id="doc-123",
                )
        finally:
            McpAuthContext.reset(token)

        assert result == expected
        assert mock_call.call_args[0][0] == "upload_document"
        assert mock_call.call_args[0][1] == "tenant_jonex_demo"
        assert mock_call.call_args[0][2] == "wk1"
        payload = mock_call.call_args[0][3]
        assert payload["knowledge_base_id"] == "kb-1"
        assert payload["storage_key"] == "jonex/kb/tenant_jonex_demo/kb-1/doc-123/doc-123_test.pdf"
        assert payload["doc_id"] == "doc-123"
        assert payload["storage_backend"] == "local"


# ============================================================================
# TestGenerateUploadUrl — generate_upload_url（写 Key 专属直传）
# ============================================================================


class TestGenerateUploadUrl:
    """generate_upload_url tool handler — 写 Key 专属预签名直传 URL 工具，grants 级鉴权。"""

    @pytest.mark.asyncio
    async def test_validation_empty_params(self):
        """空 service_id / knowledge_base_id / file_name → McpToolError(-32602)。"""
        from mcp_server.tools import McpToolError, generate_upload_url

        exc_info = _tool_error(await generate_upload_url("", "kb-1", "test.pdf"))
        assert exc_info.value.code == -32602

        exc_info = _tool_error(await generate_upload_url("svc-1", "", "test.pdf"))
        assert exc_info.value.code == -32602

        exc_info = _tool_error(await generate_upload_url("svc-1", "kb-1", ""))
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_filename_path_traversal(self):
        """file_name 含 ../ 路径穿越 → McpToolError(-32602)。"""
        from mcp_server.tools import McpToolError, generate_upload_url

        exc_info = _tool_error(await generate_upload_url("svc-1", "kb-1", "../../../etc/passwd"))
        assert exc_info.value.code == -32602
        assert "非法" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_filename_empty(self):
        """file_name 全空白 → McpToolError(-32602, 不能为空)。"""
        from mcp_server.tools import McpToolError, generate_upload_url

        exc_info = _tool_error(await generate_upload_url("svc-1", "kb-1", "   "))
        assert exc_info.value.code == -32602
        assert "不能为空" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_unauthorized(self):
        """无写 Key → require_mcp_auth('write') 抛 McpAuthError(401)。"""
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import generate_upload_url
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "需要认证"),
        ):
            exc_info = _tool_error(await generate_upload_url("svc-1", "kb-1", "test.pdf"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_service_key_type_denied_for_write(self):
        """service key 即便 permissions=["write"] 也不放行（真实 require_mcp_auth）。"""
        from mcp_server.auth import McpAuthContext, McpAuthError
        from mcp_server.tools import generate_upload_url

        ctx = McpAuthContext(
            key_id="k",
            tenant_id="tenant_jonex_demo",
            service_permissions={"svc-1": "call"},
        )
        token = McpAuthContext.set(ctx)
        try:
            exc_info = _tool_error(await generate_upload_url("svc-1", "kb-1", "test.pdf"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401
        finally:
            McpAuthContext.reset(token)

    @pytest.mark.asyncio
    async def test_write_key_success(self):
        """write key + grants mode=all → call_gateway 返回 {doc_id, storage_key, upload_url, storage_backend}。"""
        from mcp_server.auth import McpAuthContext
        from mcp_server.tools import generate_upload_url
        from unittest.mock import AsyncMock, patch

        expected = {
            "doc_id": "doc-123",
            "storage_key": "tenant/kb-1/doc-123/test.pdf",
            "upload_url": "https://cos.example/put-url",
            "storage_backend": "cos",
        }
        mock_call = AsyncMock(return_value=expected)

        ctx = McpAuthContext(
            key_id="wk1",
            tenant_id="tenant_jonex_demo",
            write_grants=[{"kb": "kb-1", "mode": "all", "directories": []}],
        )
        token = McpAuthContext.set(ctx)
        try:
            with patch("mcp_server.tools.call_gateway", mock_call):
                result = await generate_upload_url(
                    "svc-1", "kb-1", "test.pdf", "application/pdf"
                )
        finally:
            McpAuthContext.reset(token)

        assert result == expected
        assert mock_call.call_args[0][0] == "generate_upload_url"
        assert mock_call.call_args[0][1] == "tenant_jonex_demo"
        assert mock_call.call_args[0][2] == "wk1"
        payload = mock_call.call_args[0][3]
        assert payload["knowledge_base_id"] == "kb-1"
        assert payload["file_name"] == "test.pdf"
        assert payload["content_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_write_key_success_with_file_size(self):
        """file_size > 0 → 透传进 payload；file_size = 0 → 不透传（保持向后兼容）。"""
        from mcp_server.auth import McpAuthContext
        from mcp_server.tools import generate_upload_url
        from unittest.mock import AsyncMock, patch

        ctx = McpAuthContext(
            key_id="wk1",
            tenant_id="tenant_jonex_demo",
            write_grants=[{"kb": "kb-1", "mode": "all", "directories": []}],
        )
        token = McpAuthContext.set(ctx)
        try:
            mock_call = AsyncMock(return_value={})
            with patch("mcp_server.tools.call_gateway", mock_call):
                await generate_upload_url("svc-1", "kb-1", "test.pdf", file_size=12345)
                assert mock_call.call_args[0][3]["file_size"] == 12345

            mock_call = AsyncMock(return_value={})
            with patch("mcp_server.tools.call_gateway", mock_call):
                await generate_upload_url("svc-1", "kb-1", "test.pdf", file_size=0)
                assert "file_size" not in mock_call.call_args[0][3]
        finally:
            McpAuthContext.reset(token)

    @pytest.mark.asyncio
    async def test_file_size_negative_rejected(self):
        """file_size < 0 → McpToolError(-32602)。"""
        from mcp_server.auth import McpAuthContext
        from mcp_server.tools import McpToolError, generate_upload_url

        ctx = McpAuthContext(
            key_id="wk1",
            tenant_id="tenant_jonex_demo",
            write_grants=[{"kb": "kb-1", "mode": "all", "directories": []}],
        )
        token = McpAuthContext.set(ctx)
        try:
            exc_info = _tool_error(await generate_upload_url("svc-1", "kb-1", "test.pdf", file_size=-1))
            assert exc_info.value.code == -32602
        finally:
            McpAuthContext.reset(token)

    @pytest.mark.asyncio
    async def test_specified_mode_rejected(self):
        """grants mode=specified → McpToolError(-32602, 目录级写入)。"""
        from mcp_server.auth import McpAuthContext
        from mcp_server.tools import McpToolError, generate_upload_url

        ctx = McpAuthContext(
            key_id="wk1",
            tenant_id="tenant_jonex_demo",
            write_grants=[{"kb": "kb-1", "mode": "specified", "directories": ["dir-1"]}],
        )
        token = McpAuthContext.set(ctx)
        try:
            exc_info = _tool_error(await generate_upload_url("svc-1", "kb-1", "test.pdf"))
            assert exc_info.value.code == -32602
            assert "目录级写入" in exc_info.value.message
        finally:
            McpAuthContext.reset(token)

    @pytest.mark.asyncio
    async def test_kb_not_in_scope(self):
        """kb 不在 grants → require_write_scope 抛 McpAuthError(401)。"""
        from mcp_server.auth import McpAuthContext, McpAuthError
        from mcp_server.tools import generate_upload_url

        ctx = McpAuthContext(
            key_id="wk1",
            tenant_id="tenant_jonex_demo",
            write_grants=[{"kb": "kb-other", "mode": "all", "directories": []}],
        )
        token = McpAuthContext.set(ctx)
        try:
            exc_info = _tool_error(await generate_upload_url("svc-1", "kb-1", "test.pdf"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401
        finally:
            McpAuthContext.reset(token)


# ============================================================================
# TestGetUploadStatus — get_upload_status（写 Key 专属）
# ============================================================================


class TestGetUploadStatus:
    """get_upload_status — 写 Key 专属状态查询，kb 范围由 grants 平铺决定。"""

    @pytest.mark.asyncio
    async def test_write_key_returns_status(self, mock_http_client):
        """写 Key（key_type=write + grants mode=all）→ 查状态，kb_ids 来自 grants 平铺。"""
        from mcp_server.auth import McpAuthContext
        from mcp_server.tools import get_upload_status
        from unittest.mock import patch

        mock_resp = mock_http_client.post.return_value
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "data": {"doc_id": "doc-1", "status": "READY"},
        }
        mock_resp.raise_for_status = mock_http_client.post.return_value.raise_for_status

        ctx = McpAuthContext(
            key_id="wk1",
            tenant_id="tenant_jonex_demo",
            write_grants=[
                {"kb": "kb-1", "mode": "all", "directories": []},
                {"kb": "kb-2", "mode": "all", "directories": []},
            ],
        )
        token = McpAuthContext.set(ctx)
        try:
            with patch("mcp_server.tools.get_http_client", return_value=mock_http_client):
                result = await get_upload_status("svc-ignored", "doc-1")
        finally:
            McpAuthContext.reset(token)

        assert result["status"] == "READY"
        payload = mock_http_client.post.call_args[1]["json"]
        assert payload["action"] == "get_document_status"
        assert payload["data"]["document_id"] == "doc-1"
        # kb_ids 来自写 Key 的 grants（mode=all 的 kb 平铺），非 service 解析
        assert payload["data"]["kb_ids"] == ["kb-1", "kb-2"]

    @pytest.mark.asyncio
    async def test_service_key_denied(self):
        """普通 Key（key_type=service）→ require_mcp_auth("write") 401。"""
        import pytest as _pytest
        from mcp_server.auth import McpAuthContext, McpAuthError
        from mcp_server.tools import get_upload_status

        ctx = McpAuthContext(
            key_id="k1",
            tenant_id="tenant_jonex_demo",
            service_permissions={"svc-1": "view"},
        )
        token = McpAuthContext.set(ctx)
        try:
            exc_info = _tool_error(await get_upload_status("svc-1", "doc-1"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401
        finally:
            McpAuthContext.reset(token)

    @pytest.mark.asyncio
    async def test_write_key_empty_grants(self):
        """写 Key 但 allowed_kb_ids 空（无 grants）→ McpToolError(-32000)。"""
        import pytest as _pytest
        from mcp_server.auth import McpAuthContext
        from mcp_server.tools import McpToolError, get_upload_status

        ctx = McpAuthContext(
            key_id="wk1",
            tenant_id="tenant_jonex_demo",
            write_grants=[{"kb": "kb-1", "mode": "specified", "directories": ["dir-1"]}],
        )
        token = McpAuthContext.set(ctx)
        try:
            exc_info = _tool_error(await get_upload_status("svc-1", "doc-1"))
            assert exc_info.value.code == -32000
        finally:
            McpAuthContext.reset(token)


# ============================================================================
# TestServiceScope — C3 交集校验
# ============================================================================


class TestServiceScope:
    """Verify C3: service-level permission check (returns [] = all KBs allowed)."""

    @pytest.mark.asyncio
    async def test_service_not_mapped_raises_401(self):
        """service 不在 service_permissions 映射 -> McpAuthError(401)。"""
        from mcp_server.tools import _check_service_scope
        from mcp_server.auth import McpAuthContext, McpAuthError

        ctx = McpAuthContext(
            key_id="key-1",
            tenant_id="tenant_t",
            service_permissions={},
        )
        with pytest.raises(McpAuthError) as exc_info:
            await _check_service_scope(ctx, "svc-1")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_insufficient_permission_raises_403(self):
        """service 映射为 view，要求 call -> McpAuthError(403)。"""
        from mcp_server.tools import _check_service_scope
        from mcp_server.auth import McpAuthContext, McpAuthError

        ctx = McpAuthContext(
            key_id="key-1",
            tenant_id="tenant_t",
            service_permissions={"svc-1": "view"},
        )
        with pytest.raises(McpAuthError) as exc_info:
            await _check_service_scope(ctx, "svc-1", required_permission="call")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_permission_sufficient_returns_empty(self):
        """权限通过 -> 返回 []（空 = 全部 KB 允许，调用方自行取 service 的 kb_ids）。"""
        from mcp_server.tools import _check_service_scope
        from mcp_server.auth import McpAuthContext

        ctx = McpAuthContext(
            key_id="key-1",
            tenant_id="tenant_t",
            service_permissions={"svc-1": "call"},
        )
        result = await _check_service_scope(ctx, "svc-1", required_permission="call")
        assert result == []


# ============================================================================
# TestSearchOntology — search_ontology tool handler tests
# ============================================================================


class TestSearchOntology:
    """search_ontology tool handler — validation + auth + Gateway contract tests."""

    # ── Parameter validation ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_validation_empty_service_id(self):
        """service_id="" → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_ontology

        exc_info = _tool_error(await search_ontology("", "test query"))
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_validation_empty_query(self):
        """query="" → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_ontology

        exc_info = _tool_error(await search_ontology("svc-1", ""))
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_validation_query_too_long(self):
        """query length > 2000 → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_ontology

        long_query = "x" * 2001
        exc_info = _tool_error(await search_ontology("svc-1", long_query))
        assert exc_info.value.code == -32602
        assert "2000" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_validation_invalid_mode(self):
        """mode="invalid" → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_ontology

        exc_info = _tool_error(await search_ontology("svc-1", "test query", mode="invalid"))
        assert exc_info.value.code == -32602
        assert "mode 必须为" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_validation_mode_alias(self, mock_service_scope_db):
        """mode="mix" → call_gateway receives mode="hybrid" (alias conversion)."""
        from mcp_server.tools import search_ontology
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_ontology("svc-1", "test query", mode="mix")

        call_args = mock_call.call_args[0]
        assert call_args[0] == "query_with_ontology"
        # mode in data should be "hybrid" after alias conversion
        assert mock_call.call_args[0][3]["mode"] == "hybrid"

    @pytest.mark.asyncio
    async def test_validation_top_k_out_of_range(self):
        """top_k=0 and top_k=101 → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_ontology

        # top_k=0 (below minimum)
        exc_info = _tool_error(await search_ontology("svc-1", "test query", top_k=0))
        assert exc_info.value.code == -32602

        # top_k=101 (above maximum)
        exc_info = _tool_error(await search_ontology("svc-1", "test query", top_k=101))
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_validation_strict_mode_true(self, mock_service_scope_db):
        """strict_mode=True → payload contains strict_mode=True."""
        from mcp_server.tools import search_ontology
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_ontology("svc-1", "test query", strict_mode=True)

        payload = mock_call.call_args[0][3]
        assert payload["strict_mode"] is True

    # ── Auth tests ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_auth_unauthorized(self):
        """require_mcp_auth raises McpAuthError(401) → exception propagates."""
        import pytest as _pytest
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import search_ontology
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "需要认证"),
        ):
            exc_info = _tool_error(await search_ontology("svc-1", "test query"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_no_call_permission(self):
        """require_mcp_auth raises McpAuthError(401, "权限不足") → 结构化 -32002。"""
        import pytest as _pytest
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import search_ontology
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "权限不足"),
        ):
            exc_info = _tool_error(await search_ontology("svc-1", "test query"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401

    # ── Success path ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_success_path(self, mock_service_scope_db):
        """Valid params + auth + scope → call_gateway returns success result."""
        from mcp_server.tools import search_ontology
        from unittest.mock import AsyncMock, MagicMock, patch

        expected_result = {"answer": "test answer", "references": [], "source": "ontology"}
        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=(expected_result, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    result = await search_ontology("svc-1", "test query", "hybrid", 5, False)

        assert result == expected_result
        # Verify call_gateway called with correct action
        assert mock_call.call_args[0][0] == "query_with_ontology"
        payload = mock_call.call_args[0][3]
        assert payload["service_id"] == "svc-1"
        assert payload["query"] == "test query"
        assert payload["mode"] == "hybrid"
        assert payload["top_k"] == 5
        assert payload["strict_mode"] is False

    # ── with_reasoning 透传 ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_with_reasoning_true(self, mock_service_scope_db):
        """with_reasoning=True → payload contains with_reasoning=True."""
        from mcp_server.tools import search_ontology
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_ontology("svc-1", "test query", "hybrid", 5, False, True)

        payload = mock_call.call_args[0][3]
        assert payload["with_reasoning"] is True

    @pytest.mark.asyncio
    async def test_with_reasoning_default_false(self, mock_service_scope_db):
        """with_reasoning omitted → payload contains with_reasoning=False."""
        from mcp_server.tools import search_ontology
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_ontology("svc-1", "test query")

        payload = mock_call.call_args[0][3]
        assert payload["with_reasoning"] is False


# ============================================================================
# TestSearchDeep — search_deep tool handler tests
# ============================================================================


class TestSearchDeep:
    """search_deep tool handler — validation + auth + Gateway contract tests."""

    # ── Parameter validation ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_validation_empty_service_id(self):
        """service_id="" → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_deep

        exc_info = _tool_error(await search_deep("", "test query"))
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_validation_empty_query(self):
        """query="" → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_deep

        exc_info = _tool_error(await search_deep("svc-1", ""))
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_validation_query_too_long(self):
        """query length > 2000 → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_deep

        long_query = "x" * 2001
        exc_info = _tool_error(await search_deep("svc-1", long_query))
        assert exc_info.value.code == -32602
        assert "2000" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_validation_invalid_mode(self):
        """mode="invalid" → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_deep

        exc_info = _tool_error(await search_deep("svc-1", "test query", mode="invalid"))
        assert exc_info.value.code == -32602
        assert "mode 必须为" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_validation_mode_alias(self, mock_service_scope_db):
        """mode="mix" → call_gateway receives mode="hybrid" (alias conversion)."""
        from mcp_server.tools import search_deep
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_deep("svc-1", "test query", mode="mix")

        assert mock_call.call_args[0][0] == "deep_query"
        assert mock_call.call_args[0][3]["mode"] == "hybrid"

    @pytest.mark.asyncio
    async def test_validation_top_k_out_of_range(self):
        """top_k=0 and top_k=101 → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_deep

        exc_info = _tool_error(await search_deep("svc-1", "test query", top_k=0))
        assert exc_info.value.code == -32602

        exc_info = _tool_error(await search_deep("svc-1", "test query", top_k=101))
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_validation_strict_mode_true(self, mock_service_scope_db):
        """strict_mode=True → payload contains strict_mode=True."""
        from mcp_server.tools import search_deep
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_deep("svc-1", "test query", strict_mode=True)

        payload = mock_call.call_args[0][3]
        assert payload["strict_mode"] is True

    # ── Auth tests ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_auth_unauthorized(self):
        """require_mcp_auth raises McpAuthError(401) → exception propagates."""
        import pytest as _pytest
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import search_deep
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "需要认证"),
        ):
            exc_info = _tool_error(await search_deep("svc-1", "test query"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_no_call_permission(self):
        """require_mcp_auth raises McpAuthError(401, "权限不足") → 结构化 -32002。"""
        import pytest as _pytest
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import search_deep
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "权限不足"),
        ):
            exc_info = _tool_error(await search_deep("svc-1", "test query"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401

    # ── Success path ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_success_path(self, mock_service_scope_db):
        """Valid params + auth + scope → call_gateway returns success result."""
        from mcp_server.tools import search_deep
        from unittest.mock import AsyncMock, MagicMock, patch

        expected_result = {"answer": "deep analysis result", "references": [], "source": "deep"}
        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=(expected_result, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    result = await search_deep("svc-1", "test query", "hybrid", 5, False)

        assert result == expected_result
        assert mock_call.call_args[0][0] == "deep_query"
        payload = mock_call.call_args[0][3]
        assert payload["service_id"] == "svc-1"
        assert payload["query"] == "test query"
        assert payload["mode"] == "hybrid"
        assert payload["top_k"] == 5
        assert payload["strict_mode"] is False

    # ── with_reasoning 透传 ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_with_reasoning_true(self, mock_service_scope_db):
        """with_reasoning=True → payload contains with_reasoning=True."""
        from mcp_server.tools import search_deep
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_deep("svc-1", "test query", "hybrid", 5, False, True)

        payload = mock_call.call_args[0][3]
        assert payload["with_reasoning"] is True


# ============================================================================
# TestSearchLlmwiki — search_llmwiki tool handler tests
# ============================================================================


class TestSearchLlmwiki:
    """search_llmwiki tool handler — validation + auth + feature flag + fallback tests."""

    # ── Parameter validation ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_validation_empty_service_id(self):
        """service_id="" → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_llmwiki

        exc_info = _tool_error(await search_llmwiki("", "test query"))
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_validation_empty_query(self):
        """query="" → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_llmwiki

        exc_info = _tool_error(await search_llmwiki("svc-1", ""))
        assert exc_info.value.code == -32602

    @pytest.mark.asyncio
    async def test_validation_query_too_long(self):
        """query length > 2000 → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_llmwiki

        long_query = "x" * 2001
        exc_info = _tool_error(await search_llmwiki("svc-1", long_query))
        assert exc_info.value.code == -32602
        assert "2000" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_validation_invalid_mode(self):
        """mode="invalid" → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_llmwiki

        exc_info = _tool_error(await search_llmwiki("svc-1", "test query", mode="invalid"))
        assert exc_info.value.code == -32602
        assert "mode 必须为" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_validation_mode_alias(self, mock_service_scope_db):
        """mode="mix" → call_gateway receives mode="hybrid" (alias conversion)."""
        from mcp_server.tools import search_llmwiki
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_llmwiki("svc-1", "test query", mode="mix")

        assert mock_call.call_args[0][0] == "search_llmwiki"
        assert mock_call.call_args[0][3]["mode"] == "hybrid"

    @pytest.mark.asyncio
    async def test_validation_top_k_out_of_range(self):
        """top_k=0 and top_k=101 → McpToolError(code=-32602)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_llmwiki

        exc_info = _tool_error(await search_llmwiki("svc-1", "test query", top_k=0))
        assert exc_info.value.code == -32602

        exc_info = _tool_error(await search_llmwiki("svc-1", "test query", top_k=101))
        assert exc_info.value.code == -32602

    # ── Auth tests ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_auth_unauthorized(self):
        """require_mcp_auth raises McpAuthError(401) → exception propagates."""
        import pytest as _pytest
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import search_llmwiki
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "需要认证"),
        ):
            exc_info = _tool_error(await search_llmwiki("svc-1", "test query"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_no_call_permission(self):
        """require_mcp_auth raises McpAuthError(401, "权限不足") → 结构化 -32002。"""
        import pytest as _pytest
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import search_llmwiki
        from unittest.mock import patch

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "权限不足"),
        ):
            exc_info = _tool_error(await search_llmwiki("svc-1", "test query"))
            assert exc_info.value.code == -32002
            assert exc_info.value.status_code == 401

    # ── Feature flag test ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_feature_flag_disabled(self):
        """SEARCH_LLMWIKI_ENABLED=False → McpToolError(code=-32000)."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_llmwiki
        from unittest.mock import patch

        with patch("mcp_server.tools.SEARCH_LLMWIKI_ENABLED", False):
            exc_info = _tool_error(await search_llmwiki("svc-1", "test query"))
            assert exc_info.value.code == -32000
            assert "LLM-Wiki 检索暂未开放" in exc_info.value.message

    # ── Pipeline fallback tests ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_pipeline_fallback(self, mock_service_scope_db):
        """McpToolError(-32602) on search_llmwiki → auto fallback to search_mix."""
        from mcp_server.tools import McpToolError, search_llmwiki
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock()
        mock_call.side_effect = [
            McpToolError(code=-32602, message="仅支持 OPENKB 管道知识库"),
            ({"answer": "fallback via mix", "source": "mixed"}, "req-fallback"),
        ]

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    result = await search_llmwiki("svc-1", "test query", "hybrid", 5)

        assert result["source"] == "mixed"
        assert mock_call.call_count == 2
        first_call_args = mock_call.call_args_list[0][0]
        assert first_call_args[0] == "search_llmwiki"
        second_call_args = mock_call.call_args_list[1][0]
        assert second_call_args[0] == "search_mix"

    @pytest.mark.asyncio
    async def test_pipeline_fallback_failure(self, mock_service_scope_db):
        """Both search_llmwiki and fallback fail → second error (-32000) propagates."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_llmwiki
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock()
        mock_call.side_effect = [
            McpToolError(code=-32602, message="仅支持 OPENKB 管道知识库"),
            McpToolError(code=-32000, message="知识库服务暂不可用"),
        ]

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    exc_info = _tool_error(await search_llmwiki("svc-1", "test query", "hybrid", 5))

        assert exc_info.value.code == -32000
        assert "知识库服务暂不可用" in exc_info.value.message
        assert mock_call.call_count == 2

    @pytest.mark.asyncio
    async def test_auth_error_no_fallback(self, mock_service_scope_db):
        """McpToolError with auth code (-32002) → should re-raise, NOT fallback."""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_llmwiki
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock()
        mock_call.side_effect = McpToolError(code=-32002, message="认证失败或权限不足")

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    exc_info = _tool_error(await search_llmwiki("svc-1", "test query", "hybrid", 5))

        # Auth error propagates directly — no fallback attempted
        assert exc_info.value.code == -32002
        assert "认证" in exc_info.value.message
        assert mock_call.call_count == 1  # Only search_llmwiki, no query_with_ontology

    @pytest.mark.asyncio
    async def test_server_error_falls_back(self, mock_service_scope_db):
        """McpToolError with internal code (-32000) → falls back to search_mix.

        Current source falls back on both -32602 (pipeline mismatch) and -32000
        (OpenKB runtime unavailable); only auth (-32002) and rate-limit (-32001)
        errors propagate without fallback.
        """
        import pytest as _pytest
        from mcp_server.tools import McpToolError, search_llmwiki
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock()
        mock_call.side_effect = McpToolError(code=-32000, message="知识库服务暂不可用")

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    exc_info = _tool_error(await search_llmwiki("svc-1", "test query", "hybrid", 5))

        # -32000 triggers fallback; the fallback call also raises -32000 and propagates
        assert exc_info.value.code == -32000
        assert mock_call.call_count == 2
        assert mock_call.call_args_list[0][0][0] == "search_llmwiki"
        assert mock_call.call_args_list[1][0][0] == "search_mix"

    # ── Success path ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_success_path(self, mock_service_scope_db):
        """Valid params + auth + scope → call_gateway returns success result."""
        from mcp_server.tools import search_llmwiki
        from unittest.mock import AsyncMock, MagicMock, patch

        expected_result = {"answer": "wiki answer", "references": [], "source": "openkb"}
        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=(expected_result, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    result = await search_llmwiki("svc-1", "test query", "hybrid", 5)

        assert result == expected_result
        assert mock_call.call_args[0][0] == "search_llmwiki"
        payload = mock_call.call_args[0][3]
        assert payload["service_id"] == "svc-1"
        assert payload["query"] == "test query"
        assert payload["mode"] == "hybrid"
        assert payload["top_k"] == 5
        # search_llmwiki does NOT have strict_mode — verify absent
        assert "strict_mode" not in payload

    # ── with_reasoning 透传 ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_with_reasoning_true(self, mock_service_scope_db):
        """with_reasoning=True → payload contains with_reasoning=True."""
        from mcp_server.tools import search_llmwiki
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_llmwiki("svc-1", "test query", "hybrid", 5, True)

        payload = mock_call.call_args[0][3]
        assert payload["with_reasoning"] is True

    @pytest.mark.asyncio
    async def test_with_reasoning_fallback_preserved(self, mock_service_scope_db):
        """fallback 到 search_mix 时，with_reasoning 仍透传（不丢失）。"""
        from mcp_server.tools import McpToolError, search_llmwiki
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock()
        mock_call.side_effect = [
            McpToolError(code=-32602, message="仅支持 OPENKB 管道知识库"),
            ({"answer": "fallback via mix", "source": "mixed"}, "req-fallback"),
        ]

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_llmwiki("svc-1", "test query", "hybrid", 5, True)

        assert mock_call.call_count == 2
        second_payload = mock_call.call_args_list[1][0][3]
        assert second_payload["with_reasoning"] is True


# ============================================================================
# TestSearchMix — search_mix tool handler tests
# ============================================================================


class TestSearchMix:
    """search_mix tool handler — with_reasoning 透传."""

    @pytest.mark.asyncio
    async def test_with_reasoning_true(self, mock_service_scope_db):
        """with_reasoning=True → payload contains with_reasoning=True."""
        from mcp_server.tools import search_mix
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_auth = MagicMock(tenant_id="tenant_t", key_id="key-1", allowed_kb_ids=[], service_permissions={"svc-1": "call"})
        mock_call = AsyncMock(return_value=({"answer": "result", "references": []}, "req-test"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                with patch("mcp_server.tools.require_write_scope"):
                    await search_mix("svc-1", "test query", "hybrid", 5, True)

        payload = mock_call.call_args[0][3]
        assert payload["with_reasoning"] is True


class TestValidateFileName:
    """_validate_file_name 白名单放宽：允许 () 空格 [] 全角（），路径穿越仍拒绝。"""

    @pytest.mark.parametrize(
        "name",
        [
            "report(1).pdf",            # 半角圆括号
            "my report.pdf",            # 半角空格
            "report[1].pdf",            # 半角方括号
            "报告（终版）.pdf",          # 全角圆括号
            "版本(1) 报告 [v2].pdf",     # 混合
        ],
    )
    def test_accepts_parentheses_spaces_brackets(self, name):
        """带括号/空格/方括号的文件名应通过，且返回 stripped 原值。"""
        from mcp_server.tools import _validate_file_name

        assert _validate_file_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "../../../etc/passwd",      # 路径穿越 ../
            "..\\..\\windows\\system32",  # 路径穿越 ..\
            "a/b",                      # 正斜杠
            "a\\b",                     # 反斜杠
        ],
    )
    def test_rejects_path_traversal(self, name):
        """路径穿越 / 斜杠字符仍拒绝 → McpToolError(-32602)。"""
        import pytest as _pytest
        from mcp_server.tools import McpToolError, _validate_file_name

        with _pytest.raises(McpToolError) as exc_info:
            _validate_file_name(name)
        assert exc_info.value.code == -32602
        assert "非法" in exc_info.value.message


# ============================================================================
# TestCallGatewayExt — 能力信封 request_id 透出
# ============================================================================


def _mock_gateway_client(response_body: dict):
    """构造一个返回固定 JSON 的 httpx 客户端替身。"""
    client = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = response_body
    client.post = AsyncMock(return_value=resp)
    return client


class TestCallGatewayExt:
    """``call_gateway_ext`` 返回 data + 信封 request_id；``call_gateway`` 只返回 data。"""

    @pytest.mark.asyncio
    async def test_ext_returns_data_and_request_id(self):
        from mcp_server.tools import call_gateway_ext

        client = _mock_gateway_client(
            {"code": 0, "message": "success", "request_id": "rid-1", "data": {"answer": "a"}}
        )
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            data, request_id = await call_gateway_ext("search_mix", "tenant_t", "key-1", {"query": "q"})

        assert data == {"answer": "a"}
        assert request_id == "rid-1"

    @pytest.mark.asyncio
    async def test_ext_missing_request_id_returns_none(self):
        from mcp_server.tools import call_gateway_ext

        client = _mock_gateway_client({"code": 0, "message": "success", "data": {"answer": "a"}})
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            data, request_id = await call_gateway_ext("search_mix", "tenant_t", "key-1")

        assert data == {"answer": "a"}
        assert request_id is None

    @pytest.mark.asyncio
    async def test_call_gateway_still_returns_data_only(self):
        from mcp_server.tools import call_gateway

        client = _mock_gateway_client(
            {"code": 0, "message": "success", "request_id": "rid-1", "data": {"items": []}}
        )
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            data = await call_gateway("list_services", "tenant_t", "key-1")

        assert data == {"items": []}


# ============================================================================
# TestSearchFeedbackAnchor — 检索响应带 request_id + 固定关闭搜索历史
# ============================================================================


_SEARCH_TOOLS = [
    ("search_ontology", "query_with_ontology"),
    ("search_deep", "deep_query"),
    ("search_llmwiki", "search_llmwiki"),
    ("search_jonex_wiki", "search_jonex_wiki"),
    ("search_mix", "search_mix"),
]


class TestSearchFeedbackAnchor:
    """5 个检索工具：payload 固定 save_history=False，响应顶层带 request_id。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name,action", _SEARCH_TOOLS)
    async def test_payload_disables_history_and_returns_request_id(
        self, mock_service_scope_db, tool_name, action
    ):
        import mcp_server.tools as tools

        handler = getattr(tools, tool_name)
        mock_auth = MagicMock(
            tenant_id="tenant_t",
            key_id="key-1",
            allowed_kb_ids=[],
            service_permissions={"svc-1": "call"},
        )
        mock_call = AsyncMock(return_value=({"answer": "ok"}, "req-1"))

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                result = await handler("svc-1", "test query")

        assert mock_call.call_args[0][0] == action
        assert mock_call.call_args[0][3]["save_history"] is False
        assert result["request_id"] == "req-1"
        assert result["answer"] == "ok"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["search_llmwiki", "search_jonex_wiki"])
    async def test_fallback_uses_final_request_id(self, mock_service_scope_db, tool_name):
        """回退到 search_mix 时，request_id 取第二次（最终成功）调用的值。"""
        import mcp_server.tools as tools

        handler = getattr(tools, tool_name)
        mock_auth = MagicMock(
            tenant_id="tenant_t",
            key_id="key-1",
            allowed_kb_ids=[],
            service_permissions={"svc-1": "call"},
        )
        mock_call = AsyncMock()
        mock_call.side_effect = [
            tools.McpToolError(code=-32602, message="管线不匹配"),
            ({"answer": "fallback", "source": "mixed"}, "req-second"),
        ]

        with patch("mcp_server.tools.call_gateway_ext", mock_call):
            with patch("mcp_server.tools.require_mcp_auth", return_value=mock_auth):
                result = await handler("svc-1", "test query")

        assert result["request_id"] == "req-second"
        assert result["answer"] == "fallback"
        assert mock_call.call_count == 2
        first_payload = mock_call.call_args_list[0][0][3]
        second_payload = mock_call.call_args_list[1][0][3]
        assert first_payload["save_history"] is False
        assert second_payload["save_history"] is False
        assert mock_call.call_args_list[1][0][0] == "search_mix"


# ============================================================================
# TestSubmitSearchFeedback — MCP 检索反馈入口（request_id 锚点）
# ============================================================================


def _feedback_auth(**overrides):
    """构造 MCP 反馈所需的鉴权上下文（默认覆盖 svc-1）。"""
    from mcp_server.auth import McpAuthContext

    params = {
        "key_id": "key-1",
        "tenant_id": "tenant_t",
        "allowed_kb_ids": [],
        "service_permissions": {"svc-1": "call"},
        "write_grants": [],
    }
    params.update(overrides)
    return McpAuthContext(**params)


class TestSubmitSearchFeedback:
    """submit_search_feedback：鉴权 / 服务范围 / payload 透传 / 幂等键缺省生成。"""

    @pytest.mark.asyncio
    async def test_missing_auth_raises(self):
        """未认证 → 结构化 -32002（原始 401 在 data.status_code），不调 Gateway。"""
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import submit_search_feedback

        mock_call = AsyncMock()
        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "未提供认证信息"),
        ):
            with patch("mcp_server.tools.call_gateway", mock_call):
                exc_info = _tool_error(
                    await submit_search_feedback("req-1", "svc-1", "like")
                )

        assert exc_info.value.code == -32002
        assert exc_info.value.status_code == 401
        mock_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_service_not_in_scope_raises(self):
        """service 不在 Key 的授权映射里 → 结构化 -32002，不调 Gateway。

        这条走真实 ``_check_service_scope``（非构造异常对象），守住
        「服务不在授权范围」也带码值的契约。
        """
        from mcp_server.tools import submit_search_feedback

        mock_call = AsyncMock()
        with patch("mcp_server.tools.require_mcp_auth", return_value=_feedback_auth()):
            with patch("mcp_server.tools.call_gateway", mock_call):
                exc_info = _tool_error(
                    await submit_search_feedback("req-1", "svc-other", "like")
                )

        assert exc_info.value.code == -32002
        assert exc_info.value.status_code == 401
        assert "不在授权范围" in exc_info.value.message
        mock_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_submit_passes_action_identity_and_payload(self):
        """合法提交 → action=submit_mcp_feedback，租户/Key 取鉴权上下文。"""
        from mcp_server.tools import submit_search_feedback

        expected = {"feedback": {"id": "fb-1"}, "superseded": False, "idempotent": False}
        mock_call = AsyncMock(return_value=expected)
        with patch("mcp_server.tools.require_mcp_auth", return_value=_feedback_auth()):
            with patch("mcp_server.tools.call_gateway", mock_call):
                result = await submit_search_feedback(
                    "req-1",
                    "svc-1",
                    "dislike",
                    feedback_reason="inaccurate",
                    feedback_comment="命中段落与问题无关",
                    operation_id="op-fixed",
                    version=2,
                )

        assert result == expected
        args = mock_call.call_args[0]
        assert args[0] == "submit_mcp_feedback"
        assert args[1] == "tenant_t"
        assert args[2] == "key-1"
        assert args[3] == {
            "request_id": "req-1",
            "service_id": "svc-1",
            "feedback_type": "dislike",
            "operation_id": "op-fixed",
            "version": 2,
            "feedback_reason": "inaccurate",
            "feedback_comment": "命中段落与问题无关",
        }

    @pytest.mark.asyncio
    async def test_operation_id_generated_when_omitted(self):
        """缺省 operation_id → 自动生成 uuid4 hex；like 不携带原因/评论。"""
        from mcp_server.tools import submit_search_feedback

        mock_call = AsyncMock(return_value={})
        with patch("mcp_server.tools.require_mcp_auth", return_value=_feedback_auth()):
            with patch("mcp_server.tools.call_gateway", mock_call):
                await submit_search_feedback("req-1", "svc-1", "like")

        payload = mock_call.call_args[0][3]
        generated = payload["operation_id"]
        assert isinstance(generated, str)
        assert len(generated) == 32
        int(generated, 16)  # 必须是十六进制
        assert payload["version"] == 1
        assert "feedback_reason" not in payload
        assert "feedback_comment" not in payload

    @pytest.mark.asyncio
    async def test_blank_required_params_rejected(self):
        """request_id / service_id / feedback_type 为空 → McpToolError(-32602)。"""
        from mcp_server.tools import McpToolError, submit_search_feedback

        for kwargs in (
            {"request_id": "  ", "service_id": "svc-1", "feedback_type": "like"},
            {"request_id": "req-1", "service_id": "", "feedback_type": "like"},
            {"request_id": "req-1", "service_id": "svc-1", "feedback_type": " "},
        ):
            exc_info = _tool_error(await submit_search_feedback(**kwargs))
            assert exc_info.value.code == -32602


# ============================================================================
# TestMcpToolNameReport — 工具名请求级上报
# ============================================================================

# app.py 中 15 个 @mcp.tool() 的公开名，必须与 tools.py 注入的 __mcp_tool_name__ 一致。
_MCP_PUBLIC_TOOLS = [
    "list_domain_services",
    "list_documents",
    "list_ontology_entity_types",
    "list_ontology_instances",
    "expand_ontology_neighbors",
    "search_ontology",
    "search_deep",
    "search_llmwiki",
    "search_jonex_wiki",
    "search_mix",
    "read_source",
    "confirm_upload",
    "get_upload_status",
    "generate_upload_url",
    "submit_search_feedback",
]


def _tool_http_client(*response_datas):
    """构造按顺序返回成功信封的 httpx 客户端替身。

    每个 response_data 对应一次 ``/internal/kb/invoke`` 的 ``data``。
    """
    client = MagicMock()
    responses = []
    for data in response_datas:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"code": 0, "request_id": "rid-test", "data": data}
        responses.append(resp)
    client.post = AsyncMock(side_effect=responses)
    return client


def _tool_request_bodies(client):
    """取出 mock 客户端收到的全部请求体。"""
    return [call.kwargs["json"] for call in client.post.call_args_list]


# (tool_name, 调用参数, 各次请求的 data, 鉴权类型)
_MCP_TOOL_CALL_CASES = [
    ("list_domain_services", (), [{"items": [], "total": 0}], "view"),
    ("list_documents", ("svc-1",), [{"kb_ids": ["kb1"]}, {"items": []}], "view"),
    (
        "list_ontology_entity_types",
        ("svc-1", "kb1"),
        [{"kb_ids": ["kb1"]}, {"items": [], "total": 0}],
        "view",
    ),
    (
        "list_ontology_instances",
        ("svc-1", "kb1"),
        [
            {"kb_ids": ["kb1"]},
            {"items": [], "total": 0, "page": 1, "page_size": 20},
        ],
        "view",
    ),
    (
        "expand_ontology_neighbors",
        ("svc-1", "kb1", "设备", "1号变压器"),
        [{"kb_ids": ["kb1"]}, {"nodes": [], "edges": [], "degraded": False}],
        "view",
    ),
    ("search_ontology", ("svc-1", "q"), [{}], "view"),
    ("search_deep", ("svc-1", "q"), [{}], "view"),
    ("search_llmwiki", ("svc-1", "q"), [{}], "view"),
    ("search_jonex_wiki", ("svc-1", "q"), [{}], "view"),
    ("search_mix", ("svc-1", "q"), [{}], "view"),
    ("read_source", ("svc-1", "doc-1"), [{"kb_ids": ["kb1"]}, {"url": "u"}], "view"),
    ("confirm_upload", ("svc-1", "f.pdf", "kb1", "sk"), [{}], "write"),
    ("get_upload_status", ("svc-1", "doc-1"), [{}], "write"),
    ("generate_upload_url", ("svc-1", "kb1", "f.pdf"), [{}], "write"),
    ("submit_search_feedback", ("req-1", "svc-1", "like"), [{}], "view"),
]


class TestMcpToolNameReport:
    """工具名由请求级 ContextVar 注入统一网络出口，且不跨调用泄漏。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name,args,response_datas,auth_kind", _MCP_TOOL_CALL_CASES
    )
    async def test_all_public_tools_send_their_name(
        self, tool_name, args, response_datas, auth_kind
    ):
        """15 个公开工具经装饰后，真实业务请求都带本工具名。

        工具内部的前置校验 ``get_service`` 不是公开 MCP 工具，必须不带工具名：
        否则一次工具调用会在 ``monitor.call_log`` 落两条同名记录。
        """
        import mcp_server.tools as tools

        auth = _feedback_auth(
            write_grants=[{"kb": "kb1", "mode": "all"}] if auth_kind == "write" else []
        )
        client = _tool_http_client(*response_datas)

        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            with patch("mcp_server.tools.require_mcp_auth", return_value=auth):
                with patch("mcp_server.tools.require_write_scope"):
                    with patch("mcp_server.tools.touch_service_call_async"):
                        await getattr(tools, tool_name)(*args)

        bodies = _tool_request_bodies(client)
        assert bodies, f"{tool_name} 未发出 Gateway 请求"
        for body in bodies:
            if body["action"] == "get_service":
                assert "mcp_tool" not in body, (
                    f"{tool_name} 的前置校验 get_service 不应上报 mcp_tool"
                )
                continue
            assert body.get("mcp_tool") == tool_name, (
                f"{tool_name} 的请求体 mcp_tool={body.get('mcp_tool')!r}"
            )
        # 成功返回后上下文必须复位，不能泄漏给下一次调用
        assert tools._current_mcp_tool.get() is None

    @pytest.mark.asyncio
    async def test_report_tool_false_suppresses_tool_name(self):
        """工具栈内的前置校验调用可显式不上报工具名。"""
        import mcp_server.tools as tools

        client = _tool_http_client({})
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            token = tools._set_current_mcp_tool("list_ontology_entity_types")
            try:
                await tools.call_gateway_ext(
                    "get_service",
                    "tenant_t",
                    "key-1",
                    {"service_id": "svc-1"},
                    report_tool=False,
                )
            finally:
                tools._reset_current_mcp_tool(token)

        body = _tool_request_bodies(client)[0]
        assert body["action"] == "get_service"
        assert "mcp_tool" not in body

    @pytest.mark.asyncio
    async def test_direct_call_gateway_ext_omits_mcp_tool(self):
        """不在工具调用栈内时，请求体完全省略 mcp_tool（不发 null）。"""
        import mcp_server.tools as tools

        client = _tool_http_client({})
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            await tools.call_gateway_ext("search_mix", "tenant_t", "key-1", {"query": "q"})

        body = _tool_request_bodies(client)[0]
        assert "mcp_tool" not in body
        assert body["action"] == "search_mix"

    @pytest.mark.asyncio
    async def test_context_resets_after_exception(self):
        """工具抛异常（翻译成 isError 结果）时 finally 同样复位上下文。"""
        import mcp_server.tools as tools
        from mcp_server.auth import McpAuthError

        with patch(
            "mcp_server.tools.require_mcp_auth",
            side_effect=McpAuthError(401, "未认证"),
        ):
            result = await tools.search_ontology("svc-1", "q")

        assert result.isError is True
        assert result.structuredContent["code"] == -32002
        assert tools._current_mcp_tool.get() is None

    @pytest.mark.asyncio
    async def test_no_leak_to_following_direct_gateway_call(self):
        """工具调用结束后紧接着的直连网关请求不得残留工具名。"""
        import mcp_server.tools as tools

        auth = _feedback_auth()
        client = _tool_http_client({"items": [], "total": 0}, {})
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            with patch("mcp_server.tools.require_mcp_auth", return_value=auth):
                with patch("mcp_server.tools.touch_service_call_async"):
                    await tools.list_domain_services()
                    await tools.call_gateway_ext("list_services", "tenant_t", "key-1")

        first, second = _tool_request_bodies(client)
        assert first["mcp_tool"] == "list_domain_services"
        assert "mcp_tool" not in second

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name,first_action",
        [("search_llmwiki", "search_llmwiki"), ("search_jonex_wiki", "search_jonex_wiki")],
    )
    async def test_wiki_fallback_keeps_original_tool_name(
        self, tool_name, first_action
    ):
        """wiki 工具 fallback 到 search_mix action 时仍上报原工具名。"""
        import mcp_server.tools as tools

        # 第一次返回业务错误码 1xxx → McpToolError(-32602) → 触发 fallback
        client = MagicMock()
        failed = MagicMock()
        failed.raise_for_status = MagicMock()
        failed.json.return_value = {"code": 1001, "message": "管线不匹配"}
        succeeded = MagicMock()
        succeeded.raise_for_status = MagicMock()
        succeeded.json.return_value = {"code": 0, "request_id": "rid-2", "data": {}}
        client.post = AsyncMock(side_effect=[failed, succeeded])

        auth = _feedback_auth()
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            with patch("mcp_server.tools.require_mcp_auth", return_value=auth):
                with patch("mcp_server.tools.touch_service_call_async"):
                    await getattr(tools, tool_name)("svc-1", "q")

        bodies = _tool_request_bodies(client)
        assert [b["action"] for b in bodies] == [first_action, "search_mix"]
        assert [b["mcp_tool"] for b in bodies] == [tool_name, tool_name]

    @pytest.mark.asyncio
    async def test_list_documents_multi_kb_reports_name_each_time(self):
        """list_documents 真实查询每次带工具名，前置 get_service 不带。"""
        import mcp_server.tools as tools

        client = _tool_http_client(
            {"kb_ids": ["kb1", "kb2"]},
            {"items": [{"id": "d1"}]},
            {"items": [{"id": "d2"}]},
        )
        auth = _feedback_auth()
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            with patch("mcp_server.tools.require_mcp_auth", return_value=auth):
                with patch("mcp_server.tools.touch_service_call_async"):
                    result = await tools.list_documents("svc-1")

        assert result["total"] == 2
        bodies = _tool_request_bodies(client)
        assert [b["action"] for b in bodies] == ["get_service", "list_documents", "list_documents"]
        assert "mcp_tool" not in bodies[0], "前置 get_service 不能上报 mcp_tool"
        assert all(b["mcp_tool"] == "list_documents" for b in bodies[1:])

    @pytest.mark.asyncio
    async def test_read_source_multi_kb_reports_name_each_time(self):
        """read_source 真实查询每次带工具名，前置 get_service 不带。"""
        import mcp_server.tools as tools

        # kb1 返回业务错误 → 继续尝试 kb2；kb2 成功
        client = MagicMock()
        responses = []
        for payload in (
            {"code": 0, "data": {"kb_ids": ["kb1", "kb2"]}},
            {"code": 1001, "message": "not found"},
            {"code": 0, "request_id": "rid-3", "data": {"url": "u"}},
        ):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = payload
            responses.append(resp)
        client.post = AsyncMock(side_effect=responses)

        auth = _feedback_auth()
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            with patch("mcp_server.tools.require_mcp_auth", return_value=auth):
                with patch("mcp_server.tools.touch_service_call_async"):
                    result = await tools.read_source("svc-1", "doc-1")

        assert result["url"] == "u"
        bodies = _tool_request_bodies(client)
        assert [b["action"] for b in bodies] == ["get_service", "get_raw_url", "get_raw_url"]
        assert "mcp_tool" not in bodies[0], "前置 get_service 不能上报 mcp_tool"
        assert all(b["mcp_tool"] == "read_source" for b in bodies[1:])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name,args,target_action,response_data",
        [
            (
                "list_ontology_entity_types",
                ("svc-1", "kb1"),
                "list_ontology_entity_types",
                {"items": [], "total": 0},
            ),
            (
                "list_ontology_instances",
                ("svc-1", "kb1"),
                "list_ontology_instances",
                {"items": [], "total": 0, "page": 1, "page_size": 20},
            ),
            (
                "expand_ontology_neighbors",
                ("svc-1", "kb1", "设备", "1号变压器"),
                "expand_ontology_neighbors",
                {"nodes": [], "edges": [], "degraded": False},
            ),
        ],
    )
    async def test_ontology_preflight_get_service_omits_tool_name(
        self, tool_name, args, target_action, response_data
    ):
        """本体浏览的 KB 归属校验不落工具名，真实查询仍落。

        回归「一次工具调用在 monitor.call_log 落两条同名记录」。
        """
        import mcp_server.tools as tools

        client = _tool_http_client({"kb_ids": ["kb1"]}, response_data)
        auth = _feedback_auth()
        with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
            with patch("mcp_server.tools.require_mcp_auth", return_value=auth):
                with patch("mcp_server.tools.touch_service_call_async"):
                    await getattr(tools, tool_name)(*args)

        bodies = _tool_request_bodies(client)
        assert [b["action"] for b in bodies] == ["get_service", target_action]
        assert "mcp_tool" not in bodies[0], "前置 get_service 不能上报 mcp_tool"
        assert bodies[1]["mcp_tool"] == tool_name


class TestMcpToolStaticCoverage:
    """静态覆盖：app.py 注册的每个工具所委托的 tools 函数都必须带注入标记。"""

    @staticmethod
    def _app_tool_references():
        """解析 app.py 的 @mcp.tool() 函数体，返回 {公开名: {tools.<attr>...}}。"""
        import ast
        from pathlib import Path

        app_path = Path(__file__).resolve().parents[1] / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))

        def _is_mcp_tool(decorator) -> bool:
            if isinstance(decorator, ast.Call):
                decorator = decorator.func
            return (
                isinstance(decorator, ast.Attribute)
                and decorator.attr == "tool"
                and isinstance(decorator.value, ast.Name)
                and decorator.value.id == "mcp"
            )

        refs: dict[str, set[str]] = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_mcp_tool(d) for d in node.decorator_list):
                continue
            attrs = {
                sub.attr
                for sub in ast.walk(node)
                if isinstance(sub, ast.Attribute)
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "tools"
            }
            refs[node.name] = attrs
        return refs

    def test_every_registered_tool_handler_is_marked(self):
        import mcp_server.tools as tools

        refs = self._app_tool_references()
        assert set(refs) == set(_MCP_PUBLIC_TOOLS), (
            f"app.py 注册的工具与预期不一致: {sorted(refs)}"
        )
        for public_name, attrs in refs.items():
            assert attrs, f"@{public_name} 未委托任何 tools 函数"
            for attr in attrs:
                handler = getattr(tools, attr)
                assert getattr(handler, "__mcp_tool_name__", None) == public_name, (
                    f"tools.{attr} 未注入工具名标记（期望 {public_name}）"
                )


# ============================================================================
# TestTopKUpperBound：top_k 上限与后端 DTO（le=50）对齐
# ============================================================================


class TestTopKUpperBound:
    """MCP 侧 top_k 上限必须与后端 DTO 的 ``le=50`` 一致。

    51-100 此前先过 MCP 校验、再被后端 400 拒绝并误映射成「参数有误」，
    调用方无法从错误里看出真正的边界。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name",
        [
            "search_ontology",
            "search_deep",
            "search_llmwiki",
            "search_jonex_wiki",
            "search_mix",
        ],
    )
    async def test_top_k_50_is_accepted(self, tool_name):
        """边界值 50 应通过校验并原样透传给上游。"""
        import mcp_server.tools as tools

        auth = _feedback_auth()
        mock_call = AsyncMock(return_value=({"answer": "ok"}, "req-1"))
        with patch("mcp_server.tools.require_mcp_auth", return_value=auth):
            with patch.object(tools, "SEARCH_LLMWIKI_ENABLED", True):
                with patch.object(tools, "SEARCH_JONEX_WIKI_ENABLED", True):
                    with patch("mcp_server.tools._call_search_gateway", mock_call):
                        await getattr(tools, tool_name)("svc-1", "q", top_k=50)

        assert mock_call.call_args[0][2]["top_k"] == 50

    @pytest.mark.asyncio
    @pytest.mark.parametrize("top_k", [51, 0, -1])
    async def test_out_of_range_rejected_with_backend_range(self, top_k):
        """51 / 0 / -1 → -32602，且消息给出与后端一致的 1-50。"""
        from mcp_server.tools import search_ontology

        exc_info = _tool_error(
            await search_ontology("svc-1", "test query", top_k=top_k)
        )
        assert exc_info.value.code == -32602
        assert "1-50" in exc_info.value.message


# ============================================================================
# TestListDocumentsPartialFailure：逐 KB 吞错改为结构化上报
# ============================================================================


def _list_documents_gateway(kb_ids, outcomes):
    """构造 list_documents 的 call_gateway 替身。

    ``outcomes`` 以 kb_id 为键，值是成功响应 dict 或待抛出的 ``McpToolError``。
    前置 ``get_service`` 返回给定的 ``kb_ids``。
    """
    from mcp_server.tools import McpToolError

    async def _call(action, tenant_id, key_id, data=None, **kwargs):
        if action == "get_service":
            return {"kb_ids": kb_ids}
        kb_id = (data or {}).get("knowledge_base_id")
        outcome = outcomes[kb_id]
        if isinstance(outcome, McpToolError):
            raise outcome
        return outcome

    return AsyncMock(side_effect=_call)


async def _invoke_list_documents(gateway):
    import mcp_server.tools as tools

    auth = _feedback_auth()
    with patch("mcp_server.tools.call_gateway", gateway):
        with patch("mcp_server.tools.require_mcp_auth", return_value=auth):
            with patch("mcp_server.tools.touch_service_call_async"):
                return await tools.list_documents("svc-1")


class TestListDocumentsPartialFailure:
    """单个 KB 失败不得再伪装成「没有文档」。

    - 部分失败：成功 KB 照常返回，响应带 ``errors`` 数组；
    - 全部成功：响应无 ``errors`` 键（向后兼容）；
    - 全部失败：任一 -32002 优先以 -32002 抛出，否则 -32000，绝不抛 -32602。
    """

    @pytest.mark.asyncio
    async def test_partial_failure_returns_items_and_errors(self):
        """一个 KB 成功、一个 -32002 → 返回成功库的 items + errors，不抛异常。"""
        from mcp_server.tools import McpToolError

        gateway = _list_documents_gateway(
            ["kb-ok", "kb-denied"],
            {
                "kb-ok": {"items": [{"id": "d1"}]},
                "kb-denied": McpToolError(code=-32002, message="认证失败或权限不足"),
            },
        )
        result = await _invoke_list_documents(gateway)

        assert result["items"] == [{"id": "d1"}]
        assert result["total"] == 1
        assert result["errors"] == [
            {
                "knowledge_base_id": "kb-denied",
                "code": -32002,
                "message": "该知识库不可访问，已跳过",
            }
        ]

    @pytest.mark.asyncio
    async def test_partial_failure_keeps_original_code_for_diagnostics(self):
        """400/404/422 映射出的 -32602 在 errors 里保留原码，但文案中性。"""
        from mcp_server.tools import McpToolError

        gateway = _list_documents_gateway(
            ["kb-ok", "kb-locked"],
            {
                "kb-ok": {"items": [{"id": "d1"}]},
                "kb-locked": McpToolError(code=-32602, message="知识库服务请求参数有误"),
            },
        )
        result = await _invoke_list_documents(gateway)

        assert result["total"] == 1
        assert result["errors"][0]["code"] == -32602
        assert result["errors"][0]["message"] == "该知识库不可访问，已跳过"
        assert "不存在" not in result["errors"][0]["message"]

    @pytest.mark.asyncio
    async def test_all_succeed_has_no_errors_key(self):
        """全部成功 → 响应不含 errors 键（旧客户端可无感消费）。"""
        gateway = _list_documents_gateway(
            ["kb-1", "kb-2"],
            {
                "kb-1": {"items": [{"id": "d1"}]},
                "kb-2": {"items": [{"id": "d2"}]},
            },
        )
        result = await _invoke_list_documents(gateway)

        assert result["total"] == 2
        assert "errors" not in result

    @pytest.mark.asyncio
    async def test_all_failed_with_auth_error_raises_32002(self):
        """全部失败且含 -32002 → 抛 -32002。"""
        from mcp_server.tools import McpToolError

        gateway = _list_documents_gateway(
            ["kb-1", "kb-2"],
            {
                "kb-1": McpToolError(code=-32000, message="知识库服务暂不可用"),
                "kb-2": McpToolError(code=-32002, message="认证失败或权限不足"),
            },
        )
        exc_info = _tool_error(await _invoke_list_documents(gateway))
        assert exc_info.value.code == -32002

    @pytest.mark.asyncio
    async def test_all_failed_internal_raises_32000(self):
        """全部失败均为 -32000 → 抛 -32000。"""
        from mcp_server.tools import McpToolError

        gateway = _list_documents_gateway(
            ["kb-1", "kb-2"],
            {
                "kb-1": McpToolError(code=-32000, message="知识库服务暂不可用"),
                "kb-2": McpToolError(code=-32000, message="知识库服务暂不可用"),
            },
        )
        exc_info = _tool_error(await _invoke_list_documents(gateway))
        assert exc_info.value.code == -32000

    @pytest.mark.asyncio
    async def test_all_failed_invalid_params_normalized_to_32000(self):
        """全部失败均为 -32602 → 抛 -32000，不把密级问题误导成调用方参数错。"""
        from mcp_server.tools import McpToolError

        gateway = _list_documents_gateway(
            ["kb-1", "kb-2"],
            {
                "kb-1": McpToolError(code=-32602, message="知识库服务请求参数有误"),
                "kb-2": McpToolError(code=-32602, message="知识库服务请求参数有误"),
            },
        )
        exc_info = _tool_error(await _invoke_list_documents(gateway))
        assert exc_info.value.code == -32000
        assert exc_info.value.code != -32602

    @pytest.mark.asyncio
    async def test_empty_kb_ids_still_raises_32000(self):
        """kb_ids 为空 → 维持原逻辑 -32000「服务下无可用知识库」。"""
        gateway = _list_documents_gateway([], {})
        exc_info = _tool_error(await _invoke_list_documents(gateway))
        assert exc_info.value.code == -32000
        assert exc_info.value.message == "服务下无可用知识库"
