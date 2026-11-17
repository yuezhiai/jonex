#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""AI 辅助配置：工具绑定参数映射的规则候选 + LLM 消歧。

边界
    只产出建议，不写绑定、不调用工具。最终保存仍走 create/update_tool_binding，
    权限、唯一索引和 validate_param_mapping 仍由原链路负责。

为什么规则优先
    同名 / 归一化同名 / 类型兼容是最可靠的候选；只有没有高置信候选的参数才交给
    LLM。LLM 失败时仍可返回规则候选，不把整个功能绑死在模型上。

无状态
    propose/refine 都从数据库重新读上下文；前端带回 current_mapping、answers、
    context_hash 和 round。服务端不存提案、不设 TTL。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from typing import Any, Optional

from sqlalchemy import text

from jonex_core.common import get_db_session
from jonex_core.common.catalog_skill_spec import CONTRACT_FIELDS, parse_catalog_item
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.ontology_llm import (
    suggest_extract_tool_declaration,
    suggest_kb_tool_draft,
    suggest_tool_binding_mapping,
)
from jonex_core.common.tool_runtime import validate_runtime, validate_tool_schema

from .ontology_tool_binding_service import (
    OntologyToolBindingService,
    validate_param_mapping,
)
from .tool_doc_parser import (
    _is_sensitive_header,
    _strip_secret_object,
    parse_tool_input,
    redact_text,
)

logger = logging.getLogger(__name__)

ENV_ASSIST_ENABLED = "ONTOLOGY_CONFIG_ASSIST_ENABLED"
MAX_ATTRIBUTES = 80
MAX_QUESTIONS = 3
MAX_ROUNDS = 3
RULE_HIGH_CONFIDENCE = 0.9


def config_assist_enabled() -> bool:
    """AI 辅助配置总闸（默认关）；关闭时 propose/refine 返回 disabled。"""
    return os.getenv(ENV_ASSIST_ENABLED, "false").lower() in ("1", "true", "yes", "on")


def _normalize(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").casefold())


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _schema_type(prop: Any) -> str:
    if not isinstance(prop, dict):
        return ""
    value = prop.get("type")
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _types_compatible(schema_type: str, value_type: str) -> bool:
    if not schema_type or value_type in ("", "null"):
        return True
    if schema_type in ("integer", "number"):
        return value_type in ("integer", "number")
    return schema_type == value_type


def build_rule_candidates(
    input_schema: Optional[dict], attributes: Optional[dict],
) -> dict[str, list[dict]]:
    """按参数名给规则候选；没有同名候选时回落到调用方同名输入（低置信）。"""
    props = (input_schema or {}).get("properties") or {}
    attrs = attributes or {}
    attr_names = [str(k) for k in attrs.keys()]
    norm_map: dict[str, list[str]] = {}
    for name in attr_names:
        norm_map.setdefault(_normalize(name), []).append(name)

    out: dict[str, list[dict]] = {}
    for raw_param, prop in props.items():
        param = str(raw_param)
        schema_type = _schema_type(prop)
        candidates: list[dict] = []
        seen: set[str] = set()
        for attr in attr_names:
            exact = attr == param
            normalized = attr in norm_map.get(_normalize(param), [])
            if not exact and not normalized:
                continue
            value = f"$attr.{attr}"
            if value in seen:
                continue
            seen.add(value)
            compatible = _types_compatible(schema_type, _value_type(attrs.get(attr)))
            if exact:
                confidence = 0.98 if compatible else 0.72
                reason = "同名本体属性" if compatible else "同名本体属性，但类型可能不匹配"
            else:
                confidence = 0.9 if compatible else 0.65
                reason = "归一化同名本体属性" if compatible else "归一化同名本体属性，但类型可能不匹配"
            candidates.append({
                "param": param,
                "value": value,
                "provenance": "rule",
                "confidence": confidence,
                "reason": reason,
            })
        if not candidates:
            candidates.append({
                "param": param,
                "value": f"$input.{param}",
                "provenance": "rule",
                "confidence": 0.6,
                "reason": "由调用方同名输入提供",
            })
        candidates.sort(key=lambda c: float(c.get("confidence") or 0), reverse=True)
        out[param] = candidates
    return out


def _best_rule(candidates: list[dict]) -> Optional[dict]:
    high = [c for c in candidates if float(c.get("confidence") or 0) >= RULE_HIGH_CONFIDENCE]
    if len(high) == 1:
        return high[0]
    return None


def _stable_context(tool: Optional[dict], entity: Optional[dict], source: str, tool_name: str) -> dict:
    attrs = (entity or {}).get("attributes") or {}
    attributes = [
        {
            "name": str(k),
            "value_type": _value_type(v),
            "has_value": v not in (None, "", [], {}),
        }
        for k, v in list(attrs.items())[:MAX_ATTRIBUTES]
    ]
    definition = str(attrs.get("definition") or (entity or {}).get("description") or "")
    return {
        "action": {
            "name": str((entity or {}).get("name") or ""),
            "definition": definition[:500],
            "attributes": attributes,
        },
        "tool": {
            "source": source,
            "tool_name": tool_name,
            "name": str((tool or {}).get("name") or ""),
            "description": str((tool or {}).get("description") or "")[:1000],
            "input_schema": (tool or {}).get("input_schema") or {},
        },
    }


def context_hash(context: dict) -> str:
    raw = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sanitize_ai_mappings(
    raw_items: Any, props: dict, attr_names: set[str],
) -> tuple[list[dict], list[str]]:
    out: list[dict] = []
    warnings: list[str] = []
    if not isinstance(raw_items, list):
        return out, warnings
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        param = str(item.get("param") or "").strip()
        if not param or param not in props:
            warnings.append(f"AI 建议的参数不在工具 input_schema 中：{param[:64]}")
            continue
        value = item.get("value")
        if isinstance(value, str) and value.startswith("$attr."):
            attr = value[len("$attr."):]
            if attr not in attr_names:
                warnings.append(f"AI 建议的本体属性不存在：{attr[:64]}")
                continue
        try:
            validated = validate_param_mapping({param: value})
        except InvalidParameterError:
            warnings.append(f"AI 建议的取值不合法：{param[:64]}")
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        out.append({
            "param": param,
            "value": validated[param],
            "provenance": "ai",
            "confidence": round(confidence, 2),
            "reason": str(item.get("reason") or "")[:300],
        })
    return out, warnings


def _sanitize_questions(raw_items: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw_items, list):
        return out
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        options: list[dict] = []
        for option in (item.get("options") or [])[:4]:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "").strip()
            value = str(option.get("value") or "").strip()
            if not label or not value:
                continue
            options.append({
                "label": label[:160],
                "value": value[:300],
                "recommended": option.get("recommended") is True,
            })
        if not options:
            continue
        out.append({
            "id": str(item.get("id") or f"q{index + 1}")[:64],
            "question": question[:300],
            "options": options,
            "allow_free_text": item.get("allow_free_text") is not False,
        })
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def _deterministic_questions(
    required: list[str], covered: set[str], attributes: list[dict],
) -> list[dict]:
    out: list[dict] = []
    attr_names = [str(a.get("name")) for a in attributes if a.get("name")]
    for param in required:
        if param in covered:
            continue
        options = [
            {"label": f"本体属性 {name}", "value": f"$attr.{name}", "recommended": index == 0}
            for index, name in enumerate(attr_names[:3])
        ]
        options.append({
            "label": "由调用方同名输入提供",
            "value": f"$input.{param}",
            "recommended": not options,
        })
        out.append({
            "id": f"q_{param}"[:64],
            "question": f"工具参数 {param} 应取哪个本体重属性？",
            "options": options,
            "allow_free_text": True,
        })
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def _normalize_answers(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:10]:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question and answer:
            out.append({"question": question[:300], "answer": answer[:500]})
    return out


