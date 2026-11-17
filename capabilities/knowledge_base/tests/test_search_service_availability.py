"""检索按「领域服务可用性」判定的能力层单测。

覆盖 7 条场景：

1. 服务 active → 放行；
2. 服务 status=inactive → 3028；
3. 服务 enabled=0 → 3028；
4. 服务不存在 / 跨租户 / 已软删 → 3028；
5. 空间下无启用服务 → 3029；
6. 空间下有启用服务 → 放行；
7. 停用服务但请求带其 kb_ids（不带 service_id）→ 走空间剔除，全被剔除拒 3029、
   部分剔除保留可用空间下的 KB。

测试策略：不打真实 DB。把 `capability` 与 `service_availability` 两个模块的
`get_db_session` 都指向同一个假会话，由假会话按 SQL 文本分发：
- `knowledge_base.knowledge_info` → KB → space 映射
- `knowledge_base.services` + `DISTINCT space_id` → 有启用服务的空间集合
- `knowledge_base.services` + `id=:sid` → 单服务可用性
服务行的 `status` / `enabled` / `is_deleted` / `tenant_id` 由假会话按判定谓词过滤，
从而让「inactive」「enabled=0」「跨租户」「已删」各自成为独立可断言的场景。

运行：python -m pytest capabilities/knowledge_base/tests/test_search_service_availability.py -v
"""
from __future__ import annotations

import asyncio

import pytest

from capabilities.knowledge_base import capability as capability_mod
from capabilities.knowledge_base.services import service_availability as availability_mod
from capabilities.knowledge_base.services import kb_permission_service as kb_perm_mod
from capabilities.knowledge_base.services import space_permission_service as space_perm_mod
from jonex_core.capability.models import CapabilityRequest
from jonex_core.common.exceptions import (
    ServiceNotSearchableError,
    SpaceNoEnabledServiceError,
)

TENANT = "tenant_jonex_demo"
OTHER_TENANT = "tenant_other"
USER = "7"
SERVICE = "svc-1"
SPACE = "space-1"
SPACE_2 = "space-2"


# ---------------------------------------------------------------------------
# 假 DB：按 SQL 文本分发 + 按判定谓词过滤服务行
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def _service_row(
    *,
    service_id: str = SERVICE,
    tenant_id: str = TENANT,
    space_id: str = SPACE,
    status: str = "active",
    enabled: int | None = 1,
    is_deleted: int = 0,
) -> dict:
    return {
        "id": service_id,
        "tenant_id": tenant_id,
        "space_id": space_id,
        "status": status,
        "enabled": enabled,
        "is_deleted": is_deleted,
    }


def _is_searchable(row: dict, tenant_id: str) -> bool:
    """与 3028/3029 谓词同口径：租户 + 未删 + active + COALESCE(enabled,1)<>0。"""
    return (
        row["tenant_id"] == tenant_id
        and row["is_deleted"] == 0
        and row["status"] == "active"
        and (row["enabled"] is None or row["enabled"] != 0)
    )


class _FakeSession:
    def __init__(self, kb_rows=(), service_rows=()):
        self.kb_rows = list(kb_rows)
        # service_rows 直接收 `_service_row(...)` 产出的 dict（不再二次展开）。
        self.service_rows = list(service_rows)
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.statements.append(sql)
        if "knowledge_base.knowledge_info" in sql:
            return _FakeResult(self.kb_rows)
        if "knowledge_base.services" in sql:
            tenant_id = params.get("t")
            searchable = [
                r for r in self.service_rows if _is_searchable(r, tenant_id)
            ]
            if "DISTINCT space_id" in sql:
                wanted = params.get("space_ids")
                spaces = {
                    r["space_id"] for r in searchable
                    if wanted is None or r["space_id"] in wanted
                }
                return _FakeResult([(s,) for s in sorted(spaces)])
            wanted_id = params.get("sid")
            return _FakeResult(
                [(r["id"],) for r in searchable if r["id"] == wanted_id]
            )
        raise AssertionError(f"测试未预期的 SQL: {sql}")


class _FakeSessionCM:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    """安装按 SQL 分发的假会话（两个模块共用同一个实例）。"""

    def _install(*, kb_rows=(), service_rows=()):
        session = _FakeSession(kb_rows=kb_rows, service_rows=service_rows)
        monkeypatch.setattr(
            capability_mod, "get_db_session", lambda: _FakeSessionCM(session)
        )
        monkeypatch.setattr(
            availability_mod, "get_db_session", lambda: _FakeSessionCM(session)
        )
        return session

    return _install


