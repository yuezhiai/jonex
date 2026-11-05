#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""本体实例 → 平台可执行工具的绑定管理，以及执行前解析（`resolve_action_execution`）。

职责边界
    本体只定义边界、不执行动作（docs/ontology/action-execution-boundary-protocol.md §1）。
    本服务只做两件事：管理「哪个 Skill / Action 实例对应哪个工具」，以及把一个 Action
    的契约字段、绑定、参数映射组装成执行计划并给出判定。真正调用工具的是
    business_domain 的执行器，它带着调用人身份经 Sidecar 调 `prepare_action_execution`
    拿计划（与 `resolve_action_execution` 同一份解析；前者按知识库写口径判定调用人，
    后者按读口径，供页面预览）。

只读 / 问答调用
    工具的 `read_only` 由登记人显式声明（平台工具在 capability_json.read_only，本库工具在
    kb_tools.read_only）；绑定的 `qa_callable` 是绑定级开关。两者都只影响知识库问答能否
    自动调用（见 `qa_call`），不影响人工试执行。

工具来源与可用性（绑定上的 tool_source）
    platform  平台工具目录（business_domain.skill_catalog）+ 租户启用状态（tenant_skills），
              在 business_domain schema。knowledge-base 镜像里没有 business_domain 的代码包，
              沿用 parser_setting_service / template_schema_provider 的做法：同库直接用 SQL 读。
              「可用」= 目录里已上架、未删除，且本租户 tenant_skills.status = 'enabled'。
    kb        本库工具（knowledge_base.kb_tools）：「可用」= 同一知识库内未删除且已启用。

判定（D15，见 `decide_execution`）
    refuse  工具不可用 / 动作不存在 / 本体不可用；
    manual  无绑定、绑定已停用、authorization 缺失（缺失 = 未知，不是无约束）；
    confirm 写明了权限要求或前置条件 —— 这些是文字规则，执行器无法自动判断，
            必须由调用方逐项确认后才执行；
    allow   authorization 显式写明无需授权，且没有前置条件。
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jonex_core.common import get_db_session
from jonex_core.common.audit import schedule_emit
from jonex_core.common.cache import get_redis_client
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.neo4j_client import get_neo4j_driver
from jonex_core.common.tenant import require_tenant
from jonex_core.common.tool_invocation import (
    STATUS_FAILED,
    STATUS_INVALID_INPUT,
    STATUS_TOOL_UNAVAILABLE,
    invoke_tool,
    tool_audit_entry,
)

from ..models.ontology_tool_binding import (
    BINDABLE_ENTITY_TYPES,
    EXECUTABLE_ENTITY_TYPES,
    TOOL_SOURCE_KB,
    TOOL_SOURCE_PLATFORM,
    TOOL_SOURCES,
)
from ..repository.kb_tool_repository import KbToolRepository
from ..repository.knowledge_info_repository import KnowledgeInfoRepository
from ..repository.ontology_graph_repository import OntologyGraphRepository
from ..repository.ontology_tool_binding_repository import OntologyToolBindingRepository
from .ontology_tools import CONTRACT_FIELDS, contract_status

logger = logging.getLogger(__name__)

MAX_MAPPING_ENTRIES = 50
MAX_NAME_LENGTH = 512
_PARAM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$")
_INPUT_REF_RE = re.compile(r"^\$input\.([A-Za-z_][A-Za-z0-9_.\-]{0,127})$")
_ATTR_REF_RE = re.compile(r"^\$attr\.([A-Za-z_][A-Za-z0-9_]{0,127})$")
_ENTITY_NAME_REF = "$entity.name"

# 判定严格度：数字越大越严格，多个原因取最严格的
_VERDICT_RANK = {"allow": 0, "confirm": 1, "manual": 2, "refuse": 3}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


# ── 知识库问答自动调用工具（ontology_call_tool）的开关与上限 ──
# 总闸（默认关）：关闭时问答不注册 ontology_call_tool，也不调用任何工具。
# 另两层开关：绑定级 qa_callable、工具级 read_only（见 qa_call）。
ONTOLOGY_TOOL_EXECUTION_ENABLED = os.getenv(
    "ONTOLOGY_TOOL_EXECUTION_ENABLED", "false",
).lower() in ("1", "true", "yes", "on")
# 一次问答最多调用几次工具
ONTOLOGY_TOOL_EXEC_MAX_CALLS = _env_int("ONTOLOGY_TOOL_EXEC_MAX_CALLS", 2, 1, 5)
# 单次调用的超时上限（秒）：取它与工具自身 runtime.timeout 的较小值
ONTOLOGY_TOOL_EXEC_TIMEOUT = float(_env_int("ONTOLOGY_TOOL_EXEC_TIMEOUT", 5, 1, 30))
# 每租户每工具每分钟最多调用次数；0 = 不限
ONTOLOGY_TOOL_EXEC_RATE_PER_MIN = _env_int("ONTOLOGY_TOOL_EXEC_RATE_PER_MIN", 30, 0, 10000)
# 回给模型的工具输出上限（字符）：问答上下文比人工试执行紧
QA_MAX_OUTPUT_CHARS = 4000

