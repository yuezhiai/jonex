#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演化台信任闭环 — 可重复集成验收脚本（A2）。

在指定**已存在**的 KB 上建一条合成 compiled 版本链（`ds` 必填、默认
`ds_trustloop_check`），走一遍真实 HTTP 链路：

    preview（gate=reject / override_required）
      → 无 override 采纳 409/1003
      → override 采纳 202（受理；后台 apply / smoke / 完整语料基准）
      → 同 request_id 立即重放（202 受理重放 / 200 已收口，幂等不新建任务）
      → 轮询 GET /evolution/runs/{id} 至离开 adopting
      → 空语料 + override：bench_status=skipped、终态 adopted
      → 不同 request_id 409
      → 版本列表 / 详情（默认不含 snapshot）
      → 回滚到 v0（省略 target = run.parent_version_id）
      → ad-hoc 支路（202 受理 → 轮询回 evaluated / gate_adhoc=reject）

任务 11 起采纳是**异步**的：`confirm=true` 返回 202 + `accepted=true`，客户端
必须轮询 run 详情。A2 的合成链没有语料，所以每次采纳都显式带
`override_reason`（空语料默认阻止、override 放行），并断言收口时
`bench_status="skipped"`。

全部数据在 `try/finally` 中清理（版本 / 归档 / 日志 / 候选 / 台账 / run /
本 run 审计 / 合成编译行 / Redis 缓存 / 合成的 Neo4j 数据源子图）；
`--keep` 时跳过清理并打印保留清单。

⚠️ 轮询超时（600s）时后台可能仍在 `adopting`：此时 `finally` 清理会与后台
采纳并发（删合成链 vs 后台 apply）。脚本**不改变**这个语义；出现超时应先按
`--keep` 保留现场、确认 run 已收口，再手工清理。

用法：

    python scripts/evolution_trust_loop_check.py \
      --tenant tenant_jonex_demo --kb kb_assets_r2 --ds ds_trustloop_check

前置：迁移 016/017/024/025 已应用；API 网关可达；本机 demo 库可直接跑通。
所有 DB 访问走 `get_db_session()`，SQL 全部参数化；不使用 `shell=True`。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1/knowledge-base"
ENTITY_NAME = "T"
ATTR_NAME = "a"
#: 合成链没有语料：每次采纳都显式放行（同时验证「空语料默认阻止、override 放行」）。
OVERRIDE_REASON = "A2 验收：空语料/门禁显式放行"
#: 轮询采纳收口的默认上限（秒）；任务简报固定 600。
POLL_TIMEOUT_SECONDS = 600.0


def load_env_file(path: str) -> str:
    """加载 shell 风格 env 文件（已存在的环境变量不覆盖）。返回实际加载路径。"""
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
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
    return str(file_path)


def _normalized_entity() -> dict:
    """与 `OntologyCompiler._normalize_entity_types` 同形状的合成实体。

    v0 快照必须存**归一化后**的内容：`restore_snapshot` 写回时还会再归一化
    一次，raw 形状会在回滚 hash 校验时对不上。
    """
    return {
        "name": ENTITY_NAME,
        "display_name": ENTITY_NAME,
        "description": "",
        "requirement": "",
        "status": "active",
        "aliases": [],
        "source_object_id": None,
        "attributes": [{
            "name": ATTR_NAME,
            "display_name": ATTR_NAME,
            "description": "",
            "type": "string",
            "required": False,
            "is_primary_key": False,
            "source_attribute_id": None,
        }],
        "extraction_skills": None,
    }


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
          timeout: float = 180.0) -> tuple[int, dict]:
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
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw.decode("utf-8", "replace")[:500]}
        return exc.code, payload


def _bundle(call, run_id: str) -> tuple[int, dict, dict, dict]:
    """读 RunBundle：返回 (HTTP status, data, run, 完整 body)。"""
    status, body = call("GET", f"/evolution/runs/{run_id}")
    data = body.get("data") or {}
    return status, data, (data.get("run") or {}), body


