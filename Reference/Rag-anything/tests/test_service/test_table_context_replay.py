"""[jonex] P0-b：表格上下文跨任务回放。

背景：Row-as-Object 表格对象只从 content_list 的 table item 推导
（``_merge_table_objects`` 的唯一输入），而 ontology_only 重抽既不本地化原
文件也不解析（P0-A）→ ``ctx.content_list`` 恒为空 → 重抽结果**不含表格
对象**；写图又是 ``delete_by_document`` 全量替换 → 首轮由表格对象建立的节点
被静默换掉。

为什么不让重抽自己重新解析：解析缓存键含 ``str(file_path.absolute())`` 与
``st_mtime``（``ParseCacheManager.generate_cache_key``），而
``_fetch_cos_object`` 每次下载都落在带随机 uuid 前缀的新路径上 → key 必不同
→ 必然 miss → 每次都要重解一遍原文件。

修法：full 链路进本体阶段时把 content_list 的表格子集 gzip 落盘，ontology_only
读回放进 ``ctx.content_list``——让 table_handled / _merge_table_objects /
P10 零产出兜底三条既有逻辑原样生效，判据一个不改；存档缺失（上线前入库的
老文档）**不降级**，由 P0-b 闸门按 chunk 判据拒绝重抽，首轮结果原样保留。

本文件钉住的契约：
  ① 存档三态必须可区分——文件不存在（无存档）/ 空列表（首轮确认无表格）/
     非空（回放）；把「本来就没表格」误报成「存档丢失」是回归。
  ② 存档条目一律扁平化、不带 ``_mapped_tables`` 嵌套，否则读回后同一个表
     会被 _merge_table_objects 当作顶层 item 和内层条目各消费一次。
  ③ 存档须由上游调用点保证落在早退之前；早退不得毁档（chunk 读回失败 /
     native 抽取异常都拦不住本体重试，但存档要在；调用点守卫见
     TestPersistCallSites）。
  ④ **无存档 + 本文档确有表格 chunk → 拒绝抽取**，落 ontology_status=failed
     且不调 extract / 不调 _merge_table_objects（降级会以更差口径全量替换
     首轮的好结果，还要多烧一小时 LLM）；无表格 chunk 的纯文本文档不受影响。
  ⑤ 上限按**解压后**字节判定（内存保护：读回是一次性 gzip+json.loads 再展开
     成 Python 对象）。压缩比是文档属性、能差两个数量级，故「压缩后」当不了
     安全阈值——gzip 只负责省磁盘（实测 XBRL 财报 23.72MB → 0.57MB）。
"""

import gzip
import json
import os
import threading
import time
from unittest import mock

import pytest

from jonex_core.capability.atomic.rag.native_ontology_extractor import (
    NativeOntologyExtractor,
)
from raganything.service import task_manager
from raganything.service.models import TaskInfo
from raganything.service.task_manager import (
    TaskManager,
    _load_table_context,
    _persist_table_context,
    _sweep_table_context,
    _table_context_path,
    _write_table_context,
)


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


def _make_handle():
    return mock.MagicMock(cancel_event=threading.Event())


def _mapped_table(**overrides) -> dict:
    item = {
        "type": "table",
        "table_body": "| 编号 | 名称 |\n| --- | --- |\n| 1 | A |",
        "table_caption": "登记表",
        "table_idx": 3,
    }
    item.update(overrides)
    return item


