# -*- coding:utf-8 -*-
"""演化操作审计（`platform.audit_logs`）。

复用 `AuditLogService`（`service_name=knowledge_base`、`log_type=OPERATION`、
`resource=evolution_run`），语义见设计 spec §9：
- `evolution.adopt_accept`：采纳请求已受理（异步受理，仅前置校验 + CAS
  `evaluated → adopting`；`bench_status="pending"`）；
- `evolution.adopt`：采纳成功；
- `evolution.adopt_override`：`gate_effective != accept` 但人工 override 采纳；
- `evolution.bench_fail`：完整语料基准判定失败（enforce 模式 → 恢复快照、
  `smoke-failed`、409）；
- `evolution.bench_warn`：完整语料基准判定失败但 `EVOLUTION_BENCH_MODE=warn`
  → **不阻断**，版本照常激活并以 `bench_status="warn"` 收口；
- `evolution.bench_error`：基准执行器不可用 / 父本臂无效（默认阻止采纳，
  人工 override 才放行）；
- `evolution.adopt_stale`：父版本已不是链头（stale candidate），基准 skipped；
- `evolution.promote_adhoc`：把 run 里已验证的 ad-hoc case 提升为语料
  （`CorpusService.promote_from_run`）。`request_params` 只含计数
  （`requested` / `created` / `skipped` / `conflicts`），**不含 SQL 文本**。
  `requested` 是**批内归并之前**的候选条数，恒有
  `created + skipped == requested`；`conflicts` 已计入 `skipped`；
- `evolution.seed_corpus`：从编译 schema 或显式载荷播种语料
  （`corpus_seed.seed`）。`resource="evolution_corpus"`、`run_id="corpus:<kb>:<ds>"`，
  `request_params` 只含 `mode` / 计数，**不含 SQL 文本**；`dry_run` 不审计；
- `evolution.adopt_error`：legacy `adopt()` 内联执行时抛出**除参数/并发冲突外**
  的异常（`InvalidParameterError` / `ResourceConflictError` 仍按各自口径处理，
  不补此审计；`DatabaseError` / `DataIntegrityError` / `ServiceError` /
  `UpstreamServiceError` 等 4xxx/5xxx 均计入）；先回滚会话再记 failed，
  原始异常照常上抛。

上述 bench 相关动作的 `request_params` 统一带 `request_id` / `bench_status` /
`corpus_size` / `new_red_count`；`adopt_stale` 额外带 `parent_version_id` /
`active_version_id`。

与 `evolution_ledgers`（领域台账）分工不同：台账记「采纳了哪些条目」，审计
记「谁在什么时候对哪个 run 做了什么」。**不用** `GovernanceAuditService`：
它写的是表/列/密级/策略的 PDP 决策，字段语义不匹配。

审计是旁路：任何异常只 `logger.warning`，绝不抛给调用方、也不污染主事务
（`AuditLogService.record(sync=False)` 走异步 sink）。

KB 镜像不含 `capabilities/platform`（`deploy/docker/capability.Dockerfile`
只拷本能力目录），因此平台审计依赖**惰性导入**，导入失败时回退为直接 SQL
写入同一个 `platform.audit_logs` 表；直写路径需要自己 `commit()`。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

#: 平台审计枚举/服务的惰性加载槽位；未加载成功时保持 `None`。
LogType = None
Outcome = None
AuditLogService = None

_SUCCESS = "SUCCESS"
_FAILED = "FAILED"


def _load_platform():
    """惰性导入平台审计依赖，返回 `(LogType, Outcome, AuditLogService)`。

    KB 镜像不含 `capabilities/platform`，导入失败时三者皆为 `None`，由
    `record()` 回退到直写 SQL。模块级槽位一旦被外部（单测 monkeypatch）
    赋过值就**不覆盖**，便于继续从 `audit.AuditLogService` 打补丁。
    """
    global LogType, Outcome, AuditLogService
    try:
        from capabilities.platform.models.audit_enums import (
            LogType as _LogType,
            Outcome as _Outcome,
        )
        from capabilities.platform.services.audit_log_service import (
            AuditLogService as _AuditLogService,
        )
    except Exception:  # noqa: BLE001 —— 镜像裁剪后属预期缺失
        return None, None, None
    if LogType is None:
        LogType = _LogType
    if Outcome is None:
        Outcome = _Outcome
    if AuditLogService is None:
        AuditLogService = _AuditLogService
    return LogType, Outcome, AuditLogService


def _user_fields(user_id) -> tuple[int | None, str | None]:
    """`user_id` → (BigInteger 可写的 int | None, username)。

    审计表的 `user_id` 是 BigInteger：非数字身份（如 `u_admin`）只写
    `username`，不强行转换、也不丢审计。
    """
    if user_id is None:
        return None, None
    if isinstance(user_id, bool):  # bool 是 int 的子类，先挡掉
        return None, str(user_id)
    if isinstance(user_id, int):
        return user_id, str(user_id)
    raw = str(user_id).strip()
    if not raw:
        return None, None
    if raw.isdigit():
        try:
            return int(raw), raw
        except ValueError:  # 理论不可达（isdigit 已过滤）
            return None, raw
    return None, raw


def _sink_running() -> bool:
    """平台审计 sink 是否真的在消费（`_running` 且队列已建）。

    `AuditLogService.record(sync=False)` 只是把条目 `put()` 进 sink 队列；
    sink 未启动时 `put()` 只打 warning 然后**丢弃**，`record` 仍正常返回。
    KB 进程不会启动 platform 的 sink，所以「能否 import」绝不能作为选路依据
    —— 导入成功但 sink 没跑时走服务等于静默丢审计。
    """
    try:
        from capabilities.platform.services.audit_log_sink import (
            get_audit_log_sink,
        )
        sink = get_audit_log_sink()
    except Exception:  # noqa: BLE001 —— 镜像裁剪/未初始化都按「未运行」处理
        return False
    return bool(getattr(sink, "_running", False)) and \
        getattr(sink, "_queue", None) is not None


def _normalize_outcome(outcome) -> str:
    """平台枚举值是 `SUCCESS` / `FAILED`（大写）；接口接受小写/枚举两种写法。"""
    text = str(getattr(outcome, "value", outcome) or "").strip().upper()
    if text in (_SUCCESS, "OK"):
        return _SUCCESS
    return _FAILED


async def _write_direct(session, *, tenant_id: str, action: str, outcome: str,
                        run_id: str, request_params: dict | None,
                        error_message: str | None,
                        uid: int | None, username: str | None,
                        resource: str = "evolution_run") -> None:
    """无 `AuditLogService` 时直写 `platform.audit_logs`。

    与 `AuditLogService.record(sync=False)` 的异步 sink 不同，这条路径必须
    自己提交；提交失败先 `rollback()` 再上抛，避免把中毒的 session 交回调用方。
    `resource` 默认 `evolution_run`（旧行为），语料播种传 `evolution_corpus`。
    """
    from sqlalchemy import text

    stmt = text(
        """
        INSERT INTO platform.audit_logs (
            tenant_id, action, log_type, outcome, service_name,
            user_id, username, resource, resource_id, request_params,
            error_message, log_level, created_at, updated_at
        ) VALUES (
            :tenant_id, :action, 'OPERATION', :outcome, 'knowledge_base',
            :user_id, :username, :resource, :resource_id,
            CAST(:request_params AS jsonb), :error_message, :log_level,
            now(), now()
        )
        """
    )
    params = {
        "tenant_id": tenant_id,
        "action": action,
        "outcome": outcome,
        "user_id": uid,
        "username": username,
        "resource": resource,
        "resource_id": run_id,
        "request_params": (
            json.dumps(request_params, ensure_ascii=False)
            if request_params is not None else None
        ),
        "error_message": error_message,
        "log_level": "INFO" if outcome == _SUCCESS else "ERROR",
    }
    try:
        await session.execute(stmt, params)
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def record(session, *, tenant_id: str, action: str,
                 outcome="success", run_id: str,
                 request_params: dict | None = None,
                 error_message: str | None = None,
                 user_id=None, resource: str = "evolution_run") -> bool:
    """写一条演化操作审计；失败只 warning，不抛。

    返回 `True/False` 表示审计是否写入成功：采纳流程据此把失败升级成响应
    `warnings` + `evolution_run_logs` 一行（spec §9 / 交接 I7）。

    `resource` 默认 `evolution_run`（保持旧行为）；语料播种传
    `evolution_corpus`。两条写入路径（platform sink / 直写 SQL）都透传。
    """
    try:
        uid, username = _user_fields(user_id)
        log_type, _outcome, audit_service = _load_platform()
        normalized = _normalize_outcome(outcome)
        # 选路依据是 **sink 是否在跑**，不是「能否导入」：KB 镜像里 import
        # 通常成功，但 platform 的 sink 只在 platform 进程启动 —— 走服务只会
        # 把条目丢进没人消费的队列（`put()` 只 warning），审计静默丢失。
        if audit_service is not None and _sink_running():
            operation = (
                log_type.OPERATION.value if log_type is not None
                else "OPERATION"
            )
            await audit_service(session).record(
                tenant_id,
                operation,
                action,
                normalized,
                service_name="knowledge_base",
                user_id=uid,
                username=username,
                resource=resource,
                resource_id=run_id,
                request_params=request_params,
                error_message=error_message,
                sync=False,
            )
        else:
            await _write_direct(
                session, tenant_id=tenant_id, action=action,
                outcome=normalized, run_id=run_id,
                request_params=request_params,
                error_message=error_message, uid=uid, username=username,
                resource=resource)
        return True
    except Exception as exc:  # noqa: BLE001 —— 审计不是关键路径
        logger.warning("演化审计写入失败 action=%s run=%s: %s",
                       action, run_id, exc)
        return False


__all__ = ["record"]