def poll_run(call, run_id: str, timeout: float = POLL_TIMEOUT_SECONDS,
             interval: float = 2.0,
             max_failures: int = 10) -> tuple[int, dict, dict]:
    """轮询运行详情直到 `status != "adopting"`（或超时）。

    返回最后一次 `(status, data, run)`；调用方自己断言终态 —— 超时也原样返回，
    绝不能把「还在 adopting」当成成功。连续 `max_failures` 次非 200（鉴权/路由/
    服务未就绪）则立刻抛错并带上最后一次 status/body，不硬耗满 600s。

    ⚠️ 若**超时**返回时后台仍在 `adopting`，调用方的 `finally` 清理可能与后台
    采纳并发（清理会删合成链、后台还在 apply）。发现超时后应先用 `--keep`
    保留现场并确认 run 收口，再决定是否清理；本函数不改这个语义。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    failures = 0
    while True:
        status, data, run, body = _bundle(call, run_id)
        state = run.get("status")
        if status == 200 and state and state != "adopting":
            return status, data, run
        if status != 200:
            failures += 1
            if failures >= max(1, int(max_failures)):
                detail = json.dumps(body, ensure_ascii=False)[:300]
                raise RuntimeError(
                    f"轮询 GET /evolution/runs/{run_id} 连续 {failures} 次"
                    f"非 200（最后 status={status}，body={detail}）")
        else:
            failures = 0
        if time.monotonic() >= deadline:
            return status, data, run
        time.sleep(interval)


def _make_token(tenant: str, user_id: str | int) -> str:
    """用 `get_config().JWT_SECRET` 现场签 30 分钟管理员 token。"""
    import jwt
    from jonex_core.common.config import get_config

    cfg = get_config()
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(user_id),
        "tenant_id": tenant,
        "username": f"trustloop_{user_id}",
        "role": "admin",
        "roles": ["admin"],
        "type": "user",
        "exp": now + datetime.timedelta(minutes=30),
        "iat": now,
    }
    return jwt.encode(payload, cfg.JWT_SECRET, algorithm=cfg.JWT_ALGORITHM)


async def _find_admin_user(session, tenant: str) -> str | None:
    from sqlalchemy import text

    row = (await session.execute(text(
        """
        SELECT u.id
        FROM platform.users u
        JOIN platform.user_roles ur ON ur.user_id = u.id
        JOIN platform.roles r ON r.id = ur.role_id
        JOIN platform.role_permissions rp ON rp.role_id = r.id
        JOIN platform.permissions p ON p.id = rp.permission_id
        WHERE u.tenant_id = :tenant AND p.code = 'tenant:admin'
        ORDER BY u.id
        LIMIT 1
        """), {"tenant": tenant})).first()
    return str(row[0]) if row else None


async def _kb_exists(session, tenant: str, kb: str) -> bool:
    from sqlalchemy import text

    row = (await session.execute(text(
        "SELECT 1 FROM knowledge_base.knowledge_info "
        "WHERE tenant_id = :t AND id = :kb AND is_deleted = 0 LIMIT 1"),
        {"t": tenant, "kb": kb})).first()
    return row is not None


async def _precondition(session, tenant: str, kb: str, ds: str) -> str | None:
    """安全前置：目标链已有编译行/版本行 → 返回拒绝原因。"""
    from sqlalchemy import text

    compiled = (await session.execute(text(
        "SELECT 1 FROM knowledge_base.ontology_compiled_schemas "
        "WHERE tenant_id = :t AND knowledge_base_id = :kb "
        "AND data_source_id = :ds LIMIT 1"),
        {"t": tenant, "kb": kb, "ds": ds})).first()
    if compiled is not None:
        return (f"({kb}, {ds}) 已有 ontology_compiled_schemas 行；"
                f"请换一个 --ds（如 ds_trustloop_check_{uuid.uuid4().hex[:6]}）")
    for table in ("semantic_versions", "semantic_versions_archive"):
        versions = (await session.execute(text(
            f"SELECT 1 FROM knowledge_base.{table} "
            "WHERE tenant_id = :t AND knowledge_base_id = :kb "
            "AND scope = 'compiled' AND data_source_id = :ds LIMIT 1"),
            {"t": tenant, "kb": kb, "ds": ds})).first()
        if versions is not None:
            return (f"({kb}, {ds}) 已有 {table} 行；"
                    f"请换一个 --ds（如 ds_trustloop_check_"
                    f"{uuid.uuid4().hex[:6]}）")
    return None


async def _clear_redis(tenant: str, kb: str, ds: str) -> None:
    from jonex_core.common.cache import CacheUtil

    for key in (f"ontology:compiled:{tenant}:{kb}:{ds}",
                f"ontology:compiled:hash:{tenant}:{kb}:{ds}"):
        try:
            await CacheUtil.delete(key)
        except Exception as exc:  # noqa: BLE001 —— 缓存清理是旁路
            print(f"WARN  Redis 清理失败 {key}: {exc}")


def _base_patch(kb: str, ds: str, run_id: str,
                candidate_evidence: dict | None = None) -> dict:
    patch = {
        "patch_id": f"patch_{run_id}",
        "kind": "query_rule",
        "kb": kb,
        "ds": ds,
        "entries": [{
            "target_code": f"{ENTITY_NAME}.{ATTR_NAME}",
            "expression": f"{ATTR_NAME} IS NOT NULL",
        }],
        "skipped": [],
        "target_cases": [],
        "rollback": {"added": []},
    }
    if candidate_evidence is not None:
        patch["candidate"] = {"evidence": candidate_evidence}
    return patch


async def _insert_run(session, tenant: str, kb: str, ds: str, run_id: str,
                      parent_version_id: str, patch: dict,
                      gate_decision: str) -> None:
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
            '{}'::jsonb, '{}'::jsonb, CAST(:report AS jsonb),
            CAST(:gate AS jsonb), CAST(:patch AS jsonb), '{}'::jsonb, 'check',
            :parent_version_id, :gate_decision, 0, now(), now()
        )
        """), {"id": run_id, "t": tenant, "kb": kb, "ds": ds,
               "report": json.dumps({"target_cases": []}, ensure_ascii=False),
               "gate": json.dumps({"decision": gate_decision},
                                  ensure_ascii=False),
               "patch": json.dumps(patch, ensure_ascii=False),
               "parent_version_id": parent_version_id,
               "gate_decision": gate_decision})
    await session.commit()


