#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Jonex Wiki 列式浏览 MCP 工具测试（list_wiki_topics / list_wiki_pages /
expand_wiki_links / list_wiki_claims）。

只 patch 三个既有接缝（require_mcp_auth / call_gateway / touch_service_call_async），
断言 payload 与返回形状，不依赖真实网络 / 数据库。
"""
import inspect
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult

_project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from mcp_server.tests.test_tools_ontology_multi_kb import (  # noqa: E402
    _patched_env,
    _tool_error,
)

# app.py 模块级 assert_secure_jwt_secret 要求 JWT_SECRET 非空；仅在 import
# 期间临时设置，import 完立即还原，避免影响其他用例的环境基线。
_JWT_SECRET_ADDED = not os.environ.get("JWT_SECRET")
if _JWT_SECRET_ADDED:
    os.environ["JWT_SECRET"] = "wiki_browse_schema_secret_9d3f"
import app as _mcp_app  # noqa: E402
if _JWT_SECRET_ADDED:
    os.environ.pop("JWT_SECRET", None)

from mcp_server.tools import McpToolError  # noqa: E402


# ============================================================================
# helpers
# ============================================================================


def _serve_wiki(kb_ids, kb_types, results):
    """按 action / knowledge_base_id 分派；``get_service`` 带 kb_ids + kb_types。

    ``kb_types=None`` 表示响应里故意不提供该字段；也可以传字符串等非法形状。
    ``results`` 的键可为 ``(action, kb_id)`` 或 ``kb_id``；值是 dict 或异常实例。
    """

    async def _side_effect(action, tenant_id, mcp_key_id, data=None, **kwargs):
        if action == "get_service":
            detail = {"kb_ids": list(kb_ids)}
            if kb_types is not None:
                detail["kb_types"] = (
                    list(kb_types) if isinstance(kb_types, list) else kb_types
                )
            return detail
        kb_id = (data or {}).get("knowledge_base_id")
        outcome = results.get((action, kb_id))
        if outcome is None:
            outcome = results.get(kb_id)
        if outcome is None:
            raise AssertionError(f"unexpected upstream call: {action} {kb_id}")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return AsyncMock(side_effect=_side_effect)


def _actions(upstream):
    return [call.args[0] for call in upstream.call_args_list]


def _target_payloads(upstream):
    return [
        call.args[3]
        for call in upstream.call_args_list
        if call.args[0] != "get_service"
    ]


def _page(path, title="", type_="concept"):
    return {
        "path": path,
        "title": title or path,
        "type": type_,
        "aliases": [],
        "description": "",
    }


def _link(source, target, kind="navigation", why_visit=""):
    return {
        "source": source,
        "target": target,
        "kind": kind,
        "why_visit": why_visit,
    }


def _graph(pages, links, *, truncated=False, reasons=None, degraded=False):
    return {
        "pages": pages,
        "links": links,
        "page_total": len(pages),
        "link_total": len(links),
        "truncated": truncated,
        "truncated_reason": list(reasons or []),
        "degraded": degraded,
    }


# ============================================================================
# KB 三态 / 类型校验
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"knowledge_base_id": "kb-1", "knowledge_base_ids": ["kb-1"]},
        {"knowledge_base_ids": []},
        {"knowledge_base_ids": "kb-1"},
        {"knowledge_base_ids": [f"kb-{i}" for i in range(21)]},
    ],
)
async def test_wiki_kb_selection_invalid_tri_state(kwargs):
    import mcp_server.tools as tools

    upstream = AsyncMock()
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics("svc-1", **kwargs)

    assert _tool_error(result).value.code == -32602
    upstream.assert_not_called()


@pytest.mark.asyncio
async def test_wiki_explicit_kb_not_in_service_is_32002():
    import mcp_server.tools as tools

    upstream = _serve_wiki(["kb-1"], ["jonex_wiki"], {})
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics(
            "svc-1", knowledge_base_id="kb-2"
        )

    assert _tool_error(result).value.code == -32002
    assert _actions(upstream) == ["get_service"]


@pytest.mark.asyncio
async def test_wiki_explicit_kb_wrong_type_is_32602():
    import mcp_server.tools as tools

    upstream = _serve_wiki(["kb-1"], ["lightrag"], {})
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics(
            "svc-1", knowledge_base_id="kb-1"
        )

    exc = _tool_error(result).value
    assert exc.code == -32602
    assert "Jonex Wiki" in exc.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kb_types",
    [
        None,  # 字段缺失
        ["jonex_wiki"],  # 与 kb_ids 长度不一致
        "jonex_wiki",  # 非 list
    ],
)
async def test_wiki_kb_types_missing_or_malformed_is_32602(kb_types):
    import mcp_server.tools as tools

    kb_ids = ["kb-1"] if kb_types != ["jonex_wiki"] else ["kb-1", "kb-2"]
    upstream = _serve_wiki(kb_ids, kb_types, {})
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics("svc-1")

    exc = _tool_error(result).value
    assert exc.code == -32602
    assert "无法判定知识库类型" in exc.message


# ============================================================================
# 目录 / 页面 / 多库聚合
# ============================================================================


def _outline_payload(kb_id, topic_id="t1"):
    return {
        "knowledge_base_id": kb_id,
        "page_count": 2,
        "pages": [
            _page("canteen/lunch-dishes.md", "午餐菜品", "table"),
            _page("canteen/dish-category.md", "菜品分类", "concept"),
        ],
        "outline": {
            "version": 1,
            "overview": "",
            "topics": [
                {
                    "id": topic_id,
                    "kind": "topic",
                    "title": "餐饮服务",
                    "description": "",
                    "order": 1,
                    "entries": [
                        {"path": "canteen/lunch-dishes.md", "title": "午餐菜品"}
                    ],
                    "children": [],
                }
            ],
            "page_count": 2,
            "covered_count": 1,
        },
        "outline_version": 3,
        "stale": True,
    }


@pytest.mark.asyncio
async def test_wiki_topics_single_projection():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1"],
        ["jonex_wiki"],
        {("get_wiki_outline", "kb-1"): _outline_payload("kb-1")},
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics("svc-1")

    assert _target_payloads(upstream) == [{"knowledge_base_id": "kb-1"}]
    assert result["knowledge_base_id"] == "kb-1"
    assert result["topics"][0]["id"] == "t1"
    assert result["page_count"] == 2
    assert result["outline_available"] is True
    assert result["outline_version"] == 3
    assert result["stale"] is True
    assert result["wiki_degraded"] is False


@pytest.mark.asyncio
async def test_wiki_topics_multi_groups():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1", "kb-2"],
        ["jonex_wiki", "jonex_wiki"],
        {
            ("get_wiki_outline", "kb-1"): _outline_payload("kb-1"),
            ("get_wiki_outline", "kb-2"): _outline_payload("kb-2", topic_id="t2"),
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics(
            "svc-1", knowledge_base_ids=["kb-1", "kb-2"]
        )

    assert set(result.keys()) == {"groups"}
    assert [g["knowledge_base_id"] for g in result["groups"]] == ["kb-1", "kb-2"]
    assert result["groups"][1]["topics"][0]["id"] == "t2"


@pytest.mark.asyncio
async def test_wiki_multi_groups_single_failure_isolated():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1", "kb-2"],
        ["jonex_wiki", "jonex_wiki"],
        {
            ("get_wiki_outline", "kb-1"): _outline_payload("kb-1"),
            ("get_wiki_outline", "kb-2"): McpToolError(-32002, "认证失败或权限不足"),
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics(
            "svc-1", knowledge_base_ids=["kb-1", "kb-2"]
        )

    ok_group, bad_group = result["groups"]
    assert ok_group["knowledge_base_id"] == "kb-1"
    assert ok_group["topics"]
    assert set(bad_group.keys()) == {"knowledge_base_id", "errors"}
    assert bad_group["errors"][0]["code"] == -32002


@pytest.mark.asyncio
async def test_wiki_multi_all_failed_normalizes_to_32002():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1", "kb-2"],
        ["jonex_wiki", "jonex_wiki"],
        {
            ("get_wiki_outline", "kb-1"): McpToolError(-32002, "认证失败或权限不足"),
            ("get_wiki_outline", "kb-2"): McpToolError(-32002, "认证失败或权限不足"),
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics(
            "svc-1", knowledge_base_ids=["kb-1", "kb-2"]
        )

    assert _tool_error(result).value.code == -32002


@pytest.mark.asyncio
async def test_wiki_multi_all_rate_limited_normalizes_to_32001():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1", "kb-2"],
        ["jonex_wiki", "jonex_wiki"],
        {
            ("get_wiki_outline", "kb-1"): McpToolError(
                -32001, "Rate limit exceeded.", {"retry_after": 7}
            ),
            ("get_wiki_outline", "kb-2"): McpToolError(
                -32001, "Rate limit exceeded.", {"retry_after": 9}
            ),
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics(
            "svc-1", knowledge_base_ids=["kb-1", "kb-2"]
        )

    exc = _tool_error(result).value
    assert exc.code == -32001
    assert exc.data["retry_after"] == 9


@pytest.mark.asyncio
async def test_wiki_multi_all_invalid_never_normalizes_to_32602():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1", "kb-2"],
        ["jonex_wiki", "jonex_wiki"],
        {
            ("get_wiki_outline", "kb-1"): McpToolError(-32602, "参数有误"),
            ("get_wiki_outline", "kb-2"): McpToolError(-32602, "参数有误"),
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics(
            "svc-1", knowledge_base_ids=["kb-1", "kb-2"]
        )

    assert _tool_error(result).value.code == -32000


@pytest.mark.asyncio
async def test_wiki_pages_topic_and_type_filter_and_paging():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1"],
        ["jonex_wiki"],
        {("get_wiki_outline", "kb-1"): _outline_payload("kb-1")},
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_pages(
            "svc-1", topic="t1", type="table", page=1, page_size=20
        )

    assert result["items"] == [
        {
            "path": "canteen/lunch-dishes.md",
            "title": "午餐菜品",
            "type": "table",
            "category": "",
            "description": "",
            "aliases": [],
            "tags": [],
            "why_visit": "",
            "reference_count": 0,
        }
    ]
    assert result["total"] == 1
    assert result["page"] == 1
    assert result["page_size"] == 20
    assert result["outline_available"] is True


@pytest.mark.asyncio
async def test_wiki_pages_topic_missing_is_32602():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1"],
        ["jonex_wiki"],
        {("get_wiki_outline", "kb-1"): _outline_payload("kb-1")},
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_pages("svc-1", topic="不存在")

    assert _tool_error(result).value.code == -32602


@pytest.mark.asyncio
async def test_wiki_pages_multi_has_no_top_level_items():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1", "kb-2"],
        ["jonex_wiki", "jonex_wiki"],
        {
            ("get_wiki_outline", "kb-1"): _outline_payload("kb-1"),
            ("get_wiki_outline", "kb-2"): _outline_payload("kb-2", topic_id="t2"),
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_pages(
            "svc-1", knowledge_base_ids=["kb-1", "kb-2"]
        )

    assert set(result.keys()) == {"total", "groups"}
    assert result["total"] == 4
    for group in result["groups"]:
        assert "items" in group and "page" in group and "page_size" in group
    assert "items" not in result


# ============================================================================
# BFS 多跳
# ============================================================================


def _abc_graph():
    return _graph(
        [_page("a.md", "A"), _page("b.md", "B"), _page("c.md", "C")],
        [_link("a.md", "b.md", why_visit="去 B"), _link("b.md", "c.md")],
    )


@pytest.mark.asyncio
async def test_wiki_expand_depth_2_hops():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1"],
        ["jonex_wiki"],
        {("get_wiki_page_graph", "kb-1"): _abc_graph()},
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_wiki_links(
            "svc-1", path="a.md", depth=2, limit=50
        )

    assert _target_payloads(upstream) == [{"knowledge_base_id": "kb-1"}]
    assert result["start_found"] is True
    assert [(p["path"], p["hop"]) for p in result["pages"]] == [
        ("a.md", 0),
        ("b.md", 1),
        ("c.md", 2),
    ]
    assert [(e["source"], e["target"], e["hop"]) for e in result["links"]] == [
        ("a.md", "b.md", 1),
        ("b.md", "c.md", 2),
    ]
    assert result["returned"] == 2
    assert result["truncated"] is False
    # a.md 的链入为空；b.md 的链入是 a.md。
    page_b = next(p for p in result["pages"] if p["path"] == "b.md")
    assert page_b["backlink_total"] == 1
    assert page_b["backlinks"][0]["path"] == "a.md"


@pytest.mark.asyncio
async def test_wiki_expand_depth_1_excludes_start():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1"],
        ["jonex_wiki"],
        {("get_wiki_page_graph", "kb-1"): _abc_graph()},
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_wiki_links(
            "svc-1", path="a.md", depth=1, limit=50
        )

    assert [p["path"] for p in result["pages"]] == ["b.md"]
    assert all(p["hop"] == 1 for p in result["pages"])
    assert [(e["source"], e["target"], e["hop"]) for e in result["links"]] == [
        ("a.md", "b.md", 1)
    ]


@pytest.mark.asyncio
async def test_wiki_expand_limit_truncation_has_no_isolated_pages():
    import mcp_server.tools as tools

    graph = _graph(
        [
            _page("a.md", "A"),
            _page("b.md", "B"),
            _page("c.md", "C"),
            _page("d.md", "D"),
            _page("e.md", "E"),
        ],
        [
            _link("a.md", "b.md"),
            _link("a.md", "c.md"),
            _link("a.md", "d.md"),
            _link("a.md", "e.md"),
        ],
    )
    upstream = _serve_wiki(
        ["kb-1"], ["jonex_wiki"], {("get_wiki_page_graph", "kb-1"): graph}
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_wiki_links(
            "svc-1", path="a.md", depth=2, limit=3
        )

    assert result["returned"] == 3
    assert result["truncated"] is True
    assert "limit" in result["truncated_reason"]
    page_paths = {p["path"] for p in result["pages"]}
    assert "e.md" not in page_paths
    connected = set()
    for edge in result["links"]:
        connected.add(edge["source"])
        connected.add(edge["target"])
    # 起点例外；其余返回页都必须连在一条已返回边上。
    assert page_paths - {"a.md"} <= connected


@pytest.mark.asyncio
async def test_wiki_expand_cycle_does_not_duplicate_pages_or_edges():
    import mcp_server.tools as tools

    graph = _graph(
        [_page("a.md", "A"), _page("b.md", "B"), _page("c.md", "C")],
        [
            _link("a.md", "b.md"),
            _link("b.md", "c.md"),
            _link("c.md", "a.md"),
        ],
    )
    upstream = _serve_wiki(
        ["kb-1"], ["jonex_wiki"], {("get_wiki_page_graph", "kb-1"): graph}
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_wiki_links(
            "svc-1", path="a.md", depth=3, limit=50
        )

    paths = [p["path"] for p in result["pages"]]
    assert sorted(paths) == ["a.md", "b.md", "c.md"]
    assert len(paths) == len(set(paths))
    edges = [(e["source"], e["target"]) for e in result["links"]]
    assert len(edges) == len(set(edges))


@pytest.mark.asyncio
async def test_wiki_expand_backlinks_truncated_at_30():
    import mcp_server.tools as tools

    sources = [f"s{i:02d}.md" for i in range(34)]
    pages = [_page("a.md", "A"), _page("hub.md", "Hub")]
    pages += [_page(path, path) for path in sources]
    links = [_link("a.md", "hub.md")] + [_link(path, "hub.md") for path in sources]
    graph = _graph(pages, links)
    upstream = _serve_wiki(
        ["kb-1"], ["jonex_wiki"], {("get_wiki_page_graph", "kb-1"): graph}
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_wiki_links(
            "svc-1", path="a.md", depth=1, limit=50
        )

    hub = next(p for p in result["pages"] if p["path"] == "hub.md")
    assert hub["backlink_total"] == 35
    assert len(hub["backlinks"]) == 30
    assert hub["backlinks_truncated"] is True


@pytest.mark.asyncio
async def test_wiki_expand_legacy_path_prefix_and_missing_start():
    import mcp_server.tools as tools

    graph = _graph(
        [_page("tables/b.md", "B"), _page("sources/s.md", "S")],
        [_link("tables/b.md", "sources/s.md")],
    )
    upstream = _serve_wiki(
        ["kb-1"], ["jonex_wiki"], {("get_wiki_page_graph", "kb-1"): graph}
    )
    with _patched_env(upstream=upstream):
        legacy = await tools.expand_wiki_links(
            "svc-1", path="entities/b", depth=1
        )
        missing = await tools.expand_wiki_links(
            "svc-1", path="nope.md", depth=1
        )

    assert legacy["start_found"] is True
    assert [p["path"] for p in legacy["pages"]] == ["sources/s.md"]
    assert missing["start_found"] is False
    assert missing["pages"] == [] and missing["links"] == []


# ============================================================================
# claims
# ============================================================================


@pytest.mark.asyncio
async def test_wiki_claims_limit_and_truncation():
    import mcp_server.tools as tools

    items = [
        {
            "claim_id": f"c{i}",
            "page_path": "a.md",
            "page_title": "A",
            "text": "x" * 400,
            "excerpt": "",
            "chunk_id": f"chunk-{i}",
        }
        for i in range(500)
    ]
    upstream = _serve_wiki(
        ["kb-1"],
        ["jonex_wiki"],
        {
            ("get_wiki_claims", "kb-1"): {
                "items": items,
                "total": 812,
                "knowledge_base_id": "kb-1",
                "path": "a.md",
                "document_id": "",
            }
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_claims(
            "svc-1", path="a.md", limit=500
        )

    assert _target_payloads(upstream) == [
        {"knowledge_base_id": "kb-1", "limit": 500, "path": "a.md"}
    ]
    assert result["returned"] == 500
    assert result["total"] == 812
    assert result["truncated"] is True
    assert result["text_limit"] == 400
    assert result["wiki_degraded"] is False


@pytest.mark.asyncio
async def test_wiki_claims_upstream_internal_error_degrades():
    """引擎不可用应与 topics/pages 同口径降级，而不是把 -32000 抛给调用方。"""
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1"],
        ["jonex_wiki"],
        {
            ("get_wiki_claims", "kb-1"): McpToolError(
                -32000, "知识库服务暂不可用"
            )
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_claims("svc-1", path="a.md", limit=50)

    assert result["items"] == []
    assert result["total"] == 0
    assert result["returned"] == 0
    assert result["wiki_degraded"] is True


@pytest.mark.asyncio
async def test_wiki_claims_multi_internal_error_degrades_group():
    """多库扇出里单库 -32000 应降级该组，不拖垮另一组。"""
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1", "kb-2"],
        ["jonex_wiki", "jonex_wiki"],
        {
            ("get_wiki_claims", "kb-1"): {
                "items": [{"claim_id": "c1", "page_path": "a.md", "text": "x"}],
                "total": 1,
                "knowledge_base_id": "kb-1",
                "path": "a.md",
                "document_id": "",
            },
            ("get_wiki_claims", "kb-2"): McpToolError(
                -32000, "知识库服务暂不可用"
            ),
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_claims(
            "svc-1", knowledge_base_ids=["kb-1", "kb-2"], path="a.md"
        )

    by_kb = {g["knowledge_base_id"]: g for g in result["groups"]}
    assert by_kb["kb-1"]["wiki_degraded"] is False
    assert by_kb["kb-1"]["returned"] == 1
    assert by_kb["kb-2"]["wiki_degraded"] is True
    assert by_kb["kb-2"]["items"] == []
    assert by_kb["kb-2"]["total"] == 0


@pytest.mark.asyncio
async def test_wiki_claims_requires_path_or_document_id():
    import mcp_server.tools as tools

    upstream = AsyncMock()
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_claims("svc-1")

    assert _tool_error(result).value.code == -32602
    upstream.assert_not_called()


# ============================================================================
# 开关 / 降级 / 参数严格校验
# ============================================================================


@pytest.mark.asyncio
async def test_wiki_switch_off_returns_empty_without_upstream(monkeypatch):
    import mcp_server.tools as tools

    monkeypatch.setattr(tools, "_WIKI_BROWSE_ENABLED", False)
    upstream = _serve_wiki(["kb-1"], ["jonex_wiki"], {})
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_pages("svc-1")

    assert _actions(upstream) == ["get_service"]
    assert result["items"] == []
    assert result["total"] == 0
    assert result["wiki_browse_disabled"] is True
    assert result["wiki_degraded"] is False


@pytest.mark.asyncio
async def test_wiki_switch_off_multi_returns_groups(monkeypatch):
    import mcp_server.tools as tools

    monkeypatch.setattr(tools, "_WIKI_BROWSE_ENABLED", False)
    upstream = _serve_wiki(
        ["kb-1", "kb-2"], ["jonex_wiki", "jonex_wiki"], {}
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_wiki_links(
            "svc-1",
            knowledge_base_ids=["kb-1", "kb-2"],
            path="a.md",
        )

    assert _actions(upstream) == ["get_service"]
    assert set(result.keys()) == {"groups"}
    assert all(g["wiki_browse_disabled"] is True for g in result["groups"])


@pytest.mark.asyncio
async def test_wiki_topics_upstream_internal_error_degrades():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1"],
        ["jonex_wiki"],
        {
            ("get_wiki_outline", "kb-1"): McpToolError(
                -32000, "知识库服务暂不可用"
            )
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_topics("svc-1")

    assert result["topics"] == []
    assert result["outline_available"] is False
    assert result["wiki_degraded"] is True
    assert result["page_count"] == 0


@pytest.mark.asyncio
async def test_wiki_expand_upstream_degraded_body():
    import mcp_server.tools as tools

    upstream = _serve_wiki(
        ["kb-1"],
        ["jonex_wiki"],
        {("get_wiki_page_graph", "kb-1"): _graph([], [], degraded=True)},
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_wiki_links("svc-1", path="a.md")

    assert result["pages"] == []
    assert result["links"] == []
    assert result["wiki_degraded"] is True
    assert result["start_found"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"page": "2"},
        {"page": True},
        {"page": 2.0},
        {"page": 0},
        {"page_size": "20"},
        {"page_size": False},
        {"page_size": 101},
    ],
)
async def test_wiki_pages_strict_int_params(kwargs):
    import mcp_server.tools as tools

    upstream = AsyncMock()
    with _patched_env(upstream=upstream):
        result = await tools.list_wiki_pages("svc-1", **kwargs)

    assert _tool_error(result).value.code == -32602
    upstream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"depth": "2"},
        {"depth": True},
        {"depth": 2.0},
        {"depth": 0},
        {"depth": 4},
        {"limit": "50"},
        {"limit": False},
        {"limit": 201},
    ],
)
async def test_wiki_expand_strict_int_params(kwargs):
    import mcp_server.tools as tools

    upstream = AsyncMock()
    with _patched_env(upstream=upstream):
        result = await tools.expand_wiki_links("svc-1", path="a.md", **kwargs)

    assert _tool_error(result).value.code == -32602
    upstream.assert_not_called()


# ============================================================================
# app.py / tools.py 签名与注册一致性
# ============================================================================


_WIKI_TOOL_NAMES = (
    "list_wiki_topics",
    "list_wiki_pages",
    "expand_wiki_links",
    "list_wiki_claims",
)


def _ast_signature(fn):
    positional = fn.args.args
    defaults = fn.args.defaults
    import ast

    mapping = {}
    for arg, default in zip(positional[-len(defaults):], defaults):
        mapping[arg.arg] = ast.literal_eval(default)
    return [arg.arg for arg in positional], mapping


def test_wiki_app_and_tools_signatures_match():
    import ast
    from pathlib import Path

    app_path = Path(__file__).resolve().parents[2] / "mcp_server" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    app_functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }

    import mcp_server.tools as tools

    for name in _WIKI_TOOL_NAMES:
        app_args, app_defaults = _ast_signature(app_functions[name])
        signature = inspect.signature(getattr(tools, name))
        tools_args = list(signature.parameters)
        tools_defaults = {
            param_name: param.default
            for param_name, param in signature.parameters.items()
            if param.default is not inspect.Parameter.empty
        }
        assert app_args == tools_args, name
        assert app_defaults == tools_defaults, name
        # 装饰器注入的工具名必须与 app.py 注册名一致。
        assert getattr(tools, name).__mcp_tool_name__ == name


def test_wiki_depth_schema_stays_integer_and_keeps_raw_value():
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    meta = func_metadata(_mcp_app.expand_wiki_links)
    schema = meta.arg_model.model_json_schema()["properties"]
    assert schema["depth"]["type"] == "integer"
    assert schema["limit"]["type"] == "integer"

    parsed = meta.arg_model.model_validate(
        {"service_id": "svc-1", "path": "a.md", "depth": "2", "limit": True}
    )
    assert parsed.depth == "2"
    assert parsed.limit is True


@pytest.mark.asyncio
async def test_wiki_app_rejects_non_integer_depth_with_structured_error():
    auth_mock = MagicMock()
    with patch.object(_mcp_app.tools, "require_mcp_auth", auth_mock):
        for bad in ("2", "2.0", 2.0, True, None):
            result = await _mcp_app.mcp.call_tool(
                "expand_wiki_links",
                {"service_id": "svc-1", "path": "a.md", "depth": bad},
            )
            assert isinstance(result, CallToolResult), bad
            assert result.isError is True, bad
            assert result.structuredContent["code"] == -32602, bad
            assert result.structuredContent["message"] == "depth 必须为整数", bad

    auth_mock.assert_not_called()
