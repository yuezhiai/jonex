"""
业务领域 — 本体动作最小执行器

把「本体里写着该怎么做」接到「平台上真去做」：

    knowledge_base.prepare_action_execution（代调用人取计划，只解析、不执行）
      → 判定（D15）+ 调用方确认
      → 按绑定的工具组装入参
      → 按工具运行时配置调用（api / echo，公共收尾在 jonex_core/common/tool_invocation.py）
      → 结果与拦截记录落审计

权限
    只认调用人对该知识库的写权限（空间管理者 / 租户管理员 / kb_manager），由 knowledge_base
    在 prepare_action_execution 上判定；本执行器把调用人身份（含模拟态）原样带过去。
    没有用户身份的调用（内部链路、测试 token）直接拒绝。

边界（docs/ontology/action-execution-boundary-protocol.md）
    - 本执行器**不放宽**任何已声明的约束：判定为 manual / refuse 一律不执行；
      confirm 必须由调用方对每一类文字规则（权限要求、前置条件）显式确认。
      执行器无法自动校验「客服专员级及以上」这类文字，确认责任在调用方，
      确认结果写进审计。
    - follow_ups（提交后联动）与 failure_policy（失败处理规则）只回传给调用方，
      执行器不自动执行联动。
    - 工具有两个来源（绑定上的 tool_source）：
        platform  平台工具目录 SkillCatalog：运行时在 capability_json.runtime，凭据在
                  capability_json.runtime_secret（加密），由本执行器直接调用；
        kb        知识库工具 knowledge_base.kb_tools：运行时与凭据都留在 knowledge_base，
                  本执行器经 Sidecar 调它的内部 action `run_kb_tool`，凭据不出 knowledge_base。
      两边的运行时格式与调用逻辑同一份：jonex_core/common/tool_runtime.py（echo / api）。
      没有 runtime 的工具返回 no_runtime（目录里现有的 MCP 制品类工具没有可调入口）。
    - api 运行时出站前做 SSRF 校验：默认禁止私网 / 回环 / 链路本地，内网工具要在
      ONTOLOGY_TOOL_RUNTIME_ALLOWED_HOSTS 里显式放行；不跟随重定向。
    - 调用方传入的 inputs 可能含业务敏感值（卡号等），审计与日志只记键名不记值。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

from jonex_core.common import get_db_session
from jonex_core.common.audit import schedule_emit
from jonex_core.common.exceptions import (
    CapabilityInvokeError,
    InvalidParameterError,
    JonexException,
    PermissionDeniedError,
    UpstreamServiceError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.security.permission import get_impersonation_context
from jonex_core.common.tool_invocation import STATUS_TOOL_UNAVAILABLE, invoke_tool
from jonex_core.common.tool_runtime import (
    RUNTIME_API,
    RUNTIME_ECHO,
    run_runtime,
    runtime_allowed_hosts,
)

from capabilities.business_domain.repository.skill_repository import (
    SkillCatalogRepository,
    TenantSkillRepository,
)

logger = logging.getLogger(__name__)

TOOL_SOURCE_PLATFORM = "platform"
TOOL_SOURCE_KB = "kb"

# 执行结果状态
STATUS_EXECUTED = "executed"
STATUS_PLANNED = "planned"              # dry_run：判定通过、入参齐全，但未调用
STATUS_BLOCKED = "blocked"              # 判定 refuse
STATUS_MANUAL = "manual"                # 判定 manual：转人工
STATUS_NEEDS_CONFIRMATION = "needs_confirmation"
STATUS_INVALID_INPUT = "invalid_input"  # 缺必填入参
STATUS_NO_RUNTIME = "no_runtime"        # 工具没有可调用的运行时配置
STATUS_FAILED = "failed"                # 工具调用失败（网络 / 非 2xx / 超时）


def build_arguments(parameters: dict, inputs: dict, input_schema: dict) -> tuple[dict, list[str]]:
    """按执行计划组装工具入参，返回 `(arguments, missing_required)`。

    取值顺序：计划里已解析的值（本体属性 / 实例名 / 字面量）→ `$input.*` 引用的调用方输入
    → 工具 schema 里声明、但映射没写的入参，直接取调用方同名输入。
    """
    args = dict((parameters or {}).get("resolved") or {})
    for param, key in ((parameters or {}).get("input_refs") or {}).items():
        if key in inputs:
            args[param] = inputs[key]
    props = ((input_schema or {}).get("properties") or {}).keys()
    for name in props:
        if name not in args and name in inputs:
            args[name] = inputs[name]
    required = [str(r) for r in ((input_schema or {}).get("required") or [])]
    missing = [r for r in required if args.get(r) in (None, "")]
    return args, missing


def check_confirmations(checklist: list[dict], confirmations: dict) -> list[dict]:
    """返回尚未确认的检查项。`confirmations` 形如 `{"authorization": true, "preconditions": true}`。"""
    key_of = {"authorization": "authorization", "precondition": "preconditions"}
    pending = []
    for item in checklist or []:
        key = key_of.get(item.get("kind"), item.get("kind"))
        if confirmations.get(key) is not True:
            pending.append(item)
    return pending


_OUTCOME_STATUSES = (STATUS_EXECUTED, STATUS_FAILED, STATUS_NO_RUNTIME, STATUS_INVALID_INPUT)
_OUTCOME_FIELDS = ("output", "output_truncated", "http_status", "error", "missing_inputs")


def _outcome_fields(outcome: dict) -> tuple[str, dict]:
    """把一次工具调用的结果（`invoke_tool` 或 knowledge_base `run_kb_tool` 回的同形结果）
    转成执行结果的 `(status, 附加字段)`；认不出的状态按失败处理。"""
    status = outcome.get("status")
    if status not in _OUTCOME_STATUSES:
        status = STATUS_FAILED
    extra = {k: outcome[k] for k in _OUTCOME_FIELDS if outcome.get(k) is not None}
    if status == STATUS_INVALID_INPUT:
        extra.setdefault("missing_inputs", [])
    return status, extra


# ── 运行时 ──────────────────────────────────────────────────────────

# (runtime, arguments, context) -> {"ok", "output", "http_status"?}；context["secret"] 是解密后的凭据
Runner = Callable[[dict, dict, dict], Awaitable[dict]]


async def _run_shared(runtime: dict, arguments: dict, context: dict) -> dict:
    return await run_runtime(
        runtime, arguments, secret=context.get("secret") or "",
        context={k: v for k, v in context.items() if k != "secret"},
    )


DEFAULT_RUNNERS: dict[str, Runner] = {RUNTIME_ECHO: _run_shared, RUNTIME_API: _run_shared}


# ── 执行器 ──────────────────────────────────────────────────────────


class OntologyActionExecutor:
    """最小执行器：只执行判定通过、确认齐全、入参齐全的单个动作。"""

    def __init__(
        self,
        kb_client_factory: Optional[Callable[[], Any]] = None,
        runners: Optional[dict[str, Runner]] = None,
        audit: Optional[Callable[[dict], None]] = None,
    ):
        if kb_client_factory is None:
            from capabilities.business_domain.clients.knowledge_base_client import (
                get_knowledge_base_client,
            )
            kb_client_factory = get_knowledge_base_client
        self._kb = kb_client_factory
        self._runners = runners or DEFAULT_RUNNERS
        self._audit = audit or schedule_emit

    async def _prepare(
        self, tenant_id: str, kb_id: str, action_name: str, *,
        user_id: str, username: Optional[str], request_id: Optional[str],
    ) -> dict:
        """代调用人取执行计划：knowledge_base 按调用人的知识库写权限判定（空间管理者 /
        租户管理员 / kb_manager），没有写权限直接拒绝 —— 执行权限只认这一处。"""
        ctx = get_impersonation_context() or {}
        extra = (
            {"impersonated": True, "perms": ctx.get("perms") or [],
             "original_tenant_id": ctx.get("original_tenant_id")}
            if ctx.get("impersonated") else None
        )
        try:
            return await self._kb().invoke(
                "prepare_action_execution",
                {"knowledge_base_id": kb_id, "action_name": action_name},
                tenant_id,
                user_id=user_id, username=username, request_id=request_id, extra=extra,
            ) or {}
        except CapabilityInvokeError as exc:
            # 知识库侧的权限拒绝原样还给调用方（否则前端只看到一个笼统的调用失败）
            if (exc.details or {}).get("upstream_code") == PermissionDeniedError.code:
                raise PermissionDeniedError(
                    message=(exc.details or {}).get("upstream_message") or None,
                )
            raise
        except JonexException:
            raise
        except httpx.HTTPError as exc:
            raise UpstreamServiceError(
                message="知识库执行解析调用失败",
                details={"action": "prepare_action_execution", "error": type(exc).__name__},
            )

    async def _run_kb_tool(
        self, tenant_id: str, kb_id: str, tool_name: str, arguments: dict, context: dict,
    ) -> dict:
        """经 knowledge_base 内部 action 调用本库工具（凭据不离开 knowledge_base）。"""
        try:
            return await self._kb().invoke(
                "run_kb_tool",
                {
                    "knowledge_base_id": kb_id, "tool_name": tool_name, "arguments": arguments,
                    "action_name": context.get("action_name"), "request_id": context.get("request_id"),
                },
                tenant_id,
            ) or {}
        except JonexException:
            raise
        except httpx.HTTPError as exc:
            raise UpstreamServiceError(
                message="知识库工具调用失败",
                details={"action": "run_kb_tool", "error": type(exc).__name__},
            )

    async def _tool(self, tenant_id: str, tool_name: str):
        """已发布且本租户已启用的工具；否则 None（知识库侧已校验过，这里再查一次防竞态）。"""
        async with get_db_session() as session:
            skill = await SkillCatalogRepository(session).get_published_by_tool_name(tool_name)
            if skill is None:
                return None
            ts = await TenantSkillRepository(session).get_by_skill_id(tenant_id, skill.id)
            if ts is None or ts.status != "enabled":
                return None
            return skill

    async def execute(
        self,
        tenant_id: str,
        data: dict,
        *,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id = str(data.get("knowledge_base_id") or "").strip()
        action_name = str(data.get("action_name") or "").strip()
        inputs = data.get("inputs") or {}
        confirmations = data.get("confirmations") or {}
        dry_run = bool(data.get("dry_run", False))
        if not kb_id or not action_name:
            raise InvalidParameterError(message="knowledge_base_id 与 action_name 必填")
        if not isinstance(inputs, dict) or not isinstance(confirmations, dict):
            raise InvalidParameterError(message="inputs 与 confirmations 必须是对象")
        if not user_id or str(user_id) == "anonymous":
            # 执行权限只认调用人的知识库写权限；没有用户身份（内部调用 / 测试 token）无从判定，拒绝
            raise PermissionDeniedError(message=translate(
                "err.ontology_action.user_required",
                fallback="执行本体动作需要登录用户身份，内部调用或测试 token 不能执行",
            ))

        t0 = time.perf_counter()
        plan = await self._prepare(
            tenant_id, kb_id, action_name,
            user_id=str(user_id), username=username, request_id=request_id,
        )
        decision = plan.get("decision") or {}
        binding = plan.get("binding") or {}
        contract = plan.get("contract") or {}
        result: dict = {
            "status": None,
            "knowledge_base_id": kb_id,
            "action": plan.get("action"),
            "decision": decision,
            "tool_name": binding.get("tool_name"),
            "arguments": None,
            "output": None,
            "post_actions": {
                # 提交后联动只回传，执行器不自动执行
                "follow_ups": (contract.get("follow_ups") or {}).get("value") or [],
                "failure_policy": (contract.get("failure_policy") or {}).get("value") or "",
            },
            "dry_run": dry_run,
        }

        def _finish(status: str, **extra) -> dict:
            result["status"] = status
            result.update(extra)
            result["duration_ms"] = int((time.perf_counter() - t0) * 1000)
            self._emit_audit(tenant_id, user_id, username, request_id, result, inputs, confirmations)
            return result

        verdict = decision.get("verdict")
        if verdict == "refuse" or not decision:
            return _finish(STATUS_BLOCKED)
        if verdict == "manual":
            return _finish(STATUS_MANUAL)
        pending = check_confirmations(decision.get("checklist") or [], confirmations)
        if pending:
            return _finish(STATUS_NEEDS_CONFIRMATION, pending_confirmations=pending)

        tool_name = binding.get("tool_name") or ""
        source = binding.get("tool_source") or TOOL_SOURCE_PLATFORM
        result["tool_source"] = source
        skill = None
        if source == TOOL_SOURCE_KB:
            # 本库工具的可用性由知识库在解析时给出；真正调用时 run_kb_tool 会再查一次
            if not binding.get("tool_available"):
                return _finish(STATUS_BLOCKED, error="tool_unavailable")
            input_schema = binding.get("input_schema") or {}
        else:
            skill = await self._tool(tenant_id, tool_name)
            if skill is None:
                return _finish(STATUS_BLOCKED, error="tool_unavailable")
            input_schema = skill.input_schema_json or {}

        arguments, missing = build_arguments(plan.get("parameters") or {}, inputs, input_schema)
        result["arguments_keys"] = sorted(arguments.keys())
        if missing:
            return _finish(STATUS_INVALID_INPUT, missing_inputs=missing)
        result["arguments"] = arguments
        if dry_run:
            return _finish(STATUS_PLANNED)

        context = {
            "tenant_id": tenant_id, "knowledge_base_id": kb_id, "action_name": action_name,
            "tool_name": tool_name, "tool_source": source, "request_id": request_id,
        }
        if source == TOOL_SOURCE_KB:
            outcome = await self._run_kb_tool(tenant_id, kb_id, tool_name, arguments, context)
            if outcome.get("status") == STATUS_TOOL_UNAVAILABLE:
                return _finish(STATUS_BLOCKED, error=STATUS_TOOL_UNAVAILABLE)
        else:
            cap = skill.capability_json or {}
            runtime = cap.get("runtime") or {}
            runner = self._runners.get(str(runtime.get("type") or ""))
            # 入参必填已由 build_arguments 校验，这里不再按 schema 复查
            outcome = await invoke_tool(
                runtime if runner is not None else None,
                cap.get("runtime_secret"),
                None,
                arguments,
                context,
                runner=(
                    (lambda rt, a, s, c: runner(rt, a, {**c, "secret": s}))
                    if runner is not None else None
                ),
            )
        status, extra = _outcome_fields(outcome)
        return _finish(status, **extra)

    def _emit_audit(
        self, tenant_id: str, user_id: Optional[str], username: Optional[str],
        request_id: Optional[str], result: dict, inputs: dict, confirmations: dict,
    ) -> None:
        """每次执行尝试（含拦截）落一条审计；入参只记键名。"""
        try:
            uid = int(user_id) if user_id else None
        except (TypeError, ValueError):
            uid = None
        decision = result.get("decision") or {}
        entry = {
            "tenant_id": tenant_id,
            "log_type": "OPERATION",
            "action": "execute_ontology_action",
            "outcome": "SUCCESS" if result.get("status") in (STATUS_EXECUTED, STATUS_PLANNED) else "FAILED",
            "service_name": "business_domain",
            "user_id": uid,
            "username": username,
            "resource": "skill",
            "resource_id": result.get("tool_name"),
            "trace_id": request_id,
            "duration_ms": result.get("duration_ms"),
            "request_params": {
                "knowledge_base_id": result.get("knowledge_base_id"),
                "action_name": (result.get("action") or {}).get("name"),
                "tool_source": result.get("tool_source"),
                "input_keys": sorted(inputs.keys()),
                "confirmations": {k: bool(v) for k, v in confirmations.items()},
                "dry_run": result.get("dry_run"),
            },
            "response_body": {
                "status": result.get("status"),
                "verdict": decision.get("verdict"),
                "reasons": [r.get("code") for r in decision.get("reasons") or []],
                "http_status": result.get("http_status"),
                "error": result.get("error"),
            },
        }
        try:
            self._audit(entry)
        except Exception as exc:  # noqa: BLE001 — 审计失败不影响执行结果
            logger.warning("本体动作执行审计写入失败: %s", exc)


__all__ = [
    "DEFAULT_RUNNERS",
    "OntologyActionExecutor",
    "TOOL_SOURCE_KB",
    "TOOL_SOURCE_PLATFORM",
    "build_arguments",
    "check_confirmations",
    "runtime_allowed_hosts",
]
