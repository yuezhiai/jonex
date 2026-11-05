#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""② 读路径的 **active 版本解析** + **漂移标注** + 进程内短 TTL 缓存（P1 版本化读取）。

用户 2026-10-25 的选型：**语义 = 版本戳 + 漂移**（回答仍用 schema **行内容**，
不改画布/生成行为）。本模块是 ② 里**唯一**的 schema 行读口：

- `resolve_active_version`：读 `knowledge_base.semantic_versions` 的 active 版本
  （`uq_evo_version_active` 覆盖的唯一键，单次索引查找）；
- `load_compiled_row` / `load_abstract_row`：读行；compiled 与 abstract 都算 `drift`；
  abstract 走共享序列化器 `abstract_schema_canonical`（剥读时派生键），旧版本 hash
  口径差异由内容重算吸收；
- `tables_from_compiled_row`：治理用的 `{物理表: {列}}` 投影（与
  `governance.common.schema_tables` 同口径），让治理视图与回答共用同一份行；
- `start_drift_collection` / `collected_drift`：每个提问一个 `ContextVar` 账本，
  `observe.build_scene` 在末尾把它装进 scene（**只进观测，不进响应**）。

compiled 漂移的 hash **必须与版本行同口径**：`schema_io.snapshot_from_row` 只装内容
（`schema_version` / 时间戳 / `evidence` 都不进快照），所以改这些列**不算**漂移。
拿整行算 hash 会恒 drift —— 这是本模块最容易被「顺手优化」掉的一条。

缓存（R2）：模块级 `dict` + `threading.Lock`；键
`(tenant, kb, scope, ds_or_empty, version_id_or_empty, schema_version, generation)`；
TTL = `config.NL2SQL_SCHEMA_CACHE_TTL_SECONDS`（默认 60，`0` 关闭）。
- **命中即零会话**：`(tenant, kb, scope, ds)` 指到当前键，TTL 内不重复读库；
- **写入立刻换键**（关键）：`schema_generation` 在**每次成功的 schema 行写入**后 +1
  （两个仓储的 upsert），所以本进程的写后读立刻重读 —— 演化基准「父臂填缓存 → 写库 →
  候选臂」不会读到写前的行（只靠 `schema_version` 不够：画布保存/回滚/采纳的 PUT
  不一定推进 active 版本；只靠 TTL 更不够，那段时间里候选 == 父）；
- **代号在未命中时采样**：读行期间若有并发 upsert 推进代号，这次读到的 payload
  归档在**旧代**下 ⇒ 下一次查找立刻作废并重读（不会把写前的行冒充成新代）；
- `schema_version` 也进键：同一代内的行级版本变化仍会换键；
- TTL 只服务「没有本进程写入」的稳态流量（以及跨 worker 的他进程写入，最多滞后 TTL）；
- 同一链只留当前键（旧键在换键时摘掉）、过期项在读取时惰性删除、
  条目超 `_MAX_CACHE_ENTRIES` 时先清过期再丢最旧 ⇒ 不无界增长。

并发（R1/R3）：`Nl2SqlService` 无状态、可并发调用 ⇒ 漂移账本必须是
`contextvars.ContextVar`（每个请求一份），**绝不能**放实例属性。

失败姿态：版本解析是**观测面的增能项** —— 版本表读不了（未迁移 / DB 抖动）
一律按「无 active 版本」处理（`drift=False`），绝不拖垮提问（旧库行为逐字节不变）。
行读取本身不吞异常（与改动前的 `_load_concepts` / `_entity_columns` 同姿势）。
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time

from sqlalchemy import text

from jonex_core.common.config import config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.schema_generation import schema_generation

from ...repository.ontology_abstract_schema_repository import (
    OntologyAbstractSchemaRepository,
)
from ...repository.ontology_schema_repository import OntologySchemaRepository
from ..abstract_schema_canonical import canonical_abstract_snapshot_hash
from ..evolution.schema_io import snapshot_from_row
from ..evolution.version_store import canonical_snapshot_hash

logger = logging.getLogger(__name__)

#: active 版本查询：**一次索引查找**，只取 id 与 snapshot_hash（不搬 snapshot JSONB）。
#: `coalesce(data_source_id, '') = :ds` 与 `uq_evo_version_active` 的索引表达式同形
#: （abstract 传 ""，compiled 传 ds id）—— 别改成 `IS NULL` / `= :ds` 两条分支。
_ACTIVE_VERSION_SQL = (
    "SELECT sv.id, sv.snapshot_hash "
    "FROM knowledge_base.semantic_versions AS sv "
    "WHERE sv.tenant_id = :tenant "
    "AND sv.knowledge_base_id = :kb "
    "AND sv.scope = :scope "
    "AND sv.status = 'active' "
    "AND coalesce(sv.data_source_id, '') = :ds "
    "LIMIT 1"
)

