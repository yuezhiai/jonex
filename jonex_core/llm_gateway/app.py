# -*- coding:utf-8 -*-
"""
LLM 网关 FastAPI 应用工厂。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jonex_core.llm_gateway.auth import token_swap
from jonex_core.llm_gateway.router import router
from jonex_core.common.config import get_config


def build_app() -> FastAPI:
    """构建 LLM 网关 FastAPI 应用"""
    cfg = get_config()

    app = FastAPI(
        title="Jonex 平台 - LLM 网关",
        description="OpenAI 兼容代理服务，统一 LLM/Embedding 出口计量",
        version=cfg.APP_VERSION,
    )

    # CORS（容器内调用，宽松即可）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Token Swap 中间件（校验内部 token -> 注入上游 key）
    app.middleware("http")(token_swap)

    # 挂载路由
    app.include_router(router)

    # 全局异常处理器
    try:
        from jonex_core.common import register_exception_handlers, install_locale_middleware
        register_exception_handlers(app)
        install_locale_middleware(app)
    except ImportError:
        pass

    return app


app = build_app()