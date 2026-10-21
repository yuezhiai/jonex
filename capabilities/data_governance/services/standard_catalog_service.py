"""标准目录读服务：模板标准 + 知识库应用 + 影响分析。

只读：SQL 全在 `standard_read_repository`，本层负责登录/参数/可见性/响应整形。
响应一律 snake_case；`doc_stats` 只透出五个统计键（不泄漏 `knowledge_base_id`）。
"""
from __future__ import annotations

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from ..repository import standard_read_repository as repo
from ..repository.standard_read_repository import REFERENCE_LIMIT
from .permissions import visible_kb_ids
from .standard_scenario_diff import diff_snapshots

#: 影响分析作用域；capability 层直接复用，避免两份真值漂移。
SCOPES = frozenset({"scenario", "object", "attribute"})

#: `doc_stats` 的对外形状；顺序即响应键顺序。
_DOC_STAT_KEYS = (
    "doc_total",
    "no_target",
    "not_applied",
    "applied_current",
    "ontology_ready",
)

#: 影响分析单次最多返回的 KB 数；与仓储 SQL LIMIT 同一来源。
_REFERENCE_LIMIT = REFERENCE_LIMIT

#: 合并时允许 compiled 行补齐 binding 行缺失的字段。
_REFERENCE_FILL_KEYS = (
    "source_version",
    "schema_version",
    "sync_status",
    "schema_mode",
    "source_type",
    "compiled_scenario_id",
)

_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 20
#: 页码上限：`(page-1)*page_size` 会进 SQL OFFSET，必须挡住溢出 bigint 的超大值。
_MAX_PAGE = 1_000_000


# ---------------------------------------------------------------------------
# 入口守卫与参数归一
# ---------------------------------------------------------------------------


def _require_login(user_id: str | None) -> None:
    """匿名（空值 / `anonymous`）不得读标准目录。"""
    if not user_id or user_id == "anonymous":
        raise PermissionDeniedError(message="需要登录后查看标准目录")


def _as_id(value) -> str:
    """非空字符串 id（strip 后）；其他类型/空值一律 400。"""
    if not isinstance(value, str) or not value.strip():
        raise InvalidParameterError(
            message="id 必须是非空字符串", details={"value": value},
        )
    return value.strip()


def _as_scope(value) -> str:
    if not isinstance(value, str) or value not in SCOPES:
        raise InvalidParameterError(
            message="scope_type 必须是 scenario/object/attribute",
            details={"scope_type": value},
        )
    return value


def _bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    return bool(value)


def _parse_int(raw, default: int) -> int:
    if raw is None or raw == "":
        return default
    # bool 是 int 的子类，但 `true` 不是合法页码；float 交给 int() 会被静默截断。
    # 只接受 int 与整数字符串（如 "3"），其余一律 400。
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise InvalidParameterError(
            message="分页参数必须是整数", details={"value": raw},
        )
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise InvalidParameterError(
            message="分页参数必须是整数", details={"value": raw},
        )


def _as_data(data) -> dict:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise InvalidParameterError(message="请求参数必须是对象")
    return data


# ---------------------------------------------------------------------------
# 响应整形
# ---------------------------------------------------------------------------


def _shape_doc_stats(stats: dict | None) -> dict:
    """固定五键；缺省/None 归 0，其余列（含 `knowledge_base_id`）一律丢弃。"""
    stats = stats or {}
    return {key: stats.get(key) or 0 for key in _DOC_STAT_KEYS}


def _doc_stats_map(
    raw: dict[str, dict] | None, kb_ids: list[str],
) -> dict[str, dict]:
    """按请求的 kb_ids 补零：返回的每个 KB 都有完整五键。"""
    raw = raw or {}
    return {kb_id: _shape_doc_stats(raw.get(kb_id)) for kb_id in kb_ids}


