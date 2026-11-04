#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""本体列式浏览 MCP 工具——同 KB 多跳（D11）测试。

只 patch 三个既有接缝（require_mcp_auth / call_gateway / touch_service_call_async），
断言 payload 与返回形态，不依赖实现私有 helper。
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from mcp_server.tests.test_tools_ontology_multi_kb import (  # noqa: E402
    _patched_env,
    _serve,
    _target_payloads,
    _tool_error,
)

# app.py 模块级 assert_secure_jwt_secret 要求 JWT_SECRET 非空；仅在 import
# 期间临时设置，import 完立即还原，避免影响其他用例的环境基线。
_JWT_SECRET_ADDED = not os.environ.get("JWT_SECRET")
if _JWT_SECRET_ADDED:
    os.environ["JWT_SECRET"] = "multihop_depth_schema_secret_7c1e"
import app as _mcp_app  # noqa: E402
if _JWT_SECRET_ADDED:
    os.environ.pop("JWT_SECRET", None)

_AUTH_PATH = "mcp_server.tools.require_mcp_auth"
_CALL_PATH = "mcp_server.tools.call_gateway"
_TOUCH_PATH = "mcp_server.tools.touch_service_call_async"


def _multi_kb_result(kb_id):
    return {
        "nodes": [
            {"id": "设备:A", "name": "A", "type": "设备", "hop": 0},
            {"id": "部件:B", "name": "B", "type": "部件", "hop": 1},
        ],
        "edges": [
            {
                "source": "A",
                "source_type": "设备",
                "target": "B",
                "target_type": "部件",
                "label": "包含",
                "confidence": 0.9,
                "hop": 1,
            }
        ],
        "degraded": False,
        "depth": 2,
        "limit": 50,
        "returned": 1,
        "truncated": True,
        "kb": kb_id,
    }


@pytest.mark.asyncio
async def test_depth_default_payload_has_no_depth():
    """depth 不传：payload 不含 depth，保持现有精确断言。"""
    import mcp_server.tools as tools

    upstream = _serve(["kb-1"], {"kb-1": {"nodes": [], "edges": [], "degraded": False}})
    with _patched_env(upstream=upstream):
        result = await tools.expand_ontology_neighbors(
            "svc-1", entity_type="设备", canonical_name="A"
        )

    assert _target_payloads(upstream) == [
        {"knowledge_base_id": "kb-1", "entity_type": "设备", "canonical_name": "A", "limit": 50}
    ]
    assert result == {"nodes": [], "edges": [], "degraded": False}


@pytest.mark.asyncio
async def test_depth_one_payload_has_no_depth():
    import mcp_server.tools as tools

    upstream = _serve(["kb-1"], {"kb-1": {"nodes": [], "edges": [], "degraded": False}})
    with _patched_env(upstream=upstream):
        await tools.expand_ontology_neighbors(
            "svc-1", entity_type="设备", canonical_name="A", depth=1
        )

    assert "depth" not in _target_payloads(upstream)[0]


@pytest.mark.asyncio
async def test_depth_two_payload_contains_depth():
    import mcp_server.tools as tools

    upstream = _serve(["kb-1"], {"kb-1": _multi_kb_result("kb-1")})
    with _patched_env(upstream=upstream):
        result = await tools.expand_ontology_neighbors(
            "svc-1", entity_type="设备", canonical_name="A", depth=2
        )

    payload = _target_payloads(upstream)[0]
    assert payload["depth"] == 2
    assert result["depth"] == 2
    assert result["returned"] == 1
    assert result["truncated"] is True
    assert result["nodes"][0]["hop"] == 0


@pytest.mark.asyncio
async def test_depth_out_of_range_rejected_before_upstream():
    import mcp_server.tools as tools

    for bad in (0, 4, "2", "2.0", True, 2.0, "abc", None):
        upstream = AsyncMock()
        with _patched_env(upstream=upstream):
            exc = _tool_error(
                await tools.expand_ontology_neighbors(
                    "svc-1", entity_type="设备", canonical_name="A", depth=bad
                )
            )
        assert exc.value.code == -32602, bad
        upstream.assert_not_called()


