"""数据合同服务：权限、状态机、审计、导入导出（W2.3 任务 5）。

三层口径：

- **写**（建/改/发布/退役/删草稿/导入）仅租户管理员；
- **读**任意已登录用户，但草稿只对管理员可见（非管理员一律按不存在处理，
  不泄露草稿存在性）；
- 状态机由仓储的 ``WHERE status='draft' AND revision=:revision`` 与
  ``publish`` 的锁内校验兜底，服务层把失败统一翻成 1003（资源状态冲突）。

审计资源类型为 ``ResourceType.DATA_CONTRACT``（任务 6 已登记枚举），
摘要只记标识与状态，不落 payload 内容。
"""
from __future__ import annotations

import copy
import json
import logging
import re

import yaml
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

from capabilities.knowledge_base.services.space_permission_service import (
    is_tenant_admin,
)

from ..repository import contract_repository as repo
from .contracts import scaffold
from .contracts.validation import normalize_contract, validate_contract

logger = logging.getLogger(__name__)

_RESOURCE_TYPE = ResourceType.DATA_CONTRACT.value
_STATUSES = frozenset({"draft", "published", "retired"})
_MAX_NAME_LEN = 255
_MAX_IMPORT_BYTES = 4 * 1024 * 1024
#: 与 payload 校验同口径的 4MB 上限（预览时提前预警，不改变返回结构）。
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
_FORMATS = frozenset({"json", "yaml"})
#: 审计摘要字段：只记标识、版本与状态 + payload 的键名/字节数（规格 §4）。
_AUDIT_FIELDS = ("data_source_id", "name", "version", "status", "revision")
#: 导出时行状态 → ODCS status（ODCS 用 active 表示已生效）。
_EXPORT_STATUS = {"draft": "draft", "published": "active", "retired": "retired"}


def _instance_value(row, field):
    """兼容 RowMapping / dict / ORM 对象取列。"""
    if row is None:
        return None
    if hasattr(row, "get"):
        return row.get(field)
    return getattr(row, field, None)


def _row_payload(row) -> dict:
    """取行 payload；asyncpg 未注册 jsonb codec 时会是字符串，这里兜底解析。"""
    payload = _instance_value(row, "payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    return payload if isinstance(payload, dict) else {}


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
        # 键可能是 YAML 产生的非字符串类型，排序前统一转 str。
        return {"payload_keys": sorted(map(str, payload))}
    return {"payload_keys": sorted(payload), "payload_bytes": len(raw)}


def _snapshot(row, fields, *, include_payload: bool = True) -> dict | None:
    if row is None:
        return None
    snapshot = {field: _instance_value(row, field) for field in fields}
    if include_payload:
        meta = _payload_meta(_row_payload(row))
        if meta:
            snapshot.update(meta)
    return snapshot


def _constraint_name(exc: IntegrityError) -> str | None:
    """取唯一约束名。

    psycopg 挂在 ``diag.constraint_name``；asyncpg 的异常对象没有稳定的
    ``constraint_name`` 属性，因此再按异常消息回退匹配。只认
    ``unique constraint "<name>"`` 的正式位置：违规 DETAIL 里会回显值，
    按子串搜全文会误判。
    """
    orig = getattr(exc, "orig", None)
    name = getattr(orig, "constraint_name", None)
    if name:
        return name
    name = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if name:
        return name
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


def _resolve_name(name, payload_name, fallback) -> str:
    """创建时的 name 解析：显式入参 → payload.name → 数据源名。"""
    if name is not None and not isinstance(name, str):
        raise InvalidParameterError(
            message="name 必须是非空字符串", details={"field": "name"},
        )
    for candidate in (name, payload_name):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:_MAX_NAME_LEN]
    value = str(fallback or "").strip()
    return value[:_MAX_NAME_LEN] or "contract"


def _normalize_format(value) -> str:
    if value is None or value == "":
        return "json"
    normalized = str(value).strip().lower()
    if normalized == "yml":
        normalized = "yaml"
    if normalized not in _FORMATS:
        raise InvalidParameterError(
            message="format 必须是 json 或 yaml", details={"format": value},
        )
    return normalized


