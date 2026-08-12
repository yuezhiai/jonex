#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""OpenKB 健康检查模块——独立于 MCP 框架，可单独测试。

控制 SEARCH_LLMWIKI_ENABLED 运行时开关 (D-08)
"""
import asyncio
import logging

import httpx

logger = logging.getLogger("mcp")

_OPENKB_HEALTH_RETRIES = 3
_OPENKB_HEALTH_RETRY_DELAY = 2  # seconds


async def check_openkb_health(
    openkb_health_url: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """检查 OpenKB 健康状态，带 3 次重试 + 2s 退避。

    参数：
        openkb_health_url  -- OpenKB health 端点 URL
        client             -- 可选的共享 httpx 客户端（复用连接池）。
                              不传时创建独立客户端（向后兼容，适用于独立测试）。

    返回值：
        True  —— 健康检查通过
        False —— 健康检查失败（重试耗尽或配置错误）
    """
    for attempt in range(_OPENKB_HEALTH_RETRIES):
        try:
            if client is not None:
                # 使用共享客户端（复用连接池），per-request timeout 覆盖 client 级默认值
                resp = await client.get(openkb_health_url, timeout=5)
            else:
                async with httpx.AsyncClient(timeout=5) as health_client:
                    resp = await health_client.get(openkb_health_url)
            if resp.status_code != 200:
                raise Exception(f"OpenKB health check returned {resp.status_code}")
            logger.info(
                "OpenKB 健康检查通过 (attempt %d/%d): %s",
                attempt + 1, _OPENKB_HEALTH_RETRIES, openkb_health_url,
            )
            return True
        except httpx.InvalidURL as e:
            # 配置错误（URL 格式不合法）→ 不应重试，直接报告
            logger.error(
                "OpenKB 健康检查 URL 配置错误: %s (url=%s)", e, openkb_health_url,
                exc_info=True,
            )
            return False
        except Exception as e:
            if attempt < _OPENKB_HEALTH_RETRIES - 1:
                logger.warning(
                    "OpenKB 健康检查失败 (attempt %d/%d)，%ds 后重试: %s",
                    attempt + 1, _OPENKB_HEALTH_RETRIES, _OPENKB_HEALTH_RETRY_DELAY, e,
                )
                await asyncio.sleep(_OPENKB_HEALTH_RETRY_DELAY)
            else:
                logger.warning(
                    "OpenKB 不可用（已重试 %d 次），禁用 search_llmwiki: %s",
                    _OPENKB_HEALTH_RETRIES, e,
                    exc_info=True,
                )
    return False
