#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""本体列式浏览 MCP 工具测试——认证 / 服务范围 / KB 归属 / 上限 / 静态契约。

覆盖契约 `docs/ai-mcp/ontology-column-browse-mcp-tool-contract.md` 的：
  A1–A3、A6、E3、E5、E6、T3–T7。

三个工具均为只读透传：handler 自校验参数 → 鉴权 → 服务级 view 权限 →
刷新 last_call_at → get_service 取 kb_ids 校验 KB 归属 → 调目标 action。
"""

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

_AUTH_PATH = "mcp_server.tools.require_mcp_auth"
_CALL_PATH = "mcp_server.tools.call_gateway"
_TOUCH_PATH = "mcp_server.tools.touch_service_call_async"

_READ_ACTIONS = {
    "get_service",
    "list_ontology_entity_types",
    "list_ontology_instances",
    "expand_ontology_neighbors",
}


def _tool_error(result):
    """工具 handler 的异常由 ``_inject_mcp_tool`` 翻译成
    ``CallToolResult(isError=True)``，码值在 ``structuredContent``。

    按线上形态断言并还原 ``value.code / .message / .data``，让既有断言
    语义不变，同时锁定「错误必须带码值」。``McpAuthError`` 路径的原始
    HTTP 状态码在 ``data.status_code``，还原成 ``value.status_code``。
    """
    from mcp_server.tools import McpToolError

    assert isinstance(result, CallToolResult), f"expected CallToolResult, got {type(result)!r}"
    assert result.isError is True
    sc = result.structuredContent or {}
    data = sc.get("data") or {}
    err = McpToolError(sc["code"], sc["message"], data)
    if "status_code" in data:
        err.status_code = data["status_code"]
    return SimpleNamespace(value=err)


def _auth(**overrides):
    """构造服务级授权上下文；默认 svc-1 具备 view 权限。"""
    from mcp_server.auth import McpAuthContext

    params = {
        "key_id": "key-1",
        "tenant_id": "tenant_t",
        "allowed_kb_ids": [],
        "service_permissions": {"svc-1": "view"},
        "write_grants": [],
    }
    params.update(overrides)
    return McpAuthContext(**params)


def _call_mock(responses):
    """按顺序返回各次 call_gateway 结果的 AsyncMock。"""
    return AsyncMock(side_effect=list(responses))


# ============================================================================
# A1–A3：认证 / 服务授权范围 / view 级放行
# ============================================================================


class TestAuthAndServiceScope:
    """认证与授权三档：无认证、服务不在范围、view 级放行。"""

    @pytest.mark.asyncio
    async def test_a1_missing_auth_raises_and_skips_upstream(self):
        """A1 未认证 → 结构化 -32002（原始 401），且不调用任何上游 action。"""
        from mcp_server.auth import McpAuthError
        from mcp_server.tools import list_ontology_entity_types

        upstream = AsyncMock()
        with patch(_AUTH_PATH, side_effect=McpAuthError(401, "需要认证")):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    exc_info = _tool_error(
                        await list_ontology_entity_types("svc-1", "kb-1")
                    )

        assert exc_info.value.code == -32002
        assert exc_info.value.status_code == 401
        upstream.assert_not_called()

    @pytest.mark.asyncio
    async def test_a2_service_not_in_scope_rejected(self):
        """A2 服务不在 Key 授权范围 → 结构化 -32002，不调上游。

        走真实 ``_check_service_scope``，不是构造异常对象。
        """
        from mcp_server.tools import list_ontology_instances

        upstream = AsyncMock()
        with patch(_AUTH_PATH, return_value=_auth(service_permissions={"svc-other": "view"})):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    exc_info = _tool_error(
                        await list_ontology_instances("svc-1", "kb-1")
                    )

        assert exc_info.value.code == -32002
        assert exc_info.value.status_code == 401
        assert "不在授权范围" in exc_info.value.message
        upstream.assert_not_called()

    @pytest.mark.asyncio
    async def test_a3_view_permission_allows_tools(self):
        """A3 view 级即可调用三个只读工具。"""
        import mcp_server.tools as tools

        upstream = _call_mock([{"kb_ids": ["kb-1"]}, {"items": [], "total": 0}])
        with patch(_AUTH_PATH, return_value=_auth(service_permissions={"svc-1": "view"})):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    await tools.list_ontology_entity_types("svc-1", "kb-1")

        assert [c.args[0] for c in upstream.call_args_list] == [
            "get_service",
            "list_ontology_entity_types",
        ]


# ============================================================================
# A6：上游 500 / 超时 → -32000，message 脱敏
# ============================================================================


class TestUpstreamFailure:
    """上游异常统一 -32000，且不泄露 URL / 堆栈。"""

    @pytest.mark.asyncio
    async def test_a6_upstream_timeout_maps_to_internal_error(self):
        """A6 目标 action 超时 → McpToolError(-32000)，message 不含 URL/堆栈。"""
        import httpx

        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        ok = MagicMock()
        ok.raise_for_status = MagicMock()
        ok.json.return_value = {"code": 0, "data": {"kb_ids": ["kb-1"]}}
        client = MagicMock()
        client.post = AsyncMock(
            side_effect=[ok, httpx.ReadTimeout("upstream timed out")]
        )

        with patch(_AUTH_PATH, return_value=_auth()):
            with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
                with patch(_TOUCH_PATH):
                    err = _tool_error(
                        await tools.list_ontology_instances("svc-1", "kb-1")
                    ).value

        assert err.code == -32000
        assert err.message == "知识库服务暂不可用"
        lowered = err.message.lower()
        for forbidden in ("http://", "https://", "/internal", "traceback", "timeout"):
            assert forbidden not in lowered

    @pytest.mark.asyncio
    async def test_a6_upstream_500_maps_to_internal_error(self):
        """A6 上游 500 → -32000，message 脱敏。"""
        import httpx

        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        ok = MagicMock()
        ok.raise_for_status = MagicMock()
        ok.json.return_value = {"code": 0, "data": {"kb_ids": ["kb-1"]}}
        request = httpx.Request("POST", "http://gateway/internal/kb/invoke")
        response = httpx.Response(500, request=request)
        unauth = MagicMock()
        unauth.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "server error", request=request, response=response
            )
        )
        client = MagicMock()
        client.post = AsyncMock(side_effect=[ok, unauth])

        with patch(_AUTH_PATH, return_value=_auth()):
            with patch("mcp_server.tools.get_http_client", AsyncMock(return_value=client)):
                with patch(_TOUCH_PATH):
                    err = _tool_error(
                        await tools.expand_ontology_neighbors(
                            "svc-1", "kb-1", "设备", "1号变压器"
                        )
                    ).value

        assert err.code == -32000
        assert "gateway" not in err.message.lower()


# ============================================================================
# E3：KB 归属校验（唯一活路径 get_service → kb_ids）
# ============================================================================


class TestKnowledgeBaseOwnership:
    """E3：KB 不在服务 kb_ids 内一律 -32002，不区分存在性。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name,args,target_action",
        [
            ("list_ontology_entity_types", ("svc-1", "kb-x"), "list_ontology_entity_types"),
            (
                "list_ontology_instances",
                ("svc-1", "kb-x"),
                "list_ontology_instances",
            ),
            (
                "expand_ontology_neighbors",
                ("svc-1", "kb-x", "设备", "1号变压器"),
                "expand_ontology_neighbors",
            ),
        ],
    )
    async def test_e3_kb_not_in_service_rejected_uniformly(
        self, tool_name, args, target_action
    ):
        """E3 KB 越界 → -32002「知识库不在该服务范围内」，且不调目标 action。"""
        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        upstream = _call_mock([{"kb_ids": ["kb-1", "kb-2"]}])
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    exc_info = _tool_error(await getattr(tools, tool_name)(*args))

        assert exc_info.value.code == -32002
        assert exc_info.value.message == "知识库不在该服务范围内"
        # 只发了 get_service，目标 action 未触发
        assert [c.args[0] for c in upstream.call_args_list] == ["get_service"]
        assert target_action != "get_service"

    @pytest.mark.asyncio
    async def test_e3_message_does_not_leak_existence(self):
        """E3 不存在的 KB 与存在但越界的 KB 返回完全相同的错误消息。"""
        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        messages = []
        for kb_id in ("kb-x", "kb-2"):
            upstream = _call_mock([{"kb_ids": ["kb-1"]}])
            with patch(_AUTH_PATH, return_value=_auth()):
                with patch(_CALL_PATH, upstream):
                    with patch(_TOUCH_PATH):
                        exc_info = _tool_error(
                            await tools.list_ontology_entity_types("svc-1", kb_id)
                        )
            messages.append(exc_info.value.message)

        assert messages[0] == messages[1] == "知识库不在该服务范围内"

    @pytest.mark.asyncio
    async def test_e3_empty_kb_ids_maps_to_service_unavailable(self):
        """E3 服务下无可用知识库 → -32000。"""
        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        upstream = _call_mock([{"kb_ids": []}])
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    exc_info = _tool_error(
                        await tools.list_ontology_instances("svc-1", "kb-1")
                    )

        assert exc_info.value.code == -32000
        assert exc_info.value.message == "服务下无可用知识库"


