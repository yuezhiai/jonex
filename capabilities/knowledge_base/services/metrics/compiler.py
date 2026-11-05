# -*- coding: utf-8 -*-
"""指标编译器（W3.3 M2）：抽象指标定义 + 编译 schema → PostgreSQL SQL。

M2 边界（刻意划死）：
- **只出 PostgreSQL**；`dialect` 参数是 M5 扩方言的挂点，非 postgres 一律拒绝；
- 只编译 SIMPLE / RATIO / DERIVED。CUMULATIVE 与 `non_additive_dim` 在 M2
  结构化拒绝（M5 再实现时间轴/半可加编译）；
- **不执行、不接问答链路**：影子对比在 M4，执行 helper 在 `executor.py`；
- 多个输入指标**先各自聚合再 FULL OUTER JOIN**（fan-out 不放大），
  RATIO 分母用 `NULLIF(…, 0)` 兜零。

安全性：所有表达式都过 sqlglot 解析（拒绝子查询/表引用），标识符逐列解析到
编译 schema 的物理列并重新渲染 —— 绝不把定义里的文本直接拼进 SQL。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from jonex_core.common.config import config

from ..engines.base import get_profile
from .executable import GRANULARITIES, normalize_executable, validate_executable

_VALUE = "v"
_TABLE_ALIAS = "t"
_MAX_DEPTH = 8


class MetricCompileError(Exception):
    """编译失败；`code` 是稳定原因码，供观测/影子对比归因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CompiledMetric:
    sql: str
    tables: frozenset[str]
    inputs: tuple[str, ...]
    metric_type: str


def compile_metric(
    definition: dict,
    *,
    scope: dict[str, dict],
    compiled_row: dict | None,
    data_source_id: str,
    group_by: list[str] | None = None,
    max_rows: int | None = None,
    dialect: str = "postgres",
    key: str | None = None,
    time_grain: str | None = None,
    offset: int | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
) -> CompiledMetric:
    """把一条抽象指标编译成一条只读 SELECT（值列别名恒为 `v`）。

    `scope` 是**同作用域**的指标名 → 定义映射（抽象 schema 用 `name`，口径卡
    用 `code`），供 RATIO/DERIVED 解析输入指标；调用方负责把目标指标也放进去。
    """
    profile = get_profile(dialect)
    target_dialect = profile.capabilities.dialect
    if not target_dialect:
        raise MetricCompileError(
            "unsupported_dialect", f"暂不支持方言 {dialect}")
    if offset is not None and (not isinstance(offset, int)
                               or isinstance(offset, bool) or offset < 0):
        raise MetricCompileError("metric_invalid", "offset 必须是非负整数")
    if offset and profile.id == "sqlserver":
        raise MetricCompileError("unsupported_pagination",
                                 "SQL Server 分页需要 ORDER BY，M4.2 暂不支持")
    time_from = _parse_time_bound(time_from, "time_from")
    time_to = _parse_time_bound(time_to, "time_to")
    if not isinstance(definition, dict):
        raise MetricCompileError("metric_invalid", "指标定义必须是对象")
    executable = definition.get("executable")
    if not isinstance(executable, dict):
        raise MetricCompileError("metric_not_executable", "指标没有可执行定义")
    executable = normalize_executable(executable)
    issues = validate_executable(executable, additivity=definition.get("additivity"))
    if issues:
        raise MetricCompileError("metric_invalid", "；".join(issues[:3]))
    metric_type = executable["metric_type"]
    dims = [str(dim).strip() for dim in (group_by or []) if str(dim).strip()]
    if len(dims) != len(set(dims)):
        raise MetricCompileError("metric_invalid", "group_by 维度不得重复")
    limit = _limit(max_rows)
    select, tables = _select_for(
        definition, executable, scope=scope or {}, compiled_row=compiled_row,
        data_source_id=data_source_id, group_by=dims, visiting=(), depth=0,
        key=key, time_grain=time_grain,
        time_from=time_from, time_to=time_to, dialect_id=profile.id,
    )
    if metric_type in ("ratio", "derived") and not profile.capabilities.full_outer_join:
        raise MetricCompileError(
            "unsupported_engine_capability",
            f"{profile.id} 不支持 FULL OUTER JOIN，RATIO/DERIVED 无法编译")
    if metric_type == "cumulative" and not profile.capabilities.window_functions:
        raise MetricCompileError(
            "unsupported_engine_capability",
            f"{profile.id} 不支持窗口函数，CUMULATIVE 无法编译")
    if metric_type == "cumulative" and target_dialect not in (
            "postgres", "clickhouse"):
        # MySQL/SQL Server 的窗口帧 interval 语义与 PG 不同，M4.2 先明确拒绝，
        # 不把方言差异伪装成执行失败。
        raise MetricCompileError(
            "unsupported_engine_capability",
            f"{profile.id} 的 CUMULATIVE 窗口语义尚未适配")
    inputs = tuple(_input_names(metric_type, executable))
    limited = select.limit(limit)
    if offset:
        limited = limited.offset(offset)
    postgres_sql = limited.sql(dialect="postgres")
    sql = (postgres_sql if target_dialect == "postgres"
           else sqlglot.transpile(postgres_sql, read="postgres",
                                  write=target_dialect)[0])
    return CompiledMetric(
        sql=sql,
        tables=frozenset(tables),
        inputs=inputs,
        metric_type=metric_type,
    )