async def _seed(session, tenant: str, kb: str, ds: str,
                run_id: str) -> tuple[dict, str]:
    """建合成编译行 + v0 版本 + 一条 evaluated run；返回 (v0, run_id)。"""
    from sqlalchemy import text

    from capabilities.knowledge_base.services.evolution.version_store import (
        VersionStore,
    )

    entity = _normalized_entity()
    await session.execute(text(
        """
        INSERT INTO knowledge_base.ontology_compiled_schemas (
            tenant_id, knowledge_base_id, data_source_id, source_type,
            source_version, schema_version, entity_types, relation_types,
            constraints, disambiguation, prompt_schema, status, schema_mode,
            sync_status, compiled_at, created_at, updated_at
        ) VALUES (
            :t, :kb, :ds, 'manual', 1, 1,
            CAST(:entities AS jsonb), '[]'::jsonb, '[]'::jsonb,
            '{"case_insensitive": true, "alias_merge": true}'::jsonb,
            '{}'::jsonb, 'active', 'manual_edited', 'synced',
            now(), now(), now()
        )
        """), {"t": tenant, "kb": kb, "ds": ds,
               "entities": json.dumps([entity], ensure_ascii=False)})
    await session.commit()

    # 读行 → v0 快照 → candidate → activate。
    from capabilities.knowledge_base.services.evolution.schema_io import (
        snapshot_for,
    )

    snapshot = await snapshot_for(tenant, "query_rule", kb, ds)
    if snapshot.get("content") is None:
        raise RuntimeError("合成编译行读取失败：快照内容为空")
    versions = VersionStore(session)
    v0 = await versions.create_candidate(
        tenant, kb, "compiled", ds, snapshot,
        created_by="check", note="v0 seed")
    await versions.activate(tenant, v0["version_id"], previous_id=None)

    await _insert_run(session, tenant, kb, ds, run_id, v0["version_id"],
                      _base_patch(kb, ds, run_id), "reject")
    return v0, run_id


