"""经 Sidecar 调用 knowledge_base 能力的内部客户端。

business_domain 与 knowledge_base 是**独立容器**：模板发布后的「标记受影响
KB schema 过期 / 查询影响范围」必须经 Sidecar invoke，不能跨容器 import。
内部鉴权用配置里的 `GATEWAY_API_KEY`（Sidecar 精确比对）。
"""
from __future__ import annotations

import httpx

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import CapabilityInvokeError
from jonex_core.common.sidecar_client import post_sidecar

_CAPABILITY_ID = "business.knowledge_base.v1"


class KnowledgeBaseClient:
    def __init__(self, endpoint: str | None = None):
        cfg = get_config()
        self._client = httpx.AsyncClient(
            base_url=(endpoint or cfg.SIDECAR_URL or "http://sidecar:8000").rstrip("/"),
            headers={"X-API-Key": cfg.GATEWAY_API_KEY},
            timeout=300,
        )

    async def invoke(self, action: str, data: dict, tenant_id: str) -> dict:
        body = {
            "capability_id": _CAPABILITY_ID,
            "payload": {"action": action, "data": data},
            "tenant_id": tenant_id,
        }
        resp = await post_sidecar(self._client, body, tenant_id)
        result = resp.json()
        if not result.get("success"):
            raise CapabilityInvokeError(
                message=f"knowledge_base invoke failed: {result.get('message')}",
                details={
                    "upstream_message": result.get("message"),
                    "error_code": (result.get("details") or {}).get("error_code"),
                },
            )
        return result.get("data")

    async def aclose(self) -> None:
        await self._client.aclose()


_client: KnowledgeBaseClient | None = None


def get_knowledge_base_client() -> KnowledgeBaseClient:
    global _client
    if _client is None:
        _client = KnowledgeBaseClient()
    return _client


async def reset_knowledge_base_client() -> None:
    """关闭并置空单例（供服务 lifespan 在重启/测试间清理）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
