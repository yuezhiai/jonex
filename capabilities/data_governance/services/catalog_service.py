"""资产目录读服务 + 元数据写入/审计：实时 list / detail + 质量档位只读投影。

- 目录主数据来自一次 `UNION ALL`（文档 + 数据源），不做投影、不做缓存。
- 质量档位是第 1 期质量报告的投影：按当前页 document 资产**按 kb_id 分组**调
  KB 的批量 action；失败只记 warning，不影响目录主数据。
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.exc import SQLAlchemyError

from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    JonexException,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant
from jonex_core.security import classification_access as policy

from capabilities.knowledge_base.services.space_permission_service import (
    is_tenant_admin,
)

from ..clients.knowledge_base_client import get_knowledge_base_client
from ..repository import asset_metadata_repository
from ..repository.asset_metadata_repository import normalize_metadata_fields
from ..repository.catalog_read_repository import (
    build_catalog_sql,
    count,
    fetch_detail_fields,
    fetch_page,
)
from . import classification_service
from .permissions import assert_can_write, visible_kb_ids

logger = logging.getLogger(__name__)

_LIST_ASSET_TYPES = {None, "all", "document", "data_source"}
_DETAIL_ASSET_TYPES = {"document", "data_source"}

# spec §5.1：一页最多投影 20 个不同 KB 的质量档位，超出的 KB 本页不调。
_MAX_QUALITY_KBS = 20

# KB 批量 action 的入参上限（与 KB 侧同一道闸）。
_QUALITY_BATCH_LIMIT = 100

# 单个 KB 的质量投影超时；上游「活着但不响应」时不能无限拖住目录。
_QUALITY_TIMEOUT_SECONDS = 10

# KB 调用的有界并发（最多 5 个 KB 同时在途）。
_QUALITY_CONCURRENCY = 5

# 批量打标单次上限（全量预检 + 单事务写入，超出直接 400）。
_MAX_BATCH_ASSETS = 200


def _audit_user_id(user_id) -> int | None:
    """invoke 链路的 user_id 是字符串；审计表只收 int，非法值落 None。"""
    if user_id and str(user_id).isdigit():
        return int(user_id)
    return None


async def _user_max_rank(
    tenant_id: str, user_id: str | None,
) -> int | None:
    """当前用户可读最高 rank；``None`` = 不限（租户管理员）。

    无用户 / anonymous 视为 rank 0：目录读入口的原有可见性校验会先拦住它们，
    这里给 0 只是保证不因缺身份而 fail open。
    """
    if not user_id or user_id == "anonymous":
        return 0
    if await is_tenant_admin(tenant_id, user_id):
        return None
    async with get_db_session() as session:
        return await policy.get_user_max_rank(
            session, tenant_id, user_id, is_tenant_admin=False,
        )


async def _write_metadata(
    *, tenant_id: str, asset_type: str, asset_id: str, fields: dict,
):
    """单事务落元数据；归一/租户校验由仓储再兜一次。"""
    async with get_db_session() as session:
        return await asset_metadata_repository.upsert(
            session, tenant_id=tenant_id, asset_type=asset_type,
            asset_id=asset_id, fields=fields,
        )


def _as_text(value) -> str | None:
    """任意 JSON 值 → 去空白字符串；空值/空串 → None（绝不因非字符串 500）。"""
    return str(value or "").strip() or None


def _source_of(asset_value, kb_value, tenant_value) -> str:
    """生效值来自哪一层：资产覆盖 → KB 默认 → 租户默认 → 未设置。

    判据是 `is not None`：空数组 `[]` 是「显式无分类」，仍算该层覆盖。
    """
    if asset_value is not None:
        return "asset"
    if kb_value is not None:
        return "kb"
    if tenant_value is not None:
        return "tenant"
    return "none"


def _apply_classification(row: dict) -> None:
    """把仓储行的三组覆盖列解析成生效值 + 来源，并移除原始列。

    - `sensitivity` / `classification_categories` 是**生效值**（展示与筛选口径）；
    - `classification_sources` 告诉写路径审计 `before` 取哪个覆盖值
      （`_override_of` 只认 `asset`）。
    - 未分级为 `None`，无分类为 `[]`；原始列不进入响应。
    """
    asset_level = row.pop("asset_level", None)
    kb_level = row.pop("kb_level", None)
    tenant_level = row.pop("tenant_level", None)
    asset_categories = row.pop("asset_categories", None)
    kb_categories = row.pop("kb_categories", None)
    tenant_categories = row.pop("tenant_categories", None)
    row["sensitivity"] = row.pop("effective_level", None)
    row["classification_categories"] = row.pop("effective_categories", None) or []
    row["classification_sources"] = {
        "sensitivity": _source_of(asset_level, kb_level, tenant_level),
        "classification_categories": _source_of(
            asset_categories, kb_categories, tenant_categories,
        ),
    }
    # 密级上限是内部过滤字段，不进入 API 响应。
    row.pop("effective_rank", None)


def _normalize_asset_type(value) -> str | None:
    return _as_text(value)


def _classification_validation_fields(fields: dict) -> dict:
    """归一后的元数据字段 → 词表校验入参（`sensitivity` / `categories`）。"""
    validation_fields = {}
    if "sensitivity" in fields:
        validation_fields["sensitivity"] = fields["sensitivity"]
    if "classification_categories" in fields:
        validation_fields["categories"] = fields["classification_categories"]
    return validation_fields


def _override_of(asset: dict, key: str):
    """审计 `before` 取**覆盖值**而非生效值。

    读侧已产出 `classification_sources`：字段不来自资产覆盖时为 None；
    无来源信息（防御旧调用方）时退回行上的值。
    """
    sources = asset.get("classification_sources")
    if key in ("sensitivity", "classification_categories"):
        if sources is None:
            return asset.get(key)
        return asset.get(key) if sources.get(key) == "asset" else None
    return asset.get(key)


def _parse_positive_int(data: dict, key: str, default: int) -> int:
    raw = data.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        if key == "page_size":
            message = "分页参数 page_size 必须是 1..100 的整数"
        else:
            message = "分页参数 page 必须是正整数"
        raise InvalidParameterError(message=message, details={key: raw})


def _filters(
    data: dict, visible, *, asset_id: str = "", include_archived: bool = False,
) -> dict:
    """请求字段 → 仓储筛选参数（`domain_space_id` 映射为 `space_id`）。

    分级/分类只做归一（strip + lower）；值域由 `assert_filter_values`
    按租户词表校验（在 list_assets 入口先行）。

    目录默认排除归档文档；**显式筛选 `status='archived'` 时必须保留**
    （否则 `<> 'archived'` 与 `= 'archived'` 自相矛盾，永远 0 条）。
    详情/写前读传 `include_archived=True`，保证归档文档仍可打开。
    """
    asset_type = _normalize_asset_type(data.get("asset_type"))
    if asset_type == "all":
        asset_type = None
    sensitivity = _as_text(data.get("sensitivity"))
    sensitivity = sensitivity.lower() if sensitivity else None
    category = _as_text(data.get("category"))
    category = category.lower() if category else None
    status = _as_text(data.get("status"))
    return {
        "asset_type": asset_type,
        "space_id": _as_text(data.get("domain_space_id")),
        "kb_id": _as_text(data.get("kb_id")),
        "kb_ids": visible,          # None = 租户管理员，不加范围过滤
        "keyword": _as_text(data.get("keyword")),
        "domain": _as_text(data.get("domain")),
        "owner": _as_text(data.get("owner")),
        "sensitivity": sensitivity,
        "category": category,
        "status": status,
        "asset_id": _as_text(asset_id),
        "include_archived": include_archived or status == "archived",
    }


async def _quality_for_kb(
    tenant_id: str, kb_id: str, doc_ids: list[str],
) -> tuple[dict[str, str | None], dict[str, list]]:
    """单个 KB 的批量质量摘要（超时/异常由调用方隔离）。"""
    verdicts: dict[str, str | None] = {}
    reasons: dict[str, list] = {}
    # page_size ≤ 100 ⇒ 单 KB 的文档数天然 ≤ 100；仍按上限切分兜底。
    for start in range(0, len(doc_ids), _QUALITY_BATCH_LIMIT):
        batch = doc_ids[start:start + _QUALITY_BATCH_LIMIT]
        result = await asyncio.wait_for(
            get_knowledge_base_client().invoke(
                "get_document_quality_reports",
                {"knowledge_base_id": kb_id, "document_ids": batch},
                tenant_id,
            ),
            timeout=_QUALITY_TIMEOUT_SECONDS,
        )
        for item in (result or {}).get("items") or []:
            doc_id = item.get("document_id")
            if not doc_id:
                continue
            verdicts[doc_id] = item.get("verdict")
            reasons[doc_id] = item.get("reasons") or []
    return verdicts, reasons


async def _attach_quality(
    tenant_id: str, rows: list[dict], *, include_reasons: bool = False,
) -> None:
    """按 kb_id 分组批量投影质量档位；失败不影响目录主数据。

    - 有界并发（`_QUALITY_CONCURRENCY`）+ 单 KB 超时（`_QUALITY_TIMEOUT_SECONDS`）：
      上游「活着但不响应」时不会串行拖出 20×300s。
    - 一个 KB 失败只影响它自己的 `quality_verdict`（None），不拖累其他 KB。
    - `include_reasons=False`（列表）时**不写** `quality_reasons` 键；
      只有详情传 `True`（spec §5.1 列表项只有 `quality_verdict`）。
    """
    docs_by_kb: dict[str, list[str]] = {}
    for row in rows:
        if row.get("asset_type") == "document" and row.get("kb_id"):
            docs_by_kb.setdefault(row["kb_id"], []).append(row["asset_id"])

    kb_items = list(docs_by_kb.items())
    selected = kb_items[:_MAX_QUALITY_KBS]
    skipped = [kb_id for kb_id, _ in kb_items[_MAX_QUALITY_KBS:]]
    if skipped:
        logger.warning(
            "quality projection skipped: too many KBs in one page "
            "(%d > %d), skipped=%s",
            len(kb_items), _MAX_QUALITY_KBS, skipped,
        )

    semaphore = asyncio.Semaphore(_QUALITY_CONCURRENCY)

    async def _guarded(kb_id: str, doc_ids: list[str]):
        async with semaphore:
            try:
                return await _quality_for_kb(tenant_id, kb_id, doc_ids)
            except Exception:  # noqa: BLE001 — 质量是附加信息，失败不拖垮目录
                logger.warning(
                    "quality projection failed for kb=%s", kb_id, exc_info=True
                )
                return {}, {}

    results = await asyncio.gather(
        *(_guarded(kb_id, doc_ids) for kb_id, doc_ids in selected),
        return_exceptions=True,
    )

    verdicts: dict[str, str | None] = {}
    reasons: dict[str, list] = {}
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("quality projection task failed", exc_info=result)
            continue
        kb_verdicts, kb_reasons = result
        verdicts.update(kb_verdicts)
        reasons.update(kb_reasons)

    for row in rows:
        if row.get("asset_type") == "document":
            row["quality_verdict"] = verdicts.get(row["asset_id"])
            if include_reasons:
                row["quality_reasons"] = reasons.get(row["asset_id"], [])
        else:
            row["quality_verdict"] = None
            if include_reasons:
                row["quality_reasons"] = []


async def _resolve_retention_policy(
    tenant_id: str, *, kb_id: str, document_id: str = "",
) -> dict:
    """详情投影生效策略；延迟导入避免与 retention service 的模块环。

    **自开会话**：目录查询的 session 已关闭，不复用请求事务（策略是附加信息，
    不应延长主查询的事务边界）。
    """
    from . import retention_policy_service

    return await retention_policy_service.resolve_policy(
        tenant_id, kb_id=kb_id, document_id=document_id,
    )


class CatalogService:
    async def list_assets(self, tenant_id: str, user_id: str | None, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        data = data or {}
        asset_type = _normalize_asset_type(data.get("asset_type"))
        if asset_type not in _LIST_ASSET_TYPES:
            raise InvalidParameterError(
                message="invalid_asset_type",
                details={"asset_type": asset_type},
            )
        # 参数值域校验在权限/DB 之前完成，非法输入不触发任何查询。
        # 与 `_filters` 同口径 lower：`unclassified` 哨兵必须大小写归一后才可识别。
        level = _as_text(data.get("sensitivity"))
        level = level.lower() if level else None
        category = _as_text(data.get("category"))
        category = category.lower() if category else None
        # 空筛选不写库：`assert_filter_values` 会懒播种词表（DB 写）。
        if level or category:
            await classification_service.assert_filter_values(
                tenant_id, level=level, category=category,
            )

        page = max(1, _parse_positive_int(data, "page", 1))
        page_size = min(100, max(1, _parse_positive_int(data, "page_size", 20)))

        visible = await visible_kb_ids(tenant_id, user_id)
        if visible is not None and not visible:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}

        # 无条件取用户密级上限（管理员返回 None）：不依赖 `visible is None`
        # 推断管理员，避免未来可见性口径变化时静默失去密级过滤。
        max_rank = await _user_max_rank(tenant_id, user_id)
        filters = _filters(data, visible)
        filters["max_rank"] = max_rank
        async with get_db_session() as session:
            sql, params = build_catalog_sql(
                tenant_id=tenant_id,
                limit=page_size,
                offset=(page - 1) * page_size,
                **filters,
            )
            rows = await fetch_page(session, sql, params)
            # 计数与列表共用同一份筛选；count 侧不接收分页参数。
            total = await count(session, tenant_id=tenant_id, **filters)
            for row in rows:
                _apply_classification(row)

        await _attach_quality(tenant_id, rows)
        return {
            "items": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def get_asset(
        self, tenant_id: str, user_id: str | None,
        asset_type: str, asset_id: str, *, include_quality: bool = True,
        enforce_rank: bool = True,
    ) -> dict:
        """资产完整详情。

        `include_quality=False` 时整段跳过质量投影：不调 KB、响应不产生
        `quality` 键。供写路径的「写前读」使用 —— 元数据写入不依赖质量档位。

        `enforce_rank=False` 时不追加用户密级过滤：写路径的「写前读」必须
        让 space_manager / kb_manager 能维护**高于自身密级**的资产（写权限
        仍由 `assert_can_write` 把关）。列表/详情/血缘保持默认 True。
        """
        tenant_id = require_tenant(tenant_id)
        # 空/非法 asset_id 会让筛选被丢弃、LIMIT 1 命中任意同类型资产（类型校验还会
        # 通过），必须在任何查询之前挡住。`_as_text` 同时吃掉非字符串（int/list），
        # 避免 `(asset_id or "").strip()` 的 AttributeError 500；非字符串 id 不可能是
        # 合法资产 id，一律按「不存在」处理（与 404 同一响应，防探测）。
        asset_id = _as_text(asset_id) if isinstance(asset_id, str) else None
        if asset_id is None:
            raise ResourceNotFoundError(message="资产不存在")

        asset_type = _normalize_asset_type(asset_type)
        if asset_type not in _DETAIL_ASSET_TYPES:
            raise InvalidParameterError(
                message="invalid_asset_type",
                details={"asset_type": asset_type},
            )

        visible = await visible_kb_ids(tenant_id, user_id)
        if visible is not None and not visible:
            raise ResourceNotFoundError(message="资产不存在")

        # 详情不能套用列表的「默认排除归档」：归档文档仍须可打开（含写前读）。
        filters = _filters(
            {}, visible, asset_id=asset_id, include_archived=True,
        )
        filters["asset_type"] = asset_type
        if enforce_rank:
            # 同列表：无条件取用户密级上限，不依赖可见性返回值推断管理员。
            filters["max_rank"] = await _user_max_rank(tenant_id, user_id)
        async with get_db_session() as session:
            sql, params = build_catalog_sql(
                tenant_id=tenant_id, limit=1, offset=0, **filters,
            )
            rows = await fetch_page(session, sql, params)
            if not rows or rows[0].get("asset_type") != asset_type:
                raise ResourceNotFoundError(message="资产不存在")
            # 同一个 session 内取详情特有字段（列表页不取）。
            detail = await fetch_detail_fields(
                session, tenant_id=tenant_id,
                asset_type=asset_type, asset_id=asset_id,
            )

        row = rows[0]
        # 详情返回**生效**分级/分类（资产 → KB → 租户），并带来源供审计用。
        _apply_classification(row)
        # 生效保留策略是详情附加信息：**session 关闭后**自开会话解析；
        # 只吞可预期的存储/租户错误，编程错误（AttributeError 等）照常暴露。
        try:
            retention_policy = await _resolve_retention_policy(
                tenant_id,
                kb_id=row.get("kb_id") or "",
                document_id=(asset_id if asset_type == "document" else ""),
            )
        except (JonexException, SQLAlchemyError, OSError):
            logger.warning(
                "retention policy projection failed for %s/%s",
                asset_type, asset_id, exc_info=True,
            )
            retention_policy = None
        if include_quality:
            await _attach_quality(tenant_id, rows, include_reasons=True)
            verdict = row.pop("quality_verdict", None)
            quality_reasons = row.pop("quality_reasons", [])
        out = {**row, **detail}
        # 列表行不含 archived_at；详情以 detail SQL 为准，缺失时兜底为 None。
        out.setdefault("archived_at", row.get("archived_at"))
        # 创建时间同样只在详情 SQL 中取；兜底保证响应键始终存在。
        out.setdefault("created_at", None)
        out["retention_policy"] = retention_policy
        if include_quality:
            out["quality"] = {
                "verdict": verdict, "reasons": quality_reasons or [],
            }
        return out

    async def _get_asset_row(
        self, tenant_id: str, user_id: str | None,
        asset_type: str, asset_id: str,
    ) -> dict:
        """写前读：复用读可见性（不存在/不可见一律 404）。

        质量投影是列表/详情的附加信息，写路径用不到；`include_quality=False`
        保证这里不产生任何 KB 调用。

        `enforce_rank=False`：不按用户密级过滤，space_manager / kb_manager
        才能维护高密资产；写权限仍由调用方的 `assert_can_write` 判定。
        """
        return await self.get_asset(
            tenant_id, user_id, asset_type, asset_id,
            include_quality=False, enforce_rank=False,
        )

    async def update_metadata(
        self, tenant_id: str, user_id: str | None,
        asset_type: str, asset_id: str, data: dict,
        *, username: str | None = None, ip: str | None = None,
    ) -> dict:
        """更新资产元数据并落一条治理审计，返回更新后的完整详情。

        顺序固定：租户 → 形态守卫（400）→ 读可见性（404）→ 写权限（403）
        → 归一（400）→ 写入 → 审计。未授权用户在归一之前被挡住，拿不到值域
        校验信息；失败路径不写、不审计。

        `before` 是写前读的快照（非 `SELECT FOR UPDATE`）：并发写时审计记录的
        可能是被覆盖前的旧值，本期按低成本口径接受。
        """
        tenant_id = require_tenant(tenant_id)
        # 入口先挡非对象：JSON body 传 null/字符串/数组时不能落成 AttributeError 500。
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise InvalidParameterError(message="元数据必须是对象")

        asset = await self._get_asset_row(
            tenant_id, user_id, asset_type, asset_id,
        )
        await assert_can_write(
            tenant_id, user_id,
            kb_id=asset.get("kb_id") or "",
            space_id=asset.get("space_id") or "",
        )

        fields = normalize_metadata_fields(data)
        # 显式在写之前挡住空更新：保证「无写、无审计」（不依赖 upsert 内部兜底）。
        if not fields:
            raise InvalidParameterError(message="没有可更新的字段")

        # 显式给出的分级/分类先过租户词表（before 取覆盖值，见 `_override_of`）。
        # owner/domain-only 更新不触达词表（省一次 DB 会话）。
        validation_fields = _classification_validation_fields(fields)
        if validation_fields:
            await classification_service.validate_assignment(
                tenant_id, validation_fields,
            )

        before = {key: _override_of(asset, key) for key in fields}

        # 写库/审计一律用读回行的规范值（读侧已 trim/校验），与权限参数同源。
        canonical_type = asset["asset_type"]
        canonical_id = asset["asset_id"]
        row = await _write_metadata(
            tenant_id=tenant_id, asset_type=canonical_type,
            asset_id=canonical_id, fields=fields,
        )
        updated = {
            key: getattr(row, key)
            for key in (
                "owner", "domain", "sensitivity", "classification_categories",
            )
        }

        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "username": username,
            "ip": ip,
            "log_type": "OPERATION",
            "action": "asset_metadata_update",
            "outcome": "SUCCESS",
            "service_name": "data_governance",
            "resource": (
                ResourceType.DATA_SOURCE.value
                if canonical_type == "data_source"
                else ResourceType.DOCUMENT.value
            ),
            "resource_id": str(canonical_id),
            # after 用落库后的实际值（行上的三键），不是请求归一值。
            "request_params": {"before": before, "after": updated},
        })
        # 响应与 GET 详情逐键同形：写入/审计成功后再做一次（且仅一次）质量投影。
        # 质量投影发生在写权限通过、写入完成之后，失败路径不会触达。
        # `enforce_rank=False`：写后回读同样不能按用户密级过滤 —— 否则低密级
        # 但有写权限的 manager 会在这里被 404 挡住，写入已成功却拿不到响应。
        return await self.get_asset(
            tenant_id, user_id, canonical_type, canonical_id,
            enforce_rank=False,
        )

    async def batch_update_classification(
        self, tenant_id: str, user_id: str | None, data: dict,
        *, username: str | None = None, ip: str | None = None,
    ) -> dict:
        """批量覆盖资产的分级/分类：全量预检 → 单事务写入 → 一条聚合审计。

        预检（读可见性 404 + 写权限 403）先于任何写入：任一资产不可写，
        整批一行都不落（全有或全无）。字段值域与单资产更新同源。

        已知取舍：预检对内联资产逐个 `_get_asset_row`（N+1），本期按低频
        配置操作接受；后续可改为按 `(asset_type, asset_id)` 批量读。
        """
        tenant_id = require_tenant(tenant_id)
        if not isinstance(data, dict):
            raise InvalidParameterError(message="数据必须是对象")

        assets = data.get("assets")
        if not isinstance(assets, list) or not assets:
            raise InvalidParameterError(message="assets 必须是非空数组")
        if len(assets) > _MAX_BATCH_ASSETS:
            raise InvalidParameterError(
                message=f"单次最多 {_MAX_BATCH_ASSETS} 个资产",
                details={"count": len(assets)},
            )

        targets: list[tuple[str, str]] = []
        for item in assets:
            if not isinstance(item, dict):
                raise InvalidParameterError(message="assets 元素必须是对象")
            unknown = set(item) - {"asset_type", "asset_id"}
            if unknown:
                # 元素级 sensitivity/categories 会被静默丢弃，明确拒绝更安全。
                raise InvalidParameterError(
                    message="assets 元素只允许 asset_type/asset_id",
                    details={"unknown_keys": sorted(str(key) for key in unknown)},
                )
            asset_type = _normalize_asset_type(item.get("asset_type"))
            if asset_type not in _DETAIL_ASSET_TYPES:
                raise InvalidParameterError(
                    message="invalid_asset_type",
                    details={"asset_type": item.get("asset_type")},
                )
            asset_id = item.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id.strip():
                raise InvalidParameterError(message="asset_id 不能为空")
            targets.append((asset_type, asset_id.strip()))

        # 保序去重：`d-1` 与 `" d-1 "` 只写一次，count/审计同一口径。
        targets = list(dict.fromkeys(targets))

        fields = normalize_metadata_fields({
            key: data[key]
            for key in ("sensitivity", "categories")
            if key in data
        })
        if not fields:
            raise InvalidParameterError(message="没有可更新的字段")

        validation_fields = _classification_validation_fields(fields)
        if validation_fields:
            await classification_service.validate_assignment(
                tenant_id, validation_fields,
            )

        # 全量预检：任一 404/403 直接抛，不写任何行。
        keys: list[tuple[str, str]] = []
        for asset_type, asset_id in targets:
            asset = await self._get_asset_row(
                tenant_id, user_id, asset_type, asset_id,
            )
            await assert_can_write(
                tenant_id, user_id,
                kb_id=asset.get("kb_id") or "",
                space_id=asset.get("space_id") or "",
            )
            keys.append((asset["asset_type"], asset["asset_id"]))

        async with get_db_session() as session:
            for asset_type, asset_id in keys:
                await asset_metadata_repository.upsert(
                    session, tenant_id=tenant_id, asset_type=asset_type,
                    asset_id=asset_id, fields=fields,
                )
            await session.commit()

        schedule_emit({
            "tenant_id": tenant_id, "user_id": _audit_user_id(user_id),
            "username": username, "ip": ip, "log_type": "OPERATION",
            "action": "classification_batch_update", "outcome": "SUCCESS",
            "service_name": "data_governance",
            "resource": ResourceType.SYSTEM_CONFIG.value,
            "resource_id": f"batch:{len(keys)}",
            "request_params": {
                "count": len(keys),
                "asset_types": sorted({t for t, _ in keys}),
                "fields": fields,
                "asset_keys": [f"{t}:{i}" for t, i in keys[:50]],
            },
        })
        return {"updated": len(keys)}
