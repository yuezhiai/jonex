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

    async def invoke(
        self,
        action: str,
        data: dict,
        tenant_id: str,
        *,
        user_id: str | None = None,
        username: str | None = None,
        request_id: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        """调一个 knowledge_base action。

        `user_id` / `username`：代真实用户调用时传 —— knowledge_base 据此做空间 / KB 权限判定；
        不传就是「平台内部调用」（无用户身份，knowledge_base 跳过空间判定、内部 action 才放行）。
        `extra`：并入 Sidecar `/invoke` 报文的其它身份字段（如模拟态 `impersonated` / `perms` /
        `original_tenant_id`）。`request_id` 作 `X-Request-ID` 透传，串起两边的审计与日志。
        """
        body = {
            "capability_id": _CAPABILITY_ID,
            "payload": {"action": action, "data": data},
            "tenant_id": tenant_id,
        }
        if user_id:
            body["user_id"] = str(user_id)
        if username:
            body["username"] = username
        for k, v in (extra or {}).items():
            if v is not None:
                body[k] = v
        headers = {"X-Request-ID": request_id} if request_id else None
        resp = await post_sidecar(self._client, body, tenant_id, headers=headers)
        result = resp.json()
        if not result.get("success"):
            raise CapabilityInvokeError(
                message=f"knowledge_base invoke failed: {result.get('message')}",
                details={
                    "upstream_message": result.get("message"),
                    "upstream_code": result.get("code"),
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
