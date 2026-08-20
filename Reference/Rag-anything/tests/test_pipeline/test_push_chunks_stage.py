"""Unit tests for PushChunksStage."""

import asyncio
import re
import pytest
from unittest import mock

from raganything.pipeline.base import PipelineContext, PipelineServices, StageResult
from raganything.pipeline.stages import PushChunksStage, _build_file_source


@pytest.fixture(autouse=True)
def _disable_text_block_packing(monkeypatch):
    """[jonex] §block-packing: 既有测试锁定「开关关闭」的逐块 1:1 旧路径
    （验收标准 1：RAG_TEXT_BLOCK_PACKING=false 时产出与改造前逐字节一致）。
    打包开启路径由 TestTextBlockPacking 类显式覆盖（改动 6b）。"""
    monkeypatch.setenv("RAG_TEXT_BLOCK_PACKING", "false")


class TestBuildFileSource:
    def test_basic_text_chunk(self):
        fs = _build_file_source("t1", "kb1", "doc-123", "report.pdf", chunk_index=0,
                                page=1, line_start=10, line_end=20)
        assert fs == "kb=kb1|doc=doc-123|tenant=t1|file=report.pdf|chunk=0|cstart=10|cend=20|page=1|trace="

    def test_table_chunk(self):
        fs = _build_file_source("t1", "kb1", "doc-123", "data.xlsx", chunk_index=5,
                                page=2, table_idx=1)
        assert "table_idx=1" in fs
        assert "chunk=5" in fs
        assert "tenant=t1" in fs
        assert fs.endswith("|trace=")

    def test_image_chunk(self):
        fs = _build_file_source("t1", "kb1", "doc-123", "slides.pdf", chunk_index=2,
                                page=3, image_idx=0)
        assert "image_idx=0" in fs
        assert "page=3" in fs
        assert "tenant=t1" in fs
        # [jonex] §image-refs P1-3: 未传 asset_ext → 不写 aext= 键（存量行为不变）
        assert "aext=" not in fs

    def test_image_chunk_with_asset_ext(self):
        """[jonex] §image-refs P1-3: 上传成功才传 asset_ext → 写 aext=。"""
        fs = _build_file_source("t1", "kb1", "doc-123", "slides.pdf", chunk_index=2,
                                page=3, image_idx=0, asset_ext="png")
        assert "image_idx=0" in fs
        assert "aext=png" in fs
        # aext 位于 trace 之前，不破坏分隔结构
        assert fs.index("aext=png") < fs.index("|trace=")

    def test_audio_chunk(self):
        fs = _build_file_source("t1", "kb1", "doc-123", "recording.mp3", chunk_index=1,
                                start_time=10.5, end_time=25.3)
        assert "tstart=10.500" in fs
        assert "tend=25.300" in fs

    def test_no_optional_fields(self):
        fs = _build_file_source("t1", "kb1", "doc-123", "file.txt", chunk_index=0)
        assert fs == "kb=kb1|doc=doc-123|tenant=t1|file=file.txt|chunk=0|trace="

    def test_long_table_cols_truncated_to_filename_limit(self):
        """§25 修复：table_cols/notes 膨胀使 file_source 超过 255 字节文件名
        上限 → LightRAG 删除文档 [Errno 36] File name too long → reparse 收敛
        失败。构建后总长必须 ≤ 240 字符。"""
        long_cols = "|".join(f"列名{i}很长很长的表头名称" for i in range(20))
        long_notes = "说明文字" * 40
        fs = _build_file_source(
            "t1", "kb1", "doc-123", "f.pdf", chunk_index=0,
            page=1, row_start=0, row_end=2, table_idx=0,
            ctype="table_row", table_sig="a1b2c3d4",
            table_cols=long_cols, notes=long_notes,
        )
        assert len(fs) <= 240
        # 截断仍保留签名与类型（检索侧对齐键不丢）
        assert "table_sig=a1b2c3d4" in fs
        assert "ctype=table_row" in fs
        assert fs.endswith("…|trace=") or fs.endswith("|trace=")

    def test_short_metadata_not_truncated(self):
        fs = _build_file_source(
            "t1", "kb1", "doc-123", "f.pdf", chunk_index=0,
            ctype="table_row", table_sig="a1b2c3d4",
            table_cols="品种|交易所|手续费", notes="注意",
        )
        assert "table_cols=品种 交易所 手续费" in fs
        assert "notes=注意" in fs
        assert len(fs) <= 240

    def test_entity_hint_written(self):
        """[jonex] 改动 20：ehint= 主体实体提示（表标题 heading）。"""
        fs = _build_file_source(
            "t1", "kb1", "doc-123", "f.pdf", chunk_index=0,
            ctype="table_row", table_sig="a1b2c3d4",
            entity_hint="代理手续费表",
        )
        assert "ehint=代理手续费表" in fs
        assert len(fs) <= 240

    def test_entity_hint_pipe_escaped_and_truncated(self):
        """ehint 值内的 | 转义为空格；超长截断。"""
        fs = _build_file_source(
            "t1", "kb1", "doc-123", "f.pdf", chunk_index=0,
            entity_hint="标题 | 子标题",
        )
        assert "ehint=标题   子标题" in fs
        assert "|" not in fs.split("ehint=")[1].split("|trace=")[0]
        fs2 = _build_file_source(
            "t1", "kb1", "doc-123", "f.pdf", chunk_index=0,
            entity_hint="长" * 100,
        )
        assert len(fs2) <= 240
        assert "…" in fs2

    def test_no_entity_hint_key_when_absent(self):
        """未传 entity_hint → 不写 ehint 键（存量行为不变）。"""
        fs = _build_file_source(
            "t1", "kb1", "doc-123", "f.pdf", chunk_index=0,
            ctype="table_row",
        )
        assert "ehint=" not in fs


