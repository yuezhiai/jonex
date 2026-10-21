"""asset_metadata 读写。"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ..models import AssetMetadata
from . import classification_repository


def normalize_metadata_fields(data: dict) -> dict:
    """只保留调用方给的键；空串/纯空白归一为 None。

    `sensitivity` 只做 trim + 小写，值域由分级词表在 service 层校验；
    `categories` 归一后映射为 `classification_categories`。
    """
    out: dict = {}
    for key in ("owner", "domain"):
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            out[key] = None
            continue
        text = str(value).strip()
        out[key] = text or None
    if "sensitivity" in data:
        value = data.get("sensitivity")
        out["sensitivity"] = (
            None if value is None else (str(value).strip().lower() or None)
        )
    if "categories" in data:
        out["classification_categories"] = (
            classification_repository.normalize_categories(data.get("categories"))
        )
    elif "classification_categories" in data:
        # `_write_metadata` 会把已归一的 fields 再喂进来一次；必须认这个键，
        # 否则分类会被二次归一静默丢掉。
        out["classification_categories"] = (
            classification_repository.normalize_categories(
                data.get("classification_categories")
            )
        )
    if out.get("owner") is not None and len(out["owner"]) > 128:
        raise InvalidParameterError(message="owner 超长（>128）")
    if out.get("domain") is not None and len(out["domain"]) > 64:
        raise InvalidParameterError(message="domain 超长（>64）")
    return out


async def upsert(
    session: AsyncSession, *, tenant_id: str, asset_type: str, asset_id: str,
    fields: dict,
) -> AssetMetadata:
    tid = require_tenant(tenant_id)
    fields = normalize_metadata_fields(fields)
    if not fields:
        raise InvalidParameterError(message="没有可更新的字段")
    row = (
        await session.execute(
            select(AssetMetadata).where(
                AssetMetadata.tenant_id == tid,
                AssetMetadata.asset_type == asset_type,
                AssetMetadata.asset_id == asset_id,
            )
        )
    ).scalars().first()
    if row is None:
        row = AssetMetadata(
            id=uuid.uuid4().hex, tenant_id=tid,
            asset_type=asset_type, asset_id=asset_id,
        )
        session.add(row)
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    return row


async def list_for_assets(
    session: AsyncSession, *, tenant_id: str, keys: list[tuple[str, str]],
) -> dict[tuple[str, str], AssetMetadata]:
    """批量读元数据；`keys` 是 `[(asset_type, asset_id)]` 列表。"""
    tid = require_tenant(tenant_id)
    if not keys:
        return {}
    types = {t for t, _ in keys}
    ids = {a for _, a in keys}
    rows = (
        await session.execute(
            select(AssetMetadata).where(
                AssetMetadata.tenant_id == tid,
                AssetMetadata.asset_type.in_(types),
                AssetMetadata.asset_id.in_(ids),
            )
        )
    ).scalars().all()
    wanted = set(keys)
    return {(r.asset_type, r.asset_id): r for r in rows
            if (r.asset_type, r.asset_id) in wanted}
