"""[jonex] 本体多跳邻居 service（D11）单测：分派、字段映射、超时降级、互斥。"""

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest
from neo4j.exceptions import ServiceUnavailable

from capabilities.knowledge_base.services import ontology_query_service as svc_mod
from capabilities.knowledge_base.services.ontology_query_service import (
    OntologyQueryService,
)
from jonex_core.common.exceptions import InvalidParameterError

TENANT = "tenant_demo"
KB = "kb-1"


@pytest.fixture
def repo(monkeypatch):
    mock_repo = AsyncMock()
    mock_repo.expand_neighbors.return_value = {"nodes": [], "edges": []}
    mock_repo.expand_neighbors_multihop.return_value = {
        "nodes": [{"id": "设备:A", "name": "A", "type": "设备", "hop": 0}],
        "edges": [],
        "truncated": False,
        "returned_edges": 0,
        "returned_nodes": 1,
        "depth_requested": 2,
        "depth_reached": 1,
    }
    monkeypatch.setattr(svc_mod, "OntologyGraphRepository", lambda _driver: mock_repo)
    monkeypatch.setattr(svc_mod, "get_neo4j_driver", lambda: MagicMock())
    return mock_repo


def _req(**overrides):
    payload = {
        "knowledge_base_id": KB,
        "entity_type": "设备",
        "canonical_name": "A",
        "limit": 50,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_depth_one_uses_legacy_path(repo):
    result = await OntologyQueryService().expand_ontology_neighbors(TENANT, _req())

    repo.expand_neighbors.assert_awaited_once()
    repo.expand_neighbors_multihop.assert_not_called()
    assert result == {"nodes": [], "edges": [], "degraded": False}


@pytest.mark.asyncio
async def test_depth_one_passes_document_id(repo):
    await OntologyQueryService().expand_ontology_neighbors(
        TENANT, _req(document_id="doc-1")
    )

    kwargs = repo.expand_neighbors.await_args.kwargs
    assert kwargs["document_id"] == "doc-1"


@pytest.mark.asyncio
async def test_depth_two_maps_contract_fields(repo):
    result = await OntologyQueryService().expand_ontology_neighbors(
        TENANT, _req(depth=2)
    )

    repo.expand_neighbors_multihop.assert_awaited_once()
    kwargs = repo.expand_neighbors_multihop.await_args.kwargs
    assert kwargs["depth"] == 2
    assert kwargs["limit"] == 50
    assert result["degraded"] is False
    assert result["depth"] == 2
    assert result["limit"] == 50
    assert result["returned"] == 0
    assert result["truncated"] is False
    # 内部字段不透传
    for internal in ("depth_requested", "returned_edges", "returned_nodes", "depth_reached"):
        assert internal not in result


@pytest.mark.asyncio
async def test_timeout_degrades_with_multihop_fields(repo):
    repo.expand_neighbors_multihop.side_effect = asyncio.TimeoutError

    result = await OntologyQueryService().expand_ontology_neighbors(
        TENANT, _req(depth=2)
    )

    assert result["degraded"] is True
    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["depth"] == 2
    assert result["limit"] == 50
    assert result["returned"] == 0
    assert result["truncated"] is False
    assert "degraded_reason" in result


@pytest.mark.asyncio
async def test_neo4j_down_degrades(repo):
    repo.expand_neighbors_multihop.side_effect = ServiceUnavailable("down")

    result = await OntologyQueryService().expand_ontology_neighbors(
        TENANT, _req(depth=3)
    )

    assert result["degraded"] is True
    assert result["nodes"] == []
    assert result["edges"] == []


def _raise_dns_error(*_args, **_kwargs):
    """模拟 `docker stop neo4j` 后驱动抛出的 ValueError(cause=socket.gaierror)。"""
    raise ValueError("Cannot resolve address neo4j:7687") from socket.gaierror(
        -2, "Name or service not known"
    )


@pytest.mark.asyncio
async def test_neo4j_dns_error_degrades_depth_one(repo):
    """容器停止 → 主机名解析失败：单跳也必须降级，不能冒成 500。"""
    repo.expand_neighbors.side_effect = _raise_dns_error

    result = await OntologyQueryService().expand_ontology_neighbors(TENANT, _req())

    assert result["degraded"] is True
    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["degraded_reason"] == svc_mod._DEGRADED_REASON


@pytest.mark.asyncio
async def test_neo4j_dns_error_degrades_multihop(repo):
    """容器停止 → 主机名解析失败：多跳降级且契约字段齐全。"""
    repo.expand_neighbors_multihop.side_effect = _raise_dns_error

    result = await OntologyQueryService().expand_ontology_neighbors(
        TENANT, _req(depth=2)
    )

    assert result["degraded"] is True
    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["depth"] == 2
    assert result["returned"] == 0
    assert result["truncated"] is False
    assert result["degraded_reason"] == svc_mod._DEGRADED_REASON


def test_is_neo4j_down_matches_cause_chain():
    """沿 cause 链识别；普通业务 ValueError 不得误降级。"""
    dns = ValueError("Cannot resolve address neo4j:7687")
    dns.__cause__ = socket.gaierror(-2, "Name or service not known")
    assert svc_mod._is_neo4j_down(ServiceUnavailable("down")) is True
    assert svc_mod._is_neo4j_down(dns) is True
    assert svc_mod._is_neo4j_down(ValueError("业务参数非法")) is False


@pytest.mark.asyncio
async def test_plain_value_error_is_not_degraded(repo):
    """真正的业务 ValueError 必须继续上抛，不能被降级出口吞掉。"""
    repo.expand_neighbors.side_effect = ValueError("业务参数非法")

    with pytest.raises(ValueError, match="业务参数非法"):
        await OntologyQueryService().expand_ontology_neighbors(TENANT, _req())


@pytest.mark.asyncio
async def test_document_id_with_multihop_rejected(repo):
    with pytest.raises(InvalidParameterError):
        await OntologyQueryService().expand_ontology_neighbors(
            TENANT, _req(depth=2, document_id="doc-1")
        )

    repo.expand_neighbors.assert_not_called()
    repo.expand_neighbors_multihop.assert_not_called()


@pytest.mark.asyncio
async def test_depth_out_of_range_rejected(repo, monkeypatch):
    monkeypatch.setenv("ONTOLOGY_NEIGHBOR_DEPTH_MAX", "3")

    with pytest.raises(InvalidParameterError):
        await OntologyQueryService().expand_ontology_neighbors(
            TENANT, _req(depth=4)
        )

    repo.expand_neighbors.assert_not_called()
    repo.expand_neighbors_multihop.assert_not_called()