def _limit(max_rows: int | None) -> int:
    try:
        value = int(max_rows if max_rows is not None else config.NL2SQL_MAX_ROWS)
    except (TypeError, ValueError):
        value = int(config.NL2SQL_MAX_ROWS)
    return max(1, value)


def _select_for(
    definition: dict,
    executable: dict,
    *,
    scope: dict[str, dict],
    compiled_row: dict | None,
    data_source_id: str,
    group_by: list[str],
    visiting: tuple[str, ...],
    depth: int,
    input_filter: str | None = None,
    key: str | None = None,
    time_grain: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
    dialect_id: str = "postgres",
):
    """返回 `(select, tables)`；select 的值列别名恒为 `v`，不含 LIMIT。"""
    if depth > _MAX_DEPTH:
        raise MetricCompileError("metric_invalid", "指标依赖层级过深")
    if time_from or time_to:
        if executable["metric_type"] not in ("simple", "cumulative"):
            raise MetricCompileError(
                "time_filter_unsupported",
                "时间窗目前只支持 SIMPLE/CUMULATIVE 指标")
    metric_type = executable["metric_type"]
    if metric_type == "simple":
        return _compile_simple(definition, executable, compiled_row,
                               data_source_id, group_by,
                               input_filter=input_filter,
                               time_from=time_from, time_to=time_to)
    if metric_type == "cumulative":
        return _compile_cumulative(
            definition, executable, scope=scope, compiled_row=compiled_row,
            data_source_id=data_source_id, group_by=group_by,
            time_grain=time_grain, time_from=time_from, time_to=time_to,
            dialect_id=dialect_id)
    return _compile_multi(definition, executable, scope=scope,
                          compiled_row=compiled_row,
                          data_source_id=data_source_id, group_by=group_by,
                          visiting=visiting, depth=depth, key=key)


def _compile_simple(
    definition: dict,
    executable: dict,
    compiled_row: dict | None,
    data_source_id: str,
    group_by: list[str],
    *,
    input_filter: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
):
    entity_name = _landing_entity(definition, data_source_id)
    entity = _entity(compiled_row, entity_name)
    table_id = str(entity.get("source_object_id") or "").strip()
    columns = _columns(entity)
    measure = executable["measure"]
    if measure.get("non_additive_dim") is not None:
        raise MetricCompileError(
            "unsupported_non_additive_dim",
            "半可加（non_additive_dim）编译在 M5 提供",
        )

    dim_columns = [_dim_column(dim, columns, f"{definition.get('name') or 'metric'}")
                   for dim in group_by]
    select = exp.select(
        *[column.as_(dim, quoted=True) for column, dim in zip(dim_columns, group_by)],
        _aggregate(measure, columns).as_(_VALUE),
    ).from_(exp.alias_(_table_expr(table_id), _TABLE_ALIAS))

    conditions = []
    filter_sql = measure.get("filter")
    if filter_sql:
        conditions.append(_qualify(
            _parse_expr(filter_sql, "measure.filter"), columns, "measure.filter"))
    if input_filter:
        # `inputs[].filter` 是该输入指标聚合前的行过滤；忽略它会让分子/分母
        # 静默变成全量（口径错且无报错），所以必须 AND 进同一个 WHERE。
        conditions.append(_qualify(
            _parse_expr(input_filter, "inputs.filter"), columns, "inputs.filter"))
    conditions.extend(_time_window_conditions(
        measure, columns, "measure.time_column", time_from, time_to))
    if conditions:
        select = select.where(
            conditions[0] if len(conditions) == 1 else exp.and_(*conditions))
    if dim_columns:
        select = select.group_by(*dim_columns)
    return select, {table_id}


