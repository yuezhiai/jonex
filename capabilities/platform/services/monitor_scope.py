"""平台监控读接口的「生效租户」解析（三族 18 条读接口共用）。

生效租户**只**来自认证上下文（用户 JWT / 模拟 token 的 ``tenant_id``），
不读 body / query / 自定义 Header。平台管理员跨租户通过既有 impersonation
换取目标租户 token 实现，本模块不做角色分支。

``unknown`` 的额外拦截依据未知租户的临时口径：``metering.llm_usage_log``
里 ``tenant_id='unknown'`` 的历史行在产品确认前不得归入任何租户；同时也不
允许以 ``unknown`` 作为生效租户去读监控数据。
"""

from typing import Optional

from jonex_core.common.exceptions import PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

#: 不能作为监控生效租户的额外取值（``require_tenant`` 已挡默认租户，这里补 ``unknown``）。
_ILLEGAL_EFFECTIVE_TENANTS = frozenset({"unknown"})


def require_monitor_tenant(tenant_id: Optional[str]) -> str:
    """返回规范化后的监控生效租户；非法值一律 403（3006）。

    认证上下文里的租户异常属于身份问题，不是业务参数错误 —— 用
    ``PermissionDeniedError`` 而不是 ``1001``，避免前端把它当成「用户填错参数」。
    """
    tenant = require_tenant(tenant_id)
    if tenant in _ILLEGAL_EFFECTIVE_TENANTS:
        raise PermissionDeniedError(
            message=translate(
                "err.monitor.tenant_unavailable",
                fallback="当前租户上下文不可用于监控查询",
            )
        )
    return tenant


__all__ = ["require_monitor_tenant"]