# ============================================================================
# E5：只刷新 last_call_at，不写搜索历史 / 审计
# ============================================================================


class TestSideEffects:
    """E5：每次工具调用仅触发一次 touch，不携带历史/审计写参数。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name,args,response_datas",
        [
            (
                "list_ontology_entity_types",
                ("svc-1", "kb-1"),
                [{"kb_ids": ["kb-1"]}, {"items": [], "total": 0}],
            ),
            (
                "list_ontology_instances",
                ("svc-1", "kb-1"),
                [{"kb_ids": ["kb-1"]}, {"items": [], "total": 0, "page": 1, "page_size": 20}],
            ),
            (
                "expand_ontology_neighbors",
                ("svc-1", "kb-1", "设备", "1号变压器"),
                [{"kb_ids": ["kb-1"]}, {"nodes": [], "edges": [], "degraded": False}],
            ),
        ],
    )
    async def test_e5_touch_once_and_no_history_or_audit(
        self, tool_name, args, response_datas
    ):
        import mcp_server.tools as tools

        touch = MagicMock()
        upstream = _call_mock(response_datas)
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH, touch):
                    await getattr(tools, tool_name)(*args)

        touch.assert_called_once_with("tenant_t", "svc-1")
        payload = upstream.call_args_list[-1].args[3]
        assert "save_history" not in payload
        assert "tenant_id" not in payload
        actions = [c.args[0] for c in upstream.call_args_list]
        assert all("history" not in a and "audit" not in a for a in actions)


# ============================================================================
# E6：邻居节点透传稳定 id
# ============================================================================


class TestNeighborStableId:
    """E6：节点 id 原样透传，供调用方维护已访问集合。"""

    @pytest.mark.asyncio
    async def test_e6_nodes_keep_stable_id(self):
        import mcp_server.tools as tools

        nodes = [
            {"id": "设备:1号变压器", "name": "1号变压器", "type": "设备"},
            {"id": "部件:绕组", "name": "绕组", "type": "部件"},
        ]
        edges = [{"source": "1号变压器", "target": "绕组", "label": "包含"}]
        upstream = _call_mock(
            [{"kb_ids": ["kb-1"]}, {"nodes": nodes, "edges": edges, "degraded": False}]
        )
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    result = await tools.expand_ontology_neighbors(
                        "svc-1", "kb-1", "设备", "1号变压器"
                    )

        assert result["nodes"] == nodes
        assert [n["id"] for n in result["nodes"]] == ["设备:1号变压器", "部件:绕组"]
        assert result["edges"] == edges
        assert result["degraded"] is False


# ============================================================================
# T3：上限 / 边界校验（handler 自校验，越界不调上游）
# ============================================================================


class TestParameterBounds:
    """T3：MCP 不经 REST le 约束，handler 必须自行校验上下界。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"page": 0},
            {"page": -1},
            {"page": 1001},
            {"page_size": 0},
            {"page_size": 101},
            {"page_size": -5},
        ],
    )
    async def test_t3_instances_paging_bounds(self, kwargs):
        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        upstream = _call_mock([{"kb_ids": ["kb-1"]}])
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    exc_info = _tool_error(
                        await tools.list_ontology_instances("svc-1", "kb-1", **kwargs)
                    )

        assert exc_info.value.code == -32602
        upstream.assert_not_called()

    @pytest.mark.asyncio
    async def test_t3_instances_page_upper_bound_accepted(self):
        """page=1000 / page_size=100 合法，可透传。"""
        import mcp_server.tools as tools

        upstream = _call_mock(
            [{"kb_ids": ["kb-1"]}, {"items": [], "total": 0, "page": 1000, "page_size": 100}]
        )
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    await tools.list_ontology_instances(
                        "svc-1", "kb-1", page=1000, page_size=100
                    )

        payload = upstream.call_args_list[-1].args[3]
        assert payload["page"] == 1000
        assert payload["page_size"] == 100

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, -1, 201])
    async def test_t3_neighbors_limit_bounds(self, limit):
        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        upstream = _call_mock([{"kb_ids": ["kb-1"]}])
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    exc_info = _tool_error(
                        await tools.expand_ontology_neighbors(
                            "svc-1", "kb-1", "设备", "1号变压器", limit=limit
                        )
                    )

        assert exc_info.value.code == -32602
        upstream.assert_not_called()

    @pytest.mark.asyncio
    async def test_t3_neighbors_limit_upper_bound_accepted(self):
        import mcp_server.tools as tools

        upstream = _call_mock(
            [{"kb_ids": ["kb-1"]}, {"nodes": [], "edges": [], "degraded": False}]
        )
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    await tools.expand_ontology_neighbors(
                        "svc-1", "kb-1", "设备", "1号变压器", limit=200
                    )

        assert upstream.call_args_list[-1].args[3]["limit"] == 200

    @pytest.mark.asyncio
    async def test_t3_entity_type_and_keyword_length_bounds(self):
        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        cases = [
            {"entity_type": "x" * 129},
            {"keyword": "x" * 256},
        ]
        for kwargs in cases:
            upstream = _call_mock([{"kb_ids": ["kb-1"]}])
            with patch(_AUTH_PATH, return_value=_auth()):
                with patch(_CALL_PATH, upstream):
                    with patch(_TOUCH_PATH):
                        exc_info = _tool_error(
                            await tools.list_ontology_instances("svc-1", "kb-1", **kwargs)
                        )
            assert exc_info.value.code == -32602
            upstream.assert_not_called()

    @pytest.mark.asyncio
    async def test_t3_canonical_name_length_bound(self):
        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        upstream = _call_mock([{"kb_ids": ["kb-1"]}])
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    exc_info = _tool_error(
                        await tools.expand_ontology_neighbors(
                            "svc-1", "kb-1", "设备", "x" * 513
                        )
                    )

        assert exc_info.value.code == -32602
        upstream.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name,args",
        [
            ("list_ontology_entity_types", ("", "kb-1")),
            ("expand_ontology_neighbors", ("svc-1", "kb-1", "", "1号变压器")),
            ("expand_ontology_neighbors", ("svc-1", "kb-1", "设备", " ")),
        ],
    )
    async def test_t3_blank_required_params_rejected(self, tool_name, args):
        import mcp_server.tools as tools
        from mcp_server.tools import McpToolError

        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, AsyncMock()) as upstream:
                with patch(_TOUCH_PATH):
                    exc_info = _tool_error(await getattr(tools, tool_name)(*args))

        assert exc_info.value.code == -32602
        upstream.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank_kb", ["", "  "])
    async def test_t3_blank_knowledge_base_id_means_unspecified(self, blank_kb):
        """knowledge_base_id 空白 = 未提供 → 查询服务下全部 KB（单 KB 时旧形态）。"""
        import mcp_server.tools as tools

        upstream = _call_mock([{"kb_ids": ["kb-1"]}, {"items": [], "total": 0}])
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    await tools.list_ontology_entity_types("svc-1", blank_kb)

        assert [c.args[0] for c in upstream.call_args_list] == [
            "get_service",
            "list_ontology_entity_types",
        ]
        assert upstream.call_args_list[-1].args[3] == {"knowledge_base_id": "kb-1"}


