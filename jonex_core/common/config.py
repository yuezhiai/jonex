#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 配置管理模块

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

from jonex_core.common.model_config import resolve_optional_llm_model


# ==================== 配置基类 ====================
class DatabaseSettings(BaseSettings):
    """数据库配置"""
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USERNAME: str = "jonex"
    DB_PASSWORD: str = "change-me"
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

    # 租户模拟配置
    IMPERSONATE_EXPIRE_MINUTES: int = 30
    # 到期自动退出的审计宽限（秒）：模拟 token 到期那一刻前端才调
    # `/auth/impersonate/end`，此时 access token 已失效。仅该端点接受
    # 「签名有效 + impersonated=true + exp 已过但不超过该宽限」的 token，
    # 用于写 `impersonate_expired` 审计；不放开任何数据接口。
    IMPERSONATE_END_GRACE_SECONDS: int = 300

    class Config:
        env_file = ".env"


# KeycloakSettings 中「空值回退」白名单：仅这些 **int / bool** 字段在 env 为
# 空串 / 纯空白串时回退到**该字段自己的默认值**。
# ⛔ 不要往里加 str 字段：str 字段空串的既有语义是「原样得空串」（如
# ``KEYCLOAK_IDP_HINT_DEFAULT_ALIAS=`` 得 ``""`` 而非 ``"casdoor"``），加了会改变语义。
_KEYCLOAK_BLANK_FALLBACK_FIELDS = frozenset({
    # 验签参数
    "KEYCLOAK_CLOCK_SKEW_LEEWAY_SECONDS",
    "KEYCLOAK_JWKS_TIMEOUT_SECONDS",
    "KEYCLOAK_JWKS_CACHE_TTL_SECONDS",
    # Admin API
    "KEYCLOAK_ADMIN_TOKEN_TTL_SECONDS",
    "KEYCLOAK_TIMEOUT",
    # OIDC authorize
    "KEYCLOAK_STATE_TTL_SECONDS",
    "KEYCLOAK_IDP_HINT_ENABLED",
    # 登录页 SSO 入口显隐
    "HIDE_SSO_LOGIN",
    # Back-Channel Logout
    "KEYCLOAK_BACKCHANNEL_IAT_MAX_AGE_SECONDS",
    "KEYCLOAK_BACKCHANNEL_JTI_TTL_SECONDS",
})


