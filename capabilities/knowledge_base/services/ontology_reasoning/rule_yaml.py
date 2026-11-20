"""推理规则 YAML 文件（kind: reasoning_rules）解析与序列化。"""
from __future__ import annotations

from typing import Any, Optional

import yaml

from .contract import CONTRACT_VERSION

MAX_RULE_YAML_CHARS = 2_000_000


def parse_rule_yaml(yaml_text: str) -> tuple[Optional[dict], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    text = str(yaml_text or "")
    if not text.strip():
        return None, ["YAML 文本为空"], warnings
    if len(text) > MAX_RULE_YAML_CHARS:
        return None, [f"YAML 文本过大（上限 {MAX_RULE_YAML_CHARS} 字符）"], warnings
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"YAML 语法错误: {exc}"], warnings
    if not isinstance(raw, dict):
        return None, ["YAML 顶层必须是对象"], warnings
    if raw.get("version") != 2:
        errors.append(f"不支持的 YAML version: {raw.get('version')!r}（需要 2）")
    if raw.get("kind") != "reasoning_rules":
        errors.append(f"不支持的 kind: {raw.get('kind')!r}（需要 reasoning_rules）")
    if raw.get("contract_version") != CONTRACT_VERSION:
        errors.append(
            f"contract_version 不匹配: {raw.get('contract_version')!r}"
            f"（平台为 {CONTRACT_VERSION}）"
        )
    rules = raw.get("rules")
    if not isinstance(rules, list):
        errors.append("rules 必须是数组")
        rules = []
    removed = raw.get("removed_rule_keys") or []
    if not isinstance(removed, list):
        errors.append("removed_rule_keys 必须是数组")
        removed = []
    if errors:
        return None, errors, warnings
    return {
        "knowledge_base": raw.get("knowledge_base"),
        "rules": rules,
        "removed_rule_keys": [str(x) for x in removed if str(x or "").strip()],
    }, errors, warnings


def rule_to_export_dict(rule: dict) -> dict:
    out: dict[str, Any] = {
        "rule_key": rule.get("rule_key"),
        "name": rule.get("name"),
        "text": rule.get("text") or "",
        "premises": rule.get("premises") or [],
        "conclusion": rule.get("conclusion") or {},
        "status": rule.get("status") or "active",
        "enabled": rule.get("enabled") is not False,
        "origin": rule.get("origin") or "imported",
    }
    if rule.get("module"):
        out["module"] = rule.get("module")
    if rule.get("content_revision") is not None:
        out["content_revision"] = int(rule.get("content_revision") or 0)
        out["base_revision"] = int(rule.get("content_revision") or 0)
    if rule.get("description"):
        out["description"] = rule.get("description")
    return out


def dump_rule_yaml(
    rules: list[dict], knowledge_base_id: str = "",
    removed_rule_keys: Optional[list[str]] = None,
) -> str:
    data: dict[str, Any] = {
        "version": 2,
        "kind": "reasoning_rules",
        "contract_version": CONTRACT_VERSION,
    }
    if knowledge_base_id:
        data["knowledge_base"] = knowledge_base_id
    if removed_rule_keys:
        data["removed_rule_keys"] = list(removed_rule_keys)
    data["rules"] = [rule_to_export_dict(r) for r in rules]
    return yaml.dump(
        data, allow_unicode=True, default_flow_style=False,
        sort_keys=False, indent=2,
    )


__all__ = [
    "MAX_RULE_YAML_CHARS",
    "dump_rule_yaml",
    "parse_rule_yaml",
    "rule_to_export_dict",
]
