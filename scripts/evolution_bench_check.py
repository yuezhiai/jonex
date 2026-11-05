#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演化台完整语料基准（bench）回归拦截 — 125 真机验收脚本。

在**真实数据源**上构造「父本臂能答上、候选臂被新约束弄红」的回归，验证
采纳后基准判定确实拦截并回滚。与 A5 脚本的合成 run 构造方式一致，但**不建
合成数据源**：直接用 `--kb` / `--ds` 指向已有只读凭据的真实源，读取该源的
active compiled schema 选实体。

流程：

1. 前置：KB/DS 存在、compiled 行 active、`query_config` 有**只读凭据**；
   用只读凭据实跑 control SQL，必须返回非空行。
2. 建 1 条回归 + N 条无害合成语料（全部 `bench_check_` 前缀，`try/finally` 清理）：
   - regression：问句点名回归实体，control 计数 > 0。实体与问句**不再写死**
     表名：语料里 control 指向同表的题优先，其次用 `derive_question(entity)`，
     旧 `KNOWN_QUESTIONS` 只作最后兜底；实体默认取「语料 control 命中的表对应
     实体」，语料没有命中时取 compiled schema 首个实体；
   - harmless：默认 12 条无害题（由两条已验证问法循环生成），用来把语料扩到
     smoke 抽样上限（10）以上——否则 `plan_smoke` 每次都会把回归题抽进抽样，
     采纳会在 smoke 阶段先失败，测不到 bench。
3. 建一个 `query_rule` 合成 run：`target_code=Entity.column`、
   **自然语言口径**（默认「只统计 `{column} = -999999` 的记录；生成 SQL 时必须
   附加 `WHERE {column} = -999999`」）、`target_cases=[]`（smoke 只跑非目标抽样；
   回归题必须不在抽样里，否则 smoke 会先看到它）。
   ⚠️ `expression` 是给模型的自然语言说明，**不是会被执行的 SQL 谓词**——
   早期用 `1=0` 时模型照样生成不加过滤的 SQL，候选臂仍然全过，测不到拦截。
4. 用 `plan_smoke` / preview 确认 regression **不在** smoke 的非目标抽样里；
   在的话换一个 run_id 重新建 run（抽样按 run_id 固定 seed，不改抽样逻辑）。
5. adopt（带 override_reason）→ 202 → 轮询至离开 `adopting`。
6. 断言：父本臂 regression `pass`、候选臂 regression `fail`；run
   `smoke-failed`、`bench_status="failed"`、`bench.reasons` 含 `new_red`；
   版本 `rejected`、内容已恢复、审计有 `evolution.bench_fail`。
7. `try/finally` 清理 run / 版本 / 语料 / 台账 / 审计 / Redis 缓存；
   真实 compiled 行只做内容恢复，**绝不删除**。

若 NL→SQL 没有稳定选中该实体（父本臂不是 pass）或候选臂没被约束打红，
脚本**显式失败并打印两臂逐题结果**，不改判据、不跳过断言；换实体/问句
重试（`--regression-entity` / `--regression-question` / `--regression-control`）。

用法（125 容器内，代码根 `/app`）：

    python /tmp/evolution_bench_check.py \\
      --tenant tenant_jonex_demo --kb kb_assets_r2 \\
      --ds 4cdb29e4-268c-4daf-8abe-7b879fa6abf0 \\
      --base-url http://gateway:8000/api/v1/knowledge-base --user-id 1

所有 DB 访问走 `get_db_session()`，SQL 全部参数化；不使用 `shell=True`。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
# 容器内 /app 是代码根；本地跑时回退到仓库根。
for _candidate in ("/app", str(_REPO_ROOT)):
    if _candidate and Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

DEFAULT_BASE_URL = "http://gateway:8000/api/v1/knowledge-base"
#: 字面默认值；`build_parser` 在**加载 --env-file 之后**才解析 `EVOLUTION_CHECK_*`，
#: 所以 env 里的值能真正影响 argparse 默认值（不是 import 时就固化）。
DEFAULT_TENANT = "tenant_jonex_demo"
DEFAULT_USER_ID = "1"
SCOPE = "compiled"
#: 合成数据名前缀：清理只认这个前缀，绝不误删真实语料 / run。
NAME_PREFIX = "bench_check"
RUN_PREFIX = "run_bench_check"
CREATED_BY = "bench_check"
#: `query_rule` 的回归表达式。注意：这是给模型的**自然语言口径**，不是 SQL
#: 谓词——`1=0` 不会被执行，必须写成模型会照做的过滤指令。
REGRESSION_EXPRESSION = (
    "该列的取值口径：只统计 {column} = -999999 的记录；生成 SQL 时必须附加 "
    "WHERE {column} = -999999，其他记录一律排除。这只是该列的取值口径，"
    "不是整表默认过滤。"
)
#: 通过路径（--expect-pass）用的无害口径：只陈述事实，不改变任何记录范围。
PASS_EXPRESSION = (
    "该列的取值口径：{column} 是记录的唯一标识；统计时不改变任何记录范围，"
    "也不新增过滤条件。这只是该列的口径说明，不是整表默认过滤。"
)
OVERRIDE_REASON = "bench_check：合成回归验收（显式放行）"
#: 已由真实语料证明能稳定选中的问句（按表名后缀匹配，实体名不同也能用）。
KNOWN_QUESTIONS = {
    "jobs": "一共有多少个职位",
    "company": "一共有多少家公司",
    "citys": "系统里一共有多少个城市",
}
#: 失败路径上逐题打印上限（两臂 × 题数，默认全量）。
POLL_INTERVAL_SECONDS = 3.0


# ------------------------------------------------------------------ 基础件
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


def _bundle(call, run_id: str) -> tuple[int, dict, dict, dict]:
    status, body = call("GET", f"/evolution/runs/{run_id}")
    data = body.get("data") or {}
    return status, data, (data.get("run") or {}), body


