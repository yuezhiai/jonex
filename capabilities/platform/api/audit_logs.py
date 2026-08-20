"""审计日志 API 路由（platform 容器内部）

提供：
- GET /audit-logs           — 分页 + 多维筛选
- GET /audit-logs/actions   — 操作类型枚举（C 方案）
- GET /audit-logs/{id}      — 详情（含 response_body / error_stack）
- POST /audit-logs:ingest   — 内部批量入库（仅 internal-auth）
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request

from jonex_core.common.database import get_db
from jonex_core.common.response import success_response
from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.security.internal_auth import verify_internal_service
from jonex_core.security.permission import require_permission

from capabilities.platform.dtos.audit import AuditEntryBatch
from capabilities.platform.services.audit_log_service import AuditLogService

router = APIRouter()


@router.get("/audit-logs", summary="分页查询审计日志")
async def list_audit_logs(
    request: Request,
    log_type: Optional[str] = Query(None, description="日志大类：LOGIN/OPERATION/SYSTEM/TASK"),
    action: Optional[str] = Query(None, description="动作码过滤"),
    outcome: Optional[str] = Query(None, description="SUCCESS/FAILED"),
    service_name: Optional[str] = Query(None, description="来源服务名"),
    user_id: Optional[int] = Query(None, description="用户 ID"),
    keyword: Optional[str] = Query(None, description="关键字（用户名/resource_id）"),
    start_time: Optional[str] = Query(None, description="开始时间 ISO 格式"),
    end_time: Optional[str] = Query(None, description="结束时间 ISO 格式"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:audit:read")),
):
    """获取当前租户的审计日志分页列表"""
    tenant_id = extract_tenant_id(request)
    svc = AuditLogService(db)
    result = await svc.query(
        tenant_id=tenant_id,
        log_type=log_type,
        action=action,
        outcome=outcome,
        service_name=service_name,
        user_id=user_id,
        keyword=keyword,
        start_time=start_time,
        end_time=end_time,
        page=page,
        page_size=page_size,
    )
    return success_response(data=result.dict())


@router.get("/audit-logs/actions", summary="获取当前租户所有已使用的操作类型")
async def list_audit_actions(
    request: Request,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:audit:read")),
):
    """返回当前租户审计日志中已使用（去重、排序）的操作类型列表。

    供前端动态渲染筛选下拉框，替代硬编码映射。
    由 C 方案引入，解决前后端操作类型值不匹配问题。
    """
    tenant_id = extract_tenant_id(request)
    svc = AuditLogService(db)
    actions = await svc.list_actions(tenant_id)
    return success_response(data={"actions": actions})

@router.get("/audit-logs/resource-types", summary="获取当前租户所有已使用的资源类型")
async def list_audit_resource_types(
    request: Request,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:audit:read")),
):
    """返回当前租户审计日志中已使用（去重、排序）的资源类型列表。

    供前端动态渲染资源类型筛选下拉框。
    """
    tenant_id = extract_tenant_id(request)
    svc = AuditLogService(db)
    resources = await svc.list_resource_types(tenant_id)
    return success_response(data={"resources": resources})


@router.get("/audit-logs/{log_id}", summary="获取审计日志详情")
async def get_audit_log_detail(
    request: Request,
    log_id: int,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:audit:read")),
):
    """获取单条审计日志详情（含 response_body / error_stack）"""
    tenant_id = extract_tenant_id(request)
    svc = AuditLogService(db)
    result = await svc.get_log_detail(tenant_id, log_id)
    if not result:
        raise ResourceNotFoundError(
            message=translate("err.audit.not_found", params={"log_id": str(log_id)}, fallback=f"审计日志不存在: {log_id}")
        )  # 原消息: 审计日志不存在: {log_id}
    return success_response(data=result.dict())


@router.post("/audit-logs:ingest", summary="内部批量入库", dependencies=[Depends(verify_internal_service)])
async def ingest_audit_logs(
    batch: AuditEntryBatch,
    db=Depends(get_db),
):
    """内部批量入库接口，供 Sidecar AuditForwarder / 其他能力服务调用"""
    svc = AuditLogService(db)
    entries = [e.dict(exclude_none=True) for e in batch.entries]
    await svc.ingest_batch(entries)
    return success_response(message=f"已接收 {len(entries)} 条审计日志")
