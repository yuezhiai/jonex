#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""② NL→SQL 分源查询：一问一源一条 SQL。

范围边界（spec §二 决策 3）：多源分派、汇总与「不合成统一数字」的呈现全归 ③；
这里只保证「给我一个源，我把它答准答安全」。

**失败不是异常**：答不了（没候选 / 多候选 / 没只读凭据 / 被 guard 拒 / 执行失败）
一律回 `{ok: False, not_answerable: True, reason_code, reason, …}` —— ③ 需要区分
「没有数据答案」与「这次调用坏了」，异常会把两者搅在一起。只有调用方用错
（租户非法、KB 不存在、问题为空）才抛 `JonexException`。

**每个失败出口都会落一条 `outcome="FAILED"` 审计**（收口在 `_fail`）。

`reason_code` 是 ③ 的决策输入（澄清 / 跳过这个源 / 重试）：

| 码 | 什么情况 |
| --- | --- |
| `no_candidate` | 词面没命中任何**已确认**的实体（含「指定的那一对不在候选里」）|
| `ambiguous` | 命中多个 —— `candidates` 一并回给调用方去澄清 |
| `entity_gone` | 源里已经没有这个实体（schema 重生成过）|
| `no_readonly_creds` | 该源没登记只读凭据（**绝不回退写凭据**）|
| `source_gone` / `not_a_database_source` | 源被删 / 被改成非 database（③ 的多源扇出里，一个源的 404 不该掀翻整个提问）|
| `llm_failed` | 生成 SQL 那次调用坏了（超时 / 坏答案）—— 模型抖动，不是「没数据」|
| `not_answerable` | 模型判定「用这些列答不了」|
| `rejected_unsafe_sql` | 被 guard 拒：`rejected_sql` 回带给用户看，**绝不执行**（审计与 warning 见 `_fail`，安全拦截必须留痕）|
| `rejected_sensitive` | 被治理策略拒（密级/标签/行过滤）：`rejected_sql` 回带，**绝不执行** |
| `exec_failed` | 执行失败（超时 / 权限 / 方言）：`sql` 回带便于排查 |

拒绝类两码（`rejected_unsafe_sql` / `rejected_sensitive`）另带 **`rejection_kind`**
细分（观测口径，D1）：`forbidden_pair`（禁止同查）/ `guard`（其它安全闸）/
`deny`（治理密级或策略拒绝）/ `mask_position`（需掩码的列进了谓词/聚合）/
`parse_error` 等改写面故障。它同时进响应体、观测 scene 与诊断归因，**不改 `reason_code`**。
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import functools
import hashlib
import logging
import re
import time
from typing import Awaitable, Callable

from sqlalchemy import select

from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.config import config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.ontology_llm import generate_sql
from jonex_core.common.tenant import require_tenant

from ...models.evolution import EvolutionRun
from ...repository.data_source_repository import KnowledgeDataSourceRepository
from ...repository.knowledge_info_repository import KnowledgeInfoRepository
from ..concept_expansion import expand_candidates
from ..concept_filter import filter_candidates
from ..metrics import shadow as metric_shadow
from ..metrics.compiler import MetricCompileError
from ..metrics.executor import MetricExecutionRejected, execute_metric
from ..metrics.validation import preflight
from ..schema_source import normalize_engine
from ..data_source_service import DataSourceService
from ..document_service import _audit_user_id
from ..evidence_probe import evidence_key
from ..evolution.tokens import EVOLUTION_USERNAME
from . import multiturn
from . import observe
from . import schema_read
from . import value_aliases
from .auto_hints import MAX_RELATED_ENTITIES, build_auto_hints
from .ambiguity import find_ambiguities
from .anchor_fallback import choose_anchor
from .candidates import select_candidate, should_generate_candidate
from .constraints import filter_constraints
from .cte_joins import selected_join_edges
from .dimension_joins import build_dimension_joins
from .evidence import pick_examples, pick_samples
from .exec_errors import is_fixable_exec_error
from .fewshot import (load_corpus_examples, load_examples, select_examples)
from .format_predict import predict_format
from .grain_hint import wants_entity_grain
from .hints import merge_hints, normalize_hints
from .left_join_hint import wants_negative_case
from .lineage_search import LineageSearchMixin
from .metric_ask import MetricAskMixin
from .result_contract import attach_blocks
from .unit_grounding import scale_candidates
from .unit_tester import (
    build_repair_note, evaluate_assertions, failed_assertions, generate_assertions,
)
from .hints_rewrite import rewrite_evidence_hints
from .planner import plan_sql
from .probe_summary import summarize_columns
from .schema_preview import build_schema_preview
from .schema_select import select_schema, should_select_schema
from .sample_rows import SAMPLE_CELL_CHARS, build_sample_sql, normalize_rows
from .projection import ProjectionViolation, check_projection
from .value_mapping import map_question_values
from .value_grounding import (build_lookup_sql, extract_literals, pick_case_match,
                              pick_value_match, replace_literals)
from .forbidden import filter_joins, prompt_note
from .guard import guard_sql, normalize_dialect_sql
from .joins import JoinPlan, all_join_edges, eligible_joins
from .lineage_source import load_forbidden_pairs_from_db, load_lineage_edges_from_db
from .runner import run_readonly
from .result_vote import vote_index

logger = logging.getLogger(__name__)

#: 影子任务强引用（防 GC）；完成即摘除。
_shadow_tasks: set[asyncio.Task] = set()


def _on_shadow_done(task: asyncio.Task) -> None:
    _shadow_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("[metric-shadow] 后台任务异常（已吞）: %s", exc)

#: B（锚错兜底）：执行失败且属「可修复」白名单时的重试预算（次数，不含首发）。
#: 常量而非开关：纯失败路径上的增益、无行为争议；回滚 = revert。
_RETRY_MAX = 1


async def _load_lineage_edges(tenant_id: str, kb_id: str, ds_id: str) -> list:
    """运行时血缘边：开一个短会话读表（`Nl2SqlService` 无状态，没有常驻 session）。

    与同文件的 `_load_concepts` / `_entity_columns` 同姿势。**整个 `async with` 都兜住**：
    `load_lineage_edges_from_db` 的 fail-open 只覆盖 `select/execute/scalars` 那一段；
    行映射的列表推导、`get_db_session` 的进入/退出（commit/close）都在它之外 ——
    这里一并退空图，让「读表抛任何异常都 fail-open」字面为真（血缘是增能项，
    坏了不该让提问坏掉）。
    ⚠️ 编程错误（`AssertionError` / `NameError` / `AttributeError` / `TypeError` /
    `KeyError`）**照抛**：退空图会把 bug 静默伪装成「没有血缘」，反而更难发现。
    """
    try:
        async with get_db_session() as session:
            return await load_lineage_edges_from_db(session, tenant_id, kb_id, ds_id)
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        # 编程错误照抛（与 `load_lineage_edges_from_db` 内部的重抛集合同口径）：
        # 宽泛 fail-open 只该兜「库/会话坏了」，不该兜「代码写错了」。
        raise
    except Exception as exc:  # noqa: BLE001 —— 增能项：会话进入/退出与行映射的残余失败也退空图
        logger.warning("血缘边读表会话失败，本次退空图：%s", exc)
        return []


async def _load_forbidden_pairs(tenant_id: str, kb_id: str, ds_id: str) -> list:
    """运行时禁止对；**始终加载**（安全闸必须生效，不受 `NL2SQL_JOIN_PATHS` 开关影响）。"""
    try:
        async with get_db_session() as session:
            return await load_forbidden_pairs_from_db(session, tenant_id, kb_id, ds_id)
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 —— 增能约束，会话层失败也退空
        logger.warning("禁止同查表对读表会话失败，本次退空：%s", exc)
        return []


#: 相对时间词（问句含任一词时才把「今天」注入 NL→SQL payload）。
#: 模型此前把「上周」锚到**数据最大日期所在周的前一周**（真机 → 0 行）；
#: 给了 today 它才能按真实日历落到 10-12~18。
_RELATIVE_TIME_WORDS = (
    "上周", "本周", "这周", "下周", "上月", "本月", "下月", "上个月",
    "上季度", "本季度", "去年", "今年", "明年", "昨天", "今天", "明天",
    "最近", "近7天", "近30天", "过去", "前两天",
)


def _today_for_relative_question(question: str) -> str | None:
    """问句含相对时间词 → 返回 ISO 今天（`YYYY-MM-DD`）；否则 None（payload 不追加键）。"""
    q = str(question or "")
    if any(word in q for word in _RELATIVE_TIME_WORDS):
        return datetime.date.today().isoformat()
    return None


def _filter_join_plan(plan: JoinPlan, forbidden_pairs: list) -> JoinPlan:
    """禁止对命中时重建 JoinPlan：剔边，并把不再被任何边引用的 related_tables 一并去掉。"""
    if not forbidden_pairs:
        return plan
    kept = filter_joins(plan.joins, forbidden_pairs)
    if len(kept) == len(plan.joins):
        return plan
    tables = {str(j.get("to_table") or "") for j in kept}
    related = [r for r in (plan.related_tables or [])
               if str(r.get("table") or "") in tables]
    return JoinPlan(joins=kept, related_tables=related, truncated=plan.truncated)


async def _load_governance(tenant_id: str, kb_id: str, ds_id: str,
                           user_id: str | None) -> dict:
    """读取本次提问的标签/策略/密级 + 编译 schema；失败 fail-open 到「无治理」。"""
    try:
        async with get_db_session() as session:
            from ..governance.label_service import GovernanceLabelService
            from ..governance.policy_service import GovernancePolicyService
            from ..governance.clearance_service import GovernanceClearanceService
            from ..governance.levels import effective_level, level_rank
            labels = (await GovernanceLabelService(session).list(
                tenant_id, kb_id, ds_id))["items"]
            policies = (await GovernancePolicyService(session).list(
                tenant_id, kb_id))["items"]
            clearance = await GovernanceClearanceService(session).get(
                tenant_id, user_id)
        # 编译 schema **必须走版本化读口**（P1）：否则同一次提问会混用「缓存的
        # schema 视图」与「现读的治理视图」—— 列集合不一致会产出假的
        # `rejected_sensitive` / guard 拒绝，最长持续一个 TTL。放在治理会话**之外**
        # 读：别占着连接池的一条连接再开第二个会话。
        # 行不存在时 `tables_from_compiled_row` 抛 `InvalidParameterError`
        # （与旧 `schema_tables` 同型）→ 下面同一条 fail-open 接住。
        schema = schema_read.tables_from_compiled_row(
            await schema_read.load_compiled_row(tenant_id, kb_id, ds_id))
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 —— 治理是增能约束，读失败退空
        # kb/ds 一并记：行缺失那条的异常文案不再带 ds（投影函数签名里没有它）。
        logger.warning("治理数据读取失败，本次退空：kb=%s ds=%s err=%s",
                       kb_id, ds_id, exc)
        return {"active": False, "policies": [], "clearance": "public", "schema": {}}

    grouped: dict[tuple[str, str | None], list[dict]] = {}
    for r in labels:
        grouped.setdefault((r["table_name"], r["column_name"]), []).append(r)
    col_levels: dict[tuple[str, str], str] = {}
    table_levels: dict[str, str] = {}
    tags: dict[tuple[str, str | None], set[str]] = {}
    for (table, col), rows in grouped.items():
        level = effective_level(rows)
        if col is None:
            table_levels[table] = level
        else:
            col_levels[(table, col)] = level
        bucket: set[str] = set()
        for r in rows:
            bucket.update(str(t) for t in (r.get("tags") or ()) if str(t).strip())
        tags[(table, col)] = bucket
    # 表级是列级的下限
    for (table, col), level in list(col_levels.items()):
        floor = table_levels.get(table)
        if floor and level_rank(floor) > level_rank(level):
            col_levels[(table, col)] = floor
    return {
        "active": bool(labels or policies),
        "col_levels": col_levels, "table_levels": table_levels,
        "tags": tags, "policies": policies,
        "clearance": clearance, "schema": schema,
    }


def _governance_tables(gov: dict, table: str) -> list[str]:
    """SQL 表名 → 标签表名候选（精确优先，否则末段匹配）。"""
    names = set(gov.get("table_levels") or {})
    names |= {t for t, _ in (gov.get("col_levels") or {})}
    if table in names:
        return [table]
    last = table.split(".")[-1].lower()
    hits = [n for n in names if n.split(".")[-1].lower() == last]
    return hits or [table]


def _governance_level(gov: dict, table: str, column: str | None) -> str | None:
    from ..governance.levels import max_level
    levels: list[str | None] = []
    for t in _governance_tables(gov, table):
        if column is not None:
            levels.append((gov.get("col_levels") or {}).get((t, column)))
        levels.append((gov.get("table_levels") or {}).get(t))
    found = [lv for lv in levels if lv]
    return max_level(*found) if found else None


def _governance_tags(gov: dict, table: str, column: str | None) -> list[str]:
    out: set[str] = set()
    for t in _governance_tables(gov, table):
        out |= set((gov.get("tags") or {}).get((t, column)) or ())
        out |= set((gov.get("tags") or {}).get((t, None)) or ())
    return sorted(out)


def _governance_decide(gov: dict, table: str, column: str | None, action: str):
    from ..governance.pdp import decide
    return decide(
        clearance=str(gov.get("clearance") or "public"),
        level=_governance_level(gov, table, column),
        tags=_governance_tags(gov, table, column),
        policies=list(gov.get("policies") or []),
        action=action, table=table, column=column)


def _hints_meta(texts: list[str]) -> dict:
    """hints 的观测摘要：计数 / 总字符 / 内容哈希前 12 位（**不记原文**）。"""
    if not texts:
        return {}
    blob = "\n".join(texts)
    return {"count": len(texts), "chars": sum(len(t) for t in texts),
            "hash": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]}


def _sql_hash(sql: str) -> str:
    """执行工具的审计指纹：只记 hash，**不落完整 SQL**（探索语句不是用户答案）。"""
    return hashlib.sha256(str(sql or "").encode("utf-8")).hexdigest()[:12]


def _edge_ends(edge) -> tuple[str, str]:
    if isinstance(edge, dict):
        return (str(edge.get("from_table") or "").strip(),
                str(edge.get("to_table") or "").strip())
    return (str(getattr(edge, "from_table", "") or "").strip(),
            str(getattr(edge, "to_table", "") or "").strip())


def _exec_allowed_tables(*, anchor_entity: str, anchor_table: str,
                         related_tables, known_tables, lineage_edges,
                         join_edges=(), max_hops: int = 3) -> set[str]:
    """执行工具的表白名单（A3）：与最终答案**同源**的那几张表。

    - 锚点（实体名 + 物理表）+ 这次注入的 1 跳 related；
    - 多跳路径表：从锚点出发、沿「join 边 ∪ 血缘边」在 `max_hops` 内**可达**、
      且属于编译 schema 已知表的血缘端点。可达性过滤是必要的——只按「血缘端点 ∩
      已知表」取交会把**与本题无关**的远端表也放进来（审查 #1），而最终答案的白名单
      只含**实际注入的路径表**。
    """
    tables = {str(anchor_entity or "").strip(), str(anchor_table or "").strip()}
    for t in related_tables or []:
        tables.add(str((t or {}).get("table") or "").strip())
    known = {str(t).strip() for t in known_tables or () if str(t).strip()}
    adj: dict[str, set[str]] = {}
    for edge in list(join_edges or []) + list(lineage_edges or []):
        a, b = _edge_ends(edge)
        if not a or not b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    # 锚点与 1 跳 related 是**已经交给模型**的表，本身就是可达起点（不一定在边池里）；
    # 三者都为空时无从展开，白名单只剩空集（不能凭空造一个起点）。
    reachable = {t for t in tables if t}
    frontier = set(reachable)
    for _ in range(max(0, int(max_hops))):
        nxt: set[str] = set()
        for t in frontier:
            nxt |= adj.get(t, set())
        nxt -= reachable
        if not nxt:
            break
        reachable |= nxt
        frontier = nxt
    for edge in lineage_edges or []:
        for t in _edge_ends(edge):
            if t and t in known and t in reachable:
                tables.add(t)
    tables.discard("")
    return tables


def _build_exec_tool(*, source, cfg: dict, engine: str, allowed_tables: set[str],
                     forbidden_pairs, gov: dict, gov_active: bool,
                     stats: dict, max_rows: int | None = None
                     ) -> Callable[[str], Awaitable[dict]]:
    """构造 `execute_sql` 的异步执行闭包（A3）：normalize → guard → 治理 → 只读执行。

    **探索与最终答案是同一套安全口径**：同一个 guard 白名单/禁止同查、同一个治理
    decide、同一组只读凭据。探索失败**只回错误文本**（工具契约），不抛、不写审计；
    `stats` 只累计 calls/rows/errors，供观测落键。
    """
    max_rows = max(1, int(max_rows if max_rows is not None
                            else config.NL2SQL_EXEC_TOOL_MAX_ROWS))
    timeout = float(config.NL2SQL_EXEC_TOOL_TIMEOUT)

    async def executor(sql: str) -> dict:
        stats["calls"] += 1
        try:
            norm = normalize_dialect_sql(str(sql or "").strip(), engine)
            guarded = guard_sql(norm, engine=engine, max_rows=max_rows,
                                allowed_tables=allowed_tables,
                                forbidden_pairs=forbidden_pairs)
            if not guarded.ok:
                stats["errors"] += 1
                logger.info("NL→SQL 探索执行被安全闸拒绝: engine=%s reason=%s sql_hash=%s",
                            engine, guarded.reason, _sql_hash(norm))
                return {"error": f"rejected: {guarded.reason}"}
            exec_sql = guarded.sql
            if gov_active:
                from ..governance.sql_rewrite import apply_governance
                gov_result = apply_governance(
                    exec_sql, engine=engine,
                    decide_fn=lambda t, c, a: _governance_decide(gov, t, c, a),
                    schema=gov.get("schema") or {})
                if not gov_result.ok:
                    stats["errors"] += 1
                    logger.info("NL→SQL 探索执行被治理拒绝: engine=%s reason=%s sql_hash=%s",
                                engine, gov_result.reason, _sql_hash(norm))
                    return {"error": f"rejected: {gov_result.reason}"}
                exec_sql = gov_result.sql
            try:
                result = await asyncio.wait_for(
                    run_readonly(source, cfg, exec_sql), timeout=timeout)
            except asyncio.TimeoutError:
                stats["errors"] += 1
                return {"error": f"execution timed out after {timeout:g}s"}
            rows = list(result.get("rows") or [])[:max_rows]
            raw_count = result.get("row_count")
            row_count = int(raw_count if raw_count is not None else len(rows))
            stats["rows"] += len(rows)
            # guard 已把 LIMIT 钉在 max_rows，回满就是「可能还有更多」——这是**提示**，
            # 不是精确判定（正好 max_rows 行也会标 truncated，模型据此自行收窄）。
            return {"columns": list(result.get("columns") or []), "rows": rows,
                    "row_count": row_count, "truncated": row_count >= max_rows}
        except Exception as exc:  # noqa: BLE001 —— 探索失败不能掀翻整次提问
            stats["errors"] += 1
            return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    return executor


