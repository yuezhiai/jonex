# -*- coding: utf-8 -*-
"""W4.1 确定性指标问数（MVP）：匹配 → 编译 → 安全/治理 → 执行 → 溯源。

设计：docs/superpowers/specs/2026-11-01-w4.1-metric-ask-design.md（工作区文档，不入库）。

边界：
- **不调 LLM**、不猜意图；`group_by` / `time_grain` 由调用方显式给；
- 不改 `ask_data_source`；这是独立动作 `ask_metric`；
- 只消费 active 抽象 schema 里带 `executable` 的指标；legacy 指标明确拒绝；
- 失败一律回 `{ok:false, not_answerable:true, reason_code, reason, ...}`，不静默回退自由 SQL。
"""
from __future__ import annotations

import datetime
import logging
import time

from sqlalchemy import text

from jonex_core.common.config import config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from ..concept_expansion import match_metrics
from . import metric_call_approval
from .clarify import apply_pool_action, build_pool
from ..metrics import cache as metric_cache
from ..metrics.cell_provenance import build_cell_provenance
from . import schema_read
from .guard import guard_sql, normalize_dialect_sql
from .result_contract import attach_blocks, summary_text
from .runner import run_readonly

logger = logging.getLogger(__name__)

_MAX_GROUP_BY = 10
_MAX_SOURCES = 20
_CARD_BINDING_SQL = (
    "SELECT sa.code, sa.name, sa.version "
    "FROM data_governance.standard_asset_bindings b "
    "JOIN data_governance.standard_assets sa ON sa.id = b.standard_asset_id "
    "WHERE b.tenant_id = :tenant AND b.standard_kind = 'metric_card' "
    "AND b.asset_type = 'metric' AND b.asset_id = :asset "
    "AND b.status = 'active' AND b.is_deleted = 0 "
    "AND sa.status = 'published' AND sa.is_deleted = 0 "
    "ORDER BY sa.version DESC LIMIT 1"
)


_REJECTION_KINDS = {
    "rejected_unsafe_sql": "safety",
    "rejected_sensitive": "governance",
    "metric_not_approved": "approval",
    "approval_unavailable": "approval",
    "ambiguous_metric": "resolution",
    "ambiguous_source": "resolution",
    "ambiguous_landing": "resolution",
    "no_metric": "resolution",
    "no_landing": "resolution",
    "metric_not_executable": "resolution",
    "unsupported_dialect": "capability",
    "unsupported_engine_capability": "capability",
    "unsupported_pagination": "capability",
    "time_filter_unavailable": "capability",
    "time_filter_unsupported": "capability",
    "compile_error": "execution",
    "exec_failed": "execution",
    "no_readonly_creds": "source",
    "source_gone": "source",
    "not_a_database_source": "source",
    "provenance_unavailable": "provenance",
}

def _fail(reason_code: str, reason: str, **extra) -> dict:
    category = _REJECTION_KINDS.get(reason_code, "other")
    payload = {"ok": False, "not_answerable": True,
               "reason_code": reason_code, "reason": reason,
               "rejection_category": category}
    if "rejection_kind" not in extra:
        payload["rejection_kind"] = category
    payload.update(extra)
    return payload


def _entity_landings(metric: dict) -> list[dict]:
    return [landing for landing in metric.get("landings") or []
            if isinstance(landing, dict)
            and str(landing.get("data_source_id") or "").strip()
            and str(landing.get("entity_name") or "").strip()]


def _match_executable(metrics: list[dict], question: str) -> tuple[dict | None, dict]:
    """命中 + 可执行 + 同档歧义判定；返回 `(hit, info)`，hit 为 None 时 info 带状态。"""
    hits = match_metrics(question, metrics)
    if not hits:
        return None, {"status": "no_metric", "hits": []}
    executable = [hit for hit in hits
                  if isinstance(hit.get("metric"), dict)
                  and isinstance(hit["metric"].get("executable"), dict)]
    if not executable:
        return None, {"status": "metric_not_executable", "hits": hits}
    top = executable[0]
    tied = [hit for hit in executable[1:]
            if hit.get("match_kind") == top.get("match_kind")
            and hit.get("matched_term") == top.get("matched_term")]
    if tied:
        return None, {"status": "ambiguous_metric", "hits": [top, *tied]}
    return top, {"status": "ok", "hits": executable}


