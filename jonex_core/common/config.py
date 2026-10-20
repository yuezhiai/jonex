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
        "review,retry,cancel,approve,reject,bind,unbind,enable,disable,reset,upload"
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
    # 仅对命中 MODELS 且 scene 命中 SCENES 的 chat 请求注入 body.thinking={"type":"disabled"}
    # ⚠️ 仅适配腾讯 tokenhub（tencentmaas）：该字段为其专有关思考格式；换其他上游
    #    需改 upstream._maybe_disable_thinking 适配，否则应把 ENABLED 设为 False
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
    LLMGW_DISABLE_THINKING_SCENES: str = (
        "lightrag_extract,ontology_extract,raganything_ingest,"
        "ontology_arbitration,ontology_qa_fast,rag_chunk_qa_fast,rag_fusion_fast"
    )
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
