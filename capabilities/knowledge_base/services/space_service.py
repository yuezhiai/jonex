"""
知识库能力 — 领域空间服务
"""
from __future__ import annotations

import uuid

from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.exc import IntegrityError

from jonex_core.common import get_db_session
from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceConflictError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models.space import Space, SpacePermission
from ..repository import SpaceRepository
from .space_permission_service import (
    SPACE_MANAGER,
    SPACE_MEMBER,
    get_write_space_ids,
    is_tenant_admin,
    require_space_manage,
    require_space_visible,
)

# [jonex] 权限重构 B2：空间角色取值与去重优先级。
# 从 space_permission_service 引入取值常量，避免两处各写一份字面量。
# dedup 用 rank 比较（原实现是 `role == "manager" and deduped[uid] == "viewer"` 的
# 字符串两两比较）—— 将来加层级只改 _SPACE_ROLE_RANK 一处。
_SPACE_ROLE_RANK: dict[str, int] = {SPACE_MEMBER: 0, SPACE_MANAGER: 1}


class SpaceService:
    """领域空间 CRUD + 权限"""

    MAX_NAME_LENGTH = 255
    MAX_DESCRIPTION_LENGTH = 1024

    @staticmethod
    def _validate_name(name: str) -> None:
        """名称非空 + 长度上限（strip 后判定）。"""
        if not name:
            raise InvalidParameterError(
                message=translate("err.space.name_required", params={}, fallback="空间名称不能为空")
            )
        if len(name) > SpaceService.MAX_NAME_LENGTH:
            raise InvalidParameterError(
                message=translate(
                    "err.space.name_too_long",
                    params={"max": SpaceService.MAX_NAME_LENGTH},
                    fallback=f"空间名称长度不能超过 {SpaceService.MAX_NAME_LENGTH} 个字符",
                )
            )

    @staticmethod
    def _validate_description(description: str | None) -> None:
        """描述长度上限（None 安全跳过；超长拒绝不截断）。"""
        if description is not None and len(description) > SpaceService.MAX_DESCRIPTION_LENGTH:
            raise InvalidParameterError(
                message=translate(
                    "err.space.description_too_long",
                    params={"max": SpaceService.MAX_DESCRIPTION_LENGTH},
                    fallback=f"描述长度不能超过 {SpaceService.MAX_DESCRIPTION_LENGTH} 个字符",
                )
            )

    async def _ensure_name_available(
        self, repo: SpaceRepository, tenant_id: str, name: str, exclude_id: str | None = None
    ) -> None:
        """同租户名称查重，命中抛 ResourceConflictError(409)。"""
        if await repo.name_exists(tenant_id, name, exclude_id=exclude_id):
            raise ResourceConflictError(
                message=translate("err.space.name_exists", params={"name": name}, fallback=f"空间名称已存在: {name}")
            )

    async def create(self, tenant_id: str, data: dict, owner_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        # owner 必须可确定（排除「无 user_id 放行」口径）且是租户管理员（tenant:write）——
        # 收紧终版（2026-08-19）：空间统一由平台/租户管理员创建，再授权给领域服务管理员管理。
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
        name = (data.get("name") or "").strip()
        description = data.get("description")
        self._validate_name(name)
        self._validate_description(description)
        async with get_db_session() as session:
            repo = SpaceRepository(session)
            await self._ensure_name_available(repo, tenant_id, name)
            try:
                obj = await repo.create(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    name=name,
                    description=description,
                    owner_id=str(owner_id),
                )
                # [jonex] 权限重构 B2（D2）：把创建人写入 space_permissions 为 space_manager。
                #
                # 取消 owner 概念后，`spaces.owner_id` 只是「创建人」展示字段，不参与判定
                # （space_permission_service 三个函数都已移除对它的引用）。创建人的管理权
                # 完全来自下面这条记录。
                #
                # **必须与建空间同事务**：commit 在本 try 块末尾，add 在它之前 →
                # 任一步失败一起回滚。分成两个事务的话，中途失败会产生
                # 「无人可管的空间」，且只有租户管理员能补救。
                #
                # 注意当前 create 仍要求调用者是租户管理员（上方 is_tenant_admin 校验），
                # 所以这条记录在功能上不改变任何人的权限（租户管理员本就全空间可管），
                # 它的价值是让「谁能管这个空间」这件事在数据上是显式的、可转交的。
                session.add(SpacePermission(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    space_id=obj.id,
                    user_id=str(owner_id),
                    role=SPACE_MANAGER,
                ))
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise ResourceConflictError(
                    message=translate(
                        "err.space.name_exists",
                        params={"name": name},
                        fallback=f"空间名称已存在: {name}",
                    )
                )
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
            # 可管空间（space_manager 成员）；租户管理员全可管
            write_ids = await get_write_space_ids(tenant_id, user_id)
            # [jonex] B2（N2/D2）：原先是 `tenant_admin or (owner_id == user_id)`。
            # 取消 owner 后那个条件恒为 false → **所有空间管理者的「管理成员 / 编辑 /
            # 删除」按钮全部消失**，而后端其实放行 —— 前端看得见点不动，最难查的一类不一致。
            # 现在与 can_write_space 同源（write_ids 已在上一行算出，零额外查询）。
            result["can_manage_permissions"] = tenant_admin or (space_id in (write_ids or ()))
            result["can_write_space"] = write_ids is None or space_id in write_ids
            return result

    async def list(self, tenant_id: str, offset: int = 0, limit: int = 20,
                   user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = SpaceRepository(session)
            tenant_admin = await is_tenant_admin(tenant_id, user_id)
            # 成员过滤落 SQL（否则分页/total 错乱）：存在 space_permissions 记录即可见；
            # 租户管理员与无 user 内部链路不过滤。
            #
            # [jonex] 权限重构 B2（N1/D2）：去掉了 `Space.owner_id == user_id` 分支。
            # ⚠️ 这段是 `list` **自己**写的一份可见性过滤，不走 get_visible_space_ids，
            # 所以方案 §7.1 的「4 个判定函数」改名清单覆盖不到这里 —— 漏改的话
            # 创建人即使没有成员记录也能在列表里看到空间，与判定链不一致。
            #
            # 刻意保留 exists() 子查询形态、不改调 get_visible_space_ids：
            # 后者返回 list，套进来会破坏 repo 的分页与 count。
            conds = None
            if user_id and user_id != "anonymous" and not tenant_admin:
                conds = [
                    exists().where(and_(
                        SpacePermission.space_id == Space.id,
                        SpacePermission.tenant_id == tenant_id,
                        SpacePermission.user_id == user_id,
                        SpacePermission.role.in_(tuple(_SPACE_ROLE_RANK)),
                        SpacePermission.is_deleted == 0,
                    ))
                ]
            items = await repo.list_all(tenant_id, offset, limit, extra_conditions=conds)
            total = await repo.count(tenant_id, extra_conditions=conds)
            # 可管空间集合（space_manager；租户管理员 None=全可管）——批量计算两个布尔
            write_ids = await get_write_space_ids(tenant_id, user_id)
            rows = [o.to_dict() for o in items]
            for row in rows:
                # [jonex] B2（N2）：同 get() —— 由资源身份决定，不再看 owner_id
                row["can_manage_permissions"] = tenant_admin or (row.get("id") in (write_ids or ()))
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
            if data.get("name") is not None:
                name = data["name"].strip()
                self._validate_name(name)
                await self._ensure_name_available(repo, tenant_id, name, exclude_id=obj.id)
                data = {**data, "name": name}
            if data.get("description") is not None:
                self._validate_description(data["description"])
            updatable = {"name", "description", "status"}
            values = {k: v for k, v in data.items() if k in updatable and v is not None}
            try:
                if values:
                    obj = await repo.update(space_id, tenant_id, **values)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise ResourceConflictError(
                    message=translate(
                        "err.space.name_exists",
                        params={"name": data.get("name", "")},
                        fallback=f"空间名称已存在: {data.get('name', '')}",
                    )
                )
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

            # 级联：软删其下 KB 的授权记录（否则空间已删后授权用户仍能看到孤儿 KB）。
            # [jonex] 权限重构 B5（执行文档 §6.3 步骤 2）：由硬删改为**软删**，与
            # set_permissions 的 D5 级联口径一致 —— KB 授权保留痕迹便于事后追溯，
            # 且 space 本身也是软删（delete_soft），两者一致才不会一边留痕一边抹掉。
            await session.execute(
                text(
                    "UPDATE knowledge_base.kb_permissions SET is_deleted = 1, "
                    "updated_at = NOW() "
                    "WHERE tenant_id = :t AND is_deleted = 0 "
                    "  AND kb_id IN (SELECT id FROM knowledge_base.knowledge_info "
                    "                WHERE space_id = :sid AND tenant_id = :t "
                    "                  AND is_deleted = 0)"
                ),
                {"sid": space_id, "t": tenant_id},
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
            # [jonex] B2（D2）：创建人现在**也在** space_permissions 里（space_manager），
            # 所以他会正常出现在上面的 permissions 列表中。这里额外返回的 owner 信息
            # 是**纯展示用**（UI 想标注「谁建的」），**不参与任何权限判定**。
            # 前端不要拿它做门控 —— 门控看 can_manage_permissions / can_write_space。
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

    async def get_permission_candidates(self, space_id: str, tenant_id: str,
                                        user_id: str | None = None) -> list:
        """空间「添加成员」候选用户：本租户活跃用户，排除已加入空间成员与操作者本人。

        鉴权：require_space_manage（空间管理者 / 租户管理员），与 set_permissions 同口径；
        invoke 链路在 capability 层已按 _SPACE_LEVEL_ACTIONS 前置判定，此处是 REST 直连兜底。
        """
        tenant_id = require_tenant(tenant_id)
        await require_space_manage(tenant_id, space_id, user_id)
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT u.id, u.username, u.display_name, u.email
                      FROM platform.users u
                     WHERE u.tenant_id = :tenant_id
                       AND u.is_deleted = 0
                       AND u.id::text <> :user_id
                       AND NOT EXISTS (
                           SELECT 1
                             FROM knowledge_base.space_permissions sp
                            WHERE sp.space_id = :space_id
                              AND sp.tenant_id = :tenant_id
                              AND sp.user_id = u.id::text
                              AND sp.is_deleted = 0
                       )
                     ORDER BY u.id
                """),
                {"space_id": space_id, "tenant_id": tenant_id, "user_id": user_id or ""},
            )
            return [
                {
                    "user_id": str(row[0]),
                    "username": row[1],
                    "display_name": row[2],
                    "email": row[3],
                }
                for row in result.all()
            ]

    async def set_permissions(self, space_id: str, tenant_id: str, permissions: list,
                              user_id: str | None = None) -> bool:
        tenant_id = require_tenant(tenant_id)
        await require_space_manage(tenant_id, space_id, user_id)
        # [jonex] 权限重构 B2（E3）：**撤掉了「空间创建者不能作为普通成员添加」拦截**。
        #
        # 原实现先查 spaces.owner_id，若提交列表里含 owner 就抛 400。这与 D2 互斥：
        # D2 要求创建人以 space_manager 身份**落在** space_permissions 里。
        #
        # 冲突形态很具体：本方法是**全量替换**语义。029 回填后成员列表含 owner，
        # 设置页原样提交 → 旧拦截会让**每个有 owner 的空间保存失败**。
        #
        # 创建人保护改由 B5 的「空间至少保留一名管理者」承担 —— 那条不依赖
        # 「谁是创建人」这个会过期的事实（创建人可能离职），也不会让「转交空间」永远做不了。
        #
        # role 白名单：可写值只有 space_manager / member（两级，无 owner）
        for perm in permissions:
            if perm.get("role") not in _SPACE_ROLE_RANK:
                raise InvalidParameterError(
                    message=translate(
                        "err.space.invalid_role",
                        params={"role": str(perm.get("role"))},
                        fallback=(
                            f"非法角色: {perm.get('role')}"
                            f"（仅支持 {SPACE_MANAGER} / {SPACE_MEMBER}）"
                        ),
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

        # 去重：同 user_id 多行保留最高角色（space_manager > member）
        # [jonex] B2：原实现是字符串两两比较，加一级就要改；改成 rank 比较后只动 _SPACE_ROLE_RANK。
        # 提到写库之前算，是为了让下面的「管理者不得清零」在**删除任何东西之前**就能拦下 ——
        # 否则先删旧成员、再发现新集合非法，事务虽会回滚，但白做一趟。
        deduped: dict[str, str] = {}
        for perm in permissions:
            uid = str(perm["user_id"])
            role = perm["role"]
            if uid not in deduped or _SPACE_ROLE_RANK[role] > _SPACE_ROLE_RANK[deduped[uid]]:
                deduped[uid] = role

        # [jonex] 权限重构 B5（方案 §7.1 / 执行文档 §6.3 步骤 3）：管理者不得清零。
        # 判定**基于 deduped（提交后的最终集合）**而非单条操作 —— 这样「换管理者」
        # （旧管理者移除 + 新管理者加入，同一次提交）能放行，只有「最终一个管理者都不剩」才拦。
        if not any(role == SPACE_MANAGER for role in deduped.values()):
            raise InvalidParameterError(
                message=translate(
                    "err.space.no_manager_left",
                    fallback="空间至少需保留一名管理者",
                )
            )

        async with get_db_session() as session:
            # 移除已有权限（硬删）。先查旧成员集合 —— D5 级联要用它算「被移出的人」。
            existing = await session.execute(
                select(SpacePermission).where(
                    SpacePermission.space_id == space_id,
                    SpacePermission.tenant_id == tenant_id,
                    SpacePermission.is_deleted == 0,
                )
            )
            existing_rows = existing.scalars().all()
            old_member_ids = {str(sp.user_id) for sp in existing_rows}
            for sp in existing_rows:
                await session.delete(sp)
            # 关键：先 flush 让 DELETE 落库再插入——SQLAlchemy UoW 默认 flush 顺序是
            # INSERT 先于 DELETE，同一 flush 内删旧插新会撞 partial unique index
            # （idx_kb_spp_unique_member：tenant_id+space_id+user_id WHERE is_deleted=0）
            await session.flush()

            # [jonex] 权限重构 B5（方案 D5 / 执行文档 §6.3 步骤 2）：移出空间级联软删 KB 授权。
            # 被移出的人 = 旧成员 − 新成员。对这些人，软删他们在**本空间下全部 KB** 的授权，
            # 否则会留下「已不是空间成员、却仍持 KB 授权」的越池状态（B5 成员池约束的反面）。
            #
            # · 软删（is_deleted=1）而非硬删：保留痕迹便于事后追溯「谁曾被授权过」。
            # · 与 space_permissions 替换**同事务**：半途失败不会留下越池残留。
            removed_ids = old_member_ids - set(deduped.keys())
            if removed_ids:
                await session.execute(
                    text(
                        "UPDATE knowledge_base.kb_permissions SET is_deleted = 1, "
                        "updated_at = NOW() "
                        "WHERE tenant_id = :t AND is_deleted = 0 "
                        "  AND user_id = ANY(:uids) "
                        "  AND kb_id IN (SELECT id FROM knowledge_base.knowledge_info "
                        "                WHERE space_id = :sid AND tenant_id = :t "
                        "                  AND is_deleted = 0)"
                    ),
                    {"t": tenant_id, "uids": list(removed_ids), "sid": space_id},
                )

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
