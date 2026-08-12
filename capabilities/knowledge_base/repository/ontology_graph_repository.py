#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Neo4j repository for ontology graph instances."""

import json
import logging
import os
from typing import Optional

from neo4j import AsyncDriver

from jonex_core.common.ontology_embedding import build_embed_text, embed, embed_hash
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

logger = logging.getLogger(__name__)


class OntologyGraphRepository:
    """Ontology graph storage backed by Neo4j."""

    def __init__(self, driver: AsyncDriver):
        self._driver = driver
        # P1 性能配套：单文档写入范围内缓存端点解析结果，避免每条关系两端各查一次库。
        # key=(kb_id, name, type_hint or "")；每文档写入前调 reset_endpoint_cache 清空。
        self._endpoint_cache: dict[tuple, tuple[str, str]] = {}

    def reset_endpoint_cache(self) -> None:
        """每文档写入前调用，避免跨文档/跨重跑命中过期解析结果。"""
        self._endpoint_cache.clear()

    async def merge_entity(self, tenant_id: str, kb_id: str, doc_id: str, entity: dict,
                          hash_cache: dict | None = None) -> None:
        tenant_id = require_tenant(tenant_id)

        # ── embedding 生成（仅非 stub 正式实体；幂等：文本未变跳过 embedding 调用）──
        embedding = None
        embedding_hash_val = None
        if os.getenv("ONTOLOGY_VECTOR_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
            text = build_embed_text(
                entity["canonical_name"], entity.get("aliases", []), entity.get("description", ""),
            )
            new_hash = embed_hash(text)
            key = (entity["entity_type"], entity["canonical_name"])
            if hash_cache is not None:
                existing_hash = hash_cache.get(key)
            else:
                existing_hash = await self._get_embedding_hash(tenant_id, kb_id, *key)
            if text and existing_hash != new_hash:
                # 入库路径透传 kb/doc + 稳定 trace（同一文档的本体 embedding 归组，
                # 避免 request_id 落成 auto: 兜底前缀）；聚合开启时 doc_id 进聚合键，可按文档归因。
                vec = await embed(
                    text, tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id,
                    trace_id=f"ontology_ingest:{doc_id}" if doc_id else None,
                )
                if vec is not None:
                    embedding = vec
                    embedding_hash_val = new_hash

        cypher = """
        MERGE (e:OntologyEntity {
            tenant_id:$tenant_id, kb_id:$kb_id,
            entity_type:$entity_type, canonical_name:$canonical_name
        })
        ON CREATE SET
            e.aliases=$aliases, e.aliases_text=$aliases_text,
            e.attributes=$attributes, e.confidence=$confidence,
            e.description=$description,
            e.doc_ids=[$doc_id], e.source_chunks=$source_chunks,
            e.lightrag_doc_ids=$lightrag_doc_ids, e.extraction_method=$extraction_method,
            e.embedding=$embedding, e.embedding_hash=$embedding_hash,
            e.stub=false, e.created_at=timestamp(), e.updated_at=timestamp()
        ON MATCH SET
            e.aliases=$aliases, e.aliases_text=$aliases_text, e.attributes=$attributes,
            e.description=CASE WHEN size($description) > size(coalesce(e.description,''))
                THEN $description ELSE e.description END,
            e.confidence=CASE WHEN $confidence>coalesce(e.confidence,0) THEN $confidence ELSE e.confidence END,
            e.doc_ids=apoc.coll.toSet(coalesce(e.doc_ids,[])+[$doc_id]),
            e.source_chunks=$source_chunks,
            e.lightrag_doc_ids=apoc.coll.toSet(coalesce(e.lightrag_doc_ids,[])+$lightrag_doc_ids),
            e.embedding=CASE WHEN $embedding IS NOT NULL THEN $embedding ELSE e.embedding END,
            e.embedding_hash=CASE WHEN $embedding IS NOT NULL THEN $embedding_hash ELSE e.embedding_hash END,
            e.stub=false, e.updated_at=timestamp()
        """
        params = {
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "doc_id": doc_id,
            "canonical_name": entity["canonical_name"],
            "entity_type": entity["entity_type"],
            "aliases": entity.get("aliases", []),
            "aliases_text": " ".join(entity.get("aliases", [])),
            "attributes": json.dumps(entity.get("attributes", {}), ensure_ascii=False),
            "description": entity.get("description", ""),
            "confidence": entity.get("confidence", 1.0),
            "source_chunks": json.dumps(entity.get("source_chunks", []), ensure_ascii=False),
            "lightrag_doc_ids": entity.get("lightrag_doc_ids", []),
            "extraction_method": entity.get("extraction_method", "llm_guided"),
            "embedding": embedding,
            "embedding_hash": embedding_hash_val,
        }
        async with self._driver.session() as session:
            await session.run(cypher, params)

    async def merge_relation(self, tenant_id: str, kb_id: str, doc_id: str, rel: dict) -> None:
        tenant_id = require_tenant(tenant_id)
        use_endpoint_resolve = os.getenv("ONTOLOGY_REL_ENDPOINT_RESOLVE", "true").lower() in ("1", "true", "yes", "on")
        if use_endpoint_resolve:
            src_type, src_name = await self._resolve_endpoint(
                tenant_id, kb_id, rel["source_name"], rel.get("source_type"))
            tgt_type, tgt_name = await self._resolve_endpoint(
                tenant_id, kb_id, rel["target_name"], rel.get("target_type"))
        else:
            src_type = await self._resolve_entity_type(
                tenant_id, kb_id, rel["source_name"], rel.get("source_type"))
            tgt_type = await self._resolve_entity_type(
                tenant_id, kb_id, rel["target_name"], rel.get("target_type"))
            src_name = rel["source_name"]
            tgt_name = rel["target_name"]
        # [jonex] 方案⑧：从 rel dict 取关系的 source_chunks，用于 ONT_REL 边出处 + stub 端点兜底
        # Neo4j 不允许 list-of-map 作为属性值，以 JSON string 存储（与 merge_entity 口径一致）
        rel_source_chunks = rel.get("source_chunks") or []
        cypher = """
        MERGE (s:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$src_type, canonical_name:$src_name})
        ON CREATE SET s.stub=true, s.confidence=0.1, s.aliases=[], s.aliases_text='',
            s.attributes='{}',
            s.source_chunks=apoc.convert.toJson($rel_source_chunks),
            s.lightrag_doc_ids=[],
            s.extraction_method='stub', s.doc_ids=[$doc_id], s.created_at=timestamp(), s.updated_at=timestamp()
        MERGE (o:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$tgt_type, canonical_name:$tgt_name})
        ON CREATE SET o.stub=true, o.confidence=0.1, o.aliases=[], o.aliases_text='',
            o.attributes='{}',
            o.source_chunks=apoc.convert.toJson($rel_source_chunks),
            o.lightrag_doc_ids=[],
            o.extraction_method='stub', o.doc_ids=[$doc_id], o.created_at=timestamp(), o.updated_at=timestamp()
        MERGE (s)-[r:ONT_REL {relation_type:$rel_type}]->(o)
        ON CREATE SET r.confidence=$confidence, r.attributes=$attributes,
            r.lightrag_doc_ids=$lightrag_doc_ids, r.doc_ids=[$doc_id],
            r.source_chunks=apoc.convert.toJson($rel_source_chunks),
            r.created_at=timestamp(), r.updated_at=timestamp()
        ON MATCH SET r.doc_ids=apoc.coll.toSet(coalesce(r.doc_ids,[])+[$doc_id]),
            r.lightrag_doc_ids=apoc.coll.toSet(coalesce(r.lightrag_doc_ids,[])+$lightrag_doc_ids),
            r.source_chunks=apoc.convert.toJson(
                apoc.coll.toSet(
                    apoc.convert.fromJsonList(coalesce(r.source_chunks,'[]'))+$rel_source_chunks
                )
            ),
            r.attributes=$attributes,
            r.confidence=CASE WHEN $confidence>coalesce(r.confidence,0) THEN $confidence ELSE r.confidence END,
            r.updated_at=timestamp()
        """
        params = {
            "t": tenant_id,
            "k": kb_id,
            "doc_id": doc_id,
            "src_type": src_type,
            "src_name": src_name,
            "tgt_type": tgt_type,
            "tgt_name": tgt_name,
            "rel_type": rel["relation_type"],
            "confidence": rel.get("confidence", 1.0),
            "attributes": json.dumps(rel.get("attributes", {}), ensure_ascii=False),
            "lightrag_doc_ids": rel.get("lightrag_doc_ids", []),
            "rel_source_chunks": rel_source_chunks,   # [jonex] 方案⑧
        }
        async with self._driver.session() as session:
            await session.run(cypher, params)

    async def _resolve_entity_type(
        self,
        tenant_id: str,
        kb_id: str,
        name: str,
        type_hint: Optional[str],
    ) -> str:
        tenant_id = require_tenant(tenant_id)
        if type_hint:
            return type_hint
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, canonical_name:$name})
        RETURN e.entity_type AS entity_type
        ORDER BY coalesce(e.stub,false) ASC
        LIMIT 1
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "k": kb_id, "name": name})
            record = await result.single()
            return record["entity_type"] if record else "Unknown"

    async def _resolve_endpoint(
        self,
        tenant_id: str,
        kb_id: str,
        name: str,
        type_hint: Optional[str],
    ) -> tuple[str, str]:
        """解析关系端点到正式实体，返回 (entity_type, canonical_name)。

        优先 canonical_name 精确命中正式节点；次选 name 命中某正式节点的 aliases；
        命中则用该正式节点的 (entity_type, canonical_name) 对齐端点，避免捏造 stub；
        未命中则回退 (type_hint or 'Unknown', name)，沿用既有 stub 行为。

        匹配优先级（降低别名跨类型误匹配）：
          1) canonical_name 精确命中；
          2) 有 type_hint 时优先命中该类型；
          3) 再按 confidence 降序取一条。
        """
        tenant_id = require_tenant(tenant_id)
        hint = type_hint or None
        cache_key = (kb_id, name, hint or "")
        if cache_key in self._endpoint_cache:
            return self._endpoint_cache[cache_key]
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(e.stub,false)=false
          AND (e.canonical_name=$name OR $name IN coalesce(e.aliases,[]))
          AND ($hint IS NULL OR e.canonical_name=$name OR e.entity_type=$hint)
        RETURN e.entity_type AS et, e.canonical_name AS cn
        ORDER BY (CASE WHEN e.canonical_name=$name THEN 0 ELSE 1 END),
                 (CASE WHEN $hint IS NOT NULL AND e.entity_type=$hint THEN 0 ELSE 1 END),
                 coalesce(e.confidence,0) DESC
        LIMIT 1
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "k": kb_id, "name": name, "hint": hint})
            record = await result.single()
            if record:
                resolved = (record["et"], record["cn"])
            else:
                resolved = (type_hint or "Unknown"), name
        self._endpoint_cache[cache_key] = resolved
        return resolved

    async def delete_by_document(self, tenant_id: str, doc_id: str) -> None:
        """移除某文档对本体图谱的贡献：从实体/关系的 doc_ids 摘除该 doc，
        并清理因此变为孤立（无 doc 归属且无边）的实体/关系。

        注意：Neo4j 单次 session.run 只接受一条语句，故拆成多条按序执行
        （原先用 ';' 拼多语句会被服务端拒绝）。
        """
        tenant_id = require_tenant(tenant_id)
        async with self._driver.session() as session:
            # 1) 关系：摘除该 doc_id
            await session.run(
                """
                MATCH (:OntologyEntity {tenant_id:$t})-[r:ONT_REL]->(:OntologyEntity {tenant_id:$t})
                WHERE $doc_id IN r.doc_ids
                SET r.doc_ids = [x IN r.doc_ids WHERE x <> $doc_id]
                """,
                {"t": tenant_id, "doc_id": doc_id},
            )
            # 2) 关系：doc_ids 清空者删除
            await session.run(
                """
                MATCH (:OntologyEntity {tenant_id:$t})-[r:ONT_REL]->(:OntologyEntity {tenant_id:$t})
                WHERE size(coalesce(r.doc_ids,[]))=0
                DELETE r
                """,
                {"t": tenant_id},
            )
            # 3) 实体：摘除该 doc_id
            await session.run(
                """
                MATCH (e:OntologyEntity {tenant_id:$t})
                WHERE $doc_id IN e.doc_ids
                SET e.doc_ids = [x IN e.doc_ids WHERE x <> $doc_id]
                """,
                {"t": tenant_id, "doc_id": doc_id},
            )
            # 4) 实体：无 doc 归属且无边者删除
            await session.run(
                """
                MATCH (e:OntologyEntity {tenant_id:$t})
                WHERE size(coalesce(e.doc_ids,[]))=0 AND NOT (e)--()
                DELETE e
                """,
                {"t": tenant_id},
            )

    async def search_entities(self, tenant_id: str, kb_ids: list[str], query: str, limit: int = 5) -> list[dict]:
        tenant_id = require_tenant(tenant_id)
        cypher = """
        CALL db.index.fulltext.queryNodes('ont_entity_ft', $q) YIELD node, score
        WHERE node.tenant_id=$t AND node.kb_id IN $kbs AND coalesce(node.stub,false)=false
        RETURN node.canonical_name AS name, node.entity_type AS type,
               node.aliases AS aliases, node.attributes AS attributes,
               node.description AS description,
               node.confidence AS confidence, node.kb_id AS kb_id,
               node.doc_ids AS doc_ids, node.source_chunks AS source_chunks, score
        ORDER BY score DESC LIMIT $limit
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "kbs": kb_ids, "q": query, "limit": limit})
            rows = [dict(record) async for record in result]
        for row in rows:
            if isinstance(row.get("attributes"), str):
                try:
                    row["attributes"] = json.loads(row["attributes"])
                except (json.JSONDecodeError, TypeError):
                    row["attributes"] = {}
            if isinstance(row.get("aliases"), str):
                try:
                    row["aliases"] = json.loads(row["aliases"])
                except (json.JSONDecodeError, TypeError):
                    row["aliases"] = []
            if isinstance(row.get("source_chunks"), str):
                try:
                    row["source_chunks"] = json.loads(row["source_chunks"])
                except (json.JSONDecodeError, TypeError):
                    row["source_chunks"] = []
        return rows

    async def neighbors(
        self,
        tenant_id: str,
        kb_id: str,
        entity_name: str,
        limit: int = 20,
        depth: int = 1,
        per_hop_limit: int = 50,
    ) -> dict:
        """多跳邻域取证（可配深度 1~N）。

        depth==1 仍走定长一跳 Cypher（性能零回归），但 RETURN 补齐目标实体完整内容；
        depth>1 走变长路径，按跳数分层 + 确定性排序 + 总量截断。
        两条分支的事实结构完全一致，每条 fact 含 target_entity/hop/relation_chain/path。
        """
        tenant_id = require_tenant(tenant_id)

        if depth <= 1:
            cypher = """
            MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, canonical_name:$name})
            MATCH (e)-[r:ONT_REL]-(n:OntologyEntity {tenant_id:$t, kb_id:$k})
            WHERE coalesce(n.stub,false)=false
            WITH e, r, n, CASE WHEN startNode(r) = e THEN 'outgoing' ELSE 'incoming' END AS rel_dir
            ORDER BY coalesce(n.confidence,0) DESC, n.canonical_name ASC
            RETURN e.canonical_name AS source,
                   collect({
                       source: e.canonical_name,
                       relation_type: r.relation_type,
                       direction: rel_dir,
                       target: n.canonical_name,
                       target_type: n.entity_type,
                       target_entity: {
                           name: n.canonical_name, type: n.entity_type,
                           aliases: n.aliases, description: n.description,
                           attributes: n.attributes, confidence: n.confidence,
                           kb_id: n.kb_id, doc_ids: n.doc_ids,
                           source_chunks: n.source_chunks
                       },
                       relation_source_chunks: r.source_chunks,
                       hop: 1,
                       relation_chain: [r.relation_type],
                       path: [e.canonical_name, n.canonical_name]
                   })[..$limit] AS facts
            """
        else:
            cypher = """
            MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, canonical_name:$name})
            MATCH path = (e)-[rels:ONT_REL*1..$depth]-(n:OntologyEntity {tenant_id:$t, kb_id:$k})
            WHERE coalesce(n.stub,false)=false
            WITH e, n, rels, length(path) AS hop,
                 [r IN rels | r.relation_type] AS rel_types,
                 rels[-1] AS last_rel,
                 nodes(path) AS path_nodes
            WITH e, n, hop, rel_types, last_rel, path_nodes,
                 CASE WHEN startNode(last_rel) = path_nodes[hop-1] THEN 'outgoing' ELSE 'incoming' END AS rel_dir
            WITH e, n, rel_dir, rel_types,
                 [pn IN path_nodes | pn.canonical_name] AS path_names,
                 last_rel.source_chunks AS relation_source_chunks,
                 min(hop) AS hop
            ORDER BY hop ASC, coalesce(n.confidence,0) DESC, n.canonical_name ASC
            WITH e, collect({
                    source: e.canonical_name,
                    target: n.canonical_name,
                    target_type: n.entity_type,
                    target_entity: {
                        name: n.canonical_name, type: n.entity_type,
                        aliases: n.aliases, description: n.description,
                        attributes: n.attributes, confidence: n.confidence,
                        kb_id: n.kb_id, doc_ids: n.doc_ids,
                        source_chunks: n.source_chunks
                    },
                    relation_type: rel_types[-1],
                    relation_chain: rel_types,
                    relation_source_chunks: relation_source_chunks,
                    path: path_names,
                    direction: rel_dir,
                    hop: hop
                 })[..$limit] AS facts
            RETURN e.canonical_name AS source, facts
            """
            # Neo4j 变长路径上界不支持参数绑定（`*1..$depth` 会报
            # Neo.ClientError.Statement.SyntaxError），必须内联字面量整数。
            # depth 由调用方 clamp 到 1~MAX，此处再强转 int 兜底，无注入风险。
            cypher = cypher.replace("$depth", str(max(2, int(depth))))

        async with self._driver.session() as session:
            result = await session.run(
                cypher,
                {"t": tenant_id, "k": kb_id, "name": entity_name, "limit": limit, "depth": depth},
            )
            record = await result.single()
            data = dict(record) if record else {"source": entity_name, "facts": []}

        facts: list[dict] = data.get("facts", [])

        # ── 反序列化 target_entity.attributes/aliases（一跳/多跳共用）──
        self._deserialize_fact_entities(facts)

        # ── 应用侧分层裁剪 + 统计 ──
        truncated = False
        if depth > 1 and per_hop_limit:
            hop_groups: dict[int, list[dict]] = {}
            for f in facts:
                h = f.get("hop", 1)
                hop_groups.setdefault(h, []).append(f)

            trimmed: list[dict] = []
            for h in sorted(hop_groups.keys()):
                group = hop_groups[h]
                if len(group) > per_hop_limit:
                    truncated = True
                    group = group[:per_hop_limit]
                trimmed.extend(group)

            if len(trimmed) > limit:
                truncated = True
                facts = trimmed[:limit]
            else:
                facts = trimmed
        elif len(facts) > limit:
            truncated = True
            facts = facts[:limit]

        # 各跳事实数分布
        hop_distribution: dict[int, int] = {}
        for f in facts:
            h = f.get("hop", 1)
            hop_distribution[h] = hop_distribution.get(h, 0) + 1

        return {
            "source": data.get("source", entity_name),
            "facts": facts,
            "depth": depth,
            "hop_distribution": hop_distribution,
            "truncated": truncated,
        }

    async def exact_match_entities(self, tenant_id: str, kb_ids: list[str], name: str) -> list[dict]:
        """精确匹配 canonical_name 或 aliases（多 KB），返回列表（同名实体在不同 KB 各命中一条）。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t})
        WHERE e.kb_id IN $kbs AND coalesce(e.stub,false)=false
          AND (e.canonical_name=$name OR $name IN coalesce(e.aliases,[]))
        RETURN e.canonical_name AS name, e.entity_type AS type,
               e.aliases AS aliases, e.attributes AS attributes,
               e.description AS description, e.confidence AS confidence,
               e.kb_id AS kb_id, e.doc_ids AS doc_ids, e.source_chunks AS source_chunks, 1.0 AS score
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "kbs": kb_ids, "name": name})
            rows = [dict(record) async for record in result]
        for row in rows:
            if isinstance(row.get("attributes"), str):
                try:
                    row["attributes"] = json.loads(row["attributes"])
                except (json.JSONDecodeError, TypeError):
                    row["attributes"] = {}
            if isinstance(row.get("aliases"), str):
                try:
                    row["aliases"] = json.loads(row["aliases"])
                except (json.JSONDecodeError, TypeError):
                    row["aliases"] = []
            if isinstance(row.get("source_chunks"), str):
                try:
                    row["source_chunks"] = json.loads(row["source_chunks"])
                except (json.JSONDecodeError, TypeError):
                    row["source_chunks"] = []
        return rows

    async def prefix_match_entities(self, tenant_id: str, kb_ids: list[str], prefix: str, limit: int = 5) -> list[dict]:
        """canonical_name 前缀匹配（多 KB），精确匹配的兜底，处理"研发"→"研发流程"这类场景。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t})
        WHERE e.kb_id IN $kbs AND e.canonical_name STARTS WITH $prefix AND coalesce(e.stub,false)=false
        RETURN e.canonical_name AS name, e.entity_type AS type,
               e.aliases AS aliases, e.attributes AS attributes,
               e.description AS description, e.confidence AS confidence,
               e.kb_id AS kb_id, e.doc_ids AS doc_ids, e.source_chunks AS source_chunks, 1.0 AS score
        ORDER BY size(e.canonical_name) ASC
        LIMIT $limit
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "kbs": kb_ids, "prefix": prefix, "limit": limit})
            rows = [dict(record) async for record in result]
        for row in rows:
            if isinstance(row.get("attributes"), str):
                try:
                    row["attributes"] = json.loads(row["attributes"])
                except (json.JSONDecodeError, TypeError):
                    row["attributes"] = {}
            if isinstance(row.get("aliases"), str):
                try:
                    row["aliases"] = json.loads(row["aliases"])
                except (json.JSONDecodeError, TypeError):
                    row["aliases"] = []
            if isinstance(row.get("source_chunks"), str):
                try:
                    row["source_chunks"] = json.loads(row["source_chunks"])
                except (json.JSONDecodeError, TypeError):
                    row["source_chunks"] = []
        return rows


    async def count_entities(self, tenant_id: str, kb_id: str) -> int:
        """统计 kb 内非 stub 的本体实例数量。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(e.stub,false)=false
        RETURN count(e) AS c
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "k": kb_id})
            record = await result.single()
            return record["c"] if record else 0

    async def count_relations(self, tenant_id: str, kb_id: str) -> int:
        """统计 kb 内本体关系数量（两端均为非 stub 正式实体）。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (s:OntologyEntity {tenant_id:$t, kb_id:$k})-[r:ONT_REL]->(o:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(s.stub,false)=false AND coalesce(o.stub,false)=false
        RETURN count(r) AS c
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "k": kb_id})
            record = await result.single()
            return record["c"] if record else 0

    async def count_entities_by_type(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """按 entity_type 聚合非 stub 实例数，返回 {entity_type: count}。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(e.stub,false)=false
        RETURN e.entity_type AS type, count(e) AS c
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "k": kb_id})
            return {rec["type"]: rec["c"] async for rec in result}

    async def count_relations_by_type(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """按 relation_type 聚合关系数（两端均非 stub），返回 {relation_type: count}。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (s:OntologyEntity {tenant_id:$t, kb_id:$k})-[r:ONT_REL]->(o:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(s.stub,false)=false AND coalesce(o.stub,false)=false
        RETURN r.relation_type AS type, count(r) AS c
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "k": kb_id})
            return {rec["type"]: rec["c"] async for rec in result}

    async def list_entities(
        self,
        tenant_id: str,
        kb_id: str,
        offset: int = 0,
        limit: int = 20,
        entity_type: Optional[str] = None,
        keyword: Optional[str] = None,
        include_unknown: bool = True,
        document_id: Optional[str] = None,
    ) -> tuple[list[dict], int]:
        """分页查询 kb 内本体实例列表，支持按类型、关键词和文档过滤。"""
        tenant_id = require_tenant(tenant_id)
        where = ["coalesce(e.stub,false)=false"]
        params = {"t": tenant_id, "k": kb_id, "offset": offset, "limit": limit}
        if document_id:
            where.append("$doc_id IN e.doc_ids")
            params["doc_id"] = document_id
        if entity_type:
            where.append("e.entity_type=$etype")
            params["etype"] = entity_type
        elif not include_unknown:
            # 仅在未显式指定 entity_type 时生效：剔除未归类(unknown，含 P2C 兜底端点)
            where.append("e.entity_type <> 'unknown'")
        if keyword:
            where.append(
                "(toLower(e.canonical_name) CONTAINS toLower($kw) "
                "OR toLower(e.aliases_text) CONTAINS toLower($kw))"
            )
            params["kw"] = keyword
        where_clause = " AND ".join(where)

        count_cypher = f"""
        MATCH (e:OntologyEntity {{tenant_id:$t, kb_id:$k}})
        WHERE {where_clause}
        RETURN count(e) AS c
        """
        page_cypher = f"""
        MATCH (e:OntologyEntity {{tenant_id:$t, kb_id:$k}})
        WHERE {where_clause}
        RETURN e.canonical_name AS name, e.entity_type AS type,
               e.aliases AS aliases, e.attributes AS attributes,
               e.description AS description, e.confidence AS confidence,
               e.doc_ids AS doc_ids
        ORDER BY e.updated_at DESC, elementId(e)
        SKIP $offset LIMIT $limit
        """
        async with self._driver.session() as session:
            total_rec = await (await session.run(count_cypher, params)).single()
            total = total_rec["c"] if total_rec else 0
            result = await session.run(page_cypher, params)
            rows = [dict(rec) async for rec in result]
        for row in rows:
            if isinstance(row.get("attributes"), str):
                try:
                    row["attributes"] = json.loads(row["attributes"])
                except (json.JSONDecodeError, TypeError):
                    row["attributes"] = {}
            if isinstance(row.get("aliases"), str):
                try:
                    row["aliases"] = json.loads(row["aliases"])
                except (json.JSONDecodeError, TypeError):
                    row["aliases"] = []
        return rows, total

    async def list_relations(
        self,
        tenant_id: str,
        kb_id: str,
        offset: int = 0,
        limit: int = 20,
        relation_type: Optional[str] = None,
        source_name: Optional[str] = None,
        target_name: Optional[str] = None,
        source_type: Optional[str] = None,
        target_type: Optional[str] = None,
        keyword: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> tuple[list[dict], int]:
        """分页查询 kb 内本体关系列表，两端节点均在 kb 内，支持按文档过滤。"""
        tenant_id = require_tenant(tenant_id)
        where = ["coalesce(s.stub,false)=false", "coalesce(o.stub,false)=false"]
        params = {"t": tenant_id, "k": kb_id, "offset": offset, "limit": limit}
        if document_id:
            where.append("$doc_id IN r.doc_ids")
            params["doc_id"] = document_id
        if relation_type:
            where.append("r.relation_type=$rtype")
            params["rtype"] = relation_type
        if source_name:
            where.append("s.canonical_name=$sname")
            params["sname"] = source_name
        if target_name:
            where.append("o.canonical_name=$tname")
            params["tname"] = target_name
        if source_type:
            where.append("s.entity_type=$stype")
            params["stype"] = source_type
        if target_type:
            where.append("o.entity_type=$ttype")
            params["ttype"] = target_type
        if keyword:
            where.append(
                "(toLower(r.relation_type) CONTAINS toLower($kw) "
                "OR toLower(s.canonical_name) CONTAINS toLower($kw) "
                "OR toLower(o.canonical_name) CONTAINS toLower($kw))"
            )
            params["kw"] = keyword
        where_clause = ("WHERE " + " AND ".join(where)) if where else ""

        match_clause = (
            "MATCH (s:OntologyEntity {tenant_id:$t, kb_id:$k})"
            "-[r:ONT_REL]->"
            "(o:OntologyEntity {tenant_id:$t, kb_id:$k})"
        )
        count_cypher = f"{match_clause} {where_clause} RETURN count(r) AS c"
        page_cypher = f"""
        {match_clause}
        {where_clause}
        RETURN s.canonical_name AS source, s.entity_type AS source_type,
               r.relation_type AS relation_type, r.confidence AS confidence,
               r.attributes AS attributes,
               o.canonical_name AS target, o.entity_type AS target_type
        ORDER BY r.created_at DESC, elementId(r)
        SKIP $offset LIMIT $limit
        """
        async with self._driver.session() as session:
            total_rec = await (await session.run(count_cypher, params)).single()
            total = total_rec["c"] if total_rec else 0
            result = await session.run(page_cypher, params)
            rows = [dict(rec) async for rec in result]
        for row in rows:
            if isinstance(row.get("attributes"), str):
                try:
                    row["attributes"] = json.loads(row["attributes"])
                except (json.JSONDecodeError, TypeError):
                    row["attributes"] = {}
        return rows, total


    @staticmethod
    def _deserialize_attrs(rows: list[dict]) -> None:
        """就地把 rows 里的 attributes/aliases/source_chunks 字符串反序列化为 dict/list。"""
        for row in rows:
            if isinstance(row.get("attributes"), str):
                try:
                    row["attributes"] = json.loads(row["attributes"])
                except (json.JSONDecodeError, TypeError):
                    row["attributes"] = {}
            if isinstance(row.get("aliases"), str):
                try:
                    row["aliases"] = json.loads(row["aliases"])
                except (json.JSONDecodeError, TypeError):
                    row["aliases"] = []
            if isinstance(row.get("source_chunks"), str):
                try:
                    row["source_chunks"] = json.loads(row["source_chunks"])
                except (json.JSONDecodeError, TypeError):
                    row["source_chunks"] = []

    @staticmethod
    def _deserialize_fact_entities(facts: list[dict]) -> None:
        """就地把每条 fact 的 target_entity.attributes/aliases/source_chunks 反序列化。

        注：仅 attributes/aliases/source_chunks 在 Neo4j 内以 JSON 字符串存储需反序列化；
        doc_ids 为 Neo4j 原生数组、confidence 为原生 float，Python driver 已自动转换为
        list/float，无需在此处理（与 exact_match_entities/list_entities 反序列化口径一致）。
        """
        for f in facts:
            ent = f.get("target_entity")
            if not isinstance(ent, dict):
                continue
            if isinstance(ent.get("attributes"), str):
                try:
                    ent["attributes"] = json.loads(ent["attributes"])
                except (json.JSONDecodeError, TypeError):
                    ent["attributes"] = {}
            if isinstance(ent.get("aliases"), str):
                try:
                    ent["aliases"] = json.loads(ent["aliases"])
                except (json.JSONDecodeError, TypeError):
                    ent["aliases"] = []
            if isinstance(ent.get("source_chunks"), str):
                try:
                    ent["source_chunks"] = json.loads(ent["source_chunks"])
                except (json.JSONDecodeError, TypeError):
                    ent["source_chunks"] = []
            # [jonex] 方案⑧：relation_source_chunks 在 fact 顶层，以 JSON 字符串存储
            if isinstance(f.get("relation_source_chunks"), str):
                try:
                    f["relation_source_chunks"] = json.loads(f["relation_source_chunks"])
                except (json.JSONDecodeError, TypeError):
                    f["relation_source_chunks"] = []

    # ══════════════════════════════════════════════════════════════════
    # P1-6 图查询模板：时间线 / 枚举 / 计数
    # ══════════════════════════════════════════════════════════════════

    async def find_release_supporting_toolkit(
        self, tenant_id: str, kb_id: str, toolkit_version: str,
    ) -> list[dict]:
        """反查：哪个 SoftwareRelease 支持指定 ToolKit 版本。

        对应问题："哪个版本支持 CUDA 12.8"
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (t:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareToolkit'})
        WHERE coalesce(t.stub,false)=false AND t.canonical_name CONTAINS $toolkit_version
        MATCH (sr:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareRelease'})
              -[r:ONT_REL {relation_type:'SUPPORTS_TOOLKIT'}]->(t)
        WHERE coalesce(sr.stub,false)=false
        RETURN sr.canonical_name AS name, sr.entity_type AS type,
               sr.aliases AS aliases, sr.attributes AS attributes,
               sr.description AS description, sr.confidence AS confidence,
               sr.kb_id AS kb_id, sr.doc_ids AS doc_ids,
               sr.source_chunks AS source_chunks,
               t.canonical_name AS toolkit_name,
               r.relation_type AS relation_type
        ORDER BY sr.canonical_name DESC
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher, {"t": tenant_id, "k": kb_id, "toolkit_version": toolkit_version},
            )
            rows = [dict(r) async for r in result]
        self._deserialize_attrs(rows)
        return rows

    async def find_earliest_release_for(
        self, tenant_id: str, kb_id: str, relation_types: list[str], target_name: str,
    ) -> dict | None:
        """找最早满足关系的 SoftwareRelease（沿 SUPERSEDES 链取最旧端）。

        链方向：新版本 --[:SUPERSEDES]-> 旧版本。
        因此"最早"= 无 outgoing SUPERSEDES 的节点（链尾，最旧）。
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (sr:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareRelease'})
              -[r:ONT_REL]->(target:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(sr.stub,false)=false AND coalesce(target.stub,false)=false
          AND r.relation_type IN $rel_types AND target.canonical_name = $target_name
        WITH collect(DISTINCT sr) AS candidates
        UNWIND candidates AS sr
        OPTIONAL MATCH (sr)-[:ONT_REL {relation_type:'SUPERSEDES'}]->(older:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareRelease'})
        WHERE older IN candidates
        WITH sr, older WHERE older IS NULL
        RETURN sr.canonical_name AS name, sr.entity_type AS type,
               sr.aliases AS aliases, sr.attributes AS attributes,
               sr.description AS description, sr.confidence AS confidence,
               sr.kb_id AS kb_id, sr.doc_ids AS doc_ids,
               sr.source_chunks AS source_chunks
        LIMIT 1
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher,
                {"t": tenant_id, "k": kb_id, "rel_types": relation_types, "target_name": target_name},
            )
            record = await result.single()
        if record:
            row = dict(record)
            self._deserialize_attrs([row])
            return row
        return None

    async def find_latest_release_for(
        self, tenant_id: str, kb_id: str, relation_types: list[str], target_name: str,
    ) -> dict | None:
        """找最新满足关系的 SoftwareRelease（沿 SUPERSEDES 链取最新端）。

        链方向：新版本 --[:SUPERSEDES]-> 旧版本。
        因此"最新"= 无 incoming SUPERSEDES 的节点（链首，最新）。
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (sr:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareRelease'})
              -[r:ONT_REL]->(target:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(sr.stub,false)=false AND coalesce(target.stub,false)=false
          AND r.relation_type IN $rel_types AND target.canonical_name = $target_name
        WITH collect(DISTINCT sr) AS candidates
        UNWIND candidates AS sr
        OPTIONAL MATCH (newer:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareRelease'})
                       -[:ONT_REL {relation_type:'SUPERSEDES'}]->(sr)
        WHERE newer IN candidates
        WITH sr, newer WHERE newer IS NULL
        RETURN sr.canonical_name AS name, sr.entity_type AS type,
               sr.aliases AS aliases, sr.attributes AS attributes,
               sr.description AS description, sr.confidence AS confidence,
               sr.kb_id AS kb_id, sr.doc_ids AS doc_ids,
               sr.source_chunks AS source_chunks
        LIMIT 1
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher,
                {"t": tenant_id, "k": kb_id, "rel_types": relation_types, "target_name": target_name},
            )
            record = await result.single()
        if record:
            row = dict(record)
            self._deserialize_attrs([row])
            return row
        return None

    async def find_last_supporting_before_drop(
        self, tenant_id: str, kb_id: str, software_name: str,
    ) -> dict | None:
        """找「最后支持某 Software 的版本」。

        策略：找到执行 DROPPED_SUPPORT_FOR 的版本，沿 SUPERSEDES 链反向
        （即取该版本的前驱），即为"最后支持"的版本。

        对应问题："最后支持 VS2019 的版本"
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (drop:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareRelease'})
              -[:ONT_REL {relation_type:'DROPPED_SUPPORT_FOR'}]->(sw:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(drop.stub,false)=false AND coalesce(sw.stub,false)=false
          AND sw.canonical_name = $sw_name
        MATCH (last_support:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareRelease'})
              -[:ONT_REL {relation_type:'SUPERSEDES'}]->(drop)
        WHERE coalesce(last_support.stub,false)=false
        RETURN last_support.canonical_name AS name, last_support.entity_type AS type,
               last_support.aliases AS aliases, last_support.attributes AS attributes,
               last_support.description AS description, last_support.confidence AS confidence,
               last_support.kb_id AS kb_id, last_support.doc_ids AS doc_ids,
               last_support.source_chunks AS source_chunks,
               drop.canonical_name AS dropped_by
        LIMIT 1
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher, {"t": tenant_id, "k": kb_id, "sw_name": software_name},
            )
            record = await result.single()
        if record:
            row = dict(record)
            self._deserialize_attrs([row])
            return row
        return None

    async def find_first_release_adding(
        self, tenant_id: str, kb_id: str, gpu_or_arch: str,
    ) -> dict | None:
        """找首次加入某 GPU 架构支持的 SoftwareRelease。

        先查所有有 ADDED_SUPPORT_FOR 边的版本，再取其中最早的那个
        （沿 SUPERSEDES 链取最旧端）。

        对应问题："首次支持 GH100 的版本"
        """
        return await self.find_earliest_release_for(
            tenant_id, kb_id,
            relation_types=["ADDED_SUPPORT_FOR"],
            target_name=gpu_or_arch,
        )

    async def count_toolkit_versions(
        self, tenant_id: str, kb_id: str, version_lo: str = "", version_hi: str = "",
    ) -> int:
        """统计 SoftwareToolkit 实体数量（支持版本号范围过滤，数值化比较）。

        因为 canonical_name 格式如 "CUDA Toolkit 12.8"，而 lo/hi 是裸版本号
        "12.4"/"13.3"，不能直接做字符串比较（前缀不同 + 字典序 "12.10" < "12.9" 错误）。
        改为：提取 canonical_name 最后的空格分隔 token 作为版本号 →
        按 '.' 拆分转换为整数元组做数值化区间比较。

        对应问题："12.4→13.3 共几个 CUDA 版本"
        """
        tenant_id = require_tenant(tenant_id)
        params: dict = {"t": tenant_id, "k": kb_id}

        # 在 Python 侧预解析 lo/hi 为数值对，传给 Cypher 做逐位数值比较
        def _parse_ver(v: str) -> tuple[int, int] | None:
            parts = v.strip().split(".")
            if len(parts) >= 2:
                try:
                    return (int(parts[0]), int(parts[1]))
                except ValueError:
                    return None
            return None

        lo_pair = _parse_ver(version_lo) if version_lo else None
        hi_pair = _parse_ver(version_hi) if version_hi else None

        # 基础 Cypher：提取版本号 → 拆分为整数元组
        cypher = """
        MATCH (t:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareToolkit'})
        WHERE coalesce(t.stub,false)=false
        WITH t, split(t.canonical_name, ' ')[-1] AS version_str
        WITH t, [x IN split(version_str, '.') | toInteger(x)] AS v
        WHERE size(v) >= 2
        """

        # 版本号区间过滤（数值化比较）
        if lo_pair and hi_pair:
            cypher += """
            AND (
              v[0] > $lo_major OR (v[0] = $lo_major AND v[1] >= $lo_minor)
            )
            AND (
              v[0] < $hi_major OR (v[0] = $hi_major AND v[1] <= $hi_minor)
            )
            """
            params["lo_major"] = lo_pair[0]
            params["lo_minor"] = lo_pair[1]
            params["hi_major"] = hi_pair[0]
            params["hi_minor"] = hi_pair[1]
        elif lo_pair:
            cypher += """
            AND (v[0] > $lo_major OR (v[0] = $lo_major AND v[1] >= $lo_minor))
            """
            params["lo_major"] = lo_pair[0]
            params["lo_minor"] = lo_pair[1]
        elif hi_pair:
            cypher += """
            AND (v[0] < $hi_major OR (v[0] = $hi_major AND v[1] <= $hi_minor))
            """
            params["hi_major"] = hi_pair[0]
            params["hi_minor"] = hi_pair[1]

        cypher += "\nRETURN count(t) AS c"

        async with self._driver.session() as session:
            result = await session.run(cypher, params)
            record = await result.single()
            return record["c"] if record else 0

    async def get_toolkit_release_year(
        self, tenant_id: str, kb_id: str, toolkit_version: str,
    ) -> dict | None:
        """获取 SoftwareToolkit 的发布年份。

        属性存储在 JSON 字段 attributes 中，key='release_year'。

        对应问题："12.7 与 12.4 同属哪一年"
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (t:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:'SoftwareToolkit'})
        WHERE coalesce(t.stub,false)=false AND t.canonical_name CONTAINS $toolkit_version
        RETURN t.canonical_name AS name, t.entity_type AS type,
               t.attributes AS attributes, t.confidence AS confidence,
               t.kb_id AS kb_id
        LIMIT 1
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher, {"t": tenant_id, "k": kb_id, "toolkit_version": toolkit_version},
            )
            record = await result.single()
        if record:
            row = dict(record)
            self._deserialize_attrs([row])
            return row
        return None

    async def get_kb_graph(
        self,
        tenant_id: str,
        kb_id: str,
        limit: int = 500,
        entity_types: Optional[list[str]] = None,
    ) -> dict:
        """获取 KB 维度的图谱数据（nodes + edges），用于前端力导向图渲染。

        为避免超大图一次性渲染卡顿，仅返回按"连接度"排序的 top-N 节点，
        边只保留两端都在返回节点集合内的关系（不产生悬挂边）。同时返回
        全量统计（total_nodes / total_relations / type_counts），供前端做
        "已显示 N / 共 M"提示与类型筛选。

        Args:
            tenant_id: 租户 ID
            kb_id: 知识库 ID
            limit: 节点数量上限（按连接度取 top-N）
            entity_types: 仅返回这些实体类型的节点（None 表示不过滤）

        Returns:
            {
              "nodes": [...], "edges": [...],
              "total_nodes": int, "total_relations": int,
              "type_counts": {type: count}, "returned_nodes": int,
              "returned_edges": int, "truncated": bool, "limit": int,
            }
        """
        tenant_id = require_tenant(tenant_id)

        type_filter = ""
        params: dict = {"t": tenant_id, "k": kb_id, "limit": limit}
        if entity_types:
            type_filter = "AND e.entity_type IN $types"
            params["types"] = entity_types

        # 1) 全量分类型计数（无视 limit，用于侧栏筛选与总数提示）
        type_counts = await self.count_entities_by_type(tenant_id, kb_id)
        if entity_types:
            total_nodes = sum(type_counts.get(t, 0) for t in entity_types)
        else:
            total_nodes = sum(type_counts.values())

        # 2) 按连接度排序取 top-N 节点（连接度高的是图谱枢纽，优先展示）
        node_cypher = f"""
        MATCH (e:OntologyEntity {{tenant_id:$t, kb_id:$k}})
        WHERE coalesce(e.stub,false)=false {type_filter}
        OPTIONAL MATCH (e)-[r:ONT_REL]-(:OntologyEntity {{tenant_id:$t, kb_id:$k}})
        WITH e, count(r) AS degree
        RETURN e.entity_type + ':' + e.canonical_name AS id,
               e.canonical_name AS name,
               e.entity_type AS type,
               e.aliases AS aliases,
               e.attributes AS attributes,
               e.description AS description,
               e.confidence AS confidence,
               e.doc_ids AS doc_ids,
               degree
        ORDER BY degree DESC, e.updated_at DESC
        LIMIT $limit
        """
        async with self._driver.session() as session:
            result = await session.run(node_cypher, params)
            nodes = [dict(rec) async for rec in result]

        node_ids = [n["id"] for n in nodes]

        # 3) 全量关系总数（受类型筛选约束：两端均在筛选类型内）
        rel_where = "coalesce(s.stub,false)=false AND coalesce(o.stub,false)=false"
        rel_params: dict = {"t": tenant_id, "k": kb_id}
        if entity_types:
            rel_where += " AND s.entity_type IN $types AND o.entity_type IN $types"
            rel_params["types"] = entity_types
        count_rel_cypher = f"""
        MATCH (s:OntologyEntity {{tenant_id:$t, kb_id:$k}})-[r:ONT_REL]->(o:OntologyEntity {{tenant_id:$t, kb_id:$k}})
        WHERE {rel_where}
        RETURN count(r) AS c
        """
        async with self._driver.session() as session:
            rec = await (await session.run(count_rel_cypher, rel_params)).single()
            total_relations = rec["c"] if rec else 0

        # 4) 边：只保留两端都在返回节点集合内的关系，避免悬挂边
        edges: list[dict] = []
        if node_ids:
            edge_cypher = """
            MATCH (s:OntologyEntity {tenant_id:$t, kb_id:$k})
                  -[r:ONT_REL]->
                  (o:OntologyEntity {tenant_id:$t, kb_id:$k})
            WHERE coalesce(s.stub,false)=false AND coalesce(o.stub,false)=false
              AND (s.entity_type + ':' + s.canonical_name) IN $ids
              AND (o.entity_type + ':' + o.canonical_name) IN $ids
            RETURN s.canonical_name AS source,
                   s.entity_type AS source_type,
                   o.canonical_name AS target,
                   o.entity_type AS target_type,
                   r.relation_type AS label,
                   r.confidence AS confidence
            """
            async with self._driver.session() as session:
                result = await session.run(edge_cypher, {"t": tenant_id, "k": kb_id, "ids": node_ids})
                edges = [dict(rec) async for rec in result]

        self._deserialize_attrs(nodes)
        for n in nodes:
            n.pop("degree", None)

        return {
            "nodes": nodes,
            "edges": edges,
            "total_nodes": total_nodes,
            "total_relations": total_relations,
            "type_counts": type_counts,
            "returned_nodes": len(nodes),
            "returned_edges": len(edges),
            "truncated": total_nodes > len(nodes),
            "limit": limit,
        }

    async def expand_neighbors(
        self,
        tenant_id: str,
        kb_id: str,
        entity_type: str,
        canonical_name: str,
        limit: int = 50,
    ) -> dict:
        """展开某个实体的一跳邻居，返回邻居节点 + 连接边（同 get_kb_graph 结构）。

        前端双击节点时调用，把新邻居增量并入当前图。
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$etype, canonical_name:$name})
        MATCH (e)-[r:ONT_REL]-(n:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(n.stub,false)=false
        WITH e, r, n, startNode(r) AS sn, endNode(r) AS en
        RETURN n.entity_type + ':' + n.canonical_name AS id,
               n.canonical_name AS name,
               n.entity_type AS type,
               n.aliases AS aliases,
               n.attributes AS attributes,
               n.description AS description,
               n.confidence AS confidence,
               n.doc_ids AS doc_ids,
               sn.canonical_name AS source,
               sn.entity_type AS source_type,
               en.canonical_name AS target,
               en.entity_type AS target_type,
               r.relation_type AS label,
               r.confidence AS rel_confidence
        LIMIT $limit
        """
        params = {
            "t": tenant_id, "k": kb_id,
            "etype": entity_type, "name": canonical_name, "limit": limit,
        }
        async with self._driver.session() as session:
            result = await session.run(cypher, params)
            rows = [dict(rec) async for rec in result]

        nodes_by_id: dict[str, dict] = {}
        edges: list[dict] = []
        for row in rows:
            nid = row["id"]
            if nid not in nodes_by_id:
                nodes_by_id[nid] = {
                    "id": nid,
                    "name": row["name"],
                    "type": row["type"],
                    "aliases": row["aliases"],
                    "attributes": row["attributes"],
                    "description": row["description"],
                    "confidence": row["confidence"],
                    "doc_ids": row["doc_ids"],
                }
            edges.append({
                "source": row["source"],
                "source_type": row["source_type"],
                "target": row["target"],
                "target_type": row["target_type"],
                "label": row["label"],
                "confidence": row["rel_confidence"],
            })

        nodes = list(nodes_by_id.values())
        self._deserialize_attrs(nodes)
        return {"nodes": nodes, "edges": edges}


    async def _get_embedding_hash(self, tenant_id: str, kb_id: str, entity_type: str, canonical_name: str) -> Optional[str]:
        """查询单个实体的 embedding_hash（用于幂等跳过）。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$et, canonical_name:$cn})
        RETURN e.embedding_hash AS h
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "k": kb_id, "et": entity_type, "cn": canonical_name})
            record = await result.single()
            return record["h"] if record and record["h"] else None

    async def get_embedding_hashes(self, tenant_id: str, kb_id: str) -> dict[tuple, str]:
        """批量预取 KB 内全部正式实体的 (entity_type, canonical_name) → embedding_hash 映射。

        用于 _handle_completed 在循环 merge_entity 之前一次性查出，消除 N+1。
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(e.stub,false)=false AND e.embedding_hash IS NOT NULL
        RETURN e.entity_type AS et, e.canonical_name AS cn, e.embedding_hash AS h
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "k": kb_id})
            return {(r["et"], r["cn"]): r["h"] async for r in result}

    async def vector_search_entities(self, tenant_id: str, kb_ids: list[str], query_embedding: list[float],
                                     limit: int = 10) -> list[dict]:
        """向量语义召回：余弦近邻 + 租户/KB 过滤。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        CALL db.index.vector.queryNodes('ont_entity_embedding', $topk, $vec)
        YIELD node, score
        WHERE node.tenant_id=$t AND node.kb_id IN $kbs AND coalesce(node.stub,false)=false
        RETURN node.canonical_name AS name, node.entity_type AS type,
               node.aliases AS aliases, node.attributes AS attributes,
               node.description AS description, node.confidence AS confidence,
               node.kb_id AS kb_id, node.doc_ids AS doc_ids,
               node.source_chunks AS source_chunks, score AS vscore
        ORDER BY score DESC LIMIT $limit
        """
        topk = int(os.getenv("ONTOLOGY_VECTOR_TOPK", "200"))
        topk = max(topk, limit * 20)
        async with self._driver.session() as session:
            result = await session.run(cypher, {
                "t": tenant_id, "kbs": kb_ids, "vec": query_embedding,
                "topk": topk, "limit": limit,
            })
            rows = [dict(r) async for r in result]
        self._deserialize_attrs(rows)
        return rows

    # ══════════════════════════════════════════════════════════════════
    # 本体实例/关系 编辑与删除（用户在前端手动编辑）
    # ══════════════════════════════════════════════════════════════════

    async def create_entity(
        self,
        tenant_id: str,
        kb_id: str,
        entity_type: str,
        canonical_name: str,
        aliases: Optional[list] = None,
        description: str = "",
        attributes: Optional[dict] = None,
    ) -> dict:
        """创建本体实体实例节点。

        使用 MERGE 确保幂等：已存在的节点直接返回，不做更新。
        与 merge_entity 不同：不处理 embedding/source_chunks/lightrag_doc_ids 等文档写入字段。

        Args:
            tenant_id: 租户 ID
            kb_id: 知识库 ID
            entity_type: 实体类型
            canonical_name: 规范名
            aliases: 别名列表
            description: 描述
            attributes: 属性 dict

        Returns:
            dict: {name, type, aliases, attributes, description, confidence, doc_ids}

        Raises:
            InvalidParameterError: 缺少必填字段
        """
        tenant_id = require_tenant(tenant_id)
        if not kb_id or not entity_type or not canonical_name:
            raise InvalidParameterError(
                message=translate("err.ontology.missing_required_fields", params={"fields": "knowledge_base_id, entity_type, canonical_name"}, fallback="缺少必填字段: knowledge_base_id, entity_type, canonical_name")
            )

        aliases = aliases or []
        attributes_json = json.dumps(attributes or {}, ensure_ascii=False)
        aliases_text = " ".join(aliases)

        cypher = """
        MERGE (e:OntologyEntity {tenant_id:$tenant_id, kb_id:$kb_id, entity_type:$entity_type, canonical_name:$canonical_name})
        ON CREATE SET
            e.aliases=$aliases, e.aliases_text=$aliases_text,
            e.description=$description, e.attributes=$attributes,
            e.stub=false, e.confidence=1.0,
            e.doc_ids=[], e.source_chunks=[], e.lightrag_doc_ids=[],
            e.created_at=timestamp(), e.updated_at=timestamp()
        RETURN e
        """
        params = {
            "tenant_id": tenant_id, "kb_id": kb_id,
            "entity_type": entity_type, "canonical_name": canonical_name,
            "aliases": aliases, "aliases_text": aliases_text,
            "description": description, "attributes": attributes_json,
        }
        async with self._driver.session() as session:
            result = await session.run(cypher, params)
            record = await result.single()
            if not record:
                raise ResourceNotFoundError(
                    message=translate("err.ontology.entity_create_failed", params={"entity_type": entity_type, "name": canonical_name}, fallback=f"实体创建失败: type={entity_type}, name={canonical_name}"),
                )
            node = record["e"]
            props = dict(node)
            raw_attrs = props.get("attributes", "{}")
            if isinstance(raw_attrs, str):
                try:
                    raw_attrs = json.loads(raw_attrs)
                except (json.JSONDecodeError, TypeError):
                    raw_attrs = {}
            return {
                "name": props.get("canonical_name", canonical_name),
                "type": props.get("entity_type", entity_type),
                "aliases": props.get("aliases", aliases),
                "attributes": raw_attrs,
                "description": props.get("description", description),
                "confidence": props.get("confidence", 1.0),
                "doc_ids": props.get("doc_ids", []),
            }

    async def create_relation(
        self,
        tenant_id: str,
        kb_id: str,
        source_entity_type: str,
        source_canonical_name: str,
        relation_type: str,
        target_entity_type: str,
        target_canonical_name: str,
        attributes: Optional[dict] = None,
    ) -> dict:
        """创建实体间关系。

        两端实体如不存在则创建 stub 节点。关系使用 MERGE 确保幂等。

        Args:
            tenant_id: 租户 ID
            kb_id: 知识库 ID
            source_entity_type: 源实体类型
            source_canonical_name: 源实体规范名
            relation_type: 关系类型
            target_entity_type: 目标实体类型
            target_canonical_name: 目标实体规范名
            attributes: 属性 dict

        Returns:
            dict: {source, source_type, relation_type, target, target_type, attributes, confidence}

        Raises:
            InvalidParameterError: 缺少必填字段
        """
        tenant_id = require_tenant(tenant_id)
        missing = [k for k, v in [
            ("knowledge_base_id", kb_id),
            ("source_entity_type", source_entity_type),
            ("source_canonical_name", source_canonical_name),
            ("relation_type", relation_type),
            ("target_entity_type", target_entity_type),
            ("target_canonical_name", target_canonical_name),
        ] if not v]
        if missing:
            raise InvalidParameterError(message=translate("err.ontology.missing_required_fields", params={"fields": ', '.join(missing)}, fallback=f"缺少必填字段: {', '.join(missing)}"))

        attributes_json = json.dumps(attributes or {}, ensure_ascii=False)

        cypher = """
        MERGE (s:OntologyEntity {tenant_id:$tenant_id, kb_id:$kb_id, entity_type:$source_type, canonical_name:$source_name})
        ON CREATE SET
            s.stub=true, s.description='', s.attributes='{}',
            s.aliases=[], s.aliases_text='',
            s.confidence=0.1, s.doc_ids=[], s.source_chunks=[], s.lightrag_doc_ids=[],
            s.created_at=timestamp(), s.updated_at=timestamp()
        MERGE (o:OntologyEntity {tenant_id:$tenant_id, kb_id:$kb_id, entity_type:$target_type, canonical_name:$target_name})
        ON CREATE SET
            o.stub=true, o.description='', o.attributes='{}',
            o.aliases=[], o.aliases_text='',
            o.confidence=0.1, o.doc_ids=[], o.source_chunks=[], o.lightrag_doc_ids=[],
            o.created_at=timestamp(), o.updated_at=timestamp()
        MERGE (s)-[r:ONT_REL {relation_type:$relation_type}]->(o)
        ON CREATE SET
            r.confidence=1.0, r.attributes=$attributes,
            r.doc_ids=[], r.lightrag_doc_ids=[],
            r.created_at=timestamp(), r.updated_at=timestamp()
        RETURN r
        """
        params = {
            "tenant_id": tenant_id, "kb_id": kb_id,
            "source_type": source_entity_type, "source_name": source_canonical_name,
            "relation_type": relation_type,
            "target_type": target_entity_type, "target_name": target_canonical_name,
            "attributes": attributes_json,
        }
        async with self._driver.session() as session:
            result = await session.run(cypher, params)
            record = await result.single()
            if not record:
                raise ResourceNotFoundError(
                    message=translate("err.ontology.relation_create_failed", params={"source": source_canonical_name, "type": relation_type, "target": target_canonical_name}, fallback=f"关系创建失败: {source_canonical_name} --[{relation_type}]--> {target_canonical_name}"),
                )
            rel = record["r"]
            rel_props = dict(rel)
            raw_attrs = rel_props.get("attributes", "{}")
            if isinstance(raw_attrs, str):
                try:
                    raw_attrs = json.loads(raw_attrs)
                except (json.JSONDecodeError, TypeError):
                    raw_attrs = {}
            return {
                "source": source_canonical_name,
                "source_type": source_entity_type,
                "relation_type": rel_props.get("relation_type", relation_type),
                "target": target_canonical_name,
                "target_type": target_entity_type,
                "attributes": raw_attrs,
                "confidence": rel_props.get("confidence", 1.0),
            }

    async def update_entity(
        self,
        tenant_id: str,
        kb_id: str,
        entity_type: str,
        canonical_name: str,
        updates: dict,
    ) -> None:
        """更新本体实体实例的字段（名称/别名/描述/属性）。

        Args:
            tenant_id: 租户 ID
            kb_id: 知识库 ID
            entity_type: 实体类型
            canonical_name: 当前规范名
            updates: 待更新字段 dict，支持键：
                - name: 新规范名（canonical_name）
                - aliases: 别名列表
                - description: 描述
                - attributes: 属性 dict
        """
        tenant_id = require_tenant(tenant_id)

        # 构建 SET 子句
        set_clauses = []
        params = {
            "t": tenant_id, "k": kb_id,
            "entity_type": entity_type, "canonical_name": canonical_name,
        }

        if "name" in updates:
            set_clauses.append("e.canonical_name=$new_name")
            params["new_name"] = updates["name"]
        if "aliases" in updates:
            set_clauses.append("e.aliases=$aliases")
            set_clauses.append("e.aliases_text=$aliases_text")
            params["aliases"] = updates["aliases"]
            params["aliases_text"] = " ".join(updates["aliases"])
        if "description" in updates:
            set_clauses.append("e.description=$description")
            params["description"] = updates["description"]
        if "attributes" in updates:
            set_clauses.append("e.attributes=$attributes")
            params["attributes"] = json.dumps(updates["attributes"], ensure_ascii=False)

        if not set_clauses:
            raise ValueError("update_entity: no valid fields to update")

        set_clauses.append("e.updated_at=timestamp()")
        set_str = ", ".join(set_clauses)

        cypher = f"""
        MATCH (e:OntologyEntity {{tenant_id:$t, kb_id:$k, entity_type:$entity_type, canonical_name:$canonical_name}})
        SET {set_str}
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, params)
            summary = await result.consume()
            if not summary.counters.contains_updates:
                raise ResourceNotFoundError(
                    message=translate("err.ontology.entity_not_found", params={"entity_type": entity_type, "name": canonical_name}, fallback=f"实体不存在: type={entity_type}, name={canonical_name}"),
                )

    async def delete_entity(
        self,
        tenant_id: str,
        kb_id: str,
        entity_type: str,
        canonical_name: str,
    ) -> None:
        """删除本体实体实例及关联的所有关系（DETACH DELETE）。

        Args:
            tenant_id: 租户 ID
            kb_id: 知识库 ID
            entity_type: 实体类型
            canonical_name: 规范名
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$entity_type, canonical_name:$canonical_name})
        DETACH DELETE e
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {
                "t": tenant_id, "k": kb_id,
                "entity_type": entity_type, "canonical_name": canonical_name,
            })
            summary = await result.consume()
            if not summary.counters.nodes_deleted:
                raise ResourceNotFoundError(
                    message=translate("err.ontology.entity_not_found", params={"entity_type": entity_type, "name": canonical_name}, fallback=f"实体不存在: type={entity_type}, name={canonical_name}"),
                )

    async def update_relation(
        self,
        tenant_id: str,
        kb_id: str,
        source_entity_type: str,
        source_canonical_name: str,
        relation_type: str,
        target_entity_type: str,
        target_canonical_name: str,
        updates: dict,
    ) -> None:
        """更新实体间的关系类型或属性。

        Args:
            tenant_id: 租户 ID
            kb_id: 知识库 ID
            source_entity_type: 源实体类型
            source_canonical_name: 源实体规范名
            relation_type: 当前关系类型
            target_entity_type: 目标实体类型
            target_canonical_name: 目标实体规范名
            updates: 待更新字段 dict，支持键：
                - relation_type: 新关系类型
                - attributes: 属性 dict
        """
        tenant_id = require_tenant(tenant_id)

        set_clauses = []
        params = {
            "t": tenant_id, "k": kb_id,
            "source_type": source_entity_type, "source_name": source_canonical_name,
            "old_rel_type": relation_type,
            "target_type": target_entity_type, "target_name": target_canonical_name,
        }

        if "relation_type" in updates:
            set_clauses.append("r.relation_type=$new_rel_type")
            params["new_rel_type"] = updates["relation_type"]
        if "attributes" in updates:
            set_clauses.append("r.attributes=$attributes")
            params["attributes"] = json.dumps(updates["attributes"], ensure_ascii=False)

        if not set_clauses:
            raise ValueError("update_relation: no valid fields to update")

        set_clauses.append("r.updated_at=timestamp()")
        set_str = ", ".join(set_clauses)

        cypher = f"""
        MATCH (s:OntologyEntity {{tenant_id:$t, kb_id:$k, entity_type:$source_type, canonical_name:$source_name}})
              -[r:ONT_REL {{relation_type:$old_rel_type}}]->
              (o:OntologyEntity {{tenant_id:$t, kb_id:$k, entity_type:$target_type, canonical_name:$target_name}})
        SET {set_str}
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, params)
            summary = await result.consume()
            if not summary.counters.contains_updates:
                raise ResourceNotFoundError(
                    message=translate("err.ontology.relation_not_found", params={"source": source_canonical_name, "type": relation_type, "target": target_canonical_name}, fallback=f"关系不存在: {source_canonical_name} --[{relation_type}]--> {target_canonical_name}"),
                )

    async def delete_relation(
        self,
        tenant_id: str,
        kb_id: str,
        source_entity_type: str,
        source_canonical_name: str,
        relation_type: str,
        target_entity_type: str,
        target_canonical_name: str,
    ) -> None:
        """删除实体间的关系（不删除两端实体）。

        Args:
            tenant_id: 租户 ID
            kb_id: 知识库 ID
            source_entity_type: 源实体类型
            source_canonical_name: 源实体规范名
            relation_type: 关系类型
            target_entity_type: 目标实体类型
            target_canonical_name: 目标实体规范名
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (s:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$source_type, canonical_name:$source_name})
              -[r:ONT_REL {relation_type:$rel_type}]->
              (o:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$target_type, canonical_name:$target_name})
        DELETE r
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {
                "t": tenant_id, "k": kb_id,
                "source_type": source_entity_type, "source_name": source_canonical_name,
                "rel_type": relation_type,
                "target_type": target_entity_type, "target_name": target_canonical_name,
            })
            summary = await result.consume()
            if not summary.counters.relationships_deleted:
                raise ResourceNotFoundError(
                    message=translate("err.ontology.relation_not_found", params={"source": source_canonical_name, "type": relation_type, "target": target_canonical_name}, fallback=f"关系不存在: {source_canonical_name} --[{relation_type}]--> {target_canonical_name}"),
                )


__all__ = ["OntologyGraphRepository"]
