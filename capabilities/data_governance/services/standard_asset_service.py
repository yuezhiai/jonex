"""标准资产服务：权限、校验、状态机、审计。

三层口径：

- **写**（建/改/发布/退役/删草稿）仅租户管理员；
- **读**任意已登录用户，但草稿只对管理员可见（非管理员一律按不存在处理）；
- 状态机由仓储的 `WHERE status='draft' AND revision=:revision` 兜底，服务层把
  rowcount=0 统一翻成 1003（资源状态冲突）。

审计资源类型为 `ResourceType.STANDARD_ASSET`（落库值 `standard_asset`）。
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.exc import IntegrityError

from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from capabilities.knowledge_base.services.metrics.validation import validate_metric
from capabilities.knowledge_base.services.space_permission_service import (
    is_tenant_admin,
)

from ..repository import standard_asset_repository as repo
from .standards.validation import KINDS, validate_asset_fields, validate_payload

logger = logging.getLogger(__name__)

_RESOURCE_TYPE = ResourceType.STANDARD_ASSET.value
_STATUSES = frozenset({"draft", "published", "retired"})
#: 审计摘要字段：只记标识与状态，不落 payload 内容（规格 §4）。
_CONTENT_FIELDS = ("kind", "code", "name", "status", "version", "revision")
_STATE_FIELDS = ("kind", "code", "status", "version", "revision")


def _instance_value(row, field):
    """兼容 RowMapping / dict / ORM 对象取列。"""
    if row is None:
        return None
    if hasattr(row, "get"):
        return row.get(field)
    return getattr(row, field, None)


def _payload_meta(payload) -> dict | None:
    """payload 的审计元信息：只记键名与序列化字节数，不记值。"""
    if not isinstance(payload, dict):
        return None
    try:
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        # 理论上不会发生（payload 已校验为 JSON 兼容），兜底只回键名。
        return {"payload_keys": sorted(payload)}
    return {"payload_keys": sorted(payload), "payload_bytes": len(raw)}


def _snapshot(row, fields, *, include_payload: bool = False) -> dict | None:
    if row is None:
        return None
    snapshot = {field: _instance_value(row, field) for field in fields}
    if include_payload:
        meta = _payload_meta(_instance_value(row, "payload"))
        if meta:
            snapshot.update(meta)
    return snapshot


def _constraint_name(exc: IntegrityError) -> str | None:
    """取唯一约束名。

    psycopg 挂在 ``diag.constraint_name``；asyncpg 的异常对象没有稳定的
    ``constraint_name`` 属性（2026-10-19 真机：草稿冲突未被识别而冒成 500），
    因此再按异常消息回退匹配本模块关心的约束名。
    """
    orig = getattr(exc, "orig", None)
    name = getattr(orig, "constraint_name", None)
    if name:
        return name
    name = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if name:
        return name
    # 只认 `unique constraint "<name>"` 的正式位置；不能按子串搜全文 ——
    # 违规 DETAIL 里会回显 code 值，而 code 正则可以恰好等于约束名。
    match = re.search(r'unique constraint "([^"]+)"', str(exc))
    return match.group(1) if match else None


def _audit_user_id(user_id):
    """invoke 链路的 user_id 是字符串；审计表只收 int，非法值落 None。"""
    return int(user_id) if user_id and str(user_id).isdigit() else None


def _is_authenticated(user_id) -> bool:
    """与保留策略/分级配置同口径：空值与 `anonymous` 视为未登录。"""
    return bool(user_id) and user_id != "anonymous"


def _page_param(value, *, default: int, minimum: int, maximum: int | None,
                field: str) -> int:
    """分页参数：非法值回 1001，越界按边界截断。"""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidParameterError(
            message=f"{field} 必须是整数", details={"field": field},
        )
    number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _validate_identity(kind: str, code: str) -> tuple[str, str]:
    """克隆路径只校验 kind/code（不校验调用方传入的 name/payload）。

    `validate_asset_fields` 的签名要求 name；这里把已通过 code 校验的值临时
    当作 name 传入，复用同一套 code 正则与长度约束，不解第二份规则。
    """
    if kind not in KINDS:
        raise InvalidParameterError(message="kind 非法", details={"kind": kind})
    code, _, kind = validate_asset_fields(kind, code, code)
    return kind, code


class StandardAssetService:
    async def _require_admin(self, tenant_id: str, user_id) -> None:
        # 先判登录态：`anonymous` 不能拿去查权限表。
        if not _is_authenticated(user_id) or not await is_tenant_admin(
            tenant_id, user_id
        ):
            raise PermissionDeniedError(message="仅租户管理员可维护标准资产")

    @staticmethod
    def _require_login(user_id) -> None:
        if not _is_authenticated(user_id):
            raise PermissionDeniedError(message="需要登录后查看标准资产")

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def list_assets(self, tenant_id, user_id, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        self._require_login(user_id)
        data = data or {}

        kind = data.get("kind") or None
        if kind is not None and kind not in KINDS:
            raise InvalidParameterError(message="kind 非法", details={"kind": kind})
        status = data.get("status") or None
        if status is not None and status not in _STATUSES:
            raise InvalidParameterError(
                message="status 非法", details={"status": status},
            )

        is_admin = await is_tenant_admin(tenant_id, user_id)
        # 非管理员无论传什么都不能看草稿。
        include_drafts = bool(data.get("include_drafts")) and is_admin
        page = _page_param(
            data.get("page"), default=1, minimum=1, maximum=None, field="page",
        )
        page_size = _page_param(
            data.get("page_size"), default=20, minimum=1, maximum=100,
            field="page_size",
        )

        async with get_db_session() as session:
            rows = await repo.list_page(
                session, tenant_id=tenant_id, kind=kind, status=status,
                keyword=data.get("keyword"), include_drafts=include_drafts,
                limit=page_size, offset=(page - 1) * page_size,
            )
            # list 与 count 必须是同一 included_drafts 口径，否则 total 与 items 对不上。
            total = await repo.count(
                session, tenant_id=tenant_id, kind=kind, status=status,
                keyword=data.get("keyword"), include_drafts=include_drafts,
            )

        items = [
            repo.to_summary(row, include_drafts=include_drafts) for row in rows
        ]
        # 仓储已按 include_drafts 过滤；这里再兜一道，防止仓储口径回归时泄露草稿。
        if not is_admin:
            items = [item for item in items if item["status"] != "draft"]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def get_asset(self, tenant_id, user_id, asset_id) -> dict:
        tenant_id = require_tenant(tenant_id)
        self._require_login(user_id)
        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, asset_id=asset_id,
            )
        if row is None:
            raise ResourceNotFoundError(message="标准资产不存在")
        if row["status"] == "draft" and not await is_tenant_admin(
            tenant_id, user_id
        ):
            # 不暴露草稿存在性：与非管理员读不存在的资产同样回 1002。
            raise ResourceNotFoundError(message="标准资产不存在")
        return repo.to_detail(row)

    async def list_versions(self, tenant_id, user_id, kind, code) -> dict:
        tenant_id = require_tenant(tenant_id)
        self._require_login(user_id)
        if kind not in KINDS:
            raise InvalidParameterError(message="kind 非法", details={"kind": kind})
        async with get_db_session() as session:
            rows = await repo.list_versions(
                session, tenant_id=tenant_id, kind=kind, code=code,
            )
        if not rows:
            raise ResourceNotFoundError(message="标准资产不存在")
        is_admin = await is_tenant_admin(tenant_id, user_id)
        items = [
            repo.to_detail(row) for row in rows
            if is_admin or row["status"] != "draft"
        ]
        if not items:
            # 只有草稿时对非管理员同样按不存在处理，不暴露版本历史的存在性。
            raise ResourceNotFoundError(message="标准资产不存在")
        return {"items": items}

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def _validate_metric_card_scope(self, session, tenant_id, code,
                                          payload) -> None:
        """指标口径卡的输入存在 / 成环 / 跨源校验（W3.3 M3）。

        口径卡的作用域是**同租户其它 metric_card 的 code**；没有 executable 的
        普通卡直接跳过（旧卡行为不变）。定义错误一律 1001 —— 发布后不可变，
        坏卡必须在草稿保存时就挡住。
        """
        if not isinstance(payload, dict) or not isinstance(
                payload.get("executable"), dict):
            return
        scope = await repo.load_metric_card_scope(session, tenant_id=tenant_id)
        key = str(code)
        scope[key] = {
            "name": payload.get("definition") or key,
            "executable": payload["executable"],
            "additivity": payload.get("additivity"),
        }
        issues = validate_metric(scope[key], scope=scope, key=key)
        if issues:
            raise InvalidParameterError(
                message="指标定义非法", details={"issues": issues[:20]},
            )

    async def create_draft(self, tenant_id, user_id, kind, code, name, payload,
                           base_version=None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        if base_version is not None:
            # 克隆：name/payload 来自基准行，调用方传入的内容不参与校验。
            kind, code = _validate_identity(kind, code)
        else:
            code, name, kind = validate_asset_fields(kind, code, name)
            payload = validate_payload(kind, payload)

        async with get_db_session() as session:
            if base_version is not None:
                base = await repo.get_version(
                    session, tenant_id=tenant_id, kind=kind, code=code,
                    version=base_version,
                )
                if base is None:
                    raise ResourceNotFoundError(message="基准版本不存在")
                name = base["name"]
                # 基准内容落库时校验过，但规则可能收紧；克隆时再过一遍当前规则。
                payload = validate_payload(kind, base["payload"])
            if kind == "metric_card":
                await self._validate_metric_card_scope(
                    session, tenant_id, code, payload)
            version = await repo.next_version(
                session, tenant_id=tenant_id, kind=kind, code=code,
            )
            try:
                row = await repo.create_draft(
                    session, tenant_id=tenant_id, kind=kind, code=code,
                    version=version, name=name, payload=payload,
                    created_by=user_id,
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                constraint = _constraint_name(exc)
                if constraint == "uq_std_asset_version":
                    raise ResourceConflictError(
                        message="该版本已存在，请重试",
                    ) from exc
                if constraint == "uq_std_asset_draft":
                    raise ResourceConflictError(
                        message="该编码已有草稿",
                    ) from exc
                raise
            except Exception:
                await session.rollback()
                raise

        detail = repo.to_detail(row)
        self._audit(
            tenant_id, user_id, "standard_asset_create", detail,
            before=None,
            after=_snapshot(detail, _CONTENT_FIELDS, include_payload=True),
        )
        return detail

    async def update_draft(self, tenant_id, user_id, asset_id, revision,
                           name=None, payload=None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, asset_id=asset_id,
            )
            if row is None:
                raise ResourceNotFoundError(message="标准资产不存在")
            if row["status"] != "draft":
                raise ResourceConflictError(
                    message="发布后的标准资产不可修改，请新建草稿",
                )
            before = _snapshot(row, _CONTENT_FIELDS, include_payload=True)

            fields = {}
            if name is not None:
                _, fields["name"], _ = validate_asset_fields(
                    row["kind"], row["code"], name,
                )
            if payload is not None:
                fields["payload"] = validate_payload(row["kind"], payload)
            if not fields:
                raise InvalidParameterError(message="没有可更新的字段")
            if row["kind"] == "metric_card" and "payload" in fields:
                await self._validate_metric_card_scope(
                    session, tenant_id, row["code"], fields["payload"])
            fields["updated_by"] = user_id

            changed = await repo.update_draft(
                session, tenant_id=tenant_id, asset_id=asset_id,
                revision=revision, fields=fields,
            )
            if changed == 0:
                await session.rollback()
                raise ResourceConflictError(
                    message="版本已变化，请刷新后重试",
                )
            await session.commit()
            updated = await repo.get_by_id(
                session, tenant_id=tenant_id, asset_id=asset_id,
            )

        # 回读可能因并发删除而为 None：审计用 before 的 id 兜底，不 500。
        audit_row = updated if updated is not None else {"id": asset_id}
        self._audit(
            tenant_id, user_id, "standard_asset_update", audit_row,
            before=before,
            after=_snapshot(updated, _CONTENT_FIELDS, include_payload=True),
        )
        return repo.to_detail(updated)

    async def publish(self, tenant_id, user_id, asset_id, revision) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, asset_id=asset_id,
            )
            if row is None:
                raise ResourceNotFoundError(message="标准资产不存在")
            if row["status"] != "draft":
                raise ResourceConflictError(message="只有草稿可以发布")
            if row.get("kind") == "metric_card":
                # 草稿存下后，被引用的卡可能被删除/退役；发布前再验一次
                # 作用域（发布后不可变，坏卡不能上线）。
                await self._validate_metric_card_scope(
                    session, tenant_id, row["code"], row.get("payload"))
            before = _snapshot(row, _STATE_FIELDS)
            try:
                # 仓储内部先 FOR UPDATE 再校验 revision，失配直接回 0。
                changed = await repo.publish(
                    session, tenant_id=tenant_id, asset_id=asset_id,
                    revision=revision, user_id=user_id,
                )
                if changed == 0:
                    await session.rollback()
                    raise ResourceConflictError(
                        message="版本已变化，请刷新后重试",
                    )
                if changed < 0:
                    # 仓储在锁内发现已有更高版本的 published：禁止旧草稿覆盖。
                    await session.rollback()
                    raise ResourceConflictError(
                        message="已有更新的发布版本，旧草稿不可发布",
                    )
                await session.commit()
            except ResourceConflictError:
                raise
            except IntegrityError as exc:
                await session.rollback()
                if _constraint_name(exc) == "uq_std_asset_published":
                    raise ResourceConflictError(
                        message="该编码已有发布版本，请刷新后重试",
                    ) from exc
                raise
            except Exception:
                await session.rollback()
                raise
            updated = await repo.get_by_id(
                session, tenant_id=tenant_id, asset_id=asset_id,
            )

        self._audit(
            tenant_id, user_id, "standard_asset_publish", updated,
            before=before, after=_snapshot(updated, _STATE_FIELDS),
        )
        return repo.to_detail(updated)

    async def retire(self, tenant_id, user_id, asset_id) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, asset_id=asset_id,
            )
            if row is None:
                raise ResourceNotFoundError(message="标准资产不存在")
            before = _snapshot(row, _STATE_FIELDS)
            changed = await repo.retire(
                session, tenant_id=tenant_id, asset_id=asset_id,
                user_id=user_id,
            )
            if changed == 0:
                await session.rollback()
                raise ResourceConflictError(
                    message="只有已发布的标准可以退役",
                )
            await session.commit()
            updated = await repo.get_by_id(
                session, tenant_id=tenant_id, asset_id=asset_id,
            )

        self._audit(
            tenant_id, user_id, "standard_asset_retire", updated,
            before=before, after=_snapshot(updated, _STATE_FIELDS),
        )
        return repo.to_detail(updated)

    async def delete_draft(self, tenant_id, user_id, asset_id) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, asset_id=asset_id,
            )
            if row is None:
                raise ResourceNotFoundError(message="标准资产不存在")
            before = _snapshot(row, _STATE_FIELDS)
            changed = await repo.delete_draft(
                session, tenant_id=tenant_id, asset_id=asset_id,
                user_id=user_id,
            )
            if changed == 0:
                await session.rollback()
                raise ResourceConflictError(message="只有草稿可以删除")
            await session.commit()
            # 软删除后普通读取拿不到行，必须显式带 include_deleted 才能记录 after。
            deleted_row = await repo.get_by_id(
                session, tenant_id=tenant_id, asset_id=asset_id,
                include_deleted=True,
            )

        self._audit(
            tenant_id, user_id, "standard_asset_delete", row,
            before=before,
            after=_snapshot(deleted_row, _STATE_FIELDS + ("is_deleted",)),
        )
        return {"asset_id": asset_id, "deleted": True}

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def _audit(self, tenant_id, user_id, action, row, *, before, after) -> None:
        # 入参既可能是仓储原始行（`id`），也可能是 `to_detail` 的结果（`asset_id`）。
        asset_id = row.get("asset_id") if hasattr(row, "get") else None
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "log_type": "OPERATION",
            "action": action,
            "outcome": "SUCCESS",
            "service_name": "data_governance",
            "resource": _RESOURCE_TYPE,
            "resource_id": str(asset_id if asset_id is not None else row["id"]),
            "request_params": {"before": before, "after": after},
        })
