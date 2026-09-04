"""
platform 能力 API 路由
"""
from fastapi import APIRouter


def create_platform_router() -> APIRouter:
    router = APIRouter()

    from capabilities.platform.api.auth import router as auth_router
    router.include_router(auth_router, prefix="/auth", tags=["认证"])

    from capabilities.platform.api.platform_crud import router as platform_router
    router.include_router(platform_router, prefix="/platform", tags=["平台管理"])

    from capabilities.platform.api.audit_logs import router as audit_router
    router.include_router(audit_router, prefix="/platform", tags=["审计日志"])

    from capabilities.platform.api.mcp_key_api import router as mcp_router
    router.include_router(mcp_router, prefix="/platform", tags=["MCP Key 管理"])

    from capabilities.platform.api.mcp_service_api import router as mcp_svc_router
    router.include_router(mcp_svc_router, prefix="/platform", tags=["MCP 服务目录"])

    return router
