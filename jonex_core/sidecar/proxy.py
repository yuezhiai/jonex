#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
能力服务反向代理

Sidecar 通过 HTTP 代理方式调用远程能力服务，实现与能力实现的解耦
支持从服务发现中心动态获取能力服务端点
"""

import os
import httpx
from typing import AsyncGenerator, Optional, Dict

from jonex_core.common import get_config, get_logger, require_tenant, transmit_locale_header
from jonex_core.common.i18n import LocaleContext, translate
from jonex_core.common.exceptions import (
    CapabilityIdFormatError,
    CapabilityNotFoundError,
    CapabilityTimeoutError,
    UpstreamServiceError,
    ServiceUnavailableError,
)
from jonex_core.discovery import get_service_registry
from jonex_core.security import get_internal_auth

logger = get_logger("sidecar.proxy")


class CapabilityProxy:
    """能力服务反向代理"""

    def __init__(self):
        self.config = get_config()
        self.registry = get_service_registry()
        self.auth = get_internal_auth()
        # 静态配置是当前部署拓扑的权威地址；服务发现只作为未配置时的 fallback。
        self._static_endpoints = {
            "knowledge_base": self.config.KNOWLEDGE_BASE_URL,
            "business_domain": self.config.BUSINESS_DOMAIN_URL,
            "rag.lightrag": self.config.ATOMIC_RAG_URL,
            "platform": self.config.PLATFORM_URL,
            "openkb": self.config.OPENKB_URL,  # [jonex]
        }

    @property
    def capability_endpoints(self) -> Dict[str, str]:
        """获取所有已配置的能力端点（静态配置）"""
        return self._static_endpoints

    async def invoke_capability(
        self,
        capability_id: str,
        payload: Dict,
        tenant_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
        request_id: Optional[str] = None,
    ):
        """
        转发调用到对应的能力服务

        Args:
            capability_id: 完整的能力 ID (如: business.knowledge_base.v1)
            payload: 调用参数
            tenant_id: 租户 ID
            user_id: 用户 ID (可选)
            username: 用户名 (可选)
            ip: 客户端 IP (可选)
            request_id: 请求 ID (可选)

        Returns:
            能力服务返回的结果

        Raises:
            HTTPException: 调用失败时抛出
        """
        tenant_id = require_tenant(tenant_id)
        service_name = self._extract_service_name(capability_id)
        endpoint = await self._get_capability_endpoint(service_name)

        logger.info(
            f"[Proxy] 转发能力调用: {capability_id} -> {endpoint}, "
            f"request_id={request_id}, tenant={tenant_id}"
        )

        timeout = float(os.getenv("SIDECAR_PROXY_TIMEOUT", "120"))
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                # 生成内部服务认证 Token
                internal_token = self.auth.generate_token("sidecar")

                _headers = {
                    "X-Request-ID": request_id or "",
                    "X-Tenant-ID": tenant_id,
                    "Authorization": f"Bearer {internal_token}",
                }
                transmit_locale_header(_headers)
                response = await client.post(
                    f"{endpoint}/invoke",
                    json={
                        "capability_id": capability_id,
                        "payload": payload,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "username": username,
                        "ip": ip,
                        "request_id": request_id,
                    },
                    headers=_headers,
                )

                response.raise_for_status()
                result = response.json()

                logger.info(
                    f"[Proxy] 能力调用完成: {capability_id}, "
                    f"success={result.get('success', True)}, latency={response.elapsed.total_seconds() * 1000:.2f}ms"
                )

                return result

            except httpx.TimeoutException:
                logger.error(f"能力服务调用超时: {capability_id} -> {endpoint}")
                raise CapabilityTimeoutError(
                    message=translate("err.capability.timeout", params={"capability_id": capability_id}, fallback=f"能力服务调用超时: {capability_id}"),
                    details={"endpoint": endpoint},
                )
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"能力服务返回错误: {capability_id}, "
                    f"status={e.response.status_code}, detail={e.response.text[:200]}"
                )
                raise UpstreamServiceError(
                    message=translate("err.capability.upstream_error", params={"status": str(e.response.status_code)}, fallback=f"能力服务返回错误: HTTP {e.response.status_code}"),
                    details={
                        "capability_id": capability_id,
                        "upstream_status": e.response.status_code,
                        "upstream_body": e.response.text[:200],
                    },
                    cause=e,
                )
            except Exception as e:
                msg = str(e)
                if "Name or service not known" in msg:
                    hint = f"容器 '{service_name}' 未启动或无法解析"
                elif "Connection refused" in msg:
                    hint = f"容器 '{service_name}' 已启动但端口未就绪"
                elif "ConnectError" in msg:
                    hint = f"连接 '{service_name}' 失败"
                else:
                    hint = msg
                logger.error(f"能力服务不可用: {capability_id} -> {endpoint}, {hint}")
                raise ServiceUnavailableError(
                    message=translate("err.capability.unavailable", params={"service_name": service_name}, fallback=f"能力服务不可用: {service_name}"),
                    details={"service": service_name, "endpoint": endpoint, "hint": hint},
                    cause=e,
                )

    async def stream_invoke(
        self,
        capability_id: str,
        payload: Dict,
        tenant_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
        request_id: Optional[str] = None,
    ):
        """流式转发能力调用，逐行 yield NDJSON"""
        tenant_id = require_tenant(tenant_id)
        service_name = self._extract_service_name(capability_id)
        endpoint = await self._get_capability_endpoint(service_name)

        logger.info(
            f"[Proxy] 流式转发: {capability_id} -> {endpoint}, "
            f"request_id={request_id}, tenant={tenant_id}"
        )

        timeout = float(os.getenv("SIDECAR_PROXY_TIMEOUT", "120"))
        # 急切捕获 locale：流式生成器在中间件 reset 后迭代，惰性读会拿到 None
        _locale = LocaleContext.get()
        async with httpx.AsyncClient(timeout=timeout) as client:
            internal_token = self.auth.generate_token("sidecar")
            _stream_headers = {
                "X-Request-ID": request_id or "",
                "X-Tenant-ID": tenant_id,
                "Authorization": f"Bearer {internal_token}",
            }
            if _locale:
                _stream_headers["X-Lang"] = _locale
            async with client.stream(
                "POST",
                f"{endpoint}/invoke",
                json={
                    "capability_id": capability_id,
                    "payload": payload,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "username": username,
                    "ip": ip,
                    "request_id": request_id,
                },
                headers=_stream_headers,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.strip():
                        yield line

    async def stream_rag_query(
        self,
        query: str,
        tenant_id: str,
        mode: str = "hybrid",
        top_k: int = 5,
        user_id: Optional[str] = None,
        knowledge_base_id: str = "",
        request_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """流式 RAG 查询，直接调用能力服务的流式查询端点 /query/stream"""
        tenant_id = require_tenant(tenant_id)
        endpoint = await self._get_capability_endpoint("rag.lightrag")

        logger.info(
            f"[Proxy] 流式 RAG 查询: -> {endpoint}/query/stream, "
            f"query={query[:80]}, tenant={tenant_id}, kb={knowledge_base_id}"
        )

        timeout = float(os.getenv("SIDECAR_PROXY_TIMEOUT", "120"))
        params = {"query": query, "mode": mode, "top_k": str(top_k), "tenant_id": tenant_id}
        # 知识库作用域：透传给 atomic /query/stream，由其按 (tenant, kb) 注入
        # LIGHTRAG-WORKSPACE 实现按知识库隔离检索，避免跨库串库。
        if knowledge_base_id:
            params["knowledge_base_id"] = knowledge_base_id
        # [jonex] Gap B: 透传 trace_id + user_id，使流式查询也带计量维度
        if request_id:
            params["trace_id"] = request_id
        if user_id:
            params["user_id"] = user_id

        # 急切捕获 locale：流式生成器在中间件 reset 后迭代，惰性读会拿到 None
        _locale = LocaleContext.get()
        async with httpx.AsyncClient(timeout=timeout) as client:
            internal_token = self.auth.generate_token("sidecar")
            _rag_headers = {
                "X-Request-ID": request_id or "",
                "Authorization": f"Bearer {internal_token}",
                "X-Tenant-ID": tenant_id,
            }
            if _locale:
                _rag_headers["X-Lang"] = _locale
            async with client.stream(
                "GET",
                f"{endpoint}/query/stream",
                params=params,
                headers=_rag_headers,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.strip():
                        yield line

    def _extract_service_name(self, capability_id: str) -> str:
        """
        从能力 ID 中提取服务名

        格式: {type}.{name}.{version}
        business.knowledge_base.v1 → knowledge_base
        atomic.rag.lightrag.v1  → rag.lightrag
        """
        parts = capability_id.split(".")
        if len(parts) >= 3:
            return ".".join(parts[1:-1])
        if len(parts) == 2:
            return parts[1]
        raise CapabilityIdFormatError(
            message=translate("err.capability.invalid_id_format", params={"capability_id": capability_id}, fallback=f"无效的能力ID格式: {capability_id}"),
            details={"capability_id": capability_id},
        )

    async def _get_capability_endpoint(self, service_name: str) -> str:
        """
        获取能力服务端点

        优先使用静态配置端点，未配置时再从服务发现中心获取端点。

        Args:
            service_name: 服务名

        Returns:
            服务端点 URL

        Raises:
            HTTPException: 服务未配置时抛出
        """
        # Docker 与本地调试的网络拓扑不同，静态配置代表当前进程可访问的地址。
        endpoint = self._static_endpoints.get(service_name)
        if endpoint:
            logger.debug(f"使用静态配置端点: {service_name} -> {endpoint}")
            return endpoint

        # 未显式配置时，从服务发现获取
        try:
            endpoint = await self.registry.discover(service_name)
            if endpoint:
                logger.debug(f"从服务发现获取端点: {service_name} -> {endpoint}")
                return endpoint
        except Exception as e:
            logger.warning(f"服务发现失败: {e}")

        raise CapabilityNotFoundError(
            message=translate("err.capability.not_configured", params={"service_name": service_name}, fallback=f"能力服务未配置: {service_name}"),
            details={"service": service_name},
        )


# 全局单例
_proxy_instance: Optional[CapabilityProxy] = None


def get_capability_proxy() -> CapabilityProxy:
    """
    获取能力服务代理实例（单例）

    Returns:
        CapabilityProxy 实例
    """
    global _proxy_instance
    if _proxy_instance is None:
        _proxy_instance = CapabilityProxy()
    return _proxy_instance
