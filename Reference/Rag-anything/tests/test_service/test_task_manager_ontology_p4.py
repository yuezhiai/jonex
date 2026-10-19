"""[jonex] P4：_run_ontology_extraction 链路切换 + custom-kg 回写映射契约。

覆盖：① chunk 读回失败语义；② 正常流程 ontology_data 只落 TBox 档
（ontology_* 字段）、custom-kg 回写两档全集；③ relation 序列化补
attributes/source_chunks/keywords（既有缺陷修复）；④ §5.7 映射契约
（source_id <SEP> 拼接、keywords/description 规则、失败重试 1 次）。
"""

import os
from unittest import mock

import pytest

from jonex_core.capability.atomic.rag.native_ontology_extractor import (
    NativeExtractionResult,
    NativeOntologyExtractor,
)
from jonex_core.capability.atomic.rag.ontology_extractor import (
    ExtractedEntity,
    ExtractedRelation,
)
from jonex_core.capability.atomic.rag.table_object_extractor import TableObject
from raganything.pipeline.base import PipelineContext
from raganything.service.models import TaskInfo
from raganything.service.task_manager import TaskManager


def _make_task(**overrides) -> TaskInfo:
    base = dict(
        task_id="task-1",
        tenant_id="tenant_jonex_demo",
        name="doc.pdf",
        file_path="/tmp/doc.pdf",
        kb_id="kb1",
        document_id="doc-1",
        ontology_schema={
            "entity_types": [],
            "relation_types": [
                {"name": "OWNS", "description": "拥有关系", "source": "", "target": ""},
            ],
            "prompt_schema": {"entity_types": [], "relation_types": []},
        },
    )
    base.update(overrides)
    return TaskInfo(**base)


def _make_ctx() -> PipelineContext:
    return PipelineContext(
        file_path="/tmp/doc.pdf",
        tenant_id="tenant_jonex_demo",
        kb_id="kb1",
        document_id="doc-1",
        content_list=None,
    )


def _make_result() -> NativeExtractionResult:
    tbox_entity = ExtractedEntity(
        canonical_name="XX集团",
        entity_type="Organization",
        description="大型制造企业",
        confidence=0.95,
        source_chunks=[
            {"source_id": "chunk-aaa", "file_path": "kb=kb1|doc=doc-1|ctype=text"},
            {"source_id": "chunk-bbb", "file_path": "kb=kb1|doc=doc-1|ctype=text"},
        ],
        extraction_method="native_llm",
    )
    fallback_entity = ExtractedEntity(
        canonical_name="数字化转型趋势",
        entity_type="概念",
        source_chunks=[{"source_id": "chunk-aaa", "file_path": "kb=kb1|doc=doc-1|ctype=text"}],
        extraction_method="native_llm",
    )
    tbox_rel = ExtractedRelation(
        source_name="XX集团", source_type="Organization",
        target_name="产品A", target_type="Product",
        relation_type="OWNS", confidence=0.9,
        source_chunks=[{"source_id": "chunk-aaa", "file_path": "kb=kb1|doc=doc-1|ctype=text"}],
        keywords="", attributes={"step_no": 1},
    )
    fallback_rel = ExtractedRelation(
        source_name="XX集团", source_type="Organization",
        target_name="数字化转型趋势", target_type="概念",
        relation_type="", keywords="战略布局,行业趋势", confidence=0.85,
        source_chunks=[
            {"source_id": "chunk-aaa", "file_path": "kb=kb1|doc=doc-1|ctype=text"},
            {"source_id": "chunk-bbb", "file_path": "kb=kb1|doc=doc-1|ctype=text"},
        ],
    )
    return NativeExtractionResult(
        entities=[tbox_entity, fallback_entity],
        relations=[tbox_rel, fallback_rel],
        ontology_entities=[tbox_entity],
        ontology_relations=[tbox_rel],
        ok=True,
    )


class TestSerializeRelation:
    """P4 缺陷修复：relation 序列化补 attributes/source_chunks/keywords。"""

    def test_full_relation_roundtrip(self):
        rel = ExtractedRelation(
            source_name="A", source_type="T1",
            target_name="B", target_type="T2",
            relation_type="OWNS", confidence=0.9,
            source_chunks=[{"source_id": "chunk-1", "file_path": "fp"}],
            keywords="", attributes={"step_no": 3},
        )
        out = TaskManager._serialize_relation(rel)
        assert out == {
            "source_name": "A",
            "source_type": "T1",
            "target_name": "B",
            "target_type": "T2",
            "relation_type": "OWNS",
            "confidence": 0.9,
            "source_chunks": [{"source_id": "chunk-1", "file_path": "fp"}],
            "keywords": "",
            "attributes": {"step_no": 3},
        }

    def test_default_relation_has_keys(self):
        rel = ExtractedRelation(
            source_name="A", source_type="", target_name="B",
            target_type="", relation_type="",
        )
        out = TaskManager._serialize_relation(rel)
        # 旧序列化缺失的字段现在必须存在（reconcile→Neo4j 依赖）
        assert "source_chunks" in out and out["source_chunks"] == []
        assert "attributes" in out and out["attributes"] == {}
        assert out["keywords"] == ""


