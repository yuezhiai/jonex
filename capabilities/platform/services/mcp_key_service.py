"""
MCP Key 管理服务。
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.config import get_config
from jonex_core.common.crypto import generate_mcp_key, hash_mcp_key
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.security.user_auth import get_user_auth
from capabilities.platform.models.mcp_key import McpKey
from capabilities.platform.repository.mcp_key_repository import McpKeyRepository, McpKeyServiceMappingRepository
from capabilities.platform.dtos.mcp_key_dto import (
    McpKeyCreateRequest,
    McpKeyCreateResponse,
    McpKeyResponse,
    McpKeyUpdateRequest,
    ServicePermissionItem,
    WriteGrant,
)

logger = logging.getLogger(__name__)


class McpKeyService:
    """MCP Key 管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = McpKeyRepository(session)
        self.user_auth = get_user_auth()

    # ---- helpers ----

    def _extract_username(self, authorization_header: str | None) -> str:
        """从 Authorization Header 提取用户名，处理所有边界情况。

        边界处理（按优先级）：
        1. authorization_header is None → "test_token"
        2. authorization_header 是空字符串 → "test_token"
        3. authorization_header 不以 "Bearer " 开头 → "test_token"
        4. 提取 token = authorization_header[7:]（去掉 "Bearer " 前缀）
        5. token 以 "jonex_test_" 开头（且 ENABLE_TEST_TOKENS=true）→ "test_token"
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

        if get_config().ENABLE_TEST_TOKENS and token.startswith("jonex_test_"):
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

    async def _validate_space_id(self, tenant_id: str, space_id: str) -> None:
        """校验 space_id 属于当前租户——跨 schema 查询 knowledge_base.spaces。"""
        result = await self.session.execute(
            sa_text(
                "SELECT 1 FROM knowledge_base.spaces "
                "WHERE id = :sid AND tenant_id = :tid AND is_deleted = 0"
            ),
            {"sid": space_id, "tid": tenant_id},
        )
        if result.first() is None:
            raise InvalidParameterError(
                message=f"space_id 不存在或不属于当前租户: {space_id}",
                details={"space_id": space_id},
            )

    async def _validate_service_ids_in_space(
        self, tenant_id: str, space_id: str, service_ids: list[str]
    ) -> None:
        """校验 service_ids 中所有 service 都属于指定 space——跨 schema 查询 knowledge_base.services。"""
        if not service_ids:
            return
        result = await self.session.execute(
            sa_text(
                "SELECT id FROM knowledge_base.services "
                "WHERE id = ANY(:sids) AND tenant_id = :tid AND space_id = :sid AND is_deleted = 0"
            ),
            {"sids": service_ids, "tid": tenant_id, "sid": space_id},
        )
        found = {row[0] for row in result.fetchall()}
        missing = set(service_ids) - found
        if missing:
            raise InvalidParameterError(
                message=f"以下 service_id 不在指定空间内: {', '.join(sorted(missing))}",
                details={"space_id": space_id, "missing_service_ids": sorted(missing)},
            )

    async def _resolve_space_id(self, tenant_id: str, kb_id: str) -> str:
        """反推 space_id：grants[0].kb 对应知识库的 space_id。

        跨 schema 只读查询 knowledge_base.knowledge_info；kb 不存在/跨租户
        → ResourceNotFoundError（不泄露目标知识库是否存在）。
        """
        result = await self.session.execute(
            sa_text(
                "SELECT space_id FROM knowledge_base.knowledge_info "
                "WHERE id = :kb_id AND tenant_id = :tid AND is_deleted = 0"
            ),
            {"kb_id": kb_id, "tid": tenant_id},
        )
        row = result.first()
        if row is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_write_key.kb_not_found",
                    fallback=f"知识库不存在或不属于当前租户: {kb_id}",
                ),
                details={"kb_id": kb_id},
            )
        return row[0]

    async def _validate_kb_ids_in_space(
        self, tenant_id: str, space_id: str, kb_ids: list[str]
    ) -> None:
        """校验 kb_ids 中所有知识库都属于指定 space（跨 schema 查 knowledge_info）。

        空 kb_ids → InvalidParameterError；任何 kb 缺失（跨 space/跨租户）
        → InvalidParameterError。
        """
        if not kb_ids:
            raise InvalidParameterError(
                message="grants 不能为空", details={"space_id": space_id}
            )
        result = await self.session.execute(
            sa_text(
                "SELECT id FROM knowledge_base.knowledge_info "
                "WHERE id = ANY(:kb_ids) AND tenant_id = :tid "
                "AND space_id = :sid AND is_deleted = 0"
            ),
            {"kb_ids": kb_ids, "tid": tenant_id, "sid": space_id},
        )
        found = {row[0] for row in result.fetchall()}
        missing = set(kb_ids) - found
        if missing:
            raise InvalidParameterError(
                message=f"以下 kb_id 不在指定空间内: {', '.join(sorted(missing))}",
                details={"space_id": space_id, "missing_kb_ids": sorted(missing)},
            )

    async def _validate_directory_ownership(
        self, tenant_id: str, kb_ids: list[str], grants: list
    ) -> None:
        """校验 grants 中 specified 模式的 directory 归属。

        每个 folder_id 映射回其 knowledge_base_id，且该 kb 必须 ∈ grants[].kb
        （防「目录与 KB 跨 space 混配」）。folder 不存在/跨租户 → InvalidParameterError。
        """
        folder_ids = [
            fid
            for g in grants
            if getattr(g, "mode", None) == "specified"
            for fid in (getattr(g, "directories", None) or [])
        ]
        if not folder_ids:
            return

        result = await self.session.execute(
            sa_text(
                "SELECT id, knowledge_base_id FROM knowledge_base.folders "
                "WHERE id = ANY(:folder_ids) AND tenant_id = :tid AND is_deleted = 0"
            ),
            {"folder_ids": folder_ids, "tid": tenant_id},
        )
        folder_to_kb = {row[0]: row[1] for row in result.fetchall()}

        missing = set(folder_ids) - set(folder_to_kb.keys())
        if missing:
            raise InvalidParameterError(
                message=f"以下目录不存在或不属于当前租户: {', '.join(sorted(missing))}",
                details={"missing_folder_ids": sorted(missing)},
            )

        kb_set = set(kb_ids)
        invalid = [fid for fid in folder_ids if folder_to_kb[fid] not in kb_set]
        if invalid:
            raise InvalidParameterError(
                message=f"以下目录与知识库跨 space 混配: {', '.join(sorted(invalid))}",
                details={"invalid_folder_ids": sorted(invalid)},
            )

    def _validate_grant_modes(self, grants: list) -> None:
        """校验 grants 的 mode 与 directories 语义一致。

        - specified 必须携带非空 directories；
        - all 不应携带 directories（带了也静默忽略，语义混淆有越权写入风险）。
        """
        for g in grants:
            if g.mode == "specified" and not g.directories:
                raise InvalidParameterError(
                    message="specified 模式必须指定至少一个目录",
                    details={"kb": g.kb},
                )
            if g.mode == "all" and g.directories:
                raise InvalidParameterError(
                    message="all 模式不应携带 directories",
                    details={"kb": g.kb},
                )

    # ---- business methods ----

    @staticmethod
    def _is_client_request_id_conflict(exc: IntegrityError) -> bool:
        """区分 IntegrityError 约束：uq_mcp_keys_tenant_reqid（幂等键冲突）vs key_hash 碰撞。"""
        orig = getattr(exc, "orig", None)
        text = str(orig).lower() if orig is not None else ""
        return "uq_mcp_keys_tenant_reqid" in text

    def _build_mcp_config(self, plaintext: str) -> dict:
        """组装 WorkBuddy 兼容 mcp_config JSON。

        url 直接取 MCP_SERVER_PUBLIC_URL（锁定决策选项 1：无 localhost 兜底，
        生产由 deploy 注入 HTTPS，本地由 .env.local 显式填）。
        """
        return {
            "mcpServers": {
                "jonex-knowledge": {
                    "url": get_config().MCP_SERVER_PUBLIC_URL,
                    "transport": "streamable-http",
                    "headers": {"Authorization": f"Bearer {plaintext}"},
                }
            }
        }

    @staticmethod
    def _build_auth_summary(service_permissions: list, write_grants) -> str:
        """组装 auth_summary：可调用 N · 仅查看 N · 写入开关。"""
        perms = service_permissions or []
        call = sum(
            1
            for sp in perms
            if (sp.get("permission_level") if isinstance(sp, dict) else sp.permission_level) == "call"
        )
        view = sum(
            1
            for sp in perms
            if (sp.get("permission_level") if isinstance(sp, dict) else sp.permission_level) == "view"
        )
        write_state = "写入开启" if write_grants else "写入关闭"
        return f"可调用 {call} · 仅查看 {view} · {write_state}"

    @staticmethod
    def _grant_mode(g) -> str | None:
        """兼容 dict（JSONB 反序列化）与 WriteGrant 对象，取 mode 字段。"""
        return g.get("mode") if isinstance(g, dict) else getattr(g, "mode", None)

    @staticmethod
    def _grant_directories(g) -> list:
        """兼容 dict（JSONB 反序列化）与 WriteGrant 对象，取 directories 字段。"""
        if isinstance(g, dict):
            return g.get("directories") or []
        return getattr(g, "directories", None) or []

    @staticmethod
    def _build_write_scope_summary(write_grants) -> str:
        """组装 write_scope_summary：可写知识库 N · 指定目录 N。"""
        grants = write_grants or []
        if not grants:
            return "未开启写入"
        kb_count = len(grants)
        dir_count = sum(
            len(McpKeyService._grant_directories(g))
            for g in grants
            if McpKeyService._grant_mode(g) == "specified"
        )
        return f"可写知识库 {kb_count} · 指定目录 {dir_count}"

    async def _validate_service_publish(
        self, tenant_id: str, service_ids: list[str]
    ) -> None:
        """校验 service_ids 均已发布且未停用（platform.mcp_service_publish）。"""
        if not service_ids:
            return
        result = await self.session.execute(
            sa_text(
                "SELECT service_id FROM platform.mcp_service_publish "
                "WHERE service_id = ANY(:sids) AND tenant_id = :tid "
                "AND is_published = 1 AND stopped_at IS NULL"
            ),
            {"sids": service_ids, "tid": tenant_id},
        )
        published = {row[0] for row in result.fetchall()}
        unpublished = set(service_ids) - published
        if unpublished:
            raise InvalidParameterError(
                message=f"以下服务未发布或已停用: {', '.join(sorted(unpublished))}",
                details={"unpublished_service_ids": sorted(unpublished)},
            )

    async def _build_idempotent_response(self, existing: McpKey) -> dict:
        """幂等命中：返回脱敏信息 + delivery_failed=True，无明文/mcp_config。"""
        dto = McpKeyResponse.from_orm(existing)
        mapping_repo = McpKeyServiceMappingRepository(self.session)
        mappings = await mapping_repo.get_mappings(existing.id)
        dto.service_permissions = [
            {"service_id": sid, "permission_level": pl} for sid, pl in mappings
        ]
        dto.status = McpKey._derive_status(existing)
        dto.auth_summary = self._build_auth_summary(dto.service_permissions, dto.write_grants)
        dto.write_scope_summary = self._build_write_scope_summary(dto.write_grants)
        return McpKeyCreateResponse(
            **dto.dict(),
            plaintext=None,
            mcp_config=None,
            delivery_failed=True,
        ).dict()

    async def create(
        self,
        tenant_id: str,
        req: McpKeyCreateRequest,
        authorization_header: str | None = None,
    ) -> dict:
        """创建统一 MCP Key：幂等（client_request_id）+ 强校验 + 三态 write_grants + 一次性明文 + mcp_config。

        校验顺序即优先级：require_tenant/space → 至少一授权 → write_grants 三态 →
        发布校验 → 幂等预查 → 生成 Key → 映射 → 审计 → 组装响应。
        """
        tenant_id = require_tenant(tenant_id)
        created_by = self._extract_username(authorization_header)

        # 1. 校验 space_id 归属
        await self._validate_space_id(tenant_id, req.space_id)

        # 2. 至少一授权（仅当写入关闭 write_grants=None 且无 service_permissions 时触发；
        #    write_grants=[] 属「写入开启但无知识库」，交由下方三态校验产出特定文案）
        if not req.service_permissions and req.write_grants is None:
            raise InvalidParameterError(
                message="至少需要授权一个已发布服务（service_permissions）或开启写入（write_grants）",
                details={"space_id": req.space_id},
            )

        # 3. write_grants 三态
        write_grants_value = None
        if req.write_grants is not None:
            if req.write_grants == []:
                raise InvalidParameterError(
                    message="写入开启但无知识库（write_grants 不能为空列表）",
                    details={"space_id": req.space_id},
                )
            self._validate_grant_modes(req.write_grants)
            kb_ids = list({g.kb for g in req.write_grants})
            await self._validate_kb_ids_in_space(tenant_id, req.space_id, kb_ids)
            await self._validate_directory_ownership(tenant_id, kb_ids, req.write_grants)
            write_grants_value = [g.dict() for g in req.write_grants]

        # 4. service_permissions 发布校验
        service_permissions = req.service_permissions or []
        if service_permissions:
            service_ids = [sp.service_id for sp in service_permissions]
            await self._validate_service_ids_in_space(tenant_id, req.space_id, service_ids)
            await self._validate_service_publish(tenant_id, service_ids)

        # 5. 幂等预查
        existing = await self.repo.get_by_client_request_id(tenant_id, req.client_request_id)
        if existing is not None:
            return await self._build_idempotent_response(existing)

        # 6. 生成 Key
        plaintext = generate_mcp_key()
        key_prefix = plaintext[4:12]  # yxm_ 后 8 位
        key_hash_value = hash_mcp_key(plaintext)
        key_id = uuid.uuid4().hex

        mcp_key = McpKey(
            id=key_id,
            tenant_id=tenant_id,
            name=req.name,
            note=req.note,
            space_id=req.space_id,
            key_prefix=key_prefix,
            key_hash=key_hash_value,
            write_grants=write_grants_value,
            client_request_id=req.client_request_id,
            created_by=created_by,
            created_at=datetime.now(timezone.utc),
            expires_at=req.expires_at,
        )

        # 7. 事务 + IntegrityError 兜底（约束名区分：幂等键冲突 vs hash 碰撞）
        sp = await self.session.begin_nested()
        try:
            self.session.add(mcp_key)
            await self.session.flush()
            await sp.commit()
        except IntegrityError as exc:
            await sp.rollback()
            if self._is_client_request_id_conflict(exc):
                existing = await self.repo.get_by_client_request_id(tenant_id, req.client_request_id)
                if existing is not None:
                    return await self._build_idempotent_response(existing)
                raise
            logger.warning("MCP Key hash 碰撞，重试生成")
            self.session.expunge(mcp_key)
            plaintext = generate_mcp_key()
            key_prefix = plaintext[4:12]
            key_hash_value = hash_mcp_key(plaintext)
            mcp_key = McpKey(
                id=key_id,
                tenant_id=tenant_id,
                name=req.name,
                note=req.note,
                space_id=req.space_id,
                key_prefix=key_prefix,
                key_hash=key_hash_value,
                write_grants=write_grants_value,
                client_request_id=req.client_request_id,
                created_by=created_by,
                created_at=datetime.now(timezone.utc),
                expires_at=req.expires_at,
            )
            sp2 = await self.session.begin_nested()
            try:
                self.session.add(mcp_key)
                await self.session.flush()
                await sp2.commit()
            except IntegrityError as exc2:
                await sp2.rollback()
                if self._is_client_request_id_conflict(exc2):
                    existing = await self.repo.get_by_client_request_id(tenant_id, req.client_request_id)
                    if existing is not None:
                        return await self._build_idempotent_response(existing)
                raise

        # 8. 写入 service 映射（仅来自 service_permissions）
        if service_permissions:
            mapping_repo = McpKeyServiceMappingRepository(self.session)
            await mapping_repo.set_mappings(
                key_id,
                [(sp.service_id, sp.permission_level) for sp in service_permissions],
            )

        logger.info(f"MCP Key 已创建: id={key_id}, name={req.name}")

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_key.create",
                resource="mcp_key",
                resource_id=key_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 创建审计日志写入失败（不阻塞业务）")

        service_permissions_dicts = [
            {"service_id": sp.service_id, "permission_level": sp.permission_level}
            for sp in service_permissions
        ]

        return {
            "id": key_id,
            "plaintext": plaintext,
            "name": req.name,
            "key_prefix": key_prefix,
            "space_id": req.space_id,
            "service_permissions": service_permissions_dicts,
            "write_grants": write_grants_value,
            "status": McpKey._derive_status(mcp_key),
            "auth_summary": self._build_auth_summary(service_permissions_dicts, write_grants_value),
            "write_scope_summary": self._build_write_scope_summary(req.write_grants or []),
            "mcp_config": self._build_mcp_config(plaintext),
            "delivery_failed": False,
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
            dto.service_permissions = [
                {"service_id": sid, "permission_level": pl}
                for sid, pl in k_mappings
            ]
            dto.status = McpKey._derive_status(k)
            dto.auth_summary = self._build_auth_summary(dto.service_permissions, dto.write_grants)
            dto.write_scope_summary = self._build_write_scope_summary(dto.write_grants)
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
        dto.service_permissions = [
            {"service_id": sid, "permission_level": pl}
            for sid, pl in mappings
        ]
        dto.status = McpKey._derive_status(key)
        dto.auth_summary = self._build_auth_summary(dto.service_permissions, dto.write_grants)
        dto.write_scope_summary = self._build_write_scope_summary(dto.write_grants)
        return dto

    async def revoke(self, tenant_id: str, key_id: str, authorization_header: str | None = None) -> dict:
        """撤销 MCP Key（设置 revoked_at + revoked_by 审计归属）。Key 不存在时 raise ResourceNotFoundError。

        幂等操作——对已撤销 Key 再次调用不报错。
        返回 {"status": "revoked"}，与 toggle 响应 data.status 形状一致。
        """
        tenant_id = require_tenant(tenant_id)
        operator = self._extract_username(authorization_header)
        success = await self.repo.revoke(key_id, tenant_id, revoked_by=operator)
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
                resource="mcp_key",
                resource_id=key_id,
                username=operator,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 撤销审计日志写入失败（不阻塞业务）")

        return {"status": "revoked"}

    async def toggle(
        self,
        tenant_id: str,
        key_id: str,
        authorization_header: str | None = None,
    ) -> McpKeyResponse:
        """停用/启用 MCP Key（可逆，key_id 不变，非 reset 换新）。

        - 停用：置 disabled_at = now（授权保留，可逆）。
        - 启用：清 disabled_at = None（恢复原 Key）。
        - 已撤销 Key 调 toggle → ResourceConflictError（撤销不可逆，不可停用/启用）。
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

        if key.revoked_at is not None:
            raise ResourceConflictError(
                message="已撤销的 Key 不可停用/启用",
                details={"key_id": key_id},
            )

        created_by = self._extract_username(authorization_header)

        if key.disabled_at is not None:
            key.disabled_at = None
            action = "mcp_key.enable"
        else:
            key.disabled_at = datetime.now(timezone.utc)
            action = "mcp_key.disable"

        await self.session.flush()

        logger.info(f"MCP Key 状态已切换: id={key_id}, action={action}")

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action=action,
                resource="mcp_key",
                resource_id=key_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 停用/启用审计日志写入失败（不阻塞业务）")

        # 查询映射构建响应 DTO
        dto = McpKeyResponse.from_orm(key)
        mapping_repo = McpKeyServiceMappingRepository(self.session)
        mappings = await mapping_repo.get_mappings(key_id)
        dto.service_permissions = [
            {"service_id": sid, "permission_level": pl}
            for sid, pl in mappings
        ]
        dto.status = McpKey._derive_status(key)
        dto.auth_summary = self._build_auth_summary(dto.service_permissions, dto.write_grants)
        dto.write_scope_summary = self._build_write_scope_summary(dto.write_grants)
        return dto

    async def recreate(
        self,
        tenant_id: str,
        key_id: str,
        authorization_header: str | None = None,
        expires_at: datetime | None = None,
        name: str | None = None,
    ) -> dict:
        """重新创建 MCP Key（仅 expired 态可用，继承授权 + 生成新明文 Key）。

        继承原 Key 的 service_permissions（过滤已删除/无权服务）+ write_grants；
        生成全新 id/key_hash/key_prefix，旧 Key 保留历史（不撤销）。
        名称可改（name 非 None 时用新名），默认新 Key 有效期 12 个月。不幂等。
        """
        tenant_id = require_tenant(tenant_id)

        old_key = await self.repo.get_by_id(key_id, tenant_id)
        if old_key is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_key.not_found",
                    fallback=f"MCP Key 不存在: {key_id}",
                ),
                details={"key_id": key_id},
            )

        # expired 门禁：仅 expired 态可 recreate，其余（active/disabled/revoked）→ 409
        if McpKey._derive_status(old_key) != "expired":
            raise ResourceConflictError(
                message="仅过期（expired）状态的 Key 可重新创建",
                details={"key_id": key_id, "status": McpKey._derive_status(old_key)},
            )

        mapping_repo = McpKeyServiceMappingRepository(self.session)
        old_mappings = await mapping_repo.get_mappings(key_id)

        # 过滤已删除/无权服务（仅存在性过滤，不过滤发布状态），被丢弃的记入差异列表
        old_sids = [sid for sid, _pl in old_mappings]
        valid_sids: set = set()
        if old_sids and old_key.space_id:
            result = await self.session.execute(
                sa_text(
                    "SELECT id FROM knowledge_base.services "
                    "WHERE id = ANY(:sids) AND tenant_id = :tid "
                    "AND space_id = :sid AND is_deleted = 0"
                ),
                {"sids": old_sids, "tid": tenant_id, "sid": old_key.space_id},
            )
            valid_sids = {row[0] for row in result.fetchall()}
        dropped_service_ids = sorted(set(old_sids) - valid_sids)
        filtered_mappings = [(sid, pl) for sid, pl in old_mappings if sid in valid_sids]

        old_write_grants = old_key.write_grants

        new_name = name if name is not None else old_key.name
        new_expires_at = (
            expires_at if expires_at is not None
            else datetime.now(timezone.utc) + timedelta(days=365)
        )
        created_by = self._extract_username(authorization_header)

        plaintext = generate_mcp_key()
        key_prefix = plaintext[4:12]
        key_hash_value = hash_mcp_key(plaintext)
        new_id = uuid.uuid4().hex

        new_key = McpKey(
            id=new_id,
            tenant_id=tenant_id,
            name=new_name,
            note=old_key.note,
            space_id=old_key.space_id,
            key_prefix=key_prefix,
            key_hash=key_hash_value,
            write_grants=old_write_grants,
            created_by=created_by,
            created_at=datetime.now(timezone.utc),
            expires_at=new_expires_at,
        )

        # 写入新 Key + hash 碰撞重试（savepoint 避免 aborted transaction）
        sp = await self.session.begin_nested()
        try:
            self.session.add(new_key)
            await self.session.flush()
            await sp.commit()
        except IntegrityError:
            await sp.rollback()
            logger.warning("MCP Key recreate hash 碰撞，重试生成")
            self.session.expunge(new_key)
            plaintext = generate_mcp_key()
            key_prefix = plaintext[4:12]
            key_hash_value = hash_mcp_key(plaintext)
            new_key = McpKey(
                id=new_id,
                tenant_id=tenant_id,
                name=new_name,
                note=old_key.note,
                space_id=old_key.space_id,
                key_prefix=key_prefix,
                key_hash=key_hash_value,
                write_grants=old_write_grants,
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

        # 继承授权：写入新 Key 的 service 映射（过滤后）
        await mapping_repo.set_mappings(new_id, filtered_mappings)

        logger.info(f"MCP Key 已重新创建: old_id={key_id}, new_id={new_id}")

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_key.recreate",
                resource="mcp_key",
                resource_id=new_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 重新创建审计日志写入失败（不阻塞业务）")

        # 查询最终映射用于响应
        final_mappings = await mapping_repo.get_mappings(new_id)
        service_permissions_dicts = [
            {"service_id": sid, "permission_level": pl}
            for sid, pl in final_mappings
        ]

        return {
            "id": new_id,
            "plaintext": plaintext,
            "name": new_name,
            "key_prefix": key_prefix,
            "space_id": old_key.space_id,
            "service_permissions": service_permissions_dicts,
            "write_grants": old_write_grants,
            "status": McpKey._derive_status(new_key),
            "auth_summary": self._build_auth_summary(service_permissions_dicts, old_write_grants),
            "write_scope_summary": self._build_write_scope_summary(old_write_grants),
            "mcp_config": self._build_mcp_config(plaintext),
            "delivery_failed": False,
            "dropped_service_ids": dropped_service_ids,
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
                resource="mcp_key",
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
        支持更新 name、space_id、service_permissions、expires_at、write_grants。
        write_grants 用 __fields_set__ 区分「未传」（保留原值）与「显式 null」（关闭写入）。
        返回更新后的 McpKeyResponse DTO（含 service_permissions）。
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
        if req.note is not None:
            key.note = req.note
        if req.space_id is not None:
            await self._validate_space_id(tenant_id, req.space_id)
            if req.space_id != key.space_id:
                # 变更 space：现有 service 映射必须仍属于新 space，否则拒绝跨空间越权
                existing_mappings = await McpKeyServiceMappingRepository(
                    self.session
                ).get_mappings(key_id)
                existing_sids = [sid for sid, _pl in existing_mappings]
                if existing_sids:
                    await self._validate_service_ids_in_space(
                        tenant_id, req.space_id, existing_sids
                    )
                # 变更 space：若本次未同步更新 write_grants，现有 write_grants 的
                # 知识库必须仍属于新 space，否则拒绝跨空间写授权泄露（对称于 service 映射校验）
                if key.write_grants and "write_grants" not in req.__fields_set__:
                    existing_kb_ids = list(
                        {g.get("kb") for g in key.write_grants if g and g.get("kb")}
                    )
                    await self._validate_kb_ids_in_space(
                        tenant_id, req.space_id, existing_kb_ids
                    )
            key.space_id = req.space_id
        if "expires_at" in req.__fields_set__:
            key.expires_at = req.expires_at
        if "write_grants" in req.__fields_set__:
            # 显式传了（含 null）：null 关闭写入；非空才重跑三态/跨 schema 校验
            if req.write_grants is None:
                key.write_grants = None
            else:
                kb_ids = list({g.kb for g in req.write_grants})
                if not kb_ids:
                    raise InvalidParameterError(
                        message="写入开启但无知识库（write_grants 不能为空列表）",
                        details={"key_id": key_id},
                    )
                self._validate_grant_modes(req.write_grants)
                await self._validate_kb_ids_in_space(tenant_id, key.space_id, kb_ids)
                await self._validate_directory_ownership(tenant_id, kb_ids, req.write_grants)
                key.write_grants = [g.dict() for g in req.write_grants]

        await self.session.flush()

        # service_permissions 全量替换（set_mappings）
        if req.service_permissions is not None:
            mapping_repo = McpKeyServiceMappingRepository(self.session)
            mappings = [
                (item.service_id, item.permission_level)
                for item in req.service_permissions
            ]
            new_sids = [sid for sid, _pl in mappings]
            if new_sids and key.space_id:
                await self._validate_service_ids_in_space(
                    tenant_id, key.space_id, new_sids
                )
            await self._validate_service_publish(tenant_id, new_sids)
            await mapping_repo.set_mappings(key_id, mappings)

        # 最终态复查：更新后 Key 至少需保留一个已发布服务授权或写入授权
        final_write_grants = key.write_grants
        if req.service_permissions is not None:
            final_service_permissions = req.service_permissions
        else:
            final_service_permissions = await McpKeyServiceMappingRepository(
                self.session
            ).get_mappings(key_id)
        if not final_write_grants and not final_service_permissions:
            raise InvalidParameterError(
                message="更新后 Key 至少需保留一个已发布服务授权（service_permissions）或写入授权（write_grants）",
                details={"key_id": key_id},
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
                resource="mcp_key",
                resource_id=key_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP Key 更新审计日志写入失败（不阻塞业务）")

        # 构建响应 DTO（含 service_permissions 查询）
        dto = McpKeyResponse.from_orm(key)
        mapping_repo = McpKeyServiceMappingRepository(self.session)
        mappings = await mapping_repo.get_mappings(key_id)
        dto.service_permissions = [
            {"service_id": sid, "permission_level": pl}
            for sid, pl in mappings
        ]
        dto.status = McpKey._derive_status(key)
        dto.auth_summary = self._build_auth_summary(dto.service_permissions, dto.write_grants)
        dto.write_scope_summary = self._build_write_scope_summary(dto.write_grants)
        return dto