def _compile_cumulative(
    definition: dict,
    executable: dict,
    *,
    scope: dict[str, dict],
    compiled_row: dict | None,
    data_source_id: str,
    group_by: list[str],
    time_grain: str | None,
    time_from: str | None = None,
    time_to: str | None = None,
    dialect_id: str = "postgres",
):
    """CUMULATIVE（M5a）：先按时间桶聚合，再开窗累计。

    输出两列：`"period"`（时间桶）与 `v`（累计值），按 period 升序。窗口语义：
    - `time.window`：`RANGE BETWEEN INTERVAL '<count> <unit>' PRECEDING`（日历感知）；
    - `grain_to_date`：按 `DATE_TRUNC(grain, period)` 分区，从分区起点累计。
    M5a 暂不支持额外分组维度（会与时间桶语义纠缠，留到 M5b）。
    """
    if group_by:
        raise MetricCompileError(
            "unsupported_group_by", "CUMULATIVE M5a 暂不支持额外分组维度")
    grain = str(time_grain or "day").strip().lower()
    if grain not in GRANULARITIES:
        raise MetricCompileError(
            "metric_invalid", f"time_grain 非法：{grain}（允许 "
                              + "/".join(GRANULARITIES) + "）")
    ordered = _ordered_inputs("cumulative", executable)
    item = ordered[0]
    input_name = str(item.get("name") or "")
    target = (scope or {}).get(input_name)
    if not isinstance(target, dict):
        raise MetricCompileError("metric_unresolved",
                                 f"输入指标不存在：{input_name}")
    sub_executable = target.get("executable")
    if not isinstance(sub_executable, dict):
        raise MetricCompileError("metric_not_executable",
                                 f"输入指标没有可执行定义：{input_name}")
    sub_executable = normalize_executable(sub_executable)
    issues = validate_executable(
        sub_executable, additivity=target.get("additivity"))
    if issues:
        raise MetricCompileError(
            "metric_invalid",
            f"输入指标 {input_name} 定义非法：" + "；".join(issues[:2]))
    if sub_executable["metric_type"] != "simple":
        raise MetricCompileError("metric_invalid",
                                 f"累计输入必须是 simple 指标：{input_name}")

    entity_name = _landing_entity(target, data_source_id)
    entity = _entity(compiled_row, entity_name)
    table_id = str(entity.get("source_object_id") or "").strip()
    columns = _columns(entity)
    measure = sub_executable["measure"]
    if measure.get("non_additive_dim") is not None:
        raise MetricCompileError(
            "unsupported_non_additive_dim",
            "半可加（non_additive_dim）编译在 M5 提供",
        )
    time_column = str(measure.get("time_column") or "").strip()
    physical_time = columns.get(time_column) if time_column else None
    if not physical_time:
        raise MetricCompileError(
            "metric_invalid",
            f"累计输入 {input_name} 缺少可解析的 measure.time_column")

    conditions = []
    if measure.get("filter"):
        conditions.append(_qualify(
            _parse_expr(measure["filter"], "measure.filter"), columns,
            "measure.filter"))
    if item.get("filter"):
        conditions.append(_qualify(
            _parse_expr(item["filter"], "inputs.filter"), columns,
            "inputs.filter"))
    conditions.extend(_time_window_conditions(
        measure, columns, "measure.time_column", time_from, time_to))
    period = _date_trunc(grain, exp.Column(
        this=_q(physical_time), table=exp.to_identifier(_TABLE_ALIAS)))
    base = exp.select(
        period.as_("period", quoted=True),
        _aggregate(measure, columns).as_(_VALUE),
    ).from_(exp.alias_(_table_expr(table_id), _TABLE_ALIAS))
    if conditions:
        base = base.where(
            conditions[0] if len(conditions) == 1 else exp.and_(*conditions))
    base = base.group_by(period)

    window = _cumulative_window(executable["time"], grain, dialect_id)
    outer = exp.select(
        exp.Column(this=_q("period")).as_("period", quoted=True),
        window.as_(_VALUE),
    ).from_(exp.to_table("base")).order_by(exp.Column(this=_q("period")))
    outer = outer.with_("base", as_=base)
    return outer, {table_id}