def _normalize_attribute_names(raw: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    items = raw if isinstance(raw, list) else []
    for item in items[:200]:
        if isinstance(item, dict):
            name = item.get("key") or item.get("name") or item.get("label")
        else:
            name = item
        name = str(name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _schema_paths(schema: Any, prefix: str = "$", depth: int = 0) -> list[dict]:
    if depth > 3 or not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    out: list[dict] = []
    for key, prop in list(props.items())[:40]:
        path = f"{prefix}.{key}"
        prop_type = str((prop or {}).get("type") or "") if isinstance(prop, dict) else ""
        out.append({"path": path, "type": prop_type})
        out.extend(_schema_paths(prop, path, depth + 1))
        if len(out) >= 80:
            break
    return out[:80]


def build_extract_input_candidates(
    input_schema: Optional[dict], attr_names: set[str],
) -> dict[str, dict]:
    """抽取期入参的规则候选：同名属性、归一化同名、实体名 / 类型。"""
    props = (input_schema or {}).get("properties") or {}
    attrs = sorted(attr_names)
    norm_map: dict[str, list[str]] = {}
    for name in attrs:
        norm_map.setdefault(_normalize(name), []).append(name)
    out: dict[str, dict] = {}
    for raw_param in props:
        param = str(raw_param)
        if param in attr_names:
            out[param] = {"value": f"$attr.{param}", "source": "rule", "reason": "同名本体属性"}
            continue
        matches = norm_map.get(_normalize(param), [])
        if len(matches) == 1:
            out[param] = {
                "value": f"$attr.{matches[0]}", "source": "rule",
                "reason": "归一化同名本体属性",
            }
            continue
        folded = param.casefold()
        if folded in ("name", "entity_name", "canonical_name", "action_name"):
            out[param] = {"value": "$entity.name", "source": "rule", "reason": "实体名"}
        elif folded in ("type", "entity_type"):
            out[param] = {"value": "$entity.type", "source": "rule", "reason": "实体类型"}
    return out


def _sanitize_extract_declaration(
    raw: Any, group: str, tool_name: str, attr_names: set[str],
) -> tuple[Optional[dict], list[str]]:
    if not isinstance(raw, dict):
        return None, ["AI 返回的声明不是对象"]
    warnings: list[str] = []
    raw_inputs = raw.get("inputs")
    inputs: dict = {}
    if isinstance(raw_inputs, dict):
        for param, value in list(raw_inputs.items())[:20]:
            name = str(param).strip()
            if isinstance(value, str) and value.startswith("$attr."):
                attr = value[len("$attr."):]
                if attr not in attr_names:
                    warnings.append(f"AI 引用的属性不存在：{attr[:64]}")
                    continue
            inputs[name] = value
    else:
        warnings.append("AI 未返回 inputs 对象")

    description = str(raw.get("description") or "")[:500]
    if group == "refine":
        raw_writes = raw.get("writes")
        writes: dict = {}
        if isinstance(raw_writes, dict):
            for attr, path in list(raw_writes.items())[:20]:
                name = str(attr).strip()
                if name in CONTRACT_FIELDS or name.startswith("_"):
                    warnings.append(f"AI 建议写入契约/系统属性：{name[:64]}")
                    continue
                if name not in attr_names:
                    # 新属性由前端在保存时自动加入对象定义（页面有「新属性」标记）
                    warnings.append(
                        f"AI 建议新增属性 {name[:64]}（保存时会自动加入，类型为字符串）"
                    )
                writes[name] = path
        else:
            warnings.append("AI 未返回 writes 对象")
        declaration = {
            "skill": f"catalog:{tool_name}",
            "description": description,
            "inputs": inputs,
            "writes": writes,
            "on_error": str(raw.get("on_error") or "keep"),
        }
    else:
        declaration = {
            "skill": f"catalog:{tool_name}",
            "description": description,
            "inputs": inputs,
            "reject_when": raw.get("reject_when"),
            "on_error": str(raw.get("on_error") or "keep"),
        }
    call, errors = parse_catalog_item(group, declaration)
    if call is None:
        return None, warnings + errors
    return declaration, warnings


def _summarize_preview(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    paths = raw.get("output_paths")
    clean_paths = []
    if isinstance(paths, list):
        for item in paths[:80]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            clean_paths.append({
                "path": path[:200],
                "type": str(item.get("type") or "")[:40],
                "sample": str(item.get("sample") or "")[:120],
            })
    writes = raw.get("writes")
    return {
        "status": str(raw.get("status") or "")[:40],
        "unresolved_writes": [
            str(x)[:128] for x in (raw.get("unresolved_writes") or [])[:20]
        ],
        "writes": {
            str(k)[:64]: str(v)[:128]
            for k, v in (writes or {}).items() if isinstance(writes, dict)
        } if isinstance(writes, dict) else {},
        "output_paths": clean_paths,
    }


_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_MAX_TOOL_TEXT = 50000


async def _existing_tool_names(tenant_id: str, kb_id: str) -> list[str]:
    async with get_db_session() as session:
        rows = (await session.execute(text(
            "SELECT tool_name FROM knowledge_base.kb_tools "
            "WHERE tenant_id = :t AND knowledge_base_id = :k AND is_deleted = 0"
        ), {"t": tenant_id, "k": kb_id})).all()
    return [str(row[0]) for row in rows]


def _strip_runtime_secrets(runtime: Any, warnings: list[str]) -> dict:
    if not isinstance(runtime, dict):
        return {}
    safe = dict(runtime)
    headers = safe.get("headers")
    if isinstance(headers, dict):
        clean_headers = {
            key: value for key, value in headers.items()
            if not _is_sensitive_header(key)
        }
        if clean_headers:
            safe["headers"] = clean_headers
        else:
            safe.pop("headers", None)
    # 递归删除 secret/token/password/Authorization/Cookie 等字段，覆盖
    # auth.secret、body.password、headers 中的敏感头（headers 已先按头名过滤）。
    # 注意 runtime.auth 是结构字段，不能被 _is_sensitive_name 当成凭据删掉。
    out = _strip_secret_object(safe, warnings)
    if not isinstance(out, dict):
        return {}
    if isinstance(out.get("url"), str) and out["url"]:
        out["url"] = redact_text(out["url"])[0]
    return out


def _sanitize_tool_draft(
    raw: Any, seed: Optional[dict], existing_names: list[str],
) -> tuple[Optional[dict], list[str]]:
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return None, ["AI 返回的草稿不是对象"]
    seed = seed if isinstance(seed, dict) else {}

    tool_name = str(raw.get("tool_name") or seed.get("tool_name") or "").strip().lower()
    if not _TOOL_NAME_RE.match(tool_name):
        fallback = str(seed.get("tool_name") or "new_tool")
        warnings.append("AI 生成的 tool_name 不合法，已回退为种子值/占位值")
        tool_name = fallback if _TOOL_NAME_RE.match(fallback) else "new_tool"
    if tool_name in existing_names:
        warnings.append(f"本库已有同名工具：{tool_name}，保存时会被拒绝")

    name = str(raw.get("name") or seed.get("name") or tool_name)[:255].strip() or tool_name
    description = str(raw.get("description") or seed.get("description") or "")[:1000]

    input_schema = raw.get("input_schema")
    if input_schema is None:
        input_schema = seed.get("input_schema") or {}
    try:
        input_schema = validate_tool_schema(input_schema, "input_schema")
    except InvalidParameterError as exc:
        warnings.append(f"input_schema 未通过校验：{exc.message}")
        input_schema = input_schema if isinstance(input_schema, dict) else {}

    output_schema = raw.get("output_schema")
    if output_schema is None:
        output_schema = seed.get("output_schema") or {}
    try:
        output_schema = validate_tool_schema(output_schema, "output_schema")
    except InvalidParameterError as exc:
        warnings.append(f"output_schema 未通过校验：{exc.message}")
        output_schema = output_schema if isinstance(output_schema, dict) else {}

    seed_runtime = seed.get("runtime") if isinstance(seed.get("runtime"), dict) else {}
    raw_runtime = _strip_runtime_secrets(raw.get("runtime"), warnings)
    runtime: dict = dict(seed_runtime)
    for key, value in raw_runtime.items():
        if value not in (None, "", {}, []):
            runtime[key] = value
    # 种子已识别出的鉴权方式不要被 AI 的 none 覆盖
    seed_auth = seed_runtime.get("auth") if isinstance(seed_runtime.get("auth"), dict) else {}
    if seed_auth.get("type") not in (None, "", "none") and (
        not isinstance(runtime.get("auth"), dict)
        or runtime.get("auth", {}).get("type") in (None, "", "none")
    ):
        runtime["auth"] = seed_auth
    runtime = _strip_runtime_secrets(runtime, warnings)
    try:
        runtime = validate_runtime(runtime) or {}
    except InvalidParameterError as exc:
        warnings.append(f"runtime 未通过校验：{exc.message}；请手工补全后再保存")

    suggestion = raw.get("read_only_suggestion")
    read_only_suggestion = None
    if isinstance(suggestion, dict):
        read_only_suggestion = {
            "value": suggestion.get("value") is True,
            "reason": str(suggestion.get("reason") or "")[:300],
        }
    if "read_only" in raw:
        warnings.append("AI 草稿里的 read_only 已忽略；只读声明必须由用户手动勾选")

    assumptions = [
        str(item)[:300] for item in (raw.get("assumptions") or [])[:10]
        if str(item).strip()
    ]
    unsupported = [
        str(item)[:300] for item in (raw.get("unsupported") or [])[:10]
        if str(item).strip()
    ]
    draft = {
        "tool_name": tool_name,
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "runtime": runtime,
        "read_only_suggestion": read_only_suggestion,
        "assumptions": assumptions,
        "unsupported": unsupported,
    }
    return draft, warnings


class ConfigAssistService:
    """工具绑定参数映射的 AI 辅助（仅建议，不写库）。"""

    def __init__(self, bindings: OntologyToolBindingService):
        self._bindings = bindings

    def _base_result(self, session_id: str, round_no: int, status: str) -> dict:
        return {
            "assist_session_id": session_id,
            "status": status,
            "round": round_no,
            "context_hash": None,
            "mappings": [],
            "questions": [],
            "warnings": [],
            "error": None,
        }

    async def _load(
        self, tenant_id: str, data: dict,
    ) -> tuple[Optional[dict], Optional[dict], str, str, str, bool]:
        kb_id = str(data.get("knowledge_base_id") or "").strip()
        action_name = str(data.get("action_name") or "").strip()
        source = str(data.get("tool_source") or "platform").strip()
        tool_name = str(data.get("tool_name") or "").strip()
        if not kb_id or not action_name or not tool_name:
            raise InvalidParameterError(message=translate(
                "err.config_assist.fields_required",
                fallback="knowledge_base_id、action_name、tool_name 必填",
            ))
        ctx = await self._bindings.load_mapping_context(
            tenant_id, kb_id, action_name, source, tool_name,
        )
        return (
            ctx["tool"], ctx["entity"], ctx["source"], ctx["tool_name"],
            kb_id, ctx["ontology_available"],
        )

    def _context_and_rules(
        self, tool: Optional[dict], entity: Optional[dict], source: str,
        tool_name: str, current_mapping: dict,
    ) -> tuple[dict, dict, dict, list[str]]:
        stable = _stable_context(tool, entity, source, tool_name)
        attrs = (entity or {}).get("attributes") or {}
        rules = build_rule_candidates((tool or {}).get("input_schema") or {}, attrs)
        params = ((tool or {}).get("input_schema") or {}).get("properties") or {}
        required = [str(r) for r in ((tool or {}).get("input_schema") or {}).get("required") or []]
        context = {
            **stable,
            "rule_candidates": rules,
            "current_mapping": current_mapping,
        }
        return context, stable, params, required

    def _merge(
        self,
        rules: dict[str, list[dict]],
        ai_mappings: list[dict],
        current_mapping: dict,
        required: list[str],
        attributes: list[dict],
        ai_questions: list[dict],
        warnings: list[str],
    ) -> tuple[list[dict], list[dict], str]:
        merged: dict[str, dict] = {}
        for param, candidates in rules.items():
            # 用户已经显式映射的参数保持不动，AI 只补空缺
            if param in current_mapping:
                continue
            best = _best_rule(candidates)
            if best is not None:
                merged[param] = dict(best)

        for item in ai_mappings:
            param = item["param"]
            if param in current_mapping:
                continue
            if param in merged:
                warnings.append(f"规则已确定参数 {param}，忽略 AI 的不同建议")
                continue
            merged[param] = item

        covered = set(merged) | set(current_mapping)
        questions = ai_questions or _deterministic_questions(required, covered, attributes)
        mappings = list(merged.values())
        if questions:
            status = "needs_input"
        elif mappings or not required:
            status = "ready"
        else:
            status = "invalid"
        return mappings, questions, status

    async def _load_extract_tool(self, tenant_id: str, data: dict) -> tuple[Optional[dict], str, list[str], str, str]:
        kb_id = str(data.get("knowledge_base_id") or "").strip()
        tool_name = str(data.get("tool_name") or "").strip()
        source = str(data.get("tool_source") or "platform").strip()
        group = str(data.get("group") or "refine").strip()
        if group not in ("refine", "constrain"):
            raise InvalidParameterError(message="group 只能是 refine 或 constrain")
        if not kb_id or not tool_name:
            raise InvalidParameterError(message=translate(
                "err.config_assist.fields_required",
                fallback="knowledge_base_id、tool_name 必填",
            ))
        tool = await self._bindings.load_tool(tenant_id, kb_id, source, tool_name)
        attrs = _normalize_attribute_names(data.get("attributes"))
        return tool, kb_id, attrs, group, tool_name

    async def propose_extract_declaration(
        self, tenant_id: str, data: dict, *,
        user_id: Optional[str] = None, username: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        session_id = str(data.get("assist_session_id") or uuid.uuid4().hex)
        result = self._base_result(session_id, 1, "disabled")
        result.update({"group": None, "declaration": None})
        if not config_assist_enabled():
            result["error"] = translate(
                "msg.config_assist.disabled",
                fallback="AI 辅助配置未开启（ONTOLOGY_CONFIG_ASSIST_ENABLED）",
            )
            return result

        tool, kb_id, attrs, group, tool_name = await self._load_extract_tool(tenant_id, data)
        result["group"] = group
        if tool is None:
            result["status"] = "tool_unavailable"
            result["error"] = translate(
                "msg.config_assist.tool_unavailable",
                fallback="工具不可用（未上架、本租户未启用或本库工具已停用）",
            )
            return result
        if tool.get("read_only") is not True:
            result["status"] = "tool_not_read_only"
            result["error"] = "抽取期只能调用只读工具"
            return result

        attr_set = set(attrs)
        if group == "refine" and not attr_set:
            result["status"] = "invalid"
            result["error"] = translate(
                "msg.config_assist.no_attributes",
                fallback="本对象还没有属性，AI 无法生成写入；请先在对象里添加属性",
            )
            return result
        input_schema = tool.get("input_schema") or {}
        output_schema = tool.get("output_schema") or {}
        if group == "refine" and not output_schema:
            result["status"] = "invalid"
            result["error"] = translate(
                "msg.config_assist.preview_required",
                fallback=(
                    "工具未声明 output_schema，无法确定写入路径；"
                    "请先点「试跑」拿到输出，再点「AI 修正」"
                ),
            )
            result["warnings"] = [result["error"]]
            return result
        rule_inputs = build_extract_input_candidates(input_schema, attr_set)
        current = data.get("current_declaration")
        current = current if isinstance(current, dict) else {}
        stable = {
            "entity_type": str(data.get("entity_type") or "")[:128],
            "group": group,
            "tool": {
                "tool_name": tool_name,
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or "")[:1000],
                "input_schema": input_schema,
                "output_schema": output_schema,
            },
            "attributes": attrs,
        }
        result["context_hash"] = context_hash(stable)
        warnings: list[str] = []
        context = {
            **stable,
            "rule_inputs": rule_inputs,
            "output_paths": _schema_paths(output_schema),
            "current_declaration": current,
        }
        try:
            llm = await suggest_extract_tool_declaration(
                context, tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                trace_id=request_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("抽取期声明 AI 生成失败 kb=%s tool=%s err=%s", kb_id, tool_name, exc)
            result["status"] = "failed"
            result["error"] = translate(
                "msg.config_assist.ai_failed", fallback="AI 推荐失败，请稍后重试",
            )
            return result

        declaration, sanitize_warnings = _sanitize_extract_declaration(
            llm.get("declaration"), group, tool_name, attr_set,
        )
        warnings.extend(sanitize_warnings)
        warnings.extend(str(w)[:300] for w in (llm.get("warnings") or [])[:10])
        if declaration is None:
            result["status"] = "invalid"
            result["error"] = "；".join(warnings[:3]) or "AI 没有给出合法声明"
            result["warnings"] = warnings
            return result

        merged_inputs = {k: v["value"] for k, v in rule_inputs.items()}
        for key, value in declaration.get("inputs", {}).items():
            if key in merged_inputs and merged_inputs[key] != value:
                warnings.append(f"规则已确定入参 {key}，忽略 AI 的不同建议")
                continue
            merged_inputs.setdefault(key, value)
        declaration["inputs"] = merged_inputs
        for required_name in (input_schema.get("required") or []):
            name = str(required_name)
            if name not in merged_inputs:
                warnings.append(f"工具必填入参 {name} 未映射")
        result.update({
            "status": "ready",
            "declaration": declaration,
            "warnings": warnings,
        })
        return result

    async def refine_extract_declaration(
        self, tenant_id: str, data: dict, *,
        user_id: Optional[str] = None, username: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        session_id = str(data.get("assist_session_id") or uuid.uuid4().hex)
        try:
            round_no = int(data.get("round") or 1)
        except (TypeError, ValueError):
            round_no = 1
        round_no = max(1, min(round_no, MAX_ROUNDS))
        result = self._base_result(session_id, round_no, "disabled")
        result.update({"group": None, "declaration": None})
        if not config_assist_enabled():
            result["error"] = translate(
                "msg.config_assist.disabled",
                fallback="AI 辅助配置未开启（ONTOLOGY_CONFIG_ASSIST_ENABLED）",
            )
            return result
        if round_no >= MAX_ROUNDS:
            result["status"] = "invalid"
            result["error"] = translate(
                "msg.config_assist.round_limit",
                fallback="AI 推荐最多 3 轮，请人工完成剩余参数",
            )
            return result

        tool, kb_id, attrs, group, tool_name = await self._load_extract_tool(tenant_id, data)
        result["group"] = group
        if tool is None:
            result["status"] = "tool_unavailable"
            result["error"] = translate(
                "msg.config_assist.tool_unavailable",
                fallback="工具不可用（未上架、本租户未启用或本库工具已停用）",
            )
            return result
        if tool.get("read_only") is not True:
            result["status"] = "tool_not_read_only"
            result["error"] = "抽取期只能调用只读工具"
            return result

        input_schema = tool.get("input_schema") or {}
        output_schema = tool.get("output_schema") or {}
        attr_set = set(attrs)
        if group == "refine" and not attr_set:
            result["status"] = "invalid"
            result["error"] = translate(
                "msg.config_assist.no_attributes",
                fallback="本对象还没有属性，AI 无法生成写入；请先在对象里添加属性",
            )
            return result
        rule_inputs = build_extract_input_candidates(input_schema, attr_set)
        current = data.get("current_declaration")
        current = current if isinstance(current, dict) else {}
        stable = {
            "entity_type": str(data.get("entity_type") or "")[:128],
            "group": group,
            "tool": {
                "tool_name": tool_name,
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or "")[:1000],
                "input_schema": input_schema,
                "output_schema": output_schema,
            },
            "attributes": attrs,
        }
        current_hash = context_hash(stable)
        result["context_hash"] = current_hash
        warnings: list[str] = []
        if data.get("context_hash") and data.get("context_hash") != current_hash:
            warnings.append("配置已变化，AI 基于最新配置重新生成")
        preview = _summarize_preview(data.get("preview"))
        if group == "refine" and not output_schema and not preview.get("output_paths"):
            result["status"] = "invalid"
            result["error"] = translate(
                "msg.config_assist.preview_paths_missing",
                fallback=(
                    "工具未声明 output_schema，试跑输出也没有可用路径；"
                    "请确认工具返回 JSON，或手动填写写入路径"
                ),
            )
            result["warnings"] = [result["error"]]
            return result
        context = {
            **stable,
            "rule_inputs": rule_inputs,
            "output_paths": _schema_paths(output_schema),
            "current_declaration": current,
            "preview": preview,
            "round": round_no + 1,
        }
        try:
            llm = await suggest_extract_tool_declaration(
                context, tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                trace_id=request_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("抽取期声明 AI 修正失败 kb=%s tool=%s err=%s", kb_id, tool_name, exc)
            result["status"] = "failed"
            result["error"] = translate(
                "msg.config_assist.ai_failed", fallback="AI 推荐失败，请稍后重试",
            )
            result["warnings"] = warnings
            return result

        declaration, sanitize_warnings = _sanitize_extract_declaration(
            llm.get("declaration"), group, tool_name, attr_set,
        )
        warnings.extend(sanitize_warnings)
        warnings.extend(str(w)[:300] for w in (llm.get("warnings") or [])[:10])
        if declaration is None:
            result["status"] = "invalid"
            result["error"] = "；".join(warnings[:3]) or "AI 没有给出合法声明"
            result["warnings"] = warnings
            return result

        merged_inputs = {k: v["value"] for k, v in rule_inputs.items()}
        for key, value in declaration.get("inputs", {}).items():
            if key in merged_inputs and merged_inputs[key] != value:
                warnings.append(f"规则已确定入参 {key}，忽略 AI 的不同建议")
                continue
            merged_inputs.setdefault(key, value)
        declaration["inputs"] = merged_inputs
        for required_name in (input_schema.get("required") or []):
            name = str(required_name)
            if name not in merged_inputs:
                warnings.append(f"工具必填入参 {name} 未映射")
        result.update({
            "status": "ready",
            "round": round_no + 1,
            "declaration": declaration,
            "warnings": warnings,
        })
        return result

    async def propose_kb_tool_draft(
        self, tenant_id: str, data: dict, *,
        user_id: Optional[str] = None, username: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        session_id = str(data.get("assist_session_id") or uuid.uuid4().hex)
        result = self._base_result(session_id, 1, "disabled")
        result["draft"] = None
        if not config_assist_enabled():
            result["error"] = translate(
                "msg.config_assist.disabled",
                fallback="AI 辅助配置未开启（ONTOLOGY_CONFIG_ASSIST_ENABLED）",
            )
            return result

        kb_id = str(data.get("knowledge_base_id") or "").strip()
        if not kb_id:
            raise InvalidParameterError(message=translate(
                "err.config_assist.fields_required",
                fallback="knowledge_base_id 必填",
            ))
        text = str(data.get("text") or "").strip()
        if not text:
            result["status"] = "invalid"
            result["error"] = "请输入工具描述或粘贴 curl / OpenAPI 文档"
            return result
        warnings: list[str] = []
        if len(text) > _MAX_TOOL_TEXT:
            text = text[:_MAX_TOOL_TEXT]
            warnings.append(f"输入超过 {_MAX_TOOL_TEXT} 字符，已截断后处理")

        parsed = parse_tool_input(text)
        seed = parsed.get("seed")
        warnings.extend(parsed.get("warnings") or [])
        existing: list[str] = []
        try:
            existing = await _existing_tool_names(tenant_id, kb_id)
        except Exception as exc:  # noqa: BLE001 — 重名预检失败不阻断
            logger.warning("工具草稿：读取本库工具名失败 kb=%s err=%s", kb_id, exc)
            warnings.append("读取本库工具名失败，未做重名预检")

        redacted_text = str(parsed.get("redacted_text") or "")[:20000]
        context = {
            # 只送打码后的文本；原始 intent 可能含用户粘贴的凭据
            "intent": redacted_text[:2000],
            "redacted_text": redacted_text,
            "seed": seed or {},
            "existing_tool_names": existing,
        }
        try:
            llm = await suggest_kb_tool_draft(
                context, tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                trace_id=request_id,
            )
            raw_draft = llm.get("draft")
            warnings.extend(str(w)[:300] for w in (llm.get("warnings") or [])[:10])
        except Exception as exc:  # noqa: BLE001
            logger.warning("工具草稿 AI 生成失败 kb=%s err=%s", kb_id, exc)
            if seed:
                raw_draft = seed
                warnings.append(translate(
                    "msg.config_assist.ai_fallback",
                    fallback="AI 推荐失败，已返回规则匹配结果",
                ))
            else:
                result["status"] = "failed"
                result["error"] = translate(
                    "msg.config_assist.ai_failed", fallback="AI 推荐失败，请稍后重试",
                )
                result["warnings"] = warnings
                return result

        draft, sanitize_warnings = _sanitize_tool_draft(raw_draft, seed, existing)
        warnings.extend(sanitize_warnings)
        if draft is None:
            result["status"] = "invalid"
            result["error"] = "；".join(warnings[:3]) or "AI 没有给出合法草稿"
            result["warnings"] = warnings
            return result
        result.update({
            "status": "ready",
            "draft": draft,
            "warnings": warnings,
        })
        return result

    async def propose_binding_mapping(
        self, tenant_id: str, data: dict, *,
        user_id: Optional[str] = None, username: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        session_id = str(data.get("assist_session_id") or uuid.uuid4().hex)
        result = self._base_result(session_id, 1, "disabled")
        if not config_assist_enabled():
            result["error"] = translate(
                "msg.config_assist.disabled",
                fallback="AI 辅助配置未开启（ONTOLOGY_CONFIG_ASSIST_ENABLED）",
            )
            return result

        tool, entity, source, tool_name, kb_id, ontology_available = await self._load(tenant_id, data)
        if tool is None:
            result["status"] = "tool_unavailable"
            result["error"] = translate(
                "msg.config_assist.tool_unavailable",
                fallback="工具不可用（未上架、本租户未启用或本库工具已停用）",
            )
            return result
        if not ontology_available:
            result["status"] = "ontology_unavailable"
            result["error"] = translate(
                "msg.config_assist.ontology_unavailable",
                fallback="本体暂不可用，无法读取动作属性",
            )
            return result
        if entity is None:
            result["status"] = "action_not_found"
            result["error"] = translate(
                "msg.config_assist.action_not_found",
                fallback="动作在本体中不存在",
            )
            return result

        try:
            current_mapping = validate_param_mapping(data.get("current_mapping") or {})
        except InvalidParameterError as exc:
            result["status"] = "invalid"
            result["error"] = exc.message
            return result

        context, stable, params, required = self._context_and_rules(
            tool, entity, source, tool_name, current_mapping,
        )
        result["context_hash"] = context_hash(stable)
        attr_names = {str(a.get("name")) for a in stable["action"]["attributes"]}
        attributes = stable["action"]["attributes"]
        # 只对必填参数做 AI 消歧；可选参数没匹配到属性时留给调用方，不追问
        ambiguous = [
            p for p in required
            if _best_rule(context["rule_candidates"].get(p, [])) is None
        ]

        ai_mappings: list[dict] = []
        ai_questions: list[dict] = []
        warnings: list[str] = []
        if ambiguous:
            try:
                llm = await suggest_tool_binding_mapping(
                    {**context, "ambiguous_params": ambiguous, "round": 1},
                    tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                    trace_id=request_id,
                )
                ai_mappings, dropped = _sanitize_ai_mappings(
                    llm.get("mappings"), params, attr_names,
                )
                warnings.extend(dropped)
                warnings.extend(str(w)[:300] for w in (llm.get("warnings") or [])[:10])
                ai_questions = _sanitize_questions(llm.get("questions"))
            except Exception as exc:  # noqa: BLE001 — AI 失败不阻断规则候选
                logger.warning("工具绑定映射 AI 推荐失败 kb=%s action=%s err=%s", kb_id, data.get("action_name"), exc)
                warnings.append(translate(
                    "msg.config_assist.ai_fallback",
                    fallback="AI 推荐失败，已返回规则匹配结果",
                ))

        mappings, questions, status = self._merge(
            context["rule_candidates"], ai_mappings, current_mapping,
            required, attributes, ai_questions, warnings,
        )
        result.update({
            "status": status,
            "mappings": mappings,
            "questions": questions,
            "warnings": warnings,
        })
        return result

    async def refine_binding_mapping(
        self, tenant_id: str, data: dict, *,
        user_id: Optional[str] = None, username: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        session_id = str(data.get("assist_session_id") or uuid.uuid4().hex)
        try:
            round_no = int(data.get("round") or 1)
        except (TypeError, ValueError):
            round_no = 1
        round_no = max(1, min(round_no, MAX_ROUNDS))
        result = self._base_result(session_id, round_no, "disabled")
        if not config_assist_enabled():
            result["error"] = translate(
                "msg.config_assist.disabled",
                fallback="AI 辅助配置未开启（ONTOLOGY_CONFIG_ASSIST_ENABLED）",
            )
            return result
        if round_no >= MAX_ROUNDS:
            result["status"] = "invalid"
            result["error"] = translate(
                "msg.config_assist.round_limit",
                fallback="AI 推荐最多 3 轮，请人工完成剩余参数",
            )
            return result

        tool, entity, source, tool_name, kb_id, ontology_available = await self._load(tenant_id, data)
        if tool is None:
            result["status"] = "tool_unavailable"
            result["error"] = translate(
                "msg.config_assist.tool_unavailable",
                fallback="工具不可用（未上架、本租户未启用或本库工具已停用）",
            )
            return result
        if not ontology_available:
            result["status"] = "ontology_unavailable"
            result["error"] = translate(
                "msg.config_assist.ontology_unavailable",
                fallback="本体暂不可用，无法读取动作属性",
            )
            return result
        if entity is None:
            result["status"] = "action_not_found"
            result["error"] = translate(
                "msg.config_assist.action_not_found",
                fallback="动作在本体中不存在",
            )
            return result

        try:
            current_mapping = validate_param_mapping(data.get("current_mapping") or {})
        except InvalidParameterError as exc:
            result["status"] = "invalid"
            result["error"] = exc.message
            return result

        context, stable, params, required = self._context_and_rules(
            tool, entity, source, tool_name, current_mapping,
        )
        current_hash = context_hash(stable)
        result["context_hash"] = current_hash
        warnings: list[str] = []
        if data.get("context_hash") and data.get("context_hash") != current_hash:
            warnings.append("配置已变化，AI 基于最新配置重新推荐")

        answers = _normalize_answers(data.get("answers"))
        try:
            llm = await suggest_tool_binding_mapping(
                {**context, "answers": answers, "round": round_no + 1},
                tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                trace_id=request_id,
            )
            ai_mappings, dropped = _sanitize_ai_mappings(
                llm.get("mappings"), params, {str(a.get("name")) for a in stable["action"]["attributes"]},
            )
            warnings.extend(dropped)
            warnings.extend(str(w)[:300] for w in (llm.get("warnings") or [])[:10])
            ai_questions = _sanitize_questions(llm.get("questions"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("工具绑定映射 AI 续推失败 kb=%s action=%s err=%s", kb_id, data.get("action_name"), exc)
            result["status"] = "failed"
            result["error"] = translate(
                "msg.config_assist.ai_failed",
                fallback="AI 推荐失败，请稍后重试",
            )
            result["warnings"] = warnings
            return result

        mappings, questions, status = self._merge(
            context["rule_candidates"], ai_mappings, current_mapping,
            required, stable["action"]["attributes"], ai_questions, warnings,
        )
        result.update({
            "status": status,
            "round": round_no + 1,
            "mappings": mappings,
            "questions": questions,
            "warnings": warnings,
        })
        return result
