#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""确认 join 提案 → 真正落到编译 schema 的 `relation_types`。

边界与理由：
- **只加边，不改别的**：读当前数据源级编译行，在 `relation_types` 末尾追加一条，
  其余字段原样带回（`save_compiled_schema` 是整份替换语义 —— 少带一样就洗掉一样）。
- **带来源标记**：新边的 `origin="agent"`，描述里带 `proposal:<id>`，可据此回滚；
  也是「人和 agent 加的边」区分的唯一依据。
- **幂等**：同一对 (source, source_column, target, target_column) 已存在时不重复加，
  返回 `reason="already_exists"`，确认动作本身仍然成功（人已经做过决定了）。
- **乐观锁**：走 `save_compiled_schema` 的 `expected_schema_version`；并发保存冲突
  时抛 409，由调用方如实回给前端，不重试（重试可能盖掉别人的编辑）。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from jonex_core.common.tenant import require_tenant

from ...repository.ontology_schema_repository import OntologySchemaRepository

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_]")


def build_relation_name(existing: list[dict], source: str, target: str) -> str:
    """`REF_<SOURCE>_<TARGET>`，与既有名字冲突时追加 `_2`、`_3`…（确定性）。"""
    base = "REF_" + _SAFE.sub("_", f"{source}_{target}").upper()
    names = {str(item.get("name") or "") for item in existing
             if isinstance(item, dict)}
    if base not in names:
        return base
    index = 2
    while f"{base}_{index}" in names:
        index += 1
    return f"{base}_{index}"


def build_relation_entry(existing: list[dict], *, source: str, source_column: str,
                         target: str, target_column: str, rationale: str = "",
                         proposal_id: str = "") -> dict:
    """构造一条可保存的 relation（键集合与 `_normalize_relation_types` 对齐）。"""
    name = build_relation_name(existing, source, target)
    note = (rationale or "").strip()
    description = f"{source}.{source_column} → {target}.{target_column}"
    if note:
        description += f"；{note}"
    if proposal_id:
        description += f"；proposal:{proposal_id}"
    return {
        "name": name,
        "display_name": name,
        "description": description[:1000],
        "status": "active",
        "aliases": [],
        "source": source,
        "target": target,
        "cardinality": "many_to_one",
        "inverse_of": None,
        "source_types": [],
        "target_types": [],
        "source_column": source_column,
        "target_column": target_column,
        "source_relation_id": None,
        "origin": "agent",
    }


def _edge_exists(relations: list[dict], *, source: str, source_column: str,
                 target: str, target_column: str) -> bool:
    for item in relations:
        if not isinstance(item, dict):
            continue
        if (str(item.get("source") or "") == source
                and str(item.get("source_column") or "") == source_column
                and str(item.get("target") or "") == target
                and str(item.get("target_column") or "") == target_column):
            return True
    return False


async def apply_edge(tenant_id: str, kb_id: str, proposal: dict,
                     *, edited_by: str = "") -> dict:
    """把一条已确认的 join 提案写进编译 schema；返回 `{applied, reason, relation}`。

    **不抛**：保存失败也回结构化结果（`applied=False` + `reason`），
    由调用方决定怎么展示 —— 决定（confirmed）本身已经生效，不该被回滚。
    """
    from ...services.ontology_compiler import OntologyCompiler  # noqa: PLC0415

    tenant_id = require_tenant(tenant_id)
    ds_id = str(proposal.get("data_source_id") or "").strip()
    if not ds_id:
        return {"applied": False, "reason": "no_data_source"}
    source = str(proposal.get("source_entity") or "").strip()
    source_column = str(proposal.get("source_column") or "").strip()
    target = str(proposal.get("target_entity") or "").strip()
    target_column = str(proposal.get("target_column") or "").strip()
    if not all((source, source_column, target, target_column)):
        return {"applied": False, "reason": "incomplete_edge"}

    from jonex_core.common import get_db_session

    async with get_db_session() as session:
        current = await OntologySchemaRepository(session).get_compiled_schema(
            tenant_id, kb_id, ds_id)
    if current is None:
        return {"applied": False, "reason": "no_compiled_schema"}

    relations = [item for item in (current.relation_types or [])
                 if isinstance(item, dict)]
    if _edge_exists(relations, source=source, source_column=source_column,
                    target=target, target_column=target_column):
        return {"applied": False, "reason": "already_exists"}

    entry = build_relation_entry(
        relations, source=source, source_column=source_column,
        target=target, target_column=target_column,
        rationale=str(proposal.get("rationale") or ""),
        proposal_id=str(proposal.get("proposal_id") or ""),
    )
    try:
        await OntologyCompiler().save_compiled_schema(
            tenant_id, kb_id,
            entity_types=list(current.entity_types or []),
            relation_types=[*relations, entry],
            edited_by=edited_by or "join-agent",
            expected_schema_version=current.schema_version,
            data_source_id=ds_id,
        )
    except Exception as exc:  # noqa: BLE001 - 决定已生效，保存失败只如实回报
        logger.warning("[join-agent] 落边失败 kb=%s ds=%s: %s", kb_id, ds_id, exc)
        return {"applied": False, "reason": "save_failed",
                "message": f"{type(exc).__name__}: {exc}"}
    return {"applied": True, "reason": "ok", "relation": entry}


__all__ = ["apply_edge", "build_relation_entry", "build_relation_name"]