# 问答调用的不调用状态（调用了的沿用 tool_invocation 的 executed / failed / no_runtime）
QA_NOT_CALLABLE = "not_callable"            # 总闸关 / 未绑定 / 绑定未开问答调用 / 工具未声明只读
QA_NEEDS_CONFIRMATION = "needs_confirmation"  # 判定为 confirm：文字规则需人确认
QA_NOT_ALLOWED = "not_allowed"              # 判定为 manual / refuse
QA_RATE_LIMITED = "rate_limited"


# ── 纯函数 ───────────────────────────────────────────────────────────


def validate_param_mapping(mapping: Any) -> dict:
    """校验参数映射：键是工具入参名；值是 `$input.<key>` / `$attr.<属性>` / `$entity.name` / 字面量。"""
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise InvalidParameterError(message=translate(
            "err.tool_binding.invalid_mapping", params={"reason": "param_mapping 必须是对象"},
            fallback="参数映射不合法：param_mapping 必须是对象",
        ))
    if len(mapping) > MAX_MAPPING_ENTRIES:
        raise InvalidParameterError(message=translate(
            "err.tool_binding.invalid_mapping", params={"reason": f"最多 {MAX_MAPPING_ENTRIES} 项"},
            fallback=f"参数映射不合法：最多 {MAX_MAPPING_ENTRIES} 项",
        ))
    out: dict = {}
    for key, value in mapping.items():
        key = str(key).strip()
        if not _PARAM_KEY_RE.match(key):
            raise InvalidParameterError(message=translate(
                "err.tool_binding.invalid_mapping", params={"reason": f"入参名不合法：{key[:40]}"},
                fallback=f"参数映射不合法：入参名不合法：{key[:40]}",
            ))
        if isinstance(value, str) and value.startswith("$"):
            if not (_INPUT_REF_RE.match(value) or _ATTR_REF_RE.match(value) or value == _ENTITY_NAME_REF):
                raise InvalidParameterError(message=translate(
                    "err.tool_binding.invalid_mapping", params={"reason": f"取值来源不合法：{value[:60]}"},
                    fallback=f"参数映射不合法：取值来源不合法：{value[:60]}",
                ))
        elif not isinstance(value, (str, int, float, bool)) and value is not None:
            raise InvalidParameterError(message=translate(
                "err.tool_binding.invalid_mapping", params={"reason": f"{key} 的字面量只能是字符串、数字或布尔"},
                fallback=f"参数映射不合法：{key} 的字面量只能是字符串、数字或布尔",
            ))
        out[key] = value
    return out


def plan_parameters(mapping: dict, entity: dict) -> dict:
    """把参数映射拆成「已解析的值」和「需要调用方提供的输入」。

    返回 `{"resolved": {入参: 值}, "input_refs": {入参: 输入键}, "unresolved_attrs": [入参]}`。
    `$attr.*` 指向的属性在本体里为空时记入 `unresolved_attrs`（不臆造值）。
    """
    attrs = (entity or {}).get("attributes") or {}
    resolved: dict = {}
    input_refs: dict = {}
    unresolved: list[str] = []
    for param, source in (mapping or {}).items():
        if isinstance(source, str):
            m_in = _INPUT_REF_RE.match(source)
            if m_in:
                input_refs[param] = m_in.group(1)
                continue
            m_attr = _ATTR_REF_RE.match(source)
            if m_attr:
                value = attrs.get(m_attr.group(1))
                if value in (None, "", [], {}):
                    unresolved.append(param)
                else:
                    resolved[param] = value
                continue
            if source == _ENTITY_NAME_REF:
                resolved[param] = (entity or {}).get("name")
                continue
        resolved[param] = source
    return {"resolved": resolved, "input_refs": input_refs, "unresolved_attrs": unresolved}


