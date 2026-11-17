#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""N1-1 verified query 服务：登记 / 激活 / 退役 / 预览匹配。

设计口径见 `docs/superpowers/specs/2026-11-07-nl2sql-verified-query-design.md`：

- **保存只做形状校验**（名称、问句、参数正则、占位符与参数声明一致）；
- **激活才做可执行性校验**（方言可解析 + 模板表都在 compiled schema 里 + 无未知列）。
  compiled schema 不存在时只做方言解析（运行时命中还会再校验一次，stale 回退）。
- 运行时（`service.py::_try_verified`）只用 `verified.py` 的纯函数 + 仓储读口，
  不经过本服务的写路径。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.tenant import require_tenant

from ..models.nl2sql_verified_query import STATUS_ACTIVE, STATUS_DRAFT, STATUS_RETIRED
from ..repository.nl2sql_verified_query_repository import Nl2SqlVerifiedQueryRepository
from .governance.common import ensure_kb
from .nl2sql import verified
from .nl2sql.precompile import check_precompile

_MAX_QUESTIONS = 20
_MAX_QUESTION_CHARS = 500
_MAX_SQL_CHARS = 10000
_MAX_PARAMS = 20
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_PARAM_NAME = re.compile(r"^\w{1,32}$")

_SAMPLE_VALUES = {
    "string": "sample",
    "number": "1",
    "date": "2026-01-01",
    "boolean": "true",
}


def _invalid(message: str) -> InvalidParameterError:
    return InvalidParameterError(message=message)