def poll_run(call, run_id: str, timeout: float,
             interval: float = POLL_INTERVAL_SECONDS,
             max_failures: int = 10) -> tuple[int, dict, dict]:
    """轮询运行详情直到 `status != "adopting"`（或超时）。

    连续 `max_failures` 次非 200（网关/鉴权/服务未就绪）就**立刻抛错**并带上最后
    一次 status/body —— 不能把配置错误硬等到 1 小时超时。超时本身仍原样返回，
    由调用方断言终态。
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
        "username": f"{CREATED_BY}_{user_id}",
        "role": "admin",
        "roles": ["admin"],
        "type": "user",
        "exp": now + datetime.timedelta(minutes=30),
        "iat": now,
    }
    return jwt.encode(payload, cfg.JWT_SECRET, algorithm=cfg.JWT_ALGORITHM)


def _json_list(value) -> list:
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


async def _compiled_row(session, tenant: str, kb: str, ds: str) -> dict | None:
    """直接读 active compiled 行（绕过 Redis 缓存，验收用真值）。"""
    from sqlalchemy import text

    row = (await session.execute(text(
        "SELECT status, entity_types, relation_types, constraints, "
        "schema_version FROM knowledge_base.ontology_compiled_schemas "
        "WHERE tenant_id = :t AND knowledge_base_id = :kb "
        "AND data_source_id = :ds LIMIT 1"),
        {"t": tenant, "kb": kb, "ds": ds})).first()
    if row is None:
        return None
    return {
        "status": row[0],
        "content": {"entity_types": _json_list(row[1]),
                    "relation_types": _json_list(row[2]),
                    "constraints": _json_list(row[3])},
        "schema_version": row[4],
    }


async def _read_content(session, tenant: str, kb: str, ds: str) -> dict:
    row = await _compiled_row(session, tenant, kb, ds)
    return (row or {}).get("content") or {}


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


async def _audit_count(session, tenant: str, run_id: str, action: str) -> int:
    from sqlalchemy import text

    return int((await session.execute(text(
        "SELECT count(*) FROM platform.audit_logs WHERE tenant_id = :t "
        "AND resource = 'evolution_run' AND resource_id = :r "
        "AND action = :a"), {"t": tenant, "r": run_id, "a": action})).scalar() or 0)


async def _audit_count_retry(tenant: str, run_id: str, action: str,
                             attempts: int = 5,
                             delay: float = 0.2) -> int:
    """审计是**收口之后**写的旁路：轮询刚看到终态时行可能还没提交。

    每次用独立会话重查（READ COMMITTED 下能看到新提交），最多
    `attempts` 次；仍为 0 才交给调用方判失败 —— 不给审计写入窗口就误报。
    """
    from jonex_core.common.database import get_db_session

    count = 0
    total = max(1, int(attempts))
    for index in range(total):
        async with get_db_session() as session:
            count = await _audit_count(session, tenant, run_id, action)
        if count:
            return count
        if index + 1 < total:
            await asyncio.sleep(delay)
    return count


# ------------------------------------------------------------------ DB 写
def _patch(kb: str, ds: str, run_id: str, target_code: str,
           target_cases: list[str], expression: str) -> dict:
    return {
        "patch_id": f"patch_{run_id}",
        "kind": "query_rule",
        "kb": kb,
        "ds": ds,
        "entries": [{"target_code": target_code,
                     "expression": expression}],
        "skipped": [],
        "target_cases": list(target_cases),
        "rollback": {"added": []},
    }


async def _insert_run(session, tenant: str, kb: str, ds: str, run_id: str,
                      parent_version_id: str, patch: dict) -> None:
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
            CAST(:gate AS jsonb), CAST(:patch AS jsonb), '{}'::jsonb, :by,
            :parent_version_id, 'accept', 0, now(), now()
        )
        """), {"id": run_id, "t": tenant, "kb": kb, "ds": ds,
               "gate": json.dumps({"decision": "accept"}, ensure_ascii=False),
               "patch": json.dumps(patch, ensure_ascii=False),
               "by": CREATED_BY,
               "parent_version_id": parent_version_id})
    await session.commit()


async def _insert_corpus_case(session, tenant: str, kb: str, ds: str,
                              name: str, question: str, control: str,
                              case_id: str) -> str:
    from sqlalchemy import text

    await session.execute(text(
        """
        INSERT INTO knowledge_base.evolution_corpus_cases (
            id, tenant_id, name, question, tier, expect, control,
            kb_id, ds_id, enabled, created_at, updated_at
        ) VALUES (
            :id, :t, :name, :question, 'single_table', 'answer',
            CAST(:control AS jsonb), :kb, :ds, 1, now(), now()
        )
        """), {"id": case_id, "t": tenant, "name": name,
               "question": question,
               "control": json.dumps([control], ensure_ascii=False),
               "kb": kb, "ds": ds})
    await session.commit()
    return case_id


async def _delete_runs(session, tenant: str, run_ids: list[str]) -> None:
    """删 run 及其从属行（预览失败重试 / 最终清理共用）。"""
    from sqlalchemy import text

    for run_id in run_ids:
        await session.execute(text(
            "DELETE FROM knowledge_base.evolution_run_logs WHERE run_id = :r"),
            {"r": run_id})
        await session.execute(text(
            "DELETE FROM knowledge_base.evolution_candidates WHERE run_id = :r"),
            {"r": run_id})
        await session.execute(text(
            "DELETE FROM knowledge_base.evolution_ledgers "
            "WHERE tenant_id = :t AND run_id = :r"), {"t": tenant, "r": run_id})
        await session.execute(text(
            "DELETE FROM platform.audit_logs WHERE tenant_id = :t "
            "AND resource = 'evolution_run' AND resource_id = :r"),
            {"t": tenant, "r": run_id})
        await session.execute(text(
            "DELETE FROM knowledge_base.evolution_runs "
            "WHERE tenant_id = :t AND id = :r"), {"t": tenant, "r": run_id})
    await session.commit()


# ------------------------------------------------------------------ 问句选择
def _control_text(control) -> str:
    """`control`（list / str / None）→ 用于匹配表名的 SQL 文本。"""
    if control is None:
        return ""
    if isinstance(control, str):
        return control
    if isinstance(control, (list, tuple, set)):
        return " ".join(str(item) for item in control if item is not None)
    return str(control)


