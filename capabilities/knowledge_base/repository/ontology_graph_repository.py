#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Neo4j repository for ontology graph instances."""

import asyncio
import json
import logging
import os
import random
from typing import Optional

from neo4j import AsyncDriver
from neo4j.exceptions import TransientError

from jonex_core.common.ontology_embedding import (
    build_embed_text,
    embed,
    embed_hash,
    ingest_embed_timeout,
)
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

logger = logging.getLogger(__name__)


# [jonex] P0-a：原 _row_object_merge_key（表格对象按 table_sig+pk_value
# upsert）已删除——该键不受唯一约束保护，会撞 ConstraintValidationFailed
# 导致整轮写图失败。合并键现统一为约束键，详见 merge_entity 内注释。


# ── 写图 transient 错误重试兜底 ──────────────────────────────
# Neo4j 对 TransientError（尤其 DeadlockDetected、锁超时）的官方语义就是
# 「重试即可」；但 driver 只在 managed transaction（session.execute_write）
# 内自动重试，本仓储用手动 ``session.run``，必须自己兜。默认 6 次尝试
# （1 次首跑 + 5 次重试），指数退避 + 抖动，最坏单条约 15.5s。
_WRITE_RETRY_ATTEMPTS_DEFAULT = 6
_WRITE_RETRY_BASE_DELAY_DEFAULT = 0.5


def _write_retry_attempts() -> int:
    try:
        return max(1, int(os.getenv("ONTOLOGY_WRITE_RETRY_ATTEMPTS", str(_WRITE_RETRY_ATTEMPTS_DEFAULT))))
    except (TypeError, ValueError):
        return _WRITE_RETRY_ATTEMPTS_DEFAULT


def _write_retry_base_delay() -> float:
    try:
        return max(0.0, float(os.getenv("ONTOLOGY_WRITE_RETRY_BASE_DELAY", str(_WRITE_RETRY_BASE_DELAY_DEFAULT))))
    except (TypeError, ValueError):
        return _WRITE_RETRY_BASE_DELAY_DEFAULT


def _is_transient_error(exc: Exception) -> bool:
    """Neo4j transient（可重试）错误判据：类型优先，``code`` 前缀兜底。"""
    if isinstance(exc, TransientError):
        return True
    return str(getattr(exc, "code", "") or "").startswith("Neo.TransientError")


async def _with_transient_retry(fn, *, what: str):
    """执行 ``fn()``（async，含结果消费）；transient 错误退避重试，其余上抛。

    重试次数/退避可用 ``ONTOLOGY_WRITE_RETRY_ATTEMPTS`` /
    ``ONTOLOGY_WRITE_RETRY_BASE_DELAY`` 覆盖（后者置 0 即不等待，测试用）。
    """
    attempts = _write_retry_attempts()
    base = _write_retry_base_delay()
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — 非 transient 原样上抛
            if attempt >= attempts - 1 or not _is_transient_error(exc):
                raise
            delay = base * (2 ** attempt) + (random.uniform(0, base) if base else 0.0)
            logger.warning(
                "%s 撞 Neo4j transient 错误（第 %d/%d 次），%.2fs 后重试：%s",
                what, attempt + 1, attempts, delay, exc,
            )
            if delay > 0:
                await asyncio.sleep(delay)


