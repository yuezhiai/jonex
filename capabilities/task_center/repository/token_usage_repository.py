#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务级 Token 消耗只读聚合 —— 跨 schema 查 knowledge_base / metering。

口径（2026-11-11 定的按文档归集方案）：

* 任务与文档是 **1:1**（`knowledge_base.task_center_documents.document_id` 是主键、
  `task_id` 有唯一索引 `uq_kb_tc_doc_task`），归属行 release 之后不删除；
* Token 按 **文档** 归集，不按 trace：ingest 的计量行（含 embedding 的 `emb-agg`
 聚合行）都带 `doc_id`，而 `trace_id` 在异步 RAG 任务里恒等于 `rag_task_id`，
  与任务中心的 `execution_id` 没有关系（实测 `execution_id` 下 0 行）；
* **不设时间窗，按 `doc_id` 取该文档全量**（2026-11-12 修正）。
  原先用 attempt 合并窗口 `[MIN(started_at), MAX(finished_at|now()) + 10s)`，但
  **本体抽取是 knowledge-base 侧的后置异步动作**，它在任务 `finished_at` 之后才
  跑完（57 实测：任务 16:28:52 收口、`ontology_status` 到 16:37 才 ready，窗口外
  仍有 8,482 tokens，非 md 文件低估约 11%）。去掉窗口**不引入污染**：检索类调用
  （`*_query` / `*_rerank` / `rag_*`）的计量行不带 `doc_id`，按 `doc_id` 过滤天然
  只命中入库消耗；同一文档重解析产生的计量行本就该累加（那是真花掉的钱）。
  `emb-agg` 行 `ON CONFLICT` 只累加 token、不改 `created_at` —— 全量 `SUM` 不受影响，
  「逐 attempt 相加会算漏算重」的顾虑随之消失；
* 只统计 `is_estimated = false` 的行，`SUM(total_tokens)`；
* **分阶段拆分**（2026-11-12 加）：按计量行 `scene` 把「本体抽取 / 本体向量化」
  与其余（解析侧向量化 `lightrag_embed` 等）拆成两段返回，前端分列展示。
  本体阶段白名单见 `ONTOLOGY_STAGE_SCENES`：`ontology_extract` 由
  `jonex_core/capability/atomic/rag/ontology_extractor.py` 发出、`ontology_embed`
  由 `jonex_core/common/ontology_embedding.py` 发出。查询期的
  `ontology_arbitration` / `ontology_qa` **不在**白名单（那是问答消耗，且不带 `doc_id`）；
* **整库编译段（2026-11-12 晚加，产品决定）**：`jonex_wiki` 库编译时会做整库动作
  （同名归属判定、目录生成、修复轮复核），这些计量行**只有 `kb_id`、没有 `doc_id`**，
  按文档归集永远归不到。现在按「同一次引擎编译」把它们平摊给该批文档：
  - 组 = 同一 `(tenant_id, knowledge_base_id, knowledge_documents.llm_wiki_task_id)`；
    实测同批 4 份文档共享一个 `task-xxxx`（引擎合并 ingest，不是一份一次）；
  - 窗口 = `[MIN(llm_wiki_compile_requested_at), MAX(llm_wiki_compiled_at)]`，
    实测一次编译约 31 秒；
  - 桶 = 该 KB、窗口内、`doc_id IS NULL`、`scene ∈ WIKI_KB_STAGE_SCENES` 的合计；
  - 单文档值 = `ROUND(桶 / 该批文档数)`。平摊保证「页面各份相加 = 真实账单」。
  - **软删文档不参与**（2026-11-17 定）：被软删文档的接管行仍展示自己的文档级计量，
    但**不再分整库桶** —— 分母已按 `is_deleted = 0` 只数未删文档，分子若仍给它一份，
    可见各份相加会超过真实桶（4 份删 1 份时 4B/3）。实现在 `doc_map` 的 CASE。
  查询期（`concept_filter` / 检索 / 问答）**一律不计** —— 那是别人看文档时花的钱，
  碰巧在窗口内也不能算到这次上传头上；