def _parse_time_bound(value: str | None, field: str) -> str | None:
    """ISO 日期/时间窗边界；只接受明确格式（不做自然语言）。"""
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        if len(text_value) == 10:
            datetime.date.fromisoformat(text_value)
        else:
            datetime.datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetricCompileError(
            "metric_invalid", f"{field} 必须是 ISO 日期/时间") from exc
    return text_value


def _time_window_conditions(measure: dict, columns: dict[str, str | None],
                            field: str, time_from: str | None,
                            time_to: str | None) -> list:
    if not time_from and not time_to:
        return []
    time_column = str(measure.get("time_column") or "").strip()
    physical = columns.get(time_column) if time_column else None
    if not physical:
        raise MetricCompileError(
            "time_filter_unavailable",
            "该指标没有可解析的 measure.time_column，不能加时间窗")
    column = exp.Column(this=_q(physical),
                        table=exp.to_identifier(_TABLE_ALIAS))
    conditions = []
    if time_from:
        conditions.append(exp.GTE(
            this=column, expression=exp.Literal.string(time_from)))
    if time_to:
        conditions.append(exp.LTE(
            this=column.copy(), expression=exp.Literal.string(time_to)))
    return conditions


def _date_trunc(grain: str, column):
    template = _parse_expr(
        f"DATE_TRUNC('{grain}', __t__)", "time_grain")
    placeholder = template.find(exp.Column)
    if placeholder is None:
        raise MetricCompileError("metric_invalid", "DATE_TRUNC 模板构造失败")
    placeholder.replace(column)
    return template


def _cumulative_window(time: dict, grain: str, dialect_id: str = "postgres"):
    value = exp.Column(this=exp.to_identifier(_VALUE))
    window = time.get("window")
    if window is not None and dialect_id == "clickhouse":
        # ClickHouse 的窗口帧只接受数值 offset；`dateTrunc` 产出 DateTime，
        # 数值 offset 按**秒**解释（真机 2026-11-01 实测：按天窗口被算成单行）。
        # 正确做法需要时间轴补齐（timespine），不在 M4.2 里猜，明确拒绝。
        raise MetricCompileError(
            "unsupported_engine_capability",
            "ClickHouse 的滚动窗口累计需要时间轴补齐，M4.2 暂不支持"
            "（grain_to_date 可用）")
    elif window is not None:
        interval = _interval_text(window)
        template = _parse_expr(
            "SUM(__v__) OVER (ORDER BY \"period\" RANGE BETWEEN "
            f"INTERVAL '{interval}' PRECEDING AND CURRENT ROW)",
            "time.window")
    else:
        grain_to_date = str(time.get("grain_to_date") or "").strip().lower()
        template = _parse_expr(
            "SUM(__v__) OVER (PARTITION BY DATE_TRUNC("
            f"'{grain_to_date}', \"period\") ORDER BY \"period\" "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
            "time.grain_to_date")
    placeholder = template.find(exp.Column)
    if placeholder is None:
        raise MetricCompileError("metric_invalid", "累计窗口模板构造失败")
    placeholder.replace(value)
    return template


def _interval_text(window: dict) -> str:
    count = int(window.get("count") or 0)
    if count < 1:
        raise MetricCompileError("metric_invalid", "窗口 count 必须 ≥1")
    granularity = str(window.get("granularity") or "").strip().lower()
    if granularity == "quarter":
        return f"{count * 3} months"
    if granularity in ("day", "week", "month", "year"):
        return f"{count} {granularity}s"
    raise MetricCompileError("metric_invalid",
                             f"窗口粒度非法：{granularity}")


