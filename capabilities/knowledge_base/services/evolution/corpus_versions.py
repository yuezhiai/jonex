# -*- coding:utf-8 -*-
"""演化语料版本存储（迁移 030 / 语料治理线 Task 2）。

语料版本是**按 (tenant, knowledge_base_id, data_source_id) 分链的 append-only
快照**：每条链任意时刻至多一条 `active`（迁移 030 的
`uq_evo_corpus_version_active` 部分唯一索引兜底）。本模块只做存储原语：

- `corpus_snapshot` / `corpus_snapshot_hash`：把一批语料行规范化成快照并按
  `version_store.canonical_snapshot_hash` 的口径（排序键 + 紧凑 JSON）算 hash；
- `snapshot_for_chain`：读该链**当前全部题**（含停用题）→ 规范快照 + hash，
  是版本口径的唯一读口（`record` 与运行来源记录都复用它）；
- `record`：读该链当前全部题（**含停用题**）→ 与 active 同 hash 则不再写 →
  否则先 `UPDATE ... status='superseded'` 再 `INSERT` 新 active；
- `active`：读该链当前 active 版本（带 `case_count`，不回传完整快照）；
- `coverage`：按 (kb, ds) 的覆盖率汇总（题数 / 启用数 / tier / expect），并
  用同一读口判断 active 版本与当前语料是否一致（fresh / drifted / missing）。

为什么不走 ORM 对象改状态：SQLAlchemy 默认先 INSERT 后 UPDATE 的 flush 顺序
会撞上「同链至多一条 active」的部分唯一索引。`record` 用显式 `UPDATE` 语句把
旧 active 置 `superseded`，再 INSERT 新行；并发下冲突时 `rollback` 后**重试
一次**（重读 active：内容相同就直接复用它，否则重做 supersede + INSERT）。

租户隔离：所有读写都带 `tenant_id` 条件。SQL 直接走 `sqlalchemy.text()` +
具名参数（与同目录 `version_store.py` / `run_store.py` 一致）；JSONB 参数传
规范化 JSON 字符串（asyncpg 的 jsonb codec 收 str），读取侧兼容 codec 已解码
的 dict 与未解码的 str 两种形态。
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import InvalidParameterError

from .version_store import canonical_snapshot_hash

#: 语料快照保留的字段（顺序即 SELECT / 快照 dict 的列序）。
_CASE_FIELDS = ("name", "question", "tier", "expect", "control", "enabled")

#: 版本 `source` 白名单（与设计 D1 同口径）。
#: `auto` = `ensure_corpus_for_kb` 自动借/搬语料后的入账（Task 4）。
SNAPSHOT_SOURCES = frozenset({"manual", "promote", "seed", "seed_yaml", "auto"})

_MAIN = "knowledge_base.evolution_corpus_versions"
_CASES = "knowledge_base.evolution_corpus_cases"


# ---------------------------------------------------------------- 快照 / hash
def _case_value(case, field: str):
    if isinstance(case, dict):
        return case.get(field)
    return getattr(case, field, None)


def corpus_snapshot(kb_id, ds_id, cases) -> dict:
    """规范化语料快照：只保留六字段，按 `name` 排序（停用题也在内容里）。

    入参 `cases` 可以是 dict 列表（服务层）或带属性的行对象（DB 读取侧）；
    返回新 dict，**不修改入参**。
    """
    rows = [{field: _case_value(case, field) for field in _CASE_FIELDS}
            for case in (cases or [])]
    rows.sort(key=lambda case: str(case.get("name") or ""))
    return {"kb": kb_id, "ds": ds_id, "cases": rows}


def corpus_snapshot_hash(kb_id, ds_id, cases) -> str:
    """语料快照的规范化 sha256（与 `version_store.canonical_snapshot_hash` 同口径）。"""
    return canonical_snapshot_hash(corpus_snapshot(kb_id, ds_id, cases))


def _canonical_json(snapshot: dict) -> str:
    """与 `version_store` 相同的规范化 JSON（键排序、紧凑、中文不转义）。"""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _as_json(value):
    """读取侧容错：asyncpg 正常返回 dict，驱动配置差异下可能是 JSON 字符串。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------- SQL 片段
