#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""调用者身份：从请求的 `Authorization` 头里解出「这次调用是谁发起的」。

**为什么非得在网关解 JWT**：能力调用走 Sidecar `/invoke`，那条路上认证用的是
`X-API-Key`（网关对 Sidecar 的内部凭证），**用户 JWT 根本到不了 Sidecar**。所以身份
只能由网关自己解出来，再放进 invoke 报文体透传给能力服务。Sidecar 收到后合并
（body 优先、认证兜底，见 `jonex_core/sidecar/main.py` 的 `/invoke` 处理），最终落成
能力侧的 `CapabilityRequest.user_id` / `.username`。

**身份只走报文体，不走请求头**：网关 → Sidecar → 能力服务这三跳，`user_id` 一直是
JSON body 里的字段。下游能力服务的 HTTP 头只有 `X-Request-ID` / `X-Tenant-ID` /
`Authorization`（内部服务 token）/ `X-Lang`——别指望从头里读身份。

三个函数原先在 `routes/knowledge_base.py` 和 `routes/business_domain.py` 里**各抄了
一份**（两份 `_extract_user_id` 还差一个 `or None`）；任务中心要用时成了第三个复制品。
收敛到这里，三处共用一份。

⚠️ **`has_permission` 那类判权的口径是「没有 user_id 就放行」**（见
`capabilities/business_domain/capability.py` 的 `_check_action_permission`）。所以
**漏传身份的失败方向是「悄悄放行」，不是「拒绝」**——加新的 invoke 路由时别忘了带上
这几个字段，漏了不会有任何报错，只会让权限判定静默失效。
"""

from typing import Any, Optional

import jwt
from fastapi import Request

from jonex_core.common.config import get_config


def _decode(request: Request) -> Optional[dict]:
    """解出 JWT payload；没有 Bearer 头 / 解不开 / 过期，一律回 `None`。

    **不抛异常**：这些函数是「尽力取身份」，取不到就是匿名（由调用方决定匿名怎么办）。
    把解析失败抛出去会让一个坏 token 变成 500，而正常路径已经由 `require_auth_header`
    在路由入口挡过一道了。
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    # 与 `require_auth_header` 保持同一口径：容忍 Bearer 与 token 之间的多余空白，
    # 否则认证门放行的 token 在这里解不出身份（密级守卫会因 user_id 为空而失效）。
    token = auth[7:].strip()
    config = get_config()
    try:
        return jwt.decode(
            token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM]
        )
    except jwt.PyJWTError:
        return None


def extract_user_id(request: Request) -> Optional[str]:
    """调用者用户 ID（JWT 的 `sub`）。"""
    payload = _decode(request)
    return None if payload is None else (payload.get("sub") or None)


def extract_username(request: Request) -> Optional[str]:
    """调用者用户名（JWT 的 `username`）。"""
    payload = _decode(request)
    return None if payload is None else (payload.get("username") or None)


def extract_impersonation(request: Request) -> tuple[bool, list[str], Optional[str]]:
    """模拟态上下文 `(impersonated, perms, original_tenant_id)`。

    管理员「模拟某用户」时，真实身份与权限集跟正常登录不同，得原样透传给能力侧
    （能力侧靠它做权限短路，见 `jonex_core/security/permission.py` 的
    `_impersonation_ctx`）。非模拟态回 `(False, [], None)`。
    """
    payload = _decode(request)
    if payload is None:
        return False, [], None
    return (
        bool(payload.get("impersonated")),
        payload.get("perms") or [],
        payload.get("original_tenant_id"),
    )


def identity_fields(request: Request) -> dict[str, Any]:
    """把身份收成一组**可以直接合进 `sidecar_payload`** 的字段（空值不出现）。

    这是给 invoke 路由用的推荐入口：拿到了直接 `sidecar_payload.update(...)` 就完事，
    省得每个路由自己写「非空才塞」的四行判断 —— 那种手写判断正是漏字段的来源。

    只管「用户身份」，不含 `tenant_id`（租户有自己的解析规则与优先级，见
    `jonex_core/common/tenant.py` 的 `extract_tenant_id`，不在本模块职责内）。
    """
    fields: dict[str, Any] = {}

    user_id = extract_user_id(request)
    if user_id:
        fields["user_id"] = user_id

    username = extract_username(request)
    if username:
        fields["username"] = username

    impersonated, perms, original_tenant_id = extract_impersonation(request)
    if impersonated:
        fields["impersonated"] = True
        fields["perms"] = perms
        if original_tenant_id:
            fields["original_tenant_id"] = original_tenant_id

    return fields


__all__ = [
    "extract_impersonation",
    "extract_user_id",
    "extract_username",
    "identity_fields",
]
