#!/usr/bin/env python3
"""
能力服务启动脚本

根据环境变量动态启动指定能力服务，提供：
- 服务注册与心跳
- 能力调用端点
- 健康检查
"""

import os
import importlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends

from jonex_core.capability import get_capability_registry
from jonex_core.common.exception_handler import register_exception_handlers
from jonex_core.common.i18n import install_locale_middleware
from jonex_core.discovery import get_service_registry, HeartbeatManager, ServiceInstance
from jonex_core.security import verify_internal_service

# 配置日志
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

# 获取要启动的能力名称
CAPABILITY_NAME = os.getenv("CAPABILITY_NAME", "knowledge_base")
CAPABILITY_KIND = os.getenv("CAPABILITY_KIND", "business")  # business / domain / atomic
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8000"))
SERVICE_HOST = os.getenv("SERVICE_HOST", "0.0.0.0")
# 服务端点（用于服务发现注册）
# Docker 环境下应该使用服务名，本地开发使用 localhost
SERVICE_ENDPOINT = os.getenv("SERVICE_ENDPOINT", f"http://{CAPABILITY_KIND}-{CAPABILITY_NAME.replace('_', '-')}:{SERVICE_PORT}")

# 能力 ID 格式: {kind}.{name}.v1
# 示例: business.knowledge_base.v1 / atomic.rag.lightrag.v1 / domain.rag.text.v1
CAPABILITY_ID = f"{CAPABILITY_KIND}.{CAPABILITY_NAME}.v1"

logger.info(f"启动能力服务: {CAPABILITY_NAME}")
logger.info(f"服务端点: {SERVICE_ENDPOINT}")
logger.info(f"能力 ID: {CAPABILITY_ID}")