def _compile_multi(
    definition: dict,
    executable: dict,
    *,
    scope: dict[str, dict],
    compiled_row: dict | None,
    data_source_id: str,
    group_by: list[str],
    visiting: tuple[str, ...],
    depth: int,
    key: str | None = None,
):
    metric_type = executable["metric_type"]
    ordered = _ordered_inputs(metric_type, executable)
    # 口径卡的 payload 没有 `name`（只有 definition 文本），成环判定必须用
    # scope key；缺省才回落到 name（抽象 schema 的 name 就是 scope key）。
    name = str(key or definition.get("name") or "")
    if name in visiting:
        raise MetricCompileError("metric_cycle", f"指标依赖成环：{name}")
    visiting = (*visiting, name)

    ctes: list[tuple[str, exp.Expression]] = []
    tables: set[str] = set()
    for index, item in enumerate(ordered):
        input_name = item["name"]
        target = scope.get(input_name)
        if not isinstance(target, dict):
            raise MetricCompileError("metric_unresolved",
                                     f"输入指标不存在：{input_name}")
        sub_executable = target.get("executable")
        if not isinstance(sub_executable, dict):
            raise MetricCompileError("metric_not_executable",
                                     f"输入指标没有可执行定义：{input_name}")
        sub_executable = normalize_executable(sub_executable)
        issues = validate_executable(
            sub_executable, additivity=target.get("additivity"))
        if issues:
            raise MetricCompileError(
                "metric_invalid",
                f"输入指标 {input_name} 定义非法：" + "；".join(issues[:2]))
        if sub_executable["metric_type"] == "cumulative":
            raise MetricCompileError("metric_type_unsupported",
                                     f"输入指标 {input_name} 是 CUMULATIVE（M5）")
        input_filter = item.get("filter")
        if input_filter and sub_executable["metric_type"] != "simple":
            # 复合指标上的行过滤没有唯一语义（要推给哪些叶 measure？），
            # M2 明确拒绝，绝不静默忽略。
            raise MetricCompileError(
                "input_filter_unsupported",
                f"输入指标 {input_name} 不是 simple，M2 不支持给复合指标加过滤器",
            )
        sub_select, sub_tables = _select_for(
            target, sub_executable, scope=scope, compiled_row=compiled_row,
            data_source_id=data_source_id, group_by=group_by,
            visiting=visiting, depth=depth + 1, input_filter=input_filter,
            key=input_name,
        )
        ctes.append((f"m{index}", sub_select))
        tables |= sub_tables

    if metric_type == "ratio":
        value = sqlglot.parse_one(
            "CAST(m0.v AS DOUBLE PRECISION) / NULLIF(m1.v, 0)", read="postgres")
    else:  # derived
        value = _substitute_metric_names(
            _parse_expr(executable["metric_expr"], "metric_expr"), ordered)

    if group_by:
        # ⚠️ 两层坑：
        # ① 不能用 `USING`/`FULL OUTER JOIN ON k = k`：NULL 分组两边配不上，
        #    会被拆成两行（静默丢一个分组）。
        # ② PG 的 FULL JOIN 只接受 merge/hash-joinable 条件，
        #    `IS NOT DISTINCT FROM` 会报 FeatureNotSupported。
        # 所以先对**分组键**做 UNION（SQL 的 UNION 天然把 NULL 视为相等并去重），
        # 再 LEFT JOIN 各输入（LEFT JOIN 允许 nested-loop 的非等值条件）。
        key_selects = [
            exp.select(*[exp.Column(this=_q(dim)).as_(dim, quoted=True)
                         for dim in group_by]).from_(exp.to_table(alias))
            for alias, _ in ctes
        ]
        keys = key_selects[0]
        for extra in key_selects[1:]:
            keys = exp.Union(this=keys, expression=extra, distinct=True)
        select = exp.select(
            *[_keys_column(dim).as_(dim, quoted=True) for dim in group_by],
            value.as_(_VALUE),
        ).from_(exp.to_table("keys"))
        for index in range(len(ctes)):
            conditions = [
                exp.NullSafeEQ(this=_keys_column(dim),
                               expression=_metric_column(index, dim))
                for dim in group_by
            ]
            select = select.join(
                exp.to_table(f"m{index}"),
                on=(conditions[0] if len(conditions) == 1
                    else exp.and_(*conditions)),
                join_type="left",
            )
        for alias, cte in ctes:
            select = select.with_(alias, as_=cte)
        select = select.with_("keys", as_=keys)
        return select, tables

    select = exp.select(value.as_(_VALUE)).from_(exp.to_table("m0"))
    for index in range(1, len(ctes)):
        select = select.join(exp.to_table(f"m{index}"), on=None,
                             join_type="cross")
    for alias, cte in ctes:
        select = select.with_(alias, as_=cte)
    return select, tables


def _ordered_inputs(metric_type: str, executable: dict) -> list[dict]:
    inputs = [item for item in executable.get("inputs") or []
              if isinstance(item, dict)]
    if metric_type == "ratio":
        by_role = {item.get("role"): item for item in inputs}
        return [by_role["numerator"], by_role["denominator"]]
    return inputs


def _input_names(metric_type: str, executable: dict) -> list[str]:
    return [str(item["name"]) for item in _ordered_inputs(metric_type, executable)]


