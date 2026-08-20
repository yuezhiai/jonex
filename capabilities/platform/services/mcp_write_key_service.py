"""知识写入 Key 管理服务（v1.4 Phase 17 WRITE-02）。

安全契约（SDD）：
- 明文仅在创建响应一次性返回，数据库只存 key_hash（HMAC-SHA256 单向，绝不存明文）；
- 列表/查询脱敏（仅 key_prefix + 派生 status，不返回 key_hash / 明文）；
- 租户隔离：require_tenant() + 跨 schema sa.text() 查询均带 tenant_id 过滤；
- grants 写入范围校验：kb 同 space（_validate_kb_ids_in_space）+ directory 归属；
- space_id 由 grants[0].kb 反推、kb_id = grants[0].kb 冗余索引（D-04）；
- 4 态派生（expired > disabled > revoked > active）复用 McpWriteKey._derive_status。
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.dtos.mcp_write_key_dto import (
    WriteGrant,
    WriteKeyCreateRequest,
    WriteKeyCreateResponse,
    WriteKeyResponse,
    WriteKeyUpdateRequest,
)
from capabilities.platform.models.mcp_write_key import McpWriteKey
from capabilities.platform.repository.mcp_write_key_repository import (
    McpWriteKeyRepository,
)
from jonex_core.common.crypto import generate_write_key, hash_mcp_key
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.security.user_auth import get_user_auth

logger = logging.getLogger(__name__)


class McpWriteKeyService:
    """知识写入 Key 业务逻辑服务。"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = McpWriteKeyRepository(session)
        self.user_auth = get_user_auth()

    # ---- helpers ----

    @staticmethod
    def _derive_status(key) -> str:
        """派生 4 态状态（复用 17-01 模型 McpWriteKey._derive_status）。"""
        return McpWriteKey._derive_status(key)

    def _to_response(self, key) -> WriteKeyResponse:
        """构造脱敏响应 DTO（不暴露 key_hash / 明文）。"""
        return WriteKeyResponse(
            id=key.id,
            name=key.name,
            key_prefix=key.key_prefix,
            grants=[WriteGrant(**g) for g in (key.grants or [])],
            space_id=key.space_id,
            kb_id=key.kb_id,
            created_at=key.created_at,
            created_by=key.created_by,
            updated_at=key.updated_at,
            expires_at=key.expires_at,
            disabled_at=key.disabled_at,
            revoked_at=key.revoked_at,
            revoked_by=key.revoked_by,
            status=self._derive_status(key),
        )

    async def _resolve_space_id(self, tenant_id: str, kb_id: str) -> str:
        """反推 space_id（D-04）：grants[0].kb 对应知识库的 space_id。

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
        """校验 grants 的 mode 与 directories 语义一致（WR-03）。

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

    def _extract_username(self, authorization_header: str | None) -> str:
        """从 Authorization Header 提取操作者用户名（JWT 解码，对齐读 Key）。

        写 Key 端点已 require_admin，JWT 必然可用；落真实 username 使审计可追溯。
        兜底 "test_token"（对齐读 Key）——None / 空 / 非 Bearer / 空 token /
        jonex_test_ 前缀 / JWT 解码失败 / username 缺失 均兜底。
        """
        if not authorization_header or not authorization_header.startswith("Bearer "):
            return "test_token"
        token = authorization_header[7:]
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
            logger.warning(
                "JWT decode failed in _extract_username, falling back to test_token: %s",
                e,
            )
            return "test_token"

    # ---- business methods ----

    async def create(
        self,
        tenant_id: str,
        req: WriteKeyCreateRequest,
        authorization_header: str | None = None,
    ) -> WriteKeyCreateResponse:
        """创建知识写入 Key，一次性返回明文 yxm_... Key。

        明文仅在返回值中出现一次，数据库只存 key_hash。
        space_id 由 grants[0].kb 反推；kb_id = grants[0].kb 冗余索引（D-04）。
        IntegrityError（key_hash 唯一碰撞）时重试一次。
        """
        tenant_id = require_tenant(tenant_id)
        operator = self._extract_username(authorization_header)

        kb_ids: list[str] = []
        for g in req.grants:
            if g.kb not in kb_ids:
                kb_ids.append(g.kb)
        if not kb_ids:
            raise InvalidParameterError(message="grants 不能为空", details={})

        self._validate_grant_modes(req.grants)

        # D-04：反推 space_id + kb_id 冗余索引
        space_id = await self._resolve_space_id(tenant_id, kb_ids[0])
        kb_id = kb_ids[0]

        await self._validate_kb_ids_in_space(tenant_id, space_id, kb_ids)
        await self._validate_directory_ownership(tenant_id, kb_ids, req.grants)

        plaintext = generate_write_key()
        key_prefix = "mcpw_" + plaintext[4:8]  # 脱敏展示 mcpw_ + 明文第 5-8 位（运行时明文前缀 yxm_）
        key_hash_value = hash_mcp_key(plaintext)
        key_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)

        entity = McpWriteKey(
            id=key_id,
            tenant_id=tenant_id,
            name=req.name,
            key_prefix=key_prefix,
            key_hash=key_hash_value,
            grants=[g.dict() for g in req.grants],
            space_id=space_id,
            kb_id=kb_id,
            expires_at=req.expires_at,
            created_at=created_at,
            created_by=operator,
        )

        sp = await self.session.begin_nested()
        try:
            self.session.add(entity)
            await self.session.flush()
            await sp.commit()
        except IntegrityError:
            await sp.rollback()
            logger.warning("知识写入 Key hash 碰撞，重试生成")
            self.session.expunge(entity)
            plaintext = generate_write_key()
            key_prefix = "mcpw_" + plaintext[4:8]
            key_hash_value = hash_mcp_key(plaintext)
            entity = McpWriteKey(
                id=key_id,
                tenant_id=tenant_id,
                name=req.name,
                key_prefix=key_prefix,
                key_hash=key_hash_value,
                grants=[g.dict() for g in req.grants],
                space_id=space_id,
                kb_id=kb_id,
                expires_at=req.expires_at,
                created_at=created_at,
                created_by=operator,
            )
            sp2 = await self.session.begin_nested()
            try:
                self.session.add(entity)
                await self.session.flush()
                await sp2.commit()
            except IntegrityError:
                await sp2.rollback()
                raise

        logger.info(
            "知识写入 Key 已创建: id=%s, name=%s, operator=%s",
            key_id, req.name, operator,
        )

        return WriteKeyCreateResponse(
            id=key_id,
            plaintext=plaintext,
            name=req.name,
            key_prefix=key_prefix,
            grants=[WriteGrant(**g) for g in (entity.grants or [])],
            space_id=space_id,
            kb_id=kb_id,
            created_at=created_at,
            created_by=operator,
            expires_at=req.expires_at,
        )

    async def list_all(self, tenant_id: str) -> list[WriteKeyResponse]:
        """查询租户下所有知识写入 Key（脱敏，含已撤销，不暴露 key_hash）。"""
        require_tenant(tenant_id)
        keys = await self.repo.list_by_tenant(tenant_id)
        return [self._to_response(k) for k in keys]

    async def get_by_id(self, tenant_id: str, key_id: str) -> WriteKeyResponse:
        """查询单个知识写入 Key（脱敏），不存在/跨租户 → ResourceNotFoundError。"""
        require_tenant(tenant_id)
        key = await self.repo.get_by_id(key_id, tenant_id)
        if key is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_write_key.not_found",
                    fallback=f"知识写入 Key 不存在: {key_id}",
                ),
                details={"key_id": key_id},
            )
        return self._to_response(key)

    async def update(
        self,
        tenant_id: str,
        key_id: str,
        req: WriteKeyUpdateRequest,
        authorization_header: str | None = None,
    ) -> WriteKeyResponse:
        """原位编辑知识写入 Key（不重置 Key，不生成新明文）。

        所有字段可选——不传则保持原值。grants 变更时重新校验写入范围。
        """
        require_tenant(tenant_id)
        operator = self._extract_username(authorization_header)
        key = await self.repo.get_by_id(key_id, tenant_id)
        if key is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_write_key.not_found",
                    fallback=f"知识写入 Key 不存在: {key_id}",
                ),
                details={"key_id": key_id},
            )

        if req.name is not None:
            key.name = req.name.strip()
        if "expires_at" in req.__fields_set__:
            key.expires_at = req.expires_at
        if req.grants is not None:
            kb_ids: list[str] = []
            for g in req.grants:
                if g.kb not in kb_ids:
                    kb_ids.append(g.kb)
            if not kb_ids:
                raise InvalidParameterError(message="grants 不能为空", details={})

            self._validate_grant_modes(req.grants)

            space_id = await self._resolve_space_id(tenant_id, kb_ids[0])
            await self._validate_kb_ids_in_space(tenant_id, space_id, kb_ids)
            await self._validate_directory_ownership(tenant_id, kb_ids, req.grants)

            key.grants = [g.dict() for g in req.grants]
            key.space_id = space_id
            key.kb_id = kb_ids[0]

        await self.session.flush()
        logger.info("知识写入 Key 已更新: id=%s, operator=%s", key_id, operator)
        return self._to_response(key)

    async def toggle(
        self,
        tenant_id: str,
        key_id: str,
        authorization_header: str | None = None,
    ) -> WriteKeyResponse:
        """停用/启用知识写入 Key（可逆，key 不变）。

        - 停用：置 disabled_at = now（授权保留，可逆）。
        - 启用：清 disabled_at = None（恢复原 Key）。
        - 已撤销 Key 调 toggle → ResourceConflictError（撤销不可逆）。
        """
        require_tenant(tenant_id)
        operator = self._extract_username(authorization_header)
        key = await self.repo.get_by_id(key_id, tenant_id)
        if key is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_write_key.not_found",
                    fallback=f"知识写入 Key 不存在: {key_id}",
                ),
                details={"key_id": key_id},
            )

        if key.revoked_at is not None:
            raise ResourceConflictError(
                message="已撤销的 Key 不可停用/启用", details={"key_id": key_id}
            )

        if key.disabled_at is not None:
            key.disabled_at = None
        else:
            key.disabled_at = datetime.now(timezone.utc)

        await self.session.flush()
        logger.info("知识写入 Key 状态已切换: id=%s, operator=%s", key_id, operator)
        return self._to_response(key)

    async def revoke(
        self,
        tenant_id: str,
        key_id: str,
        authorization_header: str | None = None,
    ) -> None:
        """撤销知识写入 Key（设置 revoked_at，不可逆）。

        幂等——对已撤销 Key 再次调用不报错（repo 内 UPDATE 置同值 rowcount 仍 > 0）。
        """
        require_tenant(tenant_id)
        operator = self._extract_username(authorization_header)
        ok = await self.repo.revoke(key_id, tenant_id, revoked_by=operator)
        if not ok:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_write_key.not_found",
                    fallback=f"知识写入 Key 不存在: {key_id}",
                ),
                details={"key_id": key_id, "tenant_id": tenant_id},
            )
        logger.info("知识写入 Key 已撤销: id=%s, operator=%s", key_id, operator)