#: 响应白名单：新列不会因仓储 SELECT * 的演进自动泄漏到对外 JSON。
_OBJECT_FIELDS = (
    "id", "name", "description", "status", "ontology_code", "aliases",
)
_ATTRIBUTE_FIELDS = (
    "id", "template_object_id", "attr_name", "description", "attr_type",
    "is_primary_key", "is_required", "ontology_code", "constraints_json",
    "sort_order",
)
_KB_ROW_FIELDS = (
    "kb_id", "kb_name", "space_id", "scenario_id", "scenario_name",
    "domain_id", "domain_name", "binding_scenario_id", "compiled_scenario_id",
    "binding_status", "source_type", "source_version", "source_hash",
    "schema_version", "schema_mode", "sync_status", "compiled_at",
    "updated_at",
)
_REFERENCE_ROW_FIELDS = (
    "kb_id", "kb_name", "space_id", "binding_status", "source_type",
    "binding_scenario_id", "compiled_scenario_id",
    "source_version", "schema_version", "sync_status", "schema_mode",
)


def _shape_attribute(row: dict) -> dict:
    item = {key: row.get(key) for key in _ATTRIBUTE_FIELDS}
    item["description"] = item.get("description") or ""
    item["constraints_json"] = item.get("constraints_json") or {}
    item["is_primary_key"] = _bool(item.get("is_primary_key"))
    item["is_required"] = _bool(item.get("is_required"))
    return item


def _shape_object(row: dict, attributes: list[dict]) -> dict:
    out = {key: row.get(key) for key in _OBJECT_FIELDS}
    out["description"] = out.get("description") or ""
    out["aliases"] = out.get("aliases") or []
    out["attributes"] = [_shape_attribute(attribute) for attribute in attributes]
    return out


def _shape_kb_row(row: dict, stats: dict) -> dict:
    out = {key: row.get(key) for key in _KB_ROW_FIELDS}
    if not out.get("binding_status"):
        out["binding_status"] = "none"
    out["doc_stats"] = _shape_doc_stats(stats)
    return out


def _shape_reference_row(row: dict, stats: dict) -> dict:
    out = {key: row.get(key) for key in _REFERENCE_ROW_FIELDS}
    if not out.get("binding_status"):
        out["binding_status"] = "none"
    out["doc_stats"] = _shape_doc_stats(stats)
    return out


def _coverage(scope_type: str) -> str:
    """场景能给出绑定（source_id 全覆盖）；对象/属性只有 compiled 侧。"""
    return "full" if scope_type == "scenario" else "partial"


def _empty_references(scope_type: str, scope_id: str) -> dict:
    return {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "items": [],
        "total": 0,
        "truncated": False,
        "source_id_coverage": _coverage(scope_type),
    }


def _merge_references(bindings: list[dict], compiled: list[dict]) -> list[dict]:
    """binding 优先；compiled 补齐缺失 KB 与 binding 行的空字段。"""
    merged: dict[str, dict] = {}
    for row in bindings:
        kb_id = row.get("kb_id")
        if kb_id:
            merged[kb_id] = dict(row)

    for row in compiled:
        kb_id = row.get("kb_id")
        if not kb_id:
            continue
        target = merged.get(kb_id)
        if target is None:
            merged[kb_id] = dict(row)
            continue
        for key in _REFERENCE_FILL_KEYS:
            if target.get(key) is None and row.get(key) is not None:
                target[key] = row.get(key)
    return list(merged.values())


def _sort_key(row: dict) -> tuple[str, str]:
    return (row.get("kb_name") or "", row.get("kb_id") or "")


async def _page_doc_stats(
    session, tenant_id: str, kb_ids: list[str],
) -> dict[str, dict]:
    """只对当前页的 KB 查一次统计；空列表不查库。"""
    if not kb_ids:
        return {}
    raw = await repo.doc_stats(session, tenant_id, kb_ids)
    return _doc_stats_map(raw, kb_ids)


def _unique_kb_ids(rows: list[dict]) -> list[str]:
    return list(dict.fromkeys(
        row.get("kb_id") for row in rows if row.get("kb_id")
    ))


# ---------------------------------------------------------------------------
# 模板标准
# ---------------------------------------------------------------------------


async def list_domains(tenant_id: str, user_id: str | None) -> dict:
    tenant_id = require_tenant(tenant_id)
    _require_login(user_id)
    async with get_db_session() as session:
        items = await repo.list_domains(session, tenant_id)
    return {"items": items, "total": len(items)}