def _candidate_view(hit: dict) -> dict:
    metric = hit.get("metric") or {}
    return {
        "name": hit.get("name") or metric.get("name") or "",
        "display_name": metric.get("display_name") or metric.get("name") or "",
        "metric_type": (metric.get("executable") or {}).get("metric_type"),
        "sources": sorted({str(landing.get("data_source_id"))
                           for landing in _entity_landings(metric)}),
    }


class MetricAskMixin:
    """挂在 `Nl2SqlService` 上：复用 `_ds`、审计与治理 helper。"""

    async def clarify_metric(
        self, tenant_id: str, kb_id: str, question: str, *,
        data_source_id: str = "", pool=None, action: str = "ask",
        question_id: str = "", text: str = "", need_time: bool = False,
        user_id: str = "",
    ) -> dict:
        """显式澄清池（W4.7）：不执行、不猜；ask 重算，drop/add 编辑调用方池子。"""
        tenant_id = require_tenant(tenant_id)
        q = str(question or "").strip()
        if not q:
            raise InvalidParameterError(message="question 不能为空")
        abstract = await schema_read.load_abstract_row(tenant_id, kb_id)
        if not abstract or abstract.get("status") != "active":
            return {"ok": True, "question": q, "action": action,
                    "pool": [], "pending": False, "submit_ready": True}
        metrics = (abstract.get("schema") or {}).get("metrics")
        if not isinstance(metrics, list):
            metrics = []
        hit, info = _match_executable(metrics, q)
        metric_choices: list[dict] = []
        sources: list[str] = []
        entities: list[str] = []
        units: list = []
        if info.get("status") == "ambiguous_metric":
            metric_choices = [_candidate_view(item)
                              for item in info.get("hits") or []][:10]
        elif hit is not None:
            metric = hit["metric"]
            landings = _entity_landings(metric)
            matched = landings
            if data_source_id:
                matched = [item for item in landings
                           if item.get("data_source_id") == data_source_id]
            else:
                sources = sorted({str(item.get("data_source_id"))
                                  for item in landings})
            entities = sorted({str(item.get("entity_name"))
                               for item in matched})
            if isinstance(metric.get("units"), list):
                units = metric.get("units")
        if action == "ask":
            current = build_pool(
                metrics=metric_choices, sources=sources, entities=entities,
                units=units, need_time=need_time,
                metric_name=str(hit.get("name") or "") if hit else "",
            )
            return {"ok": True, "question": q, "action": "ask",
                    "pool": current, "pending": bool(current),
                    "submit_ready": not current}
        base = pool if pool is not None else build_pool(
            metrics=metric_choices, sources=sources, entities=entities,
            units=units, need_time=need_time,
            metric_name=str(hit.get("name") or "") if hit else "",
        )
        result = apply_pool_action(base, action,
                                   question_id=question_id, text=text)
        return {"ok": True, "question": q, **result}

    async def ask_metric(
        self, tenant_id: str, kb_id: str, question: str, *,
        data_source_id: str = "", group_by: list[str] | None = None,
        time_grain: str | None = None, limit: int | None = None,
        offset: int = 0, time_from: str | None = None,
        time_to: str | None = None, timeout: float | None = None,
        context: dict | None = None, compare: str | None = None,
        record_audit: bool = True,
        user_id: str = "", username: str = "", ip: str = "",
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        q = str(question or "").strip()
        if not q:
            raise InvalidParameterError(message="问题不能为空")
        if len(q) > config.NL2SQL_MAX_QUESTION_LENGTH:
            raise InvalidParameterError(
                message=f"问题过长（上限 {config.NL2SQL_MAX_QUESTION_LENGTH} 字）")
        if group_by is not None and not isinstance(group_by, list):
            raise InvalidParameterError(message="group_by 必须是字符串数组")
        if limit is not None and (not isinstance(limit, int)
                                  or isinstance(limit, bool) or limit < 1):
            raise InvalidParameterError(message="limit 必须是正整数")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise InvalidParameterError(message="offset 必须是非负整数")
        ctx = context if isinstance(context, dict) else {}
        if not data_source_id:
            data_source_id = str(ctx.get("data_source_id") or "")
        if group_by is None:
            group_by = ctx.get("group_by")
        if time_grain is None:
            time_grain = ctx.get("time_grain")
        if time_from is None:
            time_from = ctx.get("time_from")
        if time_to is None:
            time_to = ctx.get("time_to")
        context_metric = str(ctx.get("metric") or "").strip()
        if compare is not None and compare not in (
                "previous_period", "yoy", "mom"):
            raise InvalidParameterError(
                message="compare 必须是 previous_period/yoy/mom")
        if compare and not (time_from and time_to):
            raise InvalidParameterError(
                message="对比需要同时提供 time_from 与 time_to")
        dims = [str(item).strip() for item in (group_by or [])
                if str(item).strip()]
        if len(dims) > _MAX_GROUP_BY:
            raise InvalidParameterError(
                message=f"group_by 最多 {_MAX_GROUP_BY} 个")

        def fail(reason_code: str, reason: str, *, sql: str | None = None,
                 rejected_sql: str | None = None, metric: str = "",
                 extra: dict | None = None, **payload) -> dict:
            request_extra = {"mode": "metric"}
            if metric:
                request_extra["metric"] = metric
            request_extra.update(extra or {})
            if record_audit:
                self._audit_failed(
                    tenant_id, user_id, username, ip, data_source_id or "",
                    q, reason_code, reason, sql=sql,
                    rejected_sql=rejected_sql, extra=request_extra,
                )
            if sql:
                payload["sql"] = sql
            if rejected_sql:
                payload["rejected_sql"] = rejected_sql
            return _fail(reason_code, reason, **payload)

        abstract = await schema_read.load_abstract_row(tenant_id, kb_id)
        if not abstract or abstract.get("status") != "active":
            return fail("no_metric", "没有可用的 active 抽象 schema")
        metrics = (abstract.get("schema") or {}).get("metrics")
        if not isinstance(metrics, list) or not metrics:
            return fail("no_metric", "该知识库没有登记指标")

        if context_metric:
            target_metric = next(
                (item for item in metrics if isinstance(item, dict)
                 and str(item.get("name") or "") == context_metric), None)
            if not isinstance(target_metric, dict) or not isinstance(
                    target_metric.get("executable"), dict):
                return fail("no_metric",
                            f"context.metric 不存在或不可执行：{context_metric}")
            hit = {"name": context_metric, "match_kind": "context",
                   "matched_term": context_metric, "metric": target_metric}
            info = {"status": "ok", "hits": [hit]}
        else:
            hit, info = _match_executable(metrics, q)
        if hit is None:
            status = info["status"]
            if status == "metric_not_executable":
                first = info["hits"][0]
                return fail("metric_not_executable",
                            "命中的指标没有可执行定义",
                            metric=str(first.get("name") or ""),
                            candidates=[_candidate_view(item)
                                        for item in info["hits"][:10]])
            if status == "ambiguous_metric":
                return fail(
                    "ambiguous_metric", "命中多个可执行指标，需要点名",
                    candidates=[_candidate_view(item)
                                for item in info["hits"][:10]],
                    clarifications=build_pool(
                        metrics=[_candidate_view(item)
                                 for item in info["hits"][:10]]),
                )
            return fail("no_metric", "没有命中任何指标")

        metric = hit["metric"]
        metric_name = str(metric.get("name") or "")
        metric_type = str((metric.get("executable") or {}).get("metric_type")
                          or "")
        if config.METRIC_CALL_APPROVAL_ENABLED:
            try:
                approval_status = await metric_call_approval.status_for(
                    tenant_id, kb_id, metric_name)
            except Exception as exc:  # noqa: BLE001 —— 闸门开启时失败即拒绝
                logger.warning("[metric-ask] 审批读取失败（拒绝调用）: %s", exc)
                return fail("approval_unavailable",
                            "指标调用审批不可用", metric=metric_name)
            if approval_status != "approved":
                return fail(
                    "metric_not_approved",
                    f"指标未获调用审批（{approval_status}）",
                    metric=metric_name, approval_status=approval_status,
                )
        landings = _entity_landings(metric)
        if not landings:
            return fail("no_landing", "指标没有可编译的实体落点",
                        metric=metric_name)
        if data_source_id:
            matched = [item for item in landings
                       if item.get("data_source_id") == data_source_id]
            if not matched:
                return fail("no_landing",
                            f"指标在数据源 {data_source_id} 没有实体落点",
                            metric=metric_name)
        else:
            sources = sorted({str(item.get("data_source_id"))
                              for item in landings})
            if len(sources) > 1:
                return fail(
                    "ambiguous_source", "指标在多个数据源有落点，需要点名",
                    metric=metric_name, sources=sources[:_MAX_SOURCES],
                    clarifications=build_pool(
                        sources=sources[:_MAX_SOURCES],
                        metric_name=metric_name),
                )
            data_source_id = sources[0]
            matched = landings
        entities = sorted({str(item.get("entity_name")) for item in matched})
        if len(entities) > 1:
            return fail(
                "ambiguous_landing",
                "指标在该数据源有多个实体落点，需要点名",
                metric=metric_name, entities=entities[:_MAX_SOURCES],
                clarifications=build_pool(
                    entities=entities[:_MAX_SOURCES]),
            )
        entity_name = entities[0]

        try:
            target = await self._ds.query_config(tenant_id, data_source_id)
        except ResourceNotFoundError:
            return fail("source_gone", "数据源已被删除", metric=metric_name)
        except InvalidParameterError:
            return fail("not_a_database_source",
                        "该数据源不是可查询的数据库源", metric=metric_name)
        if target is None:
            return fail("no_readonly_creds",
                        "该数据源未登记只读凭据", metric=metric_name)
        source, cfg = target
        engine = str(cfg.get("engine") or "")
        compiled_row = await schema_read.load_compiled_row(
            tenant_id, kb_id, data_source_id)
        if compiled_row is None:
            return fail("provenance_unavailable",
                        "该数据源没有编译 schema，无法溯源",
                        metric=metric_name)
        scope = {str(item.get("name")): item for item in metrics
                 if isinstance(item, dict) and item.get("name")}
        # 延迟导入：service 在模块加载期会 import 本 mixin，反向导入只能放到调用期。
        from .service import (  # noqa: PLC0415
            _governance_decide, _load_forbidden_pairs, _load_governance,
        )
        from ..metrics.compiler import MetricCompileError  # noqa: PLC0415
        from ..metrics.validation import preflight  # noqa: PLC0415

        row_limit = int(limit or config.NL2SQL_MAX_ROWS)
        try:
            compiled = preflight(
                metric, scope=scope, key=metric_name,
                compiled_row=compiled_row, data_source_id=data_source_id,
                group_by=dims, max_rows=row_limit, time_grain=time_grain,
                dialect=engine, offset=offset,
                time_from=time_from, time_to=time_to,
            )
        except MetricCompileError as exc:
            passthrough = {
                "unsupported_dialect", "unsupported_engine_capability",
                "unsupported_pagination", "time_filter_unavailable",
                "time_filter_unsupported", "meaningless_group_by",
            }
            reason_code = (exc.code if exc.code in passthrough
                           else "compile_error")
            return fail(reason_code, str(exc)[:500], metric=metric_name,
                        compile_code=exc.code)

        gov = await _load_governance(
            tenant_id, kb_id, data_source_id, user_id or None)
        forbidden_pairs = await _load_forbidden_pairs(
            tenant_id, kb_id, data_source_id)
        guarded = guard_sql(
            normalize_dialect_sql(compiled.sql, engine),
            engine=engine, max_rows=row_limit,
            allowed_tables=set(compiled.tables),
            forbidden_pairs=forbidden_pairs,
        )
        if not guarded.ok:
            return fail("rejected_unsafe_sql", guarded.reason,
                        metric=metric_name, rejected_sql=compiled.sql)
        exec_sql = guarded.sql
        if gov.get("active"):
            from ..governance.sql_rewrite import apply_governance  # noqa: PLC0415
            gov_result = apply_governance(
                exec_sql, engine=engine,
                decide_fn=lambda table, column, action: _governance_decide(
                    gov, table, column, action),
                schema=gov.get("schema") or {},
            )
            if not gov_result.ok:
                return fail("rejected_sensitive", gov_result.reason,
                            metric=metric_name, rejected_sql=compiled.sql,
                            rejection_kind=gov_result.kind)
            exec_sql = gov_result.sql

        cache_key = metric_cache.make_key(
            tenant_id=tenant_id, kb_id=kb_id, data_source_id=data_source_id,
            sql=exec_sql, max_rows=row_limit, offset=offset)
        started = time.perf_counter()
        cached = metric_cache.get(cache_key)
        if cached is not None:
            result = cached["result"]
            elapsed_ms = 0
        else:
            timeout_budget = (float(timeout) if timeout else
                              (float(config.METRIC_ASK_TIMEOUT_SECONDS)
                               if config.METRIC_ASK_TIMEOUT_SECONDS else None))
            try:
                result = await run_readonly(source, cfg, exec_sql,
                                            timeout=timeout_budget)
            except Exception as exc:  # noqa: BLE001 —— 执行失败归类，不抛给调用方
                return fail("exec_failed",
                            f"{type(exc).__name__}: {str(exc)[:300]}",
                            metric=metric_name, sql=exec_sql)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
        rows = list(result.get("rows") or [])
        row_count = int(result.get("row_count")
                        if result.get("row_count") is not None else len(rows))
        columns = list(result.get("columns") or [])

        provenance = await self._metric_provenance(
            tenant_id=tenant_id, kb_id=kb_id, data_source_id=data_source_id,
            entity_name=entity_name, metric=metric, compiled_row=compiled_row,
            user_id=user_id,
        )
        if provenance is None:
            return fail("provenance_unavailable",
                        "指标结果缺少完整的表/列溯源", metric=metric_name,
                        sql=exec_sql)
        if cached is None:
            metric_cache.put(cache_key, {"result": result})

        if record_audit:
            self._audit(
                tenant_id, user_id, username, ip, data_source_id, q,
                exec_sql, row_count, elapsed_ms,
                extra={"mode": "metric", "metric": metric_name,
                       "metric_type": metric_type,
                       "cached": cached is not None},
            )
        response = {
            "ok": True,
            "not_answerable": False,
            "mode": "metric",
            "data_source_id": data_source_id,
            "entity_name": entity_name,
            "metric": {
                "name": metric_name,
                "display_name": metric.get("display_name") or metric_name,
                "metric_type": metric_type,
                "additivity": metric.get("additivity") or "unknown",
                "definition": metric.get("description") or "",
            },
            "sql": exec_sql,
            "columns": columns,
            "rows": rows,
            "row_count": row_count,
            "truncated": row_count >= row_limit,
            "offset": offset,
            "time_window": {"from": time_from, "to": time_to},
            "cached": cached is not None,
            "context_used": bool(context_metric),
            "provenance": provenance,
            "warnings": [],
        }
        if config.METRIC_CELL_PROVENANCE_ENABLED:
            response["cell_provenance"] = build_cell_provenance(
                metric=metric, metrics=metrics,
                data_source_id=data_source_id, columns=columns,
                rows=rows, sql=exec_sql,
                executed_at=datetime.datetime.now(
                    datetime.timezone.utc).isoformat(),
            )
        # W4.6：统一结果契约（只增不改旧字段）；1×1 数值才给文本块。
        text = summary_text(columns, rows)
        if text:
            text = f"{metric.get('display_name') or metric_name}：{text}"
        response = attach_blocks(response, text=text)
        if compare:
            response["comparison"] = await self._compare_metric(
                tenant_id=tenant_id, kb_id=kb_id, question=q,
                data_source_id=data_source_id, group_by=dims,
                time_grain=time_grain, limit=row_limit, mode=compare,
                time_from=time_from, time_to=time_to,
                metric_name=metric_name, current=response,
                user_id=user_id, username=username, ip=ip,
            )
        return response

    async def _compare_metric(
        self, *, tenant_id: str, kb_id: str, question: str,
        data_source_id: str, group_by: list[str], time_grain,
        limit: int, mode: str, time_from: str, time_to: str,
        metric_name: str, current: dict, user_id: str, username: str,
        ip: str,
    ) -> dict:
        """同期/同比/环比：同一指标再跑一次窗口，返回差异（不额外审计）。"""
        try:
            base_from, base_to = _shift_window(mode, time_from, time_to)
        except ValueError as exc:
            return {"mode": mode, "degraded": True, "reason": str(exc)}
        base = await self.ask_metric(
            tenant_id, kb_id, question, data_source_id=data_source_id,
            group_by=group_by, time_grain=time_grain, limit=limit,
            time_from=base_from, time_to=base_to,
            context={"metric": metric_name}, record_audit=False,
            user_id=user_id, username=username, ip=ip,
        )
        comparison = {
            "mode": mode,
            "current_window": {"from": time_from, "to": time_to},
            "base_window": {"from": base_from, "to": base_to},
            "degraded": not bool(base.get("ok")),
        }
        if base.get("ok"):
            comparison["base"] = {
                "columns": base.get("columns") or [],
                "rows": base.get("rows") or [],
                "row_count": base.get("row_count"),
            }
            comparison["delta"] = _scalar_delta(current, base)
            if base.get("cached"):
                comparison["base_cached"] = True
        else:
            comparison["reason"] = base.get("reason_code")
            comparison["reason_text"] = base.get("reason")
        return comparison

    async def _metric_provenance(
        self, *, tenant_id: str, kb_id: str, data_source_id: str,
        entity_name: str, metric: dict, compiled_row: dict,
        user_id: str = "",
    ) -> dict | None:
        """基础溯源：指标—落点—表—列（+ 口径卡绑定，若有）。缺表/列 → None。"""
        entity = next(
            (item for item in compiled_row.get("entity_types") or []
             if isinstance(item, dict)
             and str(item.get("name") or "") == entity_name),
            None,
        )
        table = str((entity or {}).get("source_object_id") or "").strip()
        if not entity or not table:
            return None
        columns = []
        for attribute in entity.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            name = str(attribute.get("physical_name")
                       or attribute.get("name") or "").strip()
            if name:
                columns.append(name)
        if not columns:
            return None
        card = await _metric_card_binding(
            tenant_id, kb_id, str(metric.get("name") or ""))
        lineage = await _table_lineage(tenant_id, data_source_id, table)
        anchors = await _source_anchors(
            tenant_id, user_id, data_source_id, table)
        return {
            "metric": {
                "name": str(metric.get("name") or ""),
                "metric_type": (metric.get("executable") or {}).get("metric_type"),
                "additivity": metric.get("additivity") or "unknown",
            },
            "landing": {"data_source_id": data_source_id,
                        "entity_name": entity_name},
            "table": {"name": table, "entity": entity_name},
            "columns": columns,
            "card": card,
            # W4.3：表级一跳血缘 + 原文锚点。二者都是**增强项**：读取失败显式
            # degraded，不伪装成「没有」；指标/表/列本体缺失仍在上面硬拒。
            "lineage": lineage,
            "anchors": anchors,
        }


def _shift_window(mode: str, time_from: str, time_to: str) -> tuple[str, str]:
    """当前窗口 → 对比窗口；日期/时间两种 ISO 格式都支持。"""
    def parse(value: str):
        text_value = str(value).strip()
        if len(text_value) == 10:
            return datetime.date.fromisoformat(text_value), True
        return datetime.datetime.fromisoformat(
            text_value.replace("Z", "+00:00")), False

    start, _ = parse(time_from)
    end, _ = parse(time_to)
    if end < start:
        raise ValueError("time_to 不能早于 time_from")
    if mode == "previous_period":
        duration = end - start
        base_end = start - datetime.timedelta(days=1)
        base_start = base_end - duration
    elif mode == "yoy":
        base_start = _shift_years(start, -1)
        base_end = _shift_years(end, -1)
    elif mode == "mom":
        base_start = _shift_months(start, -1)
        base_end = _shift_months(end, -1)
    else:
        raise ValueError(f"不支持的对比模式：{mode}")
    return base_start.isoformat(), base_end.isoformat()


def _shift_years(value, delta: int):
    try:
        return value.replace(year=value.year + delta)
    except ValueError:  # 2/29 → 2/28
        return value.replace(year=value.year + delta, day=28)


def _shift_months(value, delta: int):
    month = value.month - 1 + delta
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, _days_in_month(year, month))
    return value.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime.date(year, month + 1, 1)
            - datetime.date(year, month, 1)).days


