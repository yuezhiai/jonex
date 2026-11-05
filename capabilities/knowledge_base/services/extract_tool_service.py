#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""抽取期调用工具：执行 TBox 里的 `catalog:` 声明（写图前，在 knowledge-base 内）。

声明格式与校验见 `jonex_core/common/catalog_skill_spec.py`。抽取器不执行这些声明 ——
工具定义、凭据、出站地址校验与审计都在 knowledge-base，所以放在对账写图前统一执行：
`reconciliation_service._handle_completed` 在同义词归一之后、`_rewrite_ontology_graph` 之前
调 `ExtractToolService().apply(...)`，拿回改过的 `ont_data` 和一份摘要。

三道开关（AND）
    1. 进程级总闸 `ONTOLOGY_EXTRACT_TOOL_ENABLED`（默认关）；
    2. 库级 `knowledge_info.extract_tool_calls`，只有明确为 TRUE 才算开；
    3. 编译 schema 的 `entity_types[].extraction_skills` 里有 `catalog:` 声明。
    任一不满足：不调用、摘要为 None（调用方据此清掉上一轮的摘要）。

工具解析
    先找本库工具（`kb_tools`，同一知识库、已启用），再找本租户已启用的平台工具
    （`skill_catalog` 已上架 + `tenant_skills.status = 'enabled'`）。工具必须声明
    `read_only`：写图前的调用不经人确认，不允许改动对方系统的数据。

成本控制（每篇文档）
    调用次数上限、总耗时预算、并发、单次超时上限；同一篇里入参相同只调一次；
    成功结果按「工具运行时 + 入参」缓存在 Redis（改了工具定义缓存自然失效）；
    每知识库每工具每天限次（Redis 不可用时放行）。超时或对方 5xx 重试一次。

失败处理
    任何失败都不让文档失败：refine 按 `on_error` 保留或清掉 `writes` 里的属性，
    constrain 按 `on_error` 放行或丢弃。本方法从不抛异常，内部异常时原样返回 `ont_data`。

留痕
    工具写入的属性记在实体属性 `_tool_sources: {属性: "tool:<工具名>"}`；
    每次真实调用一条审计（action = `ontology_extract_tool`，入参只记键名）；
    每篇文档一行 `[extract-tool] ...` 日志；摘要由调用方写进
    `knowledge_documents.extra_metadata["ontology_enrich"]`。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text

from jonex_core.common.audit import schedule_emit
from jonex_core.common.cache import get_redis_client
from jonex_core.common.catalog_skill_spec import (
    CatalogSkillCall,
    parse_catalog_calls,
    parse_catalog_item,
    resolve_inputs,
)
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.common.tool_invocation import (
    STATUS_EXECUTED,
    STATUS_FAILED,
    Runner,
    invoke_tool,
    tool_audit_entry,
)
from jonex_core.common.tool_runtime import extract_path

from ..models.ontology_tool_binding import TOOL_SOURCE_KB, TOOL_SOURCE_PLATFORM
from ..repository.kb_tool_repository import KbToolRepository

logger = logging.getLogger(__name__)

MASTER_SWITCH_ENV = "ONTOLOGY_EXTRACT_TOOL_ENABLED"
AUDIT_ACTION = "ontology_extract_tool"
TOOL_SOURCES_ATTR = "_tool_sources"
MAX_SUMMARY_ERRORS = 10
# 工具输出进实体属性前的上限（字符）：属性随实体写进图、进检索上下文，不能无限大
MAX_OUTPUT_CHARS = 8000

# 试跑
PREVIEW_AUDIT_ACTION = "preview_extract_tool"
PREVIEW_NO_ENTITY = "no_entity"            # 图里还没有这一类型的实体
PREVIEW_MISSING_INPUTS = "missing_inputs"  # 实体上取不到声明要的入参