class TestTableContextArchive:
    """存档读写（纯函数，不起 TaskManager）。"""

    def test_persist_extracts_both_sources(self, tmp_path, monkeypatch):
        """两类来源都要抽到：type=="table" item + text item 上的 _mapped_tables。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        n = _persist_table_context(task, [
            {"type": "table", "table_body": "| a |\n| - |\n| 1 |"},
            {"type": "text", "text": "正文", "_mapped_tables": [_mapped_table()]},
            {"type": "text", "text": "无表的正文"},
        ])
        assert n == 2
        assert len(_load_table_context(task)) == 2

    def test_entries_are_flat_without_nested_mapped_tables(self, tmp_path, monkeypatch):
        """读回后每个条目都不得再带 _mapped_tables，否则表格会被重复消费。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        _persist_table_context(task, [
            {"type": "text", "text": "正文", "_mapped_tables": [_mapped_table()]},
        ])
        entries = _load_table_context(task)
        assert len(entries) == 1
        assert "_mapped_tables" not in entries[0]
        assert entries[0]["type"] == "table"
        assert entries[0]["table_idx"] == 3

    def test_table_data_alias_is_folded_into_table_body(self, tmp_path, monkeypatch):
        """table_data / text 承载表体时（非 MinerU parser），统一落到 table_body，
        保证读回后 get_table_body 取值口径不变。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        _persist_table_context(task, [
            {"type": "table", "table_data": [["a"], ["1"]]},
        ])
        entries = _load_table_context(task)
        assert entries[0]["table_body"] == [["a"], ["1"]]

    def test_empty_content_list_writes_nothing(self, tmp_path, monkeypatch):
        """content_list 为空 = 非 full 链路/解析无产出 → 不写、不定论。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        assert _persist_table_context(task, None) is None
        assert _persist_table_context(task, []) is None
        assert not os.path.exists(_table_context_path(task))
        assert _load_table_context(task) is None

    def test_zero_tables_still_writes_empty_archive(self, tmp_path, monkeypatch):
        """无表格 ≠ 无存档：仍要落一个空列表，否则重抽会把「本来就没表格」
        误报成「存档丢失」并告警。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        assert _persist_table_context(task, [{"type": "text", "text": "正文"}]) == 0
        assert os.path.exists(_table_context_path(task))
        assert _load_table_context(task) == []

    def test_missing_archive_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        assert _load_table_context(_make_task()) is None

    def test_corrupt_archive_returns_none_and_warns(self, tmp_path, monkeypatch):
        """存档损坏按「无存档」降级，不得抛出去打断重抽。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        path = _table_context_path(task)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"{ not json")
        with mock.patch("jonex_core.capability.atomic.ontology_compile.table_context.logger") as logger_mock:
            assert _load_table_context(task) is None
        assert any("读取失败" in str(c) for c in logger_mock.warning.call_args_list)

    def test_non_list_archive_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        path = _table_context_path(task)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"type": "table"}, fh)
        assert _load_table_context(task) is None

    def test_oversize_does_not_write(self, tmp_path, monkeypatch):
        """契约⑤：上限按**解压后**字节判（内存保护）——即使这段内容压缩后会
        很小，只要解压后超限就不落盘，避免读回时 gzip+json.loads 把内存撑爆
        （「压缩后」当不了安全阈值：压缩比是文档属性，能差两个数量级）。
        """
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        monkeypatch.setenv("RAG_TABLE_CTX_MAX_BYTES", "1024")
        task = _make_task()
        # ≈20KB 原始、压缩后仅几百字节 —— 按压缩后判会放行，按解压后判要拒绝
        body = "| 2020 | Q1 | 12345 |\n" * 1000
        with mock.patch("jonex_core.capability.atomic.ontology_compile.table_context.logger") as logger_mock:
            assert _persist_table_context(task, [
                {"type": "table", "table_body": body},
            ]) is None
        assert not os.path.exists(_table_context_path(task))
        assert any("超限" in str(c) for c in logger_mock.warning.call_args_list)

    def test_highly_repetitive_body_survives_small_limit(self, tmp_path, monkeypatch):
        """上限按解压后判 + gzip 只省磁盘，两者独立：实测最脏的 XBRL 财报
        表体 23.72MB，落盘后磁盘占用应远小于原始（该文档实测 41.5x）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        monkeypatch.setenv("RAG_TABLE_CTX_MAX_BYTES", str(1024 * 1024))
        task = _make_task()
        body = "| 2020 | Q1 | 12345 |\n" * 40000  # ≈800KB 原始 < 1MiB 上限
        assert _persist_table_context(task, [
            {"type": "table", "table_body": body},
        ]) == 1
        size = os.path.getsize(_table_context_path(task))
        assert size < 64 * 1024
        assert size < len(body) / 10
        assert _load_table_context(task)[0]["table_body"] == body

    def test_archive_is_gzip_on_disk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        _persist_table_context(task, [{"type": "table", "table_body": "| a |"}])
        with open(_table_context_path(task), "rb") as fh:
            blob = fh.read()
        assert blob[:2] == b"\x1f\x8b"
        assert json.loads(gzip.decompress(blob).decode("utf-8"))[0][
            "table_body"
        ] == "| a |"

    def test_plain_json_archive_is_accepted(self, tmp_path, monkeypatch):
        """宽容读：非 gzip 魔数时按明文 JSON 解析（方便运维手工塞存档排查，
        不是历史格式兼容——本功能上线前没有存档文件）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        path = _table_context_path(task)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(json.dumps([{"type": "table", "table_body": "| a |"}]).encode())
        assert _load_table_context(task) == [{"type": "table", "table_body": "| a |"}]

    def test_path_is_tenant_kb_scoped_and_sanitized(self, tmp_path, monkeypatch):
        """逐段净化：目录分隔符不得存活，防路径穿越。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task(tenant_id="../../etc", kb_id="kb/../x", document_id="d\\1")
        rel = os.path.relpath(_table_context_path(task), str(tmp_path))
        parts = rel.split(os.sep)
        assert len(parts) == 3
        assert all(os.sep not in p and "/" not in p and "\\" not in p for p in parts)
        assert parts[-1].endswith(".json.gz")

    def test_roundtrip_is_lossless_for_list_body(self, tmp_path, monkeypatch):
        """list 型表体（非 MinerU parser）往返保真。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        body = [["编号", "名称"], ["1", "A"]]
        _persist_table_context(task, [
            {"type": "table", "table_body": body, "table_caption": ["表1", "登记表"]},
        ])
        entries = _load_table_context(task)
        assert entries[0]["table_body"] == body
        assert entries[0]["table_caption"] == ["表1", "登记表"]

    def test_write_table_context_writes_empty_list_unconditionally(self, tmp_path, monkeypatch):
        """[A18] 底层写口**无条件可写**：空列表是合法值（前置段恒写档用）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        assert _write_table_context(task, []) == 0
        assert _load_table_context(task) == []

    def test_persist_write_when_empty_false_preserves_existing_archive(
        self, tmp_path, monkeypatch,
    ):
        """[A18] wrapper 语义保持：write_when_empty=False + 抽到 0 条 ⇒ 不写、
        磁盘上原有存档原样保留（提炼不得改这条）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        assert _persist_table_context(task, [_mapped_table()]) == 1
        assert _persist_table_context(
            task, [{"type": "text", "text": "无表正文"}], write_when_empty=False,
        ) is None
        assert len(_load_table_context(task) or []) == 1

    def test_extract_failure_warns_and_writes_nothing(self, tmp_path, monkeypatch):
        """[A18 复审] wrapper 的抽取 except 分支：`_table_context_items` 抛错
        ⇒ 返回 None、warning 文案含「抽取失败」、磁盘不产生存档（best-effort）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        with mock.patch(
            "jonex_core.capability.atomic.ontology_compile.table_context._table_context_items",
            side_effect=RuntimeError("boom"),
        ), mock.patch("jonex_core.capability.atomic.ontology_compile.table_context.logger") as logger_mock:
            assert _persist_table_context(
                task, [{"type": "table", "table_body": "| a |"}],
            ) is None
        assert any("抽取失败" in str(c) for c in logger_mock.warning.call_args_list)
        assert not os.path.exists(_table_context_path(task))

    def test_write_failure_is_swallowed_returns_none(self, tmp_path, monkeypatch):
        """[A18 复审] 写口内层异常（如 os.replace 失败）必须被吞掉：返回 None
        不抛出（best-effort 不得阻断本体链路），且不产生完整存档。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        with mock.patch(
            "raganything.service.task_manager.os.replace",
            side_effect=OSError("disk full"),
        ):
            assert _write_table_context(
                task, [{"type": "table", "table_body": "| a |"}],
            ) is None
        assert _load_table_context(task) is None

    def test_sweep_called_on_success(self, tmp_path, monkeypatch):
        """[A18 复审] 成功路径必须触发 TTL 清扫（兜底磁盘泄漏；防误删靠 TTL）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        task = _make_task()
        with mock.patch(
            "jonex_core.capability.atomic.ontology_compile.table_context._sweep_table_context",
        ) as sweep:
            assert _write_table_context(
                task, [{"type": "table", "table_body": "| a |"}],
            ) == 1
        sweep.assert_called_once()


class TestPersistCallSites:
    """落盘调用点与存档存活（不再声称「落盘时机在所有早退之前」）。

    历史：本类原名 TestPersistOnFullPath，断言的是「落盘发生在 _run_ontology_
    extraction 内的所有早退之前」（de8b4235 引入）。4e5b71ad（2026-09-17 三步
    拆分）把落盘上移到 push 时的三个调用点后，该断言恒红，且无任何测试再覆盖
    「调用点真的落盘」。现按实际语义重写：① 真实调用点（prepare_only /
    push_only 入口）必须落盘；② 抽取读回失败不得毁掉已落的存档。full 链路的
    第三个调用点（`_execute_pipeline_http` push 时）同点**未加守卫**——mock
    面过大、成本/收益不划算；盲区影响是「响的」：该处缺失会让走完整链路的
    文档在日后 ontology_only 重抽时命中 P0-b 闸门判 FAILED，而不是静默丢表。
    """

    @pytest.mark.asyncio
    async def test_archive_survives_chunk_readback_failure(self, tmp_path, monkeypatch):
        """抽取读回失败（任务判 failed）不得毁掉上游已落的存档。

        [A18 修订] 原用例名 test_persist_happens_before_chunk_readback_failure，
        断言的是「落盘发生在 `_run_ontology_extraction` 内的所有早退之前」
        （de8b4235 引入）；4e5b71ad（2026-09-17 三步拆分）把落盘上移到 push 时的
        三个调用点后该断言恒红、也不再是被测属性。现按实际断言语义重写（对当前
        代码通过 = 测试修正，不是驱动新代码）；调用点落盘由下面两条入口用例守卫。
        """
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        ctx = mock.MagicMock()
        ctx.content_list = [
            {"type": "text", "text": "正文", "_mapped_tables": [_mapped_table()]},
        ]
        # 上游（push 之后落盘形态）先落盘：
        assert _persist_table_context(task, ctx.content_list) == 1
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client:
            client.get_document_chunks = mock.AsyncMock(
                side_effect=RuntimeError("boom"),
            )
            await mgr._run_ontology_extraction(task, ctx, "doc-1")

        # 抽取失败（chunk 读回失败）
        assert task.ontology_status == "failed"
        # 存档仍在，重抽可回放同一口径
        assert len(_load_table_context(task) or []) == 1

    @pytest.mark.asyncio
    async def test_prepare_only_call_site_persists_archive(self, tmp_path, monkeypatch):
        """调用点行为守卫：走真实 `_run_prepare_only`，pipeline 产出 content_list
        后必须落盘（删掉该处 `_persist_table_context` 调用本用例即红；只 mock
        executor 与产物落盘函数，其余走真实路径）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        executor = mock.MagicMock()
        result = mock.MagicMock(success=True)

        async def _prepare_only(ctx):
            ctx.content_list = [
                {"type": "table", "table_body": "| a |\n| - |\n| 1 |"},
            ]
            return result

        executor.prepare_only = _prepare_only
        with mock.patch.object(mgr, "_pipeline_executor", new=executor), \
             mock.patch(
                 "raganything.service.task_manager._persist_ingest_artifacts",
                 return_value=(True, ""),
             ):
            await mgr._run_prepare_only(task, _make_handle())

        assert len(_load_table_context(task) or []) == 1

    @pytest.mark.asyncio
    async def test_push_only_call_site_persists_archive(self, tmp_path, monkeypatch):
        """调用点行为守卫：走真实 `_run_push_only`，切片推完（含 push 阶段刚登记
        `_mapped_tables` 的形态）后必须落盘（删掉该处调用本用例即红）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        executor = mock.MagicMock()
        result = mock.MagicMock(success=True)

        async def _push_only(ctx):
            ctx.content_list = [
                {"type": "text", "text": "正文", "_mapped_tables": [_mapped_table()]},
            ]
            return result

        executor.push_chunks_only = _push_only
        with mock.patch.object(mgr, "_pipeline_executor", new=executor), \
             mock.patch(
                 "raganything.service.task_manager._load_ingest_artifacts",
                 return_value=([{"type": "text", "text": "正文"}], [], {}),
             ), mock.patch.object(
                 mgr, "_read_result_summary_http",
                 new=mock.AsyncMock(return_value=mock.MagicMock()),
             ):
            await mgr._run_push_only(task, _make_handle())

        assert len(_load_table_context(task) or []) == 1


class TestOntologyOnlyReplay:
    """ontology_only 重抽回放。"""

    # 一条纯文本 chunk + 一条表格 chunk（表格 chunk 是 P0-b 闸门的触发条件）
    _CHUNKS_WITH_TABLE = [
        {"chunk_id": "chunk-txt", "content": "正文",
         "file_path": "kb=kb1|doc=doc-1|ctype=text"},
        {"chunk_id": "chunk-tbl", "content": "| 1 | A |",
         "chunk_type": "table_row",
         "file_path": "kb=kb1|doc=doc-1|ctype=table_row"},
    ]
    _CHUNKS_TEXT_ONLY = [_CHUNKS_WITH_TABLE[0]]

    async def _run_ontology_only(self, mgr, task, merge=None, chunks=None,
                                 writeback=None):
        """跑一次 ``_run_ontology_only``，返回 ``(extractor_cls, merge_mock)``。

        ``merge`` / ``writeback`` 必须由调用方传入才能断言其 await_count——
        patch 出去的必须是同一个对象，否则拿到的是另一个 mock（恒为 0）。
        """
        if merge is None:
            merge = mock.AsyncMock(return_value=True)
        if writeback is None:
            writeback = mock.AsyncMock(return_value=None)
        with mock.patch.object(mgr, "_http_client", new=mock.MagicMock()) as client, \
             mock.patch.object(mgr, "_merge_table_objects", new=merge), \
             mock.patch.object(mgr, "_writeback_custom_kg", new=writeback), \
             mock.patch(
                 "jonex_core.capability.atomic.rag.native_ontology_extractor."
                 "NativeOntologyExtractor",
             ) as extractor_cls:
            extractor_cls._is_table_chunk = staticmethod(
                NativeOntologyExtractor._is_table_chunk
            )
            client.get_document_chunks = mock.AsyncMock(return_value={
                "doc_id": "doc-1", "total": len(
                    self._CHUNKS_WITH_TABLE if chunks is None else chunks
                ),
                "chunks": (
                    self._CHUNKS_WITH_TABLE if chunks is None else chunks
                ),
            })
            client.insert_custom_kg = mock.AsyncMock(
                return_value={"status": "success"},
            )
            extractor_cls.return_value.extract = mock.AsyncMock(
                return_value=mock.MagicMock(
                    ok=True, errors=[], entities=[], relations=[],
                    ontology_entities=[], ontology_relations=[],
                ),
            )
            await mgr._run_ontology_only(task, _make_handle())
        return extractor_cls, merge

    @pytest.mark.asyncio
    async def test_replay_marks_table_handled_like_first_round(self, tmp_path, monkeypatch):
        """回放命中 → table_handled=True（表格 chunk 退出 LLM 路径，改由
        Row-as-Object 确定性映射接管）——与首轮 full 链路同口径。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        # 首轮 full 链路落盘
        _persist_table_context(task, [
            {"type": "text", "text": "正文", "_mapped_tables": [_mapped_table()]},
        ])

        extractor_cls, _ = await self._run_ontology_only(mgr, task)

        kwargs = extractor_cls.return_value.extract.await_args_list[0].kwargs
        assert kwargs["options"]["table_objects_handled"] is True

    @pytest.mark.asyncio
    async def test_missing_archive_with_table_chunks_is_rejected(self, tmp_path, monkeypatch):
        """契约④（核心）：无存档 + 本文档确有表格 chunk → **拒绝抽取**，不降级。

        降级会用更差的文本口径全量替换首轮由表格对象建立的节点，还要多烧一小时
        LLM；拒绝则首轮结果一行不动，KB 侧走「已完成但无 ontology_data」分支
        落 FAILED 并把指引透到文档 error。
        """
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        merge = mock.AsyncMock(return_value=True)

        with mock.patch("jonex_core.capability.atomic.ontology_compile.extract.logger") as logger_mock:
            extractor_cls, _ = await self._run_ontology_only(mgr, task, merge=merge)

        assert task.ontology_status == "failed"
        assert "force_reparse" in (task.ontology_error or "")
        # 没进 LLM、没做确定性映射 —— 拒绝是真的拒绝
        assert extractor_cls.return_value.extract.await_count == 0
        assert merge.await_count == 0
        assert any("拒绝" in str(c) for c in logger_mock.error.call_args_list)

    @pytest.mark.asyncio
    async def test_missing_archive_text_only_doc_still_runs(self, tmp_path, monkeypatch):
        """契约④的另一半：无存档但无表格 chunk（纯文本文档）→ 照常跑，
        不被「上线前入库的老文档」误伤。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()

        extractor_cls, merge = await self._run_ontology_only(
            mgr, task, chunks=self._CHUNKS_TEXT_ONLY,
        )

        assert task.ontology_status != "failed"
        assert extractor_cls.return_value.extract.await_count == 1
        assert extractor_cls.return_value.extract.await_args_list[0].kwargs[
            "options"]["table_objects_handled"
        ] is False
        assert merge.await_count == 0

    @pytest.mark.asyncio
    async def test_empty_archive_does_not_warn(self, tmp_path, monkeypatch):
        """首轮确认无表格（空存档）→ 不告警：把「本来就没表格」报成降级是误报。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        _persist_table_context(task, [{"type": "text", "text": "纯文本，无表"}])

        with mock.patch("jonex_core.capability.atomic.ontology_compile.extract.logger") as logger_mock:
            extractor_cls, _ = await self._run_ontology_only(mgr, task)

        kwargs = extractor_cls.return_value.extract.await_args_list[0].kwargs
        assert kwargs["options"]["table_objects_handled"] is False
        # 空存档不是「无存档」：不得命中 P0-b 闸门（这一条是契约①的回归闸门）
        assert task.ontology_status != "failed"
        assert not any(
            "拒绝" in str(c) for c in logger_mock.error.call_args_list
        )

    @pytest.mark.asyncio
    async def test_replay_reaches_merge_table_objects(self, tmp_path, monkeypatch):
        """回放非空 → _merge_table_objects 真的被调用（首轮表格对象才不会
        在这次全量替换写图里丢掉）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        mgr = TaskManager(base_dir=str(tmp_path))
        task = _make_task()
        _persist_table_context(task, [
            {"type": "text", "text": "正文", "_mapped_tables": [_mapped_table()]},
        ])
        merge = mock.AsyncMock(return_value=True)
        _, merge = await self._run_ontology_only(mgr, task, merge=merge)
        assert merge.await_count == 1
        # 传下去的表格条目与首轮一致（含 table_idx，可溯源）
        ctx = merge.await_args_list[0].args[0]
        assert ctx.content_list[0]["table_idx"] == 3

class TestTableContextSweep:
    """契约：TTL 回收存档（文档删除链路无钩子，防磁盘泄漏）。"""

    def _write_aged_archive(self, tmp_path, monkeypatch, task, age_sec):
        monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
        _persist_table_context(task, [{"type": "table", "table_body": "| a |"}])
        path = _table_context_path(task)
        old = time.time() - age_sec
        os.utime(path, (old, old))
        # 节流时间戳是模块级全局，重置后本次扫描才会真的执行
        monkeypatch.setattr("jonex_core.capability.atomic.ontology_compile.table_context._last_table_ctx_sweep", 0.0)
        return path

    def test_expired_archive_is_removed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAG_TABLE_CTX_TTL_SEC", "3600")
        path = self._write_aged_archive(tmp_path, monkeypatch, _make_task(), 7200)
        _sweep_table_context()
        assert not os.path.exists(path)

    def test_fresh_archive_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAG_TABLE_CTX_TTL_SEC", "3600")
        path = self._write_aged_archive(tmp_path, monkeypatch, _make_task(), 60)
        _sweep_table_context()
        assert os.path.exists(path)
        assert _load_table_context(_make_task()) is not None

    def test_non_positive_ttl_disables_sweep(self, tmp_path, monkeypatch):
        """TTL<=0 = 显式关停清扫（存档是重抽的唯一凭据，宁可不删）。"""
        monkeypatch.setenv("RAG_TABLE_CTX_TTL_SEC", "0")
        path = self._write_aged_archive(tmp_path, monkeypatch, _make_task(), 10 ** 8)
        _sweep_table_context()
        assert os.path.exists(path)
