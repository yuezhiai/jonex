#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""启动期安全守卫（fail-closed）。

与 ``api_gateway/main.py`` 的 ``INTERNAL_API_KEY`` 守卫同构：配置不安全时在启动
阶段抛 ``RuntimeError`` 拒绝启动，而不是等第一个请求才在运行期炸。

JWT_SECRET 泄漏的后果（2026-09-22 生产事件）：
- 伪造任意用户 JWT（HS256 对称签名）⇒ 可冒充管理员；
- 伪造 ingest key ⇒ ``decode_ingest_key`` 解析出**真实租户**，可向任意租户知识库
  注入数据（``generate_ingest_key`` 的 payload 是 base64 明文，secret 泄漏即可自造签名）。

> ⚠️ 2026-10-14 变更：**公开默认值判定已恢复启用**。
> 2026-09-22 曾为「解除未配置真实 JWT_SECRET 的环境无法启动」而注释停用该判定，
> 代价是**任何非空值都被放行** —— 2026-10-14 自检实测确认生产
> ``deploy/env/tencent/.env.tencent`` 的 ``JWT_SECRET`` 正是仓库内公开的占位串
> （长度 72，命中本模块黑名单），且 ``_classify_jwt_secret`` 返回 ``None`` 放行
> ⇒ **用公开值即可签发任意用户/管理员 JWT**（已端到端实证：伪造 token 调
> ``GET /api/v1/platform/users`` 返回 200 与真实数据）。
> 现恢复判定，并把仓库内**所有曾公开过的值**纳入黑名单。
> 本地开发不再依赖「公开值 + 逃生阀」：``make init`` 会为 ``deploy/.env``
> 生成真实随机值（见 Makefile），确需沿用不安全值时显式设 ``ALLOW_INSECURE_JWT_SECRET=1``。

> ⚠️ 2026-10-15 变更：**硬拦只在生产档生效**（``ENV=prod``），见下方约束 2。
> 10-14 那版是无条件校验，代价是**非生产**容器只要沿用仓库内公开的 JWT_SECRET 就
> 直接起不来（``api_gateway`` startup 抛 ``RuntimeError`` ⇒ 容器重启循环），
> 而这类值在 dev / test 画像里本来就是预期值（``deploy/.env.example`` 的 ``ENV=dev``）。
> 现在：生产**照旧拒绝启动**，其余环境**只告警后放行**。

三条设计约束（已踩坑，勿改）：

1. **判据必须覆盖仓库内所有曾公开的值。** pydantic 字段默认
   （``your_jwt_secret_key_here_please_change_in_production``，52 字符）、
   历史 ``deploy/.env.example`` 模板值（``..._please_use_a_strong_secret``，72 字符，
   **实测生产正在用**）、以及一度写进 ``.env.example`` 的
   ``Ftr6Cru3_...``。**三者互不相同**，只判其一必然漏掉另外两条路径。
2. **硬拦按 ``ENV`` 分档，且「未设置」按非生产处理。** 与 ``sidecar`` /
   ``api_gateway`` 里 ``GATEWAY_API_KEY`` / ``KEYCLOAK_CLIENT_SECRET`` 守卫的既有政策
   一致（那两处本来就是 ``if not config.is_production: return``）：``ENV=prod`` 硬拦，
   dev / test / uat / 未设置一律**只告警**。

   2026-10-15 用户口径：dev 与其它非生产环境**不该被启动守卫拦住** —— 无条件校验会让
   沿用了仓库内公开值的本地/测试容器起不来，表现成 ``api_gateway`` 启动即
   ``RuntimeError`` 的重启循环，而那个值在那些画像里本就不是问题。

   代价与兜底（**这是本约束唯一真正的风险，改动前先读**）：一个**忘了设 ``ENV=prod``
   的对外实例会失去这层硬拦**。三道兜底：
   · 非生产档**一律打 warning**（不是静默 ``return``），日志里直接点名
     「若这是对外实例，请设 ``ENV=prod``」——静默失效是最难查的形态；
   · ``scripts/security/audit_profile.py`` 第 2 项已把「画像未声明 ``ENV=prod``」判 🔴
     （那条的理由更硬：不设 ``ENV`` 会同时打开 ``jonex_test_<租户>`` 测试 token 后门）；
   · 对外画像 ``deploy/env/tencent/.env`` 与 ``deploy/env/91/.env`` 实测均已显式
     ``ENV=prod`` ⇒ 本次分档对它们**行为完全不变**（仍旧拒绝启动）。
