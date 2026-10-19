"""[A18] 任务体前置段：灌引擎全量 chunk + 等终态 + 读回校验（spec §4.3）。"""
import html
import re
import threading
from unittest import mock

import pytest

from raganything.service import task_manager
from raganything.service.http_lightrag_client import TrackStatus, UploadResult
from raganything.service.models import TaskInfo
from raganything.service.task_manager import (
    TaskManager,
    _load_table_context,
)

PUSHED: list = []  # (text, file_source)，fake client 实例内用


def _make_task(**overrides) -> TaskInfo:
    base = dict(
        task_id="task-a18",
        tenant_id="t1",
        name="doc.md",
        file_path="/tmp/doc.md",
        kb_id="kb1",
        document_id="d1",
        ontology_schema={"entity_types": [], "relation_types": []},
    )
    base.update(overrides)
    return TaskInfo(**base)


def _make_handle():
    return mock.MagicMock(cancel_event=threading.Event())


def _fs(chunk: int, *, ctype="text", extra="") -> str:
    return f"kb=kb1|doc=d1|tenant=t1|chunk={chunk}|ctype={ctype}|{extra}"


def _chunks(*specs) -> list:
    """specs: (chunk, content, ctype[, extra_anchor])"""
    out = []
    for spec in specs:
        chunk, content = spec[0], spec[1]
        ctype = spec[2] if len(spec) > 2 else "text"
        extra = spec[3] if len(spec) > 3 else ""
        out.append({
            "chunk_index": chunk, "content": content, "file_source": _fs(chunk, ctype=ctype, extra=extra),
        })
    return out


TABLE_EXTRA = "table_sig=sig00001|table_idx=0|row_start=0|row_end=0|"
TABLE_CONTENT = "表格明细：【表】T\n| c1 | c2 |\n| A | B |"


# 列表端点读回剥除 ns marker（Reference/LightRAG/lightrag/api/routers/document_routes.py:3923-3928）
_NS_MARKER_RE = re.compile(r"\s*<!--yx:[0-9a-f]+-->\s*")


class _FakeHttpClient:
    """最小 fake：记录 push、构造读回、可控失败。"""

    def __init__(self, *, upload_fail_times: int = 0, upload_statuses=None,
                 readback_transform=None, batch_side_effect=None, readback_side_effect=None):
        self.pushed: list = []
        self.upload_fail_times = upload_fail_times
        self.upload_statuses = list(upload_statuses or [])
        self.readback_transform = readback_transform or (lambda recs: recs)
        self.batch_side_effect = batch_side_effect
        self.readback_side_effect = readback_side_effect
        self.batch_calls = 0
        self.readback_calls = 0

    async def upload_text(self, text, file_source, *, tenant_id, kb_id):
        self.pushed.append((text, file_source))
        if len(self.pushed) <= self.upload_fail_times:
            raise RuntimeError("boom-upload")
        idx = len(self.pushed) - 1
        status = self.upload_statuses[idx] if idx < len(self.upload_statuses) else "success"
        return UploadResult(track_id=f"tr-{len(self.pushed)}", status=status)

    async def batch_track_status(self, track_ids, *, tenant_id, kb_id,
                                 max_wait_seconds=1800, per_track_timeout_seconds=None):
        self.batch_calls += 1
        if self.batch_side_effect is not None:
            raise self.batch_side_effect
        terminal = {tid: TrackStatus(state="completed", doc_ids=["x"]) for tid in track_ids}
        return terminal, {}

    async def get_document_chunks(self, doc_id, *, tenant_id, kb_id):
        self.readback_calls += 1
        if self.readback_side_effect is not None:
            raise self.readback_side_effect
        # 真机形态：列表端点剥除 ns marker 与环绕空白（见 _NS_MARKER_RE）
        recs = [
            {"chunk_id": f"c{i}", "content": _NS_MARKER_RE.sub("", text), "file_path": fs}
            for i, (text, fs) in enumerate(self.pushed)
        ]
        return {"doc_id": doc_id, "total": len(recs),
                "chunks": self.readback_transform(recs)}


def _mgr_with(fake, tmp_path, monkeypatch) -> TaskManager:
    monkeypatch.setenv("RAG_TABLE_CTX_DIR", str(tmp_path))
    mgr = TaskManager(base_dir=str(tmp_path))
    mgr._http_client = fake
    return mgr


# ── ① 锚校验 ───────────────────────────────────────────────────────────

