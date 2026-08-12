#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
安全模块

包含服务间内部认证、权限校验等安全相关功能
"""

from jonex_core.security.internal_auth import (
    InternalAuth,
    get_internal_auth,
    verify_internal_service,
)
from jonex_core.security.user_auth import (
    require_admin,
    require_role,
)

__all__ = [
    "InternalAuth",
    "get_internal_auth",
    "verify_internal_service",
    "require_admin",
    "require_role",
]
