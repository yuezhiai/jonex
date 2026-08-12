#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
悦溪平台 - 配置管理模块

支持：
- 多环境配置（dev/test/uat/prod）
- 环境变量覆盖
- 类型安全
- 热重载（可选）
"""

import os
from functools import lru_cache
from typing import Optional
from pathlib import Path

from dotenv import load_dotenv
try:
    # pydantic v2
    from pydantic.v1 import BaseSettings, Field, validator
except ImportError:
    # pydantic v1
    from pydantic import BaseSettings, Field, validator


# ==================== 配置基类 ====================
class DatabaseSettings(BaseSettings):
    """数据库配置"""
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USERNAME: str = "jonex"
    DB_PASSWORD: str = "jonex123"
    DB_NAME: str = "jonex"

    # 连接池配置
    DB_POOL_SIZE: int = 50
    DB_MAX_OVERFLOW: int = 100
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 3600
    DB_ECHO: bool = False

    class Config:
        env_file = ".env"


class RedisSettings(BaseSettings):
    """Redis 配置"""
    REDIS_URL: Optional[str] = None
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None

    # 连接池配置
    REDIS_MAX_CONNECTIONS: int = 50
    REDIS_SOCKET_TIMEOUT: int = 10
    REDIS_CONNECT_TIMEOUT: int = 5
    REDIS_HEALTH_CHECK_INTERVAL: int = 30
    REDIS_DECODE_RESPONSES: bool = True

    @validator("REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD", pre=True)
    def parse_redis_url(cls, v, values, field):
        """从 REDIS_URL 解析连接参数（如果提供）"""
        url = values.get("REDIS_URL")
        if url and url.startswith("redis://"):
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                if parsed.hostname:
                    values["REDIS_HOST"] = parsed.hostname
                if parsed.port:
                    values["REDIS_PORT"] = parsed.port
                if parsed.path and parsed.path != "/":
                    db_str = parsed.path.lstrip("/")
                    if db_str.isdigit():
                        values["REDIS_DB"] = int(db_str)
                if parsed.password:
                    values["REDIS_PASSWORD"] = parsed.password
                return values.get(field.name, v)
            except Exception:
                pass
        return v

    class Config:
        env_file = ".env"


class SecuritySettings(BaseSettings):
    """安全配置"""
    JWT_SECRET: str = "your_jwt_secret_key_here_please_change_in_production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_DAYS: int = 7

    # 用户认证配置
    USER_JWT_EXPIRE_HOURS: int = 24
    USER_JWT_REFRESH_DAYS: int = 7
    BCRYPT_ROUNDS: int = 12

    # API Key 配置
    API_KEY_HEADER: str = "X-API-Key"
    API_KEY_LENGTH: int = 32

    # 登录票据配置
    LOGIN_TICKET_EXPIRE_SECONDS: int = 60
    AUTH_ALLOWED_REDIRECT_URIS: str = ""  # JSON 字符串: {"appId": ["uri1", "uri2"]}

    class Config:
        env_file = ".env"


class LoggingSettings(BaseSettings):
    """日志配置"""
    LOG_LEVEL: str = "INFO"
    LOG_FILE_PATH: str = "./logs/jonex.log"
    LOG_JSON_FORMAT: bool = False

    @validator("LOG_LEVEL")
    def validate_log_level(cls, v):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"LOG_LEVEL 必须是以下之一: {valid_levels}")
        return v.upper()

    class Config:
        env_file = ".env"


class TCADPSettings(BaseSettings):
    """TCADP 平台配置"""
    TCADP_API_URL: str = "https://tcadp.tencent.com/api"
    TCADP_API_KEY: Optional[str] = None
    TCADP_WEBHOOK_SECRET: Optional[str] = None
    TCADP_WEBHOOK_URL: Optional[str] = None
    TCADP_TIMEOUT: int = 30

    class Config:
        env_file = ".env"


class MilvusSettings(BaseSettings):
    """Milvus 向量数据库配置"""
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_USER: Optional[str] = None
    MILVUS_PASSWORD: Optional[str] = None
    MILVUS_HTTP_PORT: int = 9091

    # 连接配置
    MILVUS_CONNECT_TIMEOUT: int = 30
    MILVUS_KEEP_ALIVE: bool = True
    MILVUS_ALIAS: str = "default"

    # 向量配置
    MILVUS_DEFAULT_DIM: int = 1536
    MILVUS_DEFAULT_METRIC: str = "COSINE"
    MILVUS_DEFAULT_INDEX: str = "IVF_FLAT"

    class Config:
        env_file = ".env"


class CorsSettings(BaseSettings):
    """CORS 和 Cookie 配置"""
    AUTH_CORS_ORIGINS: str = ""  # 逗号分隔: "https://a.com,https://b.com"
    AUTH_COOKIE_DOMAIN: str = ""
    AUTH_COOKIE_SECURE: bool = True
    AUTH_COOKIE_SAMESITE: str = "Lax"

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.AUTH_CORS_ORIGINS:
            return []
        return [o.strip() for o in self.AUTH_CORS_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"


class AppSettings(BaseSettings):
    """应用配置"""
    APP_NAME: str = "jonex-platform"
    APP_VERSION: str = "0.1.0"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # 环境标识
    ENV: str = Field("dev", description="运行环境: dev/test/uat/prod")

    # 限流配置
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 100
    RATE_LIMIT_TENANT_PER_MINUTE: int = 300  # 租户级限流阈值（invoke 链路专用）

    # 计量配置
    METERING_ENABLED: bool = True

    # ── 国际化（i18n）──
    # 系统默认语言：任何上层 locale 缺失时的兜底（评审决策：en-US）
    DEFAULT_LOCALE: str = "en-US"
    # 支持的语言列表（逗号分隔）；X-Lang 不在此列表时回落 DEFAULT_LOCALE
    SUPPORTED_LOCALES: str = "zh-CN,en-US"
    # X-Lang header 值最大长度（安全：防止恶意超长输入）
    LOCALE_MAX_LENGTH: int = 32

    @property
    def supported_locales_list(self) -> list[str]:
        return [x.strip() for x in self.SUPPORTED_LOCALES.split(",") if x.strip()]

    # 熔断配置
    CIRCUIT_BREAKER_ENABLED: bool = False
    CIRCUIT_BREAKER_THRESHOLD: int = 5

    # 审计日志配置
    AUDIT_LOG_ENABLED: bool = True
    AUDIT_LOG_ASYNC: bool = True
    AUDIT_QUEUE_MAX_SIZE: int = 10000
    AUDIT_FLUSH_BATCH_SIZE: int = 100
    AUDIT_FLUSH_INTERVAL_MS: int = 200
    AUDIT_HTTP_METHODS: str = "POST,PUT,PATCH,DELETE"
    AUDIT_RETENTION_DAYS: int = 90
    AUDIT_INGEST_URL: str = ""
    # 仅记录关键行为：invoke 能力调用按 payload.action 关键字过滤，
    # 读类动作（list/get/search/query/preview 等）不入库
    AUDIT_KEY_ACTION_KEYWORDS: str = (
        "create,update,delete,remove,save,publish,modify,edit,import,"
        "review,retry,cancel,approve,reject,bind,unbind,enable,disable,reset,upload"
    )

    # Sidecar 服务地址
    SIDECAR_URL: str = "http://localhost:8001"

    # Gateway 到 Sidecar 的内部认证 key
    GATEWAY_API_KEY: str = "jonex_test_gateway"

    # MCP Server 的内部 API Key（用于 /internal/* 端点认证）
    # 默认值为哨兵字符串；生产部署必须用真实随机 key 覆盖。
    # Gateway 启动时会在 on_event("startup") 中校验非默认值，拒绝启动。
    INTERNAL_API_KEY: str = "change-me-in-production"

    # 能力服务地址
    KNOWLEDGE_BASE_URL: str = "http://localhost:8003"
    BUSINESS_DOMAIN_URL: str = "http://localhost:8005"
    ATOMIC_RAG_URL: str = "http://localhost:8004"
    PLATFORM_URL: str = "http://localhost:8006"
    OPENKB_URL: str = "http://localhost:7566"  # [jonex]

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() == "prod"

    @property
    def is_development(self) -> bool:
        return self.ENV.lower() == "dev"

    class Config:
        env_file = ".env"


class LLMGatewaySettings(BaseSettings):
    """LLM 网关配置"""
    # 上游路由（key 必填：写入 deploy/.env 的 LLMGW_UPSTREAM_LLM_API_KEY）
    LLMGW_UPSTREAM_LLM_HOST: str = "https://tokenhub.tencentmaas.com/v1"
    LLMGW_UPSTREAM_LLM_API_KEY: str = ""          # 必填：真实 deepseek key，仅配在网关
    LLMGW_UPSTREAM_EMBED_HOST: str = "http://host.docker.internal:11434/v1"  # 可改线上
    LLMGW_UPSTREAM_EMBED_API_KEY: str = "ollama"  # 当前本地 Ollama；切线上时改真实云端 key
    # ── Rerank 上游（方案 B2：RAG fallback 引用重排）──
    # binding=ollama-generate：ollama 无原生 /rerank，走 /api/generate raw 模板 + yes/no 概率算分
    # binding=cohere：透传上游标准 /rerank（vLLM / TEI / Xinference / llama-server / 云端）
    LLMGW_RERANK_BINDING: str = "ollama-generate"      # ollama-generate | cohere
    LLMGW_RERANK_MODEL: str = "awenleven/Qwen3-Reranker-4B:Q4_K_M"
    LLMGW_UPSTREAM_RERANK_HOST: str = "http://host.docker.internal:11434"  # 基地址，不含 /v1
    LLMGW_UPSTREAM_RERANK_API_KEY: str = "ollama"
    LLMGW_RERANK_PROMPT_PROFILE: str = "qwen3"         # 仅 ollama-generate 用：qwen3 | gemma | plain
    LLMGW_RERANK_MAX_DOCS: int = 12                    # 单次最多重排文档数（控延迟）
    LLMGW_RERANK_CONCURRENCY: int = 4                  # 逐文档并发打分数
    LLMGW_RERANK_TIMEOUT: int = 30                     # rerank 总超时（秒）
    # 网关自身
    LLMGW_PORT: int = 8787
    LLMGW_INTERNAL_TOKENS: str = ""          # 逗号分隔合法内部 token
    LLMGW_REQUEST_TIMEOUT: int = 600
    # ── [jonex] 上游 429 有界重试 + 退避（§12 B 方案：llm-gateway 内部重试吸收上游限流）──
    # 总开关：false 回退旧「透传不重试」行为
    LLMGW_UPSTREAM_RETRY_ENABLED: bool = True
    # 触发重试的上游状态码（逗号分隔，默认仅 429；可加 503）
    LLMGW_UPSTREAM_RETRY_STATUS: str = "429"
    # 最大重试次数（不含首次）；Retry-After≈60 时 1 次重试即可跨过整窗
    LLMGW_UPSTREAM_RETRY_MAX: int = 1
    # 指数退避基数秒（无 Retry-After 时用）
    LLMGW_UPSTREAM_RETRY_BACKOFF_BASE: float = 2
    # 单次退避上限秒（无 Retry-After 时用）
    LLMGW_UPSTREAM_RETRY_BACKOFF_MAX: float = 30
    # 抖动上限秒（0~JITTER 随机叠加，避免多 chunk 同步重试踩点）
    LLMGW_UPSTREAM_RETRY_JITTER: float = 1.0
    # 单请求累计重试等待总预算秒（仅含 sleep，不含请求耗时）；须 < LightRAG LLM 超时
    LLMGW_UPSTREAM_RETRY_TOTAL_BUDGET: float = 75
    # 病态 Retry-After 值的裁剪上限秒（防上游返回 86400 等极端值）
    LLMGW_UPSTREAM_RETRY_AFTER_CAP: float = 120
    # 计量开关
    LLMGW_METERING_ENABLED: bool = True
    LLMGW_QUOTA_ENABLED: bool = False        # 预留，默认关
    # 关思考（thinking.disabled）注入：加速实体/关系抽取等分类任务
    # 仅对命中 MODELS 且 scene 命中 SCENES 的 chat 请求注入 body.thinking={"type":"disabled"}
    # ⚠️ 仅适配腾讯 tokenhub（tencentmaas）：该字段为其专有关思考格式；换其他上游
    #    需改 upstream._maybe_disable_thinking 适配，否则应把 ENABLED 设为 False
    LLMGW_DISABLE_THINKING_ENABLED: bool = True
    LLMGW_DISABLE_THINKING_MODELS: str = "deepseek-v4-flash-202605"             # 逗号分隔；空=不限模型
    LLMGW_DISABLE_THINKING_SCENES: str = "lightrag_extract,ontology_extract"  # 逗号分隔；空=不限场景
    # PG 批量缓冲
    LLMGW_PG_FLUSH_MAX_ROWS: int = 20
    LLMGW_PG_FLUSH_MAX_SECONDS: float = 5.0
    # embedding 聚合写入（降低 metering.llm_usage_log 行量，见 docs/llm-usage-log-optimization-plan.md §4.3）
    # 命中 scene 的 embedding 调用按 (tenant,scene,model,kb,doc,day) 日级 UPSERT 累加，从"每 chunk 一行"降到"每文档每天一行"。
    # 默认关；开启时建议同时上调 LLMGW_PG_FLUSH_MAX_ROWS（如 100）提升单批预聚合效率。
    LLMGW_EMBED_AGGREGATE_ENABLED: bool = False
    LLMGW_EMBED_AGGREGATE_SCENES: str = "lightrag_embed,ontology_embed"  # 逗号分隔；空=不聚合
    # 估算兜底
    LLMGW_EMBED_AVG_CHARS_PER_TOKEN: int = 4

    class Config:
        env_file = ".env"


# ==================== 主配置类 ====================
class Settings(
    DatabaseSettings,
    RedisSettings,
    SecuritySettings,
    LoggingSettings,
    TCADPSettings,
    MilvusSettings,
    CorsSettings,
    AppSettings,
    LLMGatewaySettings,
):
    """全局配置类"""

    class Config:
        env_file = ".env"
        case_sensitive = True


# ==================== 配置加载 ====================
def _load_env_file() -> None:
    """加载环境变量文件"""
    # 按优先级尝试加载：
    # 1. 环境变量指定的配置文件
    # 2. .env.{环境}
    # 3. .env

    env_specific_file = os.getenv("ENV_FILE")
    env_name = os.getenv("ENV", "dev").lower()

    search_paths = [
        Path(".") / f".env.{env_name}",
        Path(".") / ".env",
        Path(".") / "deploy" / ".env",
    ]

    if env_specific_file:
        search_paths.insert(0, Path(env_specific_file))

    for path in search_paths:
        if path.exists():
            load_dotenv(path, override=False)
            break


# 先加载环境文件
_load_env_file()


@lru_cache(maxsize=None)
def get_config() -> Settings:
    """
    获取全局配置实例（单例）

    Returns:
        Settings: 全局配置实例
    """
    return Settings()


def reload_config() -> Settings:
    """
    重新加载配置（热重载）

    注意：已创建的对象不会自动更新，需要手动重新创建
    """
    _load_env_file()
    get_config.cache_clear()
    return get_config()


# ==================== 配置导出 ====================
# 模块级配置快照，运行期动态变更请调用 get_config/reload_config。
config = get_config()

ENV = config.ENV
DEBUG = config.is_development
