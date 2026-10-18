# -*- coding: utf-8 -*-
"""[jonex] 本体抽取主链 + Row-as-Object 表格对象 + custom-kg 回写。

自 raganything/service/task_manager.py L3892-4782 搬迁（P1）。行为逐字不变，
触点改写见 docs/superpowers/plans/2026-10-15-ontology-compile-p1-code-move.md §1.3。
平台侧函数级 import（native_ontology_extractor / ontology_extractor /
table_object_extractor / compiled_schema_client）**保持函数级**，既有测试依赖这些 patch 点。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from .deps import CompileDeps

logger = logging.getLogger(__name__)


async def run_ontology_extraction(deps: CompileDeps, task, ctx, doc_id: str) -> None:
    """[jonex] P4：独立本体抽取——chunk 读回 + NativeOntologyExtractor。

    读取 LightRAG chunk 存储原文（§5.6 权威 chunk_id，禁止按 content
    重算）→ NativeOntologyExtractor 双层类型抽取（TBox 档 + 兜底粗类型
    档，§5.8）→ 两档全集 custom-kg 回写 LightRAG 图（§5.7）→ TBox 档
    写 Redis task 供 reconcile 落 PG/Neo4j（下游零改动）。

    旧链路（读 LightRAG 候选实体/关系 → 归类/定型）已被本函数替换（P5 清理）。
    """
    task.ontology_status = "extracting"
    # Push a timeline entry for the ontology stage
    task.timeline.append(deps.stage_timing(
        stage="ontology_extract",
        label="本体抽取",
        detail="chunk 原文独立抽取 + 双层类型归类",
        started_at=datetime.now(timezone.utc),
    ))
    await deps.save_task(task)

    try:
        kb_id = task.kb_id or ""

        # [jonex] P0-b：表格子集落盘已上移到「推完切片」处，见
        # _execute_pipeline_http。原因：解析与编译拆成两次任务后，首次任务
        # 可能不抽本体，落盘点必须早于本体阶段才不会被整段跳过
        # （否则 ontology_only 回放不到存档，含表格文档会被 P0-b 闸门判 FAILED）。

        # ① 读回 LightRAG chunk 存储原文（§5.6）。时序前提：常规链路
        # PushChunksStage 等 track 全部 processed 后才进本体阶段，
        # ontology_only 复用已入库 chunk——两种模式读回口径一致。
        try:
            chunks_resp = await deps.http_client.get_document_chunks(
                task.document_id or "",
                tenant_id=task.tenant_id, kb_id=kb_id,
            )
        except Exception as e:
            logger.warning(
                f"Ontology: chunk readback failed for {task.task_id}: {e}"
            )
            task.ontology_status = "failed"
            task.ontology_error = f"chunk 读回失败: {e}"[:500]
            return

        chunks = (
            chunks_resp.get("chunks") or []
            if isinstance(chunks_resp, dict)
            else []
        )
        if not chunks:
            task.ontology_status = "failed"
            task.ontology_error = (
                "LightRAG chunk 存储中未找到该文档 chunk，无法抽取本体"
            )
            return

        # [jonex] P4 §8 跨文档判重缺口暴露：内容与先入文档完全相同的
        # chunk 判重后不重复入库（file_path 带先入文档锚点），按 doc_id
        # 读回时当前文档缺这些段。PushChunksStage 只透传判重计数、未透传
        # duplicated_indices，此处至少记 warning 暴露缺口（§8 风险表
        # 最后一行），不静默。
        dup_count = int(getattr(task, "duplicated_chunk_count", 0) or 0)
        if dup_count > 0:
            logger.warning(
                "Ontology: doc=%s 有 %d 个 chunk 因跨文档判重未入库，"
                "按 doc_id 读回缺这些段（读回 %d 个）——抽取输入可能缺段",
                task.document_id, dup_count, len(chunks),
            )

        # ── [jonex] P0-b 闸门：无表格上下文存档 + 本文档确有表格 chunk
        # → **拒绝抽取**，不降级。──
        #
        # 为什么不降级跑：降级把表格当普通文本抽，产出通用实体（无结构化
        # attributes、无复合主键区分），而写图是 delete_by_document 全量替换
        # ——不是补齐缺失，是用差的换掉首轮的好结果；代价还是全部 chunk 进
        # LLM（本例 24212 chunk / 3027 批 ≈45 分钟）+ backfill 分钟级 CPU。
        # 拒绝则首轮结果一行不动：KB 侧见 ontology_status=failed 走
        # reconciliation 的「已完成但无 ontology_data」分支，最多重试
        # MAX_ONTOLOGY_RETRIES 次（秒级空转）后落 FAILED、把 ontology_error
        # 透到文档 error，**全程不调 _handle_completed、不碰 Neo4j**。
        #
        # 判据三重收敛，缺一不可：
        #   ctx.ontology_only——只有重抽才存在「全量替换掉首轮好结果」的
        #     问题；full 首轮入库 content_list 为 None 也无所谓（首次写图，
        #     没有可替换的东西），故不能误伤；
        #   ``is None`` 而非 falsy——空列表是「首轮确认无表格」的有效存档；
        #   chunk 侧检测——避免误伤纯文本文档（它没有表格 chunk，重抽本就
        #     不需要表格上下文）。
        from jonex_core.capability.atomic.rag.native_ontology_extractor import (
            NativeOntologyExtractor,
        )

        if ctx.ontology_only and ctx.content_list is None:
            table_chunk_count = sum(
                1 for c in chunks if NativeOntologyExtractor._is_table_chunk(c)
            )
            if table_chunk_count:
                task.ontology_status = "failed"
                task.ontology_error = (
                    f"表格上下文存档缺失，本文档含 {table_chunk_count} 个表格 "
                    "chunk：重抽会以文本口径全量替换首轮由 Row-as-Object 建立"
                    "的表格对象，已拒绝。需 force_reparse 重建存档后重抽"
                )[:500]
                logger.error(
                    "[jonex][P0-b] 拒绝 ontology_only 重抽（无表格上下文存档）: "
                    "doc=%s 表格 chunk=%d ——首轮表格对象保留，需 force_reparse",
                    task.document_id, table_chunk_count,
                )
                return

        # ② Resolve compiled schema（native 抽取的 TBox 用 prompt_schema）
        compiled_schema = task.ontology_schema
        if not compiled_schema:
            try:
                from jonex_core.capability.atomic.ontology.compiled_schema_client import (
                    CompiledSchemaClient,
                )
                compiled_schema = await CompiledSchemaClient().get_schema(
                    task.tenant_id, kb_id,
                )
            except Exception as e:
                logger.warning(
                    f"Ontology: compiled schema fetch failed for {task.task_id}: {e}"
                )
        tbox = (compiled_schema or {}).get("prompt_schema")

        # ③ NativeOntologyExtractor 独立抽取（§5.3 七步流程）。
        # tbox 不可解析 → extract 返回 ok=False 整体不抽取（§5.8 P1-4，
        # 不得「空集全丢弃」静默清空本体）。该 import 已上移到 P0-b 闸门处。
        scope = {
            "tenant_id": task.tenant_id,
            "knowledge_base_id": kb_id,
            "document_id": task.document_id or "",
            "trace_id": task.task_id,
        }

        # [jonex] P7 M2：procedural 每文档触发条件 = schema 含 Action
        # （总闸 ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED 在 extractor 侧按 env
        # AND 判定，§5 P2-12 gate 关系）。未声明 Action 的 KB 传 null 零开销。
        procedural_opt = None
        if tbox and any(
            (et.get("name") == "Action") for et in (tbox.get("entity_types") or [])
        ):
            procedural_opt = {}

        # [jonex] P5-var（方案 B）：仅当 Row-as-Object 已接管表格
        # （content_list 含 type=="table" item）时剔除表格 chunk；
        # 无接管（content_list 丢失）时表格 chunk 进 native 文本抽取
        # ——修本体召回，不重复映射。
        # [jonex] §table-llm-bypass：md 文档的表格不产出 type=="table"
        # item（parse_text 只发 text item），但 push 阶段已确定性识别并
        # 登记到 content_list item 的 _mapped_tables；两处任一命中即接管。
        # [jonex] P0-b：ontology_only 的 content_list 由首轮存档回放
        # （_load_table_context），故重抽同样落到「已接管」分支、口径与
        # 首轮一致。存档缺失（content_list is None）时不会走到这里——上面
        # 的 P0-b 闸门已按 chunk 判据裁决（有表格 chunk 即拒绝 return）；
        # 能到这里的 None 只可能是「无表格 chunk 的纯文本文档」，走「无
        # 接管」这一支本就正确。
        table_handled = any(
            (item or {}).get("type") == "table"
            or (item or {}).get("_mapped_tables")
            for item in (ctx.content_list or [])
        )
        result = await NativeOntologyExtractor().extract(
            chunks=chunks, tbox=tbox, scope=scope,
            options={
                "procedural": procedural_opt,
                "table_objects_handled": table_handled,
            },
        )
        # [jonex] P0-b（2026-09-13 OOM 复盘）：主抽取的返回值此前**完全未被
        # 检查**——ok=False（TBox 不可用、无可用 chunk 等）被静默吞掉，直接
        # 落到下面的表格映射与零产出兜底，日志上看不出「主抽取其实什么都没
        # 跑」。此处补一条带 task/doc 关联的告警（提取器内部另有一条无关联
        # 的明细告警，两条配合即可定位到具体任务）。
        if not result.ok:
            logger.warning(
                "[jonex][P0-b] 主抽取未产出（ok=False）: task=%s doc=%s "
                "chunks=%d errors=%s",
                task.task_id, task.document_id, len(chunks),
                str(result.errors[:1])[:200],
            )

        # ── [jonex] §table-grid-v2 步14：Row-as-Object 表格对象抽取 ──
        # ctx.content_list 可用（常规 insert 链路，或 P0-b 重抽回放）时，
        # 对表格做确定性映射（一行 = 一个对象，列名 = 属性），产出并入
        # 本体存储档（TBox 档）。content_list 为 None（上线前入库的老文档
        # 重抽且无存档）时走不到这里——上面 P0-b 闸门已拒绝或确认无表格
        # chunk，故此处的空值只代表「本文档确实没有表格」。
        # 失败降级：仅打日志，不影响 native 抽取结果。
        table_produced = False
        if ctx.content_list:
            try:
                table_produced = await deps.hooks.merge_table_objects(
                    ctx, scope, result, compiled_schema=compiled_schema,
                    # [jonex] S1b：本函数已把整 doc 的 chunk 读进内存
                    # （上方 ①，此处仍在同一作用域内、中间无重绑定），
                    # 传给行对象做「区间包含」证据回挂，零额外 IO。
                    chunks=chunks,
                )
            except Exception as exc:  # noqa: BLE001 — 表格对象失败不阻断本体
                logger.warning(
                    "Table objects extraction failed for %s: %s",
                    task.task_id, exc,
                )

        # [jonex] P10 批 2.4：Row-as-Object 零产出兜底——table_handled=True
        # 时 native 主抽取已剔除表格 chunk；若确定性映射也零产出（无 pk、
        # 表头判定失败等），表格信息整体丢失。对被剔除的表格 chunk 补跑
        # 一次 native 抽取（table_objects_handled=False 使表格 chunk 进
        # 文本抽取），产出并入两档全集，随后 ⑤ 一并回写 LightRAG。
        # 补跑失败仅告警不阻断。
        #
        # [jonex] P0-a（2026-09-13 线上 OOM 复盘）：兜底必须设规模闸门。
        # 实证（financial_metric.md）：1438/1438 张表主键识别失败 →
        # table_produced=False；而该文档**全部** 46290 个 chunk 都是表格
        # chunk，于是「兜底集合 == 全量集合」，兜底退化成「把整篇文档原样
        # 喂回 LLM」——单次 46290 chunk / 5787 批 ≈ 4.6 小时，产出 19647
        # 实体 + 34522 关系，backfill 阶段把宿主内存吃到 23.7GB 被
        # oom-killer 杀掉（容器当时无 mem_limit，故 OOMKilled=false）。
        # 兜底的定位是「补回少量被误剔除的表格 chunk」，一旦逼近全量就不再
        # 是兜底，而是最坏情况的全量 LLM 抽取，必须显式拒绝。
        # 拒绝而非降级：写图是 delete_by_document 全量替换，放它跑完等于用
        # 「表格当普通文本」的差结果换掉首轮的好结果；拒绝则图一行不动
        # （与上方 P0-b 闸门同款收尾：ontology_status=failed + return，
        # KB 侧走 reconciliation「已完成但无 ontology_data」分支）。
        # 判据双闸，任一命中即拒绝：
        #   count——兜底集合绝对规模（本次 46290 > 2000，真正防 OOM 的那道）；
        #   ratio——兜底集合占全量 chunk 的比例（本次 1.0，最极端形态），
        #          需叠加规模下限才生效，避免误杀「整篇都是表」的小文档。
        # 判据与默认值集中在 _table_fallback_gate，单测见
        # tests/test_service/test_table_fallback_gate.py。
        if table_handled and not table_produced and chunks:
            table_chunks = [
                c for c in chunks
                if NativeOntologyExtractor._is_table_chunk(c)
            ]
            if table_chunks:
                gate_ok, gate_detail = table_fallback_gate(
                    len(table_chunks), len(chunks),
                )
                if not gate_ok:
                    task.ontology_status = "failed"
                    task.ontology_error = (
                        f"表格确定性映射零产出，且待回补的表格 chunk 规模超闸门"
                        f"（{gate_detail}）：零产出兜底会退化为全量 LLM 抽取，"
                        "已拒绝。请拆分文档，或修正表格主键识别（per-KB 配置"
                        "主键列 / 整行 hash 兜底）后重抽"
                    )[:500]
                    logger.error(
                        "[jonex][P0-a] 拒绝零产出兜底（规模超闸门）: task=%s "
                        "doc=%s 表格 chunk=%s ——表格对象保留不动，"
                        "避免全量 LLM 抽取",
                        task.task_id, task.document_id, gate_detail,
                    )
                    return
                try:
                    supplement = await NativeOntologyExtractor().extract(
                        chunks=table_chunks, tbox=tbox, scope=scope,
                        options={
                            "procedural": procedural_opt,
                            "table_objects_handled": False,
                        },
                    )
                    if supplement.ok:
                        result.entities.extend(supplement.entities)
                        result.relations.extend(supplement.relations)
                        result.ontology_entities.extend(
                            supplement.ontology_entities
                        )
                        result.ontology_relations.extend(
                            supplement.ontology_relations
                        )
                        result.ok = True
                        logger.info(
                            "Row-as-Object 零产出，表格 chunk 回补 "
                            "native 抽取 %d 实体 / %d 关系",
                            len(supplement.ontology_entities),
                            len(supplement.ontology_relations),
                        )
                    else:
                        logger.warning(
                            "表格 chunk 回补 native 抽取失败: %s",
                            supplement.errors,
                        )
                except Exception as exc:  # noqa: BLE001 — 兜底失败不阻断
                    logger.warning(
                        "表格 chunk 回补 native 抽取异常: %s", exc,
                    )

        # ⑤ custom-kg 回写：两档全集回写 LightRAG 图（§5.7）。
        # 失败重试 1 次，仍失败本体链路照常成功（本体存储已落），
        # 由 retry_ontology_extract 重抽时再次回写。
        if result.entities or result.relations:
            await deps.hooks.writeback_custom_kg(
                task, kb_id, result, compiled_schema or {},
            )

        # ⑥ Redis task 落盘：本体存储侧只落 TBox 档（ontology_* 字段，
        # 与旧链路 _post_validate 后口径一致，下游 reconcile 零改动）。
        task.ontology_status = "completed" if result.ok else "failed"
        task.ontology_data = {
            "entities": [
                serialize_entity(e) for e in result.ontology_entities
            ],
            "relations": [
                serialize_relation(r)
                for r in result.ontology_relations
            ],
        }
        task.ontology_error = (
            str(result.errors[:1]) if result.errors else ""
        )

        logger.info(
            f"Ontology extraction done for {task.task_id}: "
            f"status={task.ontology_status} "
            f"entities={len(result.ontology_entities)} "
            f"relations={len(result.ontology_relations)} "
            f"kg_entities={len(result.entities)} "
            f"kg_relations={len(result.relations)}"
        )

    except Exception as e:
        logger.warning(
            f"Ontology extraction failed for {task.task_id}: {e}",
            exc_info=True,
        )
        task.ontology_status = "failed"
        task.ontology_error = str(e)[:500]
    finally:
        # Close the ontology stage timing
        if task.timeline:
            last = task.timeline[-1]
            if last.stage == "ontology_extract" and last.ended_at is None:
                now = datetime.now(timezone.utc)
                last.ended_at = now
                if last.started_at:
                    last.elapsed_seconds = (now - last.started_at).total_seconds()
        await deps.save_task(task)


def serialize_entity(e) -> dict:
    """[jonex] P4：本体实体 → Redis task dict（与旧链路字段逐一对齐）。"""
    return {
        "canonical_name": e.canonical_name,
        "entity_type": e.entity_type,
        "aliases": e.aliases,
        "attributes": e.attributes,
        "description": e.description,
        "confidence": e.confidence,
        "source_chunks": e.source_chunks,
        "extraction_method": e.extraction_method,
        # [jonex] 改动 18：row_as_object 的 upsert 键随 ont_data 透传
        "table_sig": e.table_sig,
        "pk_value": e.pk_value,
    }


def serialize_relation(r) -> dict:
    """[jonex] P4：本体关系 → Redis task dict。

    缺陷修复：补齐 attributes / source_chunks（reconcile→Neo4j ONT_REL
    边出处与关系属性依赖它们，旧序列化两字段缺失）；keywords 一并透传
    （M3-e 观测）。
    """
    return {
        "source_name": r.source_name,
        "source_type": r.source_type,
        "target_name": r.target_name,
        "target_type": r.target_type,
        "relation_type": r.relation_type,
        "confidence": r.confidence,
        "source_chunks": r.source_chunks,
        "keywords": r.keywords,
        "attributes": r.attributes,
    }


async def writeback_custom_kg(deps: CompileDeps, task, kb_id: str, result, compiled_schema: dict) -> bool:
    """[jonex] P4 §5.7：两档全集回写 LightRAG 图（POST /documents/custom-kg）。

    映射契约：source_id = §5.6 权威 chunk_id（多 chunk 用 ``<SEP>`` 拼接，
    与 LightRAG 自抽路径同构，禁止按 content 重算）；keywords = relation_type
    （TBox 内）或自由关键词（TBox 外）；description = compiled schema 该关系
    类型中文描述，TBox 外关系取 keywords 首项。失败重试 1 次；仍失败返回
    False（本体链路不受影响，retry_ontology_extract 重抽时再次回写）。
    """
    rel_desc = {
        str(rt.get("name") or ""): str(rt.get("description") or "")
        for rt in (compiled_schema.get("relation_types") or [])
        if rt.get("name")
    }

    def _source_of(source_chunks) -> tuple[str, str]:
        ids: list[str] = []
        fp = ""
        for sc in source_chunks or []:
            sid = str(sc.get("source_id") or "").strip()
            if sid:
                ids.append(sid)
                if not fp:
                    fp = str(sc.get("file_path") or "")
        # 保持插入序去重
        return "<SEP>".join(dict.fromkeys(ids)), fp

    entities = []
    for e in result.entities:
        sid, fp = _source_of(e.source_chunks)
        entities.append({
            "entity_name": e.canonical_name,
            "entity_type": e.entity_type,
            "description": e.description,
            "source_id": sid,
            "file_path": fp,
        })

    relationships = []
    for r in result.relations:
        sid, fp = _source_of(r.source_chunks)
        keywords = (r.relation_type or "").strip() or (r.keywords or "").strip()
        description = rel_desc.get(r.relation_type, "")
        if not description and r.keywords:
            description = str(r.keywords).split(",")[0].strip()
        relationships.append({
            "src_id": r.source_name,
            "tgt_id": r.target_name,
            "description": description,
            "keywords": keywords,
            "weight": r.confidence,
            "source_id": sid,
            "file_path": fp,
        })

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            await deps.http_client.insert_custom_kg(
                entities, relationships,
                tenant_id=task.tenant_id, kb_id=kb_id,
            )
            logger.info(
                "custom-kg 回写成功 %s: entities=%d relationships=%d",
                task.task_id, len(entities), len(relationships),
            )
            return True
        except Exception as exc:  # noqa: BLE001 — 回写失败不阻断本体
            last_exc = exc
            logger.warning(
                "custom-kg 回写失败 %s (attempt %d/2): %s",
                task.task_id, attempt + 1, exc,
            )
            if attempt == 0:
                await asyncio.sleep(2)
    logger.warning(
        "custom-kg 回写最终失败 %s（本体链路不受影响，重抽时再回写）: %s",
        task.task_id, last_exc,
    )
    return False


def row_evidence_enabled() -> bool:
    """[jonex] S1b 开关：行对象 chunk 证据回挂（默认关，灰度）。

    关 = 改造前行为（行对象在图上贡献 0 引用）。
    """
    return os.getenv(
        "ONTOLOGY_ROW_EVIDENCE_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes", "on")


def row_evidence_cap() -> int:
    """行对象 ``source_chunks`` 总量上限（0 = 不限）。

    与 native 抽取的 ``ONTOLOGY_SOURCE_CHUNKS_MAX`` 同口径、同默认值：
    同一起因（2026-09-13 无上限的「命中即 append」把宿主内存吃到
    23.7GB 被 oom-killer 杀）——行对象同样是「一行命中多个 chunk」的
    放大点，必须受同一个配额约束。
    """
    try:
        return max(0, int(os.getenv("ONTOLOGY_SOURCE_CHUNKS_MAX", "32")))
    except (TypeError, ValueError):
        return 32


def table_fallback_gate(table_count: int, total_count: int) -> tuple[bool, str]:
    """[jonex] P0-a：零产出兜底的规模闸门。

    兜底的定位是「补回**少量**被误剔除的表格 chunk」。当待回补集合逼近全量
    （线上实证 46290/46290）时，兜底已等价于「整篇文档全量进 LLM」，必须
    拒绝——判据与实证详见调用处注释。

    双闸，任一命中即拒绝：
      - 绝对闸 ``ONTOLOGY_TABLE_FALLBACK_MAX_CHUNKS``（默认 2000）——
        兜底规模的硬上限，独立生效，是真正防 OOM 的那道；
      - 比例闸 ``ONTOLOGY_TABLE_FALLBACK_MAX_RATIO``（默认 0.5）——
        兜底占文档主导即失去兜底语义。**必须叠加规模下限**
        ``ONTOLOGY_TABLE_FALLBACK_RATIO_MIN_CHUNKS``（默认 200）才生效：
        小文档「整篇都是表」是常态（既有单测里就有 1 chunk 的表格文档），
        1 个 chunk 占 100% 也花不掉几个 LLM —— 只有规模大到值得警惕时，
        占比才有判据意义。无下限时比例闸会把这类正常小文档一并误杀。

    Returns:
        (allowed, detail)：allowed=False 时 detail 为超限明细，直接用于日志
        与 ``ontology_error``；True 时为当前占比，供日志记录。
    """
    ratio = table_count / max(1, total_count)
    try:
        max_ratio = float(
            os.getenv("ONTOLOGY_TABLE_FALLBACK_MAX_RATIO", "0.5")
        )
    except ValueError:
        max_ratio = 0.5
    try:
        max_chunks = int(
            os.getenv("ONTOLOGY_TABLE_FALLBACK_MAX_CHUNKS", "2000")
        )
    except ValueError:
        max_chunks = 2000
    try:
        ratio_min_chunks = int(
            os.getenv("ONTOLOGY_TABLE_FALLBACK_RATIO_MIN_CHUNKS", "200")
        )
    except ValueError:
        ratio_min_chunks = 200
    over_count = table_count > max_chunks
    over_ratio = ratio > max_ratio and table_count > ratio_min_chunks
    detail = (
        f"{table_count}/{total_count} = {ratio:.0%}"
        f"（上限 {max_ratio:.0%} / {max_chunks} 个，"
        f"比例闸下限 {ratio_min_chunks} 个）"
    )
    if over_count or over_ratio:
        return False, detail
    return True, detail


async def merge_table_objects(deps: CompileDeps, ctx, scope: dict, result,
                         compiled_schema=None, chunks=None) -> bool:
    """[jonex] §table-grid-v2 步14：Row-as-Object 表格对象抽取。

    对 content_list 的每个 table item：重跑网格化（与 push 阶段同函数
    保证一致性）→ 表级判定（LLM/缓存/规则，含表头判定模型化）→ 列名
    →TBox 属性映射（§10.2.4 第二次 LLM）→ 行级对象实例 + 分组关系，
    并入本体存储档 ontology_entities/ontology_relations
    （extraction_method=row_as_object，attributes 结构化，不走 200
    截断）。表格对象不回写 LightRAG（方案 C 通道已入库）。

    Args:
        chunks: 本 doc 的 LightRAG chunk 列表（与上方 ① **同一批**物理
            对象）。S1b 用它给行对象回挂 chunk 引用（§6 S1b / §12.5）；
            缺省 None 表示不开启该步骤（回挂全部计入 miss）。

    Returns:
        bool: 是否有表格对象产出（P10 批 2.4 零产出兜底判定依据）。
    """
    import hashlib as _hashlib

    from jonex_core.capability.atomic.rag.ontology_extractor import (
        ExtractedEntity,
        ExtractedRelation,
    )
    from jonex_core.capability.atomic.rag.table_object_extractor import (
        TableObjectExtractor,
        _compose_header_parts,
        apply_header_verdict,
        attach_row_chunk_evidence,
        index_row_chunks,
    )

    max_rows = int(os.getenv("TABLE_OBJECT_MAX_ROWS", "2000"))
    extractor = TableObjectExtractor()
    file_name = ctx.file_name or ""
    # §10.2.4 可选标准属性词表：compiled schema 各实体类型的属性名并集
    tbox_attributes: list | None = None
    if compiled_schema:
        try:
            tbox_attributes = sorted({
                str(a.get("name", ""))
                for et in (compiled_schema.get("entity_types") or [])
                for a in (et.get("attributes") or [])
                if a.get("name")
            }) or None
        except Exception as exc:  # noqa: BLE001 — 词表是提示项，失败不阻塞
            logger.warning("Row-as-Object: tbox_attributes 提取失败: %s", exc)

    table_entities: list = []
    table_relations: list = []
    # [jonex] P0-c 观测指标：主键质量。与 stages.py 的 tables_fallback
    # 并列写回 ctx.table_stats（`_execute_pipeline_http` 在抽取阶段之后
    # 会二次合并，见该方法内注释）。
    #   tables_pk_empty     —— 重判后仍无主键、走整行 hash 命名的表数
    #   tables_pk_from_llm  —— 主键由 LLM 重判得到的表数
    #   tables_pk_from_rules—— 主键由规则兜底得到的表数
    #   rows_row_hash       —— 整行 hash 命名的对象数
    tables_seen = 0
    objects_total = 0
    relations_total = 0
    pk_failed = 0
    pk_from_llm = 0
    pk_from_rules = 0
    rows_row_hash = 0
    # [jonex] 分组聚合节点实体化去重集：同一分组（列名·值）在多个表片段
    # 里会重复出现，只在首次登记实体，关系仍逐条保留。
    group_nodes: set = set()
    # [jonex] S1b（§6 S1b / §1.1 例外 / §12.5）：行对象 chunk 证据回挂。
    # 索引按 table_sig 归集本 doc 的 ctype=table_row chunk（含行区间），
    # 建一次、全部表片段复用。索引为空 = 降级场景（chunks 缺失，或
    # RAG_TABLE_GRID_V2=false 使 chunk 侧不写 table_sig → join 必然全
    # miss），按 §12.5 要求**打点降级**：记 warning + 计数，行对象退回
    # 无 chunk 引用，不报错、不阻断抽取。
    row_chunk_index: dict = {}
    if row_evidence_enabled():
        row_chunk_index = index_row_chunks(chunks or [])
        if not row_chunk_index:
            logger.warning(
                "Row-as-Object S1b: 行级证据 join 无可用索引"
                "（chunks=%d；chunk 侧 table_sig 仅在 RAG_TABLE_GRID_V2"
                "=true 时写入）——行对象本轮退回无 chunk 引用，抽取继续",
                len(chunks or []),
            )
    row_ev_matched = 0
    row_ev_added = 0
    row_ev_missed = 0
    # [jonex] §table-llm-bypass：除 MinerU/表格类 content_list 的
    # type=="table" item 外，一并消费 push 阶段登记的 _mapped_tables
    # （md/内嵌表格路径；只登记归一化成功的表，失败块已退回文本链路）。
    table_items: list = []
    for item in ctx.content_list or []:
        if (item or {}).get("type") == "table":
            table_items.append(item)
        table_items.extend((item or {}).get("_mapped_tables") or [])
    for item in table_items:
        raw_body = deps.table_grid.get_table_body(item)
        if deps.table_grid.enabled():
            header, data_rows, meta = deps.table_grid.normalize_table_grid(raw_body)
        else:
            header, data_rows = deps.table_grid.normalize_table_rows(raw_body)
        if not data_rows or not header:
            continue

        caption = item.get("table_caption")
        if isinstance(caption, list):
            caption = "、".join(
                str(c) for c in caption if str(c).strip()
            )
        caption = (caption or "").strip() or file_name

        verdict = await extractor.analyze_table(
            caption, header, data_rows, scope=scope,
            tbox_attributes=tbox_attributes,
        )
        # §10.2.3 层级关系：多级表头父级链（属性组），无额外表头时为空
        header_parts: list | None = None
        # [jonex] §table-grid-v2 步14 补缺：消费表级判定的表头/说明行
        # 结论。此前 header_row_indices / note_row_indices 只校验不消费
        # （对象抽取仍用规则表头）——模型认为 data_rows 中还有表头行
        # （多级表头漏识别）或说明行（长文本漏吸净）时，重建列名与
        # 对象行，行锚点经 row_map 保持与原 data_rows 对齐。
        if verdict.source != "rules":
            header2, rows2, row_map = apply_header_verdict(
                header, data_rows, verdict,
            )
            if header2 != header:
                # 属性组父级链（与 apply_header_verdict 同拼接口径，
                # 同样过滤越界行号）
                header_parts = _compose_header_parts(
                    [header]
                    + [
                        data_rows[i]
                        for i in verdict.header_row_indices
                        if 0 <= i < len(data_rows)
                    ],
                    len(header2),
                )
                # 主键/限定列按列位置重映射（多级拼接不改变列序）
                pos = {name: i for i, name in enumerate(header)}
                verdict.pk_columns = [
                    header2[pos[c]] for c in verdict.pk_columns if c in pos
                ]
                verdict.qualifier_columns = [
                    header2[pos[c]]
                    for c in verdict.qualifier_columns
                    if c in pos
                ]
                logger.info(
                    "Row-as-Object: 表「%s」应用表头判定：列名 %d 列重拼 "
                    "（+%d 表头行）、剔除 %d 说明行",
                    caption[:40], len(header2),
                    len(verdict.header_row_indices),
                    len(verdict.note_row_indices),
                )
            header, data_rows = header2, rows2
        else:
            row_map = list(range(len(data_rows)))

        # [jonex] P0-c（2026-09-13 线上 OOM 复盘）：主键为空时用**纠正后的
        # 表头**重判一次（LLM 主判 + 规则兜底），不再直接整表放弃。
        # 原实现中 `analyze_table` 只校验 header_row_indices，LLM 给出合法
        # 表头判定却给不出主键（pk_columns=[]）时照样按 source="llm" 放行
        # → 规则兜底被跳过 → 整表零对象 → 零产出兜底全量 LLM。
        # 重判后仍为空则退化为整行 hash 兜底命名（build_objects 内建），
        # 对象照落——「永不因主键缺失退回 LLM 全量抽取」。
        pk_source = ""
        if not verdict.pk_columns:
            pk_cols, pk_source = await extractor.resolve_pk_columns(
                caption, header, data_rows, scope=scope,
            )
            if pk_cols:
                verdict.pk_columns = pk_cols
                logger.info(
                    "Row-as-Object: 表「%s」主键重判成功 → %s（source=%s）",
                    caption[:40], "、".join(pk_cols), pk_source,
                )
            else:
                pk_failed += 1
                logger.warning(
                    "Row-as-Object: 表「%s」主键重判失败（LLM 与规则均无"
                    "可用主键），退化整行 hash 兜底命名，对象照常产出",
                    caption[:40],
                )

        table_sig = _hashlib.md5(
            "|".join(header).encode()
        ).hexdigest()[:8]
        objects = extractor.build_objects(
            caption, header, data_rows[:max_rows], verdict,
            table_sig=table_sig,
            table_idx=item.get("table_idx"),
            row_indices=row_map[:max_rows],
            header_parts=header_parts,
        )
        # [jonex] S1b：按「区间包含」把命中 chunk 追加进行对象
        # source_chunks（不是三元组相等——行对象单行、chunk 多行段）。
        if row_chunk_index:
            _m, _a, _mi = attach_row_chunk_evidence(
                objects, row_chunk_index, max_chunks=row_evidence_cap(),
            )
            row_ev_matched += _m
            row_ev_added += _a
            row_ev_missed += _mi
        relations = extractor.build_group_relations(
            header, data_rows[:max_rows], objects, verdict,
        )
        # [jonex] row-to-table：每个行对象再连一条到所属表的 `包含` 边。
        # 与分组关系**并存**——没有分组列的表（实测大量参数表/方案表/字段
        # 说明表，`detect_group_column` 正确地返回 None）靠这条不再孤立。
        # 实测：本体作答时命中实体的 63% 度≤1，其中孤立 39 个里 79% 是行对象。
        # 表节点名只用 table_sig（不带 caption）——确定性优先；实机实测同一张表
        # 会出现带/不带 caption 两种条目，按 caption 命名必然分裂。
        relations += extractor.build_table_relations(
            objects, table_sig,
        )
        tables_seen += 1
        objects_total += len(objects)
        relations_total += len(relations)
        if pk_source == "llm":
            pk_from_llm += 1
        elif pk_source == "rules":
            pk_from_rules += 1
        rows_row_hash += sum(
            1 for o in objects if o.name_source == "row_hash"
        )
        for obj in objects:
            table_entities.append(ExtractedEntity(
                canonical_name=obj.canonical_name,
                entity_type=obj.entity_type,
                aliases=obj.aliases,
                attributes=obj.attributes,
                description="",
                confidence=1.0,
                source_chunks=obj.source_chunks,
                extraction_method="row_as_object",
                table_sig=obj.table_sig,   # [jonex] 改动 18 upsert 键
                pk_value=obj.pk_value,     # [jonex] 改动 18 upsert 键
            ))
        for rel in relations:
            # [jonex] §group-node 分组聚合节点实体化：此前只产出边，源端点
            # 无实体 → 写图时 merge_relation 兜底建 stub（stub=True）→
            # count_relations 要求两端非 stub，分组关系永远统计为 0。
            key = (rel["source_name"], rel["source_type"])
            if key not in group_nodes:
                group_nodes.add(key)
                table_entities.append(ExtractedEntity(
                    canonical_name=rel["source_name"],
                    entity_type=rel["source_type"],
                    description="",
                    confidence=1.0,
                    # 不设 table_sig/pk_value：分组节点不是行对象，
                    # 按名走普通 upsert 键（同名跨表片段自然合并）
                    extraction_method="row_as_object",
                ))
            table_relations.append(ExtractedRelation(
                source_name=rel["source_name"],
                source_type=rel["source_type"],
                target_name=rel["target_name"],
                target_type=rel["target_type"],
                relation_type=rel["relation_type"],
            ))
        logger.info(
            "Row-as-Object: 表「%s」→ %d 对象 + %d 分组关系 "
            "(pk=%s, verdict=%s)",
            caption[:40], len(objects), len(relations),
            "、".join(verdict.pk_columns), verdict.source,
        )

    # [jonex] P0-c：主键质量指标写回 ctx.table_stats（与 stages.py 的
    # tables_fallback / cols_unnamed 同表），并落一条汇总日志——指标是
    # 「留文档级告警」的判据：tables_pk_empty > 0 说明该文档仍有表格
    # 走整行 hash 命名（对象名不可读），需要补主键配置或拆分文档。
    if tables_seen:
        stats = getattr(ctx, "table_stats", None)
        if not isinstance(stats, dict):
            stats = {}
            try:
                ctx.table_stats = stats
            except Exception:  # noqa: BLE001 — 观测指标不阻塞入库
                pass
        stats["tables_pk_empty"] = stats.get("tables_pk_empty", 0) + pk_failed
        stats["tables_pk_from_llm"] = (
            stats.get("tables_pk_from_llm", 0) + pk_from_llm
        )
        stats["tables_pk_from_rules"] = (
            stats.get("tables_pk_from_rules", 0) + pk_from_rules
        )
        stats["rows_row_hash"] = stats.get("rows_row_hash", 0) + rows_row_hash
        # [jonex] S1b：行级证据回挂指标（§7 第 6 条验收 + §12.5 降级打点）。
        #   row_evidence_matched —— 至少回挂到 1 条 chunk 引用的行对象数；
        #   row_evidence_refs    —— 追加的 chunk 引用总条数；
        #   row_evidence_missed  —— 一条都没命中的行对象数。miss 恒等于
        #     对象总数说明 join 整体失效（table_sig 口径分叉 / grid-v2
        #     关闭），是排障的第一信号，故单列而非并入 matched。
        if row_evidence_enabled():
            stats["row_evidence_matched"] = (
                stats.get("row_evidence_matched", 0) + row_ev_matched
            )
            stats["row_evidence_refs"] = (
                stats.get("row_evidence_refs", 0) + row_ev_added
            )
            stats["row_evidence_missed"] = (
                stats.get("row_evidence_missed", 0) + row_ev_missed
            )
        logger.info(
            "Row-as-Object 汇总: 表 %d 张 → 对象 %d 个 + 分组关系 %d 条；"
            "主键来源 llm=%d / rules=%d / 仍失败=%d（整行 hash 兜底 %d 行）",
            tables_seen, objects_total, relations_total,
            pk_from_llm, pk_from_rules, pk_failed, rows_row_hash,
        )
        if row_evidence_enabled():
            logger.info(
                "Row-as-Object S1b 行级证据: 命中对象 %d / 追加引用 %d / "
                "未命中对象 %d（共 %d 个对象）",
                row_ev_matched, row_ev_added, row_ev_missed, objects_total,
            )

    if table_entities:
        # O6：同名去重——表格对象（结构化 attributes）优先于 native
        # 抽取文本实体；别名也占位（防止同名别名实体残留）。只作用于
        # 本体存储档（TBox 档）：LightRAG 侧表格实体已由方案 C 通道在
        # push 阶段入库，custom-kg 回写不含表格对象，同名合并由
        # LightRAG upsert 语义处理。
        table_names = set()
        for e in table_entities:
            table_names.add(e.canonical_name)
            table_names.update(e.aliases)
        result.ontology_entities = [
            e for e in result.ontology_entities
            if not (
                e.extraction_method != "row_as_object"
                and (
                    e.canonical_name in table_names
                    or any(a in table_names for a in e.aliases)
                )
            )
        ]
        result.ontology_entities.extend(table_entities)
        result.ontology_relations.extend(table_relations)
        # 表格对象保证 ok（与 native 零实体互不依赖）
        result.ok = True
    return bool(table_entities)