class KeycloakSettings(BaseSettings):
    """Keycloak OIDC SSO 配置（验签工具只读这些键，键名勿改）"""

    # ── Issuer / JWKS 白名单（防 SSRF：禁止由 token `iss` 动态拼接）──
    # JSON 字符串，映射 issuer URL -> 条目：
    #   {"jwks_uri": "...", "client_id": "...", "audience": "..."}
    # audience 可选，默认取 client_id；空 = 未启用 SSO
    KEYCLOAK_ISSUER_WHITELIST: str = ""
    # JWKS 公钥端点 URL（RS256 验签）；由部署在 issuer 白名单内配置
    KEYCLOAK_JWKS_URL: str = ""

    # ── Client 凭据 ──
    # 平台级单 client 模型（有意设计）：整个平台共用一个 Keycloak client，
    # KEYCLOAK_CLIENT_ID / KEYCLOAK_CLIENT_SECRET / KEYCLOAK_REDIRECT_URI /
    # KEYCLOAK_REALM_ADMIN_* 均为平台级常量，非按租户配置；租户 realm 由
    # tenant_realm_map 映射（jonex-{tenant_id}）。
    # 登录用 public client（SPA + PKCE，无 secret）
    KEYCLOAK_CLIENT_ID: str = ""
    # confidential client secret（服务端调用：Admin API / backchannel）
    # 哨兵默认值；生产须覆盖，禁止明文硬编码进代码
    KEYCLOAK_CLIENT_SECRET: str = "change-me-in-production"

    # ── 验签参数（对齐 internal_auth.CLOCK_SKEW_LEEWAY_SECONDS=120）──
    KEYCLOAK_CLOCK_SKEW_LEEWAY_SECONDS: int = 120
    # JWKS 拉取超时（秒）：拉 JWKS 公钥的网络超时，与下方缓存 TTL 无关
    KEYCLOAK_JWKS_TIMEOUT_SECONDS: int = 5
    # JWKS 本地缓存 TTL（秒），kid 不匹配时刷新
    KEYCLOAK_JWKS_CACHE_TTL_SECONDS: int = 3600

    # ── Admin API client（账号状态双向同步等）──
    # Keycloak 服务基地址（如 http://keycloak:8080）；空 = 未启用 Admin API（fail-closed）
    KEYCLOAK_SERVER_URL: str = ""
    # admin token 内存缓存 TTL（秒）
    KEYCLOAK_ADMIN_TOKEN_TTL_SECONDS: int = 60
    # Admin API httpx 请求超时（秒）
    KEYCLOAK_TIMEOUT: int = 30

    # ── refresh cookie（httpOnly + Secure + SameSite 见 AUTH_COOKIE_*）──
    KEYCLOAK_REFRESH_COOKIE_NAME: str = "jonex_refresh"
    KEYCLOAK_REFRESH_COOKIE_PATH: str = "/"
    # 模拟态标记 cookie（httpOnly）：`/auth/impersonate` 种下、`/auth/impersonate/end` 清除，
    # 供 `/auth/refresh` 的「模拟态禁止续期」纵深防御层判定（防跨租户静默覆盖）。
    # 同一 `Path` / 同域 ⇒ 与 `jonex_refresh` 同行、随请求自动发送；`Max-Age` 取
    # `IMPERSONATE_EXPIRE_MINUTES * 60` ⇒ `/end` 失败时标记自动过期，不会永久锁死续期。
    IMPERSONATION_MARKER_COOKIE_NAME: str = "jonex_impersonating"
    # id_token cookie（httpOnly）：仅作 RP-initiated logout 的 id_token_hint 载体。
    # 不带 hint 时 Keycloak 26 会先弹「Do you want to log out?」确认页，登出不确定
    # （用户关掉页面 = SSO 会话仍存续）；带上则由 Keycloak 直接终结该 sid 的会话。
    # id_token 的 exp 很短，但 OIDC RP-Initiated Logout 要求 OP 接受 exp 已过的 hint
    # （Keycloak 亦然），故 cookie 生命周期对齐 refresh cookie。
    KEYCLOAK_ID_TOKEN_COOKIE_NAME: str = "jonex_id_token"
    # RP-initiated logout 完成后回跳地址（浏览器须可达）。**带上就必须命中 Keycloak
    # client 的 post.logout.redirect.uris 白名单**，否则 Keycloak 报 invalid_redirect_uri
    # 导致 SSO 登出整体失败；空 = 不带该参数（Keycloak 结束后停在自己的已登出页）。
    KEYCLOAK_POST_LOGOUT_REDIRECT_URI: str = ""

    # ── OIDC authorize 发起（授权码登录入口）──
    # OIDC 回调地址：Keycloak 登录成功后跳回后端的完整 URL，须与 Keycloak client 注册的
    # redirect URI 精确一致（含 /api/v1 前缀）。空 = 未启用 SSO 发起（fail-closed）。
    KEYCLOAK_REDIRECT_URI: str = ""
    # 浏览器视角的 Keycloak 基地址（authorize 302 目标，浏览器须可达）。
    # 与 KEYCLOAK_SERVER_URL（后端进程访问 keycloak）解耦：Docker 下后端用
    # host.docker.internal，宿主机浏览器解析不了，authorize 须填浏览器可达地址
    # （如 http://localhost:8080）。空 = 回退 KEYCLOAK_SERVER_URL（向后兼容）。
    KEYCLOAK_AUTHORIZE_BASE_URL: str = ""
    # OIDC state 一次性票据 Redis TTL（秒）。
    KEYCLOAK_STATE_TTL_SECONDS: int = 300
    # 平台级开关：authorize URL 是否注入 `kc_idp_hint`，跳过 Keycloak 自身的
    # 中间登录表单、直接 303 到指定 IdP（本环境为 casdoor）。
    # 默认 False = 关闭（fail-safe 底线）：关闭时不携带任何 IdP 提示参数，授权行为
    # 与改造前逐字一致。这是**唯一的回退开关**——线上出问题关掉即完全回退，
    # 不需改代码、不需动 Keycloak 侧任何配置。
    # 与下方 KEYCLOAK_IDP_HINT_DEFAULT_ALIAS 配对：本开关管「要不要带」，alias 管「带哪个」。
    KEYCLOAK_IDP_HINT_ENABLED: bool = False
    # 平台级默认 IdP alias（**非**租户级配置）：当租户无覆盖值时使用，因此不产生
    # 任何逐租户手工动作。租户级覆盖值来自 DB 列
    # `platform.identity_integration_config.idp_hint_alias`（NULL = 用本默认值）。
    # 取值 = 该租户 realm 内 IdP 的 alias（本环境为 `casdoor`）；配错时 Keycloak
    # 静默回落到自身登录表单（软失败，不报错）。
    KEYCLOAK_IDP_HINT_DEFAULT_ALIAS: str = "casdoor"

    # ── 登录页「企业 SSO」入口显隐 ──
    # 部署级开关：true = 登录页**不渲染**「使用公司SSO登录」按钮及其上方「或」分隔线
    # （两者同进同退，见 frontends/shell/src/pages/Login/index.tsx 的同一条件块）。
    # ⚠️ 只控制**入口显隐**，不关闭 SSO 端点本身（知道 authorize URL 仍可发起授权）。
    # ⛔ **不得**并入下方 `sso_enabled` 的派生式：`sso_enabled` 同时是两处生产启动守卫
    #    （侧车 `jonex_core/sidecar/main.py` 与 `capabilities/platform/capability.py` 的
    #    `_assert_keycloak_client_secret_configured`）的开关 —— 并入会让配了 Keycloak 的
    #    部署被判定为「未启用 SSO」，从而跳过 `KEYCLOAK_CLIENT_SECRET` 哨兵校验（fail-closed 静默失效）。
    # 默认 True = 隐藏（未配置 / `=true` / `=""` 都隐藏登录页 SSO 入口，只有显式 `=false` 才展示）。
    # ⛔ 勿类推同族的 HIDE_TENANT_INPUT：两者反转后方向相反（HIDE_TENANT_INPUT 仍是「默认 false = 不隐藏」），极易被一并「统一」。
    # 本项与 `AppSettings.HIDE_TENANT_INPUT` 同属「登录页开关」家族（env 文件中同段落登记），
    # 但需与 `sso_enabled` 同源计算，故声明在此类。
    HIDE_SSO_LOGIN: bool = True

    # ── Back-Channel Logout（logout_token 校验，T4）──
    # iat 距当前时间超过该值（秒）即拒绝，防旧 logout_token 重放。
    KEYCLOAK_BACKCHANNEL_IAT_MAX_AGE_SECONDS: int = 300
    # logout_token jti 防重放键 TTL（秒），应 ≥ iat 窗口。
    KEYCLOAK_BACKCHANNEL_JTI_TTL_SECONDS: int = 600

    # ── 企业身份集成出站白名单 ──
    # 逗号分隔的 host 列表（不含 scheme 与端口，如 "keycloak.internal,10.20.0.5"）。
    # 命中的出站目标跳过 SSRF 的私网/回环阻断与 https 强制，用于放行私有化部署的
    # 内网 Keycloak（仅 SSO「测试连接」探测 well-known 元数据，不携带凭据）。
    # 公网目标不受影响，仍走全量 SSRF 校验；白名单为空 = 保持默认全阻断。
    IDENTITY_INTEGRATION_ALLOWED_HOSTS: str = ""

    # ── 知识库数据源出站白名单（P2 / RC-C，SSRF 修复）──
    # 逗号分隔的 host 列表（不含 scheme 与端口，如 "minio.internal,10.20.0.5"）。
    # KB 数据源（api / storage）出站默认走**严格档**（``validate_kb_ingestion_url``：
    # 阻断私网 / 回环 / 链路本地 / 云元数据），命中的目标跳过网段检查。
    # 这是**安全默认**下的逃生阀：私有化部署确实要拉内网源时，由部署方逐个显式登记
    # 主机名，而不是把整类私网地址放开。公网目标不受影响；为空 = 保持默认全阻断。
    KB_INGESTION_ALLOWED_HOSTS: str = ""

    @validator("*", pre=True)
    def _blank_env_falls_back_to_default(cls, v, values, field):
        """空串 / 纯空白串 → 回退该字段**自己的默认值**（仅白名单内的 int/bool 字段）。

        为什么需要：键**存在但为空**（``KEYCLOAK_TIMEOUT=``）是 docker-compose 从空
        变量插值出的常见形态，pydantic v1 会拿 ``""`` 去解析 int/bool 并抛
        ``ValidationError``；而本模块在 **import 期**就实例化 ``config = get_config()``
        （见文件末尾），于是任何 import config 的服务整体崩 —— 未填真实 SSO 配置的
        部署根本起不来。三态语义：

        - 键**缺失** → 字段默认值（pre-validator 不触发，pydantic 直接用默认值）；
        - 键**存在但空 / 纯空白** → 回退字段默认值（本 validator）；
        - 键**有合法值** → 用该值（顺带 ``strip()``，见下）。

        带首尾空白的合法值（``KEYCLOAK_IDP_HINT_ENABLED=" true "``）同样生效：pydantic
        v1 的 bool 解析**不**去空白（而 ``int(" 45 ")`` 天然可用），不 strip 就会让一个
        尾随空格把整个服务挡在 import 之外 —— 与本次修复的动机同源。取值的 strip 只在
        白名单字段上做，且这些字段本就不该有语义空白。

        用 ``"*"`` 通配是为了「模块新增 int/bool 键时自动继承该语义」，但**分派必须
        按名字白名单**：``str`` 字段一旦被回退 / strip，``KEYCLOAK_IDP_HINT_DEFAULT_ALIAS=``
        就会从 ``""`` 变成 ``"casdoor"``，改动既有语义。
        """
        if field.name in _KEYCLOAK_BLANK_FALLBACK_FIELDS and isinstance(v, str):
            if not v.strip():
                # 取字段**自己的**默认值（而非在代码里重抄一份字面量），避免两处漂移
                return cls.__fields__[field.name].default
            return v.strip()
        return v

    @property
    def identity_integration_allowed_hosts(self) -> list[str]:
        if not self.IDENTITY_INTEGRATION_ALLOWED_HOSTS:
            return []
        return [
            h.strip().lower()
            for h in self.IDENTITY_INTEGRATION_ALLOWED_HOSTS.split(",")
            if h.strip()
        ]

    @property
    def kb_ingestion_allowed_hosts(self) -> list[str]:
        if not self.KB_INGESTION_ALLOWED_HOSTS:
            return []
        return [
            h.strip().lower()
            for h in self.KB_INGESTION_ALLOWED_HOSTS.split(",")
            if h.strip()
        ]

    @property
    def sso_enabled(self) -> bool:
        """部署级 SSO 启用判据 —— 与 build_authorize_url() 的 fail-closed 判据同源。

        ⛔ 这不是租户级 identity_integration_config.sso_enabled（那个默认 FALSE、
           且 AuthService.oidc_authorize 从不读它）。用错会把可用的 SSO 判成未启用。
        """
        return bool(
            self.KEYCLOAK_REDIRECT_URI
            and self.KEYCLOAK_CLIENT_ID
            and (self.KEYCLOAK_AUTHORIZE_BASE_URL or self.KEYCLOAK_SERVER_URL)
        )

    @property
    def sso_entry_visible(self) -> bool:
        """登录页是否渲染「企业 SSO」入口 = 部署级 SSO 可用 **且** 未被强制隐藏。

        取值来源为 ``sso_enabled`` 叠加一层 ``HIDE_SSO_LOGIN`` 覆盖：
        未配 Keycloak（``sso_enabled`` 为假）时，无论本开关取何值都为假（既有自动隐藏不回归，
        ``and`` 短路 ⇒ 本开关**改不动它**）；
        配了 Keycloak 时由 ``HIDE_SSO_LOGIN`` 决定显隐 —— 真值表（默认值为 ``True``）：
        未配置 / ``=true`` / ``=""``（空串经 ``_KEYCLOAK_BLANK_FALLBACK_FIELDS`` 回落该字段
        默认值）一律**隐藏**，只有显式 ``=false`` 才**展示**。

        这是 ``GET /auth/login-config`` 的 ``enabled`` 取值来源（Shell 登录页据此渲染），
        ⛔ 与本类的 ``sso_enabled`` **分开**：后者语义是「SSO 是否可用」，还被两处生产启动
        守卫消费（见 ``HIDE_SSO_LOGIN`` 字段注释），改动它的取值会连带放宽安全校验。
        """
        return bool(self.sso_enabled) and not bool(self.HIDE_SSO_LOGIN)

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

    # ── 演化台（②b）──
    # 点踩试跑的 router 入口：试跑**必须经 router :8010**（② 主答翻转 / DI 扇出 /
    # 观测写入都在那里拦 `search/mix`），直连 Jonex 会绕过编排、试跑也就落不了观测。
    # 默认指向宿主机（KB 能力跑在容器里）；宿主侧调试可用 env 覆盖。
    EVOLUTION_ROUTER_BASE: str = "http://host.docker.internal:8010"
    # 演化评测上游档位：`ask`（默认）= 进程内 ② 问答，逐字节沿用旧行为；
    # `router` = 经 router 的 data-only 检索路径（W4）。非法值回落 `ask` 并告警，
    # 归一化在 `services/evolution/upstream._upstream_mode()` 里做（不在配置层）。
    EVOLUTION_UPSTREAM: str = "ask"
    # router 档单次问答的超时（秒）。非法值回落 120（归一化在
    # `services/evolution/upstream._router_timeout()`）。
    EVOLUTION_ROUTER_TIMEOUT: int = 120
    # router 档演化 JWT 的 `sub`（需要有目标 KB 空间权限的数值用户；
    # username 仍是 `evolution`，审计/观测仍可辨识合成流量）。
    EVOLUTION_ROUTER_USER_ID: str = "1"
    # 基准臂的网关入口：演化基准从平台网关走完整 invoke 链路（认证 / 租户 /
    # 观测与用户问答同一条），**不直连 router**（那是点踩试跑的入口）。
    # 默认宿主回环；拼 URL 前统一 `rstrip("/")`。容器里部署时按实际网关地址覆盖。
    EVOLUTION_GATEWAY_BASE: str = "http://127.0.0.1:8000"
    # ── 登录页（面向前端，经 GET /api/v1/auth/login-config 下发）──
    # 默认租户 ID：登录页租户输入框的预填值。空 = 不预填，由用户手输。
    DEFAULT_TENANT_ID: str = ""
    # 是否隐藏登录页的租户 ID 输入框。**默认 false**：不配置即不隐藏 ——
    # 多租户部署下必须让用户能自己填租户，隐藏是单租户部署才显式打开的开关。
    #
    # ⚠️ 与 DEFAULT_TENANT_ID 的联动：租户是登录的**必需**参数（platform 侧
    # auth.py 的 login 缺 X-Tenant-ID 直接 400），所以「显式开了隐藏却没有默认
    # 租户」会让登录页直接不可用。因此下发给前端的 hide_tenant_input 是**解析
    # 后**的值 = HIDE_TENANT_INPUT and bool(DEFAULT_TENANT_ID)——只有确实有默认
    # 租户时才隐藏，否则强制显示并记一条 warning（见 `login_page_config_conflict`）。
    HIDE_TENANT_INPUT: bool = False

    # ── 部署级界面开关（面向前端，经一个只读接口下发给前端照值渲染）──
    #
    # HIDE_TASK_CENTER：任务中心功能尚未完善，某些环境不该显示它的入口。
    # **默认 false = 不隐藏**
    #
    # 下发路径与登录页的 SSO 入口/租户框显隐同一套形状：
    #   env → platform 侧解析 → `GET /platform/frontend/ui-config` 下发
    #   `{"hide_task_center": bool}` → shell 顶栏取一次，true 就不渲染那个按钮。
    # 后端在 `capabilities/platform/api/platform_crud.py` 的 `get_frontend_ui_config`；
    # 前端在 `frontends/shell/src/api/uiConfig.ts`。
    #
    # ⚠ **只关顶栏那一个按钮**：子应用侧栏入口与路由守卫仍按权限码 `task_center:read`
    # 响应形状由登录页独占。
    # ⚠ 垃圾值（如 `ture`）由 Pydantic 的 bool 解析直接抛错拒绝启动，不会静默当成 false
    # —— 静默的失败方式是「配了却还是看得见、且没有任何提示」。
    HIDE_TASK_CENTER: bool = False

    @property
    def resolved_login_page_config(self) -> dict:
        """登录页配置的**解析后**形态（前端直接照此渲染）。

        `hide_tenant_input` 不是原样透传 HIDE_TENANT_INPUT：没有默认租户时
        隐藏输入框会让页面无法完成登录，故强制回落到「显示」。
        """
        tenant = (self.DEFAULT_TENANT_ID or "").strip()
        return {
            "default_tenant_id": tenant,
            "hide_tenant_input": bool(self.HIDE_TENANT_INPUT) and bool(tenant),
        }

    @property
    def login_page_config_conflict(self) -> bool:
        """配置冲突：要求隐藏租户框，却没有配默认租户（会导致登录不可用）。"""
        return bool(self.HIDE_TENANT_INPUT) and not (self.DEFAULT_TENANT_ID or "").strip()

    # 熔断配置
    CIRCUIT_BREAKER_ENABLED: bool = False
    CIRCUIT_BREAKER_THRESHOLD: int = 5

    # 平台监控采集配置（monitor.call_log / monitor.call_attempt 异步写口）
    # ⚠️ 独立队列，**不与审计队列共享 maxsize**（一个卡住不牵另一个）
    MONITOR_COLLECT_ENABLED: bool = True
    MONITOR_QUEUE_MAX_SIZE: int = 10000
    MONITOR_FLUSH_BATCH_SIZE: int = 100
    MONITOR_FLUSH_INTERVAL_MS: int = 200
    # 「系统后台发起」的场景标签表（逗号分隔）。命中即 trigger_type=system，
    # 其余算 user。默认只收**入库侧批量**场景（文档向量化 / 本体抽取）——
    # 检索链路（ontology_qa / rag_fusion 等）由用户提问触发，归 user。
    # 新场景上线后在此补一行即可，不必改代码。
    MONITOR_SYSTEM_SCENES: str = (
        "lightrag_embed,ontology_embed,ontology_extract,lightrag_extract"
    )
    # 明细表保留期（天）。<= 0 = 不清理（0 不是「立即删除」）。
    # 与 AUDIT_RETENTION_DAYS 对齐：监控明细同属运维数据，保留 90 天。
    MONITOR_RETENTION_DAYS: int = 90
    # 保留清理调度器的轮询间隔（秒）。<= 0 = 显式关闭（本地/测试可关）。
    MONITOR_RETENTION_SCHEDULER_INTERVAL_SECONDS: int = 86400
    # attempt_count 周期对账调度器（秒）。<= 0 = 显式关闭。
    # 背景：call_log / call_attempt 跨进程写事务重叠时互相不可见，批内补正
    # 会双双落空；由 platform 定期把最近窗口的 attempt_count 收敛到
    # call_attempt 行数。
    MONITOR_ATTEMPT_RECONCILE_INTERVAL_SECONDS: int = 30
    # 每轮对账回看窗口（分钟）。要覆盖写入慢 / 队列积压的极端情况。
    MONITOR_ATTEMPT_RECONCILE_LOOKBACK_MINUTES: int = 10
    # 单次查询允许的最大时间跨度（天）。<= 0 = 不限制。
    # 三族读接口（调用 / 任务 / Token）共用；超限返回 4004（不属于参数格式错误）。
    MONITOR_QUERY_MAX_RANGE_DAYS: int = 30

    # 审计日志配置
    AUDIT_LOG_ENABLED: bool = True
    AUDIT_LOG_ASYNC: bool = True
    AUDIT_QUEUE_MAX_SIZE: int = 10000
    AUDIT_FLUSH_BATCH_SIZE: int = 100
    AUDIT_FLUSH_INTERVAL_MS: int = 200
    AUDIT_HTTP_METHODS: str = "POST,PUT,PATCH,DELETE"
    AUDIT_RETENTION_DAYS: int = 90
    # 审计保留清理调度器的轮询间隔（秒）。**<= 0 = 显式关闭**（不启动后台任务）。
    # 默认一天一轮：审计日志淘汰是低频维护，没必要更密。
    AUDIT_RETENTION_SCHEDULER_INTERVAL_SECONDS: int = 86400
    AUDIT_INGEST_URL: str = ""
    # 仅记录关键行为：invoke 能力调用按 payload.action 关键字过滤，
    # 读类动作（list/get/search/query/preview 等）不入库
    AUDIT_KEY_ACTION_KEYWORDS: str = (
        "create,update,delete,remove,save,publish,modify,edit,import,"
        "review,retry,cancel,approve,reject,bind,unbind,enable,disable,reset,upload,"
        "recompile,reseed"
    )

    # Sidecar 服务地址
    SIDECAR_URL: str = "http://localhost:8001"

    # MCP Server 公网地址（mcp_config.url 来源）
    # 默认空串；生产由 deploy 环境变量注入 HTTPS，本地由 .env.local 显式填，无 localhost 兜底。
    MCP_SERVER_PUBLIC_URL: str = ""

    # Gateway 到 Sidecar 的内部认证 key
    # 默认值为哨兵字符串；生产部署必须用真实随机 key 覆盖（对齐 INTERNAL_API_KEY）。
    # Gateway 启动时在 on_event("startup") 中校验生产未覆盖则拒绝启动。
    GATEWAY_API_KEY: str = "change-me-in-production"

    # MCP Server 的内部 API Key（用于 /internal/* 端点认证）
    # 默认值为哨兵字符串；生产部署必须用真实随机 key 覆盖。
    # Gateway 启动时会在 on_event("startup") 中校验非默认值，拒绝启动。
    INTERNAL_API_KEY: str = "change-me-in-production"

    # 能力服务地址
    KNOWLEDGE_BASE_URL: str = "http://localhost:8003"
    BUSINESS_DOMAIN_URL: str = "http://localhost:8005"
    DATA_GOVERNANCE_URL: str = "http://localhost:8011"
    ATOMIC_RAG_URL: str = "http://localhost:8004"
    PLATFORM_URL: str = "http://localhost:8006"
    OPENKB_URL: str = "http://localhost:7566"  # [jonex]
    JONEX_WIKI_URL: str = "http://localhost:7567"  # [jonex] Sidecar → 适配器容器（实现 /invoke）
    JONEX_WIKI_ENGINE_URL: str = "http://localhost:8007"  # [jonex] 适配器 → 引擎容器（jonex_wiki REST）
    JONEX_WIKI_API_KEY: str = "jonex_wiki_engine_key"  # [jonex] 适配器→引擎内部 key（方案 B 单一 key，与引擎 JONEX_WIKI_API_KEY 一致）
    ONTOLOGY_COMPILE_URL: str = "http://ontology-compile:8009"  # [jonex] Sidecar → 本体编译独立容器（P2，B1 单容器，实现 /invoke）

    # 注：此处原有 `CONTAINER_REST_ENABLED`（能力容器并行 REST 层开关）。
    # 该层（kb 84 条 + business_domain 53 条）已于 2026-10 渗透测试整改中
    # **物理删除**：它不在生产链路上（一律经 Sidecar `/invoke`），却无鉴权且按
    # 客户端 `X-Tenant-ID` 决定租户，是报告 3.2/3.3/3.4/3.5 的根因。
    # 开关随实现一并移除 —— 不留「一个 env 就能把攻击面开回来」的隐患。
    # `platform` 不受影响：它的 `/api/v1/*` 是**生产链路**（Sidecar REST 透传，
    # 承载登录引导），见 `capabilities/platform/capability.py`。

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
    LLMGW_RERANK_MAX_DOCS: int = 12                    # 单批最多重排文档数（控单批时长）
    # rerank 并发上限。两个 binding 都消费它：
    #   · ollama-generate：逐文档并发打分（原用途）；
    #   · cohere（现网用的这个）：**批并发** —— 批数超过本值时按轮次串行。
    # 即：默认 4 时，115 条 = 10 批要跑 3 轮，不是一轮出完。
    LLMGW_RERANK_CONCURRENCY: int = 4
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
    # 仅对命中 MODELS 且 scene 命中 SCENES 的 chat 请求注入思考控制字段
    # （具体写什么由 LLMGW_THINKING_FIELDS 决定，见 jonex_core/common/thinking.py）
    LLMGW_DISABLE_THINKING_ENABLED: bool = True
    # 逗号分隔；空=不限模型（注意：空会把 thinking.disabled 注入到**所有**模型，
    # 所以兜底取「本部署的默认模型」而不是空串）。
    # ⚠️ 不再写字面量：模型名唯一来源是环境变量（见 jonex_core.common.model_config）。
    # 本字段未配置时回退 DEFAULT_LLM_MODEL；它也缺失则留空。
    LLMGW_DISABLE_THINKING_MODELS: str = Field(
        default_factory=resolve_optional_llm_model,
    )
    # 逗号分隔；空=不限场景。分档策略见 docs/ontology-query-thinking-latency-fix-plan.md §3：
    # 抽取场景恒禁；ontology_arbitration 恒禁（二选一分类）；`_fast` 变体=严格模式快档；
    # 原始查询 scene（ontology_qa/rag_chunk_qa/rag_fusion）不在列表=精档与非严格保留思考。
    # abstract_schema_align（概念对齐候选生成）按**抽取档**恒禁：它是同类任务 ——
    # 一次性结构化 JSON 输出（response_format=json_object），不需要推理链，而输入可以很大
    # （267 个实体类型 ≈ 17.5k prompt token）。漏登记时故障形态是**静默的**：网关不注入、
    # 模型思考 token 先把输出预算占满，正文被截断成非法 JSON 或直接为空，调用侧只看到
    # 「JSON 解析失败 / 返回空内容」。（该调用的输出预算是多少见
    # ontology_llm.propose_concept_clusters —— 已按实体数放大，这里不复述数字。）
    # nl2sql_generate（② NL→SQL 分源查询的 SQL 生成）同样按**抽取档**恒禁：它也是
    # 一次性结构化输出（一段确定语法的 SQL，不需要推理链），且思考 token 会把输出
    # 预算吃光 → 回包被截断成非法 SQL / 空内容，调用侧同样看不出根因在网关配置。
    # nl2sql_multiturn（② 的多轮档）同理：每轮产出要么是工具调用参数、要么是与单次档
    # 同形的终答，都是结构化输出；**不登记 = 网关静默不关思考**，思考 token 先把每轮
    # 预算吃光（历史故障见 `ontology_llm.propose_concept_clusters`）。
    # abstract_schema_attr_names（「自动构造」的属性中文名档）同样按**抽取档**恒禁：
    # 它也是批量的「属性 → 中文 display_name」一次性结构化 JSON 输出，输入规模是
    # 236 实体 × 数千属性，思考 token 会把输出预算吃光 → 回包被截断成非法 JSON /
    # 空内容，调用侧同样看不出根因在网关配置。
    # concept_relevance_filter（抽象概念候选的相关性筛选，v2）同属抽取档恒禁：
    # 输入是问句 + ≤ 数十条候选，输出只是一个下标数组；不登记 = 网关静默不关思考，
    # 思考 token 先把输出预算吃光 → 回包截断（调用侧按「筛选失败」原样放行，看不出根因）。
    # nl2sql_alias_suggest（闭环别名候选）同属抽取档恒禁：一次性结构化 JSON，漏登记 = 静默截断。
    # nl2sql_entry_revise（演化台「条目人工调整」的一轮修订）同属抽取档恒禁：一次性结构化
    # JSON 输出（{"revised","rationale"}），漏登记 = 网关静默不关思考 → 思考 token 吃光输出
    # 预算 → 回包截断成非法 JSON，弹窗只能看到「修订失败」，看不出根因在网关配置。
    # abstract_schema_focus（聚焦生成的种子解析）与 abstract_schema_align 同档恒禁：
    # 输入是同规模的实体清单，输出是结构化 JSON 种子组；思考 token 会吃光输出预算。
    # nl2sql_supplement_fuse（「本体+数据」的融合 / 数据梳理）同属抽取档恒禁：一次性短抽取，
    # 思考 token 会吃光 1200 / 400 max_tokens → 正文为空 → 静默回退（调用侧看不出根因）。
    # nl2sql_value_synonym_suggest（值同义词候选）同属抽取档恒禁：一次性结构化 JSON，
    # 漏登记 = 网关静默不关思考 → 回包截断 / 空正文，候选生成直接 failed。
    LLMGW_DISABLE_THINKING_SCENES: str = (
        "lightrag_extract,ontology_extract,raganything_ingest,abstract_schema_align,"
        "abstract_schema_landing_align,abstract_schema_focus,"
        "ontology_arbitration,ontology_qa_fast,rag_chunk_qa_fast,rag_fusion_fast,"
        "image_query_caption,"
        "nl2sql_generate,nl2sql_multiturn,nl2sql_format,nl2sql_value_map,"
        "nl2sql_probe_summary,nl2sql_candidate_select,"
        "abstract_schema_attr_names,"
        "concept_relevance_filter,nl2sql_alias_suggest,nl2sql_entry_revise,"
        "nl2sql_value_synonym_suggest,nl2sql_supplement_fuse,ontology_qa_tools"
    )
    # 思考字段注入写法，语法 <字段路径>.<类型>[.<取值>][,...]（见 jonex_core/common/thinking.py）：
    #   string → enabled / disabled（省略取值 = disabled）
    #   bool   → true / false      （省略取值 = false）
    # 缺省 thinking.type.string = 历史 TokenHub 行为；Qwen3 自建服务用
    # chat_template_kwargs.enable_thinking.bool。
    # ⚠️ 本项只决定「怎么写」，是否被上游认需 curl 实测。
    LLMGW_THINKING_FIELDS: str = "thinking.type.string"
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


