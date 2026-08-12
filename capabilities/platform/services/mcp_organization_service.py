"""
MCP 组织管理服务。
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import ResourceNotFoundError, InvalidParameterError
from jonex_core.common.tenant import require_tenant
from jonex_core.security.user_auth import get_user_auth
from capabilities.platform.models.mcp_key import McpOrganization
from capabilities.platform.repository.mcp_organization_repository import McpOrganizationRepository
from capabilities.platform.dtos.mcp_key_dto import (
    McpOrganizationCreateRequest,
    McpOrganizationResponse,
    McpOrganizationUpdateRequest,
)

logger = logging.getLogger(__name__)


class McpOrganizationService:
    """MCP 组织管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = McpOrganizationRepository(session)
        self.user_auth = get_user_auth()

    # ---- helpers ----

    def _extract_username(self, authorization_header: str | None) -> str:
        """从 Authorization Header 提取用户名，处理所有边界情况。

        与 McpKeyService._extract_username 同逻辑。
        """
        if not authorization_header:
            return "test_token"

        if not authorization_header.startswith("Bearer "):
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
                "JWT decode failed in _extract_username, falling back to test_token: %s", e
            )
            return "test_token"

    # ---- business methods ----

    async def create(
        self,
        tenant_id: str,
        req: McpOrganizationCreateRequest,
        authorization_header: str | None = None,
    ) -> McpOrganizationResponse:
        """创建 MCP 组织。

        name 在 validator 中已 strip，空字符串已被拒绝。
        UNIQUE(tenant_id, name) 冲突时抛出 InvalidParameterError。
        """
        tenant_id = require_tenant(tenant_id)
        created_by = self._extract_username(authorization_header)

        org = McpOrganization(
            id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            name=req.name,
            description=req.description,
            created_at=datetime.now(timezone.utc),
        )

        self.session.add(org)
        try:
            await self.session.flush()
        except IntegrityError:
            raise InvalidParameterError(
                message="组织名称已存在",
                details={"name": req.name},
            )

        logger.info(
            "MCP 组织已创建: id=%s, name=%s, tenant_id=%s",
            org.id, org.name, tenant_id,
        )

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_organization.create",
                resource="MCP_ORGANIZATION",
                resource_id=org.id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP 组织创建审计日志写入失败（不阻塞业务）")

        return McpOrganizationResponse.from_orm(org)

    async def list_all(self, tenant_id: str) -> list[McpOrganizationResponse]:
        """全量返回租户下所有组织，按创建时间降序。"""
        tenant_id = require_tenant(tenant_id)
        orgs = await self.repo.list_by_tenant(tenant_id)
        return [McpOrganizationResponse.from_orm(o) for o in orgs]

    async def update(
        self,
        tenant_id: str,
        org_id: str,
        req: McpOrganizationUpdateRequest,
        authorization_header: str | None = None,
    ) -> McpOrganizationResponse:
        """编辑组织名称或描述。

        get_by_id 使用 BaseRepository 的过滤（is_deleted=0, tenant_id 匹配）。
        不存在时抛出 ResourceNotFoundError。
        """
        tenant_id = require_tenant(tenant_id)
        created_by = self._extract_username(authorization_header)

        org = await self.repo.get_by_id(org_id, tenant_id)
        if org is None:
            raise ResourceNotFoundError(
                message=f"组织不存在: {org_id}",
                details={"org_id": org_id},
            )

        if req.name is not None:
            org.name = req.name
        if req.description is not None:
            org.description = req.description

        try:
            await self.session.flush()
        except IntegrityError:
            raise InvalidParameterError(
                message="组织名称已存在",
                details={"name": req.name},
            )

        logger.info(
            "MCP 组织已更新: id=%s, name=%s", org_id, req.name or org.name,
        )

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_organization.update",
                resource="MCP_ORGANIZATION",
                resource_id=org_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP 组织更新审计日志写入失败（不阻塞业务）")

        return McpOrganizationResponse.from_orm(org)

    async def delete(
        self,
        tenant_id: str,
        org_id: str,
        authorization_header: str | None = None,
    ) -> None:
        """软删除 MCP 组织（设置 is_deleted=1）。

        幂等——对已删除组织再次调用不报错。
        区分"从未存在"（raise ResourceNotFoundError）和"已删除"（静默返回）。
        """
        tenant_id = require_tenant(tenant_id)

        org = await self.repo.get_by_id_include_deleted(org_id, tenant_id)
        if org is None:
            raise ResourceNotFoundError(
                message=f"组织不存在: {org_id}",
                details={"org_id": org_id},
            )

        if org.is_deleted == 1:
            logger.info("MCP 组织已处于软删除状态，幂等跳过: id=%s", org_id)
            return

        org.is_deleted = 1
        await self.session.flush()

        logger.info("MCP 组织已软删除: id=%s", org_id)

        # 审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            created_by = self._extract_username(authorization_header)
            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type="MCP_KEY",
                action="mcp_organization.delete",
                resource="MCP_ORGANIZATION",
                resource_id=org_id,
                username=created_by,
                sync=True,
            )
        except Exception:
            logger.warning("MCP 组织删除审计日志写入失败（不阻塞业务）")
