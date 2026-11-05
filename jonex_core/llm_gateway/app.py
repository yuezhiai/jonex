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

# 关键「恒关思考」场景：这些都是一次性结构化输出调用，思考 token 会先吃光
# max_tokens，导致正文被截断 / 为空（历史故障形态：finish_reason=length）。
# 部署侧 `deploy/.env` 的整行赋值会**覆盖** config.py 默认值（不是追加），
# 漏写一个就会静默丢失该场景的关思考注入 —— 启动时告警，避免靠线上回包才发现。
REQUIRED_THINKING_OFF_SCENES = frozenset(
    {
        "image_query_caption",
        "nl2sql_generate",
        "nl2sql_multiturn",
        "nl2sql_alias_suggest",
        "nl2sql_entry_revise",
        "nl2sql_value_synonym_suggest",
        "concept_relevance_filter",
        "nl2sql_supplement_fuse",
    }
)


def missing_thinking_off_scenes(configured: str) -> set[str]:
    """返回关键关思考场景中**未出现在**配置串里的那些。

    纯函数：逗号分隔、逐项 strip、忽略空项。
    """
    scenes = {s.strip() for s in (configured or "").split(",") if s.strip()}
    return set(REQUIRED_THINKING_OFF_SCENES) - scenes


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
    async def _warn_missing_thinking_off_scenes():
        try:
            missing = missing_thinking_off_scenes(
                getattr(cfg, "LLMGW_DISABLE_THINKING_SCENES", "") or ""
            )
            if missing:
                logger.warning(
                    "LLMGW_DISABLE_THINKING_SCENES 缺少关键场景 %s："
                    "这些场景的思考 token 会吃光输出预算（可能回包截断）",
                    sorted(missing),
                )
        except Exception:
            # 配置自检是旁路，不能拖垮网关启动
            logger.exception("[llm-gateway] 关键关思考场景自检失败")

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