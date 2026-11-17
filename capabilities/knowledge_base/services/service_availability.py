"""领域服务可用性判定 — 知识检索的「服务 / 空间」闸门。

职责边界：只做判定（查询 + 抛异常），不修改任何数据。

背景：
知识检索必须按「领域服务可用性」判定，不能只看空间角色 / KB 授权 ——

- 服务必须存在、属于当前租户、未软删、``status='active'`` 且
  ``COALESCE(enabled,1) <> 0``；否则拒绝（``3028``）。
- 空间下必须至少有 1 个启用中的服务；否则该空间不可检索（``3029``）。
- 停用口径取「或」：``status != 'active'`` **或** ``enabled == 0``。
  页面停用按钮写 ``status``、``/disable`` 端点写 ``enabled``，两条写入口都要覆盖。

为什么单列模块而不是塞进 ``space_permission_service``：空间角色是**授权**维度，
服务可用性是**资源可用性**维度 —— 管理员同样受限，且判定谓词完全不同（后者带
``status`` / ``enabled``）。两个维度不要合并，合并即回归。

``enabled`` 是 ``SmallInteger`` 且可为 NULL：谓词必须写 ``COALESCE(enabled,1) <> 0``，
不能写 ``enabled = 1``（NULL 会被误判为不可用）。
"""
from __future__ import annotations

from sqlalchemy import func, text

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import (
    ServiceNotSearchableError,
    SpaceNoEnabledServiceError,
)
from jonex_core.common.tenant import require_tenant

#: 单服务可用性谓词（与 `get_space_ids_with_enabled_service` 保持同口径）。
#: 三处闸门（能力层服务闸门、能力层空间闸门、MCP 本地闸门）都以此为准，
#: 改一处必须同步另一处。
_SERVICE_SEARCHABLE_WHERE = (
    "tenant_id=:t AND is_deleted=0 AND status='active' "
    "AND COALESCE(enabled,1)<>0"
)


def searchable_service_conditions(model) -> list:
    """ORM 版可用性谓词，供列表查询拼进 ``WHERE``（与上方 SQL 版同口径）。

    检索面候选过滤（``DomainServiceService.list(scope="search")``）用它把停用服务
    从下拉里剔除：``status='active'`` 且 ``COALESCE(enabled,1)<>0``。

    ⚠️ 与 ``_SERVICE_SEARCHABLE_WHERE`` **同口径**：改谓词必须两处同步，否则会出现
    「列表能看到、进去 3028」的不一致。``enabled`` 是 ``SmallInteger`` 且可空，
    只能写 ``COALESCE(enabled,1)<>0``，**不能**写 ``enabled == 1``（NULL 会被误判为停用）。
    """
    return [model.status == "active", func.coalesce(model.enabled, 1) != 0]


async def require_service_searchable(tenant_id: str, service_id: str) -> None:
    """服务不存在 / 跨租户 / 已软删 / 停用（status 或 enabled 任一不满足）→ ``3028``。

    只判可用性，不解析服务关联的 KB —— ``_resolve_service_id_to_kb_ids`` 见到
    ``knowledge_base_ids`` 就短路，服务状态在那里永远查不到，所以必须由本函数
    独立兜住（服务解析函数见到 ``knowledge_base_ids`` 会提前返回，那里永远查不到
    服务状态）。
    """
    tenant_id = require_tenant(tenant_id)
    if not service_id:
        raise ServiceNotSearchableError(details={"service_id": ""})

    async with get_db_session() as session:
        row = (await session.execute(
            text(
                "SELECT id FROM knowledge_base.services "
                f"WHERE id=:sid AND {_SERVICE_SEARCHABLE_WHERE}"
            ),
            {"sid": service_id, "t": tenant_id},
        )).first()

    if row is None:
        raise ServiceNotSearchableError(details={"service_id": service_id})


async def get_space_ids_with_enabled_service(
    tenant_id: str, space_ids: list[str] | None = None
) -> set[str]:
    """返回「至少有 1 个启用中服务」的空间 id 集合。

    ``space_ids`` 为 ``None`` 时查全租户；空列表直接返回空集合（不发 SQL）。
    一次 SQL 批量判定，供多 KB 检索剔除无启用服务空间下的 KB（零 N+1）。
    """
    tenant_id = require_tenant(tenant_id)

    params: dict = {"t": tenant_id}
    sql = (
        "SELECT DISTINCT space_id FROM knowledge_base.services "
        f"WHERE {_SERVICE_SEARCHABLE_WHERE}"
    )
    if space_ids is not None:
        normalized = [s for s in space_ids if s]
        if not normalized:
            return set()
        sql += " AND space_id = ANY(:space_ids)"
        params["space_ids"] = normalized

    async with get_db_session() as session:
        rows = (await session.execute(text(sql), params)).all()
    return {row[0] for row in rows if row[0]}


async def require_space_has_enabled_service(tenant_id: str, space_id: str) -> None:
    """空间下没有任何启用中的服务 → ``3029``。

    与空间「禁用」是两件事：空间本身可以是 active 的，只是其下服务全部停用 /
    尚未创建，此时检索入口不可用。
    """
    if not space_id:
        raise SpaceNoEnabledServiceError(details={"space_id": ""})

    enabled_space_ids = await get_space_ids_with_enabled_service(
        tenant_id, [space_id]
    )
    if space_id not in enabled_space_ids:
        raise SpaceNoEnabledServiceError(details={"space_id": space_id})