* **没有计量行返回 `None`，不返回 0** —— 与平台监控 `sum_token_usage` 同口径，
  避免把「算不出来」伪装成「0 Token」。

跨 schema 只读的纪律同 `space_repository.py`：任务中心不 import 其他 capability 的
ORM / service，一律 `sqlalchemy.text()` 原始 SQL，写路径一概不碰。

⚠️ **前提**：逐文档口径依赖 ingest 计量行带 `doc_id`（ontology / lightrag 实测全带；
wiki 的源文档理解 2026-11-12 起也带）。**库级**那部分没有 `doc_id`，改由
「同批 + 编译窗口」归集并平摊（见上文「整库编译段」），不再显示「-」。
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: 本体入库阶段的计量 `scene` 白名单（2026-11-12 加）。
#: 只有这两个 scene 属于「本体抽取 / 本体向量化」；查询期的
#: `ontology_arbitration`（多跳仲裁）、`ontology_qa`（本体问答）不在这里 ——
#: 它们既不是入库消耗，也不带 `doc_id`，按文档归集时天然不会被选中。
ONTOLOGY_STAGE_SCENES = ("ontology_extract", "ontology_embed")

#: Wiki 编译阶段的计量 `scene` 白名单（2026-11-12 加）。
#: `jonex_wiki_source_understanding` 是**逐 passage / 逐文档**调用，能带 `doc_id`；
#: `jonex_wiki_compile_organization_identity` 与 `jonex_wiki_outline` 是**库级**
#: 归并动作（跨文档判身份、生成目录），计量行没有 `doc_id` —— 它们按文档永远
#: 归不到任何一行，这是 wiki 管线的结构事实，不是这里能补的。
#: 查询期的 `jonex_wiki_query_*` 不在白名单（那是问答消耗，且不带 `doc_id`）。
WIKI_STAGE_SCENES = (
    "jonex_wiki_source_understanding",
    "jonex_wiki_compile_organization_identity",
    "jonex_wiki_outline",
)

#: **整库编译**阶段的计量 `scene` 白名单（2026-11-12 晚加）。
#:
#: 这些调用一次服务整个知识库（或其一批），计量行**没有 `doc_id`**，只能靠
#: 「同批 + 编译窗口」归集。逐条依据：
#:
#: * `jonex_wiki_compile_organization_identity` —— `domain/engine/compile/organization.py`
#:   的同名歧义归属判定，一次编译最多 8 次调用，判的是「这份材料属于哪个已有页面」；
#: * `jonex_wiki_compile_repair` —— `domain/engine/compile/error_book.py` 的有界修复轮；
#: * `jonex_wiki_compile_review` —— 修复轮之后的语义复核；
#: * `jonex_wiki_outline` —— `domain/outline/service.py` 的整库阅读目录，实测落在
#:   编译窗口内；它由编译完成后另投的 outline 任务发出，窗口外会漏（已知边界）；
#: * `jonex_wiki_source_understanding` —— 逐 passage 调用，**带 `doc_id` 时归文档级**
#:   走 `WIKI_STAGE_SCENES`；只有历史行（wiki 镜像重建前）没有 `doc_id` 时才回退进
#:   整库桶，避免这段消耗凭空消失。
#:
#: ⛔ 不纳入：`jonex_wiki_query_*`（检索）、`semantic_index` / `scenario_interpret`
#: （scenario 操作）、`visual_inspect` / `image_inspect`（阅读期按需）—— 都不是编译，
#: 也不该记到某次上传头上。
WIKI_KB_STAGE_SCENES = (
    "jonex_wiki_compile_organization_identity",
    "jonex_wiki_compile_repair",
    "jonex_wiki_compile_review",
    "jonex_wiki_outline",
    "jonex_wiki_source_understanding",
)

