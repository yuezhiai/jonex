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


def _brief_error(exc: BaseException) -> str:
    """把异常压成一行原因（**不打堆栈**）。

    依赖没起来 / 网络不通是部署常态，不是代码缺陷；打完整 traceback 只会把启动日志
    刷成几十行且淹掉真正的问题。只保留「异常类型 + 消息」这一行。
    """
    message = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"


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
                "OpenKB 健康检查 URL 配置错误（url=%s）：%s —— 禁用 search_llmwiki",
                openkb_health_url, _brief_error(e),
            )
            return False
        except Exception as e:
            if attempt < _OPENKB_HEALTH_RETRIES - 1:
                # 逐次失败只留 debug 一行（启动竞态很常见，不该刷屏）
                logger.debug(
                    "OpenKB 健康检查失败 (attempt %d/%d)，%ds 后重试：%s",
                    attempt + 1, _OPENKB_HEALTH_RETRIES,
                    _OPENKB_HEALTH_RETRY_DELAY, _brief_error(e),
                )
                await asyncio.sleep(_OPENKB_HEALTH_RETRY_DELAY)
            else:
                # 重试耗尽：整段只打这**一条**，不带 traceback
                logger.error(
                    "OpenKB 不可用（已重试 %d 次，url=%s）：%s —— 已禁用 search_llmwiki",
                    _OPENKB_HEALTH_RETRIES, openkb_health_url, _brief_error(e),
                )
    return False


async def check_jonex_wiki_health(
    jonex_wiki_health_url: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """检查 jonex_wiki 适配器健康状态，带 3 次重试 + 2s 退避（对照 check_openkb_health）。"""
    for attempt in range(_OPENKB_HEALTH_RETRIES):
        try:
            if client is not None:
                resp = await client.get(jonex_wiki_health_url, timeout=5)
            else:
                async with httpx.AsyncClient(timeout=5) as health_client:
                    resp = await health_client.get(jonex_wiki_health_url)
            if resp.status_code != 200:
                raise Exception(f"jonex_wiki health check returned {resp.status_code}")
            logger.info(
                "jonex_wiki 健康检查通过 (attempt %d/%d): %s",
                attempt + 1, _OPENKB_HEALTH_RETRIES, jonex_wiki_health_url,
            )
            return True
        except httpx.InvalidURL as e:
            logger.error(
                "jonex_wiki 健康检查 URL 配置错误（url=%s）：%s —— 禁用 search_jonex_wiki",
                jonex_wiki_health_url, _brief_error(e),
            )
            return False
        except Exception as e:
            if attempt < _OPENKB_HEALTH_RETRIES - 1:
                logger.debug(
                    "jonex_wiki 健康检查失败 (attempt %d/%d)，%ds 后重试：%s",
                    attempt + 1, _OPENKB_HEALTH_RETRIES,
                    _OPENKB_HEALTH_RETRY_DELAY, _brief_error(e),
                )
                await asyncio.sleep(_OPENKB_HEALTH_RETRY_DELAY)
            else:
                logger.error(
                    "jonex_wiki 不可用（已重试 %d 次，url=%s）：%s —— 已禁用 search_jonex_wiki",
                    _OPENKB_HEALTH_RETRIES, jonex_wiki_health_url, _brief_error(e),
                )
    return False
