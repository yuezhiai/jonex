"""
腾讯 TCADP 平台集成适配器

TCADP API 插件模式：
1. TCADP 通过 OpenAPI YAML 导入插件定义
2. TCADP 直接调用Jonex 平台的业务路由
3. Jonex 平台验证 TCADP 请求签名，处理后返回结果
4. 异步场景：Jonex通过 Webhook 回调通知 TCADP
"""

import httpx
import logging
from typing import Dict, Any, Optional

from jonex_core.common import get_config, get_logger
from jonex_core.integrations.tcadp.auth import get_tcadp_auth

logger = get_logger("tcadp.adapter")


class TCADPAdapter:
    """TCADP 平台适配器

    功能：
    1. 验证 TCADP 平台的请求签名（供中间件使用）
    2. 异步处理完成后向 TCADP 发送 Webhook 回调
    """

    def __init__(self):
        self.config = get_config()
        self.auth = get_tcadp_auth()
        self.tcadp_api_url = self.config.TCADP_API_URL
        self.tcadp_webhook_url = self.config.TCADP_WEBHOOK_URL

    def verify_request_signature(self, method: str, path: str, headers: Dict[str, str], body: str) -> bool:
        """
        验证 TCADP 请求签名

        Args:
            method: HTTP 方法 (GET/POST/PUT/DELETE)
            path: 请求路径（如 /api/v1/knowledge-base/）
            headers: 请求头
            body: 请求体字符串

        Returns:
            bool: 签名是否有效
        """
        if not self.config.TCADP_API_KEY:
            logger.warning("未配置 TCADP_API_KEY，跳过签名验证")
            return True

        return self.auth.verify_webhook_signature(headers, body)

    async def send_webhook(self, event_type: str, payload: Dict[str, Any]) -> bool:
        """
        向 TCADP 平台发送事件回调（异步能力场景）

        Args:
            event_type: 事件类型，如 interview_completed, interview_failed
            payload: 事件数据

        Returns:
            bool: 是否发送成功
        """
        if not self.tcadp_webhook_url:
            logger.warning("未配置 TCADP_WEBHOOK_URL，跳过 Webhook 发送")
            return False

        import time

        body = {
            "event_type": event_type,
            "payload": payload,
            "timestamp": int(time.time()),
        }

        try:
            if self.config.ENV == "dev":
                logger.info(f"[Mock] 开发环境，跳过真实 Webhook 发送: {self.tcadp_webhook_url}")
                logger.info(f"[Mock] event_type: {event_type}, payload: {payload}")
                return True

            async with httpx.AsyncClient(timeout=self.config.TCADP_TIMEOUT) as client:
                path = "/webhook/tcadp/callback"
                headers = self.auth.get_auth_headers("POST", path, body=body)

                response = await client.post(
                    self.tcadp_webhook_url,
                    json=body,
                    headers=headers,
                )
                response.raise_for_status()
                result = response.json()

                if result.get("code") == 0:
                    logger.info(f"Webhook 发送成功: {event_type}")
                    return True
                else:
                    logger.error(f"Webhook 发送失败: {result.get('message')}")
                    return False

        except Exception as e:
            logger.exception(f"Webhook 发送异常: {e}")
            return False

    async def call_tcadp_api(self, path: str, method: str = "POST", body: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        调用 TCADP 平台 API（通用方法）

        Args:
            path: API 路径
            method: HTTP 方法
            body: 请求体

        Returns:
            API 响应
        """
        if not self.tcadp_api_url:
            raise ValueError("未配置 TCADP_API_URL")

        api_url = f"{self.tcadp_api_url.rstrip('/')}{path}"
        body = body or {}

        try:
            async with httpx.AsyncClient(timeout=self.config.TCADP_TIMEOUT) as client:
                headers = self.auth.get_auth_headers(method, path, body=body)

                if method.upper() == "GET":
                    response = await client.get(api_url, headers=headers)
                else:
                    response = await client.post(api_url, json=body, headers=headers)

                response.raise_for_status()
                return response.json()

        except Exception as e:
            logger.exception(f"调用 TCADP API 失败: {method} {api_url}")
            raise


# 全局单例
_adapter_instance: Optional[TCADPAdapter] = None


def get_tcadp_adapter() -> TCADPAdapter:
    """获取 TCADP 适配器实例"""
    global _adapter_instance
    if _adapter_instance is None:
        _adapter_instance = TCADPAdapter()
    return _adapter_instance
