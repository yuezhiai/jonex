"""Neo4j 数据资产 schema 子图仓储（P1.5a）。

**独立 label `DataSourceEntity`**：与文档抽取的 `OntologyEntity` 完全隔离，
现有 29 处本体查询/统计/匹配/对账零改动。

身份（spec §二）：`table`（物理表名）= 作用域内唯一键；列是节点属性
`attributes`（JSON 字符串），不建列节点；血缘是表级 `:DERIVED_FROM` 边，
列映射在边属性 `column_map`（JSON 字符串）。

会话写路径统一走 `_with_transient_retry`（Neo4j TransientError 退避重试）。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from neo4j import AsyncDriver

from jonex_core.common.tenant import require_tenant

# 同包私有助手：与本体图仓储共用同一套 transient 重试口径
# （ONTOLOGY_WRITE_RETRY_ATTEMPTS / ONTOLOGY_WRITE_RETRY_BASE_DELAY）。
from .ontology_graph_repository import _with_transient_retry

logger = logging.getLogger(__name__)

# 读语句只定义一次：`list_nodes` / `list_edges` / `snapshot` 共用，避免读面
# 分叉出第二份 Cypher（字段顺序/别名漂移会让上层解析静默拿不到键）。
# `ORDER BY` 固定返回顺序：画布上限的「保留前 N 个」（节点/边 200/400）与
# 前端 BFS 的同跳顺序都依赖快照顺序，不加排序时 Neo4j 的返回序不保证稳定，
# 跨请求可能换掉入选的节点/边。
_NODES_CYPHER = (
    "MATCH (e:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
    "data_source_id:$data_source_id})\n"
    "RETURN e.table AS table, e.entity_name AS entity_name, "
    "e.attributes AS attributes, e.schema_version AS schema_version, "
    "e.provenance AS provenance\n"
    "ORDER BY e.table"
)
_EDGES_CYPHER = (
    "MATCH (a:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
    "data_source_id:$data_source_id})-[r:DERIVED_FROM]->"
    "(b:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
    "data_source_id:$data_source_id})\n"
    "RETURN a.table AS from_table, b.table AS to_table, "
    "r.column_map AS column_map, r.origin AS origin, r.confidence AS confidence\n"
    "ORDER BY a.table, b.table, r.pair_key"
)

# `MAPS_TO` 重建后的读面（可观测性）：`edges` 必须是图里**实际**的边数，
# 不是执行 MERGE 的次数 —— 期望三元组的文档实体不存在时 MERGE 空转，两者会分叉。
#
# 端点 pattern 只定义一次：快速跳过不变式（`count_maps_to == 0` ⇒ DELETE 必为空）
# 依赖 count 与 DELETE 的 MATCH 完全同构。三条语句若各自持有字符串，未来任何一侧
# 漏改都不会报错，只会在某些租户/KB 上静默数不准或删不干净。
_MAPS_TO_LEFT = "(e:OntologyEntity {tenant_id:$tenant_id, kb_id:$kb_id})"
_MAPS_TO_RIGHT = "(d:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id})"

_COUNT_MAPS_TO_CYPHER = (
    "MATCH " + _MAPS_TO_LEFT + "-[r:MAPS_TO]->" + _MAPS_TO_RIGHT + "\n"
    "RETURN count(r) AS c"
)
_MATCHED_DOC_TYPES_CYPHER = (
    "MATCH " + _MAPS_TO_LEFT + "-[r:MAPS_TO]->" + _MAPS_TO_RIGHT + "\n"
    "RETURN e.canonical_name AS name, count(r) AS c"
)
_DELETE_MAPS_TO_CYPHER = (
    "MATCH " + _MAPS_TO_LEFT + "-[r:MAPS_TO]->" + _MAPS_TO_RIGHT + " DELETE r"
)


class OntologyDataAssetRepository:
    def __init__(self, driver: AsyncDriver):
        self._driver = driver

    async def merge_node(self, tenant_id: str, kb_id: str, data_source_id: str,
                         table: str, entity_name: str, attributes: list,
                         schema_version: Optional[int] = None) -> None:
        tenant_id = require_tenant(tenant_id)
        cypher = (
            "MERGE (e:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
            "data_source_id:$data_source_id, table:$table})\n"
            "SET e.entity_name=$entity_name, e.attributes=$attributes, "
            "e.schema_version=$schema_version, e.provenance='compiled_schema', "
            "e.updated_at=timestamp()"
        )
        params = {"tenant_id": tenant_id, "kb_id": kb_id, "data_source_id": data_source_id,
                  "table": table, "entity_name": entity_name,
                  "attributes": json.dumps(attributes or [], ensure_ascii=False),
                  "schema_version": schema_version}
        async with self._driver.session() as s:
            async def _write():
                # consume：确保错误在此刻抛出，transient 重试才覆盖到执行阶段。
                result = await s.run(cypher, **params)
                await result.consume()

            await _with_transient_retry(
                _write, what=f"merge_data_node({table})")

    async def merge_edge(self, tenant_id: str, kb_id: str, data_source_id: str,
                         from_table: str, to_table: str, column_map: list,
                         origin: str, confidence: float) -> None:
        tenant_id = require_tenant(tenant_id)
        # Neo4j 无法对关系做唯一约束；MERGE 模式必须带稳定属性键，
        # 否则同一端点对的并发投影会各建一条边（list_edges 重复计数）。
        pair_key = f"{from_table}->{to_table}"
        cypher = (
            "MATCH (a:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
            "data_source_id:$data_source_id, table:$from_table})\n"
            "MATCH (b:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
            "data_source_id:$data_source_id, table:$to_table})\n"
            "MERGE (a)-[r:DERIVED_FROM {pair_key:$pair_key}]->(b)\n"
            "SET r.column_map=$column_map, r.origin=$origin, "
            "r.confidence=$confidence, r.updated_at=timestamp()"
        )
        params = {"tenant_id": tenant_id, "kb_id": kb_id, "data_source_id": data_source_id,
                  "from_table": from_table, "to_table": to_table,
                  "pair_key": pair_key,
                  "column_map": json.dumps(column_map or [], ensure_ascii=False),
                  "origin": origin, "confidence": confidence}
        async with self._driver.session() as s:
            async def _write():
                result = await s.run(cypher, **params)
                await result.consume()

            await _with_transient_retry(
                _write, what=f"merge_data_edge({from_table}->{to_table})")

    async def rebuild_maps_to(self, tenant_id: str, kb_id: str,
                              desired: list[dict]) -> dict:
        """重建跨模态对齐边：KB 作用域内先清空 `MAPS_TO`，再按期望集重挂。

        `desired` 的每一项是 `{doc_type, data_source_id, table}`：文档实体
        `OntologyEntity.canonical_name` → 数据源表节点 `DataSourceEntity.table`。

        先删是**权威重建**的一部分：对齐行（PG）是权威、边是派生物，一条
        active 对齐被撤销/改表后，MERGE 只增不减，旧边只能靠这一步消失。
        空白业务键 MATCH 不到任何节点（MERGE 空转），直接跳过且不计数。

        返回值：`edges` 是重建后同作用域内**实际**的 `MAPS_TO` 边数（重建后
        重新统计，不是 MERGE 执行次数）；`merge_calls` 是执行过的 MERGE 次数；
        `matched_doc_types` 是重建后仍有边的文档实体 `canonical_name` 列表。
        """
        tenant_id = require_tenant(tenant_id)
        # 与 `merge_edge` 同款：关系唯一性只能靠 MERGE 模式里的稳定属性键。
        # 没有键时，同一端点对的并发投影会各建一条 `MAPS_TO`（计数重复、
        # 重建前一直挂着）；键由三元组拼成，语义唯一且与作用域一致。
        merge_cypher = (
            "MATCH (e:OntologyEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
            "canonical_name:$doc_type})\n"
            "MATCH (d:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
            "data_source_id:$data_source_id, table:$table})\n"
            "MERGE (e)-[r:MAPS_TO {pair_key:$pair_key}]->(d)\n"
            "SET r.updated_at=timestamp()"
        )
        merge_calls = 0
        async with self._driver.session() as s:
            async def _delete():
                result = await s.run(_DELETE_MAPS_TO_CYPHER,
                                     tenant_id=tenant_id, kb_id=kb_id)
                await result.consume()

            await _with_transient_retry(_delete, what="delete_maps_to_scope")

            for item in desired or []:
                doc_type = str((item or {}).get("doc_type") or "").strip()
                data_source_id = str(
                    (item or {}).get("data_source_id") or "").strip()
                table = str((item or {}).get("table") or "").strip()
                if not (doc_type and data_source_id and table):
                    continue
                params = {"tenant_id": tenant_id, "kb_id": kb_id,
                          "doc_type": doc_type,
                          "data_source_id": data_source_id, "table": table,
                          "pair_key": f"{doc_type}->{data_source_id}.{table}"}

                async def _merge(_params=params):
                    result = await s.run(merge_cypher, **_params)
                    await result.consume()

                await _with_transient_retry(
                    _merge, what=f"merge_maps_to({doc_type}->{table})")
                merge_calls += 1

            async def _count():
                result = await s.run(_COUNT_MAPS_TO_CYPHER,
                                     tenant_id=tenant_id, kb_id=kb_id)
                record = await result.single()
                return int(record["c"]) if record is not None else 0

            edges = await _with_transient_retry(
                _count, what="count_maps_to_scope")

            async def _matched_doc_types():
                result = await s.run(_MATCHED_DOC_TYPES_CYPHER,
                                     tenant_id=tenant_id, kb_id=kb_id)
                names = []
                async for record in result:
                    if int(record["c"] or 0) > 0:
                        names.append(str(record["name"] or ""))
                return names

            matched_doc_types = await _with_transient_retry(
                _matched_doc_types, what="matched_maps_to_doc_types")
        return {"edges": edges, "merge_calls": merge_calls,
                "matched_doc_types": matched_doc_types}

    async def count_maps_to(self, tenant_id: str, kb_id: str) -> int:
        """KB 作用域内当前 `MAPS_TO` 边数（无 active 对齐时快速跳过清旧的判据）。"""
        tenant_id = require_tenant(tenant_id)
        async with self._driver.session() as s:
            async def _read():
                result = await s.run(_COUNT_MAPS_TO_CYPHER,
                                     tenant_id=tenant_id, kb_id=kb_id)
                record = await result.single()
                return int(record["c"]) if record is not None else 0

            return await _with_transient_retry(
                _read, what="count_maps_to_scope")

    async def delete_edge(self, tenant_id: str, kb_id: str, data_source_id: str,
                          from_table: str, to_table: str) -> None:
        tenant_id = require_tenant(tenant_id)
        cypher = (
            "MATCH (:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
            "data_source_id:$data_source_id, table:$from_table})"
            "-[r:DERIVED_FROM]->"
            "(:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
            "data_source_id:$data_source_id, table:$to_table})\n"
            "DELETE r"
        )
        params = {"tenant_id": tenant_id, "kb_id": kb_id, "data_source_id": data_source_id,
                  "from_table": from_table, "to_table": to_table}
        async with self._driver.session() as s:
            async def _write():
                result = await s.run(cypher, **params)
                await result.consume()

            await _with_transient_retry(_write, what="delete_data_edge")

    async def delete_node(self, tenant_id: str, kb_id: str, data_source_id: str,
                          table: str) -> None:
        tenant_id = require_tenant(tenant_id)
        cypher = (
            "MATCH (e:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
            "data_source_id:$data_source_id, table:$table}) DETACH DELETE e"
        )
        params = {"tenant_id": tenant_id, "kb_id": kb_id,
                  "data_source_id": data_source_id, "table": table}
        async with self._driver.session() as s:
            async def _write():
                result = await s.run(cypher, **params)
                await result.consume()

            await _with_transient_retry(
                _write, what=f"delete_data_node({table})")

    async def list_nodes(self, tenant_id: str, kb_id: str, data_source_id: str) -> list[dict]:
        tenant_id = require_tenant(tenant_id)
        async with self._driver.session() as s:
            async def _read():
                r = await s.run(_NODES_CYPHER, tenant_id=tenant_id, kb_id=kb_id,
                                data_source_id=data_source_id)
                return [dict(x) async for x in r]
            rows = await _with_transient_retry(_read, what="list_data_nodes")
        return rows

    async def list_edges(self, tenant_id: str, kb_id: str, data_source_id: str) -> list[dict]:
        tenant_id = require_tenant(tenant_id)
        async with self._driver.session() as s:
            async def _read():
                r = await s.run(_EDGES_CYPHER, tenant_id=tenant_id, kb_id=kb_id,
                                data_source_id=data_source_id)
                return [dict(x) async for x in r]
            rows = await _with_transient_retry(_read, what="list_data_edges")
        return rows

    async def snapshot(self, tenant_id: str, kb_id: str,
                       data_source_id: str) -> dict:
        """一次会话读回该源的节点与边。

        `list_nodes` + `list_edges` 是两次独立会话，并发投影可能在两次读取
        之间改变图 ⇒ 调用方可能拿到撕裂视图（Task 1 评审 Low）。只读查询
        统一走这里；`list_nodes` / `list_edges` 保留给其它调用方。
        """
        tenant_id = require_tenant(tenant_id)

        async def _read():
            async with self._driver.session() as s:
                # 同一会话内串行执行两段读，不再分两次建会话。
                nodes_result = await s.run(
                    _NODES_CYPHER, tenant_id=tenant_id, kb_id=kb_id,
                    data_source_id=data_source_id)
                nodes = [dict(x) async for x in nodes_result]
                edges_result = await s.run(
                    _EDGES_CYPHER, tenant_id=tenant_id, kb_id=kb_id,
                    data_source_id=data_source_id)
                edges = [dict(x) async for x in edges_result]
            return {"nodes": nodes, "edges": edges}

        return await _with_transient_retry(_read, what="snapshot_data_assets")

    async def delete_scope(self, tenant_id: str, kb_id: str, data_source_id: str) -> None:
        """删除该数据源作用域内的全部边与节点。

        必须先删边、再删节点：顺序反了会先删掉节点连带边（或留下悬空边），
        正确性依赖这里的调用顺序。
        """
        tenant_id = require_tenant(tenant_id)
        async with self._driver.session() as s:
            async def _delete_edges():
                result = await s.run(
                    "MATCH (:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
                    "data_source_id:$data_source_id})-[r:DERIVED_FROM]->"
                    "(:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
                    "data_source_id:$data_source_id}) DELETE r",
                    tenant_id=tenant_id, kb_id=kb_id,
                    data_source_id=data_source_id)
                await result.consume()

            await _with_transient_retry(
                _delete_edges, what="delete_data_edges_scope")

            async def _delete_nodes():
                result = await s.run(
                    "MATCH (e:DataSourceEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
                    "data_source_id:$data_source_id}) DETACH DELETE e",
                    tenant_id=tenant_id, kb_id=kb_id,
                    data_source_id=data_source_id)
                await result.consume()

            await _with_transient_retry(
                _delete_nodes, what="delete_data_nodes_scope")

    # BFS 过滤在 service 层纯函数 `filter_neighbors` 里实现（Task 4）：
    # 仓储只负责一次性取快照，方向/跳数/unknown 语义不依赖 Neo4j。