# ==================== 任务中心 ====================
class TaskCenterSettings(BaseSettings):
    """任务中心（task_center）的全部配置。

    ⚠️ 本模块所有 `TASK_CENTER_*` 配置**只在这个类里**，不散到别处。
    """

    # 能力服务地址：Sidecar 的静态端点表里 `task_center` 指向它（§4.1）。
    TASK_CENTER_URL: str = "http://localhost:8007"
    # 接入方（知识库等）调任务中心的 HTTP 超时（秒）。跟下面的 `_RUNNER_TIMEOUT`
    # 是**两个方向**：这个是「接入方 → 任务中心」，那个是「任务中心 → 运行方」。
    #
    # **管哪些方法**（只在接入方以 `mode: remote` 经 Sidecar 反代时生效；
    # `local` 是进程内直调、`mock` 是桩，都不走 HTTP，没有这个超时）：
    #   · `report_status`（§4.2 ①）—— 用本值；
    #   · `init_task`（§4.2 ②）—— **不用本值**，被下面的
    #     `TASK_CENTER_CLIENT_INIT_TASK_TIMEOUT` 覆写（**若在事务里调**，等它多久
    #     那笔事务就持锁多久，理由见那条配置）；
    #   · `execute` / `remove_task`（§4.1）—— **不归它管**：那两个方向是
    #     「任务中心 → 运行方」，由运行方实现、管理方经 `runner_adapter` 调，
    #     超时是下面的 `TASK_CENTER_RUNNER_TIMEOUT`。
    TASK_CENTER_CLIENT_TIMEOUT: int = 30
    # `init_task` 专用的单次 HTTP 超时（秒）—— 上面那个 30 秒对它的覆写。
    #
    # 为什么要单独给一个更短的：**上传路径上的 `init_task` 有可能是在事务里调的**。
    # 知识库 `upload_document` 的旧形态正是「文档行 INSERT 与 `init_task` 同事务，
    # 失败一起回滚」；事务不结束，`QuotaService.check_upload_counts` 拿的
    # `pg_advisory_xact_lock`（键 `quota:doc:{租户}`）就不释放 —— 也就是说
    # **等 `init_task` 等多久，同一个租户的其它上传就在这把锁上排多久**。
    # 30 秒的全局值一旦被任务中心卡死放大，会变成该租户上传整体停摆 30 秒。
    #
    # ⚠️ **2026-09-28 起，Ontology 库（任务中心的首个接入方）的上传路径已经不这样调了**：
    # `upload_document` 先落文档行（它自己那笔事务），`_upload_via_task_center` 再**另起**
    # 一笔事务落接管行 + 粘性值，**两笔都提交之后**才单独提交任务（顺序的理由见
    # `_upload_via_task_center` 的 docstring：反过来会让常见失败变成死任务）。
    # 所以「锁被占住」这段代价对**今天这条路径**不再适用 —— 这个值仍保持 5 秒不动，
    # 因为它是「单次 HTTP 上限」，与在不在事务里无关；将来若有接入方回到
    # 「事务里建任务」那种形态，上面这段仍然是它该读的理由。
    #
    # 5 秒够不够：「建一条任务」正常是几十毫秒（校验定义 / 版本 / 空间 → 落四张表），
    # 5 秒已是百倍余量。调小它的前提是**你清楚自己不在事务里等**。
    #
    # ⚠️ **这是单次上限，不是「这条路最多花多久」的上限**。走 SDK（`TaskRuntime.submit`）且
    # **自带 `task_id`** 时会重试满预算（首次 + 2 次，退避 0.5s / 1s），于是最坏干等
    # **3 × 5s + 1.5s = 16.5s**；在事务里调的话，那笔事务（连同 `quota:doc:{租户}`）
    # 也就被占这么久，不是上面那个 5 秒。不带 `task_id` 的调用方不重试，才真的是 5 秒。
    # 16.5 秒可不可接受是产品判断；要压就压这个值本身（它按值放大 3.3 倍）。
    TASK_CENTER_CLIENT_INIT_TASK_TIMEOUT: float = 5.0

    # 后台循环（触发抢占 → 派活抢占 → 消化上报 → 超时巡检 → **补收口**，§7.5）。
    # **恒开、不设开关** —— 这几趟是任务中心的心脏，关掉等于这个服务没意义；
    # 多副本下不选主、靠 CAS 抢占，所以每一份都跑。
    # 一轮扫描的间隔（秒）。
    TASK_CENTER_SCHEDULER_INTERVAL: float = 3.0
    # 「补收口」那一趟的**降噪**阈值（秒）：任务行至少这么久没被动过，才值得去看它一眼。
    #
    # 那一趟要救的是「任务快照还停在 `pending`/`running`、但当次运行早就跑完了」——
    # 成因是收口那一刻任务快照那次 CAS 落空（偶尔会真的发生）。那种任务没有任何一趟后台
    # 会碰它，三扇操作门也各自拒它，从前只剩人工改库。
    #
    # ⚠️ **这个数只管降噪，不管正确性**：真正的「该不该收口」由 Python 侧那把唯一的尺子判
    # （`_run_settled`）。所以调大只是少查几次、调小只是多查几次，**两种都不会判错**。
    TASK_CENTER_SETTLE_STALE_SEC: int = 60
    # 一轮里各扫多少条，防止一个副本一口气把整库的活都揽下。
    # ⓪ 触发抢占 / ① 派活 / ② 消化上报各自用这个上限、各算各的 10 条（不是加起来 10 条）；
    # ① 里的「防僵尸」和 ③ 超时巡检**都不受它约束**（一次全清）。
    #
    # ⚠️ 对 ① 派活，这个数是**一轮真派出去几条**，**不是「一轮扫几条候选」**：扫描按
    # `TASK_CENTER_SCHEDULER_SCAN_FACTOR` 放宽，派满这个数就停。理由见那条配置。
    TASK_CENTER_SCHEDULER_BATCH: int = 10
    # ① 派活**扫描面**相对 `TASK_CENTER_SCHEDULER_BATCH` 的放大倍数：一轮扫
    # `BATCH × 这个数` 条候选，其中**真派出去的**够 `BATCH` 条就提前收工。
    #
    # 为什么扫描面要放宽（这是「派活队头阻塞」的修法）：被 `max_concurrency` 挡下的
    # 候选**一个字都不写库**（`_over_concurrency` 在 `claim_pending` 之前就 return，
    # 所以它还留在 `pending`、`updated_at` 没动、**排序键也没动**）→ 下一轮它还在
    # 候选集里、还在原位。若扫描面就是 `BATCH`，队头那 10 条一旦全卡在同一个瓶颈上
    # （比如某一步的并发上限占满 —— 内置三个步骤的 `max_concurrency` 当前都是 `10`），
    # **每一轮都被它们吃满 10 个名额**，排在它们
    # 后面、配额明明空着的活（别的 `step_code`、别的租户）连窗口都进不去 —— 一直堵到
    # 队头被防僵尸判死（`timeout_seconds × 3`，内置步骤 = 21 小时）。
    #
    # 放宽扫描面之后：被挡下的候选只消耗扫描名额、**不消耗派活名额**，一轮照样能派满
    # `BATCH` 条。代价是「这堆候选全是满的」时一轮要多做几次加锁 + 判条件的空转 ——
    # 纯本地读，不出站，相比 21 小时的队头阻塞可以忽略。5 倍是「队头堵住 4 层仍能派满」
    # 的余量；调大只会更稳，调小到 1 就退回修之前的行为。
    TASK_CENTER_SCHEDULER_SCAN_FACTOR: int = 5
    # 「就绪的 pending 卡多久算僵尸」——⚠️ 配的是**倍数**、不是秒数（§7.5、附录 A.4 D2b）：
    # 判据是 `现在 − 该子任务的 updated_at > 该步 timeout_seconds × 这个倍数`，
    # 所以每个子任务按自己的 timeout_seconds 算、阈值各不相同。超了就置 timeout。
    TASK_CENTER_PENDING_STUCK_TIMEOUT: int = 3
    # 一条上报**连续消化失败多少次就判死信**（§7.5 ②）。
    #
    # 上报收件箱的兜底出口：`_absorb` 判不出来的失败（DB 类异常）会每轮重现，而
    # `_report_pass` 每轮只捞 `list_pending(BATCH)` 的前 BATCH 条（按 `reported_at`
    # 升序）—— 一条永远失败的上报只要排在最前面，就**每轮占着一个名额**，后面的正常上报
    # 全得排队，严重时整条队列堵住。达本值就判死信：`dead_lettered=1` + `is_deleted=1`
    # （**出队**，名额让出来）+ ERROR 告警日志。
    #
    # **死信不是把上报丢掉**：行永远留着，`attempts` / `last_error` / `dead_lettered`
    # 都在，「管理方到底收到没、卡在哪」查得出来；只是不再自动重试（要放它重来，把那一行的
    # `dead_lettered` 与 `is_deleted` 都清回 0、`attempts` 归 0 即可 —— `is_deleted` 不能漏，
    # 队列只看它）。
    #
    # 判死是**按"上报行"算**的，不是按子任务算：同一张凭证重发一条新的上报，`attempts`
    # 从 0 重新起算 —— 运行方自己的重发预算是 3 次（`RETRY_MAX_ATTEMPTS`），堆不出量来。
    # 5 与知识库的 `ONTOLOGY_WRITE_RETRIES` 同量级；**调小它等于更早放弃**，别跟着
    # 瞬时故障的窗口调（那种错本来就该重试过去）。
    TASK_CENTER_REPORT_MAX_ATTEMPTS: int = 5
    # **基础设施故障那一档的绝对上限**（小时，§7.5 ②）。⚠️ 这是「让路死锁」的解药，
    # 动它之前把下面读完。
    #
    # 上面那条阈值**只管「这条上报自己的错」** —— 基础设施故障那一档
    # （`_is_infra_failure`：Redis / DB 连不上、可重试码）**刻意不递增 `attempts`**
    # （抖十几秒不是上报的错），于是它也**永远到不了阈值、永远留在收件箱里**。
    # 而超时巡检的「让路」判据是「收件箱里还有这条子任务的未消化上报，就先别判它超时」
    # （`list_timed_out_for_patrol` 里那个 EXISTS）。两者一叠 = **一条永远出不去的
    # 基础设施档上报，把那条子任务永久钉在 `running`**：
    #   · 子任务收不掉 → 任务永远不终态 → `retry_task` / `task.delete` 全都要求终态
    #     → **那条任务彻底无法处置**；
    #   · 那个 `running` 还**占着并发名额**（按「目录 + `step_code`」共用额度）→ 攒够上限
    #     → **这一步在本目录派不出活**。
    #
    # 解药：一条上报在收件箱里躺过本值（判据 `now - reported_at`，也就是「它等了多久」），
    # 就**不管是谁的错**一律判死信、让它出队 —— 躺这么久已经不是「等一等就好」，
    # 而是「出不去了」。真正的短时故障（Redis 抖十几秒、DB 重启一分钟）在本值之内早就
    # 恢复并处理完，**完全不受影响**。
    #
    # 代价（改了要认）：真出现**持续超过本值的大故障**（如 Redis 挂了一整天）时，那期间
    # 积压在收件箱里的上报会被判死信出队。但那时它们本来也处理不了；记录都留着
    # （`last_error` / `dead_lettered`）+ ERROR 日志点得出名字，人工查得到、能补
    # （把那一行的 `dead_lettered` 与 `is_deleted` 都清回 0、`attempts` 归 0 即可重来）。
    # 24 小时 ≈「一个工作日之内必有人看一眼」；调大只会更晚兜底；**别调到 0**
    # —— 那等于「下一次记失败就判死信」，把上面「不计数」这条设计整个抵消掉。
    TASK_CENTER_REPORT_INFRA_MAX_HOURS: float = 24.0

    # 派活出站调运行方的**单次 HTTP 超时**（秒）。派活是「投递 + 回执」、不等对方跑完，
    # 所以给得短；真正的执行耗时由子任务自己的 `timeout_seconds` 在超时巡检里管（§7.4）。
    TASK_CENTER_RUNNER_TIMEOUT: float = 30.0
    # 「停止时通知运行方」（§4.1 ③，`task_stopped`）的**单次 HTTP 超时**（秒）。
    #
    # 为什么比上面那个 30 秒短得多：这一条跑在**用户点「停止」那条同步请求**里 ——
    # 用户点一下按钮，不该为一次「通知」干等半分钟。而且它是**通知、不是命令**
    # （停不停由运行方自己决定），发不出去也不影响「停止」本身成没成；丢了只是那份
    # 被接管的文档晚一点解锁（退回看门狗那条 30 分钟的兜底，不会更差）。
    #
    # ⚠️ **本值只用于这一个动作**：`execute` / `remove_task` 仍用 `TASK_CENTER_RUNNER_TIMEOUT`。
    TASK_CENTER_NOTIFY_TIMEOUT: float = 5.0
    # **对账（拉）**：子任务派出去多久没消息，才值得主动去问运行方一句（秒，§4.1 ④）。
    #
    # 背景：推进编排原来**只能等运行方上报**，上报丢了子任务就挂到编排硬超时（预置 7 小时）。
    # 现在多一条路 —— 管理方**主动问**（`query_step_status`），问不到下一拍再问，**不存在永久丢**。
    #
    # 这个值只决定「**多久算沉默**」，不决定对账本身的节拍：问早一点没有代价（问到就收口、
    # 问到「还在跑」就再等），唯一的成本是打扰运行方。默认 120 秒 —— 比派活的 30 秒超时宽，
    # 比谁都短的硬超时（编排 7 小时、看门狗 30 分钟）窄得多。
    TASK_CENTER_QUERY_QUIET_SEC: int = 120
    # 触发抢占「读—算—写」撞车后的重试次数（§4.3 M5b）：这几步走的是带 `state_version`
    # 条件的更新，命中 0 行说明别的副本刚推过、版本号 +1 了 → 重读再来；试满仍撞车就报冲突。
    TASK_CENTER_CAS_ATTEMPTS: int = 3

    # 任务锁（§7.2）：Redis `SET NX PX` + UUID token，键 `task_center:lock:task:{task_id}`。
    # 租约时长（毫秒）—— 锁**不跨出站调用**，所以只要盖住一次事务就够，不需要很长。
    TASK_CENTER_LOCK_TIMEOUT: int = 30000
    # 抢锁的**短重试**：3 次 × 50ms（§7.2 ①）。仍拿不到就按调用方分**三类**处置：
    #   ① 调度循环里「本轮跳过、下轮还会扫到」的那些（派活抢占 / 超时巡检
    #      / 消化上报）→ 本轮跳过；
    #      ⚠️ **触发抢占不在这一组，也不在任何一组 —— 它压根不拿任务锁**：它防的是
    #      「两个副本同时触发同一条触发器」，走的是 `task_triggers` 自己的 `state_version`
    #      CAS（`scheduler_service`）。别照这一行去找它的锁策略 —— 从前的注释把它列在
    #      ①里，与 `task_lock.py` 顶部那份「不拿锁的三类」直接打架。
    #   ② 调用方主动发起的写操作（retry_* / **task.stop** / task.delete）→ **绝不能跳过**，
    #      继续等（拉长窗口，走下面那对配置），仍拿不到就回「可重试」的错误码让调用方
    #      自己重试；跳过 = 调用方以为成了、事却没做。
    #   ③ **已经拿到运行方回执、只差落库收尾的那一段**（`_finalize_one`）→ 同样绝不能
    #      跳过：跳过等于「运行方回了、没人记」，回执就丢了。它属于调度循环，但**不适用
    #      ①**，判据是「跳过会不会丢东西」而不是「谁调用的」。
    #   ⚠️ **`report_status` 不在②里**（它从前在）：上报**不拿任务锁**，入队即可。
    TASK_CENTER_LOCK_RETRY: int = 3
    TASK_CENTER_LOCK_RETRY_INTERVAL: float = 0.05
    # 「主动发起的写操作」拉长后的等待窗口：30 次 × 100ms（§7.2 ①）。
    TASK_CENTER_LOCK_WAIT: int = 30
    TASK_CENTER_LOCK_WAIT_INTERVAL: float = 0.1

    class Config:
        env_file = ".env"


