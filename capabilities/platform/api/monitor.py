"""平台监控 · 读接口路由（调用监控、Token 监控、调用记录 Token 补值与未归因汇总，共 20 条）。

薄路由：只解析请求 + 取 session + 调 service，不含业务规则。业务在
``services/monitor_call.py``，取数在两个 ``repository/monitor_*.py``。

⚠️ 网关对 platform 是**逐条登记**的（``api_gateway/routes/platform.py``，不像 Sidecar
有 ``/platform/{path:path}`` 兜底）⇒ 本文件每加一条端点，**必须**同步在网关加一条，
否则浏览器侧 404。``tests/unit/test_hide_task_center.py`` 的正反双向断言盯着这件事。

路由注册顺序：静态路径段在前、``{request_id}`` 参数路径在后（段数相同才可能吞路径，
此处靠顺序 + 段数双重保险）。
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request

from jonex_core.common.database import get_db
from jonex_core.common.exceptions import PermissionDeniedError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.logger import get_logger
from jonex_core.common.response import success_response
from jonex_core.security.permission import has_permission, require_any_permission
from jonex_core.security.user_auth import get_current_user

from capabilities.platform.models.audit_enums import AuditAction, LogLevel, LogType, Outcome
from capabilities.platform.services.audit_log_service import AuditLogService
from capabilities.platform.services.monitor_call import MonitorCallService
from capabilities.platform.services.monitor_task import MonitorTaskService
from capabilities.platform.services.monitor_token import MonitorTokenService
from capabilities.platform.services.monitor_unattributed import (
    MonitorUnattributedService,
)

logger = get_logger(__name__)

router = APIRouter()

#: 监控读权限码。无此码一律 403（3006）。
#: 授权范围（2026-11-06 已确认）：平台管理员 + 各租户「租户管理员」。
#: 存量回填见 ``deploy/postgres/update/107_grant_monitor_read_to_tenant_admin.sql``；
#: 新建租户由 ``misc_services.seed_tenant_roles`` 按 ``_TENANT_ADMIN_PLATFORM_CODES`` 复制。
_MONITOR_READ_CODE = "platform:monitor:read"


def _build_monitor_read_guard():
    # 守卫内部仍持有既有判定依赖：语义与 require_permission 同源。守卫体不直接
    # 调用它（否则会先抛 403、来不及写审计），但保留引用以便审查/复用。
    _permission_check = require_any_permission(_MONITOR_READ_CODE)
    _permission_codes = (_MONITOR_READ_CODE,)

    async def monitor_read_guard(
        request: Request,
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        """监控读接口统一权限守卫（20 条路由共用）。

        与 ``require_permission`` 判定数据源一致，唯一差别是**失败时先写审计再抛 403**。
        审计只落身份 / 端点 / 判定结果元数据，不落
        request_params、response_body、header、query、body。

        测试 token（``user_id == 0`` 且 ``role == "admin"``）直接放行，与
        ``require_permission`` 的本地调试后门保持一致。
        """
        if current_user.get("user_id") == 0 and current_user.get("role") == "admin":
            return current_user

        allowed = await has_permission(
            current_user["tenant_id"],
            current_user["user_id"],
            _permission_codes[0],
            impersonated=current_user.get("impersonated", False),
            perms=current_user.get("perms"),
        )
        if allowed:
            return current_user

        # 审计失败绝不能把越权请求放行：吞掉异常，继续抛 403。
        try:
            await AuditLogService().record(
                tenant_id=current_user["tenant_id"],
                log_type=LogType.OPERATION.value,
                action=AuditAction.MONITOR_ACCESS_DENIED.value,
                outcome=Outcome.FAILED.value,
                service_name="platform",
                user_id=current_user["user_id"],
                username=current_user.get("username"),
                resource="monitor",
                method=request.method,
                path=request.url.path,
                status_code=403,
                trace_id=request.headers.get("X-Request-ID"),
                request_params=_monitor_audit_params(current_user),
                log_level=LogLevel.ERROR.value,
                sync=False,
            )
        except Exception:
            logger.exception("监控越权审计写入失败，仍拒绝访问")

        raise PermissionDeniedError(
            message=translate(
                "err.auth.insufficient_permission",
                params={"required": ", ".join(_permission_codes)},
                fallback=f"权限不足：需要权限 {', '.join(_permission_codes)}",
            )
        )

    #: 稳定属性：源码级测试据它确认每条路由挂的正是这个权限码。权限码元组同时
    #: 留在闭包里，兼容此前从 ``__closure__`` 提取 tuple 的测试 helper。
    monitor_read_guard.permission_codes = _permission_codes
    monitor_read_guard.__jonex_permission_codes__ = _permission_codes
    monitor_read_guard._permission_check = _permission_check
    return monitor_read_guard


monitor_read_guard = _build_monitor_read_guard()

_MONITOR_READ = monitor_read_guard

#: 时间区间查询参数：``from``/``to`` 必填，ISO8601 UTC。
_FROM = Query(..., alias="from", description="开始时间（ISO8601 UTC，左闭）")
_TO = Query(..., description="结束时间（ISO8601 UTC，右开）")
_PRESET = Query(None, description="周期标签（仅回显用，不参与计算）")

# 分页出参形状照契约冻结：``items`` + ``total`` + ``mode_meta``（C8 另加
# ``retry_count``），不再自加 ``page`` / ``page_size`` —— 出参字段以契约为准，不自造。


def _mode_meta_dict(mode_meta) -> Optional[dict]:
    """``CallModeMeta`` → dict；降级空壳 / 未判定时为 ``None``。"""
    return None if mode_meta is None else mode_meta.dict()


def _monitor_audit_params(current_user: dict) -> dict:
    """监控拒绝审计字段：角色 + 来源租户 + 目标租户。

    模拟态下 ``tenant_id`` 是目标租户、``original_tenant_id`` 是操作人原租户；
    非模拟态两者相同。
    """
    target_tenant = current_user["tenant_id"]
    return {
        "role": current_user.get("role"),
        "roles": current_user.get("roles"),
        "source_tenant_id": current_user.get("original_tenant_id") or target_tenant,
        "target_tenant_id": target_tenant,
    }


async def _audit_resource_miss(
    request: Request, current_user: dict, resource_id: str, *, status_code: int
) -> None:
    """详情类读接口未命中时记录拒绝审计（跨租户详情的临时安全下限）。

    跨租户与「真的不存在」在外层响应上**完全一致**（404 / 空结构），
    因此这里对两类情况一视同仁地留痕，不做存在性区分。
    审计写失败只记日志，绝不影响响应（也不得因此放行）。
    """
    try:
        await AuditLogService().record(
            tenant_id=current_user["tenant_id"],
            log_type=LogType.OPERATION.value,
            action=AuditAction.MONITOR_ACCESS_DENIED.value,
            outcome=Outcome.FAILED.value,
            service_name="platform",
            user_id=current_user["user_id"],
            username=current_user.get("username"),
            resource="monitor",
            resource_id=resource_id,
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            trace_id=request.headers.get("X-Request-ID"),
            # 审计：角色 + 来源租户 + 目标租户，便于回溯「谁、从哪个租户、看哪个租户」。
            request_params=_monitor_audit_params(current_user),
            log_level=LogLevel.ERROR.value,
            sync=False,
        )
    except Exception:
        logger.exception("监控详情未命中审计写入失败")


# ==================== C1 / C3 统计卡 ====================


@router.get("/monitor/calls/external/summary", summary="外部依赖统计卡")
async def external_summary(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """C1 外部依赖（direction=outbound + call_scope=external）统计卡。"""
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    data = await svc.external_summary(from_, to)
    return success_response(data=data.dict())


@router.get("/monitor/calls/open/summary", summary="开放能力统计卡")
async def open_summary(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """开放能力统计卡（``direction=inbound``，排除 ``protocol='invoke'``）。

    隐藏口径见 :data:`monitor_call._HIDDEN_CALL_PROTOCOL`：只收窄读侧展示，
    采集与历史数据不变。
    """
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    data = await svc.open_summary(from_, to)
    return success_response(data=data.dict())


# ==================== C2 / C4 对象列表 ====================


@router.get("/monitor/calls/external/objects", summary="外部依赖对象列表")
async def external_objects(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    object_type: Optional[str] = Query(
        None,
        description="对象类型（由 protocol 推导：模型服务 / 能力；未知值返空结果）",
    ),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """C2 外部依赖对象列表（分页，固定 call_scope=external）。"""
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    items, total, mode_meta = await svc.external_objects(
        from_, to, object_type=object_type, page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [item.dict() for item in items],
            "total": total,
            "mode_meta": _mode_meta_dict(mode_meta),
        }
    )


@router.get("/monitor/calls/open/objects", summary="开放能力对象列表")
async def open_objects(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    object_type: Optional[str] = Query(
        None,
        description="对象类型（由 protocol 推导：模型服务 / 能力；未知值返空结果）",
    ),
    protocol: Optional[str] = Query(
        None, description="调用方式：invoke / mcp / http / openai"
    ),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """开放能力对象列表（分页，补 protocol 枚举筛选，恒排除 ``invoke``）。"""
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    items, total, mode_meta = await svc.open_objects(
        from_,
        to,
        object_type=object_type,
        protocol=protocol,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data={
            "items": [item.dict() for item in items],
            "total": total,
            "mode_meta": _mode_meta_dict(mode_meta),
        }
    )


# ==================== C5 趋势 ====================


@router.get("/monitor/calls/trend", summary="调用趋势")
async def call_trend(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    metric: str = Query(
        ..., description="指标：call_count / success_rate / avg_latency / failed_count"
    ),
    bucket: str = Query(..., description="桶粒度：5m / hour / day"),
    object_id: Optional[str] = Query(None, description="对象 / 能力 ID（可选）"),
    direction: Optional[str] = Query(
        None,
        description="调用方向：outbound（外部依赖）/ inbound（开放能力）；不传 = 全量",
    ),
    call_scope: Optional[str] = Query(
        None,
        description="调用边界：external / internal / open；不传 = 不筛",
    ),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """调用趋势（按 metric 与 bucket 分桶；``direction`` + ``call_scope`` 切页签口径）。

    ``direction=inbound``（开放能力页）时与统计卡、对象列表同口径排除
    ``protocol='invoke'``。
    """
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    points, mode_meta = await svc.trend(
        from_,
        to,
        metric=metric,
        bucket=bucket,
        object_id=object_id,
        direction=direction,
        call_scope=call_scope,
    )
    return success_response(
        data={
            "points": [p.dict() for p in points],
            "mode_meta": _mode_meta_dict(mode_meta),
        }
    )


# ==================== C6 调用记录 ====================


@router.get("/monitor/calls/records", summary="调用记录列表")
async def call_records(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    direction: Optional[str] = Query(None, description="调用方向：outbound / inbound"),
    status: Optional[str] = Query(
        None, description="终态：success / failed / cancelled / running / unknown"
    ),
    trigger_type: Optional[str] = Query(None, description="触发方：user / system"),
    protocol: Optional[str] = Query(
        None, description="调用方式：invoke / mcp / http / openai"
    ),
    source: Optional[str] = Query(None, description="来源 / 调用方"),
    target: Optional[str] = Query(
        None, description="目标对象 / 能力（精确匹配，降级模式下回查逻辑调用）"
    ),
    keyword: Optional[str] = Query(None, description="关键字（来源 / 目标 / Request ID）"),
    target_any: Optional[List[str]] = Query(
        None,
        description=(
            "目标对象标识列表（重复参数）；与关键字 OR 组合，"
            "按 COALESCE(mcp_tool, target) 口径匹配"
        ),
    ),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """调用记录列表（正常模式 = 逻辑调用；降级模式 = 物理 Attempt）。

    恒排除 ``protocol='invoke'``（2026-11-16 产品口径）：``total`` 与列表同口径，
    其余筛选（分页 / 时间窗 / 排序 / status 等）行为不变。
    """
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    items, total, mode_meta = await svc.records(
        from_,
        to,
        direction=direction,
        status=status,
        trigger_type=trigger_type,
        protocol=protocol,
        source=source,
        target=target,
        keyword=keyword,
        target_any=target_any,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data={
            "items": [item.dict() for item in items],
            "total": total,
            "mode_meta": _mode_meta_dict(mode_meta),
        }
    )


@router.get("/monitor/calls/token-usage", summary="调用记录 Token 补值")
async def call_token_usage(
    request_ids: Optional[List[str]] = Query(
        None,
        description=(
            "调用记录的 request_id 列表（重复参数）；一次最多 100 条。"
            "服务于「首屏先显示 -、数据到达后自动补值」，口径与 /monitor/calls/records 的 total_tokens 相同"
        ),
    ),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """按 request_id 批量取精确 Token 合计（供前端首屏后短轮询的轻量接口）。

    只返回 ``request_id`` + ``total_tokens``；值为 ``null`` 表示仍没有精确计量
    （含「本来没调模型」与「还没落库」两种，调用方不区分）。
    """
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    items = await svc.token_usage(request_ids or [])
    return success_response(data={"items": [item.dict() for item in items]})


# ==================== C9 对象 / 能力详情 ====================


@router.get("/monitor/calls/objects/{object_id}/detail", summary="对象 / 能力详情")
async def object_detail(
    request: Request,
    object_id: str,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    protocol: Optional[str] = Query(None, description="调用方式（可选，用于消歧）"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """C9 对象 / 能力详情（基础指标 + 来源 TOP3 + 最近失败调用）。"""
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    try:
        data = await svc.object_detail(object_id, from_, to, protocol=protocol)
    except ResourceNotFoundError:
        await _audit_resource_miss(request, _p, object_id, status_code=404)
        raise
    return success_response(data=data.dict())


# ==================== C7 / C8 记录下钻（参数路径放最后）====================


@router.get("/monitor/calls/{request_id}/detail", summary="单次调用详情")
async def call_detail(
    request: Request,
    request_id: str,
    attempt_id: Optional[str] = Query(
        None,
        description=(
            "Attempt ID（行选择器：传入返回该物理尝试行、mode_meta=degraded；"
            "不传返回逻辑调用行、mode_meta=logical）"
        ),
    ),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """单次调用详情 单次调用详情（``attempt_id`` 为行选择器，可选）。

    跨租户、跨 request 的 attempt 与不存在统一返回 404；响应完全一致，
    仅内部写拒绝审计。
    """
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    try:
        data = await svc.call_detail(request_id, attempt_id=attempt_id)
    except ResourceNotFoundError:
        await _audit_resource_miss(request, _p, request_id, status_code=404)
        raise
    return success_response(data=data.dict())


@router.get("/monitor/calls/{request_id}/attempts", summary="Attempt 明细列表")
async def call_attempts(
    request: Request,
    request_id: str,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """C8 某逻辑调用下的 Attempt 明细（分页 + retry_count）。

    跨租户与不存在统一返回空结构（``items=[]`` / ``total=0``），并写拒绝审计。
    """
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    items, total, retry_count = await svc.attempts(
        request_id, page=page, page_size=page_size
    )
    if total == 0:
        await _audit_resource_miss(request, _p, request_id, status_code=200)
    return success_response(
        data={
            "items": [item.dict() for item in items],
            "total": total,
            "retry_count": retry_count,
        }
    )


# ==================== M1 筛选元数据 ====================


@router.get("/monitor/meta/filters", summary="筛选元数据")
async def meta_filters(
    request: Request,
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """M1 四类下拉候选值（任务类型 / 状态 / 对象 / 来源）。"""
    svc = MonitorCallService(db, tenant_id=_p["tenant_id"])
    data = await svc.meta_filters()
    return success_response(data=data.dict())


# ==================== K1–K4 Token 监控族 ====================
#
# 数据源唯一 = 既有表 ``metering.llm_usage_log``；调用次数一律 ``SUM(call_count)``。
# 与 C/T 族一样**按生效租户过滤**（租户来自认证上下文，见模块 docstring）。
# 四条全静态路径，与参数路径无冲突（见模块 docstring 的顺序约定）。


@router.get("/monitor/tokens/summary", summary="Token 用量统计卡")
async def token_summary(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """K1 Token 用量统计卡。"""
    svc = MonitorTokenService(db, tenant_id=_p["tenant_id"])
    data = await svc.summary(from_, to)
    return success_response(data=data.dict())


@router.get("/monitor/tokens/trend", summary="Token 用量趋势")
async def token_trend(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    metric: str = Query(
        "total",
        description="指标：total / input / output（选填，默认 total；仅校验，不裁剪响应）",
    ),
    bucket: str = Query(..., description="桶粒度：5m / hour / day"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """K2 Token 用量趋势（按 bucket 分桶；响应恒返四个量，不随 metric 裁剪）。"""
    svc = MonitorTokenService(db, tenant_id=_p["tenant_id"])
    points = await svc.trend(from_, to, metric=metric, bucket=bucket)
    return success_response(data={"points": [p.dict() for p in points]})


@router.get("/monitor/tokens/models/top", summary="Token 用量模型 TOP N")
async def token_models_top(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    limit: int = Query(5, ge=1, le=100, description="TOP N（1..100）"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """K3 Token 用量模型 TOP N（按 total_tokens 倒序）。"""
    svc = MonitorTokenService(db, tenant_id=_p["tenant_id"])
    items = await svc.top_models(from_, to, limit=limit)
    return success_response(data={"items": [item.dict() for item in items]})


@router.get("/monitor/tokens/usage", summary="Token 用量明细列表")
async def token_usage(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    keyword: Optional[str] = Query(None, description="模型关键字（大小写不敏感模糊匹配）"),
    sort_by: str = Query(
        "total_tokens",
        description=(
            "排序字段：total_tokens / call_count / prompt_tokens / completion_tokens"
            " / model / avg_tokens_per_call / last_called_at"
        ),
    ),
    sort_order: str = Query("desc", description="排序方向：asc / desc"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(5, ge=1, le=100, description="每页条数"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """K4 按模型聚合的 Token 用量明细（分页）。"""
    svc = MonitorTokenService(db, tenant_id=_p["tenant_id"])
    items, total = await svc.usage_list(
        from_,
        to,
        keyword=keyword,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data={
            "items": [item.dict() for item in items],
            "total": total,
        }
    )


# ==================== T1–T4 任务监控族 ====================
#
# 数据源 = 既有表 `task_center.tasks` / `task_center.subtasks`（裸 SQL，仓储层不 import
# task_center）。与 C 族 / K 族一样**按生效租户过滤**（租户来自认证上下文）。
# 四条全静态路径（`/monitor/tasks/*`），与 `calls/{request_id}` 参数路径不同段、无冲突。


@router.get("/monitor/tasks/summary", summary="任务监控统计卡")
async def task_summary(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """T1 任务监控统计卡。"""
    svc = MonitorTaskService(db, tenant_id=_p["tenant_id"])
    data = await svc.summary(from_, to)
    return success_response(data=data.dict())


@router.get("/monitor/tasks/trend", summary="任务监控趋势")
async def task_trend(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    metric: str = Query(
        ...,
        description="指标：task_count / success_rate / avg_duration / failed_count",
    ),
    bucket: str = Query(..., description="桶粒度：5m / hour / day"),
    task_type: Optional[str] = Query(None, description="编排定义 ID（= task_center.tasks.definition_id；取值见 M1 `task_types[].code`，可选）"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """T2 任务监控趋势（按 metric 与 bucket 分桶）。"""
    svc = MonitorTaskService(db, tenant_id=_p["tenant_id"])
    points = await svc.trend(
        from_, to, metric=metric, bucket=bucket, task_type=task_type
    )
    return success_response(data={"points": [p.dict() for p in points]})


@router.get("/monitor/tasks/failures", summary="失败任务列表")
async def task_failures(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    task_type: Optional[str] = Query(None, description="编排定义 ID（= task_center.tasks.definition_id；取值见 M1 `task_types[].code`，可选）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(5, ge=1, le=100, description="每页条数（默认 5）"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """T3 失败任务列表（分页；出参只含 items + total）。"""
    svc = MonitorTaskService(db, tenant_id=_p["tenant_id"])
    items, total = await svc.failures(
        from_, to, task_type=task_type, page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [item.dict() for item in items],
            "total": total,
        }
    )


@router.get("/monitor/tasks/overview", summary="任务运行概览")
async def task_overview(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    task_type: Optional[str] = Query(None, description="编排定义 ID（= task_center.tasks.definition_id；取值见 M1 `task_types[].code`，可选）"),
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """T4 任务运行概览（父表按类型分组 + 嵌套 stages[] 按 step_code 分组）。"""
    svc = MonitorTaskService(db, tenant_id=_p["tenant_id"])
    items = await svc.overview(from_, to, task_type=task_type)
    return success_response(data={"items": [item.dict() for item in items]})


# ==================== 未归因调用汇总 ====================

_PLATFORM_ADMIN_CODE = "platform:admin"


async def _is_platform_admin(current_user: dict) -> bool:
    """当前用户是否持有 ``platform:admin``（决定是否下发占比字段）。

    与守卫同源：本地调试后门（``user_id == 0`` 且 ``role == "admin"``）直接视为
    平台管理员，避免测试 token 拿到 ``null`` 占比而与生产行为不一致。
    """
    if current_user.get("user_id") == 0 and current_user.get("role") == "admin":
        return True
    return await has_permission(
        current_user["tenant_id"],
        current_user["user_id"],
        _PLATFORM_ADMIN_CODE,
        impersonated=current_user.get("impersonated", False),
        perms=current_user.get("perms"),
    )


@router.get("/monitor/unattributed/summary", summary="未归因调用汇总")
async def unattributed_summary(
    request: Request,
    from_: str = _FROM,
    to: str = _TO,
    range_preset: Optional[str] = _PRESET,
    db=Depends(get_db),
    _p: dict = Depends(_MONITOR_READ),
):
    """未归因（``tenant_id='unknown'``）调用汇总。

    与其余 19 条的差别只有一处：**不按生效租户过滤**，因为这些行不属于任何租户
    （见 ``repository/monitor_unattributed.py`` 的受控例外说明）。权限码仍是
    ``platform:monitor:read``；占比字段另按 ``platform:admin`` 降级，避免把全平台
    调用规模暴露给租户管理员。
    """
    svc = MonitorUnattributedService(db, include_share=await _is_platform_admin(_p))
    data = await svc.summary(from_, to)
    return success_response(data=data.dict())
