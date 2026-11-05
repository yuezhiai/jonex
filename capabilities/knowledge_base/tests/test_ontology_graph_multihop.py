"""[jonex] 本体多跳邻居查询（D11）的仓储层单测。

全部用 fake driver，不连真实 Neo4j。Cypher 语义（stub 过滤、kb 隔离、双向展开）
在 fake 层无法执行，因此对关键 Cypher 片段做静态断言；真实图行为由 T5 真机验证。
"""

import pytest

from capabilities.knowledge_base.repository.ontology_graph_repository import (
    OntologyGraphRepository,
)
from jonex_core.common.exceptions import InvalidParameterError

TENANT = "tenant_demo"
KB = "kb-1"


class _FakeResult:
    def __init__(self, records):
        self._records = records

    def __aiter__(self):
        async def _gen():
            for record in self._records:
                yield record

        return _gen()

    async def single(self):
        return self._records[0] if self._records else None


class _FakeSession:
    def __init__(self, responder):
        self._responder = responder
        self.calls = []

    async def run(self, cypher, params):
        self.calls.append((cypher, params))
        return _FakeResult(self._responder(cypher, params))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDriver:
    def __init__(self, responder):
        self.session_obj = _FakeSession(responder)

    def session(self):
        return self.session_obj


def _start(name="A", etype="设备"):
    return [
        {
            "entity_type": etype,
            "canonical_name": name,
            "aliases": [],
            "attributes": {},
            "description": "",
            "confidence": 0.9,
            "doc_ids": ["doc-1"],
        }
    ]


def _row(src_name, src_type, nid_name, nid_type, label="包含", capped=False, nconf=0.8):
    return {
        "src_type": src_type,
        "src_name": src_name,
        "capped": capped,
        "nid": f"{nid_type}:{nid_name}",
        "nname": nid_name,
        "ntype": nid_type,
        "naliases": [],
        "nattributes": {},
        "ndescription": "",
        "nconfidence": nconf,
        "ndoc_ids": ["doc-1"],
        "source": src_name,
        "source_type": src_type,
        "target": nid_name,
        "target_type": nid_type,
        "label": label,
        "confidence": nconf,
    }


def _repo_with_layers(layers, start=None):
    """layers: {hop: [row, ...]}；起点查询按 params 是否含 hop 区分。"""

    def responder(cypher, params):
        if "hop" not in params:
            return _start() if start is None else start
        return layers.get(params["hop"], [])

    driver = _FakeDriver(responder)
    return OntologyGraphRepository(driver)


async def _call(repo, depth=2, limit=50, fanout=20):
    return await repo.expand_neighbors_multihop(
        TENANT,
        KB,
        "设备",
        "A",
        depth=depth,
        limit=limit,
        per_node_fanout=fanout,
    )


@pytest.mark.asyncio
async def test_ring_graph_layers_without_duplicates():
    """环形图 A-B-C-A：节点不重复、边按 hop 分层、环边保留。"""
    layers = {
        1: [_row("A", "设备", "B", "部件")],
        2: [_row("B", "部件", "C", "部件")],
        3: [_row("C", "部件", "A", "设备")],
    }
    repo = _repo_with_layers(layers)

    result = await _call(repo, depth=3)

    assert [n["id"] for n in result["nodes"]] == ["设备:A", "部件:B", "部件:C"]
    assert [n["hop"] for n in result["nodes"]] == [0, 1, 2]
    assert [(e["source"], e["target"], e["hop"]) for e in result["edges"]] == [
        ("A", "B", 1),
        ("B", "C", 2),
        ("C", "A", 3),
    ]
    assert result["truncated"] is False
    assert result["returned_edges"] == 3


@pytest.mark.asyncio
async def test_multi_path_convergence_keeps_min_hop_node_once():
    """A→B→D、A→C→D：D 只出现一次，hop 取最小值。"""
    layers = {
        1: [_row("A", "设备", "B", "部件"), _row("A", "设备", "C", "部件")],
        2: [_row("B", "部件", "D", "部件"), _row("C", "部件", "D", "部件")],
    }
    repo = _repo_with_layers(layers)

    result = await _call(repo, depth=2)

    d_nodes = [n for n in result["nodes"] if n["id"] == "部件:D"]
    assert len(d_nodes) == 1
    assert d_nodes[0]["hop"] == 2
    assert result["returned_edges"] == 4


@pytest.mark.asyncio
async def test_same_name_different_type_not_merged():
    """「设备:王明」与「人员:王明」是不同节点。"""
    layers = {1: [_row("王明", "设备", "王明", "人员")]}
    repo = _repo_with_layers(layers, start=_start("王明", "设备"))

    result = await _call(repo, depth=2)

    assert {n["id"] for n in result["nodes"]} == {"设备:王明", "人员:王明"}


