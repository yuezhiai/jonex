"""[A18] engine_chunks 随 CreateTaskRequest / TaskInfo 承载（落盘任务 JSON）。"""
from raganything.service.models import CreateTaskRequest, TaskInfo

CHUNKS = [
    {"chunk_index": 0, "content": "甲", "file_source": "kb=k1|doc=d1|tenant=t1|chunk=0|"},
    {"chunk_index": 1, "content": "乙", "file_source": "kb=k1|doc=d1|tenant=t1|chunk=1|"},
]


def _make_task(**overrides):
    base = dict(task_id="task-1", tenant_id="t1", name="doc.md", file_path="/tmp/doc.md")
    base.update(overrides)
    return TaskInfo(**base)


def test_create_task_request_carries_engine_chunks():
    req = CreateTaskRequest(file_path="/tmp/doc.md", engine_chunks=CHUNKS)
    assert req.engine_chunks == CHUNKS


def test_task_info_defaults_empty_engine_chunks():
    assert _make_task().engine_chunks == []


def test_task_info_roundtrips_engine_chunks_through_json():
    task = _make_task(engine_chunks=CHUNKS)
    dumped = task.model_dump(mode="json")
    assert dumped["engine_chunks"] == CHUNKS
    assert TaskInfo.model_validate(dumped).engine_chunks == CHUNKS
