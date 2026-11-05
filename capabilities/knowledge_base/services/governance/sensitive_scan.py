#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""W2.7a 敏感数据分布扫描（只读建议 + 人工采纳）。

口径见 `docs/superpowers/specs/2026-11-01-w2.7a-sensitive-scan-design.md`：

- 只用**编译 schema 已存证据**（列名/描述/样例），不额外查源库、不调模型；
- 规则命中给「建议密级 + 建议标签 + 置信度」，返回样例一律**掩码**；
- 采纳是独立写动作：复用现有的 `governance_labels`（origin=manual），
  已有人工标签不覆盖；每源成功后触发一次传播。
"""
from __future__ import annotations

import re
import uuid

from sqlalchemy import select

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ...models.governance import GovernanceLabel
from ...repository.data_source_repository import KnowledgeDataSourceRepository
from ..nl2sql.schema_read import load_compiled_row
from .common import ensure_kb, schema_tables
from .label_service import MAX_NOTE, normalize_label_payload

#: 置信度排序（过滤用）。
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}
#: 等级排序（同列多规则取更高）。
_LEVEL_RANK = {"public": 0, "internal": 1, "confidential": 2, "secret": 3}
#: 采纳单次上限。
MAX_ADOPT_ITEMS = 200
#: 扫描样例最多看几个。
MAX_SAMPLES = 5
#: note 前缀（回滚按它筛）。
ADOPT_NOTE_PREFIX = "敏感扫描："

#: 列名规则：`(type_key, level, tags, keywords)`。英文关键词按词边界匹配，CJK 按子串。
_NAME_RULES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("id_card", "secret", ("PII", "ID_CARD"),
     ("idcard", "id_card", "identity_card", "identity_no", "citizen_id", "身份证", "证件号")),
    ("passport", "secret", ("PII", "PASSPORT"), ("passport", "护照")),
    ("bank_card", "secret", ("PII", "BANK_CARD"),
     ("bank_card", "bankcard", "card_no", "account_no", "银行卡", "卡号")),
    ("phone", "confidential", ("PII", "PHONE"),
     ("phone", "mobile", "telephone", "手机", "电话")),
    ("email", "confidential", ("PII", "EMAIL"), ("email", "邮箱")),
    ("address", "confidential", ("PII", "ADDRESS"), ("address", "addr", "地址", "住址")),
    ("name", "confidential", ("PII", "NAME"),
     ("full_name", "fullname", "real_name", "realname", "first_name", "firstname",
      "last_name", "lastname", "fname", "lname", "surname", "given_name", "姓名")),
    ("salary", "confidential", ("PII", "SALARY"),
     ("salary", "wage", "income", "工资", "薪资", "薪酬")),
    ("birth", "confidential", ("PII", "BIRTH"),
     ("birth", "birthday", "dob", "出生")),
)

#: 值规则：`(type_key, level, tags, regex)`；只有字符串样例参与。
_VALUE_RULES: tuple[tuple[str, str, tuple[str, ...], re.Pattern], ...] = (
    ("id_card", "secret", ("PII", "ID_CARD"), re.compile(r"^\d{17}[\dXx]$")),
    ("phone", "confidential", ("PII", "PHONE"), re.compile(r"^1[3-9]\d{9}$")),
    ("email", "confidential", ("PII", "EMAIL"),
     re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")),
    ("bank_card", "secret", ("PII", "BANK_CARD"), re.compile(r"^\d{16,19}$")),
)
#: 地址值判定（没有固定形态，用行政区/道路后缀）。
_ADDRESS_RE = re.compile(r"(省|市|区|县|路|街|号|镇|村)")
#: 明显不是姓名的列（账号名不是自然人姓名）。
_NAME_EXCLUDE = ("username", "user_name", "login", "account_name")


def _name_hit(column: str, keyword: str) -> bool:
    name = str(column or "").lower()
    if not name:
        return False
    if re.fullmatch(r"[a-z0-9_]+", keyword):
        return re.search(rf"(^|[^a-z0-9]){re.escape(keyword)}($|[^a-z0-9])", name) is not None
    return keyword in name


def _sample_has(rule_re: re.Pattern, samples: list) -> bool:
    for value in (samples or [])[:MAX_SAMPLES]:
        text = str(value or "").strip()
        if text and rule_re.search(text):
            return True
    return False


def mask_sample(value) -> str:
    """样例掩码：长度 ≤8 全掩；否则保留首尾各 4 位。"""
    text = str(value or "")
    if not text:
        return ""
    text = text[:64]
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * (len(text) - 8)}{text[-4:]}"


def classify_column(column: str, samples: list | None = None,
                    description: str | None = None) -> dict | None:
    """单列判定（纯函数）。无命中返回 None。

    返回 `{signals, level, tags, confidence, sample_masked}`。
    """
    samples = list(samples or [])
    name_lower = str(column or "").lower()
    signals: list[str] = []
    levels: list[str] = []
    tags: list[str] = []

    def add(tag_list):
        for t in tag_list:
            if t not in tags:
                tags.append(t)

    # 名称规则按「更具体优先」排列，只取**第一条**命中：`id_card_no` 不应再被
    # `card_no` 判成银行卡、`email_address` 不应再被判成地址。值规则仍可多命，
    # 由下面的身份证优先去重兜住。
    for key, level, tag_list, keywords in _NAME_RULES:
        if key == "name" and any(x in name_lower for x in _NAME_EXCLUDE):
            continue
        # `income_id` / `phone_id` 这类是**标识列**，不是该语义的值列。
        if name_lower.endswith("_id"):
            continue
        if any(_name_hit(column, kw) for kw in keywords):
            signals.append(f"name:{key}")
            levels.append(level)
            add(tag_list)
            break

    if not signals and name_lower == "name":
        signals.append("name:name")
        levels.append("confidential")
        add(("PII", "NAME"))

    value_hit = False
    for key, level, tag_list, regex in _VALUE_RULES:
        if _sample_has(regex, samples):
            # 18 位纯数字既像身份证也像卡号：身份证优先，不再记一次银行卡
            if key == "bank_card" and any(s.startswith("value:id_card") for s in signals):
                continue
            signals.append(f"value:{key}")
            levels.append(level)
            add(tag_list)
            value_hit = True
    if any(_ADDRESS_RE.search(str(v)) for v in samples[:MAX_SAMPLES] if str(v).strip()) \
            and len(str(samples[0])) >= 8:
        signals.append("value:address")
        levels.append("confidential")
        add(("PII", "ADDRESS"))
        value_hit = True

    if not signals:
        return None
    level = max(levels, key=lambda x: _LEVEL_RANK.get(x, 0))
    name_hits = sum(1 for sig in signals if sig.startswith("name:"))
    if value_hit and name_hits:
        confidence = "high"
    elif value_hit:
        confidence = "medium"
    elif samples:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "signals": signals,
        "level": level,
        "tags": tags,
        "confidence": confidence,
        "sample_masked": mask_sample(samples[0]) if samples else "",
    }


async def _resolve_sources(session, tenant_id: str, kb_id: str,
                           ds_ids: list[str] | None) -> list:
    rows = await KnowledgeDataSourceRepository(session).list_by_kb(
        tenant_id, kb_id, offset=0, limit=500)
    by_id = {r.id: r for r in rows}
    if ds_ids:
        missing = [d for d in ds_ids if d not in by_id]
        if missing:
            raise InvalidParameterError(f"数据源不存在或不属于该知识库：{missing[0]}")
        return [by_id[d] for d in ds_ids]
    return [r for r in rows if str(getattr(r, "access_type", "") or "") == "database"]


async def _existing_labels(session, tenant_id: str, kb_id: str,
                           ds_id: str) -> dict[tuple, list[GovernanceLabel]]:
    rows = list((await session.execute(select(GovernanceLabel).where(
        GovernanceLabel.tenant_id == tenant_id,
        GovernanceLabel.knowledge_base_id == kb_id,
        GovernanceLabel.data_source_id == ds_id,
        GovernanceLabel.is_deleted == 0,
    ))).scalars().all())
    out: dict[tuple, list[GovernanceLabel]] = {}
    for r in rows:
        out.setdefault((r.table_name, r.column_name), []).append(r)
    return out


async def scan(tenant_id: str, kb_id: str, *, session,
               data_source_ids: list[str] | None = None,
               min_confidence: str = "medium") -> dict:
    """扫描 KB 下的数据库源，返回候选（只读，不写任何行）。"""
    tenant_id = require_tenant(tenant_id)
    if min_confidence not in _CONF_RANK:
        raise InvalidParameterError("min_confidence 只能是 low/medium/high")
    await ensure_kb(session, kb_id, tenant_id)
    sources = await _resolve_sources(session, tenant_id, kb_id, data_source_ids)

    items: list[dict] = []
    stats = {"sources": 0, "tables": 0, "columns": 0, "candidates": 0,
             "by_level": {}, "warnings": []}
    for source in sources:
        ds_id = source.id
        try:
            row = await load_compiled_row(tenant_id, kb_id, ds_id)
        except Exception as exc:  # noqa: BLE001 —— 单源失败不拖垮整次扫描
            stats["warnings"].append(f"{ds_id}: {type(exc).__name__}")
            continue
        if not row:
            stats["warnings"].append(f"{ds_id}: 无编译 schema")
            continue
        stats["sources"] += 1
        labels = await _existing_labels(session, tenant_id, kb_id, ds_id)
        evidence = row.get("evidence") or {}
        for entity in row.get("entity_types") or []:
            if not isinstance(entity, dict):
                continue
            table = str(entity.get("source_object_id") or entity.get("name") or "").strip()
            if not table:
                continue
            stats["tables"] += 1
            table_ev = ((evidence.get(table) or {}).get("columns") or {})
            for attr in entity.get("attributes") or []:
                if not isinstance(attr, dict):
                    continue
                column = str(attr.get("name") or attr.get("physical_name") or "").strip()
                if not column:
                    continue
                stats["columns"] += 1
                ev = table_ev.get(column) or table_ev.get(
                    str(attr.get("physical_name") or "")) or {}
                samples = list(ev.get("samples") or [])[:MAX_SAMPLES]
                verdict = classify_column(column, samples,
                                          attr.get("description") or "")
                if verdict is None:
                    continue
                if _CONF_RANK[verdict["confidence"]] < _CONF_RANK[min_confidence]:
                    continue
                existing = labels.get((table, column)) or []
                current = next((x for x in existing if x.origin == "manual"), None)
                if current is not None:
                    status = "labeled"
                elif existing:
                    status = "derived_present"
                else:
                    status = "candidate"
                item = {
                    "data_source_id": ds_id,
                    "table": table,
                    "column": column,
                    "matched": verdict["signals"],
                    "suggested_level": verdict["level"],
                    "suggested_tags": verdict["tags"],
                    "confidence": verdict["confidence"],
                    "status": status,
                    "current_level": current.level if current else (
                        existing[0].level if existing else None),
                    "current_tags": list(
                        (current.tags if current else existing[0].tags) or []
                    ) if (current or existing) else [],
                    "sample_masked": verdict["sample_masked"],
                }
                items.append(item)
    items.sort(key=lambda x: (-_CONF_RANK[x["confidence"]], x["data_source_id"],
                              x["table"], x["column"]))
    stats["candidates"] = len(items)
    for item in items:
        stats["by_level"][item["suggested_level"]] = \
            stats["by_level"].get(item["suggested_level"], 0) + 1
    return {"items": items, "stats": stats}


async def adopt(tenant_id: str, kb_id: str, items: list[dict], *,
                session, user_id: str | None = None,
                request_id: str | None = None) -> dict:
    """人工采纳扫描候选：逐项校验、写过标签、按源传播、一条审计。"""
    tenant_id = require_tenant(tenant_id)
    if not isinstance(items, list) or not items:
        raise InvalidParameterError("items 不能为空")
    if len(items) > MAX_ADOPT_ITEMS:
        raise InvalidParameterError(f"一次最多采纳 {MAX_ADOPT_ITEMS} 条")
    await ensure_kb(session, kb_id, tenant_id)

    tables_cache: dict[str, dict[str, set[str]]] = {}
    results = []
    created = skipped = 0
    touched_sources: set[str] = set()
    for raw in items:
        payload = dict(raw) if isinstance(raw, dict) else {}
        if "table_name" not in payload and "table" in payload:
            payload["table_name"] = payload.get("table")
        if "column_name" not in payload and "column" in payload:
            payload["column_name"] = payload.get("column")
        try:
            got = normalize_label_payload(payload)
        except InvalidParameterError as exc:
            results.append({"item": _echo_item(raw), "status": "invalid",
                            "reason": str(exc)})
            continue
        ds_id = got["data_source_id"]
        if ds_id not in tables_cache:
            tables_cache[ds_id] = await schema_tables(session, tenant_id, kb_id, ds_id)
        cols = tables_cache[ds_id].get(got["table_name"])
        if cols is None or (got["column_name"] and got["column_name"] not in cols):
            results.append({"item": _echo_item(raw), "status": "invalid",
                            "reason": "列不在编译 schema 里"})
            continue
        exists = (await session.execute(select(GovernanceLabel.id).where(
            GovernanceLabel.tenant_id == tenant_id,
            GovernanceLabel.knowledge_base_id == kb_id,
            GovernanceLabel.data_source_id == ds_id,
            GovernanceLabel.table_name == got["table_name"],
            GovernanceLabel.column_name == got["column_name"]
            if got["column_name"] is not None else GovernanceLabel.column_name.is_(None),
            GovernanceLabel.origin == "manual",
            GovernanceLabel.is_deleted == 0,
        ))).first()
        if exists is not None:
            skipped += 1
            results.append({"item": _echo_item(raw), "status": "exists"})
            continue
        note = got["note"] or f"{got['level']} {'/'.join(got['tags'])}"
        note = f"{ADOPT_NOTE_PREFIX}{note}"[:MAX_NOTE]
        session.add(GovernanceLabel(
            id=uuid.uuid4().hex, tenant_id=tenant_id, knowledge_base_id=kb_id,
            data_source_id=ds_id, table_name=got["table_name"],
            column_name=got["column_name"], level=got["level"], tags=got["tags"],
            origin="manual", note=note, created_by=user_id,
        ))
        created += 1
        touched_sources.add(ds_id)
        results.append({"item": _echo_item(raw), "status": "created"})
    if created:
        await session.commit()
    # 传播按源 best-effort；失败只 warning（与标签服务同姿态）。
    from .propagation import propagate_for_source
    for ds_id in touched_sources:
        try:
            await propagate_for_source(session, tenant_id, kb_id, ds_id)
        except Exception:  # noqa: BLE001
            pass
    # 用**平台操作审计**（`platform.audit_logs`），不是 PDP 决策审计：
    # `governance_decision_audit.action` 只允许 select/predicate/aggregate。
    # 该助手是旁路，失败只 warning，不会回滚已写标签。
    from ..evolution.audit import record as audit_record
    await audit_record(
        session, tenant_id=tenant_id,
        action="governance.adopt_sensitive_scan",
        run_id=f"scan:{kb_id}", resource="governance_scan",
        request_params={"created": created, "skipped": skipped,
                        "items": len(items)},
        user_id=user_id)
    return {"created": created, "skipped": skipped, "items": results}


def _echo_item(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"data_source_id": raw.get("data_source_id"),
            "table": raw.get("table_name") or raw.get("table"),
            "column": raw.get("column_name") or raw.get("column")}


__all__ = ["classify_column", "mask_sample", "scan", "adopt",
           "MAX_ADOPT_ITEMS", "ADOPT_NOTE_PREFIX"]
