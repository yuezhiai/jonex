"""规则服务：CRUD / 启停 / 校验 / 冲突 / 导入导出（T6）。"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ...models.ontology_reasoning_rule import OntologyReasoningRule
from ...repository.ontology_axiom_repository import OntologyAxiomRepository
from ...repository.ontology_reasoning_rule_repository import (
    OntologyReasoningRuleRepository,
)
from .context import ReasoningContext, resolve_reasoning_context
from .rule_checks import check_rules, has_violation
from .rule_conflicts import check_rule_conflicts
from .rule_yaml import dump_rule_yaml, parse_rule_yaml, rule_to_export_dict


def _plan_hash(items: list[dict]) -> str:
    raw = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RuleService:
    def __init__(
        self,
        *,
        context_loader=resolve_reasoning_context,
        session_cm=get_db_session,
        rule_repo_cls=OntologyReasoningRuleRepository,
        axiom_repo_cls=OntologyAxiomRepository,
    ):
        self._context_loader = context_loader
        self._session_cm = session_cm
        self._rule_repo_cls = rule_repo_cls
        self._axiom_repo_cls = axiom_repo_cls

    async def _context(self, tenant_id: str, kb_id: str) -> ReasoningContext:
        return await self._context_loader(tenant_id, kb_id)

    async def _load_rules(self, tenant_id: str, kb_id: str) -> list[OntologyReasoningRule]:
        async with self._session_cm() as session:
            repo = self._rule_repo_cls(session)
            return await repo.list_by_kb(tenant_id, kb_id, limit=10_000)

    async def _load_axioms(self, tenant_id: str, kb_id: str) -> list[dict]:
        async with self._session_cm() as session:
            repo = self._axiom_repo_cls(session)
            rows = await repo.list_by_kb(tenant_id, kb_id, limit=10_000)
        return [row.to_dict() for row in rows]

    async def list_rules(self, tenant_id: str, kb_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        ctx = await self._context(tenant_id, kb_id)
        rows = await self._load_rules(tenant_id, kb_id)
        all_axioms = await self._load_axioms(tenant_id, kb_id)
        raw_rules = [row.to_dict() for row in rows]
        normalized, issues = check_rules(raw_rules, ctx.schema_context, ctx.axioms)
        all_issues = list(issues)
        all_issues.extend(check_rule_conflicts(
            normalized, ctx.schema_context,
            active_axioms=[a for a in all_axioms if a.get("status") == "active"],
            rejected_axioms=[a for a in all_axioms if a.get("status") == "rejected"],
        ))
        by_key: dict[str, list[dict]] = {}
        for item in all_issues:
            target = item.get("target") or {}
            key = target.get("id")
            if key:
                by_key.setdefault(key, []).append(item)
        items = []
        for row in rows:
            item = row.to_dict()
            item["issues"] = by_key.get(item["rule_key"], [])
            items.append(item)
        return {"items": items, "total": len(items)}

    async def get_rule(self, tenant_id: str, kb_id: str, rule_key: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        rows = await self._load_rules(tenant_id, kb_id)
        row = next((r for r in rows if r.rule_key == rule_key), None)
        if row is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.reasoning_rule.not_found",
                    params={"rule_key": rule_key},
                    fallback=f"规则不存在: {rule_key}",
                ),
            )
        ctx = await self._context(tenant_id, kb_id)
        all_axioms = await self._load_axioms(tenant_id, kb_id)
        item = row.to_dict()
        _, issues = check_rules([item], ctx.schema_context, ctx.axioms)
        item["issues"] = issues
        return item

    async def validate_rules(self, tenant_id: str, kb_id: str, payload: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        ctx = await self._context(tenant_id, kb_id)
        all_axioms = await self._load_axioms(tenant_id, kb_id)
        raw, parse_errors, warnings = self._extract_payload(payload)
        if parse_errors:
            raise InvalidParameterError(
                message=translate(
                    "err.reasoning_rule.invalid_yaml",
                    fallback="规则 YAML 无效",
                ),
                details={"errors": parse_errors},
            )
        normalized, issues = check_rules(raw, ctx.schema_context, ctx.axioms)
        issues.extend(check_rule_conflicts(
            normalized, ctx.schema_context,
            active_axioms=[a for a in all_axioms if a.get("status") == "active"],
            rejected_axioms=[a for a in all_axioms if a.get("status") == "rejected"],
        ))
        return {
            "contract_version": 1,
            "rules": [rule_to_export_dict(rule) for rule in normalized],
            "issues": issues,
            "has_violation": has_violation(issues),
            "warnings": warnings,
        }

    async def create_rule(
        self, tenant_id: str, kb_id: str, payload: dict, *, user_id: str = "",
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        ctx = await self._context(tenant_id, kb_id)
        raw = dict(payload or {})
        raw.setdefault("origin", "platform")
        normalized, issues = check_rules([raw], ctx.schema_context, ctx.axioms)
        rule = normalized[0]
        existing_rows = await self._load_rules(tenant_id, kb_id)
        existing = [r.to_dict() for r in existing_rows]
        issues.extend(check_rule_conflicts(
            [*existing, rule], ctx.schema_context,
            active_axioms=ctx.axioms,
        ))
        if has_violation(issues):
            raise InvalidParameterError(
                message=translate(
                    "err.reasoning_rule.validation_failed",
                    fallback="规则校验失败，未写入",
                ),
                details={"issues": [i for i in issues if i.get("severity") == "Violation"][:50]},
            )
        if any(r.rule_key == rule["rule_key"] for r in existing_rows):
            raise ResourceConflictError(message=translate(
                "err.reasoning_rule.name_conflict",
                params={"name": rule["rule_key"]},
                fallback=f"规则已存在: {rule['rule_key']}",
            ))
        if any(r.rule_key != rule["rule_key"] and r.name == rule["name"] for r in existing_rows):
            raise ResourceConflictError(message=translate(
                "err.reasoning_rule.name_conflict",
                params={"name": rule["name"]},
                fallback=f"规则名称已存在: {rule['name']}",
            ))
        async with self._session_cm() as session:
            repo = self._rule_repo_cls(session)
            row = OntologyReasoningRule(
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                rule_key=rule["rule_key"],
                name=rule["name"],
                text=rule["text"],
                premises=rule.get("premises") or [],
                conclusion=rule.get("conclusion") or {},
                status=rule.get("status") or "active",
                enabled=rule.get("enabled") is not False,
                origin=rule.get("origin") or "platform",
                module=rule.get("module"),
                description=rule.get("description"),
                content_revision=1,
                content_hash=rule.get("content_hash"),
                created_by=user_id or None,
            )
            await repo.create(row)
        result = row.to_dict()
        result["issues"] = issues
        return result

    async def update_rule(
        self, tenant_id: str, kb_id: str, rule_key: str, payload: dict,
        *, user_id: str = "",
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        ctx = await self._context(tenant_id, kb_id)
        rows = await self._load_rules(tenant_id, kb_id)
        row = next((r for r in rows if r.rule_key == rule_key), None)
        if row is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.reasoning_rule.not_found",
                    params={"rule_key": rule_key},
                    fallback=f"规则不存在: {rule_key}",
                ),
            )
        expected = (payload or {}).get("expected_content_revision")
        if expected is not None and int(expected) != int(row.content_revision or 0):
            raise ResourceConflictError(message=translate(
                "err.reasoning_rule.revision_conflict",
                fallback="规则内容已被修改，请刷新后重试",
            ))
        merged = {**row.to_dict(), **(payload or {}), "rule_key": rule_key}
        merged["origin"] = row.origin
        merged["enabled"] = row.enabled
        normalized, issues = check_rules([merged], ctx.schema_context, ctx.axioms)
        rule = normalized[0]
        others = [r.to_dict() for r in rows if r.rule_key != rule_key]
        issues.extend(check_rule_conflicts(
            [*others, rule], ctx.schema_context, active_axioms=ctx.axioms,
        ))
        if has_violation(issues):
            raise InvalidParameterError(
                message=translate(
                    "err.reasoning_rule.validation_failed",
                    fallback="规则校验失败，未写入",
                ),
                details={"issues": [i for i in issues if i.get("severity") == "Violation"][:50]},
            )
        if any(r.name == rule["name"] for r in rows if r.rule_key != rule_key):
            raise ResourceConflictError(message=translate(
                "err.reasoning_rule.name_conflict",
                params={"name": rule["name"]},
                fallback=f"规则名称已存在: {rule['name']}",
            ))
        async with self._session_cm() as session:
            repo = self._rule_repo_cls(session)
            current = await repo.get_by_key(tenant_id, kb_id, rule_key)
            if current is None:
                raise ResourceNotFoundError(message="规则不存在")
            current.name = rule["name"]
            current.text = rule["text"]
            current.premises = rule.get("premises") or []
            current.conclusion = rule.get("conclusion") or {}
            current.status = rule.get("status") or "active"
            current.module = rule.get("module")
            current.description = rule.get("description")
            current.content_revision = int(current.content_revision or 0) + 1
            current.content_hash = rule.get("content_hash")
            current.updated_by = user_id or None
        return {**current.to_dict(), "issues": issues}

    async def delete_rule(
        self, tenant_id: str, kb_id: str, rule_key: str,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with self._session_cm() as session:
            repo = self._rule_repo_cls(session)
            current = await repo.get_by_key(tenant_id, kb_id, rule_key)
            if current is None:
                raise ResourceNotFoundError(message="规则不存在")
            await repo.delete_soft(current, tenant_id)
        return {"rule_key": rule_key, "deleted": True}

    async def set_enabled(
        self, tenant_id: str, kb_id: str, rule_key: str, enabled: bool,
        *, user_id: str = "",
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        if not isinstance(enabled, bool):
            raise InvalidParameterError(message="enabled 必须是布尔值")
        ctx = await self._context(tenant_id, kb_id)
        rows = await self._load_rules(tenant_id, kb_id)
        row = next((r for r in rows if r.rule_key == rule_key), None)
        if row is None:
            raise ResourceNotFoundError(message="规则不存在")
        if enabled:
            candidate = row.to_dict()
            candidate["enabled"] = True
            others = [r.to_dict() for r in rows if r.rule_key != rule_key]
            issues = check_rule_conflicts(
                [*others, candidate], ctx.schema_context, active_axioms=ctx.axioms,
            )
            if has_violation(issues):
                raise InvalidParameterError(
                    message=translate(
                        "err.reasoning_rule.enable_conflict",
                        fallback="启用失败：存在规则冲突",
                    ),
                    details={"issues": [i for i in issues if i.get("severity") == "Violation"][:50]},
                )
        async with self._session_cm() as session:
            from datetime import datetime, timezone
            repo = self._rule_repo_cls(session)
            current = await repo.get_by_key(tenant_id, kb_id, rule_key)
            if current is None:
                raise ResourceNotFoundError(message="规则不存在")
            current.enabled = enabled
            current.enabled_changed_at = datetime.now(timezone.utc)
            current.enabled_changed_by = user_id or None
        return {**current.to_dict()}

    async def preview_reasoning(
        self, tenant_id: str, kb_id: str, *,
        rule_keys: Optional[list[str]] = None,
        rule_ast: Optional[dict] = None,
        anchor_entity: str = "",
        limit: int = 200,
        timeout: float = 3.0,
    ) -> dict:
        """只执行规则表（B 档）；结果不写回图、不进作答。"""
        from .executor import execute_rule
        from .view import build_reasoning_view

        tenant_id = require_tenant(tenant_id)
        ctx = await self._context(tenant_id, kb_id)
        view = build_reasoning_view(ctx)
        limit = max(1, min(int(limit or 200), 1000))

        selected: list[dict] = []
        skipped: list[dict] = []
        if rule_ast is not None:
            normalized, issues = check_rules(
                [rule_ast], ctx.schema_context, ctx.axioms,
            )
            if has_violation(issues):
                return {
                    "contract_version": 1,
                    "results": [],
                    "issues": issues,
                    "skipped": [],
                }
            selected = normalized
        else:
            rows = await self._load_rules(tenant_id, kb_id)
            wanted = set(rule_keys or [])
            normalized, issues = check_rules(
                [row.to_dict() for row in rows], ctx.schema_context, ctx.axioms,
            )
            by_key = {rule["rule_key"]: rule for rule in normalized}
            for row in rows:
                if not (row.status == "active" and row.enabled is not False):
                    continue
                if wanted and row.rule_key not in wanted:
                    continue
                rule_issues = [
                    item for item in issues
                    if (item.get("target") or {}).get("id") == row.rule_key
                ]
                if has_violation(rule_issues):
                    skipped.append({
                        "rule_key": row.rule_key,
                        "reason": "validation",
                        "issues": rule_issues,
                    })
                    continue
                rule = by_key.get(row.rule_key)
                if rule:
                    selected.append(rule)

        # T9：按依赖图分层；上游规则结论作为下游规则的额外事实。
        layers = self._rule_layers(selected)
        extra_edges: list[dict] = []
        extra_types: list[dict] = []
        results: list[dict] = []
        for layer_index, layer in enumerate(layers, start=1):
            layer_results = []
            for rule in layer:
                out = await execute_rule(
                    rule, view,
                    tenant_id=tenant_id, kb_id=kb_id,
                    limit=limit, timeout=timeout,
                    anchor_entity=(anchor_entity or "").strip(),
                    extra_edges=extra_edges,
                    extra_types=extra_types,
                )
                out["layer"] = layer_index
                layer_results.append(out)
                results.append(out)
            for out in layer_results:
                for item in out.get("derived") or []:
                    docs = (item.get("premise") or {}).get("doc_ids") or []
                    if item.get("p") == "type":
                        extra_types.append({
                            "s": item.get("s"),
                            "cls": item.get("o"),
                            "docs": docs,
                        })
                    else:
                        extra_edges.append({
                            "s": item.get("s"),
                            "p": item.get("p"),
                            "o": item.get("o"),
                            "docs": docs,
                        })
            # 去重并给单层输入封顶，防止链式规则把 Cypher 参数撑爆。
            extra_edges = list({
                (e.get("s"), e.get("p"), e.get("o")): e for e in extra_edges
            }.values())[:1000]
            extra_types = list({
                (t.get("s"), t.get("cls")): t for t in extra_types
            }.values())[:1000]
        return {
            "contract_version": 1,
            "results": results,
            "skipped": skipped,
            "issues": [],
        }

    @staticmethod
    def _rule_layers(rules: list[dict]) -> list[list[dict]]:
        """按「结论谓词出现在另一条前提」建依赖图，做拓扑分层；成环时兜底单层。"""
        if not rules:
            return []

        def predicate(atom: dict) -> str:
            if atom.get("kind") == "rel":
                return f"rel:{atom.get('p')}"
            return f"type:{atom.get('cls')}"

        by_key = {rule.get("rule_key"): rule for rule in rules}
        conclusions = {
            key: predicate(rule.get("conclusion") or {})
            for key, rule in by_key.items()
            if rule.get("conclusion")
        }
        deps: dict[str, set[str]] = {}
        for key, rule in by_key.items():
            premise_preds = {
                predicate(atom) for atom in (rule.get("premises") or [])
            }
            deps[key] = {
                other for other, pred in conclusions.items()
                if other != key and pred in premise_preds
            }

        remaining = set(by_key)
        layers: list[list[dict]] = []
        while remaining:
            layer_keys = {
                key for key in remaining
                if not (deps.get(key, set()) & remaining)
            }
            if not layer_keys:
                # 成环：规则校验本应拦截；这里兜底为单层，避免死循环。
                layer_keys = set(remaining)
            layers.append([by_key[key] for key in sorted(layer_keys)])
            remaining -= layer_keys
        return layers

    async def export_rules(self, tenant_id: str, kb_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        rows = await self._load_rules(tenant_id, kb_id)
        rules = [row.to_dict() for row in rows]
        return {
            "filename": f"{kb_id}_reasoning_rules.yaml",
            "yaml_text": dump_rule_yaml(rules, kb_id),
            "count": len(rules),
            "contract_version": 1,
        }

    async def import_rules(
        self, tenant_id: str, kb_id: str, yaml_text: str, *,
        user_id: str = "", dry_run: bool = False, mode: str = "merge",
        removed_rule_keys: Optional[list[str]] = None,
        plan_hash: Optional[str] = None, override_enabled: bool = False,
    ) -> dict:
        payload, errors, warnings = parse_rule_yaml(yaml_text)
        if errors or payload is None:
            raise InvalidParameterError(
                message=translate(
                    "err.reasoning_rule.invalid_yaml",
                    fallback="规则 YAML 无效",
                ),
                details={"errors": errors},
            )
        removed = list(payload.get("removed_rule_keys") or [])
        for key in removed_rule_keys or []:
            key = str(key or "").strip()
            if key and key not in removed:
                removed.append(key)
        return await self.import_rule_list(
            tenant_id, kb_id, payload.get("rules") or [],
            user_id=user_id, dry_run=dry_run, mode=mode,
            removed_rule_keys=removed, plan_hash=plan_hash,
            override_enabled=override_enabled, warnings=warnings,
        )

    async def import_rule_list(
        self, tenant_id: str, kb_id: str, rules: list[dict], *,
        user_id: str = "", dry_run: bool = False, mode: str = "merge",
        removed_rule_keys: Optional[list[str]] = None,
        plan_hash: Optional[str] = None, override_enabled: bool = False,
        warnings: Optional[list[str]] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        mode = (mode or "merge").strip().lower()
        if mode not in {"merge", "replace"}:
            raise InvalidParameterError(message=f"不支持的导入模式: {mode}")
        ctx = await self._context(tenant_id, kb_id)
        rows = await self._load_rules(tenant_id, kb_id)
        existing_by_key = {row.rule_key: row for row in rows}
        normalized, issues = check_rules(rules, ctx.schema_context, ctx.axioms)
        all_axioms = await self._load_axioms(tenant_id, kb_id)
        incoming_keys = {rule["rule_key"] for rule in normalized}
        conflict_base = [
            row.to_dict() for row in rows if row.rule_key not in incoming_keys
        ]
        issues.extend(check_rule_conflicts(
            conflict_base + normalized, ctx.schema_context,
            active_axioms=[a for a in all_axioms if a.get("status") == "active"],
            rejected_axioms=[a for a in all_axioms if a.get("status") == "rejected"],
        ))
        if has_violation(issues) and not dry_run:
            raise InvalidParameterError(
                message=translate(
                    "err.reasoning_rule.validation_failed",
                    fallback="规则校验失败，未写入",
                ),
                details={"issues": [i for i in issues if i.get("severity") == "Violation"][:50]},
            )
        plan = self._build_plan(
            normalized, existing_by_key, mode=mode,
            removed=list(removed_rule_keys or []),
        )
        digest = _plan_hash(plan["items"])
        if not dry_run and plan_hash and plan_hash != digest:
            raise ResourceConflictError(message=translate(
                "err.reasoning_rule.plan_changed",
                fallback="导入计划已变化，请重新预演",
            ))
        if dry_run:
            return {
                "dry_run": True, "contract_version": 1,
                "summary": plan["summary"], "plan": plan["items"],
                "plan_hash": digest, "issues": issues,
                "warnings": list(warnings or []),
            }

        async with self._session_cm() as session:
            repo = self._rule_repo_cls(session)
            for item in plan["items"]:
                key = item["rule_key"]
                action = item["action"]
                current = existing_by_key.get(key)
                if action in {"update", "reject"}:
                    current = await repo.get_by_key(tenant_id, kb_id, key) or current
                if action in {"unchanged", "conflict"}:
                    continue
                if action == "reject":
                    if current is not None:
                        current.status = "rejected"
                        current.content_revision = int(current.content_revision or 0) + 1
                        current.updated_by = user_id or None
                    continue
                rule = item["axiom"] if "axiom" in item else item["rule"]
                if current is None:
                    row = OntologyReasoningRule(
                        tenant_id=tenant_id, knowledge_base_id=kb_id,
                        rule_key=key, name=rule["name"], text=rule["text"],
                        premises=rule.get("premises") or [],
                        conclusion=rule.get("conclusion") or {},
                        status=rule.get("status") or "active",
                        enabled=rule.get("enabled") is not False,
                        origin=rule.get("origin") or "imported",
                        module=rule.get("module"),
                        description=rule.get("description"),
                        content_revision=1,
                        content_hash=rule.get("content_hash"),
                        created_by=user_id or None,
                    )
                    await repo.create(row)
                else:
                    current.name = rule["name"]
                    current.text = rule["text"]
                    current.premises = rule.get("premises") or []
                    current.conclusion = rule.get("conclusion") or {}
                    current.status = rule.get("status") or "active"
                    current.module = rule.get("module")
                    current.description = rule.get("description")
                    if override_enabled:
                        current.enabled = rule.get("enabled") is not False
                    current.content_revision = int(current.content_revision or 0) + 1
                    current.content_hash = rule.get("content_hash")
                    current.updated_by = user_id or None
        return {
            "dry_run": False, "contract_version": 1,
            "summary": plan["summary"], "plan_hash": digest,
            "issues": issues, "warnings": list(warnings or []),
        }

    @staticmethod
    def _extract_payload(payload: dict) -> tuple[list[dict], list[str], list[str]]:
        payload = payload or {}
        if isinstance(payload.get("rules"), list):
            return payload["rules"], [], []
        yaml_text = payload.get("yaml_text")
        if isinstance(yaml_text, str) and yaml_text.strip():
            parsed, errors, warnings = parse_rule_yaml(yaml_text)
            if parsed is None:
                return [], errors, warnings
            return parsed.get("rules") or [], [], warnings
        return [], ["缺少 rules 或 yaml_text"], []

    @staticmethod
    def _build_plan(
        normalized: list[dict], existing_by_key: dict[str, OntologyReasoningRule],
        *, mode: str, removed: list[str],
    ) -> dict:
        items: list[dict] = []
        summary = {"create": 0, "update": 0, "unchanged": 0, "reject": 0, "conflict": 0}
        incoming_keys: set[str] = set()
        for rule in normalized:
            key = rule["rule_key"]
            incoming_keys.add(key)
            existing = existing_by_key.get(key)
            if existing is None:
                action = "create"
            elif (existing.content_hash or "") == (rule.get("content_hash") or ""):
                action = "unchanged"
            elif mode == "replace":
                action = "update"
            else:
                base = rule.get("base_revision")
                if base is None or int(base) < int(existing.content_revision or 0):
                    action = "conflict"
                else:
                    action = "update"
            summary[action] += 1
            items.append({"rule_key": key, "action": action, "rule": rule})
        for key in removed:
            if key in incoming_keys:
                continue
            if key in existing_by_key:
                items.append({"rule_key": key, "action": "reject"})
                summary["reject"] += 1
            incoming_keys.add(key)
        if mode == "replace":
            for key in existing_by_key:
                if key not in incoming_keys:
                    items.append({"rule_key": key, "action": "reject"})
                    summary["reject"] += 1
        return {"items": items, "summary": summary}


__all__ = ["RuleService"]