# ============================================================================
# T4 / T5：include_unknown 不暴露；空串参数省略
# ============================================================================


class TestPayloadExposure:
    """T4/T5：不暴露 include_unknown / document_id，空串不入 payload。"""

    @pytest.mark.asyncio
    async def test_t4_include_unknown_never_in_payload(self):
        import mcp_server.tools as tools

        upstream = _call_mock(
            [{"kb_ids": ["kb-1"]}, {"items": [], "total": 0, "page": 1, "page_size": 20}]
        )
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    await tools.list_ontology_instances("svc-1", "kb-1")

        payload = upstream.call_args_list[-1].args[3]
        assert "include_unknown" not in payload
        assert "document_id" not in payload

    @pytest.mark.asyncio
    async def test_t5_include_unknown_not_accepted_by_handler(self):
        """include_unknown 不是 handler 入参，传了直接 TypeError。"""
        import inspect

        import mcp_server.tools as tools

        for name in (
            "list_ontology_entity_types",
            "list_ontology_instances",
            "expand_ontology_neighbors",
        ):
            params = inspect.signature(getattr(tools, name)).parameters
            assert "include_unknown" not in params
            assert "document_id" not in params

    @pytest.mark.asyncio
    async def test_t5_blank_optional_params_omitted_from_payload(self):
        import mcp_server.tools as tools

        upstream = _call_mock(
            [{"kb_ids": ["kb-1"]}, {"items": [], "total": 0, "page": 1, "page_size": 20}]
        )
        with patch(_AUTH_PATH, return_value=_auth()):
            with patch(_CALL_PATH, upstream):
                with patch(_TOUCH_PATH):
                    await tools.list_ontology_instances(
                        "svc-1", "kb-1", entity_type="  ", keyword=""
                    )

        payload = upstream.call_args_list[-1].args[3]
        assert "entity_type" not in payload
        assert "keyword" not in payload


