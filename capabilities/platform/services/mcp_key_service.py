"""
MCP Key 管理服务。
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.crypto import generate_mcp_key, hash_mcp_key
from jonex_core.common.exceptions import ResourceNotFoundError, InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.security.user_auth import get_user_auth
from capabilities.platform.models.mcp_key import McpKey
from capabilities.platform.repository.mcp_key_repository import McpKeyRepository, McpKeyServiceMappingRepository
from capabilities.platform.dtos.mcp_key_dto import (
    McpKeyCreateRequest,
    McpKeyResponse,
    McpKeyUpdateRequest,
    ServicePermissionItem,
)

logger = logging.getLogger(__name__)


class McpKeyService:
    """MCP Key 管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = McpKeyRepository(session)
        self.user_auth = get_user_auth()

    # ---- helpers ----

    @staticmethod
    def _default_service_permission_level(key_permissions: list[str]) -> str:
        """根据 Key 级权限推断 service 级默认权限级别。

        Key 级 "*" 或含 "write" → service 级 "*"（跳过所有 service 检查）
        Key 级 "read" 或无 → service 级 "call"（当前行为，向后兼容）
        """
        if "*" in key_permissions or "write" in key_permissions:
            return "*"
        return "call"

    def _extract_username(self, authorization_header: str | None) -> str:
        """从 Authorization Header 提取用户名，处理所有边界情况。

        边界处理（按优先级）：
        1. authorization_header is None → "test_token"
        2. authorization_header 是空字符串 → "test_token"
        3. authorization_header 不以 "Bearer " 开头 → "test_token"
        4. 提取 token = authorization_header[7:]（去掉 "Bearer " 前缀）
        5. token 以 "jonex_test_" 开头 → "test_token"（测试 token 无法 JWT 解码）
        6. decode_token(token) → payload.get("username")
        7. username 为 None 或空字符串 → "test_token"
        8. decode_token 抛出任何异常 → "test_token"
        9. 否则返回 username（正常 JWT 解码结果）

        最低可辨识值为 "test_token"（非空字符串）。
        """
        if not authorization_header:
            return "test_token"

        if not authorization_header.startswith("Bearer "):
            return "test_token"

        token = authorization_header[7:]  # 去掉 "Bearer " 前缀
        if not token:
            return "test_token"

        if token.startswith("jonex_test_"):
            return "test_token"

        try:
            payload = self.user_auth.decode_token(token)
            username = payload.get("username")
            if not username:
                return "test_token"
            return username
        except Exception as e:
            logger.warning("JWT decode failed in _extract_username, falling back to test_token: %s", e)
            return "test_token"

    # ---- business methods ----

    async def create(
        self,
        tenant_id: str,
        req: McpKeyCreateRequest,
        authorization_header: str | None = None,
    ) -> dict:
        """创建 MCP Key，一次性返回明文 yxm_... Key。

        permissions 存前用 ",".join() 转为逗号分隔字符串。
        created_by 通过 _extract_username() helper 统一提取。
        service_ids 写入 mcp_key_service_mappings 中间表。
        IntegrityError（hash 碰撞）时重试一次。
        """
        tenant_id = require_tenant(tenant_id)
        created_by = self._extract_username(authorization_header)

        plaintext = generate_mcp_key()
        key_prefix = plaintext[4:12]  # yxm_ 后 8 位
        key_hash_value = hash_mcp_key(plaintext)
        key_id = uuid.uuid4().hex
        service_ids = req.service_ids or []
        service_permissions = req.service_permissions or []

        mcp_key = McpKey(
            id=key_id,
            tenant_id=tenant_id,
            name=req.name,
            key_prefix=key_prefix,
            key_hash=key_hash_value,
            permissions=",".join(req.permissions),
            allowed_kb_ids=req.allowed_kb_ids or [],
            created_by=created_by,
            created_at=datetime.now(timezone.utc),
            expires_at=req.expires_at,
        )

        sp = await self.session.begin_nested()
        try:
            self.session.add(mcp_key)
            await self.session.flush()
            await sp.commit()
        except IntegrityError:
            await sp.rollback()
            logger.warning("MCP Key hash 碰撞，重试生成")
            # Expunge the invalidated object so the session can accept a fresh one
            self.session.expunge(mcp_key)
            plaintext = generate_mcp_key()
            key_prefix = plaintext[4:12]
            key_hash_value = hash_mcp_key(plaintext)
            mcp_key = McpKey(
                id=key_id,
                tenant_id=tenant_id,
                name=req.name,
                key_prefix=key_prefix,
                key_hash=key_hash_value,
                permissions=",".join(req.permissions),
                allowed_kb_ids=req.allowed_kb_ids or [],
                created_by=created_by,
                created_at=datetime.now(timezone.utc),
                expires_at=req.expires_at,
            )
            # Retry in a fresh savepoint
            sp2 = await self.session.begin_nested()
            try:
                self.session.add(mcp_key)
                await self.session.flush()
                await sp2.commit()
            except IntegrityError:
                await sp2.rollback()
                raise

        # 写入 service 映射（service_permissions 优先于 service_ids）
        sp_items = service_permissions or []
        if sp_items:
            mappings: list[str] | list[tuple[str, str]] = [
                (item.service_id, item.permission_level) for item in sp_items
            ]
        elif service_ids:
            default_pl = self._default_service_permission_level(req.permissions)
            mappings = [(sid, default_pl) for sid in service_ids]
        else:
            mappings = []
        if mappings:
            mapping_repo = McpKeyServiceMappingRepository(self.session)
            await mapping_repo.set_mappings(key_id, mappings)

        logger.info(f"MCP Key 已创建: id={key_id}, name={req.name}")

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_key.create",
                resource="MCP_KEY",
                resource_id=key_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 创建审计日志写入失败（不阻塞业务）")

        # 查询当前映射用于响应
        mapping_repo = McpKeyServiceMappingRepository(self.session)
        current_mappings = await mapping_repo.get_mappings(key_id)

        return {
            "id": key_id,
            "plaintext": plaintext,
            "name": req.name,
            "key_prefix": key_prefix,
            "permissions": req.permissions,
            "allowed_kb_ids": req.allowed_kb_ids or [],
            "service_ids": [sid for sid, _pl in current_mappings],
            "service_permissions": [
                {"service_id": sid, "permission_level": pl}
                for sid, pl in current_mappings
            ],
            "created_at": mcp_key.created_at,
            "expires_at": req.expires_at,
        }

    async def list_all(self, tenant_id: str) -> list[McpKeyResponse]:
        """全量返回租户下所有 Key 元数据（含已撤销），不含 key_hash 和明文 Key。

        委托给 self.repo.list_by_tenant()，通过 McpKeyResponse.from_orm() 转换为 DTO。
        批量查询 service_ids 映射，一次 SQL 避免 N+1。
        """
        tenant_id = require_tenant(tenant_id)
        keys = await self.repo.list_by_tenant(tenant_id)

        # 批量查询 service_ids 映射
        mapping_repo = McpKeyServiceMappingRepository(self.session)
        key_ids = [k.id for k in keys]
        mappings_map = await mapping_repo.get_mappings_batch(key_ids)

        result = []
        for k in keys:
            dto = McpKeyResponse.from_orm(k)
            k_mappings = mappings_map.get(k.id, [])
            dto.service_ids = [sid for sid, _pl in k_mappings]
            dto.service_permissions = [
                {"service_id": sid, "permission_level": pl}
                for sid, pl in k_mappings
            ]
            result.append(dto)
        return result

    async def get_by_id(self, tenant_id: str, key_id: str) -> McpKeyResponse:
        """获取单个 MCP Key，不存在时抛出 ResourceNotFoundError。
        响应包含 service_ids 映射。"""
        require_tenant(tenant_id)
        key = await self.repo.get_by_id(key_id, tenant_id)
        if key is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_key.not_found",
                    fallback=f"MCP Key 不存在: {key_id}",
                ),
                details={"key_id": key_id},
            )
        dto = McpKeyResponse.from_orm(key)
        mapping_repo = McpKeyServiceMappingRepository(self.session)
        mappings = await mapping_repo.get_mappings(key_id)
        dto.service_ids = [sid for sid, _pl in mappings]
        dto.service_permissions = [
            {"service_id": sid, "permission_level": pl}
            for sid, pl in mappings
        ]
        return dto

    async def revoke(self, tenant_id: str, key_id: str, authorization_header: str | None = None) -> None:
        """撤销 MCP Key（设置 revoked_at）。Key 不存在时 raise ResourceNotFoundError。

        幂等操作——对已撤销 Key 再次调用不报错。
        """
        tenant_id = require_tenant(tenant_id)
        created_by = self._extract_username(authorization_header)
        success = await self.repo.revoke(key_id, tenant_id)
        if not success:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_key.not_found",
                    fallback=f"MCP Key 不存在: {key_id}",
                ),
                details={"key_id": key_id, "tenant_id": tenant_id},
            )

        logger.info(f"MCP Key 已撤销: id={key_id}")

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_key.revoke",
                resource="MCP_KEY",
                resource_id=key_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 撤销审计日志写入失败（不阻塞业务）")

    async def reset(
        self,
        tenant_id: str,
        key_id: str,
        authorization_header: str | None = None,
        name: str | None = None,
        permissions: list[str] | None = None,
        allowed_kb_ids: list[str] | None = None,
        service_ids: list[str] | None = None,
        service_permissions: list | None = None,
        expires_at: datetime | None = None,
    ) -> dict:
        """原子事务重置 MCP Key：撤销旧 Key + 替换映射 + 生成新 Key，返回新明文。

        使用 async with self.session.begin() 包裹整个事务：
        1. 在事务内使用 get_by_id_for_update()（内部 SELECT ... FOR UPDATE 行锁，
           使用 with_for_update() 防止并发 reset 读到同一旧 Key）
        2. 检查 old_key 是否为 None（Key 不存在 → ResourceNotFoundError）
        3. 从旧 Key 读取 name/permissions/allowed_kb_ids（撤销前读取）
        4. 撤销旧 Key（幂等）
        5. 删除旧 service 映射
        6. 生成新 Key（plaintext → hash → INSERT）
        7. 写入新 service 映射
        8. 参数传入值覆盖旧 Key 字段（None 时继承旧值）
        9. 审计日志

        事务退出后自动 commit（成功）或 rollback（异常）。
        幂等——对已撤销 Key 调用 reset 正常生成新 Key。
        IntegrityError（hash 碰撞）时重试一次。
        """
        tenant_id = require_tenant(tenant_id)

        async with self.session.begin():
            # 1. 行锁查询旧 Key（必须在活跃事务内）
            old_key = await self.repo.get_by_id_for_update(key_id, tenant_id)

            # 2. 检查 None — 防止后续 old_key.name 抛 AttributeError
            if old_key is None:
                raise ResourceNotFoundError(
                    message=translate(
                        "err.mcp_key.not_found",
                        fallback=f"MCP Key 不存在: {key_id}",
                    ),
                    details={"key_id": key_id, "tenant_id": tenant_id},
                )

            # 3. 从旧 Key 读取属性（撤销前读取，revoke 是 UPDATE 不删除行）
            #    参数传入值 > 旧 Key 继承
            new_name = name if name is not None else old_key.name
            new_permissions_str = (
                ",".join(permissions) if permissions is not None
                else old_key.permissions
            )
            new_allowed_kb_ids = (
                allowed_kb_ids if allowed_kb_ids is not None
                else old_key.allowed_kb_ids
            )
            new_expires_at = (
                expires_at if expires_at is not None
                else old_key.expires_at
            )
            # Determine service mappings: service_permissions > service_ids > inherit from old key
            if service_permissions is not None:
                new_mappings: list[str] | list[tuple[str, str]] = [
                    (item.service_id, item.permission_level)
                    for item in service_permissions
                ]
            elif service_ids is not None:
                # Key 级权限推断默认 service 级权限
                new_perms_list = (
                    permissions if permissions is not None
                    else old_key.permissions.split(",")
                )
                default_pl = self._default_service_permission_level(new_perms_list)
                new_mappings = [(sid, default_pl) for sid in service_ids]
            else:
                new_mappings = await McpKeyServiceMappingRepository(self.session).get_mappings(key_id)

            # 4. 撤销旧 Key（幂等——已撤销也不报错）
            await self.repo.revoke(key_id, tenant_id)

            # 5. 生成新 Key
            plaintext = generate_mcp_key()
            key_prefix = plaintext[4:12]
            key_hash_value = hash_mcp_key(plaintext)
            new_id = uuid.uuid4().hex

            # 6. created_by
            created_by = self._extract_username(authorization_header)

            # 7. 构建新 McpKey 对象
            new_key = McpKey(
                id=new_id,
                tenant_id=tenant_id,
                name=new_name,
                key_prefix=key_prefix,
                key_hash=key_hash_value,
                permissions=new_permissions_str,
                allowed_kb_ids=new_allowed_kb_ids,
                created_by=created_by,
                created_at=datetime.now(timezone.utc),
                expires_at=new_expires_at,
            )

            # 8. 写入新 Key + hash 碰撞重试（使用 savepoint 避免 aborted transaction）
            sp = await self.session.begin_nested()
            try:
                self.session.add(new_key)
                await self.session.flush()
                await sp.commit()
            except IntegrityError:
                await sp.rollback()
                logger.warning("MCP Key reset hash 碰撞，重试生成")
                # Expunge the invalidated object so the session can accept a fresh one
                self.session.expunge(new_key)
                plaintext = generate_mcp_key()
                key_prefix = plaintext[4:12]
                key_hash_value = hash_mcp_key(plaintext)
                new_key = McpKey(
                    id=new_id,
                    tenant_id=tenant_id,
                    name=new_name,
                    key_prefix=key_prefix,
                    key_hash=key_hash_value,
                    permissions=new_permissions_str,
                    allowed_kb_ids=new_allowed_kb_ids,
                    created_by=created_by,
                    created_at=datetime.now(timezone.utc),
                    expires_at=new_expires_at,
                )
                # Retry in a fresh savepoint
                sp2 = await self.session.begin_nested()
                try:
                    self.session.add(new_key)
                    await self.session.flush()
                    await sp2.commit()
                except IntegrityError:
                    await sp2.rollback()
                    raise

            # 9. 替换 service 映射（全量：删旧 → 写新）
            mapping_repo = McpKeyServiceMappingRepository(self.session)
            await mapping_repo.set_mappings(new_id, new_mappings)

        # 事务已提交

        logger.info(
            f"MCP Key 已重置: old_id={key_id}, new_id={new_id}"
        )

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_key.reset",
                resource="MCP_KEY",
                resource_id=new_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 重置审计日志写入失败（不阻塞业务）")

        # 查询最终映射用于响应
        mapping_repo = McpKeyServiceMappingRepository(self.session)
        final_mappings = await mapping_repo.get_mappings(new_id)

        # 注意：permissions 在 DB 中是逗号分隔字符串，返回前 split 为 list
        return {
            "id": new_id,
            "plaintext": plaintext,
            "name": new_name,
            "key_prefix": key_prefix,
            "permissions": new_permissions_str.split(",") if new_permissions_str else [],
            "allowed_kb_ids": new_allowed_kb_ids,
            "service_ids": [sid for sid, _pl in final_mappings],
            "service_permissions": [
                {"service_id": sid, "permission_level": pl}
                for sid, pl in final_mappings
            ],
            "created_at": new_key.created_at,
            "expires_at": new_expires_at,
        }

    async def delete(
        self,
        tenant_id: str,
        key_id: str,
        authorization_header: str | None = None,
    ) -> None:
        """软删除 MCP Key（设置 is_deleted=1）。

        删除后 Key 不可再用于认证，list/get API 自动不可见（BaseRepository
        get_by_id / list_by_tenant 均过滤 is_deleted == 0）。

        幂等——对已删除 Key 再次调用不报错。
        区分"从未存在"（raise ResourceNotFoundError）和"已删除"（静默返回）。
        """
        tenant_id = require_tenant(tenant_id)

        key = await self.repo.get_by_id_include_deleted(key_id, tenant_id)
        if key is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_key.not_found",
                    fallback=f"MCP Key 不存在: {key_id}",
                ),
                details={"key_id": key_id},
            )

        if key.is_deleted == 1:
            logger.info(f"MCP Key 已处于软删除状态，幂等跳过: id={key_id}")
            return

        key.is_deleted = 1
        await self.session.flush()

        logger.info(f"MCP Key 已软删除: id={key_id}")

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService

            created_by = self._extract_username(authorization_header)
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_key.delete",
                resource="MCP_KEY",
                resource_id=key_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 删除审计日志写入失败（不阻塞业务）")

    async def update(
        self,
        tenant_id: str,
        key_id: str,
        req: McpKeyUpdateRequest,
        authorization_header: str | None = None,
    ) -> McpKeyResponse:
        """原位编辑 MCP Key 元数据（不重置 Key，不生成新 plaintext）。

        所有字段可选——不传则保持原值。
        支持更新 name、permissions、allowed_kb_ids、service_ids。
        返回更新后的 McpKeyResponse DTO（含 service_ids）。
        """
        tenant_id = require_tenant(tenant_id)

        key = await self.repo.get_by_id(key_id, tenant_id)
        if key is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_key.not_found",
                    fallback=f"MCP Key 不存在: {key_id}",
                ),
                details={"key_id": key_id},
            )

        if req.name is not None:
            key.name = req.name.strip()
        if req.permissions is not None:
            key.permissions = ",".join(req.permissions)
        if req.allowed_kb_ids is not None:
            key.allowed_kb_ids = req.allowed_kb_ids
        if req.expires_at is not None:
            key.expires_at = req.expires_at

        await self.session.flush()

        # service_permissions 优先于 service_ids
        if req.service_permissions is not None:
            mapping_repo = McpKeyServiceMappingRepository(self.session)
            mappings = [
                (item.service_id, item.permission_level)
                for item in req.service_permissions
            ]
            await mapping_repo.set_mappings(key_id, mappings)
        elif req.service_ids is not None:
            curr_perms = key.permissions.split(",")
            default_pl = self._default_service_permission_level(curr_perms)
            mapping_repo = McpKeyServiceMappingRepository(self.session)
            await mapping_repo.set_mappings(
                key_id, [(sid, default_pl) for sid in req.service_ids]
            )

        logger.info(f"MCP Key 已更新: id={key_id}")

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            created_by = self._extract_username(authorization_header)
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_key.update",
                resource="MCP_KEY",
                resource_id=key_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 更新审计日志写入失败（不阻塞业务）")

        # 构建响应 DTO（含 service_ids 和 service_permissions 查询）
        dto = McpKeyResponse.from_orm(key)
        mapping_repo = McpKeyServiceMappingRepository(self.session)
        mappings = await mapping_repo.get_mappings(key_id)
        dto.service_ids = [sid for sid, _pl in mappings]
        dto.service_permissions = [
            {"service_id": sid, "permission_level": pl}
            for sid, pl in mappings
        ]
        return dto