async def get_domain(
    tenant_id: str, user_id: str | None, domain_id: str,
) -> dict:
    tenant_id = require_tenant(tenant_id)
    _require_login(user_id)
    domain_id = _as_id(domain_id)
    async with get_db_session() as session:
        domain = await repo.get_domain(session, tenant_id, domain_id)
        if domain is None:
            raise ResourceNotFoundError(
                message="领域不存在", details={"domain_id": domain_id},
            )
        scenarios = await repo.list_scenarios(session, tenant_id, domain_id)
    return {"domain": domain, "scenarios": scenarios}


async def get_scenario(
    tenant_id: str, user_id: str | None, scenario_id: str,
) -> dict:
    tenant_id = require_tenant(tenant_id)
    _require_login(user_id)
    scenario_id = _as_id(scenario_id)
    async with get_db_session() as session:
        scenario = await repo.get_scenario(session, tenant_id, scenario_id)
        if scenario is None:
            raise ResourceNotFoundError(
                message="场景不存在", details={"scenario_id": scenario_id},
            )
        objects = await repo.list_objects(session, tenant_id, scenario_id)
        attributes = await repo.list_attributes(session, tenant_id, scenario_id)
        relations = await repo.list_relations(session, tenant_id, scenario_id)
        constraints = await repo.list_constraints(
            session, tenant_id, scenario_id,
        )

    attributes_by_object: dict[str, list[dict]] = {}
    for attribute in attributes:
        object_id = attribute.get("template_object_id")
        if object_id:
            attributes_by_object.setdefault(object_id, []).append(attribute)

    return {
        "scenario": scenario,
        "objects": [
            _shape_object(obj, attributes_by_object.get(obj.get("id"), []))
            for obj in objects
        ],
        "relations": relations,
        "constraints": constraints,
    }


# ---------------------------------------------------------------------------
# 知识库应用
# ---------------------------------------------------------------------------


