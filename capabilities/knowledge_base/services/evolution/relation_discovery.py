# -*- coding:utf-8 -*-
"""join 边候选自动发现（`relation` 演化的自动来源，P1-3 Task 2）。

从 `tests/eval/nl2sql/join_discovery.py` 移植**发现逻辑**（命名短名单 +
值包含采样），去掉 HTTP/CLI 外壳：

- 输入：编译 schema 行（`entity_types` / `relation_types`）+ 注入的 `sampler`
  （`async def(sql) -> list[list]`，生产用 `upstream.truth_runner_for` 的只读凭据）
  + `request`（`max_candidates` / `max_queries` / `sample_n` / `picks`）；
- 输出：`{"entries": [...], "evidence": {...}}`。`entries` 只含**无歧义的 new
  候选**（`ratio >= 0.95`、短名单唯一命中、采样数 > 0），形状与
  `engine.plan_relation_entries` 的入参一致；其余判词（`ambiguous` / `weak` /
  `none` / `untestable` / `review` / `error`）只进 evidence。

**只读**：本模块只构造 SELECT 并调用注入的 sampler，从不写库、不写行。
采纳/落库仍由既有 pipeline（`builders` → `apply_patch`）负责。

方言可移植：采样 SQL 按引擎生成 —— SQL Server `SELECT DISTINCT TOP n …`，
postgres / mysql `… LIMIT n`；引擎不明直接跳过（`reason="unknown_engine"`）。
包含关系在 Python 里算（`str(v)` 逐字比较、不做 strip/casefold），避免
`CONVERT(nvarchar(200), …)` 这类方言专有写法。

**标识符白名单（安全边界）**：编译 schema 可被 KB 管理员改写
（`save_compiled_schema` / YAML 导入），表名/列名是**不可信输入**。所有拼进
SQL 的标识符必须先通过 `_IDENTIFIER_RE`（`schema.table` 形态、每段
`[A-Za-z_@#][A-Za-z0-9_$#@]*`）；不合规的候选直接 `verdict="error"`，**不发
查询**，`why` 只给通用原因、不回显原始标识符（回显会把它再泄进运行详情）。
不走 `guard_sql`：那套闸会拒绝合法的采样形状（DISTINCT / 无 ORDER BY 等），
这里用白名单而不是语句级校验。

**白名单的已知限制（fail-safe）**：带引号或非 ASCII 的标识符（如
`[dbo].[jobs]`、`"jobs"`、中文物理列名）**不**在白名单内，会以
`error_class="identifier_rejected"` 被安全拒绝（不生成 SQL）。这是有意的
保守取舍：这些名字在三种方言里的引号/转义规则不同，统一处理容易写出可注入
的拼接；需要支持时应在编译层先归一成无引号的 `schema.table` 形态。

**已知限制（目标侧采样上限）**：目标值同样最多取 `sample_n` 个 DISTINCT 键，
且**没有 ORDER BY**。当目标表 DISTINCT 键数远大于 `sample_n` 时，命中率会被
**低估**（`ratio` 只能当下界，真实 join 可能更强）；同一候选换一次运行也可能
取到不同子集，比率不可复现。源侧同理（采样不保证代表全表分布）。本模块因此
只把 `>= 0.95` 当**提案**门槛，不宣称精确的引用完整性。

**`entries: []` 的语义**：API 显式传空数组（或缺省）表示「**跑发现器**」，
不是「空草稿」；只有发现器零产出才会收口 `empty-draft`。
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time

from ..schema_source import normalize_engine
from .upstream import truth_runner_for

logger = logging.getLogger(__name__)

#: 只对 `_id` / `_code` 后缀列探测（与源发现器同口径）。
_SUFFIX = re.compile(r"_(id|code)$", re.I)
#: 时间/二进制列做包含没有意义，纯烧查询。
_SKIP_TYPES = {"datetime", "date", "time", "timestamp", "blob", "bytes", "binary"}
#: 每候选的命名短名单上限（每多一个目标就多一条采样查询）。
MAX_SHORTLIST = 5
#: 主干短于这个长度不做短名单（`bu` 撞 `budget` 全是噪声）。
_MIN_STEM = 3
#: 采样地板：低于它的比率只能当弱证据（与源发现器同口径）。
MIN_SAMPLE_STRONG = 10
#: 无歧义 new 的比率阈值。
STRONG_RATIO = 0.95

#: 采样 SQL 的标识符白名单：可带 schema 前缀，每段
#: `[A-Za-z_@#][A-Za-z0-9_$#@]*`（不接受的字符一律视为不可信，候选直接 error）。
_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z_@#][A-Za-z0-9_$#@]*(\.[A-Za-z_@#][A-Za-z0-9_$#@]*)*$")

#: 默认预算（控制器裁决：20 候选 / 40 查询 / 500 采样）。
RELATION_DISCOVERY_MAX_CANDIDATES = 20
RELATION_DISCOVERY_MAX_QUERIES = 40
RELATION_DISCOVERY_SAMPLE_N = 500
RELATION_DISCOVERY_TIMEOUT_SECONDS = 60.0
RELATION_DISCOVERY_TIMEOUT_ENV = "RELATION_DISCOVERY_TIMEOUT_SECONDS"

REASON_NO_DATA_SOURCE = "no_data_source"
REASON_NO_READONLY_CREDS = "no_readonly_creds"
REASON_UNKNOWN_ENGINE = "unknown_engine"
REASON_ENGINE_LOOKUP_FAILED = "engine_lookup_failed"

TRUNCATED_MAX_CANDIDATES = "max_candidates"
TRUNCATED_MAX_QUERIES = "max_queries"
TRUNCATED_TIMEOUT = "timeout"

#: 稳定的错误分类（写进 evidence 的 `error_class`，运维可据此区分「权限/连接
#: 失败」与「保留字/非法标识符」，同时不泄露 SQL 或驱动原文）。
ERROR_IDENTIFIER_REJECTED = "identifier_rejected"
ERROR_QUERY_FAILED = "query_failed"
ERROR_TIMEOUT = "timeout"
ERROR_ENGINE_LOOKUP_FAILED = "engine_lookup_failed"

_ERROR_CLASS_MESSAGES = {
    ERROR_IDENTIFIER_REJECTED: "采样标识符不符合白名单",
    ERROR_QUERY_FAILED: "采样查询失败",
    ERROR_TIMEOUT: "采样查询超时",
}


class IdentifierRejected(ValueError):
    """采样标识符未通过白名单（消息固定，不回显原始标识符）。"""


# ------------------------------------------------------------------ 命名短名单
def _stem(column: str) -> str | None:
    """`xxx_id` / `xxx_code` → `xxx`（小写）；主干不足 `_MIN_STEM` → None。"""
    match = _SUFFIX.search(column or "")
    if not match:
        return None
    stem = (column or "")[: match.start()].lower()
    return stem if len(stem) >= _MIN_STEM else None


def _shortlist(entities: list[dict], col: str) -> list[str]:
    """`<stem>` 与实体名/物理表名的粗匹配（≤MAX_SHORTLIST，按匹配强度与名字排序）。

    与源发现器同规则：完全相等 0 分、前缀 1 分、包含 2 分；同分取**名字最短**
    （`job` 下 `Jobs` 必须压过 `JobAttributes`）。

    **末段词回退**：复合列名的主干对不上实体（`creator_user_id` 的主干是
    `creator_user`，实体叫 `Users`）——补一轮「主干最后一段」（`user`）再试。
    """
    stem = _stem(col)
    if not stem:
        return []
    stems = [stem]
    tail = stem.rsplit("_", 1)[-1]
    if tail != stem and len(tail) >= _MIN_STEM:
        stems.append(tail)
    scored: list[tuple[int, str]] = []
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        if not name:
            continue
        low = name.lower()
        table = str(entity.get("source_object_id") or "").split(".")[-1].lower()
        best: int | None = None
        for candidate in stems:
            if low == candidate or table == candidate:
                score = 0
            elif low.startswith(candidate) or table.startswith(candidate):
                score = 1
            elif (low.endswith(candidate) or table.endswith(candidate)
                  or candidate in low or candidate in table):
                score = 2
            else:
                continue
            best = score if best is None else min(best, score)
        if best is not None:
            scored.append((best, name))
    scored.sort(key=lambda item: (item[0], len(item[1]), item[1]))
    return [name for _, name in scored[:MAX_SHORTLIST]]


def _entity_index(entities: list[dict]) -> dict[str, dict]:
    return {str(e.get("name")): e for e in entities or []
            if isinstance(e, dict) and str(e.get("name") or "").strip()}


def _attributes(entity: dict) -> list[dict]:
    return [a for a in (entity or {}).get("attributes") or []
            if isinstance(a, dict) and a.get("name")]


def _attribute(entity: dict, column: str) -> dict | None:
    wanted = str(column or "").strip()
    if not wanted:
        return None
    for attr in _attributes(entity):
        name = str(attr.get("name") or "").strip()
        physical = str(attr.get("physical_name") or "").strip()
        if wanted == name or (physical and wanted == physical):
            return attr
    return None


def _physical_column(entity: dict, column: str) -> str:
    """逻辑列名 → 物理列名（编译行没带 `physical_name` 时退回逻辑名）。"""
    attr = _attribute(entity, column)
    physical = str((attr or {}).get("physical_name") or "").strip()
    return physical or str(column or "").strip()


def _table_of(entity: dict) -> str:
    """实体 → 物理表名（`source_object_id` 原样，缺省退回实体名）。"""
    return (str((entity or {}).get("source_object_id") or "").strip()
            or str((entity or {}).get("name") or ""))


def _target_pk(entity: dict) -> str | None:
    """目标实体的关联列：显式主键 → 名为 `id` 的属性 → None（候选跳过）。

    编译行里 `is_primary_key` 才是权威；只有 `id` 是受限回退（与控制器裁决
    「attribute flagged pk? else `"id"` if present, else skip」同口径）。
    """
    attrs = _attributes(entity or {})
    for attr in attrs:
        if attr.get("is_primary_key") and attr.get("name"):
            return str(attr["name"])
    for attr in attrs:
        if str(attr.get("name")) == "id":
            return "id"
    return None


# ------------------------------------------------------------------ 候选生成
def suffix_candidates(entities: list[dict], relations: list[dict], *,
                      picks: list[str] | None = None
                      ) -> tuple[list[dict], list[dict]]:
    """候选生成（移植源 `suffix_candidates`，只读、不查库）：

    - `new`：带 `_id/_code` 后缀、且**没有任何关系**（任何 origin）的列 ——
      短名单给目标；同向/反向边已存在、自连、目标无主键 → 记 `skipped` 带 `why`；
    - `review`：`origin=derived` 的边逐条复核（对端 = 该边现有目标）。复核只做
      证据，**永不产出条目**（自动构建只补新边）。

    `picks`（可选）是目标实体白名单：只保留短名单里命中 picks 的目标（与
    `query_rule` 的 `picks` 同语义：限定本批关注面）。
    """
    entities = [e for e in entities or [] if isinstance(e, dict)]
    by_name = _entity_index(entities)
    picks_set = {str(p).strip() for p in picks or [] if str(p).strip()} or None

    existing_columns: set[tuple[str, str]] = set()
    pairs: set[tuple[str, str, str, str]] = set()
    review: list[dict] = []
    for relation in relations or []:
        if not isinstance(relation, dict):
            continue
        src = str(relation.get("source") or "").strip()
        col = str(relation.get("source_column") or "").strip()
        tgt_name = str(relation.get("target") or "").strip()
        tcol = str(relation.get("target_column") or "").strip()
        if src and col:
            existing_columns.add((src, col))
        if src and col and tgt_name and tcol:
            pairs.add((src, col, tgt_name, tcol))
        if str(relation.get("origin") or "") != "derived":
            continue
        target = by_name.get(tgt_name)
        if not src or not col or target is None:
            continue
        # 对端列先在编译 schema 上自校；对不上回落主键（与源发现器同口径）。
        if _attribute(target, tcol) is None:
            tcol = _target_pk(target) or ""
        if not tcol or _attribute(target, tcol) is None:
            continue
        review.append({"kind": "review", "from": src, "from_column": col,
                       "to": tgt_name, "to_column": tcol})

    new: list[dict] = []
    skipped: list[dict] = []
    for entity in by_name.values():
        entity_name = str(entity.get("name"))
        for attr in _attributes(entity):
            col = str(attr.get("name"))
            if not _SUFFIX.search(col):
                continue
            if str(attr.get("type") or "").lower() in _SKIP_TYPES:
                continue
            if (entity_name, col) in existing_columns:
                skipped.append({"source": entity_name, "source_column": col,
                                "why": "该列已有关系边（任何来源），不重复发现"})
                continue
            shortlist = _shortlist(entities, col)
            targets: list[str] = []
            for target_name in shortlist:
                if target_name == entity_name:
                    continue                      # 自连边不产
                if picks_set is not None and target_name not in picks_set:
                    continue
                target = by_name.get(target_name)
                pk = _target_pk(target or {})
                if not pk:
                    skipped.append({
                        "source": entity_name, "source_column": col,
                        "target": target_name,
                        "why": "目标实体没有可识别主键（无 is_primary_key / id）"})
                    continue
                if (entity_name, col, target_name, pk) in pairs:
                    skipped.append({"source": entity_name, "source_column": col,
                                    "target": target_name,
                                    "why": "同向 join 边已存在"})
                    continue
                if (target_name, pk, entity_name, col) in pairs:
                    skipped.append({"source": entity_name, "source_column": col,
                                    "target": target_name,
                                    "why": "反向 join 边已存在"})
                    continue
                targets.append(target_name)
            if not targets:
                if shortlist and all(t == entity_name for t in shortlist):
                    skipped.append({"source": entity_name, "source_column": col,
                                    "target": entity_name,
                                    "why": "命名短名单只命中自身（不做自连边）"})
                continue
            new.append({"kind": "new", "from": entity_name,
                        "from_column": col, "targets": targets})
    # 该顺序与源发现器相反：自动构建的产物是 new 条目，先花预算在能产条目的
    # 候选上；复核（review）只用剩余预算做证据（有意偏离，见报告）。
    return new + review, skipped


# ------------------------------------------------------------------ 判词
def confidence(ratio: float, sampled: int, target_is_pk: bool) -> str:
    """置信度三档（源发现器同口径）：high / mid / low / none。"""
    if ratio >= 1.0 and sampled >= 200 and target_is_pk:
        return "high"
    if ratio >= STRONG_RATIO and sampled >= MIN_SAMPLE_STRONG:
        return "mid"
    if ratio > 0:
        return "low"
    return "none"


def verdict(kind: str, ratio: float, hits: int, *, sampled: int,
            extra_hits: int = 0) -> str:
    """条目判词（源发现器同口径）。

    - review：confirm / veto / suspect / **untestable**（`sampled == 0` 不是
      否决证据 —— 本库列全空/全 NULL 时别把「没数据可测」说成「数据不支持」）；
    - new：none（零命中）/ ambiguous（多个目标都能对上，别替人挑）/ new / weak。
    """
    if kind == "review":
        if sampled == 0:
            return "untestable"
        if ratio >= 1.0:
            return "confirm"
        if hits == 0:
            return "veto"
        return "suspect"
    if ratio <= 0:
        return "none"
    if extra_hits > 0:
        return "ambiguous"
    if ratio >= STRONG_RATIO:
        return "new"
    return "weak"


# ------------------------------------------------------------------ 方言
def _dialect(engine: str) -> str:
    """引擎名 → 采样 SQL 方言键（`sqlserver` / `postgres` / `mysql`）；空串=不明。"""
    return normalize_engine(engine)


def _valid_identifier(value) -> bool:
    """标识符是否命中白名单（`schema.table` 形态；空串/非法字符 → False）。"""
    return bool(_IDENTIFIER_RE.match(str(value or "")))


def _sample_sql(dialect: str, table: str, column: str, sample_n: int) -> str:
    """按方言生成「取最多 sample_n 个非空 DISTINCT 值」的只读 SQL。

    写进 SQL 的标识符（表名/列名）**必须**先过 `_IDENTIFIER_RE`，否则抛通用
    `ValueError`（消息不回显标识符）。编译 schema 可被 KB 管理员改写，不能
    当可信字符串拼接。

    标识符不做引号改写：`source_object_id` 已是 `dbo.users` 这类限定形态，
    三家方言引号规则不同，统一加引号反而会把它弄坏。
    """
    if not _valid_identifier(table) or not _valid_identifier(column):
        raise IdentifierRejected(_ERROR_CLASS_MESSAGES[ERROR_IDENTIFIER_REJECTED])
    if dialect == "sqlserver":
        return (f"SELECT DISTINCT TOP {int(sample_n)} {column} AS v "
                f"FROM {table} WHERE {column} IS NOT NULL")
    return (f"SELECT DISTINCT {column} AS v FROM {table} "
            f"WHERE {column} IS NOT NULL LIMIT {int(sample_n)}")


def relation_discovery_timeout(value=None) -> float:
    """`RELATION_DISCOVERY_TIMEOUT_SECONDS` 解析：默认 60 秒，非法/非正数回落。

    与 `bench.bench_timeout` 同口径：非有限值（inf/nan）一律回落默认，避免
    一个错字符让发现器永不超时。
    """
    raw = value if value is not None else os.environ.get(
        RELATION_DISCOVERY_TIMEOUT_ENV)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return RELATION_DISCOVERY_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        return RELATION_DISCOVERY_TIMEOUT_SECONDS
    return timeout


def _int_option(request: dict, key: str, default: int) -> int:
    """正整数选项；缺省/非法/负数回落默认值（`bool` 不算整数）。"""
    raw = (request or {}).get(key)
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# ------------------------------------------------------------------ 采样
def _row_value(row):
    """采样行 → 单值（`list[list]` 是契约；dict/标量只做防御）。"""
    if isinstance(row, (list, tuple)):
        return row[0] if row else None
    if isinstance(row, dict):
        return next(iter(row.values()), None) if row else None
    return row


async def _fetch_values(sampler, sql: str) -> list[str]:
    """跑一条采样 SQL → `str(v)` 去重列表（None 丢弃；SQL 侧已 DISTINCT）。"""
    rows = await sampler(sql) or []
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = _row_value(row)
        if value is None:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        values.append(text)
    return values


def _error_class(exc: Exception) -> str:
    """异常 → 稳定错误分类（`identifier_rejected` / `timeout` / `query_failed`）。"""
    if isinstance(exc, IdentifierRejected):
        return ERROR_IDENTIFIER_REJECTED
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return ERROR_TIMEOUT
    return ERROR_QUERY_FAILED


def _why(exc: Exception) -> str:
    """异常 → **通用**原因（不回显 SQL / 标识符 / 驱动原文）。

    采样 SQL 由不可信标识符拼成，驱动异常常会把语句片段带回来；运行详情
    会被用户读到，所以这里只保留异常类型 + 分类固定文案。诊断靠
    `error_class`（稳定的机器可读 token）与候选端点。
    """
    return f"{type(exc).__name__}: {_ERROR_CLASS_MESSAGES[_error_class(exc)]}"


def _empty_result(evidence: dict) -> dict:
    return {"entries": [], "evidence": evidence}


#: 摘要里的判词顺序（先失败/歧义，再正向；未知判词排后按字母序）。
_VERDICT_ORDER = ("error", "ambiguous", "weak", "none", "untestable",
                  "new", "confirm", "suspect", "veto")


def summarize_evidence(evidence: dict, *, max_reasons: int = 3,
                       max_length: int = 300) -> str:
    """候选证据 → **有界**一行摘要（判词计数 + 前几个原因 + 跳过数）。

    用于「发现器零产出」时的 patch `note`：用户要能看到「为什么没提案」，
    但 note 会进运行详情/日志，必须限长，且只摘已脱敏的 `why`（`_why` 不回显
    SQL/标识符）。
    """
    evidence = evidence or {}
    items = [i for i in evidence.get("candidates") or [] if isinstance(i, dict)]
    skipped = [s for s in evidence.get("skipped") or [] if isinstance(s, dict)]
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get("verdict") or "unknown")
        counts[key] = counts.get(key, 0) + 1

    parts: list[str] = []
    reason = evidence.get("reason")
    if reason:
        parts.append(f"原因={str(reason)[:40]}")
    truncation = evidence.get("truncation_reason")
    if truncation:
        parts.append(f"截断={str(truncation)[:40]}")
    if counts:
        ordered = [k for k in _VERDICT_ORDER if k in counts]
        ordered += sorted(k for k in counts if k not in _VERDICT_ORDER)
        parts.append("判词 " + " ".join(f"{k}:{counts[k]}" for k in ordered))

    reasons: list[str] = []
    for item in [*items, *skipped]:
        why = " ".join(str(item.get("why") or "").split())
        if why and why not in reasons:
            reasons.append(why[:80])
        if len(reasons) >= max_reasons:
            break
    if reasons:
        parts.append("候选原因 " + "；".join(reasons))
    if skipped:
        parts.append(f"跳过候选 {len(skipped)} 条")
    if not parts:
        parts.append("无候选")
    return "｜".join(parts)[:max_length]


# ------------------------------------------------------------------ 主流程
async def discover_relations(tenant_id: str, ds: str | None, row: dict,
                             request: dict | None = None, *,
                             sampler=None, engine: str | None = None,
                             clock=time.monotonic) -> dict:
    """在编译 schema 行上跑一轮 join 边候选发现（只读）。

    参数：
    - `sampler`：`async def(sql) -> list[list]`。缺省用
      `upstream.truth_runner_for(tenant_id, ds)`（只读凭据，绝不回退写凭据）；
    - `engine`：数据库引擎。两者都不给时才去 `DataSourceService.query_config`
      解析（`no_readonly_creds` → 无条目 + reason，不抛异常）；
    - `clock`：单调时钟（测试注入，超时按「候选之间」检查）。

    返回 `{"entries": [...], "evidence": {...}}`：`entries` 只含无歧义 new
    候选；任何一步失败只影响该候选（fail-open）。
    """
    request = request or {}
    evidence: dict = {
        "engine": "",
        "sample_n": _int_option(request, "sample_n",
                                RELATION_DISCOVERY_SAMPLE_N),
        "max_candidates": _int_option(request, "max_candidates",
                                      RELATION_DISCOVERY_MAX_CANDIDATES),
        "max_queries": _int_option(request, "max_queries",
                                   RELATION_DISCOVERY_MAX_QUERIES),
        "timeout_seconds": relation_discovery_timeout(
            request.get("timeout_seconds")),
        "queries": 0,
        "truncated": False,
        "truncation_reason": None,
        "reason": None,
        "candidates": [],
        "skipped": [],
    }
    if not ds:
        evidence["reason"] = REASON_NO_DATA_SOURCE
        return _empty_result(evidence)

    if engine is None and sampler is None:
        # 只读凭据与引擎同源（`query_config` 拿不到只读配置就返回 None）。
        from ..data_source_service import DataSourceService

        try:
            resolved = await DataSourceService().query_config(tenant_id, ds)
        except Exception as exc:  # noqa: BLE001 —— 发现失败不掀翻整次运行
            logger.warning("relation discovery 解析数据源失败 ds=%s: %s", ds, exc)
            evidence["reason"] = REASON_ENGINE_LOOKUP_FAILED
            evidence["error_class"] = ERROR_ENGINE_LOOKUP_FAILED
            # 这一步没有跑任何采样查询，文案不能是「采样查询失败」。
            evidence["why"] = f"{type(exc).__name__}: 数据源解析失败"
            return _empty_result(evidence)
        if not resolved:
            evidence["reason"] = REASON_NO_READONLY_CREDS
            return _empty_result(evidence)
        source, _cfg = resolved
        engine = getattr(source, "engine", "") or ""
    if sampler is None:
        sampler = truth_runner_for(tenant_id, ds)

    dialect = _dialect(engine or "")
    evidence["engine"] = str(engine or "")
    if not dialect:
        evidence["reason"] = REASON_UNKNOWN_ENGINE
        return _empty_result(evidence)

    entities = [e for e in (row or {}).get("entity_types") or []
                if isinstance(e, dict)]
    relations = [r for r in (row or {}).get("relation_types") or []
                 if isinstance(r, dict)]
    candidates, skipped = suffix_candidates(
        entities, relations, picks=request.get("picks"))
    evidence["skipped"] = skipped

    max_candidates = evidence["max_candidates"]
    truncated_reason = None
    if len(candidates) > max_candidates:
        candidates = candidates[:max_candidates]
        truncated_reason = TRUNCATED_MAX_CANDIDATES

    by_name = _entity_index(entities)
    sample_n = evidence["sample_n"]
    max_queries = evidence["max_queries"]
    entries: list[dict] = []
    queries = 0
    started = clock()
    stopped = False
    items = evidence["candidates"]

    for candidate in candidates:
        if clock() - started > evidence["timeout_seconds"]:
            truncated_reason = truncated_reason or TRUNCATED_TIMEOUT
            stopped = True
            break
        planned = (1 + len(candidate["targets"])
                   if candidate["kind"] == "new" else 2)
        if queries + planned > max_queries:
            truncated_reason = truncated_reason or TRUNCATED_MAX_QUERIES
            stopped = True
            break

        if candidate["kind"] == "review":
            item = await _probe_review(
                sampler, candidate, by_name, dialect, sample_n)
            queries += item.pop("_queries")
            items.append(item)
            continue

        item, found, used = await _probe_new(
            sampler, candidate, by_name, dialect, sample_n)
        queries += used
        items.extend(item)
        entries.extend(found)

    evidence["queries"] = queries
    evidence["truncated"] = bool(stopped or (truncated_reason is not None))
    evidence["truncation_reason"] = truncated_reason
    evidence["entries"] = len(entries)
    evidence["elapsed_s"] = max(0.0, clock() - started)
    return {"entries": entries, "evidence": evidence}


async def _probe_review(sampler, candidate: dict, by_name: dict, dialect: str,
                        sample_n: int) -> dict:
    """复核一条 derived 边：采样两侧值 → {sampled, hits, ratio, verdict}。"""
    source_entity = by_name.get(str(candidate.get("from") or "")) or {}
    target_entity = by_name.get(str(candidate.get("to") or "")) or {}
    base = {"kind": "review", "source": candidate.get("from"),
            "source_column": candidate.get("from_column"),
            "target": candidate.get("to"),
            "target_column": candidate.get("to_column")}
    used = 0
    try:
        # 先过标识符白名单（`_sample_sql` 内校验），再计数/发查询：非法标识符
        # 不消耗预算、不产生任何 SQL。计数在发查询**之前**：失败的那一变同样
        # 消耗预算（否则一个总报错的候选可以绕过 max_queries 无限重试）。
        source_sql = _sample_sql(
            dialect, _table_of(source_entity),
            _physical_column(source_entity, candidate["from_column"]), sample_n)
        used += 1
        source_values = await _fetch_values(sampler, source_sql)
        target_sql = _sample_sql(
            dialect, _table_of(target_entity),
            _physical_column(target_entity, candidate["to_column"]), sample_n)
        used += 1
        target_values = await _fetch_values(sampler, target_sql)
    except Exception as exc:  # noqa: BLE001 —— 单条候选失败不掀翻整跑
        return {**base, "sampled": 0, "hits": 0, "ratio": 0.0,
                "verdict": "error", "confidence": "none",
                "error_class": _error_class(exc), "why": _why(exc),
                "_queries": used}

    sampled = len(source_values)
    target_set = set(target_values)
    hits = sum(1 for value in source_values if value in target_set)
    ratio = (hits / sampled) if sampled else 0.0
    is_pk = candidate["to_column"] == _target_pk(target_entity)
    return {**base, "sampled": sampled, "hits": hits, "ratio": ratio,
            "verdict": verdict("review", ratio, hits, sampled=sampled),
            "confidence": confidence(ratio, sampled, bool(is_pk)),
            "_queries": used}


async def _probe_new(sampler, candidate: dict, by_name: dict, dialect: str,
                     sample_n: int) -> tuple[list[dict], list[dict], int]:
    """探测一条 new 候选：源列采样一次，短名单逐个采样对端主键。

    返回 `(evidence_items, entries, used_queries)`。目标列 = 目标实体主键
    （`_target_pk`，候选生成时已保证可识别）；`str(v)` 逐字比较。
    """
    source_entity = by_name.get(str(candidate.get("from") or "")) or {}
    base = {"kind": "new", "source": candidate.get("from"),
            "source_column": candidate.get("from_column")}
    used = 0
    try:
        # 非法标识符在这里就抛（不计数、不发查询，`why` 不回显标识符）。
        source_sql = _sample_sql(
            dialect, _table_of(source_entity),
            _physical_column(source_entity, candidate["from_column"]), sample_n)
        used += 1                          # 发出即计数（失败同样消耗预算）
        source_values = await _fetch_values(sampler, source_sql)
    except Exception as exc:  # noqa: BLE001
        return ([{**base, "sampled": 0, "hits": 0, "ratio": 0.0,
                  "verdict": "error", "confidence": "none",
                  "error_class": _error_class(exc), "why": _why(exc)}], [], used)

    sampled = len(source_values)
    if sampled == 0:
        # 列全空/全 NULL：不是否决证据，单列 untestable 档。
        return ([{**base, "sampled": 0, "hits": 0, "ratio": 0.0,
                  "verdict": "untestable", "confidence": "none",
                  "why": "源列没有可采样值"}], [], used)

    items: list[dict] = []
    results: list[dict] = []
    target_errors = 0
    for target_name in candidate["targets"]:
        target_entity = by_name.get(target_name) or {}
        target_pk = _target_pk(target_entity)
        if not target_pk:                     # 候选生成时已过滤，防御性跳过
            continue
        try:
            target_sql = _sample_sql(
                dialect, _table_of(target_entity),
                _physical_column(target_entity, target_pk), sample_n)
            used += 1                      # 发出即计数（失败同样消耗预算）
            target_values = await _fetch_values(sampler, target_sql)
        except Exception as exc:  # noqa: BLE001
            target_errors += 1
            items.append({**base, "target": target_name,
                          "target_column": target_pk, "sampled": sampled,
                          "hits": 0, "ratio": 0.0, "verdict": "error",
                          "confidence": "none",
                          "error_class": _error_class(exc), "why": _why(exc)})
            continue
        target_set = set(target_values)
        hits = sum(1 for value in source_values if value in target_set)
        results.append({"target": target_name, "target_column": target_pk,
                        "hits": hits,
                        "ratio": (hits / sampled) if sampled else 0.0})

    if not results:
        return items, [], used

    best = results[0]
    for result in results[1:]:
        if result["ratio"] > best["ratio"]:
            best = result
    # 身份比较排除 best（results 里每个 dict 都是独立对象）。
    other_hits = [f"{r['target']}({r['ratio']:.2f})" for r in results
                  if r is not best and r["hits"] > 0]
    if target_errors:
        # 有目标没测成 → 无法确认「唯一命中」。宁可不提案，也不能把一条
        # 未经完整验证的候选当无歧义 new（保守方向：漏提 > 错提）。
        judgement = "error"
        confidence_label = "none"
        extra = {"error_class": ERROR_QUERY_FAILED,
                 "why": "目标采样失败，未判定唯一命中"}
    else:
        judgement = verdict("new", best["ratio"], best["hits"], sampled=sampled,
                            extra_hits=len(other_hits))
        confidence_label = confidence(best["ratio"], sampled, True)
        extra = {}
    items.append({**base, "target": best["target"],
                  "target_column": best["target_column"], "sampled": sampled,
                  "hits": best["hits"], "ratio": best["ratio"],
                  "verdict": judgement,
                  "confidence": confidence_label,
                  "other_hits": other_hits[:5], "targets": results,
                  **extra})
    entries: list[dict] = []
    if judgement == "new":
        entries.append({"source": candidate["from"],
                        "source_column": candidate["from_column"],
                        "target": best["target"],
                        "target_column": best["target_column"]})
    return items, entries, used


__all__ = [
    "ERROR_ENGINE_LOOKUP_FAILED",
    "ERROR_IDENTIFIER_REJECTED",
    "ERROR_QUERY_FAILED",
    "ERROR_TIMEOUT",
    "IdentifierRejected",
    "MAX_SHORTLIST",
    "MIN_SAMPLE_STRONG",
    "RELATION_DISCOVERY_MAX_CANDIDATES",
    "RELATION_DISCOVERY_MAX_QUERIES",
    "RELATION_DISCOVERY_SAMPLE_N",
    "RELATION_DISCOVERY_TIMEOUT_ENV",
    "RELATION_DISCOVERY_TIMEOUT_SECONDS",
    "REASON_ENGINE_LOOKUP_FAILED",
    "REASON_NO_DATA_SOURCE",
    "REASON_NO_READONLY_CREDS",
    "REASON_UNKNOWN_ENGINE",
    "STRONG_RATIO",
    "confidence",
    "discover_relations",
    "relation_discovery_timeout",
    "suffix_candidates",
    "summarize_evidence",
    "verdict",
]
