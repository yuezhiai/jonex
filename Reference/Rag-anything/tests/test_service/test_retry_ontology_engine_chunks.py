"""[A18] retry_ontology_extract handler：engine_chunks 解析/透传；在途复用留声（I3）。

该 handler 不依赖模块级 HTTP client（与 handle_update_chunk 不同），
传 fake task_manager 即可全链跑通。
"""
import importlib.util
import os
import sys

from raganything.service.models import TaskStatus

CHUNKS = [
    {"chunk_index": 0, "content": "甲", "file_source": "kb=kb1|doc=doc1|tenant=t1|chunk=0|"},
]

PARAMS = {
    "document_id": "doc1",
    "knowledge_base_id": "kb1",
    "ontology_schema": {"entity_types": [], "relation_types": []},
    "schema_version": 7,
}


def _get_handler():
    mod_name = "atomic_rag_server_v2_a18"
    if mod_name in sys.modules:
        return sys.modules[mod_name].handle_retry_ontology_extract
    spec = importlib.util.spec_from_file_location(
        mod_name,
        os.path.join(os.path.dirname(__file__), "..", "..", "atomic-rag-server-v2.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.handle_retry_ontology_extract


class _FakeCreateResult:
    task_id = "new-task-1"


class _FakeTaskManager:
    def __init__(self, tasks=None):
        self._tasks = {t.task_id: t for t in (tasks or [])}
        self.created = None

    async def create(self, req, tenant_id, idempotency_key=None):
        self.created = {"req": req, "tenant_id": tenant_id, "key": idempotency_key}
        return _FakeCreateResult()


class _ExistingTask:
    """只带 handler 复用分支会读的字段（避免 MagicMock 的松散断言）。"""

    def __init__(self, status):
        self.status = status
        self.task_id = "existing-1"
        self.idempotency_key = "ontology:t1:kb1:doc1:7"
        self.content_generation = 0


async def test_handler_passes_engine_chunks_into_create_request():
    handler = _get_handler()
    tm = _FakeTaskManager()
    resp = await handler(dict(PARAMS, engine_chunks=CHUNKS), tenant_id="t1", task_manager=tm)
    assert resp["data"] == {"status": "queued", "task_id": "new-task-1"}
    req = tm.created["req"]
    assert req.engine_chunks == CHUNKS
    assert req.execution_mode == "ontology_only"
    assert req.force_retry is False


async def test_handler_defaults_empty_engine_chunks():
    handler = _get_handler()
    tm = _FakeTaskManager()
    await handler(dict(PARAMS), tenant_id="t1", task_manager=tm)
    assert tm.created["req"].engine_chunks == []


async def test_handler_inflight_reuse_logs_warning(caplog):
    """I3：非终态在途复用 ⇒ 本次 engine_chunks 被丢弃，必须留声。"""
    handler = _get_handler()
    tm = _FakeTaskManager([_ExistingTask(TaskStatus.PROCESSING)])
    with caplog.at_level("WARNING"):
        resp = await handler(dict(PARAMS, engine_chunks=CHUNKS), tenant_id="t1", task_manager=tm)
    assert resp["data"]["task_id"] == "existing-1"
    assert tm.created is None  # 复用了在途任务，未新建
    assert any(
        "ontology:t1:kb1:doc1:7" in rec.getMessage() and "engine_chunks" in rec.getMessage()
        for rec in caplog.records
    )
    # 留声须足以事后区分「同世代重叠」与「跨世代复用」并定位被复用任务
    assert any(
        "dropped=1" in rec.getMessage() and "reused_task=existing-1" in rec.getMessage()
        for rec in caplog.records
    )


async def test_handler_inflight_reuse_without_chunks_no_warning(caplog):
    """负例：不带 engine_chunks 的复用不触发丢弃留声（门控反向）。"""
    handler = _get_handler()
    tm = _FakeTaskManager([_ExistingTask(TaskStatus.PROCESSING)])
    with caplog.at_level("WARNING"):
        resp = await handler(dict(PARAMS), tenant_id="t1", task_manager=tm)
    assert resp["data"]["task_id"] == "existing-1"
    assert tm.created is None
    assert not any("被丢弃" in rec.getMessage() for rec in caplog.records)


async def test_handler_new_task_no_discard_warning(caplog):
    """负例：正常新建路径不得出现丢弃留声（防留声被挪出复用分支）。"""
    handler = _get_handler()
    tm = _FakeTaskManager()
    with caplog.at_level("WARNING"):
        await handler(dict(PARAMS, engine_chunks=CHUNKS), tenant_id="t1", task_manager=tm)
    assert tm.created is not None
    assert not any("被丢弃" in rec.getMessage() for rec in caplog.records)
