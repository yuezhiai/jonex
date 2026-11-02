"""[jonex] KB 级 VLM 覆盖 P2a —— 原子层单元测试。

覆盖四件事（对应 docs/knowledge-base/kb-model-settings-design.md §5.1）：

1. **contextvar 语义**：覆盖值能被子 task 继承、任务结束能还原、降级计数用共享 list
   跨子 task 累计。
2. **driver 作用域**：只有带图片块的请求才换模型；文本请求必须仍用绑定模型
   （否则同一个任务里的摘要/标签会被一起改掉）。
3. **预检**：有覆盖时先打一发真实视觉请求，失败要抛 ``VlmOverridePrecheckError``
   且**不能**吞掉成普通异常；无覆盖时完全不动（连 bound 都不碰）。
4. **降级护栏**：无覆盖 / fail 模式不介入；record 模式超阈值抛
   ``VlmStrictModeError``。
"""

import asyncio

import pytest

from raganything.models.vlm_override import (
    effective_vlm_model,
    has_image_part,
    resolve_vision_model,
)
from raganything.service.jonex_metering_ctx import (
    get_vlm_degraded_count,
    get_vlm_degraded_reasons,
    get_vlm_override,
    get_vlm_strict_mode,
    record_vlm_degraded,
    reset_ingest_ctx,
    reset_vlm_degraded,
    set_ingest_ctx,
)
from raganything.service.vlm_guard import (
    VlmOverridePrecheckError,
    VlmStrictModeError,
    guard_vlm_degraded,
    precheck_vlm_override,
    strict_fail_ratio,
)


def _vision_messages():
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ],
    }]


def _text_messages():
    return [{"role": "user", "content": "just text"}]


# ── 1. contextvar ────────────────────────────────────────────────────────────


def test_ctx_defaults_are_empty():
    """没有任务上下文时，覆盖/严格模式都必须是空 —— 这是"不配置就不干涉"的底座。"""
    assert get_vlm_override() == ""
    assert get_vlm_strict_mode() == ""
    assert resolve_vision_model("default-model", _vision_messages()) == "default-model"


def test_ctx_roundtrip_and_reset():
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="qwen-vl", vlm_strict_mode="record",
    )
    try:
        assert get_vlm_override() == "qwen-vl"
        assert get_vlm_strict_mode() == "record"
        assert resolve_vision_model("default-model", _vision_messages()) == "qwen-vl"
    finally:
        reset_ingest_ctx(token)
    assert get_vlm_override() == ""
    assert resolve_vision_model("default-model", _vision_messages()) == "default-model"


def test_ctx_inherited_by_child_task():
    """asyncio.create_task 复制 contextvar —— 子 task 读得到覆盖（否则多模态全瞎）。"""
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="child-vlm", vlm_strict_mode="record",
    )

    async def child():
        return resolve_vision_model("default-model", _vision_messages())

    try:
        assert asyncio.run(child()) == "child-vlm"
    finally:
        reset_ingest_ctx(token)


def test_degraded_counter_shared_with_child_tasks():
    """降级计数用可变 list：子 task append 后父 task 必须看得到。"""
    token = set_ingest_ctx(tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1")

    async def _gather():
        async def bump(i):
            record_vlm_degraded(f"item-{i}")

        await asyncio.gather(*(bump(i) for i in range(3)))

    try:
        asyncio.run(_gather())
        assert get_vlm_degraded_count() == 3
        assert sorted(get_vlm_degraded_reasons()) == ["item-0", "item-1", "item-2"]
        reset_vlm_degraded()
        assert get_vlm_degraded_count() == 0
    finally:
        reset_ingest_ctx(token)


def test_set_ingest_ctx_clears_previous_degraded_count():
    """新任务必须清零 —— 同一事件循环里上一个任务的降级不能带进来。"""
    token = set_ingest_ctx(tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1")
    record_vlm_degraded("stale")
    assert get_vlm_degraded_count() == 1
    token2 = set_ingest_ctx(tenant_id="t1", kb_id="kb1", doc_id="d2", trace_id="tr2")
    try:
        assert get_vlm_degraded_count() == 0
    finally:
        reset_ingest_ctx(token2)
        reset_ingest_ctx(token)


# ── 2. driver 作用域 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("messages,expected", [
    (_vision_messages(), True),
    (_text_messages(), False),
    # anthropic 风格 image 块
    ([{"role": "user", "content": [{"type": "image", "source": {}}]}], True),
    # gemini 风格 inline_data
    ([{"role": "user", "content": [{"type": "inline_data", "data": "x"}]}], True),
    # 字符串 content 永远不会是视觉请求
    ([{"role": "system", "content": "image_url 只是文字"}], False),
    ([], False),
    (None, False),
])
def test_has_image_part(messages, expected):
    assert has_image_part(messages) is expected