class TestSerializeEntity:
    def test_full_entity_roundtrip(self):
        ent = ExtractedEntity(
            canonical_name="X", entity_type="Organization",
            aliases=["X公司"], attributes={"industry": "制造"},
            description="desc", confidence=0.8,
            source_chunks=[{"source_id": "chunk-1", "file_path": "fp"}],
            extraction_method="row_as_object",
            table_sig="sig1", pk_value="pk1",
        )
        out = TaskManager._serialize_entity(ent)
        assert out["canonical_name"] == "X"
        assert out["aliases"] == ["X公司"]
        assert out["table_sig"] == "sig1" and out["pk_value"] == "pk1"


class TestWritebackCustomKg:
    @pytest.mark.asyncio
    async def test_source_id_sep_join_and_mapping(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        result = _make_result()
        with mock.patch.object(
            mgr, "_http_client", new=mock.MagicMock(),
        ) as client:
            client.insert_custom_kg = mock.AsyncMock(return_value={"status": "success"})
            ok = await mgr._writeback_custom_kg(
                task, "kb1", result, task.ontology_schema,
            )

        assert ok is True
        entities, relationships = client.insert_custom_kg.await_args.args[:2]
        # TBox 内实体：source_id 用 <SEP> 拼接权威 chunk_id（§5.7）
        tbox_e = next(e for e in entities if e["entity_name"] == "XX集团")
        assert tbox_e["source_id"] == "chunk-aaa<SEP>chunk-bbb"
        assert tbox_e["entity_type"] == "Organization"
        assert tbox_e["description"] == "大型制造企业"
        # 兜底粗类型实体同样回写（两档全集）
        assert any(e["entity_name"] == "数字化转型趋势" for e in entities)
        # TBox 内关系：keywords=relation_type，description=schema 中文描述
        rel1 = next(r for r in relationships if r["src_id"] == "XX集团" and r["tgt_id"] == "产品A")
        assert rel1["keywords"] == "OWNS"
        assert rel1["description"] == "拥有关系"
        assert rel1["weight"] == 0.9
        # TBox 外关系：keywords=自由关键词，description=keywords 首项
        rel2 = next(r for r in relationships if r["tgt_id"] == "数字化转型趋势")
        assert rel2["keywords"] == "战略布局,行业趋势"
        assert rel2["description"] == "战略布局"
        assert rel2["source_id"] == "chunk-aaa<SEP>chunk-bbb"

    @pytest.mark.asyncio
    async def test_source_id_dedup_preserves_order(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ent = ExtractedEntity(
            canonical_name="X", entity_type="T",
            source_chunks=[
                {"source_id": "chunk-aaa", "file_path": "fp1"},
                {"source_id": "chunk-bbb", "file_path": "fp2"},
                {"source_id": "chunk-aaa", "file_path": "fp1"},
            ],
        )
        result = NativeExtractionResult(entities=[ent], ok=True)
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client:
            client.insert_custom_kg = mock.AsyncMock(return_value={"status": "success"})
            await mgr._writeback_custom_kg(task, "kb1", result, {})

        entities, _ = client.insert_custom_kg.await_args.args[:2]
        assert entities[0]["source_id"] == "chunk-aaa<SEP>chunk-bbb"

    @pytest.mark.asyncio
    async def test_retry_once_then_success(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        result = _make_result()
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch("raganything.service.task_manager.asyncio.sleep",
                        new=mock.AsyncMock()):
            client.insert_custom_kg = mock.AsyncMock(
                side_effect=[RuntimeError("boom"), {"status": "success"}],
            )
            ok = await mgr._writeback_custom_kg(
                task, "kb1", result, task.ontology_schema,
            )
        assert ok is True
        assert client.insert_custom_kg.await_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_returns_false_no_raise(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        result = _make_result()
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch("raganything.service.task_manager.asyncio.sleep",
                        new=mock.AsyncMock()):
            client.insert_custom_kg = mock.AsyncMock(
                side_effect=RuntimeError("boom"),
            )
            ok = await mgr._writeback_custom_kg(
                task, "kb1", result, task.ontology_schema,
            )
        # §5.7：仍失败本体链路照常成功——返回 False 不抛出
        assert ok is False
        assert client.insert_custom_kg.await_count == 2


class TestRunOntologyExtractionP4:
    @pytest.mark.asyncio
    async def test_chunk_readback_failure_marks_failed(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ctx = _make_ctx()
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client:
            client.get_document_chunks = mock.AsyncMock(
                side_effect=RuntimeError("LightRAG down"),
            )
            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        assert task.ontology_status == "failed"
        assert "chunk 读回失败" in task.ontology_error

    @pytest.mark.asyncio
    async def test_empty_chunks_marks_failed(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ctx = _make_ctx()
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client:
            client.get_document_chunks = mock.AsyncMock(
                return_value={"doc_id": "doc-1", "total": 0, "chunks": []},
            )
            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        assert task.ontology_status == "failed"
        assert "未找到该文档 chunk" in task.ontology_error

    @pytest.mark.asyncio
    async def test_full_flow_uses_tbox_tier_and_writeback(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ctx = _make_ctx()
        chunks = [
            {"chunk_id": "chunk-aaa", "content": "XX集团收购了产品A",
             "file_path": "kb=kb1|doc=doc-1|ctype=text"},
            {"chunk_id": "chunk-bbb", "content": "行业趋势",
             "file_path": "kb=kb1|doc=doc-1|ctype=text"},
        ]
        result = _make_result()
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch(
                 "jonex_core.capability.atomic.rag.native_ontology_extractor."
                 "NativeOntologyExtractor",
             ) as extractor_cls:
            client.get_document_chunks = mock.AsyncMock(
                return_value={"doc_id": "doc-1", "total": 2, "chunks": chunks},
            )
            client.insert_custom_kg = mock.AsyncMock(return_value={"status": "success"})
            extractor_cls.return_value.extract = mock.AsyncMock(return_value=result)

            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        # tbox 传的是 compiled schema 的 prompt_schema（§5.2）
        call_kwargs = extractor_cls.return_value.extract.await_args.kwargs
        assert call_kwargs["tbox"] == task.ontology_schema["prompt_schema"]
        assert [c["chunk_id"] for c in call_kwargs["chunks"]] == ["chunk-aaa", "chunk-bbb"]

        assert task.ontology_status == "completed"
        # 本体存储侧只落 TBox 档（ontology_*），下游 reconcile 零改动
        assert len(task.ontology_data["entities"]) == 1
        assert task.ontology_data["entities"][0]["canonical_name"] == "XX集团"
        assert len(task.ontology_data["relations"]) == 1
        assert task.ontology_data["relations"][0]["relation_type"] == "OWNS"
        # custom-kg 回写被调用（两档全集）
        assert client.insert_custom_kg.await_count == 1

    @pytest.mark.asyncio
    async def test_duplicated_chunks_warns_gap_not_silent(self, tmp_path):
        """§8 风险表最后一行：跨文档判重 chunk 读不回时至少记 warning。"""
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task(duplicated_chunk_count=3)
        ctx = _make_ctx()
        result = _make_result()
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch(
                 "jonex_core.capability.atomic.rag.native_ontology_extractor."
                 "NativeOntologyExtractor",
             ) as extractor_cls, \
             mock.patch(
                 "jonex_core.capability.atomic.ontology_compile.extract.logger",
             ) as logger_mock:
            client.get_document_chunks = mock.AsyncMock(
                return_value={"doc_id": "doc-1", "total": 1,
                              "chunks": [{"chunk_id": "chunk-aaa",
                                          "content": "x",
                                          "file_path": "fp"}]},
            )
            client.insert_custom_kg = mock.AsyncMock(return_value={"status": "success"})
            extractor_cls.return_value.extract = mock.AsyncMock(return_value=result)

            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        assert any(
            "跨文档判重" in str(c) for c in logger_mock.warning.call_args_list
        ), "判重缺口必须记 warning，不得静默"

    @pytest.mark.asyncio
    async def test_writeback_failure_does_not_block_ontology(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ctx = _make_ctx()
        result = _make_result()
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch(
                 "jonex_core.capability.atomic.rag.native_ontology_extractor."
                 "NativeOntologyExtractor",
             ) as extractor_cls, \
             mock.patch("raganything.service.task_manager.asyncio.sleep",
                        new=mock.AsyncMock()):
            client.get_document_chunks = mock.AsyncMock(
                return_value={"doc_id": "doc-1", "total": 1,
                              "chunks": [{"chunk_id": "chunk-aaa",
                                          "content": "x",
                                          "file_path": "fp"}]},
            )
            client.insert_custom_kg = mock.AsyncMock(side_effect=RuntimeError("boom"))
            extractor_cls.return_value.extract = mock.AsyncMock(return_value=result)

            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        # §5.7：回写失败本体链路照常成功
        assert task.ontology_status == "completed"
        assert client.insert_custom_kg.await_count == 2


class TestTableSupplement:
    """P10 批 2.4：Row-as-Object 零产出时表格 chunk 回补 native 抽取。

    主抽取 table_objects_handled=True 剔除表格 chunk；确定性映射零产出
    （无 pk/表头判定失败）时表格信息整体丢失——对被剔除的表格 chunk
    补跑一次 native（table_objects_handled=False），产出并入两档全集。
    """

    @pytest.mark.asyncio
    async def test_zero_table_objects_triggers_supplement(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ctx = _make_ctx()
        ctx.content_list = [{"type": "table", "table_caption": "产品参数"}]
        chunks = [
            {"chunk_id": "chunk-txt", "content": "XX集团发布产品A",
             "file_path": "kb=kb1|doc=doc-1|ctype=text"},
            {"chunk_id": "chunk-tbl", "content": "型号|参数量\nX1|309B",
             "chunk_type": "table",
             "file_path": "kb=kb1|doc=doc-1|ctype=table"},
        ]
        main_result = _make_result()
        supp_entity = ExtractedEntity(
            canonical_name="X1", entity_type="Product",
            source_chunks=[{"source_id": "chunk-tbl",
                            "file_path": "kb=kb1|doc=doc-1|ctype=table"}],
            extraction_method="native_llm",
        )
        supplement = NativeExtractionResult(
            entities=[supp_entity], ontology_entities=[supp_entity],
            relations=[], ontology_relations=[], ok=True,
        )
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch.object(
                 mgr, "_merge_table_objects",
                 new=mock.AsyncMock(return_value=False),
             ), \
             mock.patch(
                 "jonex_core.capability.atomic.rag.native_ontology_extractor."
                 "NativeOntologyExtractor",
             ) as extractor_cls:
            # 类被整体 mock 后恢复真实 _is_table_chunk（补跑筛表格 chunk 用）
            extractor_cls._is_table_chunk = staticmethod(
                NativeOntologyExtractor._is_table_chunk
            )
            client.get_document_chunks = mock.AsyncMock(
                return_value={"doc_id": "doc-1", "total": 2, "chunks": chunks},
            )
            client.insert_custom_kg = mock.AsyncMock(
                return_value={"status": "success"},
            )
            extractor_cls.return_value.extract = mock.AsyncMock(
                side_effect=[main_result, supplement],
            )

            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        assert extractor_cls.return_value.extract.await_count == 2
        # 第一次主抽取：Row-as-Object 已接管（剔除表格 chunk）
        main_kwargs = extractor_cls.return_value.extract.await_args_list[0].kwargs
        assert main_kwargs["options"]["table_objects_handled"] is True
        # 第二次补跑：只含表格 chunk、table_objects_handled=False
        supp_kwargs = extractor_cls.return_value.extract.await_args_list[1].kwargs
        assert [c["chunk_id"] for c in supp_kwargs["chunks"]] == ["chunk-tbl"]
        assert supp_kwargs["options"]["table_objects_handled"] is False
        # 补跑产出并入本体存储档（TBox 档）
        names = [e["canonical_name"] for e in task.ontology_data["entities"]]
        assert "X1" in names
        assert task.ontology_status == "completed"

    @pytest.mark.asyncio
    async def test_table_objects_produced_skips_supplement(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ctx = _make_ctx()
        ctx.content_list = [{"type": "table", "table_caption": "产品参数"}]
        chunks = [
            {"chunk_id": "chunk-tbl", "content": "型号|参数量\nX1|309B",
             "chunk_type": "table",
             "file_path": "kb=kb1|doc=doc-1|ctype=table"},
        ]
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch.object(
                 mgr, "_merge_table_objects",
                 new=mock.AsyncMock(return_value=True),
             ), \
             mock.patch(
                 "jonex_core.capability.atomic.rag.native_ontology_extractor."
                 "NativeOntologyExtractor",
             ) as extractor_cls:
            # 类被整体 mock 后恢复真实 _is_table_chunk（补跑筛表格 chunk 用）
            extractor_cls._is_table_chunk = staticmethod(
                NativeOntologyExtractor._is_table_chunk
            )
            client.get_document_chunks = mock.AsyncMock(
                return_value={"doc_id": "doc-1", "total": 1, "chunks": chunks},
            )
            client.insert_custom_kg = mock.AsyncMock(
                return_value={"status": "success"},
            )
            extractor_cls.return_value.extract = mock.AsyncMock(
                return_value=_make_result(),
            )

            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        # 有表格对象产出 → 不补跑
        assert extractor_cls.return_value.extract.await_count == 1

    @pytest.mark.asyncio
    async def test_supplement_failure_does_not_block(self, tmp_path):
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ctx = _make_ctx()
        ctx.content_list = [{"type": "table", "table_caption": "产品参数"}]
        chunks = [
            {"chunk_id": "chunk-tbl", "content": "型号|参数量\nX1|309B",
             "chunk_type": "table",
             "file_path": "kb=kb1|doc=doc-1|ctype=table"},
        ]
        failed = NativeExtractionResult(ok=False, errors=["补跑失败"])
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch.object(
                 mgr, "_merge_table_objects",
                 new=mock.AsyncMock(return_value=False),
             ), \
             mock.patch(
                 "jonex_core.capability.atomic.rag.native_ontology_extractor."
                 "NativeOntologyExtractor",
             ) as extractor_cls, \
             mock.patch(
                 "jonex_core.capability.atomic.ontology_compile.extract.logger",
             ) as logger_mock:
            # 类被整体 mock 后恢复真实 _is_table_chunk（补跑筛表格 chunk 用）
            extractor_cls._is_table_chunk = staticmethod(
                NativeOntologyExtractor._is_table_chunk
            )
            client.get_document_chunks = mock.AsyncMock(
                return_value={"doc_id": "doc-1", "total": 1, "chunks": chunks},
            )
            client.insert_custom_kg = mock.AsyncMock(
                return_value={"status": "success"},
            )
            extractor_cls.return_value.extract = mock.AsyncMock(
                side_effect=[_make_result(), failed],
            )

            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        # 补跑失败仅告警，本体链路照常成功
        assert task.ontology_status == "completed"
        assert any(
            "回补" in str(c) for c in logger_mock.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_mapped_tables_count_as_table_handled(self, tmp_path):
        """§table-llm-bypass：md 文档 content_list 只有 text item，靠
        push 阶段登记的 _mapped_tables 判定「Row-as-Object 已接管」。

        sdn.md 实测：无此判定时表格 chunk 全量进 LLM 本体抽取（4123 chunk
        ≈ 90 分钟）。"""
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ctx = _make_ctx()
        ctx.content_list = [{
            "type": "text", "text": "正文",
            "_mapped_tables": [{
                "type": "table",
                "table_body": "| 编号 | 名称 |\n| --- | --- |\n| 1 | A |",
                "table_caption": "登记表",
                "table_idx": 3,
            }],
        }]
        chunks = [
            {"chunk_id": "chunk-txt", "content": "正文",
             "file_path": "kb=kb1|doc=doc-1|ctype=text"},
            {"chunk_id": "chunk-tbl", "content": "| 1 | A |",
             "chunk_type": "table_row",
             "file_path": "kb=kb1|doc=doc-1|ctype=table_row"},
        ]
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch.object(
                 mgr, "_merge_table_objects",
                 new=mock.AsyncMock(return_value=True),
             ), \
             mock.patch(
                 "jonex_core.capability.atomic.rag.native_ontology_extractor."
                 "NativeOntologyExtractor",
             ) as extractor_cls:
            extractor_cls._is_table_chunk = staticmethod(
                NativeOntologyExtractor._is_table_chunk
            )
            client.get_document_chunks = mock.AsyncMock(
                return_value={"doc_id": "doc-1", "total": 2, "chunks": chunks},
            )
            client.insert_custom_kg = mock.AsyncMock(
                return_value={"status": "success"},
            )
            extractor_cls.return_value.extract = mock.AsyncMock(
                return_value=_make_result(),
            )

            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        assert extractor_cls.return_value.extract.await_count == 1
        kwargs = extractor_cls.return_value.extract.await_args_list[0].kwargs
        assert kwargs["options"]["table_objects_handled"] is True

    @pytest.mark.asyncio
    async def test_merge_table_objects_consumes_mapped_tables(self, tmp_path):
        """md 路径：_mapped_tables 登记的表体进入 Row-as-Object 抽取。"""
        mgr = TaskManager(base_dir=str(tmp_path))
        ctx = _make_ctx()
        ctx.content_list = [{
            "type": "text", "text": "正文",
            "_mapped_tables": [{
                "type": "table",
                "table_body": "| 编号 | 名称 |\n| --- | --- |\n| 1 | A |",
                "table_caption": "登记表",
                "table_idx": 3,
            }],
        }]
        seen: list = []
        verdict = mock.MagicMock()
        verdict.source = "rules"
        verdict.pk_columns = ["编号"]
        verdict.qualifier_columns = []
        obj = mock.MagicMock(
            canonical_name="A", entity_type="品种", aliases=[],
            attributes={}, source_chunks=[], table_sig="s", pk_value="1",
            name_source="pk",
        )
        with mock.patch(
            "raganything.utils.get_table_body",
            side_effect=lambda item: seen.append(item) or item["table_body"],
        ), mock.patch(
            "jonex_core.capability.atomic.rag.table_object_extractor."
            "TableObjectExtractor",
        ) as extractor_cls:
            ex = extractor_cls.return_value
            ex.analyze_table = mock.AsyncMock(return_value=verdict)
            ex.build_objects = mock.MagicMock(return_value=[obj])
            ex.build_group_relations = mock.MagicMock(return_value=[])
            # row-to-table：新方法的 mock 必须显式给——MagicMock 的返回值不可
            # 迭代，`relations += <MagicMock>` 会回退到 `__radd__` 把整个
            # relations 变成 MagicMock，后续 `for rel in relations` 迭代出空。
            ex.build_table_relations = mock.MagicMock(return_value=[])
            produced = await mgr._merge_table_objects(
                ctx, {}, _make_result(),
            )

        # 登记的 md 表体被消费（而非只有 type=="table" item）
        assert len(seen) == 1
        assert seen[0]["table_idx"] == 3
        assert "编号" in seen[0]["table_body"]
        assert produced is True

    # ── P0-c（2026-09-13 线上 OOM 复盘）：主键为空必须重判 + 兜底落对象 ──

    @staticmethod
    def _p0c_ctx():
        ctx = _make_ctx()
        ctx.content_list = [{
            "type": "text", "text": "正文",
            "_mapped_tables": [{
                "type": "table",
                "table_body": "| 编号 | 名称 |\n| --- | --- |\n| 1 | A |",
                "table_caption": "登记表",
                "table_idx": 3,
            }],
        }]
        return ctx

    async def test_pk_empty_triggers_rejudgment(self, tmp_path):
        """主键为空时用纠正后的表头重判一次（原实现直接跳表）。"""
        mgr = TaskManager(base_dir=str(tmp_path))
        ctx = self._p0c_ctx()
        obj = mock.MagicMock(
            canonical_name="A", entity_type="品种", aliases=[],
            attributes={}, source_chunks=[], table_sig="s", pk_value="1",
            name_source="pk",
        )
        verdict = mock.MagicMock()
        verdict.source = "llm"
        verdict.pk_columns = []          # LLM 判得出表头、给不出主键（事故现场）
        verdict.qualifier_columns = []
        verdict.header_row_indices = []
        verdict.note_row_indices = []
        with mock.patch(
            "raganything.utils.get_table_body",
            side_effect=lambda item: item["table_body"],
        ), mock.patch(
            "jonex_core.capability.atomic.rag.table_object_extractor."
            "TableObjectExtractor",
        ) as extractor_cls:
            ex = extractor_cls.return_value
            ex.analyze_table = mock.AsyncMock(return_value=verdict)
            ex.resolve_pk_columns = mock.AsyncMock(return_value=(["编号"], "llm"))
            ex.build_objects = mock.MagicMock(return_value=[obj])
            ex.build_group_relations = mock.MagicMock(return_value=[])
            # row-to-table：新方法的 mock 必须显式给——MagicMock 的返回值不可
            # 迭代，`relations += <MagicMock>` 会回退到 `__radd__` 把整个
            # relations 变成 MagicMock，后续 `for rel in relations` 迭代出空。
            ex.build_table_relations = mock.MagicMock(return_value=[])
            produced = await mgr._merge_table_objects(ctx, {}, _make_result())

        assert ex.resolve_pk_columns.await_count == 1
        # 重判结果写回 verdict，对象按真主键命名
        assert verdict.pk_columns == ["编号"]
        assert produced is True

    async def test_pk_rejudgment_failure_still_produces_objects(self, tmp_path):
        """LLM + 规则都判不出主键时也不再整表放弃——对象走整行 hash 兜底。"""
        mgr = TaskManager(base_dir=str(tmp_path))
        ctx = self._p0c_ctx()
        obj = mock.MagicMock(
            canonical_name="1·ab12cd", entity_type="unknown", aliases=[],
            attributes={}, source_chunks=[], table_sig="s", pk_value="1·ab12cd",
            name_source="row_hash",
        )
        verdict = mock.MagicMock()
        verdict.source = "llm"
        verdict.pk_columns = []
        verdict.qualifier_columns = []
        verdict.header_row_indices = []
        verdict.note_row_indices = []
        with mock.patch(
            "raganything.utils.get_table_body",
            side_effect=lambda item: item["table_body"],
        ), mock.patch(
            "jonex_core.capability.atomic.rag.table_object_extractor."
            "TableObjectExtractor",
        ) as extractor_cls:
            ex = extractor_cls.return_value
            ex.analyze_table = mock.AsyncMock(return_value=verdict)
            ex.resolve_pk_columns = mock.AsyncMock(return_value=([], ""))
            ex.build_objects = mock.MagicMock(return_value=[obj])
            ex.build_group_relations = mock.MagicMock(return_value=[])
            # row-to-table：新方法的 mock 必须显式给——MagicMock 的返回值不可
            # 迭代，`relations += <MagicMock>` 会回退到 `__radd__` 把整个
            # relations 变成 MagicMock，后续 `for rel in relations` 迭代出空。
            ex.build_table_relations = mock.MagicMock(return_value=[])
            produced = await mgr._merge_table_objects(ctx, {}, _make_result())

        # 关键：原实现在这里 `continue`（零产出 → 触发全量 LLM 兜底）
        assert ex.build_objects.call_count == 1
        assert produced is True
        # P0-c 观测指标落 ctx.table_stats
        assert ctx.table_stats["tables_pk_empty"] == 1
        assert ctx.table_stats["rows_row_hash"] == 1

    async def test_pk_present_skips_rejudgment(self, tmp_path):
        """主键可用时不得多打一次 LLM。"""
        mgr = TaskManager(base_dir=str(tmp_path))
        ctx = self._p0c_ctx()
        obj = mock.MagicMock(
            canonical_name="A", entity_type="品种", aliases=[],
            attributes={}, source_chunks=[], table_sig="s", pk_value="1",
            name_source="pk",
        )
        verdict = mock.MagicMock()
        verdict.source = "llm"
        verdict.pk_columns = ["编号"]
        verdict.qualifier_columns = []
        verdict.header_row_indices = []
        verdict.note_row_indices = []
        with mock.patch(
            "raganything.utils.get_table_body",
            side_effect=lambda item: item["table_body"],
        ), mock.patch(
            "jonex_core.capability.atomic.rag.table_object_extractor."
            "TableObjectExtractor",
        ) as extractor_cls:
            ex = extractor_cls.return_value
            ex.analyze_table = mock.AsyncMock(return_value=verdict)
            ex.resolve_pk_columns = mock.AsyncMock(return_value=([], ""))
            ex.build_objects = mock.MagicMock(return_value=[obj])
            ex.build_group_relations = mock.MagicMock(return_value=[])
            # row-to-table：新方法的 mock 必须显式给——MagicMock 的返回值不可
            # 迭代，`relations += <MagicMock>` 会回退到 `__radd__` 把整个
            # relations 变成 MagicMock，后续 `for rel in relations` 迭代出空。
            ex.build_table_relations = mock.MagicMock(return_value=[])
            await mgr._merge_table_objects(ctx, {}, _make_result())

        assert ex.resolve_pk_columns.await_count == 0

    async def test_group_nodes_materialized_as_entities(self, tmp_path):
        """分组聚合节点必须实体化。

        此前分组关系只产出边，端点没有实体 → 写图时 ``merge_relation`` 兜底
        建 stub → ``count_relations``（要求两端非 stub）数不到任何分组关系，
        前端恒显示 0 关系（Neo4j 里其实有边）。
        """
        mgr = TaskManager(base_dir=str(tmp_path))
        ctx = _make_ctx()
        ctx.content_list = [{
            "type": "text", "text": "正文",
            "_mapped_tables": [{
                "type": "table",
                "table_body": "| 编号 | 名称 |\n| --- | --- |\n| 1 | A |",
                "table_caption": "登记表",
                "table_idx": 3,
            }],
        }]
        obj = mock.MagicMock(
            canonical_name="A", entity_type="品种", aliases=[],
            attributes={}, source_chunks=[], table_sig="s", pk_value="1",
        )
        verdict = mock.MagicMock()
        verdict.source = "rules"
        verdict.pk_columns = ["编号"]
        verdict.qualifier_columns = []
        with mock.patch(
            "raganything.utils.get_table_body",
            side_effect=lambda item: item["table_body"],
        ), mock.patch(
            "jonex_core.capability.atomic.rag.table_object_extractor."
            "TableObjectExtractor",
        ) as extractor_cls:
            ex = extractor_cls.return_value
            ex.analyze_table = mock.AsyncMock(return_value=verdict)
            ex.build_objects = mock.MagicMock(return_value=[obj])
            ex.build_group_relations = mock.MagicMock(return_value=[{
                "source_name": "类型·甲", "source_type": "类型",
                "target_name": "A", "target_type": "品种",
                "relation_type": "包含",
            }])
            # row-to-table：本用例断言的是分组节点实体化，表归属边显式给空，
            # 保持断言面不变。（表节点的实体化走同一段循环，由端到端验证覆盖。）
            ex.build_table_relations = mock.MagicMock(return_value=[])
            result = _make_result()
            produced = await mgr._merge_table_objects(ctx, {}, result)

        assert produced is True
        # 分组节点实体化后，关系的两端才都是正式实体
        assert "类型·甲" in [e.canonical_name for e in result.ontology_entities]
        assert any(
            e.canonical_name == "类型·甲"
            and e.extraction_method == "row_as_object"
            for e in result.ontology_entities
        )
        assert any(
            r.source_name == "类型·甲" and r.target_name == "A"
            and r.relation_type == "包含"
            for r in result.ontology_relations
        )


class TestRowEvidenceWiring:
    """S1b：`_merge_table_objects` 用已读进内存的 chunk 给行对象回挂证据。

    （ontology-rag-scoring-unification-and-routing-plan.md §6 S1b / §12.5）
    单测覆盖 join 本身见 tests/unit/test_row_chunk_evidence.py；此处只管
    **接线**：开关、chunks 透传、降级不炸。
    """

    @staticmethod
    def _ctx() -> PipelineContext:
        ctx = _make_ctx()
        ctx.content_list = [{
            "type": "text", "text": "正文",
            "_mapped_tables": [{
                "type": "table",
                "table_body": "| 编号 | 名称 |\n| --- | --- |\n| 1 | A |",
                "table_caption": "登记表",
                "table_idx": 3,
            }],
        }]
        return ctx

    @staticmethod
    def _obj() -> TableObject:
        return TableObject(
            canonical_name="A", entity_type="品种",
            source_chunks=[{"row_start": 1, "row_end": 2,
                            "table_idx": 3, "table_sig": "sigX"}],
            pk_value="1", table_sig="sigX",
        )

    @staticmethod
    def _chunk() -> dict:
        """chunk 打包 0-3 行（多行段），命中其中的第 1 行。"""
        return {
            "chunk_id": "chunk-tbl", "content": "| 1 | A |",
            "chunk_type": "table_row",
            "file_path": (
                "kb=kb1|doc=doc-1|tenant=t1|file=f.pdf|chunk=2|page=1"
                "|row_start=0|row_end=3|ctype=table_row|table_sig=sigX|trace="
            ),
        }

    async def _run(self, tmp_path, obj: TableObject, chunks):
        mgr = TaskManager(base_dir=str(tmp_path))
        verdict = mock.MagicMock()
        verdict.source = "rules"
        verdict.pk_columns = ["编号"]
        verdict.qualifier_columns = []
        with mock.patch(
            "raganything.utils.get_table_body",
            side_effect=lambda item: item["table_body"],
        ), mock.patch(
            "jonex_core.capability.atomic.rag.table_object_extractor."
            "TableObjectExtractor",
        ) as extractor_cls:
            ex = extractor_cls.return_value
            ex.analyze_table = mock.AsyncMock(return_value=verdict)
            ex.build_objects = mock.MagicMock(return_value=[obj])
            ex.build_group_relations = mock.MagicMock(return_value=[])
            # row-to-table：新方法的 mock 必须显式给——MagicMock 的返回值不可
            # 迭代，`relations += <MagicMock>` 会回退到 `__radd__` 把整个
            # relations 变成 MagicMock，后续 `for rel in relations` 迭代出空。
            ex.build_table_relations = mock.MagicMock(return_value=[])
            produced = await mgr._merge_table_objects(
                self._ctx(), {}, _make_result(), chunks=chunks,
            )
        return produced

    @pytest.mark.asyncio
    async def test_disabled_keeps_row_anchor_only(self, tmp_path):
        """开关关闭 = 改造前行为：只有行锚点，不产出 chunk 引用。"""
        obj = self._obj()
        with mock.patch.dict(
            os.environ, {"ONTOLOGY_ROW_EVIDENCE_ENABLED": "false"},
        ):
            produced = await self._run(tmp_path, obj, [self._chunk()])
        assert produced is True
        assert obj.source_chunks == [{
            "row_start": 1, "row_end": 2, "table_idx": 3, "table_sig": "sigX",
        }]

    @pytest.mark.asyncio
    async def test_enabled_appends_chunk_ref(self, tmp_path):
        """开关开启：区间包含命中 → 追加 {source_id, file_path}。"""
        obj = self._obj()
        with mock.patch.dict(
            os.environ, {"ONTOLOGY_ROW_EVIDENCE_ENABLED": "true"},
        ):
            await self._run(tmp_path, obj, [self._chunk()])
        assert obj.source_chunks[0]["row_start"] == 1      # 行锚点保留
        assert obj.source_chunks[1] == {
            "source_id": "chunk-tbl",
            "file_path": self._chunk()["file_path"],
        }

    @pytest.mark.asyncio
    async def test_no_chunks_degrades_without_error(self, tmp_path):
        """chunks 缺失（grid-v2 关闭等）→ 打点降级，不抛错、不断抽取。"""
        obj = self._obj()
        with mock.patch.dict(
            os.environ, {"ONTOLOGY_ROW_EVIDENCE_ENABLED": "true"},
        ):
            produced = await self._run(tmp_path, obj, [])
        assert produced is True
        assert len(obj.source_chunks) == 1

    @pytest.mark.asyncio
    async def test_cap_follows_source_chunks_max(self, tmp_path):
        """配额沿用 ONTOLOGY_SOURCE_CHUNKS_MAX（同一 OOM 根因的同一道闸）。"""
        with mock.patch.dict(
            os.environ, {"ONTOLOGY_SOURCE_CHUNKS_MAX": "3"},
        ):
            assert TaskManager._row_evidence_cap() == 3
        with mock.patch.dict(
            os.environ, {"ONTOLOGY_SOURCE_CHUNKS_MAX": "0"},
        ):
            assert TaskManager._row_evidence_cap() == 0      # 0 = 不限
        with mock.patch.dict(
            os.environ, {"ONTOLOGY_SOURCE_CHUNKS_MAX": "not-a-number"},
        ):
            assert TaskManager._row_evidence_cap() == 32     # 坏值回落默认
