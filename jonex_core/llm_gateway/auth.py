# -*- coding:utf-8 -*-
"""
Token Swap 中间件：校验调用方内部 token -> 注入上游真实 API key。
"""

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from jonex_core.common.config import get_config
from jonex_core.common.logger import get_logger

logger = get_logger("llm_gateway.auth")


def _valid_internal_tokens() -> set[str]:
    """解析 LLMGW_INTERNAL_TOKENS 逗号分隔列表为 set"""
    raw = get_config().LLMGW_INTERNAL_TOKENS
    if not raw:
        return set()
    return {t.strip() for t in raw.split(",") if t.strip()}


async def token_swap(request: Request, call_next):
    """Token Swap 中间件

    1. 从 Authorization / X-API-Key 提取内部 token
    2. 校验合法性；非法返回 401
    3. request.state 记录 token_id 供上游路由使用
    """
    # 跳过健康检查
    if request.url.path == "/health":
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    api_key_header = request.headers.get("X-API-Key", "")

    incoming = ""
    if auth_header.startswith("Bearer "):
        incoming = auth_header[len("Bearer "):].strip()
    elif api_key_header:
        incoming = api_key_header.strip()

    valid_tokens = _valid_internal_tokens()
    # 2026-10-14 修复 fail-OPEN：原判据
    #   if not incoming or (valid_tokens and incoming not in valid_tokens)
    # 在 valid_tokens 为空集时第二个条件恒为 False ⇒ **任何非空 token 都放行**，
    # 即「白名单没配」等价于「不需要认证」。任何能到达 8787/8788 的人都能免费用
    # 该部署配置的上游 LLM 额度、并把计量记到别人头上。
    # 现改为 fail-closed：白名单为空即拒绝服务（而不是放行）。
    if not valid_tokens:
        logger.error(
            "LLMGW_INTERNAL_TOKENS 未配置（为空）——LLM 网关对所有请求返回 503（fail-closed）。"
            "请在 .env 中设置逗号分隔的内部令牌白名单，并确保下游各 "
            "*_BINDING_API_KEY 与其逐字一致。"
        )
        return JSONResponse(
            {"error": "service_unavailable", "message": "内部令牌白名单未配置"},
            status_code=503,
        )
    if not incoming or incoming not in valid_tokens:
        return JSONResponse(
            {"error": "unauthorized", "message": "无效的内部 token"},
            status_code=401,
        )

    request.state.token_id = incoming
    return await call_next(request)