SUMMARY_OK = "ok"
SUMMARY_PARTIAL = "partial"
SUMMARY_SKIPPED = "skipped"
SUMMARY_ERROR = "error"


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def extract_tool_master_switch_on() -> bool:
    """进程级总闸是否打开（缺省关）。读时求值，便于测试与详情页展示。"""
    return _env_flag(MASTER_SWITCH_ENV)


@dataclass
class ExtractToolSettings:
    max_calls: int        # 每篇文档最多真实调用次数（不含重试）
    doc_budget: float     # 每篇文档的总耗时预算（秒）
    concurrency: int
    timeout: float        # 单次调用超时上限（秒），与工具自身 timeout 取较小值
    cache_ttl: int        # 成功结果缓存秒数；0 = 不缓存
    rate_per_day: int     # 每知识库每工具每天调用上限；0 = 不限

    @classmethod
    def from_env(cls) -> "ExtractToolSettings":
        return cls(
            max_calls=_env_int("ONTOLOGY_EXTRACT_TOOL_MAX_CALLS_PER_DOC", 20, 0, 500),
            doc_budget=float(_env_int("ONTOLOGY_EXTRACT_TOOL_DOC_BUDGET", 30, 1, 600)),
            concurrency=_env_int("ONTOLOGY_EXTRACT_TOOL_CONCURRENCY", 4, 1, 16),
            timeout=float(_env_int("ONTOLOGY_EXTRACT_TOOL_TIMEOUT", 5, 1, 30)),
            cache_ttl=_env_int("ONTOLOGY_EXTRACT_TOOL_CACHE_TTL", 86400, 0, 30 * 86400),
            rate_per_day=_env_int("ONTOLOGY_EXTRACT_TOOL_RATE_PER_DAY", 1000, 0, 1_000_000),
        )


@dataclass
class ExtractTool:
    source: str                    # kb | platform
    tool_name: str
    runtime: Optional[dict]
    secret_encrypted: Optional[str]
    input_schema: Optional[dict]
    read_only: bool


@dataclass
class _Outcome:
    ok: bool
    output: Any = None
    error: Optional[str] = None
    cached: bool = False
    skipped: bool = False          # 没调用（预算 / 限次 / 工具不可用）


ToolLoader = Callable[[str, str, str], Awaitable[Optional[ExtractTool]]]


