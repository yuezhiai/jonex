#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""指标构建 agent 的服务端编排：读编译 schema → 建上下文 → 跑循环 → 落提案。

边界：
- **只读**。工具层的所有 SQL 都过 `nl2sql.guard`（单语句、禁注释、表作用域、
  行数上限），并且只认 `DataSourceService.query_config` 的**只读**凭据；没登记
  只读凭据的数据源一律不可查（宁可只做元数据推断，也不回退写账号）。
- **写库只有一处**：提案行。不直接改抽象 schema，也不动任何业务表。
- **时间/步数/token 三重预算**：任何一个到点都收口为 `give_up`，不拿半成品凑提案。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from jonex_core.common.config import config
from jonex_core.common.ontology_llm import metric_agent_step
from jonex_core.common.tenant import require_tenant

from ..data_source_service import DataSourceService
from ..nl2sql.guard import guard_sql
from ..schema_source import normalize_engine
from ...repository.metric_proposal_repository import MetricProposalRepository
from ...repository.ontology_schema_repository import OntologySchemaRepository
from .loop import MetricDecision, run_metric_task
from .verify import verify_proposal
from .tools import ToolContext

logger = logging.getLogger(__name__)

#: 单条预览查询的超时（秒）。小于整体预算，慢查询不会把整次构建拖死。
_QUERY_TIMEOUT_S = 15.0