async def test_anchor_mismatch_raises_before_any_push(tmp_path, monkeypatch):
    fake = _FakeHttpClient()
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    bad = [{"chunk_index": 0, "content": "x", "file_source": "kb=kb1|doc=WRONG|tenant=t1|chunk=0|ctype=text|"}]
    with pytest.raises(RuntimeError, match="锚校验失败"):
        await mgr._ingest_engine_chunks(_make_task(), bad)
    assert fake.pushed == []


# ── ② 恒写档 + 原始 content 合成 ────────────────────────────────────────

async def test_archive_synthesized_from_original_content_before_injection(tmp_path, monkeypatch):
    fake = _FakeHttpClient()
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    task = _make_task()
    chunks = _chunks((0, TABLE_CONTENT, "table_row", TABLE_EXTRA), (1, "正文", "text"))
    await mgr._ingest_engine_chunks(task, chunks)
    archive = _load_table_context(task)
    assert archive == [{
        "type": "table",
        "table_body": "| c1 | c2 |\n| A | B |",
        "table_caption": "T",
        "table_idx": 0,
    }]
    table_pushes = [t for t, fs in fake.pushed if "ctype=table_row" in fs]
    assert table_pushes and table_pushes[0].startswith(TABLE_CONTENT)  # 原始在 token 之前


async def test_no_table_chunks_writes_empty_archive(tmp_path, monkeypatch):
    fake = _FakeHttpClient()
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    task = _make_task()
    await mgr._ingest_engine_chunks(task, _chunks((0, "正文", "text")))
    assert _load_table_context(task) == []   # 恒写档：空列表是有效值


# ── ③ 灌库 ────────────────────────────────────────────────────────────

async def test_empty_content_skipped_and_ns_token_injected(tmp_path, monkeypatch):
    fake = _FakeHttpClient()
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    chunks = _chunks((0, "甲", "text"), (1, "", "image"), (2, "乙", "text"))
    await mgr._ingest_engine_chunks(_make_task(), chunks)
    assert len(fake.pushed) == 2                      # 空 content 跳过
    from raganything.pipeline.stages import _inject_ns_token
    assert fake.pushed[0][0].endswith("<!--yx:" + _inject_ns_token("甲", "t1", "kb1", "d1").split("<!--yx:")[1])
    assert fake.pushed[0][1] == _fs(0)                # file_source 原样


async def test_upload_retry_then_success(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager.asyncio, "sleep", mock.AsyncMock())
    fake = _FakeHttpClient(upload_fail_times=1)
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))
    assert len(fake.pushed) == 2                      # 首败重试


async def test_upload_retries_exhausted_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager.asyncio, "sleep", mock.AsyncMock())
    fake = _FakeHttpClient(upload_fail_times=99)
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="灌库失败"):
        await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))


# ── ④ 等待：无成功语义（N7）────────────────────────────────────────────

async def test_wait_empty_release_does_not_fail_by_itself(tmp_path, monkeypatch):
    async def _batch(track_ids, *, tenant_id, kb_id, max_wait_seconds=1800,
                     per_track_timeout_seconds=None):
        return ({tid: TrackStatus(state="completed", empty=True) for tid in track_ids}, {})
    fake = _FakeHttpClient(batch_side_effect=None)
    fake.batch_track_status = _batch
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 不抛


async def test_wait_exception_still_proceeds_to_readback(tmp_path, monkeypatch):
    fake = _FakeHttpClient(batch_side_effect=RuntimeError("batch down"))
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 不抛
    assert fake.readback_calls == 1   # N7：等待异常后仍进读回裁决


# ── ⑤ 读回裁决 ────────────────────────────────────────────────────────

async def test_readback_missing_group_raises(tmp_path, monkeypatch):
    fake = _FakeHttpClient(readback_transform=lambda recs: recs[:0])
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="读回校验失败"):
        await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))


async def test_resplit_amplification_cannot_mask_missing(tmp_path, monkeypatch):
    """N2：重切放大（同锚多条记录）不得掩蔽另一锚的缺席
    （success 锚存在性判定：锚 0 被放大不得掩蔽锚 1 缺席）。"""
    def _transform(recs):
        amplified = [dict(recs[0]) for _ in range(3)]   # 锚 0 被重切放大成 3 条
        return amplified                                 # 锚 1 完全缺席

    fake = _FakeHttpClient(readback_transform=_transform)
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="读回校验失败"):
        await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text"), (1, "乙", "text")))