# ==================== NL→SQL 分源查询（②） ====================
class NL2SqlSettings(BaseSettings):
    """NL→SQL 分源查询（②）的全部配置。

    ⚠️ 本模块所有 `NL2SQL_*` 配置**只在这个类里**，不散到别处。
    """

    #: 单次查询最多回多少行。**这是行数上限的唯一出处**：guard 按它注入 LIMIT。
    NL2SQL_MAX_ROWS: int = 200
    #: 连库与执行的总超时（秒）。目标库是外部系统，必须有界。
    NL2SQL_TIMEOUT_SECONDS: int = 30
    #: 生成 SQL 那次 LLM 调用的超时（秒）。SQL 很短，不该等太久。
    NL2SQL_LLM_TIMEOUT: int = 60
    #: 单次档生成 SQL 的 max_tokens。默认 1024（只出一条 SQL）；**开思考时必须放大**
    #: （reasoning 会先吃掉预算），否则空正文/截断。
    NL2SQL_LLM_MAX_TOKENS: int = 1024
    #: 多轮档每轮往返的 max_tokens。默认 2048（工具调用 + 最终 SQL）。
    NL2SQL_MULTITURN_LLM_MAX_TOKENS: int = 2048
    #: 问句长度上限（字符）。**这是「调用方用错」与「答不了」的分界线**：超了抛
    #: `InvalidParameterError`，不是回 `not_answerable` —— 问句本身不合法，换个源
    #: 也答不了，混进「没有数据答案」会让 ③ 一直重试。500 足够宽，正常问句到不了。
    NL2SQL_MAX_QUESTION_LENGTH: int = 500

    #: 多轮档开关（P2-2）：**2026-10-02 第三档后默认开**（原默认关）。
    #: 翻默认的依据（同日真机）：① 能力题 3/3 绿（2 跳，单次答不了）；② 第三档「标识符精确性行」
    #: 修掉多轮专属拼错（同窗口 A/B 4/20→0/20）后，**同窗口双臂 26/26 ≥ 单次 25/26**（「不降」门成立）。
    #: 代价与要求：每问句最多 5 轮往返（端到端延迟约 **+40%**、峰值 8.4s）；llm-gateway 的 scene 白名单
    #: 必须登记 `nl2sql_multiturn`（否则思考 token 吃光每轮预算）。**置 0 即回单次档（一字不动）**。
    NL2SQL_MULTITURN: bool = True
    #: 多轮档的**总预算**（秒）：整个循环（最多 max_rounds+1 次往返）包一个 wait_for。
    NL2SQL_MULTITURN_TIMEOUT: int = 120
    #: 多轮档最多几次 LLM 往返（**全程带 tools**；预算用尽那轮若仍要调工具，
    #: 接住它再补一次收尾 ⇒ 实际调用数上界 = 本值 + 1）。
    #: ⚠️ 3 → 5（续档 2026-10-02 真机矩阵）：模型实测要「3 次探索 + 1 次终答」才肯收口，
    #: 4 仍差一轮（差一轮 = 补轮也还在调 ⇒ 抛）；5 是三条硬样本全过的档。别只看成本调低。
    NL2SQL_MULTITURN_MAX_ROUNDS: int = 5
    #: 每回合最多执行几个工具调用（超出的回错误文本给模型）。
    NL2SQL_MULTITURN_MAX_TOOL_CALLS_PER_ROUND: int = 3

    #: 运行时观测开关（2026-10-12 运行时观测线）：`1`（默认）= 每次 ask 落一行观测
    #: （失败现场 + 工具轨迹）供诊断/演化消费；`0` = **完全不写** —— 失败行不再有观测、
    #: 诊断存量耗尽后**会饿**（部署纪律，见设计 §8）。回滚 = 置 0 + 重建 KB 容器。
    NL2SQL_OBSERVE_ENABLED: bool = True

    #: 语义注入档开关（2026-10-13 TBox 落点线，§5.3）：`1`（默认）= 把抽象字段落点的
    #: 「概念 → 多列」映射按需注入 SQL 生成 payload（只在**有落点差异**时；无差异时
    #: payload 与 prompt 逐字节不变）。`0` = 回退（不组装、不注入、不下发 4e 段）。
    NL2SQL_CONCEPT_FIELDS: bool = True

    #: 多跳 join 路径化（2026-10-16）：**额外**注入「锚点 1 跳之外」的 join 路径（`min_hops=2`），
    #: 让模型看得到真多跳的走法（1 跳边仍由 `joins` 照发，不替代）。
    #: ⚠️⚠️ **默认开，但增量「未证实」—— 别把这句话读成「门过了」**（2026-10-16 用户拍板保留）。
    #: 翻档当时的测量是 off 29/35 → on 30/35（35 题 × `--repeat 3`）；**但后续用「交错 A/B」
    #: （按时间块交替开关、让服务端跨时段漂移同等作用于两臂）重测那道据称被救回的题，
    #: 得到 **OFF 18/18 · ON 18/18** ⇒ 那次「off 失败 / on 通过」是**漂移撞上的**，
    #: 不是本功能的效应。**本线现在的立场是：本功能在这套语料上测不出增量。**
    #: ⚠️ **成因不是「题不够难」，是「join 图这类题原理上就测不出」**（2026-10-16 追加实测）：
    #: 又造了 4 道**真 3 跳**题（company→jobs→working_lifes→apply_jobs 等），**关档下仍 3/3 全过**；
    #: 两臂中位耗时差在 ±330ms 内（基线 ~4.5s）⇒ **效率上也没测出增益**。
    #: 机制：**模型自带的 `list_related` 工具本来就能自己走 join 图**（连 3 跳也行），
    #: 路径注入把「模型自己走」换成「平台预先走」，**结果一样** ⇒ **任何 join 图问题都判别不出本功能**。
    #: ⇒ 唯一能判别的形态是**血缘才能到达的表**（schema 里不参与任何 join 关系的那种）——
    #: 那种表**任何工具都揭露不了**，只有换乘能带模型看见（真机走查已验证过一次）。
    #: 而那需要**真实的血缘来源**。手工来源的首次验证（2026-10-16）用的是
    #: `deploy/config/nl2sql/lineage_edges.json` 里的 15 条列级边（覆盖 5 张编译 schema 里
    #: 孤立的表，**全部经值包含验证**、100% 命中才采纳）：默认档下真机写出过
    #: `JOIN dbo.job_forward jf ON jf.job_id = j.id` —— 那张表是孤立的，**任何工具都揭露不了，
    #: 只有注入的换乘路径能带它看见** ⇒ 换乘端到端成立。
    #: ⚠️ **运行时来源已换成表**（2026-10-16 Task 5）：`knowledge_base.lineage_edges`（迁移 010），
    #: 由 KB 设置「血缘」页签写入、② 经 `load_lineage_edges_from_db` 读取。上面那个 JSON 文件
    #: **已清空（`edges: []`）且运行时不读它** —— 它留给 P1 的 file 摄取通道
    #: （`load_lineage_edges()` 仍是那条通道的解析器；`NL2SQL_LINEAGE_EDGES_PATH` 只是它的默认路径）。
    #: ⚠️ 该文件目录**仍必须被挂载进容器**（`deploy/docker-compose.yml` 里已补）：默认路径是
    #: `deploy/config/nl2sql/…`，而 Dockerfile 不 copy `deploy/config/` —— 不挂时 file 摄取
    #: 会在容器里**静默返回空**（表这条运行时路径不受影响）。
    #: 保留默认开是用户的**显式选择（理由是「至少不劣」）**，不是「已证有效」。
    #: `0` = 回退到「只发 1 跳边」。
    #: ⚠️ 配套改动（缺任一半模型写对了也白写）：`all_join_edges` 给足边池 + 安全闸白名单
    #: 并入注入路径上的表（Task 6B/6C）。
    #: ⚠️ **量这个功能的任何 A/B，必须用交错块**：`--repeat` 只采样会话内抖动，
    #: 而实测方差在**会话之间**（同一配置前后两次跑出 0/10 与 18/18）。
    NL2SQL_JOIN_PATHS: bool = True
    NL2SQL_JOIN_PATHS_MAX_HOPS: int = 3
    NL2SQL_JOIN_PATHS_MAX_PATHS: int = 5

    #: 投影契约（2026-10-28，P0-A1）：`1` = 提示词追加「只投影问句要的列」规则 +
    #: 生成后 AST 检查（`nl2sql/projection.py`），标量问句多列时**最多修复一次**。
    #: `0`（默认）= 提示词/payload/流程与引入前**逐字节相同**。
    NL2SQL_PROJECTION_CONTRACT: bool = False

    #: evidence/hints（2026-10-28，P0-A2）：`1` = `ask` 接受可选 `hints` 并注入
    #: payload/提示词（BIRD evidence、业务口径、值映射）。`0`（默认）= 忽略 hints，
    #: 请求/提示词/payload 与引入前逐字节相同。
    NL2SQL_HINTS: bool = False
    #: 生产侧 hints 自动组装（A2 第二半，2026-10-28）：`1` = 在 hints 通道开启时，
    #: 由 Jonex 从锚点/1 跳实体的描述、枚举值、口径与指标组装 hints。**不影响**
    #: 调用方显式传入的 hints（两者合并去重）。`0`（默认）= 只用调用方 hints，
    #: 行为与引入前完全相同。
    NL2SQL_AUTO_HINTS: bool = False
    #: 列级示例值扩容（P0-1，2026-10-28）：`1` = 锚点实体给**所有**列的示例值
    #: （每列最多 3 个、总预算 2400 字符），不再只给「像枚举」的列。`0`（默认）=
    #: 维持 `pick_samples` 现有口径，payload 与今天逐字节相同。
    NL2SQL_COLUMN_EXAMPLES: bool = False
    #: 列描述注入（2026-10-30）：`1` = 锚点/1 跳列的 `description` 随列清单进 payload
    #: （截 120 字符）。`0`（默认）= 列清单与今天逐字节相同。
    NL2SQL_COLUMN_DESC: bool = False
    #: 空结果修复（P0-2，2026-10-28）：`1` = 最终 SQL 执行成功但 0 行时，
    #: 带“0 rows + 列样例”反馈重生成**一次**；`0`（默认）= 不重试，空结果照常返回。
    #: 合法空结果存在（问“没有 X 的公司”），所以必须开关 + A/B。
    NL2SQL_EMPTY_REPAIR: bool = False
    #: 指标编译影子对比（W3.3 M4，2026-11-01）：`1` = 真实提问在答案产出后**旁路**
    #: 跑一次「指标编译 SQL」，只记录不替换（`nl2sql_metric_shadow`）；
    #: `0`（默认）= 零副作用（不调度、不读 schema、不建任务）。合成流量/observe_only
    #: /record_evidence=False 一律不采样。开启会额外占用数据源连接，按 2–4 周窗口使用。
    NL2SQL_METRIC_SHADOW: bool = False
    # W4.8：指标级调用审批（默认关；关时 ask_metric 不查审批表）。
    METRIC_CALL_APPROVAL_ENABLED: bool = False
    # W4.3b：单元格级溯源（只描述定义/落点/SQL 快照，不抓源行）。
    METRIC_CELL_PROVENANCE_ENABLED: bool = True
    METRIC_CELL_PROVENANCE_MAX_CELLS: int = 200
    # W1.6：只读查询结果缓存（默认关；键含 engine + 实际执行 SQL）。
    NL2SQL_QUERY_CACHE_TTL_SECONDS: int = 0
    NL2SQL_QUERY_CACHE_MAX_ENTRIES: int = 128

    # ── W4.2 指标问数执行面 ──────────────────────────────────────────
    #: 指标结果进程内缓存 TTL（秒）：`0`（默认）= 关闭。键含 schema 写代号，
    #: 任何 schema 写入立即失效；只缓存**成功**结果。
    METRIC_ASK_CACHE_TTL_SECONDS: int = 0
    #: 缓存条目上限（防无界增长）。
    METRIC_ASK_CACHE_MAX_ENTRIES: int = 128
    #: 单次指标执行的语句级超时（秒）：`0` = 沿用 `NL2SQL_TIMEOUT_SECONDS`。
    METRIC_ASK_TIMEOUT_SECONDS: int = 0
    #: 空结果反馈里追加「先简化过滤条件」（S5，2026-10-30）：只在空结果修复触发时生效。
    #: `0`（默认）= 反馈文案与引入前逐字节相同。
    NL2SQL_EMPTY_SIMPLIFY: bool = False
    #: 字面量值接地（P1-1，2026-10-28）：`1` = 从最终 SQL 抽字符串字面量，
    #: 在白名单列上查真实值；仅当候选与字面量只差大小写/空白时**确定性改写**。
    #: `0`（默认）= 不查库、不改 SQL，行为与今天逐字节相同。
    NL2SQL_VALUE_LOOKUP: bool = False
    #: 值查询单次超时（秒）。失败 fail-open，不影响提问。
    NL2SQL_VALUE_LOOKUP_TIMEOUT: float = 3.0
    #: 值接地增强（P1-1 v2，2026-10-28）：`1` = 除大小写/空白外，再做唯一 initialism /
    #: 前缀扩展 / fuzzy（≥0.85）匹配；多个候选或低置信度不猜。`0`（默认）= v1 行为。
    NL2SQL_VALUE_LOOKUP_FUZZY: bool = False
    #: 值同义词注入（P1，2026-11-04）：`1` = ② 组装 payload 前按问句词面命中注入
    #: `value_aliases`（KB 级 `ontology_synonyms` 的 `terms → canonical`）；
    #: 同档还会在**候选召回之前**按同一份组把问句里的 variant 改写为 canonical
    #: （2026-11-05，否则召回看到原词面、no_candidate 卡在注入之前）。
    #: `0` = 不读表、不注入、不改写，payload / 提示词与引入前**逐字节相同**（回滚开关）。
    NL2SQL_VALUE_ALIASES_ENABLED: bool = True
    #: 注入上限（条数）。`0` = 不限；**只限制注入条数**，不限制读取/缓存规模。
    #: 命中按 term 长度降序（长词优先），防止短词噪声挤掉真正的别名。
    NL2SQL_VALUE_ALIASES_MAX_GROUPS: int = 10
    #: 值同义词进程缓存 TTL（秒）。`0` = 关闭缓存（每次现读库）。同义词写操作经
    #: 写代号让**本进程**缓存立即失效；TTL 只服务跨 worker / 无本进程写入的稳态流量。
    NL2SQL_SYNONYM_CACHE_TTL_SECONDS: int = 60
    #: evidence 改写（P1-2，2026-10-28）：`1` = 有 hints 时先调一次 LLM，把
    #: question + hints 改写成「输出列/过滤/公式/排序限制」四段再合并。`0`（默认）
    #: = 不改写，行为与今天逐字节相同；改写失败 fail-open 到原始 hints。
    NL2SQL_EVIDENCE_REWRITE: bool = False
    #: planner（P2-1，2026-10-28）：`1` = 生成 SQL 前先调一次 LLM 产出计划
    #: （join 路径/隐藏公式/投影/单行多行/DISTINCT），随 payload 的 `plan` 键注入。
    #: `0`（默认）= 不调用，payload/提示词与今天逐字节相同。
    NL2SQL_PLANNER: bool = False
    #: 选择性 planner（P2-1b，2026-10-29）：`1` = 只在本次 join 图非空（多表题）时才规划；
    #: 单表题不调用，避免全局 planner 的 p50 翻倍。需要 `NL2SQL_PLANNER=1` 才生效。
    NL2SQL_PLANNER_SELECTIVE: bool = False
    #: 排名列提示（2026-10-29）：`1` = 问题里出现 "rank" 时，提示词追加规则 4l，
    #: 要求输出 `RANK() OVER (...)` 列且只投影问题点名的列 + 该排名列。
    #: `0`（默认）= 提示词与今天逐字节相同。
    NL2SQL_RANK_HINT: bool = False
    #: 平均每 Y 有多少 X 的规则（2026-10-29）：`1` = 问题出现 "average number of..." /
    #: "on average how many..." 时追加规则 4m：先按 Y 分组 COUNT，再对计数求 AVG。
    #: `0`（默认）= 提示词与今天逐字节相同。
    NL2SQL_PER_GROUP_AVG_HINT: bool = False
    #: 输出列精确性（2026-10-29）：`1` = 追加规则 4n —— 问句点名字段就只投影那些列；
    #: 「list all X」没点名字段时只投影标识列。多选列在官方口径下直接算错。
    #: `0`（默认）= 提示词与今天逐字节相同。
    NL2SQL_PROJECTION_EXACT_FIELDS: bool = False
    #: 行粒度提示（2026-11-01）：列表/计数题先判实体粒度，列表投影 DISTINCT、
    #: 计数用 COUNT(DISTINCT key)。窄触发；`0`（默认）= 提示词与今天逐字节相同。
    NL2SQL_GRAIN_HINT: bool = False
    #: 否定/条件问句（Did ...? If yes / Whether / Is there）：保留反面答案，
    #: 用 LEFT JOIN + CASE。窄触发；`0`（默认）= 提示词逐字节相同。
    NL2SQL_LEFT_JOIN_HINT: bool = False
    #: 百分比单位接地（2026-11-01）：问句含 `%` 且最终 0 行时，把比较谓词里的
    #: 数字试一次 /100（或 *100），能出结果才接受。窄触发；默认关。
    NL2SQL_UNIT_GROUNDING: bool = False
    #: 维度标签 join（2026-11-01）：CTE 选中表里，事实→有 name/title 等标签列
    #: 的维度表，按声明关系注入 join 提示；问句不要标签时不计算。默认关。
    NL2SQL_DIMENSION_JOINS: bool = False
    NL2SQL_DIMENSION_JOIN_MAX: int = 8
    #: CHESS 式可执行断言（2026-11-01）：生成结果级断言并验证，失败重写一次。
    #: 默认关；定向 row/value 实验用。
    NL2SQL_UNIT_TESTER: bool = False
    NL2SQL_UNIT_TEST_TIMEOUT: float = 15.0
    #: hint 忠实度（2026-10-29）：`1` = 有 hints 时追加规则 4o —— hint 给的公式/值映射
    #: 必须原样使用，不得换成同名近似列；未要求时不加 ROUND。
    #: `0`（默认）= 提示词与今天逐字节相同。
    NL2SQL_HINT_EXACTNESS: bool = False
    #: 百分比分母规则（2026-10-29）：`1` = 问题含 "percentage of" / "proportion of"
    #: 时追加 4p —— 分母是实体总量（标量子查询），不是过滤后的子集；
    #: 只有 "among X / of those X" 才用子集。
    #: `0`（默认）= 提示词与今天逐字节相同。
    NL2SQL_PERCENTAGE_BASE_HINT: bool = False
    #: 选择性 CTE 提示（2026-10-30）：`1` = join 图 ≥2 条且非 sqlserver 时追加规则 4q，
    #: 鼓励用一层 `WITH` 算中间结果。`0`（默认）= 提示词逐字节不变。
    NL2SQL_CTE_HINT: bool = False
    #: 除法 NULLIF 规则（2026-10-30）：`1` = 提示词要求所有除法/百分比分母用
    #: `NULLIF(<expr>, 0)`，避免 PG 的 division by zero。`0`（默认）= 提示词不变。
    NL2SQL_NULLIF_HINT: bool = False
    #: 单列投影提示（2026-10-30）：`1` = 单属性/实体列表问句（which/what/list/name/who，
    #: 且没有 and/also 等多字段诉求）追加规则 4s，要求只投影一列。
    #: `0`（默认）= 提示词不变。
    NL2SQL_SINGLE_COLUMN_HINT: bool = False
    #: 日期 cast 提示（2026-10-30）：`1` = 问句出现明确日历日期（YYYY/MM/DD 或英文月名）时
    #: 追加规则 4t，要求时间戳列先转 date 再比较。`0`（默认）= 提示词不变。
    NL2SQL_DATE_CAST_HINT: bool = False
    #: 生成前格式预测（S1，2026-10-30）：`1` = 生成前调一次 LLM，只预测「应返回
    #: 哪些列」，作为 `answer_format` 进 payload 并渲染规则 4u。`0`（默认）= 不调用，
    #: 请求/提示词/payload 与引入前逐字节相同。
    NL2SQL_FORMAT_PREDICT: bool = False
    #: 格式预测单次超时（秒）；失败 fail-open 到空列表，不影响提问。
    NL2SQL_FORMAT_TIMEOUT: float = 10.0
    #: 语义值映射（S2，2026-10-30）：`1` = 生成前把问题里的叫法映射到列样例里
    #: **真实存在的值**，以 hints 注入；未知值经列字典校验后丢弃。`0`（默认）=
    #: 不调用、不追加 hint，请求与引入前逐字节相同。
    NL2SQL_VALUE_MAPPING: bool = False
    #: 值映射单次超时（秒）；失败 fail-open 到空映射。
    NL2SQL_VALUE_MAPPING_TIMEOUT: float = 8.0
    #: 探查→摘要（S4，2026-10-30）：`1` = 把列样例摘要成枚举/单位/同义词，进 hints。
    #: `0`（默认）= 不调用、不追加 hint，请求与引入前逐字节相同。
    NL2SQL_PROBE_SUMMARY: bool = False
    #: 探查摘要单次超时（秒）；失败 fail-open 到空摘要。
    NL2SQL_PROBE_SUMMARY_TIMEOUT: float = 8.0
    #: 候选 + 成对选择（S3，2026-10-30）：`0`/`1` = 关（逐字节不变）；`2` = 对难例
    #: （有 join 或问句 ≥14 词）多生成一条单次档候选，用一次 LLM 选择后再走现有
    #: guard/治理/执行。选择失败回退 baseline。
    NL2SQL_CANDIDATES: int = 0
    #: 候选窄触发（S3 复测，2026-10-30）：`true` = 只对「问句 ≥22 词」或
    #: 「有 join 且 ≥14 词且带最高级/比较线索」的难例多生成候选；`false`（默认）=
    #: 旧宽触发（有 join 或 ≥14 词），与引入前逐字节相同。需 `NL2SQL_CANDIDATES>=2`。
    NL2SQL_CANDIDATE_NARROW: bool = False
    #: 无候选回退锚点（2026-11-01）：`1` = 词面匹配不到任何已对齐实体、且调用方
    #: 指定了数据源时，按编译 schema 选一个确定性锚点继续生成（preview 档下有效）。
    #: `0`（默认）= 维持 no_candidate 拒答，行为与引入前逐字节相同。
    NL2SQL_NO_CANDIDATE_FALLBACK: bool = False
    #: 执行结果投票（2026-11-01，OpenSearch-SQL `vote.py` 的窄移植）：`1` = 难例生成
    #: `NL2SQL_VOTE_CANDIDATES` 条候选，各自执行后按结果集合投票选一条；空/失败弃权。
    #: `0`（默认）= 不生成额外候选，行为与引入前逐字节相同。
    NL2SQL_RESULT_VOTE: bool = False
    #: 投票候选数（含基线）。仅在 `NL2SQL_RESULT_VOTE=1` 时生效。
    NL2SQL_VOTE_CANDIDATES: int = 3
    #: 候选选择单次超时（秒）。
    NL2SQL_CANDIDATE_TIMEOUT: float = 15.0
    #: 确定性歧义预检（S7，2026-10-30）：`1` = 报告「问句词命中多个列」与
    #: 「带引号的值不在样例里」两类 hint。`0`（默认）= 不追加 hint，请求逐字节不变。
    NL2SQL_AMBIGUITY_PRECHECK: bool = False
    #: CTE 选中表的结构化 join 边（2026-10-31）：`1` = 把声明关系/同名列补边
    #: 作为 `cte_joins` 注入 payload；`0`（默认）= payload 与今天逐字节相同。
    NL2SQL_CTE_JOIN_EDGES: bool = False
    #: CTE 大 schema join 提示（2026-10-31）：`1` = 仅当 `CTE_VIEW` 生效时追加规则 4x，
    #: 要求跨表过滤/字段必须 join 而不是丢掉；`0`（默认）= 提示词逐字节不变。
    NL2SQL_CTE_JOIN_HINT: bool = False
    #: 最高级问句单行（2026-10-30）：`1` = “Which X has the highest Y” 类问句追加
    #: 规则 4v，要求最终结果 LIMIT 1 / TOP 1。`0`（默认）= 提示词逐字节不变。
    NL2SQL_SUPERLATIVE_LIMIT: bool = False
    #: ORDER BY 显式 NULL 排序（2026-10-30）：`1` = 有排序意图的问句追加规则 4w，
    #: DESC 加 NULLS LAST、ASC 加 NULLS FIRST（SQL Server 用 CASE 模拟）。
    #: `0`（默认）= 提示词逐字节不变。
    NL2SQL_NULLS_ORDER: bool = False
    #: LLM 生成失败重试（2026-10-30）：`1` = 首次生成抛异常时同参重试一次（仅一次）。
    NL2SQL_LLM_RETRY: bool = False
    #: 同库 few-shot（2026-10-29）：`1` = 从 `NL2SQL_FEWSHOT_PATH` 里按当前表集合
    #: 选 ≤K 条同库问答注入提示词（只作列语义/SQL 风格参考）。
    #: `0`（默认）= 不读文件、提示词逐字节不变；生产默认无 BIRD 语料。
    NL2SQL_FEWSHOT: bool = False
    #: few-shot 语料路径（`{db: [{question, sql, tables}]}` JSON）。空 = 不启用。
    NL2SQL_FEWSHOT_PATH: str = ""
    #: 生产语料入口（2026-10-30）：`1` = 路径为空时，从演化台语料
    #: `evolution_corpus_cases`（同租户/KB/数据源、`expect=answer` 且带 control SQL）
    #: 读同库问答，按表重叠选。结果按 `NL2SQL_FEWSHOT_CACHE_TTL` 缓存。
    NL2SQL_FEWSHOT_FROM_CORPUS: bool = False
    #: 演化语料的进程内缓存秒数（0 = 不缓存）。
    NL2SQL_FEWSHOT_CACHE_TTL: int = 300
    #: 最多注入几条。
    NL2SQL_FEWSHOT_K: int = 2
    #: 小 schema 预览（P2-2，2026-10-28）：`1` = 对 ≤15 表的源，给「其他表 + 列名」
    #: 紧凑预览（payload `schema_preview`），让模型知道可 join 的表；预览表并入安全闸白名单。
    #: `0`（默认）= 不注入，payload/提示词与今天逐字节相同。
    NL2SQL_SCHEMA_PREVIEW: bool = False
    #: 大 schema 分块选择（P2-3 / P2-4c，2026-10-31）：`1` = 当该源表数 > 15 或估算
    #: token 超 `NL2SQL_SCHEMA_CONTEXT_BUDGET` 时，不一次性注入全量 schema，而是按
    #: 「题面 + 锚点/1 跳 + join 连通闭包」选表、按预算截断（payload 仍走 `schema_preview`
    #: 形状，选出表并入安全闸白名单）。**与 `NL2SQL_SCHEMA_PREVIEW` 互斥**：小 schema
    #: 仍由预览负责，BIRD 50 题不激活。`0`（默认）= 不调用，payload 与今天逐字节相同。
    NL2SQL_CTE_VIEW: bool = False
    #: 大 schema 上下文预算（估算 token；按 4 字符/token 折算，不引入 tokenizer 依赖）。
    NL2SQL_SCHEMA_CONTEXT_BUDGET: int = 12000
    #: 选择器最多保留的表数（含连通闭包补进来的桥表）。
    NL2SQL_SCHEMA_MAX_TABLES: int = 32
    #: 大 schema 选表时是否把**血缘边**作为弱相关信号（B2，2026-10-31）：同一条数据流
    #: 上的表优先于完全无关的表，但排序低于 join 连通（血缘不保证可 join）。`1` = 开；
    #: `0`（默认）= 不读血缘、选择器行为与今天逐字节相同。只在 CTE 激活时生效。
    NL2SQL_CTE_LINEAGE: bool = False
    #: 题面点名的实体表提升为 CTE 选择种子（2026-11-01）：修复 works_cycles 三条
    #: 多跳题 vendor/department/person 被预算挤掉。默认关。
    NL2SQL_CTE_ENTITY_SEEDS: bool = False
    #: 预览列示例值（P2-4a，2026-10-29）：**依赖 `NL2SQL_SCHEMA_PREVIEW`**。`1` = 预览表
    #: 每列再带 ≤3 个示例值（`column_examples`），数据只取编译行里**已存证据**，不查源库。
    #: `0`（默认）= 预览形状与今天逐字节相同。
    NL2SQL_PREVIEW_EXAMPLES: bool = False
    #: 预览示例值的总字符预算（按表序→列序确定性截断）。
    NL2SQL_PREVIEW_EXAMPLE_BUDGET: int = 3000
    #: 预览每列最多几个示例值。
    NL2SQL_PREVIEW_EXAMPLE_LIMIT: int = 3
    #: 预览列明细（2026-10-30）：`1` = 预览表的列从「列名」升级为
    #: `{name, type, description}`（描述截 80 字符、总预算 4000），帮模型选对同义列。
    #: `0`（默认）= 预览形状与今天逐字节相同。
    NL2SQL_PREVIEW_DETAILS: bool = False
    #: 小 schema 采样行（P2-4b，2026-10-29）：**依赖 `NL2SQL_SCHEMA_PREVIEW`**。`1` = 对
    #: 预览表各取 ≤3 行真实数据（只 SELECT 指定列、3s 超时、fail-open）挂到
    #: `schema_preview[].sample_rows`。生产含敏感数据，生产启用前必须过治理/PII 评审；
    #: `0`（默认）= 不查源库，payload 与今天逐字节相同。
    NL2SQL_SAMPLE_ROWS: bool = False
    #: 每表采样几行。
    NL2SQL_SAMPLE_ROW_LIMIT: int = 3
    #: 最多给几张预览表采样（按预览顺序）。
    NL2SQL_SAMPLE_ROW_MAX_TABLES: int = 10
    #: 采样行总字符预算（按表序→行序→列序确定性截断）。
    NL2SQL_SAMPLE_ROW_BUDGET: int = 2000
    #: 单表采样查询超时（秒）；失败 fail-open，不影响提问。
    NL2SQL_SAMPLE_ROW_TIMEOUT: float = 3.0

    #: 执行工具（2026-10-28，P0-A3）：`1` = 多轮档注册 `execute_sql`（只读、有界、
    #: guard + 治理），让模型在出终答前核对行数/值格式/join。`0`（默认）= 工具清单
    #: 与请求**逐字节不变**；没有执行器时即使打开也不注册。
    NL2SQL_EXEC_TOOL: bool = False
    #: 每题最多执行几次 `execute_sql`；超限回错误文本让模型收口（**不是**抛异常）。
    NL2SQL_EXEC_TOOL_MAX_CALLS: int = 2
    #: 探索执行返回给模型的行数上限（安全闸 LIMIT 与结果截断同一口径）。
    NL2SQL_EXEC_TOOL_MAX_ROWS: int = 20
    #: 单次探索执行的外层超时（秒）；卡住的是这一层，与用户看的 120s 无关。
    NL2SQL_EXEC_TOOL_TIMEOUT: float = 10.0

    #: ② 读路径的 schema 行缓存 TTL（秒；P1 版本化读取）。**默认 60**，`0` = 关闭缓存。
    #: 键 = `(tenant, kb, scope, ds, active version_id, schema_version, schema_generation)`。
    #: **写入立刻可见**靠的是 `schema_generation`：两个 schema 仓储的 upsert 成功后 +1
    #: （画布保存 / 回滚写回 / 采纳 PUT 都算），下一次读取直接换键 —— 所以演化基准的
    #: 「父臂填缓存 → 写库 → 候选臂」不会读到写前的行，TTL 不是失效机制。
    #: TTL 只服务「没有本进程写入」的稳态流量，以及**别的 worker** 的写入
    #: （跨进程失效最多滞后一个 TTL）。无 active 版本的旧库键里 `version_id=""`。
    #: ⚠️ 只影响「多久读一次库」，**不影响回答内容**（行内容始终是回答来源）；
    #: 漂移只进观测 scene（`schema_drift` / `schema_drift_scopes`），不进 HTTP 响应。
    NL2SQL_SCHEMA_CACHE_TTL_SECONDS: int = 60

    # 治理 P1（#11）：② 的标签/策略执行开关。
    # 默认开；关闭 / 无标签 / 无策略时 ② 行为与今天**逐字节相同**（回滚 = 设 0）。
    GOVERNANCE_ENFORCEMENT_ENABLED: bool = True