def decide_execution(
    contract: dict,
    binding: Optional[dict],
    *,
    action_found: bool = True,
    ontology_available: bool = True,
) -> dict:
    """D15 判定：返回 `{"verdict", "executable", "reasons", "checklist"}`。

    `reasons`：`[{"code", "message"}]`，按发现顺序；`checklist`：需要调用方逐项确认的
    文字规则 `[{"kind": "authorization"|"precondition", "items": [...]}]`。
    """
    reasons: list[dict] = []
    checklist: list[dict] = []
    verdict = "allow"

    def _add(level: str, code: str, message: str) -> None:
        nonlocal verdict
        reasons.append({"code": code, "message": message})
        if _VERDICT_RANK[level] > _VERDICT_RANK[verdict]:
            verdict = level

    if not ontology_available:
        _add("refuse", "ontology_unavailable", "本体图不可用，无法读取执行契约")
    elif not action_found:
        _add("refuse", "action_not_found", "知识库本体中没有这个动作")
    else:
        if binding is None:
            _add("manual", "no_binding", "该动作未绑定可执行工具，需人工办理")
        elif not binding.get("binding_enabled"):
            _add("manual", "binding_disabled", "该动作的工具绑定已停用，需人工办理")
        elif not binding.get("tool_available"):
            _add("refuse", "tool_unavailable", "绑定的工具不可用（未上架、本租户未启用或本库工具已停用）")

        auth = (contract or {}).get("authorization") or {"status": "missing"}
        if auth.get("status") == "missing":
            _add("manual", "authorization_missing", "文档未规定权限要求，按规定不自动执行，需人工确认")
        elif auth.get("status") == "present":
            items = auth.get("value")
            items = items if isinstance(items, list) else [items]
            checklist.append({"kind": "authorization", "items": [str(i) for i in items if str(i).strip()]})

        pre = (contract or {}).get("preconditions") or {"status": "missing"}
        if pre.get("status") == "present":
            items = pre.get("value")
            items = items if isinstance(items, list) else [items]
            checklist.append({"kind": "precondition", "items": [str(i) for i in items if str(i).strip()]})

        if checklist:
            _add("confirm", "confirmation_required", "存在权限要求或前置条件，需调用方逐项确认后执行")

    return {
        "verdict": verdict,
        "executable": verdict in ("allow", "confirm"),
        "reasons": reasons,
        "checklist": checklist,
    }


# ── 工具目录（business_domain，同库 SQL 读）────────────────────────────


async def _available_tools(session, tenant_id: str, tool_names: Optional[list[str]] = None) -> dict:
    """本租户可用的工具：`{tool_name: {name, description, input_schema}}`。"""
    sql = (
        "SELECT sc.tool_name, sc.name, sc.description, sc.input_schema_json, "
        # 只读声明存在 capability_json.read_only；只认字面 true，其它一律按写操作
        "       (sc.capability_json->>'read_only') = 'true' AS read_only "
        "FROM business_domain.skill_catalog sc "
        "JOIN business_domain.tenant_skills ts "
        "  ON ts.skill_id = sc.id AND ts.tenant_id = :t "
        " AND ts.status = 'enabled' AND ts.is_deleted = 0 "
        "WHERE sc.is_deleted = 0 AND sc.status = 'published'"
    )
    params: dict = {"t": tenant_id}
    if tool_names is not None:
        if not tool_names:
            return {}
        sql += " AND sc.tool_name = ANY(:names)"
        params["names"] = list(tool_names)
    sql += " ORDER BY sc.name"
    rows = (await session.execute(text(sql), params)).all()
    return {
        r[0]: {"tool_name": r[0], "name": r[1], "description": r[2] or "", "input_schema": r[3] or {},
               "read_only": r[4] is True, "source": TOOL_SOURCE_PLATFORM}
        for r in rows
    }


async def _available_kb_tools(
    session, tenant_id: str, kb_id: str, tool_names: Optional[list[str]] = None,
) -> dict:
    """本库已启用的工具：`{tool_name: {...}}`。"""
    repo = KbToolRepository(session)
    if tool_names is None:
        items, _ = await repo.list_by_kb(tenant_id, kb_id, limit=500, enabled_only=True)
    else:
        items = await repo.list_by_names(tenant_id, kb_id, tool_names, enabled_only=True)
    return {
        t.tool_name: {"tool_name": t.tool_name, "name": t.name, "description": t.description or "",
                      "input_schema": t.input_schema or {}, "read_only": t.read_only is True,
                      "source": TOOL_SOURCE_KB}
        for t in items
    }


async def _tools_for(session, tenant_id: str, kb_id: str, refs: list[tuple[str, str]]) -> dict:
    """按 (source, tool_name) 批量查可用工具：`{(source, tool_name): info}`。"""
    by_source: dict[str, set] = {}
    for source, name in refs:
        if name:
            by_source.setdefault(source or TOOL_SOURCE_PLATFORM, set()).add(name)
    out: dict = {}
    if by_source.get(TOOL_SOURCE_PLATFORM):
        for name, info in (await _available_tools(
            session, tenant_id, sorted(by_source[TOOL_SOURCE_PLATFORM]),
        )).items():
            out[(TOOL_SOURCE_PLATFORM, name)] = info
    if by_source.get(TOOL_SOURCE_KB):
        for name, info in (await _available_kb_tools(
            session, tenant_id, kb_id, sorted(by_source[TOOL_SOURCE_KB]),
        )).items():
            out[(TOOL_SOURCE_KB, name)] = info
    return out