async def test_archive_write_failure_raises(tmp_path, monkeypatch):
    fake = _FakeHttpClient()
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    monkeypatch.setattr(
        "jonex_core.capability.atomic.ontology_compile.ingest._write_table_context",
        lambda t, i: None,
    )
    with pytest.raises(RuntimeError, match="存档写入失败"):
        await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))


# ── ⑤c 读回裁决：按条 status 分流（修复波）───

async def test_rerun_old_anchor_same_content_passes(tmp_path, monkeypatch):
    """跨代重跑：内容门拒收（duplicated）⇒ 引擎保留首推旧锚；
    内容面在场即通过（T1 §3「读回校验必须容忍旧锚点」）。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    fake.readback_transform = lambda recs: [dict(recs[0], file_path=_fs(3))]  # 读回锚=旧锚 3
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 不抛


async def test_same_content_second_anchor_duplicated_passes(tmp_path, monkeypatch):
    """同 sanitize 内容两条片：第二条必被内容门拒（duplicated）；
    内容在场 ⇒ 通过（组内 ≥1 命中语义的钉死）。"""
    fake = _FakeHttpClient(upload_statuses=["success", "duplicated"])
    fake.readback_transform = lambda recs: recs[:1]   # 读回只留锚 0
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text"), (1, "甲", "text")))


async def test_duplicated_content_absent_raises(tmp_path, monkeypatch):
    """同锚异内容被 file_source 门拒（duplicated）且该内容在引擎不可读
    ⇒ 响亮失败（防静默陈旧）；旧记录多片形态（canary 跳过、专测内容面）。
    B6：旧代分片非「全片子串」⇒ 分片容忍不得误放行。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    fake.readback_transform = lambda recs: [
        dict(recs[0], content="旧代分片一"), dict(recs[0], content="旧代分片二"),
    ]
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="读回校验失败"):
        await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))


async def test_duplicated_content_matches_when_readback_strips_marker(tmp_path, monkeypatch):
    """承重修复：读回通道（列表端点）剥 ns marker ⇒ 内容键与读回两侧
    都用注入前内容，duplicated 条不因 marker 错位被误杀。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])   # 默认读回 = 真机形态
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 不抛


# ── ⑤c2 duplicated 分片形态容忍（Important-1，补充波）─────────────────

async def test_resplit_rerun_fragments_tolerated(tmp_path, monkeypatch):
    """Important-1：超限片重跑被内容门拒（duplicated），引擎内只剩重切分片
    （同锚多条、相邻重叠的连续子串）⇒ 分片形态容忍，不误杀。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    def _transform(recs):
        c = "甲" * 400
        return [dict(recs[0], content=c[:240]), dict(recs[0], content=c[216:])]
    fake.readback_transform = _transform
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲" * 400, "text")))  # 不抛


async def test_fragment_edge_replacement_char_tolerated(tmp_path, monkeypatch):
    """分片边缘的半截 token 被 decode 成 U+FFFD（探针实测压力样本形态）⇒
    规范化剥边缘 U+FFFD 后仍是全片子串 ⇒ 容忍。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    def _transform(recs):
        c = "甲" * 400
        return [dict(recs[0], content=c[:240] + "�"), dict(recs[0], content=c[216:])]
    fake.readback_transform = _transform
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲" * 400, "text")))  # 不抛


async def test_partial_marker_edge_fragments_tolerated(tmp_path, monkeypatch):
    """窗口切在 ns marker 内部（机制推论）⇒ 分片边缘带半截 marker 时
    规范化剥除后仍是全片子串 ⇒ 容忍。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    def _transform(recs):
        c = "甲" * 400
        return [dict(recs[0], content=c[:240] + "\n<!--yx:84"),
                dict(recs[0], content="41e8-->" + c[216:])]
    fake.readback_transform = _transform
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲" * 400, "text")))  # 不抛


