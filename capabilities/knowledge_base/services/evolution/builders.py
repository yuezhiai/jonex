# -*- coding:utf-8 -*-
"""演化补丁的真实生成（替代旧 CLI 的本地文件 + HTTP 登录）。

两条通道：

- `alias`：窗口内失败的 ② 观测（`nl2sql_observations`）→ `diagnosis.diagnose`
  取 `concept_gap` 提案的样本问句 → `suggest_concept_aliases`（本体 LLM）→
  `alias_entries_from_suggestions`（与人工采纳同一判据）；
- `query_rule`：编译 schema 列注释里的码表（`candidates.build_candidates`）→
  `constraint_entries_from_candidates`（`picks` 指定子集，缺省全取）。

两者都产出旧 `evolve.py` 同形 patch：
`{patch_id, kind, kb, ds, entries, skipped, target_cases, source, rollback:{"added": []}}`。
`target_cases` 是「失败问句在语料里归一后命中的 case 名」∪「请求显式指定的 case」，
门禁按它判目标题。

空输入不抛异常：返回 `entries: []` 的草稿，由 `run_pipeline` 收口成 `empty-draft`。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging

from sqlalchemy import text

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.ontology_llm import (
    suggest_concept_aliases,
    suggest_value_synonyms,
)

from ...dtos.ontology_synonym import MAX_TERM_LENGTH, MAX_TERMS_PER_GROUP
from ..abstract_schema_service import AbstractSchemaService
from ..nl2sql.diagnosis import diagnose
from .candidates import build_candidates, extract_codes
from .engine import (
    CONSTRAINT_MAX_ENTRIES,
    MAPPING_MAX_ENTRIES,
    RELATION_MAX_ENTRIES,
    TOOL_SCHEMA_MAX_ENTRIES,
    TOOL_SCHEMA_OPS,
    alias_entries_from_suggestions,
    constraint_entries_from_candidates,
    match_target_cases,
    plan_constraint_ops,
    plan_mapping_ops,
    plan_relation_entries,
    plan_tool_schema_entries,
)
# `run_pipeline` 从本模块导入 `probe_targets`（`from .builders import build_patch,
# probe_targets`）；真实实现在 `upstream`。这里 re-export 保住那条既有 import 路径。
from .relation_discovery import discover_relations, summarize_evidence
from .upstream import probe_targets

logger = logging.getLogger(__name__)

#: alias 诊断的默认回看窗口（天）；`request.window_days` 可覆盖。
DEFAULT_WINDOW_DAYS = 7

#: synonym 候选接受的失败原因码：值/术语识别失败（spec §4.2）。安全闸、
#: 敏感列等失败不是「值说不清」，交给对应 kind，不在这里猜。
_SYNONYM_REASON_CODES = frozenset({"no_candidate", "not_answerable"})

#: 一批 synonym 候选的条目上限；超出部分记 skipped（不静默截断丢证据）。
SYNONYM_MAX_ENTRIES = 20

#: 一次诊断最多读多少条失败观测（与旧 CLI 的 LIMIT 5000 同口径）。
_OBSERVATION_LIMIT = 5000

_FAILED_OBSERVATIONS_SQL = text("""
    SELECT id, question, reason_code, reason, sql, rejected_sql, row_count,
           data_source_id AS resource_id, tenant_id, created_at, outcome, scene
    FROM knowledge_base.nl2sql_observations
    WHERE tenant_id = :tenant_id
      AND knowledge_base_id = :kb
      AND outcome = 'failed'
      AND reason_code IS NOT NULL
      AND reason_code <> ''
      AND created_at >= :since AND created_at < :until
      -- 演化基准（合成流量）不入诊断样本：scene 标记整类排除（Task 4）
      AND COALESCE(scene->>'evolution_bench', 'false') <> 'true'
    ORDER BY created_at DESC
    LIMIT :row_limit
""")

_REJECT_ENTRIES_SQL = text("""
    SELECT entry
    FROM knowledge_base.evolution_ledgers
    WHERE tenant_id = :tenant_id AND ledger_type = 'reject'
    ORDER BY created_at DESC, id DESC