@pytest.mark.asyncio
async def test_truncated_when_db_hits_exceed_limit():
    """DB 命中 > limit：返回恰 limit 条，truncated=true。"""
    layers = {
        1: [_row("A", "设备", f"N{i}", "部件") for i in range(30)],
    }
    repo = _repo_with_layers(layers)

    result = await _call(repo, depth=2, limit=20)

    assert result["returned_edges"] == 20
    assert result["truncated"] is True
    assert len(result["edges"]) == 20


@pytest.mark.asyncio
async def test_exact_limit_is_not_truncated():
    """恰好 limit 条不应误报截断。"""
    layers = {1: [_row("A", "设备", f"N{i}", "部件") for i in range(20)]}
    repo = _repo_with_layers(layers)

    result = await _call(repo, depth=2, limit=20)

    assert result["returned_edges"] == 20
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_depth_three_with_limit_one_returns_one_edge_truncated():
    """depth=3 × limit=1：总边数上限优先于深度，恰返回 1 条且 truncated=true。"""
    layers = {
        1: [_row("A", "设备", "B", "部件")],
        2: [_row("B", "部件", "C", "部件")],
        3: [_row("C", "部件", "D", "部件")],
    }
    repo = _repo_with_layers(layers)

    result = await _call(repo, depth=3, limit=1)

    assert result["returned_edges"] == 1
    assert len(result["edges"]) == 1
    # 按 hop 稳定排序后保留的是第一跳边；后续层被截断。
    assert result["edges"][0] == {
        "source": "A",
        "source_type": "设备",
        "target": "B",
        "target_type": "部件",
        "label": "包含",
        "confidence": 0.8,
        "hop": 1,
    }
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_parallel_edges_and_self_loop_dedup():
    """平行边按四元组去重；自环保留一条且不重复展开节点。"""
    layers = {
        1: [
            _row("A", "设备", "B", "部件"),
            _row("A", "设备", "B", "部件"),
            _row("A", "设备", "A", "设备"),
        ]
    }
    repo = _repo_with_layers(layers)

    result = await _call(repo, depth=2)

    assert result["returned_edges"] == 2
    assert [n["id"] for n in result["nodes"]] == ["设备:A", "部件:B"]


@pytest.mark.asyncio
async def test_fanout_cap_sets_truncated():
    """单节点命中扇出上限时置 truncated=true。"""
    layers = {1: [_row("A", "设备", "B", "部件", capped=True)]}
    repo = _repo_with_layers(layers)

    result = await _call(repo, depth=2, fanout=20)

    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_cypher_pins_tenant_kb_and_filters_stub():
    """Cypher 必须钉死 tenant/kb、过滤 stub、使用双向关系。"""
    layers = {1: [_row("A", "设备", "B", "部件")]}
    repo = _repo_with_layers(layers)

    await _call(repo, depth=2)

    assert repo._driver.session_obj.calls, "应至少发起起点查询与一层展开"
    for cypher, params in repo._driver.session_obj.calls:
        assert params["t"] == TENANT
        assert params["k"] == KB
        if "hop" in params:
            assert "coalesce(n.stub,false)=false" in cypher
            assert "-[r:ONT_REL]-" in cypher
        else:
            assert "coalesce(e.stub,false)=false" in cypher
            assert "entity_type:$etype" in cypher
            assert "canonical_name:$name" in cypher


@pytest.mark.asyncio
async def test_invalid_parameters_rejected():
    """depth<2、limit<1、fanout<1 由本层拒绝。"""
    repo = _repo_with_layers({})

    with pytest.raises(InvalidParameterError):
        await _call(repo, depth=1)
    with pytest.raises(InvalidParameterError):
        await _call(repo, depth=2, limit=0)
    with pytest.raises(InvalidParameterError):
        await _call(repo, depth=2, fanout=0)


@pytest.mark.asyncio
async def test_missing_start_returns_empty():
    """起点不存在：返回空图，不报错。"""
    repo = _repo_with_layers({}, start=[])

    result = await _call(repo, depth=2)

    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["truncated"] is False
    assert result["depth_reached"] == 0


@pytest.mark.asyncio
async def test_stub_start_returns_empty_graph_without_expansion():
    """起点为 stub：起点查询被 Cypher 过滤为空 ⇒ 空图且不发起任何层展开。

    fake driver 无法执行 Cypher，这里模拟「起点查询返回空」；stub 过滤本身
    由同一文件 ``test_cypher_pins_tenant_kb_and_filters_stub`` 的静态断言覆盖。
    """
    driver = _FakeDriver(lambda _cypher, _params: [])
    repo = OntologyGraphRepository(driver)

    result = await _call(repo, depth=3)

    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["truncated"] is False
    assert result["depth_reached"] == 0
    # 只有起点查询；frontier 为空后不再发起 hop 展开。
    assert len(driver.session_obj.calls) == 1
    start_cypher, start_params = driver.session_obj.calls[0]
    assert "hop" not in start_params
    assert "coalesce(e.stub,false)=false" in start_cypher