# 心跳管理器实例
heartbeat_manager: HeartbeatManager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    服务生命周期管理

    启动时：注册能力到本地注册表 + 注册到服务发现中心
    关闭时：停止心跳，从服务发现中心注销
    """
    global heartbeat_manager

    # 注册能力到本地注册表
    registry = get_capability_registry()
    capability = None
    try:
        # [jonex] v1 退役：唯一 adapter 即 v2，ATOMIC_RAG_VERSION 开关已删除
        module_overrides = {
            ("atomic", "rag.lightrag"): (
                "jonex_core.capability.atomic.rag.lightrag_adapter_v2",
                "LightRAGAdapterV2",
            ),
            ("domain", "rag.text"): (
                "jonex_core.capability.domain.rag_text.rag_text",
                "DomainRAGText",
            ),
        }
        override = module_overrides.get((CAPABILITY_KIND, CAPABILITY_NAME))
        if override:
            module_path, class_name = override
        elif CAPABILITY_KIND == "business":
            module_path = f"capabilities.{CAPABILITY_NAME}"
            class_name = "".join(word.title() for word in CAPABILITY_NAME.split("_")) + "Capability"
        elif CAPABILITY_KIND in {"atomic", "domain"}:
            module_path = f"jonex_core.capability.{CAPABILITY_KIND}.{CAPABILITY_NAME.replace('.', '_')}"
            class_name = "".join(
                word.title() for word in CAPABILITY_NAME.replace(".", "_").split("_")
            ) + "Capability"
        else:
            raise ValueError(f"不支持的能力类型: {CAPABILITY_KIND}")

        # 动态导入能力模块
        module = importlib.import_module(module_path)
        capability_class = getattr(module, class_name)
        capability = capability_class()
        registry.register(capability)
        await capability.initialize()
        # 允许能力注册自定义路由（如流式查询端点）
        if hasattr(capability, "register_routes"):
            try:
                capability.register_routes(app)
            except Exception:
                logger.exception("能力自定义路由注册失败")
        logger.info(f"能力 {CAPABILITY_KIND}.{CAPABILITY_NAME} 本地注册成功")
    except Exception as e:
        logger.exception(f"能力本地注册失败: {e}")
        raise

    # 注册到服务发现中心并启动心跳
    try:
        service_registry = get_service_registry()
        instance = ServiceInstance(
            service_name=CAPABILITY_NAME,
            service_type="capability",
            endpoint=SERVICE_ENDPOINT,
            capability_id=CAPABILITY_ID,
            version="v1",
            metadata={
                "capability_name": capability.get_metadata().capability_name,
                "description": capability.get_metadata().description,
            }
        )
        heartbeat_manager = HeartbeatManager(
            registry=service_registry,
            instance=instance,
            interval=30,  # 30 秒心跳间隔
        )
        await heartbeat_manager.start()
        logger.info(f"能力服务 {CAPABILITY_NAME} 已注册到服务发现中心")
    except Exception as e:
        logger.exception(f"服务注册失败: {e}")
        # 服务发现失败不影响启动，继续运行

    yield  # 服务运行期间

    # 服务关闭时
    if capability:
        try:
            await capability.shutdown()
        except Exception as e:
            logger.exception(f"能力关闭失败: {e}")
    if heartbeat_manager:
        try:
            await heartbeat_manager.stop()
            logger.info(f"能力服务 {CAPABILITY_NAME} 已从服务发现中心注销")
        except Exception as e:
            logger.exception(f"服务注销失败: {e}")


# 创建 FastAPI 应用
app = FastAPI(
    title=f"Jonex Capability: {CAPABILITY_NAME}",
    description=f"Jonex 平台能力服务: {CAPABILITY_NAME}",
    version="1.0.0",
    lifespan=lifespan,
)
register_exception_handlers(app)
install_locale_middleware(app)


# 健康检查端点
@app.get("/health")
async def health_check():
    """健康检查"""
    registry = get_capability_registry()
    return {
        "status": "healthy",
        "capability": CAPABILITY_NAME,
        "capability_id": CAPABILITY_ID,
        "endpoint": SERVICE_ENDPOINT,
        "capabilities": registry.list_capabilities(),
    }


# 能力调用端点
@app.post("/invoke")
async def invoke_capability(
    request: dict,
    service_name: str = Depends(verify_internal_service)
):
    """
    调用能力服务

    Args:
        request: 调用请求，包含：
            - capability_id: 能力 ID
            - payload: 调用参数
            - tenant_id: 租户 ID
            - user_id: 用户 ID（可选）
            - request_id: 请求 ID（可选）

    Returns:
        能力执行结果
    """
    capability_id = request.get("capability_id")
    payload = request.get("payload", {})
    tenant_id = request.get("tenant_id", "default")

    from jonex_core.capability.models import CapabilityRequest
    req = CapabilityRequest(
        tenant_id=tenant_id,
        capability_id=capability_id,
        payload=payload,
        user_id=request.get("user_id"),
        username=request.get("username"),
        ip=request.get("ip"),
    )
    # 透传链路追踪 ID（来自 Sidecar 的 X-Request-ID / body.request_id）；
    # 缺失时保留 CapabilityRequest 默认生成的 uuid。
    incoming_request_id = request.get("request_id")
    if incoming_request_id:
        req.request_id = incoming_request_id

    # 模拟态上下文：Sidecar 透传的 impersonated/perms/original_tenant_id 注入 contextvar，
    # 供 has_permission 兜底短路（模拟态按内嵌 perms 判权，不按原始 user_id 查 DB）。
    from jonex_core.security.permission import reset_impersonation_context, set_impersonation_context

    _imp_token = None
    if request.get("impersonated"):
        _imp_token = set_impersonation_context(
            {
                "impersonated": True,
                "perms": request.get("perms") or [],
                "original_tenant_id": request.get("original_tenant_id"),
            }
        )

    registry = get_capability_registry()
    try:
        result = await registry.invoke(capability_id, req)
    finally:
        if _imp_token is not None:
            reset_impersonation_context(_imp_token)

    return {
        "request_id": result.request_id,
        "success": result.success,
        "code": result.code,
        "message": result.message,
        "data": result.data,
        "details": result.details,
        "params": result.params,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVICE_HOST, port=SERVICE_PORT)
