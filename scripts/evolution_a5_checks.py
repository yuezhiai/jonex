#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演化台 A5 三项真机验收（同链并发采纳 / legacy 回滚 / smoke 失败自动恢复）。

在 125 部署环境（KB 容器内）对**指定编译链**跑三项真机检查，全部合成数据
在 `finally` 中清理，并把链恢复成脚本开始前的 active / 内容。

目标链（默认值，可用参数覆盖）：
    KB  2d3c7be1f32347c0b3ab96b511502e6c
    DS  67a69d92-3692-4ccc-919d-ceb6a3e482bb
    实体/属性 Circuits.circuitid（当前 constraints 长度 0）

三项：
    A5-1  同链并发第二个采纳必须 409/1003，且不产生第二个 active 版本；
    A5-2  回滚到手工插入的 legacy=1 版本：200、legacy=true、run rolled-back、
          新 active 的 source_version_id=legacy、内容 hash 与 legacy 一致；
    A5-3  目标语料 case 不可答 → smoke 失败：run smoke-failed、smoke_status=failed、
          reasons 含 target_not_pass、版本 rejected、内容恢复到采纳前、审计有
          `evolution.smoke_fail`。

用法（容器内）：
    python /tmp/evolution_a5_checks.py \
      --base-url http://gateway:8000/api/v1/knowledge-base \
      --tenant tenant_jonex_demo --kb <kb> --ds <ds> --user-id 1

说明：本数据源当前没有语料，A5-1 的采纳会因 smoke `skipped` 被默认阻止；
脚本在并发采纳请求里带 `override_reason`（显式放行 skipped），这是为了让
A5-1 真正走到「第一个采纳成功、第二个 409」的并发分支，不影响并发断言。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
import threading
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# 容器内 /app 是代码根；本地跑时回退到仓库根。
for _candidate in ("/app", str(Path(__file__).resolve().parents[1])):
    if _candidate and Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

DEFAULT_BASE_URL = "http://gateway:8000/api/v1/knowledge-base"
DEFAULT_TENANT = "tenant_jonex_demo"
DEFAULT_KB = "2d3c7be1f32347c0b3ab96b511502e6c"
DEFAULT_DS = "67a69d92-3692-4ccc-919d-ceb6a3e482bb"
DEFAULT_USER_ID = "1"
SCOPE = "compiled"


# ------------------------------------------------------------------ 基础件
class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}"
              + (f"  [{detail}]" if detail else ""))
        return bool(ok)

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [item for item in self.results if not item[1]]


def _http(base_url: str, method: str, path: str, *, token: str,
          tenant: str, body: dict | None = None,
          timeout: float = 330.0) -> tuple[int, dict]:
    """最小 HTTP 客户端：返回 (status, json)。非 2xx 也返回解析后的 body。"""
    url = base_url.rstrip("/") + path
    data = (json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else None)
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-ID": tenant,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read() or b"{}"
            return resp.status, _parse_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        return exc.code, _parse_json(raw)


def _parse_json(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"value": value}
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")[:800]}


def _make_token(tenant: str, user_id: str | int) -> str:
    """用 `get_config().JWT_SECRET` 现场签 30 分钟管理员 token。"""
    import jwt
    from jonex_core.common.config import get_config

    cfg = get_config()
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(user_id),
        "tenant_id": tenant,
        "username": f"a5_{user_id}",
        "role": "admin",
        "roles": ["admin"],
        "type": "user",
        "exp": now + datetime.timedelta(minutes=30),
        "iat": now,
    }
    return jwt.encode(payload, cfg.JWT_SECRET, algorithm=cfg.JWT_ALGORITHM)