def _args_key(tool: ExtractTool, args: dict) -> str:
    """同一篇内去重、跨文档缓存的键：工具运行时 + 入参（改了工具定义就换键）。"""
    raw = json.dumps(
        {"rt": tool.runtime or {}, "args": args}, ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _matches(value: Any, expected: Any) -> bool:
    """reject_when 的相等判定：布尔只和布尔比（避免 1 == True），数字之间按数值比。"""
    if expected is None or value is None:
        return value is None and expected is None
    if isinstance(expected, bool) or isinstance(value, bool):
        return isinstance(expected, bool) and isinstance(value, bool) and value == expected
    if isinstance(expected, (int, float)) and isinstance(value, (int, float)):
        return float(value) == float(expected)
    return value == expected


def _fit_value(value: Any) -> Any:
    """写进属性前限长：超长的值按 JSON 文本截断。"""
    try:
        size = len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        value, size = str(value), len(str(value))
    if size <= MAX_OUTPUT_CHARS:
        return value
    text_value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text_value[:MAX_OUTPUT_CHARS]


class ExtractToolService:
    """写图前执行 `catalog:` 声明。依赖都可注入，单测不碰数据库 / Redis / 网络。"""

    def __init__(
        self,
        *,
        settings: Optional[ExtractToolSettings] = None,
        tool_loader: Optional[ToolLoader] = None,
        runner: Optional[Runner] = None,
        kb_switch: Optional[Callable[[str, str], Awaitable[bool]]] = None,
        schema_loader: Optional[Callable[[str, str], Awaitable[Optional[dict]]]] = None,
        entity_loader: Optional[
            Callable[[str, str, str, Optional[str]], Awaitable[tuple[Optional[dict], list[str]]]]
        ] = None,
        use_redis: bool = True,
        emit_audit: bool = True,
    ):
        self._entity_loader = entity_loader or self._graph_entity
        self._settings = settings
        self._tool_loader = tool_loader or self._load_tool
        self._runner = runner
        self._kb_switch = kb_switch or self._kb_enabled
        self._schema_loader = schema_loader or self._load_schema
        self._use_redis = use_redis
        self._emit_audit_on = emit_audit

    # ── 入口 ─────────────────────────────────────────────────────

    async def apply(
        self, tenant_id: str, kb_id: str, doc_id: str, ont_data: dict,
    ) -> tuple[dict, Optional[dict]]:
        """返回 `(ont_data, 摘要)`；开关没开或没有声明时摘要为 None。从不抛异常。"""
        try:
            return await self._apply(tenant_id, kb_id, doc_id, ont_data)
        except Exception as exc:  # noqa: BLE001 — 抽取期工具失败绝不影响文档收尾
            logger.warning(
                "[extract-tool] 执行异常，按未调用处理 kb=%s doc=%s err=%s", kb_id, doc_id, exc,
            )
            return ont_data, {
                "status": SUMMARY_ERROR, "calls": 0, "cached": 0, "ok": 0, "failed": 0,
                "skipped": 0, "rejected": 0, "attrs_written": 0, "budget_exhausted": False,
                "errors": [f"internal:{type(exc).__name__}"],
                "at": datetime.now(timezone.utc).isoformat(),
            }

    async def _apply(
        self, tenant_id: str, kb_id: str, doc_id: str, ont_data: dict,
    ) -> tuple[dict, Optional[dict]]:
        if not extract_tool_master_switch_on() or not isinstance(ont_data, dict):
            return ont_data, None
        if not ont_data.get("entities"):
            return ont_data, None
        tenant_id = require_tenant(tenant_id)
        if not await self._kb_switch(tenant_id, kb_id):
            return ont_data, None
        schema = await self._schema_loader(tenant_id, kb_id) or {}
        decl: dict[str, list[CatalogSkillCall]] = {}
        decl_errors: list[str] = []
        for et in schema.get("entity_types") or []:
            if not isinstance(et, dict):
                continue
            calls, errs = parse_catalog_calls(et.get("extraction_skills"))
            decl_errors.extend(errs)
            if calls:
                decl[str(et.get("name") or "")] = calls
        if decl_errors:
            logger.warning("[extract-tool] catalog 声明有误，已跳过错误项 kb=%s: %s",
                           kb_id, "; ".join(decl_errors[:5]))
        if not decl:
            return ont_data, None

        run = _DocRun(self, tenant_id, kb_id, doc_id, self._settings or ExtractToolSettings.from_env())
        data = dict(ont_data)
        data["entities"] = copy.deepcopy(list(ont_data.get("entities") or []))
        data["relations"] = list(ont_data.get("relations") or [])
        for etype, calls in decl.items():
            for call in calls:
                await run.run_stage(data["entities"], etype, call)
        if run.rejected_keys:
            data["entities"] = [
                e for e in data["entities"]
                if (e.get("entity_type"), e.get("canonical_name")) not in run.rejected_keys
            ]
            data["relations"] = [r for r in data["relations"] if not run.touches_rejected(r)]
        summary = run.summary()
        logger.info(
            "[extract-tool] kb=%s doc=%s status=%s calls=%d cached=%d ok=%d failed=%d "
            "skipped=%d rejected=%d attrs=%d budget_exhausted=%s ms=%d",
            kb_id, doc_id, summary["status"], summary["calls"], summary["cached"], summary["ok"],
            summary["failed"], summary["skipped"], summary["rejected"], summary["attrs_written"],
            summary["budget_exhausted"], run.elapsed_ms(),
        )
        return data, summary

    # ── 试跑（编辑对象弹窗）──────────────────────────────────────

    async def preview(
        self, tenant_id: str, kb_id: str, entity_type: str, group: str, declaration: dict,
        canonical_name: Optional[str] = None, *, user_id: Optional[str] = None,
        username: Optional[str] = None, request_id: Optional[str] = None,
    ) -> dict:
        """拿图里一个现有实体，按（可能还没保存的）声明真实调用一次工具，返回会发生什么。

        不写图、不读写缓存、不占每日限次；不看总闸与库级开关（配置声明时就要能试）。
        工具同样必须声明只读。每次真实调用落一条审计（action = `preview_extract_tool`）。
        """
        tenant_id = require_tenant(tenant_id)
        if group not in ("refine", "constrain") or not isinstance(declaration, dict):
            raise InvalidParameterError(message=translate(
                "err.extract_tool.invalid_declaration", params={"errors": "group / declaration"},
                fallback="抽取期工具声明不合法：group / declaration",
            ))
        call, errs = parse_catalog_item(group, declaration)
        if call is None:
            raise InvalidParameterError(
                message=translate(
                    "err.extract_tool.invalid_declaration", params={"errors": "；".join(errs)[:500]},
                    fallback=f"抽取期工具声明不合法：{'；'.join(errs)[:500]}",
                ),
                details={"errors": errs},
            )
        entity, candidates = await self._entity_loader(tenant_id, kb_id, entity_type, canonical_name)
        out: dict = {"status": PREVIEW_NO_ENTITY, "candidates": candidates, "entity": None,
                     "inputs": None, "tool": None, "group": group}
        if entity is None:
            return out
        before = copy.deepcopy(entity.get("attributes") or {})
        out["entity"] = {"canonical_name": entity.get("canonical_name"),
                         "entity_type": entity.get("entity_type"), "attributes": before}
        args = resolve_inputs(call.inputs, entity)
        out["inputs"] = args
        if args is None:
            out["status"] = PREVIEW_MISSING_INPUTS
            return out
        timeout = (self._settings or ExtractToolSettings.from_env()).timeout
        settings = ExtractToolSettings(
            max_calls=1, doc_budget=timeout + 2, concurrency=1, timeout=timeout,
            cache_ttl=0, rate_per_day=0,
        )
        run = _DocRun(self, tenant_id, kb_id, None, settings, audit_action=PREVIEW_AUDIT_ACTION,
                      use_redis=False, user_id=user_id, username=username, request_id=request_id)
        tool = await run._tool(call.tool_name)
        if tool is None:
            reason = (run.errors[0].split(":", 1)[1] if run.errors else "tool_unavailable")
            out["status"] = reason
            return out
        out["tool"] = {"source": tool.source, "tool_name": tool.tool_name}
        work = copy.deepcopy(entity)
        await run.run_stage([work], str(entity.get("entity_type") or entity_type), call)
        outcome = next(iter(run.memo.values()), None)
        if outcome is None:
            out["status"] = STATUS_FAILED
            return out
        out["status"] = STATUS_EXECUTED if outcome.ok else STATUS_FAILED
        out["output"] = outcome.output
        out["error"] = outcome.error
        if group == "refine":
            after = work.get("attributes") or {}
            out["attributes_after"] = after
            writes: dict = {}
            if outcome.ok:
                for attr, path in call.writes.items():
                    value = extract_path(outcome.output, path)
                    if value is not None:
                        writes[attr] = _fit_value(value)
            out["writes"] = writes
            # 输出里取不到值的写入项：前端据此提示「路径可能写错了」
            out["unresolved_writes"] = [a for a in call.writes if outcome.ok and a not in writes]
            out["dropped"] = [a for a in call.writes if a in before and a not in after]
        else:
            out["rejected"] = bool(run.rejected_keys)
        return out

    # ── 默认依赖 ─────────────────────────────────────────────────

    @staticmethod
    async def _graph_entity(
        tenant_id: str, kb_id: str, entity_type: str, canonical_name: Optional[str],
    ) -> tuple[Optional[dict], list[str]]:
        """试跑取样：最近更新的 20 个同类型实体名作候选；没指定名字时取第一个。"""
        from jonex_core.common.neo4j_client import get_neo4j_driver

        from ..repository.ontology_graph_repository import OntologyGraphRepository

        repo = OntologyGraphRepository(get_neo4j_driver())
        rows, _ = await repo.list_entities(tenant_id, kb_id, limit=20, entity_type=entity_type)
        candidates = [str(r.get("name")) for r in rows if r.get("name")]
        name = canonical_name or (candidates[0] if candidates else None)
        if not name:
            return None, candidates
        row = await repo.get_entity(tenant_id, kb_id, entity_type, name)
        if row is None:
            return None, candidates
        attrs = row.get("attributes")
        return {"canonical_name": row.get("canonical_name"), "entity_type": row.get("entity_type"),
                "attributes": attrs if isinstance(attrs, dict) else {}}, candidates

    @staticmethod
    async def _kb_enabled(tenant_id: str, kb_id: str) -> bool:
        from .kb_type_service import get_kb_extract_tool_calls

        return await get_kb_extract_tool_calls(tenant_id, kb_id)

    @staticmethod
    async def _load_schema(tenant_id: str, kb_id: str) -> Optional[dict]:
        from .ontology_compiler import OntologyCompiler

        return await OntologyCompiler().get_compiled_schema(tenant_id, kb_id, auto_compile=False)

    @staticmethod
    async def _load_tool(tenant_id: str, kb_id: str, tool_name: str) -> Optional[ExtractTool]:
        """先本库已启用的工具，再本租户已启用的平台工具；都没有返回 None。"""
        async with get_db_session() as session:
            obj = await KbToolRepository(session).get_by_name(tenant_id, kb_id, tool_name)
            if obj is not None and obj.enabled:
                return ExtractTool(
                    source=TOOL_SOURCE_KB, tool_name=obj.tool_name, runtime=obj.runtime,
                    secret_encrypted=obj.secret_encrypted, input_schema=obj.input_schema or {},
                    read_only=obj.read_only is True,
                )
            row = (await session.execute(text(
                "SELECT sc.capability_json, sc.input_schema_json FROM business_domain.skill_catalog sc "
                "JOIN business_domain.tenant_skills ts "
                "  ON ts.skill_id = sc.id AND ts.tenant_id = :t "
                " AND ts.status = 'enabled' AND ts.is_deleted = 0 "
                "WHERE sc.is_deleted = 0 AND sc.status = 'published' AND sc.tool_name = :n"
            ), {"t": tenant_id, "n": tool_name})).first()
        if row is None:
            return None
        cap = row[0] or {}
        return ExtractTool(
            source=TOOL_SOURCE_PLATFORM, tool_name=tool_name, runtime=cap.get("runtime"),
            secret_encrypted=cap.get("runtime_secret"), input_schema=row[1] or {},
            # 只认字面 true，其它一律按写操作
            read_only=cap.get("read_only") is True,
        )


class _DocRun:
    """一篇文档的一次执行：计数、预算、去重与工具解析结果都只在这一篇内有效。"""

    def __init__(self, svc: ExtractToolService, tenant_id: str, kb_id: str, doc_id: Optional[str],
                 settings: ExtractToolSettings, *, audit_action: str = AUDIT_ACTION,
                 use_redis: Optional[bool] = None, user_id: Optional[str] = None,
                 username: Optional[str] = None, request_id: Optional[str] = None):
        self.svc = svc
        self.tenant_id = tenant_id
        self.kb_id = kb_id
        self.doc_id = doc_id
        self.s = settings
        self.audit_action = audit_action
        # 试跑不读写缓存、不占每日限次：要看到的是工具此刻的真实返回
        self.use_redis = svc._use_redis if use_redis is None else use_redis
        self.user_id = user_id
        self.username = username
        self.request_id = request_id
        self.t0 = time.monotonic()
        self.deadline = self.t0 + settings.doc_budget
        self.sem = asyncio.Semaphore(settings.concurrency)
        self.tools: dict[str, Optional[ExtractTool]] = {}
        self.memo: dict[str, _Outcome] = {}
        self.rejected_keys: set[tuple] = set()
        self.calls = 0
        self.cached = 0
        self.ok = 0
        self.failed = 0
        self.skipped = 0
        self.attrs_written = 0
        self.budget_exhausted = False
        self.errors: list[str] = []

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def _error(self, msg: str) -> None:
        if len(self.errors) < MAX_SUMMARY_ERRORS and msg not in self.errors:
            self.errors.append(msg)

    # ── 一条声明作用于一类实体 ───────────────────────────────────

    async def run_stage(self, entities: list[dict], etype: str, call: CatalogSkillCall) -> None:
        targets = [
            e for e in entities
            if isinstance(e, dict) and e.get("entity_type") == etype
            and (e.get("entity_type"), e.get("canonical_name")) not in self.rejected_keys
        ]
        if not targets:
            return
        tool = await self._tool(call.tool_name)
        groups: dict[str, tuple[dict, list[dict]]] = {}
        for e in targets:
            args = resolve_inputs(call.inputs, e)
            if args is None:
                self.skipped += 1
                continue
            if tool is None:
                # 工具不可用是配置问题、不是调用失败：不套用 on_error，实体原样保留
                self.skipped += 1
                continue
            key = _args_key(tool, args)
            groups.setdefault(key, (args, []))[1].append(e)
        if not groups:
            return

        async def _one(key: str, args: dict, members: list[dict]) -> None:
            outcome = await self._outcome(tool, key, args, members[0], etype)
            for e in members:
                self._apply_outcome(e, call, outcome)

        await asyncio.gather(*(_one(k, a, m) for k, (a, m) in groups.items()))

    async def _tool(self, tool_name: str) -> Optional[ExtractTool]:
        if tool_name in self.tools:
            return self.tools[tool_name]
        try:
            tool = await self.svc._tool_loader(self.tenant_id, self.kb_id, tool_name)
        except Exception as exc:  # noqa: BLE001 — 查不到工具按不可用处理
            logger.warning("[extract-tool] 读取工具失败 tool=%s err=%s", tool_name, exc)
            tool = None
        if tool is None:
            self._error(f"{tool_name}:tool_unavailable")
        elif not tool.read_only:
            self._error(f"{tool_name}:not_read_only")
            tool = None
        elif not tool.runtime:
            self._error(f"{tool_name}:no_runtime")
            tool = None
        self.tools[tool_name] = tool
        return tool

    # ── 一次（去重后的）调用 ─────────────────────────────────────

    async def _outcome(self, tool: ExtractTool, key: str, args: dict, entity: dict, etype: str) -> _Outcome:
        if key in self.memo:
            self.cached += 1
            return self.memo[key]
        cached = await self._cache_get(tool, key)
        if cached is not None:
            self.cached += 1
            out = _Outcome(ok=True, output=cached, cached=True)
            self.memo[key] = out
            return out
        # 预算与次数在 await 之前占位：单线程事件循环里「检查 + 计数」不会被打断
        if self.calls >= self.s.max_calls or time.monotonic() >= self.deadline:
            self.budget_exhausted = True
            self.skipped += 1
            self._error(f"{tool.tool_name}:budget_exhausted")
            return _Outcome(ok=False, error="budget_exhausted", skipped=True)
        self.calls += 1
        if not await self._rate_allowed(tool):
            self.calls -= 1
            self.skipped += 1
            self._error(f"{tool.tool_name}:rate_limited")
            return _Outcome(ok=False, error="rate_limited", skipped=True)
        async with self.sem:
            result = await self._invoke(tool, args, entity, etype)
            if not result.get("ok") and self._retryable(result) and time.monotonic() < self.deadline:
                result = await self._invoke(tool, args, entity, etype)
        if result.get("ok"):
            self.ok += 1
            out = _Outcome(ok=True, output=result.get("output"))
            await self._cache_set(tool, key, out.output)
        else:
            self.failed += 1
            err = result.get("error") or (
                f"http_{result['http_status']}" if result.get("http_status") else result.get("status")
            )
            self._error(f"{tool.tool_name}:{err}")
            out = _Outcome(ok=False, error=str(err))
        self.memo[key] = out
        return out

    @staticmethod
    def _retryable(result: dict) -> bool:
        if result.get("error") == "timeout":
            return True
        http = result.get("http_status")
        return isinstance(http, int) and http >= 500

    async def _invoke(self, tool: ExtractTool, args: dict, entity: dict, etype: str) -> dict:
        remaining = max(1.0, self.deadline - time.monotonic())
        cap = min(self.s.timeout, remaining)
        request_id = self.request_id or f"extract-{uuid.uuid4().hex[:16]}"
        context = {
            "tenant_id": self.tenant_id, "knowledge_base_id": self.kb_id,
            "tool_name": tool.tool_name, "tool_source": tool.source,
            "action_name": entity.get("canonical_name"), "request_id": request_id,
        }
        try:
            result = await asyncio.wait_for(
                invoke_tool(
                    tool.runtime, tool.secret_encrypted, tool.input_schema, args, context,
                    timeout_cap=cap, runner=self.svc._runner, max_output_chars=MAX_OUTPUT_CHARS,
                ),
                timeout=cap + 1.0,
            )
        except asyncio.TimeoutError:
            result = {"status": STATUS_FAILED, "ok": False, "error": "timeout",
                      "duration_ms": int(cap * 1000)}
        except Exception as exc:  # noqa: BLE001 — invoke_tool 本身不抛，这里兜注入的 runner
            result = {"status": STATUS_FAILED, "ok": False, "error": type(exc).__name__, "duration_ms": 0}
        self._audit(tool, args, entity, etype, result, request_id)
        return result

    def _audit(self, tool: ExtractTool, args: dict, entity: dict, etype: str,
               result: dict, request_id: str) -> None:
        if not self.svc._emit_audit_on:
            return
        try:
            schedule_emit(tool_audit_entry(
                tenant_id=self.tenant_id, action=self.audit_action, service_name="knowledge_base",
                status=result.get("status") or STATUS_FAILED, tool_name=tool.tool_name,
                tool_source=tool.source, request_id=request_id,
                user_id=self.user_id, username=self.username,
                duration_ms=result.get("duration_ms"), resource="kb_tool",
                request_params={
                    "knowledge_base_id": self.kb_id, "document_id": self.doc_id,
                    "entity_type": etype, "entity_name": entity.get("canonical_name"),
                    "input_keys": sorted(str(k) for k in args.keys()),
                },
                response_body={"http_status": result.get("http_status"), "error": result.get("error")},
                success_statuses=(STATUS_EXECUTED,),
            ))
        except Exception as exc:  # noqa: BLE001 — 审计失败不影响抽取
            logger.warning("[extract-tool] 审计写入失败: %s", exc)

    # ── 结果落到实体 ─────────────────────────────────────────────

    def _apply_outcome(self, entity: dict, call: CatalogSkillCall, outcome: _Outcome) -> None:
        # 没调用（预算用完 / 限次）不是调用失败：不套用 on_error，实体原样保留，
        # 否则预算一紧就会成批清属性或丢实体
        if outcome.skipped:
            return
        if call.group == "refine":
            self._apply_refine(entity, call, outcome)
        else:
            self._apply_constrain(entity, call, outcome)

    def _apply_refine(self, entity: dict, call: CatalogSkillCall, outcome: _Outcome) -> None:
        attrs = entity.get("attributes")
        if not isinstance(attrs, dict):
            attrs = {}
            entity["attributes"] = attrs
        sources = attrs.get(TOOL_SOURCES_ATTR)
        sources = dict(sources) if isinstance(sources, dict) else {}
        if outcome.ok:
            for attr, path in call.writes.items():
                value = extract_path(outcome.output, path)
                if value is None:
                    continue
                attrs[attr] = _fit_value(value)
                sources[attr] = f"tool:{call.tool_name}"
                self.attrs_written += 1
        elif call.on_error == "drop_attr":
            for attr in call.writes:
                attrs.pop(attr, None)
                sources.pop(attr, None)
        if sources:
            attrs[TOOL_SOURCES_ATTR] = sources
        else:
            attrs.pop(TOOL_SOURCES_ATTR, None)

    def _apply_constrain(self, entity: dict, call: CatalogSkillCall, outcome: _Outcome) -> None:
        if outcome.ok:
            rw = call.reject_when or {}
            reject = _matches(extract_path(outcome.output, rw.get("path", "")), rw.get("equals"))
        else:
            reject = call.on_error == "reject"
        if reject:
            self.rejected_keys.add((entity.get("entity_type"), entity.get("canonical_name")))

    def touches_rejected(self, rel: dict) -> bool:
        for name_key, type_key in (("source_name", "source_type"), ("target_name", "target_type")):
            name, rtype = rel.get(name_key), rel.get(type_key)
            for etype, ename in self.rejected_keys:
                # 关系端点类型未定（unknown / 空）时按名字匹配
                if name == ename and (rtype == etype or rtype in (None, "", "unknown")):
                    return True
        return False

    def summary(self) -> dict:
        # errors 只记「工具不可用 / 预算 / 限次 / 调用失败」；入参取不到值跳过是正常情况，不算降级
        if self.calls == 0 and self.cached == 0:
            status = SUMMARY_PARTIAL if self.errors else SUMMARY_SKIPPED
        elif self.errors:
            status = SUMMARY_PARTIAL
        else:
            status = SUMMARY_OK
        return {
            "status": status, "calls": self.calls, "cached": self.cached, "ok": self.ok,
            "failed": self.failed, "skipped": self.skipped, "rejected": len(self.rejected_keys),
            "attrs_written": self.attrs_written, "budget_exhausted": self.budget_exhausted,
            "errors": self.errors[:MAX_SUMMARY_ERRORS],
            "at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Redis：缓存与每日限次（都 fail-open）─────────────────────

    def _cache_key(self, tool: ExtractTool, key: str) -> str:
        return f"ontology_extract_tool:cache:{self.tenant_id}:{self.kb_id}:{tool.source}:{tool.tool_name}:{key}"

    async def _cache_get(self, tool: ExtractTool, key: str) -> Any:
        if not self.use_redis or self.s.cache_ttl <= 0:
            return None
        try:
            raw = await get_redis_client().get(self._cache_key(tool, key))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[extract-tool] 读缓存失败: %s", exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError):
            return None

    async def _cache_set(self, tool: ExtractTool, key: str, output: Any) -> None:
        if not self.use_redis or self.s.cache_ttl <= 0 or output is None:
            return
        try:
            await get_redis_client().set(
                self._cache_key(tool, key), json.dumps(output, ensure_ascii=False, default=str),
                ex=self.s.cache_ttl,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[extract-tool] 写缓存失败: %s", exc)

    async def _rate_allowed(self, tool: ExtractTool) -> bool:
        """每知识库每工具每天计数；Redis 不可用时放行（限次是保护对方接口，不是安全边界）。"""
        if not self.use_redis or self.s.rate_per_day <= 0:
            return True
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = f"ontology_extract_tool:rate:{self.tenant_id}:{self.kb_id}:{tool.source}:{tool.tool_name}:{day}"
        try:
            redis = get_redis_client()
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, 90000)
            return int(count) <= self.s.rate_per_day
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("[extract-tool] 限次计数失败，放行: %s", exc)
            return True


__all__ = [
    "AUDIT_ACTION",
    "PREVIEW_AUDIT_ACTION",
    "ExtractTool",
    "ExtractToolService",
    "ExtractToolSettings",
    "MASTER_SWITCH_ENV",
    "TOOL_SOURCES_ATTR",
    "extract_tool_master_switch_on",
]
