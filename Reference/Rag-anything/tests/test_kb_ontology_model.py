"""KB 级本体抽取模型（P3a）：模型从平台报文 → 引擎任务 → 抽取 LLM 出口的穿透。

两条事实必须钉住：

1. **模型只在有值时下发**：平台报文无键 ⇒ 引擎任务字段为空 ⇒ `_openai_chat`
   回落 `ONTOLOGY_LLM_MODEL`（"没配置"与"平台没下发"是同一件事）。
2. **一个值喂四个出口**：主抽取 / 表格补跑 / 表级判定 / 列名映射共用同一个模型名
   —— 后两个在 `table_object_extractor` 里，只有 `scope` 一个通道，所以模型挂在
   scope 上（这也是这里断言 `scope["model"]` 的原因）。
"""

import importlib.util
import os
import sys
import textwrap

import pytest

CHUNKS = [{"chunk_index": 0, "content": "甲", "file_source": "kb=kb1|doc=doc1"}]

PARAMS = {
    "document_id": "doc1",
    "knowledge_base_id": "kb1",
    "ontology_schema": {"entity_types": [], "relation_types": []},
    "schema_version": 3,
}


# ── 1) handler：报文参数进 CreateTaskRequest ────────────────────────────


def _get_handler():
    mod_name = "atomic_rag_server_v2_p3a"
    if mod_name in sys.modules:
        return sys.modules[mod_name].handle_retry_ontology_extract
    spec = importlib.util.spec_from_file_location(
        mod_name,
        os.path.join(os.path.dirname(__file__), "..", "atomic-rag-server-v2.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.handle_retry_ontology_extract


class _FakeCreateResult:
    task_id = "new-task-1"


class _FakeTaskManager:
    def __init__(self):
        self._tasks = {}
        self.created = None

    async def create(self, req, tenant_id, idempotency_key=None):
        self.created = {"req": req, "tenant_id": tenant_id, "key": idempotency_key}
        return _FakeCreateResult()


async def test_handler_passes_ontology_model():
    handler = _get_handler()
    tm = _FakeTaskManager()
    await handler(dict(PARAMS, ontology_model="Qwen3.6-35B-A3B"), tenant_id="t1", task_manager=tm)
    assert tm.created["req"].ontology_model == "Qwen3.6-35B-A3B"


async def test_handler_defaults_empty_ontology_model():
    """报文没带这个键 ⇒ 空串 ⇒ 引擎回落 ONTOLOGY_LLM_MODEL（不编默认值）。"""
    handler = _get_handler()
    tm = _FakeTaskManager()
    await handler(dict(PARAMS), tenant_id="t1", task_manager=tm)
    assert tm.created["req"].ontology_model == ""


def test_task_schema_carries_ontology_model():
    from raganything.service.models import CreateTaskRequest, TaskInfo

    assert CreateTaskRequest(file_path="").ontology_model == ""
    fields = getattr(TaskInfo, "model_fields", None) or TaskInfo.__fields__
    assert "ontology_model" in fields


def test_create_maps_ontology_model_into_task_info():
    """req → TaskInfo 的映射漏了就等于"平台下发了但引擎不认"（源码级断言）。"""
    from raganything.service.task_manager import TaskManager

    src = textwrap.dedent(
        __import__("inspect").getsource(TaskManager.create)
    )
    assert 'ontology_model=getattr(req, "ontology_model", "")' in src


# ── 2) 抽取侧：一个模型喂四个出口 ───────────────────────────────────────


def test_extraction_scope_carries_task_model():
    """scope 是本轮唯一通道：四处 LLM 出口都从它读 model。"""
    import inspect

    from raganything.service.task_manager import TaskManager

    # 抽取在 _execute_pipeline_http_inner 里那段（含主抽取与表格补跑）
    src = textwrap.dedent(inspect.getsource(TaskManager._run_ontology_extraction))
    assert '"model": getattr(task, "ontology_model", "")' in src


def test_table_extraction_receives_scope():
    """表格对象抽取必须拿到同一个 scope（它没有别的通道能拿到模型）。"""
    import inspect

    from raganything.service.task_manager import TaskManager

    src = textwrap.dedent(inspect.getsource(TaskManager._merge_table_objects))
    assert "scope=scope" in src or "scope: dict" in src
    assert "scope=scope" in src


def test_ontology_extraction_passes_scope_to_native_extractor():
    import inspect

    from raganything.service.task_manager import TaskManager

    src = textwrap.dedent(inspect.getsource(TaskManager._run_ontology_extraction))
    # 主抽取与表格补跑两次都会带上同一个 scope
    assert src.count("scope=scope") >= 2


# ── 3) _openai_chat：三级优先级 ─────────────────────────────────────────


class _FakeResponse:
    def __init__(self, content="{}"):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}, "finish_reason": "stop"}]
        }


class _FakeAsyncClient:
    instances = []

    def __init__(self, *a, **kw):
        self.calls = []
        _FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        return _FakeResponse()


@pytest.fixture()
def chat_env(monkeypatch):
    import httpx

    from jonex_core.capability.atomic.rag import ontology_extractor as oe

    _FakeAsyncClient.instances = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    # env 默认值固定下来，避免受运行环境影响
    monkeypatch.setattr(oe, "resolve_llm_model", lambda key: "env-default-model")
    return oe


async def _payload_of(coro):
    await coro
    return _FakeAsyncClient.instances[-1].calls[-1]["json"]


async def test_openai_chat_uses_explicit_model(chat_env):
    payload = await _payload_of(
        chat_env._openai_chat(
            [{"role": "user", "content": "x"}], model="kb-model", scope={"model": "scope-model"}
        )
    )
    assert payload["model"] == "kb-model"


async def test_openai_chat_uses_scope_model(chat_env):
    """表格对象抽取只有 scope 通道 —— 这是它的模型来源。"""
    payload = await _payload_of(
        chat_env._openai_chat([{"role": "user", "content": "x"}], scope={"model": "scope-model"})
    )
    assert payload["model"] == "scope-model"


async def test_openai_chat_falls_back_to_env(chat_env):
    for scope in (None, {}, {"model": ""}, {"model": "   "}):
        payload = await _payload_of(
            chat_env._openai_chat([{"role": "user", "content": "x"}], scope=scope)
        )
        assert payload["model"] == "env-default-model"


async def test_native_extractor_passes_options_model(chat_env, monkeypatch):
    """`native_ontology_extract` 这条外部 action 允许 options.model 覆盖。"""
    from jonex_core.capability.atomic.rag import native_ontology_extractor as noe

    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["model"] = kwargs.get("model")
        return "{}", {"finish_reason": "stop"}

    monkeypatch.setattr(noe, "_openai_chat", fake_chat)

    extractor = noe.NativeOntologyExtractor()
    await extractor._call_llm_once("sys", "user", {"tenant_id": "t"}, {"model": "opts-model"})
    assert captured["model"] == "opts-model"


async def test_native_extractor_passes_none_without_options_model(chat_env, monkeypatch):
    """options 里没有 model ⇒ 传 None，由 _openai_chat 按 scope/env 回落。"""
    from jonex_core.capability.atomic.rag import native_ontology_extractor as noe

    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["model"] = kwargs.get("model")
        return "{}", {"finish_reason": "stop"}

    monkeypatch.setattr(noe, "_openai_chat", fake_chat)

    extractor = noe.NativeOntologyExtractor()
    await extractor._call_llm_once("sys", "user", {"tenant_id": "t"}, {})
    assert captured["model"] is None
