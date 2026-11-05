#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""知识库工具（KB 级工具目录）：本库登记的可执行 API 工具。

与平台工具的关系
    平台工具目录（business_domain.skill_catalog）由平台管理员维护、租户启用；本服务管理的是
    知识库自己登记的工具，只在这个库里可见、可绑定。两者在工具绑定上用 `tool_source`
    区分（platform / kb），运行时格式共用 `jonex_core/common/tool_runtime.py`，
    一次调用的公共收尾（解密、必填、截断、状态）共用 `jonex_core/common/tool_invocation.py`。

权限
    管理（新建 / 编辑 / 删除 / 试运行）按知识库写口径（空间管理者 / 租户管理员 / kb_manager），
    在 capability 层判定；列表按读口径。出站地址在保存与调用时都做 SSRF 校验。

只读声明
    `read_only` 由登记人显式勾选（默认 false = 写操作）。只有声明了只读的工具才可能在问答里
    被自动调用；请求方式是 GET 不等于只读，不做推断。

凭据
    鉴权凭据经 `encrypt_secret` 加密后存 `secret_encrypted`，任何接口都不回明文或密文，
    只回 `has_secret`。表单里 `secret` 留空 = 不修改，`clear_secret: true` = 清掉。

执行
    `run`（内部 action `run_kb_tool`）只供 business_domain 的本体动作执行器调用：执行器已经做完
    判定、确认、入参组装并落了执行审计，这里只负责「拿凭据、调接口」，凭据因此不离开
    knowledge-base 服务。`test`（`test_kb_tool`）是工具登记人联调用的试运行，不经过本体契约
    判定，每次都落一条审计。
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Callable, Optional

from sqlalchemy.exc import IntegrityError

from jonex_core.common import get_db_session
from jonex_core.common.audit import schedule_emit
from jonex_core.common.crypto import encrypt_secret
from jonex_core.common.exceptions import InvalidParameterError, ResourceAlreadyExistsError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.common.tool_invocation import (
    STATUS_TOOL_UNAVAILABLE,
    invoke_tool,
    tool_audit_entry,
)
from jonex_core.common.tool_runtime import requires_secret, validate_runtime, validate_tool_schema

from ..repository.kb_tool_repository import KbToolRepository
from ..repository.knowledge_info_repository import KnowledgeInfoRepository

logger = logging.getLogger(__name__)

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
MAX_SECRET_LEN = 4096


def _field_err(field: str, reason: str) -> InvalidParameterError:
    return InvalidParameterError(message=translate(
        "err.kb_tool.invalid_field", params={"field": field, "reason": reason},
        fallback=f"{field} 不合法：{reason}",
    ))