def _normalize_sql(text) -> str:
    """大小写归一 + 把多余空白（含换行）压成单个空格。"""
    return " ".join(str(text or "").split()).lower()


def _control_targets_table(control, table: str) -> bool:
    """control SQL 里是否有 `FROM <table>`（大小写/空白归一，词边界匹配）。

    词边界避免 `FROM t1` 命中 `FROM t1x` / `FROM t1_x`。
    """
    text = _normalize_sql(_control_text(control))
    target = _normalize_sql(table)
    if not text or not target:
        return False
    pattern = r"\bfrom\s+" + re.escape(target) + r"(?![0-9a-z_])"
    return re.search(pattern, text) is not None


#: 纯计数：整条 SQL 只有 `SELECT COUNT(*) [AS alias] FROM <ident>`。
_PLAIN_COUNT_RE = re.compile(
    r"^select\s+count\s*\(\s*\*\s*\)\s*(?:as\s+[a-z_][a-z0-9_]*\s+)?"
    r"from\s+[a-z0-9_.\[\]\"`]+;?$")
#: 任何附加结构都按「不纯」处理（保守判定，解析不了也不借）。
_PLAIN_COUNT_FORBIDDEN_RE = re.compile(
    r"\b(?:where|group|having|join|union|except|intersect|limit|offset)\b")


def is_plain_count(control) -> bool:
    """保守判定 `control` 是否为「单表全表计数」的纯 `SELECT COUNT(*) FROM <t>`。

    回归真值默认是 `SELECT COUNT(*) FROM <table>`（无 WHERE）；只有纯计数题
    的问句与它严格对应。借用带过滤的语料题（如「高薪职位有多少」）会让健康
    管线判失败。`control` 可以是单条 SQL 字符串或只含一条 SQL 的列表；
    None / 多语句 / 任何附加结构（WHERE/GROUP/HAVING/JOIN/UNION/子查询等）/
    解析不了的一律按「不纯」处理。
    """
    if isinstance(control, (list, tuple)):
        if len(control) != 1:
            return False
        control = control[0]
    if not isinstance(control, str):
        return False
    text = _normalize_sql(control)
    if not text or text.count("(") != 1 or text.count(")") != 1:
        return False
    if _PLAIN_COUNT_FORBIDDEN_RE.search(text):
        return False
    return _PLAIN_COUNT_RE.match(text) is not None


def pick_question(table: str, corpus_cases, entity: dict) -> str:
    """语料里 control 指向同表的**纯计数**题优先；否则 derive_question；再否则空串。

    只借 `is_plain_count` 的题：回归真值默认是全表 `COUNT(*)`，带过滤的问句
    与真值不匹配，真机上会把健康管线误判为失败。
    """
    for case in corpus_cases or []:
        if not isinstance(case, dict):
            continue
        control = case.get("control")
        if not is_plain_count(control):
            continue
        if not _control_targets_table(control, table):
            continue
        question = str(case.get("question") or "").strip()
        if question:
            return question
    from capabilities.knowledge_base.services.evolution.corpus_seed import (
        derive_question,
    )
    return derive_question(entity) or ""


# ------------------------------------------------------------------ 实体选择
def _table_suffix(table: str) -> str:
    return str(table or "").rsplit(".", 1)[-1].strip().lower()


def _entity_candidates(content: dict) -> list[dict]:
    out: list[dict] = []
    for entity in content.get("entity_types") or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        table = str(entity.get("source_object_id") or "").strip()
        attrs = [item for item in entity.get("attributes") or []
                 if isinstance(item, dict) and str(item.get("name") or "").strip()]
        if not name or not table or not attrs:
            continue
        out.append({
            "name": name,
            "table": table,
            "attrs": attrs,
            "display": str(entity.get("display_name") or name).strip() or name,
            "suffix": _table_suffix(table),
        })
    return out


def _corpus_hit_entity(candidates: list[dict], corpus_cases) -> dict | None:
    """compiled 顺序里，第一个「语料 control 指向其表」的实体。"""
    for item in candidates:
        for case in corpus_cases or []:
            if not isinstance(case, dict):
                continue
            if _control_targets_table(case.get("control"), item["table"]):
                return item
    return None


def _pick_entities(content: dict, args,
                   corpus_cases=None) -> tuple[dict, list[dict]]:
    candidates = _entity_candidates(content)
    if not candidates:
        raise RuntimeError("compiled schema 里没有带 source_object_id 与属性的实体")
    by_name = {item["name"]: item for item in candidates}

    if args.regression_entity:
        regression = by_name.get(args.regression_entity)
        if regression is None:
            raise RuntimeError(
                f"回归实体 {args.regression_entity!r} 不在 compiled schema 里；"
                f"可选：{', '.join(sorted(by_name))}")
    else:
        # 语料 control 命中的表对应实体优先；否则 compiled schema 首个实体。
        regression = _corpus_hit_entity(candidates, corpus_cases) or candidates[0]

    harmless = [item for item in candidates
                if item["name"] != regression["name"]][:2]
    if len(harmless) < 2:
        raise RuntimeError("找不到两条不会被本次约束影响的无害实体/题")
    return regression, harmless


def _injectable(content: dict, entity_name: str) -> bool:
    """新约束是否还能被 `filter_constraints` 注入（≤8 条 / ≤1500 字）。"""
    from capabilities.knowledge_base.services.nl2sql.constraints import (
        MAX_CONSTRAINTS,
        MAX_TOTAL_CHARS,
    )

    prefix = f"{entity_name}."
    count = used = 0
    for item in content.get("constraints") or []:
        if not isinstance(item, dict):
            continue
        target_type = str(item.get("target_type") or "").strip().lower()
        target_code = str(item.get("target_code") or "").strip()
        if target_type == "entity":
            hit = target_code == entity_name
        elif target_type == "attribute":
            hit = target_code.startswith(prefix)
        else:
            hit = False
        raw = str(item.get("expression") or "")
        if hit and raw.strip():
            count += 1
            used += len(raw)
    return (count < MAX_CONSTRAINTS
            and used + len(REGRESSION_EXPRESSION) <= MAX_TOTAL_CHARS)


