"""公理 YAML 文件（kind: ontology_axioms）的解析与序列化。

只做文件级校验；单条公理的形状/引用/冲突由 `axiom_checks` 负责。
"""
from __future__ import annotations

from typing import Any, Optional

import yaml

from .contract import CONTRACT_VERSION

MAX_AXIOM_YAML_CHARS = 2_000_000


def parse_axiom_yaml(yaml_text: str) -> tuple[Optional[dict], list[str], list[str]]:
    """返回 (payload, errors, warnings)。payload 含 axioms / removed_axiom_keys。"""
    errors: list[str] = []
    warnings: list[str] = []
    text = str(yaml_text or "")
    if not text.strip():
        return None, ["YAML 文本为空"], warnings
    if len(text) > MAX_AXIOM_YAML_CHARS:
        return None, [f"YAML 文本过大（上限 {MAX_AXIOM_YAML_CHARS} 字符）"], warnings
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"YAML 语法错误: {exc}"], warnings
    if not isinstance(raw, dict):
        return None, ["YAML 顶层必须是对象"], warnings

    if raw.get("version") != 2:
        errors.append(f"不支持的 YAML version: {raw.get('version')!r}（需要 2）")
    if raw.get("kind") != "ontology_axioms":
        errors.append(f"不支持的 kind: {raw.get('kind')!r}（需要 ontology_axioms）")
    if raw.get("contract_version") != CONTRACT_VERSION:
        errors.append(
            f"contract_version 不匹配: {raw.get('contract_version')!r}"
            f"（平台为 {CONTRACT_VERSION}）"
        )
    axioms = raw.get("axioms")
    if not isinstance(axioms, list):
        errors.append("axioms 必须是数组")
        axioms = []
    removed = raw.get("removed_axiom_keys") or []
    if not isinstance(removed, list):
        errors.append("removed_axiom_keys 必须是数组")
        removed = []
    removed = [str(item) for item in removed if str(item or "").strip()]
    if errors:
        return None, errors, warnings
    return {
        "knowledge_base": raw.get("knowledge_base"),
        "axioms": axioms,
        "removed_axiom_keys": removed,
    }, errors, warnings


def axiom_to_export_dict(axiom: dict) -> dict:
    """把存储/归一化后的公理转成导出条目。"""
    out: dict[str, Any] = {
        "axiom_key": axiom.get("axiom_key"),
        "kind": axiom.get("kind"),
        "subject": axiom.get("subject") or {},
    }
    if axiom.get("object") is not None:
        out["object"] = axiom.get("object")
    if axiom.get("chain"):
        out["chain"] = axiom.get("chain")
    if axiom.get("facets"):
        out["facets"] = axiom.get("facets")
    out["status"] = axiom.get("status") or "active"
    out["source"] = axiom.get("source") or "imported"
    out["origin"] = axiom.get("origin") or "imported"
    if axiom.get("module"):
        out["module"] = axiom.get("module")
    if axiom.get("content_revision") is not None:
        out["content_revision"] = int(axiom.get("content_revision") or 0)
        out["base_revision"] = int(axiom.get("content_revision") or 0)
    if axiom.get("description"):
        out["description"] = axiom.get("description")
    return out


def dump_axiom_yaml(
    axioms: list[dict], knowledge_base_id: str = "",
    removed_axiom_keys: Optional[list[str]] = None,
) -> str:
    data: dict[str, Any] = {
        "version": 2,
        "kind": "ontology_axioms",
        "contract_version": CONTRACT_VERSION,
    }
    if knowledge_base_id:
        data["knowledge_base"] = knowledge_base_id
    if removed_axiom_keys:
        data["removed_axiom_keys"] = list(removed_axiom_keys)
    data["axioms"] = [axiom_to_export_dict(a) for a in axioms]
    return yaml.dump(
        data, allow_unicode=True, default_flow_style=False,
        sort_keys=False, indent=2,
    )


__all__ = [
    "MAX_AXIOM_YAML_CHARS",
    "axiom_to_export_dict",
    "dump_axiom_yaml",
    "parse_axiom_yaml",
]