def _case_from_row(row) -> dict:
    """DB 行 → 六字段语料 dict（SQLAlchemy Row 支持属性访问）。"""
    return {field: getattr(row, field, None) for field in _CASE_FIELDS}


def _chain_params(tenant_id, kb_id, ds_id) -> dict:
    return {"tenant": tenant_id, "kb": kb_id, "ds": ds_id}


async def _read_cases(session, tenant_id, kb_id, ds_id) -> list[dict]:
    sql = (f"SELECT {', '.join(_CASE_FIELDS)} FROM {_CASES} "
           f"WHERE tenant_id = :tenant AND kb_id = :kb AND ds_id = :ds "
           f"ORDER BY name")
    rows = (await session.execute(
        text(sql), _chain_params(tenant_id, kb_id, ds_id))).all()
    return [_case_from_row(row) for row in rows]


async def snapshot_for_chain(session, tenant_id, kb_id,
                             ds_id) -> tuple[dict, str]:
    """规范全链快照（六字段、含停用题、按 `name` 排序）与其 hash。

    **版本口径的唯一入口**：`record` / 运行来源记录都不许拿 `load_corpus`
    的 `cases` 算 hash —— 它只含启用题、且缺 `enabled` 键，与版本链永远
    对不上。这里直接读该链全部题（含停用题），与 `record` 落库的快照同源。
    """
    cases = await _read_cases(session, tenant_id, kb_id, ds_id)
    snapshot = corpus_snapshot(kb_id, ds_id, cases)
    return snapshot, canonical_snapshot_hash(snapshot)


async def _read_active_brief(session, tenant_id, kb_id, ds_id):
    """record 用：只要 `id` 与 `snapshot_hash` 判同。"""
    sql = (f"SELECT id, snapshot_hash FROM {_MAIN} "
           f"WHERE tenant_id = :tenant AND knowledge_base_id = :kb "
           f"AND data_source_id = :ds AND status = 'active' LIMIT 1")
    return (await session.execute(
        text(sql), _chain_params(tenant_id, kb_id, ds_id))).first()


def _same_hash(row, snapshot_hash: str) -> bool:
    """CHAR(64) 在某些驱动下可能带右侧空格，比较前先 strip。"""
    return row is not None and str(row.snapshot_hash or "").strip() == snapshot_hash


async def _write_active(session, tenant_id, kb_id, ds_id, snapshot,
                        snapshot_hash: str, version_id: str, source: str,
                        note, created_by) -> None:
    """显式 supersede 旧 active，再 INSERT 新 active（顺序不可颠倒）。"""
    params = _chain_params(tenant_id, kb_id, ds_id)
    supersede_sql = (f"UPDATE {_MAIN} SET status = 'superseded' "
                     f"WHERE tenant_id = :tenant AND knowledge_base_id = :kb "
                     f"AND data_source_id = :ds AND status = 'active'")
    await session.execute(text(supersede_sql), params)

    insert_sql = (
        f"INSERT INTO {_MAIN} "
        f"(id, tenant_id, knowledge_base_id, data_source_id, snapshot, "
        f"snapshot_hash, status, source, note, created_by, activated_at) "
        f"VALUES (:id, :tenant, :kb, :ds, CAST(:snapshot AS jsonb), "
        f":snapshot_hash, 'active', :source, :note, :created_by, now())")
    await session.execute(text(insert_sql), {
        "id": version_id,
        "tenant": tenant_id,
        "kb": kb_id,
        "ds": ds_id,
        "snapshot": _canonical_json(snapshot),
        "snapshot_hash": snapshot_hash,
        "source": source,
        "note": note,
        "created_by": created_by,
    })


def _new_version_id() -> str:
    return "cv_" + uuid.uuid4().hex