@pytest.mark.asyncio
async def test_multikb_depth_groups_carry_contract_fields():
    import mcp_server.tools as tools

    upstream = _serve(
        ["kb-1", "kb-2"],
        {"kb-1": _multi_kb_result("kb-1"), "kb-2": _multi_kb_result("kb-2")},
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_ontology_neighbors(
            "svc-1",
            entity_type="设备",
            canonical_name="A",
            depth=2,
            knowledge_base_ids=["kb-1", "kb-2"],
        )

    assert set(result.keys()) == {"groups"}
    assert [g["knowledge_base_id"] for g in result["groups"]] == ["kb-1", "kb-2"]
    for group in result["groups"]:
        assert group["depth"] == 2
        assert group["limit"] == 50
        assert group["returned"] == 1
        assert group["truncated"] is True
        assert group["nodes"][0]["hop"] == 0


@pytest.mark.asyncio
async def test_multikb_depth_one_groups_unchanged():
    import mcp_server.tools as tools

    upstream = _serve(
        ["kb-1", "kb-2"],
        {
            "kb-1": {"nodes": [], "edges": [], "degraded": False},
            "kb-2": {"nodes": [], "edges": [], "degraded": False},
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_ontology_neighbors(
            "svc-1",
            entity_type="设备",
            canonical_name="A",
            knowledge_base_ids=["kb-1", "kb-2"],
        )

    for group in result["groups"]:
        assert set(group.keys()) == {"knowledge_base_id", "nodes", "edges", "degraded"}


@pytest.mark.asyncio
async def test_multikb_deadline_marks_remaining_as_errors(monkeypatch):
    """多 KB × 多跳整体 deadline：超时库进 errors，已完成库照常返回。"""
    import mcp_server.tools as tools

    monkeypatch.setattr(tools, "_MULTIHOP_DEADLINE_SECONDS", 0.05)

    async def _side_effect(action, tenant_id, mcp_key_id, data=None, **kwargs):
        if action == "get_service":
            return {"kb_ids": ["kb-1", "kb-2"]}
        kb_id = (data or {}).get("knowledge_base_id")
        if kb_id == "kb-2":
            await asyncio.sleep(0.2)
        return _multi_kb_result(kb_id)

    from unittest.mock import AsyncMock as _AsyncMock

    upstream = _AsyncMock(side_effect=_side_effect)
    with _patched_env(upstream=upstream):
        result = await tools.expand_ontology_neighbors(
            "svc-1",
            entity_type="设备",
            canonical_name="A",
            depth=2,
            knowledge_base_ids=["kb-1", "kb-2"],
        )

    assert result["groups"][0]["knowledge_base_id"] == "kb-1"
    assert "errors" in result["groups"][1]
    assert result["groups"][1]["errors"][0]["code"] == -32000


@pytest.mark.asyncio
async def test_multikb_depth_partial_failure_error_group_only_errors():
    """多 KB × depth>1 部分失败：成功组照常，失败组只含 errors，顶层没有图字段。"""
    from mcp_server.tools import McpToolError
    import mcp_server.tools as tools

    upstream = _serve(
        ["kb-1", "kb-2"],
        {
            "kb-1": _multi_kb_result("kb-1"),
            "kb-2": McpToolError(-32000, "知识库服务暂不可用"),
        },
    )
    with _patched_env(upstream=upstream):
        result = await tools.expand_ontology_neighbors(
            "svc-1",
            entity_type="设备",
            canonical_name="A",
            depth=2,
            knowledge_base_ids=["kb-1", "kb-2"],
        )

    # 多 KB 形态只回 groups：顶层不得出现可跨库混淆的 nodes/edges/degraded。
    assert set(result.keys()) == {"groups"}
    ok_group, bad_group = result["groups"]

    assert ok_group["knowledge_base_id"] == "kb-1"
    assert ok_group["nodes"]
    assert ok_group["edges"]
    assert ok_group["degraded"] is False
    assert ok_group["depth"] == 2
    assert ok_group["limit"] == 50
    assert ok_group["returned"] == 1
    assert ok_group["truncated"] is True

    assert set(bad_group.keys()) == {"knowledge_base_id", "errors"}
    assert bad_group["knowledge_base_id"] == "kb-2"
    assert len(bad_group["errors"]) == 1
    assert bad_group["errors"][0]["code"] == -32000


def test_app_and_tools_expand_signature_match():
    """app.py 注册签名与 tools.py handler 的参数名/顺序/默认值一致（含 depth=1）。"""
    import ast
    import inspect
    from pathlib import Path

    # 不 import mcp_server.app：模块级 assert_secure_jwt_secret 会拒绝无 secret 启动。
    app_path = Path(__file__).resolve().parents[2] / "mcp_server" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))

    app_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "expand_ontology_neighbors"
    )

    def _defaults(fn):
        result = {}
        positional = fn.args.args
        defaults = fn.args.defaults
        for arg, default in zip(positional[-len(defaults):], defaults):
            result[arg.arg] = ast.literal_eval(default)
        return [a.arg for a in positional], result

    app_args, app_defaults = _defaults(app_fn)
    tools_signature = inspect.signature(
        __import__("mcp_server.tools", fromlist=["expand_ontology_neighbors"]).expand_ontology_neighbors
    )
    tools_args = list(tools_signature.parameters)
    tools_defaults = {
        name: param.default
        for name, param in tools_signature.parameters.items()
        if param.default is not inspect.Parameter.empty
    }

    assert app_args == tools_args
    assert app_defaults.get("depth") == 1
    assert tools_defaults.get("depth") == 1