async def test_fragments_partial_coverage_raises(tmp_path, monkeypatch):
    """分片合计长度不覆盖全片（缺块）⇒ 不得当「全片在场」放行 ⇒ 抛。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    def _transform(recs):
        c = "甲" * 400
        return [dict(recs[0], content=c[:240])]
    fake.readback_transform = _transform
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="读回校验失败"):
        await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲" * 400, "text")))


async def test_fragments_only_count_for_own_anchor(tmp_path, monkeypatch):
    """锚域限定：分片只在自身锚下计入——锚 6 的合格分片不得为锚 5 的内容背书。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    def _transform(recs):
        c = "乙" * 400
        return [dict(recs[0], file_path=_fs(6), content=c[:240]),
                dict(recs[0], file_path=_fs(6), content=c[216:])]
    fake.readback_transform = _transform
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="读回校验失败"):
        await mgr._ingest_engine_chunks(_make_task(), _chunks((5, "乙" * 400, "text")))


async def test_fragments_with_non_substring_piece_raises(tmp_path, monkeypatch):
    """分片含非子图片（异质内容混入）⇒ 子串判据是 all 而非 any，不得
    「任一片命中」即放行 ⇒ 抛（对抗性变异 N1 擒杀点）。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    def _transform(recs):
        c = "甲" * 400
        return [dict(recs[0], content=c[:240]), dict(recs[0], content="异质" * 100)]
    fake.readback_transform = _transform
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="读回校验失败"):
        await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲" * 400, "text")))


async def test_unknown_status_content_present_anchor_absent_passes(tmp_path, monkeypatch):
    """未知 status：锚漂移（读回锚=9）但内容在场 ⇒ 内容面兜底通过。"""
    fake = _FakeHttpClient(upload_statuses=["some_new_state"])
    fake.readback_transform = lambda recs: [dict(recs[0], file_path=_fs(9))]
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 不抛


async def test_unknown_status_anchor_and_content_absent_raises(tmp_path, monkeypatch):
    """未知状态 + 锚与内容双缺席（读回空）⇒ 推入 missing_either ⇒ 抛且
    消息含「未知状态未落地」（对抗性变异 N4 擒杀点）。"""
    fake = _FakeHttpClient(upload_statuses=["some_new_state"])
    fake.readback_transform = lambda recs: []
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="读回校验失败") as excinfo:
        await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))
    assert "未知状态未落地 1/1" in str(excinfo.value)


async def test_duplicated_content_key_sanitize_normalization(tmp_path, monkeypatch):
    """duplicated 条：sanitize 归一（html.unescape）在内容键与读回两侧
    同时生效 ⇒ 不因实体转义形态差异误杀。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    fake.readback_transform = lambda recs: [
        dict(recs[0], content=html.unescape(recs[0]["content"]))
    ]
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲 &amp; 乙", "text")))  # 不抛


async def test_duplicated_content_in_anchorless_record_passes(tmp_path, monkeypatch):
    """无锚记录也计入 record_sanitized（内容门全局语义，内容在场即证据）：
    读回记录缺 chunk= 锚但内容在场 ⇒ duplicated 条通过。"""
    fake = _FakeHttpClient(upload_statuses=["duplicated"])
    fake.readback_transform = lambda recs: [
        dict(recs[0], file_path="kb=kb1|doc=d1|tenant=t1|ctype=text|")
    ]
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 不抛