@pytest.fixture(autouse=True)
def _allow_active_tenant(monkeypatch):
    """租户 active 闸门依赖真实 DB；本文件只钉住服务/空间可用性口径。"""

    async def _allow(_tenant_id, session=None):
        return None

    monkeypatch.setattr(capability_mod, "require_active_tenant", _allow)


def _request(data: dict, *, action: str = "search", user_id: str | None = None):
    return CapabilityRequest(
        tenant_id=TENANT,
        capability_id="business.knowledge_base.v1",
        payload={"action": action, "data": dict(data)},
        user_id=user_id,
        request_id="req-search-availability",
    )


def _bare_capability(*, action: str = "search"):
    cap = object.__new__(capability_mod.KnowledgeBaseCapability)

    async def _handler(_request, _data):
        return {"results": []}

    async def _noop(*_args, **_kwargs):
        return None

    cap._dispatch = {action: _handler}
    cap._record_search_request = _noop
    return cap


def _run_existing_active(monkeypatch):
    """`_filter_search_kbs` 直接调用的空间集合桩。"""
    monkeypatch.setattr(space_perm_mod, "get_active_space_ids",
                        _async_return({SPACE, SPACE_2}))
    monkeypatch.setattr(space_perm_mod, "get_visible_space_ids",
                        _async_return([SPACE, SPACE_2]))
    monkeypatch.setattr(kb_perm_mod, "get_granted_kb_ids",
                        _async_return([]))


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


# ---------------------------------------------------------------------------
# 1–4：服务闸门（经 execute，对所有调用方生效）
# ---------------------------------------------------------------------------


def test_active_service_passes(fake_db):
    fake_db(service_rows=[_service_row()])
    resp = asyncio.run(_bare_capability().execute(_request({"service_id": SERVICE})))
    assert resp.success is True, resp.message


def test_inactive_service_rejected_3028(fake_db):
    fake_db(service_rows=[_service_row(status="inactive")])
    resp = asyncio.run(_bare_capability().execute(_request({"service_id": SERVICE})))
    assert resp.success is False
    assert resp.code == 3028
    assert resp.details == {"service_id": SERVICE}


def test_disabled_service_rejected_3028(fake_db):
    """enabled=0（前端 /disable 写入口）同样视为停用。"""
    fake_db(service_rows=[_service_row(enabled=0)])
    resp = asyncio.run(_bare_capability().execute(_request({"service_id": SERVICE})))
    assert resp.success is False
    assert resp.code == 3028
    assert resp.details == {"service_id": SERVICE}


@pytest.mark.parametrize(
    "row",
    [
        None,  # 不存在
        _service_row(tenant_id=OTHER_TENANT),  # 跨租户
        _service_row(is_deleted=1),  # 已软删
    ],
    ids=["missing", "cross_tenant", "soft_deleted"],
)
def test_unavailable_service_rejected_3028(fake_db, row):
    fake_db(service_rows=[] if row is None else [row])
    resp = asyncio.run(_bare_capability().execute(_request({"service_id": SERVICE})))
    assert resp.success is False
    assert resp.code == 3028
    assert resp.details == {"service_id": SERVICE}


def test_service_predicate_covers_status_and_enabled(fake_db):
    """谓词是「或」口径：status != active 或 enabled == 0 都不可检索。

    单测层面分别由 test_inactive_service_rejected_3028 /
    test_disabled_service_rejected_3028 走假会话谓词验证；这里再钉住 SQL 文本，
    防止实现被改成只认其中一条。
    """
    session = fake_db(service_rows=[])
    with pytest.raises(ServiceNotSearchableError):
        asyncio.run(availability_mod.require_service_searchable(TENANT, SERVICE))
    sql = "\n".join(session.statements)
    assert "tenant_id=:t" in sql
    assert "is_deleted=0" in sql
    assert "status='active'" in sql
    assert "COALESCE(enabled,1)<>0" in sql


def test_service_gate_applies_to_mcp_like_anonymous_callers(fake_db):
    """MCP 链路 skip_space_check=True，服务闸门仍必须执行（放在块外）。"""
    fake_db(service_rows=[_service_row(status="inactive")])
    # user_id=None ⇒ skip_space_check=True（等价 MCP / 内部链路）
    resp = asyncio.run(_bare_capability().execute(_request({"service_id": SERVICE})))
    assert resp.code == 3028


def test_search_service_action_rejected_3028(fake_db):
    """Gateway REST ``/services/{id}/search`` 直连 ``search_service`` 也要拦。

    ``search_service`` 不在 ``_SEARCH_ACTIONS``（MCP / API Key 链路单独分组），
    服务闸门必须同时覆盖 ``_SERVICE_SEARCH_ACTIONS``，否则这条 REST 路径
    可以绕过停用判定（MCP 本地闸门只覆盖 MCP 客户端）。
    """
    fake_db(service_rows=[_service_row(status="inactive")])
    resp = asyncio.run(
        _bare_capability(action="search_service").execute(
            _request({"service_id": SERVICE, "query": "x"}, action="search_service")
        )
    )
    assert resp.success is False
    assert resp.code == 3028