def test_app_depth_schema_stays_integer_and_keeps_raw_value():
    """注册层 depth 仍是 integer，且 raw 值不再被 pydantic 静默转成 int。"""
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    meta = func_metadata(_mcp_app.expand_ontology_neighbors)
    assert meta.arg_model.model_json_schema()["properties"]["depth"] == {
        "default": 1,
        "title": "Depth",
        "type": "integer",
    }

    parsed = meta.arg_model.model_validate(
        {
            "service_id": "svc-1",
            "knowledge_base_id": "kb-1",
            "entity_type": "设备",
            "canonical_name": "A",
            "depth": "2",
        }
    )
    assert parsed.depth == "2"


@pytest.mark.asyncio
async def test_app_depth_non_integer_returns_structured_32602():
    """非整数/bool 必须由 handler 统一返回结构化 -32602，且不触达鉴权。"""
    auth_mock = MagicMock()
    with patch.object(_mcp_app.tools, "require_mcp_auth", auth_mock):
        for bad in ("2", "2.0", 2.0, True, "abc", None):
            result = await _mcp_app.mcp.call_tool(
                "expand_ontology_neighbors",
                {
                    "service_id": "svc-1",
                    "knowledge_base_id": "kb-1",
                    "entity_type": "设备",
                    "canonical_name": "A",
                    "depth": bad,
                },
            )
            assert result.isError is True, bad
            assert result.structuredContent["code"] == -32602, bad
            assert result.structuredContent["message"] == "depth 必须为整数", bad

    auth_mock.assert_not_called()


# ============================================================================
# 改造前 golden 回归（docs/ai-mcp/golden/ontology-neighbors-before-depth.json）
# ============================================================================

_GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "ai-mcp"
    / "golden"
    / "ontology-neighbors-before-depth.json"
)

# 与 record_ontology_neighbors_golden.py 的 NORMAL 常量逐字段一致。
# golden 的 ``handler_response`` 是 handler 的原始 dict；``mcp_result`` 才是
# FastMCP ``content[0].text``（格式化 JSON）包装，本用例比对 ``handler_response``。
_GOLDEN_NORMAL = {
    "nodes": [
        {
            "id": "设备:1号变压器",
            "name": "1号变压器",
            "type": "设备",
            "aliases": ["主变"],
            "attributes": {"rated_power": "50MVA", "voltage_level": "110kV"},
            "description": "1号主变压器",
            "confidence": 1.0,
            "doc_ids": ["doc-1"],
        },
        {
            "id": "部件:绕组",
            "name": "绕组",
            "type": "部件",
            "aliases": [],
            "attributes": {"material": "铜"},
            "description": "",
            "confidence": 0.9,
            "doc_ids": ["doc-1"],
        },
    ],
    "edges": [
        {
            "source": "1号变压器",
            "source_type": "设备",
            "target": "绕组",
            "target_type": "部件",
            "label": "包含",
            "confidence": 1.0,
        }
    ],
    "degraded": False,
}


def _golden_scenario(scenario_id):
    data = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    return next(s for s in data["scenarios"] if s["id"] == scenario_id)


def _assert_matches_golden(result, scenario):
    """逐字段 + 键顺序（``sort_keys=False``）与 golden 响应数据一致。"""
    expected = scenario["handler_response"]
    assert result == expected, "handler 响应字段值与改造前 golden 不一致"
    assert json.dumps(result, ensure_ascii=False, sort_keys=False) == json.dumps(
        expected, ensure_ascii=False, sort_keys=False
    ), "handler 响应键顺序与改造前 golden 不一致"
    assert list(result.keys()) == scenario["handler_top_level_key_order"]


@pytest.mark.asyncio
async def test_golden_single_kb_depth_omitted_unchanged():
    """depth 省略（默认 1）的单 KB 响应与改造前 golden 逐字段/逐键一致。"""
    import mcp_server.tools as tools

    scenario = _golden_scenario("mcp_single_kb_depth_omitted")
    upstream = _serve(["kb-1"], {"kb-1": _GOLDEN_NORMAL})
    with _patched_env(upstream=upstream):
        result = await tools.expand_ontology_neighbors(
            "svc-1",
            knowledge_base_id="kb-1",
            entity_type="设备",
            canonical_name="1号变压器",
        )

    _assert_matches_golden(result, scenario)
    # depth=1 仍不写 payload。
    assert "depth" not in _target_payloads(upstream)[0]


@pytest.mark.asyncio
async def test_golden_single_kb_depth_one_unchanged():
    """显式 depth=1 与 golden 的 depth=1 基线（等价调用）逐字节一致。"""
    import mcp_server.tools as tools

    scenario = _golden_scenario("mcp_single_kb_depth_1")
    upstream = _serve(["kb-1"], {"kb-1": _GOLDEN_NORMAL})
    with _patched_env(upstream=upstream):
        result = await tools.expand_ontology_neighbors(
            "svc-1",
            knowledge_base_id="kb-1",
            entity_type="设备",
            canonical_name="1号变压器",
            depth=1,
        )

    _assert_matches_golden(result, scenario)
    assert "depth" not in _target_payloads(upstream)[0]