class IdentityIntegrationSettings(BaseSettings):
    """企业身份集成「自动同步」调度器的全部配置。

    ⚠️ 本模块所有 `IDENTITY_SYNC_*` 配置**只在这个类里**，不散到别处。
    ⚠️ **不设 ENABLED 开关**（对齐 task_center 的后台循环恒开）：自动同步是否启用
    是**逐租户**的产品配置（`identity_integration_config.auto_sync_enabled`），
    一个全局开关会让「某个租户打开了但没跑」变得无从解释。要全局禁掉就把
    `IDENTITY_SYNC_SCHEDULER_BATCH` 设 0（每轮扫 0 条）。
    """

    # 后台循环心跳（秒）：一轮扫描的间隔。**启动时立刻跑第一轮**，不等这个间隔。
    IDENTITY_SYNC_SCHEDULER_INTERVAL: float = 60.0
    # 一轮最多处理几个到期租户。⚠️ 这是**每轮派出去几个**的配额，不是「扫几条候选」：
    # 没抢到（被别的副本抢走 / 与手动同步互斥）的不占配额，下一轮还会被扫到。
    IDENTITY_SYNC_SCHEDULER_BATCH: int = 50
    # 租户没有自己的 `sync_interval` 时的兜底同步周期（小时）。
    # ⚠️ **必须与前端 `DEFAULT_INTERVAL_HOURS` 同值**
    # （frontends/platform-management/src/pages/TenantDetail/SyncConfigDrawer.tsx 的
    # `const DEFAULT_INTERVAL_HOURS = 24`）：两处不一致时，用户在界面上看到的
    #「默认 24 小时」与实际调度周期会对不上，而且前端只在「没填过」时提示该值，
    # 后端的兜底才是真正决定跑多久的那个。
    # 调度侧另有**硬下限** `max(interval_hours, 1)`（见
    # services/identity_sync_scheduler.py::MIN_INTERVAL_HOURS），本值调小于 1 无意义。
    IDENTITY_SYNC_DEFAULT_INTERVAL_HOURS: int = 24

    class Config:
        env_file = ".env"