class TestPushChunksStage:
    @pytest.fixture
    def mock_http_client(self):
        client = mock.AsyncMock()
        # Default: upload_text succeeds synchronously
        client.upload_text.return_value = mock.MagicMock(
            track_id="trk-0", status="success", doc_ids=[]
        )
        # Default: batch_track_status returns all completed
        async def _batch(ids, **kw):
            terminal = {
                tid: mock.MagicMock(state="completed", doc_ids=[f"chunk-{i}"])
                for i, tid in enumerate(ids)
            }
            return terminal, {}
        client.batch_track_status.side_effect = _batch
        return client

    @pytest.fixture
    def ctx(self):
        return PipelineContext(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            tenant_id="t1",
            kb_id="kb1",
            doc_id="doc-123",
            content_list=[
                {"type": "text", "text": "Hello world", "page_idx": 1, "line_start": 0, "line_end": 1},
                {"type": "text", "text": "Second paragraph", "page_idx": 1, "line_start": 2, "line_end": 3},
            ],
        )

    @pytest.fixture
    def services(self, mock_http_client):
        return PipelineServices(
            config=mock.MagicMock(),
            lightrag=None,
            doc_parser=mock.MagicMock(),
            http_client=mock_http_client,
            logger=mock.MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_pushes_text_chunks(self, ctx, services):
        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        assert result.error is None
        assert services.http_client.upload_text.call_count == 2

    @pytest.mark.asyncio
    async def test_collects_doc_ids(self, ctx, services):
        # Setup: each upload returns a unique track_id
        track_counter = [0]

        async def _upload(text, file_source, *, tenant_id, kb_id):
            tid = f"trk-{track_counter[0]}"
            track_counter[0] += 1
            return mock.MagicMock(track_id=tid, status="success", doc_ids=[])

        services.http_client.upload_text.side_effect = _upload

        async def _batch(ids, **kw):
            terminal = {
                tid: mock.MagicMock(state="completed", doc_ids=[f"chunk-{i}"])
                for i, tid in enumerate(ids)
            }
            return terminal, {}
        services.http_client.batch_track_status.side_effect = _batch

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        assert len(ctx.collected_doc_ids) == 2

    @pytest.mark.asyncio
    async def test_empty_content_returns_early(self, services):
        ctx = PipelineContext(
            file_path="/tmp/empty.pdf",
            file_name="empty.pdf",
            tenant_id="t1",
            kb_id="kb1",
            content_list=[],
        )
        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        assert result.error is None
        services.http_client.upload_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_requires_http_client(self, ctx):
        services = PipelineServices(
            config=mock.MagicMock(),
            lightrag=None,
            doc_parser=mock.MagicMock(),
            http_client=None,
            logger=mock.MagicMock(),
        )
        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        assert result.error is not None
        assert "requires http_client" in result.error

    @pytest.mark.asyncio
    async def test_cancellation_stops_push(self, ctx, services):
        cancel_event = asyncio.Event()
        cancel_event.set()  # already cancelled
        ctx.cancel_event = cancel_event

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        # No chunks should be pushed since cancel was already set
        services.http_client.upload_text.assert_not_called()
        assert "cancelled" in (result.error or "").lower() or result.error is not None

    @pytest.mark.asyncio
    async def test_upload_failure_single_chunk(self, ctx, services):
        # Make upload fail for the second chunk
        call_count = [0]

        async def _upload(text, file_source, *, tenant_id, kb_id):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Connection refused")
            return mock.MagicMock(track_id=f"trk-{call_count[0]}", status="success", doc_ids=[])

        services.http_client.upload_text.side_effect = _upload

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        # Should still succeed (1/2 succeeded)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_all_uploads_fail(self, ctx, services):
        services.http_client.upload_text.side_effect = Exception("All failed")

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        # All failed — should report error
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_track_status_timeout(self, ctx, services):
        # All tracks stay pending after timeout
        async def _batch(ids, **kw):
            return {}, {tid: mock.MagicMock(state="processing") for tid in ids}
        services.http_client.batch_track_status.side_effect = _batch

        # Short timeout for test
        stage = PushChunksStage()
        stage._track_timeout = 0.1

        result = await stage.execute(ctx, services)
        # All timed out, no doc_ids collected — should fail
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_collects_multimodal_chunks(self, ctx, services):
        ctx.multimodal_results = [
            {
                "description": "A chart showing revenue growth",
                "content_type": "image",
                "item_info": {"page_idx": 1},
                "index": 0,
            }
        ]

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        # Text (2) + multimodal (1) = 3 chunks
        assert services.http_client.upload_text.call_count == 3


# ── [jonex] #6: strict doc_id/track_id confirmation ────────────────


class TestPushChunksStrictConfirmation:
    """[jonex] #6: RAG_REQUIRE_DOC_IDS strict mode tests."""

    @pytest.fixture
    def ctx(self):
        return PipelineContext(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            tenant_id="t1",
            kb_id="kb1",
            document_id="doc-123",
            content_list=[
                {"type": "text", "text": "Chunk A", "page_idx": 1},
                {"type": "text", "text": "Chunk B", "page_idx": 2},
                {"type": "text", "text": "Chunk C", "page_idx": 3},
            ],
        )

    @pytest.fixture
    def services(self):
        client = mock.AsyncMock()
        client.upload_text.return_value = mock.MagicMock(
            track_id="trk-0", status="success", doc_ids=[]
        )
        return PipelineServices(
            config=mock.MagicMock(),
            lightrag=None,
            doc_parser=mock.MagicMock(),
            http_client=client,
            logger=mock.MagicMock(),
        )

    def _setup_upload_with_track_ids(self, services):
        """Each chunk gets a unique track_id."""
        counter = [0]

        async def _upload(text, file_source, *, tenant_id, kb_id):
            tid = f"trk-{counter[0]}"
            counter[0] += 1
            return mock.MagicMock(track_id=tid, status="success", doc_ids=[])

        services.http_client.upload_text.side_effect = _upload

    @pytest.mark.asyncio
    async def test_all_completed_success(self, ctx, services):
        """#6: all track_status completed → SUCCESS."""
        self._setup_upload_with_track_ids(services)

        async def _batch(ids, **kw):
            terminal = {
                tid: mock.MagicMock(state="completed", doc_ids=[f"doc-{i}"])
                for i, tid in enumerate(ids)
            }
            return terminal, {}

        services.http_client.batch_track_status.side_effect = _batch

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        assert result.error is None
        assert ctx.total_chunk_count == 3
        assert ctx.failed_chunk_count == 0
        assert ctx.timeout_chunk_count == 0
        assert len(ctx.collected_doc_ids) == 3

    @pytest.mark.asyncio
    async def test_partial_hard_failure_strict_mode(self, ctx, services):
        """#6: RAG_REQUIRE_DOC_IDS=true, 1 terminal failed → hard failure error."""
        self._setup_upload_with_track_ids(services)

        async def _batch(ids, **kw):
            terminal = {}
            for i, tid in enumerate(ids):
                if i == 1:  # second chunk failed
                    terminal[tid] = mock.MagicMock(
                        state="failed", doc_ids=[], error="parse error"
                    )
                else:
                    terminal[tid] = mock.MagicMock(
                        state="completed", doc_ids=[f"doc-{i}"]
                    )
            return terminal, {}

        services.http_client.batch_track_status.side_effect = _batch

        stage = PushChunksStage()
        # default: _require_doc_ids=True
        result = await stage.execute(ctx, services)

        assert result.error is not None
        assert "入库部分失败" in result.error
        assert "1/3" in result.error
        assert ctx.failed_chunk_count == 1
        # collected_doc_ids still has the successful ones
        assert len(ctx.collected_doc_ids) == 2

    @pytest.mark.asyncio
    async def test_timed_out_strict_mode(self, ctx, services):
        """#6: RAG_REQUIRE_DOC_IDS=true, chunk pending after timeout → TIMEOUT error."""
        self._setup_upload_with_track_ids(services)

        async def _batch(ids, **kw):
            # 2 completed, 1 still pending
            terminal = {
                ids[0]: mock.MagicMock(state="completed", doc_ids=["doc-0"]),
                ids[1]: mock.MagicMock(state="completed", doc_ids=["doc-1"]),
            }
            still_pending = {ids[2]: mock.MagicMock(state="processing")}
            return terminal, still_pending

        services.http_client.batch_track_status.side_effect = _batch

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        assert result.error is not None
        assert "RAG_PUSH_TIMEOUT" in result.error
        assert "1/3" in result.error
        assert ctx.timeout_chunk_count == 1
        assert ctx.failed_chunk_count == 0
        assert len(ctx.collected_doc_ids) == 2
        assert len(ctx.pending_track_ids) == 1

    @pytest.mark.asyncio
    async def test_relaxed_mode_partial_failure_succeeds(self, ctx, services):
        """#6: RAG_REQUIRE_DOC_IDS=false, partial failure → preserved宽松semantics."""
        self._setup_upload_with_track_ids(services)

        async def _batch(ids, **kw):
            terminal = {
                ids[0]: mock.MagicMock(state="completed", doc_ids=["doc-0"]),
                ids[1]: mock.MagicMock(state="failed", doc_ids=[], error="err"),
                ids[2]: mock.MagicMock(state="completed", doc_ids=["doc-2"]),
            }
            return terminal, {}

        services.http_client.batch_track_status.side_effect = _batch

        stage = PushChunksStage()
        stage._require_doc_ids = False  # relaxed mode
        result = await stage.execute(ctx, services)

        # Relaxed mode: 2/3 succeeded → SUCCESS
        assert result.error is None
        assert ctx.failed_chunk_count == 1

    @pytest.mark.asyncio
    async def test_relaxed_mode_all_timed_out_no_doc_ids(self, ctx, services):
        """#6: even in relaxed mode, all timed out with no doc_ids → failure."""
        self._setup_upload_with_track_ids(services)

        async def _batch(ids, **kw):
            still_pending = {tid: mock.MagicMock(state="processing") for tid in ids}
            return {}, still_pending

        services.http_client.batch_track_status.side_effect = _batch

        stage = PushChunksStage()
        stage._require_doc_ids = False
        result = await stage.execute(ctx, services)

        # All timed out, no doc_ids
        assert result.error is not None
        assert "All 3 chunks failed" in result.error

    @pytest.mark.asyncio
    async def test_push_failure_in_failed_chunk_count(self, ctx, services):
        """#6: push-level exception counts as hard_failed."""
        failed_chunk_idx = 2  # the 3rd chunk

        async def _upload(text, file_source, *, tenant_id, kb_id):
            # Use the chunk index embedded in file_source
            fs = file_source
            # chunk=N in file_source tells us which chunk
            import re
            m = re.search(r"chunk=(\d+)", fs)
            idx = int(m.group(1)) if m else -1
            if idx == failed_chunk_idx:
                raise Exception("Connection refused")
            return mock.MagicMock(
                track_id=f"trk-{idx}", status="success", doc_ids=[]
            )

        services.http_client.upload_text.side_effect = _upload

        async def _batch(ids, **kw):
            terminal = {
                tid: mock.MagicMock(state="completed", doc_ids=[f"doc-{i}"])
                for i, tid in enumerate(ids)
            }
            return terminal, {}

        services.http_client.batch_track_status.side_effect = _batch

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        # Strict mode: 1 push failure → hard failure
        assert result.error is not None
        assert "入库部分失败" in result.error
        assert ctx.failed_chunk_count == 1
        assert ctx.total_pushed_count == 2  # only 2 got track_ids

    @pytest.mark.asyncio
    async def test_context_counters_persisted(self, ctx, services):
        """#6: ctx counters are set correctly for all outcomes."""
        self._setup_upload_with_track_ids(services)

        async def _batch(ids, **kw):
            terminal = {
                ids[0]: mock.MagicMock(state="completed", doc_ids=["doc-0"]),
                ids[1]: mock.MagicMock(state="completed", doc_ids=["doc-1"]),
            }
            still_pending = {ids[2]: mock.MagicMock(state="processing")}
            return terminal, still_pending

        services.http_client.batch_track_status.side_effect = _batch

        stage = PushChunksStage()
        await stage.execute(ctx, services)

        assert ctx.total_chunk_count == 3
        assert ctx.failed_chunk_count == 0  # no hard failures
        assert ctx.timeout_chunk_count == 1  # one timed out
        assert ctx.total_pushed_count == 3  # all 3 got track_ids


# ── [jonex] #5: duplicated chunk tracking ───────────────────────────


class TestPushChunksDuplicatedTracking:
    """[jonex] #5: all-duplicated guard for ontology skip."""

    @pytest.fixture
    def ctx(self):
        return PipelineContext(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            tenant_id="t1",
            kb_id="kb1",
            document_id="doc-123",
            content_list=[
                {"type": "text", "text": "Chunk A", "page_idx": 1},
                {"type": "text", "text": "Chunk B", "page_idx": 2},
            ],
        )

    @pytest.fixture
    def services(self):
        client = mock.AsyncMock()
        return PipelineServices(
            config=mock.MagicMock(),
            lightrag=None,
            doc_parser=mock.MagicMock(),
            http_client=client,
            logger=mock.MagicMock(),
        )

    def _setup_uploads(self, services, statuses):
        """Setup upload_text to return given statuses in order."""
        counter = [0]

        async def _upload(text, file_source, *, tenant_id, kb_id):
            s = statuses[counter[0]]
            counter[0] += 1
            return mock.MagicMock(track_id=f"trk-{counter[0]}", status=s, doc_ids=[])

        services.http_client.upload_text.side_effect = _upload

        async def _batch(ids, **kw):
            terminal = {
                tid: mock.MagicMock(state="completed", doc_ids=[f"doc-{i}"])
                for i, tid in enumerate(ids)
            }
            return terminal, {}

        services.http_client.batch_track_status.side_effect = _batch

    @pytest.mark.asyncio
    async def test_all_duplicated_tracking(self, ctx, services):
        """#5: all chunks duplicated → duplicated_chunk_count == total_pushed_count."""
        self._setup_uploads(services, ["duplicated", "duplicated"])

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        assert result.error is None
        assert ctx.duplicated_chunk_count == 2
        assert ctx.total_pushed_count == 2

    @pytest.mark.asyncio
    async def test_partial_duplicated_tracking(self, ctx, services):
        """#5: 1 duplicated, 1 new → duplicated_chunk_count=1."""
        self._setup_uploads(services, ["duplicated", "success"])

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        assert result.error is None
        assert ctx.duplicated_chunk_count == 1
        assert ctx.total_pushed_count == 2

    @pytest.mark.asyncio
    async def test_none_duplicated_tracking(self, ctx, services):
        """#5: all new → duplicated_chunk_count=0."""
        self._setup_uploads(services, ["success", "success"])

        stage = PushChunksStage()
        result = await stage.execute(ctx, services)

        assert result.error is None
        assert ctx.duplicated_chunk_count == 0
        assert ctx.total_pushed_count == 2


# ── [jonex] §table-grid-v2 O1/O4: 表格统计与超限断言 ───────────────────────


class TestTableGridStats:
    """O4 table_stats 计数 + O1 表格专用预算/超限断言（容器内跑，依赖 LightRAG）。"""

    TABLE_HTML = (
        "<table>"
        "<tr><th>部门负责人</th><th>分管领导</th><th>备注</th></tr>"
        "<tr><td>A</td><td>B</td><td>C</td></tr>"
        "</table>"
    )

    def _stage(self, monkeypatch, tokenizer=None):
        stage = PushChunksStage()
        stage._table_tokenizer = tokenizer  # None → 断言跳过
        stage._lightrag_chunk_size = 10
        monkeypatch.setattr(stage, "_table_chunk_body_budget", 200)
        return stage

    def _collect(self, stage, content_list):
        chunks: list[dict] = []
        stats: dict = {}
        stage._collect_text_chunks(
            chunks, content_list,
            tenant_id="t1", kb_id="kb1", document_id="doc-1",
            file_name="f.pdf", stats=stats,
        )
        return chunks, stats

    def test_table_stats_counters(self, monkeypatch):
        stage = self._stage(monkeypatch)
        content = [
            {"type": "table", "table_body": self.TABLE_HTML, "page_idx": 0},
            # fallback：无法归一化的表格（归一化返回空）
            {"type": "table", "table_body": "普通文本", "page_idx": 1},
            {"type": "text", "text": "一段正文", "page_idx": 2},
        ]
        chunks, stats = self._collect(stage, content)
        assert stats["tables_total"] == 2
        assert stats["tables_normalized"] == 1
        assert stats["tables_fallback"] == 1
        assert stats["rows_total"] == 1
        assert stats["cols_unnamed"] == 0
        # 表格明细行 + fallback 文本各 1
        assert len(chunks) == 2
        assert chunks[0]["type"] == "table_row"
        assert chunks[1]["type"] == "table"  # fallback 走文本分支

    def test_table_stats_cols_unnamed(self, monkeypatch):
        stage = self._stage(monkeypatch)
        # 无表头的数字表 → 全 col_N 兜底
        html = (
            "<table>"
            "<tr><td>1.1</td><td>45</td><td>0.3</td></tr>"
            "<tr><td>x</td><td>y</td><td>z</td></tr>"
            "</table>"
        )
        chunks, stats = self._collect(stage, [{"type": "table", "table_body": html}])
        assert stats["cols_unnamed"] == 3
        assert stats["tables_normalized"] == 1

    def test_table_stats_header_levels(self, monkeypatch):
        """[jonex] §18.3 观测口径：header_levels 分布——单级表头计 1 档。"""
        stage = self._stage(monkeypatch)
        chunks, stats = self._collect(
            stage, [{"type": "table", "table_body": self.TABLE_HTML}],
        )
        assert stats["header_levels"] == {"1": 1}

    def test_table_stats_header_levels_zero_when_no_header(self, monkeypatch):
        """[jonex] §18.3：无表头（全 col_N）→ header_levels 0 档可观测。"""
        stage = self._stage(monkeypatch)
        html = (
            "<table>"
            "<tr><td>1.1</td><td>45</td><td>0.3</td></tr>"
            "<tr><td>x</td><td>y</td><td>z</td></tr>"
            "</table>"
        )
        chunks, stats = self._collect(stage, [{"type": "table", "table_body": html}])
        assert stats["header_levels"] == {"0": 1}

    def test_table_budget_segments(self, monkeypatch):
        """表格专用预算（非 RAG_CHUNK_MAX_CHARS）决定 segment 切分。"""
        stage = self._stage(monkeypatch)
        monkeypatch.setattr(stage, "_table_chunk_body_budget", 100)
        html = (
            "<table>"
            "<tr><th>列一</th><th>列二</th></tr>"
            + "".join(
                f"<tr><td>值{i}号很长很长的内容占预算</td><td>{i}</td></tr>"
                for i in range(10)
            )
            + "</table>"
        )
        chunks, stats = self._collect(stage, [{"type": "table", "table_body": html}])
        assert stats["rows_total"] == 10
        assert len(chunks) > 1  # 小预算 → 多 segment
        for c in chunks:
            assert c["type"] == "table_row"
            assert "row_start=" in c["file_source"]

    def test_oversize_assertion_counts(self, monkeypatch):
        """tokenizer 实测超阈值 → oversize_table_chunks 计数（不阻断推送）。"""
        class _FakeTok:
            def encode(self, text):
                return list(range(25))  # 25 tokens > lightrag_chunk_size=10
        stage = self._stage(monkeypatch, tokenizer=_FakeTok())
        chunks, stats = self._collect(
            stage, [{"type": "table", "table_body": self.TABLE_HTML}],
        )
        assert stats["oversize_table_chunks"] == 1
        assert len(chunks) == 1  # 计数不影响 chunk 产出

    def test_oversize_within_limit_not_counted(self, monkeypatch):
        class _FakeTok:
            def encode(self, text):
                return list(range(3))  # 3 tokens ≤ 10
        stage = self._stage(monkeypatch, tokenizer=_FakeTok())
        chunks, stats = self._collect(
            stage, [{"type": "table", "table_body": self.TABLE_HTML}],
        )
        assert stats.get("oversize_table_chunks", 0) == 0

    def test_o2_embedded_table_in_text_block(self, monkeypatch):
        """O2：md text block 内嵌 HTML 表格 → 表格走 table_row，前后文本独立 chunk。"""
        stage = self._stage(monkeypatch)
        text = (
            "营业部名单如下：\n"
            "<table><tr><th>营业部</th><th>联系人</th></tr>"
            "<tr><td>武汉分公司</td><td>顾雅文</td></tr></table>\n"
            "以上为全部名单。"
        )
        chunks, stats = self._collect(
            stage, [{"type": "text", "text": text, "page_idx": 3}],
        )
        types = [c["type"] for c in chunks]
        assert "table_row" in types  # 内嵌表走表格分支
        assert types.count("text") == 2  # 前后文本各成 chunk
        assert stats["tables_total"] == 1
        assert stats["tables_normalized"] == 1
        assert stats["tables_fallback"] == 0
        table_chunk = next(c for c in chunks if c["type"] == "table_row")
        assert "row_start=0" in table_chunk["file_source"]
        assert "ctype=table_row" in table_chunk["file_source"]
        # 表格片段不再以裸 HTML 形式混入文本 chunk
        for c in chunks:
            if c["type"] == "text":
                assert "<table" not in c["text"]

    def test_o2_markdown_pipe_table_in_text_block(self, monkeypatch):
        """O2：连续 ≥3 管道行 → 走表格分支（10 个解析_md_*.md 的形态）。"""
        stage = self._stage(monkeypatch)
        text = (
            "收入明细\n"
            "| 年份 | 收入 |\n"
            "| --- | --- |\n"
            "| 2023 | 100 |\n"
            "| 2024 | 120 |\n"
        )
        chunks, stats = self._collect(
            stage, [{"type": "text", "text": text, "page_idx": 1}],
        )
        assert stats["tables_normalized"] == 1
        assert any(c["type"] == "table_row" for c in chunks)
        # 表头推断识别年份序列
        table_chunk = next(c for c in chunks if c["type"] == "table_row")
        assert "2023" in table_chunk["text"]

    def test_l41_caption_and_metadata_in_file_source(self, monkeypatch):
        """L4.1：上下文头进正文、列名签名/清单/notes 进 file_source。"""
        stage = self._stage(monkeypatch)
        prose = "本表适用于总部各部室。" + "详情见制度。" * 20
        html = (
            "<table>"
            f"<tr><td>{prose}</td><td></td><td></td></tr>"
            "<tr><td>部门负责人</td><td>分管领导</td><td>备注</td></tr>"
            "<tr><td>A</td><td>B</td><td>C</td></tr>"
            "</table>"
        )
        content = [
            {"type": "text", "text": "审批授权情况", "page_idx": 0},
            {"type": "table", "table_body": html, "page_idx": 0,
             "table_caption": [], "table_idx": 0},
        ]
        chunks, stats = self._collect(stage, content)
        table_chunk = next(c for c in chunks if c["type"] == "table_row")
        # 标题来源：prev_block（table_caption 无效）
        assert table_chunk["text"].startswith("表格明细：【表】审批授权情况\n")
        # file_source 旁路：签名/列名清单/notes
        assert "table_sig=" in table_chunk["file_source"]
        assert "table_cols=" in table_chunk["file_source"]
        assert "notes=" in table_chunk["file_source"]
        assert "本表适用" in table_chunk["file_source"]  # notes 含说明文字
        # 正文不含 notes（不进 embedding）
        assert "本表适用" not in table_chunk["text"].split("表格明细：", 1)[1]
        # caption_source 统计：prev_block
        assert stats["caption_source"].get("prev_block") == 1


class TestResolveTableCaption:
    """L4.1 表标题来源链（4 级优先级 + 防误抓约束）。"""

    def _resolve(self, content_list, idx, file_name="f.pdf", sheet_hint=None):
        from raganything.pipeline.stages import _resolve_table_caption

        item = content_list[idx]
        return _resolve_table_caption(item, content_list, idx, file_name, sheet_hint)

    def test_priority_1_valid_caption(self):
        cl = [
            {"type": "table", "table_caption": ["招商期货分支机构"], "table_body": "x"},
        ]
        cap, src = self._resolve(cl, 0)
        assert cap == "招商期货分支机构"
        assert src == "caption"

    def test_invalid_caption_falls_through(self):
        """字面量 "None" / 空 caption → 不回填【表】None，继续下一级。"""
        for bad in (["None"], [""], ["none"], "N/A", []):
            cl = [{"type": "table", "table_caption": bad, "table_body": "x"}]
            cap, src = self._resolve(cl, 0, file_name="兜底.pdf")
            assert cap == "兜底.pdf", f"bad={bad!r}"
            assert src == "filename"

    def test_priority_2_prev_block(self):
        cl = [
            {"type": "text", "text": "招商期货分支机构"},
            {"type": "table", "table_caption": [], "table_body": "x"},
        ]
        cap, src = self._resolve(cl, 1)
        assert cap == "招商期货分支机构"
        assert src == "prev_block"

    def test_prev_block_sentence_end_rejected(self):
        """L5 用例 11：表前是句号结尾的正文段落 → 不采用，回落文件名。"""
        cl = [
            {"type": "text", "text": "以下是各分支机构的情况说明。"},
            {"type": "table", "table_caption": [], "table_body": "x"},
        ]
        cap, src = self._resolve(cl, 1, file_name="报告.pdf")
        assert cap == "报告.pdf"
        assert src == "filename"

    def test_prev_block_too_far_rejected(self):
        """距离 > 3 block → 不采用。"""
        cl = [
            {"type": "text", "text": "标题"},
            {"type": "text", "text": "a"},
            {"type": "image", "img_path": "i.png"},
            {"type": "text", "text": "b"},
            {"type": "table", "table_caption": [], "table_body": "x"},
        ]
        cap, src = self._resolve(cl, 4, file_name="f.pdf")
        assert cap == "f.pdf"
        assert src == "filename"

    def test_prev_block_too_long_rejected(self):
        cl = [
            {"type": "text", "text": "长" * 61},
            {"type": "table", "table_caption": [], "table_body": "x"},
        ]
        cap, src = self._resolve(cl, 1, file_name="f.pdf")
        assert cap == "f.pdf"

    def test_prev_block_with_newline_rejected(self):
        cl = [
            {"type": "text", "text": "第一行\n第二行"},
            {"type": "table", "table_caption": [], "table_body": "x"},
        ]
        cap, src = self._resolve(cl, 1, file_name="f.pdf")
        assert cap == "f.pdf"


class TestTextBlockPacking:
    """[jonex] §block-packing 改动 6b：打包开启路径行为验证。

    开关关闭（RAG_TEXT_BLOCK_PACKING=false）的逐块 1:1 旧路径由本文件顶部
    autouse fixture 锁定（既有用例全部走旧路径，验收标准 1）；本类显式
    开启打包，覆盖方案 §4 改动 6b 的验收点：on/off 双路对照、硬边界、
    caption 预解析、pspans 写入 file_source、240 字符防御。
    """

    TABLE_HTML = (
        "<table>"
        "<tr><th>部门负责人</th><th>分管领导</th><th>备注</th></tr>"
        "<tr><td>A</td><td>B</td><td>C</td></tr>"
        "</table>"
    )
    NS_TOKEN_RE = re.compile(r"\s*<!--yx:[0-9a-f]+-->\s*")

    @pytest.fixture
    def stage_on(self, monkeypatch):
        monkeypatch.setenv("RAG_TEXT_BLOCK_PACKING", "true")
        monkeypatch.setenv("RAG_TEXT_PACK_CHARS", "1260")
        monkeypatch.setenv("RAG_TEXT_PACK_MAX_TOKENS", "1200")
        monkeypatch.setenv("RAG_TEXT_PACK_HEADING_MAX_LEN", "40")
        monkeypatch.setenv("RAG_TEXT_PACK_DROP_NOISE", "true")
        return PushChunksStage()

    def _collect(self, stage, content_list, file_name="test.pdf"):
        chunks: list[dict] = []
        stats: dict = {}
        stage._collect_text_chunks(
            chunks, content_list,
            tenant_id="t1", kb_id="kb1", document_id="doc-123",
            file_name=file_name, stats=stats,
        )
        return chunks, stats

    def _clean(self, text: str) -> str:
        """去掉注入的命名空间隔离标记，还原打包正文。"""
        return self.NS_TOKEN_RE.sub("", text).strip()

    def test_pack_on_merges_same_page_blocks(self, stage_on):
        content = [
            {"type": "text", "text": "第一段正文内容。", "page_idx": 1},
            {"type": "text", "text": "第二段正文内容。", "page_idx": 1},
        ]
        chunks, stats = self._collect(stage_on, content)
        assert len(chunks) == 1
        assert self._clean(chunks[0]["text"]) == "第一段正文内容。\n第二段正文内容。"
        assert chunks[0]["type"] == "text"
        assert stats["blocks_total"] == 2
        assert stats["packs_total"] == 1
        assert stats["blocks_packed"] == 2

    def test_pack_off_one_to_one_contrast(self, monkeypatch):
        """双路对照：开关关闭时同输入走逐块 1:1（与顶部 autouse fixture 同语义）。"""
        monkeypatch.setenv("RAG_TEXT_BLOCK_PACKING", "false")
        stage = PushChunksStage()
        content = [
            {"type": "text", "text": "第一段正文内容。", "page_idx": 1},
            {"type": "text", "text": "第二段正文内容。", "page_idx": 1},
        ]
        chunks, _ = self._collect(stage, content)
        assert len(chunks) == 2
        assert self._clean(chunks[0]["text"]) == "第一段正文内容。"
        assert self._clean(chunks[1]["text"]) == "第二段正文内容。"

    def test_table_hard_boundary_flushes_pending(self, stage_on):
        """C1 硬边界：表格两侧的文本不跨表合并成一个包。"""
        content = [
            {"type": "text", "text": "表格前的正文内容。", "page_idx": 1},
            {"type": "table", "table_body": self.TABLE_HTML, "page_idx": 1},
            {"type": "text", "text": "表格后的正文内容。", "page_idx": 1},
        ]
        chunks, stats = self._collect(stage_on, content)
        text_chunks = [c for c in chunks if c["type"] == "text"]
        assert len(text_chunks) == 2
        assert self._clean(text_chunks[0]["text"]) == "表格前的正文内容。"
        assert self._clean(text_chunks[1]["text"]) == "表格后的正文内容。"
        assert any(c["type"] == "table_row" for c in chunks)
        assert stats["tables_total"] == 1

    def test_image_hard_boundary_breaks_pack(self, stage_on):
        """C1 硬边界：image 不产出 chunk 但打断相邻文本打包（跨页也不合并）。"""
        content = [
            {"type": "text", "text": "图像前的正文内容。", "page_idx": 1},
            {"type": "image", "img_path": "i.png", "page_idx": 1},
            {"type": "text", "text": "图像后的正文内容。", "page_idx": 2},
        ]
        chunks, _ = self._collect(stage_on, content)
        assert len(chunks) == 2
        assert self._clean(chunks[0]["text"]) == "图像前的正文内容。"
        assert self._clean(chunks[1]["text"]) == "图像后的正文内容。"

    def test_caption_prefilled_before_packing(self, stage_on):
        """阶段 A：caption 预解析在打包/噪声丢弃前完成，prev_block 判据
        在原始块序上命中（方案 §5.1 阻塞项的验收）。"""
        content = [
            {"type": "text", "text": "代理手续费表", "page_idx": 1},
            {"type": "table", "table_caption": [], "table_body": self.TABLE_HTML, "page_idx": 1},
        ]
        _, stats = self._collect(stage_on, content)
        assert content[1]["_resolved_caption"] == "代理手续费表"
        assert content[1]["_caption_source"] == "prev_block"
        assert stats["caption_source"]["prev_block"] == 1

    def test_cross_page_pspans_written_to_file_source(self, stage_on):
        """2d：跨页包 file_source 写 page_end 与 pspans 页边界表。
        包头「【第一节 折衷中西】\\n」= 1 + 8（7 汉字 + 1 空格） + 1 + 1
        = 11 字符 → 正文起点 11@2。"""
        content = [
            {"type": "text", "text": "第一节 折衷中西", "page_idx": 1},
            {"type": "text", "text": "第二页的正文内容。", "page_idx": 2},
        ]
        chunks, stats = self._collect(stage_on, content)
        assert len(chunks) == 1
        fs = chunks[0]["file_source"]
        assert "page=1" in fs
        assert "page_end=2" in fs
        assert "pspans=0@1;11@2" in fs
        assert stats["packs_cross_page"] == 1
        assert self._clean(chunks[0]["text"]).startswith("【第一节 折衷中西】\n")

    def test_pspans_dropped_when_source_over_limit(self, stage_on):
        """2d 240 防御：file_source 总长超限时丢弃 pspans 段并计数，
        降级到 page/page_end 粗锚点（长文件名把 base 段推到超限）。"""
        long_name = "很长的文件名" * 25  # 150 字符：base+pspans 超 240，丢 pspans 后回到限内
        content = [
            {"type": "text", "text": "第一节 折衷中西", "page_idx": 1},
            {"type": "text", "text": "第二页的正文内容。", "page_idx": 2},
        ]
        chunks, stats = self._collect(stage_on, content, file_name=long_name)
        assert len(chunks) == 1
        fs = chunks[0]["file_source"]
        assert len(fs) <= 240
        assert "pspans=" not in fs
        assert "page=1" in fs and "page_end=2" in fs
        assert stats["pspans_truncated"] == 1

    def test_noise_blocks_dropped_by_default(self, stage_on):
        """C2：纯数字页码噪声按默认 drop_noise=true 丢弃，不进包。"""
        content = [
            {"type": "text", "text": "1", "page_idx": 1},
            {"type": "text", "text": "正文内容在这里。", "page_idx": 1},
            {"type": "text", "text": "2", "page_idx": 2},
        ]
        chunks, stats = self._collect(stage_on, content)
        assert len(chunks) == 1
        assert self._clean(chunks[0]["text"]) == "正文内容在这里。"
        assert stats["blocks_dropped_noise"] == 2

    def test_heading_inherits_across_table_boundary(self, stage_on):
        """review P1/P0-2 端到端：标题跨表格硬边界继承为包头，且表格后
        文本包的页码取首个正文块页（修复前被拉回标题页 1）。"""
        content = [
            {"type": "text", "text": "第一节 折衷中西", "page_idx": 1},
            {"type": "text", "text": "表格前的正文内容。", "page_idx": 1},
            {"type": "table", "table_body": self.TABLE_HTML, "page_idx": 1},
            {"type": "text", "text": "表格后的正文内容。", "page_idx": 3},
        ]
        chunks, _ = self._collect(stage_on, content)
        text_chunks = [c for c in chunks if c["type"] == "text"]
        assert len(text_chunks) == 2
        # 包 1：新收标题 → 标题页 1
        assert self._clean(text_chunks[0]["text"]) == (
            "【第一节 折衷中西】\n表格前的正文内容。"
        )
        assert "page=1" in text_chunks[0]["file_source"]
        # 包 2：标题跨表格继承，page=3（首个正文块页），pspans 无（同页）
        fs2 = text_chunks[1]["file_source"]
        assert self._clean(text_chunks[1]["text"]) == (
            "【第一节 折衷中西】\n表格后的正文内容。"
        )
        assert "page=3" in fs2
        assert "page=1" not in fs2
        assert "pspans=" not in fs2

    def test_noise_drop_sampling_warning(self, stage_on, caplog):
        """review P1：噪声丢弃落地采样 WARNING——每文档前 20 块原文+判据
        （§3.2 灰度核对门槛）。"""
        import logging

        content = [
            {"type": "text", "text": "1", "page_idx": 1},
            {"type": "text", "text": "正文内容在这里。", "page_idx": 1},
        ]
        with caplog.at_level(logging.WARNING, logger="raganything.pipeline.stages"):
            self._collect(stage_on, content)
        records = [r for r in caplog.records if "noise block" in (r.message or "")]
        assert len(records) == 1
        assert "('1', 'noise')" in records[0].message

    def test_stage_a_skipped_when_pack_off(self, monkeypatch):
        """review 低优先：打包关闭时阶段 A 不执行，content_list 不被写入
        _resolved_caption/_caption_source（表格分支现场解析结果一致）。"""
        monkeypatch.setenv("RAG_TEXT_BLOCK_PACKING", "false")
        stage = PushChunksStage()
        content = [
            {"type": "text", "text": "代理手续费表", "page_idx": 1},
            {"type": "table", "table_caption": [], "table_body": self.TABLE_HTML, "page_idx": 1},
        ]
        self._collect(stage, content)
        assert "_resolved_caption" not in content[1]
        assert "_caption_source" not in content[1]


# ── [jonex] §image-refs P1 测试：图片 chunk 的 aext 旁路与正文前缀 ─────────


class TestImageChunkFileSourceAext:
    """[jonex] §image-refs P1-3：ctx.asset_exts 有该 image_idx 才在
    file_source 写 aext=（上传失败的图片不写，检索侧据此判定无 URL 可取）；
    P0-5：图片描述正文带「图片描述：」前缀。"""

    @pytest.fixture
    def ctx(self):
        return PipelineContext(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            tenant_id="t1",
            kb_id="kb1",
            document_id="doc-123",
            content_list=[],
        )

    @pytest.fixture
    def services(self):
        client = mock.AsyncMock()
        client.upload_text.return_value = mock.MagicMock(
            track_id="trk-0", status="success", doc_ids=[]
        )

        async def _batch(ids, **kw):
            terminal = {
                tid: mock.MagicMock(state="completed", doc_ids=[f"chunk-{i}"])
                for i, tid in enumerate(ids)
            }
            return terminal, {}

        client.batch_track_status.side_effect = _batch
        return PipelineServices(
            config=mock.MagicMock(),
            lightrag=None,
            doc_parser=mock.MagicMock(),
            http_client=client,
            logger=mock.MagicMock(),
        )

    @staticmethod
    def _image_result(idx: int) -> dict:
        return {
            "description": "A chart showing revenue growth",
            "content_type": "image",
            "item_info": {"page_idx": 1},
            "index": idx,
        }

    def _uploaded(self, services):
        """[(text, file_source), ...] 按调用顺序。"""
        return [
            (c.args[0], c.args[1])
            for c in services.http_client.upload_text.call_args_list
        ]

    @pytest.mark.asyncio
    async def test_aext_written_when_uploaded(self, ctx, services):
        ctx.multimodal_results = [self._image_result(1)]
        ctx.asset_exts = {1: "png"}

        result = await PushChunksStage().execute(ctx, services)

        assert result.error is None
        uploaded = self._uploaded(services)
        assert len(uploaded) == 1
        fs = uploaded[0][1]
        assert "image_idx=1" in fs
        assert "aext=png" in fs

    @pytest.mark.asyncio
    async def test_aext_absent_when_not_uploaded(self, ctx, services):
        """上传失败 / 开关关闭 → ctx.asset_exts 无该 idx → 不写 aext=。"""
        ctx.multimodal_results = [self._image_result(1)]
        ctx.asset_exts = {}  # 默认空：无上传结果

        result = await PushChunksStage().execute(ctx, services)

        assert result.error is None
        uploaded = self._uploaded(services)
        assert len(uploaded) == 1
        fs = uploaded[0][1]
        assert "image_idx=1" in fs
        assert "aext=" not in fs

    @pytest.mark.asyncio
    async def test_partial_upload_only_matching_idx_gets_aext(self, ctx, services):
        """两张图只有 idx=3 上传成功 → idx=1 不写 aext，idx=3 写。"""
        ctx.multimodal_results = [self._image_result(1), self._image_result(3)]
        ctx.asset_exts = {3: "jpg"}

        result = await PushChunksStage().execute(ctx, services)

        assert result.error is None
        fs_by_idx = {}
        for _text, fs in self._uploaded(services):
            idx = int(fs.split("image_idx=")[1].split("|")[0])
            fs_by_idx[idx] = fs
        assert "aext=" not in fs_by_idx[1]
        assert "aext=jpg" in fs_by_idx[3]

    @pytest.mark.asyncio
    async def test_image_description_prefix(self, ctx, services):
        """P0-5：图片 chunk 正文带「图片描述：」前缀（嵌入空间与文本 chunk 区分）。"""
        ctx.multimodal_results = [self._image_result(1)]
        ctx.asset_exts = {1: "png"}

        result = await PushChunksStage().execute(ctx, services)

        assert result.error is None
        texts = [text for text, _fs in self._uploaded(services)]
        assert len(texts) == 1
        assert "图片描述：" in texts[0]
        assert texts[0].split("图片描述：", 1)[1].startswith("A chart showing")

    @pytest.mark.asyncio
    async def test_non_image_modalities_unaffected(self, ctx, services):
        """表格/公式等非 image 模态不写 image_idx/aext（既有行为不变）。"""
        ctx.multimodal_results = [
            {"description": "表格摘要", "content_type": "table",
             "item_info": {"page_idx": 1}, "index": 0},
            self._image_result(1),
        ]
        ctx.asset_exts = {1: "png"}

        result = await PushChunksStage().execute(ctx, services)

        assert result.error is None
        for text, fs in self._uploaded(services):
            if "table_summary" in fs:
                assert "image_idx=" not in fs
                assert "aext=" not in fs
                assert text.split("表格概览：", 1)[1].startswith("表格摘要")
