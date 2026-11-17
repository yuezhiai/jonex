"""检索页候选过滤与空间可用性标注的能力层单测。

服务候选：``DomainServiceService.list(scope="search")`` 只返回可用服务
   （``status='active'`` 且 ``COALESCE(enabled,1)<>0``），且该过滤
   **不受可见性分支影响** —— 租户管理员（``visible is None``）同样看不到停用服务。

空间可用性：``SpaceService.list(scope="search")`` 逐行标注 ``has_enabled_service``，
   判据与 3029 同源（``service_availability.get_space_ids_with_enabled_service``）；
   ``scope="manage"`` 不标注，避免管理视角被检索口径污染。

测试策略：不打真实 DB。
- 谓词：把 SQLAlchemy 条件编译成 SQL 文本断言；
- ``list()``：假 repo 捕获 ``extra_conditions``，假会话只提供 async context manager。

运行：python -m pytest capabilities/knowledge_base/tests/test_search_scope_candidate_filter.py -v
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from capabilities.knowledge_base.models.domain_service import DomainService
from capabilities.knowledge_base.services import domain_service as domain_service_mod
from capabilities.knowledge_base.services import kb_permission_service as kb_perm_mod
from capabilities.knowledge_base.services import service_availability as availability_mod
from capabilities.knowledge_base.services import space_permission_service as space_perm_mod
from capabilities.knowledge_base.services import space_service as space_service_mod
from capabilities.knowledge_base.services.domain_service import DomainServiceService
from capabilities.knowledge_base.services.service_availability import (
    searchable_service_conditions,
)
from capabilities.knowledge_base.services.space_service import SpaceService

TENANT = "tenant_jonex_demo"
USER = "7"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _compile(conditions) -> str:
    """把 ORM 条件编译成单行 SQL 文本（便于子串断言）。"""
    stmt = select(DomainService.id).where(*conditions)
    return " ".join(
        str(stmt.compile(compile_kwargs={"literal_binds": True})).split()
    )


def _async_returning(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn


class _SessionCtx:
    """只提供 ``async with get_db_session() as session`` 的形状。"""

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# A · 服务候选过滤
# ---------------------------------------------------------------------------


def test_searchable_conditions_match_the_gate_predicate():
    """谓词必须同时覆盖 status 与 enabled（enabled 可空，用 COALESCE）。"""
    sql = _compile(searchable_service_conditions(DomainService))
    lowered = sql.lower()
    assert "status = 'active'" in sql
    assert "coalesce(" in lowered
    assert "enabled" in lowered


class _ConditionCapturingRepo:
    captured: list = []

    def __init__(self, session):
        pass

    async def list_all(self, tenant_id, offset, limit, extra_conditions=None):
        _ConditionCapturingRepo.captured = list(extra_conditions or [])
        return []

    async def count(self, tenant_id, extra_conditions=None):
        return 0


def _patch_list_deps(monkeypatch, *, visible):
    monkeypatch.setattr(domain_service_mod, "get_db_session", lambda: _SessionCtx())
    monkeypatch.setattr(
        domain_service_mod, "DomainServiceRepository", _ConditionCapturingRepo
    )
    monkeypatch.setattr(
        space_perm_mod, "get_visible_space_ids", _async_returning(visible)
    )
    # search 分支与 KB 明细裁剪会用到；测试无 KB 行，返回空即可。
    if visible is not None:
        monkeypatch.setattr(
            kb_perm_mod, "get_granted_kb_ids", _async_returning([]), raising=False
        )
        monkeypatch.setattr(
            kb_perm_mod, "get_accessible_kb_ids", _async_returning(None), raising=False
        )


def test_list_search_scope_filters_for_tenant_admin(monkeypatch):
    """租户管理员（visible=None）也不能在检索下拉看到停用服务。

    ⚠️ 这是本改动最容易写错的一处：过滤若放进 ``if visible is not None`` 分支内，
    管理员链路（visible 为 None）就完全不过滤。
    """
    _patch_list_deps(monkeypatch, visible=None)
    asyncio.run(DomainServiceService().list(TENANT, scope="search", user_id=USER))
    sql = _compile(_ConditionCapturingRepo.captured)
    assert "status = 'active'" in sql
    assert "coalesce(" in sql.lower()


def test_list_search_scope_filters_for_space_member(monkeypatch):
    """普通成员链路同样生效（可见性条件 + 可用性条件并存）。"""
    _patch_list_deps(monkeypatch, visible=["space-1"])
    asyncio.run(DomainServiceService().list(TENANT, scope="search", user_id=USER))
    sql = _compile(_ConditionCapturingRepo.captured)
    assert "status = 'active'" in sql
    assert "coalesce(" in sql.lower()


def test_list_manage_scope_keeps_disabled_services(monkeypatch):
    """管理视角必须仍能看到停用服务（否则服务管理页没法重新启用）。"""
    _patch_list_deps(monkeypatch, visible=None)
    asyncio.run(DomainServiceService().list(TENANT, scope="manage", user_id=USER))
    sql = _compile(_ConditionCapturingRepo.captured)
    assert "status = 'active'" not in sql


# ---------------------------------------------------------------------------
# C · 空间可用性标注
# ---------------------------------------------------------------------------


class _FakeSpace:
    def __init__(self, space_id: str, name: str):
        self._row = {"id": space_id, "name": name}

    def to_dict(self):
        return dict(self._row)


class _FakeSpaceRepo:
    def __init__(self, session):
        pass

    async def list_all(self, tenant_id, offset, limit, extra_conditions=None):
        return [_FakeSpace("space-1", "S1"), _FakeSpace("space-2", "S2")]

    async def count(self, tenant_id, extra_conditions=None):
        return 2


def _patch_space_list_deps(monkeypatch, *, enabled_ids, captured):
    monkeypatch.setattr(space_service_mod, "get_db_session", lambda: _SessionCtx())
    monkeypatch.setattr(space_service_mod, "SpaceRepository", _FakeSpaceRepo)
    monkeypatch.setattr(space_service_mod, "is_tenant_admin", _async_returning(False))
    monkeypatch.setattr(space_service_mod, "get_write_space_ids", _async_returning(None))
    # 响应形状与口径无关，can_create_space 必须照常计算；这里 patch 掉是为了让用例
    # 不连真库（多个 asyncio.run 共用模块级连接池会触发跨事件循环错误）。
    monkeypatch.setattr(space_service_mod, "can_create_space", _async_returning(True))

    async def fake_enabled_space_ids(tenant_id, space_ids=None):
        captured["space_ids"] = list(space_ids or [])
        return set(enabled_ids)

    # 实现方可以在模块级或函数内 import，两处都替换，避免断言绑定到导入风格。
    monkeypatch.setattr(
        availability_mod,
        "get_space_ids_with_enabled_service",
        fake_enabled_space_ids,
        raising=False,
    )
    monkeypatch.setattr(
        space_service_mod,
        "get_space_ids_with_enabled_service",
        fake_enabled_space_ids,
        raising=False,
    )


def _space_service_without_lazy_create() -> SpaceService:
    svc = SpaceService()

    async def _noop(*args, **kwargs):
        return None

    svc.ensure_default_space = _noop  # type: ignore[method-assign]
    return svc


def test_space_list_search_scope_marks_has_enabled_service(monkeypatch):
    captured: dict = {}
    _patch_space_list_deps(monkeypatch, enabled_ids={"space-1"}, captured=captured)

    result = asyncio.run(
        _space_service_without_lazy_create().list(TENANT, scope="search", user_id=USER)
    )

    by_id = {row["id"]: row for row in result["items"]}
    assert by_id["space-1"]["has_enabled_service"] is True
    assert by_id["space-2"]["has_enabled_service"] is False
    # 只对本页空间做一次批量判定，不查全租户
    assert captured["space_ids"] == ["space-1", "space-2"]
    # 响应形状不随口径变化：新增的是一个字段，不是换一套响应
    assert result["can_create_space"] is True


def test_space_list_search_scope_marks_false_when_no_service(monkeypatch):
    captured: dict = {}
    _patch_space_list_deps(monkeypatch, enabled_ids=set(), captured=captured)

    result = asyncio.run(
        _space_service_without_lazy_create().list(TENANT, scope="search", user_id=USER)
    )

    assert all(row["has_enabled_service"] is False for row in result["items"])


def test_space_list_manage_scope_does_not_mark_availability(monkeypatch):
    captured: dict = {}
    _patch_space_list_deps(monkeypatch, enabled_ids={"space-1"}, captured=captured)

    result = asyncio.run(
        _space_service_without_lazy_create().list(TENANT, scope="manage", user_id=USER)
    )

    assert "has_enabled_service" not in result["items"][0]
    assert "space_ids" not in captured