def test_override_only_applies_to_vision_requests():
    """核心不变量：覆盖只在视觉请求生效，文本请求一字不改。"""
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="record",
    )
    try:
        assert resolve_vision_model("bound-vlm", _vision_messages()) == "kb-vlm"
        assert resolve_vision_model("bound-llm", _text_messages()) == "bound-llm"
        # effective_vlm_model 是"实际会用哪个模型"的口径（日志/留痕）
        assert effective_vlm_model("bound-vlm") == "kb-vlm"
    finally:
        reset_ingest_ctx(token)


def test_blank_override_is_treated_as_no_override():
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="   ", vlm_strict_mode="record",
    )
    try:
        assert resolve_vision_model("bound-vlm", _vision_messages()) == "bound-vlm"
    finally:
        reset_ingest_ctx(token)


# ── 3. 预检 ─────────────────────────────────────────────────────────────────


class _FakeBound:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail:
            raise RuntimeError("model 'kb-vlm' not found")
        return "pong"


def test_precheck_is_noop_without_override():
    bound = _FakeBound()
    asyncio.run(precheck_vlm_override(bound, stage="full"))
    assert bound.calls == []


def test_precheck_sends_tiny_vision_request():
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="record",
    )
    bound = _FakeBound()
    try:
        asyncio.run(precheck_vlm_override(bound, stage="reparse_strict"))
    finally:
        reset_ingest_ctx(token)
    assert len(bound.calls) == 1
    messages, kwargs = bound.calls[0]
    assert has_image_part(messages) is True
    assert kwargs["max_tokens"] == 1


def test_precheck_fails_loudly_when_model_unusable():
    """预检失败必须抛专用异常（→ MODEL_NOT_FOUND），不能降级成普通 Exception。"""
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="fail",
    )
    try:
        with pytest.raises(VlmOverridePrecheckError) as excinfo:
            asyncio.run(precheck_vlm_override(_FakeBound(fail=True), stage="reparse_strict"))
    finally:
        reset_ingest_ctx(token)
    assert "kb-vlm" in str(excinfo.value)
    assert "预检失败" in str(excinfo.value)


def test_precheck_fails_when_process_has_no_vlm_binding():
    """有覆盖却完全没有 VLM 绑定 ⇒ 必须报错，不能静默"看起来成功"。"""
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="record",
    )
    try:
        with pytest.raises(VlmOverridePrecheckError):
            asyncio.run(precheck_vlm_override(None, stage="full"))
    finally:
        reset_ingest_ctx(token)


# ── 4. 降级护栏 ─────────────────────────────────────────────────────────────


def test_ratio_env_fallback(monkeypatch):
    monkeypatch.delenv("VLM_STRICT_FAIL_RATIO", raising=False)
    assert strict_fail_ratio() == 0.5
    monkeypatch.setenv("VLM_STRICT_FAIL_RATIO", "0.8")
    assert strict_fail_ratio() == 0.8
    monkeypatch.setenv("VLM_STRICT_FAIL_RATIO", "not-a-number")
    assert strict_fail_ratio() == 0.5
    monkeypatch.setenv("VLM_STRICT_FAIL_RATIO", "3")
    assert strict_fail_ratio() == 0.5


def test_guard_is_noop_without_override():
    """没有 KB 覆盖 ⇒ 一次都不介入（存量行为零变化）。"""
    token = set_ingest_ctx(tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1")
    try:
        record_vlm_degraded("x")
        guard_vlm_degraded(2, stage="multimodal")  # 100% 降级也不该失败
    finally:
        reset_ingest_ctx(token)


def test_guard_is_noop_in_fail_mode():
    """reparse_strict：旧数据已删，运行期只留痕（靠预检兜底），不判失败。"""
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="fail",
    )
    try:
        record_vlm_degraded("x")
        guard_vlm_degraded(1, stage="reparse_strict")
    finally:
        reset_ingest_ctx(token)


def test_guard_raises_when_ratio_exceeds_threshold(monkeypatch):
    monkeypatch.setenv("VLM_STRICT_FAIL_RATIO", "0.5")
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="record",
    )
    try:
        for _ in range(3):
            record_vlm_degraded("x")
        with pytest.raises(VlmStrictModeError) as excinfo:
            guard_vlm_degraded(4, stage="multimodal")
    finally:
        reset_ingest_ctx(token)
    assert "kb-vlm" in str(excinfo.value)
    assert "3/4" in str(excinfo.value)


def test_guard_passes_at_or_below_threshold(monkeypatch):
    monkeypatch.setenv("VLM_STRICT_FAIL_RATIO", "0.5")
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="record",
    )
    try:
        record_vlm_degraded("x")
        record_vlm_degraded("x")
        guard_vlm_degraded(4, stage="multimodal")  # 恰好 50%，不超过阈值不失败
    finally:
        reset_ingest_ctx(token)