def _substitute_metric_names(node, ordered: list[dict]):
    aliases = {str(item["name"]): f"m{index}"
               for index, item in enumerate(ordered)}
    for column in list(node.find_all(exp.Column)):
        alias = aliases.get(column.name)
        if alias is None:
            raise MetricCompileError(
                "metric_invalid", f"metric_expr 引用了非输入指标：{column.name}")
        replacement = exp.Column(this=exp.to_identifier(_VALUE),
                                 table=exp.to_identifier(alias))
        # 根节点就是标识符时没有父节点，`replace` 不会写回 —— 必须自己接手。
        if column is node:
            node = replacement
        else:
            column.replace(replacement)
    return node


def value_expression(
    definition: dict,
    *,
    scope: dict[str, dict],
    compiled_row: dict | None,
    data_source_id: str,
    visiting: tuple[str, ...] = (),
    depth: int = 0,
    input_filter: str | None = None,
    key: str | None = None,
) -> str:
    """把指标编译成**单条 SQL 表达式**（无 SELECT/FROM），供 OSI 导出。

    与 `compile_metric` 共用校验与 landing 解析，差异只在输出形状：
    - 过滤内联为 `CASE WHEN`（MetricFlow `msi_to_osi` 同口径）；
    - RATIO/DERIVED 把输入表达式**递归内联**，不建 CTE；
    - CUMULATIVE 返回其输入指标的表达式（时间语义由 OSI 扩展承载）。
    """
    if depth > _MAX_DEPTH:
        raise MetricCompileError("metric_invalid", "指标依赖层级过深")
    if not isinstance(definition, dict):
        raise MetricCompileError("metric_invalid", "指标定义必须是对象")
    executable = definition.get("executable")
    if not isinstance(executable, dict):
        raise MetricCompileError("metric_not_executable", "指标没有可执行定义")
    executable = normalize_executable(executable)
    issues = validate_executable(executable, additivity=definition.get("additivity"))
    if issues:
        raise MetricCompileError("metric_invalid", "；".join(issues[:3]))

    metric_type = executable["metric_type"]
    if metric_type == "simple":
        return _simple_value_expression(
            definition, executable, compiled_row, data_source_id,
            input_filter=input_filter)
    if input_filter:
        raise MetricCompileError(
            "input_filter_unsupported",
            "M2 不支持给复合指标加输入过滤器（请把它下沉到叶 simple 指标）",
        )

    name = str(key or definition.get("name") or "")
    if name in visiting:
        raise MetricCompileError("metric_cycle", f"指标依赖成环：{name}")
    visiting = (*visiting, name)
    ordered = _ordered_inputs(metric_type, executable)
    subs: list[str] = []
    for item in ordered:
        input_name = item["name"]
        target = (scope or {}).get(input_name)
        if not isinstance(target, dict):
            raise MetricCompileError("metric_unresolved",
                                     f"输入指标不存在：{input_name}")
        if not isinstance(target.get("executable"), dict):
            raise MetricCompileError("metric_not_executable",
                                     f"输入指标没有可执行定义：{input_name}")
        input_filter = item.get("filter")
        if input_filter:
            sub_type = (normalize_executable(target["executable"])
                        or {}).get("metric_type")
            if sub_type != "simple":
                raise MetricCompileError(
                    "input_filter_unsupported",
                    f"输入指标 {input_name} 不是 simple，M2 不支持给复合指标加过滤器",
                )
        subs.append(value_expression(
            target, scope=scope, compiled_row=compiled_row,
            data_source_id=data_source_id, visiting=visiting, depth=depth + 1,
            input_filter=input_filter, key=input_name,
        ))

    if metric_type == "cumulative":
        return subs[0]
    if metric_type == "ratio":
        return (f"(CAST(({subs[0]}) AS DOUBLE PRECISION) "
                f"/ NULLIF(({subs[1]}), 0))")

    node = _parse_expr(executable["metric_expr"], "metric_expr")
    aliases = {str(item["name"]): index
               for index, item in enumerate(ordered)}
    for column in list(node.find_all(exp.Column)):
        index = aliases.get(column.name)
        if index is None:
            raise MetricCompileError(
                "metric_invalid", f"metric_expr 引用了非输入指标：{column.name}")
        replacement = exp.Paren(
            this=sqlglot.parse_one(subs[index], read="postgres"))
        if column is node:
            node = replacement
        else:
            column.replace(replacement)
    return node.sql(dialect="postgres")


