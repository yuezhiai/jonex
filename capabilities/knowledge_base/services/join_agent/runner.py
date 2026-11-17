#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""join 复核 agent 的编排：确定性发现 → agent 复核 → 落提案。

边界：
- **候选必须来自发现器**（`evolution/relation_discovery.discover_relations`），
  agent 不能凭空造边，也没有全表扫描工具；
- 所有采样/校验都走**只读**凭据（`DataSourceService.query_config`，拿不到就
  整体不可用，绝不回退写账号）；
- 写库只有一处：提案行。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from jonex_core.common.config import config
from jonex_core.common.ontology_llm import join_agent_step
from jonex_core.common.tenant import require_tenant

from ..data_source_service import DataSourceService
from ..evolution.relation_discovery import discover_relations
from ..nl2sql.guard import guard_sql
from ..schema_source import normalize_engine
from ...repository.join_proposal_repository import JoinProposalRepository
from ...repository.ontology_schema_repository import OntologySchemaRepository
from .loop import run_join_review
from .tools import JoinContext

logger = logging.getLogger(__name__)

_SAMPLE_TIMEOUT_S = 15.0


class JoinAgentService:
    """一次 join 复核的编排；会话由调用方注入。"""

    def __init__(self, session):
        self.session = session

    async def discover(
            self, tenant_id: str, kb_id: str, *,
            data_source_id: str = "", task: str = "",
            user_id: str = "", call_step=None,
            max_steps: Optional[int] = None,
            timeout_s: Optional[float] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        if not config.JOIN_AGENT_ENABLED:
            return {"ok": False, "reason": "join_agent_disabled",
                    "message": "join 复核 agent 未开启（JOIN_AGENT_ENABLED）"}

        rows = await self._compiled_rows(tenant_id, kb_id)
        if not rows:
            return {"ok": False, "reason": "no_compiled_schema",
                    "message": "该知识库没有编译 schema"}

        ds_id = str(data_source_id or "").strip()
        if not ds_id:
            ds_id = str(rows[0].get("data_source_id") or "")
        row = next((item for item in rows
                    if str(item.get("data_source_id") or "") == ds_id), None)
        if row is None:
            return {"ok": False, "reason": "unknown_data_source",
                    "message": f"数据源 {ds_id} 没有编译 schema"}

        sampler, engine = await self._sampler(tenant_id, ds_id)
        if sampler is None:
            return {"ok": False, "reason": "no_readonly_creds",
                    "message": "该数据源未登记只读凭据，无法做 join 校验"}

        evidence = await discover_relations(
            tenant_id, ds_id, row,
            {"max_candidates": max(1, int(config.JOIN_AGENT_MAX_CANDIDATES)),
             "sample_n": 200, "max_queries": 400},
            sampler=sampler, engine=engine,
        )
        candidates = list((evidence.get("evidence") or {}).get("candidates") or [])
        if not candidates:
            return {"ok": True, "candidates": 0, "proposals": [],
                    "message": "没有候选 join 边（命名短名单没命中或都已有边）"}

        ctx = JoinContext(
            candidates=candidates,
            rows=rows,
            run_sql=self._sql_runner(tenant_id, ds_id, row),
            default_source=ds_id,
            check_budget=max(1, int(config.JOIN_AGENT_CHECK_BUDGET)),
        )
        step = call_step or self._default_step(tenant_id, kb_id, user_id)
        steps_limit = int(max_steps if max_steps is not None
                          else config.JOIN_AGENT_MAX_STEPS)
        budget_s = float(timeout_s if timeout_s is not None
                         else config.JOIN_AGENT_TIMEOUT_S)

        try:
            decision = await asyncio.wait_for(
                run_join_review(
                    task or "复核本轮 join 候选，提交可信的边",
                    candidates, _inventory(rows), ctx, step,
                    max_steps=steps_limit),
                timeout=max(1.0, budget_s),
            )
        except asyncio.TimeoutError:
            return {"ok": False, "reason": "timeout",
                    "message": f"复核超时（{budget_s:.0f}s）",
                    "candidates": len(candidates), "checks": ctx.checks}
        except Exception as exc:  # noqa: BLE001 - 入口不抛
            logger.warning("[join-agent] 复核失败 kb=%s: %s", kb_id, exc)
            return {"ok": False, "reason": "internal_error",
                    "message": f"{type(exc).__name__}: {exc}"}

        if decision.action != "propose":
            return {"ok": False, "reason": decision.reason or "give_up",
                    "message": "模型没有给出可用结论",
                    "steps": decision.steps, "usage": decision.usage,
                    "candidates": len(candidates), "checks": ctx.checks}

        repo = JoinProposalRepository(self.session)
        created = []
        for edge in decision.edges:
            proposal_id = await repo.create(
                tenant_id, kb_id,
                source_entity=edge["source_entity"],
                source_column=edge["source_column"],
                target_entity=edge["target_entity"],
                target_column=edge["target_column"],
                data_source_id=ds_id,
                payload={
                    "rationale": edge.get("rationale") or decision.rationale,
                    "confidence": edge.get("confidence") or "low",
                    "steps": decision.steps,
                    "usage": decision.usage,
                    "checks": ctx.checks,
                },
                created_by=user_id or "",
            )
            created.append({"proposal_id": proposal_id, **edge})
        return {
            "ok": True,
            "data_source_id": ds_id,
            "candidates": len(candidates),
            "checks": ctx.checks,
            "steps": decision.steps,
            "usage": decision.usage,
            "proposals": created,
        }

    async def _compiled_rows(self, tenant_id: str, kb_id: str) -> list[dict]:
        result = await OntologySchemaRepository(
            self.session).list_compiled_schemas(tenant_id, kb_id)
        rows = []
        for row in result:
            ds_id = str(getattr(row, "data_source_id", None) or "")
            if not ds_id:
                continue
            rows.append({
                "data_source_id": ds_id,
                "entity_types": list(getattr(row, "entity_types", None) or []),
                "relation_types": list(
                    getattr(row, "relation_types", None) or []),
            })
        return rows

    async def _sampler(self, tenant_id: str, ds_id: str):
        """发现器用的只读采样器；拿不到只读凭据返回 `(None, "")`。"""
        try:
            target = await DataSourceService().query_config(tenant_id, ds_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[join-agent] 数据源不可用 ds=%s: %s", ds_id, exc)
            return None, ""
        if target is None:
            return None, ""
        source, cfg = target
        engine = normalize_engine(str(cfg.get("engine") or ""))

        async def _sample(sql: str) -> list[list]:
            data = await source.run_query(cfg, sql, timeout=_SAMPLE_TIMEOUT_S)
            return data.get("rows") or []

        return _sample, engine

    def _sql_runner(self, tenant_id: str, ds_id: str, row: dict):
        """agent 的 `check_overlap` 用：安全闸 → 只读执行。"""
        allowed = {str(entity.get("source_object_id") or "").strip()
                   for entity in row.get("entity_types") or []
                   if isinstance(entity, dict)}
        allowed.discard("")

        async def _run(target_ds: str, sql: str, limit: int) -> dict:
            if str(target_ds or "") != ds_id:
                return {"ok": False, "error": "未授权的数据源"}
            try:
                target = await DataSourceService().query_config(tenant_id, ds_id)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"数据源不可用：{exc}"}
            if target is None:
                return {"ok": False, "error": "该数据源未登记只读凭据"}
            source, cfg = target
            engine = normalize_engine(str(cfg.get("engine") or ""))
            guarded = guard_sql(sql, engine=engine, max_rows=max(1, int(limit)),
                                allowed_tables=allowed or None)
            if not guarded.ok:
                return {"ok": False, "error": f"SQL 被安全闸拒绝：{guarded.reason}"}
            try:
                data = await source.run_query(cfg, guarded.sql,
                                              timeout=_SAMPLE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"执行失败：{exc}"}
            return {"ok": True, "sql": guarded.sql,
                    "columns": data.get("columns") or [],
                    "rows": data.get("rows") or [],
                    "row_count": data.get("row_count", 0)}

        return _run

    @staticmethod
    def _default_step(tenant_id: str, kb_id: str, user_id: str):
        async def _step(messages: list[dict]) -> dict:
            return await join_agent_step(
                messages, tenant_id=tenant_id, kb_id=kb_id,
                user_id=user_id or None,
                model=config.JOIN_AGENT_MODEL or None,
            )
        return _step


def _inventory(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        entities = []
        for entity in row.get("entity_types") or []:
            if isinstance(entity, dict) and entity.get("name"):
                entities.append({"name": str(entity.get("name"))})
        out.append({"data_source_id": row.get("data_source_id"),
                    "entities": entities})
    return out


__all__ = ["JoinAgentService"]
