"""本体问答的「数据来源/下游」解释块（A 用法）。

链路：命中实体 → `concept_sources`（概念在各源的落点）→ 该源编译 schema 的
`entity_types[].source_object_id` → 物理表 → `walk` 上下各 N 跳。

**fail-open**：任何一步失败 ⇒ 返回 None（响应里不出现该键），绝不让检索失败。
`unknown` 是 walk 的语义（读不到 ≠ 没有），UI 必须显式渲染。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def resolve_entity_tables(
    instance: dict, schema_by_source: dict[str, dict],
) -> list[tuple[str, str]]:
    """命中实体 → [(data_source_id, 物理表)]；没有落点就不返回。

    `concept_sources` 可能缺失（未标注的实例）或带脏条目；一律跳过而不是抛。
    """
    out: list[tuple[str, str]] = []
    for src in (instance or {}).get("concept_sources") or []:
        if not isinstance(src, dict):
            continue
        ds_id = str(src.get("data_source_id") or "").strip()
        ent_name = str(src.get("entity_name") or "").strip()
        if not ds_id or not ent_name:
            continue
        schema = schema_by_source.get(ds_id) or {}
        if not isinstance(schema, dict):
            continue
        for e in schema.get("entity_types") or []:
            if not isinstance(e, dict):
                continue
            if str(e.get("name") or "") != ent_name:
                continue
            table = str(e.get("source_object_id") or "").strip()
            if table:
                out.append((ds_id, table))
                break
    return out


def should_attach_lineage(source: str) -> bool:
    """A 块只在 ontology 答案上出现：data/clarify/rag/none 的其他路径不带。"""
    return str(source or "").strip().lower() == "ontology"


def attach_data_lineage(result: dict, block: dict | None) -> None:
    """把已算好的解释块挂进响应；`None`（无内容 / fail-open）不写键。

    两个装配点（单个本体查询 / mix）共用这一处，钉死「旧响应不出现
    `data_lineage: null`」的形状契约。
    """
    if block:
        result["data_lineage"] = block


def _dedupe_endpoints(rows: list[dict]) -> list[dict]:
    """同一源列流向多个目标列时只展示一次（UI 是「来源列」清单，不是边清单）。

    真机（2026-10-21）：`plant_a_turbine_power_daily.power_gen_kwh` 同时流向
    `ton_garbage_power` / `is_qualified` / `power_gen_kwh` 三个目标列，块里就会
    重复出现三次 —— 用户看到的是一串相同的来源，噪音大于信息。
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row.get("endpoint") or ""), str(row.get("column") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


async def build_data_lineage_block(tenant_id: str, kb_id: str, instances: list[dict], *,
                                   get_schema, walk_fn, max_items: int = 5,
                                   max_hops: int = 1) -> dict | None:
    """组装解释块；空结果/fail-open 都返回 None。"""
    try:
        # 候选 = `concept_sources` 非空的命中（未标注的命中不占名额）。
        candidates = [
            i for i in (instances or [])
            if isinstance(i, dict) and isinstance(i.get("concept_sources"), list)
            and i["concept_sources"]
        ]
        if not candidates:
            return None
        schema_by_source: dict[str, dict] = {}

        async def _ensure_schema(ds_id: str) -> None:
            if ds_id not in schema_by_source:
                schema_by_source[ds_id] = await get_schema(
                    tenant_id, kb_id, ds_id) or {}

        # **按「能否解析到物理表」再截断**：只按 concept_sources 非空截断时，
        # 同 KB 内排在前面但落点解析不到的命中会吃掉全部 max_items 名额，
        # 真正可解释的实体一条都进不来（审查 Low：窄口径饥饿）。
        # 逐候选懒加载所需 schema，凑够 max_items 张可解析物理表即停 ——
        # 不为了过滤把全部候选的 schema 都拉一遍。
        # `max_items` 按**唯一物理表**计数，不按命中实例计数：同一张汇总表会被多个
        # 文档实例命中（2026-10-21 真机：5 个 m12_plant_daily 实例 → 同一张
        # `ods.dwd_m12_plant_daily`），按实例截断会把同一张表重复渲染 5 遍。
        selected: list[tuple[dict, list[tuple[str, str]]]] = []
        seen_tables: set[tuple[str, str]] = set()
        for inst in candidates:
            for src in (inst.get("concept_sources") or []):
                if isinstance(src, dict):
                    ds_id = str(src.get("data_source_id") or "").strip()
                    if ds_id:
                        await _ensure_schema(ds_id)
            tables: list[tuple[str, str]] = []
            for pair in resolve_entity_tables(inst, schema_by_source):
                if pair not in seen_tables and pair not in tables:
                    tables.append(pair)
            if not tables:
                continue
            for pair in tables:
                seen_tables.add(pair)
            selected.append((inst, tables))
            if len(seen_tables) >= max_items:
                break
        if not selected:
            return None
        items: list[dict] = []
        truncated = False
        for inst, tables in selected:
            for ds_id, table in tables:
                if max_hops > 1:
                    # 多跳时必须按方向分开走：一次 `both` 返回的子图里，
                    # 「哪些边属于上游、哪些属于下游」无法从端点判定 ——
                    # 原先按 `to_table == table` / `from_table == table` 过滤，
                    # 会把第二跳及以后的边全部丢掉（配置成 2 跳却看起来仍是 1 跳）。
                    up = await walk_fn(tenant_id, kb_id, ds_id, table,
                                       direction="upstream", hops=max_hops)
                    down = await walk_fn(tenant_id, kb_id, ds_id, table,
                                         direction="downstream", hops=max_hops)
                    incoming = [e for e in up.get("edges") or [] if isinstance(e, dict)]
                    outgoing = [e for e in down.get("edges") or [] if isinstance(e, dict)]
                    unknown = bool(up.get("unknown")) or bool(down.get("unknown"))
                    truncated = (truncated or bool(up.get("truncated"))
                                 or bool(down.get("truncated")))
                else:
                    res = await walk_fn(tenant_id, kb_id, ds_id, table,
                                        direction="both", hops=max_hops)
                    incoming = [e for e in res.get("edges") or []
                                if isinstance(e, dict) and str(e.get("to_table")) == table]
                    outgoing = [e for e in res.get("edges") or []
                                if isinstance(e, dict) and str(e.get("from_table")) == table]
                    unknown = bool(res.get("unknown"))
                    # `truncated` 是 walk 的「边数触顶」语义，不能拿 `unknown` 冒充
                    #（读不到 ≠ 结果被截断，两件事 UI 要分开说）。
                    truncated = truncated or bool(res.get("truncated"))
                items.append({
                    "entity_type": str(inst.get("type") or ""),
                    "data_source_id": ds_id,
                    "table": table,
                    "upstream": _dedupe_endpoints([
                        {"endpoint": e.get("from_table"), "column": e.get("from_column"),
                         "origin": e.get("origin"), "confidence": e.get("confidence")}
                        for e in incoming]),
                    "downstream": _dedupe_endpoints([
                        {"endpoint": e.get("to_table"), "column": e.get("to_column"),
                         "origin": e.get("origin"), "confidence": e.get("confidence")}
                        for e in outgoing]),
                    "unknown": unknown,
                })
        if not items:
            return None
        return {"items": items, "truncated": truncated}
    except Exception as exc:  # noqa: BLE001 —— 解释块是增能项，失败即缺席
        logger.warning("data_lineage 解释块构建失败，本次省略：%s", exc)
        return None
