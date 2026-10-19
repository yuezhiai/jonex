#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""检索链路埋点写入器（数据运营二期）。

把一次检索的「请求 / 步骤 / 命中 / 采用」四类事件落进事件表。

三条硬约束（改动本文件前先读）：

1. **独立 session**：检索主流程与埋点各自 `async with get_db_session()`，
   互不共事务 —— 事件插入失败不得回滚检索，反之亦然。
2. **空 request_id 一律不写**（`logger.warning` 后 return）：绝不落 `request_id=""`
   的脏行，那会让所有按 request_id 关联的聚合（步骤/命中/采用）失去锚点。
3. **吞异常但留日志**：埋点失败不影响主流程，但不能静默。

⚠️ 本文件内的 `_clip` / `_clip_identity` / `hash_query` / `ontology_object_id`
与 `scripts/backfill_search_events.py` 里的同名实现是**两份本地副本**，口径必须一致
（回填脚本受「不 import capabilities.*」硬约束，不能直接复用本模块）。
**改这几个函数时务必同时改两处**（`tests/unit/test_backfill_search_events.py`
有等值断言守着）。

⚠️ **唯一例外是 `_trim_detail`**：两边**故意不同**。本模块按下面的 stage 白名单
硬过滤（在线链路的输入形状是确定的）；回填脚本不按白名单，只做「丢大字段 + 裁数组
+ 8KB 封顶」—— 历史行的 detail 形状逐年演化，白名单会连带丢掉对账要用的计数键
（`total_hits` / `kb_ok` / `raw_recall_count`）。**不要**为了「两边一致」把回填
改成白名单版（会静默丢历史计数），也不要为了「多留点」把本模块改成白名单放开
（在线链路是热路径，白名单是它控制体积的主要手段）。