def _endpoint_lock_key(entity_type: Optional[str], name: Optional[str]) -> tuple:
    """端点加锁排序键（见 merge_relation 死锁修复）。"""
    return (entity_type or "", name or "")


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
                    timeout=ingest_embed_timeout(),
                )
                if vec is not None:
                    embedding = vec
                    embedding_hash_val = new_hash

        # [jonex] P0-a：MERGE 键必须与唯一约束
        # (tenant_id, kb_id, entity_type, canonical_name) 完全一致。
        #
        # 旧实现（改动 18）给表格对象用 (tenant_id, kb_id, table_sig,
        # pk_value) 做 upsert 键——该键**不受约束保护**，MERGE 匹配不到已有
        # 节点就落 ON CREATE，随即撞唯一约束抛 ConstraintValidationFailed。
        # 该错误不是 TransientError（_is_transient_error 只认 Neo.TransientError
        # 前缀），仓储层重试与整段重写都不兜 → 判写图失败 → 半写收回 →
        # 文档零贡献。触发条件很常见：table_sig 只由表头 md5 得出，同一主键
        # 行出现在不同表片段（sig 不同、canonical_name 相同）即撞。
        # 线上 2026-09-12 c31ac916 实测：29433 实体全部写不进去。
        #
        # 表格对象的识别与增量语义不依赖 MERGE 键：table_sig / pk_value 仍
        # 写为节点属性；canonical_name 由 build_objects 生成时已含主键
        # （+限定值）保证 KB 内唯一（见 table_object_extractor 文件头 P3 说明），
        # 故改用约束键不改变表格对象的 upsert 粒度。
        cypher = (
            "MERGE (e:OntologyEntity {tenant_id:$tenant_id, kb_id:$kb_id, "
            "entity_type:$entity_type, canonical_name:$canonical_name})\n"
            "ON CREATE SET\n"
            "e.aliases=$aliases, e.aliases_text=$aliases_text,\n"
            "    e.table_sig=$table_sig, e.pk_value=$pk_value,\n"
            "    e.attributes=$attributes, e.confidence=$confidence,\n"
            "    e.description=$description,\n"
            "    e.doc_ids=[$doc_id], e.source_chunks=$source_chunks,\n"
            "    e.lightrag_doc_ids=$lightrag_doc_ids, e.extraction_method=$extraction_method,\n"
            "    e.embedding=$embedding, e.embedding_hash=$embedding_hash,\n"
            "    e.stub=false, e.created_at=timestamp(), e.updated_at=timestamp()\n"
            "ON MATCH SET\n"
            "e.aliases=$aliases, e.aliases_text=$aliases_text,\n"
            "    e.table_sig=$table_sig, e.pk_value=$pk_value,\n"
            "    e.attributes=$attributes,\n"
            "    e.description=CASE WHEN size($description) > size(coalesce(e.description,''))\n"
            "        THEN $description ELSE e.description END,\n"
            "    e.confidence=CASE WHEN $confidence>coalesce(e.confidence,0) THEN $confidence ELSE e.confidence END,\n"
            "    e.doc_ids=apoc.coll.toSet(coalesce(e.doc_ids,[])+[$doc_id]),\n"
            "    e.source_chunks=$source_chunks,\n"
            "    e.lightrag_doc_ids=apoc.coll.toSet(coalesce(e.lightrag_doc_ids,[])+$lightrag_doc_ids),\n"
            "    e.embedding=CASE WHEN $embedding IS NOT NULL THEN $embedding ELSE e.embedding END,\n"
            "    e.embedding_hash=CASE WHEN $embedding IS NOT NULL THEN $embedding_hash ELSE e.embedding_hash END,\n"
            "    e.stub=false, e.updated_at=timestamp()\n"
        )
        params = {
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "doc_id": doc_id,
            "canonical_name": entity["canonical_name"],
            "entity_type": entity["entity_type"],
            "table_sig": entity.get("table_sig", ""),
            "pk_value": entity.get("pk_value", ""),
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
            async def _write():
                result = await session.run(cypher, params)
                # consume：既确保错误在此刻抛出（否则被静默吞到 GC），
                # 也让 transient 重试真正覆盖到执行阶段。
                await result.consume()

            await _with_transient_retry(_write, what=f"merge_entity({entity['canonical_name']})")

    async def merge_relation(
        self, tenant_id: str, kb_id: str, doc_id: str, rel: dict,
    ) -> tuple[str, str, str, str, str, int, int]:
        """MERGE 关系 + 端点兜底 stub，返回实际写入键。

        [jonex] P10 批 4.1：返回 (src_type, src_name, tgt_type, tgt_name,
        relation_type, src_node_id, tgt_node_id)。src/tgt name 是端点解析
        后的名字（ONTOLOGY_REL_ENDPOINT_RESOLVE 开启时可能 ≠ rel 原始名），
        node id 是 MERGE 命中的实际节点。调用方（reconciliation service）
        用解析后名构造清理名单（口径与图内 canonical_name 一致），用
        节点 id 豁免本轮刚写入的边。
        """
        tenant_id = require_tenant(tenant_id)
        (src_type, src_name), (tgt_type, tgt_name) = await self._resolve_relation_endpoints(
            tenant_id, kb_id, rel)
        # [jonex] 死锁修复①：两个端点按 (entity_type, canonical_name) 字典序
        # 先加锁，与关系方向解耦。MERGE 会申请节点写锁，加锁顺序若随方向变化，
        # A→B 与 B→A 两个并发事务会互相等待对方已持有的锁（线上
        # DeadlockDetected：并发 24 时 filing_event.md 写图两次全挂）。
        # s_is_a 记录关系方向落在排序后的哪一侧。
        if _endpoint_lock_key(src_type, src_name) <= _endpoint_lock_key(tgt_type, tgt_name):
            a_type, a_name, b_type, b_name, s_is_a = src_type, src_name, tgt_type, tgt_name, True
        else:
            a_type, a_name, b_type, b_name, s_is_a = tgt_type, tgt_name, src_type, src_name, False
        # [jonex] 方案⑧：从 rel dict 取关系的 source_chunks，用于 ONT_REL 边出处 + stub 端点兜底
        # Neo4j 不允许 list-of-map 作为属性值，以 JSON string 存储（与 merge_entity 口径一致）
        rel_source_chunks = rel.get("source_chunks") or []
        cypher = """
        MERGE (a:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$a_type, canonical_name:$a_name})
        ON CREATE SET a.stub=true, a.confidence=0.1, a.aliases=[], a.aliases_text='',
            a.attributes='{}',
            a.source_chunks=apoc.convert.toJson($rel_source_chunks),
            a.lightrag_doc_ids=[],
            a.extraction_method='stub', a.doc_ids=[$doc_id], a.created_at=timestamp(), a.updated_at=timestamp()
        ON MATCH SET
            a.doc_ids = CASE WHEN coalesce(a.stub,false)=true
                THEN apoc.coll.toSet(coalesce(a.doc_ids,[])+[$doc_id]) ELSE a.doc_ids END,
            a.source_chunks = CASE WHEN coalesce(a.stub,false)=true
                THEN apoc.convert.toJson(apoc.coll.toSet(apoc.convert.fromJsonList(coalesce(a.source_chunks,'[]'))+$rel_source_chunks))
                ELSE a.source_chunks END,
            a.updated_at = CASE WHEN coalesce(a.stub,false)=true THEN timestamp() ELSE a.updated_at END
        WITH a
        MERGE (b:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$b_type, canonical_name:$b_name})
        ON CREATE SET b.stub=true, b.confidence=0.1, b.aliases=[], b.aliases_text='',
            b.attributes='{}',
            b.source_chunks=apoc.convert.toJson($rel_source_chunks),
            b.lightrag_doc_ids=[],
            b.extraction_method='stub', b.doc_ids=[$doc_id], b.created_at=timestamp(), b.updated_at=timestamp()
        ON MATCH SET
            b.doc_ids = CASE WHEN coalesce(b.stub,false)=true
                THEN apoc.coll.toSet(coalesce(b.doc_ids,[])+[$doc_id]) ELSE b.doc_ids END,
            b.source_chunks = CASE WHEN coalesce(b.stub,false)=true
                THEN apoc.convert.toJson(apoc.coll.toSet(apoc.convert.fromJsonList(coalesce(b.source_chunks,'[]'))+$rel_source_chunks))
                ELSE b.source_chunks END,
            b.updated_at = CASE WHEN coalesce(b.stub,false)=true THEN timestamp() ELSE b.updated_at END
        WITH a, b,
             CASE WHEN $s_is_a THEN a ELSE b END AS s,
             CASE WHEN $s_is_a THEN b ELSE a END AS o
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
        RETURN id(s) AS sid, id(o) AS oid
        """
        params = {
            "t": tenant_id,
            "k": kb_id,
            "doc_id": doc_id,
            "a_type": a_type,
            "a_name": a_name,
            "b_type": b_type,
            "b_name": b_name,
            "s_is_a": s_is_a,
            "rel_type": rel["relation_type"],
            "confidence": rel.get("confidence", 1.0),
            "attributes": json.dumps(rel.get("attributes", {}), ensure_ascii=False),
            "lightrag_doc_ids": rel.get("lightrag_doc_ids", []),
            "rel_source_chunks": rel_source_chunks,   # [jonex] 方案⑧
        }
        async with self._driver.session() as session:
            async def _write():
                result = await session.run(cypher, params)
                return await result.single()

            # [jonex] 死锁修复②：run+consume 是一个重试单元——服务端死锁可能在
            # 结果消费阶段才抛出，只包 run 会漏掉。
            record = await _with_transient_retry(_write, what="merge_relation")
            sid = int(record["sid"]) if record else 0
            oid = int(record["oid"]) if record else 0
        # [jonex] P10 批 4.1：返回实际写入键——src/tgt name 经端点解析
        # 可能已对齐到正式 canonical_name，调用方清理名单必须用解析后名；
        # sid/oid 供清理第 3 步豁免本轮刚写入的边（防「摘旧类型节点连带
        # 摘新边」）。
        return (src_type, src_name, tgt_type, tgt_name, rel["relation_type"], sid, oid)

    async def _resolve_relation_endpoints(
        self, tenant_id: str, kb_id: str, rel: dict,
    ) -> tuple[tuple, tuple]:
        """解析一条关系的两个端点，返回 (src_lock_key, tgt_lock_key)。

        从 merge_relation 里原样抽出，**两边共用同一个实现**——加锁口径
        与分桶口径一致必须由构造保证，不能靠人工同步。
        命中 ``_endpoint_cache`` 时零额外查询。
        """
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
        return (
            _endpoint_lock_key(src_type, src_name),
            _endpoint_lock_key(tgt_type, tgt_name),
        )

    async def resolve_relation_endpoint_keys(
        self, tenant_id: str, kb_id: str, rels: list[dict],
    ) -> list[tuple[tuple, tuple]]:
        """批量解析，返回与 ``rels`` 等长的 ``[(src_lock_key, tgt_key), ...]``。

        **为什么必须批量**：单条 ``_resolve_endpoint`` 的
        ``canonical_name=$n OR $n IN aliases`` 与 find_reference_hits 被优化掉
        的是同一个反模式——OR 让 planner 无法下推索引，只能按 (tenant_id, kb_id)
        前缀取全部节点再逐行过滤。1992 个行对象名各不相同（缓存全 miss），
        逐条串行 = 1992 次全 KB 扫描，线上 KB 已 5 万节点，是分钟级至十分钟级
        的净增开销，且加在 merge 之前的纯等待（改动前这些查询好歹在 24 并发里跑）。

        两段式（口径同 find_reference_hits）：
          ① canonical_name 等值匹配走 ont_entity_name 索引（等值 seek）；
          ② 仅对第 ① 段未命中的名字扫 aliases。
        结果回填 ``_endpoint_cache``，后续 merge_relation 内部解析零额外查询。

        端点选取口径与 ``_resolve_endpoint`` 严格对齐：canonical 精确命中优先 →
        有 type_hint 时该类型优先 → confidence 降序；未命中兜底
        ``(type_hint or "Unknown", name)``。**命中与未命中都回填缓存**——未命中
        端点在热点文档里恰恰是大多数，漏回填会让 merge_relation 内部把它们再查
        一遍，批量解析省下的开销被抵消一半。
        """
        tenant_id = require_tenant(tenant_id)
        if not rels:
            return []

        def _cache_key(name: str, hint: Optional[str]) -> tuple:
            return (kb_id, name, hint or "")

        # 去重（同一名字常被多条关系引用），跳过已缓存的
        pending: dict[tuple, dict] = {}
        for rel in rels:
            for name, hint in (
                (rel["source_name"], rel.get("source_type")),
                (rel["target_name"], rel.get("target_type")),
            ):
                ck = _cache_key(name, hint)
                if ck not in self._endpoint_cache and ck not in pending:
                    pending[ck] = {"v": name, "h": hint or None}

        if pending:
            hits = await self._lookup_entities_by_names(
                tenant_id, kb_id, list(pending.values()),
            )
            for ck, item in pending.items():
                name, hint = item["v"], item["h"]
                self._endpoint_cache[ck] = hits.get(name) or ((hint or "Unknown"), name)

        return [
            (
                _endpoint_lock_key(*self._endpoint_cache[_cache_key(rel["source_name"], rel.get("source_type"))]),
                _endpoint_lock_key(*self._endpoint_cache[_cache_key(rel["target_name"], rel.get("target_type"))]),
            )
            for rel in rels
        ]

    async def _lookup_entities_by_names(
        self, tenant_id: str, kb_id: str, values: list[dict],
    ) -> dict[str, tuple[str, str]]:
        """批量查 ``[{v, h}]`` 命中的正式实体，返回 ``{name: (entity_type, canonical_name)}``。

        与 ``_resolve_endpoint`` 同优先级口径，只是把「逐值一条 Cypher」换成
        「一次 UNWIND 批量」。``h`` 是 type_hint（可为 null）：
        它**不**在第 ① 段过滤（canonical 精确命中的一律收），只参与排序优选；
        在第 ② 段（别名命中、canonical 不等值）则过滤——与非批量版
        ``WHERE ($hint IS NULL OR e.canonical_name=$name OR e.entity_type=$hint)``
        等价。
        """
        hits: dict[str, tuple[str, str]] = {}

        # ── 第 ① 段：canonical_name 等值命中（走 ont_entity_name 索引 seek）──
        cypher_exact = """
        UNWIND $values AS row
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, canonical_name:row.v})
        WHERE coalesce(e.stub,false)=false
        WITH row, e
        ORDER BY row.v,
                 (CASE WHEN row.h IS NOT NULL AND e.entity_type=row.h THEN 0 ELSE 1 END),
                 coalesce(e.confidence,0) DESC
        WITH row.v AS value, collect(e)[0] AS best
        RETURN value AS value, best.entity_type AS entity_type,
               best.canonical_name AS canonical_name
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher_exact, {"t": tenant_id, "k": kb_id, "values": values},
            )
            async for record in result:
                hits[record["value"]] = (
                    record["entity_type"] or "Unknown",
                    record["canonical_name"],
                )

        # ── 第 ② 段：仅剩余值扫 aliases（列表属性无法索引，逐节点过滤）──
        remaining = [row for row in values if row["v"] not in hits]
        if remaining:
            cypher_alias = """
            UNWIND $values AS row
            MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
            WHERE coalesce(e.stub,false)=false
              AND row.v IN coalesce(e.aliases,[])
              AND (row.h IS NULL OR e.canonical_name=row.v OR e.entity_type=row.h)
            WITH row, e
            ORDER BY row.v,
                     (CASE WHEN e.canonical_name=row.v THEN 0 ELSE 1 END),
                     coalesce(e.confidence,0) DESC
            WITH row.v AS value, collect(e)[0] AS best
            RETURN value AS value, best.entity_type AS entity_type,
                   best.canonical_name AS canonical_name
            """
            async with self._driver.session() as session:
                result = await session.run(
                    cypher_alias, {"t": tenant_id, "k": kb_id, "values": remaining},
                )
                async for record in result:
                    hits[record["value"]] = (
                        record["entity_type"] or "Unknown",
                        record["canonical_name"],
                    )
        return hits

    async def find_reference_hits(
        self, tenant_id: str, kb_id: str, values: list[str],
    ) -> dict[str, tuple[str, str]]:
        """[jonex] §10.2.3 引用关系：候选值批量精确命中查找。

        返回 ``{值: (entity_type, canonical_name)}``——值精确命中 KB 内某
        非 stub 实体的 canonical_name 或 aliases。多命中时 canonical_name
        优先、confidence 降序取一（与 _resolve_endpoint 同优先级口径）。

        [jonex] 两段式：原实现把 ``canonical_name=v OR v IN e.aliases`` 写在同一条
        谓词里，OR 让 planner 无法下推索引、只能按 ``(tenant_id, kb_id)`` 前缀取全部
        节点再逐行过滤，复杂度 = 候选值数 × KB 节点数（候选上限 3000，线上 KB 已 5 万
        节点 → 上亿次属性比较，实测单条 Cypher 满核跑 11 分钟未完成）。拆两段后：
          ① canonical_name 等值匹配走 ``ont_entity_name`` 索引（等值 seek）；
          ② **只对第 ① 段未命中的剩余值**扫 aliases——Neo4j 常规索引不支持列表成员
             查找，这半段无法索引化。正常情况绝大多数值在第 ① 段命中，剩余量很小；
             最坏情况（值全不命中）仍会退化，故由调用方 ``_write_graph_once`` 套整体
             超时兜底（超时降级为「本轮无引用关系」，不阻断写图）。
        """
        tenant_id = require_tenant(tenant_id)
        values = [v for v in values if v]
        if not values:
            return {}
        hits: dict[str, tuple[str, str]] = {}

        # ── 第 ① 段：canonical_name 等值命中（走 ont_entity_name 索引 seek）──
        # 原查询「canonical_name 精确优先、再按 confidence 降序」的语义由两段式天然
        # 保证：本段命中的都是精确匹配，晚于本段的 aliases 命中不会覆盖它。
        cypher_exact = """
        UNWIND $values AS v
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, canonical_name:v})
        WHERE coalesce(e.stub,false)=false
        WITH v, e
        ORDER BY v, coalesce(e.confidence,0) DESC
        WITH v, collect(e)[0] AS best
        RETURN v AS value, best.entity_type AS entity_type,
               best.canonical_name AS canonical_name
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher_exact, {"t": tenant_id, "k": kb_id, "values": values},
            )
            async for record in result:
                hits[record["value"]] = (
                    record["entity_type"] or "Unknown",
                    record["canonical_name"],
                )

        # ── 第 ② 段：仅剩余值扫 aliases（列表属性无法索引，逐节点过滤）──
        remaining = [v for v in values if v not in hits]
        if remaining:
            cypher_alias = """
            UNWIND $values AS v
            MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
            WHERE coalesce(e.stub,false)=false
              AND v IN coalesce(e.aliases,[])
            WITH v, e
            ORDER BY v, coalesce(e.confidence,0) DESC
            WITH v, collect(e)[0] AS best
            RETURN v AS value, best.entity_type AS entity_type,
                   best.canonical_name AS canonical_name
            """
            async with self._driver.session() as session:
                result = await session.run(
                    cypher_alias, {"t": tenant_id, "k": kb_id, "values": remaining},
                )
                async for record in result:
                    hits[record["value"]] = (
                        record["entity_type"] or "Unknown",
                        record["canonical_name"],
                    )
        return hits

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

    async def count_entities_by_document(self, tenant_id: str, kb_id: str, doc_id: str) -> int:
        """统计某文档对本体图谱的实体贡献数（doc_ids 含该 doc 的实体）。

        [jonex] P5-var 退化闸门 + delta 观测用：reconcile 写图前/后各查
        一次，跌幅超阈值拒绝覆盖、日志记 before/after/delta。
        [jonex] P10：口径对齐其余 count 方法——kb_id 过滤 + stub 排除
        （stub 是 merge_relation 端点兜底产物，非正式实体；计入 before
        会系统性高估存量、使闸门误拒）。
        """
        tenant_id = require_tenant(tenant_id)
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
                WHERE $doc_id IN coalesce(e.doc_ids,[])
                  AND coalesce(e.stub,false)=false
                RETURN count(e) AS c
                """,
                {"t": tenant_id, "k": kb_id, "doc_id": doc_id},
            )
            record = await result.single()
            return int(record["c"]) if record else 0

    async def delete_by_document(self, tenant_id: str, kb_id: str, doc_id: str) -> None:
        """移除某文档对本体图谱的贡献：从实体/关系的 doc_ids 摘除该 doc，
        并清理因此变为孤立（无 doc 归属且无边）的实体/关系。

        [jonex] 批 6 根治（删光重来）：写图路径恢复全量删除——重抽/重解析
        语义统一为「先删后 merge」，图终态 = 本轮产出，实体不再跨轮累积。
        四条语句带 kb_id 收窄（旧实现全租户扫描，KB 十万级实体时全图
        扫描 + 写锁成瓶颈；与批 4.3 收窄同源）。

        注意：Neo4j 单次 session.run 只接受一条语句，故拆成多条按序执行
        （原先用 ';' 拼多语句会被服务端拒绝）。
        """
        tenant_id = require_tenant(tenant_id)
        async with self._driver.session() as session:
            # 1) 关系：摘除该 doc_id
            await session.run(
                """
                MATCH (:OntologyEntity {tenant_id:$t, kb_id:$k})-[r:ONT_REL]->(:OntologyEntity {tenant_id:$t, kb_id:$k})
                WHERE $doc_id IN r.doc_ids
                SET r.doc_ids = [x IN r.doc_ids WHERE x <> $doc_id]
                """,
                {"t": tenant_id, "k": kb_id, "doc_id": doc_id},
            )
            # 2) 关系：doc_ids 清空者删除
            await session.run(
                """
                MATCH (:OntologyEntity {tenant_id:$t, kb_id:$k})-[r:ONT_REL]->(:OntologyEntity {tenant_id:$t, kb_id:$k})
                WHERE size(coalesce(r.doc_ids,[]))=0
                DELETE r
                """,
                {"t": tenant_id, "k": kb_id},
            )
            # 3) 实体：摘除该 doc_id
            await session.run(
                """
                MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
                WHERE $doc_id IN e.doc_ids
                SET e.doc_ids = [x IN e.doc_ids WHERE x <> $doc_id]
                """,
                {"t": tenant_id, "k": kb_id, "doc_id": doc_id},
            )
            # 4) 实体：无 doc 归属且无边者删除
            await session.run(
                """
                MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
                WHERE size(coalesce(e.doc_ids,[]))=0 AND NOT (e)--()
                DELETE e
                """,
                {"t": tenant_id, "k": kb_id},
            )

    async def search_entities(
        self,
        tenant_id: str,
        kb_ids: list[str],
        query: str,
        limit: int = 5,
        doc_id: Optional[str] = None,
    ) -> list[dict]:
        """全文索引模糊搜索本体实体（表单选择器用）。

        `doc_id` 有值时只返回 `doc_ids` 含该文档的实体（文档页表单的候选范围），
        判据与 `list_entities(document_id=...)` 一致（`$doc_id IN e.doc_ids`），
        避免在单篇文档里列出整个 KB 的实体。
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        CALL db.index.fulltext.queryNodes('ont_entity_ft', $q) YIELD node, score
        WHERE node.tenant_id=$t AND node.kb_id IN $kbs AND coalesce(node.stub,false)=false
          AND ($doc_id IS NULL OR $doc_id IN coalesce(node.doc_ids,[]))
        RETURN node.canonical_name AS name, node.entity_type AS type,
               node.aliases AS aliases, node.attributes AS attributes,
               node.description AS description,
               node.confidence AS confidence, node.extraction_method AS extraction_method,
               node.kb_id AS kb_id,
               // [jonex] S4（§3.4 排序豁免）：真行对象判据 = extraction_method 为
               // row_as_object 且 table_sig 非空（分组聚合节点无该属性），
               // 查询侧据此给候选打 _order_locked。
               node.table_sig AS table_sig,
               node.doc_ids AS doc_ids, node.source_chunks AS source_chunks, score
        ORDER BY score DESC LIMIT $limit
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher,
                {"t": tenant_id, "kbs": kb_ids, "q": query, "limit": limit, "doc_id": doc_id or None},
            )
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
            ORDER BY
              (CASE
                WHEN n.source_chunks CONTAINS 'image_idx=' THEN 0
                WHEN n.source_chunks IS NOT NULL AND n.source_chunks <> '[]' THEN 1
                ELSE 2
              END),
              coalesce(n.confidence,0) DESC, n.canonical_name ASC
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
                           source_chunks: n.source_chunks,
                           extraction_method: n.extraction_method,
                           // [jonex] S4（§3.4）：真行对象判据（见 search_entities）
                           table_sig: n.table_sig
                       },
                       relation_source_chunks: r.source_chunks,
                       relation_attributes: r.attributes,
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
                 last_rel.attributes AS relation_attributes,
                 min(hop) AS hop
            ORDER BY hop ASC,
              (CASE
                WHEN n.source_chunks CONTAINS 'image_idx=' THEN 0
                WHEN n.source_chunks IS NOT NULL AND n.source_chunks <> '[]' THEN 1
                ELSE 2
              END),
              coalesce(n.confidence,0) DESC, n.canonical_name ASC
            WITH e, collect({
                    source: e.canonical_name,
                    target: n.canonical_name,
                    target_type: n.entity_type,
                    target_entity: {
                        name: n.canonical_name, type: n.entity_type,
                        aliases: n.aliases, description: n.description,
                        attributes: n.attributes, confidence: n.confidence,
                        kb_id: n.kb_id, doc_ids: n.doc_ids,
                        source_chunks: n.source_chunks,
                        extraction_method: n.extraction_method,
                        // [jonex] S4（§3.4）：真行对象判据（见 search_entities）
                        table_sig: n.table_sig
                    },
                    relation_type: rel_types[-1],
                    relation_chain: rel_types,
                    relation_source_chunks: relation_source_chunks,
                    relation_attributes: relation_attributes,
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

    # ══════════════════════════════════════════════════════════════════
    # 数据运营抽屉 B（Ontology 对象详情）：按三元组精确定位
    # ══════════════════════════════════════════════════════════════════
    #
    # 两个方法都收 `(kb_id, entity_type, canonical_name)` **完整三元组**，与
    # `expand_neighbors` 同款判据 —— 这是本仓里唯一能把「同名不同 type」区分开的口径。
    # `exact_match_entities` / `list_entities` 都不能用来做这件事：前者按 canonical_name
    # 在多 KB 间模糊命中、后者是 `CONTAINS` 模糊 + 分页，都会撞进同名实例。

    async def get_entity(
        self,
        tenant_id: str,
        kb_id: str,
        entity_type: str,
        canonical_name: str,
    ) -> Optional[dict]:
        """按三元组精确定位单个本体实例；不存在（或已改名）返回 None。

        返回 `{canonical_name, entity_type, aliases, attributes, description, confidence,
        created_at, updated_at, doc_ids}`；时间戳为 Neo4j `timestamp()`（epoch 毫秒），
        由调用方转 ISO（`AnalyticsService._ts_to_iso`）。
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$et, canonical_name:$name})
        WHERE coalesce(e.stub,false)=false
        RETURN e.canonical_name AS canonical_name, e.entity_type AS entity_type,
               e.aliases AS aliases, e.attributes AS attributes,
               e.description AS description, e.confidence AS confidence,
               e.created_at AS created_at,
               coalesce(e.updated_at, e.created_at) AS updated_at,
               e.doc_ids AS doc_ids
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher, {"t": tenant_id, "k": kb_id, "et": entity_type, "name": canonical_name}
            )
            record = await result.single()
        if not record:
            return None
        row = dict(record)
        self._deserialize_attrs([row])
        return row

    async def neighbors_by_entity(
        self,
        tenant_id: str,
        kb_id: str,
        entity_type: str,
        canonical_name: str,
        limit: int = 200,
    ) -> tuple[list[dict], int, bool]:
        """按三元组取一跳邻居事实，返回 `(facts, total, truncated)`。

        与 `neighbors()` 的两处差别：

        1. **MATCH 带上 `entity_type`** —— `neighbors()` 只按 `canonical_name` 匹配，
           同名不同 type 的实例会被串在一起；
        2. **`total` 是「不同 (关系类型, 目标) 的对数」，不是本次返回的条数** —— 抽屉要显示
           「共 N 条」，`neighbors()` 在 Cypher 里就 `[..$limit]` 截断了、总数拿不回来。

        `facts` 每条为 `{relation_type, direction, target, target_type}`，并已按
        `(relation_type, target)` 去重（平行边只留一条，方向上「有来有往」也算一条）。
        `truncated = total > len(facts)`：明细被 `limit` 截了，前端据此提示「仅显示前 N 条」。
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$et, canonical_name:$name})
        WHERE coalesce(e.stub,false)=false
        MATCH (e)-[r:ONT_REL]-(n:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(n.stub,false)=false
        WITH e, r, n,
             CASE WHEN startNode(r) = e THEN 'outgoing' ELSE 'incoming' END AS rel_dir
        ORDER BY r.relation_type ASC, coalesce(n.confidence,0) DESC, n.canonical_name ASC
        RETURN count(DISTINCT [r.relation_type, n.canonical_name]) AS total,
               collect({
                   relation_type: r.relation_type,
                   direction: rel_dir,
                   target: n.canonical_name,
                   target_type: n.entity_type
               })[..$limit] AS facts
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher,
                {"t": tenant_id, "k": kb_id, "et": entity_type, "name": canonical_name, "limit": limit},
            )
            record = await result.single()
        data = dict(record) if record else {"total": 0, "facts": []}
        total = int(data.get("total") or 0)

        seen: set[tuple] = set()
        facts: list[dict] = []
        for f in data.get("facts") or []:
            key = (f.get("relation_type"), f.get("target"))
            if key in seen:
                continue
            seen.add(key)
            facts.append(f)
        return facts, total, total > len(facts)

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
               e.extraction_method AS extraction_method,
               // [jonex] S4（§3.4 排序豁免）：真行对象判据（见 search_entities）
               e.table_sig AS table_sig,
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
               e.extraction_method AS extraction_method,
               // [jonex] S4（§3.4 排序豁免）：真行对象判据（见 search_entities）
               e.table_sig AS table_sig,
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

    async def stub_entity_stats(self, tenant_id: str, kb_id: str) -> dict:
        """[jonex] 改动 52：KB 级 stub 实体聚合，返回
        ``{total: int, stub: int, ratio: float}``（total=0 时 ratio=0）。

        stub 节点 = 关系端点兜底创建的空壳（merge_relation 的 ON CREATE SET
        ``stub=true, description='', attributes='{}'``），是抽取管线异常的
        可观测信号；供写图后告警与 KB 统计接口使用。
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
        RETURN count(e) AS total,
               sum(CASE WHEN coalesce(e.stub,false) THEN 1 ELSE 0 END) AS stub
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, {"t": tenant_id, "k": kb_id})
            record = await result.single()
            if not record:
                return {"total": 0, "stub": 0, "ratio": 0.0}
            total = int(record["total"] or 0)
            stub = int(record["stub"] or 0)
            return {"total": total, "stub": stub,
                    "ratio": (stub / total) if total else 0.0}

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

    async def _count_entities_by_type_doc(
        self, tenant_id: str, kb_id: str, document_id: str
    ) -> dict[str, int]:
        """按 entity_type 聚合非 stub 实例数，仅统计 doc_ids 含指定文档的实体。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(e.stub,false)=false AND $doc_id IN e.doc_ids
        RETURN e.entity_type AS type, count(e) AS c
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher, {"t": tenant_id, "k": kb_id, "doc_id": document_id}
            )
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

    async def count_relations_by_type_doc(
        self, tenant_id: str, kb_id: str, document_id: str
    ) -> dict[str, int]:
        """按 relation_type 聚合关系数（两端均非 stub），仅统计 doc_ids 含指定文档的关系。"""
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (s:OntologyEntity {tenant_id:$t, kb_id:$k})-[r:ONT_REL]->(o:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(s.stub,false)=false AND coalesce(o.stub,false)=false AND $doc_id IN r.doc_ids
        RETURN r.relation_type AS type, count(r) AS c
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher, {"t": tenant_id, "k": kb_id, "doc_id": document_id}
            )
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
            # [jonex] M3-d：relation_attributes 在 fact 顶层，JSON 字符串 → dict（如 step_no）
            if isinstance(f.get("relation_attributes"), str):
                try:
                    decoded = json.loads(f["relation_attributes"])
                    f["relation_attributes"] = decoded if isinstance(decoded, dict) else {}
                except (json.JSONDecodeError, TypeError):
                    f["relation_attributes"] = {}

    # ══════════════════════════════════════════════════════════════════
    # P8（M3-c）procedural 步骤链专用模板
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _chain_step_no(rel_attributes) -> int:
        """从 HAS_STEP 边属性 JSON 取 step_no；非法/缺失 → 999（排序兜底排末尾）。"""
        attrs = rel_attributes
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except (json.JSONDecodeError, TypeError):
                attrs = {}
        step_no = (attrs or {}).get("step_no")
        try:
            return int(step_no)
        except (TypeError, ValueError):
            return 999

    @staticmethod
    def _facts_from_chain_rows(
        rows: list[dict], root_name: str, step_relation: str,
    ) -> list[dict]:
        """[jonex] P8（M3-c）：步骤链 Cypher 行 → facts（与 neighbors() 同构 + step_no）。

        排序在构造侧完成（计划 M3-c：answer_from_facts 函数体不动）：
        按 step_no 升序排列 rows（同 step_no 按名称稳定序）；每个 step 首次
        出现先产出步骤 fact，再产出其挂接 fact。同一 Action 多行挂接时
        步骤 fact 只产出一次。
        """
        facts: list[dict] = []
        seen_steps: set[str] = set()
        rows = sorted(rows, key=lambda r: (r.get("step_no", 999), r.get("step_name") or ""))
        for row in rows:
            step_name = row.get("step_name") or ""
            if not step_name:
                continue
            step_no = row.get("step_no", 999)
            if step_name not in seen_steps:
                seen_steps.add(step_name)
                facts.append({
                    "source": root_name,
                    "target": step_name,
                    "target_type": "Action",
                    "target_entity": {
                        "name": step_name, "type": "Action",
                        "aliases": row.get("aliases", []),
                        "description": row.get("description", ""),
                        "attributes": row.get("attributes", {}),
                        "confidence": row.get("confidence", 1.0),
                        "kb_id": row.get("kb_id", ""),
                        "doc_ids": row.get("doc_ids", []),
                        "source_chunks": row.get("source_chunks", []),
                        "extraction_method": row.get("extraction_method"),
                        # [jonex] S4（§3.4）：真行对象判据（见 search_entities）
                        "table_sig": row.get("table_sig"),
                    },
                    "relation_type": step_relation,
                    "relation_chain": [step_relation],
                    "relation_source_chunks": row.get("step_rel_chunks", []),
                    "relation_attributes": row.get("step_rel_attributes", {}),
                    "path": [root_name, step_name],
                    "direction": "outgoing",
                    "hop": 1,
                    "step_no": step_no,
                })
            hook_type = row.get("hook_type")
            target = row.get("target")
            if not hook_type or not target:
                continue
            facts.append({
                "source": step_name,
                "target": target,
                "target_type": row.get("target_type", "unknown"),
                "target_entity": {
                    "name": target, "type": row.get("target_type", "unknown"),
                    "aliases": row.get("target_aliases", []),
                    "description": row.get("target_description", ""),
                    "attributes": row.get("target_attributes", {}),
                    "confidence": row.get("target_confidence", 1.0),
                    "kb_id": row.get("kb_id", ""),
                    "doc_ids": row.get("target_doc_ids", []),
                    "source_chunks": row.get("target_source_chunks", []),
                    "extraction_method": row.get("target_extraction_method"),
                    # [jonex] S4（§3.4）：真行对象判据（见 search_entities）
                    "table_sig": row.get("target_table_sig"),
                },
                "relation_type": hook_type,
                "relation_chain": [hook_type],
                "relation_source_chunks": row.get("rel_chunks", []),
                "relation_attributes": row.get("rel_attributes", {}),
                "path": [step_name, target],
                "direction": "outgoing",
                "hop": 1,
                "step_no": step_no,
            })
        OntologyGraphRepository._deserialize_fact_entities(facts)
        return facts

    async def find_action_chain(
        self,
        tenant_id: str,
        kb_id: str,
        root_name: str,
        root_type: str,
        max_steps: int = 30,
    ) -> list[dict]:
        """[jonex] P8（M3-c）步骤链专用模板：不走 neighbors()（自环类型
        + per_hop_limit 截断会砍后半段、facts 按 confidence 而非步骤序）。

        root_type=="Skill"：沿 HAS_STEP 取步骤（step_no 边属性升序）；
        root_type=="Action"：沿 NEXT_STEP 走链（深度序号作 step_no）；
        每步展开 5 挂接（ACTS_ON/USES_TOOL/PERFORMED_BY/REQUIRES/RESULTS_IN）。
        返回与 neighbors() 同构的 facts（额外 step_no 字段，步骤序排列）。
        """
        tenant_id = require_tenant(tenant_id)
        max_steps = max(1, min(int(max_steps), 50))
        _HOOK_TYPES = ["ACTS_ON", "USES_TOOL", "PERFORMED_BY", "REQUIRES", "RESULTS_IN"]

        if root_type == "Skill":
            cypher = """
            MATCH (s:OntologyEntity {tenant_id:$t, kb_id:$k, canonical_name:$name,
                                     entity_type:'Skill'})
            OPTIONAL MATCH (s)-[r:ONT_REL {relation_type:'HAS_STEP'}]
                           ->(a:OntologyEntity {tenant_id:$t, kb_id:$k})
            WHERE coalesce(a.stub,false)=false
            OPTIONAL MATCH (a)-[r2:ONT_REL]->(o:OntologyEntity {tenant_id:$t, kb_id:$k})
            WHERE r2.relation_type IN $hooks AND coalesce(o.stub,false)=false
            RETURN a.canonical_name AS step_name,
                   r.attributes AS step_rel_attributes,
                   r.source_chunks AS step_rel_chunks,
                   a.aliases AS aliases, a.description AS description,
                   a.attributes AS attributes, a.confidence AS confidence,
                   a.doc_ids AS doc_ids, a.source_chunks AS source_chunks,
                   a.extraction_method AS extraction_method, a.kb_id AS kb_id,
                   // [jonex] S4（§3.4）：真行对象判据（见 search_entities）
                   a.table_sig AS table_sig,
                   r2.relation_type AS hook_type,
                   o.canonical_name AS target, o.entity_type AS target_type,
                   o.aliases AS target_aliases, o.description AS target_description,
                   o.attributes AS target_attributes,
                   o.confidence AS target_confidence,
                   o.doc_ids AS target_doc_ids,
                   o.source_chunks AS target_source_chunks,
                   o.extraction_method AS target_extraction_method,
                   o.table_sig AS target_table_sig,
                   r2.attributes AS rel_attributes,
                   r2.source_chunks AS rel_chunks
            """
        elif root_type == "Action":
            # 变长 NEXT_STEP 链：0..N（0 跳 = root 自身，其挂接也要展开）。
            # 上界沿用 neighbors() 的内联字面量做法（参数绑定不支持）。
            # 注意：Neo4j 5 不支持「内联属性 map + 变长」组合语法
            # （-[:ONT_REL {..}*0..N]-> 直接 SyntaxError），属性过滤必须
            # 移到 WHERE 里用 relationships(path) 逐边校验。
            cypher = f"""
            MATCH path = (a0:OntologyEntity {{tenant_id:$t, kb_id:$k,
                                               canonical_name:$name,
                                               entity_type:'Action'}})
                         -[rels:ONT_REL*0..{max_steps}]->(a:OntologyEntity {{tenant_id:$t, kb_id:$k}})
            WHERE all(rel IN rels WHERE rel.relation_type = 'NEXT_STEP')
              AND coalesce(a.stub,false)=false
            WITH path, a, length(path) AS depth
            OPTIONAL MATCH (a)-[r2:ONT_REL]->(o:OntologyEntity {{tenant_id:$t, kb_id:$k}})
            WHERE r2.relation_type IN $hooks AND coalesce(o.stub,false)=false
            RETURN a.canonical_name AS step_name,
                   depth AS depth,
                   a.aliases AS aliases, a.description AS description,
                   a.attributes AS attributes, a.confidence AS confidence,
                   a.doc_ids AS doc_ids, a.source_chunks AS source_chunks,
                   a.extraction_method AS extraction_method, a.kb_id AS kb_id,
                   // [jonex] S4（§3.4）：真行对象判据（见 search_entities）
                   a.table_sig AS table_sig,
                   r2.relation_type AS hook_type,
                   o.canonical_name AS target, o.entity_type AS target_type,
                   o.aliases AS target_aliases, o.description AS target_description,
                   o.attributes AS target_attributes,
                   o.confidence AS target_confidence,
                   o.doc_ids AS target_doc_ids,
                   o.source_chunks AS target_source_chunks,
                   o.extraction_method AS target_extraction_method,
                   o.table_sig AS target_table_sig,
                   r2.attributes AS rel_attributes,
                   r2.source_chunks AS rel_chunks
            ORDER BY depth ASC, hook_type ASC, target ASC
            """
        else:
            return []

        async with self._driver.session() as session:
            result = await session.run(
                cypher,
                {"t": tenant_id, "k": kb_id, "name": root_name, "hooks": _HOOK_TYPES},
            )
            records = await result.data()

        # 构造侧排序在 _facts_from_chain_rows 内完成（step_no 升序）；
        # 这里只做 step_no 提取与 Action 菱形链去重。
        rows: list[dict] = []
        seen_rows: set[tuple] = set()
        for rec in records:
            row = dict(rec)
            if root_type == "Skill":
                row["step_no"] = self._chain_step_no(row.get("step_rel_attributes"))
            else:
                # 菱形链同一节点取最小深度（ORDER BY depth 保证首见即最小）
                row["step_no"] = int(row.get("depth", 0)) + 1
                # 同一 (step, hook, target) 挂接在菱形链里会按多个 depth 重复出现，
                # 首见（最小 depth）为准，其余丢弃
                key = (row.get("step_name"), row.get("hook_type"), row.get("target"))
                if key in seen_rows:
                    continue
                seen_rows.add(key)
            rows.append(row)

        step_relation = "HAS_STEP" if root_type == "Skill" else "NEXT_STEP"
        # 反序列化在 _facts_from_chain_rows 内完成（转换自洽）
        return self._facts_from_chain_rows(rows, root_name, step_relation)

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
        document_id: Optional[str] = None,
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
            document_id: 仅返回 doc_ids 包含该文档的节点（None 表示不过滤）

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
        doc_filter = ""
        params: dict = {"t": tenant_id, "k": kb_id, "limit": limit}
        if entity_types:
            type_filter = "AND e.entity_type IN $types"
            params["types"] = entity_types
        if document_id:
            doc_filter = "AND $doc_id IN e.doc_ids"
            params["doc_id"] = document_id

        # 1) 全量分类型计数（无视 limit，用于侧栏筛选与总数提示）
        if document_id:
            type_counts = await self._count_entities_by_type_doc(
                tenant_id, kb_id, document_id
            )
        else:
            type_counts = await self.count_entities_by_type(tenant_id, kb_id)
        if entity_types:
            total_nodes = sum(type_counts.get(t, 0) for t in entity_types)
        else:
            total_nodes = sum(type_counts.values())

        # 2) 按连接度排序取 top-N 节点（连接度高的是图谱枢纽，优先展示）
        node_cypher = f"""
        MATCH (e:OntologyEntity {{tenant_id:$t, kb_id:$k}})
        WHERE coalesce(e.stub,false)=false {type_filter} {doc_filter}
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

        # 3) 全量关系总数（受类型筛选 + 文档筛选约束：两端均在筛选范围内）
        rel_where = "coalesce(s.stub,false)=false AND coalesce(o.stub,false)=false"
        rel_params: dict = {"t": tenant_id, "k": kb_id}
        if entity_types:
            rel_where += " AND s.entity_type IN $types AND o.entity_type IN $types"
            rel_params["types"] = entity_types
        if document_id:
            rel_where += " AND $doc_id IN s.doc_ids AND $doc_id IN o.doc_ids"
            rel_params["doc_id"] = document_id
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
        document_id: Optional[str] = None,
    ) -> dict:
        """展开某个实体的一跳邻居，返回邻居节点 + 连接边（同 get_kb_graph 结构）。

        前端双击节点时调用，把新邻居增量并入当前图。
        传入 document_id 时，仅保留 doc_ids 含该文档的邻居节点（文档级下钻）。
        """
        tenant_id = require_tenant(tenant_id)
        where_clause = "coalesce(n.stub,false)=false"
        params = {
            "t": tenant_id, "k": kb_id,
            "etype": entity_type, "name": canonical_name, "limit": limit,
        }
        if document_id:
            where_clause += " AND $doc_id IN n.doc_ids"
            params["doc_id"] = document_id
        cypher = (
            "MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$etype, canonical_name:$name})\n"
            "MATCH (e)-[r:ONT_REL]-(n:OntologyEntity {tenant_id:$t, kb_id:$k})\n"
            f"WHERE {where_clause}\n"
            "WITH e, r, n, startNode(r) AS sn, endNode(r) AS en\n"
            "RETURN n.entity_type + ':' + n.canonical_name AS id,\n"
            "       n.canonical_name AS name,\n"
            "       n.entity_type AS type,\n"
            "       n.aliases AS aliases,\n"
            "       n.attributes AS attributes,\n"
            "       n.description AS description,\n"
            "       n.confidence AS confidence,\n"
            "       n.doc_ids AS doc_ids,\n"
            "       sn.canonical_name AS source,\n"
            "       sn.entity_type AS source_type,\n"
            "       en.canonical_name AS target,\n"
            "       en.entity_type AS target_type,\n"
            "       r.relation_type AS label,\n"
            "       r.confidence AS rel_confidence\n"
            "LIMIT $limit"
        )
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

    async def _get_entity_embedding_state(
        self, tenant_id: str, kb_id: str, entity_type: str, canonical_name: str
    ) -> Optional[dict]:
        """读取实体当前 aliases/description/embedding_hash，供编辑时重算 embedding。

        实体不存在时返回 None（由调用方决定是否跳过向量重算，主编辑流程仍会按
        无行命中抛 ResourceNotFoundError）。
        """
        tenant_id = require_tenant(tenant_id)
        cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$et, canonical_name:$cn})
        RETURN e.aliases AS aliases, e.description AS description,
               e.embedding_hash AS embedding_hash
        """
        async with self._driver.session() as session:
            result = await session.run(
                cypher, {"t": tenant_id, "k": kb_id, "et": entity_type, "cn": canonical_name}
            )
            record = await result.single()
        if not record:
            return None
        return {
            "aliases": record["aliases"] or [],
            "description": record["description"] or "",
            "embedding_hash": record["embedding_hash"],
        }

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
               node.extraction_method AS extraction_method,
               // [jonex] S4（§3.4 排序豁免）：真行对象判据（见 search_entities）
               node.table_sig AS table_sig,
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
        doc_id: Optional[str] = None,
    ) -> dict:
        """创建本体实体实例节点。

        使用 MERGE 确保幂等：已存在的节点**不更新其它字段**（要改走 update_entity），
        但 `doc_id` 有值时会把该文档补进已有节点的 `doc_ids`（见下）。
        与 merge_entity 不同：不处理 source_chunks/lightrag_doc_ids 等文档写入字段，
        但会写 embedding 并标记 extraction_method='manual'。

        `doc_id` 决定节点归属哪个入口建的：
        - **有值**（文档结果页建）→ `doc_ids=[doc_id]`；节点已存在时**追加**该 doc
          （该实体本来是从别的文档抽出来的，现在这份文档也要能按文档过滤看到它）；
        - **None**（KB 级「编译资产」tab 建）→ `doc_ids=[]`，全库实例，不属于任何文档；
          此时不碰已有节点的 `doc_ids`（不能把别人已有的文档关联抹掉）。

        Args:
            tenant_id: 租户 ID
            kb_id: 知识库 ID
            entity_type: 实体类型
            canonical_name: 规范名
            aliases: 别名列表
            description: 描述
            attributes: 属性 dict
            doc_id: 来源文档 ID（可选；决定 doc_ids 与是否追加）

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

        # 手动实体写侧补齐：生成 embedding 并标记 extraction_method='manual'，
        # 使向量召回（ont_entity_embedding 索引）能命中、检索侧 _is_stub 能区分手动实体。
        # 幂等由 MERGE 保证（仅 ON CREATE 写入）；embed 失败返回 None 时仍正常创建，仅不带向量。
        embedding = None
        embedding_hash_val = None
        if os.getenv("ONTOLOGY_VECTOR_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
            text = build_embed_text(canonical_name, aliases, description or "")
            if text:
                h = embed_hash(text)
                vec = await embed(
                    text, tenant_id=tenant_id, kb_id=kb_id,
                    trace_id=f"ontology_manual:{kb_id}",
                )
                if vec is not None:
                    embedding = vec
                    embedding_hash_val = h

        cypher = """
        MERGE (e:OntologyEntity {tenant_id:$tenant_id, kb_id:$kb_id, entity_type:$entity_type, canonical_name:$canonical_name})
        ON CREATE SET
            e.aliases=$aliases, e.aliases_text=$aliases_text,
            e.description=$description, e.attributes=$attributes,
            e.stub=false, e.confidence=1.0,
            e.doc_ids=$doc_ids, e.source_chunks=[], e.lightrag_doc_ids=[],
            e.extraction_method='manual',
            e.embedding=$embedding, e.embedding_hash=$embedding_hash,
            e.created_at=timestamp(), e.updated_at=timestamp()
        ON MATCH SET
            e.doc_ids=CASE WHEN $doc_id IS NOT NULL
                THEN apoc.coll.toSet(coalesce(e.doc_ids,[])+[$doc_id]) ELSE e.doc_ids END,
            e.updated_at=CASE WHEN $doc_id IS NOT NULL
                THEN timestamp() ELSE e.updated_at END
        RETURN e
        """
        params = {
            "tenant_id": tenant_id, "kb_id": kb_id,
            "entity_type": entity_type, "canonical_name": canonical_name,
            "aliases": aliases, "aliases_text": aliases_text,
            "description": description, "attributes": attributes_json,
            "embedding": embedding, "embedding_hash": embedding_hash_val,
            # doc_id 与 doc_ids 一起下发：前者判「本次是否来自某个文档」，后者是 ON CREATE 的初值
            "doc_id": doc_id or None,
            "doc_ids": [doc_id] if doc_id else [],
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
        doc_id: Optional[str] = None,
    ) -> dict:
        """创建实体间关系。

        两端实体如不存在则创建 stub 节点。关系使用 MERGE 确保幂等。

        `doc_id` 口径与 `create_entity` 一致（有值=来自文档结果页，None=KB 级资产页）：
        - 关系边：ON CREATE 写 `doc_ids=[doc_id]`；已存在则**追加**该 doc
          （该关系本来是从别的文档抽出来的，这份文档也要能按文档过滤看到它）；
        - 两端**新建的 stub**：同样带上该 doc（与抽取侧 merge_relation 一致）；
          已存在的非 stub 实体不动其 `doc_ids` —— 别人的文档归属不该因为这里画了条关系
          而被改写（对齐 merge_relation 的 `coalesce(a.stub,false)=true` 判据）。

        Args:
            tenant_id: 租户 ID
            kb_id: 知识库 ID
            source_entity_type: 源实体类型
            source_canonical_name: 源实体规范名
            relation_type: 关系类型
            target_entity_type: 目标实体类型
            target_canonical_name: 目标实体规范名
            attributes: 属性 dict
            doc_id: 来源文档 ID（可选）

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
            s.confidence=0.1, s.doc_ids=$doc_ids, s.source_chunks=[], s.lightrag_doc_ids=[],
            s.created_at=timestamp(), s.updated_at=timestamp()
        ON MATCH SET
            s.doc_ids=CASE WHEN $doc_id IS NOT NULL AND coalesce(s.stub,false)=true
                THEN apoc.coll.toSet(coalesce(s.doc_ids,[])+[$doc_id]) ELSE s.doc_ids END
        MERGE (o:OntologyEntity {tenant_id:$tenant_id, kb_id:$kb_id, entity_type:$target_type, canonical_name:$target_name})
        ON CREATE SET
            o.stub=true, o.description='', o.attributes='{}',
            o.aliases=[], o.aliases_text='',
            o.confidence=0.1, o.doc_ids=$doc_ids, o.source_chunks=[], o.lightrag_doc_ids=[],
            o.created_at=timestamp(), o.updated_at=timestamp()
        ON MATCH SET
            o.doc_ids=CASE WHEN $doc_id IS NOT NULL AND coalesce(o.stub,false)=true
                THEN apoc.coll.toSet(coalesce(o.doc_ids,[])+[$doc_id]) ELSE o.doc_ids END
        MERGE (s)-[r:ONT_REL {relation_type:$relation_type}]->(o)
        ON CREATE SET
            r.confidence=1.0, r.attributes=$attributes,
            r.doc_ids=$doc_ids, r.lightrag_doc_ids=[],
            r.created_at=timestamp(), r.updated_at=timestamp()
        ON MATCH SET
            r.doc_ids=CASE WHEN $doc_id IS NOT NULL
                THEN apoc.coll.toSet(coalesce(r.doc_ids,[])+[$doc_id]) ELSE r.doc_ids END,
            r.updated_at=CASE WHEN $doc_id IS NOT NULL
                THEN timestamp() ELSE r.updated_at END
        RETURN r
        """
        params = {
            "tenant_id": tenant_id, "kb_id": kb_id,
            "source_type": source_entity_type, "source_name": source_canonical_name,
            "relation_type": relation_type,
            "target_type": target_entity_type, "target_name": target_canonical_name,
            "attributes": attributes_json,
            # 见 create_entity 的同名参数：doc_id 判「本次是否来自某个文档」，doc_ids 是初值
            "doc_id": doc_id or None,
            "doc_ids": [doc_id] if doc_id else [],
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

        # 编辑触发向量重算：仅 name/aliases/description 影响 embedding 文本
        # （attributes 不进 build_embed_text，与 merge_entity 口径一致）。
        # 文本变化才调 embed；embed 失败返回 None 时不更新向量（保留旧值），不阻断编辑。
        embedding = None
        embedding_hash_val = None
        recompute_embedding = bool(
            {"name", "aliases", "description"} & set(updates.keys())
        )
        if recompute_embedding and os.getenv("ONTOLOGY_VECTOR_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
            cur = await self._get_entity_embedding_state(
                tenant_id, kb_id, entity_type, canonical_name
            )
            if cur is not None:
                new_name = updates.get("name", canonical_name)
                new_aliases = updates.get("aliases", cur["aliases"])
                new_description = updates.get("description", cur["description"])
                text = build_embed_text(new_name, new_aliases, new_description)
                if text:
                    new_hash = embed_hash(text)
                    if new_hash != cur.get("embedding_hash"):
                        vec = await embed(
                            text, tenant_id=tenant_id, kb_id=kb_id,
                            trace_id=f"ontology_manual:{kb_id}",
                        )
                        if vec is not None:
                            embedding = vec
                            embedding_hash_val = new_hash

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

        if recompute_embedding and embedding is not None:
            set_clauses.append("e.embedding=$embedding")
            set_clauses.append("e.embedding_hash=$embedding_hash")
            params["embedding"] = embedding
            params["embedding_hash"] = embedding_hash_val

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

    async def backfill_manual_entities(self, tenant_id: str, kb_id: str) -> int:
        """回填存量手动实体：补 extraction_method='manual' 标记与 embedding。

        识别「非 stub、无 doc_ids、无 embedding」且 extraction_method 为空（早期手动
        创建、未走编译管线）或已为 'manual'（新路径 embed 瞬时失败）的实体，逐个补
        标记 + 向量。单 session 内完成 find + 全部 set，避免逐实体开 session。
        embed 失败时仅补标记不补向量。返回回填的实体数量。
        """
        tenant_id = require_tenant(tenant_id)

        find_cypher = """
        MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
        WHERE coalesce(e.stub,false)=false
          AND (e.extraction_method IS NULL OR e.extraction_method='manual')
          AND size(coalesce(e.doc_ids,[]))=0
          AND e.embedding IS NULL
        RETURN e.entity_type AS entity_type, e.canonical_name AS canonical_name,
               e.aliases AS aliases, e.description AS description
        """
        async with self._driver.session() as session:
            result = await session.run(find_cypher, {"t": tenant_id, "k": kb_id})
            candidates = [dict(rec) async for rec in result]

            count = 0
            for c in candidates:
                entity_type = c["entity_type"]
                canonical_name = c["canonical_name"]
                aliases = c["aliases"] or []
                description = c["description"] or ""

                embedding = None
                embedding_hash_val = None
                if os.getenv("ONTOLOGY_VECTOR_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
                    text = build_embed_text(canonical_name, aliases, description)
                    if text:
                        h = embed_hash(text)
                        vec = await embed(
                            text, tenant_id=tenant_id, kb_id=kb_id,
                            trace_id=f"ontology_manual:{kb_id}",
                        )
                        if vec is not None:
                            embedding = vec
                            embedding_hash_val = h

                set_cypher = """
                MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k, entity_type:$entity_type, canonical_name:$canonical_name})
                SET e.extraction_method='manual'"""
                set_params = {
                    "t": tenant_id, "k": kb_id,
                    "entity_type": entity_type, "canonical_name": canonical_name,
                }
                if embedding is not None:
                    set_cypher += ", e.embedding=$embedding, e.embedding_hash=$embedding_hash"
                    set_params["embedding"] = embedding
                    set_params["embedding_hash"] = embedding_hash_val
                await session.run(set_cypher, set_params)
                count += 1

        return count

    async def backfill_manual_entities_all(self) -> int:
        """全局回填存量手动实体（启动时后台调用，跨所有租户/KB）。

        扫描全图中「非 stub、无 doc_ids、无 embedding」且 extraction_method 为空或
        已为 'manual' 的手动实体，按 (tenant_id, kb_id) 分组复用单 KB 回填。幂等：
        回填后不再命中识别条件。单个租户/KB 回填失败仅告警并跳过，不阻塞其余。
        返回回填总数。
        """
        find_pairs = """
        MATCH (e:OntologyEntity)
        WHERE coalesce(e.stub,false)=false
          AND (e.extraction_method IS NULL OR e.extraction_method='manual')
          AND size(coalesce(e.doc_ids,[]))=0
          AND e.embedding IS NULL
        RETURN DISTINCT e.tenant_id AS tenant_id, e.kb_id AS kb_id
        """
        async with self._driver.session() as session:
            result = await session.run(find_pairs)
            pairs = [dict(rec) async for rec in result]

        total = 0
        for p in pairs:
            tenant_id = p.get("tenant_id")
            kb_id = p.get("kb_id")
            if not tenant_id or not kb_id:
                continue
            try:
                total += await self.backfill_manual_entities(tenant_id, kb_id)
            except Exception as exc:
                logger.warning(
                    "回填手动实体失败（跳过） tenant=%s kb=%s: %s",
                    tenant_id, kb_id, exc,
                )
        return total

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

    async def list_document_provenance(
        self, tenant_id: str, kb_id: str, doc_id: str, limit: int = 100,
    ) -> tuple[list[dict], list[dict]]:
        """只读：某文档参与产出的本体实体与关系（数据治理血缘消费）。

        - 实体命中判据：``$doc_id IN coalesce(e.doc_ids, [])``；排除 stub
          （``merge_relation`` 的端点兜底产物不是正式实体，与同仓
          ``count_entities_by_document`` / ``count_relations_by_type_doc`` /
          ``get_kb_graph`` 口径一致）。
        - 关系命中判据：``$doc_id IN coalesce(r.doc_ids, [])``（边级精确过滤，与
          同仓 ``list_relations(document_id=...)`` 口径一致）；两端节点都钉
          ``tenant_id/kb_id`` 且都排除 stub（否则会返回悬挂边 / 跨租户边）。
          **不做端点 ``a.doc_ids`` 过滤**：从文档页 ``create_relation`` 建边时只给
          ``r.doc_ids`` 追加本 doc，不会补已存在的非 stub 端点；``merge_relation``
          的 ``ON MATCH`` 对非 stub 端点同样不追加。因此「边含本 doc、端点多 doc
          不含」是正常数据，端点条件会静默漏掉合法关系（端点完整性由抽取侧回填
          负责，读侧按边 doc_ids 过滤）。
        - 两条查询都 ``RETURN DISTINCT``，在 ``LIMIT`` 之前先压缩完全重复行，
          避免重复行占满 ``limit + 1`` 窗口导致 service 误判「未截断」（实体返回
          列含 ``source_chunks``，同名同型但出处不同的行仍由 service 层按业务键去重）。
        - 两条查询都有确定性排序（实体按 ``canonical_name, entity_type``，关系按
          ``source, target, relation_type``），保证 ``LIMIT`` 截断结果可复现。
        - ``source_chunks`` 原样返回（写入侧是 JSON 字符串），归一在 service 层。

        成本（治理侧按文档循环调用时需知）：``doc_ids`` 是列表属性、**没有索引**，
        本查询是 **KB 级扫描**；关系查询只能按边属性过滤，同样不是索引下钻。
        当前保持单 session 串行两条查询（简单优先）；大 KB / 高并发场景再评估
        并行双 session。

        只发 MATCH/RETURN/LIMIT，不写图。
        """
        tenant_id = require_tenant(tenant_id)
        params = {"t": tenant_id, "k": kb_id, "doc_id": doc_id, "limit": limit}
        async with self._driver.session() as session:
            ent = await session.run(
                """
                MATCH (e:OntologyEntity {tenant_id:$t, kb_id:$k})
                WHERE $doc_id IN coalesce(e.doc_ids, [])
                  AND coalesce(e.stub,false)=false
                RETURN DISTINCT e.canonical_name AS canonical_name,
                       e.entity_type AS entity_type,
                       e.source_chunks AS source_chunks
                ORDER BY e.canonical_name, e.entity_type
                LIMIT $limit
                """,
                params,
            )
            entities = [dict(r) async for r in ent]
            rel = await session.run(
                """
                MATCH (a:OntologyEntity {tenant_id:$t, kb_id:$k})-[r:ONT_REL]->
                      (b:OntologyEntity {tenant_id:$t, kb_id:$k})
                WHERE $doc_id IN coalesce(r.doc_ids, [])
                  AND coalesce(a.stub,false)=false
                  AND coalesce(b.stub,false)=false
                RETURN DISTINCT a.canonical_name AS source,
                       b.canonical_name AS target,
                       r.relation_type AS relation_type
                ORDER BY source, target, relation_type
                LIMIT $limit
                """,
                params,
            )
            relations = [dict(r) async for r in rel]
        return entities, relations


__all__ = ["OntologyGraphRepository"]