class MetricAgentService:
    """一次指标构建的编排；会话由调用方注入（与其它 service 同构）。"""

    def __init__(self, session):
        self.session = session

    async def build(
            self, tenant_id: str, kb_id: str, task: str, *,
            data_source_id: str = "", user_id: str = "",
            call_step=None, max_steps: Optional[int] = None,
            timeout_s: Optional[float] = None,
    ) -> dict:
        """跑一次构建并落提案；**不抛**（失败也是 `{"ok": False, ...}`）。"""
        tenant_id = require_tenant(tenant_id)
        task = str(task or "").strip()
        if not task:
            return {"ok": False, "reason": "empty_task",
                    "message": "任务描述不能为空"}
        if not config.METRIC_AGENT_ENABLED:
            return {"ok": False, "reason": "metric_agent_disabled",
                    "message": "指标构建 agent 未开启（METRIC_AGENT_ENABLED）"}

        rows = await self._compiled_rows(tenant_id, kb_id)
        if not rows:
            return {"ok": False, "reason": "no_compiled_schema",
                    "message": "该知识库没有编译 schema，无法构建指标"}

        default_source = str(data_source_id or "").strip()
        if not default_source:
            default_source = str(rows[0].get("data_source_id") or "")
        if default_source and not any(
                str(row.get("data_source_id") or "") == default_source
                for row in rows):
            return {"ok": False, "reason": "unknown_data_source",
                    "message": f"数据源 {default_source} 没有编译 schema"}

        ctx = ToolContext(
            rows=rows,
            run_sql=self._sql_runner(tenant_id, rows),
            preview_budget=max(1, int(config.METRIC_AGENT_PREVIEW_BUDGET)),
            default_source=default_source,
        )
        step = call_step or self._default_step(tenant_id, kb_id, user_id)
        steps_limit = int(max_steps if max_steps is not None
                          else config.METRIC_AGENT_MAX_STEPS)
        budget_s = float(timeout_s if timeout_s is not None
                         else config.METRIC_AGENT_TIMEOUT_S)

        try:
            async def _verify(proposal: dict):
                return await verify_proposal(
                    proposal, rows=rows, run_sql=ctx.run_sql,
                    default_source=default_source)

            decision = await asyncio.wait_for(
                run_metric_task(task, _inventory(rows), ctx, step,
                                max_steps=steps_limit,
                                max_tokens=int(config.METRIC_AGENT_MAX_TOKENS),
                                verify=_verify),
                timeout=max(1.0, budget_s),
            )
        except asyncio.TimeoutError:
            return {"ok": False, "reason": "timeout",
                    "message": f"构建超时（{budget_s:.0f}s）",
                    "preview_calls": ctx.preview_calls}
        except Exception as exc:  # noqa: BLE001 - 入口不抛，错误如实回
            logger.warning("[metric-agent] 构建失败 kb=%s: %s", kb_id, exc)
            return {"ok": False, "reason": "internal_error",
                    "message": f"{type(exc).__name__}: {exc}"}

        if decision.action != "propose":
            return {"ok": False, "reason": decision.reason or "give_up",
                    "message": "模型没有给出可用提案",
                    "steps": decision.steps, "usage": decision.usage,
                    "preview_calls": ctx.preview_calls}

        proposal = dict(decision.proposal)
        # 平台侧确定性复核：模型说「验证过了」不作数（2026-11-14 真机两次都误判）。
        # 正常路径由循环内的 `_verify` 完成（不通过会回灌让模型改一轮）；
        # 这里只对「循环没跑复核」的情况（注入 fake call_step / 旧路径）补一次。
        verification = decision.verification or {}
        if not verification:
            verification = (await verify_proposal(
                proposal, rows=rows, run_sql=ctx.run_sql,
                default_source=default_source)).to_dict()
        if verification.get("ok") is False:
            return {"ok": False, "reason": "verification_failed",
                    "message": "提案未通过平台复核，未入库",
                    "verification": verification,
                    "steps": decision.steps, "usage": decision.usage}
        confidence = proposal.get("confidence") or "low"
        if verification.get("warnings"):
            confidence = "low"
        payload = {
            "formula_sql": proposal.get("formula_sql") or "",
            "grain": proposal.get("grain") or "",
            "filters": proposal.get("filters") or "",
            "rationale": proposal.get("rationale") or "",
            "confidence": confidence,
            "data_source_id": proposal.get("data_source_id") or default_source,
            "steps": decision.steps,
            "usage": decision.usage,
            "verification": verification,
        }
        proposal_id = await MetricProposalRepository(self.session).create(
            tenant_id, kb_id,
            name=str(proposal.get("name") or "")[:256],
            data_source_id=payload["data_source_id"],
            payload=payload,
            created_by=user_id or "",
        )
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "status": "pending",
            "name": proposal.get("name") or "",
            **payload,
        }

    async def _compiled_rows(self, tenant_id: str, kb_id: str) -> list[dict]:
        """读编译 schema → 纯 dict（工具层不依赖 ORM，单测才好写）。"""
        result = await OntologySchemaRepository(
            self.session).list_compiled_schemas(tenant_id, kb_id)
        rows: list[dict] = []
        for row in result:
            ds_id = str(getattr(row, "data_source_id", None) or "")
            if not ds_id:
                continue
            rows.append({
                "data_source_id": ds_id,
                "entity_types": list(getattr(row, "entity_types", None) or []),
                "relation_types": list(
                    getattr(row, "relation_types", None) or []),
                "evidence": getattr(row, "evidence", None) or {},
            })
        return rows

    def _sql_runner(self, tenant_id: str, rows: list[dict]):
        """`run_sql(ds_id, sql, limit)`：安全闸 → 只读执行 → 统一信封。"""
        allowed: dict[str, set[str]] = {}
        for row in rows:
            ds_id = str(row.get("data_source_id") or "")
            tables = allowed.setdefault(ds_id, set())
            for entity in row.get("entity_types") or []:
                if not isinstance(entity, dict):
                    continue
                table = str(entity.get("source_object_id") or "").strip()
                if table:
                    tables.add(table)

        async def _run(ds_id: str, sql: str, limit: int) -> dict:
            ds_id = str(ds_id or "")
            target = None
            try:
                target = await DataSourceService().query_config(tenant_id, ds_id)
            except Exception as exc:  # noqa: BLE001 - 不可查是常态，如实回
                return {"ok": False, "error": f"数据源不可用：{exc}"}
            if target is None:
                return {"ok": False,
                        "error": "该数据源未登记只读凭据，不能执行 SQL"}
            source, cfg = target
            engine = normalize_engine(str(cfg.get("engine") or ""))
            guarded = guard_sql(sql, engine=engine, max_rows=max(1, int(limit)),
                                allowed_tables=allowed.get(ds_id) or None)
            if not guarded.ok:
                return {"ok": False,
                        "error": f"SQL 被安全闸拒绝：{guarded.reason}"}
            try:
                data = await source.run_query(cfg, guarded.sql,
                                              timeout=_QUERY_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"执行失败：{exc}"}
            return {
                "ok": True,
                "sql": guarded.sql,
                "columns": _column_names(data.get("columns")),
                "rows": (data.get("rows") or [])[:max(1, int(limit))],
                "row_count": data.get("row_count", len(data.get("rows") or [])),
            }

        return _run

    @staticmethod
    def _default_step(tenant_id: str, kb_id: str, user_id: str):
        async def _step(messages: list[dict]) -> dict:
            return await metric_agent_step(
                messages, tenant_id=tenant_id, kb_id=kb_id,
                user_id=user_id or None,
                model=config.METRIC_AGENT_MODEL or None,
            )
        return _step


def _column_names(columns) -> list[str]:
    """列名口径各适配器不一致：postgres/mysql 给 `["x"]`，clickhouse 给 `[{"name": "x"}]`。"""
    return [str(item.get("name") or "") if isinstance(item, dict) else str(item)
            for item in (columns or [])]


def _inventory(rows: list[dict]) -> list[dict]:
    """给提示词用的紧凑清单（实体名 → 物理表）。"""
    out = []
    for row in rows:
        entities = []
        for entity in row.get("entity_types") or []:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name") or "").strip()
            if not name:
                continue
            entities.append({"name": name,
                             "table": str(entity.get("source_object_id") or "")})
        out.append({"data_source_id": row.get("data_source_id"),
                    "entities": entities})
    return out


__all__ = ["MetricAgentService"]