def _simple_value_expression(
    definition: dict,
    executable: dict,
    compiled_row: dict | None,
    data_source_id: str,
    *,
    input_filter: str | None = None,
) -> str:
    entity_name = _landing_entity(definition, data_source_id)
    entity = _entity(compiled_row, entity_name)
    columns = _columns(entity)
    measure = executable["measure"]
    if measure.get("non_additive_dim") is not None:
        raise MetricCompileError(
            "unsupported_non_additive_dim", "半可加（non_additive_dim）编译在 M5 提供")
    conditions = []
    if measure.get("filter"):
        conditions.append(_qualify(
            _parse_expr(measure["filter"], "measure.filter"), columns,
            "measure.filter", alias=entity_name))
    if input_filter:
        conditions.append(_qualify(
            _parse_expr(input_filter, "inputs.filter"), columns,
            "inputs.filter", alias=entity_name))
    filter_node = None
    if conditions:
        filter_node = (conditions[0] if len(conditions) == 1
                       else exp.and_(*conditions))
    aggregate = _aggregate(measure, columns, alias=entity_name,
                           filter_node=filter_node)
    return aggregate.sql(dialect="postgres")


def _landing_entity(definition: dict, data_source_id: str) -> str:
    landings = [item for item in definition.get("landings") or []
                if isinstance(item, dict)
                and str(item.get("data_source_id") or "").strip() == data_source_id]
    with_entity = [item for item in landings
                   if str(item.get("entity_name") or "").strip()]
    if with_entity:
        return str(with_entity[0]["entity_name"]).strip()
    if landings:
        raise MetricCompileError(
            "metric_not_compilable",
            "该落点只有 DI 口径（di_metric），没有可编译的物理实体",
        )
    if any(isinstance(item, dict) and item.get("data_source_id")
           for item in definition.get("landings") or []):
        raise MetricCompileError(
            "cross_source_unsupported",
            f"指标在该数据源（{data_source_id}）没有落点；M2 不支持跨源指标",
        )
    raise MetricCompileError("landing_unresolved", "指标没有登记任何落点")


def _entity(compiled_row: dict | None, entity_name: str) -> dict:
    if not isinstance(compiled_row, dict):
        raise MetricCompileError("landing_unresolved", "该数据源没有编译 schema")
    for entity in compiled_row.get("entity_types") or []:
        if not isinstance(entity, dict):
            continue
        if str(entity.get("name") or "") != entity_name:
            continue
        if not str(entity.get("source_object_id") or "").strip():
            raise MetricCompileError(
                "landing_unresolved", f"实体 {entity_name} 没有物理表名")
        return entity
    raise MetricCompileError("landing_unresolved",
                             f"编译 schema 里找不到实体 {entity_name}")


def _columns(entity: dict) -> dict[str, str | None]:
    """逻辑名/物理名 → 物理列名。两种名字都收，输出一律物理名。

    重名歧义（属性 A 的逻辑名 = 属性 B 的物理名、且指向不同列）记为 `None`，
    调用方按 `unknown_column` 硬拒 —— 属性顺序不能决定解析结果。
    """
    physical_map: dict[str, str] = {}
    logical_map: dict[str, str] = {}
    for attribute in entity.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        logical = str(attribute.get("name") or "").strip()
        physical = str(attribute.get("physical_name") or "").strip() or logical
        if physical:
            physical_map.setdefault(physical, physical)
        if logical:
            logical_map.setdefault(logical, physical)
    out: dict[str, str | None] = dict(physical_map)
    for name, target in logical_map.items():
        if name in out and out[name] != target:
            out[name] = None
        else:
            out[name] = target
    return out


def _metric_column(index: int, dim: str):
    """CTE `m{index}` 的分组列（引号化，输出别名与 join 都用它）。"""
    return exp.Column(this=_q(dim), table=exp.to_identifier(f"m{index}"))


def _keys_column(dim: str):
    """分组键 CTE（`keys`）的列。"""
    return exp.Column(this=_q(dim), table=exp.to_identifier("keys"))


def _dim_column(dim: str, columns: dict[str, str | None], metric_name: str):
    physical = columns.get(dim)
    if physical is None:
        raise MetricCompileError(
            "group_missing",
            f"分组维度 {dim} 不在指标 {metric_name} 的落点列里",
        )
    return exp.Column(this=_q(physical), table=exp.to_identifier(_TABLE_ALIAS))