# ==================== 血缘 / ETL 来源（P1） ====================
class LineageSettings(BaseSettings):
    """血缘 / ETL 来源（P1）。所有 ``LINEAGE_*`` 配置集中在这里。

    ⚠️ 与 `NL2SqlSettings` 同款：本类只放字段，`Settings` 继承后即可用。
    """
    #: 本体问答的「数据来源/下游」解释块开关（A 用法）。关掉时响应与旧版一致。
    LINEAGE_ANSWER_ENABLED: bool = True
    #: 解释块最多跟随几跳（1 = 只看直接上下游；默认 1，控制响应体量）。
    LINEAGE_ANSWER_MAX_HOPS: int = 1
    #: 解释块最多展示多少个命中实体（按命中顺序取前 N）。
    LINEAGE_ANSWER_MAX_ITEMS: int = 5
    #: 批量导入单次最多处理的行数（防一份超大 CSV 打满请求）。
    LINEAGE_IMPORT_MAX_ROWS: int = 5000
    #: 结构化数据入本体投影开关（P1.5a）。关掉后所有自动触发只记日志、不写图；
    #: 手工重建端点也返回 disabled（便于回滚）。
    ONTOLOGY_DATA_PROJECTION_ENABLED: bool = True
    #: 血缘失效窗口（roadmap P2 #9，天）：`origin=platform` 的边若超过该天数
    #: 未被再次确认（导入/OpenLineage 摄入刷新 `last_seen_at`），列表/画布读时
    #: 标 `stale=true`。只标注、不删除、不写回（与抽象 schema 的 stale 同口径）。
    #: `0` = 关闭失效标记。
    LINEAGE_STALE_AFTER_DAYS: int = 30
