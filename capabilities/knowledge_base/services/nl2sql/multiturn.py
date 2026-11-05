"""② 的多轮档：三只只读工具 + 循环（P2-2）。

设计口径（spec §二/§四；§二口径于 2026-10-02 gap1 修复修订）：
- 开局 payload 给列清单（含「像枚举」列的样本）+ **锚点实体的口径约束**（gap1：按需取对
  「模型不知道自己不知道」的口径结构性失效，试点实证 before==after、tools=0）+ **1 跳
  join 图**（E1，2026-10-07：与单次档信息面对齐——锚错现场是「模型不知道自己缺」型缺口，
  按需取结构失效）；2 跳及以上由**路径注入**给出（`NL2SQL_JOIN_PATHS` 默认开，仅当真多跳
  路径存在时注入），`list_related` 工具转为兜底；
- 工具**只读、有界、不抛**：坏输入回错误文本，让模型自己改道；
- 工具吃的是编译 schema 行 / 证据 / 关系 —— 与单次档同一批读口（不新造查询）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# 循环走**模块全局名**调用（`call_llm_with_tools` / `build_sql_system_prompt`），
# 单测用 `monkeypatch.setattr(multiturn, ...)` 换桩 —— 所以这两个名字必须是模块属性。
# 放模块顶层**不会成环**：`ontology_llm` 只依赖 `jonex_core.common.config`，
# 且同包的 `service.py` 早就顶层 import 了它的 `generate_sql`（同一层里已有先例）。
from jonex_core.common.ontology_llm import (
    build_sql_payload, build_sql_system_prompt, call_llm_with_tools,
    has_mapping_slots, payload_fingerprint, preview_has_rows)

# ⚠️ 必须用 `from jonex_core.common.config import config`（拿到 Settings **实例**），
# **不是** `from jonex_core.common import config` —— 后者拿到的是 `config` **子模块**，
# 而 `NL2SQL_*` 字段只在 Settings 实例上（模块里只有 `config = get_config()` 与
# `ENV` / `DEBUG` 两个快照）⇒ 模块形式下 `config.NL2SQL_JOIN_PATHS` 直接 AttributeError，
# 生产每次提问都会炸。全仓 5 处读配置（service/observe/runner/…）同款用实例形式。
from jonex_core.common.config import config

logger = logging.getLogger(__name__)

#: 单工具结果的字符上限（截断处加标记）。
MAX_TOOL_RESULT_CHARS = 2000

TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "list_related",
        # ⚠️ 措辞（2026-10-16 设计 §七「必须」项）：原写「Call it again on a related table to
        # go one more hop」——**在诱导模型做贪心迭代**（一跳一跳自己走），而那条路正是本线
        # 要消掉的（改为按锚点预计算路径注入）。本工具**保留作兜底**（模型仍可能要走没被
        # 预计算的边），描述改成「查某个实体的关系」口径。
        #
        # ⚠️⚠️ **措辞必须对两种开关状态都成立**：本常量**不受 `NL2SQL_JOIN_PATHS` 门控**
        # （开关关是**受支持的回退档**）。所以**不许**在这里写「路径已在 payload 里」之类 ——
        # 关档时那句话是**假的**（payload 里没有路径），会误导回退档下的模型。
        # 只描述工具**自己**做什么。
        "description": "List an entity's join edges (1-hop neighbours and the columns they join "
                       "on). Use it to look up one specific entity.",
        "parameters": {"type": "object", "properties": {"entity": {"type": "string"}},
                       "required": ["entity"]}}},
    {"type": "function", "function": {
        "name": "sample_values",
        "description": "Real value samples observed in a column (probe evidence).",
        "parameters": {"type": "object", "properties": {"entity": {"type": "string"},
                                                        "column": {"type": "string"}},
                       "required": ["entity", "column"]}}},
    {"type": "function", "function": {
        "name": "resolve_entity",
        "description": "An entity's physical table, its columns, and its library conventions.",
        "parameters": {"type": "object", "properties": {"entity": {"type": "string"}},
                       "required": ["entity"]}}},
]

#: 执行工具（A3，2026-10-28）。**不放进 `TOOL_SCHEMAS`** —— 开关关时工具清单与请求
#: 必须逐字节不变；由 `tool_schemas_for()` 按开关追加。
EXEC_TOOL_NAME = "execute_sql"
EXEC_TOOL_SCHEMA: dict = {"type": "function", "function": {
    "name": EXEC_TOOL_NAME,
    "description": "Run a read-only SELECT to inspect real data: row count, up to 20 rows, "
                   "value formats. Use it to validate filters, joins and literals before "
                   "submitting. Only tables already given/returned by tools are allowed. "
                   "Not for the final answer.",
    "parameters": {"type": "object", "properties": {"sql": {"type": "string"}},
                   "required": ["sql"]}}}


def tool_schemas_for(exec_enabled: bool) -> list[dict]:
    """按开关给出基础工具清单：关 ⇒ **原 `TOOL_SCHEMAS` 对象本身**（逐字节不变）。"""
    if not exec_enabled:
        return TOOL_SCHEMAS
    return [*TOOL_SCHEMAS, EXEC_TOOL_SCHEMA]


@dataclass(frozen=True)
class MultiturnContext:
    """循环与工具的只读读料（全部来自**同一行**编译 schema + 其 evidence）。"""
    anchor: str
    entity_types: list = field(default_factory=list)
    relation_types: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    constraints: list = field(default_factory=list)
    #: 多跳 join 路径化（2026-10-16 路径化线）：血缘边（`paths.Edge`）与「目标提示」**物理表名**
    #: （`path_targets`；2026-10-16 Task 4 实核：边池与 `search_paths` 的节点全是物理表名，
    #: 直接填实体名会让目标加成永不命中且静默无感）。
    #: 开关（`NL2SQL_JOIN_PATHS`）关时**一个都不读** —— 默认空 ⇒ 既有构造与行为零差；
    #: 由调用方（service，Task 5）用 `load_lineage_edges_from_db()` / 由候选实体名转出的物理表名填进来。
    lineage_edges: list = field(default_factory=list)
    path_targets: list = field(default_factory=list)
    #: 禁止同查表对（`forbidden.ForbiddenPair`）。空 ⇒ 所有相关行为与今天逐字节相同。
    forbidden_pairs: list = field(default_factory=list)
    #: 运行时观测的记录器（duck-typed：只认 `.record(tool, args, content)` 一个方法；
    #: `None` = 不记录 —— 既有构造与行为**零差**）。service 在 attempt 循环外创建、
    #: 跨尝试保留（观测设计 §5.3a）；本模块不认识 observe 的具体类型。
    recorder: object | None = None
    #: 执行工具（A3）：`sql -> {"columns","rows","row_count","truncated"}` 的异步闭包；
    #: service 构造（guard + 治理 + 超时）。`None` = 不注册 `execute_sql`（行为与今天相同）。
    execute_sql: Callable[[str], Awaitable[dict]] | None = None

    def find(self, name: str) -> dict | None:
        for e in self.entity_types:
            if isinstance(e, dict) and str(e.get("name") or "") == name:
                return e
        return None


def _dump(obj) -> str:
    # `default=str`：执行工具回的是**数据库原值**（Decimal/date/datetime/UUID…），
    # 不能因为类型不可序列化就把整次提问炸成 `llm_failed`（BIRD financial 实测）。
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + "…(truncated)"


def _find_or_error(ctx: MultiturnContext, entity: str) -> tuple[dict | None, dict | None]:
    e = ctx.find(entity)
    if e is None:
        return None, {"error": f"unknown entity: {entity!r} (not in this data source's schema)"}
    return e, None


def _list_related(ctx: MultiturnContext, args: dict) -> dict:
    entity = str(args.get("entity") or "").strip()
    e, err = _find_or_error(ctx, entity)
    if err:
        return err
    from .joins import eligible_joins
    plan = eligible_joins(ctx.relation_types, ctx.entity_types, entity,
                          forbidden_pairs=ctx.forbidden_pairs)
    # 续档（2026-10-02）：工具结果**回带实体名** —— 出参原是物理表名（`dbo.company`），
    # 而入参要的是实体名（`Company`），模型续跳时得自己猜（实测它会猜 `dbo.company`
    # 或小写表名然后放弃）。这里把 `source_object_id → name` 的映射一并给它。
    by_table = {str(x.get("source_object_id") or "").strip(): str(x.get("name") or "")
                for x in ctx.entity_types if isinstance(x, dict)}
    return {"entity": entity,
            "joins": [{"to_table": j.get("to_table"),
                       "to_entity": by_table.get(str(j.get("to_table") or "").strip()),
                       "from_column": j.get("from_column"),
                       "to_column": j.get("to_column"), "origin": j.get("origin")}
                      for j in plan.joins],
            "related_tables": [{"table": t.get("table"),
                                "entity": by_table.get(str(t.get("table") or "").strip())}
                               for t in plan.related_tables]}


def _sample_values(ctx: MultiturnContext, args: dict) -> dict:
    entity = str(args.get("entity") or "").strip()
    column = str(args.get("column") or "").strip()
    e, err = _find_or_error(ctx, entity)
    if err:
        return err
    from ..evidence_probe import evidence_key
    from .evidence import MAX_SAMPLES_PER_COLUMN
    # 属性名（logical）与物理列名（physical）都可能被模型传来；证据按属性名登记。
    by_name: dict[str, str] = {}
    for a in (e.get("attributes") or []):
        if not isinstance(a, dict):
            continue
        logical = str(a.get("name") or "")
        if not logical:
            continue
        by_name[logical] = logical
        if a.get("physical_name"):
            by_name[str(a["physical_name"])] = logical
    if column not in by_name:
        return {"note": f"column {column!r} is not on entity {entity!r}"}
    logical_column = by_name[column]
    ev = ((ctx.evidence or {}).get("tables") or {}).get(evidence_key(e)) or {}
    col = (ev.get("columns") or {}).get(logical_column) or {}
    samples = list(col.get("samples") or [])[:MAX_SAMPLES_PER_COLUMN]
    if not samples:
        return {"note": "no samples (never probed / high-cardinality / empty in sample)"}
    return {"samples": samples, "distinct_in_sample": col.get("distinct_in_sample")}


def _resolve_entity(ctx: MultiturnContext, args: dict) -> dict:
    entity = str(args.get("entity") or "").strip()
    e, err = _find_or_error(ctx, entity)
    if err:
        return err
    from .constraints import filter_constraints
    # ⚠️ **键序即优先级**：`_dump` 在 `MAX_TOOL_RESULT_CHARS`（2000）处直接截断 —— 宽实体的
    # 列清单**自己就能超限**（Jobs 真机实测 2012 字符），排在它后面的键会被整段吃掉。
    # `constraints` 排到 `columns` 之前：它是 ≤8 条 × ≤1500 字的**人审口径**、体量小价值高，
    # 必须保；`columns` 超限被截可接受（锚点实体的列清单开局 payload 已给过）。
    return {"entity": entity,
            "table": str(e.get("source_object_id") or "").strip() or entity,
            "constraints": [str(c.get("expression") or "")
                            for c in filter_constraints(ctx.constraints, entity)],
            # 物理列名：属性名可能是 slug（`academic_year`），SQL 必须用物理名
            # （`"Academic Year"`）。没有物理名时回退属性名（存量行行为不变）。
            "columns": [{"name": str(a.get("physical_name") or a.get("name") or ""),
                         "type": str(a.get("type") or "")}
                        for a in (e.get("attributes") or []) if isinstance(a, dict)]}


_TOOLS = {"list_related": _list_related, "sample_values": _sample_values,
          "resolve_entity": _resolve_entity}


def extract_tool_overrides(schema: dict | None,
                           status: str) -> tuple[dict[str, str], set[str]]:
    """抽象 schema 行 → `(tool_overrides, disabled_tools)`（**两处消费的唯一出处**）。

    - **只认 `status == "active"`**（与 `_load_concepts` 同一口径）：未确认的覆盖不能
      驱动查询；
    - `description` **非空**才进覆盖（`enabled:false`-only 条目不得把描述清空）；
    - `enabled` **只有显式 `is False`** 才算停用（与落库归一同口径）。
    非 active / 无 `tools` → `({}, set())`。
    """
    if status != "active" or not isinstance(schema, dict):
        return {}, set()
    overrides: dict[str, str] = {}
    disabled: set[str] = set()
    for tool in schema.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        description = tool.get("description")
        if isinstance(description, str) and description.strip():
            overrides[name] = description
        if tool.get("enabled") is False:
            disabled.add(name)
    return overrides, disabled


def apply_tool_overrides(
    tools: list[dict], overrides: dict[str, str] | None = None,
    disabled: set[str] | None = None,
) -> list[dict]:
    """按 KB 覆盖块调整工具清单（演化台 `tool_schema`，D3）。

    - **无覆盖 / 无停用 → 原样返回同一对象**：`call_llm_with_tools` 的请求逐字节
      不变（不是「深拷贝后内容相等」，而是同一个 `TOOL_SCHEMAS`）；
    - 有覆盖才 `deepcopy`，只改 `function.description`，再过滤停用工具 ——
      原常量永不就地修改（并发提问共享它）；
    - 空 description 不覆盖（`enabled:false`-only 条目不得把描述清空；
      service 侧已过滤，这里再守一道）。
    """
    if not overrides and not disabled:
        return tools
    out = copy.deepcopy(tools)
    disabled = disabled or set()
    overrides = overrides or {}
    kept: list[dict] = []
    for tool in out:
        fn = tool.get("function") if isinstance(tool, dict) else None
        name = str(fn.get("name") or "") if isinstance(fn, dict) else ""
        if name in disabled:
            continue
        description = overrides.get(name)
        if description and isinstance(fn, dict):
            fn["description"] = description
        kept.append(tool)
    return kept


def run_tool(name: str, args: dict, ctx: MultiturnContext,
             disabled: set[str] | None = None) -> str:
    """执行一个工具调用 → JSON 字符串（**只读、有界、不抛**）。

    `disabled`（演化台 `tool_schema`）：被 KB 停用的工具**不执行**，回一条错误文本
    让模型改走终答 —— 提示词已声明该工具不可用，执行侧仍要守住（模型可能仍点名它；
    照常执行等于停用只停在纸面）。
    """
    tool_name = str(name or "")
    if tool_name in (disabled or set()):
        return _dump({"error": f"tool disabled: {tool_name}"})
    fn = _TOOLS.get(tool_name)
    if fn is None:
        return _dump({"error": f"unknown tool: {name!r}"})
    if not isinstance(args, dict):
        return _dump({"error": "arguments must be a JSON object"})
    try:
        return _dump(fn(ctx, args))
    except Exception as exc:  # noqa: BLE001 —— 工具坏一条不能掀翻整次提问
        return _dump({"error": f"{type(exc).__name__}: {str(exc)[:160]}"})




def _exec_record(args: dict, content: str) -> tuple[dict, str]:
    """`execute_sql` 的观测摘要：**只留 SQL hash 与计数**，不留原文/行值。

    其它工具的入参/结果照常记录（它们是 schema 元数据，不是用户数据）；探索 SQL 里
    可能带字面量、结果里是真实行——两者都不该进诊断库（审查 #2）。
    """
    sql = str((args or {}).get("sql") or "")
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        data = {}
    sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:12]
    summary: dict = {}
    if isinstance(data, dict):
        if data.get("error"):
            summary["error"] = str(data["error"])[:160]
        else:
            summary["row_count"] = data.get("row_count")
            summary["truncated"] = bool(data.get("truncated"))
    return {"sql_hash": sql_hash}, _dump(summary)


async def run_tool_async(name: str, args: dict, ctx: MultiturnContext,
                         disabled: set[str] | None = None) -> str:
    """`run_tool` 的异步版（A3）：只多一条 `execute_sql`，其余委托同步实现。

    执行工具同样**只读、有界、不抛**：没有执行器 / 执行器抛异常 / 返回非对象，
    都回一条 `{"error": ...}` 文本让模型改道。**调用上限在循环侧**（每题计数，
    见 `generate_sql_multiturn`）——这里是无状态的单次分发。
    """
    tool_name = str(name or "")
    if tool_name in (disabled or set()):
        return _dump({"error": f"tool disabled: {tool_name}"})
    if tool_name != EXEC_TOOL_NAME:
        return run_tool(tool_name, args, ctx, disabled=disabled)
    if ctx.execute_sql is None:
        return _dump({"error": "execute_sql is not available for this data source"})
    if not isinstance(args, dict):
        return _dump({"error": "arguments must be a JSON object"})
    sql = str(args.get("sql") or "").strip()
    if not sql:
        return _dump({"error": "execute_sql requires a non-empty `sql` string"})
    try:
        result = await ctx.execute_sql(sql)
    except Exception as exc:  # noqa: BLE001 —— 执行失败不能掀翻整次提问
        return _dump({"error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    if not isinstance(result, dict):
        return _dump({"error": "execute_sql returned an invalid result"})
    return _dump(result)


async def generate_sql_multiturn(
    question: str, *, ctx: MultiturnContext, engine: str, table_name: str,
    columns: list[dict], max_rounds: int = 3, max_tool_calls_per_round: int = 3,
    tenant_id: str | None = None, kb_id: str | None = None, user_id: str | None = None,
    concept_fields: list[dict] | None = None,
    projection_contract: bool = False,
    hints: list[str] | None = None,
    value_aliases: list[dict] | None = None,
    plan_text: str | None = None,
    schema_preview: list[dict] | None = None,
    preview_details: bool = False,
    rank_hint: bool = False,
    per_group_avg: bool = False,
    exact_fields: bool = False,
    hint_exactness: bool = False,
    percentage_base: bool = False,
    cte_hint: bool = False,
    nullif_hint: bool = False,
    single_column: bool = False,
    date_cast: bool = False,
    answer_format: list[str] | None = None,
    superlative_limit: bool = False,
    null_order: bool = False,
    cte_join_hint: bool = False,
    cte_joins: list[dict] | None = None,
    grain_hint: bool = False,
    left_join_hint: bool = False,
    dimension_joins: list[dict] | None = None,
    few_shots: list[dict] | None = None,
    retry_note: str | None = None,
    today: str | None = None,
    tool_overrides: dict[str, str] | None = None,
    disabled_tools: set[str] | None = None,
) -> dict:
    """多轮生成：开局给列清单（含样本）+ 锚点实体的口径约束 + 1 跳 join 图（E1），
    2 跳及以上由**路径注入**给出（`NL2SQL_JOIN_PATHS` 默认开）、工具兜底。

    返回 `{"sql"}` 或 `{"not_answerable", "reason"}`，并**回带 `meta`**
    （`rounds` / `tool_calls` / `tools_used`，给审计与诊断）与 `payload_version`
    （开局 payload + 规则版本的 sha256 指纹；调用方只送观测，不透给终端用户）。
    坏终答与 `generate_sql` **同口径**：抛 `RuntimeError`（由调用方归 `llm_failed`）。
    `concept_fields`（TBox 落点线）：抽象字段落点的「概念 → 多列」映射，非空才进开局
    payload / 才渲染 4e 段；None/[] 时 payload 与提示词与今天逐字节相同。
    `retry_note`（B 重试）：上次执行失败时的反馈文本，非空时附在**首条 user 消息尾部**；
    None 时不加（payload 与今天逐字节相同）。
    `tool_overrides` / `disabled_tools`（演化台 `tool_schema`，D3）：按 KB 覆盖工具
    描述 / 停用工具；**两者都为空时直接使用原 `TOOL_SCHEMAS` 对象**（请求逐字节不变）。
    `value_aliases`（值同义词 P1，2026-11-04）：问句词面命中的 KB 级值写法
    （`[{"term", "canonical"}]`），非空才进开局 payload 末位并渲染提示词 4x；
    `None` / `[]` ⇒ payload 与提示词与引入前逐字节相同。归一 / 空值丢弃由
    `build_sql_payload` 负责，提示词标志直接看**归一后 payload 里有没有该键**
    （避免「提示词说有条目、payload 却没有」）。
    """
    # 有效工具清单（D3）：无覆盖时就是原常量本身；有覆盖才 deepcopy。在函数开头算
    # 一次 —— 正常回合与「预算用尽补轮」两处 `call_llm_with_tools` 必须同源。
    # A3：执行工具**只在开关开且真有执行器时**才进基础清单；关时 `tool_schemas_for`
    # 返回原 `TOOL_SCHEMAS` 对象本身 ⇒ 请求逐字节不变。
    exec_enabled = bool(config.NL2SQL_EXEC_TOOL) and ctx.execute_sql is not None
    effective_tools = apply_tool_overrides(
        tool_schemas_for(exec_enabled), tool_overrides, disabled_tools)
    # KB 演化可以停用 execute_sql —— 提示词与执行侧都必须按**实际存在**的工具渲染。
    exec_offered = exec_enabled and any(
        str(t.get("function", {}).get("name") or "") == EXEC_TOOL_NAME
        for t in effective_tools if isinstance(t, dict))
    # 系统提示词里的工具清单（审查 Important #1）：**只在真有覆盖/停用/执行工具时**给
    # `tool_specs`（有效工具名 + 有效描述），否则保持 None ⇒ 提示词逐字节不变。
    # 否则提示词会点名已被过滤掉的工具，模型写出 DSML 文本 → `llm_failed`。
    tool_specs: list[tuple[str, str]] | None = None
    if tool_overrides or disabled_tools or exec_offered:
        tool_specs = [
            (str(t["function"].get("name") or ""),
             str(t["function"].get("description") or ""))
            for t in effective_tools
            if isinstance(t, dict) and isinstance(t.get("function"), dict)
        ]
    # 开局 payload 带**锚点实体**的口径约束（gap1 修复，2026-10-02）：与单次档同一
    # `filter_constraints`（只锚点实体、≤8 条 / ≤1500 字）、**非空才加键**（一条都没有时
    # payload 与旧版逐字节相同）。为什么不像样本那样留给工具：口径是「模型不知道自己
    # 不知道」——码表方向猜不出，不铺到眼前就不会被用（试点实证 before==after 3/3、审计 tools=0）。
    #
    # E1（2026-10-07）：**1 跳 join 图进开局**，与单次档**同源**（同一个 `eligible_joins`，
    # 入参同为该源那一行的原始两列）——锚错现场实证同一道理（锚=WorkingLifes 时模型不知道
    # 自己缺 `working_lifes_id`，3/3 猜 `working_life_id` 撞 207；单次档同现场带图 3/3 过）。
    # 0 边实体键不出现、payload 与旧版逐字节相同（`test_payload_is_byte_identical_when_anchor_has_no_edges`）。
    from .constraints import filter_constraints
    from .joins import all_join_edges, eligible_joins
    from .paths import search_paths
    constraints = filter_constraints(ctx.constraints, ctx.anchor)
    plan = eligible_joins(ctx.relation_types, ctx.entity_types, ctx.anchor,
                          forbidden_pairs=ctx.forbidden_pairs)
    # ⚠️ **搜索锚点 = 锚点实体的物理表**，不是实体名（边池全是物理表名，见 `joins.py`）。
    # 解析提到 `if` 之外：禁止同查的提示词键也要用它（即使路径开关关着）。
    anchor_entity = ctx.find(ctx.anchor)
    anchor_obj_id = str((anchor_entity or {}).get("source_object_id") or "").strip()

    # 多跳 join 路径化（2026-10-16，开关 `NL2SQL_JOIN_PATHS` **默认开**）：**额外**注入真多跳
    # 路径（`min_hops=2`，仅当真多跳路径存在时注入）。关时 `payload_kwargs` 恒空 ⇒ payload
    # 与提示词逐字节相同。
    payload_kwargs: dict = {}
    #: 注入路径上出现过的**物理表**（供安全闸把白名单与之对齐 —— 见 service.py `allowed_tables`）。
    path_tables: set[str] = set()
    if config.NL2SQL_JOIN_PATHS:
        if not anchor_obj_id:
            # 诊断出口：退回实体名 ⇒ 路径搜索大概率恒空、不注入。留一条可分辨的日志，
            # 否则真机走查看到「没有 join_paths」时分不清是「本来就没路径」还是「命名空间又错配」。
            logger.debug(
                "nl2sql join_paths: 锚点未能解析到物理表名（anchor=%r，在 entity_types 里=%s），"
                "已退回实体名；路径搜索大概率恒空", ctx.anchor, anchor_entity is not None)
        path_plan = search_paths(
            # ⚠️⚠️ **边池必须给足**（spec §6.1）：`eligible_joins` 只返回**以锚点为端点**的 1 跳边，
            # 拿它当边池 ⇒ 池里每条边都碰锚点 ⇒ **真多跳路径结构上搜不出来**。
            # 2026-10-16 A/B 门实测正是这个后果：6 道多跳题开关开/关**逐题结果完全相同**（零增益）。
            _edges_from_joins(all_join_edges(ctx.relation_types, ctx.entity_types,
                                             forbidden_pairs=ctx.forbidden_pairs))
            + list(ctx.lineage_edges or []),
            anchor_obj_id or ctx.anchor,
            targets=list(ctx.path_targets or []),
            max_hops=config.NL2SQL_JOIN_PATHS_MAX_HOPS,
            max_paths=config.NL2SQL_JOIN_PATHS_MAX_PATHS,
            # ⚠️ **只补 1 跳边给不出的东西**：1 跳边由 `plan.joins` 照发，路径层再发一遍是重复。
            # A/B 门实测（Task 6）：长度 1 的「路径」只是把同一条边换个写法渲染，却让模型改了投影
            # （多选一列 COUNT），在行宽严格匹配的判分器上把一道**本来通过**的 1 跳题判红
            # （确定性复现 3/3）⇒ 路径只在**真多跳**时出现。
            min_hops=2,
        )
        if ctx.forbidden_pairs:
            from dataclasses import replace
            from .forbidden import drop_forbidden_paths
            path_plan = replace(
                path_plan,
                paths=drop_forbidden_paths(path_plan.paths, ctx.forbidden_pairs))
        # ⚠️ **非空才加键** —— 与 concept_fields / constraints 同款：没有多跳路径时
        # payload 与关开关时逐字节相同。
        if path_plan.paths:
            payload_kwargs["join_paths"] = _render_paths(path_plan)
            # ⚠️ **白名单必须与提示词自洽**：这些表已经渲染进 `join_paths` 交给了模型，
            # 安全闸若不放行，模型**写对了也会被拒**（2026-10-16 实测成因，Task 6C）。
            path_tables = {t for p in path_plan.paths for s in p.steps
                           for t in (s.enter_table, s.leave_table)}

    if ctx.forbidden_pairs:
        from .forbidden import prompt_note
        neighborhood = {anchor_obj_id} | {
            str(t.get("table") or "") for t in (plan.related_tables or [])}
        neighborhood |= path_tables
        note = prompt_note(ctx.forbidden_pairs, neighborhood)
        if note:
            # ⚠️ 键名是 `forbidden_note`（build_sql_payload 的参数），不是 payload 键名
            # `forbidden_pairs` —— 后者由 build_sql_payload 内部写入。
            payload_kwargs["forbidden_note"] = note

    # 相对时间（2026-10-22）：只在问句含相对时间词时由调用方给（`today` 非空才进
    # payload，键追加在尾部 —— 未传时 payload 与旧版逐字节相同）。
    if today:
        payload_kwargs["today"] = today
    payload = build_sql_payload(
        question, engine=engine, table_name=table_name, columns=columns,
        joins=plan.joins, related_tables=plan.related_tables,
        joins_truncated=plan.truncated, constraints=constraints,
        concept_fields=concept_fields, hints=hints, plan=plan_text,
            value_aliases=value_aliases,
            schema_preview=schema_preview, cte_joins=cte_joins,
            dimension_joins=dimension_joins,
            answer_format=answer_format, **payload_kwargs)
    # payload 版本指纹（Task 4）：**开局 payload** 的指纹（工具回合只追加消息、不改它），
    # 与单次档同口径。只随结果回带，由 service 送进观测 scene（不进 HTTP 响应）。
    payload_version = payload_fingerprint(payload)
    user_content = json.dumps(payload, ensure_ascii=False)
    if retry_note:
        user_content = f"{user_content}\n\n{retry_note}"
    messages: list[dict] = [
        {"role": "system", "content": build_sql_system_prompt(
            engine=engine, table_name=table_name, with_tools=True,
            with_concept_fields=bool(concept_fields),
            with_mapping_slots=has_mapping_slots(concept_fields),
            with_projection_contract=projection_contract,
            with_hints=bool(hints), with_plan=bool(plan_text),
            with_schema_preview=bool(schema_preview),
            with_preview_details=bool(preview_details),
            with_preview_rows=preview_has_rows(schema_preview),
            with_rank_hint=bool(rank_hint),
            with_per_group_avg=bool(per_group_avg),
            with_exact_fields=bool(exact_fields),
            with_hint_exactness=bool(hint_exactness),
            with_percentage_base=bool(percentage_base),
            with_cte_hint=bool(cte_hint),
            with_nullif_hint=bool(nullif_hint),
            with_single_column=bool(single_column),
            with_date_cast=bool(date_cast),
            with_answer_format=bool(answer_format),
            with_superlative_limit=bool(superlative_limit),
            with_null_order=bool(null_order),
            with_cte_join_hint=bool(cte_join_hint),
            with_cte_joins=bool(cte_joins),
            with_grain_hint=bool(grain_hint),
            with_left_join_hint=bool(left_join_hint),
            with_dimension_joins=bool(dimension_joins),
            # 判据 = **归一后 payload 里真有该键**（与 `build_sql_payload` 共用一次归一，
            # 全脏条目时不会出现「提示词说有条目、payload 却没有」）。
            with_value_aliases="value_aliases" in payload,
            few_shots=few_shots,
            with_exec_tool=exec_offered,
            tool_specs=tool_specs)},
        {"role": "user", "content": user_content},
    ]
    rounds, tool_calls, tools_used = 0, 0, []
    #: A3 执行次数（每题计数，跨回合、跨「预算用尽补轮」共享）。
    exec_calls = 0
    max_exec_calls = int(getattr(config, "NL2SQL_EXEC_TOOL_MAX_CALLS", 2) or 0)
    cap_rounds = max(1, max_rounds)

    async def _consume(reply: dict) -> None:
        """一次带工具调用的 reply → assistant + tool 消息回喂（计数 / 每回合上限 / 超限回错误文本）；
        `ctx.recorder` 非 None 时，逐条把工具调用记进观测（在回喂之前）。"""
        nonlocal tool_calls, exec_calls
        messages.append({"role": "assistant", "content": reply["content"] or "",
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"],
                                                      "arguments": c["arguments_raw"]}}
                                        for c in reply["tool_calls"]]})
        for c in reply["tool_calls"][:max_tool_calls_per_round]:
            tool_calls += 1
            tools_used.append(c["name"])
            if c["name"] == EXEC_TOOL_NAME and exec_calls >= max_exec_calls:
                content = _dump({"error": f"{EXEC_TOOL_NAME} call cap reached "
                                           f"({max_exec_calls} per question)"})
            else:
                if c["name"] == EXEC_TOOL_NAME:
                    exec_calls += 1
                content = await run_tool_async(c["name"], c["arguments"], ctx,
                                               disabled=disabled_tools)
            if ctx.recorder is not None:
                # 观测钩子：**在 append 之前**记（run_tool 的异常语义一字不动——
                # run_tool 自己不抛，坏参数/未知工具都回 `{"error": ...}` 文本）。
                # 记录器若抛异常 = 本尝试整体作废（fail-fast）：模型与观测都不留半态；
                # **勿**在此包 try/except 吞异常——模型看见结果而观测缺条，正是本线最忌的静默降级。
                if c["name"] == EXEC_TOOL_NAME:
                    rec_args, rec_content = _exec_record(c["arguments"], content)
                    ctx.recorder.record(c["name"], rec_args, rec_content)
                else:
                    ctx.recorder.record(c["name"], c["arguments"], content)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": content})
        for c in reply["tool_calls"][max_tool_calls_per_round:]:
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps({"error": "per-round tool-call cap reached"},
                                                   ensure_ascii=False)})

    while rounds < cap_rounds:
        rounds += 1
        # ⚠️ **全程 `tool_choice="auto"`**（续档 2026-10-02 实测订正，两版都试过）：
        # 原稿末轮撤 tools（逼出终答）——模型仍想调工具，把调用写成 DSML 文本 → 解析必炸；
        # 改 `tool_choice="none"`（工具声明可见、调用被禁）——**照样写 DSML 文本**。
        # 结论：「想调却不能调」是 DSML 的来源，唯一稳的走法是让它调（有界地调）。
        reply = await call_llm_with_tools(
            messages, tools=effective_tools, tool_choice="auto",
            max_tokens=int(config.NL2SQL_MULTITURN_LLM_MAX_TOKENS),
            tenant_id=tenant_id, kb_id=kb_id, user_id=user_id)
        if not reply["tool_calls"]:
            out = _parse_final(reply["content"])
            out["meta"] = {"rounds": rounds, "tool_calls": tool_calls, "tools_used": tools_used,
                           "path_tables": sorted(path_tables)}
            out["payload_version"] = payload_version
            return out
        if rounds >= cap_rounds:
            # 预算用尽仍要调工具：**接住它的调用、回喂结果，再补一次收尾**（仍 auto）。
            # 收尾前给一条**明确的「现在必须作答」指令**（续档实测：模型会一直「再查一步」，
            # 而禁工具/撤 tools 都会让它把调用写成 DSML 文本 —— 只有「保留工具 + 明说没轮次了」
            # 这条既不断工具、又能催出终答）。
            # 轮数上界 = `max_rounds + 1`；补轮仍是工具调用 → 抛（真没辙，有界）。
            await _consume(reply)
            messages.append({"role": "user", "content": json.dumps(
                {"note": "No tool rounds left — answer NOW: output the final JSON "
                         "(sql or not_answerable) using the information you already have."},
                ensure_ascii=False)})
            rounds += 1
            reply = await call_llm_with_tools(
                messages, tools=effective_tools, tool_choice="auto",
                max_tokens=int(config.NL2SQL_MULTITURN_LLM_MAX_TOKENS),
                tenant_id=tenant_id, kb_id=kb_id, user_id=user_id)
            if not reply["tool_calls"]:
                out = _parse_final(reply["content"])
                out["meta"] = {"rounds": rounds, "tool_calls": tool_calls,
                               "tools_used": tools_used, "path_tables": sorted(path_tables)}
                out["payload_version"] = payload_version
                return out
            break
        await _consume(reply)
    raise RuntimeError("多轮生成未在 max_rounds 内出终答")


def _parse_final(content: str) -> dict:
    """与 `generate_sql` 的解析同口径：空正文 / 非 JSON / 无字段 → RuntimeError。

    ⚠️ 续档（2026-10-02）：多轮档的终答**常带一句前置话**（"Confirmed: …  {json}"）——
    实测（top5 题）整串 `json.loads` 直接炸、整次提问归 llm_failed。这里**先整串、再退一步
    取首个 `{` 到末个 `}` 的子串**（只对多轮档；单次档 `generate_sql` 的解析保持严格）。
    """
    if not content:
        raise RuntimeError("多轮生成：模型返回空正文")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(content[start:end + 1])
            except json.JSONDecodeError:
                raise RuntimeError(f"多轮生成：正文不是合法 JSON（{exc}）") from exc
        else:
            raise RuntimeError(f"多轮生成：正文不是合法 JSON（{exc}）") from exc
    if not isinstance(data, dict):
        raise RuntimeError("多轮生成：正文不是 JSON 对象")
    if data.get("not_answerable"):
        return {"not_answerable": True, "reason": str(data.get("reason") or "用这些列答不了")}
    sql = str(data.get("sql") or "").strip()
    if not sql:
        raise RuntimeError("多轮生成：既没有 sql 也没有 not_answerable")
    return {"sql": sql}


def _edges_from_joins(joins: list[dict]) -> list:
    """join 边 dict（`JoinPlan.joins` / `all_join_edges` 同形状）→ `Edge`（join 型）。"""
    from .paths import EDGE_JOIN, Edge
    return [Edge(type=EDGE_JOIN, from_table=j["from_table"], from_column=j["from_column"],
                 to_table=j["to_table"], to_column=j["to_column"],
                 origin=str(j.get("origin") or "")) for j in joins or []]


def _render_paths(path_plan) -> dict:
    """路径 → payload 片段。带**换乘说明**与**可解释截断**。"""
    return {
        "paths": [
            {
                "steps": [
                    {"type": s.edge.type, "from_table": s.enter_table,
                     "from_column": s.enter_column, "to_table": s.leave_table,
                     "to_column": s.leave_column, "origin": s.edge.origin,
                     "reversed": s.reversed}
                    for s in p.steps
                ],
                "transfers": p.transfers,
                "hits_target": p.hits_target,
            }
            for p in path_plan.paths
        ],
        "truncated": path_plan.truncated,
        "explored": path_plan.explored,
    }