def _json_list(value) -> list:
    """asyncpg 正常返回 list；驱动/序列化差异下可能是 JSON 字符串。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value or [])


def _as_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


async def _clear_cache(tenant: str, kb: str, ds: str) -> None:
    from jonex_core.common.cache import CacheUtil

    for key in (f"ontology:compiled:{tenant}:{kb}:{ds}",
                f"ontology:compiled:hash:{tenant}:{kb}:{ds}"):
        try:
            await CacheUtil.delete(key)
        except Exception as exc:  # noqa: BLE001 —— 缓存清理是旁路
            print(f"WARN  Redis 清理失败 {key}: {exc}")


# ------------------------------------------------------------------ DB 读
async def _read_content(session, tenant: str, kb: str, ds: str) -> dict:
    """直接读编译行内容（绕过 Redis 缓存，验收用真值）。"""
    from sqlalchemy import text

    row = (await session.execute(text(
        "SELECT entity_types, relation_types, constraints "
        "FROM knowledge_base.ontology_compiled_schemas "
        "WHERE tenant_id = :t AND knowledge_base_id = :kb "
        "AND data_source_id = :ds LIMIT 1"),
        {"t": tenant, "kb": kb, "ds": ds})).first()
    if row is None:
        return {}
    return {
        "entity_types": _json_list(row[0]),
        "relation_types": _json_list(row[1]),
        "constraints": _json_list(row[2]),
    }


def _content_snapshot(kb: str, ds: str, content: dict) -> dict:
    return {"scope": SCOPE, "kb": kb, "ds": ds, "content": content}


def _content_hash(kb: str, ds: str, content: dict) -> str:
    from capabilities.knowledge_base.services.evolution.version_store import (
        canonical_snapshot_hash,
    )
    return canonical_snapshot_hash(_content_snapshot(kb, ds, content))


async def _version_rows(session, tenant: str, kb: str, ds: str) -> list[dict]:
    from sqlalchemy import text

    rows = (await session.execute(text(
        "SELECT id, status, legacy, source_run_id, source_version_id, "
        "snapshot_hash, created_by, note FROM knowledge_base.semantic_versions "
        "WHERE tenant_id = :t AND knowledge_base_id = :kb AND scope = 'compiled' "
        "AND data_source_id = :ds ORDER BY created_at, id"),
        {"t": tenant, "kb": kb, "ds": ds})).all()
    return [{
        "id": row[0], "status": row[1], "legacy": int(row[2] or 0),
        "source_run_id": row[3], "source_version_id": row[4],
        "snapshot_hash": row[5], "created_by": row[6], "note": row[7],
    } for row in rows]


async def _get_run(session, tenant: str, run_id: str) -> dict | None:
    from sqlalchemy import text

    row = (await session.execute(text(
        "SELECT id, status, stage, smoke_status, smoke_mode, smoke, error, "
        "parent_version_id, adopted_version_id, rollback_reason, "
        "rollback_target_version_id, rollback_from_version_id, "
        "rollback_started_at, gate_decision, gate_effective "
        "FROM knowledge_base.evolution_runs "
        "WHERE tenant_id = :t AND id = :r"), {"t": tenant, "r": run_id})).first()
    if row is None:
        return None
    return {
        "run_id": row[0], "status": row[1], "stage": row[2],
        "smoke_status": row[3], "smoke_mode": row[4], "smoke": _as_json(row[5]),
        "error": row[6], "parent_version_id": row[7],
        "adopted_version_id": row[8], "rollback_reason": row[9],
        "rollback_target_version_id": row[10],
        "rollback_from_version_id": row[11],
        "rollback_started_at": row[12].isoformat() if row[12] else None,
        "gate_decision": row[13], "gate_effective": row[14],
    }


async def _audit_count(session, tenant: str, run_id: str, action: str) -> int:
    from sqlalchemy import text

    return int((await session.execute(text(
        "SELECT count(*) FROM platform.audit_logs WHERE tenant_id = :t "
        "AND resource = 'evolution_run' AND resource_id = :r "
        "AND action = :a"), {"t": tenant, "r": run_id, "a": action})).scalar() or 0)


# ------------------------------------------------------------------ DB 写
def _patch(kb: str, ds: str, run_id: str, target_code: str, expression: str,
           target_cases: list[str] | None = None) -> dict:
    return {
        "patch_id": f"patch_{run_id}",
        "kind": "query_rule",
        "kb": kb,
        "ds": ds,
        "entries": [{"target_code": target_code, "expression": expression}],
        "skipped": [],
        "target_cases": list(target_cases or []),
        "rollback": {"added": []},
    }


async def _insert_run(session, tenant: str, kb: str, ds: str, run_id: str,
                      parent_version_id: str, patch: dict,
                      gate_decision: str = "accept") -> None:
    from sqlalchemy import text

    await session.execute(text(
        """
        INSERT INTO knowledge_base.evolution_runs (
            id, tenant_id, knowledge_base_id, data_source_id, kind, status,
            stage, request, parent, report, gate, patch, backup, created_by,
            parent_version_id, gate_decision, legacy_adopt,
            created_at, updated_at
        ) VALUES (
            :id, :t, :kb, :ds, 'query_rule', 'evaluated', 'gated',
            '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
            CAST(:gate AS jsonb), CAST(:patch AS jsonb), '{}'::jsonb, 'a5',
            :parent_version_id, :gate_decision, 0, now(), now()
        )
        """), {"id": run_id, "t": tenant, "kb": kb, "ds": ds,
               "gate": json.dumps({"decision": gate_decision},
                                  ensure_ascii=False),
               "patch": json.dumps(patch, ensure_ascii=False),
               "parent_version_id": parent_version_id,
               "gate_decision": gate_decision})
    await session.commit()


async def _insert_legacy_version(session, tenant: str, kb: str, ds: str,
                                 snapshot: dict) -> tuple[str, str]:
    from sqlalchemy import text
    from capabilities.knowledge_base.services.evolution.version_store import (
        canonical_snapshot_hash,
    )

    version_id = f"ver_a5_legacy_{uuid.uuid4().hex}"
    snapshot_hash = canonical_snapshot_hash(snapshot)
    await session.execute(text(
        """
        INSERT INTO knowledge_base.semantic_versions (
            id, tenant_id, knowledge_base_id, scope, data_source_id,
            parent_version_id, source_run_id, source_version_id, snapshot,
            snapshot_hash, status, created_by, legacy, note,
            created_at, activated_at
        ) VALUES (
            :id, :t, :kb, 'compiled', :ds, NULL, NULL, NULL,
            CAST(:snapshot AS jsonb), :hash, 'superseded', 'a5', 1,
            'a5 legacy target', now(), NULL
        )
        """), {"id": version_id, "t": tenant, "kb": kb, "ds": ds,
               "snapshot": json.dumps(snapshot, ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")),
               "hash": snapshot_hash})
    await session.commit()
    return version_id, snapshot_hash


async def _insert_corpus_case(session, tenant: str, kb: str, ds: str,
                              name: str, question: str) -> str:
    from sqlalchemy import text

    case_id = uuid.uuid4().hex
    await session.execute(text(
        """
        INSERT INTO knowledge_base.evolution_corpus_cases (
            id, tenant_id, name, question, tier, expect, control,
            kb_id, ds_id, enabled, created_at, updated_at
        ) VALUES (
            :id, :t, :name, :question, 'core', 'answer',
            CAST(:control AS jsonb), :kb, :ds, 1, now(), now()
        )
        """), {"id": case_id, "t": tenant, "name": name,
               "question": question,
               "control": json.dumps(["SELECT 1"]),
               "kb": kb, "ds": ds})
    await session.commit()
    return case_id


def _pick_free_target(content: dict) -> str | None:
    """优先 Circuits.circuitid；已被 query_rule 占用时换一个空 constraints 的属性。"""
    entities = {str(e.get("name")): e
                for e in content.get("entity_types") or [] if isinstance(e, dict)}
    entity = entities.get("Circuits")
    if not entity:
        return None
    occupied = {
        str(c.get("target_code"))
        for c in content.get("constraints") or []
        if isinstance(c, dict)
        and str(c.get("constraint_type") or "") == "query_rule"
    }
    attrs = [str(a.get("name")) for a in entity.get("attributes") or []
             if isinstance(a, dict) and a.get("name")]
    ordered: list[str] = []
    if "circuitid" in attrs:
        ordered.append("Circuits.circuitid")
    ordered.extend(f"Circuits.{name}" for name in attrs
                   if f"Circuits.{name}" not in ordered)
    for code in ordered:
        if code not in occupied:
            return code
    return None


# ------------------------------------------------------------------ 清理
async def _restore_original_content(tenant: str, kb: str, ds: str,
                                    original_snapshot: dict,
                                    original_hash: str) -> str:
    """把编译行内容恢复成脚本开始前的快照；失败时回退直写 SQL。"""
    from jonex_core.common.database import get_db_session
    from capabilities.knowledge_base.services.evolution import schema_io

    errors: list[str] = []
    try:
        async with get_db_session() as session:
            await _clear_cache(tenant, kb, ds)
        version = {
            "scope": SCOPE, "kb": kb, "ds": ds,
            "snapshot": original_snapshot,
            "snapshot_hash": original_hash,
        }
        await schema_io.restore_snapshot(tenant, version)
        return ""
    except Exception as exc:  # noqa: BLE001 —— 尝试直写兜底
        errors.append(f"restore_snapshot: {exc}")
        traceback.print_exc()

    try:
        from sqlalchemy import text
        content = original_snapshot.get("content") or {}
        async with get_db_session() as session:
            await session.execute(text(
                "UPDATE knowledge_base.ontology_compiled_schemas SET "
                "entity_types = CAST(:e AS jsonb), "
                "relation_types = CAST(:r AS jsonb), "
                "constraints = CAST(:c AS jsonb), "
                "schema_version = schema_version + 1, updated_at = now() "
                "WHERE tenant_id = :t AND knowledge_base_id = :kb "
                "AND data_source_id = :ds"),
                {"e": json.dumps(content.get("entity_types") or [],
                                 ensure_ascii=False),
                 "r": json.dumps(content.get("relation_types") or [],
                                 ensure_ascii=False),
                 "c": json.dumps(content.get("constraints") or [],
                                 ensure_ascii=False),
                 "t": tenant, "kb": kb, "ds": ds})
            await session.commit()
        return ""
    except Exception as exc:  # noqa: BLE001
        errors.append(f"direct_restore: {exc}")
        traceback.print_exc()
    return "; ".join(errors)


async def _cleanup(tenant: str, kb: str, ds: str, state: dict,
                   checks: Checks) -> None:
    from jonex_core.common.database import get_db_session
    from sqlalchemy import text

    print("\n== 清理 ==")
    errors: list[str] = []

    # 1) 恢复脚本开始前的编译行内容（不依赖版本链是否还在）。
    restore_error = await _restore_original_content(
        tenant, kb, ds, state["original_snapshot"], state["original_hash"])
    if restore_error:
        errors.append(restore_error)

    # 2) 删除合成语料 case。
    case_ids = set(state.get("corpus_case_ids") or [])
    try:
        async with get_db_session() as session:
            rows = (await session.execute(text(
                "SELECT id FROM knowledge_base.evolution_corpus_cases "
                "WHERE tenant_id = :t AND name LIKE 'a5_smoke_fail_%'"),
                {"t": tenant})).scalars().all()
            case_ids.update(str(row) for row in rows)
            for case_id in case_ids:
                await session.execute(text(
                    "DELETE FROM knowledge_base.evolution_corpus_cases "
                    "WHERE tenant_id = :t AND id = :id"),
                    {"t": tenant, "id": case_id})
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"corpus_case: {exc}")
        traceback.print_exc()

    # 3) 删除本脚本的 run / logs / candidates / ledgers / audit。
    try:
        async with get_db_session() as session:
            rows = (await session.execute(text(
                "SELECT id FROM knowledge_base.evolution_runs "
                "WHERE tenant_id = :t AND (id LIKE 'run_a5_%' OR created_by = 'a5')"),
                {"t": tenant})).scalars().all()
            run_ids = set(str(row) for row in rows)
            run_ids.update(state.get("run_ids") or [])
            for run_id in run_ids:
                await session.execute(text(
                    "DELETE FROM knowledge_base.evolution_run_logs "
                    "WHERE run_id = :r"), {"r": run_id})
                await session.execute(text(
                    "DELETE FROM knowledge_base.evolution_candidates "
                    "WHERE run_id = :r"), {"r": run_id})
                await session.execute(text(
                    "DELETE FROM knowledge_base.evolution_ledgers "
                    "WHERE tenant_id = :t AND run_id = :r"),
                    {"t": tenant, "r": run_id})
                await session.execute(text(
                    "DELETE FROM platform.audit_logs WHERE tenant_id = :t "
                    "AND resource = 'evolution_run' AND resource_id = :r"),
                    {"t": tenant, "r": run_id})
                await session.execute(text(
                    "DELETE FROM knowledge_base.evolution_runs "
                    "WHERE tenant_id = :t AND id = :r"),
                    {"t": tenant, "r": run_id})
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"runs: {exc}")
        traceback.print_exc()

    # 4) 删除本脚本创建的版本（主表 + 归档表）。
    try:
        async with get_db_session() as session:
            rows = (await session.execute(text(
                "SELECT id FROM knowledge_base.semantic_versions "
                "WHERE tenant_id = :t AND knowledge_base_id = :kb "
                "AND scope = 'compiled' AND data_source_id = :ds "
                "AND (id LIKE 'ver_a5_%' OR source_run_id LIKE 'run_a5_%' "
                "OR created_by = 'a5')"),
                {"t": tenant, "kb": kb, "ds": ds})).scalars().all()
            version_ids = set(str(row) for row in rows)
            version_ids.update(state.get("version_ids") or [])
            for version_id in version_ids:
                for table in ("semantic_versions",
                              "semantic_versions_archive"):
                    await session.execute(text(
                        f"DELETE FROM knowledge_base.{table} "
                        "WHERE tenant_id = :t AND id = :id"),
                        {"t": tenant, "id": version_id})
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"versions: {exc}")
        traceback.print_exc()

    # 5) 恢复脚本开始前的 active 指针（开始前没有 active 时保持空）。
    original_active = state.get("original_active_id")
    if original_active:
        try:
            from capabilities.knowledge_base.services.evolution.version_store import (
                VersionStore,
            )
            async with get_db_session() as session:
                await VersionStore(session).activate(
                    tenant, original_active, previous_id=None)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"activate_original: {exc}")
            traceback.print_exc()

    # 6) 清缓存并核对。
    await _clear_cache(tenant, kb, ds)
    versions: list[dict] = []
    active: list[dict] = []
    content: dict = {}
    try:
        async with get_db_session() as session:
            versions = await _version_rows(session, tenant, kb, ds)
            active = [v for v in versions if v["status"] == "active"]
            content = await _read_content(session, tenant, kb, ds)
        chain_hash = _content_hash(kb, ds, content)
        expected_versions = state.get("original_version_count", 0)
        checks.check(
            "清理后链版本数恢复",
            len(versions) == expected_versions,
            f"n={len(versions)} expected={expected_versions}")
        checks.check(
            "清理后 active 指针恢复",
            (active[0]["id"] if active else None)
            == state.get("original_active_id"),
            f"active={active[0]['id'] if active else None} "
            f"expected={state.get('original_active_id')}")
        checks.check(
            "清理后 constraints 长度恢复",
            len(content.get("constraints") or [])
            == state.get("original_constraint_count"),
            f"n={len(content.get('constraints') or [])} "
            f"expected={state.get('original_constraint_count')}")
        checks.check(
            "清理后内容 hash 恢复",
            chain_hash == state.get("original_hash"),
            f"hash={chain_hash[:16]}… expected="
            f"{str(state.get('original_hash'))[:16]}…")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"verify: {exc}")
        traceback.print_exc()

    checks.check("清理完成", not errors, "; ".join(errors))
    print("清理后核对：")
    print(f"  链版本数 = {len(versions)}")
    print(f"  active   = {active[0]['id'] if active else None}")
    print(f"  constraints 长度 = {len(content.get('constraints') or [])}")


# ------------------------------------------------------------------ 主流程
async def _prepare(args, checks: Checks, state: dict) -> None:
    from jonex_core.common.database import get_db_session
    from capabilities.knowledge_base.services.evolution.schema_io import (
        snapshot_for,
    )
    from capabilities.knowledge_base.services.evolution.version_store import (
        VersionStore, canonical_snapshot_hash,
    )

    async with get_db_session() as session:
        await _clear_cache(args.tenant, args.kb, args.ds)
        original = await snapshot_for(args.tenant, "query_rule",
                                      args.kb, args.ds)
        if original.get("content") is None:
            raise RuntimeError("目标链的编译行内容为空，不能作为验收基线")
        state["original_snapshot"] = original
        state["original_hash"] = canonical_snapshot_hash(original)
        state["original_constraint_count"] = len(
            original["content"].get("constraints") or [])
        versions = await _version_rows(session, args.tenant, args.kb, args.ds)
        state["original_version_count"] = len(versions)
        active = [v for v in versions if v["status"] == "active"]
        state["original_active_id"] = active[0]["id"] if active else None
        state["version_ids"] = set()
        state["run_ids"] = []
        state["corpus_case_ids"] = []

    checks.check(
        "基线：目标链可读", True,
        f"versions={state['original_version_count']} "
        f"active={state['original_active_id']} "
        f"constraints={state['original_constraint_count']} "
        f"hash={state['original_hash'][:16]}…")


async def _ensure_active(args, checks: Checks, state: dict) -> str:
    """确保链有 active：开始前有则复用；没有则建 v0 并记录待清理。"""
    from jonex_core.common.database import get_db_session
    from capabilities.knowledge_base.services.evolution.schema_io import (
        snapshot_for,
    )
    from capabilities.knowledge_base.services.evolution.version_store import (
        VersionStore,
    )

    if state.get("original_active_id"):
        checks.check("A5-1 复用开始前的 active",
                     True, str(state["original_active_id"]))
        return str(state["original_active_id"])

    async with get_db_session() as session:
        snapshot = await snapshot_for(args.tenant, "query_rule",
                                      args.kb, args.ds)
        vs = VersionStore(session)
        v0 = await vs.create_candidate(
            args.tenant, args.kb, SCOPE, args.ds, snapshot,
            created_by="a5", note="a5 seed")
        await vs.activate(args.tenant, v0["version_id"], previous_id=None)
    state["version_ids"].add(v0["version_id"])
    checks.check("A5-1 seed v0（开始前无 active，本脚本创建）",
                 True, v0["version_id"])
    return v0["version_id"]


async def _a5_1(args, checks: Checks, state: dict, token: str) -> None:
    from jonex_core.common.database import get_db_session
    from capabilities.knowledge_base.services.evolution.version_store import (
        VersionStore,
    )

    print("\n== A5-1 同链并发第二个采纳 409 ==")
    active_id = await _ensure_active(args, checks, state)
    run_id = f"run_a5_concurrent_{uuid.uuid4().hex}"
    state["run_ids"].append(run_id)
    expression = (
        "circuitid 是电路唯一标识；仅在问题明确要求按 Circuit ID 精确过滤时"
        "作为过滤条件使用，不是整张表的默认过滤。")

    async with get_db_session() as session:
        await _insert_run(session, args.tenant, args.kb, args.ds, run_id,
                          active_id, _patch(args.kb, args.ds, run_id,
                                            "Circuits.circuitid", expression),
                          gate_decision="accept")
    checks.check("A5-1 插入 evaluated run", True, run_id)

    barrier = threading.Barrier(2)
    results: list[tuple[int, dict] | None] = [None, None]

    def worker(index: int, request_id: str) -> None:
        try:
            barrier.wait(timeout=30)
            results[index] = _http(
                args.base_url, "POST", f"/evolution/runs/{run_id}/adopt",
                token=token, tenant=args.tenant,
                body={
                    "confirm": True,
                    "request_id": request_id,
                    # 该 DS 当前无语料 → smoke skipped；显式放行以进入并发分支。
                    "override_reason": "a5 concurrent check: no corpus, "
                                       "smoke skipped",
                    "note": "a5 concurrent",
                })
        except Exception as exc:  # noqa: BLE001
            results[index] = (-1, {"error": f"{type(exc).__name__}: {exc}"})

    threads = [
        threading.Thread(target=worker, args=(0, "a5c1")),
        threading.Thread(target=worker, args=(1, "a5c2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=330)

    got = [item for item in results if item is not None]
    checks.check("A5-1 两个并发请求都有响应", len(got) == 2,
                 str([item[0] for item in got]))
    if len(got) != 2:
        return

    statuses = [item[0] for item in got]
    winners = [i for i, item in enumerate(got) if item[0] == 200]
    losers = [i for i, item in enumerate(got) if item[0] == 409]
    print(f"  HTTP statuses={statuses}")
    for i, (status, body) in enumerate(got):
        print(f"  req{i}: status={status} code={body.get('code')} "
              f"message={body.get('message')}")

    checks.check("A5-1 恰好一个 200", len(winners) == 1, str(statuses))
    checks.check("A5-1 另一个 409", len(losers) == 1, str(statuses))
    if not winners or not losers:
        return
    loser_body = got[losers[0]][1]
    checks.check("A5-1 409 code=1003", loser_body.get("code") == 1003,
                 f"code={loser_body.get('code')} "
                 f"message={loser_body.get('message')}")

    winner_body = got[winners[0]][1]
    winner_data = winner_body.get("data") or {}
    winner_version = winner_data.get("version_id")
    winner_run = winner_data.get("run") or {}
    checks.check("A5-1 赢家 run.status=adopted",
                 winner_run.get("status") == "adopted",
                 str(winner_run.get("status")))
    checks.check("A5-1 赢家返回 version_id", bool(winner_version),
                 str(winner_version))

    async with get_db_session() as session:
        versions = await _version_rows(session, args.tenant, args.kb, args.ds)
        active = [v for v in versions if v["status"] == "active"]
        same_run = [v for v in versions if v["source_run_id"] == run_id]
        run = await _get_run(session, args.tenant, run_id)
    checks.check("A5-1 链上只有一个 active", len(active) == 1,
                 str([v["id"] for v in active]))
    checks.check("A5-1 active 是赢家版本",
                 len(active) == 1 and active[0]["id"] == winner_version,
                 f"active={active[0]['id'] if active else None} "
                 f"winner={winner_version}")
    checks.check("A5-1 并发只产生一个版本", len(same_run) == 1,
                 f"n={len(same_run)}")
    checks.check("A5-1 run 行最终 adopted",
                 bool(run) and run.get("status") == "adopted",
                 str(run.get("status") if run else None))

    state["a51_run_id"] = run_id
    state["a51_version_id"] = winner_version
    state["version_ids"].add(winner_version)
    for version in same_run:
        state["version_ids"].add(version["id"])


async def _a5_2(args, checks: Checks, state: dict, token: str) -> None:
    from jonex_core.common.database import get_db_session
    from capabilities.knowledge_base.services.evolution.version_store import (
        VersionStore,
    )

    print("\n== A5-2 回滚到 legacy=1 版本 ==")
    run_id = state.get("a51_run_id")
    if not run_id:
        checks.check("A5-2 前置：存在已采纳 run", False, "A5-1 未产出")
        return

    async with get_db_session() as session:
        current = await VersionStore(session).active_for(
            args.tenant, args.kb, SCOPE, args.ds)
        if not current or not current.get("snapshot"):
            checks.check("A5-2 前置：当前 active 快照可读", False,
                         str(current))
            return
        legacy_id, legacy_hash = await _insert_legacy_version(
            session, args.tenant, args.kb, args.ds, current["snapshot"])
    state["version_ids"].add(legacy_id)
    state["a52_legacy_id"] = legacy_id
    checks.check("A5-2 插入 legacy=1 版本", True,
                 f"{legacy_id} hash={legacy_hash[:16]}… "
                 f"(snapshot 复制自 active {current['version_id']})")

    status, body = _http(
        args.base_url, "POST", f"/evolution/runs/{run_id}/rollback",
        token=token, tenant=args.tenant,
        body={"to_version_id": legacy_id,
              "reason": "a5 legacy rollback"})
    data = body.get("data") or {}
    print(f"  rollback HTTP {status} code={body.get('code')} "
          f"message={body.get('message')}")
    checks.check("A5-2 rollback HTTP 200", status == 200, f"status={status}")
    checks.check("A5-2 响应 legacy=true", data.get("legacy") is True,
                 str(data.get("legacy")))
    checks.check("A5-2 响应 target=legacy",
                 data.get("target_version_id") == legacy_id,
                 f"{data.get('target_version_id')} vs {legacy_id}")
    rolled_run = data.get("run") or {}
    checks.check("A5-2 run.status=rolled-back",
                 rolled_run.get("status") == "rolled-back",
                 str(rolled_run.get("status")))

    async with get_db_session() as session:
        new_active = await VersionStore(session).active_for(
            args.tenant, args.kb, SCOPE, args.ds)
        content = await _read_content(session, args.tenant, args.kb, args.ds)
        run = await _get_run(session, args.tenant, run_id)
    content_hash = _content_hash(args.kb, args.ds, content)
    checks.check("A5-2 新 active source_version_id=legacy",
                 bool(new_active)
                 and new_active.get("source_version_id") == legacy_id,
                 f"{new_active.get('source_version_id') if new_active else None}")
    checks.check("A5-2 新 active snapshot_hash=legacy",
                 bool(new_active)
                 and new_active.get("snapshot_hash") == legacy_hash,
                 f"{new_active.get('snapshot_hash') if new_active else None}")
    checks.check("A5-2 线上内容 hash=legacy",
                 content_hash == legacy_hash,
                 f"{content_hash[:16]}…")
    checks.check("A5-2 run 行最终 rolled-back",
                 bool(run) and run.get("status") == "rolled-back",
                 str(run.get("status") if run else None))
    checks.check("A5-2 run.rollback_reason 落库",
                 bool(run) and run.get("rollback_reason") == "a5 legacy rollback",
                 str(run.get("rollback_reason") if run else None))
    if new_active:
        state["a52_version_id"] = new_active.get("version_id")
        state["version_ids"].add(new_active.get("version_id"))


async def _a5_3(args, checks: Checks, state: dict, token: str) -> None:
    from jonex_core.common.database import get_db_session
    from capabilities.knowledge_base.services.evolution.version_store import (
        VersionStore,
    )

    print("\n== A5-3 smoke 失败自动恢复 ==")
    async with get_db_session() as session:
        active = await VersionStore(session).active_for(
            args.tenant, args.kb, SCOPE, args.ds)
        content_before = await _read_content(session, args.tenant, args.kb,
                                             args.ds)
    if not active:
        checks.check("A5-3 前置：存在 active 版本", False, "无 active")
        return
    active_before_id = active["version_id"]
    before_hash = _content_hash(args.kb, args.ds, content_before)
    target = _pick_free_target(content_before)
    checks.check("A5-3 选择空 constraints 的属性", bool(target),
                 f"target={target} "
                 f"constraints={len(content_before.get('constraints') or [])}")
    if not target:
        return

    case_name = f"a5_smoke_fail_{uuid.uuid4().hex}"
    question = "请给出一个绝对无法回答的验收问句 XYZABC123？"
    async with get_db_session() as session:
        case_id = await _insert_corpus_case(
            session, args.tenant, args.kb, args.ds, case_name, question)
    state["corpus_case_ids"].append(case_id)
    checks.check("A5-3 插入合成语料 case", True,
                 f"id={case_id} name={case_name}")

    run_id = f"run_a5_smoke_{uuid.uuid4().hex}"
    state["run_ids"].append(run_id)
    expression = (
        f"{target.rsplit('.', 1)[-1]} 是 Circuits 的列；仅在问题明确要求按"
        "该列过滤时使用，不是整张表的默认过滤。")
    async with get_db_session() as session:
        await _insert_run(
            session, args.tenant, args.kb, args.ds, run_id,
            active["version_id"],
            _patch(args.kb, args.ds, run_id, target, expression,
                   target_cases=[case_name]),
            gate_decision="accept")
    checks.check("A5-3 插入 evaluated run（target_cases=合成 case）", True,
                 run_id)

    status, body = _http(
        args.base_url, "POST", f"/evolution/runs/{run_id}/adopt",
        token=token, tenant=args.tenant,
        body={"confirm": True,
              "request_id": f"a5_smoke_{uuid.uuid4().hex[:16]}",
              "note": "a5 smoke fail"})
    print(f"  adopt HTTP {status} code={body.get('code')} "
          f"message={body.get('message')}")
    checks.check("A5-3 adopt 预期 smoke 失败（409）", status == 409,
                 f"status={status} code={body.get('code')}")

    async with get_db_session() as session:
        run = await _get_run(session, args.tenant, run_id)
        content_after = await _read_content(session, args.tenant, args.kb,
                                            args.ds)
        versions = await _version_rows(session, args.tenant, args.kb, args.ds)
        active_after = await VersionStore(session).active_for(
            args.tenant, args.kb, SCOPE, args.ds)
        audit_smoke_fail = await _audit_count(
            session, args.tenant, run_id, "evolution.smoke_fail")
    after_hash = _content_hash(args.kb, args.ds, content_after)
    smoke = (run or {}).get("smoke") or {}
    reasons = smoke.get("reasons") or []
    reason_codes = [str(item.get("reason")) for item in reasons
                    if isinstance(item, dict)]
    same_run_versions = [v for v in versions
                         if v["source_run_id"] == run_id]
    smoke_case = (smoke.get("cases") or [{}])[0] if smoke.get("cases") else {}

    print(f"  run.status={run.get('status') if run else None} "
          f"smoke_status={run.get('smoke_status') if run else None} "
          f"reasons={reason_codes}")
    print(f"  smoke case: outcome={smoke_case.get('outcome')} "
          f"reason_code={smoke_case.get('reason_code')} "
          f"why={smoke_case.get('why')}")
    print(f"  content hash before={before_hash[:16]}… "
          f"after={after_hash[:16]}…")

    checks.check("A5-3 run.status=smoke-failed",
                 bool(run) and run.get("status") == "smoke-failed",
                 str(run.get("status") if run else None))
    checks.check("A5-3 smoke_status=failed",
                 bool(run) and run.get("smoke_status") == "failed",
                 str(run.get("smoke_status") if run else None))
    checks.check("A5-3 smoke.reasons 含 target_not_pass",
                 "target_not_pass" in reason_codes, str(reason_codes))
    checks.check("A5-3 本次版本 rejected",
                 len(same_run_versions) == 1
                 and same_run_versions[0]["status"] == "rejected",
                 str([(v["id"], v["status"]) for v in same_run_versions]))
    checks.check("A5-3 内容恢复到采纳前",
                 after_hash == before_hash,
                 f"{after_hash[:16]}… vs {before_hash[:16]}…")
    checks.check("A5-3 审计 evolution.smoke_fail", audit_smoke_fail >= 1,
                 f"n={audit_smoke_fail}")
    checks.check("A5-3 线上 active 未被 smoke 失败推进",
                 bool(active_after)
                 and active_after.get("version_id") == active_before_id,
                 f"active={active_after.get('version_id') if active_after else None} "
                 f"expected={active_before_id}")

    for version in same_run_versions:
        state["version_ids"].add(version["id"])
    state["a53_run_id"] = run_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="演化台 A5 三项真机验收")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    parser.add_argument("--kb", default=DEFAULT_KB)
    parser.add_argument("--ds", default=DEFAULT_DS)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--keep", action="store_true",
                        help="跳过清理（仅排障；默认清理）")
    return parser


async def run(args: argparse.Namespace) -> int:
    from jonex_core.common.tenant import require_tenant

    tenant = require_tenant(args.tenant)
    args.tenant = tenant
    checks = Checks()
    state: dict = {}
    token = _make_token(tenant, args.user_id)

    try:
        await _prepare(args, checks, state)
        await _a5_1(args, checks, state, token)
        await _a5_2(args, checks, state, token)
        await _a5_3(args, checks, state, token)
    except Exception as exc:  # noqa: BLE001 —— 保留现场并继续清理
        print(f"FAIL  验收脚本异常: {exc}", file=sys.stderr)
        traceback.print_exc()
        checks.check("验收脚本未抛异常", False,
                     f"{type(exc).__name__}: {exc}")
    finally:
        if args.keep:
            print("\n--keep：跳过清理。保留状态：")
            print(json.dumps({
                "run_ids": state.get("run_ids"),
                "version_ids": sorted(state.get("version_ids") or []),
                "corpus_case_ids": state.get("corpus_case_ids"),
                "original_active_id": state.get("original_active_id"),
                "original_hash": state.get("original_hash"),
            }, ensure_ascii=False, indent=2))
        elif state.get("original_snapshot") is not None:
            await _cleanup(tenant, args.kb, args.ds, state, checks)
        else:
            checks.check("清理完成", False, "基线未建立，未执行清理")

    failed = checks.failed
    print(f"\n== 结果：{len(checks.results) - len(failed)}/"
          f"{len(checks.results)} PASS ==")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL {name} {detail}")
        return 1
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001 —— CLI 边界
        print(f"FAIL  脚本启动异常: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