async def list_kb_schemas(
    tenant_id: str, user_id: str | None, data: dict | None,
) -> dict:
    tenant_id = require_tenant(tenant_id)
    _require_login(user_id)
    data = _as_data(data)

    page = max(1, _parse_int(data.get("page"), 1))
    if page > _MAX_PAGE:
        raise InvalidParameterError(
            message=f"page 不能超过 {_MAX_PAGE}",
            details={"page": page},
        )
    page_size = min(
        _MAX_PAGE_SIZE,
        max(1, _parse_int(data.get("page_size"), _DEFAULT_PAGE_SIZE)),
    )

    raw_kb_id = data.get("kb_id")
    kb_id = _as_id(raw_kb_id) if raw_kb_id not in (None, "") else None
    raw_keyword = data.get("keyword")
    if raw_keyword is not None and not isinstance(raw_keyword, str):
        raise InvalidParameterError(
            message="keyword 必须是字符串", details={"keyword": raw_keyword},
        )
    keyword = raw_keyword.strip() if isinstance(raw_keyword, str) else ""
    keyword = keyword or None

    visible = await visible_kb_ids(tenant_id, user_id)
    if visible is not None and not visible:
        # 无可见资产：不打开会话，也不碰任何 KB 业务表。
        return {
            "items": [], "total": 0, "page": page, "page_size": page_size,
        }

    async with get_db_session() as session:
        rows = await repo.list_kb_schemas(
            session, tenant_id,
            visible_kb_ids=visible, kb_id=kb_id, keyword=keyword,
            limit=page_size, offset=(page - 1) * page_size,
        )
        total = await repo.count_kb_schemas(
            session, tenant_id,
            visible_kb_ids=visible, kb_id=kb_id, keyword=keyword,
        )
        stats = await _page_doc_stats(session, tenant_id, _unique_kb_ids(rows))

    return {
        "items": [_shape_kb_row(row, stats.get(row.get("kb_id"), {})) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# 影响分析
# ---------------------------------------------------------------------------


async def get_references(
    tenant_id: str, user_id: str | None, data: dict | None,
) -> dict:
    tenant_id = require_tenant(tenant_id)
    _require_login(user_id)
    data = _as_data(data)

    scope_type = _as_scope(data.get("scope_type"))
    scope_id = _as_id(data.get("scope_id"))
    visible = await visible_kb_ids(tenant_id, user_id)

    async with get_db_session() as session:
        # 存在性校验必须先于 visible=[] 短路：不能把「不存在」伪装成「空影响」。
        if scope_type == "scenario":
            scope = await repo.get_scenario(session, tenant_id, scope_id)
            missing_message = "场景不存在"
        elif scope_type == "object":
            scope = await repo.get_object_scope(session, tenant_id, scope_id)
            missing_message = "对象不存在"
        else:
            scope = await repo.get_attribute_scope(session, tenant_id, scope_id)
            missing_message = "属性不存在"
        if scope is None:
            raise ResourceNotFoundError(
                message=missing_message,
                details={"scope_type": scope_type, "scope_id": scope_id},
            )

        if visible is not None and not visible:
            # scope 已确认存在；无可见 KB ⇒ 空结果，不查 KB 业务表。
            return _empty_references(scope_type, scope_id)

        if scope_type == "scenario":
            # 已知取舍：场景影响先全量拉取 bindings + compiled，再在内存合并/截断。
            # 演示租户的生效绑定数很小，本轮先保持全量；大租户需要改为
            # UNION 分页（COUNT(*) + LIMIT/OFFSET），否则成本随绑定数线性增长。
            bindings = await repo.list_reference_bindings(
                session, tenant_id, scope_id, visible_kb_ids=visible,
            )
            compiled = await repo.list_reference_compiled_by_scenario(
                session, tenant_id, scope_id, visible_kb_ids=visible,
            )
            total_override = None
        elif scope_type == "object":
            # total 必须来自 COUNT：列表侧 LIMIT 200 不能代表总量。
            total_override = await repo.count_reference_compiled_by_object(
                session, tenant_id, scope_id, visible_kb_ids=visible,
            )
            compiled = await repo.list_reference_compiled_by_object(
                session, tenant_id, scope_id, visible_kb_ids=visible,
            )
            bindings = []
        else:
            total_override = await repo.count_reference_compiled_by_attribute(
                session, tenant_id, scope_id, visible_kb_ids=visible,
            )
            compiled = await repo.list_reference_compiled_by_attribute(
                session, tenant_id, scope_id, visible_kb_ids=visible,
            )
            bindings = []

        merged = _merge_references(bindings, compiled)
        merged.sort(key=_sort_key)
        total = total_override if total_override is not None else len(merged)
        page_rows = merged[:_REFERENCE_LIMIT]
        stats = await _page_doc_stats(
            session, tenant_id, _unique_kb_ids(page_rows),
        )

    return {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "items": [
            _shape_reference_row(row, stats.get(row.get("kb_id"), {}))
            for row in page_rows
        ],
        "total": total,
        "truncated": total > _REFERENCE_LIMIT,
        "source_id_coverage": _coverage(scope_type),
    }


# ---------------------------------------------------------------------------
# 发布版本历史 + diff（第 6 项第二增量）
# ---------------------------------------------------------------------------


def _parse_version(value, field: str) -> int:
    version = _parse_int(value, 0)
    if version < 1:
        raise InvalidParameterError(
            message=f"{field} 必须是正整数",
            details={field: value},
        )
    return version


async def list_versions(
    tenant_id: str, user_id: str | None, scenario_id: str,
) -> dict:
    """场景发布版本列表（不含 snapshot）。"""
    tenant_id = require_tenant(tenant_id)
    _require_login(user_id)
    scenario_id = _as_id(scenario_id)
    async with get_db_session() as session:
        scenario = await repo.get_scenario(session, tenant_id, scenario_id)
        if scenario is None:
            raise ResourceNotFoundError(
                message="场景不存在", details={"scenario_id": scenario_id},
            )
        items = await repo.list_scenario_versions(session, tenant_id, scenario_id)
    return {"items": items, "total": len(items)}


async def get_version(
    tenant_id: str, user_id: str | None, scenario_id: str, version,
) -> dict:
    """单个发布版本（含完整 snapshot）。"""
    tenant_id = require_tenant(tenant_id)
    _require_login(user_id)
    scenario_id = _as_id(scenario_id)
    version_no = _parse_version(version, "version")
    async with get_db_session() as session:
        scenario = await repo.get_scenario(session, tenant_id, scenario_id)
        if scenario is None:
            raise ResourceNotFoundError(
                message="场景不存在", details={"scenario_id": scenario_id},
            )
        row = await repo.get_scenario_version_row(
            session, tenant_id, scenario_id, version_no,
        )
    if row is None:
        raise ResourceNotFoundError(
            message="模板版本不存在",
            details={"scenario_id": scenario_id, "version": version_no},
        )
    return row


async def diff_versions(
    tenant_id: str,
    user_id: str | None,
    scenario_id: str,
    from_version,
    to_version,
) -> dict:
    """对比两个发布版本的快照差异。"""
    tenant_id = require_tenant(tenant_id)
    _require_login(user_id)
    scenario_id = _as_id(scenario_id)
    from_no = _parse_version(from_version, "from_version")
    to_no = _parse_version(to_version, "to_version")

    async with get_db_session() as session:
        scenario = await repo.get_scenario(session, tenant_id, scenario_id)
        if scenario is None:
            raise ResourceNotFoundError(
                message="场景不存在", details={"scenario_id": scenario_id},
            )
        before = await repo.get_scenario_version_row(
            session, tenant_id, scenario_id, from_no,
        )
        after = await repo.get_scenario_version_row(
            session, tenant_id, scenario_id, to_no,
        )
    if before is None:
        raise ResourceNotFoundError(
            message="模板版本不存在",
            details={"scenario_id": scenario_id, "version": from_no},
        )
    if after is None:
        raise ResourceNotFoundError(
            message="模板版本不存在",
            details={"scenario_id": scenario_id, "version": to_no},
        )
    return diff_snapshots(
        before.get("snapshot") or {},
        after.get("snapshot") or {},
        from_version=from_no,
        to_version=to_no,
    )


# ---------------------------------------------------------------------------
# 落标校验：KB compiled schema 对照最新发布标准
# ---------------------------------------------------------------------------


async def get_compliance(
    tenant_id: str, user_id: str | None, scenario_id: str,
) -> dict:
    """场景下各 KB 的落标状态与结构差异。"""
    from .standard_compliance import evaluate_compliance, summarize

    tenant_id = require_tenant(tenant_id)
    _require_login(user_id)
    scenario_id = _as_id(scenario_id)
    visible = await visible_kb_ids(tenant_id, user_id)

    async with get_db_session() as session:
        scenario = await repo.get_scenario(session, tenant_id, scenario_id)
        if scenario is None:
            raise ResourceNotFoundError(
                message="场景不存在", details={"scenario_id": scenario_id},
            )
        latest = await repo.get_latest_scenario_version(
            session, tenant_id, scenario_id,
        )
        rows = await repo.list_compiled_for_scenario(
            session, tenant_id, scenario_id, visible,
        )
        stats = await repo.doc_stats(
            session, tenant_id, [row["kb_id"] for row in rows],
        )

    snapshot = (latest or {}).get("snapshot") or {}
    latest_version = (latest or {}).get("version")

    items = []
    for row in rows:
        result = evaluate_compliance(
            snapshot, row, latest_version=latest_version,
        )
        kb_id = row["kb_id"]
        items.append({
            "kb_id": kb_id,
            "kb_name": row.get("kb_name"),
            "source_type": row.get("source_type"),
            "source_version": row.get("source_version"),
            "schema_version": row.get("schema_version"),
            "sync_status": row.get("sync_status"),
            "schema_mode": row.get("schema_mode"),
            "compiled_at": row.get("compiled_at"),
            "doc_stats": stats.get(kb_id) or {},
            **result,
        })

    return {
        "scenario_id": scenario_id,
        "latest_published_version": latest_version,
        "latest_structure_hash": (latest or {}).get("structure_hash"),
        "items": items,
        "summary": summarize(items),
    }
