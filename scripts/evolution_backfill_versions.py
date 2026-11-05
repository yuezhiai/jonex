#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演化台语义版本回填（Task 9，演化台信任闭环 P0）。

做两件事（设计 spec `2026-10-23-evolution-trust-loop-design.md` §3.4 / §7 / §10）：

1. **v0 播种**：为每个现有 KB 的 active 抽象 schema 播种一条 `abstract` 版本；
   为每个存在 active 编译 schema 的 `(kb, ds)` 播种一条 `compiled` 版本。
   `created_by='migration'`、`note='v0 seed'`，播种后立即 `activate`。
2. **旧 run 父版本重建**：对 `status='adopted'` 的旧 run，把 `backup` 包装成
   该 scope 的快照（纯函数 `wrap_backup`），按 hash 去重复用；
   无法重建的 run 写 `parent_version_id=NULL` + `legacy_adopt=1`，不伪造 v0 血统。

用法：

    # 只打印计划，不写库
    python scripts/evolution_backfill_versions.py --tenant tenant_jonex_demo --dry-run
    # 实际回填（幂等：已有 active 的链跳过；已有 parent 的 run 跳过；同 hash 复用）
    python scripts/evolution_backfill_versions.py --tenant tenant_jonex_demo

环境变量与平台进程一致（DB_HOST / DB_PORT / DB_USERNAME / DB_PASSWORD / DB_NAME）；
默认自动加载仓库根 `.env.local`（`--env-file` 可换，`--no-env-file` 关闭）。

⚠️ 前置：迁移 **016 / 017 / 024** 已应用（本机 demo 库若缺表会直接报错，
不会静默跳过 —— 缺表时「没有旧 run」和「迁移没跑」无法区分）。

本脚本只读当前抽象/编译 schema，**不触发编译**；不写 `evolution_corpus_cases`。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("evolution_backfill_versions")

#: 迁移写入的 created_by（设计 §3.1）。
MIGRATION_ACTOR = "migration"
#: v0 播种的 note（设计 §7）。
V0_NOTE = "v0 seed"

#: 无法重建的 run 的 reason 分类（C5-1）。
REASON_BACKUP_MISSING = "backup_missing"
REASON_BACKUP_SHAPE_INVALID = "backup_shape_invalid"
REASON_COMPILED_DS_MISSING = "compiled_ds_missing"
#: kind 不在 `alias` / `query_rule` 里（历史脏数据），同样无法重建。
REASON_UNKNOWN_KIND = "unknown_kind"
UNRECOVERABLE_REASONS = (
    REASON_BACKUP_MISSING,
    REASON_BACKUP_SHAPE_INVALID,
    REASON_COMPILED_DS_MISSING,
    REASON_UNKNOWN_KIND,
)

_MAIN_TABLE = "knowledge_base.semantic_versions"
_ARCHIVE_TABLE = "knowledge_base.semantic_versions_archive"
_RUNS_TABLE = "knowledge_base.evolution_runs"


# ============================================================ 纯函数（单测面）
def scope_for_kind(kind: str) -> str:
    """`alias` → `abstract`（KB 级），`query_rule` → `compiled`（DS 级）。

    **单一来源**：直接委托 `services/evolution/locking.scope_for_kind`，不在
    脚本里维护第二份映射（C5-4）。延迟 import 是刻意的：模块加载与 `--help`
    路径不拉起 sqlalchemy / DB 基础设施；只有真正规划时才加载服务层。
    """
    from capabilities.knowledge_base.services.evolution.locking import (
        scope_for_kind as locking_scope_for_kind,
    )

    return locking_scope_for_kind(kind)


def chain_key(scope: str, kb: str, ds: str | None) -> tuple[str, str, str]:
    """版本链键 `(scope, kb, ds or '')`（与迁移 024 的 active 唯一索引同口径）。"""
    return (str(scope), str(kb), "" if ds is None else str(ds))


def build_snapshot(scope: str, kb: str, ds: str | None,
                   content: dict | None) -> dict:
    """按 `schema_io.snapshot_for` 的形状包装快照：只装内容，不装版本号/时间戳。"""
    return {"scope": scope, "kb": kb, "ds": ds, "content": content}


def canonical_hash(snapshot: dict) -> str:
    """复用版本存储的规范化 sha256（延迟 import，保持 `--help` 轻量）。"""
    from capabilities.knowledge_base.services.evolution.version_store import (
        canonical_snapshot_hash,
    )

    return canonical_snapshot_hash(snapshot)