#: 库级桶只对这类知识库生效（`knowledge_info.kb_type`）。
#: `lightrag` 没有整库编译动作；`openkb` 已停用、由 `jonex_wiki` 替代。
WIKI_KB_TYPE = "jonex_wiki"


@dataclass(frozen=True)
class TaskTokenUsageRow:
    """一个任务的 Token 用量（文档级归集 + 阶段拆分）。

    `total_tokens is None` = 没有精确计量行（没调模型 / 未落库 / 映射不出来），
    **不是 0**；`0` 是「有计量行、合计确实是 0」。两个分段字段与它同生共死：
    有合计才谈得上拆分，合计为 `None` 时两者也是 `None`。
    """

    task_id: str
    document_id: Optional[str]
    document_name: Optional[str]
    total_tokens: Optional[int]
    #: 解析阶段（`total_tokens` 减去本体、wiki 文档级与整库三段后的余量）
    parse_tokens: Optional[int] = None
    #: 本体阶段（`ONTOLOGY_STAGE_SCENES` 命中的计量行合计）
    ontology_tokens: Optional[int] = None
    #: Wiki 编译阶段中**归到本文档**的部分（`WIKI_STAGE_SCENES` 命中的计量行合计）
    wiki_tokens: Optional[int] = None
    #: Wiki **整库编译**段中**平摊给本文档**的一份（`WIKI_KB_STAGE_SCENES` 桶 ÷ 批内文档数）
    kb_compile_tokens: Optional[int] = None
    #: 这一整库编译批次覆盖的文档数（前端用来渲染「本批 N 份共享」）。
    #: 没有整库桶（非 wiki 库 / 编译未完成）时为 `None`
    batch_size: Optional[int] = None


