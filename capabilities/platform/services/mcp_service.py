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
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.dtos.mcp_service_dto import (
    AuthorizedKeyListResponse,
    AuthorizedKeyResponse,
    McpServiceDetailResponse,
    McpServiceListRequest,
    McpServiceListResponse,
    McpServiceResponse,
    TestCallRequest,
    TestCallResponse,
)
from capabilities.platform.repository.mcp_service_repository import McpServiceRepository
from jonex_core.common import get_config
from jonex_core.common.exceptions import ResourceNotFoundError
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
        self._test_call_timeout = float(os.getenv("MCP_TEST_CALL_TIMEOUT", "30"))

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

    def _determine_key_status(self, revoked_at, expires_at) -> str:
        """判断 MCP Key 状态。

        优先级：
        1. revoked_at 非空 → "revoked"
        2. expires_at 非空且已过期 → "expired"
        3. 否则 → "active"
        """
        if revoked_at is not None:
            return "revoked"
        if expires_at is not None:
            # Ensure timezone-aware comparison
            now = datetime.now(timezone.utc)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < now:
                return "expired"
        return "active"

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

        筛选参数通过 req.search / req.space_id / req.status 传入 Repository。
        """
        tenant_id = require_tenant(tenant_id)
        rows = await self.repo.list_services(
            tenant_id,
            search=req.search,
            space_id=req.space_id,
            status_filter=req.status if hasattr(req, "status") else None,
        )

        items: list[McpServiceResponse] = []
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
                status=row.get("status") or "",
                enabled=int(row["enabled"]) if row.get("enabled") is not None else 0,
                kb_count=row.get("kb_count") or 0,
                kb_names=row.get("kb_names") or [],
                is_published=bool(row.get("is_published") or 0),
                published_at=row.get("published_at"),
                published_by=row.get("published_by"),
                created_at=row.get("created_at"),
            )
            items.append(item)

        return McpServiceListResponse(items=items, total=len(items))

    async def get_service_detail(
        self, tenant_id: str, service_id: str
    ) -> McpServiceDetailResponse:
        """查询单个领域服务详情（跨 schema 聚合）。

        服务不存在或不属于当前租户时抛 ResourceNotFoundError。
        """
        tenant_id = require_tenant(tenant_id)
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
            status=row.get("status") or "",
            kb_count=row.get("kb_count") or 0,
            kb_names=row.get("kb_names") or [],
            kb_ids=row.get("kb_ids") or [],
            is_published=bool(row.get("is_published") or 0),
            published_at=row.get("published_at"),
            published_by=row.get("published_by"),
            last_call_at=row.get("last_call_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            enabled=int(row.get("enabled")) if row.get("enabled") is not None else 1,
        )

    async def publish(
        self, tenant_id: str, service_id: str, authorization_header: str | None = None
    ) -> dict:
        """发布领域服务。

        1. 校验 service 在 knowledge_base.services 中存在且属于该租户
        2. upsert platform.mcp_service_publish（is_published=1）
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

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
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        username = self._extract_username(authorization_header)
        await self.repo.upsert_publish(
            tenant_id, service_id, published_by=username, is_published=0
        )
        await self.session.flush()

        logger.info("MCP service unpublished: service_id=%s, by=%s", service_id, username)

        return {"service_id": service_id, "is_published": False}

    async def test_call(
        self, tenant_id: str, service_id: str, req: TestCallRequest
    ) -> TestCallResponse:
        """测试调用领域服务，通过 Sidecar invoke 调用 knowledge_base 的 search_service。

        不抛异常——所有失败（超时/连接拒绝/上游错误）通过 TestCallResponse.success=False 返回，
        因为 test-call 是诊断端点，调用方需要知道"为什么失败"而不仅是报错。
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        config = get_config()
        request_id = uuid.uuid4().hex
        t0 = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=self._test_call_timeout) as client:
                resp = await client.post(
                    f"{config.SIDECAR_URL}/invoke",
                    json={
                        "capability_id": "business.knowledge_base.v1",
                        "payload": {
                            "action": "search_service",
                            "data": {
                                "service_id": service_id,
                                "query": req.query,
                            },
                        },
                        "tenant_id": tenant_id,
                        "request_id": request_id,
                    },
                    headers={
                        "X-API-Key": config.GATEWAY_API_KEY,
                        "X-Tenant-ID": tenant_id,
                    },
                )
                resp.raise_for_status()
                result = resp.json()

                if not result.get("success", False):
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    return TestCallResponse(
                        success=False,
                        answer="",
                        request_id=request_id,
                        latency_ms=latency_ms,
                        error_code="INVOKE_FAILED",
                        error_message=result.get("message", "知识库服务调用失败"),
                    )

                data = result.get("data") or {}
                latency_ms = int((time.monotonic() - t0) * 1000)

                # 从返回数据提取字段
                answer = data.get("answer") or "未找到相关内容"
                kb_names = data.get("kb_names", [])
                # relevance: 从 references 数量估算
                refs = data.get("references", [])
                kb_count = data.get("kb_count", 0)
                if refs and kb_count > 0:
                    total_chunks = sum(len(r.get("locations", [])) for r in refs)
                    relevance = min(round(total_chunks / (kb_count * 3), 2), 1.0)
                else:
                    relevance = 0.0

                return TestCallResponse(
                    success=True,
                    answer=answer,
                    kb_names=kb_names,
                    relevance=relevance,
                    latency_ms=latency_ms,
                    request_id=request_id,
                )

        except httpx.TimeoutException:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return TestCallResponse(
                success=False,
                answer="",
                request_id=request_id,
                latency_ms=latency_ms,
                error_code="TIMEOUT",
                error_message=f"调用超时（{int(self._test_call_timeout)}s）",
            )
        except httpx.HTTPStatusError as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return TestCallResponse(
                success=False,
                answer="",
                request_id=request_id,
                latency_ms=latency_ms,
                error_code="UPSTREAM_ERROR",
                error_message=f"Sidecar 返回 HTTP {e.response.status_code}",
            )
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            msg = str(e)
            if "Connection refused" in msg or "ConnectError" in msg:
                error_code = "CONNECTION_REFUSED"
                error_message = "Sidecar 服务不可达"
            else:
                error_code = "UNKNOWN"
                error_message = f"未知错误：{msg}"
            return TestCallResponse(
                success=False,
                answer="",
                request_id=request_id,
                latency_ms=latency_ms,
                error_code=error_code,
                error_message=error_message,
            )

    async def get_authorized_keys(
        self, tenant_id: str, service_id: str
    ) -> AuthorizedKeyListResponse:
        """查询已授权给指定 service 的 MCP Key 列表。

        JOIN 3 表：mcp_key_service_mappings + mcp_keys + mcp_organizations。
        返回脱敏后的 Key 数据（仅 key_prefix，不暴露 key_hash）。
        """
        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        rows = await self.repo.get_authorized_keys(tenant_id, service_id)

        items: list[AuthorizedKeyResponse] = []
        for row in rows:
            item = AuthorizedKeyResponse(
                key_id=row.key_id,
                key_name=row.key_name,
                key_prefix=row.key_prefix,
                permission_level=row.permission_level,
                org_name=row.org_name,
                org_id=row.org_id,
                key_status=self._determine_key_status(row.revoked_at, row.expires_at),
            )
            items.append(item)

        return AuthorizedKeyListResponse(items=items, total=len(items))

    async def sync(
        self, tenant_id: str, authorization_header: str | None = None
    ) -> dict:
        """从 knowledge_base.services 同步服务列表到 platform.mcp_service_publish。

        只读 knowledge_base.services（不写入），为缺失的服务创建初始发布记录
        （is_published=0）。
        """
        tenant_id = require_tenant(tenant_id)

        domain_services = await self.repo.get_domain_services(tenant_id)
        synced_count = 0

        for service_id in domain_services:
            existing = await self.repo.get_publish_status(tenant_id, service_id)
            if existing is None:
                username = self._extract_username(authorization_header)
                await self.repo.upsert_publish(
                    tenant_id, service_id, published_by=username, is_published=0
                )
            else:
                synced_count += 1

        await self.session.flush()

        logger.info(
            "MCP service sync complete: synced=%d, total=%d",
            synced_count,
            len(domain_services),
        )

        return {"synced_count": synced_count, "total_services": len(domain_services)}

    async def add_key_to_service(
        self, tenant_id: str, service_id: str, req
    ) -> dict:
        """将已有 MCP Key 添加到领域服务授权列表"""
        from capabilities.platform.repository.mcp_key_repository import (
            McpKeyRepository,
            McpKeyServiceMappingRepository,
        )

        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        # 校验 key 存在且属于同租户、有效状态
        key_repo = McpKeyRepository(self.session)
        key = await key_repo.get_by_id_include_deleted(req.key_id, tenant_id)
        if not key:
            raise ResourceNotFoundError(
                message=f"MCP Key 不存在: {req.key_id}",
                details={"key_id": req.key_id},
            )
        if key.is_deleted == 1:
            raise ResourceNotFoundError(
                message=f"MCP Key 已删除: {req.key_id}",
                details={"key_id": req.key_id},
            )

        mapping_repo = McpKeyServiceMappingRepository(self.session)
        await mapping_repo.add_mapping(req.key_id, service_id, req.permission_level)
        await self.session.flush()

        return {
            "key_id": req.key_id,
            "service_id": service_id,
            "permission_level": req.permission_level,
        }

    async def remove_key_from_service(
        self, tenant_id: str, service_id: str, key_id: str
    ) -> dict:
        """移除 Key 对该领域服务的授权（不撤销 Key）"""
        from capabilities.platform.repository.mcp_key_repository import (
            McpKeyServiceMappingRepository,
        )

        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        mapping_repo = McpKeyServiceMappingRepository(self.session)
        removed = await mapping_repo.remove_mapping(key_id, service_id)
        await self.session.flush()

        if not removed:
            raise ResourceNotFoundError(
                message=f"授权映射不存在: key={key_id}, service={service_id}",
                details={"key_id": key_id, "service_id": service_id},
            )

        return {"removed": True}

    async def update_key_permission(
        self, tenant_id: str, service_id: str, key_id: str, permission_level: str
    ) -> dict:
        """切换 Key 对该服务的权限级别"""
        from capabilities.platform.repository.mcp_key_repository import (
            McpKeyServiceMappingRepository,
        )

        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        mapping_repo = McpKeyServiceMappingRepository(self.session)
        updated = await mapping_repo.update_permission(key_id, service_id, permission_level)
        await self.session.flush()

        if not updated:
            raise ResourceNotFoundError(
                message=f"授权映射不存在: key={key_id}, service={service_id}",
                details={"key_id": key_id, "service_id": service_id},
            )

        return {
            "key_id": key_id,
            "service_id": service_id,
            "permission_level": permission_level,
        }

    async def create_key_for_service(
        self, tenant_id: str, service_id: str, req,
        authorization_header: str | None = None,
    ) -> dict:
        """创建新 MCP Key 并自动关联到当前服务"""
        import uuid as _uuid
        from datetime import datetime as _datetime, timezone as _timezone

        from jonex_core.common.crypto import generate_mcp_key as _gen_key, hash_mcp_key as _hash_key
        from capabilities.platform.models.mcp_key import McpKey
        from capabilities.platform.repository.mcp_key_repository import McpKeyServiceMappingRepository

        tenant_id = require_tenant(tenant_id)
        await self._assert_service_exists(tenant_id, service_id)

        username = self._extract_username(authorization_header)

        # Generate plaintext and hash
        plaintext = _gen_key()
        prefix = plaintext[4:12]  # yxm_xxxx (first 8 chars after "yxm_")
        key_hash_val = _hash_key(plaintext)

        now = _datetime.now(_timezone.utc)
        key_id = _uuid.uuid4().hex

        new_key = McpKey(
            id=key_id,
            tenant_id=tenant_id,
            name=req.name,
            key_prefix=prefix,
            key_hash=key_hash_val,
            permissions="read",
            allowed_kb_ids=[],
            created_by=username,
            created_at=now,
            expires_at=req.expires_at,
            is_deleted=0,
        )
        self.session.add(new_key)

        # Add service mapping
        mapping_repo = McpKeyServiceMappingRepository(self.session)
        await mapping_repo.add_mapping(key_id, service_id, req.permission_level)

        await self.session.flush()

        return {
            "id": key_id,
            "plaintext": plaintext,
            "name": req.name,
            "key_prefix": prefix,
            "permission_level": req.permission_level,
            "service_id": service_id,
            "created_at": now,
            "expires_at": req.expires_at,
        }