def _scalar_delta(current: dict, base: dict) -> dict | None:
    """1×1 数值结果的绝对差与百分比；其他形状返回 None（不臆造差异）。"""
    def scalar(payload):
        columns = payload.get("columns") or []
        rows = payload.get("rows") or []
        if len(columns) != 1 or len(rows) != 1:
            return None
        row = rows[0]
        if not isinstance(row, (list, tuple)) or len(row) != 1:
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None

    cur, prev = scalar(current), scalar(base)
    if cur is None or prev is None:
        return None
    pct = ((cur - prev) / abs(prev) * 100) if prev else None
    return {"absolute": cur - prev, "pct": pct}


async def _table_lineage(tenant_id: str, data_source_id: str,
                        table: str) -> dict:
    """表级一跳血缘（W4.3）；失败 → `degraded=true` 的空图，不阻断回答。"""
    try:
        from ....data_governance.services.asset_lineage import (  # noqa: PLC0415
            build_data_asset_graph,
        )
        graph = await build_data_asset_graph(
            tenant_id,
            {"asset_type": "table", "asset_id": f"{data_source_id}:{table}",
             "name": table},
        )
    except Exception as exc:  # noqa: BLE001 —— 溯源增强项，不阻断
        logger.warning("[metric-ask] 血缘读取失败（已降级）: %s", exc)
        return {"degraded": True, "nodes": [], "edges": [],
                "truncated": False, "counts": {"nodes": 0, "edges": 0}}
    return {
        "degraded": bool(graph.get("degraded")),
        "nodes": [
            {"id": node.get("id"), "type": node.get("type"),
             "label": node.get("label")}
            for node in graph.get("nodes") or []
        ],
        "edges": [
            {"source": edge.get("source"), "target": edge.get("target"),
             "type": edge.get("type"), "meta": edge.get("meta")}
            for edge in graph.get("edges") or []
        ],
        "truncated": bool(graph.get("truncated")),
        "counts": graph.get("counts") or {"nodes": 0, "edges": 0},
    }