def _free_target(content: dict, entity: dict) -> str:
    """挑一个尚未有 query_rule 的属性：`Entity.column`。"""
    occupied = {
        str(item.get("target_code"))
        for item in content.get("constraints") or []
        if isinstance(item, dict)
        and str(item.get("constraint_type") or "") == "query_rule"
    }
    for attr in entity["attrs"]:
        code = f"{entity['name']}.{attr['name']}"
        if code not in occupied:
            return code
    raise RuntimeError(
        f"实体 {entity['name']} 的所有属性都已有 query_rule，换一个实体")


def _question_for(entity: dict, override: str | None,
                  corpus_cases=None) -> str:
    """问句优先级：显式覆盖 → 语料同表题 → derive_question → 旧兜底。"""
    if override:
        return override
    entity_like = {"name": entity["name"], "display_name": entity["display"]}
    question = pick_question(entity["table"], corpus_cases or [], entity_like)
    if question:
        return question
    known = KNOWN_QUESTIONS.get(entity["suffix"])
    return known or f"一共有多少个{entity['display']}"


def _count_sql(entity: dict, override: str | None) -> str:
    if override:
        return override
    return f"SELECT COUNT(*) AS n FROM {entity['table']}"


# ------------------------------------------------------------------ 清理
async def _restore_original_content(tenant: str, kb: str, ds: str,
                                    original_snapshot: dict,
                                    original_hash: str) -> str:
    """把 compiled 行内容恢复成脚本开始前的快照；失败时回退直写 SQL。"""
    from jonex_core.common.database import get_db_session
    from capabilities.knowledge_base.services.evolution import schema_io

    errors: list[str] = []
    try:
        # 缓存清理不需要 DB 会话（之前多开了一个只调 Redis 的空会话）。
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

    # 1) 恢复脚本开始前的 compiled 行内容（真实行：只恢复内容，不删除）。
    if state.get("original_snapshot") is not None:
        restore_error = await _restore_original_content(
            tenant, kb, ds, state["original_snapshot"], state["original_hash"])
        if restore_error:
            errors.append(restore_error)

    # 2) 删除合成语料 case（名字前缀 + 显式 id）。
    try:
        async with get_db_session() as session:
            case_ids = set(state.get("corpus_case_ids") or [])
            rows = (await session.execute(text(
                "SELECT id FROM knowledge_base.evolution_corpus_cases "
                "WHERE tenant_id = :t AND kb_id = :kb AND ds_id = :ds "
                "AND name LIKE :prefix"), {
                    "t": tenant, "kb": kb, "ds": ds,
                    "prefix": f"{NAME_PREFIX}_%",
                })).scalars().all()
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
                "WHERE tenant_id = :t AND (id LIKE :prefix OR created_by = :by)"),
                {"t": tenant, "prefix": f"{RUN_PREFIX}%", "by": CREATED_BY}
            )).scalars().all()
            run_ids = set(state.get("run_ids") or [])
            run_ids.update(str(row) for row in rows)
            await _delete_runs(session, tenant, sorted(run_ids))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"runs: {exc}")
        traceback.print_exc()

    # 3b) 删除结果复核：DELETE 不报错也可能命中 0 行，必须回查残留。
    try:
        async with get_db_session() as session:
            left_cases = int((await session.execute(text(
                "SELECT count(*) FROM knowledge_base.evolution_corpus_cases "
                "WHERE tenant_id = :t AND kb_id = :kb AND ds_id = :ds "
                "AND name LIKE :prefix"), {
                    "t": tenant, "kb": kb, "ds": ds,
                    "prefix": f"{NAME_PREFIX}_%",
                })).scalar() or 0)
            left_runs = int((await session.execute(text(
                "SELECT count(*) FROM knowledge_base.evolution_runs "
                "WHERE tenant_id = :t AND (id LIKE :prefix OR created_by = :by)"),
                {"t": tenant, "prefix": f"{RUN_PREFIX}%", "by": CREATED_BY}
            )).scalar() or 0)
        if left_cases or left_runs:
            errors.append(
                f"删除不完整：corpus_cases={left_cases} runs={left_runs}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"delete_verify: {exc}")
        traceback.print_exc()

    # 4) 删除本脚本创建的版本（主表 + 归档表）。
    try:
        async with get_db_session() as session:
            rows = []
            for table in ("semantic_versions", "semantic_versions_archive"):
                rows.extend((await session.execute(text(
                    f"SELECT id FROM knowledge_base.{table} "
                    "WHERE tenant_id = :t AND knowledge_base_id = :kb "
                    "AND scope = 'compiled' AND data_source_id = :ds "
                    "AND (id LIKE :prefix OR source_run_id LIKE :run_prefix "
                    "OR created_by = :by)"), {
                        "t": tenant, "kb": kb, "ds": ds,
                        "prefix": "ver_bench_check_%",
                        "run_prefix": f"{RUN_PREFIX}%", "by": CREATED_BY,
                    })).scalars().all())
            version_ids = set(state.get("version_ids") or [])
            version_ids.update(str(row) for row in rows)
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
            active = [item for item in versions if item["status"] == "active"]
            content = await _read_content(session, tenant, kb, ds)
        chain_hash = _content_hash(kb, ds, content)
        checks.check(
            "清理后链版本数恢复",
            len(versions) == state.get("original_version_count"),
            f"n={len(versions)} expected={state.get('original_version_count')}")
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
def _active_pointer_id(versions: list[dict]) -> str | None:
    """版本链当前 active 的 id；没有 active 时 None。"""
    active = [item for item in versions or []
              if isinstance(item, dict) and item.get("status") == "active"]
    return active[0]["id"] if active else None


def _arm_outcome(arm: dict, name: str) -> str | None:
    for case in (arm or {}).get("cases") or []:
        if isinstance(case, dict) and str(case.get("name")) == name:
            return str(case.get("outcome") or "error")
    return None


def _print_arms(bench: dict, regression_name: str, harmless_names: list[str]) -> None:
    """失败时打印两臂逐题结果（父本 -> 候选），便于换实体/问句重试。"""
    bench = bench if isinstance(bench, dict) else {}
    parent = bench.get("parent") or {}
    candidate = bench.get("candidate") or {}
    rows = [str(case.get("name")) for case in parent.get("cases") or []
            if isinstance(case, dict)]
    for case in candidate.get("cases") or []:
        name = str((case or {}).get("name"))
        if name and name not in rows:
            rows.append(name)
    print("\n  基准两臂逐题（parent -> candidate）：")
    for name in rows:
        mark = " *" if name in ([regression_name] + list(harmless_names)) else ""
        print(f"    {name}: {_arm_outcome(parent, name)} -> "
              f"{_arm_outcome(candidate, name)}{mark}")
    print(f"  reasons={bench.get('reasons')} "
          f"new_red={bench.get('new_red')} "
          f"new_error={bench.get('new_error')}")
    print(f"  parent={{{', '.join(f'{k}={parent.get(k)}' for k in ('passes', 'graded', 'errors'))}}}")
    print(f"  candidate={{{', '.join(f'{k}={candidate.get(k)}' for k in ('passes', 'graded', 'errors'))}}}")


async def _prepare(args, checks: Checks, state: dict) -> dict:
    """前置校验 + 选实体/题/靶点 + 建语料与 run（含 smoke 抽样确认）。"""
    from jonex_core.common.database import get_db_session
    from capabilities.knowledge_base.services.evolution.corpus_service import (
        load_corpus,
    )
    from capabilities.knowledge_base.services.evolution.eval import plan_smoke
    from capabilities.knowledge_base.services.evolution.schema_io import (
        snapshot_for,
    )
    from capabilities.knowledge_base.services.evolution.version_store import (
        VersionStore, canonical_snapshot_hash,
    )
    from capabilities.knowledge_base.services.evolution.upstream import (
        truth_runner_for,
    )

    tenant, kb, ds = args.tenant, args.kb, args.ds
    suffix = uuid.uuid4().hex[:8]
    regression_name = f"{NAME_PREFIX}_{suffix}_regression"
    harmless_count = max(2, int(getattr(args, "harmless_cases", 12) or 12))
    harmless_names = [f"{NAME_PREFIX}_{suffix}_control_{i:02d}"
                      for i in range(harmless_count)]
    state.update(corpus_case_ids=[], run_ids=[], version_ids=[],
                 baseline_active_id=None)

    async with get_db_session() as session:
        await _clear_cache(tenant, kb, ds)
        if not await _kb_exists(session, tenant, kb):
            raise RuntimeError(f"KB 不存在或已删除: tenant={tenant} kb={kb}")
        row = await _compiled_row(session, tenant, kb, ds)
        if row is None:
            raise RuntimeError(
                f"该数据源没有 compiled schema 行（tenant={tenant} "
                f"kb={kb} ds={ds}）；先编译 schema 再跑本脚本")
        if str(row.get("status") or "") != "active":
            raise RuntimeError(
                f"compiled schema 状态是 {row.get('status')!r}，不是 active")
        content = row["content"]

        # 只读凭据：没有就显式失败（真值/control 绝不能回落写账号）。
        from capabilities.knowledge_base.services.data_source_service import (
            DataSourceService,
        )
        resolved = await DataSourceService().query_config(tenant, ds)
        checks.check("数据源已登记只读凭据", resolved is not None,
                     f"ds={ds}")
        if resolved is None:
            raise RuntimeError(
                f"数据源 {ds} 没有登记只读凭据，无法执行 control SQL")

        # 现役语料决定实体/问句优先级（合成语料在下面才插入）。
        corpus_cases = (await load_corpus(session, tenant, kb, ds)).get(
            "cases") or []
        regression, harmless = _pick_entities(content, args, corpus_cases)
        if not _injectable(content, regression["name"]):
            raise RuntimeError(
                f"实体 {regression['name']} 的口径约束已满（8 条 / 1500 字），"
                "新约束不会被注入；换一个实体")
        target_code = _free_target(content, regression)
        regression_question = _question_for(
            regression, args.regression_question, corpus_cases)
        regression_control = _count_sql(regression, args.regression_control)
        harmless_questions = [
            _question_for(harmless[0], args.control_question_a, corpus_cases),
            _question_for(harmless[1], args.control_question_b, corpus_cases),
        ]
        harmless_controls = [
            _count_sql(harmless[0], args.control_sql_a),
            _count_sql(harmless[1], args.control_sql_b),
        ]

        # control SQL 必须能跑、且返回非空行（语料有效性的前提）。
        truth = truth_runner_for(tenant, ds)
        for label, sql in ([(f"regression({regression['name']})",
                             regression_control)]
                           + [(f"control_{chr(97 + i)}({entity['name']})", sql)
                              for i, (entity, sql) in enumerate(
                                  zip(harmless, harmless_controls))]):
            try:
                rows = await truth(sql)
            except Exception as exc:  # noqa: BLE001 —— 前置校验显式失败
                raise RuntimeError(
                    f"{label} 的 control SQL 执行失败：{exc}\n  SQL: {sql}"
                ) from exc
            checks.check(f"{label} control SQL 返回非空行", bool(rows),
                         f"rows={len(rows or [])} SQL={sql}")
            if not rows:
                raise RuntimeError(f"{label} 的 control SQL 返回 0 行：{sql}")
        # 两条已验证无害问法循环扩到 harmless_count 条：语料必须超过 smoke
        # 抽样上限（10），否则换多少 run_id 回归题都会被抽进 smoke。
        harmless_questions = [harmless_questions[i % len(harmless_questions)]
                              for i in range(harmless_count)]
        harmless_controls = [harmless_controls[i % len(harmless_controls)]
                             for i in range(harmless_count)]
        print(f"  回归实体 = {regression['name']} ({regression['table']}) "
              f"靶点 = {target_code}")
        print(f"  无害实体 = {harmless[0]['name']}, {harmless[1]['name']}")

        # 基线（恢复源）与版本链。
        original = await snapshot_for(tenant, "query_rule", kb, ds)
        if original.get("content") is None:
            raise RuntimeError("目标链的编译行内容为空，不能作为验收基线")
        state["original_snapshot"] = original
        state["original_hash"] = canonical_snapshot_hash(original)
        state["original_constraint_count"] = len(
            original["content"].get("constraints") or [])
        versions = await _version_rows(session, tenant, kb, ds)
        state["original_version_count"] = len(versions)
        active = [item for item in versions if item["status"] == "active"]
        state["original_active_id"] = active[0]["id"] if active else None
        checks.check(
            "基线：目标链可读", True,
            f"versions={state['original_version_count']} "
            f"active={state['original_active_id']} "
            f"constraints={state['original_constraint_count']} "
            f"hash={state['original_hash'][:16]}…")

        if state["original_active_id"]:
            parent_version_id = state["original_active_id"]
        else:
            # 链上还没有 active：按 A5 同款 seed v0（清理时删除）。
            v0 = await VersionStore(session).create_candidate(
                tenant, kb, SCOPE, ds, original,
                created_by=CREATED_BY, note="bench_check v0 seed")
            await VersionStore(session).activate(
                tenant, v0["version_id"], previous_id=None)
            state["version_ids"].append(v0["version_id"])
            parent_version_id = v0["version_id"]
            checks.check("seed v0（开始前无 active）", True,
                         parent_version_id)
        # 本轮验收的**有效基线**：seeded-v0 路径下开始前没有 active，但当前
        # active 已经是本脚本 seed 的 v0。断言「active 未被推进」要对比这个
        # 值，不能对比 original_active_id（那会让 seeded 路径恒失败）。
        # original_active_id 只留给清理收尾（清理后应恢复成开始前的空指针）。
        state["baseline_active_id"] = parent_version_id

        # 1 条回归 + N 条无害合成语料。
        case_ids = []
        for name, question, control in (
                [(regression_name, regression_question, regression_control)]
                + list(zip(harmless_names, harmless_questions,
                           harmless_controls))):
            case_ids.append(await _insert_corpus_case(
                session, tenant, kb, ds, name, question, control,
                uuid.uuid4().hex))
        state["corpus_case_ids"].extend(case_ids)
        checks.check(f"插入 {1 + harmless_count} 条合成语料",
                     len(case_ids) == 1 + harmless_count,
                     f"regression={regression_name} "
                     f"harmless={harmless_names}")

        # 补丁不带 target_cases：smoke 只跑非目标抽样，回归由完整语料臂发现。
        column = target_code.rsplit(".", 1)[-1]
        if getattr(args, "expect_pass", False):
            raw_expression = (getattr(args, "pass_expression", None)
                              or PASS_EXPRESSION)
        else:
            raw_expression = (getattr(args, "regression_expression", None)
                              or REGRESSION_EXPRESSION)
        expression = raw_expression.replace("{column}", column)
        patch = _patch(
            kb, ds, uuid.uuid4().hex[:12], target_code, [], expression)
        corpus = await load_corpus(session, tenant, kb, ds)
        case_names = {str(case.get("name")) for case in corpus.get("cases") or []
                      if isinstance(case, dict)}
        checks.check("合成语料已进入语料集",
                     {regression_name, *harmless_names} <= case_names,
                     f"corpus_size={len(case_names)}")

    # ---- 选 run_id：回归题不得在 smoke 非目标抽样里（抽样按 run_id 固定 seed）。
    run_id = ""
    for attempt in range(1, max(1, int(args.plan_attempts)) + 1):
        candidate_run = f"{RUN_PREFIX}_{uuid.uuid4().hex[:12]}"
        plan = plan_smoke(
            {"run_id": candidate_run, "patch": patch, "report": {}}, corpus)
        names = [str(case.get("name")) for case in plan.get("cases") or []
                 if isinstance(case, dict)]
        if regression_name in names:
            print(f"  抽样尝试 {attempt}: run_id={candidate_run} "
                  f"回归题被 smoke 抽中，换一个 run_id")
            continue
        async with get_db_session() as session:
            await _insert_run(session, tenant, kb, ds, candidate_run,
                              parent_version_id, patch)
        # 服务端 preview 复核（与真正采纳时同一份 plan_smoke）。
        status, body = _http(
            args.base_url, "POST",
            f"/evolution/runs/{candidate_run}/adopt",
            token=state["token"], tenant=tenant, body={"confirm": False})
        data = body.get("data") or {}
        server_plan = data.get("smoke_plan") or {}
        server_names = [str(case.get("name"))
                        for case in server_plan.get("cases") or []
                        if isinstance(case, dict)]
        planned = data.get("planned") or []
        skipped = data.get("skipped") or []
        # `target_cases=[]`：题集只剩非目标抽样，只需确认回归题没被抽中。
        ok = (status == 200
              and regression_name not in server_names
              and bool(planned) and not skipped)
        if not ok:
            async with get_db_session() as session:
                await _delete_runs(session, tenant, [candidate_run])
            detail = (f"status={status} smoke={server_names} "
                      f"planned={len(planned)} skipped={skipped}")
            print(f"  抽样尝试 {attempt}: run_id={candidate_run} "
                  f"服务端复核未通过（{detail}），换一个 run_id")
            if status != 200 or skipped:
                raise RuntimeError(
                    f"preview 复核失败（{detail}）；preview message="
                    f"{body.get('message')}")
            continue
        run_id = candidate_run
        checks.check(
            "回归题不在 smoke 抽样中", True,
            f"run={run_id} smoke_size={len(server_names)} "
            f"attempt={attempt}")
        break

    if not run_id:
        raise RuntimeError(
            f"{args.plan_attempts} 次尝试都没找到「回归题不被 smoke 抽样」的 "
            "run_id；语料非目标题不足时抽样会恒命中，先扩容语料或换语料集")
    return {"run_id": run_id,
            "regression_name": regression_name,
            "harmless_names": harmless_names,
            "target_code": target_code,
            "question": regression_question,
            "control": regression_control}


async def run(args: argparse.Namespace) -> int:
    from jonex_core.common.tenant import require_tenant

    tenant = require_tenant(args.tenant)
    args.tenant = tenant
    checks = Checks()
    state: dict = {}

    from jonex_core.common.database import get_db_session

    try:
        async with get_db_session() as session:
            user_id = args.user_id or await _find_admin_user(session, tenant)
        if not user_id:
            print("FAIL  找不到持有 tenant:admin 的用户；请用 --user-id 指定",
                  file=sys.stderr)
            return 2
        state["token"] = _make_token(tenant, user_id)
        token = state["token"]

        def call(method: str, path: str, body: dict | None = None):
            return _http(args.base_url, method, path, token=token,
                         tenant=tenant, body=body)

        facts = await _prepare(args, checks, state)
        regression_name = facts["regression_name"]
        harmless_names = facts["harmless_names"]
        run_id = facts["run_id"]
        state["run_ids"].append(run_id)
        request_id = f"{CREATED_BY}_{uuid.uuid4().hex[:16]}"

        # ---- 202 受理 → 轮询收口（完整语料在后台跑，不受代理超时约束）。
        adopt_body = {
            "confirm": True,
            "request_id": request_id,
            "note": ("bench_check synthetic pass"
                     if getattr(args, "expect_pass", False)
                     else "bench_check synthetic new_red"),
        }
        if not getattr(args, "expect_pass", False):
            adopt_body["override_reason"] = OVERRIDE_REASON
        status, body = call(
            "POST", f"/evolution/runs/{run_id}/adopt", adopt_body)
        data = body.get("data") or {}
        checks.check("adopt HTTP 200/202", status in (200, 202),
                     f"status={status} message={body.get('message')}")
        checks.check("adopt accepted=true", data.get("accepted") is True,
                     str(data.get("accepted")))
        if data.get("accepted") is True:
            checks.check("adopt status=adopting",
                         data.get("status") == "adopting",
                         str(data.get("status")))

        poll_status, bundle, run_doc = poll_run(
            call, run_id, timeout=args.poll_timeout)
        terminal = run_doc.get("status")
        bench = bundle.get("bench") if isinstance(bundle.get("bench"), dict) else {}
        checks.check("轮询 GET run HTTP 200", poll_status == 200,
                     f"status={poll_status}")
        checks.check("轮询离开 adopting", bool(terminal)
                     and terminal != "adopting", str(terminal))

        # ---- 判定/恢复断言（任何一条不满足都显式失败，不改判据）。
        parent_outcome = _arm_outcome(bench.get("parent"), regression_name)
        candidate_outcome = _arm_outcome(bench.get("candidate"), regression_name)
        expect_pass = bool(getattr(args, "expect_pass", False))
        checks.check("父本臂回归题 pass", parent_outcome == "pass",
                     str(parent_outcome))
        checks.check(
            "候选臂回归题 " + ("pass" if expect_pass else "fail"),
            candidate_outcome == ("pass" if expect_pass else "fail"),
            str(candidate_outcome))

        async with get_db_session() as session:
            versions = await _version_rows(session, tenant, args.kb, args.ds)
            content_after = await _read_content(
                session, tenant, args.kb, args.ds)
            current_active_id = _active_pointer_id(versions)
            error_text = str(run_doc.get("error") or "")
        # 审计在收口后写入：给 5 × 0.2s 的提交窗口，避免刚轮询到终态就误判缺失。
        audit_bench_fail = await _audit_count_retry(
            tenant, run_id, "evolution.bench_fail")
        audit_adopt = await _audit_count_retry(
            tenant, run_id, "evolution.adopt")
        audit_override = await _audit_count_retry(
            tenant, run_id, "evolution.adopt_override")
        same_run_versions = [item for item in versions
                             if item["source_run_id"] == run_id]
        after_hash = _content_hash(args.kb, args.ds, content_after)
        checks.check("bench.corpus_size > 0",
                     int(bench.get("corpus_size") or 0) > 0,
                     str(bench.get("corpus_size")))
        if expect_pass:
            checks.check("run 终态 adopted", terminal == "adopted",
                         str(terminal))
            checks.check("bench_status=passed",
                         bundle.get("bench_status") == "passed",
                         str(bundle.get("bench_status")))
            checks.check("bench.reasons 为空",
                         not (bench.get("reasons") or []),
                         str(bench.get("reasons")))
            checks.check("bench.new_red 为空",
                         not (bench.get("new_red") or []),
                         str(bench.get("new_red")))
            checks.check("bench.new_error 为空",
                         not (bench.get("new_error") or []),
                         str(bench.get("new_error")))
            checks.check("本次版本 active",
                         len(same_run_versions) == 1
                         and same_run_versions[0]["status"] == "active",
                         str([(v["id"], v["status"])
                              for v in same_run_versions]))
            checks.check("内容已应用（与采纳前不同）",
                         after_hash != state.get("original_hash"),
                         f"{after_hash[:16]}… vs "
                         f"{str(state.get('original_hash'))[:16]}…")
            checks.check(
                "active 指针推进到本次版本",
                bool(same_run_versions)
                and current_active_id == same_run_versions[0]["id"],
                f"active={current_active_id} "
                f"run_version={(same_run_versions[0]['id'] if same_run_versions else None)}")
            checks.check("审计 evolution.adopt",
                         audit_adopt + audit_override >= 1,
                         f"adopt={audit_adopt} override={audit_override}")
            checks.check("run.error 为空", not error_text, error_text[:160])
        else:
            checks.check("run 终态 smoke-failed", terminal == "smoke-failed",
                         str(terminal))
            checks.check("bench_status=failed",
                         bundle.get("bench_status") == "failed",
                         str(bundle.get("bench_status")))
            checks.check("bench.reasons 含 new_red",
                         "new_red" in (bench.get("reasons") or []),
                         str(bench.get("reasons")))
            checks.check("bench.new_red 含回归题",
                         regression_name in (bench.get("new_red") or []),
                         str(bench.get("new_red")))
            checks.check("本次版本 rejected",
                         len(same_run_versions) == 1
                         and same_run_versions[0]["status"] == "rejected",
                         str([(v["id"], v["status"])
                              for v in same_run_versions]))
            checks.check("内容已恢复到采纳前",
                         after_hash == state.get("original_hash"),
                         f"{after_hash[:16]}… vs "
                         f"{str(state.get('original_hash'))[:16]}…")
            # 对比**有效基线**（seeded-v0 路径 = 本脚本 seed 的 v0），不是开始前的
            # original_active_id（那条路径恒为 None）。
            checks.check("active 指针未被推进",
                         current_active_id == state.get("baseline_active_id"),
                         f"active={current_active_id} "
                         f"expected={state.get('baseline_active_id')}")
            checks.check("审计 evolution.bench_fail", audit_bench_fail >= 1,
                         f"n={audit_bench_fail}")
            checks.check("run.error 记录基准失败",
                         "基准判定失败" in error_text, error_text[:160])

        if checks.failed:
            _print_arms(bench, regression_name, harmless_names)
            print(f"\n  回归问句 = {facts.get('question')}")
            print(f"  control  = {facts.get('control')}")
            print(f"  靶点     = {facts.get('target_code')}")
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


def build_parser() -> argparse.ArgumentParser:
    # `main` 会**先加载 --env-file 再调用本函数**，所以这里的 env 读取能真正
    # 影响 argparse 默认值（而不是在 import 时被固化）。
    tenant_default = os.environ.get("EVOLUTION_CHECK_TENANT", DEFAULT_TENANT)
    base_default = os.environ.get("EVOLUTION_CHECK_BASE_URL", DEFAULT_BASE_URL)
    user_default = os.environ.get("EVOLUTION_CHECK_USER_ID", DEFAULT_USER_ID)

    parser = argparse.ArgumentParser(
        description="演化台完整语料基准回归拦截（125 真机验收）")
    parser.add_argument("--tenant", default=tenant_default,
                        help=f"目标租户（默认 {tenant_default}；"
                             "可用 EVOLUTION_CHECK_TENANT）")
    parser.add_argument("--kb", required=True,
                        help="真实知识库 ID（必填）")
    parser.add_argument("--ds", required=True,
                        help="真实数据源 ID（必填，必须有只读凭据）")
    parser.add_argument("--base-url", default=base_default,
                        help=f"知识库 API 基址（默认 {base_default}；"
                             "可用 EVOLUTION_CHECK_BASE_URL）")
    parser.add_argument("--user-id", default=user_default,
                        help="管理员用户 ID（默认 1，与 A5 脚本一致；传空串则"
                             "自动查找 tenant:admin）")
    parser.add_argument("--regression-entity",
                        help="回归实体名（默认取语料 control 命中的表对应实体，"
                             "否则 compiled schema 首个实体）")
    parser.add_argument("--regression-question",
                        help="回归问句（默认优先取语料里 control 指向同表的题，"
                             "其次按实体派生，旧已知问法仅兜底）")
    parser.add_argument("--regression-control",
                        help="回归题 control SQL（默认 COUNT(*) FROM 实体表）")
    parser.add_argument(
        "--regression-expression",
        help="query_rule 口径全文（自然语言，不是 SQL 谓词）；默认写死一条"
             "让模型必须过滤掉全部记录的指令")
    parser.add_argument(
        "--expect-pass", action="store_true",
        help="验收通过路径：使用无害口径，断言 run adopted / bench passed / "
             "版本 active（默认是回归拦截路径）")
    parser.add_argument(
        "--pass-expression",
        help="通过路径的 query_rule 口径全文（默认一条不改变记录范围的口径）")
    parser.add_argument("--control-question-a",
                        help="无害题 A 的问句（默认按语料/实体派生）")
    parser.add_argument("--control-question-b",
                        help="无害题 B 的问句（默认按语料/实体派生）")
    parser.add_argument("--control-sql-a", help="无害题 A 的 control SQL")
    parser.add_argument("--control-sql-b", help="无害题 B 的 control SQL")
    parser.add_argument(
        "--harmless-cases", type=int, default=12,
        help="无害语料条数（默认 12；smoke 非目标抽样上限 10，"
             "语料必须 >10 条才能通过换 run_id 把回归题排除在 smoke 外）")
    parser.add_argument("--plan-attempts", type=int, default=20,
                        help="换 run_id 让回归题避开 smoke 抽样的次数上限")
    parser.add_argument("--poll-timeout", type=float, default=3600.0,
                        help="轮询采纳收口的上限秒数（默认 3600）")
    parser.add_argument("--keep", action="store_true",
                        help="跳过清理（仅排障；默认清理）")
    parser.add_argument("--env-file", default=str(_REPO_ROOT / ".env.local"),
                        help="环境变量文件（默认仓库根 .env.local）；"
                             "在解析参数前加载，可影响 EVOLUTION_CHECK_* 默认值")
    parser.add_argument("--no-env-file", action="store_true",
                        help="不自动加载环境变量文件")
    return parser


def _prescan_env_file(argv) -> tuple[str, bool]:
    """在 `argparse` 之前找出 --env-file / --no-env-file。

    默认租户/base-url/user-id 支持 `EVOLUTION_CHECK_*` 环境变量（`--kb` /
    `--ds` 必填，无默认值）；若等到
    `parse_args()` 之后才加载 env 文件，这些默认值已经被求值，文件就不生效。
    这里先扫一遍 argv，再 build_parser（其内部才读 env）。
    返回 `(path, skip)`。
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    if "--no-env-file" in tokens:
        return "", True
    for index, item in enumerate(tokens):
        if item == "--env-file" and index + 1 < len(tokens):
            return tokens[index + 1], False
        if item.startswith("--env-file="):
            return item.split("=", 1)[1], False
    return str(_REPO_ROOT / ".env.local"), False


def main(argv=None) -> int:
    env_path, skip_env = _prescan_env_file(argv)
    if not skip_env:
        loaded = load_env_file(env_path)
        if loaded:
            print(f"已加载环境变量文件: {loaded}")
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