3. **守卫的比对集合不能只放一个哨兵值。** ``GATEWAY_API_KEY`` /
   ``KEYCLOAK_CLIENT_SECRET`` 这类守卫原先只比对 ``change-me-in-production``，
   而 ``deploy/.env.example`` 实际发出去的是 ``jonex_test_gateway``、
   生产发出去的是 ``jonex-platform-public-pkce-client`` —— **都不等于哨兵，因此全部漏过**。
   故本模块统一维护「不安全值集合」，供各处守卫复用。
"""
from __future__ import annotations

import os

from jonex_core.common import get_config, get_logger

logger = get_logger("security_guards")

# 仓库内曾公开过的 JWT_SECRET 值。改动 .env.example / config.py 默认值时必须同步此处。
_INSECURE_JWT_SECRETS = frozenset(
    {
        # config.py SecuritySettings.JWT_SECRET 的 pydantic 字段默认
        "your_jwt_secret_key_here_please_change_in_production",
        # 历史 deploy/.env.example 模板值；2026-10-14 实测生产 .env.tencent 正在用此值
        "your_jwt_secret_key_here_change_in_production_please_use_a_strong_secret",
    }
)

# 仓库内曾公开过的内部/网关键。供 sidecar / api_gateway / platform 的守卫复用。
_INSECURE_GATEWAY_API_KEYS = frozenset(
    {
        "change-me-in-production",  # config.py 哨兵 + 历史模板
        "jonex_test_gateway",       # deploy/.env.example:385 模板值；2026-10-14 实测生产在用
    }
)

# 仓库内曾公开过的 Keycloak client secret。
_INSECURE_KEYCLOAK_CLIENT_SECRETS = frozenset(
    {
        "change-me-in-production",           # config.py 哨兵
        "jonex-platform-public-pkce-client", # 公开的 PKCE client id 被当作机密 secret（2026-10-14 实测生产在用）
    }
)

# 仓库内曾公开过的内部 API key（``/internal/*`` 端点认证用）。
_INSECURE_INTERNAL_API_KEYS = frozenset(
    {
        "change-me-in-production",                            # config.py 哨兵
    }
)

# 本地开发逃生阀：显式设置后跳过校验。故意不依赖 ENV 判定（见模块 docstring 约束 2）。
_ESCAPE_HATCH_ENV = "ALLOW_INSECURE_JWT_SECRET"
_ESCAPE_HATCH_TRUE = frozenset({"1", "true", "yes", "on"})

#: 判定为「生产」的 ENV 取值（``config.ENV`` / 环境变量 ENV，大小写不敏感）。
#: 只认这两个字面量：``config.is_production`` 用的是 ``ENV == "prod"``，这里把
#: ``production`` 一并收进来（同义写法，避免有人写了全称反而丢了硬拦）。
_PRODUCTION_ENVS = frozenset({"prod", "production"})


def environment_name(config=None) -> str:
    """当前环境名（原样返回，不做归一化）；读不到返回空串。

    优先 ``config.ENV``（pydantic 字段，会把 ``.env`` 里的 ``ENV`` 一并读进来），
    回落环境变量 ``ENV``。**两者都没有就是空串**，由调用方按非生产处理。
    """
    cfg = config if config is not None else get_config()
    value = getattr(cfg, "ENV", None) or os.getenv("ENV", "")
    return str(value).strip()


def is_production_environment(config=None) -> bool:
    """是否生产环境（``ENV=prod`` / ``production``）—— **启动守卫硬拦的唯一档**。

    「未设置」按非生产处理，理由与三道兜底见模块 docstring 约束 2。
    """
    return environment_name(config).lower() in _PRODUCTION_ENVS


def _enforce_or_warn(reason: str, *, config=None, subject: str) -> None:
    """生产 → 抛 ``RuntimeError`` 拒绝启动；非生产 → **告警后放行**。

    收口在一处是为了让所有守卫共用同一条政策：改判定只改这里，不会出现
    「JWT 按 ENV 分档、别的守卫还是无条件」这种两条路（那正是最难查的一类差异）。

    ⚠️ 非生产档**必须留痕**：静默放行会让「忘了设 ``ENV=prod`` 的对外实例」
    看起来一切正常，直到被人用公开默认值签出管理员 JWT。
    """
    if is_production_environment(config):
        raise RuntimeError(reason)
    logger.warning(
        "%s 启动守卫已跳过（ENV=%s，非生产档，只告警不拦）：%s"
        "⚠️ 若这是对外实例，请显式设置 ENV=prod —— 不设时这一层保护不会生效。",
        subject,
        environment_name(config) or "<未设置>",
        reason,
    )


def escape_hatch_enabled() -> bool:
    """本地开发逃生阀是否开启。"""
    return os.getenv(_ESCAPE_HATCH_ENV, "").strip().lower() in _ESCAPE_HATCH_TRUE


def _classify_jwt_secret(value: str | None) -> str | None:
    """返回不安全原因；安全则返回 None。"""
    text = (value or "").strip()
    if not text:
        return "未配置或为空"
    if text in _INSECURE_JWT_SECRETS:
        return "仍为仓库内公开的默认值"
    return None


def is_insecure_gateway_api_key(value: str | None) -> bool:
    """``GATEWAY_API_KEY`` 是否为仓库内公开/哨兵值（空值也算不安全）。"""
    return (value or "").strip() in _INSECURE_GATEWAY_API_KEYS or not (value or "").strip()


def is_insecure_keycloak_client_secret(value: str | None) -> bool:
    """``KEYCLOAK_CLIENT_SECRET`` 是否为仓库内公开/哨兵值（空值也算不安全）。"""
    return (value or "").strip() in _INSECURE_KEYCLOAK_CLIENT_SECRETS or not (value or "").strip()


def is_insecure_internal_api_key(value: str | None) -> bool:
    """``INTERNAL_API_KEY`` 是否为仓库内公开/哨兵值（空值也算不安全）。

    该 key 保护 ``/internal/*`` 端点，泄漏等于内部面完全敞开。
    """
    return (value or "").strip() in _INSECURE_INTERNAL_API_KEYS or not (value or "").strip()


def assert_secure_jwt_secret(config=None) -> None:
    """校验 JWT_SECRET 已配置为真实随机值，否则拒绝启动（fail-closed）。

    同时校验两个来源，避免写入侧与校验侧分叉：

    - ``config.JWT_SECRET``：JWT 签发/校验（``jwt.encode`` / ``jwt.decode``）与
      查看 token 用；
    - ``环境变量 JWT_SECRET``：``crypto._sign_payload`` 每次实时读取，用于 MCP Key /
      ingest key 的 HMAC 哈希。

    **硬拦只在 ``ENV=prod`` 生效**；dev / test / uat / 未设置只打一条 warning 后放行
    （2026-10-15 用户口径，代价与兜底见模块 docstring 约束 2）。

    Args:
        config: 可注入的配置对象（测试用）；默认取 ``get_config()``。

    Raises:
        RuntimeError: 生产档下 JWT_SECRET 缺失、为空，或仍为仓库内公开的默认值。
    """
    if escape_hatch_enabled():
        logger.warning(
            "%s 已启用，跳过 JWT_SECRET 安全校验——仅限本地开发，生产环境严禁设置。",
            _ESCAPE_HATCH_ENV,
        )
        return

    cfg = config if config is not None else get_config()
    sources = (
        ("config.JWT_SECRET", getattr(cfg, "JWT_SECRET", "")),
        ("环境变量 JWT_SECRET", os.getenv("JWT_SECRET", "")),
    )

    reasons = []
    for source_name, value in sources:
        reason = _classify_jwt_secret(value)
        if reason is not None:
            reasons.append(f"{source_name} {reason}")

    if reasons:
        _enforce_or_warn(
            "JWT_SECRET 不安全，拒绝启动：" + "；".join(reasons) + "。"
            "JWT_SECRET 泄漏可伪造任意用户 JWT（含管理员）及 ingest key"
            "（后者可向任意租户知识库注入数据）。"
            "请在 .env 中设置为真实随机值（可用 `python -c \"import secrets; "
            "print(secrets.token_urlsafe(48))\"` 生成）；"
            f"若确需在本地开发沿用默认值，显式设置 {_ESCAPE_HATCH_ENV}=1。",
            config=cfg,
            subject="JWT_SECRET",
        )


def assert_secure_gateway_api_key(config=None) -> None:
    """校验 ``GATEWAY_API_KEY`` 已从仓库公开值/哨兵改掉，否则拒绝启动（fail-closed）。

    背景：该 key 用于 Gateway→Sidecar 内部认证，且 ``_gateway_api_key_matches``
    的匹配**不带租户校验**（``X-Tenant-ID`` 客户端给谁就是谁）。若沿用公开默认值，
    任何能访问 Sidecar 的人都能以任意租户身份调用能力，造成租户隔离旁路。

    **硬拦只在 ``ENV=prod`` 生效**（与 ``sidecar`` / ``api_gateway`` 里原先那两处
    ``if not config.is_production: return`` 同政策；差别是非生产档会留下一条 warning）。
    """
    cfg = config if config is not None else get_config()
    if is_insecure_gateway_api_key(getattr(cfg, "GATEWAY_API_KEY", "")):
        _enforce_or_warn(
            "GATEWAY_API_KEY 未配置或仍为仓库内公开的默认值（含 change-me-in-production、"
            "jonex_test_gateway）。该 key 用于 Gateway→Sidecar 内部认证且不校验租户，"
            "沿用公开值将导致任意租户身份可被伪造。"
            "请在 .env 或环境变量中设为真实随机 key。",
            config=cfg,
            subject="GATEWAY_API_KEY",
        )


def assert_secure_internal_api_key(config=None) -> None:
    """校验 ``INTERNAL_API_KEY`` 已从仓库公开值/哨兵改掉，否则拒绝启动（fail-closed）。

    该 key 保护 API Gateway 的 ``/internal/*`` 端点，空值或公开值等于内部面完全敞开。

    **硬拦只在 ``ENV=prod`` 生效**（同上；原先这段判定内联在 ``api_gateway/main.py``
    的 startup 里且**无条件**，非生产画像会因此起不来）。
    """
    cfg = config if config is not None else get_config()
    if is_insecure_internal_api_key(getattr(cfg, "INTERNAL_API_KEY", "")):
        _enforce_or_warn(
            "INTERNAL_API_KEY 未配置或仍为仓库内公开的默认值"
            "（含 change-me-in-production 与历史 .env.example 模板值）。"
            "请在 .env 或环境变量中设置 INTERNAL_API_KEY 为真实随机 key。"
            "未配置将导致 /internal/* 端点无认证保护。",
            config=cfg,
            subject="INTERNAL_API_KEY",
        )


def assert_secure_keycloak_client_secret(config=None) -> None:
    """校验 ``KEYCLOAK_CLIENT_SECRET`` 已从仓库公开值/哨兵改掉（fail-closed）。

    SSO 未启用时不消费该密钥，直接返回，避免无关部署启动失败。
    **硬拦只在 ``ENV=prod`` 生效**（同上）。
    """
    cfg = config if config is not None else get_config()
    if not getattr(cfg, "sso_enabled", False):
        return
    if is_insecure_keycloak_client_secret(getattr(cfg, "KEYCLOAK_CLIENT_SECRET", "")):
        _enforce_or_warn(
            "KEYCLOAK_CLIENT_SECRET 未配置或仍为仓库内公开的默认值（含 "
            "change-me-in-production、jonex-platform-public-pkce-client）。"
            "该值用于 Keycloak confidential client 身份，泄漏可伪造 client 身份"
            "（Admin API / backchannel 调用）。请在 .env 中设为真实随机值。",
            config=cfg,
            subject="KEYCLOAK_CLIENT_SECRET",
        )


__all__ = [
    "assert_secure_gateway_api_key",
    "assert_secure_internal_api_key",
    "assert_secure_jwt_secret",
    "assert_secure_keycloak_client_secret",
    "environment_name",
    "escape_hatch_enabled",
    "is_insecure_gateway_api_key",
    "is_insecure_internal_api_key",
    "is_insecure_keycloak_client_secret",
    "is_production_environment",
]
