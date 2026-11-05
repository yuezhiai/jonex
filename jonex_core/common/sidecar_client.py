"""Backpressure for internal Sidecar calls rejected before dispatch.

Only HTTP 429 is retried: transport failures and other HTTP errors can occur
after a write, so blindly replaying those requests is unsafe.
"""
import asyncio
import time

from jonex_core.common.exceptions import RateLimitExceededError


_cooldowns: dict[tuple[int, str], float] = {}


async def post_sidecar(client, body: dict, tenant_id: str, headers: dict | None = None):
    key = (id(asyncio.get_running_loop()), tenant_id)
    send_headers = {**(headers or {}), "X-Tenant-ID": tenant_id}
    for attempt in range(4):
        delay = _cooldowns.get(key, 0) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        response = await client.post("/invoke", json=body, headers=send_headers)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        try:
            delay = min(60.0, max(1.0, float(response.headers.get("Retry-After", "1"))))
        except (ValueError, TypeError):
            delay = 1.0
        if attempt == 3:
            raise RateLimitExceededError(
                message="内部调用仍被限流，请稍后重试",
                details={"retry_after_seconds": delay})
        _cooldowns[key] = max(_cooldowns.get(key, 0), time.monotonic() + delay)