def test_guard_noop_when_no_images():
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="record",
    )
    try:
        guard_vlm_degraded(0, stage="multimodal")
    finally:
        reset_ingest_ctx(token)


# ── 5. driver 真实 payload 作用域（openai 兼容驱动）────────────────────────


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """最小 httpx.AsyncClient 替身：只记录 post 的 json body。"""

    captured = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.captured.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})


def _run_driver(messages, spec_model="bound-model"):
    import httpx

    from raganything.models.drivers.openai import OpenAIDriver
    from raganything.models.types import ModelSpec

    spec = ModelSpec(model_id=spec_model, binding="vllm", host="http://unit-test")
    _FakeAsyncClient.captured = []
    original = httpx.AsyncClient
    httpx.AsyncClient = _FakeAsyncClient
    try:
        asyncio.run(OpenAIDriver().complete(messages, spec))
    finally:
        httpx.AsyncClient = original
    return _FakeAsyncClient.captured[0]["json"]


def test_driver_overrides_model_for_vision_request():
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="record",
    )
    try:
        body = _run_driver(_vision_messages())
    finally:
        reset_ingest_ctx(token)
    assert body["model"] == "kb-vlm"


def test_driver_keeps_bound_model_for_text_request():
    """文本调用（同一任务内）绝不能跟着覆盖走 —— 否则摘要/标签模型被误改。"""
    token = set_ingest_ctx(
        tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1",
        vlm_override="kb-vlm", vlm_strict_mode="record",
    )
    try:
        body = _run_driver(_text_messages())
    finally:
        reset_ingest_ctx(token)
    assert body["model"] == "bound-model"


def test_driver_untouched_without_override():
    body = _run_driver(_vision_messages())
    assert body["model"] == "bound-model"


# ── 6. 护栏分母口径 ────────────────────────────────────────────────────────


def test_count_multimodal_items_counts_images_only():
    """分母只数图片（P2a 覆盖只管图片理解，视频/音频模型不在这轮范围）。"""
    from raganything.service.task_manager import _count_multimodal_items

    content_list = [
        {"type": "text", "text": "a"},
        {"type": "image", "img_path": "x.png"},
        {"type": "image", "img_path": "y.png"},
        {"type": "video", "video_path": "v.mp4"},
        {"type": "audio", "audio_path": "a.mp3"},
        "not-a-dict",
    ]
    assert _count_multimodal_items(content_list) == 2
    assert _count_multimodal_items([]) == 0
    assert _count_multimodal_items(None) == 0


# ── 6b. 转写缓存键：图片按 VLM 模型分桶 ────────────────────────────────────


def test_transcript_cache_key_buckets_images_by_model():
    """图片描述是 VLM 产物 ⇒ 换模型必须换键，否则改完模型拿到的还是旧模型的描述。"""
    from raganything.service.task_manager import _transcript_cache_key

    assert _transcript_cache_key("image", "Qwen3-VL-8B") == "image@Qwen3-VL-8B"
    assert _transcript_cache_key("image", "  Qwen3-VL-8B  ") == "image@Qwen3-VL-8B"


def test_transcript_cache_key_unchanged_without_override():
    """没覆盖时键仍是 image —— 升级前后缓存零成本，不会把既有产物全废掉。"""
    from raganything.service.task_manager import _transcript_cache_key

    assert _transcript_cache_key("image", "") == "image"
    assert _transcript_cache_key("image", "   ") == "image"


def test_transcript_cache_key_never_buckets_video_audio():
    """视频/音频正文来自 MPS/ASR，与 VLM 模型无关：分组只会白付分钟级转写费。"""
    from raganything.service.task_manager import _transcript_cache_key

    assert _transcript_cache_key("video", "Qwen3-VL-8B") == "video"
    assert _transcript_cache_key("audio", "Qwen3-VL-8B") == "audio"


def test_transcript_cache_hit_uses_bucketed_key():
    """方法体里读/写必须用同一个分桶键（源码级断言，防只改一处）。"""
    import inspect
    import textwrap

    from raganything.service.task_manager import TaskManager

    source = textwrap.dedent(inspect.getsource(TaskManager._textualize_multimodal))
    assert "_transcript_cache_key(t, _vlm_tag)" in source
    assert "transcript_cache[_ck]" in source
    # 旧写法必须彻底消失：写回用裸 t 会把新模型的描述写进旧模型的桶
    assert "transcript_cache[t] = body" not in source
    assert "if t in transcript_cache:" not in source


# ── 7. task_manager 入口：所有执行模式都设 ctx；预检在删旧数据之前 ─────────