#: abstract 漂移要拿 active 版本的**快照内容**做比较（compiled 的轻查询不搬 JSONB）。
_ACTIVE_SNAPSHOT_SQL = (
    "SELECT sv.id, sv.snapshot_hash, sv.snapshot "
    "FROM knowledge_base.semantic_versions AS sv "
    "WHERE sv.tenant_id = :tenant "
    "AND sv.knowledge_base_id = :kb "
    "AND sv.scope = :scope "
    "AND sv.status = 'active' "
    "AND coalesce(sv.data_source_id, '') = :ds "
    "LIMIT 1"
)


def _json_value(value):
    """读取侧容错：驱动把 JSONB 返回成字符串时解码（解码失败保留原值）。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value

#: 漂移账本（每个 ask 请求一份）。`{"scopes": set[str], "warned": bool}`。
#: ContextVar 的值是**可变 dict**：同一请求内的协程共享同一份，子任务继承引用。
_drift_state: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "nl2sql_schema_drift", default=None)

#: 缓存：完整键 → `(payload, drift, expires_at)`。`payload` **不含** `drift`
#: （drift 与 payload 同存同取，命中时再拼回返回形状）。
#: 完整键最后一位是 `schema_generation` —— 判断「这一代是否还有效」只读它。
_cache: dict[tuple, tuple[dict, bool, float]] = {}
#: 链指针：`(tenant, kb, scope, ds)` → 当前完整键。命中路径只查它，**不开会话**。
_pointers: dict[tuple[str, str, str, str], tuple] = {}
_cache_lock = threading.Lock()

#: 缓存条目上限（防御性上界）。正常规模 = 活跃链数（同一链只留一个键），
#: 但**过期后不再被访问**的链会以过期项留在表里 —— 超限时先清过期、再丢最旧。
_MAX_CACHE_ENTRIES = 256


# ────────────────────────────────────────────────────────────────────────────
# 漂移账本（ContextVar）
# ────────────────────────────────────────────────────────────────────────────


def start_drift_collection() -> None:
    """每次 ask 入口调用：**覆盖**旧值（同一上下文里顺序复用也不会串上一次的账）。"""
    _drift_state.set({"scopes": set(), "warned": False})


def collected_drift() -> list[str]:
    """本次请求有漂移的 scope 名（排序；没有 = 空列表）。"""
    state = _drift_state.get()
    if not state:
        return []
    return sorted(str(scope) for scope in state["scopes"])


def _mark_drift(scope: str, *, tenant_id: str = "", kb_id: str = "",
                data_source_id: str | None = None, version_id: str = "",
                log: bool = False) -> None:
    """记一笔漂移。`log=True`（本次真检测到）时**每个请求最多一条** warning。

    缓存命中也要调（`log=False`）：漂移结论跟着缓存走，否则 scene 里的
    `schema_drift` 会随「这次是不是命中缓存」时有时无。
    """
    state = _drift_state.get()
    if state is None:                       # 未走 `start_drift_collection`（直接调 loader）
        state = {"scopes": set(), "warned": False}
        _drift_state.set(state)
    state["scopes"].add(str(scope))
    if not (log and not state["warned"]):
        return
    state["warned"] = True
    logger.warning(
        "schema 漂移：active 版本与行内容不一致（仍按行内容回答，仅标注）"
        " tenant=%s kb=%s scope=%s ds=%s version=%s",
        tenant_id, kb_id, scope, data_source_id or "", version_id or "-")


# ────────────────────────────────────────────────────────────────────────────
# 缓存原语
# ────────────────────────────────────────────────────────────────────────────


def _ttl() -> int:
    """TTL 秒（`0` = 关闭缓存）。每次读取现取配置 ⇒ 单测可即时切档。"""
    try:
        return max(0, int(config.NL2SQL_SCHEMA_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        return 60


def clear_cache() -> None:
    """清空进程内缓存（测试/运维用：模块级缓存会跨用例、跨夹具活着）。

    生产正常路径不需要它：写入经 `schema_generation` 自动换键。它是
    「进程内所有链一次作废」的粗粒度逃生口（集成测试重建同一行、
    或排障时怀疑缓存）。
    """
    with _cache_lock:
        _cache.clear()
        _pointers.clear()


def _prune_locked(now: float) -> None:
    """条目超上限时收缩：先清过期项，仍超则按到期时间（≈ 写入时间）丢最旧。

    调用方必须已持 `_cache_lock`。丢掉的键对应的链指针一并摘掉，
    否则指针会指向不存在的条目（下一次读取会自己清，但白留脏指针）。
    """
    if len(_cache) <= _MAX_CACHE_ENTRIES:
        return
    for key, (_payload, _drift, expires) in list(_cache.items()):
        if expires <= now:
            _cache.pop(key, None)
    overflow = len(_cache) - _MAX_CACHE_ENTRIES
    if overflow > 0:
        oldest = sorted(_cache.items(), key=lambda kv: kv[1][2])[:overflow]
        for key, _entry in oldest:
            _cache.pop(key, None)
    for pointer, full in list(_pointers.items()):
        if full not in _cache:
            _pointers.pop(pointer, None)


def _cache_lookup(tenant_id: str, kb_id: str, scope: str,
                  ds_id: str | None) -> tuple[dict, bool] | None:
    """链指针 → 完整键 → 未过期项；过期项**在读取时**删除。

    完整键最后一位是写入时的 `schema_generation`：与当前代号不等 ⇒ 期间发生过
    成功的 schema 写入（本进程），这份是写前的行 —— 立刻作废、下一次读取重读。
    """
    if _ttl() <= 0:
        return None
    pointer = (tenant_id, kb_id, scope, ds_id or "")
    now = time.monotonic()
    generation = schema_generation()
    with _cache_lock:
        full = _pointers.get(pointer)
        if full is None:
            return None
        if full[6] != generation:
            _cache.pop(full, None)
            _pointers.pop(pointer, None)
            return None
        entry = _cache.get(full)
        if entry is None or entry[2] <= now:
            _cache.pop(full, None)
            _pointers.pop(pointer, None)
            return None
        payload, drift, _expires = entry
        return payload, drift


def _cache_store(tenant_id: str, kb_id: str, scope: str, ds_id: str | None,
                 version_id: str, schema_version: int,
                 payload: dict, drift: bool, generation: int) -> None:
    """写缓存。同一链**只留当前键**（旧键摘掉）；超上限时收缩。

    `generation` 是调用方在**未命中那一刻**采样的代号（不是这里现读的）：
    读行期间若并发 upsert 推进了代号，这条 payload 是**写前**的行 —— 必须归档
    到旧代号下，下一次查找按新代号判定时立刻作废并重读。用写入时刻的代号归档
    会把写前的行冒充成新代，绕过全部失效（最长到 TTL）。

    并发未命中时两个协程可能各自读、各自写入：同一代号内两份 payload 是同一份
    行，后写者覆盖前写者无妨；被覆盖者若来自**不同**代号，下一次查找自然作废它。
    不为此加单飞锁（收益不抵锁的复杂度）。
    """
    ttl = _ttl()
    if ttl <= 0:
        return
    pointer = (tenant_id, kb_id, scope, ds_id or "")
    full = (tenant_id, kb_id, scope, ds_id or "", version_id or "",
            int(schema_version or 0), int(generation))
    expires = time.monotonic() + ttl
    with _cache_lock:
        old = _pointers.get(pointer)
        if old is not None and old != full:
            _cache.pop(old, None)
        _cache[full] = (payload, bool(drift), expires)
        _pointers[pointer] = full
        _prune_locked(time.monotonic())


def _with_drift(payload: dict, drift: bool) -> dict:
    """缓存/行 payload → 对外形状（`drift` 放最后；每次现拼，调用方可安全持有）。"""
    return {**payload, "drift": bool(drift)}


# ────────────────────────────────────────────────────────────────────────────
# active 版本
# ────────────────────────────────────────────────────────────────────────────


async def resolve_active_version(tenant_id: str, kb_id: str, scope: str,
                                 ds_id: str | None) -> dict | None:
    """该版本链当前的 active 版本：`{"version_id", "snapshot_hash"}`；没有 → None。

    `scope` 为 `abstract` 时 `ds_id` 传空（KB 级单链）；`compiled` 传数据源 id。
    **fail-open**：版本表读不了只记 warning、按「无版本」处理 —— 版本化读取是
    观测/信任闭环的增能项，旧库（未跑迁移 024）必须与今天完全一致。
    """
    try:
        async with get_db_session() as session:
            row = (await session.execute(text(_ACTIVE_VERSION_SQL), {
                "tenant": tenant_id,
                "kb": kb_id,
                "scope": scope,
                "ds": ds_id or "",
            })).first()
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        raise                                # 编程错误照抛（与同目录 fail-open 同款纪律）
    except Exception as exc:  # noqa: BLE001 —— 版本表不可读 = 无版本（旧库/未迁移）
        logger.warning("active 版本解析失败，按无版本处理：kb=%s scope=%s ds=%s err=%s",
                       kb_id, scope, ds_id or "", exc)
        return None
    if row is None:
        return None
    return {"version_id": str(row[0]), "snapshot_hash": str(row[1] or "")}


async def resolve_active_snapshot(tenant_id: str, kb_id: str, scope: str,
                                  ds_id: str | None) -> dict | None:
    """active 版本与其**完整快照**：`{"version_id","snapshot_hash","snapshot"}`。

    `snapshot` 尽力为 dict（JSON 字符串先解码）；NULL / 解码失败时可能是
    None/str，调用方须用 `isinstance` 守卫。
    **fail-open** 口径与 `resolve_active_version` 相同：读不了按「无版本」处理。
    """
    try:
        async with get_db_session() as session:
            row = (await session.execute(text(_ACTIVE_SNAPSHOT_SQL), {
                "tenant": tenant_id,
                "kb": kb_id,
                "scope": scope,
                "ds": ds_id or "",
            })).first()
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 —— 版本表不可读 = 无版本
        logger.warning("active 版本快照解析失败，按无版本处理：kb=%s scope=%s ds=%s err=%s",
                       kb_id, scope, ds_id or "", exc)
        return None
    if row is None:
        return None
    return {"version_id": str(row[0]),
            "snapshot_hash": str(row[1] or ""),
            "snapshot": _json_value(row[2])}


# ────────────────────────────────────────────────────────────────────────────
# 行读取（读口三分：编译行 / 抽象行）
# ────────────────────────────────────────────────────────────────────────────


async def _read_compiled_row(tenant_id: str, kb_id: str,
                             ds_id: str) -> dict | None:
    """该源那一行编译 schema（转普通 dict，会话关闭后才可能入缓存）。"""
    async with get_db_session() as session:
        rows = await OntologySchemaRepository(session).list_compiled_schemas(
            tenant_id, kb_id)
    for r in rows:
        if r.data_source_id != ds_id:
            continue
        return {
            "entity_types": list(r.entity_types or []),
            "relation_types": list(r.relation_types or []),
            "constraints": list(r.constraints or []),
            # `evidence` 不是语义内容（不进快照），但多轮档的读料需要原样带上。
            "evidence": dict(r.evidence or {}),
            "schema_version": int(r.schema_version or 1),
        }
    return None


async def _read_abstract_row(tenant_id: str, kb_id: str) -> dict | None:
    """该库那一行抽象 schema（v2 `{entities, relations}`）。"""
    async with get_db_session() as session:
        r = await OntologyAbstractSchemaRepository(session).get_by_kb(
            tenant_id, kb_id)
    if r is None:
        return None
    return {
        "schema": r.schema,
        "status": str(r.status or ""),
        "schema_version": int(r.schema_version or 1),
    }


def _is_drift(scope: str, kb_id: str, ds_id: str | None, row: dict,
              version: dict | None) -> bool:
    """行内容快照 hash 是否与 active 版本不一致（无版本 / 算不出来 → False）。

    compiled 专用；abstract 见 `_abstract_drift`。
    """
    if not version or not version.get("snapshot_hash"):
        return False
    try:
        snapshot = snapshot_from_row(scope, kb_id, ds_id, row)
        return canonical_snapshot_hash(snapshot) != str(version["snapshot_hash"])
    except Exception as exc:  # noqa: BLE001 —— 漂移只标注，不能成为新的抛点
        logger.warning("schema 漂移计算失败，按无漂移处理：kb=%s scope=%s ds=%s err=%s",
                       kb_id, scope, ds_id or "", exc)
        return False


def _abstract_drift(kb_id: str, row: dict, version: dict | None) -> bool:
    """当前原始行 vs active 版本快照（两侧都走共享序列化器）。

    - 无版本 / 快照不是 dict / 快照 content 不可用（`""`）→ 不可比 → False；
    - v1 行（`schema` 是概念数组）不可比 → False；
    - 其余：规范化后比 hash；异常只标注不抛（失败不拖垮提问）。
    """
    if not version:
        return False
    snapshot = version.get("snapshot")
    if not isinstance(snapshot, dict):
        return False
    raw = row.get("schema")
    if not isinstance(raw, dict):
        return False
    current = {"scope": "abstract", "kb": kb_id, "ds": None,
               "content": {"schema": raw, "status": row.get("status")}}
    # 两侧的 hash 计算一起放进 try：快照 content 里若有不可序列化的对象，
    # 期望值这一侧同样会抛 —— 漂移只标注，任何异常都按无漂移处理。
    try:
        expected = canonical_abstract_snapshot_hash(snapshot)
        if not expected:
            return False
        return canonical_abstract_snapshot_hash(current) != expected
    except Exception as exc:  # noqa: BLE001 —— 漂移只标注
        logger.warning("abstract 漂移计算失败，按无漂移处理：kb=%s err=%s",
                       kb_id, exc)
        return False


async def load_compiled_row(tenant_id: str, kb_id: str,
                            ds_id: str) -> dict | None:
    """该源那一行编译 schema + `drift`；行不存在 → None。

    返回 `{"entity_types", "relation_types", "constraints", "evidence",
    "schema_version", "drift"}`。⚠️ 命中缓存时返回的是缓存内容的浅拷贝，
    嵌套 list/dict 与缓存共享 —— 调用方**只读**，别原地改。
    """
    cached = _cache_lookup(tenant_id, kb_id, "compiled", ds_id)
    if cached is not None:
        payload, drift = cached
        if drift:
            _mark_drift("compiled")
        return _with_drift(payload, drift)

    # ⚠️ 代号必须在**未命中这一刻**采样（不是 `_cache_store` 里现读）：读行期间若有
    # 并发 upsert 推进代号，本次读到的是**写前**的行 —— 只有归档到 gen0 下，下一次
    # 查找才会按新代号判定、作废并重读。用写入时刻的代号归档会把写前的行冒充成新代。
    gen0 = schema_generation()
    version = await resolve_active_version(tenant_id, kb_id, "compiled", ds_id)
    row = await _read_compiled_row(tenant_id, kb_id, ds_id)
    if row is None:
        return None
    drift = _is_drift("compiled", kb_id, ds_id, row, version)
    if drift:
        _mark_drift("compiled", tenant_id=tenant_id, kb_id=kb_id,
                    data_source_id=ds_id,
                    version_id=(version or {}).get("version_id", ""), log=True)
    _cache_store(tenant_id, kb_id, "compiled", ds_id,
                 (version or {}).get("version_id", ""),
                 row["schema_version"], row, drift, gen0)
    return _with_drift(row, drift)


def tables_from_compiled_row(row: dict | None) -> dict[str, set[str]]:
    """编译行 → `{物理表: {列名}}`（治理投影）。

    与 `governance.common.schema_tables` **逐字同口径**：只收带非空
    `source_object_id` 的实体，列名取属性 `name`（**不是** `physical_name` ——
    治理标签按本体属性名登记）。行不存在 → `InvalidParameterError`
    （与旧 `schema_tables` 同型，调用方的 fail-open 分支照旧接住）。
    """
    if row is None:
        raise InvalidParameterError("该数据源没有可用的编译 schema")
    out: dict[str, set[str]] = {}
    for e in (row.get("entity_types") or []):
        if isinstance(e, dict) and str(e.get("source_object_id") or "").strip():
            out[str(e["source_object_id"]).strip()] = {
                str(a.get("name") or "") for a in (e.get("attributes") or [])
                if isinstance(a, dict)
            }
    return out


async def load_abstract_row(tenant_id: str, kb_id: str) -> dict | None:
    """该库那一行抽象 schema + `drift`；行不存在 → None。

    返回 `{"schema", "status", "schema_version", "drift"}`。`status` 过滤
    （只认 `active`）仍由调用方做 —— 本模块只负责读与标注。
    """
    cached = _cache_lookup(tenant_id, kb_id, "abstract", None)
    if cached is not None:
        payload, drift = cached
        if drift:
            _mark_drift("abstract")
        return _with_drift(payload, drift)

    # 同 `load_compiled_row`：代号在未命中这一刻采样（读行期间的并发写入不能让
    # 写前的行以新代号入缓存）。
    gen0 = schema_generation()
    version = await resolve_active_snapshot(tenant_id, kb_id, "abstract", "")
    row = await _read_abstract_row(tenant_id, kb_id)
    if row is None:
        return None
    drift = _abstract_drift(kb_id, row, version)
    if drift:
        _mark_drift("abstract", tenant_id=tenant_id, kb_id=kb_id,
                    version_id=(version or {}).get("version_id", ""), log=True)
    _cache_store(tenant_id, kb_id, "abstract", None,
                 (version or {}).get("version_id", ""),
                 row["schema_version"], row, drift, gen0)
    return _with_drift(row, drift)


__all__ = [
    "clear_cache",
    "collected_drift",
    "load_abstract_row",
    "load_compiled_row",
    "resolve_active_snapshot",
    "resolve_active_version",
    "start_drift_collection",
    "tables_from_compiled_row",
]