# ---------------------------------------------------------------- 写原语
async def record(session, tenant_id, kb_id, ds_id, *, source: str,
                 created_by=None, note=None) -> dict | None:
    """把该链当前语料入账为新版本；内容与 active 相同则返回 None（不写）。

    返回 `{"version_id", "snapshot_hash", "case_count", "source"}`。
    并发唯一索引冲突时 `rollback` 后重试一次：重读 active，若已是相同 hash
    就直接返回该 active（不重复写），否则重做 supersede + INSERT。
    """
    if source not in SNAPSHOT_SOURCES:
        raise InvalidParameterError(
            message=(f"非法语料版本 source: {source}"
                     f"（允许: {', '.join(sorted(SNAPSHOT_SOURCES))}）"))

    snapshot, snapshot_hash = await snapshot_for_chain(
        session, tenant_id, kb_id, ds_id)

    active_row = await _read_active_brief(session, tenant_id, kb_id, ds_id)
    if _same_hash(active_row, snapshot_hash):
        return None

    version_id = _new_version_id()
    try:
        await _write_active(session, tenant_id, kb_id, ds_id, snapshot,
                            snapshot_hash, version_id, source, note,
                            created_by)
        await session.commit()
    except IntegrityError:
        # 并发写同一条链：先回滚，再按「重读 active → 同 hash 复用 / 否则重做」处理。
        await session.rollback()
        active_row = await _read_active_brief(session, tenant_id, kb_id, ds_id)
        if _same_hash(active_row, snapshot_hash):
            return {
                "version_id": active_row.id,
                "snapshot_hash": snapshot_hash,
                "case_count": len(snapshot["cases"]),
                "source": source,
            }
        version_id = _new_version_id()
        try:
            await _write_active(session, tenant_id, kb_id, ds_id, snapshot,
                                snapshot_hash, version_id, source, note,
                                created_by)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    except Exception:
        await session.rollback()
        raise

    return {
        "version_id": version_id,
        "snapshot_hash": snapshot_hash,
        "case_count": len(snapshot["cases"]),
        "source": source,
    }


# ---------------------------------------------------------------- 读原语
async def active(session, tenant_id, kb_id, ds_id) -> dict | None:
    """该链当前 active 版本；没有则 None。

    返回 `{"version_id", "snapshot_hash", "status", "source", "created_at",
    "case_count"}`（`case_count` 来自快照里的 `cases` 长度，不回传完整快照）。
    """
    sql = (f"SELECT id, snapshot, snapshot_hash, status, source, created_at "
           f"FROM {_MAIN} "
           f"WHERE tenant_id = :tenant AND knowledge_base_id = :kb "
           f"AND data_source_id = :ds AND status = 'active' LIMIT 1")
    row = (await session.execute(
        text(sql), _chain_params(tenant_id, kb_id, ds_id))).first()
    if row is None:
        return None
    snapshot = _as_json(row.snapshot) or {}
    return {
        "version_id": row.id,
        "snapshot_hash": str(row.snapshot_hash or "").strip(),
        "status": row.status,
        "source": row.source,
        "created_at": _iso(row.created_at),
        "case_count": len(snapshot.get("cases") or []),
    }


# ---------------------------------------------------------------- 覆盖率（Task 5）
async def _aggregate_chains(session, tenant_id, kb_ids=None) -> dict:
    """查询 1：按 (kb, ds, tier, expect, enabled) 聚合题数。

    返回 `{(kb, ds): {"total", "enabled", "by_tier", "by_expect"}}`。
    `total` 含停用题；`enabled` / 分布只计 `enabled = 1`。
    `kb_ids` 非 None 时追加 `AND kb_id IN (...)`（授权集合下推，见 `coverage`）。
    """
    params: dict = {"tenant": tenant_id}
    conditions = " WHERE tenant_id = :tenant"
    if kb_ids is not None:
        conditions += " AND kb_id IN :kb_ids"
        params["kb_ids"] = list(kb_ids)
    sql = (f"SELECT kb_id, ds_id, tier, expect, enabled, count(*) AS n "
           f"FROM {_CASES}{conditions} "
           f"GROUP BY kb_id, ds_id, tier, expect, enabled")
    stmt = text(sql)
    if kb_ids is not None:
        stmt = stmt.bindparams(bindparam("kb_ids", expanding=True))
    rows = (await session.execute(stmt, params)).all()

    chains: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row.kb_id), str(row.ds_id))
        item = chains.setdefault(key, {"total": 0, "enabled": 0,
                                       "by_tier": {}, "by_expect": {}})
        count = int(row.n or 0)
        item["total"] += count
        if int(row.enabled or 0) == 1:
            item["enabled"] += count
            tier = str(row.tier)
            expect = str(row.expect)
            item["by_tier"][tier] = item["by_tier"].get(tier, 0) + count
            item["by_expect"][expect] = item["by_expect"].get(expect, 0) + count
    return chains


