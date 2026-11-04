"""W2.6 资源视图：业务域 / 责任聚合（读时聚合，不复制资产主数据）。

- 资产清单复用 `CatalogService.list_assets` 分页拉取（page_size=100），
  本模块只做分组与计数；不写任何表、不写审计。
- 分组键、计数口径与 `docs/superpowers/specs/2026-10-19-w2-resource-views-design.md`
  §3/§4 一致：
  - 业务域视图：`(space_id, space_name, domain)`，空 domain → `__unassigned__`；
  - 责任视图：`owner`（空 → `__unowned__`）或 `domain`。
- 质量问题工单读 `data_governance.quality_issues`（未关闭、未软删），
  按 `asset_id` 分组后挂到资产所在分组。
- owner 匹配 `platform.users` 只影响 `display_name`，不改变归属。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from .catalog_service import CatalogService

logger = logging.getLogger(__name__)

#: 参与聚合的五类资产（目录里的其他类型一律忽略）。
ASSET_TYPES: tuple[str, ...] = (
    "table", "column", "metric", "document", "data_source",
)

#: 分级五档固定顺序；`unclassified` 只计空值。
SENSITIVITY_BUCKETS: tuple[str, ...] = (
    "public", "internal", "confidential", "secret", "unclassified",
)

#: 未关闭质量工单四档（quality_issues.severity）。
ISSUE_SEVERITIES: tuple[str, ...] = (
    "critical", "error", "warning", "info",
)

GROUP_BY_VALUES = frozenset({"owner", "domain"})
UNASSIGNED_DOMAIN = "__unassigned__"
UNOWNED = "__unowned__"

#: 分页拉取参数：与目录 API 的 page_size 上限一致。
ASSET_PAGE_SIZE = 100
#: 单次视图最多拉 100 页（10000 资产）；超出记 warning 后截断，避免死循环。
MAX_ASSET_PAGES = 100
#: 分组内 owner 展示上限。
OWNERS_LIMIT = 10


def _text(value) -> str:
    """任意值 → 去空白字符串；None → ''。"""
    if value is None:
        return ""
    return str(value).strip()


def _pick(row: dict, *keys):
    """按顺序取第一个存在的键（兼容 snake_case 与 camelCase 两种行形状）。"""
    for key in keys:
        if key in row:
            return row[key]
    return None


def _asset_type(row: dict) -> str:
    return _text(_pick(row, "asset_type", "assetType")).lower()


def _asset_id(row: dict) -> str:
    return _text(_pick(row, "asset_id", "assetId"))


def _space_id(row: dict) -> str | None:
    return _text(_pick(row, "space_id", "spaceId")) or None


def _owner(row: dict) -> str | None:
    return _text(_pick(row, "owner")) or None


def _domain(row: dict) -> str | None:
    return _text(_pick(row, "domain")) or None


def _sensitivity(row: dict) -> str | None:
    return _text(_pick(row, "sensitivity")).lower() or None


def _updated_at(row: dict) -> str | None:
    """行上的更新时间 → 可比较的 ISO 文本（None 表示缺失）。"""
    value = _pick(row, "updated_at", "updatedAt")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text_value = _text(value)
    return text_value or None


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value) or None


def _empty_asset_counts() -> dict[str, int]:
    return {asset_type: 0 for asset_type in ASSET_TYPES}


def _empty_sensitivity() -> dict[str, int]:
    return {bucket: 0 for bucket in SENSITIVITY_BUCKETS}


def _empty_issues() -> dict[str, int]:
    return {severity: 0 for severity in ISSUE_SEVERITIES}


def _new_bucket() -> dict:
    return {
        "asset_counts": _empty_asset_counts(),
        "sensitivity": _empty_sensitivity(),
        "open_issues": _empty_issues(),
        "owners": {},
        "last_updated_at": None,
    }


def _accumulate(bucket: dict, asset: dict, issue_counts: dict) -> None:
    """把一条资产累加进分组桶（调用方已保证 asset_type 合法）。"""
    bucket["asset_counts"][_asset_type(asset)] += 1

    level = _sensitivity(asset)
    if not level:
        bucket["sensitivity"]["unclassified"] += 1
    elif level in bucket["sensitivity"]:
        bucket["sensitivity"][level] += 1
    # 词表外的自定义级别不在固定五档内：不计数（口径见 spec §3）。

    owner = _owner(asset)
    if owner:
        bucket["owners"][owner] = bucket["owners"].get(owner, 0) + 1

    asset_id = _asset_id(asset)
    if asset_id:
        for severity, count in (issue_counts.get(asset_id) or {}).items():
            if severity in bucket["open_issues"]:
                bucket["open_issues"][severity] += int(count or 0)

    updated = _updated_at(asset)
    if updated and (
        bucket["last_updated_at"] is None
        or updated > bucket["last_updated_at"]
    ):
        bucket["last_updated_at"] = updated


def _owners_top(owners: dict[str, int], limit: int = OWNERS_LIMIT) -> list[dict]:
    """按 (count desc, owner asc) 取前 N 个责任人，保证输出确定。"""
    ordered = sorted(owners.items(), key=lambda item: (-item[1], item[0]))
    return [
        {"owner": owner, "count": count}
        for owner, count in ordered[:limit]
    ]


def _summary(bucket: dict, *, with_owners: bool = True) -> dict:
    summary = {
        "asset_counts": dict(bucket["asset_counts"]),
        "sensitivity": dict(bucket["sensitivity"]),
        "open_issues": dict(bucket["open_issues"]),
    }
    if with_owners:
        summary["owner_count"] = len(bucket["owners"])
        summary["owners"] = _owners_top(bucket["owners"])
    return summary


def _require_login(user_id) -> None:
    """读视图要求登录用户（与 W2.5 读接口同口径）。"""
    if not user_id or user_id == "anonymous":
        raise PermissionDeniedError(message="需要登录后查看资源视图")


class ResourceViewService:
    """业务域视图 / 责任视图（只读聚合）。"""

    # ── 数据装载 ────────────────────────────────────────────────

    async def _load_assets(
        self, catalog, tenant_id: str, user_id, space_id: str | None,
    ) -> tuple[list[dict], bool]:
        """分页拉取租户内全部资产（最多 `MAX_ASSET_PAGES` 页）。

        停止条件（任一）：
        - 本页条数 < page_size（短页 = 已经到底）；
        - 累计条数 >= `total`（目录给出了总数）；
        - 已达 100 页上限（记 warning，返回 `truncated=True`）。

        返回 `(assets, truncated)`：截断必须在响应里显式化，前端才能区分
        「真的只有这些」与「只统计了前 10000 个资产」。
        """
        assets: list[dict] = []
        fetched = 0
        page = 1
        truncated = False
        while True:
            payload = {
                "asset_type": "all",
                # 目录服务的筛选键是 `domain_space_id`；同时带 `space_id`
                # 以便未来目录直接接受该键时无需改本模块。
                "domain_space_id": space_id,
                "space_id": space_id,
                "page": page,
                "page_size": ASSET_PAGE_SIZE,
            }
            # 聚合只读主数据：逐页触达 KB 质量投影既慢又无消费方。
            result = await catalog.list_assets(
                tenant_id, user_id, payload, include_quality=False,
            ) or {}
            items = list(result.get("items") or [])
            fetched += len(items)
            assets.extend(
                item for item in items if _asset_type(item) in ASSET_TYPES
            )

            if len(items) < ASSET_PAGE_SIZE:
                break
            total = result.get("total")
            if total is not None:
                try:
                    total_value = int(total)
                except (TypeError, ValueError):
                    total_value = None
                if total_value is not None and fetched >= total_value:
                    break
            if page >= MAX_ASSET_PAGES:
                truncated = True
                logger.warning(
                    "resource view asset paging hit %s pages; result truncated "
                    "(tenant=%s space=%s)",
                    MAX_ASSET_PAGES, tenant_id, space_id,
                )
                break
            page += 1
        return assets, truncated

    async def _load_issue_counts(self, tenant_id: str) -> dict[str, dict[str, int]]:
        """未关闭质量工单按 `asset_id` × severity 计数。"""
        sql = (
            "SELECT asset_id, severity, count(*) AS issue_count "
            "FROM data_governance.quality_issues "
            "WHERE tenant_id = :tenant_id "
            "AND status <> 'closed' AND is_deleted = 0 "
            "GROUP BY asset_id, severity"
        )
        async with get_db_session() as session:
            rows = (
                await session.execute(text(sql), {"tenant_id": tenant_id})
            ).mappings().all()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            asset_id = _text(row.get("asset_id"))
            severity = _text(row.get("severity")).lower()
            if not asset_id or not severity:
                continue
            bucket = counts.setdefault(asset_id, {})
            bucket[severity] = bucket.get(severity, 0) + int(
                row.get("issue_count") or 0
            )
        return counts

    async def _load_space_names(self, tenant_id: str) -> dict[str, str]:
        """`knowledge_base.spaces` → `{space_id: name}`（未软删、同租户）。"""
        sql = (
            "SELECT id, name FROM knowledge_base.spaces "
            "WHERE tenant_id = :tenant_id AND is_deleted = 0"
        )
        async with get_db_session() as session:
            rows = (
                await session.execute(text(sql), {"tenant_id": tenant_id})
            ).mappings().all()
        return {
            _text(row.get("id")): _text(row.get("name"))
            for row in rows
            if _text(row.get("id"))
        }

    async def _load_user_names(self, tenant_id: str) -> dict[str, str]:
        """`platform.users` → `{user_id: display_name || username}`。"""
        sql = (
            "SELECT id::text AS user_id, username, display_name "
            "FROM platform.users "
            "WHERE tenant_id = :tenant_id AND is_deleted = 0"
        )
        async with get_db_session() as session:
            rows = (
                await session.execute(text(sql), {"tenant_id": tenant_id})
            ).mappings().all()
        names: dict[str, str] = {}
        for row in rows:
            user_id = _text(row.get("user_id"))
            if not user_id:
                continue
            names[user_id] = (
                _text(row.get("display_name")) or _text(row.get("username"))
            )
        return names

    async def _prepare(
        self, tenant_id: str, user_id, data: dict,
    ) -> tuple[dict[str, str], str | None, list[dict], dict, bool]:
        """公共装载：空间校验 + 资产分页拉取 + 工单计数（含截断标记）。"""
        tenant_id = require_tenant(tenant_id)
        _require_login(user_id)
        data = data or {}
        space_id = _text(_pick(data, "space_id", "spaceId")) or None

        space_names = await self._load_space_names(tenant_id)
        if space_id and space_id not in space_names:
            # 显式筛选不存在的空间 → 1002（与目录详情同语义），不返回空页。
            raise ResourceNotFoundError(
                message="空间不存在", details={"space_id": space_id},
            )

        catalog = CatalogService()
        assets, truncated = await self._load_assets(
            catalog, tenant_id, user_id, space_id,
        )
        issue_counts = await self._load_issue_counts(tenant_id)
        return space_names, space_id, assets, issue_counts, truncated

    # ── 业务域视图 ──────────────────────────────────────────────

    async def domain_view(self, tenant_id: str, user_id, data: dict | None) -> dict:
        """按 `(space_id, space_name, domain)` 聚合资产。

        `domain` 参数只过滤分组（不是资产级预筛），保证 totals 与返回分组同口径。
        """
        data = data or {}
        (
            space_names, _space_filter, assets, issue_counts, truncated,
        ) = await self._prepare(tenant_id, user_id, data)
        domain_filter = _text(_pick(data, "domain")) or None

        groups: dict[tuple, dict] = {}
        totals = _new_bucket()
        for asset in assets:
            space_id = _space_id(asset)
            domain = _domain(asset) or UNASSIGNED_DOMAIN
            if domain_filter and domain != domain_filter:
                continue
            key = (space_id or "", domain)
            bucket = groups.get(key)
            if bucket is None:
                bucket = groups[key] = _new_bucket()
            _accumulate(bucket, asset, issue_counts)
            _accumulate(totals, asset, issue_counts)

        result_groups = []
        for (space_id_key, domain), bucket in sorted(groups.items()):
            summary = _summary(bucket)
            result_groups.append({
                "space_id": space_id_key or None,
                "space_name": space_names.get(space_id_key),
                "domain": domain,
                **summary,
            })

        totals_summary = _summary(totals)
        totals_summary["groups"] = len(result_groups)
        return {
            "groups": result_groups,
            "totals": totals_summary,
            "empty": not result_groups,
            "truncated": truncated,
        }

    # ── 责任视图 ────────────────────────────────────────────────

    async def responsibility_view(
        self, tenant_id: str, user_id, data: dict | None,
    ) -> dict:
        """按 owner 或 domain 聚合；空 owner/domain 显式成组。"""
        data = data or {}
        group_by = (
            _text(_pick(data, "group_by", "groupBy")).lower() or "owner"
        )
        if group_by not in GROUP_BY_VALUES:
            raise InvalidParameterError(
                message="group_by 必须是 owner 或 domain",
                details={"group_by": group_by},
            )

        (
            _space_names, _space_filter, assets, issue_counts, truncated,
        ) = await self._prepare(tenant_id, user_id, data)
        # owner 展示名只在需要时查用户表；domain 视图不触发额外查询。
        user_names = (
            await self._load_user_names(tenant_id)
            if group_by == "owner" else {}
        )

        groups: dict[str, dict] = {}
        totals = _new_bucket()
        unowned = 0
        unassigned_domain = 0
        for asset in assets:
            if group_by == "owner":
                key = _owner(asset) or UNOWNED
            else:
                key = _domain(asset) or UNASSIGNED_DOMAIN
            if not _owner(asset):
                unowned += 1
            if not _domain(asset):
                unassigned_domain += 1

            bucket = groups.get(key)
            if bucket is None:
                bucket = groups[key] = _new_bucket()
            _accumulate(bucket, asset, issue_counts)
            _accumulate(totals, asset, issue_counts)

        result_groups = []
        for key, bucket in sorted(groups.items()):
            display_name = key
            if group_by == "owner":
                display_name = user_names.get(key, key)
            result_groups.append({
                "key": key,
                "display_name": display_name,
                "asset_counts": dict(bucket["asset_counts"]),
                "sensitivity": dict(bucket["sensitivity"]),
                "open_issues": dict(bucket["open_issues"]),
                "last_updated_at": _iso(bucket["last_updated_at"]),
            })

        totals_summary = _summary(totals)
        totals_summary.update({
            "groups": len(result_groups),
            "unowned": unowned,
            "unassigned_domain": unassigned_domain,
        })
        return {
            "group_by": group_by,
            "groups": result_groups,
            "totals": totals_summary,
            "empty": not result_groups,
            "truncated": truncated,
        }
