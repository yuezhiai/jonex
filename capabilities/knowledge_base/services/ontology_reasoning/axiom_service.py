"""公理表服务：列表 / 校验 / 导入导出 / 契约。

公理表是 KB 级唯一权威（D8）；本服务不读写抽象 schema 的 axioms 镜像
（镜像同步属于 T12 merge 的职责）。
"""
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

from ...models.ontology_axiom import OntologyAxiom
from ...repository.ontology_axiom_repository import OntologyAxiomRepository
from ...repository.ontology_reasoning_rule_repository import (
    OntologyReasoningRuleRepository,
)
from .axiom_checks import check_axioms, has_violation
from .axiom_yaml import (
    axiom_to_export_dict,
    dump_axiom_yaml,
    parse_axiom_yaml,
)
from .context import ReasoningContext
from .contract import CONTRACT_VERSION, contract_payload
from .rule_checks import check_rules
from .rule_conflicts import check_rule_conflicts
from .schema_context import SchemaContext, load_schema_context
from .view import build_reasoning_view


class AxiomService:
    def __init__(
        self,
        *,
        context_loader=load_schema_context,
        session_cm=get_db_session,
        repo_cls=OntologyAxiomRepository,
        rule_repo_cls=OntologyReasoningRuleRepository,
    ):
        self._context_loader = context_loader
        self._session_cm = session_cm
        self._repo_cls = repo_cls
        self._rule_repo_cls = rule_repo_cls

    async def _context(
        self, tenant_id: str, kb_id: str, schema_override: Optional[dict] = None,
    ) -> SchemaContext:
        if schema_override is not None:
            return SchemaContext.from_schema(schema_override)
        return await self._context_loader(tenant_id, kb_id)

    async def _load_rows(self, tenant_id: str, kb_id: str) -> list[OntologyAxiom]:
        async with self._session_cm() as session:
            repo = self._repo_cls(session)
            return await repo.list_by_kb(tenant_id, kb_id, limit=10_000)

    async def _load_rule_rows(self, tenant_id: str, kb_id: str) -> list:
        async with self._session_cm() as session:
            repo = self._rule_repo_cls(session)
            return await repo.list_by_kb(tenant_id, kb_id, limit=10_000)

    @staticmethod
    def _active_rows(rows: list[OntologyAxiom]) -> list[dict]:
        return [
            row.to_dict() for row in rows
            if str(getattr(row, "status", "") or "") == "active"
        ]

    async def list_axioms(self, tenant_id: str, kb_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        context = await self._context(tenant_id, kb_id)
        rows = await self._load_rows(tenant_id, kb_id)
        active = self._active_rows(rows)
        items = []
        for row in rows:
            item = row.to_dict()
            others = [a for a in active if a.get("axiom_key") != item.get("axiom_key")]
            _, issues = check_axioms([item], context, others)
            item["issues"] = issues
            items.append(item)
        return {"items": items, "total": len(items)}

    async def get_axiom(self, tenant_id: str, kb_id: str, axiom_key: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        context = await self._context(tenant_id, kb_id)
        rows = await self._load_rows(tenant_id, kb_id)
        row = next((r for r in rows if r.axiom_key == axiom_key), None)
        if row is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.ontology_axiom.not_found",
                    params={"axiom_key": axiom_key},
                    fallback=f"公理不存在: {axiom_key}",
                ),
            )
        item = row.to_dict()
        others = [a for a in self._active_rows(rows) if a.get("axiom_key") != axiom_key]
        _, issues = check_axioms([item], context, others)
        item["issues"] = issues
        return item

    # ── 状态启停（T4-UI）：preview → apply ──────────────────────

    @staticmethod
    def _status_fingerprint(active: list[dict], context: SchemaContext) -> str:
        payload = {
            "axioms": [
                [a.get("axiom_key"), a.get("content_revision"),
                 a.get("content_hash"), a.get("status")]
                for a in active
            ],
            "entities": sorted(context.entities),
            "relations": sorted(context.relations),
            "attributes": sorted([list(key) for key in context.attributes]),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _issue_signature(issue: dict) -> str:
        return json.dumps(issue, ensure_ascii=False, sort_keys=True, default=str)

    @classmethod
    def _group_issues(cls, issues: list[dict]) -> dict[str, dict[str, dict]]:
        grouped: dict[str, dict[str, dict]] = {}
        for issue in issues:
            rule_key = (issue.get("target") or {}).get("id") or ""
            if not rule_key:
                continue
            grouped.setdefault(rule_key, {})[cls._issue_signature(issue)] = issue
        return grouped

    @staticmethod
    def _diff_view_map(
        before: dict, after: dict, key_text=lambda key: str(key),
    ) -> list[dict]:
        changes: list[dict] = []
        for key in sorted(set(before) | set(after), key=lambda item: str(item)):
            old = set(before.get(key) or [])
            new = set(after.get(key) or [])
            if old == new:
                continue
            changes.append({
                "key": key_text(key),
                "added": sorted(new - old),
                "removed": sorted(old - new),
            })
        return changes

    async def preview_axiom_status(
        self, tenant_id: str, kb_id: str, axiom_key: str, status: str,
    ) -> dict:
        """预览启用/停用的读时展开变化与规则校验变化；不写库。"""
        tenant_id = require_tenant(tenant_id)
        status = str(status or "").strip().lower()
        if status not in {"active", "proposed"}:
            raise InvalidParameterError(
                message=translate(
                    "err.ontology_axiom.invalid_status",
                    params={"status": status},
                    fallback=f"不支持的公理状态: {status}（只允许 active / proposed）",
                ),
            )
        context = await self._context(tenant_id, kb_id)
        rows = await self._load_rows(tenant_id, kb_id)
        row = next((r for r in rows if r.axiom_key == axiom_key), None)
        if row is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.ontology_axiom.not_found",
                    params={"axiom_key": axiom_key},
                    fallback=f"公理不存在: {axiom_key}",
                ),
            )

        from_status = str(row.status or "")
        current_active = self._active_rows(rows)
        next_active = []
        for item in rows:
            data = item.to_dict()
            if item.axiom_key == axiom_key:
                if status == "active":
                    data["status"] = "active"
                    next_active.append(data)
                continue
            if str(item.status or "") == "active":
                next_active.append(data)

        current_rejected = [
            item.to_dict() for item in rows if str(item.status or "") == "rejected"
        ]
        next_rejected = [
            item for item in current_rejected
            if item.get("axiom_key") != axiom_key
        ]

        if from_status == status:
            return {
                "axiom_key": axiom_key,
                "from_status": from_status,
                "to_status": status,
                "unchanged": True,
                "axiom": row.to_dict(),
                "impact": {
                    "entities": [], "relations": [], "attributes": [],
                    "characteristics": [],
                    "summary": {"entities": 0, "relations": 0,
                                "attributes": 0, "characteristics": 0},
                },
                "affected_rules": [],
                "new_axiom_issues": [],
                "has_violation": False,
                "notes": [],
            }

        current_ctx = ReasoningContext(
            merged_schema=context.schema or {},
            axioms=current_active,
            schema_context=context,
            fingerprint=(
                "status-current:"
                f"{self._status_fingerprint(current_active, context)}"
            ),
        )
        next_ctx = ReasoningContext(
            merged_schema=context.schema or {},
            axioms=next_active,
            schema_context=context,
            fingerprint=(
                "status-next:"
                f"{self._status_fingerprint(next_active, context)}"
            ),
        )
        current_view = build_reasoning_view(current_ctx)
        next_view = build_reasoning_view(next_ctx)

        entities = self._diff_view_map(
            current_view.class_ancestors, next_view.class_ancestors,
        )
        relations = self._diff_view_map(
            current_view.relation_descendants, next_view.relation_descendants,
        )
        attributes = self._diff_view_map(
            current_view.attribute_ancestors, next_view.attribute_ancestors,
            key_text=lambda key: f"{key[0]}.{key[1]}",
        )
        characteristics = self._diff_view_map(
            current_view.characteristics, next_view.characteristics,
        )
        impact = {
            "entities": entities,
            "relations": relations,
            "attributes": attributes,
            "characteristics": characteristics,
            "summary": {
                "entities": len(entities),
                "relations": len(relations),
                "attributes": len(attributes),
                "characteristics": len(characteristics),
            },
        }

        # 公理自身重校验：启用一条 proposed 公理可能引入环 / 互斥冲突。
        _, current_ax_issues = check_axioms(current_active, context)
        _, next_ax_issues = check_axioms(next_active, context)
        current_ax_sigs = {
            self._issue_signature(issue) for issue in current_ax_issues
        }
        new_axiom_issues = [
            issue for issue in next_ax_issues
            if self._issue_signature(issue) not in current_ax_sigs
        ]

        # 规则重校验：只关心 active && enabled 的规则（真正会执行的集合）。
        rule_rows = [
            r for r in await self._load_rule_rows(tenant_id, kb_id)
            if str(getattr(r, "status", "") or "") == "active"
            and getattr(r, "enabled", True) is not False
        ]
        raw_rules = [r.to_dict() for r in rule_rows]
        before_norm, before_issues = check_rules(
            raw_rules, context, current_active,
        )
        after_norm, after_issues = check_rules(
            raw_rules, context, next_active,
        )
        before_issues.extend(check_rule_conflicts(
            before_norm, context,
            active_axioms=current_active,
            rejected_axioms=current_rejected,
        ))
        after_issues.extend(check_rule_conflicts(
            after_norm, context,
            active_axioms=next_active,
            rejected_axioms=next_rejected,
        ))
        before_by_rule = self._group_issues(before_issues)
        after_by_rule = self._group_issues(after_issues)

        affected_rules: list[dict] = []
        has_violation = any(
            issue.get("severity") == "Violation" for issue in new_axiom_issues
        )
        for rule in rule_rows:
            key = rule.rule_key
            before = before_by_rule.get(key, {})
            after = after_by_rule.get(key, {})
            new_issues = [
                issue for sig, issue in after.items() if sig not in before
            ]
            resolved_issues = [
                issue for sig, issue in before.items() if sig not in after
            ]
            if not new_issues and not resolved_issues:
                continue
            if any(i.get("severity") == "Violation" for i in new_issues):
                has_violation = True
            affected_rules.append({
                "rule_key": key,
                "name": rule.name,
                "status": rule.status,
                "enabled": rule.enabled is not False,
                "new_issues": new_issues,
                "resolved_issues": resolved_issues,
            })

        notes: list[str] = []
        kind = str(row.kind or "")
        if kind in {"propertyChain", "datatypeFacet"}:
            notes.append("display_only")
        if kind in {"disjointWith", "equivalentClass"}:
            notes.append("conflict_only")
        if not any(impact["summary"].values()) and not affected_rules:
            notes.append("no_impact")

        return {
            "axiom_key": axiom_key,
            "from_status": from_status,
            "to_status": status,
            "unchanged": False,
            "axiom": row.to_dict(),
            "impact": impact,
            "affected_rules": affected_rules,
            "new_axiom_issues": new_axiom_issues,
            "has_violation": has_violation,
            "notes": notes,
        }

    async def set_axiom_status(
        self,
        tenant_id: str,
        kb_id: str,
        axiom_key: str,
        status: str,
        *,
        user_id: str = "",
        force: bool = False,
        expected_status: Optional[str] = None,
    ) -> dict:
        """启用/停用公理；有新增规则 Violation 时默认阻断，force=True 才放行。"""
        preview = await self.preview_axiom_status(
            tenant_id, kb_id, axiom_key, status,
        )
        if preview.get("unchanged"):
            return {**preview, "applied": False}

        tenant_id = require_tenant(tenant_id)
        async with self._session_cm() as session:
            repo = self._repo_cls(session)
            row = await repo.get_by_key(tenant_id, kb_id, axiom_key)
            if row is None:
                raise ResourceNotFoundError(
                    message=translate(
                        "err.ontology_axiom.not_found",
                        params={"axiom_key": axiom_key},
                        fallback=f"公理不存在: {axiom_key}",
                    ),
                )
            if expected_status and str(row.status or "") != str(expected_status):
                raise ResourceConflictError(
                    message=translate(
                        "err.ontology_axiom.status_conflict",
                        fallback="公理状态已被其他操作修改，请刷新后重试",
                    ),
                    details={"current_status": row.status},
                )
            if preview["has_violation"] and not force:
                raise ResourceConflictError(
                    message=translate(
                        "err.ontology_axiom.status_rule_violation",
                        fallback="公理状态变更会导致规则校验失败，请先处理受影响规则",
                    ),
                    details={"impact": preview},
                )
            row.status = (status or "").strip().lower()
            row.updated_by = user_id or None
            axiom = row.to_dict()

        return {**preview, "axiom": axiom, "applied": True}

    async def validate_axioms(
        self, tenant_id: str, kb_id: str, payload: dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        context = await self._context(tenant_id, kb_id)
        rows = await self._load_rows(tenant_id, kb_id)
        raw, parse_errors, parse_warnings = self._extract_payload(payload)
        if parse_errors:
            raise InvalidParameterError(
                message=translate(
                    "err.ontology_axiom.invalid_yaml",
                    fallback="公理 YAML 无效",
                ),
                details={"errors": parse_errors},
            )
        normalized, issues = check_axioms(raw, context, self._active_rows(rows))
        return {
            "contract_version": CONTRACT_VERSION,
            "axioms": [axiom_to_export_dict(a) for a in normalized],
            "issues": issues,
            "has_violation": has_violation(issues),
            "warnings": parse_warnings,
        }

    async def import_axioms(
        self,
        tenant_id: str,
        kb_id: str,
        yaml_text: str,
        *,
        user_id: str = "",
        dry_run: bool = False,
        mode: str = "merge",
        removed_axiom_keys: Optional[list[str]] = None,
    ) -> dict:
        payload, errors, warnings = parse_axiom_yaml(yaml_text)
        if errors or payload is None:
            raise InvalidParameterError(
                message=translate(
                    "err.ontology_axiom.invalid_yaml",
                    fallback="公理 YAML 无效",
                ),
                details={"errors": errors},
            )
        removed = list(payload.get("removed_axiom_keys") or [])
        for key in removed_axiom_keys or []:
            key = str(key or "").strip()
            if key and key not in removed:
                removed.append(key)
        return await self.import_axiom_list(
            tenant_id, kb_id, payload.get("axioms") or [],
            user_id=user_id, dry_run=dry_run, mode=mode,
            removed_axiom_keys=removed, warnings=warnings,
        )

    async def import_axiom_list(
        self,
        tenant_id: str,
        kb_id: str,
        axioms: list[dict],
        *,
        user_id: str = "",
        dry_run: bool = False,
        mode: str = "merge",
        removed_axiom_keys: Optional[list[str]] = None,
        schema_override: Optional[dict] = None,
        warnings: Optional[list[str]] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        mode = (mode or "merge").strip().lower()
        if mode not in {"merge", "replace"}:
            raise InvalidParameterError(
                message=translate(
                    "err.ontology_axiom.invalid_mode",
                    params={"mode": mode},
                    fallback=f"不支持的导入模式: {mode}",
                ),
            )
        warnings = list(warnings or [])
        removed = []
        for key in removed_axiom_keys or []:
            key = str(key or "").strip()
            if key and key not in removed:
                removed.append(key)

        context = await self._context(tenant_id, kb_id, schema_override)
        rows = await self._load_rows(tenant_id, kb_id)
        existing_by_key = {row.axiom_key: row for row in rows}
        existing_active = self._active_rows(rows)
        normalized, issues = check_axioms(axioms, context, existing_active)
        if has_violation(issues) and not dry_run:
            violations = [i for i in issues if i.get("severity") == "Violation"]
            raise InvalidParameterError(
                message=translate(
                    "err.ontology_axiom.validation_failed",
                    fallback="公理校验失败，未写入",
                ),
                details={"issues": violations[:50]},
            )

        plan = self._build_plan(
            normalized, existing_by_key, mode=mode, removed=removed,
        )
        if dry_run:
            return {
                "dry_run": True,
                "contract_version": CONTRACT_VERSION,
                "summary": plan["summary"],
                "plan": plan["items"],
                "issues": issues,
                "warnings": warnings,
            }

        async with self._session_cm() as session:
            repo = self._repo_cls(session)
            written = []
            for item in plan["items"]:
                key = item["axiom_key"]
                existing = existing_by_key.get(key)
                # plan 阶段读的行来自另一个 session；更新/作废前必须在当前
                # session 里重新取一次，否则改的是 detached 对象、不会落库。
                if item["action"] in {"update", "reject"}:
                    existing = await repo.get_by_key(tenant_id, kb_id, key) or existing
                if item["action"] == "unchanged":
                    continue
                if item["action"] == "reject":
                    if existing is not None:
                        existing.status = "rejected"
                        existing.content_revision = int(existing.content_revision or 0) + 1
                        existing.updated_by = user_id or None
                    continue
                if item["action"] == "conflict":
                    continue
                axiom = item["axiom"]
                if existing is None:
                    row = OntologyAxiom(
                        tenant_id=tenant_id,
                        knowledge_base_id=kb_id,
                        axiom_key=key,
                        kind=axiom["kind"],
                        subject=axiom.get("subject") or {},
                        object_ref=axiom.get("object"),
                        chain=axiom.get("chain") or [],
                        facets=axiom.get("facets") or [],
                        status=axiom.get("status") or "active",
                        source=axiom.get("source") or "imported",
                        origin=axiom.get("origin") or "imported",
                        module=axiom.get("module"),
                        description=axiom.get("description"),
                        content_revision=1,
                        content_hash=axiom.get("content_hash"),
                        created_by=user_id or None,
                    )
                    await repo.create(row)
                else:
                    existing.kind = axiom["kind"]
                    existing.subject = axiom.get("subject") or {}
                    existing.object_ref = axiom.get("object")
                    existing.chain = axiom.get("chain") or []
                    existing.facets = axiom.get("facets") or []
                    existing.status = axiom.get("status") or "active"
                    existing.source = axiom.get("source") or "imported"
                    existing.origin = axiom.get("origin") or "imported"
                    existing.module = axiom.get("module")
                    existing.description = axiom.get("description")
                    existing.content_revision = int(existing.content_revision or 0) + 1
                    existing.content_hash = axiom.get("content_hash")
                    existing.updated_by = user_id or None
                written.append({
                    "axiom_key": key,
                    "content_revision": (
                        int(existing.content_revision) if existing is not None else 1
                    ),
                })
        return {
            "dry_run": False,
            "contract_version": CONTRACT_VERSION,
            "summary": plan["summary"],
            "issues": issues,
            "warnings": warnings,
            "axioms": written,
        }

    @staticmethod
    def _extract_payload(payload: dict) -> tuple[list[dict], list[str], list[str]]:
        payload = payload or {}
        if isinstance(payload.get("axioms"), list):
            return payload["axioms"], [], []
        yaml_text = payload.get("yaml_text")
        if isinstance(yaml_text, str) and yaml_text.strip():
            parsed, errors, warnings = parse_axiom_yaml(yaml_text)
            if parsed is None:
                return [], errors, warnings
            return parsed.get("axioms") or [], [], warnings
        return [], ["缺少 axioms 或 yaml_text"], []

    @staticmethod
    def _build_plan(
        normalized: list[dict],
        existing_by_key: dict[str, OntologyAxiom],
        *,
        mode: str,
        removed: list[str],
    ) -> dict:
        items: list[dict] = []
        summary = {
            "create": 0, "update": 0, "unchanged": 0,
            "reject": 0, "conflict": 0,
        }
        incoming_keys = set()
        for axiom in normalized:
            key = axiom["axiom_key"]
            incoming_keys.add(key)
            existing = existing_by_key.get(key)
            if existing is None:
                action = "create"
            elif (existing.content_hash or "") == (axiom.get("content_hash") or ""):
                action = "unchanged"
            elif mode == "replace":
                action = "update"
            else:
                base = axiom.get("base_revision")
                if base is None or int(base) < int(existing.content_revision or 0):
                    # 无基线或基线落后：这一条视为双侧编辑冲突，跳过待人工处理。
                    action = "conflict"
                else:
                    action = "update"
            summary[action] += 1
            items.append({"axiom_key": key, "action": action, "axiom": axiom})

        for key in removed:
            if key in incoming_keys:
                continue
            if key in existing_by_key:
                items.append({"axiom_key": key, "action": "reject"})
                summary["reject"] += 1
            incoming_keys.add(key)

        if mode == "replace":
            for key in existing_by_key:
                if key not in incoming_keys:
                    items.append({"axiom_key": key, "action": "reject"})
                    summary["reject"] += 1
        return {"items": items, "summary": summary}

    async def export_axioms(self, tenant_id: str, kb_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        rows = await self._load_rows(tenant_id, kb_id)
        axioms = [row.to_dict() for row in rows]
        return {
            "filename": f"{kb_id}_ontology_axioms.yaml",
            "yaml_text": dump_axiom_yaml(axioms, kb_id),
            "count": len(axioms),
            "contract_version": CONTRACT_VERSION,
        }

    async def contract(self) -> dict:
        return contract_payload()


__all__ = ["AxiomService"]