def _prompt_columns(columns: list[dict]) -> list[dict]:
    """喂模型的列清单：只留 `name`（物理列名）/ `type` / `samples`。

    `logical_name` 是内部对账用的（证据、概念落点、治理标签都按本体属性名登记），
    不进 payload —— 模型看到两个名字会犹豫该用哪个。`NL2SQL_COLUMN_DESC=1` 时
    额外带 `description`（截 120 字符）；默认关 ⇒ payload 逐字节不变。
    """
    out = []
    for column in columns or []:
        item = {k: v for k, v in column.items() if k in ("name", "type", "samples")}
        if config.NL2SQL_COLUMN_DESC:
            desc = str(column.get("description") or "").strip()[:120]
            if desc:
                item["description"] = desc
        out.append(item)
    return out


_DATE_IN_QUESTION = re.compile(
    r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"\s+\d{1,2},?\s+\d{4}\b",
    re.IGNORECASE,
)


def _want_date_cast(question: str) -> bool:
    """问句里有明确日历日期时才提示「时间戳列先转 date」。"""
    return bool(config.NL2SQL_DATE_CAST_HINT
                and _DATE_IN_QUESTION.search(str(question or "")))


_SUPERLATIVE = re.compile(
    r"\b(highest|lowest|oldest|youngest|largest|smallest|earliest|latest|"
    r"maximum|minimum|most|least|best|worst|fastest|slowest|heaviest|"
    r"lightest|peak|majority|top)\b", re.IGNORECASE)
_COMPARATIVE_OR = re.compile(
    r"\b(older|younger|higher|lower|larger|smaller|earlier|later)\b",
    re.IGNORECASE)
_SUPERLATIVE_EXCLUDE = re.compile(
    r"\b(top\s+\d+|each|respectively|rank)\b", re.IGNORECASE)


def _want_single_row(question: str) -> bool:
    """最高级问句（哪一个最高/最老/最多）要求最终结果单行。"""
    if not config.NL2SQL_SUPERLATIVE_LIMIT:
        return False
    text = str(question or "")
    superlative = bool(_SUPERLATIVE.search(text))
    comparative = (" or " in text.casefold()
                   and bool(_COMPARATIVE_OR.search(text)))
    if not (superlative or comparative):
        return False
    return not _SUPERLATIVE_EXCLUDE.search(text)


_ORDER_INTENT = re.compile(
    r"\b(top\s+\d+|bottom\s+\d+|latest|earliest)\b", re.IGNORECASE)


def _want_null_order(question: str) -> bool:
    """有排序意图（最高级/二选一/top N/latest）时提示显式 NULL 排序。"""
    if not config.NL2SQL_NULLS_ORDER:
        return False
    text = str(question or "")
    return bool(_SUPERLATIVE.search(text) or _COMPARATIVE_OR.search(text)
                or _ORDER_INTENT.search(text))


def _want_single_column(question: str) -> bool:
    """单属性/实体列表问句才提示「只投影一列」；多字段诉求不提示。"""
    if not config.NL2SQL_SINGLE_COLUMN_HINT:
        return False
    text = str(question or "").strip().casefold()
    if not text.startswith(("which ", "what ", "list ", "name ", "who ")):
        return False
    for marker in (" and ", " also ", " as well", " along with ",
                   " indicate ", " give me ", " give their ", " provide "):
        if marker in text:
            return False
    return True


def _want_cte_hint(joins, engine: str) -> bool:
    """多 join（≥2）+ 非 sqlserver 才给 CTE 提示；sqlserver 的 `WITH` 会被 guard 拒。"""
    if not config.NL2SQL_CTE_HINT:
        return False
    if len(list(joins or [])) < 2:
        return False
    return str(engine or "").strip().lower() not in ("sqlserver", "mssql")


def _filter_secret_columns(columns: list[dict], gov: dict, table: str) -> list[dict]:
    from ..governance.levels import level_rank
    clearance = str(gov.get("clearance") or "public")
    out = []
    for c in columns or ():
        # 治理标签按**本体属性名**登记（logical）；列清单的 `name` 可能是物理名。
        level = _governance_level(
            gov, table, str(c.get("logical_name") or c.get("name") or ""))
        if level == "secret" and level_rank(clearance) < level_rank("secret"):
            continue
        out.append(c)
    return out


def _filter_secret_joins(plan: JoinPlan, gov: dict, anchor_table: str) -> JoinPlan:
    from ..governance.levels import level_rank
    clearance = str(gov.get("clearance") or "public")
    if level_rank(clearance) >= level_rank("secret"):
        return plan
    kept = []
    for j in plan.joins:
        if (_governance_level(gov, str(j.get("from_table") or ""),
                              str(j.get("from_column") or "")) == "secret"
                or _governance_level(gov, str(j.get("to_table") or ""),
                                     str(j.get("to_column") or "")) == "secret"):
            continue
        kept.append(j)
    if len(kept) == len(plan.joins):
        return plan
    tables = {str(j.get("to_table") or "") for j in kept}
    related = [r for r in (plan.related_tables or [])
               if str(r.get("table") or "") in tables]
    return JoinPlan(joins=kept, related_tables=related, truncated=plan.truncated)


async def _record_governance_audit(tenant_id: str, kb_id: str, ds_id: str,
                                   user_id: str | None, decisions: tuple,
                                   sql: str) -> None:
    if not decisions:
        return
    try:
        import hashlib
        from ..governance.audit_service import GovernanceAuditService
        async with get_db_session() as session:
            await GovernanceAuditService(session).record_many(
                tenant_id, kb_id, list(decisions), user_id=user_id,
                data_source_id=ds_id,
                sql_hash=hashlib.sha256(sql.encode("utf-8")).hexdigest())
    except Exception as exc:  # noqa: BLE001 —— 审计 best-effort，不阻断查询
        logger.warning("治理判定审计写入失败（best-effort）：%s", exc)


def _audit_extra(llm_extra: dict | None, retried: bool, first_error: str | None) -> dict | None:
    """审计扩展键的唯一出处（成功/失败两条路共用）。

    多轮档的 `llm_extra`（rounds/tool_calls）原样带；`retried=True` 时追加
    `retried`/`first_error`。**空 dict 归 None** —— 未重试的单次档与今天逐字节相同。
    """
    extra = dict(llm_extra) if llm_extra else {}
    if retried:
        extra["retried"] = True
        extra["first_error"] = first_error
    return extra or None


def _abstract_tool_overrides(row: dict | None) -> tuple[dict[str, str], set[str]]:
    """抽象行 → `(tool_overrides, disabled_tools)`（演化台 `tool_schema`，D3）。

    抽取规则**只有一处实现**（`multiturn.extract_tool_overrides`，与
    `dislike_service.tool_options` 共用）—— 两处各写一遍必然漂移。
    行不存在 → `({}, set())`，调用方转成 None（请求不变）。
    """
    if not row:
        return {}, set()
    return multiturn.extract_tool_overrides(
        row.get("schema"), str(row.get("status") or ""))


def _build_retry_note(sql: str, err: str, *, with_tools: bool) -> str:
    """重试时附在首条 user 消息尾部的反馈文本（两档文案见 spec §4.1，逐字给死）。"""
    if with_tools:
        tail = ("Call `list_related` or `resolve_entity` to confirm the exact column and join "
                "names, then rewrite the SQL. Do not guess names.")
    else:
        tail = ("The exact column and join names are in the information you were given — "
                "check them again and rewrite the SQL. Do not guess names.")
    return (f"Your previous SQL failed at execution: {err}\n"
            f"Previous SQL: {sql}\n{tail}")


def _build_projection_note(violation: ProjectionViolation) -> str:
    """投影违规的重写反馈（附在 user prompt 尾部；最多触发一次）。"""
    return (
        f"Your previous SQL has a projection problem: {violation.detail}. "
        "Rewrite the SQL so the SELECT list contains exactly the output columns the "
        "question asks for; keep helper columns (ids, names used only for ordering, "
        "join keys, intermediate counts) in WHERE/JOIN/ORDER BY, not in SELECT."
    )



def _table_key(name: str) -> str:
    """表名归一（最后一段、小写）——与 guard 的白名单同口径。"""
    return str(name or "").strip().split(".")[-1].strip('"`[]').casefold()


def _build_empty_retry_note(sql: str, columns: list[dict]) -> str:
    """空结果修复的反馈（附在 user prompt 尾部；最多触发一次）。"""
    samples: list[str] = []
    for column in columns or []:
        values = column.get("samples") or []
        if values:
            samples.append(f"{column.get('name')}: {', '.join(str(v) for v in values[:5])}")
    hint = ("Column samples: " + "；".join(samples[:8])) if samples else ""
    simplify = ("Try simplifying some filter conditions first (drop the least "
                "certain predicate), then add filters back one at a time. "
                if config.NL2SQL_EMPTY_SIMPLIFY else "")
    return (
        f"The previous SQL executed successfully but returned 0 rows. Previous SQL: {sql}. "
        "Verify the WHERE/HAVING filters against real values (case, leading/trailing spaces, "
        "date bounds, code mappings) and the join keys, then rewrite the SQL. "
        + simplify
        + "If 0 rows is genuinely correct, return the same SQL unchanged. " + hint
    )


def _near_miss_rows(pre: list[dict], post: list[dict], filter_status: str) -> list[dict]:
    """候选近失池（≤5 条，`{entity_name, data_source_id, score}`）—— 设计 §5.3b。

    - **真筛过且筛后有命中**（`filter_status == "filtered"` 且 post 非空）→ 取筛后
      **前 5**（保序）；score 从 pre 池按 `(data_source_id, entity_name)` 回填
      （`filter_candidates` 各返回路径都过 `strip_private_keys`，post 已无 `_score`），
      回填不到 → None。
    - **其余情形**（未筛/筛失败 `skipped`·`passthrough`、筛后为空、点名档）→ 取 pre 池
      按 `_score` 降序（缺省视 -1）前 5。
    """
    def _row(c: dict, score) -> dict:
        return {"entity_name": str(c.get("entity_name") or ""),
                "data_source_id": str(c.get("data_source_id") or ""),
                "score": score if isinstance(score, int) else None}

    if filter_status == "filtered" and post:
        scores = {(str(c.get("data_source_id") or ""), str(c.get("entity_name") or "")):
                  c.get("_score") for c in pre}
        return [
            _row(c, scores.get((str(c.get("data_source_id") or ""),
                                str(c.get("entity_name") or ""))))
            for c in post[:observe.MAX_NEAR_MISS]
        ]
    ranked = sorted(pre, key=lambda c: c.get("_score") if isinstance(c.get("_score"), int) else -1,
                    reverse=True)
    return [_row(c, c.get("_score")) for c in ranked[:observe.MAX_NEAR_MISS]]


def _collect_concept_fields(entities: list, ds_id: str, source_entity: str,
                            column_index: dict[str, str] | None = None,
                            ) -> tuple[list[dict], int]:
    """按 (数据源, 源实体名) 反查**全部**已确认抽象实体的字段落点 → 概念组（spec §5.1/§5.2）。

    - 不依赖 `picked["concept"]`（点名档没有该键）——直接扫全部实体，口径同时覆盖两档；
    - **多个抽象实体共享同一 (ds, 实体) 落点时全部命中、全部注入**（注入是加权提示，
      多注入无害）；
    - `column_index`（键 = **casefold 后的 slug 或物理名**，值 = **物理名**）给出时，
      落点列先解析成物理名再输出（D11：抽象 schema 的落点存的是逻辑 slug，而列清单
      给的是物理名；不解析会把不存在的列写进 SQL），解析不到 → 跳过（`attribute_gone`
      防护：抽象 schema 里的落点可能指向已不存在的真实列，不把死列喂给模型）；
      缺省 `None` = 不校验、原样回显落点名（存量口径）。同组内按**输出名**去重
      （先到者胜）——「slug + 物理名」双形态落点只产一条；
    - **按需**：只对「本源+本实体的落点列集合 ≠ {属性名}」的属性产生——**纯**同名 1:1
      落点不产（列清单里长得一样，注入没有增量；一个都不产时下游 payload 逐字节不变）；
      2026-10-25（P1-5）豁免扩展为「**且**无 role、无任何语义槽」——带槽即增量；
    - `enabled is False` 的落点**先剔除**（人工停用的映射不参与注入；缺键/其它值 = 启用，
      与抽象 schema 的归一化同口径「只有显式 false 是停用」）；
    - `role` 自由文本、非空才带；`filter` / `aggregation` / `grain` 语义槽（映射库约定的
      物理实现）同样 strip 后非空才带；`display_name` 非空才带；
    - 坏输入（非 dict 实体/属性、`attributes`/`sources` 非列表）一律跳过、**不抛**
      （宽容收敛；与 `concept_expansion` 同族）。

    返回 `(concept_fields, 列总数)`；后者供 scene 计数（观测，设计 §5.3）。
    """
    groups: list[dict] = []
    n_columns = 0
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        attrs = ent.get("attributes")
        for attr in (attrs if isinstance(attrs, list) else []):
            if not isinstance(attr, dict):
                continue
            attr_name = str(attr.get("name") or "").strip()
            if not attr_name:
                continue
            sources = attr.get("sources")
            columns: list[dict] = []
            emitted: set[str] = set()
            for s in (sources if isinstance(sources, list) else []):
                if not isinstance(s, dict):
                    continue
                if s.get("enabled") is False:
                    continue  # 人工停用的落点：不注入（只有显式 false 是停用）
                if str(s.get("data_source_id") or "") != ds_id:
                    continue
                if str(s.get("entity_name") or "") != source_entity:
                    continue
                col = str(s.get("attribute_name") or "").strip()
                if not col:
                    continue
                if column_index is not None:
                    physical = column_index.get(col.casefold())
                    if not physical:
                        continue  # 落点列已不存在：不把死列喂给模型
                else:
                    physical = col
                if physical in emitted:
                    continue  # slug + 物理名双形态落点：按输出名去重（先到者胜）
                emitted.add(physical)
                item: dict = {"name": physical}
                role = str(s.get("role") or "").strip()
                if role:
                    item["role"] = role
                # 语义槽（P1-5）：映射库约定的物理实现，strip 后非空才带键。
                for key in ("filter", "aggregation", "grain"):
                    value = str(s.get(key) or "").strip()
                    if value:
                        item[key] = value
                columns.append(item)
            if not columns:
                continue
            if ({c["name"] for c in columns} == {attr_name}
                    and not any(k in c for c in columns
                                for k in ("role", "filter", "aggregation", "grain"))):
                continue  # **纯**同名 1:1（无 role、无槽）：列清单里同名同义，注入无增量
            group: dict = {"name": attr_name}
            display = str(attr.get("display_name") or "").strip()
            if display:
                group["display_name"] = display
            group["columns"] = columns
            groups.append(group)
            n_columns += len(columns)
    return groups, n_columns


def _target_tables(candidates: list[dict], entity_types: list[dict],
                   data_source_id: str) -> list[str]:
    """候选实体名 → 物理表名（`source_object_id`）。取不到的**跳过**（保守，不炸）。

    ⚠️ 必须转换：`search_paths` 的节点全是**物理表名**（`source_object_id`，如 `dbo.jobs`），
    而候选给的是**实体名**（如 `Jobs`）—— 实体名填进去等于白填（目标加成**永不命中**，
    且**静默无感**，不报错）。见 spec §4.1。

    ⚠️⚠️ **只转换锚点源（`data_source_id`）的候选**：`entity_types` 是锚点源编译 schema 的
    实体，而 `candidates` 横跨本 KB 的**所有源** —— 不过滤的话，**别源的同名候选**会被映射
    到**锚点源**的物理表，**造出一个假目标**。路径图只存在于锚点源的编译 schema 里，别源的
    表**根本不可能出现在图中** ⇒ 别源候选产出的目标天然是噪声，同名时更会**悄悄错排路径**。

    与 `multiturn._list_related` 里那份 `by_table`（`source_object_id → name`）同源但**方向相反**。
    """
    by_name = {str(e.get("name") or ""): str(e.get("source_object_id") or "").strip()
               for e in (entity_types or []) if isinstance(e, dict)}
    out: list[str] = []
    for c in candidates or []:
        if str(c.get("data_source_id") or "") != data_source_id:
            continue
        table = by_name.get(str(c.get("entity_name") or ""))
        if table:
            out.append(table)
    return out