class ImageQuerySettings(BaseSettings):
    """以图问知识（图片转查询）的全部配置。

    设计：docs/knowledge-base/image-query-search-execution-plan.md §4-B8 / §2-D5

    ⚠️ 本模块所有 `IMAGE_QUERY_*` 配置**只在这个类里**，不散到别处。
    网关（`api_gateway`）与知识库能力服务都从 `jonex_core` 取，所以默认值只有这一份
    —— 两边各写一套默认正是「前端按 4 张判、后端按别的数拒」的来源。

    ⚠️ **`IMAGE_QUERY_VLM_TIMEOUT` 与 `IMAGE_QUERY_GATEWAY_TIMEOUT` 的关系是死约束**，
    改之前先读那两条的注释。
    """

    # ── VLM 调用（§4-B1）──
    # 网关地址 / api key / 模型名：留空则逐级回落
    # （`ONTOLOGY_LLM_BINDING_HOST` → 容器内默认；模型名 → `VLM_MODEL_NAME`），
    # 解析逻辑在 `jonex_core/common/vision_llm.py`，这里只做透传。
    IMAGE_QUERY_VLM_BINDING_HOST: str = ""
    IMAGE_QUERY_VLM_BINDING_API_KEY: str = ""
    IMAGE_QUERY_VLM_MODEL: str = ""
    # 单张识别的超时（秒）。**交互场景**：超时报错，由前端提示「可改用文字检索」，
    # 不重试、不静默降级成纯文字检索（D2）。
    #
    # 取 40s 的依据（B0 实测 2026-10-09）：关思考下单图 3.7~5.3s（素材 498KB~1790KB），
    # 即约 7.5 倍余量 —— 40 不是「够用」，是「上游抖动时的容忍上限」。
    # ⚠️ 改大它必须同步 `IMAGE_QUERY_GATEWAY_TIMEOUT`，且要守住两条：
    #   ① ≤ 部署的 `SIDECAR_PROXY_TIMEOUT`（docker-compose 默认 300，代码默认 180）；
    #   ② 本地联调要 **< 60s** —— `frontends/dev-gateway/index.js` 的
    #      `proxyTimeout` / `timeout` 写死 60000，超了会被它先掐成 502（F5）。
    IMAGE_QUERY_VLM_TIMEOUT: float = 40.0
    # 一次请求内并行识别的张数。B0 实测 4 张并行无 429 / 限流迹象，墙钟 6.03s。
    # ⚠️ 调低会让总耗时按 `ceil(MAX_IMAGES / CONCURRENCY)` 翻倍，
    # 进而顶穿下面那个 55s 的网关超时（本地还会顶穿 dev-gateway 的 60s）。
    IMAGE_QUERY_VLM_CONCURRENCY: int = 4

    # ── 限制（§2-D5）── 前端内置同样的默认值，但**识别接口回包里的 `limits` 为准**
    IMAGE_QUERY_MAX_BYTES: int = 5 * 1024 * 1024      # 单张 5MB（压缩后的那份）
    IMAGE_QUERY_MAX_IMAGES: int = 4                    # 一次检索最多几张
    IMAGE_QUERY_MAX_TOTAL_BYTES: int = 12 * 1024 * 1024  # 总量 12MB（限制 invoke 报文与 Redis 暂存体积）
    # 拼接后的查询文本截断上限。检索 DTO 的上限是 2000，这里远小于它是有意的：
    # 拼进来的主体是「实体 + 提问」，B0 实测 4 图描述拼样仅 177 字。
    # 截断优先级是「实体完整保留 → 提问 → 描述」（实体打头才不会让本体 exact/prefix
    # 匹配被长文本挤掉，F11）。
    IMAGE_QUERY_MAX_QUERY_CHARS: int = 500

    # ── 时效 ──
    IMAGE_QUERY_CACHE_TTL: int = 86400       # 识别文本缓存（按图 sha256，租户级）
    IMAGE_QUERY_PENDING_TTL: int = 1800      # 识别后等待保存历史的暂存（30 分钟；过期即丢弃）
    IMAGE_QUERY_VIEW_TOKEN_TTL: int = 900    # 图片查看票据（与 COS 预签名默认一致）

    # ── 网关侧 ──
    # 网关调识别 action 的单路由超时 = `VLM_TIMEOUT × ceil(MAX_IMAGES / CONCURRENCY) + 15`。
    # 按默认值 = 40 × 1 + 15 = 55s。那 15s 是本地解 base64、校验、组报文与回程的余量。
    # ⚠️ 必须 ≤ 部署的 `SIDECAR_PROXY_TIMEOUT`，且本地 < dev-gateway 的 60s（F5）。
    IMAGE_QUERY_GATEWAY_TIMEOUT: float = 55.0

    # ── 持久化与兜底清理 ──
    # 关掉它 = 不暂存、不返回 image_id，退化为「识别文本可用、历史不带图」。
    # 这是本需求的**回滚开关**（§8）：不需要动检索链路。
    IMAGE_QUERY_PERSIST_ENABLED: bool = True
    IMAGE_QUERY_SWEEP_INTERVAL: int = 3600        # 兜底扫描间隔（秒）
    # 兜底扫描只收「最后一次被引用」早于该时长的无引用图片。
    # 它只为避开「刚落盘、历史事务还没提交」的窗口；正常路径下图片与历史前后脚提交。
    IMAGE_QUERY_ORPHAN_GRACE_MINUTES: int = 60

    class Config:
        env_file = ".env"