# ============================================================================
# T6：静态断言——handler 只调白名单内读 action
# ============================================================================


class TestReadOnlyStatic:
    """T6：源码静态检查，禁止写 action。"""

    _HANDLERS = (
        "list_ontology_entity_types",
        "list_ontology_instances",
        "expand_ontology_neighbors",
    )

    @staticmethod
    def _tree():
        tools_path = Path(__file__).resolve().parents[1] / "tools.py"
        return ast.parse(tools_path.read_text(encoding="utf-8"))

    def _call_gateway_actions(self, func_node):
        actions = []
        for sub in ast.walk(func_node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "call_gateway":
                continue
            if sub.args and isinstance(sub.args[0], ast.Constant):
                actions.append(sub.args[0].value)
        return actions

    def test_t6_handlers_only_call_whitelisted_read_actions(self):
        tree = self._tree()
        targets = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in self._HANDLERS + ("_resolve_ontology_kb_ids",)
        }
        assert set(self._HANDLERS) <= set(targets), "三个 handler 必须存在"

        found = set()
        for name, node in targets.items():
            actions = self._call_gateway_actions(node)
            found.update(actions)
            for action in actions:
                assert action in _READ_ACTIONS, f"{name} 调用了非读白名单 action: {action}"

        assert found, "handler 必须通过 call_gateway 调能力"
        assert "get_service" in found

    def test_t6_no_write_action_strings_in_handlers(self):
        tree = self._tree()
        forbidden = ("upload", "create_", "update_", "delete_", "submit_", "import_")
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in self._HANDLERS:
                continue
            for action in self._call_gateway_actions(node):
                assert not any(token in action for token in forbidden), (
                    f"{node.name} 调用了疑似写 action: {action}"
                )


