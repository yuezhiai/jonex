#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - MCP Server 配置管理

从环境变量读取所有配置，不依赖 jonex_core。
使用纯 os.getenv 替代 pydantic Settings（避免 pydantic v1/v2 冲突）。
"""
import os


class Settings:
    """MCP Server 环境变量配置"""

    # 数据库连接
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_USERNAME: str = os.getenv("DB_USERNAME", "jonex")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "change-me")
    DB_NAME: str = os.getenv("DB_NAME", "jonex")

    # 服务配置
    MCP_SERVER_PORT: int = int(os.getenv("MCP_SERVER_PORT", "8002"))
    GATEWAY_URL: str = os.getenv("GATEWAY_URL", "http://gateway:8000")
    INTERNAL_API_KEY: str = os.getenv("INTERNAL_API_KEY", "")
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")

    # 限流配置
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")

    # OpenKB 健康检查（Phase 11）
    OPENKB_HEALTH_URL: str = os.getenv("OPENKB_HEALTH_URL", "http://openkb:7566/health")


settings = Settings()
