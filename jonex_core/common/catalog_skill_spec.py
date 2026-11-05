#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""TBox 抽取技能里的 `catalog:` 声明：格式与校验（纯函数，无 IO）。

`catalog:<tool_name>` 表示「抽取某类实体时调用一个登记过的只读工具」。抽取器（atomic-rag /
ontology-compile）不执行它；knowledge-base 在写图前统一执行（`services/extract_tool_service.py`），
因为工具定义、凭据、出站校验与审计都在 knowledge-base。保存 schema 时 DTO 用同一份校验。

声明形状（挂在 `entity_types[].extraction_skills` 的 refine / constrain 分组里）::

    refine:                                   # 补充：把工具返回的字段写进实体属性
      - skill: catalog:action_system_lookup   # 先找本库工具，再找本租户已启用的平台工具
        inputs: {action_name: "$entity.name"} # 工具入参 ← $entity.name / $entity.type / $attr.<属性> / 字面量
        writes: {system_code: "$.data.system_code"}   # 只允许写这里列出的属性（点路径取工具输出）
        on_error: keep                        # keep（保留原值，默认）| drop_attr（清掉 writes 里的属性）
    constrain:                                # 校验：命中条件就丢弃该实体及其挂接关系
      - skill: catalog:org_code_validator
        inputs: {org_code: "$attr.org_code"}
        reject_when: {path: "$.data.valid", equals: false}
        on_error: keep                        # keep（放行，默认）| reject（调用失败也丢弃）

约束
    - `writes` 不能写 Action 的契约字段：契约字段是文档写的执行边界，不能由外部系统填；
      也不能写以下划线开头的属性（留给系统字段，如工具来源记录）。
    - `inputs` 里任一 `$` 引用取不到值时，该实体跳过、不调用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

CATALOG_PREFIX = "catalog:"

# 与 knowledge_base 的 Action 契约字段一致（ontology_tools.CONTRACT_FIELDS）
CONTRACT_FIELDS = (
    "preconditions", "authorization", "atomic_effects", "follow_ups",
    "failure_policy", "audit_required",
)

REFINE_ON_ERROR = ("keep", "drop_attr")
CONSTRAIN_ON_ERROR = ("keep", "reject")

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ATTR_REF_RE = re.compile(r"^\$attr\.([A-Za-z_][A-Za-z0-9_]{0,63})$")
_PATH_RE = re.compile(r"^\$(\.[^.\[\]\s]+|\[\d+\])*$")
_ENTITY_REFS = ("$entity.name", "$entity.type")
_MAX_ENTRIES = 20
_ALLOWED_KEYS = {
    "refine": {"skill", "description", "inputs", "writes", "on_error"},
    "constrain": {"skill", "description", "inputs", "reject_when", "on_error"},
}


@dataclass
class CatalogSkillCall:
    group: str                         # refine | constrain
    tool_name: str
    description: str = ""
    inputs: dict = field(default_factory=dict)
    writes: dict = field(default_factory=dict)
    on_error: str = "keep"
    reject_when: Optional[dict] = None


def is_catalog_skill(name: Any) -> bool:
    return isinstance(name, str) and name.strip().startswith(CATALOG_PREFIX)


def _check_inputs(raw: Any, where: str, errors: list[str]) -> dict:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > _MAX_ENTRIES:
        errors.append(f"{where}.inputs 必须是对象，最多 {_MAX_ENTRIES} 项")
        return {}
    out: dict = {}
    for k, v in raw.items():
        k = str(k).strip()
        if not _KEY_RE.match(k):
            errors.append(f"{where}.inputs 的入参名不合法: {k[:40]}")
            continue
        if isinstance(v, str) and v.startswith("$"):
            if v not in _ENTITY_REFS and not _ATTR_REF_RE.match(v):
                errors.append(f"{where}.inputs.{k} 取值来源不合法: {v[:60]}")
                continue
        elif not isinstance(v, (str, int, float, bool)):
            errors.append(f"{where}.inputs.{k} 的字面量只能是字符串、数字或布尔")
            continue
        out[k] = v
    return out


def _check_writes(raw: Any, where: str, errors: list[str]) -> dict:
    if not isinstance(raw, dict) or not raw or len(raw) > _MAX_ENTRIES:
        errors.append(f"{where}.writes 必须是非空对象，最多 {_MAX_ENTRIES} 项：{{属性: \"$.输出路径\"}}")
        return {}
    out: dict = {}
    for attr, path in raw.items():
        attr = str(attr).strip()
        if not _KEY_RE.match(attr) or attr.startswith("_"):
            errors.append(f"{where}.writes 的属性名不合法: {attr[:40]}")
            continue
        if attr in CONTRACT_FIELDS:
            errors.append(f"{where}.writes 不能写契约字段 {attr}（契约字段只能来自文档）")
            continue
        if not isinstance(path, str) or not _PATH_RE.match(path.strip()):
            errors.append(f"{where}.writes.{attr} 的路径必须形如 $.data.field")
            continue
        out[attr] = path.strip()
    return out


