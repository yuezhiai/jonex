"""
MCP 服务目录 Service —— 业务逻辑层。

提供 6 个业务方法：
- list_services：跨 schema 聚合查询服务列表
- publish / unpublish：管理发布状态
- test_call：通过 Sidecar invoke 调用知识库 RAG 检索
- get_authorized_keys：查询已授权 Key 列表
- sync：从 knowledge_base.services 同步服务到 platform.mcp_service_publish
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.dtos.mcp_service_dto import (
    ApiAccessInfo,
    AuthorizedKeyItem,
    AuthorizedKeyListResponse,
    McpAccessInfo,
    McpServiceDetailResponse,
    McpServiceListRequest,
    McpServiceListResponse,
    McpServiceResponse,
    ServiceAccessInfo,
    ToolConfigRequest,
)
from capabilities.platform.models.mcp_key import McpKey
from capabilities.platform.repository.mcp_service_repository import (
    McpServiceRepository,
    SYSTEM_SERVICE_DESCRIPTION,
    SYSTEM_SERVICE_ID,
    SYSTEM_SERVICE_NAME,
    SYSTEM_SERVICE_TOOL,
)
from jonex_core.common import get_config
from jonex_core.common.exceptions import (
    OperationNotSupportedError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.security.user_auth import get_user_auth

logger = logging.getLogger(__name__)


class McpServiceService:
    """MCP 服务目录业务逻辑服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = McpServiceRepository(session)
        self.user_auth = get_user_auth()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    @staticmethod
    def _derive_publish_status(is_published: bool, stopped_at) -> str:
        """派生发布态（published/unpublished/stopped）——响应 status 字段唯一语义。

        与 Repository 的 status_filter 语义一致：
        - is_published 假 → unpublished（未发布）
        - is_published 真且 stopped_at 非空 → stopped（已停用）
        - 其余 → published（已发布运行中）
        """
        if not is_published:
            return "unpublished"
        if stopped_at is not None:
            return "stopped"
        return "published"

    def _build_system_service_item(self, row) -> McpServiceResponse:
        """构造系统服务目录条目（固定 tool + 抽象描述 + 发布状态恒 published）。"""
        return McpServiceResponse(
            id=SYSTEM_SERVICE_ID,
            name=SYSTEM_SERVICE_NAME,
            description=None,
            domain_type=None,
            space_id="",
            space_name="",
            status="published",
            enabled=1,
            kb_count=0,
            kb_names=[],
            is_published=True,
            published_at=None,
            published_by=None,
            last_call_at=None,
            created_at=row.created_at,
            tool=SYSTEM_SERVICE_TOOL,
            tool_description=SYSTEM_SERVICE_DESCRIPTION,
            service_type="system",
            capability_type="write",
            source="platform",
            default_scope="by_key",
        )

    async def _assert_service_exists(self, tenant_id: str, service_id: str) -> None:
        """校验 service 在 knowledge_base.services 中存在且属于当前租户。

        不存在时抛出 ResourceNotFoundError。
        """
        domain_services = await self.repo.get_domain_services(tenant_id)
        if service_id not in domain_services:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_service.not_found",
                    fallback="MCP 领域服务不存在: {service_id}",
                    params={"service_id": service_id},
                ),
                details={"service_id": service_id},
            )

    # ------------------------------------------------------------------
    # Business methods
    # ------------------------------------------------------------------

    async def list_services(
        self, tenant_id: str, req: McpServiceListRequest
    ) -> McpServiceListResponse:
        """查询服务目录列表（跨 schema 聚合）。

        筛选参数通过 req.search / req.space_id / req.capability_type / req.source /
        req.status 传入。领域服务条目赋 capability_type='domain'、source=领域空间名；
        内置「知识写入」条目 capability_type='write'、source='platform'。
        """
        tenant_id = require_tenant(tenant_id)
        capability_type = getattr(req, "capability_type", None)
        source = getattr(req, "source", None)
        space_id = getattr(req, "space_id", None)
        status_filter = getattr(req, "status", None)

        items: list[McpServiceResponse] = []

        # 领域服务：capability_type='write'（仅知识写入）或 source='platform'
        # （仅平台内置）时跳过领域聚合查询。
        include_domain = capability_type != "write" and source != "platform"
        if include_domain:
            rows = await self.repo.list_services(
                tenant_id,
                search=req.search,
                space_id=space_id,
                source=source,
                status_filter=status_filter,
            )

            # DS-05 / K10：列表时兜底 upsert stub。对 knowledge_base 存在但发布表
            # 无桩记录的服务，补齐 is_published=0 桩，保证前端移除「同步服务」按钮后
            # 未发布服务仍可见且可被 publish/unpublish 命中改写。
            service_ids = [row.get("id") for row in rows if row.get("id")]
            await self.repo.ensure_publish_stubs(tenant_id, service_ids)

            for row in rows:
                # ⚠️ LEFT JOIN 软删除空间/脏数据时 sp.name、s.space_id 等可能为 NULL。
                # dict.get(key, default) 只在 key 不存在时回退，key 存在但值为 None 仍返回 None。
                # 必填 str 字段（如 space_id/name/status）收到 None 会触发 Pydantic 校验 → 500。
                # 用 `or` 同时兜底缺失 key 和 None 值。
                item = McpServiceResponse(
                    id=row.get("id") or "",
                    name=row.get("name") or "",
                    description=row.get("description"),
                    domain_type=row.get("domain_type"),
                    space_id=row.get("space_id") or "",
                    space_name=row.get("space_name") or "",
                    status=self._derive_publish_status(
                        bool(row.get("is_published") or 0), row.get("stopped_at")
                    ),
                    enabled=int(row["enabled"]) if row.get("enabled") is not None else 0,
                    kb_count=row.get("kb_count") or 0,
                    kb_names=row.get("kb_names") or [],
                    is_published=bool(row.get("is_published") or 0),
                    published_at=row.get("published_at"),
                    published_by=row.get("published_by"),
                    stopped_at=row.get("stopped_at"),
                    stopped_by=row.get("stopped_by"),
                    created_at=row.get("created_at"),
                    tool=row.get("tool"),
                    tool_description=row.get("tool_description"),
                    service_type=row.get("service_type") or "domain",
                    capability_type="domain",
                    source=row.get("space_name") or "",
                    default_scope=f"{row.get('kb_count') or 0} 个知识库",
                )
                items.append(item)

        # WRITE-03 / DIR-02：系统服务条目懒 seed + 目录可见。
        # 固定内置条目不受 search 过滤；无 space 归属 → space_id 过滤时排除；
        # 已发布条目 → unpublished 视图排除；capability_type='domain' 或
        # source 指定领域空间名时也排除（系统条目 source 恒 'platform'）。
        include_system = (
            capability_type != "domain"
            and space_id is None
            and (source is None or source == "platform")
            and status_filter not in ("unpublished", "stopped")
        )
        if include_system:
            system_row = await self.repo.ensure_system_service(tenant_id)
            items.append(self._build_system_service_item(system_row))

        return McpServiceListResponse(items=items, total=len(items))

    async def get_service_detail(
        self, tenant_id: str, service_id: str
    ) -> McpServiceDetailResponse:
        """查询单个领域服务详情（跨 schema 聚合）。

        系统条目（system.knowledge_document_write）特殊路由：不查
        knowledge_base.services，直接返回内置条目，避免 404。

        服务不存在或不属于当前租户时抛 ResourceNotFoundError。
        """
        tenant_id = require_tenant(tenant_id)

        if service_id == SYSTEM_SERVICE_ID:
            row = await self.repo.ensure_system_service(tenant_id)
            item = self._build_system_service_item(row)
            return McpServiceDetailResponse(
                **item.dict(),
                kb_ids=[],
                updated_at=None,
                access=ServiceAccessInfo(
                    api=None,
                    mcp=McpAccessInfo(
                        transport="streamable-http",
                        tool_name=SYSTEM_SERVICE_TOOL,
                        auth_scheme="bearer",
                        server_url=get_config().MCP_SERVER_PUBLIC_URL,
                    ),
                ),
            )

        row = await self.repo.get_service_detail(tenant_id, service_id)
        if not row:
            raise ResourceNotFoundError(
                message=translate(
                    "err.mcp_service.not_found",
                    fallback="MCP 领域服务不存在: {service_id}",
                    params={"service_id": service_id},
                ),
                details={"service_id": service_id},
            )

        return McpServiceDetailResponse(
            id=row.get("id") or "",
            name=row.get("name") or "",
            description=row.get("description"),
            domain_type=row.get("domain_type"),
            space_id=row.get("space_id") or "",
            space_name=row.get("space_name") or "",
            status=self._derive_publish_status(
                bool(row.get("is_published") or 0), row.get("stopped_at")
            ),
            kb_count=row.get("kb_count") or 0,
            kb_names=row.get("kb_names") or [],
            kb_ids=row.get("kb_ids") or [],
            is_published=bool(row.get("is_published") or 0),
            published_at=row.get("published_at"),
            published_by=row.get("published_by"),
            stopped_at=row.get("stopped_at"),
            stopped_by=row.get("stopped_by"),
            last_call_at=row.get("last_call_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            enabled=int(row.get("enabled")) if row.get("enabled") is not None else 1,
            tool=row.get("tool"),
            tool_description=row.get("tool_description"),
            service_type=row.get("service_type") or "domain",
            access=ServiceAccessInfo(
                api=ApiAccessInfo(
                    endpoint=f"/api/v1/domain-services/{service_id}/query",
                    method="POST",
                    auth="api-key",
                    sample_payload={"query": "..."},
                ),
                mcp=McpAccessInfo(
                    transport="streamable-http",
                    tool_name=row.get("tool"),
                    auth_scheme="bearer",
                    server_url=get_config().MCP_SERVER_PUBLIC_URL,
                ),
            ),
        )

    async def get_authorized_keys(
        self, tenant_id: str, service_id: str
    ) -> AuthorizedKeyListResponse:
        """查询领域服务已授权的 MCP Key 列表。

        - 系统服务（knowledge_document_write）：写能力由 write_grants（KB 级）授权，
          不走 service_permissions 映射，本端点天然无数据 → 返回空列表，不打 404。
        - 领域服务：join mcp_keys + mcp_key_service_mappings，仅未软删、当前租户的 Key，
          status 派生 active/revoked/expired/disabled。
        """
        tenant_id = require_tenant(tenant_id)

        if service_id == SYSTEM_SERVICE_ID:
            return AuthorizedKeyListResponse(items=[], total=0)

        await self._assert_service_exists(tenant_id, service_id)

        rows = await self.repo.get_authorized_keys(tenant_id, service_id)
        items = [
            AuthorizedKeyItem(
                key_id=key.id,
                key_name=key.name,
                key_prefix=key.key_prefix,
                permission_level=permission_level,
                key_status=McpKey._derive_status(key),
                expires_at=key.expires_at,
                created_at=key.created_at,
            )
            for key, permission_level in rows
        ]
        return AuthorizedKeyListResponse(items=items, total=len(items))

    async def save_tool_config(
        self, tenant_id: str, service_id: str, req: ToolConfigRequest
    ) -> dict:
        """保存领域服务的 MCP Tool 配置（DS-03）。

        1. 校验 service 在 knowledge_base.services 中存在且属于当前租户
        2. 同租户内 Tool 名唯一性校验（跨租户同名不冲突）
        3. 落库 platform.mcp_service_publish（tool / tool_description）
        """
        tenant_id = require_tenant(tenant_id)
        if service_id == SYSTEM_SERVICE_ID:
            raise OperationNotSupportedError(
                message=translate(
                    "err.mcp_service.system_tool_immutable",
                    fallback="系统服务 Tool 不可编辑",
                    params={"service_id": service_id},
                ),
                details={"service_id": service_id},
            )
        await self._assert_service_exists(tenant_id, service_id)

        existing = await self.repo.get_by_tool(tenant_id, req.tool)
        if existing is not None and existing.service_id != service_id:
            raise ResourceConflictError(
                message=translate(
                    "err.mcp_service.tool_conflict",
                    fallback="Tool 名称已存在: {tool}",
                    params={"tool": req.tool},
                ),
                details={"tool": req.tool, "service_id": existing.service_id},
            )

        record = await self.repo.save_tool_config(
            tenant_id, service_id, req.tool, req.tool_description
        )
        await self.session.flush()

        return {
            "service_id": service_id,
            "tool": record.tool,
            "tool_description": record.tool_description,
        }

    async def publish(
        self, tenant_id: str, service_id: str, authorization_header: str | None = None
    ) -> dict:
        """发布领域服务。

        1. 校验 service 在 knowledge_base.services 中存在且属于该租户
        2. 校验该 service 已保存 Tool 配置（tool 非空），否则拒绝发布（ResourceConflictError 409）
        3. upsert platform.mcp_service_publish（is_published=1）
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        publish_record = await self.repo.get_publish_status(tenant_id, service_id)
        if publish_record is None or not publish_record.tool:
            raise ResourceConflictError(
                message=translate(
                    "err.mcp_service.tool_not_saved",
                    fallback="请先保存 Tool 配置",
                ),
                details={"service_id": service_id},
            )

        username = self._extract_username(authorization_header)
        record = await self.repo.upsert_publish(
            tenant_id, service_id, published_by=username, is_published=1
        )
        await self.session.flush()

        logger.info("MCP service published: service_id=%s, by=%s", service_id, username)

        return {
            "service_id": service_id,
            "is_published": True,
            "published_at": record.published_at,
            "published_by": username,
        }

    async def unpublish(
        self, tenant_id: str, service_id: str, authorization_header: str | None = None
    ) -> dict:
        """取消发布领域服务。

        1. 校验 service 在 knowledge_base.services 中存在且属于该租户
        2. upsert platform.mcp_service_publish（is_published=0）
        3. 若为 stopped 态一并清 stopped_at/stopped_by（PRD §5.1）
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        username = self._extract_username(authorization_header)
        record = await self.repo.upsert_publish(
            tenant_id, service_id, published_by=username, is_published=0
        )
        if record.stopped_at is not None:
            record.stopped_at = None
            record.stopped_by = None
        await self.session.flush()

        logger.info("MCP service unpublished: service_id=%s, by=%s", service_id, username)

        return {"service_id": service_id, "is_published": False}

    async def stop(
        self, tenant_id: str, service_id: str, authorization_header: str | None = None
    ) -> dict:
        """停用已发布服务（仅 published 态可用）。

        1. 校验 service 在 knowledge_base.services 中存在且属于该租户
        2. 置 stopped_at/stopped_by；未发布/已停用 → ResourceConflictError(409)
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        username = self._extract_username(authorization_header)
        record = await self.repo.stop_service(tenant_id, service_id, stopped_by=username)
        if record is None:
            raise ResourceConflictError(
                message=translate(
                    "err.mcp_service.stop_conflict",
                    fallback="仅已发布服务可停用",
                ),
                details={"service_id": service_id},
            )
        await self.session.flush()

        logger.info("MCP service stopped: service_id=%s, by=%s", service_id, username)

        return {
            "service_id": service_id,
            "is_published": True,
            "stopped": True,
            "stopped_at": record.stopped_at,
            "stopped_by": record.stopped_by,
        }

    async def start(
        self, tenant_id: str, service_id: str, authorization_header: str | None = None
    ) -> dict:
        """启用已停用服务（仅 stopped 态可用）。

        1. 校验 service 在 knowledge_base.services 中存在且属于该租户
        2. 清 stopped_at/stopped_by；未停用/未发布 → ResourceConflictError(409)
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        record = await self.repo.start_service(tenant_id, service_id)
        if record is None:
            raise ResourceConflictError(
                message=translate(
                    "err.mcp_service.start_conflict",
                    fallback="仅已停用服务可启用",
                ),
                details={"service_id": service_id},
            )
        await self.session.flush()

        logger.info("MCP service started: service_id=%s", service_id)

        return {
            "service_id": service_id,
            "is_published": True,
            "stopped": False,
            "stopped_at": None,
            "stopped_by": None,
        }
