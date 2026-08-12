"""AuditLogService — 审计日志业务服务

写入（record / record_system / ingest_batch）与查询（query / get_log_detail）。
"""

import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.audit_resource_enricher import ResourceNameEnricher
from jonex_core.common.logger import get_logger
from jonex_core.common.tenant import require_tenant

from capabilities.platform.dtos.misc import (
    AuditLogDetailResponse,
    AuditLogListResponse,
    AuditLogResponse,
)
from capabilities.platform.models.audit_enums import AuditAction, LogLevel, LogType, Outcome, ResourceType
from capabilities.platform.repository.audit_log_repository import AuditLogRepository
from capabilities.platform.services.audit_log_sink import get_audit_log_sink

logger = get_logger(__name__)

# ----- 脱敏配置 -----
SENSITIVE_KEYS = [
    "password", "passwd", "pwd", "token", "access_token", "refresh_token",
    "authorization", "authorization_code", "secret", "api_key", "apikey",
    "private_key", "ticket",
]
# 大小写不敏感匹配
SENSITIVE_PATTERN = re.compile(
    "|".join(re.escape(k) for k in SENSITIVE_KEYS),
    re.IGNORECASE,
)

MAX_BODY_BYTES = 8 * 1024  # 8KB 截断


def _sanitize_params(params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """脱敏：过滤敏感键的值"""
    if not params:
        return params
    sanitized = {}
    for k, v in params.items():
        if SENSITIVE_PATTERN.search(k):
            sanitized[k] = "***"
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_params(v)
        elif isinstance(v, list):
            sanitized[k] = [_sanitize_params(i) if isinstance(i, dict) else i for i in v]
        else:
            sanitized[k] = v
    return sanitized


def _truncate_body(body: Optional[Any]) -> Optional[Any]:
    """截断 response body"""
    if body is None:
        return None
    import json
    dumped = json.dumps(body, ensure_ascii=False, default=str)
    if len(dumped) > MAX_BODY_BYTES:
        return {"_truncated": True, "size": len(dumped)}
    return body


def _derive_log_level(entry: dict) -> str:
    """从 outcome 推导 log_level（返回纯字符串）"""
    if entry.get("log_level"):
        lvl = entry["log_level"]
        return lvl.value if isinstance(lvl, Enum) else str(lvl)
    outcome = entry.get("outcome", Outcome.SUCCESS)
    if outcome == Outcome.FAILED or outcome == Outcome.FAILED.value:
        return LogLevel.ERROR.value
    return LogLevel.INFO.value


def _coerce(value):
    """将 Enum 归一化为其字符串值，便于存入 VARCHAR 列"""
    return value.value if isinstance(value, Enum) else value


def _build_audit_dict(
    tenant_id: Optional[str],
    log_type: str,
    action: str,
    outcome: str = Outcome.SUCCESS,
    service_name: str = "platform",
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    ip: Optional[str] = None,
    resource: Optional[str] = None,
    resource_id: Optional[str] = None,
    method: Optional[str] = None,
    path: Optional[str] = None,
    status_code: Optional[int] = None,
    duration_ms: Optional[int] = None,
    request_params: Optional[Dict[str, Any]] = None,
    response_body: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    error_stack: Optional[str] = None,
    trace_id: Optional[str] = None,
    log_level: Optional[str] = None,
) -> dict:
    """构建审计日志 dict（统一处理脱敏和截断）"""
    entry = {
        "tenant_id": tenant_id,
        "log_type": _coerce(log_type),
        "action": action,
        "outcome": _coerce(outcome),
        "service_name": _coerce(service_name),
        "user_id": user_id,
        "username": username,
        "ip": ip,
        "resource": resource,
        "resource_id": resource_id,
        "method": method,
        "path": path,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "request_params": _sanitize_params(request_params),
        "response_body": _truncate_body(response_body),
        "error_message": error_message,
        "error_stack": error_stack,
        "trace_id": trace_id,
        "log_level": log_level or _derive_log_level({"outcome": outcome}),
    }
    # 移除 None 字段使模型更干净
    return {k: v for k, v in entry.items() if v is not None}


class AuditLogService:
    """审计日志服务"""

    def __init__(self, session: Optional[AsyncSession] = None):
        self.session = session
        self.repo = AuditLogRepository(session) if session else None
        self.sink = get_audit_log_sink()

    @staticmethod
    def _to_naive_datetime(iso_str: str) -> datetime:
        """将 ISO 时间字符串转为无时区的 naive datetime（Asia/Shanghai）

        DB 的 created_at 是 TIMESTAMP WITHOUT TIME ZONE，使用 datetime.now()
        写入 Asia/Shanghai 时间。前端传来的 ISO 时间带 Z（UTC），需转换对齐。
        """
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is not None:
            utc = dt.astimezone(timezone.utc)
            return utc.replace(tzinfo=None) + timedelta(hours=8)
        return dt

    # ===== 写入 =====

    async def record(
        self,
        tenant_id: str,
        log_type: str,
        action: str,
        outcome: str = Outcome.SUCCESS,
        *,
        service_name: str = "platform",
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
        resource: Optional[str] = None,
        resource_id: Optional[str] = None,
        method: Optional[str] = None,
        path: Optional[str] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        request_params: Optional[Dict[str, Any]] = None,
        response_body: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        error_stack: Optional[str] = None,
        trace_id: Optional[str] = None,
        log_level: Optional[str] = None,
        sync: bool = False,
    ):
        """记录租户审计事件

        Args:
            tenant_id: 租户 ID（必须为合法租户）
            sync: 是否同步直写（强审计事件应使用 True）
        """
        require_tenant(tenant_id)
        entry = _build_audit_dict(
            tenant_id=tenant_id,
            log_type=log_type,
            action=action,
            outcome=outcome,
            service_name=service_name,
            user_id=user_id,
            username=username,
            ip=ip,
            resource=resource,
            resource_id=resource_id,
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            request_params=request_params,
            response_body=response_body,
            error_message=error_message,
            error_stack=error_stack,
            trace_id=trace_id,
            log_level=log_level,
        )
        if sync:
            await self._write_sync(entry)
        else:
            self.sink.put(entry)

    async def record_system(
        self,
        log_type: str,
        action: str,
        outcome: str = Outcome.SUCCESS,
        *,
        service_name: str = "platform",
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
        resource: Optional[str] = None,
        resource_id: Optional[str] = None,
        method: Optional[str] = None,
        path: Optional[str] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        request_params: Optional[Dict[str, Any]] = None,
        response_body: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        error_stack: Optional[str] = None,
        trace_id: Optional[str] = None,
        log_level: Optional[str] = None,
        sync: bool = False,
    ):
        """记录系统事件（tenant_id=None，不校验租户）

        Args:
            service_name: 必须提供
            sync: 是否同步直写

        说明：接受与 record 相同的宽参数集，便于无租户的 HTTP/任务事件
        通过统一入口落库；多余的关键字会被忽略（由调用方保证字段合法）。
        """
        if not service_name:
            raise ValueError("系统事件必须提供 service_name")
        entry = _build_audit_dict(
            tenant_id=None,
            log_type=log_type,
            action=action,
            outcome=outcome,
            service_name=service_name,
            user_id=user_id,
            username=username,
            ip=ip,
            resource=resource,
            resource_id=resource_id,
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            request_params=request_params,
            response_body=response_body,
            error_message=error_message,
            error_stack=error_stack,
            trace_id=trace_id,
            log_level=log_level,
        )
        if sync:
            await self._write_sync(entry)
        else:
            self.sink.put(entry)

    async def ingest_batch(self, entries: List[Dict[str, Any]]):
        """批量投递（供 ingest 接口调用）

        ingest / remote 转发路径不经过 record()，需在此统一脱敏、截断并补默认值。
        无合法租户的条目转为系统事件（log_type=SYSTEM）落库，而非直接丢弃。
        """
        for entry in entries:
            entry = dict(entry)
            # 脱敏与截断（与 record 路径保持一致）
            if entry.get("request_params") is not None:
                entry["request_params"] = _sanitize_params(entry["request_params"])
            if entry.get("response_body") is not None:
                entry["response_body"] = _truncate_body(entry["response_body"])

            tenant_id = entry.get("tenant_id")
            valid_tenant = False
            if tenant_id:
                try:
                    require_tenant(tenant_id)
                    valid_tenant = True
                except Exception:
                    valid_tenant = False
            if not valid_tenant:
                # 无合法租户 => 系统事件
                entry["tenant_id"] = None
                entry["log_type"] = LogType.SYSTEM.value

            # 补充默认 log_level（ingest 路径跳过 _build_audit_dict，需自行推导）
            if not entry.get("log_level"):
                entry["log_level"] = (
                    LogLevel.ERROR.value
                    if entry.get("outcome") == Outcome.FAILED.value
                    else LogLevel.INFO.value
                )
            self.sink.put(entry)

    async def _write_sync(self, entry: dict):
        """同步直写数据库"""
        if self.session is None:
            raise RuntimeError("同步直写需要 AsyncSession")
        try:
            obj = AuditLogRepository.model(**entry)
            self.session.add(obj)
            await self.session.flush()
        except Exception:
            logger.exception("审计日志同步直写失败")

    # ===== 查询 =====

    @staticmethod
    def _build_resource_label(action: str, resource: Optional[str]) -> Optional[str]:
        """根据 action 和 resource 构建资源类型中文显示名。

        优先使用 resource 对应的 label_zh，其次从 action 推导。
        两者都没有时返回 None。
        """
        if resource:
            return ResourceType.get_labels(resource).get("label_zh")
        if action:
            res_type = ResourceType.resolve(action)
            if res_type:
                return ResourceType.get_labels(res_type).get("label_zh")
        return None

    async def query(
        self,
        tenant_id: str,
        log_type: Optional[str] = None,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        service_name: Optional[str] = None,
        user_id: Optional[int] = None,
        keyword: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> AuditLogListResponse:
        """分页查询租户审计日志"""
        require_tenant(tenant_id)
        offset = (page - 1) * page_size
        st = self._to_naive_datetime(start_time) if start_time else None
        et = self._to_naive_datetime(end_time) if end_time else None

        items = await self.repo.list_by_tenant(
            tenant_id=tenant_id,
            log_type=log_type,
            action=action,
            outcome=outcome,
            service_name=service_name,
            user_id=user_id,
            keyword=keyword,
            start_time=st,
            end_time=et,
            offset=offset,
            limit=page_size,
        )
        total = await self.repo.count_by_tenant(
            tenant_id=tenant_id,
            log_type=log_type,
            action=action,
            outcome=outcome,
            service_name=service_name,
            user_id=user_id,
            keyword=keyword,
            start_time=st,
            end_time=et,
        )
        items_with_label = []
        for item in items:
            log = AuditLogResponse.from_orm(item)
            log.resource_label = self._build_resource_label(
                item.action, item.resource
            )
            items_with_label.append(log)

        # 资源名称富化
        if items_with_label and self.session:
            enricher = ResourceNameEnricher(self.session)
            await enricher.enrich_batch(items_with_label)

        return AuditLogListResponse(
            total=total,
            items=items_with_label,
        )

    async def get_log_detail(self, tenant_id: str, log_id: int) -> Optional[AuditLogDetailResponse]:
        """获取单条日志详情（含敏感字段）"""
        require_tenant(tenant_id)
        log = await self.repo.get_by_id_with_detail(log_id)
        if not log or log.tenant_id != tenant_id:
            return None
        return AuditLogDetailResponse.from_orm(log)

    async def query_system_events(
        self,
        action: Optional[str] = None,
        service_name: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> AuditLogListResponse:
        """查询系统事件（tenant_id IS NULL）"""
        offset = (page - 1) * page_size
        st = self._to_naive_datetime(start_time) if start_time else None
        et = self._to_naive_datetime(end_time) if end_time else None

        items = await self.repo.list_system_events(
            action=action,
            service_name=service_name,
            start_time=st,
            end_time=et,
            offset=offset,
            limit=page_size,
        )
        total = await self.repo.count_system_events(
            action=action,
            service_name=service_name,
            start_time=st,
            end_time=et,
        )
        items_with_label = []
        for item in items:
            log = AuditLogResponse.from_orm(item)
            log.resource_label = self._build_resource_label(
                item.action, item.resource
            )
            items_with_label.append(log)

        # 资源名称富化
        if items_with_label and self.session:
            enricher = ResourceNameEnricher(self.session)
            await enricher.enrich_batch(items_with_label)

        return AuditLogListResponse(
            total=total,
            items=items_with_label,
        )

    # ===== 清理 =====

    async def cleanup_expired(self, retention_days: int = 90) -> int:
        """删除超过保留天数的过期审计日志"""
        if self.repo is None:
            raise RuntimeError("cleanup_expired 需要 AsyncSession")
        return await self.repo.delete_expired(retention_days)

    # ===== 操作类型枚举 =====

    async def list_actions(self, tenant_id: str) -> list[dict[str, str]]:
        """获取当前租户所有已使用的审计操作类型及其中英文标签。

        返回 [{action: "auth.login", label_zh: "登录", label_en: "Login"}, ...]。
        由 C+D 方案引入，供前端动态渲染筛选下拉框。
        """
        if self.repo is None:
            raise RuntimeError("list_actions 需要 AsyncSession")
        actions = await self.repo.list_distinct_actions(tenant_id)
        return [
            {"action": action, **AuditAction.get_labels(action)}
            for action in actions
        ]

    async def list_resource_types(self, tenant_id: str) -> list[dict[str, str]]:
        """获取当前租户所有已使用的资源类型及其中英文标签。

        返回 [{resource: "document", label_zh: "文档", label_en: "Document"}, ...]。
        供前端动态渲染资源类型筛选下拉框。
        """
        if self.repo is None:
            raise RuntimeError("list_resource_types 需要 AsyncSession")
        resources = await self.repo.list_distinct_resources(tenant_id)
        return [
            {"resource": res, **ResourceType.get_labels(res)}
            for res in resources
        ]