⚠️ 处理任务（parse/extract/compile）的埋点**不在本模块**，在同层仓储
`repository/processing_task_repository.py` —— 那 5 个落点跑在调用方已有的事务里，
必须复用调用方的 session，不能像本模块这样另开。
"""

import hashlib
import json
import logging

from jonex_core.common import get_db_session

from ..dtos.reasoning import (
    STAGE_ARBITRATION,
    STAGE_CONTEXT_RETRIEVE,
    STAGE_ONTOLOGY_MATCH,
    STAGE_ROUTE_DECISION,
)
from ..repository import SearchEventRepository

logger = logging.getLogger(__name__)

# ── 列宽（与 004_knowledge_base.sql 的事件表逐列对齐，超长必须截断）──
# 契约由 tests/unit/test_search_event_schema_contract.py 守卫：任何「写入值 / 裁剪上限 >
# 列宽」都会让 INSERT 失败并回滚整批埋点（2026-10-13 被引用资产数不更新的直接原因）。
_TITLE_MAX = 128          # search_step_event.title VARCHAR(128)
_SUMMARY_MAX = 2000       # summary TEXT，但单请求可含多步，仍设上限防爆
_STAGE_MAX = 32           # stage VARCHAR(32)，现有常量最长 17
_ENTITY_TYPE_MAX = 64     # entity_type VARCHAR(64)
_CANONICAL_NAME_MAX = 256  # canonical_name VARCHAR(256)
_KB_ID_MAX = 128          # kb_id VARCHAR(128)
_TARGET_ID_MAX = 128      # target_id VARCHAR(128)
_REQUEST_ID_MAX = 64      # request_id VARCHAR(64)
_QUERY_HASH_MAX = 64
# target_type 只能取 TARGET_DOCUMENT(8) / TARGET_ONTOLOGY_INSTANCE(17)，
# 但**必须显式裁剪**：裸传时一旦出现更长的取值，整批埋点会被 PG 打回（见文件头 _TARGET_TYPE_MAX 事故说明）。
_TARGET_TYPE_MAX = 32     # search_hit_event/search_adoption_event.target_type VARCHAR(32)
# ⚠️ service_id / entry_service_id / domain_space_id 三列都是 VARCHAR(64)，
# 不能复用 _KB_ID_MAX(128) —— 两者都是 128 时看不出问题，但列宽只有 64。
_SERVICE_ID_MAX = 64      # search_step_event.service_id、search_request_event.entry_service_id / domain_space_id

# detail 序列化后的单条上限（通用兜底）
_DETAIL_MAX_BYTES = 8 * 1024
# 数组型字段最多保留条数
_DETAIL_ARRAY_MAX = 20

# ── detail 丢键白名单 ──
# 只保留列出的键；未列出的 stage 走「只留标量 + 一层 dict」的通用规则。
_DETAIL_KEEP_KEYS: dict[str, tuple[str, ...]] = {
    STAGE_CONTEXT_RETRIEVE: ("raw_recall_count", "recall_count", "kb_ok", "kb_failed"),
    STAGE_ONTOLOGY_MATCH: ("total_hits", "kb_count", "hits"),
    STAGE_ROUTE_DECISION: ("route", "source", "vscore", "ft_score"),
    STAGE_ARBITRATION: ("band", "support", "struct_align"),
}
# hits 项保留的键（本体命中候选）
_HIT_ITEM_KEEP = ("name", "type", "kb_id", "score")
# recalls 项保留的键（**必须丢 text**，它是最大的字段）
_RECALL_ITEM_KEEP = ("doc_id", "chunk_index", "kb_id")

# ── 命中/采用事件的 target_type ──
TARGET_ONTOLOGY_INSTANCE = "ontology_instance"
TARGET_DOCUMENT = "document"


def hash_query(query: str | None) -> str | None:
    """query_hash：`sha256(strip + lower)`[:64]，供 top_questions 归并同一句话。

    ⚠️ 与 `models/search_history.build_query_hash` **不是同一个哈希**：后者用
    `normalize_query` 只折叠空白、**不转小写**。两者对大小写不同的同一句话给出
    不同结果。事件表语境一律以本函数为准（回填也必须按本口径现算，不能抄
    `history.query_hash`，否则切源前后会把同一句话裂成两行）。
    """
    if not query:
        return None
    return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:_QUERY_HASH_MAX]


def ontology_object_id(tenant_id: str, kb_id: str, entity_type: str, canonical_name: str) -> str:
    """本体实例的展示用 id：`sha256(tenant|kb|entity_type|canonical_name)`[:32]。

    ⚠️ **只作为返回 DTO 的展示 id，绝不用作聚合的 join 键**：
    `canonical_name` 可以被改名（`UpdateOntologyInstanceRequest.updates` 支持改名，
    对账服务也会做别名→正式名对齐），改名后历史行的 target_id 指向一个不再存在的
    键 → 「近 N 天命中且周期末仍有效」会把该实体判为失效，`active_entities` 系统性
    少算、`top_used_entities` 的计数会裂成「旧名一批 + 新名一批」。
    聚合一律按 `(kb_id, entity_type, canonical_name)` 三元组 group by，
    target_id 只在返回 DTO 时现算。
    """
    raw = f"{tenant_id}|{kb_id}|{entity_type}|{canonical_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ── 截断工具 ────────────────────────────────────────────────


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


def _clip_identity(value: str | None, limit: int) -> str | None:
    """截断**标识类**字段：截断后加短哈希后缀，避免两个不同实体截成同一个值。

    ⚠️ 表格行实体的 canonical_name 含主键（`ontology_graph_repository.py:145-146`），
    真实长度可超 256，不截断会触发 PG `StringDataRightTruncation` 导致**整批**命中
    事件插入失败；而朴素截断会让两个不同实体撞同一个 (entity_type, canonical_name)，
    统计时被并成一条。后缀用**未截断原文**的 sha，保证可区分。
    """
    if value is None:
        return None
    if len(value) <= limit:
        return value
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    # limit 含后缀长度：`#` + 8 位 = 9
    head = max(0, limit - 9)
    return f"{value[:head]}#{suffix}"


def _scalar_only(detail: dict) -> dict:
    """通用兜底：只留标量 + 一层 dict/list（丢弃深层嵌套与超大字段）。"""
    out: dict = {}
    for key, value in (detail or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = {
                k: v for k, v in value.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
        elif isinstance(value, list):
            kept = [
                item for item in value[:_DETAIL_ARRAY_MAX]
                if isinstance(item, (str, int, float, bool)) or item is None
            ]
            if kept:
                out[key] = kept
    return out


def _trim_item(item, keep: tuple[str, ...]) -> dict:
    if not isinstance(item, dict):
        return {"value": item}
    return {k: item.get(k) for k in keep if k in item}


def _trim_detail(detail: dict | None, stage: str) -> dict | None:
    """按 stage 丢键白名单裁剪 detail，并做 8KB 兜底。

    不裁的后果：`context_retrieve` 的 `recalls` 每条含 `text`
    （上限 `ONTOLOGY_RAG_RECALL_TEXT_MAX`，50000 字符实测），单请求可达数十 KB，
    事件表会被这一列撑爆，且聚合时白白反序列化。
    """
    if not detail or not isinstance(detail, dict):
        return None

    keep = _DETAIL_KEEP_KEYS.get(stage)
    if keep is not None:
        trimmed = {k: detail.get(k) for k in keep if k in detail}
    else:
        trimmed = _scalar_only(detail)

    # 数组裁剪：hits / recalls 逐项丢大字段
    if isinstance(trimmed.get("hits"), list):
        trimmed["hits"] = [
            _trim_item(i, _HIT_ITEM_KEEP) for i in trimmed["hits"][:_DETAIL_ARRAY_MAX]
        ]
    if isinstance(trimmed.get("recalls"), list):
        trimmed["recalls"] = [
            _trim_item(i, _RECALL_ITEM_KEEP) for i in trimmed["recalls"][:_DETAIL_ARRAY_MAX]
        ]

    # 8KB 兜底：先降级为通用规则，仍超则只留键名清单
    try:
        size = len(json.dumps(trimmed, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return {"truncated": True, "keys": list(trimmed.keys())}
    if size <= _DETAIL_MAX_BYTES:
        return trimmed or None
    fallback = _scalar_only(trimmed)
    try:
        size = len(json.dumps(fallback, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return {"truncated": True, "keys": list(trimmed.keys())}
    if size <= _DETAIL_MAX_BYTES:
        return fallback or None
    return {"truncated": True, "keys": list(trimmed.keys())}


def _dedup_events(events: list[dict], key_fields: tuple[str, ...]) -> list[dict]:
    """落库前按给定键去重。

    三张检索事件表（step/hit/adoption）**都没有唯一约束**（只有普通索引），
    仓储 docstring 里的「去重」只是约定 —— 必须在写入前显式做。
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for event in events:
        key = tuple(event.get(f) for f in key_fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


# ── 事件抽取 ────────────────────────────────────────────────


def _iter_steps(result: dict, collector) -> list[dict]:
    """步骤来源：**优先 `result["reasoning"]`**，退回 `collector._steps`。

    优先 result 是因为 `strict` 路径下 `result["reasoning"]["steps"]` 是
    `strict_steps + 内层全链`的合并结果，而 collector 里只有外层几条；
    读 result 才能拿到完整步骤。
    """
    reasoning = result.get("reasoning") or {}
    steps = reasoning.get("steps")
    if isinstance(steps, list) and steps:
        return steps
    return list(getattr(collector, "_steps", None) or [])


def _extract_hits(result: dict, steps: list[dict], tenant_id: str) -> list[dict]:
    """命中事件。

    ⚠️ **命中与采用的数据源必须分开**：`result["references"]` 的语义是「最终采用」
    而非「召回」（本体路径由 `_ontology_refs` 从 source_chunks 构建、RAG 路径已过
    质量过滤/配额/rerank/聚合、裁决后取二者之一）。若拿它写命中事件，
    命中数恒等于采用数、命中率恒 100%。

    | 事件 | 数据源 | target_type |
    | --- | --- | --- |
    | 命中（本体） | `result["ontology_instances"]`，要求 name/type/kb_id 三者齐备 | ontology_instance |
    | 命中（文档） | steps 的 `context_retrieve.detail["recalls"]` | document |
    """
    events: list[dict] = []

    # ① 本体命中：三者齐备才算 —— 这道过滤同时挡掉 deep 在子查询里塞的
    #    只有 `name` 没有 kb_id/type 的垃圾行（它的 target_id 会是随机垃圾，
    #    还会污染 active_entities）。
    for inst in result.get("ontology_instances") or []:
        if not isinstance(inst, dict):
            continue
        name = inst.get("name")
        entity_type = inst.get("type")
        kb_id = inst.get("kb_id")
        if not name or not entity_type or not kb_id:
            continue
        events.append({
            "target_type": TARGET_ONTOLOGY_INSTANCE,
            # ⚠️ object_id 用**未截断原文**计算：截断只用于展示列
            "target_id": ontology_object_id(
                tenant_id, str(kb_id), str(entity_type), str(name)
            ),
            "entity_type": _clip(entity_type, _ENTITY_TYPE_MAX),
            "canonical_name": _clip_identity(str(name), _CANONICAL_NAME_MAX),
            "kb_id": _clip(kb_id, _KB_ID_MAX),
        })

    # ② 文档命中：来自 context_retrieve 步骤的 recalls（含 doc_id / kb_id）。
    #    受 ONTOLOGY_RAG_RECALL_DETAIL_ENABLED 与 ONTOLOGY_RAG_RECALL_MAX_ITEMS
    #    上限约束 → 文档命中数**按 TOP-N 计**（写进契约文档）。
    for step in steps:
        if not isinstance(step, dict) or step.get("stage") != STAGE_CONTEXT_RETRIEVE:
            continue
        detail = step.get("detail") or {}
        for recall in detail.get("recalls") or []:
            if not isinstance(recall, dict):
                continue
            doc_id = recall.get("doc_id")
            if not doc_id:
                continue
            events.append({
                "target_type": TARGET_DOCUMENT,
                "target_id": _clip(str(doc_id), _TARGET_ID_MAX),
                "entity_type": None,
                "canonical_name": _clip(recall.get("file_name"), _CANONICAL_NAME_MAX),
                "kb_id": _clip(recall.get("kb_id"), _KB_ID_MAX),
            })
    return events


def _get(ref, key: str):
    """references 元素可能是 dict 也可能是对象，两种都取。"""
    if isinstance(ref, dict):
        return ref.get(key)
    return getattr(ref, key, None)


def _extract_adoptions(result: dict) -> list[dict]:
    """采用事件：`result["references"]` → target_type='document'。

    ⚠️ 不要从 `references` 生成 `ontology_instance` 的采用事件 —— references 里
    没有实体信息（只有 doc_id / kb_id / 位置锚点）。
    """
    events: list[dict] = []
    for ref in result.get("references") or []:
        doc_id = _get(ref, "doc_id")
        if not doc_id:
            continue
        events.append({
            "target_type": TARGET_DOCUMENT,
            "target_id": _clip(str(doc_id), _TARGET_ID_MAX),
            "entity_type": None,
            "canonical_name": _clip(_get(ref, "file_name"), _CANONICAL_NAME_MAX),
            "kb_id": _clip(_get(ref, "kb_id"), _KB_ID_MAX),
        })
    return events


# ── 分事务落库 ──────────────────────────────────────────────
# 三个 `_write_*_events` 各自 `async with get_db_session()`，互不共事务：
# 一类里的一条坏行只回滚那一类（2026-10-13 事故：命中事件超长把采用事件一起冲掉）。
# 空列表直接返回 True（没内容可写 ≠ 写失败），避免无谓开连接。


async def _write_step_events(
    *,
    tenant_id: str,
    request_id: str,
    kb_ids: list[str],
    service_id: str | None,
    events: list[dict],
) -> bool:
    if not events:
        return True
    try:
        async with get_db_session() as session:
            repo = SearchEventRepository(session)
            for event in events:
                await repo.record_step_event(
                    tenant_id,
                    request_id=request_id[:_REQUEST_ID_MAX],
                    stage=event["stage"],
                    title=event["title"],
                    status=event["status"],
                    summary=event["summary"],
                    detail=event["detail"],
                    duration_ms=event["duration_ms"],
                    knowledge_base_ids=kb_ids,
                    service_id=_clip(service_id, _SERVICE_ID_MAX),
                )
        return True
    except Exception:  # noqa: BLE001 — 埋点失败不得影响检索
        logger.warning(
            "[jonex] 检索步骤埋点写入失败（不影响检索；命中/采用仍会写入）：tenant=%s request_id=%s",
            tenant_id, request_id, exc_info=True,
        )
        return False


async def _write_hit_events(*, tenant_id: str, request_id: str, events: list[dict]) -> bool:
    if not events:
        return True
    try:
        async with get_db_session() as session:
            repo = SearchEventRepository(session)
            for event in events:
                await repo.record_hit_event(
                    tenant_id,
                    request_id=request_id[:_REQUEST_ID_MAX],
                    # 必须裁：'ontology_instance' 已 17 字符，靠 _TARGET_TYPE_MAX 兜住未来更长的取值
                    target_type=_clip(event["target_type"], _TARGET_TYPE_MAX),
                    target_id=event["target_id"],
                    entity_type=event["entity_type"],
                    canonical_name=event["canonical_name"],
                    kb_id=event["kb_id"],
                )
        return True
    except Exception:  # noqa: BLE001 — 埋点失败不得影响检索
        logger.warning(
            "[jonex] 检索命中埋点写入失败（不影响检索；采用事件仍会写入）：tenant=%s request_id=%s",
            tenant_id, request_id, exc_info=True,
        )
        return False


async def _write_adoption_events(
    *,
    tenant_id: str,
    request_id: str,
    answer_id: str | None,
    events: list[dict],
) -> bool:
    """采用事件：**独立事务，且优先级最高的那类**（被引用资产 KPI 唯一数据源）。"""
    if not events:
        return True
    try:
        async with get_db_session() as session:
            repo = SearchEventRepository(session)
            for event in events:
                await repo.record_adoption_event(
                    tenant_id,
                    answer_id=(answer_id or request_id)[:_REQUEST_ID_MAX],
                    request_id=request_id[:_REQUEST_ID_MAX],
                    target_type=_clip(event["target_type"], _TARGET_TYPE_MAX),
                    target_id=event["target_id"],
                    entity_type=event["entity_type"],
                    canonical_name=event["canonical_name"],
                    kb_id=event["kb_id"],
                )
        return True
    except Exception:  # noqa: BLE001 — 埋点失败不得影响检索
        logger.warning(
            "[jonex] 检索采用埋点写入失败（不影响检索；「被引用数据资产数」不会更新）：tenant=%s request_id=%s",
            tenant_id, request_id, exc_info=True,
        )
        return False


# ── 对外入口 ────────────────────────────────────────────────


async def write_request_event(
    *,
    tenant_id: str,
    request_id: str,
    user_id: str | None = None,
    query: str | None = None,
    kb_ids: list[str] | None = None,
    entry_service_id: str | None = None,
    domain_space_id: str | None = None,
) -> bool:
    """落一行检索请求事件（入口埋点）。

    写在 handler **之前**（capability.execute），因此后面三类异常
    （TenantIsolationError / JonexException / 兜底 Exception）都能被记成
    「已接收但失败」—— 请求事件有、步骤事件没有。

    口径：权限拒绝（403）的请求**不计数** —— 埋点在 `_filter_search_kbs` 之后，
    那是 403 而非检索行为。
    """
    if not request_id:
        logger.warning("[jonex] 检索请求埋点缺 request_id，已跳过（tenant=%r）", tenant_id)
        return False
    if not tenant_id:
        logger.warning("[jonex] 检索请求埋点缺 tenant_id，已跳过")
        return False

    try:
        async with get_db_session() as session:
            repo = SearchEventRepository(session)
            await repo.record_request_event(
                tenant_id,
                user_id=user_id,
                request_id=request_id[:_REQUEST_ID_MAX],
                entry_service_id=_clip(entry_service_id, _SERVICE_ID_MAX),
                domain_space_id=_clip(domain_space_id, _SERVICE_ID_MAX),
                query=query,
                query_hash=hash_query(query),
                knowledge_base_ids=list(kb_ids or []),
            )
        return True
    except Exception:  # noqa: BLE001 — 埋点失败不得影响检索
        logger.warning(
            "[jonex] 检索请求埋点写入失败（不影响检索）：tenant=%s request_id=%s",
            tenant_id, request_id, exc_info=True,
        )
        return False


async def write_trace_events(
    *,
    tenant_id: str,
    request_id: str,
    answer_id: str | None = None,
    result: dict | None = None,
    collector=None,
    kb_ids: list[str] | None = None,
    service_id: str | None = None,
) -> bool:
    """一次调用写「步骤 + 命中 + 采用」三类事件。

    `answer_id` = `knowledge_search_history.id`，是事件与检索历史之间**唯一**的
    关联键（history 表没有 request_id / trace_id 列）。为 None 时跳过采用事件
    的 answer_id 关联（仍写步骤与命中）。

    ⚠️ **三类事件各写各的事务，不合并**（顺序：步骤 → 命中 → 采用）。合并成一个
    事务时，任一类里的一条坏行会连坐回滚另外两类 —— 2026-10-13 的事故正是如此：
    本体命中事件的 `target_type='ontology_instance'` 超出当时的列宽，INSERT 报
    `value too long`，把同一事务里的**采用事件**一起冲掉，而采用事件是「被引用数据
    资产数」「高频引用资产 Top10」唯一的数据源 → 两个 KPI 长期不动，检索照常返回。
    拆分后单类失败只丢那一类，并在各自日志里留 `stage=步骤/命中/采用` 便于定位。

    返回：三类都写成功（或本就没内容可写）为 True；任一类失败为 False（调用方不关心，
    只作观测）。
    调用点：7 处 `if req.save_history:` 块内、`save_history` 之后、`return` 之前。
    """
    if not request_id:
        logger.warning("[jonex] 检索轨迹埋点缺 request_id，已跳过（tenant=%r）", tenant_id)
        return False
    if not tenant_id:
        logger.warning("[jonex] 检索轨迹埋点缺 tenant_id，已跳过")
        return False

    result = result or {}
    kb_ids = list(kb_ids or [])
    try:
        steps = _iter_steps(result, collector)
        step_events = [
            {
                "stage": _clip(step.get("stage"), _STAGE_MAX) or "unknown",
                "title": _clip(step.get("title"), _TITLE_MAX),
                "status": _clip(step.get("status"), 16) or "done",
                "summary": _clip(step.get("summary"), _SUMMARY_MAX),
                "detail": _trim_detail(step.get("detail"), step.get("stage")),
                "duration_ms": step.get("duration_ms"),
            }
            for step in steps
            if isinstance(step, dict)
        ]
        # 同一步骤可能在 collector 与 result 里各出现一次（strict 合并场景），
        # 按 (stage, title) 去重，避免同一逻辑步骤落两行。
        step_events = _dedup_events(step_events, ("stage", "title"))

        hit_events = _dedup_events(
            _extract_hits(result, steps, tenant_id), ("target_type", "target_id")
        )
        adoption_events = _dedup_events(_extract_adoptions(result), ("target_type", "target_id"))

        # 三类事件各写各的事务：单类失败只丢那一类，不连坐（见本函数 docstring 的事故说明）。
        steps_ok = await _write_step_events(
            tenant_id=tenant_id,
            request_id=request_id,
            kb_ids=kb_ids,
            service_id=service_id,
            events=step_events,
        )
        hits_ok = await _write_hit_events(
            tenant_id=tenant_id, request_id=request_id, events=hit_events
        )
        adoptions_ok = await _write_adoption_events(
            tenant_id=tenant_id,
            request_id=request_id,
            answer_id=answer_id,
            events=adoption_events,
        )
        return steps_ok and hits_ok and adoptions_ok
    except Exception:  # noqa: BLE001 — 埋点失败不得影响检索
        logger.warning(
            "[jonex] 检索轨迹埋点抽取失败（不影响检索）：tenant=%s request_id=%s",
            tenant_id, request_id, exc_info=True,
        )
        return False


__all__ = [
    "TARGET_DOCUMENT",
    "TARGET_ONTOLOGY_INSTANCE",
    "hash_query",
    "ontology_object_id",
    "write_request_event",
    "write_trace_events",
]
