"""W2.6 落标绑定服务：CRUD、建议与覆盖率。

- 读（列表/建议/覆盖率）任意登录用户；写（建/改/删）仅租户管理员；
- 绑定身份是 ``(standard_kind, standard_code)``；创建时校验 code 在租户内
  存在任意未删版本（1002），资产复用 W2.1 目录 ``CatalogService.get_asset``
  （不存在同样 1002）；
- 同一 ``(kind, code, asset)`` 至多一条 active：唯一索引冲突统一映射 1003；
  disabled / 软删不占位，可重新创建；
- 更新支持 ``note`` / ``status``，带 ``expected_revision`` 乐观锁（失配 1003）；
  删除是软删；
- 覆盖率只统计 published 标准（每个 ``(kind, code)`` 一条），汇总
  ``total_standards / bound_standards / binding_count / coverage``；
- 建议只读、不落库；归一后完全相等 1.0、包含 0.8、别名 0.7；
  ``term`` 不自动建议；``limit`` 默认 20、上限 100；
- 审计 resource 固定字符串 ``standard_binding``（任务 5 才登记枚举），
  摘要只记标识与状态，不落 note 全文。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.exc import IntegrityError

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from ..repository import binding_repository as repo
from .catalog_service import CatalogService
from .quality.common import audit, paging, require_admin, require_login
from .standards.validation import KINDS

logger = logging.getLogger(__name__)

#: 绑定可指向的资产类型（与迁移 CHECK 一致）。
ASSET_TYPES = frozenset({"table", "column", "metric"})
STATUSES = frozenset({"active", "disabled"})
SOURCES = frozenset({"manual", "suggested"})

_RESOURCE = "standard_binding"
_MAX_NOTE = 512
_MAX_CODE = 128
_MAX_ASSET_ID = 255
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

#: 建议匹配的资产类型：数据元 → 列，指标口径卡 → 指标；表与术语不自动建议。
_SUGGESTION_ASSET_TYPES = {
    "data_element": "column",
    "metric_card": "metric",
}

#: 目录分页拉取（与任务 2 同口径，防止上游 total 异常导致死循环）。
_ASSET_PAGE_SIZE = 100
_MAX_ASSET_PAGES = 100


# ----------------------------------------------------------------------
# 校验与归一
# ----------------------------------------------------------------------


def _invalid(message: str, **details) -> None:
    raise InvalidParameterError(message=message, details=details or None)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _optional_enum(data: dict, key: str, allowed, *, field: str | None = None):
    value = data.get(key)
    if value in (None, ""):
        return None
    # 非字符串（list/dict 等）也不可哈希，先挡住避免 `in frozenset` 抛 TypeError。
    if not isinstance(value, str) or value not in allowed:
        _invalid(f"{field or key} 非法", field=field or key, value=value)
    return value


def _assert_clean_text(value: str, key: str) -> None:
    """NUL 与孤立代理项会被 asyncpg 拒绝，提前回 1001。"""
    if "\x00" in value:
        _invalid(f"{key} 不能包含 NUL 字符", field=key)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid(f"{key} 含非法 Unicode 字符", field=key)


def _optional_text(data: dict, key: str, *, max_len: int | None = None):
    value = data.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        _invalid(f"{key} 必须是字符串", field=key)
    value = value.strip()
    if not value:
        return None
    _assert_clean_text(value, key)
    if max_len is not None and len(value) > max_len:
        _invalid(f"{key} 不能超过 {max_len} 字符", field=key)
    return value


def _required_text(value, key: str, *, max_len: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{key} 必填", field=key)
    value = value.strip()
    _assert_clean_text(value, key)
    if max_len is not None and len(value) > max_len:
        _invalid(f"{key} 不能超过 {max_len} 字符", field=key)
    return value


def _require_revision(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid("expected_revision 必须是整数", field="expected_revision")
    return value


def _require_limit(value) -> int:
    if value in (None, ""):
        return _DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid("limit 必须是整数", field="limit")
    if value < 1 or value > _MAX_LIMIT:
        _invalid(f"limit 必须在 1..{_MAX_LIMIT} 之间", field="limit")
    return value


def _require_confidence(value, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid("confidence 必须是数字", field="confidence")
    if value < 0 or value > 1:
        _invalid("confidence 必须在 0..1 之间", field="confidence")
    return float(value)


def _constraint_name(exc: IntegrityError) -> str | None:
    """唯一约束名：psycopg 有 ``diag``，asyncpg 只回消息文本，需正则回退。"""
    orig = getattr(exc, "orig", None)
    name = getattr(orig, "constraint_name", None)
    if name:
        return name
    name = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if name:
        return name
    match = re.search(r'unique constraint "([^"]+)"', str(exc))
    return match.group(1) if match else None


def _payload_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _pick(row: dict, *keys):
    for key in keys:
        if key in row:
            return row[key]
    return None


def _asset_type_of(row: dict) -> str:
    return _text(_pick(row, "asset_type", "assetType")).lower()


def _asset_id_of(row: dict) -> str:
    return _text(_pick(row, "asset_id", "assetId"))


def _asset_name_of(row: dict) -> str:
    return _text(_pick(row, "name", "asset_name", "assetName"))


# ----------------------------------------------------------------------
# 建议匹配
# ----------------------------------------------------------------------


def _normalize_key(value) -> str:
    """比较键：casefold，去 ``{ds}:`` 前缀与表名前缀，去 ``_ - 空格``。

    ``ds:dbo.orders.customer_name`` → ``customername``；
    ``customer_name`` / ``Customer Name`` → ``customername``。
    """
    text = _text(value).casefold()
    if not text:
        return ""
    if ":" in text:
        text = text.split(":", 1)[1]
    if "." in text:
        text = text.rsplit(".", 1)[1]
    for char in ("_", "-", " "):
        text = text.replace(char, "")
    return text


def _standard_keys(standard: dict) -> list[tuple[str, float]]:
    """标准侧候选键：code/name（强）与 payload.aliases（弱，0.7）。"""
    keys: list[tuple[str, float]] = []
    for field in ("code", "name"):
        key = _normalize_key(standard.get(field))
        if key:
            keys.append((key, 1.0))
    for alias in _payload_dict(standard.get("payload")).get("aliases") or []:
        key = _normalize_key(alias)
        if key:
            keys.append((key, 0.7))
    return keys


def _match_score(standard: dict, asset_key: str) -> tuple[float, str | None]:
    """返回 (confidence, reason)；三档：完全相等 1.0 / 包含 0.8 / 别名 0.7。"""
    best_score = 0.0
    best_reason = None
    for key, weight in _standard_keys(standard):
        if not key or not asset_key:
            continue
        if key == asset_key:
            score = 1.0 if weight >= 1.0 else 0.7
            reason = "exact" if weight >= 1.0 else "alias"
        elif key in asset_key or asset_key in key:
            score = 0.8 if weight >= 1.0 else 0.7
            reason = "contains" if weight >= 1.0 else "alias"
        else:
            continue
        if score > best_score:
            best_score = score
            best_reason = reason
    return best_score, best_reason


def _suggestion(standard: dict, row: dict, score: float, reason: str) -> dict:
    standard_name = _text(standard.get("name"))
    return {
        "standard_kind": standard.get("kind"),
        "standard_code": standard.get("code"),
        "standard_name": standard_name,
        # 兼容 spec 里两处同名 `name`：顶层 name 指标准名，资产名用 asset_name。
        "name": standard_name,
        "asset_type": _asset_type_of(row),
        "asset_id": _asset_id_of(row),
        "asset_name": _asset_name_of(row),
        "confidence": score,
        "reason": reason,
    }


# ----------------------------------------------------------------------
# 服务
# ----------------------------------------------------------------------


class BindingService:
    """落标绑定的读/写与覆盖率聚合。"""

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def list_bindings(self, tenant_id, user_id, data: dict | None) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看落标绑定")
        data = data or {}
        page, page_size = paging(data)
        filters = {
            "standard_kind": _optional_enum(data, "standard_kind", KINDS),
            "standard_code": _optional_text(data, "standard_code", max_len=_MAX_CODE),
            "asset_type": _optional_enum(data, "asset_type", ASSET_TYPES),
            "asset_id": _optional_text(data, "asset_id", max_len=_MAX_ASSET_ID),
            "status": _optional_enum(data, "status", STATUSES),
        }
        async with get_db_session() as session:
            items = await repo.list_page(
                session, tenant_id=tenant_id, page=page, page_size=page_size,
                filters=filters,
            )
            total = await repo.count(session, tenant_id=tenant_id, filters=filters)
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def suggest_bindings(self, tenant_id, user_id, data: dict | None) -> dict:
        """只读建议候选：不落库、不写审计。"""
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看落标建议")
        data = data or {}

        kind = _optional_enum(data, "standard_kind", KINDS)
        code = _optional_text(data, "standard_code", max_len=_MAX_CODE)
        asset_type = _optional_enum(data, "asset_type", ASSET_TYPES)
        asset_id = _optional_text(data, "asset_id", max_len=_MAX_ASSET_ID)
        limit = _require_limit(data.get("limit"))

        # 术语没有稳定的列名锚点；表也没有可自动匹配的标准类型。
        if kind == "term" or asset_type == "table":
            return {"items": [], "truncated": False}

        async with get_db_session() as session:
            standards = await repo.published_standard_rows(
                session, tenant_id=tenant_id, kind=kind, code=code,
            )

        standards = [
            standard for standard in standards
            if standard.get("kind") in _SUGGESTION_ASSET_TYPES
        ]
        if not standards:
            return {"items": [], "truncated": False}

        assets, truncated = await self._load_suggestion_assets(
            tenant_id, user_id, asset_type,
        )
        if asset_id:
            assets = [
                item for item in assets if _asset_id_of(item) == asset_id
            ]

        best: dict[tuple, dict] = {}
        for standard in standards:
            expected_type = _SUGGESTION_ASSET_TYPES.get(standard.get("kind"))
            if expected_type is None:
                continue
            for row in assets:
                if _asset_type_of(row) != expected_type:
                    continue
                key = _normalize_key(_asset_name_of(row))
                if not key:
                    continue
                score, reason = _match_score(standard, key)
                if score <= 0 or reason is None:
                    continue
                suggestion = _suggestion(standard, row, score, reason)
                dedupe_key = (
                    suggestion["standard_kind"], suggestion["standard_code"],
                    suggestion["asset_type"], suggestion["asset_id"],
                )
                prior = best.get(dedupe_key)
                if prior is None or suggestion["confidence"] > prior["confidence"]:
                    best[dedupe_key] = suggestion

        items = sorted(
            best.values(),
            key=lambda item: (
                -item["confidence"], item["asset_id"], item["standard_code"],
            ),
        )
        return {"items": items[:limit], "truncated": truncated}

    async def _load_suggestion_assets(
        self, tenant_id: str, user_id, asset_type: str | None,
    ) -> tuple[list[dict], bool]:
        """目录分页拉取候选资产；短页 / 达到 total / 页数上限即停。

        返回 `(assets, truncated)`：命中 `_MAX_ASSET_PAGES` 时截断必须显式
        返回给调用方，不能静默只建议前 1 万个资产。
        """
        assets: list[dict] = []
        page = 1
        truncated = False
        while True:
            payload: dict[str, Any] = {
                "page": page,
                "page_size": _ASSET_PAGE_SIZE,
            }
            if asset_type:
                payload["asset_type"] = asset_type
            # 建议只匹配名称，用不到质量档位：跳过 KB 质量投影。
            result = await CatalogService().list_assets(
                tenant_id, user_id, payload, include_quality=False,
            )
            items = (result or {}).get("items") or []
            assets.extend(items)
            total = (result or {}).get("total")
            if not items or len(items) < _ASSET_PAGE_SIZE:
                break
            if isinstance(total, int) and len(assets) >= total:
                break
            if page >= _MAX_ASSET_PAGES:
                truncated = True
                logger.warning(
                    "suggestion asset pagination truncated at %d pages",
                    _MAX_ASSET_PAGES,
                )
                break
            page += 1
        return assets, truncated

    async def coverage(self, tenant_id, user_id, data: dict | None) -> dict:
        """覆盖率视图：只统计 published 标准；汇总按全量筛选集计算。"""
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看落标覆盖率")
        data = data or {}

        kind = _optional_enum(data, "kind", KINDS)
        code = _optional_text(data, "code", max_len=_MAX_CODE)
        domain = _optional_text(data, "domain")
        page, page_size = paging(data)

        async with get_db_session() as session:
            rows = await repo.coverage_rows(
                session, tenant_id=tenant_id, kind=kind, code=code,
                domain=domain, page=page, page_size=page_size,
            )
            summary = await repo.coverage_summary(
                session, tenant_id=tenant_id, kind=kind, code=code,
                domain=domain,
            )

        items = [
            {
                **row,
                "unbound": int(row.get("bindings_count") or 0) == 0,
            }
            for row in rows
        ]
        total = int(summary.get("total_standards") or 0)
        bound = int(summary.get("bound_standards") or 0)
        result = {
            "total_standards": total,
            "bound_standards": bound,
            "binding_count": int(summary.get("binding_count") or 0),
            # total=0 时没有分母：显式 null，前端展示「—」。
            "coverage": (bound / total) if total else None,
        }
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "summary": result,
            # 兼容直接读顶层汇总的调用方。
            **result,
        }

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def create_binding(self, tenant_id, user_id, data: dict | None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可维护落标绑定")
        data = data or {}

        kind = data.get("standard_kind")
        if not isinstance(kind, str) or kind not in KINDS:
            _invalid("standard_kind 非法", field="standard_kind", value=kind)
        code = _required_text(
            data.get("standard_code"), "standard_code", max_len=_MAX_CODE,
        )
        asset_type = data.get("asset_type")
        if not isinstance(asset_type, str) or asset_type not in ASSET_TYPES:
            _invalid("asset_type 非法", field="asset_type", value=asset_type)
        requested_asset_id = _required_text(
            data.get("asset_id"), "asset_id", max_len=_MAX_ASSET_ID,
        )
        source = data.get("source") or "manual"
        if not isinstance(source, str) or source not in SOURCES:
            _invalid("source 非法", field="source", value=source)
        confidence = _require_confidence(
            data.get("confidence"), 1.0 if source == "manual" else 0.0,
        )
        note = _optional_text(data, "note", max_len=_MAX_NOTE)

        # 资产存在性走 W2.1 权威目录；不存在/不可见一律 1002（跨租户同语义）。
        asset = await CatalogService().get_asset(
            tenant_id, user_id, asset_type, requested_asset_id,
            include_quality=False,
        )
        canonical_type = _text(asset.get("asset_type")) or asset_type
        canonical_id = _text(asset.get("asset_id")) or requested_asset_id

        async with get_db_session() as session:
            standard = await repo.standard_row_for_code(
                session, tenant_id=tenant_id, kind=kind, code=code,
            )
            if standard is None:
                raise ResourceNotFoundError(message="标准资产不存在")
            # 只记录已发布行 id；只有草稿/退役时留空（读时再解析）。
            standard_asset_id = (
                standard.get("id")
                if standard.get("status") == "published" else None
            )
            try:
                row = await repo.create(
                    session, tenant_id=tenant_id, standard_kind=kind,
                    standard_code=code, standard_asset_id=standard_asset_id,
                    asset_type=canonical_type, asset_id=canonical_id,
                    source=source, confidence=confidence, note=note,
                    created_by=user_id or None, updated_by=user_id or None,
                )
            except IntegrityError as exc:
                if _constraint_name(exc) == "uq_sab_active":
                    raise ResourceConflictError(
                        message="该标准已绑定此资产",
                    ) from exc
                raise

        audit(
            tenant_id=tenant_id, user_id=user_id,
            action="standard_binding_create", resource=_RESOURCE,
            resource_id=row["id"], params=_summary(row),
        )
        return row

    async def update_binding(
        self, tenant_id, user_id, binding_id, expected_revision, data: dict | None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可维护落标绑定")
        revision = _require_revision(expected_revision)
        data = data or {}

        fields: dict[str, Any] = {}
        if "note" in data:
            # 与 create 同源：NUL/孤立代理项 1001、超长 1001、空串归一为 None
            #（显式 null / "" 表示清除 note）。
            fields["note"] = _optional_text(data, "note", max_len=_MAX_NOTE)
        if "status" in data:
            status = data.get("status")
            if not isinstance(status, str) or status not in STATUSES:
                _invalid("status 非法", field="status", value=status)
            fields["status"] = status
        if not fields:
            _invalid("没有可更新的字段")

        async with get_db_session() as session:
            existing = await repo.get_by_id(
                session, tenant_id=tenant_id, binding_id=binding_id,
            )
            if existing is None:
                raise ResourceNotFoundError(message="落标绑定不存在")
            try:
                changed = await repo.update(
                    session, tenant_id=tenant_id, binding_id=binding_id,
                    expected_revision=revision, user_id=user_id or None,
                    fields=fields,
                )
            except IntegrityError as exc:
                if _constraint_name(exc) == "uq_sab_active":
                    raise ResourceConflictError(
                        message="该标准已绑定此资产",
                    ) from exc
                raise
            if changed == 0:
                raise ResourceConflictError(
                    message="绑定已被修改，请刷新后重试",
                    details={"binding_id": binding_id, "expected_revision": revision},
                )
            fresh = await repo.get_by_id(
                session, tenant_id=tenant_id, binding_id=binding_id,
            )

        row = fresh or existing
        audit(
            tenant_id=tenant_id, user_id=user_id,
            action="standard_binding_update", resource=_RESOURCE,
            resource_id=binding_id,
            params={
                "changed": sorted(fields),
                "before_status": existing.get("status"),
                "after_status": row.get("status"),
                "revision": row.get("revision"),
                **_summary(row),
            },
        )
        return row

    async def delete_binding(self, tenant_id, user_id, binding_id) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可维护落标绑定")

        async with get_db_session() as session:
            existing = await repo.get_by_id(
                session, tenant_id=tenant_id, binding_id=binding_id,
            )
            if existing is None:
                raise ResourceNotFoundError(message="落标绑定不存在")
            changed = await repo.soft_delete(
                session, tenant_id=tenant_id, binding_id=binding_id,
                user_id=user_id or None,
            )
            if changed == 0:
                raise ResourceNotFoundError(message="落标绑定不存在")

        audit(
            tenant_id=tenant_id, user_id=user_id,
            action="standard_binding_delete", resource=_RESOURCE,
            resource_id=binding_id, params=_summary(existing),
        )
        return {"binding_id": binding_id, "deleted": True}


def _summary(row: dict) -> dict:
    """审计摘要：只记标识与状态，不落 note 全文。"""
    return {
        "kind": row.get("standard_kind"),
        "code": row.get("standard_code"),
        "asset_type": row.get("asset_type"),
        "asset_id": row.get("asset_id"),
        "source": row.get("source"),
        "status": row.get("status"),
    }