""")


def _normalise_row(row) -> dict:
    """JSONB 取回后统一口径：本栈（asyncpg）拿到 dict；str 分支是裸驱动的防御。"""
    out = dict(row)
    scene = out.get("scene")
    if isinstance(scene, str):
        try:
            out["scene"] = json.loads(scene)
        except json.JSONDecodeError:
            out["scene"] = {}
    return out


async def load_failed_observations(tenant_id: str, kb: str,
                                   since: datetime.datetime,
                                   until: datetime.datetime) -> list[dict]:
    """读窗口内该租户/KB 的失败观测，转成 `diagnose` 需要的条目形状。"""
    async with get_db_session() as session:
        rows = (await session.execute(_FAILED_OBSERVATIONS_SQL, {
            "tenant_id": tenant_id, "kb": kb,
            "since": since, "until": until,
            "row_limit": _OBSERVATION_LIMIT,
        })).mappings().all()
    return [_normalise_row(r) for r in rows]


async def load_reject_ids(tenant_id: str) -> set[str]:
    """该租户「否决台账」里的提案 id 集合（`diagnose` 据此剔除已否决提案）。"""
    async with get_db_session() as session:
        entries = (await session.execute(
            _REJECT_ENTRIES_SQL, {"tenant_id": tenant_id})).scalars().all()
    return {str(e["id"]) for e in entries
            if isinstance(e, dict) and e.get("id")}


def _entity_has_landing(entity: dict) -> bool:
    """实体是否有可用落点（与 `concept_expansion._expand_candidates_v1` 同判据）。"""
    for source in entity.get("sources") or []:
        if not isinstance(source, dict) or source.get("included") is False:
            continue
        if (str(source.get("data_source_id") or "").strip()
                and str(source.get("entity_name") or "").strip()):
            return True
    return False


def _target_cases(entries: list[dict], corpus: dict, request: dict) -> list[str]:
    """目标题 = 语料里命中 entries 问句的 case ∪ 请求显式指定的 case。"""
    questions = [str(e.get("question")) for e in entries or []
                 if isinstance(e, dict) and e.get("question")]
    return sorted(set(match_target_cases(questions, corpus))
                  | set(request.get("target_cases") or []))


# ------------------------------------------------------------ 候选与证据（Task 3）

#: ad-hoc case 的 id 前缀（spec §3.6）；不写入 `evolution_corpus_cases`。
ADHOC_PREFIX = "adhoc:"


def _normalise_question(question) -> str:
    """ad-hoc 哈希用的归一化：压空白 + 小写（spec §3.6 原文口径）。"""
    return " ".join(str(question or "").split()).strip().lower()


def adhoc_case_id(question) -> str:
    """`adhoc:<sha1(归一化问句)>`（40 位小写十六进制）。"""
    digest = hashlib.sha1(_normalise_question(question).encode("utf-8")).hexdigest()
    return f"{ADHOC_PREFIX}{digest}"


def _observation_matches(entry_question, observation_question) -> bool:
    """证据收录判据：与本批 entries 问句相关（归一化后相等）。

    `diagnosis._sample` 把**原始**问句截到 200 字，超长问句的样本与观测原文
    只差尾部；前缀相同即视为同一条（长度 < 200 时不放宽，避免误配短问句）。

    ⚠️ 截断发生在归一化之前：含连续空白的 200+ 字问句，`_normalise_question`
    折叠后长度会 < 200，只按归一化长度判断会**静默丢证据**。因此同时比较
    `raw[:200]` 原始前缀与归一化前缀两种口径。
    """
    raw_entry_full = str(entry_question or "")
    raw_entry = raw_entry_full.strip()
    raw_obs = str(observation_question or "").strip()
    entry_norm = _normalise_question(entry_question)
    obs_norm = _normalise_question(observation_question)
    if not entry_norm or not obs_norm:
        return False
    if entry_norm == obs_norm:
        return True
    # 截断长度看**截断后未 strip** 的原文（`_sample` 恰好切在空白上时，
    # strip 会掉到 199，不能因此否认它是 200 字样本）。
    if len(raw_entry_full) >= 200 and raw_obs.startswith(raw_entry):
        return True
    return len(entry_norm) >= 200 and obs_norm.startswith(entry_norm)


def _find_observation(question: str, observations: list[dict]) -> dict | None:
    for obs in observations or []:
        if isinstance(obs, dict) and _observation_matches(
                question, obs.get("question")):
            return obs
    return None


def _alias_expected_changes(entries: list[dict], corpus: dict) -> list[dict]:
    """alias 条目问句 → 语料命中 `to=pass`；未命中 → ad-hoc `to=changed`。"""
    out: list[dict] = []
    seen: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("question"):
            continue
        matched = match_target_cases([str(entry["question"])], corpus)
        if matched:
            case_id = matched[0]
            change = {"case_id": case_id, "from": "fail", "to": "pass"}
        else:
            case_id = adhoc_case_id(entry["question"])
            change = {"case_id": case_id, "from": "fail", "to": "changed"}
        if case_id not in seen:
            seen.add(case_id)
            out.append(change)
    return out


def _query_rule_expected_changes(target_cases: list[str]) -> list[dict]:
    """query_rule：父本 fail/error → 候选 pass；无 target_cases 时允许为空。"""
    out: list[dict] = []
    seen: set[str] = set()
    for name in target_cases or []:
        case_id = str(name)
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)
        out.append({"case_id": case_id, "from": "fail", "to": "pass"})
    return out


def _alias_evidence(entries: list[dict], observations: list[dict],
                    expected_changes: list[dict]) -> tuple[list[dict], list[dict]]:
    """本批问句相关的观测 + 未命中语料问句的 ad-hoc 失败签名。"""
    matchable = [e for e in entries or []
                 if isinstance(e, dict) and e.get("question")]
    kept: list[dict] = []
    seen: set[tuple] = set()
    for obs in observations or []:
        if not isinstance(obs, dict):
            continue
        if not any(_observation_matches(e["question"], obs.get("question"))
                   for e in matchable):
            continue
        key = (obs.get("id"), _normalise_question(obs.get("question")))
        if key in seen:
            continue
        seen.add(key)
        kept.append({
            "id": obs.get("id"),
            "question": obs.get("question"),
            "sql": obs.get("sql"),
            "rejected_sql": obs.get("rejected_sql"),
            "reason_code": obs.get("reason_code"),
            "row_count": obs.get("row_count"),
        })

    adhoc_cases: list[dict] = []
    seen_cases: set[str] = set()
    for change in expected_changes:
        case_id = change.get("case_id") or ""
        if not case_id.startswith(ADHOC_PREFIX) or case_id in seen_cases:
            continue
        seen_cases.add(case_id)
        entry = next((e for e in matchable
                      if adhoc_case_id(e["question"]) == case_id), None)
        question = str((entry or {}).get("question") or "")
        obs = _find_observation(question, observations)
        adhoc_cases.append({
            "case_id": case_id,
            "question": question,
            "verification": "unverified",
            "failure_signature": {
                "outcome": "fail",
                "reason_code": (obs or {}).get("reason_code"),
                "rejected_sql": (obs or {}).get("rejected_sql"),
                # spec §6：失败签名含 SQL 形态（`to="changed"` 的比较依据）。
                "sql": (obs or {}).get("sql"),
            },
            "truth": None,
        })
    return kept, adhoc_cases


def _alias_rationale(patch: dict, entries: list[dict]) -> str:
    note = str(patch.get("note") or "").strip()
    if note:
        return note
    pairs = "、".join(f"{e.get('concept')}→{e.get('alias')}"
                      for e in entries or [] if isinstance(e, dict))
    return f"为 {len(entries or [])} 条概念别名候选生成草稿：{pairs}"


def _query_rule_rationale(entries: list[dict]) -> str:
    codes = "、".join(str(e.get("target_code"))
                      for e in entries or [] if isinstance(e, dict))
    return f"从编译 schema 码表生成 {len(entries or [])} 条 query_rule 草稿：{codes}"


def _query_rule_evidence(entries: list[dict],
                         full_evidence: dict | None = None) -> dict | None:
    """query_rule 候选证据：靶点列的原始描述 + 码表值。

    候选按整批 patch 建一条（`candidate_no=1`），这里取首条 entry 作为
    「主靶点」；`target` 仍完整列出全部 `target_code`。

    码表值优先用 `full_evidence`（`_build_query_rule_patch` 从**未截断**的
    候选描述生成的 `{target_code: {...}}` 映射）——entry 的 `raw_description`
    在 `engine.constraint_entries_from_candidates` 里被 `[:200]` 截断，码表
    落在 200 字之后时会抽不到。取不到映射时才回退从 entry 重抽，并标
    `truncated: true`，让下游知道这份证据不完整。
    没有条目时返回 None，不伪造证据。
    """
    entry = next((e for e in entries or [] if isinstance(e, dict)), None)
    if entry is None:
        return None
    target_code = entry.get("target_code")
    full = (full_evidence or {}).get(target_code)
    if isinstance(full, dict):
        return {
            "raw_description": str(full.get("raw_description") or ""),
            "target_code": target_code,
            "values": dict(full.get("values") or {}),
        }
    raw_description = str(entry.get("raw_description") or "")
    return {
        "raw_description": raw_description,
        "target_code": target_code,
        "values": extract_codes(raw_description),
        "truncated": True,
    }


def build_candidate_record(patch: dict, run: dict, corpus: dict,
                           observations: list[dict]) -> dict:
    """patch → 候选记录（**纯函数**：不查库、不写库、不出网）。

    形状见 spec §3.2/§3.5/§3.6 与计划 Task 3 控制器裁决。`before` 只写
    `run.parent.schema_version`，读不到就是 None —— **不伪造变更前内容**。
    """
    patch = patch or {}
    run = run or {}
    corpus = corpus or {}
    kind = str(patch.get("kind") or "")
    entries = [e for e in patch.get("entries") or [] if isinstance(e, dict)]

    if kind == "query_rule":
        edit_type = "query_rule.add"
        target = [{"target_code": e.get("target_code")} for e in entries]
        expected_changes = _query_rule_expected_changes(
            patch.get("target_cases") or [])
        evidence_observations: list[dict] = []
        adhoc_cases: list[dict] = []
        query_rule_evidence = _query_rule_evidence(
            entries, patch.get("_query_rule_evidence"))
        source = "builder:query_rule"
        rationale = _query_rule_rationale(entries)
    elif kind == "relation":
        edit_type = "relation.add"
        # P1 收尾：remove 条目与 add 同批不混；target 逐项带 op（缺省 add），
        # 前端据 op 渲染「补边 / 删除」，端点字段两态都有。
        target = [{"op": e.get("op") or "add", "name": e.get("name"),
                   "source": e.get("source"),
                   "source_column": e.get("source_column"),
                   "target": e.get("target"),
                   "target_column": e.get("target_column")}
                  for e in entries]
        expected_changes = _query_rule_expected_changes(
            patch.get("target_cases") or [])
        evidence_observations = []
        adhoc_cases = []
        source = "builder:relation"
        changes = "、".join(
            f"{e.get('op') or 'add'}:{e.get('name')}"
            for e in entries or [] if isinstance(e, dict))
        rationale = (f"从 {len(entries or [])} 条 relation 条目生成草稿："
                     f"{changes}")
    elif kind == "constraint":
        # 约束编辑（P1-4）：add / update / disable / enable 混在一批里，
        # `target` 逐条带 op + name（add 另有 target_code），前端按 op 渲染。
        edit_type = "constraint.edit"
        target = [{"op": e.get("op"), "name": e.get("name"),
                   "target_code": e.get("target_code")} for e in entries]
        expected_changes = _query_rule_expected_changes(
            patch.get("target_cases") or [])
        evidence_observations = []
        adhoc_cases = []
        source = "builder:constraint"
        changes = "、".join(f"{e.get('op')}:{e.get('name')}"
                           for e in entries if isinstance(e, dict))
        rationale = f"约束可编辑 {len(entries)} 条：{changes}"
    elif kind == "mapping":
        # 落点编辑（P1-5）：planned 条目带身份与入参槽；`after` 只在 apply
        # 阶段产生，这里**不要求**它存在（只取 op + 身份）。
        edit_type = "mapping.edit"
        target = [{"op": e.get("op"), "target_code": e.get("target_code"),
                   "data_source_id": e.get("data_source_id"),
                   "entity_name": e.get("entity_name"),
                   "attribute_name": e.get("attribute_name")}
                  for e in entries]
        expected_changes = _query_rule_expected_changes(
            patch.get("target_cases") or [])
        evidence_observations = []
        adhoc_cases = []
        source = "builder:mapping"
        changes = "、".join(f"{e.get('op')}:{e.get('target_code')}"
                           for e in entries)
        rationale = f"映射语义 {len(entries)} 条：{changes}"
    elif kind == "tool_schema":
        # 工具口径（Tool-Schema 线）：update / disable / enable；planned 条目
        # 带 op + name（update 另带目标 description），`before` 只在 apply 阶段
        # 产生。候选落在 `layer="tool"`（DB CHECK 已允许 content/tool/schema）。
        edit_type = "tool_schema.edit"
        target = [{"op": e.get("op"), "name": e.get("name"),
                   "description": e.get("description")} for e in entries]
        expected_changes = _query_rule_expected_changes(
            patch.get("target_cases") or [])
        evidence_observations = []
        adhoc_cases = []
        source = "builder:tool_schema"
        changes = "、".join(f"{e.get('op')}:{e.get('name')}"
                           for e in entries)
        rationale = f"工具口径 {len(entries)} 条：{changes}"
    elif kind == "synonym":
        edit_type = "synonym.add"
        target = [{"terms": list(e.get("terms") or []),
                   "canonical": e.get("canonical")} for e in entries]
        expected_changes = _alias_expected_changes(entries, corpus)
        evidence_observations, adhoc_cases = _alias_evidence(
            entries, observations, expected_changes)
        source = "builder:synonym_llm"
        pairs = "、".join(
            f"{'/'.join(str(t) for t in e.get('terms') or [])}→{e.get('canonical')}"
            for e in entries if isinstance(e, dict))
        rationale = f"为 {len(entries)} 条值同义词候选生成草稿：{pairs}"
    elif kind == "alias":
        edit_type = "alias.add"
        target = [{"concept": e.get("concept"), "alias": e.get("alias"),
                   "proposal_id": e.get("proposal_id")} for e in entries]
        expected_changes = _alias_expected_changes(entries, corpus)
        evidence_observations, adhoc_cases = _alias_evidence(
            entries, observations, expected_changes)
        source = "builder:alias_llm"
        rationale = _alias_rationale(patch, entries)
    else:
        raise InvalidParameterError(f"未知的演化类型: {kind}")

    parent = run.get("parent")
    schema_version = parent.get("schema_version") if isinstance(parent, dict) else None

    evidence = {
        "observations": evidence_observations,
        "probe": {},
        "dislike_ids": [],
        "adhoc_cases": adhoc_cases,
        "source": source,
    }
    if kind == "query_rule" and query_rule_evidence is not None:
        # 只增字段：alias 分支的 evidence 形状保持不变；无条目时连键都不出现。
        evidence["query_rule"] = query_rule_evidence
    if kind == "relation":
        relation_evidence = patch.get("_relation_evidence")
        if isinstance(relation_evidence, dict):
            # 自动发现模式的逐候选证据（sampled/hits/ratio/verdict/skipped...）。
            evidence["relation"] = relation_evidence

    return {
        "candidate_no": 1,
        "parent_version_id": run.get("parent_version_id"),
        # 工具口径是「工具层」的改动（DB CHECK 允许 content/tool/schema）；
        # 其余 kind 仍是内容层。
        "layer": "tool" if kind == "tool_schema" else "content",
        "edit_type": edit_type,
        "target": target,
        "before": {"schema_version": schema_version},
        "after": {
            "entries": [dict(e) for e in entries],
            "target_cases": list(patch.get("target_cases") or []),
        },
        "expected_changes": expected_changes,
        "rationale": rationale,
        "evidence": evidence,
        "gate_decision": None,
        "gate_reasons": None,
    }


async def _build_alias_patch(tenant_id: str, kb: str, request: dict,
                             corpus: dict) -> tuple[dict, list]:
    """诊断(concept_gap) → 本体 LLM 提候选 → entries（批内叠加去重）。"""
    until = datetime.datetime.now()
    window_days = int(request.get("window_days") or DEFAULT_WINDOW_DAYS)
    since = until - datetime.timedelta(days=window_days)
    observations = await load_failed_observations(tenant_id, kb, since, until)
    reject_ids = await load_reject_ids(tenant_id)
    report = diagnose(observations, now=until, since=since, until=until,
                      tenant=tenant_id, reject_ids=reject_ids)

    questions: list[str] = []
    proposal_by_q: dict[int, str] = {}
    for proposal in report.get("proposals") or []:
        if proposal.get("kind") != "concept_gap":
            continue
        for sample in proposal.get("samples") or []:
            question = str((sample or {}).get("question") or "")
            if not question:
                continue
            proposal_by_q[len(questions)] = str(proposal.get("id") or "")
            questions.append(question)

    entries: list[dict] = []
    skipped: list[dict] = []
    note = ""
    if not observations:
        note = f"近 {window_days} 天窗口内没有失败观测"
    elif not questions:
        note = "失败观测里没有可提别名的概念缺口样本"
    if questions:
        data = await AbstractSchemaService().get(tenant_id, kb) or {}
        entities = ((data.get("schema") or {}).get("entities")) or []
        all_concepts = [{"name": e.get("name"),
                         "aliases": e.get("aliases") or [],
                         "description": e.get("description") or ""}
                        for e in entities if isinstance(e, dict)]
        # 只把**有落点**的概念交给 LLM 提别名：无落点的概念即使挂上别名也产生
        # 不了检索候选（2026-10-21 真机：吨电被建议挂到 m12_daily，采纳后零变化）。
        # 整个抽象层都没有落点时保持旧口径（不改变「还没对齐」的库的既有行为）。
        landed_names = {str(e.get("name") or "") for e in entities
                        if isinstance(e, dict) and _entity_has_landing(e)}
        concepts = [c for c in all_concepts if c["name"] in landed_names] or all_concepts
        suggestions = await suggest_concept_aliases(
            questions, concepts, tenant_id=tenant_id, kb_id=kb)
        if not suggestions:
            # 模型偶发回空（「想不出」是合法结果），但一次空就让整次运行收口
            # empty-draft 对用户没有信息量；再试一次，仍空就把原因写进 note。
            suggestions = await suggest_concept_aliases(
                questions, concepts, tenant_id=tenant_id, kb_id=kb)
            if not suggestions:
                note = "LLM 未给出别名候选（已重试 2 次）"
        entries, skipped = alias_entries_from_suggestions(
            suggestions, questions, entities, proposal_by_q)

    return {
        "patch_id": f"alias-batch-{until.strftime('%Y%m%d-%H%M%S')}",
        "kind": "alias", "kb": kb, "ds": None,
        "entries": entries, "skipped": skipped,
        "target_cases": _target_cases(entries, corpus, request),
        "source": "alias_suggest",
        "rollback": {"added": []},
        "note": note,
    }, observations


def _synonym_questions(observations: list[dict]) -> list[str]:
    """窗口内「值/术语识别失败」的问句（保持观测顺序、归一后去重）。

    只认 `outcome == "failed"` 且 `reason_code ∈ {no_candidate, not_answerable}`：
    安全闸（`rejected_unsafe_sql`）、敏感列（`rejected_sensitive`）等失败不是
    「值说不清」，不该被当成同义词候选的样本。
    """
    out: list[str] = []
    seen: set[str] = set()
    for obs in observations or []:
        if not isinstance(obs, dict):
            continue
        if obs.get("outcome") != "failed":
            continue
        if str(obs.get("reason_code") or "") not in _SYNONYM_REASON_CODES:
            continue
        question = str(obs.get("question") or "").strip()
        if not question:
            continue
        key = question.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(question)
    return out


def _synonym_concept_names(schema: dict) -> list[str]:
    """抽象 schema → 实体名 + 属性名清单（只给 LLM 定位用，不含数据行）。

    顺序 = schema 里的声明顺序；大小写不敏感去重。空/非字符串项丢弃 ——
    脏 schema 不该把整次候选生成带歪。
    """
    out: list[str] = []
    seen: set[str] = set()
    entities = ((schema or {}).get("schema") or {}).get("entities") or []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        names = [entity.get("name")]
        for attribute in entity.get("attributes") or []:
            if isinstance(attribute, dict):
                names.append(attribute.get("name"))
        for raw in names:
            name = str(raw or "").strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                out.append(name)
    return out


def _synonym_observation_source(question: str,
                                observations: list[dict]) -> str:
    """该问句在失败观测里对应的数据源 id（没有则空串）。

    观测 SQL 把 `data_source_id` 别名成 `resource_id`；两个键都认，兼容
    既有/将来的形状。同一问句多条观测时取**最近**一条（SQL 按 created_at
    DESC，调用方保持顺序），保证验真落在产生该失败的那个源上。
    """
    for observation in observations or []:
        if not isinstance(observation, dict):
            continue
        if not _observation_matches(question, observation.get("question")):
            continue
        source = str(observation.get("data_source_id")
                     or observation.get("resource_id") or "").strip()
        if source:
            return source
    return ""


def _synonym_entry_question(terms: list[str], questions: list[str]) -> str:
    """条目问句 = 第一个**包含任一 term** 的问句（LLM 只给 terms，不给下标）。

    问句表就是这次喂给 LLM 的失败问句，term 通常逐字出现在其中之一；实在对不上
    时回落首条 —— 保住验真可运行，而不是让条目带着空问句落库。
    """
    for term in terms:
        needle = term.casefold()
        for question in questions:
            if needle in question.casefold():
                return question
    return questions[0] if questions else ""


def _synonym_display(value) -> str:
    """展示/存储归一：trim + 全角转半角（与 `OntologySynonymService` 同口径）。"""
    text = str(value or "").strip()
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:
            code -= 0xFEE0
        out.append(chr(code))
    return "".join(out).strip()


def synonym_entries_from_suggestions(suggestions: list[dict],
                                     questions: list[str]) -> tuple[list, list]:
    """LLM 值同义词候选 → `(entries, skipped)`（纯函数，不查库不出网）。

    清洗口径（与 `OntologySynonymService` 的存储模型对齐）：
    - `terms` 必须是数组；逐项**展示归一**（trim + 全角转半角），空/非字符串/
      超过 `MAX_TERM_LENGTH` 的**单项**丢弃；
    - `canonical` 展示归一后必须非空且 ≤ `MAX_TERM_LENGTH`；
    - `terms = dedupe(terms + [canonical])`：service 要求 canonical 属于 terms，
      演化候选只给「别名 + 规范值」时必须把规范值并进词表；
    - 去重后只剩 canonical 本身 = 没有可注入的别名 → `skipped`（no-op）；
    - 每组词数（含 canonical）≤ `MAX_TERMS_PER_GROUP`；每批 ≤ 20 条；
    - **单 run 内 term 去重**：与更早条目撞任一词 → 整条 `skipped`（不静默
      覆盖已有组；跨组词冲突由 Task 6 的 schema_io 在采纳时再判一次）。
    """
    entries: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for item in suggestions or []:
        if not isinstance(item, dict):
            continue
        raw_terms = item.get("terms")
        raw_canonical = item.get("canonical")
        canonical = (_synonym_display(raw_canonical)
                     if isinstance(raw_canonical, str) else "")
        terms: list[str] = []
        local: set[str] = set()
        if isinstance(raw_terms, list):
            for raw in raw_terms:
                if not isinstance(raw, str):
                    continue
                term = _synonym_display(raw)
                if not term or len(term) > MAX_TERM_LENGTH:
                    continue
                key = term.casefold()
                if key in local:
                    continue
                local.add(key)
                terms.append(term)
        if not terms:
            skipped.append({
                "terms": terms, "canonical": canonical,
                "why": f"没有可用 term（空/非字符串/超过 {MAX_TERM_LENGTH} 字符）",
            })
            continue
        if not canonical:
            skipped.append({"terms": terms, "canonical": canonical,
                            "why": "canonical 为空"})
            continue
        if len(canonical) > MAX_TERM_LENGTH:
            skipped.append({
                "terms": terms, "canonical": canonical,
                "why": f"canonical 超过长度上限({MAX_TERM_LENGTH})",
            })
            continue
        canonical_key = canonical.casefold()
        if canonical_key not in local:
            terms.append(canonical)
            local.add(canonical_key)
        if len(terms) == 1:
            skipped.append({
                "terms": terms, "canonical": canonical,
                "why": "只有 canonical 本身，没有可注入的同义词",
            })
            continue
        if len(terms) > MAX_TERMS_PER_GROUP:
            skipped.append({
                "terms": terms, "canonical": canonical,
                "why": f"每组词数超过上限({MAX_TERMS_PER_GROUP})",
            })
            continue
        clash = local & seen
        if clash:
            skipped.append({
                "terms": terms, "canonical": canonical,
                "why": f"term 与本批更早条目重复：{'、'.join(sorted(clash))}",
            })
            continue
        if len(entries) >= SYNONYM_MAX_ENTRIES:
            skipped.append({
                "terms": terms, "canonical": canonical,
                "why": f"超过单批条目上限 {SYNONYM_MAX_ENTRIES} 条",
            })
            continue
        seen.update(local)
        entries.append({
            "terms": terms,
            "canonical": canonical,
            "question": _synonym_entry_question(terms, questions),
        })
    return entries, skipped


async def _build_synonym_patch(tenant_id: str, kb: str, request: dict,
                               corpus: dict) -> tuple[dict, list]:
    """值同义词草稿（P2）：窗口内值/术语识别失败的问句 → LLM 提 `terms → canonical`。

    KB 级（`ds=None`），与 alias 同姿态：没有失败观测/没有值类失败/LLM 回空都
    不抛，返回空 `entries` 由 `run_pipeline` 收口 `empty-draft`。LLM 的**坏回包**
    抛（`suggest_value_synonyms` 与 alias 同口径），由运行入口记 failed。
    """
    until = datetime.datetime.now()
    window_days = int(request.get("window_days") or DEFAULT_WINDOW_DAYS)
    since = until - datetime.timedelta(days=window_days)
    observations = await load_failed_observations(tenant_id, kb, since, until)
    relevant = [obs for obs in observations or []
                if isinstance(obs, dict)
                and obs.get("outcome") == "failed"
                and str(obs.get("reason_code") or "") in _SYNONYM_REASON_CODES]
    questions = _synonym_questions(relevant)

    entries: list[dict] = []
    skipped: list[dict] = []
    note = ""
    if not observations:
        note = f"近 {window_days} 天窗口内没有失败观测"
    elif not questions:
        note = ("失败观测里没有值/术语识别失败样本"
                "（reason_code ∈ {no_candidate, not_answerable}）")
    else:
        data = await AbstractSchemaService().get(tenant_id, kb) or {}
        suggestions = await suggest_value_synonyms(
            questions, _synonym_concept_names(data),
            tenant_id=tenant_id, kb_id=kb)
        entries, skipped = synonym_entries_from_suggestions(
            suggestions, questions)
        # 验真必须落回「产生该失败」的数据源：多源 KB 上用空 ds 会 ambiguous
        # 或跑到错误的源。观测里没有源 id 时写空串（调用方再回退语料目标源）。
        for entry in entries:
            entry["data_source_id"] = _synonym_observation_source(
                str(entry.get("question") or ""), relevant)
        if not entries:
            note = ("LLM 未给出值同义词候选" if not suggestions
                    else "值同义词候选全部被清洗（见 skipped 的 why）")

    return {
        "patch_id": f"syn-batch-{until.strftime('%Y%m%d-%H%M%S')}",
        "kind": "synonym", "kb": kb, "ds": None,
        "entries": entries, "skipped": skipped,
        "target_cases": _target_cases(entries, corpus, request),
        "source": "synonym_suggest",
        "rollback": {"added": []},
        "note": note,
    }, relevant


async def _build_query_rule_patch(tenant_id: str, kb: str, ds: str | None,
                                  request: dict, corpus: dict) -> tuple[dict, list]:
    """编译 schema 码表 → 约束草稿；`request.picks` 指定子集，缺省全取。"""
    data = await build_candidates(tenant_id, kb, ds)
    candidates = [c for c in data.get("candidates") or [] if isinstance(c, dict)]
    entries, skipped = constraint_entries_from_candidates(
        candidates, request.get("picks"))
    # 证据用**未截断**的候选描述：entry 的 raw_description 已被
    # constraint_entries_from_candidates 截到 200 字，码表落在后面会丢。
    # 私有键随 patch 传给 build_candidate_record，build_patch 构建后移除，不落库。
    full_evidence = {}
    for candidate in candidates:
        code = str(candidate.get("target_code") or "")
        if not code:
            continue
        raw_description = str(candidate.get("raw_description") or "")
        values = candidate.get("codes")
        if not isinstance(values, dict):
            values = extract_codes(raw_description)
        full_evidence[code] = {"raw_description": raw_description,
                               "target_code": code,
                               "values": dict(values)}
    return {
        "patch_id": f"qr-batch-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "kind": "query_rule", "kb": kb, "ds": ds,
        "entries": entries, "skipped": skipped,
        "target_cases": _target_cases(entries, corpus, request),
        "source": "constraint_candidates",
        "rollback": {"added": []},
        "_query_rule_evidence": full_evidence,
    }, []


async def _build_relation_patch(tenant_id: str, kb: str, ds: str | None,
                                request: dict, corpus: dict) -> tuple[dict, list]:
    """relation 草稿：**人工条目优先，缺省跑发现器**（P1-3 Task 2）。

    - `request["entries"]` 是非空数组 → `source="manual"`，**不跑发现器**
      （人工始终赢，发现器不覆盖人写的东西）；
    - 缺省/空数组 → `source="discovery"`：编译行 + 只读采样跑一轮 join 候选
      发现（`relation_discovery.discover_relations`），只把**无歧义 new** 候选
      转成条目；证据（每条候选的 sampled/hits/ratio/verdict）随
      `_relation_evidence` 传给 `build_candidate_record`，落进候选
      `evidence.relation`；
    - 两条路都先经 `plan_relation_entries` 做 **dry-run 校验**（实体/列/重复/
      边名），发现器零产出时 `entries` 为空 → `run_pipeline` 收口
      `empty-draft`，不是异常。
    """
    if not ds:
        raise InvalidParameterError(
            message="relation 演化需要数据源 ds（compiled 链）")
    raw_entries = request.get("entries")
    if raw_entries is not None and not isinstance(raw_entries, list):
        raise InvalidParameterError(message="entries 必须是对象数组")
    manual = bool(raw_entries)
    if manual and len(raw_entries) > RELATION_MAX_ENTRIES:
        raise InvalidParameterError(
            message=f"relation 单批条目上限 {RELATION_MAX_ENTRIES} 条")
    # 函数内 import：与 `run_pipeline` 的延迟依赖同姿态，也便于测试 monkeypatch。
    from .schema_io import get_row

    row = await get_row(tenant_id, "relation", kb, ds) or {}
    relation_evidence: dict | None = None
    note = ""
    if manual:
        entries, skipped = plan_relation_entries(row, raw_entries)
        source = "manual"
    else:
        result = await discover_relations(tenant_id, ds, row, request)
        relation_evidence = (result.get("evidence")
                             if isinstance(result.get("evidence"), dict) else {})
        discovered = [e for e in result.get("entries") or []
                      if isinstance(e, dict)]
        entries, skipped = plan_relation_entries(row, discovered)
        source = "discovery"
        if not entries:
            # 零产出时把「为什么没提案」带出去（判词计数 + 有限条原因）。note
            # 会进运行详情/日志，`summarize_evidence` 已限长且 `why` 已脱敏。
            reason = (relation_evidence.get("reason")
                      or relation_evidence.get("truncation_reason"))
            summary = summarize_evidence(relation_evidence)
            note = ("join 候选发现未产出条目"
                    + (f"：{reason}" if reason else "")
                    + (f"（{summary}）" if summary else ""))
    patch = {
        "patch_id": f"rel-batch-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "kind": "relation", "kb": kb, "ds": ds,
        "entries": entries, "skipped": skipped,
        "target_cases": request.get("target_cases") or [],
        "source": source,
        "rollback": {"added": []},
    }
    if relation_evidence is not None:
        # 私有键：只用于构建候选证据，`build_patch` 收尾时移除，不落 run.patch。
        patch["_relation_evidence"] = relation_evidence
    if note:
        patch["note"] = note
    return patch, []


async def _build_constraint_patch(tenant_id: str, kb: str, ds: str | None,
                                  request: dict, corpus: dict) -> tuple[dict, list]:
    """constraint 草稿（P1-4）：**必须**显式 `request.entries`，无自动发现。

    与 relation 的差别：约束编辑天然是人写意图（改哪条、改什么），没有可靠的
    自动来源，所以缺 entries / 空数组一律 1001（不建注定 empty-draft 的 run）。
    条目先经 `plan_constraint_ops` 做 **dry-run 校验**（op / name / expression /
    幂等），单条业务问题进 skipped；全被跳过时 `entries` 为空，由
    `run_pipeline` 收口 empty-draft。
    """
    if not ds:
        raise InvalidParameterError(
            message="constraint 演化需要数据源 ds（compiled 链）")
    raw_entries = request.get("entries")
    if raw_entries is not None and not isinstance(raw_entries, list):
        raise InvalidParameterError(message="entries 必须是对象数组")
    if not raw_entries:
        raise InvalidParameterError(
            message="constraint 演化需要显式 entries（add/update/disable/enable）")
    if len(raw_entries) > CONSTRAINT_MAX_ENTRIES:
        raise InvalidParameterError(
            message=f"constraint 单批条目上限 {CONSTRAINT_MAX_ENTRIES} 条")
    # 函数内 import：与 relation 同姿态，也便于测试 monkeypatch。
    from .schema_io import get_row

    row = await get_row(tenant_id, "constraint", kb, ds) or {}
    entries, skipped = plan_constraint_ops(row, raw_entries)
    patch = {
        "patch_id": f"con-batch-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "kind": "constraint", "kb": kb, "ds": ds,
        "entries": entries, "skipped": skipped,
        "target_cases": request.get("target_cases") or [],
        "source": "manual",
        "rollback": {"added": []},
    }
    if not entries:
        patch["note"] = "constraint 条目全部被跳过（见 skipped 的 why）"
    return patch, []


async def _build_mapping_patch(tenant_id: str, kb: str, request: dict,
                               corpus: dict) -> tuple[dict, list]:
    """mapping 草稿（P1-5）：抽象 schema 落点的 add/update/disable/enable。

    KB 级（`ds=None`，与 alias 同链）：与 constraint 同姿态**必须**显式
    `request.entries`，没有自动发现来源。条目先经 `plan_mapping_ops` 做
    dry-run 校验（实体/属性/落点/列名/幂等），单条业务问题进 skipped；
    全被跳过时 `entries` 为空，由 `run_pipeline` 收口 empty-draft。
    `columns_by_source` 只取本批条目涉及的源，把人工填的物理列名归一成
    compiled schema 的规范列名；没有源时传 None（跳过列校验）。
    """
    raw_entries = request.get("entries")
    if raw_entries is not None and not isinstance(raw_entries, list):
        raise InvalidParameterError(message="entries 必须是对象数组")
    if not raw_entries:
        raise InvalidParameterError(
            message="mapping 演化需要显式 entries（add/update/disable/enable）")
    if len(raw_entries) > MAPPING_MAX_ENTRIES:
        raise InvalidParameterError(
            message=f"mapping 单批条目上限 {MAPPING_MAX_ENTRIES} 条")
    # 函数内 import：与 relation / constraint 同姿态，也便于测试 monkeypatch。
    from . import schema_io

    row = await schema_io.get_row(tenant_id, "mapping", kb, None) or {}
    ds_ids = {str(e.get("data_source_id") or "").strip()
              for e in raw_entries if isinstance(e, dict)}
    ds_ids.discard("")
    columns = (await schema_io.columns_by_source(tenant_id, kb, ds_ids)
               if ds_ids else None)
    entries, skipped = plan_mapping_ops(
        row.get("schema") or {}, raw_entries, columns_by_source=columns)
    patch = {
        "patch_id": f"map-batch-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "kind": "mapping", "kb": kb, "ds": None,
        "entries": entries, "skipped": skipped,
        "target_cases": _target_cases(entries, corpus, request),
        "source": "manual",
        "rollback": {"added": []},
    }
    if not entries:
        patch["note"] = "mapping 条目全部被跳过（见 skipped 的 why）"
    return patch, []


async def _build_tool_schema_patch(tenant_id: str, kb: str, request: dict,
                                   corpus: dict) -> tuple[dict, list]:
    """tool_schema 草稿（Tool-Schema 线）：抽象 schema `tools` 覆盖块的三 op。

    KB 级（`ds=None`，与 alias / mapping 同链）：与 constraint / mapping 同
    姿态**必须**显式 `request.entries`，没有自动发现来源。受理层已校验形状；
    这里再校验一次（builder 可被单独调用），未知 op / 缺 description 是结构性
    错误 → 1001，白名单外的 name 由 `plan_tool_schema_entries` 进 skipped。
    条目先经 plan 做 dry-run 校验，全被跳过时 `entries` 为空，由
    `run_pipeline` 收口 empty-draft。
    """
    raw_entries = request.get("entries")
    if raw_entries is not None and not isinstance(raw_entries, list):
        raise InvalidParameterError(message="entries 必须是对象数组")
    if not raw_entries:
        raise InvalidParameterError(
            message="tool_schema 演化需要显式 entries（update/disable/enable）")
    if len(raw_entries) > TOOL_SCHEMA_MAX_ENTRIES:
        raise InvalidParameterError(
            message=f"tool_schema 单批条目上限 {TOOL_SCHEMA_MAX_ENTRIES} 条")
    for index, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            raise InvalidParameterError(message="entries 必须是对象数组")
        op = str(item.get("op") or "").strip()
        if op not in TOOL_SCHEMA_OPS:
            raise InvalidParameterError(
                message=f"entries[{index}].op 必须是 update/disable/enable")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidParameterError(
                message=f"entries[{index}].name 必须是非空字符串")
        if op == "update":
            description = item.get("description")
            if not isinstance(description, str) or not description.strip():
                raise InvalidParameterError(
                    message=f"entries[{index}].description 必须是非空字符串")
    # 函数内 import：与 relation / constraint / mapping 同姿态。
    from . import schema_io

    row = await schema_io.get_row(tenant_id, "tool_schema", kb, None) or {}
    entries, skipped = plan_tool_schema_entries(
        row.get("schema") or {}, raw_entries)
    patch = {
        "patch_id": f"tool-batch-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "kind": "tool_schema", "kb": kb, "ds": None,
        "entries": entries, "skipped": skipped,
        "target_cases": _target_cases(entries, corpus, request),
        "source": "manual",
        "rollback": {"added": []},
    }
    if not entries:
        patch["note"] = "tool_schema 条目全部被跳过（见 skipped 的 why）"
    return patch, []


async def build_patch(tenant_id: str, kind: str, kb: str, ds: str | None,
                      request: dict, corpus: dict, store, *,
                      run: dict | None = None) -> dict:
    """生成一次运行的补丁（`store` 预留给调用方记账，当前不写任何东西）。

    `run` 非空且补丁有条目时，把 `build_candidate_record` 的结果写进
    `patch["candidate"]`（engine 在 drafted 之后据此持久化候选）。不传 `run`
    的旧调用完全等价于之前：补丁里不会出现 `candidate` 键。
    """
    request = request or {}
    corpus = corpus or {}
    if kind == "alias":
        patch, observations = await _build_alias_patch(
            tenant_id, kb, request, corpus)
    elif kind == "synonym":
        patch, observations = await _build_synonym_patch(
            tenant_id, kb, request, corpus)
    elif kind == "query_rule":
        patch, observations = await _build_query_rule_patch(
            tenant_id, kb, ds, request, corpus)
    elif kind == "relation":
        patch, observations = await _build_relation_patch(
            tenant_id, kb, ds, request, corpus)
    elif kind == "constraint":
        patch, observations = await _build_constraint_patch(
            tenant_id, kb, ds, request, corpus)
    elif kind == "mapping":
        patch, observations = await _build_mapping_patch(
            tenant_id, kb, request, corpus)
    elif kind == "tool_schema":
        patch, observations = await _build_tool_schema_patch(
            tenant_id, kb, request, corpus)
    else:
        raise InvalidParameterError(f"未知的演化类型: {kind}")

    if run is not None and patch.get("entries"):
        patch["candidate"] = build_candidate_record(
            patch, run, corpus, observations)
    # 私有证据映射只用于构建候选，不进入持久化的 run.patch。
    patch.pop("_query_rule_evidence", None)
    patch.pop("_relation_evidence", None)
    return patch


__all__ = [
    "ADHOC_PREFIX",
    "DEFAULT_WINDOW_DAYS",
    "adhoc_case_id",
    "build_candidate_record",
    "build_patch",
    "load_failed_observations",
    "load_reject_ids",
    "probe_targets",
]