def _normalize_questions(raw) -> list[str]:
    if not isinstance(raw, list):
        raise _invalid("questions 必须是数组")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > _MAX_QUESTION_CHARS:
            raise _invalid(f"单条问句超长（最多 {_MAX_QUESTION_CHARS} 字符）")
        key = verified.normalize_question(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    if not out:
        raise _invalid("至少需要一条问句")
    if len(out) > _MAX_QUESTIONS:
        raise _invalid(f"问句最多 {_MAX_QUESTIONS} 条")
    return out


def _normalize_params(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _invalid("params 必须是数组")
    if len(raw) > _MAX_PARAMS:
        raise _invalid(f"参数最多 {_MAX_PARAMS} 个")
    out: list[dict] = []
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("params 条目必须是对象")
        name = str(item.get("name") or "").strip()
        if not _PARAM_NAME.match(name):
            raise _invalid(f"非法参数名：{name}")
        if name in names:
            raise _invalid(f"参数名重复：{name}")
        names.add(name)
        kind = str(item.get("type") or "string").strip().lower()
        if kind not in verified.PARAM_TYPES:
            raise _invalid(f"非法参数类型：{kind}")
        pattern = str(item.get("extract") or "").strip()
        if not pattern:
            raise _invalid(f"参数 {name} 缺少 extract 正则")
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise _invalid(f"参数 {name} 的 extract 正则无法编译：{exc}") from exc
        if regex.groups < 1:
            raise _invalid(f"参数 {name} 的 extract 正则必须含一个捕获组")
        enum_values: list[str] = []
        if kind == "enum":
            enum_values = [str(v).strip() for v in item.get("enum") or []
                           if str(v).strip()]
            if not enum_values:
                raise _invalid(f"枚举参数 {name} 必须给出 enum 取值")
        description = str(item.get("description") or "").strip()[:200]
        out.append({
            "name": name,
            "type": kind,
            "required": bool(item.get("required", True)),
            "description": description,
            "extract": pattern,
            **({"enum": enum_values} if enum_values else {}),
        })
    return out


def normalize_verified_query_payload(data: dict) -> dict:
    """保存入口的载荷归一；所有失败都是 1001（参数错误）。"""
    payload = data or {}
    kb_id = str(payload.get("knowledge_base_id") or "").strip()
    ds_id = str(payload.get("data_source_id") or "").strip()
    if not kb_id:
        raise _invalid("knowledge_base_id 不能为空")
    if not ds_id:
        raise _invalid("data_source_id 不能为空")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise _invalid("名称不能为空")
    if len(name) > 128:
        raise _invalid("名称超长（最多 128 字符）")
    description = str(payload.get("description") or "").strip()
    if len(description) > 500:
        raise _invalid("描述超长（最多 500 字符）")
    questions = _normalize_questions(payload.get("questions"))
    sql_template = str(payload.get("sql_template") or "").strip()
    if not sql_template:
        raise _invalid("sql_template 不能为空")
    if len(sql_template) > _MAX_SQL_CHARS:
        raise _invalid(f"sql_template 超长（最多 {_MAX_SQL_CHARS} 字符）")
    params = _normalize_params(payload.get("params"))

    declared = {p["name"] for p in params}
    used = set(_PLACEHOLDER.findall(sql_template))
    if declared != used:
        missing = sorted(declared - used)
        unknown = sorted(used - declared)
        raise _invalid(
            "模板占位符与参数声明不一致"
            + (f"；未使用：{missing}" if missing else "")
            + (f"；未声明：{unknown}" if unknown else ""))

    return {
        "knowledge_base_id": kb_id,
        "data_source_id": ds_id,
        "name": name,
        "description": description,
        "questions": questions,
        "sql_template": sql_template,
        "params": params,
    }


def _sample_value(param: dict) -> str:
    if param["type"] == "enum":
        return (param.get("enum") or ["x"])[0]
    return _SAMPLE_VALUES.get(param["type"], "1")


def validate_activation(sql_template: str, params: list[dict], *,
                        engine: str, schema_map: Optional[dict[str, set[str]]]) -> None:
    """激活前校验：模板在该方言下可解析、表都在 compiled schema、无未知/歧义列。

    `schema_map=None` ⇒ 没有 compiled schema：只做方言解析（运行时还会 stale 校验）。
    失败一律 1001（人工动作，fail-closed 比放行一条坏模板便宜）。
    """
    values = {p["name"]: _sample_value(p) for p in params or []}
    try:
        rendered = verified.render_template(sql_template, params or [], values)
    except ValueError as exc:
        raise _invalid(f"模板渲染失败：{exc}") from exc

    tables = verified.template_tables(rendered, engine)
    if tables is None:
        raise _invalid("SQL 模板在该数据源方言下无法解析")
    if not schema_map:
        return

    known = {str(key).casefold() for key in schema_map}
    unknown_tables = sorted(
        t for t in tables
        if str(t).split(".")[-1].strip().strip('"[]`').casefold() not in known)
    if unknown_tables:
        raise _invalid(f"模板引用了 compiled schema 里没有的表：{unknown_tables}")

    violation = check_precompile(rendered, engine=engine, schema_map=schema_map)
    if violation is not None:
        raise _invalid(f"模板校验失败（{violation.code}）：{violation.detail}")


class Nl2SqlVerifiedQueryService:
    def __init__(self, session) -> None:
        self.session = session
        self.repo = Nl2SqlVerifiedQueryRepository(session)

    async def list(self, tenant_id: str, kb_id: str,
                   data_source_id: str = "") -> dict:
        tenant_id = require_tenant(tenant_id)
        await ensure_kb(self.session, kb_id, tenant_id)
        rows = await self.repo.list_for_kb(tenant_id, kb_id, data_source_id)
        return {"items": [r.to_dict() for r in rows], "total": len(rows)}

    async def get(self, tenant_id: str, kb_id: str, verified_query_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        await ensure_kb(self.session, kb_id, tenant_id)
        row = await self.repo.get(tenant_id, kb_id, verified_query_id)
        if row is None:
            raise ResourceNotFoundError(message="已验证查询不存在")
        return row.to_dict()

    async def save(self, tenant_id: str, data: dict, *,
                   user_id: Optional[str] = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        payload = normalize_verified_query_payload(data)
        kb_id = payload.pop("knowledge_base_id")
        await ensure_kb(self.session, kb_id, tenant_id)

        existing = await self.repo.get_by_name(
            tenant_id, kb_id, payload["data_source_id"], payload["name"])
        query_id = str((data or {}).get("verified_query_id") or "").strip()
        if query_id:
            row = await self.repo.get(tenant_id, kb_id, query_id)
            if row is None:
                raise ResourceNotFoundError(message="已验证查询不存在")
            if existing is not None and existing.id != query_id:
                raise _invalid(f"名称已存在：{payload['name']}")
            expected = (data or {}).get("expected_version")
            if expected is None:
                raise _invalid("更新必须带 expected_version")
            updated = await self.repo.update_fields(
                tenant_id, kb_id, query_id, int(expected),
                {**payload, "updated_by": user_id})
            await self.session.commit()
            return updated.to_dict()

        if existing is not None:
            raise _invalid(f"名称已存在：{payload['name']}")
        from ..models.nl2sql_verified_query import Nl2SqlVerifiedQuery
        row = Nl2SqlVerifiedQuery(
            id=uuid.uuid4().hex, tenant_id=tenant_id, knowledge_base_id=kb_id,
            status=STATUS_DRAFT, version=1,
            created_by=user_id, updated_by=user_id, **payload)
        await self.repo.add_row(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row.to_dict()

    async def _engine_and_schema(self, tenant_id: str, kb_id: str,
                                 ds_id: str) -> tuple[str, Optional[dict[str, set[str]]]]:
        from .data_source_service import DataSourceService
        from .nl2sql import schema_read
        from .nl2sql.precompile import build_schema_map

        target = await DataSourceService().query_config(tenant_id, ds_id)
        if target is None:
            raise _invalid("数据源没有可用的只读连接，无法校验模板")
        engine = str((target[1] or {}).get("engine") or "")
        row = await schema_read.load_compiled_row(tenant_id, kb_id, ds_id)
        schema_map = build_schema_map((row or {}).get("entity_types") or []) if row else None
        return engine, schema_map

    async def activate(self, tenant_id: str, kb_id: str, verified_query_id: str,
                       expected_version: int, *, user_id: Optional[str] = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await ensure_kb(self.session, kb_id, tenant_id)
        row = await self.repo.get(tenant_id, kb_id, verified_query_id)
        if row is None:
            raise ResourceNotFoundError(message="已验证查询不存在")
        if row.status == STATUS_ACTIVE:
            return row.to_dict()
        engine, schema_map = await self._engine_and_schema(
            tenant_id, kb_id, row.data_source_id)
        validate_activation(row.sql_template or "", list(row.params or []),
                            engine=engine, schema_map=schema_map)
        updated = await self.repo.set_status(
            tenant_id, kb_id, verified_query_id, int(expected_version),
            STATUS_ACTIVE, actor=user_id or "",
            activated_at=datetime.now(timezone.utc))
        await self.session.commit()
        return updated.to_dict()

    async def retire(self, tenant_id: str, kb_id: str, verified_query_id: str,
                     expected_version: int, *, user_id: Optional[str] = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await ensure_kb(self.session, kb_id, tenant_id)
        row = await self.repo.get(tenant_id, kb_id, verified_query_id)
        if row is None:
            raise ResourceNotFoundError(message="已验证查询不存在")
        if row.status == STATUS_RETIRED:
            return row.to_dict()
        updated = await self.repo.set_status(
            tenant_id, kb_id, verified_query_id, int(expected_version),
            STATUS_RETIRED, actor=user_id or "")
        await self.session.commit()
        return updated.to_dict()

    async def delete(self, tenant_id: str, kb_id: str, verified_query_id: str,
                     expected_version: int) -> bool:
        tenant_id = require_tenant(tenant_id)
        await ensure_kb(self.session, kb_id, tenant_id)
        row = await self.repo.get(tenant_id, kb_id, verified_query_id)
        if row is None:
            raise ResourceNotFoundError(message="已验证查询不存在")
        await self.repo.soft_delete(
            tenant_id, kb_id, verified_query_id, int(expected_version))
        await self.session.commit()
        return True

    async def preview_match(self, tenant_id: str, kb_id: str, data_source_id: str,
                            question: str, *, floor: float = 0.8) -> dict:
        """只读预览：匹配 + 抽参 + 渲染；不执行、不落观测。"""
        tenant_id = require_tenant(tenant_id)
        await ensure_kb(self.session, kb_id, tenant_id)
        rows = await self.repo.list_active(tenant_id, kb_id, data_source_id)
        hit = verified.match_query(
            question, [r.to_dict() for r in rows], floor=floor)
        if hit is None:
            return {"matched": False}
        values = verified.extract_params(question, hit.get("params") or [])
        if values is None:
            return {"matched": False, "reason": "params_not_extracted",
                    "candidates": [{"id": hit["id"], "name": hit["name"]}]}
        try:
            sql = verified.render_template(
                hit.get("sql_template") or "", hit.get("params") or [], values)
        except ValueError as exc:
            return {"matched": False, "reason": f"render_failed: {exc}",
                    "candidates": [{"id": hit["id"], "name": hit["name"]}]}
        return {"matched": True, "id": hit["id"], "name": hit["name"],
                "params": values, "sql": sql}


__all__ = [
    "Nl2SqlVerifiedQueryService",
    "normalize_verified_query_payload",
    "validate_activation",
]
