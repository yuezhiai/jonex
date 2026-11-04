"""
业务领域 — 工具目录管理（平台管理员）

`business_domain.skill_catalog` 是平台共享表（无 tenant_id）：这里的新建 / 编辑 / 上下架
对所有租户生效，因此只允许平台管理员调用（校验在 capability 层 `_require_platform_admin`）。
租户侧仍只做启用 / 停用（`SkillService`）。

运行时配置
    写在 `capability_json.runtime`，格式与校验见 `jonex_core/common/tool_runtime.py`
    （`echo` 回显 / `api` 调用普通 HTTP API；知识库工具用的是同一份）。
    `runtime` 为 null 表示该工具没有可调入口（如 MCP 制品类工具），执行器返回 no_runtime。
    api 的地址在**保存时**就做 SSRF 校验（与执行时同一口径、同一白名单
    `ONTOLOGY_TOOL_RUNTIME_ALLOWED_HOSTS`），避免存进一个执行时必然被拒的地址。
    鉴权凭据（Bearer token / API Key / Basic 密码）经 `encrypt_secret` 加密后存在
    `capability_json.runtime_secret`，任何视图都不回传，只回 `has_secret`。
    `capability_json` 里其它键（requires_file / supports_batch 等）原样保留。

不可修改项
    `tool_name` 创建后不可改：工具绑定（knowledge_base.ontology_tool_bindings）按它引用工具，
    改名会让所有绑定悬空。要换名请新建工具再迁移绑定。
"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Optional

from sqlalchemy import func, or_, select

from jonex_core.common import get_db_session
from jonex_core.common.crypto import encrypt_secret
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceAlreadyExistsError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tool_runtime import (
    RUNTIME_TYPES,
    requires_secret,
    validate_runtime,
    validate_tool_schema,
)

from capabilities.business_domain.models.skill import SkillCatalog
from capabilities.business_domain.repository.skill_repository import SkillCatalogRepository

CATEGORIES = ("image", "voice", "document", "video", "fusion", "custom")
STATUSES = ("published", "disabled")
# capability_json 里由本服务管理的两个键：运行时配置、加密后的凭据
RUNTIME_KEY = "runtime"
SECRET_KEY = "runtime_secret"
# 只读声明（默认 false = 写操作）：只有显式声明只读的工具才可能在问答里被自动调用
READ_ONLY_KEY = "read_only"
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_MAX_TAGS = 20
_MAX_SECRET_LEN = 4096


def _bad(key: str, fallback: str, **params) -> InvalidParameterError:
    return InvalidParameterError(message=translate(key, params=params or None, fallback=fallback))


def _field_err(field: str, reason: str) -> InvalidParameterError:
    return _bad("err.skill_catalog.invalid_field", f"{field} 不合法：{reason}", field=field, reason=reason)


# ── 校验（纯函数）───────────────────────────────────────────────────


def clean_secret(value: Any) -> Optional[str]:
    """表单里的凭据：None / 空串 = 不修改；其余必须是不超过 4096 字的字符串。"""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > _MAX_SECRET_LEN:
        raise _field_err("secret", f"必须是字符串，不超过 {_MAX_SECRET_LEN} 字")
    return value


# 入参 / 出参 schema 校验与知识库工具同一份
validate_object_schema = validate_tool_schema


_KEEP = object()


def clean_read_only(value: Any) -> bool:
    """只读声明必须是显式布尔值；请求方式是 GET 不等于只读，不做推断。"""
    if not isinstance(value, bool):
        raise _field_err("read_only", "必须是布尔值")
    return value


def _merge_capability(
    existing: Optional[dict], runtime: Any = _KEEP, *,
    secret: Optional[str] = None, clear_secret: bool = False,
    read_only: Any = _KEEP,
) -> dict:
    """合并运行时、凭据与只读声明，返回**新的** dict（JSONB 原地改不会被检测到）。

    runtime：`_KEEP` 不动；None 去掉运行时（连同凭据）；dict 替换。
    secret：非空则加密写入；clear_secret 清掉已存凭据。运行时不再需要鉴权时凭据一并清掉，
    不留一个用不到的密文。
    read_only：`_KEEP` 不动；布尔值写入。
    """
    cap = copy.deepcopy(existing or {})
    if read_only is not _KEEP:
        cap[READ_ONLY_KEY] = bool(read_only)
    if runtime is not _KEEP:
        if runtime is None:
            cap.pop(RUNTIME_KEY, None)
        else:
            cap[RUNTIME_KEY] = runtime
    if clear_secret:
        cap.pop(SECRET_KEY, None)
    if secret:
        cap[SECRET_KEY] = encrypt_secret(secret)
    if not requires_secret(cap.get(RUNTIME_KEY)):
        cap.pop(SECRET_KEY, None)
    return cap


def _clean_tags(tags: Any) -> list:
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise _field_err("tags", "必须是字符串数组")
    out = []
    for t in tags:
        s = str(t).strip()
        if s and s not in out:
            out.append(s[:32])
    return out[:_MAX_TAGS]


def to_admin_item(obj: SkillCatalog) -> dict:
    """管理端视图：带状态与运行时（租户视图 to_skill_item 不暴露运行时地址）。"""
    cap = obj.capability_json or {}
    return {
        "id": obj.id,
        "name": obj.name,
        "description": obj.description,
        "category": obj.category,
        "icon": obj.icon,
        "status": obj.status,
        "tool_name": obj.tool_name,
        "instruction": obj.instruction,
        "input_schema": obj.input_schema_json or {},
        "output_schema": obj.output_schema_json or {},
        "tags": obj.tags_json or [],
        "runtime": cap.get(RUNTIME_KEY),
        "has_secret": bool(cap.get(SECRET_KEY)),
        "read_only": cap.get(READ_ONLY_KEY) is True,
        "capability": {k: v for k, v in cap.items()
                       if k not in (RUNTIME_KEY, SECRET_KEY, READ_ONLY_KEY)},
        "has_artifact": bool(obj.artifact_object_key),
        "created_at": obj.created_at.isoformat() if getattr(obj, "created_at", None) else None,
        "updated_at": obj.updated_at.isoformat() if getattr(obj, "updated_at", None) else None,
    }


# ── 服务 ─────────────────────────────────────────────────────────────


class SkillCatalogAdminService:
    """平台工具目录 CRUD（不删除，只上下架；平台共享实体，不传租户）。"""

    async def list(
        self, offset: int = 0, limit: int = 20,
        status: Optional[str] = None, keyword: Optional[str] = None,
    ) -> dict:
        offset = max(0, int(offset or 0))
        limit = max(1, min(200, int(limit or 20)))
        filters = [SkillCatalog.is_deleted == 0]
        if status:
            if status not in STATUSES:
                raise _field_err("status", "只能是 published 或 disabled")
            filters.append(SkillCatalog.status == status)
        if keyword:
            pattern = f"%{keyword.strip()}%"
            filters.append(or_(
                SkillCatalog.name.ilike(pattern),
                SkillCatalog.tool_name.ilike(pattern),
                SkillCatalog.description.ilike(pattern),
            ))
        async with get_db_session() as session:
            rows = (await session.execute(
                select(SkillCatalog).where(*filters)
                .order_by(SkillCatalog.created_at.desc()).offset(offset).limit(limit)
            )).scalars().all()
            total = (await session.execute(
                select(func.count()).select_from(SkillCatalog).where(*filters)
            )).scalar_one()
        return {"items": [to_admin_item(r) for r in rows], "total": int(total),
                "offset": offset, "limit": limit}

    async def get(self, skill_id: str) -> dict:
        async with get_db_session() as session:
            obj = await SkillCatalogRepository(session).get_required_shared(skill_id)
            return to_admin_item(obj)

    async def create(self, data: dict) -> dict:
        tool_name = str(data.get("tool_name") or "").strip()
        if not _TOOL_NAME_RE.match(tool_name):
            raise _bad("err.skill_catalog.invalid_tool_name",
                       "工具名只能用小写字母、数字和下划线，以字母开头，2–128 位")
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 255:
            raise _field_err("name", "不能为空，且不超过 255 字")
        category = str(data.get("category") or "custom").strip()
        if category not in CATEGORIES:
            raise _field_err("category", f"只能是 {'/'.join(CATEGORIES)}")
        status = str(data.get("status") or "published").strip()
        if status not in STATUSES:
            raise _field_err("status", "只能是 published 或 disabled")
        description = (data.get("description") or "").strip() or None
        instruction = (data.get("instruction") or "").strip() or description or name
        input_schema = validate_object_schema(data.get("input_schema"), "input_schema")
        output_schema = validate_object_schema(data.get("output_schema"), "output_schema")
        runtime = validate_runtime(data.get("runtime"))
        secret = clean_secret(data.get("secret"))
        tags = _clean_tags(data.get("tags"))
        read_only = clean_read_only(data.get("read_only", False))

        async with get_db_session() as session:
            # tool_name 是整表唯一（软删的行也占名字），先查一次给出可读错误
            taken = (await session.execute(
                select(SkillCatalog.id).where(SkillCatalog.tool_name == tool_name)
            )).first()
            if taken:
                raise ResourceAlreadyExistsError(message=translate(
                    "err.skill_catalog.tool_name_taken", params={"tool_name": tool_name},
                    fallback=f"工具名已被占用：{tool_name}",
                ))
            obj = await SkillCatalogRepository(session).create(
                id=f"skill_{uuid.uuid4().hex[:16]}",
                name=name,
                description=description,
                category=category,
                icon=(data.get("icon") or None),
                status=status,
                tool_name=tool_name,
                instruction=instruction,
                input_schema_json=input_schema,
                output_schema_json=output_schema,
                # 非制品类工具没有 MCP 制品包；两列 NOT NULL，用空串表示「无制品」
                artifact_bucket="",
                artifact_object_key="",
                tags_json=tags,
                capability_json=_merge_capability({}, runtime, secret=secret, read_only=read_only),
            )
            await session.commit()
            return to_admin_item(obj)

    async def update(self, skill_id: str, data: dict) -> dict:
        if "tool_name" in data:
            async with get_db_session() as session:
                current = await SkillCatalogRepository(session).get_required_shared(skill_id)
                if str(data.get("tool_name") or "").strip() != current.tool_name:
                    raise _bad("err.skill_catalog.tool_name_immutable",
                               "工具名创建后不可修改（工具绑定按工具名引用）")
        values: dict = {}
        if "name" in data:
            name = str(data.get("name") or "").strip()
            if not name or len(name) > 255:
                raise _field_err("name", "不能为空，且不超过 255 字")
            values["name"] = name
        if "description" in data:
            values["description"] = (data.get("description") or "").strip() or None
        if "instruction" in data:
            instruction = (data.get("instruction") or "").strip()
            if not instruction:
                raise _field_err("instruction", "不能为空")
            values["instruction"] = instruction
        if "category" in data:
            category = str(data.get("category") or "").strip()
            if category not in CATEGORIES:
                raise _field_err("category", f"只能是 {'/'.join(CATEGORIES)}")
            values["category"] = category
        if "status" in data:
            status = str(data.get("status") or "").strip()
            if status not in STATUSES:
                raise _field_err("status", "只能是 published 或 disabled")
            values["status"] = status
        if "icon" in data:
            values["icon"] = data.get("icon") or None
        if "input_schema" in data:
            values["input_schema_json"] = validate_object_schema(data.get("input_schema"), "input_schema")
        if "output_schema" in data:
            values["output_schema_json"] = validate_object_schema(data.get("output_schema"), "output_schema")
        if "tags" in data:
            values["tags_json"] = _clean_tags(data.get("tags"))
        runtime = validate_runtime(data.get("runtime")) if "runtime" in data else _KEEP
        secret = clean_secret(data.get("secret"))
        clear_secret = bool(data.get("clear_secret"))
        read_only = clean_read_only(data.get("read_only")) if "read_only" in data else _KEEP

        async with get_db_session() as session:
            repo = SkillCatalogRepository(session)
            obj = await repo.get_required_shared(skill_id)
            if runtime is not _KEEP or secret or clear_secret or read_only is not _KEEP:
                # JSONB 原地改不会被检测到，整体赋新 dict
                values["capability_json"] = _merge_capability(
                    obj.capability_json, runtime, secret=secret, clear_secret=clear_secret,
                    read_only=read_only,
                )
            for key, value in values.items():
                setattr(obj, key, value)
            await session.flush()
            await session.commit()
            return to_admin_item(obj)


__all__ = [
    "CATEGORIES",
    "READ_ONLY_KEY",
    "RUNTIME_KEY",
    "RUNTIME_TYPES",
    "SECRET_KEY",
    "STATUSES",
    "SkillCatalogAdminService",
    "clean_read_only",
    "clean_secret",
    "to_admin_item",
    "validate_object_schema",
    "validate_runtime",
]
