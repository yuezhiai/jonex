#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - MCP Server 配置管理

从环境变量读取所有配置，不依赖 jonex_core。
使用纯 os.getenv 替代 pydantic Settings（避免 pydantic v1/v2 冲突）。
"""
import logging
import os

logger = logging.getLogger("mcp.config")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取整数型环境变量；缺失、非法或越界时回落默认值并告警。

    MCP 是入口服务，配置错误应降级而不是拒绝启动。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回落默认值 %d", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            "%s=%d 超出范围 %d-%d，回落默认值 %d",
            name, value, minimum, maximum, default,
        )
        return default
    return value


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

    # 本体列式浏览单次跨库聚合上限：默认 20（与 /search/ontology 默认一致）。
    # 非法值（非整数 / 0 / 负数 / 超过 100）回落 20 并告警。
    ONTOLOGY_MAX_FANOUT_KBS: int = _env_int(
        "ONTOLOGY_MAX_FANOUT_KBS", 20, minimum=1, maximum=100
    )

    # 同 KB 多跳（D11）：深度上限与多 KB 整体截止时间。
    # 深度上限与 Gateway / capability 读取同名 env，部署调大需三处同步。
    ONTOLOGY_NEIGHBOR_DEPTH_MAX: int = _env_int(
        "ONTOLOGY_NEIGHBOR_DEPTH_MAX", 3, minimum=1, maximum=20
    )
    ONTOLOGY_MULTIHOP_DEADLINE_SECONDS: int = _env_int(
        "ONTOLOGY_MULTIHOP_DEADLINE_SECONDS", 25, minimum=1, maximum=120
    )

    # 限流配置
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")

    # OpenKB 健康检查（Phase 11）
    OPENKB_HEALTH_URL: str = os.getenv("OPENKB_HEALTH_URL", "http://openkb:7566/health")
    # jonex_wiki 健康检查（经 /health 控制 search_jonex_wiki 开关）
    # ⚠️ 目标是 B1 单容器 `jonex-wiki:8007`（引擎 + 能力同进程，见 deploy/docker-compose.yml）。
    #    旧默认值 `jonex-wiki-adapter:7567` 属已废弃的独立 adapter 架构，Docker DNS 里
    #    没有该别名 —— 用旧值启动必然 3 次重试全失败，`search_jonex_wiki` 被永久禁用。
    JONEX_WIKI_HEALTH_URL: str = os.getenv(
        "JONEX_WIKI_HEALTH_URL", "http://jonex-wiki:8007/health"
    )


settings = Settings()