class TaskTokenUsageRepository:
    """任务 Token 用量查询 —— 不继承 `BaseRepository`（跨 schema 只读不适配通用基类）。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def summarize_by_task(
        self,
        tenant_id: str,
        task_ids: Sequence[str],
        *,
        allowed_space_ids: Optional[Sequence[str]] = None,
    ) -> List[TaskTokenUsageRow]:
        """按 `task_id` 批量汇总 Token（单条 SQL，不做 N+1）。

        `allowed_space_ids` 给 `None` = 不按空间收敛（内部链路 / 租户管理员）；
        给列表 = 只返回落在这些空间里的任务，其余静默不出现（不做存在性探针）。
        传空列表时 SQL 恒假、返回空。
        """
        if not task_ids:
            return []

        # 空间条件按需拼 —— `None` 时**绝不能**传一个 NULL 数组进去（`= ANY(NULL)`
        # 会让整条查询恒假，管理员反而什么都看不到）。
        space_clause = ""
        params: dict = {
            "tenant_id": tenant_id,
            "task_ids": list(task_ids),
            "ontology_scenes": list(ONTOLOGY_STAGE_SCENES),
            "wiki_scenes": list(WIKI_STAGE_SCENES),
            "wiki_kb_scenes": list(WIKI_KB_STAGE_SCENES),
            "wiki_kb_type": WIKI_KB_TYPE,
        }
        if allowed_space_ids is not None:
            space_clause = "AND t.space_id = ANY(:allowed_space_ids)"
            params["allowed_space_ids"] = list(allowed_space_ids)

        sql = sa_text(
            f"""
            WITH visible_tasks AS (
                SELECT t.id AS task_id, t.tenant_id
                FROM task_center.tasks t
                WHERE t.tenant_id = :tenant_id
                  AND t.is_deleted = 0
                  AND t.id = ANY(:task_ids)
                  {space_clause}
            ),
            doc_map AS (
                SELECT d.task_id, d.document_id, d.knowledge_base_id,
                       kd.file_name AS document_name,
                       -- 软删文档不再挂到整库批次上（2026-11-17）：
                       -- 它的任务仍按 `doc_id` 展示文档级计量，但不能分整库桶。
                       -- 否则「分母只数未软删、分子仍给它一份」会让可见合计超过真实桶。
                       CASE WHEN kd.is_deleted = 0 THEN kd.llm_wiki_task_id END
                           AS llm_wiki_task_id
                FROM knowledge_base.task_center_documents d
                JOIN visible_tasks vt ON vt.task_id = d.task_id
                LEFT JOIN knowledge_base.knowledge_documents kd
                       ON kd.id = d.document_id
                      AND kd.tenant_id = d.tenant_id
                WHERE d.tenant_id = :tenant_id
            ),
            -- 本次请求涉及哪些整库编译批次
            batch_scope AS (
                SELECT DISTINCT dm.llm_wiki_task_id
                FROM doc_map dm
                WHERE dm.llm_wiki_task_id IS NOT NULL
            ),
            -- 批次全貌：这些引擎编译任务覆盖的**全部**文档。
            --
            -- ⛔ 分母**绝不能**从 `doc_map` 算：那只是「本次请求的任务」，查询单个任务
            -- 时分母会变成 1，整库桶会整份记到这一份文档头上（2026-11-12 真机踩过：
            -- 单查时 batch_size=1、18,155 全给一份；按批查才是 batch_size=4）。
            -- 所以这里直接按 `llm_wiki_task_id` 回查 `knowledge_documents`，不受请求
            -- 范围限制。
            --
            -- `kb_id` 取 `MAX` 是刻意的：同批必属同一库，逐行取会因多文档 join 把
            -- 库级桶放大 N 倍。窗口用平台记录的两个时刻，实测一次编译约 31 秒。
            wiki_batch AS (
                SELECT kd.llm_wiki_task_id,
                       MAX(kd.knowledge_base_id) AS kb_id,
                       -- ⚠️ 这两列是 `datetime.utcnow()` 写进去的 **naive UTC**
                       -- （见 `knowledge_base/models/document.py` 的列注释），
                       -- 而 `metering.llm_usage_log.created_at` 是 timestamptz。
                       -- 不显式声明 `AT TIME ZONE 'UTC'`，naive 值会按会话时区
                       -- （容器里是 Asia/Shanghai）解释，整整偏 8 小时、窗口恒空 ——
                       -- 这个坑 2026-11-12 真机踩过。
                       MIN(kd.llm_wiki_compile_requested_at) AT TIME ZONE 'UTC' AS win_start,
                       MAX(kd.llm_wiki_compiled_at) AT TIME ZONE 'UTC' AS win_end,
                       COUNT(DISTINCT kd.id) AS batch_size
                FROM knowledge_base.knowledge_documents kd
                JOIN batch_scope bs ON bs.llm_wiki_task_id = kd.llm_wiki_task_id
                JOIN knowledge_base.knowledge_info ki
                  ON ki.id = kd.knowledge_base_id
                 AND ki.tenant_id = kd.tenant_id
                WHERE kd.tenant_id = :tenant_id
                  AND kd.is_deleted = 0
                  AND kd.llm_wiki_compile_requested_at IS NOT NULL
                  -- 编译失败时 `compiled_at` 为空 ⇒ 该批没有可信窗口，整库段记 NULL
                  AND kd.llm_wiki_compiled_at IS NOT NULL
                  AND ki.kb_type = :wiki_kb_type
                GROUP BY kd.llm_wiki_task_id
            ),
            -- 库级桶：窗口内、无 doc_id 的编译期场景（`WIKI_KB_STAGE_SCENES`）。
            -- 查询期场景即便落在窗口内也不在白名单里，天然排除。
            kb_level AS (
                SELECT wb.llm_wiki_task_id,
                       SUM(u.total_tokens) AS kb_tokens,
                       wb.batch_size
                FROM wiki_batch wb
                JOIN metering.llm_usage_log u
                       ON u.tenant_id = :tenant_id
                      AND u.kb_id = wb.kb_id
                      AND u.doc_id IS NULL
                      AND u.is_estimated = false
                      AND u.scene = ANY(:wiki_kb_scenes)
                      AND u.created_at >= wb.win_start
                      AND u.created_at <= wb.win_end
                GROUP BY wb.llm_wiki_task_id, wb.batch_size
            ),
            doc_with_batch AS (
                SELECT dm.*,
                       kb.batch_size AS wiki_batch_size,
                       kb.kb_tokens AS wiki_kb_tokens
                FROM doc_map dm
                LEFT JOIN kb_level kb ON kb.llm_wiki_task_id = dm.llm_wiki_task_id
            )
            SELECT vt.task_id,
                   dm.document_id,
                   dm.document_name,
                   SUM(u.total_tokens) AS total_tokens,
                   -- 本体阶段：抽取 + 本体向量化
                   SUM(CASE WHEN u.scene = ANY(:ontology_scenes)
                            THEN u.total_tokens ELSE 0 END) AS ontology_tokens,
                   -- Wiki 编译阶段中归到本文档的部分（源文档理解带 doc_id 时）
                   SUM(CASE WHEN u.scene = ANY(:wiki_scenes)
                            THEN u.total_tokens ELSE 0 END) AS wiki_tokens,
                   dm.wiki_batch_size,
                   dm.wiki_kb_tokens
            FROM visible_tasks vt
            LEFT JOIN doc_with_batch dm ON dm.task_id = vt.task_id
            LEFT JOIN metering.llm_usage_log u
                   ON u.tenant_id = vt.tenant_id
                  AND u.doc_id = dm.document_id
                  AND u.is_estimated = false
            GROUP BY vt.task_id, dm.document_id, dm.document_name,
                     dm.wiki_batch_size, dm.wiki_kb_tokens
            ORDER BY vt.task_id
            """
        )
        result = await self.session.execute(sql, params)
        rows = result.fetchall()

        out: List[TaskTokenUsageRow] = []
        for row in rows:
            doc_total = row.total_tokens
            batch_size = int(row.wiki_batch_size) if row.wiki_batch_size is not None else None
            # 整库桶平摊：`kb_level` 只在「该批有窗口且窗口内有白名单计量行」时出现，
            # 所以 batch_size 非空即代表桶存在；除零由 `and batch_size` 拦住。
            kb_compile_tokens = None
            if row.wiki_kb_tokens is not None and batch_size:
                # 四舍五入（Python 内置 round 是 banker's rounding，0.5 会去偶数，
                # 分账场景要「半数进位」，故手算）。取整残差至多 ±1，不做余额补差。
                kb_compile_tokens = (int(row.wiki_kb_tokens) + batch_size // 2) // batch_size

            # 文档级与整库段**都**没有计量时才全给 None —— 只要有一段有值就走数字，
            # 否则前端会把「没有计量」画成「本体只花了 0」。
            if doc_total is None and kb_compile_tokens is None:
                total = parse_tokens = ontology_tokens = wiki_tokens = None
                batch_size = None
            else:
                total = int(doc_total or 0) + int(kb_compile_tokens or 0)
                ontology_tokens = int(row.ontology_tokens or 0)
                wiki_tokens = int(row.wiki_tokens or 0)
                parse_tokens = total - ontology_tokens - wiki_tokens - int(kb_compile_tokens or 0)
            out.append(
                TaskTokenUsageRow(
                    task_id=row.task_id,
                    document_id=row.document_id,
                    document_name=row.document_name,
                    # ⛔ 不 COALESCE 成 0：None 是「没有精确计量」，前端渲染「-」。
                    total_tokens=total,
                    parse_tokens=parse_tokens,
                    ontology_tokens=ontology_tokens,
                    wiki_tokens=wiki_tokens,
                    kb_compile_tokens=kb_compile_tokens,
                    batch_size=batch_size,
                )
            )
        return out