def _make_task(execution_mode="parse_only", vlm_override="kb-vlm", snapshot=None):
    from raganything.service.models import TaskInfo

    return TaskInfo(
        tenant_id="t1",
        name="doc.pdf",
        file_path="/tmp/doc.pdf",
        kb_id="kb1",
        document_id="doc-1",
        execution_mode=execution_mode,
        vlm_override=vlm_override,
        config_snapshot=dict(snapshot or {}),
    )


def test_entry_sets_ctx_for_early_return_modes():
    """parse_only / prepare_only 在旧代码里 set_ingest_ctx 之前就 return —— 现在必须也带上。

    这是 P2a 的第二个 bug 修复点：不设 ctx ⇒ 覆盖读不到、计量缺 doc_id。
    """
    from raganything.service.task_manager import TaskManager

    captured = {}

    async def fake_inner(task, handle):
        captured["override"] = get_vlm_override()
        captured["strict"] = get_vlm_strict_mode()
        captured["degraded"] = get_vlm_degraded_count()

    tm = TaskManager.__new__(TaskManager)
    tm._pipeline_executor = None
    tm._execute_pipeline_http_inner = fake_inner  # type: ignore[method-assign]

    asyncio.run(tm._execute_pipeline_http(_make_task("parse_only"), object()))

    assert captured == {"override": "kb-vlm", "strict": "record", "degraded": 0}
    # 任务结束必须还原，否则污染同一事件循环的下一任务
    assert get_vlm_override() == ""
    assert get_vlm_strict_mode() == ""


def test_entry_derives_fail_mode_for_reparse_strict():
    from raganything.service.task_manager import TaskManager

    captured = {}

    async def fake_inner(task, handle):
        captured["strict"] = get_vlm_strict_mode()

    tm = TaskManager.__new__(TaskManager)
    tm._pipeline_executor = None
    tm._execute_pipeline_http_inner = fake_inner  # type: ignore[method-assign]

    asyncio.run(tm._execute_pipeline_http(_make_task("reparse_strict"), object()))
    assert captured["strict"] == "fail"


def test_entry_reads_override_from_snapshot_when_task_field_empty():
    """覆盖值可能只在快照里（config_snapshot 是持久化的，task 字段是内存态）。"""
    from raganything.service.task_manager import TaskManager

    captured = {}

    async def fake_inner(task, handle):
        captured["override"] = get_vlm_override()

    tm = TaskManager.__new__(TaskManager)
    tm._pipeline_executor = None
    tm._execute_pipeline_http_inner = fake_inner  # type: ignore[method-assign]

    task = _make_task("full", vlm_override="", snapshot={"kb_vlm_override": "snap-vlm"})
    asyncio.run(tm._execute_pipeline_http(task, object()))
    assert captured["override"] == "snap-vlm"


def test_entry_clears_strict_mode_without_override():
    """没配置覆盖 ⇒ strict_mode 也是空，护栏完全不介入。"""
    from raganything.service.task_manager import TaskManager

    captured = {}

    async def fake_inner(task, handle):
        captured["strict"] = get_vlm_strict_mode()

    tm = TaskManager.__new__(TaskManager)
    tm._pipeline_executor = None
    tm._execute_pipeline_http_inner = fake_inner  # type: ignore[method-assign]

    asyncio.run(tm._execute_pipeline_http(_make_task("full", vlm_override=""), object()))
    assert captured["strict"] == ""


def test_precheck_runs_before_old_data_deletion():
    """安全不变量：reparse_strict 会删光旧 doc，预检必须排在删除**之前**。

    用 AST 而非纯文本断言 —— 代码里两处顺序一旦被后人调换，这条测试必须红。
    """
    import ast as _ast
    import inspect
    import textwrap

    from raganything.service.task_manager import TaskManager

    # getsource 带方法缩进，dedent 后再 parse
    source = textwrap.dedent(inspect.getsource(TaskManager._execute_pipeline_http_inner))
    tree = _ast.parse(source)
    lines = {}

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            func = node.func
            name = getattr(func, "id", "") or getattr(func, "attr", "")
            if name in ("precheck_vlm_override", "_reparse_delete_all_old",
                        "_snapshot_old_doc_ids"):
                lines.setdefault(name, node.lineno)

    assert "precheck_vlm_override" in lines, "预检调用丢了"
    assert "_reparse_delete_all_old" in lines, "删旧数据的调用没找到（改名了？）"
    assert lines["precheck_vlm_override"] < lines["_reparse_delete_all_old"], (
        "预检必须排在删旧数据之前，否则模型填错会先毁用户数据"
    )
    if "_snapshot_old_doc_ids" in lines:
        assert lines["precheck_vlm_override"] < lines["_snapshot_old_doc_ids"]