def _clean_source(value: Any) -> str:
    source = str(value or TOOL_SOURCE_PLATFORM).strip()
    if source not in TOOL_SOURCES:
        raise InvalidParameterError(message=translate(
            "err.tool_binding.invalid_source", params={"source": source[:16]},
            fallback=f"工具来源只能是 platform（平台工具）或 kb（本库工具）：{source[:16]}",
        ))
    return source


def _clean_flag(value: Any, field: str) -> bool:
    """开关类字段必须是显式布尔值（不把 "false" / 0 这类值猜成布尔）。"""
    if not isinstance(value, bool):
        raise InvalidParameterError(message=translate(
            "err.tool_binding.invalid_flag", params={"field": field},
            fallback=f"{field} 必须是布尔值",
        ))
    return value


def _tool_unavailable(tool_name: str) -> InvalidParameterError:
    return InvalidParameterError(message=translate(
        "err.tool_binding.tool_unavailable", params={"tool_name": tool_name[:64]},
        fallback=f"工具不可用（未上架、本租户未启用或本库工具已停用）：{tool_name[:64]}",
    ))


# ── 服务 ─────────────────────────────────────────────────────────────


class OntologyToolBindingService:
    """KB 级工具绑定 CRUD + 执行前解析。"""

    def __init__(self, graph_factory: Optional[Callable[[], Any]] = None):
        # 测试可注入假图仓储；默认按需连 Neo4j
        self._graph_factory = graph_factory or (lambda: OntologyGraphRepository(get_neo4j_driver()))

    async def _ensure_kb(self, session, kb_id: str, tenant_id: str) -> None:
        await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)

    async def _existing(self, tenant_id: str, kb_id: str, pairs: list[tuple[str, str]]) -> Optional[set]:
        """图里存在的实例；Neo4j 不可用时返回 None（调用方按「未知」展示，不报错）。"""
        if not pairs:
            return set()
        try:
            return await self._graph_factory().existing_entities(tenant_id, kb_id, pairs)
        except Exception as exc:  # noqa: BLE001 — 本体不可用时降级为「未知」
            logger.warning("工具绑定：判断目标实例是否存在失败，按未知处理: kb=%s err=%s", kb_id, exc)
            return None

    # ── 管理 ──

    async def list(
        self, tenant_id: str, kb_id: str, page: int = 1, page_size: int = 20,
        entity_type: Optional[str] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        page = max(1, int(page or 1))
        page_size = max(1, min(200, int(page_size or 20)))
        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            items, total = await OntologyToolBindingRepository(session).list_by_kb(
                tenant_id, kb_id, offset=(page - 1) * page_size, limit=page_size,
                entity_type=entity_type,
            )
            tools = await _tools_for(
                session, tenant_id, kb_id,
                [(b.tool_source or TOOL_SOURCE_PLATFORM, b.tool_name) for b in items],
            )
        existing = await self._existing(
            tenant_id, kb_id, [(b.entity_type, b.canonical_name) for b in items],
        )
        out = []
        for b in items:
            d = b.to_dict()
            tool = tools.get((b.tool_source or TOOL_SOURCE_PLATFORM, b.tool_name))
            d["tool_label"] = tool["name"] if tool else None
            d["tool_available"] = tool is not None
            d["tool_read_only"] = bool(tool and tool.get("read_only"))
            d["target_exists"] = None if existing is None else (b.entity_type, b.canonical_name) in existing
            out.append(d)
        return {"items": out, "total": total, "page": page, "page_size": page_size}

    async def list_bindable_tools(self, tenant_id: str, kb_id: str) -> dict:
        """可绑定的工具：本库已启用的工具在前，本租户已启用的平台工具在后；每项带 `source`。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            kb_tools = await _available_kb_tools(session, tenant_id, kb_id)
            platform_tools = await _available_tools(session, tenant_id)
        items = list(kb_tools.values()) + list(platform_tools.values())
        return {"items": items, "total": len(items)}

    async def create(self, tenant_id: str, data: dict, user_id: Optional[str] = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id = data["knowledge_base_id"]
        entity_type = str(data.get("entity_type") or "").strip()
        name = str(data.get("canonical_name") or "").strip()
        tool_name = str(data.get("tool_name") or "").strip()
        tool_source = _clean_source(data.get("tool_source"))
        if entity_type not in BINDABLE_ENTITY_TYPES:
            raise InvalidParameterError(message=translate(
                "err.tool_binding.invalid_entity_type", params={"entity_type": entity_type[:32]},
                fallback=f"绑定类型只能是 Skill 或 Action：{entity_type[:32]}",
            ))
        if entity_type not in EXECUTABLE_ENTITY_TYPES:
            # Skill 是多步规程，执行器只执行单个动作：新绑定一律拒绝（存量 Skill 绑定行保留，
            # 可停用 / 改映射 / 删除，列表标为历史绑定）
            raise InvalidParameterError(message=translate(
                "err.tool_binding.skill_not_executable",
                fallback="只能给 Action 绑定工具：Skill 是多步规程，当前执行器只执行单个动作",
            ))
        qa_callable = _clean_flag(data.get("qa_callable", False), "qa_callable")
        if not name or len(name) > MAX_NAME_LENGTH:
            raise InvalidParameterError(message=translate(
                "err.tool_binding.name_required", fallback="绑定的实例名不能为空，且不超过 512 字",
            ))
        mapping = validate_param_mapping(data.get("param_mapping"))
        enabled = bool(data.get("enabled", True))

        existing = await self._existing(tenant_id, kb_id, [(entity_type, name)])
        if existing is not None and (entity_type, name) not in existing:
            # 图可用且确实没有这个实例：拒绝（图不可用时放行，列表会标「未知」）
            raise InvalidParameterError(message=translate(
                "err.tool_binding.target_not_found", params={"entity_type": entity_type, "name": name[:60]},
                fallback=f"本体中不存在该实例：{entity_type} {name[:60]}",
            ))

        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            tools = await _tools_for(session, tenant_id, kb_id, [(tool_source, tool_name)])
            tool = tools.get((tool_source, tool_name))
            if tool is None:
                raise _tool_unavailable(tool_name)
            repo = OntologyToolBindingRepository(session)
            if await repo.get_active_for_entity(tenant_id, kb_id, entity_type, name):
                raise ResourceConflictError(message=translate(
                    "err.tool_binding.duplicate", params={"entity_type": entity_type, "name": name[:60]},
                    fallback=f"该实例已有绑定：{entity_type} {name[:60]}",
                ))
            try:
                obj = await repo.create(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    knowledge_base_id=kb_id,
                    entity_type=entity_type,
                    canonical_name=name,
                    tool_source=tool_source,
                    tool_name=tool_name,
                    param_mapping=mapping,
                    enabled=enabled,
                    qa_callable=qa_callable,
                    created_by=user_id,
                    updated_by=user_id,
                )
                await session.commit()
            except IntegrityError:
                # 并发下两条请求都过了上面的查重，由部分唯一索引兜底
                await session.rollback()
                raise ResourceConflictError(message=translate(
                    "err.tool_binding.duplicate", params={"entity_type": entity_type, "name": name[:60]},
                    fallback=f"该实例已有绑定：{entity_type} {name[:60]}",
                ))
            d = obj.to_dict()
        d["tool_label"] = tool["name"]
        d["tool_available"] = True
        d["tool_read_only"] = bool(tool.get("read_only"))
        d["target_exists"] = None if existing is None else True
        return d

    async def update(
        self, tenant_id: str, binding_id: str, data: dict, user_id: Optional[str] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = OntologyToolBindingRepository(session)
            obj = await repo.get_required(binding_id, tenant_id)
            if "tool_name" in data or "tool_source" in data:
                tool_name = str(data.get("tool_name", obj.tool_name) or "").strip()
                tool_source = _clean_source(data.get("tool_source", obj.tool_source))
                tools = await _tools_for(
                    session, tenant_id, obj.knowledge_base_id, [(tool_source, tool_name)],
                )
                if (tool_source, tool_name) not in tools:
                    raise _tool_unavailable(tool_name)
                obj.tool_name = tool_name
                obj.tool_source = tool_source
            if "param_mapping" in data:
                obj.param_mapping = validate_param_mapping(data.get("param_mapping"))
            if "enabled" in data:
                obj.enabled = bool(data.get("enabled"))
            if "qa_callable" in data:
                obj.qa_callable = _clean_flag(data.get("qa_callable"), "qa_callable")
            obj.updated_by = user_id
            await session.commit()
            return obj.to_dict()

    async def delete(self, tenant_id: str, binding_id: str) -> bool:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = OntologyToolBindingRepository(session)
            obj = await repo.get_required(binding_id, tenant_id)
            await repo.delete_soft(obj)
            await session.commit()
            return True

    # ── 读取（查询工具 / 执行解析共用）──

    async def get_binding_info(
        self, tenant_id: str, kb_id: str, entity_type: str, canonical_name: str,
    ) -> Optional[dict]:
        """实例的有效绑定 + 工具可用性；没有绑定返回 None。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            b = await OntologyToolBindingRepository(session).get_active_for_entity(
                tenant_id, kb_id, entity_type, canonical_name,
            )
            if b is None:
                return None
            source = b.tool_source or TOOL_SOURCE_PLATFORM
            tools = await _tools_for(session, tenant_id, kb_id, [(source, b.tool_name)])
        tool = tools.get((source, b.tool_name))
        return {
            "binding_id": b.id,
            "tool_source": source,
            "tool_name": b.tool_name,
            "tool_label": tool["name"] if tool else None,
            "binding_enabled": bool(b.enabled),
            "qa_callable": bool(b.qa_callable),
            "tool_available": tool is not None,
            "param_mapping": b.param_mapping or {},
            "input_schema": tool["input_schema"] if tool else {},
            "read_only": bool(tool and tool.get("read_only")),
        }

    async def qa_callable_targets(
        self, tenant_id: str, pairs: list[tuple[str, str]],
    ) -> set[tuple[str, str]]:
        """给定 `(kb_id, Action 名)`，返回其中「绑定启用且允许问答调用」的那些。

        问答据此决定要不要进入工具调用：只是粗筛（一次 SQL，不查工具可用性与判定），
        真正能不能调仍由 `qa_call` 的四道闸决定。
        """
        tenant_id = require_tenant(tenant_id)
        pairs = [(str(k), str(n)) for k, n in pairs if k and n]
        if not pairs:
            return set()
        async with get_db_session() as session:
            rows = (await session.execute(text(
                "SELECT knowledge_base_id, canonical_name "
                "FROM knowledge_base.ontology_tool_bindings "
                "WHERE tenant_id = :t AND is_deleted = 0 AND enabled AND qa_callable "
                "  AND entity_type = 'Action' "
                "  AND knowledge_base_id = ANY(:kbs) AND canonical_name = ANY(:names)"
            ), {"t": tenant_id, "kbs": sorted({k for k, _ in pairs}),
                "names": sorted({n for _, n in pairs})})).all()
        wanted = set(pairs)
        return {(r[0], r[1]) for r in rows if (r[0], r[1]) in wanted}

    def binding_lookup(self, tenant_id: str) -> Callable[[str, str, str], Awaitable[Optional[dict]]]:
        """给查询工具用的闭包：`(kb_id, entity_type, name) -> bound_tool 摘要`（失败返回 None）。"""

        async def _lookup(kb_id: str, entity_type: str, name: str) -> Optional[dict]:
            info = await self.get_binding_info(tenant_id, kb_id, entity_type, name)
            if info is None:
                return None
            # 存量 Skill 绑定是历史数据，不参与执行：对模型一律报「不可用」
            executable = entity_type in EXECUTABLE_ENTITY_TYPES
            return {
                "tool_source": info["tool_source"],
                "tool_name": info["tool_name"],
                "tool_label": info["tool_label"],
                "enabled": executable and info["binding_enabled"] and info["tool_available"],
                "read_only": info["read_only"],
            }

        return _lookup

    async def resolve_action_execution(self, tenant_id: str, data: dict) -> dict:
        """执行器的唯一入口：返回动作的契约状态、D15 判定、绑定工具与参数计划。不执行任何操作。"""
        tenant_id = require_tenant(tenant_id)
        kb_id = data["knowledge_base_id"]
        action_name = str(data.get("action_name") or "").strip()
        if not action_name:
            raise InvalidParameterError(message=translate(
                "err.tool_binding.name_required", fallback="绑定的实例名不能为空，且不超过 512 字",
            ))
        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)

        entity: Optional[dict] = None
        ontology_available = True
        try:
            entity = await self._graph_factory().get_entity_with_sources(
                tenant_id, kb_id, "Action", action_name,
            )
        except Exception as exc:  # noqa: BLE001 — 本体不可用时拒绝执行（fail closed）
            logger.warning("执行解析：读取动作失败 kb=%s action=%s err=%s", kb_id, action_name, exc)
            ontology_available = False

        attrs = (entity or {}).get("attributes") or {}
        contract = {f: contract_status(f, attrs.get(f)) for f in CONTRACT_FIELDS} if entity else {}
        binding = (
            await self.get_binding_info(tenant_id, kb_id, "Action", entity["name"])
            if entity else None
        )
        decision = decide_execution(
            contract, binding,
            action_found=entity is not None, ontology_available=ontology_available,
        )
        parameters = (
            plan_parameters(binding.get("param_mapping") or {}, entity)
            if binding and entity else {"resolved": {}, "input_refs": {}, "unresolved_attrs": []}
        )
        return {
            "knowledge_base_id": kb_id,
            "action": None if entity is None else {
                "name": entity.get("name"),
                "action_type": attrs.get("action_type"),
                "step_no": attrs.get("step_no"),
                "definition": attrs.get("definition") or entity.get("description") or "",
                "doc_ids": entity.get("doc_ids") or [],
            },
            "contract": contract,
            "decision": decision,
            # input_schema 随计划下发：本库工具的定义不在 business_domain，执行器靠它组装入参
            "binding": None if binding is None else {
                k: binding[k] for k in (
                    "binding_id", "tool_source", "tool_name", "tool_label", "binding_enabled",
                    "tool_available", "param_mapping", "input_schema", "read_only", "qa_callable",
                )
            },
            "parameters": parameters,
        }

    # ── 知识库问答自动调用（ontology_call_tool）──

    async def qa_call(
        self,
        tenant_id: str,
        kb_id: str,
        action_name: str,
        inputs: Any,
        *,
        user_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """问答模型要调一个动作绑定的工具：四道闸都过才真的调用，否则回一个不调用的状态。

        闸门（任一不满足都不调用）：
          1. 总闸 `ONTOLOGY_TOOL_EXECUTION_ENABLED` 打开，且该绑定 `qa_callable=true`；
          2. 绑定启用、工具可用；
          3. 执行判定为 allow（confirm 也不行 —— 问答里没有人逐项确认文字规则）；
          4. 工具声明了只读（默认按写操作处理）。
        入参只收工具 schema 声明的键与映射里 `$input.*` 的键；本体侧已解析的值（`$attr` /
        `$entity.name` / 字面量）优先于模型给的同名值。每次尝试都落审计（入参只记键名）。
        不抛异常：失败一律回 `status` 让模型据此作答。
        """
        tenant_id = require_tenant(tenant_id)
        t0 = time.perf_counter()
        action_name = str(action_name or "").strip()
        inputs = inputs if isinstance(inputs, dict) else {}
        result: dict = {"status": QA_NOT_CALLABLE, "action": action_name}
        source: Optional[str] = None
        tool_name: Optional[str] = None

        def _done(status: str, **extra) -> dict:
            result["status"] = status
            result.update(extra)
            self._emit_qa_audit(
                tenant_id, kb_id, action_name, source, tool_name, status, inputs,
                user_id=user_id, request_id=request_id,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                http_status=result.get("http_status"), error=result.get("error"),
            )
            return result

        try:
            if not ONTOLOGY_TOOL_EXECUTION_ENABLED:
                return _done(QA_NOT_CALLABLE, reason="execution_disabled")
            plan = await self.resolve_action_execution(
                tenant_id, {"knowledge_base_id": kb_id, "action_name": action_name},
            )
            binding = plan.get("binding")
            if binding is None:
                return _done(QA_NOT_CALLABLE, reason="no_binding")
            source = binding.get("tool_source") or TOOL_SOURCE_PLATFORM
            tool_name = binding.get("tool_name")
            result.update(tool_name=tool_name, tool_label=binding.get("tool_label"), tool_source=source)
            if not binding.get("qa_callable"):
                return _done(QA_NOT_CALLABLE, reason="qa_callable_off")
            if not binding.get("binding_enabled") or not binding.get("tool_available"):
                return _done(STATUS_TOOL_UNAVAILABLE)
            verdict = (plan.get("decision") or {}).get("verdict")
            if verdict != "allow":
                # confirm：文字规则要人确认；manual / refuse：本就不自动执行
                return _done(
                    QA_NEEDS_CONFIRMATION if verdict == "confirm" else QA_NOT_ALLOWED,
                    verdict=verdict,
                    reasons=[r.get("message") for r in (plan.get("decision") or {}).get("reasons") or []],
                )
            if not binding.get("read_only"):
                return _done(QA_NOT_CALLABLE, reason="tool_not_read_only")

            schema = binding.get("input_schema") or {}
            arguments, missing = qa_arguments(plan.get("parameters") or {}, inputs, schema)
            if missing:
                return _done(STATUS_INVALID_INPUT, missing_inputs=missing)
            if not await _qa_rate_allowed(tenant_id, source, tool_name or ""):
                return _done(QA_RATE_LIMITED)

            runtime, secret_enc = await self._tool_runtime(tenant_id, kb_id, source, tool_name or "")
            outcome = await invoke_tool(
                runtime, secret_enc, None, arguments,
                {"tenant_id": tenant_id, "knowledge_base_id": kb_id, "tool_name": tool_name,
                 "tool_source": source, "action_name": action_name, "request_id": request_id},
                timeout_cap=ONTOLOGY_TOOL_EXEC_TIMEOUT,
                max_output_chars=QA_MAX_OUTPUT_CHARS,
            )
            extra = {k: outcome[k] for k in ("output", "output_truncated", "http_status", "error",
                                             "missing_inputs") if outcome.get(k) is not None}
            return _done(outcome["status"], **extra)
        except Exception as exc:  # noqa: BLE001 — 问答侧工具调用失败只影响这一条事实
            logger.warning("问答调用工具失败 kb=%s action=%s err=%s", kb_id, action_name, exc)
            return _done(STATUS_FAILED, error=type(exc).__name__)

    async def _tool_runtime(
        self, tenant_id: str, kb_id: str, source: str, tool_name: str,
    ) -> tuple[Optional[dict], Optional[str]]:
        """取工具的运行时与凭据密文（只在服务内用，不出这个进程）。"""
        async with get_db_session() as session:
            if source == TOOL_SOURCE_KB:
                obj = await KbToolRepository(session).get_by_name(tenant_id, kb_id, tool_name)
                if obj is None or not obj.enabled:
                    return None, None
                return obj.runtime, obj.secret_encrypted
            row = (await session.execute(text(
                "SELECT sc.capability_json FROM business_domain.skill_catalog sc "
                "JOIN business_domain.tenant_skills ts "
                "  ON ts.skill_id = sc.id AND ts.tenant_id = :t "
                " AND ts.status = 'enabled' AND ts.is_deleted = 0 "
                "WHERE sc.is_deleted = 0 AND sc.status = 'published' AND sc.tool_name = :n"
            ), {"t": tenant_id, "n": tool_name})).first()
        cap = (row[0] if row else None) or {}
        return cap.get("runtime"), cap.get("runtime_secret")

    def _emit_qa_audit(
        self, tenant_id: str, kb_id: str, action_name: str, source: Optional[str],
        tool_name: Optional[str], status: str, inputs: dict, *,
        user_id: Optional[str], request_id: Optional[str], duration_ms: int,
        http_status: Optional[int], error: Optional[str],
    ) -> None:
        try:
            schedule_emit(tool_audit_entry(
                tenant_id=tenant_id, action="ontology_call_tool", service_name="knowledge_base",
                status=status, tool_name=tool_name, tool_source=source,
                user_id=user_id, request_id=request_id, duration_ms=duration_ms,
                resource="ontology_tool_binding",
                request_params={"knowledge_base_id": kb_id, "action_name": action_name,
                                "input_keys": sorted(str(k) for k in inputs.keys())},
                response_body={"http_status": http_status, "error": error},
            ))
        except Exception as exc:  # noqa: BLE001 — 审计失败不影响问答
            logger.warning("问答调用工具审计写入失败: %s", exc)


def qa_arguments(parameters: dict, inputs: dict, input_schema: dict) -> tuple[dict, list[str]]:
    """问答调用的入参组装：只收 schema 声明的键与 `$input.*` 引用的键，本体侧已解析的值优先。

    与执行器的 `build_arguments` 取值顺序相同；差别是模型给的输入按白名单过滤，
    且不会覆盖 `resolved`（本体属性 / 实例名 / 字面量）—— 模型不能改写服务端定下的值。
    """
    resolved = dict((parameters or {}).get("resolved") or {})
    input_refs = dict((parameters or {}).get("input_refs") or {})
    props = set(((input_schema or {}).get("properties") or {}).keys())
    allowed = props | set(input_refs.values())
    clean = {k: v for k, v in (inputs or {}).items() if k in allowed}
    args = dict(resolved)
    for param, key in input_refs.items():
        if param not in args and key in clean:
            args[param] = clean[key]
    for name in props:
        if name not in args and name in clean:
            args[name] = clean[name]
    required = [str(r) for r in ((input_schema or {}).get("required") or [])]
    missing = [r for r in required if args.get(r) in (None, "")]
    return args, missing


async def _qa_rate_allowed(tenant_id: str, source: str, tool_name: str) -> bool:
    """按租户 + 工具每分钟计数；Redis 不可用时放行并告警（限流是保护对方接口，不是安全边界）。"""
    if ONTOLOGY_TOOL_EXEC_RATE_PER_MIN <= 0:
        return True
    key = f"ontology_tool_exec:{tenant_id}:{source}:{tool_name}:{int(time.time() // 60)}"
    try:
        redis = get_redis_client()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 70)
        return int(count) <= ONTOLOGY_TOOL_EXEC_RATE_PER_MIN
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("问答调用工具限流计数失败，放行: %s", exc)
        return True


__all__ = [
    "ONTOLOGY_TOOL_EXECUTION_ENABLED",
    "ONTOLOGY_TOOL_EXEC_MAX_CALLS",
    "ONTOLOGY_TOOL_EXEC_TIMEOUT",
    "OntologyToolBindingService",
    "decide_execution",
    "plan_parameters",
    "qa_arguments",
    "validate_param_mapping",
]