def _detect_format(content: str) -> str:
    """缺省按首个非空白字符判定：``{`` / ``[`` 视为 JSON，其余 YAML。"""
    stripped = content.lstrip()
    return "json" if stripped[:1] in ("{", "[") else "yaml"


class DataContractService:
    async def _require_admin(self, tenant_id: str, user_id) -> None:
        # 先判登录态：`anonymous` 不能拿去查权限表。
        if not _is_authenticated(user_id) or not await is_tenant_admin(
            tenant_id, user_id
        ):
            raise PermissionDeniedError(message="仅租户管理员可维护数据合同")

    @staticmethod
    def _require_login(user_id) -> None:
        if not _is_authenticated(user_id):
            raise PermissionDeniedError(message="需要登录后查看数据合同")

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def list(self, tenant_id, user_id, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        self._require_login(user_id)
        data = data or {}

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
                session, tenant_id=tenant_id, status=status,
                keyword=data.get("keyword"), include_drafts=include_drafts,
                limit=page_size, offset=(page - 1) * page_size,
            )
            # list 与 count 必须是同一 include_drafts 口径，否则 total 与 items 对不上。
            total = await repo.count(
                session, tenant_id=tenant_id, status=status,
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

    async def get(self, tenant_id, user_id, contract_id) -> dict:
        tenant_id = require_tenant(tenant_id)
        self._require_login(user_id)
        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
            )
        if row is None:
            raise ResourceNotFoundError(message="数据合同不存在")
        if _instance_value(row, "status") == "draft" and not await is_tenant_admin(
            tenant_id, user_id
        ):
            # 不暴露草稿存在性：与非管理员读不存在的合同同样回 1002。
            raise ResourceNotFoundError(message="数据合同不存在")
        return repo.to_detail(row)

    async def versions(self, tenant_id, user_id, data_source_id) -> dict:
        tenant_id = require_tenant(tenant_id)
        self._require_login(user_id)
        async with get_db_session() as session:
            rows = await repo.list_versions(
                session, tenant_id=tenant_id, data_source_id=data_source_id,
            )
        if not rows:
            raise ResourceNotFoundError(message="数据合同不存在")
        is_admin = await is_tenant_admin(tenant_id, user_id)
        items = [
            repo.to_detail(row) for row in rows
            if is_admin or _instance_value(row, "status") != "draft"
        ]
        if not items:
            # 只有草稿时对非管理员同样按不存在处理，不暴露版本历史的存在性。
            raise ResourceNotFoundError(message="数据合同不存在")
        return {"items": items}

    async def preview_scaffold(self, tenant_id, user_id, data_source_id) -> dict:
        """预览脚手架（只读，不落库）。"""
        tenant_id = require_tenant(tenant_id)
        self._require_login(user_id)

        async with get_db_session() as session:
            source = await scaffold.load_source(
                session, tenant_id=tenant_id, data_source_id=data_source_id,
            )
            if source is None:
                raise ResourceNotFoundError(message="数据源不存在")
            schema = await scaffold.load_active_schema(
                session, tenant_id=tenant_id, data_source_id=data_source_id,
            )
        if schema is None:
            raise InvalidParameterError(
                message="该数据源没有可用的已编译 schema",
                details={"data_source_id": data_source_id},
            )

        payload, stats, warnings = scaffold.build_contract(
            source, schema["entity_types"],
        )
        # M5：预览阶段提前预警 4MB 上限（创建/保存会走校验器拒绝），
        # 只追加 warning，不改变返回结构。
        payload_bytes = len(
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        if payload_bytes > _MAX_PAYLOAD_BYTES:
            warnings.append(
                f"合同内容约 {payload_bytes / (1024 * 1024):.1f} MB，"
                "超过 4MB 上限，创建/保存会失败"
            )
        return {
            "data_source_id": data_source_id,
            "schema_version": schema.get("schema_version"),
            "payload": payload,
            "stats": stats,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def create_draft(self, tenant_id, user_id, data_source_id, payload=None,
                           name=None, base_version=None, *,
                           _audit_action: str = "data_contract_create") -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        async with get_db_session() as session:
            source = await scaffold.load_source(
                session, tenant_id=tenant_id, data_source_id=data_source_id,
            )
            if source is None:
                raise ResourceNotFoundError(message="数据源不存在")

            if base_version is not None:
                # 克隆：payload/name 来自基准行，调用方入参不参与。
                base = await repo.get_version(
                    session, tenant_id=tenant_id,
                    data_source_id=data_source_id, version=base_version,
                )
                if base is None:
                    raise ResourceNotFoundError(message="基准版本不存在")
                payload = _row_payload(base)
                name = _instance_value(base, "name")
                validate_contract(payload)
                payload = normalize_contract(payload)
            elif payload is None:
                schema = await scaffold.load_active_schema(
                    session, tenant_id=tenant_id, data_source_id=data_source_id,
                )
                if schema is None:
                    raise InvalidParameterError(
                        message="该数据源没有可用的已编译 schema",
                        details={"data_source_id": data_source_id},
                    )
                payload, _stats, _warnings = scaffold.build_contract(
                    source, schema["entity_types"],
                )
                # M1：脚手架产物也必须过服务层校验。build_contract 目前产出的
                # 结构合法，但校验是落库前的统一闸门，不能因来源不同而绕过。
                validate_contract(payload)
            else:
                validate_contract(payload)
                payload = normalize_contract(payload)

            name = _resolve_name(
                name, payload.get("name") if isinstance(payload, dict) else None,
                source.get("name") or data_source_id,
            )

            version = await repo.next_version(
                session, tenant_id=tenant_id, data_source_id=data_source_id,
            )
            try:
                row = await repo.create_draft(
                    session, tenant_id=tenant_id,
                    data_source_id=data_source_id, version=version,
                    name=name, payload=payload, created_by=user_id,
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                constraint = _constraint_name(exc)
                if constraint == "uq_dc_draft":
                    raise ResourceConflictError(
                        message="该数据源已有草稿",
                    ) from exc
                if constraint == "uq_dc_version":
                    raise ResourceConflictError(
                        message="该版本已存在，请重试",
                    ) from exc
                raise
            except Exception:
                await session.rollback()
                raise

        detail = repo.to_detail(row)
        self._audit(
            tenant_id, user_id, _audit_action, detail,
            before=None, after=_snapshot(detail, _AUDIT_FIELDS),
        )
        return detail

    async def update_draft(self, tenant_id, user_id, contract_id, revision,
                           payload=None, name=None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
            )
            if row is None:
                raise ResourceNotFoundError(message="数据合同不存在")
            if _instance_value(row, "status") != "draft":
                raise ResourceConflictError(
                    message="发布后的合同不可修改，请新建草稿",
                )
            current_revision = _instance_value(row, "revision")
            try:
                expected_revision = int(revision)
            except (TypeError, ValueError):
                expected_revision = None
            if (
                expected_revision is None
                or int(current_revision or 0) != expected_revision
            ):
                raise ResourceConflictError(
                    message="版本已变化，请刷新后重试",
                )
            before = _snapshot(row, _AUDIT_FIELDS)

            fields = {}
            if name is not None:
                if not isinstance(name, str) or not name.strip():
                    raise InvalidParameterError(
                        message="name 必须是非空字符串",
                        details={"field": "name"},
                    )
                fields["name"] = name.strip()[:_MAX_NAME_LEN]
            if payload is not None:
                # `payload=None` 与不传同义：不能把 JSON null 写进 NOT NULL 列。
                validate_contract(payload)
                fields["payload"] = normalize_contract(payload)
            if not fields:
                raise InvalidParameterError(message="没有可更新的字段")
            fields["updated_by"] = user_id

            changed = await repo.update_draft(
                session, tenant_id=tenant_id, contract_id=contract_id,
                # M2：传已归一/校验的 revision，不能把原始入参（如 "1"）透给仓储。
                revision=expected_revision, fields=fields,
            )
            if changed == 0:
                await session.rollback()
                raise ResourceConflictError(
                    message="版本已变化，请刷新后重试",
                )
            await session.commit()
            updated = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
            )

        if updated is None:
            # M3：提交后行被并发软删（或改写导致不可见）。操作确实已提交，
            # 因此审计仍以操作前的行 id 兜底记录，不返回全空 200。
            # 选 1003 而非 1002：1002 会误导调用方以为合同从未存在；这里与
            # revision 失配同属「读到的状态已失效」的状态冲突。
            self._audit(
                tenant_id, user_id, "data_contract_update", row,
                before=before, after=None,
            )
            raise ResourceConflictError(message="合同已被删除，请刷新")
        self._audit(
            tenant_id, user_id, "data_contract_update", updated,
            before=before, after=_snapshot(updated, _AUDIT_FIELDS),
        )
        return repo.to_detail(updated)

    async def publish(self, tenant_id, user_id, contract_id, revision) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        # M2：发布是状态机的乐观锁入参，只接受真整数。字符串数字、float、
        # bool（int 子类）与 None 都视为非法入参（1001），不做宽松转换——
        # 宽松转换会把 "1"/1.9/True 静默当成同一个 revision。
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise InvalidParameterError(
                message="revision 必须是整数",
                details={"field": "revision", "revision": revision},
            )
        expected_revision = revision

        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
            )
            if row is None:
                raise ResourceNotFoundError(message="数据合同不存在")
            if _instance_value(row, "status") != "draft":
                raise ResourceConflictError(message="只有草稿可以发布")
            # 发布口径比草稿保存更严：必须带非空 schema。
            validate_contract(_row_payload(row), require_schema=True)
            before = _snapshot(row, _AUDIT_FIELDS)
            try:
                # 仓储内部先 FOR UPDATE 再校验 revision/版本先后。
                changed = await repo.publish(
                    session, tenant_id=tenant_id, contract_id=contract_id,
                    revision=expected_revision, user_id=user_id,
                )
                if changed == repo.PUBLISH_STALE_DRAFT:
                    # 锁内发现已有更高版本的 published：禁止旧草稿覆盖。
                    await session.rollback()
                    raise ResourceConflictError(
                        message="已有更新的发布版本，旧草稿不可发布",
                    )
                if changed != repo.PUBLISH_OK:
                    await session.rollback()
                    raise ResourceConflictError(
                        message="版本已变化，请刷新后重试",
                    )
                await session.commit()
            except ResourceConflictError:
                raise
            except IntegrityError as exc:
                await session.rollback()
                if _constraint_name(exc) == "uq_dc_published":
                    raise ResourceConflictError(
                        message="该数据源已有发布版本，请刷新后重试",
                    ) from exc
                raise
            except Exception:
                await session.rollback()
                raise
            updated = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
            )

        if updated is None:
            # M3：发布已提交但行随即被并发软删。审计按操作前行 id 兜底，
            # 抛 1003（理由同 update_draft：行存在过，不是 1002 的「不存在」）。
            self._audit(
                tenant_id, user_id, "data_contract_publish", row,
                before=before, after=None,
            )
            raise ResourceConflictError(message="合同已被删除，请刷新")
        self._audit(
            tenant_id, user_id, "data_contract_publish", updated,
            before=before, after=_snapshot(updated, _AUDIT_FIELDS),
        )
        return repo.to_detail(updated)

    async def retire(self, tenant_id, user_id, contract_id) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
            )
            if row is None:
                raise ResourceNotFoundError(message="数据合同不存在")
            before = _snapshot(row, _AUDIT_FIELDS)
            changed = await repo.retire(
                session, tenant_id=tenant_id, contract_id=contract_id,
                user_id=user_id,
            )
            if changed == 0:
                await session.rollback()
                raise ResourceConflictError(
                    message="只有已发布的合同可以退役",
                )
            await session.commit()
            updated = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
            )

        if updated is None:
            # M3：退役已提交但行随即被并发软删。审计按操作前行 id 兜底，
            # 抛 1003（理由同 update_draft）。retire 无 revision 入参，不能再让
            # 调用方重试同一动作，刷新后由调用方重新读取状态。
            self._audit(
                tenant_id, user_id, "data_contract_retire", row,
                before=before, after=None,
            )
            raise ResourceConflictError(message="合同已被删除，请刷新")
        self._audit(
            tenant_id, user_id, "data_contract_retire", updated,
            before=before, after=_snapshot(updated, _AUDIT_FIELDS),
        )
        return repo.to_detail(updated)

    async def delete_draft(self, tenant_id, user_id, contract_id) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
            )
            if row is None:
                raise ResourceNotFoundError(message="数据合同不存在")
            before = _snapshot(row, _AUDIT_FIELDS)
            changed = await repo.delete_draft(
                session, tenant_id=tenant_id, contract_id=contract_id,
                user_id=user_id,
            )
            if changed == 0:
                await session.rollback()
                raise ResourceConflictError(message="只有草稿可以删除")
            await session.commit()
            # 软删除后普通读取拿不到行，必须显式带 include_deleted 才能记录 after。
            deleted_row = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
                include_deleted=True,
            )

        self._audit(
            tenant_id, user_id, "data_contract_delete", row,
            before=before,
            after=_snapshot(deleted_row, _AUDIT_FIELDS + ("is_deleted",)),
        )
        return {"contract_id": contract_id, "deleted": True}

    # ------------------------------------------------------------------
    # 导入 / 导出
    # ------------------------------------------------------------------

    async def import_contract(self, tenant_id, user_id, data_source_id, content,
                              format=None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._require_admin(tenant_id, user_id)

        if not isinstance(content, str) or not content.strip():
            raise InvalidParameterError(message="导入内容不能为空")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InvalidParameterError(
                message="导入内容含非法字符",
            ) from exc
        if size > _MAX_IMPORT_BYTES:
            raise InvalidParameterError(message="导入内容超过 4MB")

        if format is None or format == "":
            normalized = _detect_format(content)
        else:
            normalized = str(format).strip().lower()
            if normalized == "yml":
                normalized = "yaml"
            if normalized not in _FORMATS:
                raise InvalidParameterError(
                    message="format 必须是 json 或 yaml",
                    details={"format": format},
                )

        try:
            if normalized == "json":
                parsed = json.loads(content)
            else:
                parsed = yaml.safe_load(content)
        except Exception as exc:  # JSONDecodeError / YAMLError / 编码错误
            raise InvalidParameterError(
                message=f"合同解析失败：{exc}",
                details={"format": normalized},
            ) from exc
        if not isinstance(parsed, dict):
            raise InvalidParameterError(message="合同内容必须是对象")

        # 审计只保留一条：由 create_draft 按传入的 action 发出。
        return await self.create_draft(
            tenant_id, user_id, data_source_id, payload=parsed,
            _audit_action="data_contract_import",
        )

    async def export_contract(self, tenant_id, user_id, contract_id,
                              format="json") -> dict:
        tenant_id = require_tenant(tenant_id)
        self._require_login(user_id)
        normalized = _normalize_format(format)

        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, contract_id=contract_id,
            )
        if row is None:
            raise ResourceNotFoundError(message="数据合同不存在")
        if _instance_value(row, "status") == "draft" and not await is_tenant_admin(
            tenant_id, user_id
        ):
            raise ResourceNotFoundError(message="数据合同不存在")

        payload = copy.deepcopy(_row_payload(row))
        status = _instance_value(row, "status")
        payload["status"] = _EXPORT_STATUS.get(status, status)

        if normalized == "json":
            content = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            content = yaml.safe_dump(
                payload, allow_unicode=True, sort_keys=False,
            )
        version = _instance_value(row, "version")
        data_source_id = _instance_value(row, "data_source_id")
        return {
            "content": content,
            "format": normalized,
            "filename": f"{data_source_id}-{version}.{normalized}",
        }

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def _audit(self, tenant_id, user_id, action, row, *, before, after) -> None:
        # 入参既可能是仓储原始行（`id`），也可能是 `to_detail` 的结果（`contract_id`）。
        contract_id = _instance_value(row, "contract_id")
        if contract_id is None:
            contract_id = _instance_value(row, "id")
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "log_type": "OPERATION",
            "action": action,
            "outcome": "SUCCESS",
            "service_name": "data_governance",
            "resource": _RESOURCE_TYPE,
            "resource_id": str(contract_id),
            "request_params": {"before": before, "after": after},
        })
