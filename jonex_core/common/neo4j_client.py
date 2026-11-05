"""
Neo4j 图数据库连接管理

提供进程内单例 AsyncDriver + schema 初始化（约束 + 全文索引）。
knowledge-base-service 启动时调用 ensure_ontology_schema() 完成初始化。
"""

import logging
import os
from typing import Optional

from neo4j import AsyncGraphDatabase, AsyncDriver

logger = logging.getLogger(__name__)

# 进程内驱动单例
_driver: Optional[AsyncDriver] = None


def get_neo4j_driver() -> AsyncDriver:
    """获取 AsyncDriver 单例（懒加载）。"""
    global _driver
    if _driver is None:
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        username = os.getenv("NEO4J_USERNAME", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "change-me")
        _driver = AsyncGraphDatabase.driver(uri, auth=(username, password))
        logger.info("Neo4j 驱动已创建: %s", uri)
    return _driver


async def close_neo4j_driver() -> None:
    """关闭驱动单例（进程退出或 shutdown 时调用）。

    注意：AsyncDriver.close() 是协程，必须 await。
    """
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None
        logger.info("Neo4j 驱动已关闭")


async def ensure_ontology_schema() -> None:
    """初始化本体 schema：约束 + 全文索引（幂等 + analyzer 自动迁移）。

    在 knowledge-base-service 启动时调用，失败仅告警不阻塞服务启动。

    注意：``CREATE FULLTEXT INDEX ... IF NOT EXISTS`` 无法变更已存在索引的
    analyzer。若历史上以默认 analyzer 建过 ont_entity_ft，中文会被按单字切分，
    与查询端 cjk bigram 粒度不一致，导致本体检索召回极差。故这里检测现有
    analyzer，不是 cjk 时 DROP 重建（全文索引重建会自动回填现有节点）。
    """
    driver = get_neo4j_driver()
    async with driver.session() as session:
        # 复合唯一键约束（MERGE 依赖）
        await session.run(
            "CREATE CONSTRAINT ont_entity_key IF NOT EXISTS "
            "FOR (e:OntologyEntity) "
            "REQUIRE (e.tenant_id, e.kb_id, e.entity_type, e.canonical_name) IS UNIQUE"
        )

        # [jonex] 引用命中索引：上面的唯一约束复合索引列序是
        # (tenant_id, kb_id, entity_type, canonical_name)，复合索引要求连续前缀——
        # 查询若只给 tenant_id + kb_id + canonical_name（不带 entity_type），前缀在
        # 第 3 列断裂，planner 只能用到 (tenant_id, kb_id)，退化为「扫该 KB 全部节点」。
        # 引用命中（find_reference_hits）的候选值上限 3000，乘上 KB 节点数就是上亿次
        # 属性比较：线上实测单条 Cypher 满核跑 11 分钟未完成，把串行的对账循环一起
        # 卡死。这里补一条与谓词同序的索引，让 canonical_name 等值命中走 seek。
        # 注：`v IN e.aliases` 那半段 Neo4j 常规索引不支持列表成员查找，仍需逐节点
        # 过滤，故 find_reference_hits 改为两段式 + 调用方超时兜底。
        await session.run(
            "CREATE INDEX ont_entity_name IF NOT EXISTS "
            "FOR (e:OntologyEntity) ON (e.tenant_id, e.kb_id, e.canonical_name)"
        )
        logger.info("Neo4j 本体实体名索引就绪 ont_entity_name")

        # P1.5a：数据资产 schema 子图（独立 label，与 OntologyEntity 完全隔离）
        await session.run(
            "CREATE CONSTRAINT ds_entity_key IF NOT EXISTS "
            "FOR (e:DataSourceEntity) "
            "REQUIRE (e.tenant_id, e.kb_id, e.data_source_id, e.table) IS UNIQUE"
        )
        logger.info("Neo4j 数据资产唯一约束就绪 ds_entity_key")

        # 读取现有全文索引的 analyzer
        index_exists = False
        existing_analyzer: Optional[str] = None
        result = await session.run(
            "SHOW INDEXES YIELD name, options "
            "WHERE name = 'ont_entity_ft' "
            "RETURN options AS options"
        )
        record = await result.single()
        if record is not None:
            index_exists = True
            options = record["options"] or {}
            index_config = options.get("indexConfig") or {}
            existing_analyzer = index_config.get("fulltext.analyzer")

        # 旧索引 analyzer 非 cjk → DROP 重建（IF NOT EXISTS 无法改 analyzer）
        if index_exists and existing_analyzer != "cjk":
            logger.warning(
                "全文索引 ont_entity_ft 当前 analyzer=%s，非 cjk，执行 DROP 后重建",
                existing_analyzer,
            )
            await session.run("DROP INDEX ont_entity_ft IF EXISTS")
            index_exists = False

        if not index_exists:
            # 全文索引（cjk analyzer 确保中文按 bigram 切分，与查询端粒度一致）
            await session.run(
                "CREATE FULLTEXT INDEX ont_entity_ft IF NOT EXISTS "
                "FOR (e:OntologyEntity) ON EACH [e.canonical_name, e.aliases_text] "
                "OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}}"
            )
            logger.info("Neo4j 本体 schema 初始化完成（约束 + cjk 全文索引，新建/重建）")
        else:
            logger.info("Neo4j 本体全文索引已是 cjk analyzer，跳过重建")

        # ── 向量索引（语义召回；维度必须与 EMBEDDING_DIM 一致）──
        ontology_vector_enabled = os.getenv("ONTOLOGY_VECTOR_ENABLED", "true").lower() in ("1", "true", "yes", "on")
        if ontology_vector_enabled:
            dim = int(os.getenv("EMBEDDING_DIM", "1024"))

            # 1) 读现有向量索引的维度（仿 analyzer 迁移：SHOW INDEXES → indexConfig）
            vindex_exists = False
            existing_dim = None
            result = await session.run(
                "SHOW INDEXES YIELD name, options "
                "WHERE name = 'ont_entity_embedding' RETURN options AS options"
            )
            record = await result.single()
            if record is not None:
                vindex_exists = True
                index_config = (record["options"] or {}).get("indexConfig") or {}
                existing_dim = index_config.get("vector.dimensions")

            # 2) 维度不符 → DROP 重建（IF NOT EXISTS 无法改维度，与 analyzer 同理）
            if vindex_exists and existing_dim is not None and int(existing_dim) != dim:
                logger.warning(
                    "向量索引 ont_entity_embedding 维度=%s ≠ EMBEDDING_DIM=%d，DROP 后重建",
                    existing_dim, dim,
                )
                await session.run("DROP INDEX ont_entity_embedding IF EXISTS")
                vindex_exists = False

            # 3) 建索引。dim 来自 int(os.getenv(...))，受控无注入风险，用 f-string 拼维度
            #    以兼容 Neo4j 5.x 各版本对 indexConfig 参数化的不同支持。
            if not vindex_exists:
                await session.run(
                    "CREATE VECTOR INDEX ont_entity_embedding IF NOT EXISTS "
                    "FOR (e:OntologyEntity) ON (e.embedding) "
                    "OPTIONS {indexConfig: {"
                    f"  `vector.dimensions`: {dim}, "
                    "  `vector.similarity_function`: 'cosine'"
                    "}}"
                )
                logger.info("Neo4j 本体向量索引就绪 ont_entity_embedding dim=%d cosine", dim)
            else:
                logger.info("Neo4j 本体向量索引维度已匹配（dim=%d），跳过重建", dim)

        # ── M3-e：ONT_REL 关系属性索引（skill-action 批 1）──
        # 按 relation_type 过滤此前是 KB 内全边扫描。关系属性索引在较老 Neo4j
        # 版本不支持，失败仅告警不阻塞其他 schema 初始化。
        try:
            await session.run(
                "CREATE INDEX ont_rel_type IF NOT EXISTS "
                "FOR ()-[r:ONT_REL]-() ON (r.relation_type)"
            )
            logger.info("Neo4j 关系属性索引就绪 ont_rel_type")
        except Exception as exc:
            logger.warning("关系属性索引 ont_rel_type 创建失败（版本可能不支持）: %s", exc)