def _check_reject_when(raw: Any, where: str, errors: list[str]) -> Optional[dict]:
    if not isinstance(raw, dict) or "path" not in raw or "equals" not in raw:
        errors.append(f"{where}.reject_when 必须是 {{path: \"$.输出路径\", equals: 值}}")
        return None
    path = raw.get("path")
    if not isinstance(path, str) or not _PATH_RE.match(path.strip()):
        errors.append(f"{where}.reject_when.path 必须形如 $.data.field")
        return None
    equals = raw.get("equals")
    if not isinstance(equals, (str, int, float, bool)) and equals is not None:
        errors.append(f"{where}.reject_when.equals 只能是字符串、数字、布尔或 null")
        return None
    return {"path": path.strip(), "equals": equals}


def parse_catalog_item(group: str, item: dict, where: str = "") -> tuple[Optional[CatalogSkillCall], list[str]]:
    """校验一条 `catalog:` 声明；返回 (调用, 错误列表)，有错误时调用为 None。"""
    errors: list[str] = []
    where = where or f"extraction_skills.{group}"
    skill = str(item.get("skill") or "").strip()
    tool_name = skill[len(CATALOG_PREFIX):]
    if not _TOOL_NAME_RE.match(tool_name):
        return None, [f"{where} 工具名不合法: {skill[:60]}"]
    where = f"{where}[{skill}]"
    unknown = set(item.keys()) - _ALLOWED_KEYS[group]
    if unknown:
        errors.append(f"{where} 不支持的字段: {', '.join(sorted(unknown))}")
    inputs = _check_inputs(item.get("inputs"), where, errors)
    on_error = str(item.get("on_error") or "keep").strip()
    writes: dict = {}
    reject_when = None
    if group == "refine":
        writes = _check_writes(item.get("writes"), where, errors)
        if on_error not in REFINE_ON_ERROR:
            errors.append(f"{where}.on_error 只能是 {' / '.join(REFINE_ON_ERROR)}")
    else:
        reject_when = _check_reject_when(item.get("reject_when"), where, errors)
        if on_error not in CONSTRAIN_ON_ERROR:
            errors.append(f"{where}.on_error 只能是 {' / '.join(CONSTRAIN_ON_ERROR)}")
    if errors:
        return None, errors
    return CatalogSkillCall(
        group=group, tool_name=tool_name, description=str(item.get("description") or ""),
        inputs=inputs, writes=writes, on_error=on_error, reject_when=reject_when,
    ), []


def parse_catalog_calls(raw_skills: Any) -> tuple[list[CatalogSkillCall], list[str]]:
    """从一个实体类型的 `extraction_skills` 里取出全部 `catalog:` 声明（refine 在前，保持声明顺序）。"""
    calls: list[CatalogSkillCall] = []
    errors: list[str] = []
    if not isinstance(raw_skills, dict):
        return calls, errors
    for group in ("refine", "constrain"):
        for item in raw_skills.get(group) or []:
            if not isinstance(item, dict) or not is_catalog_skill(item.get("skill")):
                continue
            call, errs = parse_catalog_item(group, item)
            errors.extend(errs)
            if call is not None:
                calls.append(call)
    return calls, errors


def resolve_inputs(inputs: dict, entity: dict) -> Optional[dict]:
    """按声明从实体取工具入参；任一 `$` 引用取不到值返回 None（该实体跳过、不调用）。"""
    attrs = entity.get("attributes") or {}
    out: dict = {}
    for k, v in (inputs or {}).items():
        if isinstance(v, str) and v.startswith("$"):
            if v == "$entity.name":
                value: Any = entity.get("canonical_name") or entity.get("name")
            elif v == "$entity.type":
                value = entity.get("entity_type") or entity.get("type")
            else:
                m = _ATTR_REF_RE.match(v)
                value = attrs.get(m.group(1)) if m and isinstance(attrs, dict) else None
            if value in (None, "", [], {}):
                return None
            out[k] = value
        else:
            out[k] = v
    return out


__all__ = [
    "CATALOG_PREFIX",
    "CONTRACT_FIELDS",
    "CONSTRAIN_ON_ERROR",
    "CatalogSkillCall",
    "REFINE_ON_ERROR",
    "is_catalog_skill",
    "parse_catalog_calls",
    "parse_catalog_item",
    "resolve_inputs",
]
