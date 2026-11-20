"""规则试跑执行器（T9）：规则 AST → 参数化 Cypher。

只执行规则表（B 档）；A 档公理只展示。结果不写回图，只用于 preview/推理链。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from jonex_core.common.neo4j_client import get_neo4j_driver

from .view import ReasoningView

DEFAULT_LIMIT = 200
DEFAULT_TIMEOUT = 3.0


def _var_aliases(rule: dict) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    order: list[str] = []
    for atom in [*(rule.get("premises") or []), rule.get("conclusion") or {}]:
        for field in ("s", "o"):
            var = atom.get(field)
            if var and var not in mapping:
                mapping[var] = f"n{len(mapping)}"
                order.append(var)
    return mapping, order


def compile_rule(
    rule: dict, view: Optional[ReasoningView] = None,
    *, tenant_id: str, kb_id: str, limit: int = DEFAULT_LIMIT,
    anchor_entity: str = "",
    extra_edges: Optional[list[dict]] = None,
    extra_types: Optional[list[dict]] = None,
) -> tuple[str, dict]:
    """纯函数：返回 (cypher, params)。变量名只映射为 n0/n1…，不拼接用户文本。"""
    aliases, order = _var_aliases(rule)
    extra_edges = list(extra_edges or [])
    extra_types = list(extra_types or [])
    has_extra_edges = bool(extra_edges)
    has_extra_types = bool(extra_types)
    params: dict = {
        "t": tenant_id, "k": kb_id, "limit": limit,
        "extra_edges": extra_edges,
        "extra_types": extra_types,
    }
    # 没有额外事实时用 MATCH 直接绑定关系端点，避免「全实体笛卡尔 + OPTIONAL」。
    relation_vars: set[str] = set()
    if not has_extra_edges:
        for atom in rule.get("premises") or []:
            if atom.get("kind") == "rel":
                relation_vars.update({atom.get("s"), atom.get("o")})
    node_vars = [var for var in order if var not in relation_vars]
    node_patterns = ", ".join(
        f"({aliases[var]}:OntologyEntity {{tenant_id:$t, kb_id:$k}})"
        for var in node_vars
    )
    unwinds: list[str] = []
    matches: list[str] = []
    filters: list[str] = []

    for index, atom in enumerate(rule.get("premises") or []):
        if atom.get("kind") == "rel":
            s = aliases[atom.get("s")]
            o = aliases[atom.get("o")]
            p = atom.get("p") or ""
            allowed = sorted(view.relation_descendants.get(p, {p})) if view else [p]
            params[f"p{index}"] = allowed
            if has_extra_edges:
                unwinds.append(f"UNWIND ($extra_edges + [null]) AS x{index}")
                matches.append(f"OPTIONAL MATCH ({s})-[r{index}:ONT_REL]->({o})")
                filters.append(
                    f"((r{index} IS NOT NULL AND r{index}.relation_type IN $p{index}) "
                    f"OR (x{index} IS NOT NULL AND x{index}.p IN $p{index} "
                    f"AND x{index}.s={s}.canonical_name AND x{index}.o={o}.canonical_name)) "
                    f"AND coalesce({s}.stub,false)=false "
                    f"AND coalesce({o}.stub,false)=false"
                )
            else:
                matches.append(f"MATCH ({s})-[r{index}:ONT_REL]->({o})")
                filters.append(
                    f"r{index}.relation_type IN $p{index} "
                    f"AND coalesce({s}.stub,false)=false "
                    f"AND coalesce({o}.stub,false)=false"
                )
        elif atom.get("kind") == "type":
            s = aliases[atom.get("s")]
            cls = atom.get("cls") or ""
            allowed = sorted(view.class_descendants.get(cls, {cls})) if view else [cls]
            params[f"t{index}"] = allowed
            if has_extra_types:
                unwinds.append(f"UNWIND ($extra_types + [null]) AS y{index}")
                filters.append(
                    f"({s}.entity_type IN $t{index} OR "
                    f"(y{index} IS NOT NULL AND y{index}.s={s}.canonical_name "
                    f"AND y{index}.cls IN $t{index}))"
                )
            else:
                filters.append(f"{s}.entity_type IN $t{index}")

    if anchor_entity:
        params["anchor"] = anchor_entity
        filters.append("(" + " OR ".join(
            f"{aliases[var]}.canonical_name = $anchor" for var in order
        ) + ")")

    lines = [*unwinds]
    if node_patterns:
        lines.append(f"MATCH {node_patterns}")
    lines.extend(matches)
    if has_extra_edges:
        # OPTIONAL MATCH 紧跟的 WHERE 会被视为它的匹配条件（无匹配时仍产 null 行）；
        # `WITH *` 把下面的 WHERE 提升为全局过滤。
        lines.append("WITH *")
    if filters:
        lines.append("WHERE " + " AND ".join(filters))

    select: list[str] = []
    for var in order:
        alias = aliases[var]
        select.append(f"{alias}.canonical_name AS {alias}_name")
        select.append(f"{alias}.entity_type AS {alias}_type")
        select.append(f"coalesce({alias}.doc_ids,[]) AS {alias}_docs")

    premise_rows: list[str] = []
    for index, atom in enumerate(rule.get("premises") or []):
        if atom.get("kind") == "rel":
            if has_extra_edges:
                premise_rows.append(
                    "{s:%s.canonical_name, p:CASE WHEN r%d IS NOT NULL THEN r%d.relation_type ELSE x%d.p END, "
                    "o:%s.canonical_name, docs:CASE WHEN r%d IS NOT NULL THEN coalesce(r%d.doc_ids,[]) "
                    "ELSE coalesce(x%d.docs,[]) END}"
                    % (aliases[atom["s"]], index, index, index,
                       aliases[atom["o"]], index, index, index)
                )
            else:
                premise_rows.append(
                    "{s:%s.canonical_name, p:r%d.relation_type, o:%s.canonical_name, "
                    "docs:coalesce(r%d.doc_ids,[])}"
                    % (aliases[atom["s"]], index, aliases[atom["o"]], index)
                )
        else:
            params[f"c{index}"] = atom.get("cls") or ""
            if has_extra_types:
                premise_rows.append(
                    "{s:%s.canonical_name, p:'type', o:$c%d, docs:coalesce(y%d.docs,%s.doc_ids,[])}"
                    % (aliases[atom["s"]], index, index, aliases[atom["s"]])
                )
            else:
                premise_rows.append(
                    "{s:%s.canonical_name, p:'type', o:$c%d, docs:coalesce(%s.doc_ids,[])}"
                    % (aliases[atom["s"]], index, aliases[atom["s"]])
                )
    select.append(f"[{', '.join(premise_rows)}] AS premise_rows")

    conclusion = rule.get("conclusion") or {}
    if conclusion.get("kind") == "rel":
        s = aliases[conclusion.get("s")]
        o = aliases[conclusion.get("o")]
        p = conclusion.get("p") or ""
        params["concl_p"] = p
        select.append(f"{s}.canonical_name AS derived_s")
        select.append(f"{s}.entity_type AS derived_s_type")
        select.append(f"{o}.canonical_name AS derived_o")
        select.append(f"{o}.entity_type AS derived_o_type")
        select.append("$concl_p AS derived_p")
        select.append(
            f"EXISTS {{ MATCH ({s})-[rc:ONT_REL {{relation_type:$concl_p}}]->({o}) }} "
            f"AS already_asserted"
        )
    else:
        s = aliases[conclusion.get("s")]
        cls = conclusion.get("cls") or ""
        params["concl_cls"] = cls
        select.append(f"{s}.canonical_name AS derived_s")
        select.append(f"{s}.entity_type AS derived_s_type")
        select.append("'type' AS derived_p")
        select.append("$concl_cls AS derived_o")
        select.append("'' AS derived_o_type")
        select.append(f"{s}.entity_type = $concl_cls AS already_asserted")

    lines.append("RETURN " + ", ".join(select))
    lines.append("LIMIT $limit")
    return "\n".join(lines), params


async def execute_rule(
    rule: dict,
    view: Optional[ReasoningView],
    *,
    tenant_id: str,
    kb_id: str,
    limit: int = DEFAULT_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
    anchor_entity: str = "",
    extra_edges: Optional[list[dict]] = None,
    extra_types: Optional[list[dict]] = None,
) -> dict:
    """执行一条规则；超时/异常返回未决，不当作空结果。"""
    cypher, params = compile_rule(
        rule, view, tenant_id=tenant_id, kb_id=kb_id, limit=limit,
        anchor_entity=anchor_entity,
        extra_edges=extra_edges, extra_types=extra_types,
    )
    try:
        driver = get_neo4j_driver()
    except Exception as exc:  # noqa: BLE001
        return {
            "rule_key": rule.get("rule_key"), "status": "unresolved",
            "reason": f"neo4j_unavailable:{str(exc)[:160]}", "derived": [],
        }
    try:
        async def _run():
            async with driver.session() as session:
                result = await session.run(cypher, params)
                return [dict(record) async for record in result]

        rows = await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "rule_key": rule.get("rule_key"), "status": "unresolved",
            "reason": "timeout", "derived": [],
        }
    except Exception as exc:  # noqa: BLE001 — 单条规则失败不拖垮其它规则
        return {
            "rule_key": rule.get("rule_key"), "status": "unresolved",
            "reason": str(exc)[:200], "derived": [],
        }

    derived: list[dict] = []
    for row in rows:
        premise_rows = row.get("premise_rows") or []
        docs: list[str] = []
        for premise in premise_rows:
            docs.extend(str(d) for d in (premise.get("docs") or []))
        derived.append({
            "rule_key": rule.get("rule_key"),
            "s": row.get("derived_s"),
            "p": row.get("derived_p"),
            "o": row.get("derived_o"),
            "s_type": row.get("derived_s_type"),
            "o_type": row.get("derived_o_type"),
            "already_asserted": bool(row.get("already_asserted")),
            "premise": {
                "edges": premise_rows,
                "doc_ids": sorted(set(docs)),
            },
        })
    return {
        "rule_key": rule.get("rule_key"), "status": "done",
        "derived": derived, "count": len(derived),
    }


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_TIMEOUT",
    "compile_rule",
    "execute_rule",
]
