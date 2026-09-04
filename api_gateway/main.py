#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - API 网关

统一入口，负责：
- 请求路由
- 认证鉴权
- 限流熔断
- 日志追踪
- CORS 处理
- 健康检查
"""

import time
import uuid

from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware

from jonex_core.common import (
    get_config,
    get_logger,
    setup_logging,
    set_request_id,
    register_exception_handlers,
    install_locale_middleware,
    MissingApiKeyError,
    InvalidApiKeyError,
    require_tenant,
)

config = get_config()
logger = get_logger("api_gateway")


# ==================== 依赖注入 ====================
async def verify_api_key(request: Request) -> str:
    """
    验证 API Key

    Args:
        request: FastAPI 请求对象

    Returns:
        租户 ID

    Raises:
        MissingApiKeyError: 缺少 API Key
        InvalidApiKeyError: API Key 无效
    """
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise MissingApiKeyError()

    # TODO: 从数据库验证 API Key
    # 临时实现：仅当显式开启 ENABLE_TEST_TOKENS 时接受测试用 API Key
    if config.ENABLE_TEST_TOKENS and api_key.startswith("jonex_test_"):
        return require_tenant(api_key.removeprefix("jonex_test_"))
    else:
        raise InvalidApiKeyError()


# ==================== 创建应用 ====================
def create_app() -> FastAPI:
    """
    创建 FastAPI 应用

    Returns:
        FastAPI 应用实例
    """
    app = FastAPI(
        title="Jonex 平台 API 网关",
        description="Jonex 平台统一 API 入口，提供能力调用、认证鉴权、限流熔断等功能",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS 中间件
    # 生产环境通过 AUTH_CORS_ORIGINS 指定具体域名（支持 credentials）
    # 开发环境未配置时默认 allow_origins=["*"]（不允许 credentials，符合浏览器 CORS 规范）
    origins = config.cors_origins_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins if origins else ["*"],
        allow_credentials=bool(origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 请求 ID 中间件
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """为每个请求生成唯一 ID，用于链路追踪"""
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        set_request_id(request_id)
        # 将请求 ID 注入到请求 state 中
        request.state.request_id = request_id
        start_time = time.perf_counter()

        response = await call_next(request)

        # 计算请求耗时
        process_time = (time.perf_counter() - start_time) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"

        return response

    # 日志中间件
    @app.middleware("http")
    async def logging_middleware(request: Request, call_next):
        """记录请求日志"""
        request_id = getattr(request.state, "request_id", "N/A")
        method = request.method
        url = str(request.url.path)
        client_host = getattr(request.client, "host", "unknown")

        logger.info(
            f"[{request_id}] {method} {url} - 来自 {client_host}"
        )

        start_time = time.perf_counter()
        response = await call_next(request)
        process_time = (time.perf_counter() - start_time) * 1000

        logger.info(
            f"[{request_id}] {method} {url} - "
            f"状态码: {response.status_code}, 耗时: {process_time:.2f}ms"
        )

        return response

    # 全局异常处理（统一异常体系）
    register_exception_handlers(app)

    # 国际化 locale 中间件（X-Lang 解析 + contextvars 上下文）
    install_locale_middleware(app)

    @app.on_event("startup")
    async def configure_local_file_logging():
        # uvicorn 启动时会重设一部分 logging 配置；在 startup 再补一次文件 handler，
        # 确保本地开发可按 request_id 从日志文件定位问题。
        setup_logging(enable_file=True)
        logger.info("API Gateway 本地文件日志已启用")

        # 安全守卫: INTERNAL_API_KEY / GATEWAY_API_KEY 必须已从默认哨兵修改
        _internal_key = config.INTERNAL_API_KEY
        if not _internal_key or _internal_key == "change-me-in-production":
            raise RuntimeError(
                "INTERNAL_API_KEY 未配置或仍为默认哨兵值。"
                "请在 .env 或环境变量中设置 INTERNAL_API_KEY 为真实随机 key。"
                "未配置将导致 /internal/* 端点无认证保护。"
            )
        _gateway_key = config.GATEWAY_API_KEY
        if not _gateway_key or _gateway_key == "change-me-in-production":
            raise RuntimeError(
                "GATEWAY_API_KEY 未配置或仍为默认哨兵值。"
                "请在 .env 或环境变量中设置 GATEWAY_API_KEY 为真实随机 key。"
                "未配置将导致 Gateway 到 Sidecar 的内部调用无认证保护。"
            )

    # ==================== 路由注册 ====================
    register_routes(app)

    return app


def register_routes(app: FastAPI):
    """
    注册所有路由

    Args:
        app: FastAPI 应用实例
    """
    # ==================== 健康检查 ====================
    @app.get("/health", summary="健康检查", tags=["系统"])
    async def health_check():
        """
        服务健康检查端点

        Returns:
            服务状态信息
        """
        # 注意：能力列表现在由 Sidecar 统一管理，
        # API Gateway 不再持有能力实例，仅做业务路由聚合
        return {
            "status": "healthy",
            "service": "jonex-api-gateway",
            "version": "0.1.0",
            "note": "能力列表请通过 Sidecar 端点查询: GET http://sidecar:8001/capabilities",
        }

    @app.get("/ready", summary="就绪检查", tags=["系统"])
    async def ready_check():
        """
        服务就绪检查（用于 K8s readinessProbe）

        Returns:
            就绪状态信息
        """
        return {
            "status": "ready",
            "service": "jonex-api-gateway",
        }

    # ==================== 能力相关接口（已迁移至 Sidecar）====================
    # 说明：能力列表和调用接口已统一由 Sidecar 代理提供
    # Sidecar 负责：认证、计量、限流、能力路由
    # API Gateway 专注于：业务路由聚合、CORS处理、请求追踪

    # ==================== 系统管理接口 ====================
    @app.get("/system/info", summary="获取系统信息", tags=["系统管理"])
    async def get_system_info(
        _: str = Depends(verify_api_key),
    ):
        """
        获取系统信息

        Returns:
            系统信息
        """
        return {
            "code": 0,
            "message": "success",
            "data": {
                "service_name": "jonex-api-gateway",
                "version": "0.1.0",
                "environment": config.ENV,
                "cors_enabled": True,
                "api_key_auth_enabled": True,
                "rate_limit_enabled": False,
            }
        }

    # ==================== 导入路由模块 ====================
    try:
        from api_gateway.routes import knowledge_base_router, tcadp_router, auth_router, platform_router, ecosystem_router
        from api_gateway.routes import knowledge_base_ingest_router, internal_router
        app.include_router(auth_router, prefix="/api/v1/auth", tags=["认证"])
        app.include_router(platform_router, prefix="/api/v1/platform", tags=["平台管理"])
        app.include_router(knowledge_base_router, prefix="/api/v1/knowledge-base", tags=["知识库"])
        app.include_router(knowledge_base_ingest_router, prefix="/api/v1/knowledge-base", tags=["知识库-入站推送"])
        app.include_router(ecosystem_router, tags=["生态管理"])
        app.include_router(internal_router, prefix="/internal", tags=["内部"])
        app.include_router(tcadp_router)
        logger.info("业务路由模块加载成功")
    except ImportError as e:
        logger.warning(f"业务路由模块加载失败（可能尚未实现）: {e}")


# 全局应用实例
app = create_app()


if __name__ == "__main__":
    import uvicorn

    logger.info("启动 API 网关服务...")
    uvicorn.run(
        "api_gateway.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
