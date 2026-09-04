#!/usr/bin/env python3
"""
Jonex 平台 Sidecar 代理入口

Sidecar 作为内部能力调用的统一入口，通过反向代理方式调用能力服务
提供：认证、计量、限流、日志追踪等横切功能
"""

import uvicorn
import logging
import sys

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


def create_app():
    """创建 Sidecar 应用"""
    from jonex_core.sidecar import get_sidecar_app

    # 获取 Sidecar 应用
    sidecar_app = get_sidecar_app()

    logger.info("=" * 60)
    logger.info("Jonex 平台 Sidecar 代理启动成功")
    logger.info("运行模式: 反向代理模式（调用独立能力服务）")
    logger.info(f"监听端口: {sidecar_app.app.state}.{8001}")
    logger.info("API 文档: http://localhost:8001/docs")
    logger.info("=" * 60)

    return sidecar_app.get_app()


# 创建 FastAPI 应用供 uvicorn 启动
app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001,  # Sidecar 使用 8001 端口
        reload=True,
        log_level="info"
    )