# ---------------------------------------------------------------------------
# 5–6：空间闸门（经 execute，且在 skip_space_check 块外）
# ---------------------------------------------------------------------------


def test_space_without_enabled_service_rejected_3029(fake_db):
    fake_db(service_rows=[_service_row(status="inactive")])
    resp = asyncio.run(
        _bare_capability().execute(_request({"domain_space_id": SPACE}))
    )
    assert resp.success is False
    assert resp.code == 3029
    assert resp.details == {"space_id": SPACE}


def test_space_with_enabled_service_passes(fake_db):
    fake_db(service_rows=[_service_row()])
    resp = asyncio.run(
        _bare_capability().execute(_request({"domain_space_id": SPACE}))
    )
    assert resp.success is True, resp.message


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en-US", "This domain space has no enabled service. Search is not available."),
        ("zh-CN", "领域空间下没有启用中的服务，无法检索"),
    ],
)
def test_3029_message_follows_locale(fake_db, locale, expected):
    """3029 文案跟随当前 locale。

    能力信封不经全局异常处理器，`execute` 必须自己本地化：原先是
    `CapabilityResponse.error(..., exc.message, ...)`，把类默认的**中文**
    default_message 原样带出去，Gateway 见 message 非空即按「显式原文」跳过翻译。
    """
    from jonex_core.common.i18n import LocaleContext

    fake_db(service_rows=[_service_row(status="inactive")])
    token = LocaleContext.set(locale)
    try:
        resp = asyncio.run(
            _bare_capability().execute(_request({"domain_space_id": SPACE}))
        )
    finally:
        LocaleContext.reset(token)
    assert resp.code == 3029
    assert resp.message == expected


# ---------------------------------------------------------------------------
# 7：只带 kb_ids（不带 service_id）→ 按 KB 所属空间剔除
# ---------------------------------------------------------------------------


def test_kb_ids_in_space_without_enabled_service_rejected_3029(fake_db, monkeypatch):
    """入参 KB 全部落在无启用服务的空间 ⇒ 3029（不是静默回空）。"""
    _run_existing_active(monkeypatch)
    fake_db(
        kb_rows=[("kb1", SPACE)],
        service_rows=[_service_row(status="inactive")],
    )
    with pytest.raises(SpaceNoEnabledServiceError) as exc:
        asyncio.run(_bare_capability()._filter_search_kbs(
            _request({"knowledge_base_ids": ["kb1"]}, user_id=USER),
            {"knowledge_base_ids": ["kb1"]},
        ))
    assert exc.value.code == 3029
    assert exc.value.details == {"space_id": SPACE}


def test_kb_ids_partially_dropped_keeps_enabled_space(fake_db, monkeypatch):
    """多 KB 跨空间：无启用服务空间的 KB 被剔除，可用空间保留。"""
    _run_existing_active(monkeypatch)
    fake_db(
        kb_rows=[("kb1", SPACE), ("kb2", SPACE_2)],
        service_rows=[_service_row(space_id=SPACE_2)],
    )
    data = {"knowledge_base_ids": ["kb1", "kb2"]}
    result = asyncio.run(_bare_capability()._filter_search_kbs(
        _request(data, user_id=USER), data,
    ))
    assert result["knowledge_base_ids"] == ["kb2"]


def test_domain_space_id_restricts_kb_ids_to_space(fake_db, monkeypatch):
    """domain_space_id 分支不再早退，KB 被限制在该空间内后继续过滤。"""
    _run_existing_active(monkeypatch)
    fake_db(
        kb_rows=[("kb1", SPACE), ("kb2", SPACE_2)],
        service_rows=[_service_row(space_id=SPACE)],
    )
    data = {"domain_space_id": SPACE, "knowledge_base_ids": ["kb1", "kb2"]}
    result = asyncio.run(_bare_capability()._filter_search_kbs(
        _request(data, user_id=USER), data,
    ))
    assert result["knowledge_base_ids"] == ["kb1"]


def test_domain_space_id_without_kb_ids_returns_unchanged(fake_db, monkeypatch):
    _run_existing_active(monkeypatch)
    fake_db(service_rows=[_service_row()])
    data = {"domain_space_id": SPACE}
    result = asyncio.run(_bare_capability()._filter_search_kbs(
        _request(data, user_id=USER), data,
    ))
    assert result is data