def _clean_secret(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > MAX_SECRET_LEN:
        raise _field_err("secret", f"必须是字符串，不超过 {MAX_SECRET_LEN} 字")
    return value


def _clean_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 255:
        raise _field_err("name", "不能为空，且不超过 255 字")
    return name


def _clean_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise _field_err(field, "必须是布尔值")
    return value


class KbToolService:
    """知识库工具 CRUD + 试运行 + 内部调用。"""

    def __init__(self, audit: Optional[Callable[[dict], None]] = None):
        self._audit = audit or schedule_emit

    async def _ensure_kb(self, session, kb_id: str, tenant_id: str) -> None:
        await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)

    # ── 管理 ──

    async def list(
        self, tenant_id: str, kb_id: str, page: int = 1, page_size: int = 50,
        keyword: Optional[str] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        page = max(1, int(page or 1))
        page_size = max(1, min(200, int(page_size or 50)))
        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            items, total = await KbToolRepository(session).list_by_kb(
                tenant_id, kb_id, offset=(page - 1) * page_size, limit=page_size, keyword=keyword,
            )
            return {"items": [t.to_dict() for t in items], "total": total,
                    "page": page, "page_size": page_size}

    async def create(self, tenant_id: str, data: dict, user_id: Optional[str] = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id = data["knowledge_base_id"]
        tool_name = str(data.get("tool_name") or "").strip()
        if not TOOL_NAME_RE.match(tool_name):
            raise InvalidParameterError(message=translate(
                "err.kb_tool.invalid_tool_name",
                fallback="工具名只能用小写字母、数字和下划线，以字母开头，2–128 位",
            ))
        name = _clean_name(data.get("name"))
        description = (data.get("description") or "").strip() or None
        input_schema = validate_tool_schema(data.get("input_schema"), "input_schema")
        output_schema = validate_tool_schema(data.get("output_schema"), "output_schema")
        runtime = validate_runtime(data.get("runtime"))
        secret = _clean_secret(data.get("secret"))
        enabled = bool(data.get("enabled", True))
        read_only = _clean_bool(data.get("read_only", False), "read_only")

        async with get_db_session() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            repo = KbToolRepository(session)
            if await repo.get_by_name(tenant_id, kb_id, tool_name):
                raise self._taken(tool_name)
            try:
                obj = await repo.create(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    knowledge_base_id=kb_id,
                    tool_name=tool_name,
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    runtime=runtime,
                    secret_encrypted=encrypt_secret(secret) if secret and requires_secret(runtime) else None,
                    enabled=enabled,
                    read_only=read_only,
                    created_by=user_id,
                    updated_by=user_id,
                )
                await session.commit()
            except IntegrityError:
                # 并发下两条请求都过了上面的查重，由部分唯一索引兜底
                await session.rollback()
                raise self._taken(tool_name)
            return obj.to_dict()

    async def update(
        self, tenant_id: str, tool_id: str, data: dict, user_id: Optional[str] = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        values: dict = {}
        if "name" in data:
            values["name"] = _clean_name(data.get("name"))
        if "description" in data:
            values["description"] = (data.get("description") or "").strip() or None
        if "input_schema" in data:
            values["input_schema"] = validate_tool_schema(data.get("input_schema"), "input_schema")
        if "output_schema" in data:
            values["output_schema"] = validate_tool_schema(data.get("output_schema"), "output_schema")
        if "enabled" in data:
            values["enabled"] = bool(data.get("enabled"))
        if "read_only" in data:
            values["read_only"] = _clean_bool(data.get("read_only"), "read_only")
        runtime_given = "runtime" in data
        runtime = validate_runtime(data.get("runtime")) if runtime_given else None
        secret = _clean_secret(data.get("secret"))
        clear_secret = bool(data.get("clear_secret"))

        async with get_db_session() as session:
            obj = await KbToolRepository(session).get_required(tool_id, tenant_id)
            if "tool_name" in data and str(data.get("tool_name") or "").strip() != obj.tool_name:
                raise InvalidParameterError(message=translate(
                    "err.kb_tool.tool_name_immutable",
                    fallback="工具名创建后不可修改（工具绑定按工具名引用）",
                ))
            if runtime_given:
                values["runtime"] = runtime
            for key, value in values.items():
                setattr(obj, key, value)
            if clear_secret:
                obj.secret_encrypted = None
            if secret:
                obj.secret_encrypted = encrypt_secret(secret)
            # 运行时不再需要鉴权时凭据一并清掉，不留一个用不到的密文
            if not requires_secret(obj.runtime):
                obj.secret_encrypted = None
            obj.updated_by = user_id
            await session.flush()
            await session.commit()
            return obj.to_dict()

    async def delete(self, tenant_id: str, tool_id: str) -> bool:
        """软删；绑定到它的行留着，列表标「工具不可用」，执行器按不可用拒绝。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KbToolRepository(session)
            obj = await repo.get_required(tool_id, tenant_id)
            await repo.delete_soft(obj)
            await session.commit()
            return True

    @staticmethod
    def _taken(tool_name: str) -> ResourceAlreadyExistsError:
        return ResourceAlreadyExistsError(message=translate(
            "err.kb_tool.tool_name_taken", params={"tool_name": tool_name},
            fallback=f"本知识库已有同名工具：{tool_name}",
        ))

    # ── 调用 ──

    async def test(
        self, tenant_id: str, tool_id: str, data: dict, *,
        user_id: Optional[str] = None, username: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """工具登记人联调：按给定入参调一次（停用的工具也能试，便于先调通再启用）。每次落审计。"""
        tenant_id = require_tenant(tenant_id)
        arguments = data.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _field_err("arguments", "必须是对象")
        async with get_db_session() as session:
            obj = await KbToolRepository(session).get_required(tool_id, tenant_id)
            snapshot = (obj.runtime, obj.secret_encrypted, obj.input_schema or {},
                        obj.tool_name, obj.knowledge_base_id)
        runtime, secret_enc, schema, tool_name, kb_id = snapshot
        result = await invoke_tool(runtime, secret_enc, schema, arguments, {
            "tenant_id": tenant_id, "knowledge_base_id": kb_id, "tool_name": tool_name,
            "tool_source": "kb", "request_id": request_id,
        })
        self._emit(tool_audit_entry(
            tenant_id=tenant_id, action="test_kb_tool", service_name="knowledge_base",
            status=result["status"], tool_name=tool_name, tool_source="kb",
            user_id=user_id, username=username, request_id=request_id,
            duration_ms=result.get("duration_ms"), resource="kb_tool",
            request_params={"knowledge_base_id": kb_id, "input_keys": sorted(arguments.keys())},
            response_body={"http_status": result.get("http_status"), "error": result.get("error")},
        ))
        return result

    async def run(self, tenant_id: str, data: dict) -> dict:
        """内部：本体动作执行器的调用入口（判定、确认、入参组装与执行审计都已在执行器完成）。"""
        tenant_id = require_tenant(tenant_id)
        kb_id = data["knowledge_base_id"]
        tool_name = str(data.get("tool_name") or "").strip()
        arguments = data.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _field_err("arguments", "必须是对象")
        async with get_db_session() as session:
            obj = await KbToolRepository(session).get_by_name(tenant_id, kb_id, tool_name)
            if obj is None or not obj.enabled:
                return {"status": STATUS_TOOL_UNAVAILABLE, "ok": False}
            snapshot = (obj.runtime, obj.secret_encrypted, obj.input_schema or {})
        runtime, secret_enc, schema = snapshot
        return await invoke_tool(runtime, secret_enc, schema, arguments, {
            "tenant_id": tenant_id, "knowledge_base_id": kb_id, "tool_name": tool_name,
            "tool_source": "kb", "action_name": data.get("action_name"),
            "request_id": data.get("request_id"),
        })

    def _emit(self, entry: dict) -> None:
        try:
            self._audit(entry)
        except Exception as exc:  # noqa: BLE001 — 审计失败不影响调用结果
            logger.warning("知识库工具审计写入失败: %s", exc)


__all__ = ["KbToolService", "TOOL_NAME_RE", "invoke_tool"]
