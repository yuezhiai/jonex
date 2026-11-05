# -*- coding:utf-8 -*-
"""采纳服务的共享内核：协作方单点导入、纯函数助手与内部中止信号。

本模块是 `adopt_service` / `adopt_execute` / `adopt_recovery` 的**唯一**
协作方入口。测试通过 `monkeypatch.setattr(adopt_common, <name>, fake)`
替换依赖；拆分后的执行/回滚模块一律用 `common.<name>` 解析，绝不
`from .adopt_common import <name>`（那会把绑定复制走，patch 静默失效）。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import time
from typing import Iterable

from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import (
    InvalidParameterError,
    JonexException,
    ResourceConflictError,
)

from . import audit, builders, schema_io
from .bench import bench_mode, heartbeat, run_bench_arm_with_retry
from .corpus_service import corpus_ds, load_corpus
from .engine import (
    apply_alias_entries,
    apply_constraint_entries,
    apply_constraint_ops,
    apply_mapping_ops,
    apply_relation_entries,
    apply_tool_schema_entries,
    entry_key,
)
from .ledger_service import LedgerService
from .locking import (
    COMPILED_KINDS,
    advisory_key,
    chain_lock,
    is_abstract_kind,
    scope_for_kind,
    try_chain_lock,
)
from .run_store import RunStore
from .smoke import adhoc_cases, run_smoke, verify_adhoc
from .version_store import VersionStore, canonical_snapshot_hash

logger = logging.getLogger(f"{__package__}.adopt_service")

#: 人审写操作（采纳/否决/条目调整）只接受门禁已出结论的运行。
_ONLY_EVALUATED = frozenset({"evaluated"})
#: 预览额外允许「已采纳」（复核场景）：`already_adopted` 按状态如实回报。
#: 写操作仍只认 evaluated。
_PREVIEW_STATUSES = frozenset({"evaluated", "adopted"})
#: 回滚还允许从「已采纳 / 已否决」回到「已回滚」（与 CLI 的幂等逃生口一致）。
#: `rolled-back` 也在内：重复回滚必须是幂等成功（`rollback.added` 已空 → 0 条），
#: 不能因为「已经回滚过」报 409。
_ROLLBACK_STATUSES = frozenset({"evaluated", "adopted", "rejected",
                                "rolled-back"})
#: `failed` 运行只在这些 stage 上允许回滚：`applied` / `candidate-ran` 是「PUT
#: 已成功过」的证据（schema 里可能真的留着本批条目）；`created`/`drafted` 等
#: 阶段的 failed 从未写过 schema，没有可逆操作，仍按状态冲突拒绝。
_FAILED_ROLLBACK_STAGES = frozenset({"applied", "candidate-ran"})
#: 门禁结论白名单。缺失/未知一律 409（不是「默认 accept」）。
_GATE_DECISIONS = frozenset({"accept", "reject", "uncertain"})
#: ad-hoc truth 的期望结果（与语料 `expect` 同口径）。
_ADHOC_EXPECTS = frozenset({"answer", "refuse"})
#: ad-hoc 人工判定白名单（Task 1）。
_ADHOC_VERDICTS = frozenset({"pass", "fail"})
#: 证据票据有效期（秒）：30 分钟。超过即视为「没看过本次候选数据」。
ADHOC_EVIDENCE_TTL_SECONDS: int = 1800
#: 版本回滚可接受的目标状态：`active`（当前）与 `superseded`（历史已生效）。
#: `candidate`（未评估）与 `rejected`（smoke/应用失败）绝不能恢复上线。
_ROLLBACK_TARGET_STATUSES = frozenset({"active", "superseded"})


#: `_stage_log` 的保留键：只能由函数 kwargs 生成，`extra` 不得覆盖。
_STAGE_RESERVED_KEYS = frozenset({
    "stage", "version_id", "gate_effective", "smoke_status",
    "duration_ms", "picked_count",
})


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


async def _rollback_session(session) -> None:
    """旁路读/写失败后回滚会话，避免外层 commit 撞上失败事务再报 500。

    `session` 可能是测试假件或 None：没有 `rollback` 就跳过；回滚自身失败
    只 warning，不能盖住原始异常。
    """
    rollback = getattr(session, "rollback", None)
    if rollback is None:
        return
    try:
        await rollback()
    except Exception as exc:  # noqa: BLE001 —— 回滚是旁路
        logger.warning("演化会话回滚失败: %s", exc)


async def _stage_log(store, tenant_id: str, run_id: str, stage: str, *,
                     started: float | None = None, version_id: str | None = None,
                     gate_effective: str | None = None,
                     smoke_status: str | None = None,
                     picked_count: int | None = None,
                     extra: dict | None = None) -> None:
    """§9.1 阶段日志：每个阶段写一行 JSON 到 `evolution_run_logs`。

    只记**元数据**（阶段 / 版本 / 门禁 / smoke / 耗时 / 条数），绝不写完整
    SQL 或 schema 内容。`extra` 是附加元数据（如基准臂的 corpus_size /
    passes / new_red），同样只能放计数，不能塞 SQL / 行数据。
    日志是旁路：写失败只 warning，不能推翻主流程。
    """
    # `extra` 先合并，再由**保留键**覆盖：保留键只能由本函数按 kwargs 决定，
    # 调用方传 `extra={"stage": ...}` / `{"duration_ms": ...}` 一律无效。
    payload = {key: value for key, value in (extra or {}).items()
               if key not in _STAGE_RESERVED_KEYS and value is not None}
    payload["stage"] = stage
    if version_id is not None:
        payload["version_id"] = version_id
    if gate_effective is not None:
        payload["gate_effective"] = gate_effective
    if smoke_status is not None:
        payload["smoke_status"] = smoke_status
    if started is not None:
        payload["duration_ms"] = int((time.monotonic() - started) * 1000)
    if picked_count is not None:
        payload["picked_count"] = picked_count
    try:
        await store.log(
            tenant_id, run_id,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001 —— 阶段日志不是关键路径
        logger.warning("演化阶段日志写入失败 run=%s stage=%s: %s",
                       run_id, stage, exc)


async def _candidate_status(session, tenant_id: str, run_id: str,
                            status: str, gate: dict | None = None, *,
                            supersede_others: bool = False) -> bool:
    """候选生命周期推进（I6）；失败只 warning（旁路，不能推翻主流程）。"""
    try:
        from .candidate_service import CandidateService

        await CandidateService(session).update_status(
            tenant_id, run_id, status, gate=gate,
            supersede_others=supersede_others)
        return True
    except Exception as exc:  # noqa: BLE001 —— 候选不是关键路径
        logger.warning("演化候选状态回写失败 run=%s status=%s: %s",
                       run_id, status, exc)
        return False


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_bench_started_at(run: dict | None) -> datetime.datetime | None:
    """run 详情里的 `bench_started_at` → 可绑进 `DateTime(timezone=True)` 的值。

    `RunStore` 的详情投影（`_new_detail_fields`）把 datetime 列转成 **ISO
    字符串**；直接把字符串塞回 `bench_started_at` 列会在 bind 时失败。这里
    解析回 datetime；空值 / 无法解析（`TypeError` / `ValueError`）则返回
    None（调用方据此省略该字段，不写伪造的开始时间）。
    """
    raw = (run or {}).get("bench_started_at")
    if not raw:
        return None
    if isinstance(raw, datetime.datetime):
        return raw
    try:
        return datetime.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _identity(user_id) -> str | None:
    """操作人身份写进 varchar 列：统一转字符串并截到 64。"""
    if user_id is None:
        return None
    text = str(user_id).strip()
    return text[:64] or None


def _backup_hint(run_id: str) -> str:
    """备份落点提示（对齐旧 CLI 的备份文件路径；服务化后备份在 run 行里）。"""
    return f"db://evolution_runs/{run_id}#backup"


def _patch_hint(run_id: str) -> str:
    """补丁来源提示（旧 CLI 写 patch 文件路径，服务化后就是 run 行）。"""
    return f"db://evolution_runs/{run_id}#patch"


def pruned_backup(backup: dict | None) -> dict:
    """采纳成功后替换 `run.backup` 的剪枝占位（只留内容指纹）。

    `run.backup` 只是「采纳前内容」的兜底：恢复路径**只在 run 处于
    `adopting` 时**读它，收口成 `adopted` 之后不会再读（之后要内容就读
    版本快照）。完整快照长期留在 JSONB 里没有用途，反而让运行行随 schema
    体积膨胀；收口时剪掉，仅保留 `sha256` 指纹供事后核对「当时备份的是
    哪一版内容」。

    形状固定为 `{"pruned": True, "content_hash": <hex>}`；`backup` 为空时
    `content_hash` 是 `{}` 的 hash —— 读取侧仍能区分「已剪枝」与「从未备份」。

    指纹口径：`json.dumps(backup or {}, sort_keys=True, ensure_ascii=False,
    default=str)` 的 sha256（`default=str` 兜住 JSONB 里可能出现的非原生类型）。
    """
    canonical = json.dumps(backup or {}, sort_keys=True, ensure_ascii=False,
                           default=str)
    return {"pruned": True,
            "content_hash": hashlib.sha256(
                canonical.encode("utf-8")).hexdigest()}


def _require_status(run: dict, allowed: frozenset[str], action: str) -> None:
    status = run.get("status")
    if status not in allowed:
        raise ResourceConflictError(
            message=(f"{run.get('run_id')} 状态是 {status!r}，只有 "
                     f"{'/'.join(sorted(allowed))} 的 run 允许{action}"))


def _require_same_chain(target: dict, *, scope: str, kb: str,
                        ds: str | None) -> None:
    """回滚目标必须与 run 同一条版本链（同租户由 VersionStore.get 保证）。

    跨 KB / 跨 scope / 跨 DS 一律 409：把别的链的内容恢复过来会让 active
    指针与线上内容错位。
    """
    target_id = target.get("version_id")
    if target.get("kb") != kb:
        raise ResourceConflictError(
            message=(f"目标版本 {target_id} 属于知识库 {target.get('kb')!r}，"
                     f"与 run 的 {kb!r} 不一致"))
    if target.get("scope") != scope:
        raise ResourceConflictError(
            message=(f"目标版本 {target_id} scope 是 {target.get('scope')!r}，"
                     f"与 run 的 {scope!r} 不一致"))
    if scope == "compiled":
        if target.get("ds") != ds:
            raise ResourceConflictError(
                message=(f"目标版本 {target_id} 的数据源是 {target.get('ds')!r}，"
                         f"与 run 的 {ds!r} 不一致"))
    elif target.get("ds") is not None:
        raise ResourceConflictError(
            message=f"目标版本 {target_id} 是 compiled 版本，不能回滚 abstract 链")


def _patch_of(run: dict) -> dict:
    patch = run.get("patch")
    if not isinstance(patch, dict) or not patch:
        raise InvalidParameterError(
            message=f"{run.get('run_id')} 还没有补丁（跑到 gated 再来）")
    return patch


def _pick_entries(patch: dict, picks: list[str] | None) -> list[dict]:
    """按 `entry_key` 过滤本批条目；未知键是 1001，不是静默忽略。

    空 `picks` / `None` = 全取（与旧 CLI `--pick` 缺省同口径）。
    """
    kind = patch.get("kind")
    entries = [e for e in patch.get("entries") or [] if isinstance(e, dict)]
    if picks:
        wanted = set(picks)
        entries = [e for e in entries if entry_key(e, kind) in wanted]
        missing = wanted - {entry_key(e, kind) for e in entries}
        if missing:
            raise InvalidParameterError(
                message=f"picks 里这些键不在本批：{sorted(missing)}")
    if not entries:
        raise InvalidParameterError(message="没有要应用的条目")
    return entries


def _find_entry(patch: dict, key: str) -> dict:
    key = str(key or "")
    kind = patch.get("kind")
    entries = [e for e in patch.get("entries") or [] if isinstance(e, dict)]
    entry = next((e for e in entries if entry_key(e, kind) == key), None)
    if entry is None:
        keys = ", ".join(entry_key(e, kind) for e in entries)
        raise InvalidParameterError(
            message=f"条目不在本批：{key or '(空)'}（本批：{keys[:400]}）")
    return entry


def normalise_request_id(value) -> str | None:
    """采纳幂等键：None → None（未提供）；空白/非字符串/超长 → 1001。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParameterError(message="request_id 必须是字符串")
    text = value.strip()
    if not text:
        raise InvalidParameterError(message="request_id 不能为空")
    if len(text) > 64:  # evolution_runs.adopt_request_id VARCHAR(64)
        raise InvalidParameterError(message="request_id 超长（> 64）")
    return text


