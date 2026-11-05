# -*- coding:utf-8 -*-
"""演化台短时用户 JWT 签发：试跑（replay）与基准臂（bench）共用一份口径。

同一张「演化身份」票：租户来自 `tenant_id`，`sub` 必须是可 `int()` 的数字
（`UserAuth.decode_token` 会校验），非数字身份（如测试 token 的 `u_1`）退回 0。
它用于 router / 网关的身份与租户解析；审计 / 观测行会以 `evolution` 这个身份
留痕（与真实用户区分，便于把合成流量单列），但不代表任何人的业务写权限。

用户名固定 `evolution`（观测/审计里可辨识）；角色固定 `admin`（只要 router 与
网关认这个身份，不做业务写）。TTL 由调用方给：点踩试跑 10 分钟（`replay`），
基准臂每次提问现签 30 分钟（完整语料跑几十分钟，不能只签一次）。
"""
from __future__ import annotations

import types

from jonex_core.security.user_auth import UserAuth

#: 演化身份的 username（不是真实用户）。
EVOLUTION_USERNAME = "evolution"
#: 演化身份的 role / roles。
EVOLUTION_ROLE = "admin"
#: 基准臂 token 的默认 TTL（分钟）。
DEFAULT_TOKEN_MINUTES = 30


def issue_evolution_token(tenant_id: str, user_id: str | int | None = None,
                          minutes: int = DEFAULT_TOKEN_MINUTES) -> str:
    """签发一张演化身份的用户 JWT（`type=user`，`roles=["admin"]`）。

    `user_id` 非数字（如 `u_1`）时 `sub` 退回 `"0"`：`decode_token` 的
    `_validate_claims` 要求 `sub` 可 `int()`，退回 0 不改变租户/身份语义。
    """
    try:
        uid = int(user_id) if user_id is not None else 0
    except (TypeError, ValueError):
        uid = 0
    stub = types.SimpleNamespace(
        id=uid, tenant_id=tenant_id,
        username=EVOLUTION_USERNAME, role=EVOLUTION_ROLE)
    return UserAuth().create_access_token(
        stub, roles=[EVOLUTION_ROLE], expires_minutes=minutes)


__all__ = [
    "DEFAULT_TOKEN_MINUTES",
    "EVOLUTION_ROLE",
    "EVOLUTION_USERNAME",
    "issue_evolution_token",
]
