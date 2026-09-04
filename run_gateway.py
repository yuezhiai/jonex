#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - API 网关启动脚本

使用方法:
    python run_gateway.py          # 启动网关
    python run_gateway.py --port 8080  # 指定端口
"""

import sys
import argparse

# 添加项目根目录到路径
sys.path.insert(0, '.')

import uvicorn
from jonex_core.common import get_logger, get_config

config = get_config()
logger = get_logger("gateway_launcher")


def main():
    parser = argparse.ArgumentParser(description="Jonex 平台 API 网关")
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="监听地址 (默认: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="监听端口 (默认: 8000)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="工作进程数 (默认: 1)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="开发模式：代码变更自动重载",
    )
    parser.add_argument(
        "--env",
        type=str,
        default=None,
        choices=["dev", "test", "prod"],
        help="运行环境",
    )

    args = parser.parse_args()

    if args.env:
        import os
        os.environ["ENV"] = args.env
        logger.info(f"设置运行环境: {args.env}")

    logger.info("=" * 60)
    logger.info("Jonex 平台 API 网关启动中...")
    logger.info(f"监听地址: http://{args.host}:{args.port}")
    logger.info(f"工作进程数: {args.workers}")
    logger.info(f"开发模式: {'开启' if args.reload else '关闭'}")
    logger.info(f"运行环境: {config.ENV}")
    logger.info("=" * 60)

    uvicorn.run(
        "api_gateway.main:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        reload=args.reload,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("API 网关已停止")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"API 网关启动失败: {e}")
        sys.exit(1)