async def test_resplit_fragments_all_landed_passes(tmp_path, monkeypatch):
    """重切真实形态：同锚多条分片记录（内容≠全片）⇒ 不误报。"""
    fake = _FakeHttpClient()
    def _transform(recs):
        c = recs[0]["content"]
        return [dict(recs[0], content=c[: len(c) // 2]),
                dict(recs[0], content=c[len(c) // 2:])]
    fake.readback_transform = _transform
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲" * 400, "text")))


async def test_wait_failed_tracks_do_not_fail_by_themselves(tmp_path, monkeypatch):
    """N7：failed 轨不作判死结论（真伪交读回裁决）⇒ 不抛。"""
    async def _batch(track_ids, *, tenant_id, kb_id, max_wait_seconds=1800,
                     per_track_timeout_seconds=None):
        return ({tid: TrackStatus(state="failed") for tid in track_ids}, {})
    fake = _FakeHttpClient()
    fake.batch_track_status = _batch
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 不抛


async def test_canary_passes_with_entity_escaped_readback(tmp_path, monkeypatch):
    """读回=提交文本经 sanitize（html.unescape）后的形态（T1-②）：
    canary 按同源 sanitize 比较 ⇒ 通过；去掉 sanitize 直比则误报。"""
    fake = _FakeHttpClient()
    fake.readback_transform = lambda recs: [
        dict(recs[0], content=html.unescape(recs[0]["content"]))
    ]
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲 &amp; 乙", "text")))


async def test_unknown_status_falls_back_to_either(tmp_path, monkeypatch):
    """未知 status ⇒ 锚或内容任一在场即通过（兜底不误杀）。"""
    fake = _FakeHttpClient(upload_statuses=["some_new_state"])
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 锚在场


# ── ⑤b canary（T1-④ 判定为 sanitize 后保真：保留本段）──────────────────

async def test_canary_single_record_match_passes(tmp_path, monkeypatch):
    fake = _FakeHttpClient()      # 默认读回 = 真机形态（剥 marker 后的原始内容）
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 不抛


async def test_canary_matches_original_when_readback_strips_marker(tmp_path, monkeypatch):
    """承重修复：读回通道剥 ns marker ⇒ canary 期望值用**原始内容**，
    单记录场景不因 marker 错位被误杀。"""
    fake = _FakeHttpClient()          # 默认成功、单记录；默认读回 = 真机形态
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))  # 不抛


async def test_canary_mismatch_raises(tmp_path, monkeypatch):
    def _transform(recs):
        return [dict(recs[0], content="被篡改的内容")]
    fake = _FakeHttpClient(readback_transform=_transform)
    mgr = _mgr_with(fake, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="canary"):
        await mgr._ingest_engine_chunks(_make_task(), _chunks((0, "甲", "text")))


# ── 集成：插入点行为 ──────────────────────────────────────────────────

async def test_run_ontology_only_ingests_before_replay_and_extraction(tmp_path, monkeypatch):
    mgr = _mgr_with(_FakeHttpClient(), tmp_path, monkeypatch)
    task = _make_task(engine_chunks=_chunks((0, "甲", "text")))
    order = []
    mgr._ingest_engine_chunks = mock.AsyncMock(side_effect=lambda t, c: order.append("ingest"))
    monkeypatch.setattr(
        "jonex_core.capability.atomic.ontology_compile.service._load_table_context",
        lambda t: order.append("replay") or [],
    )
    mgr._run_ontology_extraction = mock.AsyncMock(
        side_effect=lambda *a, **k: order.append("extract"),
    )
    await mgr._run_ontology_only(task, _make_handle())
    assert order == ["ingest", "replay", "extract"]


async def test_run_ontology_only_failure_raises_and_skips_extraction(tmp_path, monkeypatch):
    mgr = _mgr_with(_FakeHttpClient(), tmp_path, monkeypatch)
    task = _make_task(engine_chunks=_chunks((0, "甲", "text")))
    mgr._ingest_engine_chunks = mock.AsyncMock(side_effect=RuntimeError("读回校验失败"))
    mgr._run_ontology_extraction = mock.AsyncMock()
    with pytest.raises(RuntimeError, match="读回校验失败"):
        await mgr._run_ontology_only(task, _make_handle())
    mgr._run_ontology_extraction.assert_not_called()


async def test_run_ontology_only_rag_chain_skips_ingest(tmp_path, monkeypatch):
    """回归：无 engine_chunks（rag 链）⇒ 前置段整体跳过，行为与从前一致。"""
    mgr = _mgr_with(_FakeHttpClient(), tmp_path, monkeypatch)
    task = _make_task()          # engine_chunks 默认 []
    mgr._ingest_engine_chunks = mock.AsyncMock()
    mgr._run_ontology_extraction = mock.AsyncMock()
    await mgr._run_ontology_only(task, _make_handle())
    mgr._ingest_engine_chunks.assert_not_called()
    mgr._run_ontology_extraction.assert_awaited_once()


async def test_run_ontology_only_timing_logged_and_reraised(tmp_path, monkeypatch, caplog):
    """N6：前置段失败补发 ingest_timing（failed+force_ontology_only）后 re-raise。"""
    mgr = _mgr_with(_FakeHttpClient(), tmp_path, monkeypatch)
    task = _make_task(engine_chunks=_chunks((0, "甲", "text")))
    mgr._ingest_engine_chunks = mock.AsyncMock(side_effect=RuntimeError("boom"))
    mgr._run_ontology_extraction = mock.AsyncMock()
    calls = []
    monkeypatch.setattr(
        task_manager, "_log_ingest_timing_v2",
        lambda t, status, *, force_ontology_only=False, error="": calls.append((status, force_ontology_only)),
    )
    with pytest.raises(RuntimeError, match="boom"):
        await mgr._run_ontology_only(task, _make_handle())
    assert ("failed", True) in calls