def normalise_version_id(value) -> str | None:
    """回滚目标版本 id：None → None（未提供）；空白/非字符串/超长 → 1001。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParameterError(message="to_version_id 必须是字符串")
    text = value.strip()
    if not text:
        raise InvalidParameterError(message="to_version_id 不能为空")
    if len(text) > 64:  # semantic_versions.id VARCHAR(64)
        raise InvalidParameterError(message="to_version_id 超长（> 64）")
    return text


def normalize_adhoc_truths(value) -> list[dict] | None:
    """`adhoc_truths` 形状校验（能力层与服务层共用）。

    `None` 表示未提供；其余必须是 `[{case_id, control: [SQL...], expect}]`，
    `expect=answer` 必须有非空 control。非法一律 1001。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidParameterError(message="adhoc_truths 必须是数组")
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise InvalidParameterError(
                message="adhoc_truths 的每一项必须是对象")
        case_id = str(item.get("case_id") or "").strip()
        if not case_id:
            raise InvalidParameterError(
                message="adhoc_truths 的每一项都必须有非空 case_id")
        if case_id in seen:
            raise InvalidParameterError(
                message=f"adhoc_truths 里 case_id 重复：{case_id}")
        expect = str(item.get("expect") or "").strip()
        if expect not in _ADHOC_EXPECTS:
            raise InvalidParameterError(
                message=f"{case_id} 的 expect 必须是 answer 或 refuse")
        control = item.get("control")
        if control is None:
            control = []
        if (not isinstance(control, list)
                or any(not isinstance(sql, str) or not sql.strip()
                       for sql in control)):
            raise InvalidParameterError(
                message=f"{case_id} 的 control 必须是 SQL 字符串数组")
        if expect == "answer" and not control:
            raise InvalidParameterError(
                message=f"{case_id} 的 expect=answer 时必须提供 control")
        seen.add(case_id)
        out.append({"case_id": case_id,
                    "control": [sql.strip() for sql in control],
                    "expect": expect})
    return out


