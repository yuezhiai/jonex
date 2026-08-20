"""领域服务 API Key 管理服务（v1.4 Phase 15 DS-04）。

安全契约（SDD）：
- 明文仅在创建响应一次性返回，数据库只存 key_hash（绝不存明文）；
- 列表/查询脱敏（仅 key_prefix + 状态，不返回 key_hash/明文）；
- 租户隔离：require_tenant()，body 禁带 tenant_id，service 存在性经跨 schema 校验；
- 撤销用 revoked_at 软撤销；状态派生：revoked / expired / active。
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.dtos.mcp_service_api_key_dto import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyResponse,
)
from capabilities.platform.models.mcp_service_api_key import McpServiceApiKey
from capabilities.platform.repository.mcp_service_api_key_repository import (
    McpServiceApiKeyRepository,
)
from capabilities.platform.repository.mcp_service_repository import McpServiceRepository
from jonex_core.common.crypto import generate_mcp_key, hash_mcp_key
from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

logger = logging.getLogger(__name__)


class McpServiceApiKeyService:
    """领域服务 API Key 业务逻辑服务。"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = McpServiceApiKeyRepository(session)
        self.mcp_service_repo = McpServiceRepository(session)

    @staticmethod
    def _determine_key_status(revoked_at, expires_at) -> str:
        """派生 API Key 状态。

        优先级：
        1. revoked_at 非空 → "revoked"
        2. expires_at 非空且已过期 → "expired"
        3. 否则 → "active"
        """
        if revoked_at is not None:
            return "revoked"
        if expires_at is not None:
            now = datetime.now(timezone.utc)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < now:
                return "expired"
        return "active"

    async def _assert_service_exists(self, tenant_id: str, service_id: str) -> None:
        """校验 service 在 knowledge_base.services 中存在且属于当前租户。

        复用 McpServiceRepository.get_domain_services（跨 schema 只读），
        避免跨 service 私有方法调用。
        """
        domain_services = await self.mcp_service_repo.get_domain_services(tenant_id)
        if service_id not in domain_services:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_service_api_key.not_found",
                    fallback="MCP 领域服务不存在: {service_id}",
                    params={"service_id": service_id},
                ),
                details={"service_id": service_id},
            )

    async def create(
        self,
        tenant_id: str,
        service_id: str,
        req: ApiKeyCreateRequest,
        authorization_header: str | None = None,
    ) -> ApiKeyCreateResponse:
        """创建领域服务 API Key，一次性返回明文 yxm_ Key。

        明文仅在返回值中出现一次，数据库只存 key_hash。
        IntegrityError（key_hash 唯一碰撞）时重试一次。
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        plaintext = generate_mcp_key()
        key_prefix = plaintext[4:12]  # yxm_ 后 8 位
        key_hash_value = hash_mcp_key(plaintext)
        key_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)

        api_key = McpServiceApiKey(
            id=key_id,
            tenant_id=tenant_id,
            service_id=service_id,
            name=req.name,
            key_prefix=key_prefix,
            key_hash=key_hash_value,
            expires_at=req.expires_at,
            created_at=created_at,
        )

        sp = await self.session.begin_nested()
        try:
            self.session.add(api_key)
            await self.session.flush()
            await sp.commit()
        except IntegrityError:
            await sp.rollback()
            logger.warning("领域服务 API Key hash 碰撞，重试生成")
            self.session.expunge(api_key)
            plaintext = generate_mcp_key()
            key_prefix = plaintext[4:12]
            key_hash_value = hash_mcp_key(plaintext)
            api_key = McpServiceApiKey(
                id=key_id,
                tenant_id=tenant_id,
                service_id=service_id,
                name=req.name,
                key_prefix=key_prefix,
                key_hash=key_hash_value,
                expires_at=req.expires_at,
                created_at=created_at,
            )
            sp2 = await self.session.begin_nested()
            try:
                self.session.add(api_key)
                await self.session.flush()
                await sp2.commit()
            except IntegrityError:
                await sp2.rollback()
                raise

        logger.info(
            "领域服务 API Key 已创建: id=%s, service_id=%s, name=%s",
            key_id,
            service_id,
            req.name,
        )

        return ApiKeyCreateResponse(
            id=key_id,
            plaintext=plaintext,
            service_id=service_id,
            name=req.name,
            key_prefix=key_prefix,
            created_at=created_at,
            expires_at=req.expires_at,
        )

    async def list(
        self, tenant_id: str, service_id: str
    ) -> list[ApiKeyResponse]:
        """查询某领域服务的 API Key 列表（脱敏，不暴露 key_hash）。"""
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        keys = await self.repo.list_by_service(tenant_id, service_id)

        result: list[ApiKeyResponse] = []
        for k in keys:
            result.append(
                ApiKeyResponse(
                    id=k.id,
                    service_id=k.service_id,
                    name=k.name,
                    key_prefix=k.key_prefix,
                    status=self._determine_key_status(k.revoked_at, k.expires_at),
                    created_at=k.created_at,
                    expires_at=k.expires_at,
                    revoked_at=k.revoked_at,
                )
            )
        return result

    async def revoke(self, tenant_id: str, service_id: str, key_id: str) -> None:
        """撤销领域服务 API Key（设置 revoked_at）。

        幂等——对已撤销 Key 再次调用不报错。
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        ok = await self.repo.revoke(key_id, tenant_id, service_id)
        if not ok:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_service_api_key.not_found",
                    fallback="领域服务 API Key 不存在: {key_id}",
                    params={"key_id": key_id},
                ),
                details={"key_id": key_id, "service_id": service_id},
            )

        logger.info(
            "领域服务 API Key 已撤销: id=%s, service_id=%s", key_id, service_id
        )