# ==================== 抽象 schema「自动构造」 ====================
class AbstractConstructSettings(BaseSettings):
    """抽象 schema「自动构造」（补齐式构造）的全部配置。

    ⚠️ 本模块所有 `ABSTRACT_CONSTRUCT_*` 配置**只在这个类里**，不散到别处。
    默认值必须让功能开箱可用（本波上线不依赖任何部署动作）。
    """

    #: 属性中文名档总开关（默认开；关 = 快档：只跑确定性骨架，中文名留人工）。
    ABSTRACT_CONSTRUCT_NAMES_ENABLED: bool = True
    #: 名字档总耗时预算（秒）。到点停手、剩余如实返回（再点一次续跑）。
    #: ⚠️ 名字档**真上限 ≈ 本值 + `ABSTRACT_CONSTRUCT_NAMES_CALL_TIMEOUT_S`**（默认 100+60=**160s**，不是 100s）：
    #: 预算只挡「发新批」，已发出的批会跑满单批超时；确定性阶段（补边探针等）另算，两项相加才是端点总耗时。
    #: ⚠️ 端点**最坏合计 ≈ 探针 30s + 名字档 160s + 确定性 ≈ 200s**，略超网关/边车 180s —— 这条
    #: **尾部风险已接受**：超时只会让用户侧看到 timeout，后台照写 proposal；重试会遇 429 守卫
    #: （在飞期间不会重复构造）。实测全量 56.6s，正常路径远在 180s 内。
    ABSTRACT_CONSTRUCT_NAMES_BUDGET_S: float = 100.0
    #: 名字档并发批次数上限。
    ABSTRACT_CONSTRUCT_NAMES_CONCURRENCY: int = 5
    #: 单批名字调用超时（秒）。
    ABSTRACT_CONSTRUCT_NAMES_CALL_TIMEOUT_S: float = 60.0
    #: 跨源补边定向探测的时间预算（秒）与探针次数上限（够不到就记「不可测」，不硬猜）。
    #: ⚠️ 探针阶段真上限 ≈ **本值 + ε**：每发探测的 timeout 被钳到「剩余预算」（下钳 1s），
    #: 不再按 `probe_distinct` 默认的 60s 跑满 —— 只挡「起新探」时阶段能超到 30+60s。
    ABSTRACT_CONSTRUCT_PROBE_BUDGET_S: float = 30.0
    ABSTRACT_CONSTRUCT_MAX_PROBE_QUERIES: int = 40
class DataGovernanceSettings(BaseSettings):
    """数据治理保留策略的全局默认值（第 3 期）。

    ⚠️ 本模块的 `RETENTION_*` 配置只在这里，不散到别处。
    解析链路：`data_governance.retention_policy` 三级作用域 → 本类默认值。
    """

    # 系统默认保留天数。**0 = 不自动到期**（不是「立即删除」）。
    RETENTION_DEFAULT_DAYS: int = 0
    # 系统默认宽限天数：到期后进入待删除宽限期，期间仍可恢复。
    RETENTION_GRACE_DAYS: int = 30
    # 保留扫描调度器的轮询间隔（秒）。**<= 0 = 显式关闭调度**（不启动后台任务）。
    RETENTION_SCHEDULER_INTERVAL_SECONDS: int = 3600
    # 每轮归档候选与销毁候选**各自**最多处理多少条（两个独立批次，不是合计）。
    # 同租户每轮另有 `max(1, batch // 10)` 的公平性上限（防止大租户积压饿死其它租户）。
    RETENTION_SCAN_BATCH: int = 200
    # 同一文档归档/销毁失败后的进程内冷却秒数：冷却期内不再尝试。
    # ⚠️ 仅进程内状态、不落库，见 `services/retention_scheduler.py` 的「单副本假设」。
    RETENTION_FAILURE_COOLDOWN_SECONDS: int = 3600

    # 文档版本链保留最近多少版（含当前生效版）。**0 = 不自动清理**。
    # 超出部分上传新版本时同步裁剪，旧版本对象存储 best-effort 删除。
    DOCUMENT_VERSION_RETENTION: int = 20

    # 上传新版本时的「疑似同一文件」提示阈值（0~1，0=关闭该检测）。
    # 文本类按字符序列相似度、二进制按字节 shingle Jaccard 近似；
    # 命中只**询问**，不阻断（用户确认后照常建新版本）。
    DOCUMENT_VERSION_SIMILARITY_THRESHOLD: float = 0.85
    # 相似度计算最多读取的字节数（防大文件把请求拖慢）。
    DOCUMENT_VERSION_SIMILARITY_MAX_BYTES: int = 262144

    # ── 解析后相似检测 ──
    DOCUMENT_SIMILARITY_THRESHOLD: float = 0.85
    DOCUMENT_SIMILARITY_CANDIDATE_THRESHOLD: float = 0.80
    DOCUMENT_SIMILARITY_TOP_K: int = 3
    DOCUMENT_SIMILARITY_TEXT_CAP: int = 2_000_000
    DOCUMENT_SIMILARITY_SIGNATURE_SHINGLES: int = 200_000
    DOCUMENT_SIMILARITY_BACKFILL_BATCH: int = 20
    DOCUMENT_SIMILARITY_BACKFILL_INTERVAL_SECONDS: int = 600
    # unavailable 哨兵（无可用文本）的重试 TTL（分钟）：哨兵超过该时长后，
    # 回填会把它重新挑出来再算一次 —— 避免一次性故障被永久固化。
    # <=0 = 关闭重试（哨兵永久保留，仅重新解析可恢复）。
    DOCUMENT_SIMILARITY_RETRY_MINUTES: int = 60

    class Config:
        env_file = ".env"


# ==================== 主配置类 ====================
class Settings(
    DatabaseSettings,
    RedisSettings,
    SecuritySettings,
    KeycloakSettings,
    LoggingSettings,
    TCADPSettings,
    MilvusSettings,
    CorsSettings,
    AppSettings,
    LLMGatewaySettings,
    TaskCenterSettings,
    NL2SqlSettings,          # ← 新增（② NL→SQL 分源查询）
    LineageSettings,         # ← 新增（血缘 / ETL 来源）
    AbstractConstructSettings,  # ← 新增（抽象 schema「自动构造」）
    IdentityIntegrationSettings,
    ImageQuerySettings,
    DataGovernanceSettings,
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