def _json_list(value) -> list:
    """asyncpg 正常返回 list；驱动/序列化差异下可能是 JSON 字符串。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value or [])


async def _read_content(session, tenant: str, kb: str, ds: str) -> dict:
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


async def _cleanup(session, tenant: str, kb: str, ds: str,
                   run_ids: list[str], *, created: dict,
                   allow_neo4j: bool) -> list[str]:
    """按外键顺序清理本脚本产生的全部数据；返回清理错误清单。"""
    from sqlalchemy import text

    errors: list[str] = []
    statements = [
        ("evolution_run_logs",
         "DELETE FROM knowledge_base.evolution_run_logs WHERE run_id = :run"),
        ("evolution_candidates",
         "DELETE FROM knowledge_base.evolution_candidates WHERE run_id = :run"),
        ("evolution_ledgers",
         "DELETE FROM knowledge_base.evolution_ledgers "
         "WHERE tenant_id = :t AND run_id = :run"),
        ("audit_logs",
         "DELETE FROM platform.audit_logs WHERE tenant_id = :t "
         "AND resource = 'evolution_run' AND resource_id = :run"),
        ("evolution_runs",
         "DELETE FROM knowledge_base.evolution_runs "
         "WHERE tenant_id = :t AND id = :run"),
    ]
    for name, sql in statements:
        for run_id in run_ids:
            try:
                await session.execute(text(sql), {"t": tenant, "run": run_id})
                await session.commit()
            except Exception as exc:  # noqa: BLE001 —— 记录后继续清理其余表
                await session.rollback()
                errors.append(f"{name}: {exc}")

    if created.get("compiled"):
        for table in ("semantic_versions", "semantic_versions_archive"):
            try:
                await session.execute(text(
                    f"DELETE FROM knowledge_base.{table} WHERE tenant_id = :t "
                    "AND knowledge_base_id = :kb AND scope = 'compiled' "
                    "AND data_source_id = :ds"),
                    {"t": tenant, "kb": kb, "ds": ds})
                await session.commit()
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                errors.append(f"{table}: {exc}")
        try:
            await session.execute(text(
                "DELETE FROM knowledge_base.ontology_compiled_schemas "
                "WHERE tenant_id = :t AND knowledge_base_id = :kb "
                "AND data_source_id = :ds"),
                {"t": tenant, "kb": kb, "ds": ds})
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            errors.append(f"ontology_compiled_schemas: {exc}")

    await _clear_redis(tenant, kb, ds)
    # 合成编译行会触发 Neo4j 数据资产投影。**只允许删以 `ds_trustloop_`
    # 开头的合成 DS**，避免误删真实派生投影；其它名字需显式 --force-neo4j。
    if allow_neo4j:
        try:
            from capabilities.knowledge_base.services.ontology_data_projection import (
                project_source_delete_best_effort,
            )
            await project_source_delete_best_effort(tenant, kb, ds)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN  合成数据源图清理失败 ds={ds}: {exc}")
    else:
        print(f"SKIP  Neo4j 清理（ds={ds!r} 不以 ds_trustloop_ 开头；"
              "确需清理请加 --force-neo4j）")
    return errors


async def run(args: argparse.Namespace) -> int:
    from jonex_core.common.tenant import require_tenant

    tenant = require_tenant(args.tenant)
    kb = str(args.kb).strip()
    ds = str(args.ds).strip()
    if not kb or not ds:
        print("FAIL  --kb / --ds 不能为空", file=sys.stderr)
        return 2

    checks = Checks()
    created = {"compiled": False}
    run_id = f"run_check_{uuid.uuid4().hex[:12]}"
    run_ids = [run_id]
    adopt_request_id = f"adopt_{run_id}_{uuid.uuid4().hex[:6]}"

    from jonex_core.common.database import get_db_session

    async with get_db_session() as session:
        if not await _kb_exists(session, tenant, kb):
            print(f"FAIL  KB 不存在或已删除: tenant={tenant} kb={kb}",
                  file=sys.stderr)
            return 2
        refusal = await _precondition(session, tenant, kb, ds)
        if refusal:
            print(f"REFUSE  {refusal}", file=sys.stderr)
            return 2
        user_id = args.user_id or await _find_admin_user(session, tenant)
        if not user_id:
            print("FAIL  找不到持有 tenant:admin 的用户；请用 --user-id 指定",
                  file=sys.stderr)
            return 2

    try:
        async with get_db_session() as session:
            await _clear_redis(tenant, kb, ds)
            # 前置已确认该链没有任何行：先置位，保证 _seed 半途失败也能清干净。
            created["compiled"] = True
            v0, _ = await _seed(session, tenant, kb, ds, run_id)
        checks.check("seed 合成编译行 + v0 + evaluated run", True,
                     f"v0={v0['version_id']} run={run_id}")

        token = _make_token(tenant, user_id)

        def call(method: str, path: str, body: dict | None = None):
            return _http(args.base_url, method, path, token=token,
                         tenant=tenant, body=body)

        # ---- 1) preview：gate=reject，必须 override
        status, body = call("POST", f"/evolution/runs/{run_id}/adopt",
                            {"confirm": False})
        data = body.get("data") or {}
        checks.check("preview HTTP 200", status == 200, f"status={status}")
        checks.check("preview gate_effective=reject",
                     data.get("gate_effective") == "reject",
                     str(data.get("gate_effective")))
        checks.check("preview override_required=true",
                     data.get("override_required") is True,
                     str(data.get("override_required")))

        # ---- 2) 无 override 采纳：409 / 1003
        status, body = call("POST", f"/evolution/runs/{run_id}/adopt",
                            {"confirm": True, "note": "check"})
        checks.check("adopt 无 override → 409", status == 409,
                     f"status={status}")
        checks.check("adopt 无 override → 1003",
                     body.get("code") == 1003, f"code={body.get('code')}")

        # ---- 3) override 采纳：202 受理（后台跑）+ 轮询收口
        adopt_body = {
            "confirm": True,
            "request_id": adopt_request_id,
            "note": "trust-loop check",
            "override_reason": OVERRIDE_REASON,
        }
        status, body = call("POST", f"/evolution/runs/{run_id}/adopt",
                            adopt_body)
        data = body.get("data") or {}
        checks.check("adopt override HTTP 200/202", status in (200, 202),
                     f"status={status} message={body.get('message')}")
        checks.check("adopt accepted=true", data.get("accepted") is True,
                     str(data.get("accepted")))
        checks.check("adopt status=adopting",
                     data.get("status") == "adopting", str(data.get("status")))
        checks.check("adopt run_id 一致", data.get("run_id") == run_id,
                     f"{data.get('run_id')} vs {run_id}")
        checks.check("adopt bench_status=pending",
                     data.get("bench_status") == "pending",
                     str(data.get("bench_status")))
        checks.check("adopt gate_effective=reject(override)",
                     data.get("gate_effective") == "reject"
                     and data.get("override") is True,
                     f"effective={data.get('gate_effective')} "
                     f"override={data.get('override')}")

        # ---- 3b) 202 后**立即**同 request_id 重放：幂等且不新建任务
        #     后台可能已经收口：202（仍在受理）与 200（已终态）都合法。
        replay_status, replay_body = call(
            "POST", f"/evolution/runs/{run_id}/adopt", adopt_body)
        replay_data = replay_body.get("data") or {}
        replay_run = replay_data.get("run") or {}
        replay_run_id = (replay_data.get("run_id")
                         or replay_run.get("run_id"))
        checks.check("重放 HTTP 200/202", replay_status in (200, 202),
                     f"status={replay_status} "
                     f"message={replay_body.get('message')}")
        checks.check("重放 idempotent_replay=true",
                     replay_data.get("idempotent_replay") is True,
                     str(replay_data.get("idempotent_replay")))
        checks.check("重放 run_id 不变", replay_run_id == run_id,
                     f"{replay_run_id} vs {run_id}")
        if replay_status == 202:
            checks.check("重放仍在受理（202 accepted/adopting）",
                         replay_data.get("accepted") is True
                         and replay_data.get("status") == "adopting",
                         f"accepted={replay_data.get('accepted')} "
                         f"status={replay_data.get('status')}")
        else:
            replay_terminal = (replay_run.get("status")
                               or replay_data.get("status"))
            checks.check("重放已收口（200 终态，未重复采纳）",
                         replay_data.get("accepted") is not True
                         and replay_terminal in ("adopted", "smoke-failed"),
                         f"accepted={replay_data.get('accepted')} "
                         f"status={replay_terminal}")

        # ---- 3c) 轮询收口：空语料 + override → bench skipped / adopted
        poll_status, bundle, adopted_run = poll_run(call, run_id)
        terminal = adopted_run.get("status")
        bench = bundle.get("bench") if isinstance(bundle.get("bench"), dict) else {}
        checks.check("轮询 GET run HTTP 200", poll_status == 200,
                     f"status={poll_status}")
        checks.check("轮询离开 adopting", bool(terminal)
                     and terminal != "adopting", str(terminal))
        checks.check("采纳终态 adopted", terminal == "adopted", str(terminal))
        checks.check("空语料 bench_status=skipped",
                     bundle.get("bench_status") == "skipped",
                     str(bundle.get("bench_status")))
        checks.check("空语料 bench.reasons 含 corpus_empty",
                     "corpus_empty" in (bench.get("reasons") or []),
                     str(bench.get("reasons")))
        checks.check("override 事实落库",
                     bool((bundle.get("override") or {}).get("used")),
                     str(bundle.get("override")))
        checks.check("adopt legacy_adopt=0",
                     int(adopted_run.get("legacy_adopt") or 0) == 0,
                     str(adopted_run.get("legacy_adopt")))
        checks.check("重放未改写 adopt_request_id",
                     adopted_run.get("adopt_request_id") == adopt_request_id,
                     f"{adopted_run.get('adopt_request_id')} "
                     f"vs {adopt_request_id}")
        adopted_version_id = adopted_run.get("adopted_version_id")
        checks.check("终态返回 adopted_version_id", bool(adopted_version_id),
                     str(adopted_version_id))

        status, body = call(
            "GET",
            f"/evolution/versions?kb={kb}&scope=compiled&ds={ds}&limit=50")
        items = (body.get("data") or {}).get("items") or []
        active = [item for item in items if item.get("status") == "active"]
        checks.check("versions list HTTP 200", status == 200, f"status={status}")
        checks.check("versions list 2 版", len(items) == 2, f"n={len(items)}")
        checks.check("新版本 active",
                     len(active) == 1
                     and active[0].get("version_id") == adopted_version_id,
                     str([a.get("version_id") for a in active]))

        # ---- 5) 不同 request_id：409
        status, body = call("POST", f"/evolution/runs/{run_id}/adopt", {
            "confirm": True, "request_id": adopt_request_id + "_other",
            "override_reason": OVERRIDE_REASON,
        })
        checks.check("不同 request_id → 409", status == 409,
                     f"status={status}")

        # ---- 6) 版本详情：默认无 snapshot，include_snapshot=true 才有
        status, body = call(
            "GET", f"/evolution/versions/{adopted_version_id}")
        data = body.get("data") or {}
        checks.check("version detail HTTP 200", status == 200,
                     f"status={status}")
        checks.check("detail 默认不含 snapshot", "snapshot" not in data,
                     ",".join(sorted(data.keys())))
        status, body = call(
            "GET",
            f"/evolution/versions/{adopted_version_id}?include_snapshot=true")
        data = body.get("data") or {}
        checks.check("detail include_snapshot 含 snapshot",
                     isinstance(data.get("snapshot"), dict),
                     str(type(data.get("snapshot"))))

        # ---- 7) 回滚省略 target → 回到 v0
        status, body = call("POST", f"/evolution/runs/{run_id}/rollback",
                            {"reason": "trust-loop check rollback"})
        data = body.get("data") or {}
        rolled = data.get("run") or {}
        checks.check("rollback HTTP 200", status == 200,
                     f"status={status} message={body.get('message')}")
        checks.check("rollback 目标=v0",
                     data.get("target_version_id") == v0["version_id"],
                     f"{data.get('target_version_id')} vs {v0['version_id']}")
        checks.check("rollback 新建版本",
                     bool(data.get("version_id"))
                     and data.get("version_id") != adopted_version_id,
                     str(data.get("version_id")))
        checks.check("rollback run.status=rolled-back",
                     rolled.get("status") == "rolled-back",
                     str(rolled.get("status")))

        status, body = call(
            "GET",
            f"/evolution/versions?kb={kb}&scope=compiled&ds={ds}&limit=50")
        items = (body.get("data") or {}).get("items") or []
        active = [item for item in items if item.get("status") == "active"]
        checks.check("rollback 后版本数 3", len(items) == 3,
                     f"n={len(items)}")
        checks.check("回滚版本 source_version_id=v0",
                     len(active) == 1
                     and active[0].get("source_version_id") == v0["version_id"],
                     str(active[0] if active else None))

        from sqlalchemy import text

        async with get_db_session() as session:
            content = await _read_content(session, tenant, kb, ds)
            pending = (await session.execute(text(
                "SELECT rollback_target_version_id, "
                "rollback_from_version_id, rollback_started_at "
                "FROM knowledge_base.evolution_runs "
                "WHERE tenant_id = :t AND id = :run"),
                {"t": tenant, "run": run_id})).first()
        from capabilities.knowledge_base.services.evolution.version_store import (
            canonical_snapshot_hash,
        )
        current_snapshot = {"scope": "compiled", "kb": kb, "ds": ds,
                            "content": content}
        checks.check("回滚后行内容恢复（hash 与 v0 一致）",
                     canonical_snapshot_hash(current_snapshot)
                     == v0["snapshot_hash"],
                     f"constraints={len(content.get('constraints') or [])}")
        # ⚠️ pending 断言只有在 knowledge-base 容器已重建（新代码会写标记）后
        # 才有实证意义。旧镜像不写标记 → 三列全空，这里显式「空过」而不是
        # 误判（新语义成功时 target/from 会保留、只清 started_at）。
        pending_tuple = tuple(pending) if pending else None
        if pending is None:
            checks.check("回滚 pending 标记（行存在）", False, "run 行不存在")
        elif pending[2] is not None:
            checks.check("回滚收口清空 rollback_started_at", False,
                         str(pending_tuple))
        elif pending[0] is None and pending[1] is None:
            checks.check("回滚 pending 标记（旧镜像空过，需重建后实证）", True,
                         str(pending_tuple))
        else:
            checks.check(
                "回滚成功保留 target/from、只清 started_at",
                pending[0] == v0["version_id"]
                and pending[1] == adopted_version_id,
                f"target={pending[0]} from={pending[1]} "
                f"expect target={v0['version_id']} "
                f"from={adopted_version_id}")

        # ---- 8) ad-hoc 支路（第二个合成 run，链头 = 当前 active）
        from capabilities.knowledge_base.services.evolution.version_store import (
            VersionStore,
            canonical_snapshot_hash,
        )

        run2_id = f"run_check_adhoc_{uuid.uuid4().hex[:10]}"
        run_ids.append(run2_id)
        adhoc_case_id = f"adhoc:{uuid.uuid4().hex[:16]}"
        adhoc_case = {
            "case_id": adhoc_case_id,
            "question": "合成 ad-hoc 真值验证题",
            "verification": "unverified",
            "failure_signature": {"outcome": "fail",
                                  "reason_code": "concept_gap",
                                  "rejected_sql": None},
            "truth": None,
        }
        async with get_db_session() as session:
            chain_active = await VersionStore(session).active_for(
                tenant, kb, "compiled", ds)
            if not chain_active:
                raise RuntimeError("回滚后找不到 active 版本，无法建 ad-hoc run")
            await _insert_run(
                session, tenant, kb, ds, run2_id, chain_active["version_id"],
                _base_patch(kb, ds, run2_id,
                            {"adhoc_cases": [adhoc_case]}),
                "accept")
            before_content = await _read_content(session, tenant, kb, ds)
        before_hash = canonical_snapshot_hash(
            {"scope": "compiled", "kb": kb, "ds": ds,
             "content": before_content})
        checks.check("seed ad-hoc 合成 run", True,
                     f"run={run2_id} parent={chain_active['version_id']}")

        # 8a) 不带 truths：未补 truth 的 ad-hoc case 覆盖校验 → 1001。
        status, body = call("POST", f"/evolution/runs/{run2_id}/adopt",
                            {"confirm": True, "note": "adhoc no truths"})
        checks.check("ad-hoc 缺 truths → 1001",
                     body.get("code") == 1001,
                     f"status={status} code={body.get('code')}")

        # 8b) 带 truths：202 受理后后台验证。合成 DS 没有只读凭据 → 真值
        #     执行失败 → gate_adhoc=reject，快照恢复、run 回 evaluated。
        #     ⚠️ 必须带 override_reason：空语料在 execute 的最前面就会收口，
        #     不带 override 根本走不到 ad-hoc 验证（bench 会先失败）。
        status, body = call("POST", f"/evolution/runs/{run2_id}/adopt", {
            "confirm": True,
            "note": "adhoc with truths",
            "override_reason": OVERRIDE_REASON,
            "adhoc_truths": [{"case_id": adhoc_case_id,
                              "control": ["SELECT 1"],
                              "expect": "answer"}],
        })
        adhoc_data = body.get("data") or {}
        checks.check("ad-hoc 真值失败受理 200/202", status in (200, 202),
                     f"status={status} message={body.get('message')}")
        checks.check("ad-hoc accepted=true",
                     adhoc_data.get("accepted") is True,
                     str(adhoc_data.get("accepted")))

        poll_status, adhoc_bundle, run2 = poll_run(call, run2_id)
        checks.check("ad-hoc 轮询 GET run HTTP 200", poll_status == 200,
                     f"status={poll_status}")
        checks.check("ad-hoc 轮询离开 adopting",
                     bool(run2.get("status"))
                     and run2.get("status") != "adopting",
                     str(run2.get("status")))
        checks.check("ad-hoc run 回 evaluated",
                     run2.get("status") == "evaluated",
                     str(run2.get("status")))
        checks.check("ad-hoc gate_adhoc=reject",
                     run2.get("gate_adhoc") == "reject",
                     str(run2.get("gate_adhoc")))
        async with get_db_session() as session:
            after_content = await _read_content(session, tenant, kb, ds)
        checks.check("ad-hoc 失败后行内容恢复",
                     canonical_snapshot_hash(
                         {"scope": "compiled", "kb": kb, "ds": ds,
                          "content": after_content}) == before_hash,
                     f"constraints={len(after_content.get('constraints') or [])}")
    finally:
        if args.keep:
            print("\n--keep：跳过清理。保留清单：")
            for rid in run_ids:
                print(f"  run_id={rid}")
            print(f"  kb={kb} ds={ds} tenant={tenant}")
            print(f"  adopt_request_id={adopt_request_id}")
            if created.get("compiled"):
                print("  knowledge_base.semantic_versions / "
                      "semantic_versions_archive / ontology_compiled_schemas"
                      " 上该链的行")
        else:
            allow_neo4j = bool(args.force_neo4j
                               or ds.startswith("ds_trustloop_"))
            async with get_db_session() as session:
                errors = await _cleanup(
                    session, tenant, kb, ds, run_ids, created=created,
                    allow_neo4j=allow_neo4j)
            checks.check("清理完成", not errors, "; ".join(errors))

    failed = checks.failed
    print(f"\n== 结果：{len(checks.results) - len(failed)}/"
          f"{len(checks.results)} PASS ==")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL {name} {detail}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="演化台信任闭环可重复集成验收")
    parser.add_argument("--tenant", required=True, help="目标租户 ID（必填）")
    parser.add_argument("--kb", required=True, help="已存在的知识库 ID（必填）")
    parser.add_argument("--ds", required=True,
                        help="合成数据源 ID（必填，建议 ds_trustloop_ 前缀，"
                             "如 ds_trustloop_check）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"知识库 API 基址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--user-id",
                        help="管理员用户 ID；缺省自动查找持有 tenant:admin 的用户")
    parser.add_argument("--keep", action="store_true",
                        help="保留合成数据供排查（不清理）")
    parser.add_argument("--force-neo4j", action="store_true",
                        help="即使 --ds 不以 ds_trustloop_ 开头也清理 Neo4j "
                             "合成数据源子图")
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
            print(f"已加载环境变量文件: {loaded}")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001 —— CLI 边界
        print(f"FAIL  验收脚本异常: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