# ============================================================================
# T7：静态断言——app.py 三段式描述 / 邻居截断与去环指引 / 无 depth
# ============================================================================


class TestToolDescriptionStatic:
    """T7：注册层 docstring 与签名契约。"""

    @staticmethod
    def _app_functions():
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))
        return {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    @pytest.mark.parametrize(
        "tool_name",
        [
            "list_ontology_entity_types",
            "list_ontology_instances",
            "expand_ontology_neighbors",
        ],
    )
    def test_t7_docstring_has_three_sections(self, tool_name):
        funcs = self._app_functions()
        assert tool_name in funcs, f"app.py 未注册 {tool_name}"
        doc = ast.get_docstring(funcs[tool_name]) or ""
        assert doc.strip(), f"{tool_name} 缺 docstring"
        assert "何时用" in doc, f"{tool_name} docstring 缺「何时用」"
        assert "何时不用" in doc, f"{tool_name} docstring 缺「何时不用」"

    def test_t7_neighbors_doc_mentions_truncation_and_visited_set(self):
        funcs = self._app_functions()
        doc = ast.get_docstring(funcs["expand_ontology_neighbors"]) or ""
        assert "截断" in doc, "邻居工具 docstring 必须提示边数==limit 时可能截断"
        assert "已访问集合" in doc, "邻居工具 docstring 必须提示自行维护已访问集合"
        assert "一跳" in doc

    def test_t7_neighbors_doc_mentions_multihop_contract(self):
        """T7：多跳改造后描述须含 depth / 多跳 / truncated / 去重，且不再声明不支持多跳。"""
        funcs = self._app_functions()
        doc = ast.get_docstring(funcs["expand_ontology_neighbors"]) or ""
        for token in ("depth", "多跳", "truncated", "去重"):
            assert token in doc, f"邻居工具 docstring 必须提到 {token!r}"
        assert "不支持多跳" not in doc, "多跳已支持，描述不得再声明不支持多跳"

    def test_t7_depth_parameter_contract(self):
        """T7：只有 expand_ontology_neighbors 接受 depth，且默认值为 1。

        旧断言 ``"depth" not in funcs[name].args.args`` 比较的是 ``ast.arg``
        对象列表与字符串，恒为真、没有约束力；这里改为解析参数名与默认值。
        """
        funcs = self._app_functions()

        def _params(fn):
            names = [a.arg for a in fn.args.args]
            defaults = fn.args.defaults
            bound = {}
            if defaults:
                for arg, default in zip(names[-len(defaults):], defaults):
                    bound[arg] = ast.literal_eval(default)
            return names, bound

        names, bound = _params(funcs["expand_ontology_neighbors"])
        assert "depth" in names, "expand_ontology_neighbors 必须接受 depth"
        assert bound.get("depth") == 1, "depth 默认值必须为 1"

        for name in ("list_ontology_entity_types", "list_ontology_instances"):
            names, _ = _params(funcs[name])
            assert "depth" not in names, f"{name} 不应接受 depth"