async def _source_anchors(tenant_id: str, user_id: str, data_source_id: str,
                          table: str) -> dict:
    """原文锚点（W4.3）；无登录身份/读取失败 → `degraded=true`。"""
    if not user_id:
        return {"degraded": True, "unresolved": True, "linked": [],
                "reason": "no_user"}
    try:
        from ....data_governance.services.source_anchor_service import (  # noqa: PLC0415
            SourceAnchorService,
        )
        result = await SourceAnchorService().get_anchors(
            tenant_id, user_id, "table", f"{data_source_id}:{table}")
    except Exception as exc:  # noqa: BLE001 —— 溯源增强项，不阻断
        logger.warning("[metric-ask] 原文锚点读取失败（已降级）: %s", exc)
        return {"degraded": True, "unresolved": True, "linked": []}
    return {
        "degraded": bool(result.get("degraded")),
        "unresolved": bool(result.get("unresolved")),
        "linked": result.get("linked") or [],
    }


async def _metric_card_binding(tenant_id: str, kb_id: str,
                               metric_name: str) -> dict | None:
    """按 W2.6 绑定找已发布的指标口径卡；失败 fail-open 到 None。"""
    if not metric_name:
        return None
    try:
        async with get_db_session() as session:
            row = (await session.execute(
                text(_CARD_BINDING_SQL),
                {"tenant": tenant_id, "asset": f"{kb_id}:{metric_name}"},
            )).mappings().first()
    except Exception as exc:  # noqa: BLE001 —— 溯源增强项，不阻断回答
        logger.warning("[metric-ask] 口径卡绑定读取失败（已忽略）: %s", exc)
        return None
    if row is None:
        return None
    return {"code": row["code"], "name": row["name"],
            "version": int(row["version"])}


__all__ = ["MetricAskMixin"]