def _as_json(value):
    """DB 驱动差异兜底：JSONB 可能已解码成 dict，也可能是 JSON 字符串。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def classify_backup(backup, kind: str) -> tuple[dict | None, str | None]:
    """包装旧 `run.backup`，同时给出**无法重建的原因**（C5-1）。

    返回 `(content, reason)`：成功时 `reason=None`；失败时 `content=None` 且
    `reason ∈ {backup_missing, backup_shape_invalid}`。

    - `backup_missing`：`None` / 空字符串 / 空 list / 空 dict —— 根本没有内容；
    - `backup_shape_invalid`：有内容但不是该 kind 期望的形状。

    形状口径：
    - `alias`（abstract）：`{"schema", "status"}`；缺 `schema` 或不是 dict。
    - `query_rule`（compiled）：`{"entity_types", "relation_types",
      "constraints"}`；三个键全缺；键存在但不是 list
      （防止把标量/空壳写进版本，恢复时反而覆盖线上内容）。
    """
    raw = _as_json(backup)
    if raw is None or raw == "" or raw == [] or raw == {}:
        return None, REASON_BACKUP_MISSING
    if not isinstance(raw, dict):
        return None, REASON_BACKUP_SHAPE_INVALID
    if kind == "alias":
        schema = raw.get("schema")
        if not isinstance(schema, dict):
            return None, REASON_BACKUP_SHAPE_INVALID
        return {"schema": schema, "status": raw.get("status") or "active"}, None
    if kind == "query_rule":
        keys = ("entity_types", "relation_types", "constraints")
        if not any(key in raw for key in keys):
            return None, REASON_BACKUP_SHAPE_INVALID
        content: dict = {}
        for key in keys:
            if key not in raw:
                content[key] = []
                continue
            value = raw[key]
            if not isinstance(value, (list, tuple)):
                return None, REASON_BACKUP_SHAPE_INVALID
            content[key] = list(value)
        return content, None
    raise ValueError(f"未知的演化类型: {kind}")


def wrap_backup(backup, kind: str) -> dict | None:
    """把旧 `run.backup` 行包装成该 scope 的快照 **content**；非法返回 None。

    兼容旧调用面；原因分类见 `classify_backup`。
    """
    return classify_backup(backup, kind)[0]


def plan_v0(candidates, active_keys) -> list[dict]:
    """dry-run 可读的 v0 播种计划。

    `candidates`: `[{"scope", "kb", "ds", "content"}]`（来自当前 schema 行）；
    `active_keys`: 已有 active 版本的链键集合。同一链在计划内只 create 一次。
    """
    active = {tuple(key) for key in active_keys}
    plan: list[dict] = []
    for candidate in candidates:
        scope = candidate["scope"]
        kb = candidate["kb"]
        ds = candidate.get("ds")
        key = chain_key(scope, kb, ds)
        snapshot = build_snapshot(scope, kb, ds, candidate.get("content"))
        action = "skip" if key in active else "create"
        if action == "create":
            active.add(key)
        plan.append({
            "scope": scope,
            "kb": kb,
            "ds": ds,
            "action": action,
            "snapshot": snapshot,
            "snapshot_hash": canonical_hash(snapshot),
        })
    return plan


def current_v0_hashes(candidates) -> dict[tuple[str, str, str], str]:
    """当前 schema 行的 v0 快照 hash，按版本链索引（孤儿 candidate 修复用）。"""
    hashes: dict[tuple[str, str, str], str] = {}
    for candidate in candidates:
        scope = candidate["scope"]
        kb = candidate["kb"]
        ds = candidate.get("ds")
        snapshot = build_snapshot(scope, kb, ds, candidate.get("content"))
        hashes[chain_key(scope, kb, ds)] = canonical_hash(snapshot)
    return hashes


def project_v0_versions(versions_by_chain, v0_plan) -> dict:
    """把「v0 计划中会 create 的版本」投影进版本索引，供 legacy 计划去重。

    dry-run 不写库，但 legacy 计划必须看到 v0 将要落库的 hash；否则
    backup 内容恰好等于当前 schema 时会重复 INSERT 一条同 hash 版本
    （R3 / spec §11.7）。执行侧先落 v0、再重新读库算 legacy 计划，天然一致。
    """
    projected = {tuple(key): list(versions or [])
                 for key, versions in (versions_by_chain or {}).items()}
    for item in v0_plan:
        if item["action"] != "create":
            continue
        key = chain_key(item["scope"], item["kb"], item["ds"])
        projected.setdefault(key, []).append({
            "version_id": None,
            "snapshot_hash": item["snapshot_hash"],
            "planned": "v0",
        })
    return projected


def plan_legacy(runs, versions_by_chain) -> list[dict]:
    """旧 run 父版本重建计划（纯函数；不写库）。

    `runs`: `[{"run_id", "kind", "kb", "ds", "backup", "parent_version_id"}]`，
    调用方按 `adopted_at NULLS LAST, updated_at, id` 升序给入。
    `versions_by_chain`: `{chain_key: [{"version_id", "snapshot_hash"}]}`；
    条目也可带 `planned`（`"v0"` = 本次 v0 计划将创建，`"run"` = 本次 legacy
    计划内先 create 的）。

    动作：`skip`（已有 parent）/ `reuse`（同链同 hash 已存在，含本次计划内
    先 create 的）/ `create` / `unset`（backup 缺失或形状非法）。
    """
    index: dict[tuple[str, str, str], dict[str, dict]] = {}
    for key, versions in (versions_by_chain or {}).items():
        bucket = index.setdefault(tuple(key), {})
        for version in versions or []:
            snapshot_hash = version.get("snapshot_hash")
            if not snapshot_hash:
                continue
            if version.get("version_id"):
                bucket[snapshot_hash] = {"version_id": version["version_id"]}
            elif version.get("planned"):
                bucket[snapshot_hash] = {
                    "planned": version["planned"],
                    "run_id": version.get("run_id"),
                }

    plan: list[dict] = []
    for run in runs:
        run_id = run.get("run_id")
        kind = run.get("kind")
        kb = run.get("kb")
        parent_id = run.get("parent_version_id")
        try:
            scope = scope_for_kind(kind)
        except ValueError:
            scope = None

        base = {
            "run_id": run_id,
            "kind": kind,
            "kb": kb,
            "ds": run.get("ds"),
            "scope": scope,
            "action": None,
            "reason": None,
            "version_id": None,
            "reuse_from_run_id": None,
            "reuse_from_v0": False,
            "snapshot": None,
            "snapshot_hash": None,
        }

        # 幂等：已经重建过（或新采纳路径写的 parent）→ 不动。
        if parent_id:
            plan.append({**base, "action": "skip",
                         "reason": "already_has_parent",
                         "version_id": parent_id})
            continue

        if scope is None:
            plan.append({**base, "action": "unset",
                         "reason": REASON_UNKNOWN_KIND})
            continue

        # abstract 链恒为 KB 级（ds=None）；run 上残留的 ds 不能带进 fast 链。
        ds = run.get("ds") if scope == "compiled" else None
        if scope == "compiled" and not ds:
            # compiled run 没有 ds：不能构造链键（`ck_evo_version_shape` 要求
            # compiled 必有 data_source_id）。归 unset —— 若混进 create，执行
            # 时 INSERT 会撞 CHECK 并中止整个脚本（历史 Minor）。
            plan.append({**base, "ds": None, "action": "unset",
                         "reason": REASON_COMPILED_DS_MISSING})
            continue
        content, reason = classify_backup(run.get("backup"), str(kind))
        if content is None:
            plan.append({**base, "ds": ds, "action": "unset",
                         "reason": reason})
            continue

        snapshot = build_snapshot(scope, kb, ds, content)
        snapshot_hash = canonical_hash(snapshot)
        bucket = index.setdefault(chain_key(scope, kb, ds), {})
        existing = bucket.get(snapshot_hash)

        if existing is None:
            # 计划内的哨兵：执行侧创建后会把真实 id 写进 hash 索引。
            bucket[snapshot_hash] = {"planned": "run", "run_id": run_id}
            plan.append({**base, "ds": ds, "action": "create",
                         "snapshot": snapshot,
                         "snapshot_hash": snapshot_hash})
        elif existing.get("planned") == "run":
            plan.append({**base, "ds": ds, "action": "reuse",
                         "reuse_from_run_id": existing.get("run_id"),
                         "snapshot": snapshot,
                         "snapshot_hash": snapshot_hash})
        elif existing.get("planned") == "v0":
            # v0 计划将创建同 hash 版本：legacy 直接复用，不能再 INSERT。
            plan.append({**base, "ds": ds, "action": "reuse",
                         "reuse_from_v0": True,
                         "snapshot": snapshot,
                         "snapshot_hash": snapshot_hash})
        else:
            plan.append({**base, "ds": ds, "action": "reuse",
                         "version_id": existing.get("version_id"),
                         "snapshot": snapshot,
                         "snapshot_hash": snapshot_hash})
    return plan


def plan_counts(plan) -> dict[str, int]:
    """动作计数（dry-run 汇总用）。"""
    counts: dict[str, int] = {}
    for item in plan:
        action = item.get("action") or "unknown"
        counts[action] = counts.get(action, 0) + 1
    return counts


def unrecoverable_items(plan) -> list[dict]:
    """无法重建的旧 run（`action == 'unset'`）—— 逐条带 run_id + reason。"""
    return [item for item in plan if item.get("action") == "unset"]


def unrecoverable_counts(plan) -> dict[str, int]:
    """按 reason 分类计数（`backup_missing` / `backup_shape_invalid` / …）。

    以 `UNRECOVERABLE_REASONS` 校验并固定输出顺序；未知 reason 追加在末尾
    （不静默丢弃，便于发现新的分类漂移）。
    """
    counts: dict[str, int] = {}
    for item in unrecoverable_items(plan):
        reason = item.get("reason") or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    ordered = {reason: counts[reason]
               for reason in UNRECOVERABLE_REASONS if reason in counts}
    for reason, value in counts.items():
        if reason not in ordered:
            ordered[reason] = value
    return ordered


# ============================================================ 环境文件
def load_env_file(path: str) -> str:
    """加载 shell 风格 env 文件（已存在的环境变量不覆盖）。返回实际加载的路径。"""
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
    return str(file_path)


# ============================================================ DB 读
async def _require_schema(session) -> None:
    """缺表时明确报错：缺表既可能代表「没有旧 run」，也可能代表「迁移没跑」。"""
    from sqlalchemy import text

    row = (await session.execute(text(
        "SELECT to_regclass(:main) IS NOT NULL AS has_main, "
        "       to_regclass(:archive) IS NOT NULL AS has_archive, "
        "       to_regclass(:runs) IS NOT NULL AS has_runs"),
        {"main": _MAIN_TABLE, "archive": _ARCHIVE_TABLE,
         "runs": _RUNS_TABLE})).first()
    missing = []
    if row is None or not row.has_main:
        missing.append(f"{_MAIN_TABLE}（迁移 024）")
    if row is None or not row.has_archive:
        missing.append(f"{_ARCHIVE_TABLE}（迁移 024）")
    if row is None or not row.has_runs:
        missing.append(f"{_RUNS_TABLE}（迁移 016）")
    if missing:
        raise RuntimeError(
            "演进表不存在：" + "、".join(missing)
            + "。请先应用对应迁移（存量库手工 `psql -f`），再跑本脚本。")


async def _load_sources(session, tenant: str) -> tuple[list[str], dict[str, list[str]]]:
    """现有 KB（未软删）与每个 KB 存在 active 编译行的 DS。"""
    from sqlalchemy import text

    kb_rows = (await session.execute(text(
        "SELECT id FROM knowledge_base.knowledge_info "
        "WHERE tenant_id = :tenant AND is_deleted = 0 ORDER BY id"),
        {"tenant": tenant})).all()
    kb_ids = [row[0] for row in kb_rows]

    ds_map: dict[str, list[str]] = {}
    for kb in kb_ids:
        rows = (await session.execute(text(
            "SELECT DISTINCT data_source_id "
            "FROM knowledge_base.ontology_compiled_schemas "
            "WHERE tenant_id = :tenant AND knowledge_base_id = :kb "
            "  AND data_source_id IS NOT NULL AND status = 'active' "
            "ORDER BY data_source_id"),
            {"tenant": tenant, "kb": kb})).all()
        ds_map[kb] = [row[0] for row in rows]
    return kb_ids, ds_map


async def _v0_candidates(tenant: str, kb_ids: list[str],
                         ds_map: dict[str, list[str]]
                         ) -> tuple[list[dict], list[dict]]:
    """读当前 schema 行，产出 v0 候选（不触发编译、不写库）。

    返回 `(candidates, failures)`。**读失败不吞**：`failures` 非空时调用方
    必须让进程以非零码退出（且不写库），否则运维会把「读挂了」看成「没有
    可播种的 schema」。单条失败不阻塞其余候选的展示。
    """
    from capabilities.knowledge_base.services.evolution.schema_io import (
        get_row,
        snapshot_from_row,
    )

    candidates: list[dict] = []
    failures: list[dict] = []
    for kb in kb_ids:
        try:
            row = await get_row(tenant, "alias", kb, None)
        except Exception as exc:  # noqa: BLE001 —— 单库失败不阻塞其余 KB
            logger.warning("读取抽象 schema 失败 kb=%s: %s", kb, exc)
            failures.append({"kb": kb, "ds": None, "scope": "abstract",
                             "error": str(exc)})
            row = None
        if isinstance(row, dict) and row.get("status") == "active":
            snapshot = snapshot_from_row("abstract", kb, None, row)
            if snapshot.get("content") is not None:
                candidates.append({"scope": "abstract", "kb": kb, "ds": None,
                                   "content": snapshot["content"]})
            else:
                logger.warning("抽象 schema 行无 schema 内容，跳过 kb=%s", kb)

        for ds in ds_map.get(kb, []):
            try:
                row = await get_row(tenant, "query_rule", kb, ds)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取编译 schema 失败 kb=%s ds=%s: %s", kb, ds, exc)
                failures.append({"kb": kb, "ds": ds, "scope": "compiled",
                                 "error": str(exc)})
                continue
            if isinstance(row, dict) and row:
                snapshot = snapshot_from_row("compiled", kb, ds, row)
                if snapshot.get("content") is not None:
                    candidates.append({"scope": "compiled", "kb": kb, "ds": ds,
                                       "content": snapshot["content"]})
    return candidates, failures


async def _load_versions(session, tenant: str):
    """读该租户全部版本（摘要），返回 (versions_by_chain, active_keys)。

    **主表 ∪ 归档表**（C5-2）：归档版本也能作为 legacy run 的父版本复用
    （spec §4.2 允许回滚归档目标）。两张表都只取 `active` / `superseded`
    （与 `_load_hash_ids` / `_find_version_by_hash` 同口径）——否则计划会
    复用一条 candidate/rejected 版本，执行侧却查不到，整脚本以退出码 1
    中止；同 hash 归档有 superseded、主表有 candidate 时也会选错。
    归档行**先**进索引，主表后进 —— `plan_legacy` 对同 hash 是「后者覆盖」，
    这样主表命中优先。归档里的 `status='active'` 不算 live active（不会出现
    在 `active_keys`）。
    """
    from sqlalchemy import text

    versions_by_chain: dict[tuple[str, str, str], list[dict]] = {}
    active_keys: set[tuple[str, str, str]] = set()

    archive_rows = (await session.execute(text(
        f"SELECT id, knowledge_base_id, scope, data_source_id, "
        f"       snapshot_hash, status "
        f"FROM {_ARCHIVE_TABLE} WHERE tenant_id = :tenant "
        f"  AND status IN ('active', 'superseded') "
        f"ORDER BY created_at ASC, id ASC"),
        {"tenant": tenant})).all()
    for row in archive_rows:
        key = chain_key(row.scope, row.knowledge_base_id, row.data_source_id)
        versions_by_chain.setdefault(key, []).append(
            {"version_id": row.id, "snapshot_hash": row.snapshot_hash})

    rows = (await session.execute(text(
        f"SELECT id, knowledge_base_id, scope, data_source_id, "
        f"       snapshot_hash, status "
        f"FROM {_MAIN_TABLE} WHERE tenant_id = :tenant "
        f"  AND status IN ('active', 'superseded') "
        f"ORDER BY created_at ASC, id ASC"),
        {"tenant": tenant})).all()
    for row in rows:
        key = chain_key(row.scope, row.knowledge_base_id, row.data_source_id)
        versions_by_chain.setdefault(key, []).append(
            {"version_id": row.id, "snapshot_hash": row.snapshot_hash})
        if row.status == "active":
            active_keys.add(key)
    return versions_by_chain, active_keys


async def _load_adopted_runs(session, tenant: str) -> list[dict]:
    """旧 run：只取 `status='adopted'`，按 `adopted_at NULLS LAST, updated_at, id`。"""
    from sqlalchemy import text

    rows = (await session.execute(text(
        f"SELECT id, knowledge_base_id, data_source_id, kind, backup, "
        f"       parent_version_id "
        f"FROM {_RUNS_TABLE} "
        f"WHERE tenant_id = :tenant AND status = 'adopted' "
        f"ORDER BY adopted_at ASC NULLS LAST, updated_at ASC, id ASC"),
        {"tenant": tenant})).all()
    return [{
        "run_id": row.id,
        "kb": row.knowledge_base_id,
        "ds": row.data_source_id,
        "kind": row.kind,
        "backup": _as_json(row.backup),
        "parent_version_id": row.parent_version_id,
    } for row in rows]


# ============================================================ DB 写
async def _load_hash_ids(session, tenant: str) -> dict[tuple[str, str, str], dict[str, str]]:
    """同链同 hash 的版本 id 索引：**主表 ∪ 归档表**（C5-2）。

    复用只认「真正评估过」的版本：`candidate` 可能尚未评估，`rejected` 是
    smoke/应用失败的内容 —— 拿它们当 legacy run 的父版本会伪造血统（历史
    Minor：reuse 索引不区分状态）。

    主表优先：同一 hash 两表都有时，返回主表 id；只有归档命中时复用归档 id
    （spec §4.2 允许父版本指向归档版本）。

    表内口径统一为「`ORDER BY created_at ASC, id ASC` 下后者覆盖」（即同
    hash 取最新一条）；跨表合并时归档用 `setdefault`，主表已占位的 hash
    不被归档覆盖。
    """
    from sqlalchemy import text

    out: dict[tuple[str, str, str], dict[str, str]] = {}
    rows = (await session.execute(text(
        f"SELECT id, knowledge_base_id, scope, data_source_id, snapshot_hash "
        f"FROM {_MAIN_TABLE} WHERE tenant_id = :tenant "
        f"  AND status IN ('active', 'superseded') "
        f"ORDER BY created_at ASC, id ASC"),
        {"tenant": tenant})).all()
    for row in rows:
        key = chain_key(row.scope, row.knowledge_base_id, row.data_source_id)
        out.setdefault(key, {})[row.snapshot_hash] = row.id

    archive_rows = (await session.execute(text(
        f"SELECT id, knowledge_base_id, scope, data_source_id, snapshot_hash "
        f"FROM {_ARCHIVE_TABLE} WHERE tenant_id = :tenant "
        f"  AND status IN ('active', 'superseded') "
        f"ORDER BY created_at ASC, id ASC"),
        {"tenant": tenant})).all()
    archive_index: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in archive_rows:
        key = chain_key(row.scope, row.knowledge_base_id, row.data_source_id)
        archive_index.setdefault(key, {})[row.snapshot_hash] = row.id
    for key, hashes in archive_index.items():
        target = out.setdefault(key, {})
        for snapshot_hash, version_id in hashes.items():
            target.setdefault(snapshot_hash, version_id)
    return out


async def _find_version_by_hash(session, tenant: str, key, snapshot_hash: str):
    """计划读库与执行之间出现新版本时兜底查一次（同链同 hash）。

    先查主表，再查归档表（C5-2：父版本允许指向归档版本）。
    """
    from sqlalchemy import text

    scope, kb, ds = key
    clauses = [
        "tenant_id = :tenant",
        "knowledge_base_id = :kb",
        "scope = :scope",
        "snapshot_hash = :hash",
        "status IN ('active', 'superseded')",
    ]
    params = {"tenant": tenant, "kb": kb, "scope": scope, "hash": snapshot_hash}
    if scope == "abstract":
        clauses.append("data_source_id IS NULL")
    else:
        clauses.append("data_source_id = :ds")
        params["ds"] = ds
    for table in (_MAIN_TABLE, _ARCHIVE_TABLE):
        row = (await session.execute(text(
            f"SELECT id FROM {table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC, id DESC LIMIT 1"), params)).first()
        if row is not None:
            return row.id
    return None


async def _load_migration_candidates(session, tenant: str) -> list[dict]:
    """本脚本可能留下的 `candidate` 版本（两步提交的崩溃窗口）。"""
    from sqlalchemy import text

    rows = (await session.execute(text(
        f"SELECT id, knowledge_base_id, scope, data_source_id, "
        f"       snapshot_hash, note "
        f"FROM {_MAIN_TABLE} "
        f"WHERE tenant_id = :tenant AND status = 'candidate' "
        f"  AND created_by = :actor "
        f"ORDER BY created_at ASC, id ASC"),
        {"tenant": tenant, "actor": MIGRATION_ACTOR})).all()
    return [{
        "version_id": row.id,
        "kb": row.knowledge_base_id,
        "scope": row.scope,
        "ds": row.data_source_id,
        "snapshot_hash": row.snapshot_hash,
        "note": row.note,
    } for row in rows]


async def repair_migration_candidates(tenant: str,
                                      current_hashes: dict) -> dict[str, int]:
    """收敛历史崩溃留下的 `candidate`（幂等，写库前先跑）。

    - `legacy backup of ...`：旧版两步提交的孤儿 → `superseded + legacy=1`；
    - `v0 seed`：当前 schema hash 一致且链上没有 active → 补 `activate`；
      否则（schema 已变 / 链已有 active）→ `superseded`，不再参与 active 指针，
      但仍可作为 legacy 父版本被复用（`superseded` 是回滚允许的状态）。
    """
    from sqlalchemy import text

    from capabilities.knowledge_base.services.evolution.version_store import (
        VersionStore,
    )
    from jonex_core.common.database import get_db_session

    stats = {"v0_activated": 0, "v0_superseded": 0,
             "legacy_fixed": 0, "unknown": 0}
    async with get_db_session() as session:
        rows = await _load_migration_candidates(session, tenant)
        if not rows:
            return stats
        _, active_keys = await _load_versions(session, tenant)
        store = VersionStore(session)
        for row in rows:
            note = row["note"] or ""
            key = chain_key(row["scope"], row["kb"], row["ds"])
            if note.startswith("legacy backup of "):
                await session.execute(text(
                    f"UPDATE {_MAIN_TABLE} "
                    f"SET status = 'superseded', legacy = 1 "
                    f"WHERE id = :version_id AND tenant_id = :tenant "
                    f"  AND status = 'candidate'"),
                    {"version_id": row["version_id"], "tenant": tenant})
                await session.commit()
                stats["legacy_fixed"] += 1
            elif note == V0_NOTE:
                if (key not in active_keys
                        and current_hashes.get(key) == row["snapshot_hash"]):
                    await store.activate(
                        tenant, row["version_id"], previous_id=None)
                    active_keys.add(key)
                    stats["v0_activated"] += 1
                else:
                    await session.execute(text(
                        f"UPDATE {_MAIN_TABLE} SET status = 'superseded' "
                        f"WHERE id = :version_id AND tenant_id = :tenant "
                        f"  AND status = 'candidate'"),
                        {"version_id": row["version_id"], "tenant": tenant})
                    await session.commit()
                    stats["v0_superseded"] += 1
            else:
                logger.warning("未知的 migration candidate，保持原样: %s",
                               row["version_id"])
                stats["unknown"] += 1
    return stats


async def _find_migration_candidate(session, tenant: str, key,
                                    snapshot_hash: str):
    """同链同 hash 的 v0 candidate（v0 崩溃恢复用）。

    只认 `note='v0 seed'`：legacy candidate 由 repair 收敛成 superseded，
    不能被 v0 的恢复路径误激活（否则会出现 `active + legacy=1` 的怪状态）。
    """
    from sqlalchemy import text

    scope, kb, ds = key
    clauses = [
        "tenant_id = :tenant",
        "knowledge_base_id = :kb",
        "scope = :scope",
        "snapshot_hash = :hash",
        "status = 'candidate'",
        "created_by = :actor",
        "note = :note",
    ]
    params = {"tenant": tenant, "kb": kb, "scope": scope,
              "hash": snapshot_hash, "actor": MIGRATION_ACTOR,
              "note": V0_NOTE}
    if scope == "abstract":
        clauses.append("data_source_id IS NULL")
    else:
        clauses.append("data_source_id = :ds")
        params["ds"] = ds
    row = (await session.execute(text(
        f"SELECT id FROM {_MAIN_TABLE} WHERE {' AND '.join(clauses)} "
        f"ORDER BY created_at ASC, id ASC LIMIT 1"), params)).first()
    return row.id if row is not None else None


# `_normalize_legacy_candidate` 已删除（review I1）：复用索引与兜底查询都
# 只认 `active` / `superseded`，migration candidate 不可能再从这里命中；
# candidate 的收敛统一由写库前的 `repair_migration_candidates` 负责。


async def _insert_legacy_version(session, tenant: str, item: dict) -> str:
    """legacy 备份**单条 INSERT**（status=superseded / legacy=1）。

    不走 `create_candidate` + UPDATE 的两步提交：崩溃在两步之间会留下一个
    `candidate` 孤儿，重跑时按 hash 复用成 parent 却仍是 candidate，
    随后回滚会被 409 拒绝且不会自愈。单条 INSERT 没有这个窗口。
    """
    import uuid

    from sqlalchemy import text

    version_id = "ver_" + uuid.uuid4().hex
    snapshot_json = json.dumps(item["snapshot"], sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
    await session.execute(text(
        f"INSERT INTO {_MAIN_TABLE} "
        f"(id, tenant_id, knowledge_base_id, scope, data_source_id, "
        f" parent_version_id, source_run_id, source_version_id, snapshot, "
        f" snapshot_hash, status, created_by, legacy, note) VALUES "
        f"(:id, :tenant, :kb, :scope, :ds, NULL, NULL, NULL, "
        f" CAST(:snapshot AS jsonb), :snapshot_hash, 'superseded', "
        f" :created_by, 1, :note)"),
        {"id": version_id, "tenant": tenant, "kb": item["kb"],
         "scope": item["scope"], "ds": item["ds"],
         "snapshot": snapshot_json, "snapshot_hash": item["snapshot_hash"],
         "created_by": MIGRATION_ACTOR,
         "note": f"legacy backup of {item['run_id']}"})
    await session.commit()
    return version_id


async def apply_v0(tenant: str, v0_plan: list[dict]) -> dict[str, int]:
    """执行 v0 播种（幂等；崩溃留下的 candidate 直接补 activate）。"""
    from capabilities.knowledge_base.services.evolution.version_store import (
        VersionStore,
    )
    from jonex_core.common.database import get_db_session

    stats = {"v0_created": 0, "v0_recovered": 0, "v0_skipped": 0}
    async with get_db_session() as session:
        store = VersionStore(session)
        _, active_keys = await _load_versions(session, tenant)
        for item in v0_plan:
            if item["action"] != "create":
                stats["v0_skipped"] += 1
                continue
            key = chain_key(item["scope"], item["kb"], item["ds"])
            # 计划是只读快照；落库前再核一次，避免与并发写撞唯一索引。
            if key in active_keys:
                logger.warning("v0 跳过（链已有 active）：%s/%s",
                               item["kb"], item["ds"] or "abstract")
                stats["v0_skipped"] += 1
                continue
            orphan = await _find_migration_candidate(
                session, tenant, key, item["snapshot_hash"])
            if orphan is not None:
                # 上次进程在 create_candidate 与 activate 之间崩了：直接补激活。
                await store.activate(tenant, orphan, previous_id=None)
                active_keys.add(key)
                stats["v0_recovered"] += 1
                print(f"  v0 recover {item['kb']}/"
                      f"{item['ds'] or 'abstract'} -> {orphan}")
                continue
            created = await store.create_candidate(
                tenant, item["kb"], item["scope"], item["ds"], item["snapshot"],
                created_by=MIGRATION_ACTOR, note=V0_NOTE)
            await store.activate(tenant, created["version_id"], previous_id=None)
            active_keys.add(key)
            stats["v0_created"] += 1
            print(f"  v0 create  {item['kb']}/"
                  f"{item['ds'] or 'abstract'} -> {created['version_id']}")
    return stats


async def apply_legacy(tenant: str, legacy_plan: list[dict]) -> dict[str, int]:
    """执行旧 run 父版本重建（幂等；同 hash 复用，缺失则 unset）。"""
    from sqlalchemy import text

    from jonex_core.common.database import get_db_session

    stats = {"reuse": 0, "create": 0, "unset": 0, "skip": 0}
    async with get_db_session() as session:
        hash_ids = await _load_hash_ids(session, tenant)

        for item in legacy_plan:
            action = item["action"]
            if action == "skip":
                stats["skip"] += 1
                continue
            if action == "unset":
                await session.execute(text(
                    f"UPDATE {_RUNS_TABLE} "
                    f"SET parent_version_id = NULL, legacy_adopt = 1 "
                    f"WHERE id = :run_id AND tenant_id = :tenant"),
                    {"run_id": item["run_id"], "tenant": tenant})
                await session.commit()
                stats["unset"] += 1
                print(f"  run unset  {item['run_id']}（{item['reason']}）")
                continue

            key = chain_key(item["scope"], item["kb"], item["ds"])
            snapshot_hash = item["snapshot_hash"]
            # I1 落库兜底：v0 播种后同一份 hash 可能已经在了；此时必须复用，
            # 不能再 INSERT 一条同 hash 的 superseded 版本。
            version_id = hash_ids.get(key, {}).get(snapshot_hash)
            if version_id is not None:
                stats["reuse"] += 1
                print(f"  run reuse  {item['run_id']} -> {version_id}")
            elif action == "create":
                version_id = await _insert_legacy_version(session, tenant, item)
                hash_ids.setdefault(key, {})[snapshot_hash] = version_id
                stats["create"] += 1
                print(f"  run create {item['run_id']} -> {version_id}")
            elif action == "reuse":
                # 计划说复用，但索引里没有（并发删除/跨进程）：兜底再查一次。
                version_id = await _find_version_by_hash(
                    session, tenant, key, snapshot_hash)
                if version_id is None:
                    raise RuntimeError(
                        f"复用失败：链 {key} 上找不到 hash={snapshot_hash}")
                stats["reuse"] += 1
                print(f"  run reuse  {item['run_id']} -> {version_id}")
            else:  # pragma: no cover —— plan_legacy 只产出上面四种动作
                raise RuntimeError(f"未知计划动作: {action}")

            await session.execute(text(
                f"UPDATE {_RUNS_TABLE} SET parent_version_id = :version_id "
                f"WHERE id = :run_id AND tenant_id = :tenant"),
                {"version_id": version_id, "run_id": item["run_id"],
                 "tenant": tenant})
            await session.commit()
    return stats


# ============================================================ 输出
def _label(item: dict) -> str:
    if item.get("scope") == "compiled" or item.get("ds"):
        return f"{item['kb']}/{item.get('ds') or '?'}"
    return f"{item['kb']}/abstract"


def print_plan(tenant: str, v0_plan: list[dict],
               legacy_plan: list[dict], *, dry_run: bool,
               failures: list[dict] | None = None,
               orphan_count: int = 0,
               repair_stats: dict[str, int] | None = None) -> None:
    header = ("（dry-run：只读，不写 PG，可能写 Redis 缓存）"
              if dry_run else "")
    print(f"[{tenant}] 演化版本回填计划{header}")

    if repair_stats and any(repair_stats.values()):
        print("\n== migration candidate 孤儿收敛 ==")
        print("  " + ", ".join(f"{key}={value}"
                               for key, value in repair_stats.items()))

    print("\n== v0 播种 ==")
    if not v0_plan:
        print("  （无候选：没有 active 抽象 schema / active 编译 schema）")
    for item in v0_plan:
        print(f"  {_label(item):<48} {item['action']:<6} "
              f"hash={item['snapshot_hash'][:12]}")
    if any(item["action"] == "skip" for item in v0_plan):
        print("  （skip = 该版本链已有 active 版本，幂等跳过）")

    print("\n== 旧 run 父版本 ==")
    if not legacy_plan:
        print("  （无 adopted run）")
    for item in legacy_plan:
        detail = ""
        if item["snapshot_hash"]:
            detail = f"hash={item['snapshot_hash'][:12]}"
        if item["version_id"]:
            detail += f" version={item['version_id']}"
        if item["reuse_from_run_id"]:
            detail += f" reuse-from={item['reuse_from_run_id']}"
        if item["reuse_from_v0"]:
            detail += " reuse-from=v0"
        if item["reason"]:
            detail += f" ({item['reason']})"
        kind = item.get("kind") or "?"
        print(f"  {item['run_id']:<36} {kind:<10} {_label(item):<44} "
              f"{item['action']:<6} {detail}".rstrip())

    unrecoverable = unrecoverable_items(legacy_plan)
    if unrecoverable:
        print("\n== 无法重建的旧 run（parent=NULL, legacy_adopt=1）==")
        for item in unrecoverable:
            reason = item.get("reason") or "unknown"
            logger.warning("旧 run 无法重建 run_id=%s kind=%s reason=%s",
                           item["run_id"], item.get("kind"), reason)
            print(f"  {item['run_id']:<36} reason={reason}")

    if orphan_count:
        print(f"\n== migration candidate 孤儿 ==\n"
              f"  发现 {orphan_count} 条（dry-run 只报告；写库时会先收敛）")

    if failures:
        print("\n== schema 读取失败 ==")
        for failure in failures:
            target = (f"{failure['kb']}/abstract"
                      if failure["scope"] == "abstract"
                      else f"{failure['kb']}/{failure['ds']}")
            print(f"  {target:<48} ERROR {failure['error']}")
        print("  ⚠️ 读失败 > 0：不会写库，进程将以非零码退出。")

    v0_counts = plan_counts(v0_plan)
    legacy_counts = plan_counts(legacy_plan)
    print("\n== 汇总 ==")
    print(f"  v0:   " + (", ".join(f"{k}={v}" for k, v in v0_counts.items())
                            or "0"))
    print(f"  runs: " + (", ".join(f"{k}={v}" for k, v in legacy_counts.items())
                            or "0"))
    reason_counts = unrecoverable_counts(legacy_plan)
    breakdown = ", ".join(f"{key}={value}"
                          for key, value in reason_counts.items())
    print(f"  unrecoverable={len(unrecoverable)}"
          + (f"（{breakdown}）" if breakdown else ""))
    if failures:
        print(f"  read_failures={len(failures)}")
    if dry_run:
        print("\n[dry-run] 未写 PG。"
              "（读取编译 schema 仍可能写入 Redis 缓存；不影响 PG 零写。）")


# ============================================================ CLI
async def run(args: argparse.Namespace) -> int:
    from jonex_core.common.database import get_db_session
    from jonex_core.common.tenant import require_tenant

    tenant = require_tenant(args.tenant)

    async with get_db_session() as session:
        await _require_schema(session)
        kb_ids, ds_map = await _load_sources(session, tenant)
        versions_by_chain, active_keys = await _load_versions(session, tenant)
        runs = await _load_adopted_runs(session, tenant)
        migration_candidates = await _load_migration_candidates(session, tenant)

    logger.info("租户=%s KB=%d run=%d 版本=%d candidate孤儿=%d", tenant,
                len(kb_ids), len(runs),
                sum(len(v) for v in versions_by_chain.values()),
                len(migration_candidates))

    v0_candidates, failures = await _v0_candidates(tenant, kb_ids, ds_map)
    v0_plan = plan_v0(v0_candidates, active_keys)
    # dry-run 也必须把「v0 将要创建的同 hash 版本」投影进 legacy 去重索引，
    # 否则计划会显示一条多余的 create（I1）。
    projected = project_v0_versions(versions_by_chain, v0_plan)
    legacy_plan = plan_legacy(runs, projected)

    if args.dry_run:
        print_plan(tenant, v0_plan, legacy_plan, dry_run=True,
                   failures=failures,
                   orphan_count=len(migration_candidates))
        if failures:
            logger.error("有 %d 个 schema 读取失败；dry-run 以非零码退出。",
                         len(failures))
            return 2
        return 0

    if failures:
        # Fail closed：读不全就不写库，避免只播一半 v0 / 漏掉 legacy 父版本。
        print_plan(tenant, v0_plan, legacy_plan, dry_run=False,
                   failures=failures,
                   orphan_count=len(migration_candidates))
        logger.error("有 %d 个 schema 读取失败；已中止写库。", len(failures))
        return 2

    # 写库前先收敛历史崩溃留下的 candidate（v0 补 activate / legacy 改 superseded）。
    repair_stats = await repair_migration_candidates(
        tenant, current_v0_hashes(v0_candidates))

    # 重新读库：repair 可能改了 active 指针。
    async with get_db_session() as session:
        versions_by_chain, active_keys = await _load_versions(session, tenant)

    v0_plan = plan_v0(v0_candidates, active_keys)
    projected = project_v0_versions(versions_by_chain, v0_plan)
    legacy_plan = plan_legacy(runs, projected)
    print_plan(tenant, v0_plan, legacy_plan, dry_run=False,
               repair_stats=repair_stats)

    print("\n== 写库 ==")
    v0_stats = await apply_v0(tenant, v0_plan)

    # v0 落库后重新读版本，再算 legacy 计划：保证同 hash 的 v0 版本被复用，
    # 不会再 INSERT 一条同 hash 的 superseded 版本（I1 根治）。
    async with get_db_session() as session:
        versions_by_chain, _ = await _load_versions(session, tenant)
    legacy_plan = plan_legacy(runs, versions_by_chain)
    legacy_stats = await apply_legacy(tenant, legacy_plan)

    stats = {**v0_stats, **legacy_stats}
    print("\n== 实际写入 ==")
    print("  " + ", ".join(f"{key}={value}" for key, value in stats.items()))
    print(f"  unrecoverable={len(unrecoverable_items(legacy_plan))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "演化台语义版本回填：v0 播种 + 旧 run 父版本重建。"
            "注意：即使 --dry-run，读取编译 schema 也可能写入 Redis 缓存"
            "（PG 零写不变）。"
        ))
    parser.add_argument("--tenant", required=True,
                        help="目标租户 ID（必填）")
    parser.add_argument("--dry-run", action="store_true",
                        help=("只打印计划，不写 PG"
                              "（读取编译 schema 仍可能写 Redis 缓存）"))
    parser.add_argument("--env-file", default=str(_REPO_ROOT / ".env.local"),
                        help="环境变量文件（默认仓库根 .env.local）")
    parser.add_argument("--no-env-file", action="store_true",
                        help="不自动加载环境变量文件")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.no_env_file:
        loaded = load_env_file(args.env_file)
        if loaded:
            logger.info("已加载环境变量文件: %s", loaded)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001 —— CLI 边界：打印可读错误并返回 1
        logger.error("回填失败: %s", exc)
        logger.debug("回填失败详情", exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