class Nl2SqlService(MetricAskMixin, LineageSearchMixin):
    """无状态；每次调用自己开 session。"""

    def __init__(self) -> None:
        self._ds = DataSourceService()

    async def ask_data_source(
        self, tenant_id: str, kb_id: str, question: str, *,
        data_source_id: str = "", entity_name: str = "",
        user_id: str = "", username: str = "", ip: str = "",
        record_evidence: bool = True, observe_only: bool = False,
        evolution_run_id: str = "", hints: list[str] | str | None = None,
            offset: int = 0,
            extra_value_aliases: list[dict] | None = None,
    ) -> dict:
        """进程内问答（router 与演化评测共用同一条链路）。

        `record_evidence=False` 时**成功/失败都不写审计与运行时观测**：演化
        评测会反复问同一批题，把这些当真实用户提问记进审计与观测会污染
        诊断数据（失败归因、采纳率、no_samples 等统计）。默认 True，其它
        调用方行为逐字节不变。

        `observe_only=True`：只记录「零候选」概念缺口（写一条 `no_candidate`
        观测），有候选直接回 `skipped`；**不选源、不调模型、不执行 SQL**。
        router 在数据路径发现零候选时用它补齐演化台的失败样本（否则这类问题
        永远进不了失败列表，别名 run 无样本可提）。

        `evolution_run_id` 非空 **且指向本租户当前 `status ∈ {running, adopting}` 的 run**
        （running=运行中、adopting=采纳中）、**且 `username == EVOLUTION_USERNAME`** =
        这次提问来自**演化基准臂**
        （合成流量）：观测 scene 打上 `evolution_bench` 标记，诊断/点踩读取器整类
        排除（不冲淡真实失败归因），且**不写任何审计**（成功 / 失败 / 治理三条出口；
        `data_source.ask` 与治理判定审计都跳过）—— 合成流量反复跑同一批题，留痕只会
        是噪声。**只认 run id 不够**：普通用户拿到一个合法 run id 不能借此隐身，
        审计照写。指向不存在 / 别的租户 / 非 running|adopting 的 run 一律退成空串（校验本身
        故障也退空，fail-open —— 用户提问不能因为标记校验而失败）；空串或非演化身份
        = 普通用户提问，行为逐字节不变（观测标记仍按 run id 走，P0 取舍不变）。

        `extra_value_aliases`（请求级值同义词，P1 验真用）：形如
        `[{"term": ..., "canonical": ...}]`，与 KB 级 `ontology_synonyms` 合并后
        **同 term 以它为准**（只作用于本次请求、不写库）。合并结果同时用于
        **候选召回前的问句改写**与 SQL 生成 payload 注入 —— 验真候选在召回阶段
        就能生效（否则零候选题仍会在改写前失败）。开关
        `NL2SQL_VALUE_ALIASES_ENABLED` 关闭时整个字段被忽略 —— 与不传逐字节相同。
        """
        tenant_id = require_tenant(tenant_id)
        # 版本化读取（P1）：每次提问从**空账本**开始 —— 之后 `_load_concepts` /
        # `_entity_columns` 读到的漂移由 `observe.build_scene` 收口进观测 scene。
        # 紧跟 `require_tenant`（租户非法就已经抛了，不必先起账本）、在任何读之前。
        schema_read.start_drift_collection()
        # 语义槽计数（P1-5）：概念字段组装**之前**恒 0（那些早退路径根本没收映射）；
        # 组装后重算。每个 `build_scene` 都带它，`>0` 才落键（按档出现纪律）。
        mapping_slots = 0
        q = (question or "").strip()
        if not q:
            raise InvalidParameterError(message=translate(
                "err.nl2sql.empty_question", fallback="问题不能为空"))
        if len(q) > config.NL2SQL_MAX_QUESTION_LENGTH:
            raise InvalidParameterError(message=translate(
                "err.nl2sql.question_too_long",
                params={"max": config.NL2SQL_MAX_QUESTION_LENGTH},
                fallback=f"问题过长（上限 {config.NL2SQL_MAX_QUESTION_LENGTH} 字）",
            ))
        # 基准标记只对**本租户、运行中/采纳中（running|adopting）**的 run 生效：
        # 否则任何调用方都能把提问伪装成基准流量、从失败诊断里消失。
        # 入口归一**一次**，下游每个
        # `observe.build_scene(evolution_run=…)` 用的都是校验后的值 —— 包括
        # `structured_disabled` 那条早退软失败。空串是普通提问，**不查库**。
        evolution_run_id = (
            await self._valid_evolution_run(tenant_id, evolution_run_id)
            if evolution_run_id else "")
        # 合成流量（演化基准臂）：**校验通过的 run id + 演化身份**双条件才算合成 ——
        # 不写 `data_source.ask` 审计（成功/失败/治理三条出口），观测照写（诊断靠它）。
        # ⚠️ 只认 run id 不够：知道一个合法 run id 的普通用户（`username` 不是
        # `evolution`）若能借此抑制自己的审计，就是一条可用的「隐身」口子；基线臂
        # 的网关 token 与 `upstream.asker_for` 都以 `EVOLUTION_USERNAME` 留痕。
        # 归一之前为空 / 校验不通过（不存在、别租户、非 running/adopting）一律退空串；
        # 任一条件不满足 ⇒ 普通提问，审计照写（观测标记不变）。绑定 `fail` 让 11 个
        # 失败出口不必各自传参（纯机械替换）。
        synthetic = bool(evolution_run_id) and username == EVOLUTION_USERNAME
        # 失败出口的 `fail` 一次性绑定采样条件：11 个调用点不必各自传，
        # 也不会把 evolution_run_id/observe_only 漏进 `**extra` 响应体。
        fail = functools.partial(
            self._fail, synthetic=synthetic,
            evolution_run_id=evolution_run_id, observe_only=observe_only,
        )
        # W1.6 分页：offset 只影响执行 SQL（guard 追加 OFFSET），不改变默认行为。
        if isinstance(offset, bool) or not isinstance(offset, int) or not (
                0 <= offset <= 1_000_000):
            raise InvalidParameterError(
                message="offset 必须是 0..1000000 的整数")
        # hints（A2）：开关关时**完全忽略**（请求形状不变）；开时 fail-closed 归一化。
        hint_texts = normalize_hints(hints) if config.NL2SQL_HINTS else []
        hints_meta = _hints_meta(hint_texts)
        # KB 存在性**必须在这里硬校验**：解析不到 KB 时下游只会回 `no_candidate`
        # （文案说「没命中任何已对齐的数据实体」），而真因可能是**这个库压根不存在** ——
        # 把调用方写错的 id 说成「数据里没有」，是最难查的那类误导。本模块 docstring
        # 第一段把「KB 不存在」归在「调用方用错要抛」那一档，这里兑现。
        kb = await self._ensure_kb(tenant_id, kb_id)
        # 观测补齐（observe_only）只对结构化库有意义：非结构化库连概念都没有，
        # 记成「零候选」只会把配置门没开说成数据缺口。静默回 skipped、不落任何行。
        if observe_only and not bool(getattr(kb, "structured_enabled", False)):
            return {"ok": False, "not_answerable": True,
                    "reason_code": "skipped", "reason": "该知识库未启用结构化数据支持"}
        # 结构化关闭 = **软失败**（区别于上面的硬 404：库存在、只是这扇门没开）。
        # 在候选/模型/凭据**一切之前**拦下：`structured_disabled` 不是「数据里答不上」，
        # 是「这个 KB 没开结构化支持」—— 继续往下走只会把门没开说成数据没有。
        if not bool(getattr(kb, "structured_enabled", False)):
            # 基准流量（`evolution_run_id` 非空）同样打标记：这条软失败若落成未标记
            # 观测，就会以 `structured_disabled` 混进失败归因。非基准提问保持原样
            # （`scene=None` ⇒ `{}`，与改动前逐字节相同）。
            bench_scene = (
                observe.build_scene(multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta,
                                    evolution_run=evolution_run_id,
                                    mapping_slots=mapping_slots)
                if evolution_run_id else None)
            return fail(
                tenant_id, user_id, username, ip, q, data_source_id or "",
                "structured_disabled", "该知识库未启用结构化数据支持",
                scene=bench_scene, kb_id=kb_id, record_evidence=record_evidence,
            )

        # 值同义词改写（2026-11-05）：候选召回 / 生成看**改写后**的问句，审计与观测
        # 仍留原问句。此前 P1 只把别名注入 SQL 生成 payload，召回更早看到的是原词面
        # （「华东环保」选不中 `GroupMaster`），于是 no_candidate 卡在注入之前。
        # 合并口径与注入路径**同一份**（库组 ⊕ 请求级 extra，同 term 以 extra 为准）——
        # 否则验真调用传入的未采纳候选在召回阶段无效，no_candidate 验真仍然失败。
        # 开关关 / 零命中 ⇒ `q_retrieval` 与 `q` 相同，行为与引入前逐字节一致。
        q_retrieval, alias_rewrites = (
            value_aliases.rewrite_question(
                q,
                value_aliases._merge_extra(
                    await value_aliases.load_groups(tenant_id, kb_id),
                    extra_value_aliases),
                limit=config.NL2SQL_VALUE_ALIASES_MAX_GROUPS)
            if config.NL2SQL_VALUE_ALIASES_ENABLED else (q, []))
        entities, ds_names = await self._load_concepts(tenant_id, kb_id)
        candidates = expand_candidates(q_retrieval, entities, knowledge_base_id=kb_id)
        if observe_only:
            # 只做概念展开：有候选 = 不是概念缺口，跳过；零候选 = 落一条 no_candidate。
            # 不经过相关性筛/选源/模型/执行，成本只有一次抽象 schema 读取。
            if candidates:
                return {"ok": False, "not_answerable": True,
                        "reason_code": "skipped",
                        "reason": "已命中候选，未记录概念缺口"}
            return fail(
                tenant_id, user_id, username, ip, q, "", "no_candidate",
                "没有命中任何已对齐的数据实体",
                scene=observe.build_scene(
                    evolution_run=evolution_run_id,
                    multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta,
                    value_alias_rewrite=alias_rewrites or None,
                    filter_status="skipped",
                    mapping_slots=mapping_slots),
                kb_id=kb_id, record_evidence=record_evidence,
            )
        # 近失池（运行时观测，设计 §5.3b）：筛前留一份 —— `filter_candidates` 的各返回
        # 路径恒过 `strip_private_keys`（`_score` 被剥），score 回填只能从这份拿。
        pre_filter = list(candidates)
        # 观测记录器（设计 §5.3a）：在 attempt 循环**之外**创建、跨尝试保留
        # （B 重试第二次的轨迹接着记）；单次档为 None（scene 无 tool_calls 键）。
        # 帽 = 循环 LLM 往返上界（rounds+1，含预算用尽补轮）× 每回合帽 —— **松界**：
        # 单循环的真实记录上界是 max(1, rounds)×每回合帽（补轮直接 break、不经 _consume），
        # 截断只在「B 重试 + 重工具」时才可能触发（保末 N，方向安全）。
        recorder = (observe.ToolRecorder(
            max_calls=(config.NL2SQL_MULTITURN_MAX_ROUNDS + 1)
                      * config.NL2SQL_MULTITURN_MAX_TOOL_CALLS_PER_ROUND)
            if config.NL2SQL_MULTITURN else None)
        filter_status = ""
        if not entity_name:
            # [v2] 相关性筛选（spec 决策 9：点名时整段跳过——点名的意义就是绕过推断，
            # 且 `_pick` 的点名分支不看候选集）。失败/关开关 → 原样（filter 内部兜）。
            # 请求已指定 data_source_id 时，先按源收窄再送筛：多源 KB 里同一问题
            # 可能命中别的源的实体，筛选模型会把目标源的候选一起筛掉（切库场景的
            # 真机问题）。收窄后为空仍走原路径，由 `_pick` 给 no_candidate。
            filter_candidates_in = self._scope_candidates(candidates, data_source_id)
            candidates, filter_status = await filter_candidates(
                q_retrieval, filter_candidates_in, tenant_id=tenant_id, kb_id=kb_id)
        picked, failure = self._pick(candidates, data_source_id, entity_name)
        if (picked is None and config.NL2SQL_NO_CANDIDATE_FALLBACK
                and not entity_name and data_source_id
                and failure is not None
                and failure.get("reason_code") == "no_candidate"):
            picked = await self._fallback_anchor(
                tenant_id, kb_id, data_source_id, q)
            if picked is not None:
                filter_status = "anchor_fallback"
        if picked is None:
            # `failure` 是 `_no_answer(...)` 的返回值（含 ok/not_answerable/reason_code/reason
            # + 可能的 candidates）—— 剥掉壳上那四个键，其余原样透传给 `_fail`。
            code = failure["reason_code"]
            reason = failure["reason"]
            extra = {k: v for k, v in failure.items()
                     if k not in ("ok", "not_answerable", "reason_code", "reason")}
            return fail(tenant_id, user_id, username, ip, q,
                        data_source_id or "", code, reason,
                        scene=observe.build_scene(
                            evolution_run=evolution_run_id,
                            multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta,
                            value_alias_rewrite=alias_rewrites or None,
                            filter_status=filter_status,
                            near_miss=_near_miss_rows(pre_filter, candidates, filter_status),
                            recorder=recorder,
                            mapping_slots=mapping_slots),
                        kb_id=kb_id, record_evidence=record_evidence,
                        **extra)

        ds_id, ent = picked["data_source_id"], picked["entity_name"]
        resolved = await self._entity_columns(tenant_id, kb_id, ds_id, ent)
        if resolved is None:
            return fail(
                tenant_id, user_id, username, ip, q, ds_id, "entity_gone",
                f"数据源里已经没有实体 {ent}（schema 可能重生成过）",
                scene=observe.build_scene(multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta,
                                          evolution_run=evolution_run_id,
                                          value_alias_rewrite=alias_rewrites or None,
                                          anchor=ent, recorder=recorder,
                                          mapping_slots=mapping_slots),
                kb_id=kb_id, entity_name=ent,
                record_evidence=record_evidence)
        source_object_id, columns, join_plan, samples_by_column, raw_constraints = resolved
        forbidden_pairs = await _load_forbidden_pairs(tenant_id, kb_id, ds_id)
        join_plan = _filter_join_plan(join_plan, forbidden_pairs)
        gov = await _load_governance(tenant_id, kb_id, ds_id, user_id)
        gov_active = bool(config.GOVERNANCE_ENFORCEMENT_ENABLED and gov.get("active"))
        if gov_active:
            # 生成前预防：无权限看的 secret 列不进注入面（生成后仍有安全闸兜底）。
            columns = _filter_secret_columns(columns, gov, source_object_id or ent)
            join_plan = _filter_secret_joins(join_plan, gov, source_object_id or ent)
        # 证据按需注入：只给「像枚举」的列；没有证据时一个键都不加（payload 与今天逐字节相同）
        columns = [{**c, **({"samples": samples_by_column[c.get("logical_name") or c["name"]]}
                            if (c.get("logical_name") or c["name"]) in samples_by_column else {})}
                   for c in columns]
        # 口径约束同样按需：只挑**锚点实体自己**的（entity/attribute 两级）、带 expression 的，
        # ≤8 条（2026-10-12 起）；一条都没有时 `generate_sql` 那边一个键都不加（payload 逐字节不变）。
        constraints = filter_constraints(raw_constraints, ent)
        # 生产侧 hints（A2 第二半）：只在 hints 通道 + 自动组装**双开**时做。
        # 材料全部来自本次已读的 schema（编译行 + active 抽象行）；没有材料 =
        # 不追加任何 hint，请求与只开通道时逐字节相同。
        if config.NL2SQL_HINTS and config.NL2SQL_AUTO_HINTS:
            auto = await self._auto_hints(
                tenant_id=tenant_id, kb_id=kb_id, ds_id=ds_id, question=q_retrieval, ent=ent,
                join_plan=join_plan, constraints=constraints, abstract_entities=entities)
            if auto:
                hint_texts = merge_hints(hint_texts, auto)
                hints_meta = _hints_meta(hint_texts)
        # evidence 改写（P1-2）：有 hints 才触发；失败 fail-open 到原始 hints。
        if config.NL2SQL_HINTS and config.NL2SQL_EVIDENCE_REWRITE and hint_texts:
            rewritten = await rewrite_evidence_hints(
                q_retrieval, hint_texts, tenant_id=tenant_id, kb_id=kb_id,
                user_id=user_id or "")
            if rewritten:
                hint_texts = merge_hints(hint_texts, rewritten)
                hints_meta = {**_hints_meta(hint_texts), "rewritten": True}
        # S2/S4 共用同一份「列 → 已存样例」：只吃编译证据，不额外查库。
        sample_values = {
            str(c.get("name") or ""): list(c.get("samples") or [])
            for c in columns
            if c.get("name") and c.get("samples")
        } if (config.NL2SQL_HINTS and (
              config.NL2SQL_VALUE_MAPPING or config.NL2SQL_PROBE_SUMMARY
              or config.NL2SQL_AMBIGUITY_PRECHECK)) else {}
        # S7 确定性歧义预检（2026-10-30）：纯规则，不调模型；有证据才追加 hint。
        if config.NL2SQL_HINTS and config.NL2SQL_AMBIGUITY_PRECHECK:
            ambiguity_hints = find_ambiguities(q_retrieval, columns, sample_values)
            if ambiguity_hints:
                hint_texts = merge_hints(hint_texts, ambiguity_hints)
                hints_meta = _hints_meta(hint_texts)
        # S2 语义值映射（2026-10-30）：映射结果必须经列字典校验
        # （`parse_mapping` 只保留真实值）；失败 fail-open。
        if config.NL2SQL_HINTS and config.NL2SQL_VALUE_MAPPING:
            values_by_column = dict(list(sample_values.items())[:5])
            if values_by_column:
                try:
                    mapped = await map_question_values(
                        q_retrieval, values_by_column, hints=hint_texts or None,
                        tenant_id=tenant_id, kb_id=kb_id, user_id=user_id or "")
                except Exception as exc:  # noqa: BLE001 —— 值映射是增能，失败跳过
                    logger.warning("值映射失败（跳过）: %s", exc)
                    mapped = {}
                mapped_hints = [
                    f"column {column}: the question's value matches these exact "
                    f"stored values: {', '.join(values)}; use them verbatim if "
                    f"filtering on {column}."
                    for column, values in mapped.items() if values
                ]
                if mapped_hints:
                    hint_texts = merge_hints(hint_texts, mapped_hints)
                    hints_meta = _hints_meta(hint_texts)
        # S4 探查→摘要（2026-10-30）：把列样例摘要成枚举/单位/同义词，进 hints。
        if config.NL2SQL_HINTS and config.NL2SQL_PROBE_SUMMARY and sample_values:
            summaries_by_column = dict(list(sample_values.items())[:5])
            try:
                summaries = await summarize_columns(
                    q_retrieval, summaries_by_column, tenant_id=tenant_id, kb_id=kb_id,
                    user_id=user_id or "")
            except Exception as exc:  # noqa: BLE001 —— 摘要是增能，失败跳过
                logger.warning("探查摘要失败（跳过）: %s", exc)
                summaries = {}
            summary_hints = [
                f"column {column}: {summary}"
                for column, summary in summaries.items() if summary
            ]
            if summary_hints:
                hint_texts = merge_hints(hint_texts, summary_hints)
                hints_meta = _hints_meta(hint_texts)
        # 语义注入档（§五）在 resolved 之后组装：落点列先解析成**物理列名**
        # （D11：抽象 schema 的落点存逻辑 slug，列清单给物理名；不解析会把不存在的
        # 列写进 SQL），再与**真实列**对账、剔除 attribute_gone 的过期条目（不把死列
        # 喂给模型）。开关关 / 没有落点差异时一个组都不产——下游 payload 与 prompt
        # 段逐字节不变。
        concept_fields: list[dict] = []
        n_concept_cols = 0
        if config.NL2SQL_CONCEPT_FIELDS:
            column_index: dict[str, str] = {}
            for c in columns:
                # 键与值都 **strip**：列名带首尾空白时，落点名（查前已 strip）与索引键
                # 永不相等 ⇒ 该落点会被静默当成 `attribute_gone` 丢掉（Task 1 复审遗留）。
                physical = str(c.get("name") or "").strip()
                logical = str(c.get("logical_name") or "").strip() or physical
                if physical:
                    column_index.setdefault(physical.casefold(), physical)
                if logical:
                    column_index.setdefault(logical.casefold(), physical)
            concept_fields, n_concept_cols = _collect_concept_fields(
                entities, ds_id, ent, column_index=column_index)
            # 语义槽计数（P1-5，设计 §5.3）：scene 归因用（`mapping_slots` >0 才落键）。
            mapping_slots = sum(
                1 for g in concept_fields for c in g["columns"]
                for k in ("filter", "aggregation", "grain") if c.get(k))

        # P2-2 小 schema 预览：只在该源表数 ≤ 上限、且无治理策略时生成；
        # 预览表会并入安全闸白名单（否则模型写对了也会被拒）。
        # P2-3 大 schema 分块选择（2026-10-31）：开关开且表数/预算超限时，用选择器
        # （题面 + 锚点/1 跳 + join 连通闭包）替代一次性全量；输出同 `schema_preview` 形状。
        schema_preview: list[dict] = []
        preview_tables: set[str] = set()
        cte_view_used = False
        # CTE 选表与多轮路径共用同一份血缘（只读一次）；关档/未触发时保持 None。
        preloaded_lineage: list | None = None
        cte_entity_types: list[dict] = []
        cte_relation_types: list[dict] = []
        if (config.NL2SQL_SCHEMA_PREVIEW or config.NL2SQL_CTE_VIEW) and not gov_active:
            preview_row = await schema_read.load_compiled_row(tenant_id, kb_id, ds_id)
            if preview_row:
                entity_types = preview_row.get("entity_types") or []
                cte_entity_types = entity_types
                cte_relation_types = preview_row.get("relation_types") or []
                if (config.NL2SQL_CTE_VIEW
                        and should_select_schema(
                            entity_types,
                            budget_tokens=config.NL2SQL_SCHEMA_CONTEXT_BUDGET)):
                    if config.NL2SQL_CTE_LINEAGE:
                        preloaded_lineage = await _load_lineage_edges(
                            tenant_id, kb_id, ds_id)
                    schema_preview, preview_tables = select_schema(
                        entity_types,
                        preview_row.get("relation_types") or [],
                        question=q_retrieval, anchor=ent,
                        related_tables=join_plan.related_tables,
                        budget_tokens=config.NL2SQL_SCHEMA_CONTEXT_BUDGET,
                        max_tables=config.NL2SQL_SCHEMA_MAX_TABLES,
                        lineage_edges=preloaded_lineage,
                        entity_seeds=config.NL2SQL_CTE_ENTITY_SEEDS)
                    cte_view_used = True
                elif config.NL2SQL_SCHEMA_PREVIEW:
                    preview_kwargs = {"anchor": ent,
                                      "related_tables": join_plan.related_tables}
                    if config.NL2SQL_PREVIEW_EXAMPLES:
                        # P2-4a：只读同一行里**已存证据**，不查源库。
                        preview_kwargs.update(
                            include_examples=True,
                            tables_evidence=(preview_row.get("evidence") or {}).get("tables") or {},
                            example_limit=config.NL2SQL_PREVIEW_EXAMPLE_LIMIT,
                            example_budget=config.NL2SQL_PREVIEW_EXAMPLE_BUDGET)
                    if config.NL2SQL_PREVIEW_DETAILS:
                        preview_kwargs.update(include_details=True)
                    schema_preview, preview_tables = build_schema_preview(
                        entity_types, **preview_kwargs)
        # 同库 few-shot（2026-10-29）：按当前表集合从语料里挑 ≤K 条；读不到就跳过。
        # 生产入口（2026-10-30）：路径为空且开 FROM_CORPUS 时，从演化台同库语料读。
        few_shots: list[dict] = []
        if config.NL2SQL_FEWSHOT and (
                config.NL2SQL_FEWSHOT_PATH or config.NL2SQL_FEWSHOT_FROM_CORPUS):
            tables = {str(source_object_id or ent)}
            tables |= {str(t.get("table") or "")
                       for t in (join_plan.related_tables or [])}
            tables |= set(preview_tables)
            try:
                if config.NL2SQL_FEWSHOT_PATH:
                    rows = load_examples(config.NL2SQL_FEWSHOT_PATH)
                else:
                    rows = await load_corpus_examples(
                        tenant_id, kb_id, ds_id,
                        ttl=config.NL2SQL_FEWSHOT_CACHE_TTL)
                few_shots = select_examples(
                    rows, tables, k=config.NL2SQL_FEWSHOT_K, question=q_retrieval)
            except Exception as exc:  # noqa: BLE001 —— 语料是增能，读不到不影响提问
                logger.warning("few-shot 语料读取失败（跳过）: %s", exc)
        preview_stats = {
            "tables": len(schema_preview),
            "example_cols": sum(len(e.get("column_examples") or {})
                                for e in schema_preview),
            "example_chars": sum(len(v) + 2
                                 for e in schema_preview
                                 for vals in (e.get("column_examples") or {}).values()
                                 for v in vals),
        }
        # 大 schema join 提示（2026-10-31）：只在 CTE 选择器生效时加，避免影响小 schema。
        cte_join_hint = bool(config.NL2SQL_CTE_JOIN_HINT and cte_view_used)
        # 行粒度 / 否定条件（2026-11-01）：窄触发，关档提示词逐字节不变。
        grain_hint = bool(config.NL2SQL_GRAIN_HINT and wants_entity_grain(q))
        left_join_hint = bool(
            config.NL2SQL_LEFT_JOIN_HINT and wants_negative_case(q))
        # 结构化 join 边：只对 CTE 选中的表；关档不计算、payload 不变。
        cte_joins: list[dict] = []
        if cte_view_used and config.NL2SQL_CTE_JOIN_EDGES:
            cte_joins = selected_join_edges(
                cte_entity_types, cte_relation_types, preview_tables, anchor=ent)
        # 维度标签 join（2026-11-01）：只在 CTE 生效且开关开时计算；问句不要标签时为空。
        dimension_joins: list[dict] = []
        if cte_view_used and config.NL2SQL_DIMENSION_JOINS:
            dimension_joins = build_dimension_joins(
                cte_entity_types, cte_relation_types, preview_tables, q,
                anchor=ent, max_joins=config.NL2SQL_DIMENSION_JOIN_MAX)
        if cte_view_used:
            preview_stats["cte_view"] = True
            # 诊断用：把选择器实际选中的**表**（只表名、不含列）写进观测，
            # 用于区分「CTE 漏选」与「模型没写 join」。截 64 个，杜绝大 schema 撑大 scene。
            preview_stats["cte_tables"] = sorted(str(t) for t in preview_tables)[:64]

        try:
            target = await self._ds.query_config(tenant_id, ds_id)
        except (ResourceNotFoundError, InvalidParameterError) as exc:
            # 源被删（编译出来的 schema 行**不会跟着删**，所以上面取列那步还找得到）、
            # 或编译后被改成非 database 源 / 引擎本层不支持 —— 都是「这个源用不了」，
            # 与下面 LLM 那条同一口径：③ 会并发扇出到多个源，一个源的 404 不该掀翻整个提问。
            logger.warning("NL→SQL 取连接失败: ds=%s err=%s", ds_id, exc)
            code = ("source_gone" if isinstance(exc, ResourceNotFoundError)
                    else "not_a_database_source")
            return fail(tenant_id, user_id, username, ip, q, ds_id, code,
                        f"{type(exc).__name__}: {str(exc)[:200]}",
                        scene=observe.build_scene(multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta,
                                                  evolution_run=evolution_run_id,
                                                  value_alias_rewrite=alias_rewrites or None,
                                                  anchor=ent, recorder=recorder,
                                                  concept_fields=len(concept_fields),
                                                  concept_columns=n_concept_cols,
                                                  mapping_slots=mapping_slots),
                        kb_id=kb_id, entity_name=ent,
                        record_evidence=record_evidence)
        if target is None:
            return fail(
                tenant_id, user_id, username, ip, q, ds_id, "no_readonly_creds",
                "该数据源未登记只读凭据，不能执行查询（请在连接池里补只读账号）",
                scene=observe.build_scene(multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta,
                                          evolution_run=evolution_run_id,
                                          value_alias_rewrite=alias_rewrites or None,
                                          anchor=ent, recorder=recorder,
                                          concept_fields=len(concept_fields),
                                          concept_columns=n_concept_cols,
                                          mapping_slots=mapping_slots),
                kb_id=kb_id, entity_name=ent,
                record_evidence=record_evidence)
        source, cfg = target
        engine = str(cfg.get("engine") or "")
        # P2-4b 采样行：只对预览表、只 SELECT 指定列；失败 fail-open。
        if config.NL2SQL_SAMPLE_ROWS and schema_preview and not gov_active:
            preview_stats.update(await self._attach_sample_rows(
                source, cfg, engine, schema_preview))
        today_text = _today_for_relative_question(q_retrieval)
        # 排名列提示（2026-10-29）：只在问题真说 rank 时加规则 4l，避免给普通排序题加列。
        q_fold = str(q_retrieval or "").casefold()
        rank_hint = bool(config.NL2SQL_RANK_HINT and "rank" in q_fold)
        # 「平均每 Y 有多少 X」（2026-10-29）：先按 Y 分组 COUNT，再对计数求 AVG。
        per_group_avg = bool(
            config.NL2SQL_PER_GROUP_AVG_HINT
            and ("average number of" in q_fold or "on average how many" in q_fold))
        # 输出列精确性（2026-10-29）：点名字段只投影那些列；未点名字段只投影标识列。
        exact_fields = bool(config.NL2SQL_PROJECTION_EXACT_FIELDS)
        # 预览列明细（2026-10-30）：列带 type/description，提示词同步换 4j 明细版。
        preview_details = bool(config.NL2SQL_PREVIEW_DETAILS)
        # 多 join 题的 CTE 提示（2026-10-30）：sqlserver 不给（WITH 被 guard 拒）。
        cte_hint = _want_cte_hint(join_plan.joins, engine)
        # 除法分母 NULLIF（2026-10-30）：避免 PG division by zero。
        nullif_hint = bool(config.NL2SQL_NULLIF_HINT)
        # 单属性问句只投影一列（2026-10-30）：窄触发，多字段问句不提示。
        single_column = _want_single_column(q_retrieval)
        # 日期 cast（2026-10-30）：只在问句含明确日历日期时提示。
        date_cast = _want_date_cast(q_retrieval)
        # 最高级单行（2026-10-30）：只在「哪一个最高/最老」类问句时提示。
        superlative_limit = _want_single_row(q_retrieval)
        # 显式 NULL 排序（2026-10-30）：有排序意图时提示 NULLS LAST/FIRST。
        null_order = _want_null_order(q_retrieval)
        # S1 格式预测（2026-10-30）：生成前一次调用，只预测应返回的列；失败退空。
        answer_format: list[str] = []
        if config.NL2SQL_FORMAT_PREDICT:
            try:
                answer_format = await predict_format(
                    q_retrieval, engine=engine, table_name=source_object_id or ent,
                    columns=_prompt_columns(columns), joins=join_plan.joins,
                    hints=hint_texts or None, tenant_id=tenant_id,
                    kb_id=kb_id, user_id=user_id or "")
            except Exception as exc:  # noqa: BLE001 —— 预测是增能，失败不影响生成
                logger.warning("格式预测失败（跳过）: %s", exc)
        format_stats = {"cols": len(answer_format)}
        # hint 忠实度（2026-10-29）：只在真有 hints 时加规则 4o。
        hint_exactness = bool(config.NL2SQL_HINT_EXACTNESS and hint_texts)
        # 百分比分母（2026-10-29）：只在问「percentage of / proportion of」时加规则 4p。
        percentage_base = bool(
            config.NL2SQL_PERCENTAGE_BASE_HINT
            and ("percentage of" in q_fold or "proportion of" in q_fold))
        # P2-1 planner：生成前一次分析（不写 SQL）；失败退空，payload 与无 plan 时相同。
        plan_text = ""
        plan_stats = {"chars": 0}
        if config.NL2SQL_PLANNER and (
                not config.NL2SQL_PLANNER_SELECTIVE or join_plan.joins):
            plan_text = await plan_sql(
                q_retrieval, table_name=source_object_id or ent, columns=_prompt_columns(columns),
                joins=join_plan.joins, related_tables=join_plan.related_tables,
                constraints=constraints, hints=hint_texts or None, today=today_text,
                tenant_id=tenant_id, kb_id=kb_id, user_id=user_id or "")
            plan_stats = {"chars": len(plan_text)}
        # 值同义词（P1，2026-11-04）：**只在真要生成 SQL 时**按问句词面命中 KB 级
        # `ontology_synonyms`，只挑相关的组（`extra_value_aliases` 同 term 覆盖库值、
        # 不写库）。开关关 / 零命中 ⇒ `[]` ⇒ 下游传 `None`，payload 与引入前逐字节
        # 相同；读库失败在 `value_aliases` 内部 fail-open 成空（增能项，不拖垮提问）。
        # ⚠️ 这里刻意用**原问句** `q`（不是 `q_retrieval`）：别名注入的意义就是把
        # 「问题里的 variant → canonical」告诉模型；改写后再匹配只会得到自己映射
        # 自己的空转条目（`term == canonical` 已被 `rewrite_question` 跳过）。
        aliases = (await value_aliases.resolve_for_question(
            tenant_id, kb_id, q, extra=extra_value_aliases)
            if config.NL2SQL_VALUE_ALIASES_ENABLED else [])

        # ⚠️ 必须包 wait_for：LLM 那次调用**自身没有上界**（Task 6 的实现者发现
        # `NL2SQL_LLM_TIMEOUT` 定义完没人消费）。不包的话，上游卡住 = 这个请求永远挂着，
        # 而 spec §六 要的是有界。多轮档同理，用**另一份**更宽的总预算
        # （`NL2SQL_MULTITURN_TIMEOUT`：整个循环最多 3 次往返都包在这一个 wait_for 里）。
        #
        # B（锚错兜底）：整段包进 attempt 循环（最多 1 + `_RETRY_MAX` 次）。重试的唯一
        # 触发 = **执行期**白名单错误（`is_fixable_exec_error`）且还在首发；其余一切
        # （生成坏 / not_answerable / guard 拒 / 白名单外执行错）都维持「一次即终局」。
        llm_extra: dict | None = None
        retry_note: str | None = None
        retried, first_error = False, None
        # 投影契约（A1）：每题最多修复一次；第二次仍违规就按现状返回。
        projection_retried, projection_code = False, ""
        # 投影重写失败时的兜底：第一次生成的 SQL 虽然违反投影契约，但通常是**可执行**的
        # ——重写那一轮回 not_answerable / 生成崩了时，不能把已有答案丢掉（A1 审查 #4）。
        # 空结果修复（P0-2）：独立预算，最多一次；不占 B/投影的额度。
        empty_retried, empty_retries = False, 0
        unit_retried, unit_retries = False, 0
        # 空结果重写失败时回退到**原始空结果**（合法空答案不能被改坏）。
        empty_fallback: dict | None = None
        projection_fallback: dict | None = None
        projection_fallback_meta: dict | None = None
        projection_fallback_payload = ""
        projection_fallback_code = ""
        # B 重试预算**独立计数**（A1 审查 #4）：投影重写不应吃掉执行失败的那次重试。
        b_retries = 0
        # LLM 生成失败重试（2026-10-30）：与 B/投影/空结果预算独立，最多一次。
        llm_retries = 0
        # payload 版本指纹（Task 4）：每次成功生成后从结果里取出（只送观测 scene）。
        # 循环外初始化：llm_failed 那一档拿不到当次指纹（生成没成），键不落。
        payload_version = ""
        # A3 执行工具的每问统计（跨尝试共享，只进观测 scene；开关关时恒 0）。
        exec_stats = {"calls": 0, "rows": 0, "errors": 0}
        value_stats = {"calls": 0, "hits": 0}
        candidate_stats = {"count": 1, "choice": 1}
        max_attempts = (_RETRY_MAX + 1
                        + (1 if config.NL2SQL_PROJECTION_CONTRACT else 0)
                        + (1 if config.NL2SQL_EMPTY_REPAIR else 0)
                        + (1 if config.NL2SQL_LLM_RETRY else 0)
                        + (1 if config.NL2SQL_UNIT_TESTER else 0))
        for attempt in range(max_attempts):
            value_stats = {"calls": 0, "hits": 0}
            if recorder is not None:
                # 每次生成前标一次尝试号（1-based）：B 重试的第二次记 attempt=2（设计 §4.2）。
                # 循环内 `continue` 会回到这里，attempt 已递增。
                recorder.set_attempt(attempt + 1)
            try:
                if config.NL2SQL_MULTITURN:
                    # 多轮档（P2-2）：开局给列清单（含样本）+ 锚点实体的口径约束 + 1 跳 join 图
                    # （E1，2026-10-07）——「模型不知道自己缺」型缺口（口径 / join 同族）按需取
                    # 兜不住，全部铺到眼前；2 跳及以上由路径注入给出（`NL2SQL_JOIN_PATHS` 默认开、
                    # min_hops=2，仅当真多跳路径存在时注入），工具兜底。ctx 的读料是**该源那一行
                    # 的原始四列** —— `_entity_columns` 的返回值是已经挑好的那一份（挑给了单次档
                    # 的 payload），而工具与 E1 注入要的是原料（`eligible_joins` / `filter_constraints`
                    # / `evidence_key` 这些判据住在工具里）。读的是同一行。
                    row = await self._compiled_row(tenant_id, kb_id, ds_id)
                    # 演化台 `tool_schema`（D3）：按 KB 的 tools 覆盖块调工具清单。
                    # 读口与 `_load_concepts` **同一个**（`schema_read.load_abstract_row`，
                    # 缓存命中零会话）；非 active / 无 tools → 两者都 None，请求不变。
                    tool_overrides, disabled_tools = _abstract_tool_overrides(
                        await schema_read.load_abstract_row(tenant_id, kb_id))
                    # A3 执行工具：白名单/治理/凭据与最终答案同源；开关关时**不构造**
                    # （`ctx.execute_sql=None` ⇒ 多轮档不注册工具，请求逐字节不变）。
                    lineage_edges = (
                        preloaded_lineage
                        if (preloaded_lineage is not None
                            and config.NL2SQL_JOIN_PATHS)
                        else (await _load_lineage_edges(tenant_id, kb_id, ds_id)
                              if config.NL2SQL_JOIN_PATHS else []))
                    exec_tool = None
                    if config.NL2SQL_EXEC_TOOL:
                        known_tables = {str(e.get("source_object_id") or "").strip()
                                        for e in (row["entity_types"] or [])
                                        if isinstance(e, dict)}
                        known_tables.discard("")
                        exec_tool = _build_exec_tool(
                            source=source, cfg=cfg, engine=engine,
                            allowed_tables=_exec_allowed_tables(
                                anchor_entity=ent, anchor_table=source_object_id or ent,
                                related_tables=join_plan.related_tables,
                                known_tables=known_tables,
                                lineage_edges=lineage_edges,
                                join_edges=all_join_edges(
                                    row["relation_types"], row["entity_types"],
                                    forbidden_pairs=forbidden_pairs),
                                max_hops=config.NL2SQL_JOIN_PATHS_MAX_HOPS),
                            forbidden_pairs=forbidden_pairs,
                            gov=gov, gov_active=gov_active, stats=exec_stats)
                    generated = await asyncio.wait_for(
                        multiturn.generate_sql_multiturn(
                            q_retrieval,
                            ctx=multiturn.MultiturnContext(
                                anchor=ent, entity_types=row["entity_types"],
                                relation_types=row["relation_types"], evidence=row["evidence"],
                                constraints=row["constraints"], recorder=recorder,
                                # 目标提示：问题词面命中过的候选实体（`_pick` 之后本会被丢掉）。
                                # ⚠️ **只当提示、不作约束** —— 见 spec §4.1：
                                # 候选集可能很大，当约束会过度收窄。
                                # ⚠️⚠️ **必须转成物理表名**（2026-10-16 Task 4 实核）：
                                # 边池与 `search_paths` 的节点全是**物理表名**（`source_object_id`），
                                # 而候选给的是**实体名** —— 直接填实体名会让目标加成**永不命中**
                                # 且**静默无感**（不报错、只是白填）。
                                path_targets=_target_tables(candidates, row["entity_types"], ds_id),
                                # 血缘边：运行时来源 = 表（迁移 010，管理面写入）。
                                # ⚠️ 开关关时**一次库都不查**（保持「关档零开销」）。
                                lineage_edges=lineage_edges,
                                # 禁止同查：注入剔边 + 路径丢弃 + payload 提示都由 multiturn 内部消费。
                                forbidden_pairs=forbidden_pairs,
                                execute_sql=exec_tool),
                            # `columns` 传**同一份**（samples 已由 evidence 那步按需合并进去）：
                            # 列清单与口径 / join 图同为该源那一行的直传语义（见上方注释）。
                            engine=engine, table_name=source_object_id or ent, columns=_prompt_columns(columns),
                            max_rounds=config.NL2SQL_MULTITURN_MAX_ROUNDS,
                            max_tool_calls_per_round=config.NL2SQL_MULTITURN_MAX_TOOL_CALLS_PER_ROUND,
                            tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                            retry_note=retry_note,
                            concept_fields=concept_fields or None,
                            projection_contract=config.NL2SQL_PROJECTION_CONTRACT,
                            hints=hint_texts or None,
                            # 值同义词（P1）：多轮首发同样按需注入（零命中/开关关 ⇒ None）。
                            value_aliases=aliases or None,
                            plan_text=plan_text or None,
                            schema_preview=schema_preview or None,
                            preview_details=preview_details,
                            rank_hint=rank_hint,
                            per_group_avg=per_group_avg,
                            exact_fields=exact_fields,
                            hint_exactness=hint_exactness,
                            percentage_base=percentage_base,
                            cte_hint=cte_hint,
                            nullif_hint=nullif_hint,
                            single_column=single_column,
                            date_cast=date_cast,
                            superlative_limit=superlative_limit,
                            null_order=null_order,
                            cte_join_hint=cte_join_hint,
                            grain_hint=grain_hint,
                            left_join_hint=left_join_hint,
                            dimension_joins=dimension_joins or None,
                            cte_joins=cte_joins or None,
                            answer_format=answer_format or None,
                            few_shots=few_shots or None,
                            today=today_text,
                            tool_overrides=tool_overrides or None,
                            disabled_tools=disabled_tools or None),
                        timeout=float(config.NL2SQL_MULTITURN_TIMEOUT),
                    )
                    # 多轮才有的两键进审计（`meta` 由 `generate_sql_multiturn` 回带）；
                    # 单次档 `llm_extra` 恒为 None ⇒ 审计 payload 一字不动。
                    meta = generated.get("meta") or {}
                    llm_extra = {"rounds": meta.get("rounds"), "tool_calls": meta.get("tool_calls")}
                else:
                    generated = await asyncio.wait_for(
                        generate_sql(
                            q_retrieval, engine=engine,
                            # ⚠️ 喂给模型的「表」是**物理表标识**，不是实体编码：① 的
                            # `Company` 是概念层的名字，真源上没有这张表。`source_object_id`
                            # 是限定名（`public.t_asset`）—— 只给表名修不掉
                            # 「表不在默认 schema」那一半。**null 回退实体名**：为兼容存量
                            # 编译 schema 行，回退后行为与修复前完全一致、不更坏。
                            # 实测 2026-09-30：四个源的编译 schema 已全部带满 `source_object_id`，
                            # 所以回退这一档现在只是保险 —— 它真被触发时值得看一眼是不是重生成漏了。
                            table_name=source_object_id or ent, columns=_prompt_columns(columns),
                            # join 图（P0-2）：同源 1 跳、已校验可用；空时 payload 与单表版一致。
                            joins=join_plan.joins,
                            related_tables=join_plan.related_tables,
                            joins_truncated=join_plan.truncated,
                            constraints=constraints,
                            concept_fields=concept_fields or None,
                            # 值同义词（P1）：零命中 / 开关关 ⇒ None（payload 键不出现）。
                            value_aliases=aliases or None,
                            projection_contract=config.NL2SQL_PROJECTION_CONTRACT,
                            hints=hint_texts or None, plan=plan_text or None,
                            schema_preview=schema_preview or None,
                            preview_details=preview_details,
                            rank_hint=rank_hint,
                            per_group_avg=per_group_avg,
                            exact_fields=exact_fields,
                            hint_exactness=hint_exactness,
                            percentage_base=percentage_base,
                            cte_hint=cte_hint,
                            nullif_hint=nullif_hint,
                            single_column=single_column,
                            date_cast=date_cast,
                            superlative_limit=superlative_limit,
                            null_order=null_order,
                            cte_join_hint=cte_join_hint,
                            grain_hint=grain_hint,
                            left_join_hint=left_join_hint,
                            dimension_joins=dimension_joins or None,
                            cte_joins=cte_joins or None,
                            answer_format=answer_format or None,
                            few_shots=few_shots or None,
                            forbidden_note=prompt_note(
                                forbidden_pairs,
                                {source_object_id}
                                | {str(t.get("table") or "")
                                   for t in (join_plan.related_tables or [])}),
                            tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                            retry_note=retry_note, today=today_text,
                        ),
                        timeout=float(config.NL2SQL_LLM_TIMEOUT),
                    )
            except Exception as exc:  # noqa: BLE001 —— **故意宽**，见下
                if empty_fallback is not None:
                    logger.warning("NL→SQL 空结果重写生成失败，回退原始空结果: kb=%s ds=%s err=%s",
                                   kb_id, ds_id, exc)
                    generated = empty_fallback["generated"]
                    exec_sql = empty_fallback["sql"]
                    result = empty_fallback["result"]
                    elapsed_ms = empty_fallback["elapsed_ms"]
                    payload_version = empty_fallback["payload_version"]
                    llm_extra = empty_fallback["llm_extra"]
                    empty_fallback = None
                    break
                # 模型这条路坏了就算这个源这次答不了，**不逃逸**：本层是 ③ 扇出里的
                # **一个子查询**，一个源的模型抖动不该把整个提问炸成泛化 500（spec §五 也要求
                # 本层不逃非 `JonexException`）；也不抛 `UpstreamServiceError`（那等于替 ③
                # 决定「整次提问失败」）。与「答不了」同形 → ③ 分得出「这个源没答上来」
                # 并且能继续问别的源。
                #
                # ⚠️ **故意宽**（不按异常类型挑）：坏答案那几档确实是 `RuntimeError`（空正文 /
                # 非 JSON / 被 `max_tokens` 截断），但 `generate_sql` 里还有 openai SDK 的
                # **传输层**错误 —— `APIConnectionError` / `APITimeoutError` 都不是
                # `RuntimeError`，llm-gateway 抖一下就会走到那条路上。这里要的是「这个源这次
                # 答不了」而不是让传输层错误逃成 500。**不静默**：有 warning、有原文。
                # （`asyncio.CancelledError` 是 `BaseException`，不受这里影响，照旧上抛。）
                logger.warning("NL→SQL 生成失败: kb=%s ds=%s err=%s", kb_id, ds_id, exc)
                if projection_fallback is not None:
                    # 投影重写那轮崩了：回退到第一次生成（它已过 guard/治理的**前置**面，
                    # 只是投影形状不理想）。不把「有答案」降级成 llm_failed。
                    logger.warning("NL→SQL 投影重写失败，回退首发 SQL: kb=%s ds=%s", kb_id, ds_id)
                    generated = projection_fallback
                    payload_version = projection_fallback_payload
                    projection_code = projection_fallback_code
                    projection_fallback = None
                    llm_extra = projection_fallback_meta
                else:
                    # LLM 生成失败重试（2026-10-30）：传输层抖动/偶发解析失败同参再试一次。
                    # 只重试一次；投影回退那一档已有可用 SQL，不在此重试。
                    if config.NL2SQL_LLM_RETRY and llm_retries < 1:
                        llm_retries += 1
                        logger.warning(
                            "NL→SQL 生成失败，同参重试一次: kb=%s ds=%s err=%s",
                            kb_id, ds_id, exc)
                        continue
                    # llm_failed 一律不带 meta：第二次生成没成、取不到**当次** meta；第一次的
                    # meta 属于已被执行失败作废的那次生成，挂到这里会误导归因（spec §3.2 审计表）。
                    return fail(tenant_id, user_id, username, ip, q, ds_id, "llm_failed",
                            f"{type(exc).__name__}: {str(exc)[:200]}",
                            audit_extra=_audit_extra(None, retried, first_error),
                            # 异常档**无 rounds**（设计 §4.2：取不到当次 meta）——
                            # 但 recorder 里的轨迹照带（观测相对审计的增值点）。
                            scene=observe.build_scene(
                                evolution_run=evolution_run_id,
                                value_alias_rewrite=alias_rewrites or None,
                                multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta, anchor=ent,
                                recorder=recorder, retried=retried,
                                first_error=first_error or "",
                                concept_fields=len(concept_fields),
                                concept_columns=n_concept_cols,
                                mapping_slots=mapping_slots,
                                exec_stats=exec_stats, empty_retried=empty_retried, value_stats=value_stats,
                                plan_stats=plan_stats, preview_stats=preview_stats,
                                format_stats=format_stats,
                                candidate_stats=candidate_stats),
                            kb_id=kb_id, entity_name=ent,
                            record_evidence=record_evidence)
            if hints_meta:
                llm_extra = {**(llm_extra or {}),
                             "hints_count": hints_meta["count"],
                             "hints_chars": hints_meta["chars"]}
            # payload 版本指纹（Task 4）：生成结果里的指纹**只进观测** —— pop 掉，
            # 确保它不会随 `generated` 的任何后续使用漏进 HTTP 响应（响应形状在下面
            # 显式构造，这里再摘一道；空 = 桩/旧路径没回带，scene 不落键）。
            payload_version = str(generated.pop("payload_version", "") or "")
            # S3 候选 + 成对选择（2026-10-30）：难例多生成一条单次档候选，再选择。
            # 只在首发、无重试反馈时触发；任何失败都保留 baseline（不新增失败出口）。
            candidate_stats = {"count": 1, "choice": 1}
            baseline_ok = (bool(generated.get("sql"))
                           and not generated.get("not_answerable"))
            if (config.NL2SQL_RESULT_VOTE and attempt == 0
                    and retry_note is None and empty_fallback is None
                    and projection_fallback is None
                    and (not baseline_ok or should_generate_candidate(
                        q, join_plan.joins, enabled=2,
                        narrow=config.NL2SQL_CANDIDATE_NARROW))):

                async def _vote_gen(note: str):
                    return await asyncio.wait_for(
                        generate_sql(
                            q, engine=engine, table_name=source_object_id or ent,
                            columns=_prompt_columns(columns),
                            joins=join_plan.joins,
                            related_tables=join_plan.related_tables,
                            joins_truncated=join_plan.truncated,
                            constraints=constraints,
                            concept_fields=concept_fields or None,
                            projection_contract=config.NL2SQL_PROJECTION_CONTRACT,
                            hints=hint_texts or None, plan=plan_text or None,
                            schema_preview=schema_preview or None,
                            preview_details=preview_details,
                            rank_hint=rank_hint, per_group_avg=per_group_avg,
                            exact_fields=exact_fields, hint_exactness=hint_exactness,
                            percentage_base=percentage_base, cte_hint=cte_hint,
                            nullif_hint=nullif_hint, single_column=single_column,
                            date_cast=date_cast, superlative_limit=superlative_limit,
                            null_order=null_order,
                            cte_join_hint=cte_join_hint,
                            grain_hint=grain_hint,
                            left_join_hint=left_join_hint,
                            dimension_joins=dimension_joins or None,
                            cte_joins=cte_joins or None,
                            answer_format=answer_format or None,
                            few_shots=few_shots or None,
                            forbidden_note=prompt_note(
                                forbidden_pairs,
                                {source_object_id}
                                | {str(t.get("table") or "")
                                   for t in (join_plan.related_tables or [])}),
                            tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                            retry_note=note, today=today_text),
                        timeout=float(config.NL2SQL_LLM_TIMEOUT),
                    )

                alts = []
                for _i in range(1, max(2, int(config.NL2SQL_VOTE_CANDIDATES))):
                    try:
                        alt = await _vote_gen(
                            "Write an independent alternative SQL for the same "
                            "question. Use a different structure from the previous "
                            "answers (for example a CTE, a subquery, or a different "
                            "join order).")
                    except Exception as exc:  # noqa: BLE001 —— 候选是增能
                        logger.warning("投票候选生成失败（跳过）: %s", exc)
                        break
                    if alt and alt.get("sql") and not alt.get("not_answerable"):
                        alts.append(alt)
                if alts:
                    cands = [generated] + alts
                    known = {str(e.get("source_object_id") or "").strip()
                             for e in ((row["entity_types"]
                                        if config.NL2SQL_MULTITURN else []) or [])
                             if isinstance(e, dict)}
                    known.discard("")
                    path_t = {str(t).strip()
                              for t in ((generated.get("meta") or {}).get("path_tables") or ())}
                    path_t.discard("")
                    vote_allowed = ({source_object_id or ent, ent}
                                    | {str(t.get("table") or "")
                                       for t in (join_plan.related_tables or [])}
                                    | (path_t & known)
                                    | preview_tables)
                    vote_stats = {"calls": 0, "rows": 0, "errors": 0}
                    executor = _build_exec_tool(
                        source=source, cfg=cfg, engine=engine,
                        allowed_tables=vote_allowed,
                        forbidden_pairs=forbidden_pairs,
                        gov=gov, gov_active=gov_active, stats=vote_stats,
                        max_rows=max(1, min(2000,
                                            int(config.NL2SQL_MAX_ROWS or 200))))
                    outcomes = []
                    ok_count = 0
                    for cand in cands:
                        res = await executor(cand.get("sql") or "")
                        if "rows" in res:
                            ok_count += 1
                            outcomes.append(res.get("rows"))
                        else:
                            outcomes.append(None)
                    idx = vote_index(outcomes)
                    candidate_stats = {"count": len(cands), "choice": idx + 1,
                                       "vote": idx + 1, "vote_ok": ok_count}
                    if (idx != 0 or not baseline_ok) and cands[idx].get("sql"):
                        chosen = cands[idx]
                        generated = {"sql": chosen["sql"]}
                        payload_version = str(chosen.pop("payload_version", "") or "")
                        llm_extra = None
            baseline_ok = bool(generated.get("sql")) and not generated.get("not_answerable")
            if (not config.NL2SQL_RESULT_VOTE
                    and config.NL2SQL_CANDIDATES >= 2 and attempt == 0
                    and retry_note is None and empty_fallback is None
                    and projection_fallback is None
                    and (not baseline_ok or should_generate_candidate(
                        q_retrieval, join_plan.joins, enabled=config.NL2SQL_CANDIDATES,
                        narrow=config.NL2SQL_CANDIDATE_NARROW))):
                alt_note = (
                    "Write an independent alternative SQL for the same question. "
                    "Use a different structure from your first answer (for example "
                    "a CTE or a subquery); do not repeat the same query.")
                try:
                    alt = await asyncio.wait_for(
                        generate_sql(
                            q_retrieval, engine=engine, table_name=source_object_id or ent,
                            columns=_prompt_columns(columns),
                            joins=join_plan.joins,
                            related_tables=join_plan.related_tables,
                            joins_truncated=join_plan.truncated,
                            constraints=constraints,
                            concept_fields=concept_fields or None,
                            # 值同义词（P1）：零命中 / 开关关 ⇒ None（payload 键不出现）。
                            value_aliases=aliases or None,
                            projection_contract=config.NL2SQL_PROJECTION_CONTRACT,
                            hints=hint_texts or None, plan=plan_text or None,
                            schema_preview=schema_preview or None,
                            preview_details=preview_details,
                            rank_hint=rank_hint, per_group_avg=per_group_avg,
                            exact_fields=exact_fields, hint_exactness=hint_exactness,
                            percentage_base=percentage_base, cte_hint=cte_hint,
                            nullif_hint=nullif_hint, single_column=single_column,
                            date_cast=date_cast, superlative_limit=superlative_limit,
                            null_order=null_order,
                            cte_join_hint=cte_join_hint,
                            grain_hint=grain_hint,
                            left_join_hint=left_join_hint,
                            dimension_joins=dimension_joins or None,
                            cte_joins=cte_joins or None,
                            answer_format=answer_format or None,
                            few_shots=few_shots or None,
                            forbidden_note=prompt_note(
                                forbidden_pairs,
                                {source_object_id}
                                | {str(t.get("table") or "")
                                   for t in (join_plan.related_tables or [])}),
                            tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                            retry_note=alt_note, today=today_text),
                        timeout=float(config.NL2SQL_LLM_TIMEOUT),
                    )
                except Exception as exc:  # noqa: BLE001 —— 候选是增能，失败保 baseline
                    logger.warning("第二候选生成失败（保留 baseline）: %s", exc)
                    alt = None
                if alt and alt.get("sql") and not alt.get("not_answerable"):
                    candidate_stats["count"] = 2
                    if not baseline_ok:
                        candidate_stats["choice"] = 2
                        generated = {"sql": alt["sql"]}
                        payload_version = str(alt.pop("payload_version", "") or "")
                        llm_extra = None
                    else:
                        picked = await select_candidate(
                            q_retrieval, [{"sql": generated["sql"]}, {"sql": alt["sql"]}],
                            tenant_id=tenant_id, kb_id=kb_id, user_id=user_id or "")
                        candidate_stats["choice"] = int(picked.get("index", 0)) + 1
                        if candidate_stats["choice"] == 2:
                            generated = {"sql": alt["sql"]}
                            payload_version = str(alt.pop("payload_version", "") or "")
                            llm_extra = None
            if generated.get("not_answerable") and projection_fallback is not None:
                logger.info("NL→SQL 投影重写回 not_answerable，回退首发 SQL: kb=%s ds=%s",
                            kb_id, ds_id)
                generated = projection_fallback
                payload_version = projection_fallback_payload
                projection_code = projection_fallback_code
                projection_fallback = None
                llm_extra = projection_fallback_meta
            if generated.get("not_answerable"):
                return fail(tenant_id, user_id, username, ip, q, ds_id, "not_answerable",
                            generated.get("reason") or "用该实体的列答不了",
                            audit_extra=_audit_extra(llm_extra, retried, first_error),
                            scene=observe.build_scene(
                                evolution_run=evolution_run_id,
                                value_alias_rewrite=alias_rewrites or None,
                                multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta, anchor=ent,
                                recorder=recorder, retried=retried,
                                first_error=first_error or "",
                                rounds=(llm_extra or {}).get("rounds"),
                                concept_fields=len(concept_fields),
                                concept_columns=n_concept_cols,
                                mapping_slots=mapping_slots,
                                payload_version=payload_version,
                                projection_violation=projection_code,
                                projection_retried=projection_retried,
                                exec_stats=exec_stats, empty_retried=empty_retried, value_stats=value_stats,
                                plan_stats=plan_stats, preview_stats=preview_stats,
                                format_stats=format_stats,
                                candidate_stats=candidate_stats),
                            kb_id=kb_id, entity_name=ent,
                            record_evidence=record_evidence)

            # 给 guard 一份「这次允许碰哪些表」的清单：锚点表 + 实体名 + 这次实际注入给模型的
            # 那几张 join 表（1 跳），**再加多跳路径上的表**。
            # ⚠️ **路径表必须再与编译 schema 求交**（Task 6C，2026-10-16）：血缘边现在来自
            # **表**（服务层写入时已回校编译 schema），但表行可能在 schema 重生成后**过期**
            # （或将来由 file / 平台摄取写入），直接并进去等于让陈旧/旁路来源**任意扩宽安全闸**。
            # 取交后，白名单仍只含**平台认识的表**；不在 schema 里的表 ② 本来也没有列信息、
            # 写不出可执行的 SQL，排掉它不损失能力。
            #
            # ⚠️ `row`（编译 schema 那一行）**只在多轮分支赋值**（单次档不拉它、不读
            # `entity_types`）—— 而 `path_tables` 也**只在多轮档才有**（单次档 `generate_sql`
            # 不产 `meta`）⇒ 单次档下这一项恒空、`row` 恒不被碰（条件表达式短路，不会
            # NameError）。brief 的原文在单次档会 `row` 未定义直接炸，此处是必要修正。
            known_tables = {str(e.get("source_object_id") or "").strip()
                            for e in ((row["entity_types"] if config.NL2SQL_MULTITURN else []) or [])
                            if isinstance(e, dict)}
            known_tables.discard("")
            path_tables = {str(t).strip()
                           for t in ((generated.get("meta") or {}).get("path_tables") or ())}
            path_tables.discard("")
            allowed_tables = ({source_object_id or ent, ent}
                              | {t["table"] for t in (join_plan.related_tables or [])}
                              | (path_tables & known_tables)
                              | preview_tables)
            # 先做方言函数名归一（SQLite 的 logical_and → Postgres bool_and），
            # 再进安全闸：安全闸只管读写边界，不负责跨方言函数名。
            generated_sql = normalize_dialect_sql(generated["sql"], engine)
            value_stats = {"calls": 0, "hits": 0}
            if config.NL2SQL_VALUE_LOOKUP:
                candidate_sql, value_stats = await self._ground_values(
                    generated_sql, source=source, cfg=cfg, engine=engine,
                    allowed_tables=allowed_tables)
                if candidate_sql != generated_sql:
                    # 确定性改写也可能让 SQL 变形；先过一次安全闸，不通过就用原文。
                    probe = guard_sql(candidate_sql, engine=engine,
                                      max_rows=config.NL2SQL_MAX_ROWS,
                                      allowed_tables=allowed_tables,
                                      forbidden_pairs=forbidden_pairs)
                    if probe.ok:
                        generated_sql = candidate_sql
            guarded = guard_sql(generated_sql, engine=engine, max_rows=config.NL2SQL_MAX_ROWS,
                                allowed_tables=allowed_tables,
                                forbidden_pairs=forbidden_pairs,
                                offset=offset)
            if not guarded.ok:
                if empty_fallback is not None:
                    logger.warning("NL→SQL 空结果重写被安全闸拒绝，回退原始空结果: kb=%s ds=%s",
                                   kb_id, ds_id)
                    generated = empty_fallback["generated"]
                    exec_sql = empty_fallback["sql"]
                    result = empty_fallback["result"]
                    elapsed_ms = empty_fallback["elapsed_ms"]
                    payload_version = empty_fallback["payload_version"]
                    llm_extra = empty_fallback["llm_extra"]
                    empty_fallback = None
                    break
                # ⚠️ 这是本特性里**安全权重最高**的事件（模型试图对生产库写一条 DELETE 被拦下），
                # 而「拦下」本身不是留痕：审计是出网的 fire-and-forget（单测里被换掉、生产里
                # 平台侧没起来也会丢），所以**本地日志与审计两条都要有**，一条兜另一条。
                # 少了这里，服务端既不知道拦过什么、也不知道拦过几次 —— 「某个源上模型正在
                # 反复尝试写操作」这种信号只有这两条路看得见。
                logger.warning("NL→SQL 生成结果被安全闸拒绝: kb=%s ds=%s engine=%s reason=%s sql=%s",
                               kb_id, ds_id, engine, guarded.reason, guarded.sql[:500])
                rejection_kind = guarded.kind or "guard"
                return fail(tenant_id, user_id, username, ip, q, ds_id,
                            "rejected_unsafe_sql", guarded.reason,
                            rejected_sql=guarded.sql,
                            rejection_kind=rejection_kind,
                            audit_extra=_audit_extra(llm_extra, retried, first_error),
                            scene=observe.build_scene(
                                evolution_run=evolution_run_id,
                                value_alias_rewrite=alias_rewrites or None,
                                multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta, anchor=ent,
                                recorder=recorder, retried=retried,
                                first_error=first_error or "",
                                rounds=(llm_extra or {}).get("rounds"),
                                guard_reason=guarded.reason,
                                rejection_kind=rejection_kind,
                                concept_fields=len(concept_fields),
                                concept_columns=n_concept_cols,
                                mapping_slots=mapping_slots,
                                payload_version=payload_version,
                                projection_violation=projection_code,
                                projection_retried=projection_retried,
                                exec_stats=exec_stats, empty_retried=empty_retried, value_stats=value_stats,
                                plan_stats=plan_stats, preview_stats=preview_stats,
                                format_stats=format_stats,
                                candidate_stats=candidate_stats),
                            kb_id=kb_id, entity_name=ent,
                            record_evidence=record_evidence)

            # 投影契约（A1）：标量问句多列 / SELECT * → 一次重写（不自动删列）。
            # ⚠️ 放在 guard **之后**（设计 §2.1）：先确认这条 SQL 本身可执行，再谈投影形状；
            # 否则一条会被安全闸拒的 SQL 会先触发重写、把 `rejected_unsafe_sql` 变成别的出口。
            if config.NL2SQL_PROJECTION_CONTRACT and not projection_retried:
                violation = check_projection(generated_sql, q_retrieval)
                if violation is not None:
                    projection_code = violation.code
                    projection_retried = True
                    # 保存首发，给重写失败留退路（审查 #4）。
                    projection_fallback = dict(generated)
                    projection_fallback_meta = llm_extra
                    projection_fallback_payload = payload_version
                    projection_fallback_code = projection_code
                    retry_note = _build_projection_note(violation)
                    logger.info(
                        "NL→SQL 投影违规，重写一次: kb=%s ds=%s code=%s detail=%s",
                        kb_id, ds_id, violation.code, violation.detail)
                    continue

            exec_sql = guarded.sql
            if gov_active:
                from ..governance.sql_rewrite import apply_governance
                gov_result = apply_governance(
                    guarded.sql, engine=engine,
                    decide_fn=lambda t, c, a: _governance_decide(gov, t, c, a),
                    schema=gov.get("schema") or {})
                if not gov_result.ok:
                    if empty_fallback is not None:
                        logger.warning("NL→SQL 空结果重写被治理拒绝，回退原始空结果: kb=%s ds=%s",
                                       kb_id, ds_id)
                        generated = empty_fallback["generated"]
                        exec_sql = empty_fallback["sql"]
                        result = empty_fallback["result"]
                        elapsed_ms = empty_fallback["elapsed_ms"]
                        payload_version = empty_fallback["payload_version"]
                        llm_extra = empty_fallback["llm_extra"]
                        empty_fallback = None
                        break
                    logger.warning(
                        "NL→SQL 生成结果被治理策略拒绝: kb=%s ds=%s reason=%s sql=%s",
                        kb_id, ds_id, gov_result.reason, guarded.sql[:500])
                    rejection_kind = gov_result.kind or "deny"
                    return fail(
                        tenant_id, user_id, username, ip, q, ds_id,
                        "rejected_sensitive", gov_result.reason,
                        rejected_sql=guarded.sql,
                        rejection_kind=rejection_kind,
                        audit_extra=_audit_extra(llm_extra, retried, first_error),
                        scene=observe.build_scene(
                            evolution_run=evolution_run_id,
                            value_alias_rewrite=alias_rewrites or None,
                            multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta, anchor=ent,
                            recorder=recorder, retried=retried,
                            first_error=first_error or "",
                            rounds=(llm_extra or {}).get("rounds"),
                            guard_reason=gov_result.reason,
                            rejection_kind=rejection_kind,
                            concept_fields=len(concept_fields),
                            concept_columns=n_concept_cols,
                            mapping_slots=mapping_slots,
                            payload_version=payload_version,
                                projection_violation=projection_code,
                                projection_retried=projection_retried,
                                exec_stats=exec_stats, empty_retried=empty_retried, value_stats=value_stats,
                                plan_stats=plan_stats, preview_stats=preview_stats,
                                format_stats=format_stats,
                                candidate_stats=candidate_stats),
                        kb_id=kb_id, entity_name=ent,
                        record_evidence=record_evidence)
                exec_sql = gov_result.sql
                # 合成流量不写治理审计：与 `data_source.ask` 同属「这次提问留痕」，
                # 基准反复跑同一批题会把它刷成噪声（治理判定本身仍完整生效）。
                if not synthetic:
                    await _record_governance_audit(
                        tenant_id, kb_id, ds_id, user_id, gov_result.decisions,
                        guarded.sql)

            started = time.monotonic()
            try:
                result = await run_readonly(source, cfg, exec_sql)
            except Exception as exc:  # noqa: BLE001 —— 外部库的失败要归类，不是崩服务
                if empty_fallback is not None:
                    logger.warning("NL→SQL 空结果重写执行失败，回退原始空结果: ds=%s err=%s",
                                   ds_id, exc)
                    generated = empty_fallback["generated"]
                    exec_sql = empty_fallback["sql"]
                    result = empty_fallback["result"]
                    elapsed_ms = empty_fallback["elapsed_ms"]
                    payload_version = empty_fallback["payload_version"]
                    llm_extra = empty_fallback["llm_extra"]
                    empty_fallback = None
                    break
                err = f"{type(exc).__name__}: {str(exc)[:200]}"
                # ⚠️ 归类**不要按异常类型**：asyncpg 的建连超时本身就是 `asyncio.TimeoutError`，
                # 与 pymssql/aiomysql 抛的驱动错误不同名（Task 7 的评审提的）。这里一律
                # `exec_failed` + 原文，由人看日志定位 —— 按类型猜只会猜错。
                logger.warning("NL→SQL 执行失败: ds=%s engine=%s err=%s", ds_id, engine, exc)
                if b_retries < _RETRY_MAX and is_fixable_exec_error(exc):
                    # B（锚错兜底）：名字/语法类错误的正确名字在模型已得的上下文里
                    # （列清单 / joins 图 / 工具），把执行错误原样反馈回去再给它一次机会。
                    retried, first_error = True, err
                    b_retries += 1
                    retry_note = _build_retry_note(
                        exec_sql, err, with_tools=bool(config.NL2SQL_MULTITURN))
                    logger.warning("NL→SQL 执行失败(可修复)，将重试一次: ds=%s err=%s", ds_id, err)
                    continue
                return fail(tenant_id, user_id, username, ip, q, ds_id, "exec_failed",
                            err, sql=exec_sql,
                            audit_extra=_audit_extra(llm_extra, retried, first_error),
                            scene=observe.build_scene(
                                evolution_run=evolution_run_id,
                                value_alias_rewrite=alias_rewrites or None,
                                multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta, anchor=ent,
                                recorder=recorder, retried=retried,
                                first_error=first_error or "",
                                rounds=(llm_extra or {}).get("rounds"),
                                concept_fields=len(concept_fields),
                                concept_columns=n_concept_cols,
                                mapping_slots=mapping_slots,
                                payload_version=payload_version,
                                projection_violation=projection_code,
                                projection_retried=projection_retried,
                                exec_stats=exec_stats, empty_retried=empty_retried, value_stats=value_stats,
                                plan_stats=plan_stats, preview_stats=preview_stats,
                                format_stats=format_stats,
                                candidate_stats=candidate_stats),
                            kb_id=kb_id, entity_name=ent,
                            record_evidence=record_evidence)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # 百分比单位接地（2026-11-01）：只对 0 行 + 问句含 % 的题试一次缩放。
            # 候选必须重新过安全闸；任何失败都保留原始 0 行结果。
            if (config.NL2SQL_UNIT_GROUNDING
                    and int(result.get("row_count") or 0) == 0):
                for factor, candidate in scale_candidates(exec_sql, q):
                    probe = guard_sql(candidate, engine=engine,
                                      max_rows=config.NL2SQL_MAX_ROWS,
                                      allowed_tables=allowed_tables,
                                      forbidden_pairs=forbidden_pairs)
                    if not probe.ok:
                        continue
                    try:
                        scaled_at = time.monotonic()
                        scaled = await run_readonly(source, cfg, probe.sql)
                    except Exception as exc:  # noqa: BLE001 —— 增能项，失败保留原结果
                        logger.info("NL→SQL 单位缩放执行失败（保留原结果）: %s", exc)
                        continue
                    if int(scaled.get("row_count") or 0) > 0:
                        exec_sql = probe.sql
                        result = scaled
                        elapsed_ms += int((time.monotonic() - scaled_at) * 1000)
                        logger.info("NL→SQL 百分比单位缩放命中: factor=%s sql=%s",
                                    factor, exec_sql[:300])
                        break
            # CHESS 式可执行断言（2026-11-01）：结果级断言失败 → 带反馈重写一次。
            # 断言生成不看候选结果，判定看真实结果；任何一步失败都 fail-open。
            if (config.NL2SQL_UNIT_TESTER and not unit_retried
                    and unit_retries < 1 and attempt + 1 < max_attempts):
                assertions = await generate_assertions(
                    q, columns, tenant_id=tenant_id, kb_id=kb_id,
                    user_id=user_id or "")
                logger.info("NL→SQL 单元断言生成: ds=%s n=%s", ds_id, len(assertions))
                if assertions:
                    evaluation = await evaluate_assertions(
                        q, assertions, exec_sql, result.get("rows") or [],
                        columns, tenant_id=tenant_id, kb_id=kb_id,
                        user_id=user_id or "")
                    failed = failed_assertions(assertions, evaluation)
                    logger.info("NL→SQL 单元断言判定: ds=%s failed=%s", ds_id, len(failed))
                    if failed:
                        unit_retried = True
                        unit_retries += 1
                        retry_note = build_repair_note(failed)
                        logger.info(
                            "NL→SQL 单元断言失败，带反馈重写一次: ds=%s failed=%s",
                            ds_id, len(failed))
                        continue
            # 空结果修复（P0-2）：生成后强制反馈一次，不是模型主动调用的工具。
            # 合法空结果存在，所以只在开关打开时做，且最多一次；第二次仍空就接受。
            if (config.NL2SQL_EMPTY_REPAIR and not empty_retried
                    and empty_retries < 1 and attempt + 1 < max_attempts
                    and int(result.get("row_count") or 0) == 0):
                empty_retried = True
                empty_retries += 1
                empty_fallback = {"generated": dict(generated), "sql": exec_sql,
                                  "result": result, "elapsed_ms": elapsed_ms,
                                  "payload_version": payload_version, "llm_extra": llm_extra}
                retry_note = _build_empty_retry_note(exec_sql, columns)
                logger.info("NL→SQL 空结果，带反馈重生成一次: ds=%s sql=%s",
                            ds_id, exec_sql[:300])
                continue
            break

        if record_evidence:
            # 合成流量（演化基准臂）：成功档同样不写 `data_source.ask` 审计；
            # 观测仍由 `record_evidence` 单独把关（诊断靠它区分基准样本）。
            if not synthetic:
                self._audit(tenant_id, user_id, username, ip, ds_id, q, exec_sql,
                            result["row_count"], elapsed_ms,
                            extra=_audit_extra(llm_extra, retried, first_error))
            # #9 成功档
            self._observe(tenant_id=tenant_id, kb_id=kb_id, data_source_id=ds_id,
                          question=q, outcome="success", entity_name=ent,
                          sql=exec_sql, row_count=result["row_count"],
                          elapsed_ms=elapsed_ms,
                          scene=observe.build_scene(
                              evolution_run=evolution_run_id,
                              value_alias_rewrite=alias_rewrites or None,
                              multiturn=config.NL2SQL_MULTITURN, hints_meta=hints_meta, anchor=ent,
                              recorder=recorder, retried=retried,
                              first_error=first_error or "",
                              rounds=(llm_extra or {}).get("rounds"),
                              concept_fields=len(concept_fields),
                              concept_columns=n_concept_cols,
                              mapping_slots=mapping_slots,
                              payload_version=payload_version,
                              projection_violation=projection_code,
                              projection_retried=projection_retried, exec_stats=exec_stats, empty_retried=empty_retried, value_stats=value_stats,
                                plan_stats=plan_stats, preview_stats=preview_stats,
                                format_stats=format_stats,
                                candidate_stats=candidate_stats))
        # W3.3 M4：成功档影子对比（只记录不替换；开关默认关）。
        self._schedule_metric_shadow(
            tenant_id=tenant_id, kb_id=kb_id, data_source_id=ds_id,
            question=q, entity_name=ent, main_outcome="success",
            main_row_count=result.get("row_count"),
            main_elapsed_ms=elapsed_ms,
            main_cell=metric_shadow.first_cell(result),
            username=username, evolution_run_id=evolution_run_id,
            record_evidence=record_evidence, observe_only=observe_only,
            synthetic=synthetic,
        )
        logger.info("NL→SQL 问答: ds=%s engine=%s rows=%d truncated=%s elapsed_ms=%d",
                    ds_id, engine, result["row_count"], guarded.limit_applied, elapsed_ms)
        response = {
            "ok": True,
            "not_answerable": False,
            "data_source_id": ds_id,
            "source_name": ds_names.get(ds_id, ""),
            "entity_name": ent,
            "sql": exec_sql,
            "columns": result["columns"],
            "rows": [[_jsonable(v) for v in row] for row in result["rows"]],
            "row_count": result["row_count"],
            "truncated": guarded.limit_applied,
            "offset": offset,
            "next_offset": (
                offset + len(result["rows"])
                if guarded.limit_applied else None),
        }
        return attach_blocks(response)

    @staticmethod
    async def _valid_evolution_run(tenant_id: str, run_id: str) -> str:
        """基准标记归属校验：`run_id` 必须是**本租户**的、当前 `status ∈ {running, adopting}` 的 run。

        `running` 覆盖发起 run 之后、人审之前的父本/候选评测与 probe —— router 模式下
        这是基准流量最大的来源；`adopting` 覆盖采纳阶段的 bench/smoke。两者之外的任何
        状态（`queued` / `evaluated` / `failed` / `dry-run` …）都不算基准标记。

        非空 `evolution_run_id` 会让观测整类排除在失败归因之外，因此不能只信调用方
        传进来的字符串。走 ORM `EvolutionRun`（表由 `models/evolution.py` 拥有）而
        不是裸 SQL：模型改名会自动跟随，无需同步改别处 SQL 字符串；若库表被改名而
        模型未跟进，这里会退化成「永不标记」并留下 warning。

        有效返回原 id，否则 `""`。**任何异常都退空串**（DB 挂了、会话坏了、查询形状
        不对都算）：漏标只会让基准样本多一条，而校验抛错会让真实用户的提问直接失败
        —— 代价不对称，所以这里是 fail-open。
        ⚠️ 这里**故意不重抛**编程错误集合（与同文件 `_load_lineage_edges` 相反），
        原因就是上一句：标记校验不值得让任何提问失败。
        """
        try:
            async with get_db_session() as session:
                row = (await session.execute(
                    select(EvolutionRun.id).where(
                        EvolutionRun.tenant_id == tenant_id,
                        EvolutionRun.id == run_id,
                        EvolutionRun.status.in_(("running", "adopting")),
                    )
                )).first()
                return run_id if row is not None else ""
        except Exception as exc:  # noqa: BLE001 —— fail-open：标记永远不能阻断提问
            # `exc_info=True`：区分「校验器坏了」（栈能看出来）与「DB 暂时不可用」。
            logger.warning("演化基准 run 归属校验失败，本次不标记：%s", exc,
                           exc_info=True)
            return ""

    @staticmethod
    async def _ensure_kb(tenant_id: str, kb_id: str):
        """KB 归属存在校验（不存在抛 `ResourceNotFoundError`，带租户 + 未软删过滤）。

        与 `FolderService._ensure_kb` / `TagService._ensure_kb` /
        `OntologySynonymService._ensure_kb` 同一句既有口径（`get_required`），
        只是那句是「在别人已经开好的 session 里调」，这里是独立入口、自己开一个。
        **不另造存在性查询**：这一句就是仓内「KB 必须存在」的既有定义。

        返回 KB 行：`ask_data_source` 入口还要读 `structured_enabled` 做软失败
        （未启用结构化 → `structured_disabled`，不答也不报错）。
        """
        async with get_db_session() as session:
            return await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)

    @staticmethod
    def _no_answer(reason_code: str, reason: str, **extra) -> dict:
        """答不了的标准形状 —— **生产路径一律经 `_fail` 调用**（直接调会绕过那条失败审计）。"""
        return {"ok": False, "not_answerable": True,
                "reason_code": reason_code, "reason": reason, **extra}

    @staticmethod
    def _scope_candidates(candidates: list[dict], data_source_id: str) -> list[dict]:
        """按请求指定的数据源收窄候选；没有指定源时原样返回。"""
        if not data_source_id:
            return list(candidates or [])
        return [c for c in (candidates or [])
                if str(c.get("data_source_id") or "") == str(data_source_id)]

    async def _fallback_anchor(self, tenant_id: str, kb_id: str, ds_id: str,
                              question: str):
        """无候选回退：从编译 schema 选确定性锚点；读不到就维持原拒答。"""
        try:
            row = await schema_read.load_compiled_row(tenant_id, kb_id, ds_id)
        except Exception:  # noqa: BLE001 —— 回退是增能项，失败退回 no_candidate
            return None
        if not row:
            return None
        name = choose_anchor(question, row.get("entity_types") or [])
        if not name:
            return None
        logger.info("NL→SQL 无候选回退锚点: ds=%s anchor=%s", ds_id, name)
        return {"data_source_id": ds_id, "entity_name": name}

    @staticmethod
    def _pick(candidates: list[dict], data_source_id: str, entity_name: str):
        """选源/选实体：**不自己发明打分规则**（spec §四）。

        - **两个都点名了 → 直接用**（spec §四「指定了 → 用它」）。词面匹配是「猜」的
          兜底，调用方点名的那一对**不需要先被匹配到**：实体到底存不存在由后面取列那一步
          兜（取不到 = `entity_gone`），那是**事实**判定，比「没进候选」准 —— 候选来自
          词面匹配 + 已确认的对齐，跟库里有没有那张表无关。
        - 只给了一个 → 在候选里按它收窄（另一个字段还得从候选拿）
        - 都没给且候选唯一 → 用它
        - 多个 → 不猜，把候选交回调用方
        """
        if data_source_id and entity_name:
            return {"data_source_id": data_source_id, "entity_name": entity_name}, None
        if data_source_id or entity_name:
            for c in candidates:
                if (not data_source_id or c.get("data_source_id") == data_source_id) and \
                   (not entity_name or c.get("entity_name") == entity_name):
                    return c, None
            return None, Nl2SqlService._no_answer(
                "no_candidate", "指定的数据源/实体不在该问题的候选里")
        if not candidates:
            return None, Nl2SqlService._no_answer(
                "no_candidate", "没有命中任何已对齐的数据实体")
        if len(candidates) > 1:
            return None, Nl2SqlService._no_answer(
                "ambiguous", "命中多个数据实体，需要指定 data_source_id / entity_name",
                candidates=candidates,
            )
        return candidates[0], None

    async def _attach_sample_rows(self, source, cfg: dict, engine: str,
                                  preview: list[dict]) -> dict:
        """P2-4b：给预览表各取 ≤N 行真实数据，挂到 `sample_rows`（失败 fail-open）。"""
        stats = {"sample_row_tables": 0, "sample_row_rows": 0,
                 "sample_row_chars": 0, "sample_row_errors": 0}
        budget = max(0, int(config.NL2SQL_SAMPLE_ROW_BUDGET))
        limit = max(1, int(config.NL2SQL_SAMPLE_ROW_LIMIT))
        timeout = float(config.NL2SQL_SAMPLE_ROW_TIMEOUT)
        max_tables = max(1, int(config.NL2SQL_SAMPLE_ROW_MAX_TABLES))
        for entry in preview[:max_tables]:
            if not isinstance(entry, dict) or budget <= 0:
                break
            sql = build_sample_sql(engine, entry.get("table"),
                                   entry.get("columns"), limit=limit)
            if not sql:
                continue
            try:
                result = await asyncio.wait_for(
                    source.run_query(cfg, sql, timeout=timeout), timeout=timeout)
            except Exception as exc:  # noqa: BLE001 —— 采样是增能，失败跳过该表
                stats["sample_row_errors"] += 1
                logger.info("NL→SQL 采样行失败（跳过该表）: engine=%s err=%s",
                            engine, type(exc).__name__)
                continue
            rows, used = normalize_rows(
                result, columns=entry.get("columns") or [], limit=limit,
                budget=budget, cell_chars=SAMPLE_CELL_CHARS)
            if not rows:
                continue
            entry["sample_rows"] = rows
            stats["sample_row_tables"] += 1
            stats["sample_row_rows"] += len(rows)
            stats["sample_row_chars"] += used
            budget -= used
        return stats

    async def _lookup_value_candidates(self, source, cfg: dict, predicate, *,
                                       engine: str) -> list:
        """查一列里包含该字面量的真实值（只读、超时、失败退空）。"""
        sql = build_lookup_sql(engine, predicate)
        if not sql:
            return []
        timeout = float(config.NL2SQL_VALUE_LOOKUP_TIMEOUT)
        try:
            result = await asyncio.wait_for(
                source.run_query(cfg, sql, timeout=timeout), timeout=timeout)
        except Exception:  # noqa: BLE001 —— 值查询是增能，失败退空
            return []
        return [row[0] for row in (result.get("rows") or []) if row]

    async def _ground_values(self, sql: str, *, source, cfg: dict, engine: str,
                             allowed_tables: set[str]) -> tuple[str, dict]:
        """v1 值接地：只做大小写/空白归一（不改拼写/缩写）。返回 (sql, stats)。"""
        stats = {"calls": 0, "hits": 0}
        allowed = {_table_key(t) for t in allowed_tables or ()}
        replacements: dict[str, str] = {}
        for predicate in extract_literals(sql, engine):
            if _table_key(predicate.table) not in allowed:
                continue
            candidates = await self._lookup_value_candidates(
                source, cfg, predicate, engine=engine)
            stats["calls"] += 1
            picker = (pick_value_match if config.NL2SQL_VALUE_LOOKUP_FUZZY
                      else pick_case_match)
            match = picker(predicate.literal, candidates)
            if match:
                replacements[predicate.literal] = match
        if not replacements:
            return sql, stats
        rewritten = replace_literals(sql, replacements, engine)
        if rewritten == sql:
            return sql, stats
        stats["hits"] = len(replacements)
        logger.info("NL→SQL 值接地改写: ds_engine=%s replacements=%s",
                    engine, replacements)
        return rewritten, stats

    async def _auto_hints(self, *, tenant_id: str, kb_id: str, ds_id: str,
                          question: str, ent: str, join_plan, constraints: list,
                          abstract_entities: list) -> list[str]:
        """组装生产侧 hints（A2）：编译 schema 的属性语义 + 抽象层的别名/枚举/指标。

        读口与 `_entity_columns` / `_load_concepts` 相同（版本化缓存，零额外会话）；
        任何一步缺失都只是「少几条 hint」，绝不抛 —— hints 是增能项，不能拖垮提问。
        """
        compiled = await schema_read.load_compiled_row(tenant_id, kb_id, ds_id)
        if not compiled:
            return []
        entity_types = compiled.get("entity_types") or []
        abstract_row = await schema_read.load_abstract_row(tenant_id, kb_id)
        abstract_schema: dict = {}
        if abstract_row and abstract_row.get("status") == "active":
            abstract_schema = abstract_row.get("schema") or {}
        # 1 跳邻居：related_tables 是物理表名，编译行按 source_object_id 反查实体。
        by_table = {str(e.get("source_object_id") or "").strip(): e
                    for e in entity_types if isinstance(e, dict)}
        related = []
        for table in (join_plan.related_tables or [])[:MAX_RELATED_ENTITIES]:
            name = str((table or {}).get("table") or "").strip()
            compiled_entity = by_table.get(name)
            if compiled_entity is not None:
                related.append((compiled_entity, None))
        try:
            return build_auto_hints(
                question=question, entity_name=ent, entity_types=entity_types,
                abstract_entities=abstract_entities, constraints=constraints,
                metrics=list(abstract_schema.get("metrics") or []), related=related)
        except Exception as exc:  # noqa: BLE001 —— hints 是增能，组装失败退空
            logger.warning("自动 hints 组装失败（本次退空）：kb=%s ds=%s err=%s",
                           kb_id, ds_id, exc)
            return []

    async def _load_concepts(self, tenant_id: str, kb_id: str):
        """该库**已确认**对齐里的实体（v2 `row.schema["entities"]`）+ 数据源名映射。

        ⚠️ 只认 `status == "active"`：`get_by_kb` **不过滤状态**，而一条 `proposed`
        （人还没看过）的对齐若能驱动查询，就是「没人确认过的概念直接去查生产库」。
        spec 决策 1 的原话是「从①的概念候选接（**人工确认过的**对齐层）」，与①的决策 4
        （未确认的对齐不进检索）同源 —— 兄弟路径 `list_active_by_kbs` 也是这么卡的。

        行读取走 `schema_read.load_abstract_row`（P1 版本化读取的唯一读口）：它同时
        解析 active 版本并标注漂移；`status` 过滤仍在这里（读口不替调用方做业务判定）。
        数据源名与 schema 行是两次读（版本化缓存只覆盖 schema 行），语义与改动前一致。
        """
        row = await schema_read.load_abstract_row(tenant_id, kb_id)
        async with get_db_session() as session:
            ds_rows = await KnowledgeDataSourceRepository(session).list_by_kb(
                tenant_id, kb_id, offset=0, limit=500)
        ds_names = {d.id: d.name for d in ds_rows}
        if row is None or row["status"] != "active":
            return [], ds_names
        schema = row["schema"] if isinstance(row["schema"], dict) else {}
        return list(schema.get("entities") or []), ds_names

    async def _entity_columns(self, tenant_id: str, kb_id: str, ds_id: str, entity: str):
        """该实体的 `(物理表标识, 列清单, join 图, 列样本, 口径约束（整行原样，调用方按实体挑）)`。取不到（源已删/实体已消失）返回 None。

        第一项是编译 schema 里的 `source_object_id`（**限定名** `schema.table`，
        生成器填的物理表标识）；**存量行没有这个字段 → 给 `None`**，由调用方回退成
        实体名（= 单表那档的行为）。⚠️ **空白值等价于没有**（`strip` 后才判空）——
        与探测侧的 `evidence_key` 同一判据：不 strip 的话 `"  "` 会被当成真表名喂给模型，
        而探测那边已经按「没有物理表名」把证据写在了实体名下。第二项是列清单（列名 + 原始类型）。

        ⚠️ 第三项（join 图）**没有**「回退」这一档：join 要写两张确定的表，
        没有物理表名就不给这条边（见 `joins.eligible_joins` 的规矩 1）。

        第四项是**已按「像枚举」挑好的列样本**（`{列名: [值]}`，可能为空），
        由 `pick_samples` 从生成期探测落下的表证据（Task 3 的独立列 `evidence.tables`）里挑 ——
        表键**与写入方共用 `evidence_probe.evidence_key`**（空白值先 strip、再回退实体名），
        两边各写一遍就会出现「写进实体名那档、读却去查 `"  "`」的静默取不到。
        没探过证据 / 存量行给 `{}`，调用方按「没有证据」处理（一个键都不加）。

        第五项是人审确认过的**库口径约束**（`r.constraints` 原样，可能为空列表），
        由调用方 `filter_constraints` 按锚点实体挑（只收 entity/attribute 两级、
        必须带 `expression`、≤8 条（2026-10-12 起））。

        五样都从**同一行**编译 schema 里取 —— 不用再多查一次库、也不会出现
        「列和证据来自不同版本」的错配。抽象层只存 `(data_source_id, entity_name)` 两键，
        物理表名与关系都在这里就地取。

        行读取走 `schema_read.load_compiled_row`（P1 版本化读取的唯一读口）：一次拿到
        该源那一行（原实现是列整库再按 ds 过滤，语义等价）+ active 版本戳与漂移标注。
        五元组**逐字不变**。
        """
        row = await schema_read.load_compiled_row(tenant_id, kb_id, ds_id)
        if row is not None:
            entity_types = row["entity_types"]
            for e in entity_types:
                if isinstance(e, dict) and str(e.get("name") or "") == entity:
                    # `name` 给**物理列名**（生成 SQL 用）；`logical_name` 是本体的属性名
                    # （概念/证据/治理用它）。两者只在属性名被 slug 化时不同
                    # （`"Academic Year"` → `academic_year`）—— 2026-10-23 BIRD 实测：
                    # 只给 slug 会让模型查到不存在的列。
                    attrs = []
                    for a in (e.get("attributes") or []):
                        if not isinstance(a, dict):
                            continue
                        logical = str(a.get("name") or "")
                        if not logical:
                            continue
                        physical = str(a.get("physical_name") or "").strip() or logical
                        attrs.append({
                            "name": physical,
                            "logical_name": logical,
                            "type": str(a.get("type") or ""),
                            "description": str(a.get("description") or "").strip(),
                        })
                    return (
                        str(e.get("source_object_id") or "").strip() or None,
                        attrs,
                        eligible_joins(row["relation_types"] or [], entity_types, entity),
                        # 证据是 Task 3 加的**独立列**，与 `entity_types` 同一行读到 ——
                        # 不会出现「列与证据来自不同版本」的错配（与 join 图同一条理由）。
                        (pick_examples if config.NL2SQL_COLUMN_EXAMPLES else pick_samples)(
                            (row["evidence"] or {}).get("tables") or {},
                            evidence_key(e),
                            # 证据按**属性名**（logical）记录；列清单的 `name` 可能是
                            # 物理名（带空格/大写），不能直接拿它查证据。
                            [{"name": a["logical_name"]} for a in attrs],
                        ),
                        # 口径约束也是同一行的列（P1-2 加的读口）；**本档只收
                        # entity/attribute 两级**（relation 级留给下一档）——
                        # 挑选用例见 `nl2sql/constraints.py`。**只读本数据源那一行**
                        # 的约束（按 `ds_id` 精确匹配）：KB 级那行（`data_source_id
                        # IS NULL`）的约束被有意排除（spec 决策 3）—— 库里 KB 级可能
                        # 存着一批 `unique_attribute`（如 15 条），它们**不服务 ②**，
                        # 别当 bug。
                        list(row["constraints"] or []),
                    )
        return None

    async def _compiled_row(self, tenant_id: str, kb_id: str, ds_id: str) -> dict:
        """该源那一行编译 schema 的**原始四列** —— 多轮档 `MultiturnContext` 的读料。

        与 `_entity_columns` 是**同一份读口、同一行**（`list_compiled_schemas` 再按
        `ds_id` 精确匹配），差别只在返回什么：这里给整行原料（`entity_types` /
        `relation_types` / `evidence` / `constraints` 原样），因为多轮档的三只工具
        各自带判据（`eligible_joins` 按实体翻正、`evidence_key` 先 strip 再回退、
        `filter_constraints` 只收 entity/attribute 两级）—— 把 `_entity_columns`
        已经挑好的那份塞进 ctx，等于让单次档的挑法替工具做决定。

        `_entity_columns` 的签名因此**不动**（P1-2 刚收口；那条路径有五元组契约）。
        取不到那行时回空值：调用点在这之前已经过了 `_entity_columns` 那道闸
        （行一定在，且实体也在），空值只是不让这里成为新的抛点。

        读口与 `_entity_columns` **同一份**（`schema_read.load_compiled_row`）：同一请求里
        第二次读同一行通常直接命中缓存（同一版本戳），不会再开一次会话。这里返回**副本**
        （`list`/`dict`），多轮档的工具消费不会碰到缓存里的对象。
        """
        row = await schema_read.load_compiled_row(tenant_id, kb_id, ds_id)
        if row is None:
            return {"entity_types": [], "relation_types": [], "evidence": {},
                    "constraints": []}
        return {
            "entity_types": list(row["entity_types"]),
            "relation_types": list(row["relation_types"]),
            "evidence": dict(row["evidence"]),
            "constraints": list(row["constraints"]),
        }

    @staticmethod
    def _audit_entry(tenant_id, user_id, username, ip, ds_id, outcome: str,
                     request_params: dict) -> dict:
        """两类记录共用的一层壳 —— 只有 `outcome` 与 `request_params` 分叉。

        同一个 `action="data_source.ask"`：一次提问就是一条记录，成败都在这一条 stream 里，
        查询「谁问过这个源、问成了没有」不用去两张表里对时间。
        """
        return {
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "username": username,
            "ip": ip,
            "log_type": "TASK",
            "action": "data_source.ask",
            "outcome": outcome,
            "service_name": "knowledge_base",
            "resource": ResourceType.DATA_SOURCE.value,
            "resource_id": str(ds_id),
            "request_params": request_params,
        }

    @staticmethod
    def _audit(tenant_id, user_id, username, ip, ds_id, question, sql, row_count,
               elapsed_ms: int, extra: dict | None = None) -> None:
        """一条成功审计：谁、哪个租户、哪个源、什么问句、生成的 SQL、执行耗时、行数（spec §七）。

        `elapsed_ms` 只算**执行**那一段（与行数并列的那一项）：生成 SQL 的模型时间由
        llm-gateway 那侧计量，两笔账不混在一条记录里。

        `extra` **只给多轮档用**（`rounds` / `tool_calls`，来自生成那步的 `meta`）：
        单次档**不传** ⇒ `request_params` 的键与今天逐字节相同（一个键都不多）。
        """
        params: dict = {"question": question[:200], "sql": sql[:500],
                        "row_count": row_count, "elapsed_ms": elapsed_ms}
        if extra:
            params.update(extra)
        schedule_emit(Nl2SqlService._audit_entry(
            tenant_id, user_id, username, ip, ds_id, "SUCCESS", params,
        ))

    @staticmethod
    def _audit_failed(tenant_id, user_id, username, ip, ds_id, question, reason_code: str,
                      reason: str = "", *, sql: str | None = None,
                      rejected_sql: str | None = None,
                      extra: dict | None = None) -> None:
        """一条**失败**审计（`outcome="FAILED"`）：与成功那条同 `action`，但 `request_params`
        里没有 `row_count`/`elapsed_ms`（这趟没执行），换成 `reason_code`（哪一档）+ `reason`
        + 可选的 `sql`（执行失败那条）/ `rejected_sql`（guard 拦下那条）。

        ⚠️ guard 拒绝那条的**既有键（`request_params["rejected_sql"]` 与 `reason_code`）一个不少**
        —— 既有用例 `test_rejected_sql_leaves_a_failed_audit_entry` 断言的正是它们。

        `extra` 为 B 重试的审计扩展键（`_audit_extra` 产出）：未重试时传 None、既有键一个不动。
        """
        params: dict = {"question": question[:200], "reason_code": reason_code}
        if reason:
            params["reason"] = reason[:200]
        if sql:
            params["sql"] = sql[:500]
        if rejected_sql:
            params["rejected_sql"] = rejected_sql[:500]
        if extra:
            params.update(extra)
        schedule_emit(Nl2SqlService._audit_entry(
            tenant_id, user_id, username, ip, ds_id, "FAILED", params))

    def _schedule_metric_shadow(
        self, *, tenant_id: str, kb_id: str, data_source_id: str, question: str,
        entity_name: str, main_outcome: str, main_reason_code: str = "",
        main_row_count: int | None = None, main_elapsed_ms: int | None = None,
        main_cell: tuple[bool, object] = (False, None),
        username: str = "", evolution_run_id: str = "",
        record_evidence: bool = True, observe_only: bool = False,
        synthetic: bool = False,
    ) -> None:
        """调度一次影子对比；**对主回答零副作用**（开关/条件不满足直接 return）。

        合成流量、observe_only、`record_evidence=False` 都不采样：这些不是真实
        用户提问（或不该留任何痕），影子表也不该被它们冲淡。
        """
        if not config.NL2SQL_METRIC_SHADOW:
            return
        if synthetic or not record_evidence or observe_only:
            return
        if evolution_run_id or username == EVOLUTION_USERNAME:
            return
        if not kb_id or not data_source_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 纯同步上下文（脚本/测试）不补后台 loop：直接跳过
            return
        try:
            task = loop.create_task(self._run_metric_shadow(
                tenant_id=tenant_id, kb_id=kb_id,
                data_source_id=data_source_id, question=question,
                entity_name=entity_name, main_outcome=main_outcome,
                main_reason_code=main_reason_code,
                main_row_count=main_row_count,
                main_elapsed_ms=main_elapsed_ms, main_cell=main_cell,
            ))
            _shadow_tasks.add(task)
            task.add_done_callback(_on_shadow_done)
        except Exception as exc:  # noqa: BLE001 —— 调度失败不影响回答
            logger.warning("[metric-shadow] 调度失败（已吞）: %s", exc)

    async def _run_metric_shadow(
        self, *, tenant_id: str, kb_id: str, data_source_id: str, question: str,
        entity_name: str, main_outcome: str, main_reason_code: str = "",
        main_row_count: int | None = None, main_elapsed_ms: int | None = None,
        main_cell: tuple[bool, object] = (False, None),
    ) -> None:
        """影子任务本体：读 schema → 命中 → 编译 → 执行 → 比较 → 落库。

        **所有异常都收口成一行记录**（或 warning），绝不冒泡；更不修改主结果。
        """
        base = {
            "tenant_id": tenant_id,
            "knowledge_base_id": kb_id[:64],
            # 请求体来的值必须按列宽截断，否则 INSERT 失败会被 record 吞掉、
            # 这条样本静默消失。
            "data_source_id": str(data_source_id or "")[:64],
            "question": str(question or "")[:500],
            "entity_name": (str(entity_name)[:255] if entity_name else None),
            "main_outcome": main_outcome,
            "main_reason_code": main_reason_code or None,
            "main_row_count": main_row_count,
            "main_elapsed_ms": main_elapsed_ms,
        }
        started_at = time.perf_counter()

        async def record(status: str, agreement: str, **extra) -> None:
            entry = {**base, "status": status, "agreement": agreement}
            entry.update(extra)
            await metric_shadow.record(entry)

        pick = None
        try:
            abstract = await schema_read.load_abstract_row(tenant_id, kb_id)
            if not abstract or abstract.get("status") != "active":
                await record("no_metric", "not_comparable",
                             scene={"reason": "no_active_abstract"})
                return
            metrics = (abstract.get("schema") or {}).get("metrics") or []
            if not isinstance(metrics, list):
                await record("no_metric", "not_comparable",
                             scene={"reason": "metrics_not_list"})
                return
            pick = metric_shadow.pick_shadow_metric(question, metrics)
            if pick.status == metric_shadow.PICK_NONE:
                await record("no_metric", "not_comparable",
                             scene={"reason": "no_match"})
                return
            if pick.status == metric_shadow.PICK_NOT_EXECUTABLE:
                await record(
                    "not_executable", "not_comparable",
                    metric_name=pick.name[:255] or None,
                    match_kind=pick.match_kind[:16] or None,
                    matched_term=pick.matched_term[:255] or None,
                    scene={"reason": "no_executable"},
                )
                return

            metric = pick.metric or {}
            metric_name = str(metric.get("name") or pick.name or "")[:255]
            metric_type = str((metric.get("executable") or {}).get("metric_type")
                              or "")[:16] or None
            target = await self._ds.query_config(tenant_id, data_source_id)
            if target is None:
                await record(
                    "compile_error", "not_comparable",
                    metric_name=metric_name or None, metric_type=metric_type,
                    match_kind=pick.match_kind[:16] or None,
                    matched_term=pick.matched_term[:255] or None,
                    scene={"reason": "no_readonly_creds"},
                )
                return
            source, cfg = target
            engine = str(cfg.get("engine") or "")
            if normalize_engine(engine) != "postgres":
                # 编译器目前只出 PostgreSQL；其他方言若硬跑，只会把「不支持」
                # 伪装成执行失败，系统性污染覆盖率。
                await record(
                    "compile_error", "not_comparable",
                    metric_name=metric_name or None, metric_type=metric_type,
                    match_kind=pick.match_kind[:16] or None,
                    matched_term=pick.matched_term[:255] or None,
                    scene={"reason": "unsupported_dialect", "engine": engine[:32]},
                )
                return
            forbidden_pairs = await _load_forbidden_pairs(
                tenant_id, kb_id, data_source_id)
            compiled_row = await schema_read.load_compiled_row(
                tenant_id, kb_id, data_source_id)
            scope = {
                str(item.get("name")): item for item in metrics
                if isinstance(item, dict) and item.get("name")
            }
            compiled = preflight(
                metric, scope=scope, key=str(metric.get("name") or ""),
                compiled_row=compiled_row, data_source_id=data_source_id,
                max_rows=config.NL2SQL_MAX_ROWS,
            )
            result = await execute_metric(
                compiled, source=source, cfg=cfg, engine=engine,
                max_rows=config.NL2SQL_MAX_ROWS,
                forbidden_pairs=forbidden_pairs,
            )
            # 影子延迟含 schema 读取/命中/编译/执行：报告要回答的是「灰度成本」，
            # 只算 execute 会低估。
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            shadow_row_count = int(result.get("row_count") or 0)
            agreement = metric_shadow.compare_results(
                main_outcome=main_outcome,
                main_row_count=main_row_count,
                main_cell=main_cell,
                shadow_row_count=shadow_row_count,
                shadow_cell=metric_shadow.first_cell(result),
            )
            await record(
                "exec_ok", agreement,
                metric_name=metric_name or None, metric_type=metric_type,
                match_kind=pick.match_kind[:16] or None,
                matched_term=pick.matched_term[:255] or None,
                compiled_sql=compiled.sql[:4000],
                shadow_row_count=shadow_row_count,
                shadow_elapsed_ms=elapsed_ms,
                scene={
                    "additivity": metric.get("additivity"),
                    # 适配器列形状不一致：PG/MySQL/MSSQL 是 list[str]，
                    # ClickHouse/Influx 是 list[dict]；统一走 helper。
                    "columns": metric_shadow.column_names(result)[:10],
                },
            )
        except MetricCompileError as exc:
            picked_metric = (pick.metric or {}) if pick is not None else {}
            await record(
                "compile_error", "not_comparable",
                metric_name=str(picked_metric.get("name") or "")[:255] or None,
                scene={"reason": exc.code, "error": str(exc)[:500]},
            )
        except MetricExecutionRejected as exc:
            picked_metric = (pick.metric or {}) if pick is not None else {}
            await record(
                "exec_failed", "not_comparable",
                metric_name=str(picked_metric.get("name") or "")[:255] or None,
                scene={"reason": "rejected", "error": str(exc)[:500]},
            )
        except Exception as exc:  # noqa: BLE001 —— 影子绝不冒泡
            logger.warning("[metric-shadow] 执行异常（已吞）: %s", exc)
            await record(
                "exec_failed", "not_comparable",
                scene={"reason": "exception",
                       "error": f"{type(exc).__name__}: {str(exc)[:400]}"},
            )

    def _fail(self, tenant_id, user_id, username, ip, question, ds_id, reason_code,
              reason: str, *, audit_extra: dict | None = None, scene: dict | None = None,
              kb_id: str = "", entity_name: str = "",
              record_evidence: bool = True, synthetic: bool = False,
              evolution_run_id: str = "", observe_only: bool = False,
              **extra) -> dict:
        """**失败出口唯一收口**：落一条失败审计 + 一条观测行，再返回 `_no_answer(...)` 同一形状。

        `extra` 原样透传给 `_no_answer`（`sql` / `rejected_sql` / `candidates` 等）；
        其中 `sql`/`rejected_sql` 同时进审计与观测（给诊断看）。

        `synthetic=True`（演化基准臂的合成流量：合法 run id **且**演化身份）：
        **跳过失败审计**，观测照写（诊断仍要看见基准样本）。由 `ask_data_source`
        用 `partial` 统一绑定；普通提问恒为 False、行为逐字节不变。

        ⚠️ `audit_extra` / `scene` / `kb_id` / `entity_name` / `synthetic` 必须是
        **显式具名参数**、不能落进 `**extra` —— 那会把它一并传进 `_no_answer(**extra)`、
        把内部键泄漏进响应体。

        观测是**旁路**：`_audit_failed` 一字未动；`_observe` 落行的**库写失败**被
        `observe.schedule_record` 调度的 `_write` 吞掉（与审计 `schedule_emit` 同姿势），
        不影响返回值。⚠️ 吞的范围只到**写**为止 —— `_observe` 自身的装配（建 entry
        dict）与 `schedule_record` 的**调度**动作（如有运行 loop 时的 `loop.create_task`；
        无 loop 时的后台投递则另裹有 try/except）不在此承诺内。
        """
        if record_evidence:
            if not synthetic:
                self._audit_failed(tenant_id, user_id, username, ip, ds_id, question,
                                   reason_code, reason,
                                   sql=extra.get("sql"),
                                   rejected_sql=extra.get("rejected_sql"),
                                   extra=audit_extra)
            self._observe(tenant_id=tenant_id, kb_id=kb_id,
                          data_source_id=ds_id, question=question,
                          outcome="failed", reason_code=reason_code,
                          reason=reason, entity_name=entity_name,
                          sql=extra.get("sql"),
                          rejected_sql=extra.get("rejected_sql"),
                          scene=scene or {})
            # W3.3 M4：失败档影子对比（失败样本最有价值；开关默认关）。
            self._schedule_metric_shadow(
                tenant_id=tenant_id, kb_id=kb_id, data_source_id=ds_id,
                question=question, entity_name=entity_name,
                main_outcome="failed", main_reason_code=reason_code,
                main_row_count=None, main_elapsed_ms=None,
                username=username, record_evidence=record_evidence,
                evolution_run_id=evolution_run_id, observe_only=observe_only,
                synthetic=synthetic,
            )
        return self._no_answer(reason_code, reason, **extra)

    def _observe(self, *, tenant_id: str, kb_id: str, data_source_id: str, question: str,
                 outcome: str, reason_code: str = "", reason: str = "",
                 entity_name: str = "", sql: str | None = None,
                 rejected_sql: str | None = None, row_count: int | None = None,
                 elapsed_ms: int | None = None, scene: dict | None = None) -> None:
        """装配一条观测行 → `observe.schedule_record`（**测试打桩点 = `observe.schedule_record`**）。

        截断口径（设计 §3.1，**有意比审计宽**）：question 500 / reason 500 / sql·rejected_sql 1000。
        空串一律归 None（列可空，别把「无」写成空串）——**NOT NULL 列除外**：
        `data_source_id` 留空串（保住诊断分组键 `str(resource_id or "")` 的既有口径）。
        """
        observe.schedule_record({
            "tenant_id": tenant_id,
            "knowledge_base_id": kb_id,
            "data_source_id": data_source_id or "",
            "question": str(question or "")[:observe.MAX_QUESTION_CHARS],
            "outcome": outcome,
            "reason_code": reason_code or None,
            "reason": (str(reason or "")[:observe.MAX_REASON_CHARS]) or None,
            "entity_name": entity_name or None,
            "sql": (str(sql or "")[:observe.MAX_SQL_CHARS]) or None,
            "rejected_sql": (str(rejected_sql or "")[:observe.MAX_SQL_CHARS]) or None,
            "row_count": row_count,
            "elapsed_ms": elapsed_ms,
            "scene": scene or {},
        })


def _jsonable(value):
    """把驱动交回来的单元格转成 JSON 原生值。

    `Decimal` / `datetime` 是真实库里最常见的两种（计数、时间戳），**不是边缘情况**。
    整数型 Decimal 保持成 `int`（「有多少家」必须回数字 `32`，不是 `"32"`），
    其余 Decimal 落 `float`；日期时间走 ISO 串；UUID / bytes 等一律 `str()`。

    非有限 Decimal（`Infinity` / `-Infinity` / `NaN`）**先挡掉**：`int()` 会抛
    `OverflowError`，而那时查询**已经成功**了 —— 一个畸形单元格能把 200 变成 500；
    `float("nan")` 又不是合法 JSON。回 `str()` 至少是个能看见的值。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return str(value)
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value)


__all__ = ["Nl2SqlService"]
