"""
知识库能力 — 领域空间服务
"""
from __future__ import annotations

import uuid

from sqlalchemy import and_, exists, or_, select, text

from jonex_core.common import get_db_session
from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.exceptions import InvalidParameterError, PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models.space import Space, SpacePermission
from ..repository import SpaceRepository
from .space_permission_service import (
    get_write_space_ids,
    is_tenant_admin,
    require_space_manage,
    require_space_visible,
)


class SpaceService:
    """领域空间 CRUD + 权限"""

    async def create(self, tenant_id: str, data: dict, owner_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        # owner 必须可确定（排除「无 user_id 放行」口径）且是租户管理员（tenant:write）——
        # 收紧终版（2026-08-19）：空间统一由平台/系统管理员创建，再授权给领域服务管理员管理。
        # 忽略 data["owner_id"]——客户端不能指定 owner（防把空间全权送人）。
        if not owner_id or owner_id == "anonymous":
            raise PermissionDeniedError(
                message=translate(
                    "err.space.tenant_admin_required",
                    params={},
                    fallback="创建空间需要租户管理员权限",
                )
            )
        if not await is_tenant_admin(tenant_id, owner_id):
            raise PermissionDeniedError(
                message=translate(
                    "err.space.tenant_admin_required",
                    params={},
                    fallback="创建空间需要租户管理员权限",
                )
            )
        async with get_db_session() as session:
            repo = SpaceRepository(session)
            obj = await repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                name=data["name"],
                description=data.get("description"),
                owner_id=str(owner_id),
            )
            await session.commit()
            schedule_emit({
                "tenant_id": tenant_id,
                "log_type": "OPERATION",
                "action": "create_space",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.SPACE.value,
                "resource_id": obj.id,
            })
            return obj.to_dict()

    async def get(self, space_id: str, tenant_id: str, user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_space_visible(tenant_id, space_id, user_id)  # 非成员 404
        async with get_db_session() as session:
            repo = SpaceRepository(session)
            obj = await repo.get_required(space_id, tenant_id)
            result = obj.to_dict()
            tenant_admin = await is_tenant_admin(tenant_id, user_id)
            result["can_manage_permissions"] = tenant_admin or (result.get("owner_id") == user_id)
            # 可写空间（KB/文档/服务写）：owner 或 manager 成员；租户管理员全可写
            write_ids = await get_write_space_ids(tenant_id, user_id)
            result["can_write_space"] = write_ids is None or space_id in write_ids
            return result

    async def list(self, tenant_id: str, offset: int = 0, limit: int = 20,
                   user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = SpaceRepository(session)
            tenant_admin = await is_tenant_admin(tenant_id, user_id)
            # 成员过滤落 SQL（否则分页/total 错乱）：owner OR 存在 space_permissions 记录；
            # 租户管理员（tenant:write）与无 user 内部链路不过滤
            conds = None
            if user_id and user_id != "anonymous" and not tenant_admin:
                conds = [
                    or_(
                        Space.owner_id == user_id,
                        exists().where(and_(
                            SpacePermission.space_id == Space.id,
                            SpacePermission.tenant_id == tenant_id,
                            SpacePermission.user_id == user_id,
                            SpacePermission.is_deleted == 0,
                        )),
                    )
                ]
            items = await repo.list_all(tenant_id, offset, limit, extra_conditions=conds)
            total = await repo.count(tenant_id, extra_conditions=conds)
            # 可写空间集合（owner/manager；租户管理员 None=全可写）——批量计算 can_write_space
            write_ids = await get_write_space_ids(tenant_id, user_id)
            rows = [o.to_dict() for o in items]
            for row in rows:
                row["can_manage_permissions"] = tenant_admin or (row.get("owner_id") == user_id)
                row["can_write_space"] = (
                    write_ids is None or row.get("id") in write_ids
                )
            return {
                "items": rows,
                "total": total, "offset": offset, "limit": limit,
                "can_create_space": tenant_admin,
            }

    async def update(self, space_id: str, tenant_id: str, data: dict,
                     user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_space_manage(tenant_id, space_id, user_id)
        async with get_db_session() as session:
            repo = SpaceRepository(session)
            obj = await repo.get_required(space_id, tenant_id)
            updatable = {"name", "description", "status"}
            values = {k: v for k, v in data.items() if k in updatable and v is not None}
            if values:
                obj = await repo.update(space_id, tenant_id, **values)
                await session.commit()
            schedule_emit({
                "tenant_id": tenant_id,
                "log_type": "OPERATION",
                "action": "update_space",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.SPACE.value,
                "resource_id": space_id,
            })
            return obj.to_dict()

    async def delete(self, space_id: str, tenant_id: str, user_id: str | None = None) -> bool:
        tenant_id = require_tenant(tenant_id)
        await require_space_manage(tenant_id, space_id, user_id)
        async with get_db_session() as session:
            repo = SpaceRepository(session)
            await repo.get_required(space_id, tenant_id)
            await repo.delete_soft(space_id, tenant_id)

            # 级联删除权限记录（硬删；space_permissions.is_deleted 为遗留列，本设计不使用软删）
            existing_perm = await session.execute(
                select(SpacePermission).where(
                    SpacePermission.space_id == space_id,
                    SpacePermission.tenant_id == tenant_id,
                    SpacePermission.is_deleted == 0,
                )
            )
            for sp in existing_perm.scalars().all():
                await session.delete(sp)

            # 级联：清理其下 KB 的授权记录（否则空间已删后授权用户仍能看到孤儿 KB）
            await session.execute(
                text(
                    "DELETE FROM knowledge_base.kb_permissions "
                    "WHERE kb_id IN (SELECT id FROM knowledge_base.knowledge_info "
                    "WHERE space_id=:sid AND is_deleted=0)"
                ),
                {"sid": space_id},
            )

            await session.commit()
            schedule_emit({
                "tenant_id": tenant_id,
                "log_type": "OPERATION",
                "action": "delete_space",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.SPACE.value,
                "resource_id": space_id,
            })
            return True

    async def get_permissions(self, space_id: str, tenant_id: str,
                              user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_space_visible(tenant_id, space_id, user_id)  # 非成员 404
        async with get_db_session() as session:
            # 后端 join display_name：viewer/知识编辑者无 user:read（id=3），
            # 前端拉 /users 会 403；join 顺带避免前端拉全量用户。
            # 反向转换 u.id::text = sp.user_id：存量非数字 user_id 不会触发 CAST 报错
            # （永不 500、无需洗数据）；排除软删用户。
            result = await session.execute(
                text("""
                    SELECT sp.user_id, sp.role, u.display_name, sp.created_at
                      FROM knowledge_base.space_permissions sp
                      LEFT JOIN platform.users u
                        ON u.id::text = sp.user_id
                       AND u.is_deleted = 0
                     WHERE sp.space_id = :space_id
                       AND sp.tenant_id = :tenant_id
                       AND sp.is_deleted = 0
                """),
                {"space_id": space_id, "tenant_id": tenant_id},
            )
            permissions = [
                {
                    "id": row[0],
                    "user_id": row[0],
                    "role": row[1],
                    "display_name": row[2],
                    "created_at": row[3].isoformat() if row[3] else None,
                }
                for row in result.all()
            ]
            # owner 不落 space_permissions（设计 §2）——但 UI 需要展示创建者，
            # 响应层合成只读 owner 信息（owner_id 来自 spaces 表，join display_name）
            owner_row = (await session.execute(
                text("""
                    SELECT s.owner_id, u.display_name
                      FROM knowledge_base.spaces s
                      LEFT JOIN platform.users u
                        ON u.id::text = s.owner_id
                       AND u.is_deleted = 0
                     WHERE s.id = :space_id
                       AND s.tenant_id = :tenant_id
                       AND s.is_deleted = 0
                """),
                {"space_id": space_id, "tenant_id": tenant_id},
            )).first()
            owner = None
            if owner_row and owner_row[0]:
                owner = {
                    "user_id": owner_row[0],
                    "display_name": owner_row[1],
                }
            return {"permissions": permissions, "owner": owner}

    async def set_permissions(self, space_id: str, tenant_id: str, permissions: list,
                              user_id: str | None = None) -> bool:
        tenant_id = require_tenant(tenant_id)
        await require_space_manage(tenant_id, space_id, user_id)
        # role 白名单：owner 不落 space_permissions（第一版不支持转移），可写值只有 manager/viewer
        for perm in permissions:
            if perm.get("role") not in ("manager", "viewer"):
                raise InvalidParameterError(
                    message=translate(
                        "err.space.invalid_role",
                        params={"role": str(perm.get("role"))},
                        fallback=f"非法角色: {perm.get('role')}（仅支持 manager / viewer）",
                    )
                )
            if not str(perm.get("user_id", "")).isdigit():
                raise InvalidParameterError(
                    message=translate(
                        "err.space.invalid_user_id",
                        params={"user_id": str(perm.get("user_id", ""))},
                        fallback=f"非法用户 ID: {perm.get('user_id', '')}（必须为数字）",
                    )
                )
        async with get_db_session() as session:
            # 移除已有权限（硬删）
            existing = await session.execute(
                select(SpacePermission).where(
                    SpacePermission.space_id == space_id,
                    SpacePermission.tenant_id == tenant_id,
                    SpacePermission.is_deleted == 0,
                )
            )
            for sp in existing.scalars().all():
                await session.delete(sp)
            # 关键：先 flush 让 DELETE 落库再插入——SQLAlchemy UoW 默认 flush 顺序是
            # INSERT 先于 DELETE，同一 flush 内删旧插新会撞 partial unique index
            # （idx_kb_spp_unique_member：tenant_id+space_id+user_id WHERE is_deleted=0）
            await session.flush()

            # 去重：同 user_id 多行保留最高角色（manager > viewer）
            deduped: dict[str, str] = {}
            for perm in permissions:
                uid = str(perm["user_id"])
                role = perm["role"]
                if uid not in deduped or (role == "manager" and deduped[uid] == "viewer"):
                    deduped[uid] = role

            # 添加新权限
            for uid, role in deduped.items():
                perm_obj = SpacePermission(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    space_id=space_id,
                    user_id=uid,
                    role=role,
                )
                session.add(perm_obj)

            await session.commit()
            schedule_emit({
                "tenant_id": tenant_id,
                "log_type": "OPERATION",
                "action": "set_space_permissions",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.SPACE.value,
                "resource_id": space_id,
            })
            return True
