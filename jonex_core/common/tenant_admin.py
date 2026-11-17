"""租户管理员判定 —— 内核唯一实现（2026-11-17 上移）。

原先这个判定住在
`capabilities/knowledge_base/services/space_permission_service.py`，被 KB 空间权限、
数据治理（`data_governance`）、平台管理多处复用。上移到内核的三个理由：

1. 它是**平台级 RBAC 判定**（只查 `tenant:admin` / `platform:admin` 两个标识码），
   不依赖任何能力领域的数据模型，不属于 knowledge_base 的职责；
2. 跨能力复用它会顺带拖入 knowledge_base 的整条 import 链（`services/__init__`
   → `knowledge_base_service` → `tbox/editor_service` → `reasoner.guards`）。
   `deploy/docker/data-governance.Dockerfile` 不带 `reasoner`，真机就是因此起不来
   （2026-11-17：`ModuleNotFoundError: No module named 'reasoner'` 循环重启）；
3. 内核镜像边界（`deploy/docker/*.Dockerfile`）保证每个能力都带 `jonex_core/`。

判定口径与上移前**完全一致**，改这里等于改全部空间 / KB / 数据治理的管理员豁免口径：

- `platform:admin`：全平台放行（上位兜底，见下）；
- `tenant:admin`：本租户全业务放行；
- 两者都是**纯标识码**，不参与任何具体资源的准入 —— 具体准入走资源身份
  （空间角色 `space_manager` / `member`、KB 权限、数据治理对象授权）。
- 历史上 `tenant:write` / `user:write` 曾使这里为真，B1 之后**不再**（方案
  `docs/permissions/PERMISSIONS_REDESIGN.md` §3.2-A / §11.3）。
"""
from __future__ import annotations

from jonex_core.security.permission import has_permission

#: 管理员标识码（唯一定义点；其它模块只做别名，不要再写字面量）。
TENANT_ADMIN_CODE = "tenant:admin"
PLATFORM_ADMIN_CODE = "platform:admin"


def coerce_user_id(user_id: str | None) -> int | None:
    """invoke 链路 user_id 是 str（'' / 'anonymous' 表示无用户）；权限码查询需要 int。"""
    if not user_id or user_id == "anonymous":
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


async def is_tenant_admin(tenant_id: str, user_id: str | None) -> bool:
    """租户管理员（平台 / 租户管理员）：持有 `tenant:admin` 或 `platform:admin`。

    判定链 ①（方案 §3.4）：`platform:admin` 全平台放行；`tenant:admin` 本租户全业务放行。
    两者都是**纯标识码**，不参与任何具体资源的准入 —— 具体准入走资源身份。

    空间权限的唯一特权判据：全空间可见 + 空间级管理 + 创建空间。
    `service:write` 与空间权限完全解耦（2026-08-19 收紧终版）。

    [jonex] B1 变更：判据从 `tenant:write` 换为 `tenant:admin`。
    `tenant:write` 回归字面语义（租户 CRUD），不再兼任管理员标识 —— 它与
    前端 auth_service 曾用的 `user:write` 都**不再**使本函数为真。
    改这里等于改了全部空间 / KB / 数据治理判定的租户管理员豁免口径，勿轻易加回旧码。
    """
    uid = coerce_user_id(user_id)
    if uid is None:
        return False
    if await has_permission(tenant_id, uid, TENANT_ADMIN_CODE):
        return True
    # 上位兜底：platform:admin 角色理应同时持有全部 tenant 码（含 tenant:admin），
    # 这里显式再判一次，避免角色矩阵播种漏配时平台管理员被挡在租户业务之外。
    return await has_permission(tenant_id, uid, PLATFORM_ADMIN_CODE)


__all__ = [
    "PLATFORM_ADMIN_CODE",
    "TENANT_ADMIN_CODE",
    "coerce_user_id",
    "is_tenant_admin",
]
