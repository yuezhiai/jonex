# -*- coding:utf-8 -*-
"""
LLM 网关 FastAPI 应用工厂。
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jonex_core.common.monitor_collector import get_monitor_collector
from jonex_core.llm_gateway.auth import token_swap
from jonex_core.llm_gateway.router import router
from jonex_core.common.config import get_config

logger = logging.getLogger("llm_gateway")


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

    # 调用明细采集写口（出站 N1 + 上游重试 N2）。
    # 不启动这两段 ⇒ 整条 llm-gateway 采集链路**静默失效**（put_* 见队列为 None 直接返回）。
    # 采集是旁路，起不来不能拖垮网关 —— 与 Sidecar 同款 try/except 吞异常 + 日志。
    @app.on_event("startup")
    async def _start_monitor_collector():
        try:
            get_monitor_collector().start()
            logger.info("[llm-gateway] MonitorCollector 已启动")
        except Exception:
            logger.exception("[llm-gateway] MonitorCollector 启动失败")

    @app.on_event("shutdown")
    async def _stop_monitor_collector():
        try:
            await get_monitor_collector().stop()
            logger.info("[llm-gateway] MonitorCollector 已停止并 flush")
        except Exception:
            logger.exception("[llm-gateway] MonitorCollector 停止失败")

    return app


app = build_app()