async def _active_versions(session, tenant_id, kb_ids=None) -> dict:
    """查询 2：该租户全部 active 版本（每个 (kb, ds) 链至多一条）。

    返回 `{(kb, ds): row}`（row 带 `id` / `snapshot_hash` / `created_at`）。
    `kb_ids` 非 None 时追加 `AND knowledge_base_id IN (...)`。
    """
    params: dict = {"tenant": tenant_id}
    sql = (f"SELECT knowledge_base_id, data_source_id, id, snapshot_hash, "
           f"created_at FROM {_MAIN} "
           f"WHERE tenant_id = :tenant AND status = 'active'")
    if kb_ids is not None:
        sql += " AND knowledge_base_id IN :kb_ids"
        params["kb_ids"] = list(kb_ids)
    stmt = text(sql)
    if kb_ids is not None:
        stmt = stmt.bindparams(bindparam("kb_ids", expanding=True))
    rows = (await session.execute(stmt, params)).all()
    return {(str(row.knowledge_base_id), str(row.data_source_id)): row
            for row in rows}


async def coverage(session, tenant_id, kb_ids: list[str] | None = None) -> dict:
    """按 (kb, ds) 汇总语料覆盖率：题数 / 启用数 / tier / expect / freshness。

    链集合 = **语料出现的链 ∪ active 版本表出现的链**（版本存在但题已清空的
    链也要暴露，否则「有版本没语料」会从面板上消失）。按 (kb, ds) 排序输出。

    `kb_ids` 是**授权集合**（`None` = 不过滤，租户管理员路径；`[]` = 一个 KB
    都管不了 → 直接空页，不发查询）。非 None 时两条聚合查询都追加
    `AND <kb 列> IN (...)`，链集合随之收窄。

    `freshness` 与 `record` 同口径：用 `snapshot_for_chain` 读该链**当前全部
    题**（含停用题、按 name 排序）算 hash，与 active 的 hash 比较 ——
    `missing`（无 active）/ `fresh` / `drifted`。只读，不写库。
    """
    if kb_ids is not None and not kb_ids:
        return {"items": [], "totals": {"chains": 0, "total": 0, "enabled": 0}}
    aggregates = await _aggregate_chains(session, tenant_id, kb_ids)
    actives = await _active_versions(session, tenant_id, kb_ids)

    items: list[dict] = []
    for kb_id, ds_id in sorted(set(aggregates) | set(actives)):
        stats = aggregates.get(
            (kb_id, ds_id),
            {"total": 0, "enabled": 0, "by_tier": {}, "by_expect": {}})
        _snapshot, current_hash = await snapshot_for_chain(
            session, tenant_id, kb_id, ds_id)
        active_row = actives.get((kb_id, ds_id))
        if active_row is None:
            freshness = "missing"
            active_version_id = None
            active_hash = None
            updated_at = None
        else:
            active_version_id = str(active_row.id)
            active_hash = str(active_row.snapshot_hash or "").strip()
            freshness = "fresh" if active_hash == current_hash else "drifted"
            updated_at = _iso(active_row.created_at)

        items.append({
            "kb_id": kb_id,
            "ds_id": ds_id,
            "total": stats["total"],
            "enabled": stats["enabled"],
            "by_tier": stats["by_tier"],
            "by_expect": stats["by_expect"],
            "active_version_id": active_version_id,
            "active_hash": active_hash,
            "freshness": freshness,
            "updated_at": updated_at,
        })

    return {
        "items": items,
        "totals": {
            "chains": len(items),
            "total": sum(item["total"] for item in items),
            "enabled": sum(item["enabled"] for item in items),
        },
    }


__all__ = [
    "SNAPSHOT_SOURCES",
    "active",
    "corpus_snapshot",
    "corpus_snapshot_hash",
    "coverage",
    "record",
    "snapshot_for_chain",
]