def _aggregate(measure: dict, columns: dict[str, str | None], *,
               alias: str = _TABLE_ALIAS, filter_node=None):
    agg = str(measure.get("agg") or "")
    expr_text = measure.get("expr")
    if agg == "count" and (not expr_text or str(expr_text).strip() == "*"):
        if filter_node is None:
            return exp.Count(this=exp.Star())
        # `CASE WHEN cond THEN * END` 是非法 SQL；COUNT 的过滤分支必须用 1。
        return exp.Count(this=_case_when(filter_node, exp.Literal.number(1)))
    column = _qualify(_parse_expr(expr_text, "measure.expr"), columns,
                      "measure.expr", alias=alias)
    if filter_node is not None:
        # OSI 导出走单表达式：过滤条件必须内联进聚合（CASE WHEN），不能落
        # 到 WHERE（那边没有表上下文）。与 MetricFlow `msi_to_osi` 同口径。
        column = _case_when(filter_node, column)
    if agg == "sum":
        return exp.Sum(this=column)
    if agg == "avg":
        return exp.Avg(this=column)
    if agg == "min":
        return exp.Min(this=column)
    if agg == "max":
        return exp.Max(this=column)
    if agg == "count":
        return exp.Count(this=column)
    if agg == "count_distinct":
        return exp.Count(this=exp.Distinct(expressions=[column]))
    # median / percentile
    percentile = 0.5 if agg == "median" else float(measure["percentile"]) / 100.0
    template = sqlglot.parse_one(
        f"PERCENTILE_CONT({percentile}) WITHIN GROUP (ORDER BY __x__)",
        read="postgres")
    placeholder = template.find(exp.Column)
    if placeholder is None:
        raise MetricCompileError("metric_invalid", "百分位模板构造失败")
    placeholder.replace(column)
    return template


def _qualify(node, columns: dict[str, str | None], field: str, *,
             alias: str = _TABLE_ALIAS):
    """逐列解析到物理列并限定到 `alias`；未知列硬失败。"""
    for column in list(node.find_all(exp.Column)):
        physical = columns.get(column.name)
        if physical is None:
            raise MetricCompileError(
                "unknown_column", f"{field} 引用了不存在的列：{column.name}")
        replacement = exp.Column(this=_q(physical),
                                 table=_alias_identifier(alias))
        # 根节点就是列时（measure.expr 只写一个列名）没有父节点，`replace`
        # 不会写回 —— 必须自己接手，否则 SUM(amount) 会漏掉表限定与物理名。
        if column is node:
            node = replacement
        else:
            column.replace(replacement)
    return node


def _parse_expr(text, field: str):
    if not isinstance(text, str) or not text.strip():
        raise MetricCompileError("metric_invalid", f"{field} 不能为空")
    try:
        node = sqlglot.parse_one(text, read="postgres")
    except Exception:  # noqa: BLE001 —— sqlglot 的异常类型不对外稳定
        raise MetricCompileError("metric_invalid", f"{field} 不是合法的 SQL 表达式")
    if node is None or isinstance(node, (exp.Select, exp.Subquery)):
        raise MetricCompileError("metric_invalid", f"{field} 必须是标量表达式")
    if (node.find(exp.Select) or node.find(exp.Subquery)
            or node.find(exp.Join) or node.find(exp.Table)):
        raise MetricCompileError(
            "metric_invalid", f"{field} 不能包含子查询或表引用")
    return node


def _q(name: str):
    return exp.to_identifier(name, quoted=True)


def _alias_identifier(alias: str):
    """表别名：安全 ASCII 标识符不引号（`t` / `Orders`），其余加引号。

    保持 `t."col"` 的既有 golden 形状；中文实体名（OSI 导出限定符）走引号。
    """
    safe = alias.isascii() and alias.isidentifier()
    return exp.to_identifier(alias, quoted=not safe)


def _case_when(condition, value):
    """`CASE WHEN condition THEN value END`（NULL 会被聚合函数忽略）。"""
    return exp.Case(ifs=[exp.If(this=condition, true=value)])


def _table_expr(source_object_id: str):
    parts = [part for part in str(source_object_id).split(".") if part]
    if not parts:
        raise MetricCompileError("landing_unresolved", "物理表名为空")
    table = exp.Table(this=_q(parts[-1]))
    if len(parts) >= 2:
        table.set("db", _q(parts[-2]))
    if len(parts) >= 3:
        table.set("catalog", _q(parts[-3]))
    return table


__all__ = ["CompiledMetric", "MetricCompileError", "compile_metric", "value_expression"]