def normalize_adhoc_verdicts(value) -> list[dict] | None:
    """`adhoc_verdicts` 形状校验（能力层与服务层共用）。

    `None` 表示未提供；其余必须是 `[{case_id, verdict, evidence_id?, note?}]`，
    `verdict=pass` 必须有非空 `evidence_id`（用户确实看过本次候选的证据），
    `fail` 的 `evidence_id` 归一为 None。非法一律 1001。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidParameterError(message="adhoc_verdicts 必须是数组")
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise InvalidParameterError(
                message="adhoc_verdicts 的每一项必须是对象")
        case_id = str(item.get("case_id") or "").strip()
        if not case_id:
            raise InvalidParameterError(
                message="adhoc_verdicts 的每一项都必须有非空 case_id")
        if case_id in seen:
            raise InvalidParameterError(
                message=f"adhoc_verdicts 里 case_id 重复：{case_id}")
        verdict = str(item.get("verdict") or "").strip()
        if verdict not in _ADHOC_VERDICTS:
            raise InvalidParameterError(
                message=f"{case_id} 的 verdict 必须是 pass 或 fail")
        evidence_id = item.get("evidence_id")
        if verdict == "pass":
            evidence_id = str(evidence_id or "").strip()
            if not evidence_id:
                raise InvalidParameterError(
                    message=f"{case_id} 的 verdict=pass 时必须提供 evidence_id")
        else:
            evidence_id = None
        note = item.get("note")
        note = None if note is None else (str(note).strip()[:200] or None)
        seen.add(case_id)
        normalized = {"case_id": case_id, "verdict": verdict,
                      "evidence_id": evidence_id, "note": note}
        # spec §4.4 第 6 条：人工判定要带「当时看到的数据摘要」，但不落完整
        # rows。两个键都是可选的（前端会透传；旧调用方不带也不报错）。
        row_count = item.get("row_count")
        if isinstance(row_count, int) and not isinstance(row_count, bool) \
                and row_count >= 0:
            normalized["row_count"] = row_count
        digest = item.get("rows_hash")
        if isinstance(digest, str) and digest.strip():
            normalized["rows_hash"] = digest.strip()[:64]
        out.append(normalized)
    return out


def rows_hash(rows: list | None) -> str:
    """证据行的内容指纹（sha256，口径与票据同款规范化）。

    人工验真的 evidence 只落 `row_count` / `rows_hash`，**不落完整 rows**
    （spec §4.4 第 6 条）：行数据可能很大，也含业务明细；保留指纹即可事后
    核对「当时看到的是哪一批数据」。空结果也会算出确定值（空数组的 hash）。
    """
    canonical = json.dumps(list(rows or []), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_evidence_id(entries: list[dict], case_id: str, issued_at: int) -> str:
    """签发证据票据：`<issued_at>.<sha256(payload)[:32]>`。

    票据只绑定 `entries + case_id + 签发时间`，**不含 rows**：服务端不缓存
    查询结果，保证的是「用户确实调用过本次候选的证据接口且未过期」，不是
    「数据逐字节未变」。换 picks / 换候选都会改变 entries，票据随之失效。
    """
    issued_at = int(issued_at)
    payload = {"entries": entries, "case_id": case_id, "issued_at": issued_at}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{issued_at}.{digest[:32]}"


def check_evidence_id(token: str, entries: list[dict], case_id: str, *,
                      now: int | None = None) -> bool:
    """校验票据：格式 / 有效期（30 分钟）/ 与本次 entries + case_id 的摘要。

    任何不合法（非字符串、无 `.`、issued_at 非整数、摘要不匹配、过期）都
    返回 False，不抛异常 —— 调用方按 409 收口。
    """
    if not isinstance(token, str) or "." not in token:
        return False
    issued_text, _, _digest = token.partition(".")
    try:
        issued_at = int(issued_text)
    except (TypeError, ValueError):
        return False
    now = int(time.time()) if now is None else int(now)
    if now - issued_at > ADHOC_EVIDENCE_TTL_SECONDS:
        return False
    return hmac.compare_digest(token,
                               make_evidence_id(entries, case_id, issued_at))


def picked_adhoc_cases(run: dict, picks: list[str] | None) -> list[dict]:
    """picks 选中条目对应的**待验真** ad-hoc case。

    只返回 `verification=unverified` 的 case；选中 entries 里没有任何
    `question` 时（非 alias kind / 旧形状）保守返回全部，避免悄悄缩小验真
    范围。未知 picks 键仍由 `_pick_entries` 报 1001。
    """
    patch = (run or {}).get("patch") if isinstance(run, dict) else {}
    patch = patch if isinstance(patch, dict) else {}
    entries = _pick_entries(patch, picks)
    case_ids = {builders.adhoc_case_id(entry.get("question"))
                for entry in entries if entry.get("question")}
    cases = _unverified_adhoc_cases(run or {})
    if not case_ids:
        return cases
    return [case for case in cases
            if str(case.get("case_id") or "") in case_ids]


def manual_adhoc_outcomes(cases: list[dict], verdicts: list[dict]) -> list[dict]:
    """把人工 pass/fail 判定转成与 `verify_adhoc` 同形状的 outcomes。

    人审结论取代自动 judge：`verification=verified`、`verified_by=human`，
    并带上票据供审计追溯。没有对应 verdict 的 case 不生成 outcome（覆盖
    完整性由 `_preflight` 保证，这里不凭空补一条 pass）。
    """
    verdict_by_id = {str(v.get("case_id")): v for v in verdicts or []
                     if isinstance(v, dict) and v.get("case_id")}
    out: list[dict] = []
    for case in cases or []:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("case_id") or "")
        verdict = verdict_by_id.get(case_id)
        if verdict is None:
            continue
        passed = verdict.get("verdict") == "pass"
        outcome = {
            "case_id": case_id,
            "verification": "verified",
            "last_verification": "verified",
            "truth": None,
            "parent_outcome": "fail",
            "outcome": "pass" if passed else "fail",
            "why": "人工验真通过" if passed else "人工验真不通过",
            "verified_by": "human",
            "evidence_id": verdict.get("evidence_id"),
        }
        # spec §4.4 第 6 条：证据摘要进 outcome，**不落完整 rows**。
        # 只透传调用方实际提供的键（旧 truths/调用方不带就不物化）。
        if verdict.get("row_count") is not None:
            outcome["row_count"] = verdict["row_count"]
        if verdict.get("rows_hash"):
            outcome["rows_hash"] = verdict["rows_hash"]
        out.append(outcome)
    return out


def _plan_synonym_entries(groups: list | None,
                          entries: list[dict]) -> tuple[list, list]:
    """同义词补丁的**纯函数**预演（预览用；不写库、不调用仓储）。

    口径与 `schema_io._apply_synonym_patch` 对齐到「能看到发生了什么」的粒度：
    按归一后的 terms 集合匹配（顺序无关、忽略大小写与空白），`delete` 找不到
    记 `not_found`，同 terms 同 canonical 记 `exists`，其余算生效项。真正的
    跨组词冲突/软删语义由 apply 阶段裁决，预览不替代它。
    """
    def _terms(raw) -> tuple[list[str], tuple[str, ...]]:
        display: list[str] = []
        seen: set[str] = set()
        for value in raw if isinstance(raw, list) else []:
            text = str(value or "").strip()
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            display.append(text)
        return display, tuple(sorted(seen))

    index: dict[tuple, str] = {}
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        _display, keys = _terms(group.get("terms"))
        if keys:
            index[tuple(keys)] = str(group.get("canonical") or "").strip()

    planned: list[dict] = []
    skipped: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        terms, keys = _terms(entry.get("terms"))
        canonical = str(entry.get("canonical") or "").strip()
        op = str(entry.get("op") or "add").strip().lower()
        if not terms:
            skipped.append({"terms": terms, "canonical": canonical,
                            "why": "empty_terms"})
            continue
        key = tuple(keys)
        if op == "delete":
            if key not in index:
                skipped.append({"terms": terms, "canonical": canonical,
                                "op": "delete", "why": "not_found"})
            else:
                planned.append({"terms": terms, "canonical": canonical,
                                "op": "delete", "before": index.pop(key)})
            continue
        if not canonical:
            skipped.append({"terms": terms, "canonical": canonical,
                            "why": "empty_canonical"})
            continue
        if index.get(key) == canonical:
            skipped.append({"terms": terms, "canonical": canonical,
                            "why": "exists"})
        else:
            planned.append({"terms": terms, "canonical": canonical,
                            "op": "update" if key in index else "add"})
            index[key] = canonical
    return planned, skipped


def bump_value_aliases(kind: str | None, tenant_id: str, kb: str) -> bool:
    """取值同义词缓存失效（仅 synonym；失败只 warning，绝不推翻已成功的写）。

    缓存键含 `(tenant, kb, generation)`；读口在另一进程/同一进程都可能命中
    旧的 `ontology_synonyms` 内容，所以采纳/回滚成功后必须推进写代号。
    """
    if str(kind) != "synonym":
        return False
    try:
        from ..nl2sql import value_aliases

        value_aliases.bump_write_generation(tenant_id, kb)
        return True
    except Exception as exc:  # noqa: BLE001 —— 缓存失效是旁路
        logger.warning("同义词缓存失效失败 tenant=%s kb=%s: %s",
                       tenant_id, kb, exc)
        return False


def _gate_effective(decision, adhoc) -> str | None:
    """spec §3.6：ad-hoc `reject` 一票否决；`uncertain` 只能靠 ad-hoc 升 accept。

    `reject` 永不被 ad-hoc 翻案；`accept` 也必须 `gate_adhoc != reject`
    （ad-hoc 补 truth 后验证失败 → effective 直接 reject）。
    """
    if decision not in _GATE_DECISIONS:
        return None
    if adhoc == "reject":
        return "reject"
    if decision == "accept":
        return "accept"
    if decision == "uncertain" and adhoc == "accept":
        return "accept"
    return decision


def _adhoc_cases(run: dict) -> list[dict]:
    """候选 evidence 里的 ad-hoc case（Task 3 写在 `patch.candidate.evidence`）。"""
    return adhoc_cases(run)


def _unverified_adhoc_cases(run: dict) -> list[dict]:
    return [c for c in _adhoc_cases(run)
            if c.get("verification") == "unverified"]


def _require_adhoc_coverage(run: dict, truths: list[dict] | None) -> dict:
    """候选含 unverified ad-hoc 时，truths 必须**恰好**覆盖这些 case。

    本任务只校验与透传；补跑并写回 `verification=verified` / `gate_adhoc`
    留给 Task 5。

    没有 unverified case 时，客户端仍带非空 `adhoc_truths` 属于无效请求：
    静默忽略会掩盖前端/调用方的状态错位，故显式 1001。
    """
    unverified = [str(c["case_id"]) for c in _unverified_adhoc_cases(run)]
    if not unverified:
        if truths:
            raise InvalidParameterError(
                message="本候选没有待补 truth 的 ad-hoc case，"
                        "不应提供 adhoc_truths")
        return {}
    by_id = {t["case_id"]: t for t in truths or []}
    missing = [cid for cid in unverified if cid not in by_id]
    if missing:
        raise InvalidParameterError(
            message=("候选含未补 truth 的 ad-hoc case，adhoc_truths 必须覆盖："
                     + "、".join(missing)))
    unknown = [cid for cid in by_id if cid not in set(unverified)]
    if unknown:
        raise InvalidParameterError(
            message="adhoc_truths 含不属于本候选的 case：" + "、".join(unknown))
    return by_id


def _require_verdict_coverage(cases: list[dict],
                              verdicts: list[dict]) -> None:
    """人工验真结论必须**恰好**覆盖本次 picks 影响到的 unverified case。

    与 `_require_adhoc_coverage` 同口径（缺失 / 多余 / 重复 → 1001），区别只在
    「必填集合」：verdicts 路径只看 picks 选中条目关联的 case，未选中的 case 不
    阻塞采纳（spec §4.4 第 2 条）。`accept` 与 `execute` 共用（execute 是公开
    后台入口，直接调用时也要做同一道校验）。
    """
    expected = [str(case.get("case_id") or "") for case in cases or []]
    provided = [str((v or {}).get("case_id") or "") for v in verdicts or []]
    duplicates = list(dict.fromkeys(
        cid for cid in provided if provided.count(cid) > 1))
    if duplicates:
        raise InvalidParameterError(
            message="adhoc_verdicts 里 case_id 重复：" + "、".join(duplicates))
    provided_set = set(provided)
    missing = [cid for cid in expected if cid not in provided_set]
    if missing:
        raise InvalidParameterError(
            message=("本次 picks 影响到的 ad-hoc case 需要提供验真结论："
                     + "、".join(missing)))
    expected_set = set(expected)
    unknown = [cid for cid in provided if cid not in expected_set]
    if unknown:
        raise InvalidParameterError(
            message="adhoc_verdicts 含不属于本次候选的 case："
                    + "、".join(unknown))


async def _active_as_parent(vs, tenant_id: str, kb: str, scope: str,
                            ds: str | None) -> dict | None:
    """取当前 active 版本作为 legacy 采纳的父版本（v0 兜底）。

    唯一索引下至多一条 active；`list` 里仍按防御口径挑出 active，防御分支
    取末位。不承诺「最早」——这里要的就是**当前** active 作 parent。
    """
    page = await vs.list(tenant_id, kb, scope, ds, include_archived=False,
                         limit=200)
    actives = [v for v in page.get("items") or []
               if isinstance(v, dict) and v.get("status") == "active"]
    return actives[-1] if actives else None


async def _seed_baseline_version(session, tenant_id: str, kind: str, kb: str,
                                 ds: str | None, scope: str,
                                 run_id: str | None = None) -> dict | None:
    """链上无任何版本时，用当前行快照补一条 v0 基线并激活。

    返回版本 dict（`version_id` 可用）；当前行内容为空 / 读取失败 → None
    （调用方保留原来的「没有父版本且没有 v0」错误，绝不拿空内容建版本）。
    `run_id` 只作归属（`source_run_id`），让来源可追溯。
    """
    snapshot = await schema_io.snapshot_for(tenant_id, kind, kb, ds)
    if not isinstance(snapshot, dict) or snapshot.get("content") is None:
        return None
    store = VersionStore(session)
    created = await store.create_candidate(
        tenant_id, kb, scope, ds, snapshot,
        source_run_id=run_id,
        created_by="system", note="v0 baseline (auto)")
    try:
        await store.activate(tenant_id, created["version_id"], previous_id=None)
    except IntegrityError:
        # 并发首次采纳：另一进程已抢先激活了基线。回滚本事务后按当前
        # active 收口（拿不到就返回 None，由调用方报原错误）。
        await _rollback_session(session)
        # 输掉的候选行已提交过：best-effort 驳回，别让它一直躺在版本列表里。
        try:
            await store.reject_version(tenant_id, created["version_id"])
        except Exception as exc:  # noqa: BLE001 —— 清理是旁路
            logger.warning("v0 基线孤儿候选驳回失败 version=%s: %s",
                           created.get("version_id"), exc)
        return await _active_as_parent(store, tenant_id, kb, scope, ds)
    created["status"] = "active"
    return created


async def _restore_evaluated(store, tenant_id: str, run_id: str) -> None:
    """把 `adopting` 回退到 `evaluated` 并清掉本次尝试留下的 override 痕迹。

    `adopted_by` / `override_by` / `override_reason` / `gate_override` 是「这一
    次采纳」的事实：回退后若不清，重试时会看到上一次的 override 标记（而实际
    那次并没有采纳），前端也会把 run 渲染成「已 override 采纳」。恢复失败只记
    warning（原始异常优先）。
    """
    try:
        await store.update(
            run_id, tenant_id=tenant_id, status="evaluated",
            adopted_by=None, override_by=None, override_reason=None,
            gate_override=0, expect_statuses={"adopting"})
    except Exception as exc:  # noqa: BLE001 —— 原始异常优先
        logger.warning("采纳失败后回退 evaluated 失败 run=%s: %s", run_id, exc)


async def _release_evaluated(store, tenant_id: str, run_id: str, *,
                             error: str, bench: dict | None = None,
                             bench_status: str | None = "failed",
                             bench_started_at=None,
                             bench_ended_at=None) -> None:
    """采纳尚未写 schema 时的收口：回 `evaluated` 并清掉本次尝试的痕迹。

    与 `_restore_evaluated` 同口径清 `adopted_by` / `override_by` /
    `override_reason` / `gate_override`；额外写基准状态（Task 6）：
    `bench` / `bench_status` / `bench_started_at` / `bench_ended_at`。
    恢复失败只记 warning（原始异常 / 主流程优先）。
    """
    fields: dict = {
        "adopted_by": None,
        "override_by": None,
        "override_reason": None,
        "gate_override": 0,
        "error": error,
    }
    if bench is not None:
        fields["bench"] = bench
    if bench_status is not None:
        fields["bench_status"] = bench_status
    if bench_started_at is not None:
        fields["bench_started_at"] = bench_started_at
    if bench_ended_at is not None:
        fields["bench_ended_at"] = bench_ended_at
    try:
        await store.update(
            run_id, tenant_id=tenant_id, status="evaluated",
            expect_statuses={"adopting"}, **fields)
    except Exception as exc:  # noqa: BLE001 —— 原始异常优先
        logger.warning("基准收口回退 evaluated 失败 run=%s: %s", run_id, exc)


def _execute_receipt(run: dict) -> dict:
    """`execute` 的只读回执：run 已不在 `adopting`（重放 / 已被并发收口）。

    `adopted` 按状态如实回报；`bench_status` 原样带回。绝不写库。
    """
    status = run.get("status")
    adopted = status == "adopted"
    return {
        "ok": adopted, "rc": 0 if adopted else 1,
        "stdout_tail": ("已采纳（幂等重放，未重复写入）" if adopted
                        else f"运行状态是 {status}，未执行采纳"),
        "run": run,
        "patch": run.get("patch"),
        "smoke": run.get("smoke"),
        "adopted": adopted,
        "accepted": False,
        "bench": run.get("bench"),
        "bench_status": run.get("bench_status"),
        "version_id": run.get("adopted_version_id"),
        "gate": run.get("gate") or None,
        "gate_effective": run.get("gate_effective"),
        "gate_adhoc": run.get("gate_adhoc"),
        "override": bool(run.get("gate_override")),
        "idempotent_replay": True,
    }


def _execute_failed_receipt(run: dict, message: str) -> dict:
    """执行侧主动放弃（stale / 执行器错误）的结果回执（`adopted=False`）。"""
    out = _execute_receipt(run)
    out.update(
        ok=False, rc=1, stdout_tail=message,
        error=run.get("error") or message,
        idempotent_replay=False,
    )
    return out


def _corpus_cases(corpus) -> list[dict]:
    """语料 case 列表（与 `bench.run_bench_arm_with_retry` 同口径过滤）。"""
    return [c for c in (corpus or {}).get("cases") or []
            if isinstance(c, dict)]


async def _bench_arm(tenant_id: str, *, kb: str, ds: str | None, corpus: dict,
                     run_id: str, user_id=None,
                     cases: list[str] | None = None) -> dict:
    """跑一臂并把执行器异常收成「无效臂」（不能让裸异常打断收口）。

    `run_bench_arm_with_retry` 在模块顶层 import：测试直接 patch 这个名字。
    """
    try:
        return await run_bench_arm_with_retry(
            tenant_id, kb=kb, ds=ds, corpus=corpus, run_id=run_id,
            user_id=user_id, cases=cases)
    except Exception as exc:  # noqa: BLE001 —— 执行器整体不可用按无效臂收口
        logger.warning("基准臂执行失败 run=%s cases=%s: %s",
                       run_id, cases, exc)
        return {"report": {"results": []}, "arm": None, "attempt": 0,
                "valid": False}


def _arm_results(arm_result: dict) -> list[dict]:
    report = (arm_result or {}).get("report")
    if not isinstance(report, dict):
        return []
    return [r for r in report.get("results") or [] if isinstance(r, dict)]


async def _ruler_hashes(tenant_id: str, kb: str,
                        bench_ds: str | None) -> dict:
    """两把尺子的内容 hash：KB 抽象 schema + 目标数据源的 compiled schema。

    基准问句**同时**读抽象 schema（概念选择）与 compiled schema（SQL 生成），
    所以「同一把尺子」必须覆盖两条链。只校验被采纳的那条链不够：同一 KB
    另一条链上的并发采纳/回滚会在两臂之间换掉尺子，把「不同尺子的两臂」
    拿来比较会产出假通过（review Critical #1 / 裁定 R8）。

    `bench_ds` 缺失（语料目标未解析出数据源）时只算抽象链 —— 空语料路径
    根本不会跑臂，这里只是防御。
    """
    abstract = await schema_io.snapshot_for(tenant_id, "alias", kb, None)
    hashes = {"abstract": canonical_snapshot_hash(abstract)}
    if bench_ds:
        compiled = await schema_io.snapshot_for(
            tenant_id, "query_rule", kb, bench_ds)
        hashes["compiled"] = canonical_snapshot_hash(compiled)
    return hashes


def _ruler_changed(before: dict | None, after: dict | None) -> bool:
    """两次 ruler hash 是否不一致（任一条链变化都算「换了尺子」）。"""
    return before != after


def _unadopted_facets(kind: str | None, run_ds: str | None,
                      bench_ds: str | None) -> set[str]:
    """本次 apply **不会**触及的 ruler facet（`abstract` / `compiled`）。

    父本臂结束到候选 baseline 之间的窗口里，apply 会合法地改掉**被采纳**
    的那条链 —— 跨臂守卫只能比较非采纳 facet，否则正常采纳会被误判成
    执行器错误。

    - 抽象家族（`alias` / `mapping` / `tool_schema`）写抽象链：非采纳的是基准数据源的
      compiled（无基准源时为空，因为 `_ruler_hashes` 根本不给 compiled facet）；
    - compiled 家族（`query_rule` / `relation` / `constraint`）写 `run_ds` 的
      compiled：基准源就是同一条时非采纳的是 abstract；不同源时两条 benchmark
      尺子都不由本次 apply 负责。
    未知 kind 返回空集合（不猜；候选臂自身的全 facet 守卫仍然生效）。
    """
    if is_abstract_kind(kind):
        return {"compiled"} if bench_ds else set()
    if kind in COMPILED_KINDS:
        return {"abstract"} if run_ds == bench_ds else {"abstract", "compiled"}
    return set()


def _ruler_facets_changed(before: dict | None, after: dict | None,
                          facets: Iterable[str]) -> set[str]:
    """在给定 facet 上比较两次 ruler hash，返回真正变化的 facet 名。

    任一侧面缺该 facet（空语料 / 无 bench_ds）时跳过 —— 没有取样就谈不上
    「变了」，不能把缺失当成变化。
    """
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    return {facet for facet in facets
            if facet in before and facet in after
            and before[facet] != after[facet]}


#: ruler facet 的面向人标签（收口文本 / 诊断用；名字保持稳定便于检索）。
_FACET_LABELS = {"abstract": "抽象 schema", "compiled": "编译 schema"}


def _facet_labels(facets: Iterable[str]) -> str:
    """把变化的 facet 排序后拼成中文标签串（未登记的名字原样保留）。"""
    return "、".join(_FACET_LABELS.get(facet, facet)
                     for facet in sorted(facets))


def _replay_success(run: dict) -> dict:
    """同 request_id 重放已采纳的 run：原结果形状 + `idempotent_replay=true`。

    Task 5 追加 `accepted=False`（这是**已完成**的采纳，不是新受理）与
    `bench_status`（基准结果可能已落库，前端轮询时按它渲染）。
    """
    return {
        "ok": True, "rc": 0,
        "stdout_tail": "已采纳（幂等重放，未重复写入）",
        "run": run,
        "patch": run.get("patch"),
        "smoke": run.get("smoke"),
        "adopted": True,
        "accepted": False,
        "bench_status": run.get("bench_status"),
        "version_id": run.get("adopted_version_id"),
        "gate": run.get("gate") or None,
        "gate_effective": run.get("gate_effective"),
        "gate_adhoc": run.get("gate_adhoc"),
        "override": bool(run.get("gate_override")),
        "idempotent_replay": True,
    }


def _replay_failure(run: dict) -> dict:
    """同 request_id 重放失败态：可区分地返回原失败结果（不重复写库）。"""
    status = run.get("status")
    return {
        "ok": False, "rc": 1,
        "stdout_tail": f"该采纳请求此前已失败（{status}）",
        "run": run,
        "patch": run.get("patch"),
        "smoke": run.get("smoke"),
        "adopted": False,
        "accepted": False,
        "bench_status": run.get("bench_status"),
        "version_id": run.get("adopted_version_id"),
        "gate": run.get("gate") or None,
        "gate_effective": run.get("gate_effective"),
        "gate_adhoc": run.get("gate_adhoc"),
        "override": bool(run.get("gate_override")),
        "status": status,
        "error": run.get("error") or "",
        "idempotent_replay": True,
    }


def _replay_accepted(run: dict, request_id: str | None) -> dict:
    """`adopting` + 同 request_id：受理已受理过，原样回执（不重复写库）。

    Task 5 的异步受理语义：第一次 `accept` 已经把 run CAS 到 `adopting`，
    后台执行（Task 6）还没收口。此时同 request_id 再打一次不是冲突，而是
    「你的请求已受理」——前端据此继续轮询。
    """
    return {
        "ok": True, "rc": 0,
        "accepted": True,
        "adopted": False,
        "run_id": run.get("run_id"),
        "status": "adopting",
        "request_id": request_id,
        "bench_status": run.get("bench_status"),
        "idempotent_replay": True,
    }


class _AdoptAbort(Exception):
    """采纳流程内部的中止信号：由统一收口恢复快照并写运行状态。

    `extra_fields` 是随收口一起落库的附加字段（Task 6 用它写基准结果：
    `bench` / `bench_status` / `bench_ended_at`）。
    """

    def __init__(self, status: str, message: str, *,
                 audit_action: str | None = None,
                 error: str | None = None,
                 extra_fields: dict | None = None,
                 raise_conflict: bool = True) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.audit_action = audit_action
        self.error = error
        self.extra_fields = extra_fields
        # 判定失败（bench_fail / smoke_fail）是 409；执行器错误 / stale 只回执。
        self.raise_conflict = raise_conflict


def _patch_with_evidence(patch: dict, evidence: dict) -> dict:
    """把 verify_adhoc 回写的 evidence 合并进 patch.candidate（不改入参）。"""
    patch = patch if isinstance(patch, dict) else {}
    candidate = patch.get("candidate")
    if isinstance(candidate, dict):
        return {**patch,
                "candidate": {**candidate, "evidence": evidence}}
    return {**patch, "candidate": {"evidence": evidence}}


def _apply_may_have_written(exc: Exception) -> bool:
    """apply_patch 抛错时，PUT 是否可能已经落地。

    - `零条生效`：写库前抛出 → 没有可恢复内容；
    - JonexException（乐观锁冲突等）：PUT 未成功，恢复快照会误伤并发赢家；
    - 其余（含「回读核对失败」）：PUT 可能已发出 → 必须按快照恢复。
    """
    if isinstance(exc, JonexException):
        return False
    if isinstance(exc, RuntimeError) and "零条生效" in str(exc):
        return False
    return True


def _smoke_summary(smoke: dict) -> str:
    reasons = (smoke or {}).get("reasons") or []
    parts = [f"{item.get('case_id') or '-'}:{item.get('reason')}"
             for item in reasons[:5] if isinstance(item, dict)]
    return "smoke " + ("，".join(parts) if parts else "failed")


async def _reject_version(versions, tenant_id: str, version: dict | None) -> None:
    """失败收口：版本置 rejected（best-effort，不能盖住主流程异常）。"""
    if not version:
        return
    try:
        await versions.reject_version(tenant_id, version["version_id"])
    except Exception as exc:  # noqa: BLE001 —— 版本收口失败只记日志
        logger.warning("版本驳回失败 version=%s: %s",
                       version.get("version_id"), exc)


async def _undo_rollback_switch(tenant_id: str, versions, *,
                                version: dict | None,
                                previous: dict | None) -> None:
    """回滚半途失败时的补偿：线上内容与 active 指针都退回切换前（best-effort）。

    `restore_snapshot` 已落库、但新建版本或 run 收口失败时调用；任何一步失败
    只记日志，不能盖住原始异常。
    """
    previous_id = (previous or {}).get("version_id")
    try:
        if previous:
            await schema_io.restore_snapshot(tenant_id, previous)
    except Exception as exc:  # noqa: BLE001 —— 内容回退失败只记日志
        logger.warning("回滚补偿恢复旧快照失败 tenant=%s: %s", tenant_id, exc)
    await _reject_version(versions, tenant_id, version)
    if previous_id:
        try:
            await versions.activate(tenant_id, previous_id, previous_id=None)
        except Exception as exc:  # noqa: BLE001 —— 指针补偿失败只记日志
            logger.warning("回滚补偿重新激活旧版本失败 version=%s: %s",
                           previous_id, exc)


async def _restore_after_failure(store, versions, tenant_id: str, run_id: str,
                                 restore_source: dict | None, *,
                                 version: dict | None = None,
                                 status: str, error: str,
                                 gate_fields: dict | None = None,
                                 extra_fields: dict | None = None,
                                 patch: dict | None = None
                                 ) -> tuple[str, str]:
    """恢复**采纳前**内容 + 版本 rejected + run 收口。

    - 旧 kind（abstract / compiled）：`restore_source` 由
      `schema_io.restore_source(pre, run.backup, ...)` 构造；为 None 表示 pre 与
      backup 都拿不到内容 → 恢复失败（run 升级 failed），绝不用空内容或
      post-state 覆盖。
    - **synonym**：只能按 `patch.rollback.added` 逆操作（`_rollback_synonym_patch`）。
      同义词行是共享表，失败期间可能有**人工新增组**：按 pre 快照整表 reconcile
      会把它们一起软删。完整重建只留给显式版本恢复（`restore_snapshot`）。

    返回 `(final_status, message)`；恢复失败时升级成 `failed` 并保留原因
    （spec §4.1 step 7-9 / §5）。
    """
    restore_error = ""
    is_synonym = (str((patch or {}).get("kind") or "") == "synonym"
                  or str((restore_source or {}).get("scope") or "") == "synonym")
    if is_synonym:
        added = ((patch or {}).get("rollback") or {}).get("added") or []
        if added:
            try:
                await schema_io.rollback_patch(tenant_id, patch)
            except Exception as exc:  # noqa: BLE001 —— 恢复失败要显式收口
                restore_error = str(exc)
                logger.warning("采纳失败后按台账恢复同义词失败 run=%s: %s",
                               run_id, exc)
        # 没有 added = apply 未落任何同步写；不做整表 reconcile（否则会误删
        # 人工编辑）。这里不是错误，run 按 status 正常收口。
    elif restore_source is None:
        restore_error = "没有可用的采纳前快照（pre 与 run.backup 都为空）"
    else:
        try:
            await schema_io.restore_snapshot(tenant_id, restore_source)
        except Exception as exc:  # noqa: BLE001 —— 恢复失败要显式收口
            restore_error = str(exc)
            logger.warning("采纳失败后恢复快照失败 run=%s: %s", run_id, exc)
    await _reject_version(versions, tenant_id, version)
    await _stage_log(store, tenant_id, run_id, "restored",
                     version_id=(version or {}).get("version_id"))
    final_status = "failed" if restore_error else status
    message = error
    if restore_error:
        message = f"{error}；恢复快照失败：{restore_error}"
    # Task 6：基准结果（bench / bench_status / bench_ended_at）先落，再让
    # 门禁事实覆盖同键（gate_fields 在冲突时胜出）。
    fields = dict(extra_fields or {})
    fields.update(gate_fields or {})
    if final_status == "evaluated":
        # 回到「待采纳」：本次尝试的采纳/override 事实必须清掉（与
        # `_restore_evaluated` 同口径），否则重试时 run 会残留上一次的
        # adopted_by / override_by / override_reason / gate_override=1，前端
        # 误渲染成「已 override 采纳」。`failed` / `smoke-failed` 是终态，
        # 保留事实。
        fields.update(adopted_by=None, override_by=None,
                      override_reason=None, gate_override=0)
    if final_status in ("failed", "smoke-failed"):
        fields["error"] = message
    updated = False
    try:
        updated = await store.update(
            run_id, tenant_id=tenant_id, status=final_status,
            expect_statuses={"adopting"}, **fields)
    except Exception as exc:  # noqa: BLE001 —— 收口失败只记日志
        logger.warning("采纳失败后收口 run=%s status=%s 失败: %s",
                       run_id, final_status, exc)
    if final_status == "evaluated" and updated:
        # evaluated 可被再次受理：**不写 run.error**（否则旧中止文本会变成
        # 失败横幅，accept 还得再清一次）。中止详情只进 run log 供复盘。
        # CAS 未命中（run 已被并发收口）时不追加日志 —— 本次尝试没有改变
        # 任何状态，日志行只会给实际收口者制造噪音。
        # `_stage_log` 内部吞掉日志异常（旁路），不会推翻本次收口。
        await _stage_log(store, tenant_id, run_id, "adopt-abort",
                         extra={"error": message})
    return final_status, message


async def _record_audit(store, session, *, tenant_id: str, run_id: str,
                        action: str | None, outcome: str,
                        request_params: dict, user_id,
                        warnings: list[str],
                        error_message: str | None = None) -> None:
    """写审计；失败时补 `evolution_run_logs` 并把 warning 带回响应（I7）。"""
    if not action:
        return
    ok = await audit.record(
        session, tenant_id=tenant_id, action=action, outcome=outcome,
        run_id=run_id, request_params=request_params,
        error_message=error_message, user_id=user_id)
    if ok is False:
        message = f"审计写入失败：{action}"
        warnings.append(message)
        try:
            await store.log(tenant_id, run_id, message)
        except Exception:  # noqa: BLE001 —— 日志也是旁路
            pass


async def _trial(tenant_id: str, patch: dict,
                 entries: list[dict]) -> tuple[list, list]:
    """读当前行 + 纯函数试算（不写库）。返回 (planned, skipped)。"""
    kind, kb, ds = patch.get("kind"), patch.get("kb"), patch.get("ds")
    row = await schema_io.get_row(tenant_id, kind, kb, ds)
    if kind == "alias":
        schema = row.get("schema") if isinstance(row.get("schema"), dict) else {}
        _, planned, skipped = apply_alias_entries(schema, entries)
    elif kind == "mapping":
        # 列索引只取本批条目涉及的源；没有源时传 None（跳过列校验）。
        schema = row.get("schema") if isinstance(row.get("schema"), dict) else {}
        ds_ids = {str(e.get("data_source_id") or "").strip()
                  for e in entries or [] if isinstance(e, dict)}
        ds_ids.discard("")
        columns = (await schema_io.columns_by_source(tenant_id, kb, ds_ids)
                   if ds_ids else None)
        _, planned, skipped = apply_mapping_ops(
            schema, entries, columns_by_source=columns)
    elif kind == "tool_schema":
        # tools 覆盖块没有落点/物理三元组，走自己的三 op 分支（绝不落进
        # query_rule 的 add-only else）。
        schema = row.get("schema") if isinstance(row.get("schema"), dict) else {}
        _, planned, skipped = apply_tool_schema_entries(schema, entries)
    elif kind == "relation":
        _, planned, skipped = apply_relation_entries(row, entries)
    elif kind == "constraint":
        _, planned, skipped = apply_constraint_ops(row, entries)
    elif kind == "synonym":
        groups = (row.get("synonyms")
                  if isinstance(row.get("synonyms"), list) else [])
        planned, skipped = _plan_synonym_entries(groups, entries)
    else:
        _, planned, skipped = apply_constraint_entries(row, entries)
    return planned, skipped


def build_revise_messages(entry: dict, history: list[dict]) -> list[dict]:
    """条目现状 + 对话历史 → LLM 消息数组（system 含修订原则与靶点现状；纯函数、离线可测）。

    逐字复制自旧 CLI `tests/eval/nl2sql/evolve.py`（2026-10-18 服务化迁移）。

    靶点在 system 里显式给出并声明**不许更换**；history 的非法条目（非 dict /
    role 非 user|assistant / content 空）逐条丢弃（与别名通道「脏条目丢弃」同姿态）。
    """
    entry = entry or {}
    is_alias = "concept" in entry
    # constraint 条目（add/update）只改 expression；disable/enable 无口径，调用方
    # 已在入口拒绝。`op` 是 constraint 家族的身份键，query_rule/alias 条目没有。
    is_constraint = entry.get("op") in ("add", "update") and "expression" in entry
    if is_alias:
        target = str(entry.get("concept") or "")
    elif is_constraint:
        target = str(entry.get("target_code") or entry.get("name") or "")
    else:
        target = str(entry.get("target_code") or "")
    current = entry.get("alias") if is_alias else entry.get("expression")
    context = entry.get("question") if is_alias else entry.get("raw_description")
    if is_alias:
        kind_line = ("条目类型：alias（概念别名）。revised 要给出**新的别名文本**：简洁、语义准确的中文"
                     "（可含英文/数字）；不得与概念名完全重复；不要口语化；≤32 字。")
    elif is_constraint:
        kind_line = ("条目类型：constraint（compiled schema 的列约束口径 expression）。"
                     "revised 要给出**新的口径全文**："
                     "是给 SQL 生成模型的自然语言口径，必须含**适用范围句**（何时用）与「不是整张表的"
                     "默认过滤」类边界声明；不写死具体数字结果；SQL 方言 = SQL Server。")
    else:
        kind_line = ("条目类型：query_rule（列口径 expression）。revised 要给出**新的口径全文**："
                     "是给 SQL 生成模型的自然语言口径，必须含**适用范围句**（何时用）与「不是整张表的"
                     "默认过滤」类边界声明；不写死具体数字结果；SQL 方言 = SQL Server。")
    system = "\n".join([
        "你是 NL→SQL 演化补丁的条目修订助手。一次只改这一条，只输出严格 JSON。",
        f"靶点（不可更改：不许建议换靶点、不许 re-anchor）：{target}",
        kind_line,
        "修订原则：保持原文里仍然正确的内容（增量修改，不整段重写）；用户的最新指示优先；"
        "若与不可更换的靶点冲突，保持靶点并在 rationale 里说明。",
        '输出契约：只输出 JSON {"revised": "...", "rationale": "..."}，无多余文字。',
        "── 条目现状 ──",
        json.dumps({"kind": ("alias" if is_alias else
                             "constraint" if is_constraint else "query_rule"),
                    "target": target,
                    "current": current, "context": context}, ensure_ascii=False),
        "（对话开始）",
    ])
    msgs = [{"role": "system", "content": system}]
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    return msgs
