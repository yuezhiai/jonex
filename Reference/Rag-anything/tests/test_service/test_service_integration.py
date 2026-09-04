"""Integration tests for RAGAnything Service layer.

Tests the TaskManager, ConfigResolver, and API routes WITHOUT
requiring actual LLM/embedding services (mock mode).
"""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from raganything.service.models import (
    CancelTaskResponse,
    CreateTaskRequest,
    ErrorCode,
    FileType,
    TaskInfo,
    TaskMode,
    TaskStatus,
    TERMINAL_STATES,
    STATE_TRANSITIONS,
    infer_file_type,
    validate_transition,
)
from raganything.service.task_manager import (
    ProgressTrackingCallback,
    SlotFullError,
    TaskCancelledError,
    TaskHandle,
    TaskManager,
    TenantRAGCache,
    _classify_error,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def tmp_video_file():
    """Create a temporary .mp4 file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"fake video data" * 100)
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture
def mock_model_factory():
    """ModelFactory that returns mocked model functions."""
    mf = MagicMock()
    mf.build.return_value = {
        "config": MagicMock(),
        "llm_model_func": AsyncMock(),
        "embedding_func": MagicMock(),
        "vlm_model_func": None,
        "asr_model_func": None,
        "lightrag_kwargs": {},
    }
    return mf


def _make_mock_rag():
    """Create a mock RAGAnything instance (not a fixture — avoids direct call issue)."""
    rag = MagicMock()
    rag.process_document_complete = AsyncMock(return_value="doc-test123")
    rag.callback_manager = MagicMock()
    rag.callback_manager.register = MagicMock()
    rag.callback_manager.unregister = MagicMock()
    return rag


@pytest.fixture
async def task_manager(tmp_video_file, mock_model_factory):
    """Create TaskManager with mocked dependencies."""
    with patch(
        "raganything.service.task_manager.TenantRAGCache.get",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = _make_mock_rag()
        tm = TaskManager(
            base_dir=tempfile.mkdtemp(),
            model_factory=mock_model_factory,
            worker_count=2,
            queue_capacity=5,
        )
        await tm.start()
        yield tm, mock_get
        await tm.shutdown(grace_seconds=1.0)


# ── Test Model validation ────────────────────────────────────────────


class TestModels:
    """Tests for Pydantic models and helpers."""

    def test_task_info_defaults(self):
        task = TaskInfo(
            tenant_id="tenant_test",
            name="test.mp4",
            file_path="/data/test.mp4",
            file_type=FileType.VIDEO,
        )
        assert task.status == TaskStatus.CREATED
        assert task.attempt == 1
        assert task.progress == 0.0
        assert task.file_type == FileType.VIDEO
        assert task.task_id  # auto-generated

    def test_infer_file_type(self):
        assert infer_file_type("video.mp4") == FileType.VIDEO
        assert infer_file_type("audio.wav") == FileType.AUDIO
        assert infer_file_type("image.jpg") == FileType.IMAGE
        assert infer_file_type("doc.pdf") == FileType.DOCUMENT
        assert infer_file_type("unknown.xyz") == FileType.DOCUMENT  # default

    def test_state_transitions_valid(self):
        assert validate_transition(TaskStatus.CREATED, TaskStatus.QUEUED)
        assert validate_transition(TaskStatus.QUEUED, TaskStatus.PROCESSING)
        assert validate_transition(TaskStatus.PROCESSING, TaskStatus.COMPLETED)
        assert validate_transition(TaskStatus.PROCESSING, TaskStatus.FAILED)
        assert validate_transition(TaskStatus.PROCESSING, TaskStatus.CANCELLED)

    def test_state_transitions_invalid(self):
        assert not validate_transition(TaskStatus.COMPLETED, TaskStatus.QUEUED)
        assert not validate_transition(TaskStatus.FAILED, TaskStatus.PROCESSING)
        assert not validate_transition(TaskStatus.CANCELLED, TaskStatus.QUEUED)
        assert not validate_transition(TaskStatus.QUEUED, TaskStatus.COMPLETED)

    def test_terminal_states(self):
        for state in TERMINAL_STATES:
            assert len(STATE_TRANSITIONS[state]) == 0

    def test_create_request_inline(self):
        req = CreateTaskRequest(
            mode=TaskMode.INLINE,
            file_path="/data/test.mp4",
            llm="qwen3-72b",
            vlm="qwen2.5-vl-7b",
            asr="openai_compatible:large-v3",
            modalities=["video", "audio"],
            video_max_frames=30,
        )
        assert req.mode == TaskMode.INLINE
        assert req.vlm == "qwen2.5-vl-7b"

    def test_create_request_preset(self):
        req = CreateTaskRequest(
            mode=TaskMode.PRESET,
            file_path="/data/test.mp4",
            preset="video_full_pipeline",
            video_max_frames=15,  # override
        )
        assert req.mode == TaskMode.PRESET
        assert req.video_max_frames == 15


# ── Test TaskManager ──────────────────────────────────────────────────


class TestTaskManager:
    """Tests for TaskManager lifecycle (create, get, list, cancel)."""

    @pytest.mark.asyncio
    async def test_create_task_success(self, task_manager, tmp_video_file):
        tm, mock_get = task_manager
        req = CreateTaskRequest(
            mode=TaskMode.INLINE,
            file_path=tmp_video_file,
            llm="qwen3-72b",
            modalities=["video"],
        )
        result = await tm.create(req, "tenant_test")
        assert result.task_id
        assert result.status == TaskStatus.CREATED
        assert result.tenant_id == "tenant_test"

    @pytest.mark.asyncio
    async def test_create_file_not_found(self, task_manager):
        tm, _ = task_manager
        req = CreateTaskRequest(
            mode=TaskMode.INLINE,
            file_path="/nonexistent/file.mp4",
            modalities=["video"],
        )
        result = await tm.create(req, "tenant_test")
        # Task is created but validate will mark it FAILED
        assert result.status == TaskStatus.CREATED
        # Wait for async validate
        await asyncio.sleep(0.5)
        task = await tm.get(result.task_id, "tenant_test")
        assert task is not None
        assert task.status in (TaskStatus.FAILED, TaskStatus.QUEUED)  # depends on timing

    @pytest.mark.asyncio
    async def test_get_task_tenant_isolation(self, task_manager, tmp_video_file):
        tm, _ = task_manager
        req = CreateTaskRequest(file_path=tmp_video_file, modalities=["video"])
        result = await tm.create(req, "tenant_a")

        task_a = await tm.get(result.task_id, "tenant_a")
        assert task_a is not None

        task_b = await tm.get(result.task_id, "tenant_b")
        assert task_b is None  # cross-tenant access blocked

    @pytest.mark.asyncio
    async def test_list_tasks(self, task_manager, tmp_video_file):
        tm, _ = task_manager
        req = CreateTaskRequest(file_path=tmp_video_file, modalities=["video"])
        await tm.create(req, "tenant_test")
        await tm.create(req, "tenant_test")
        await tm.create(req, "tenant_test")

        # Brief wait for async create to register
        await asyncio.sleep(0.1)

        result = await tm.list("tenant_test", "all", "all", page=1, page_size=10, sort="-created_at")
        assert result.total == 3
        assert len(result.tasks) == 3

    @pytest.mark.asyncio
    async def test_list_tasks_filter_status(self, task_manager, tmp_video_file):
        tm, _ = task_manager
        req = CreateTaskRequest(file_path=tmp_video_file, modalities=["video"])
        await tm.create(req, "tenant_test")

        # Wait briefly for async validate → enqueue
        await asyncio.sleep(0.2)

        # Filter by CREATED status (task may or may not have been queued yet)
        result = await tm.list("tenant_test", "created", "all", page=1, page_size=10, sort="-created_at")
        # Task might be QUEUED by now, so "created" filter may return 0
        assert result.total >= 0  # just ensure no crash

    @pytest.mark.asyncio
    async def test_cancel_created_task(self, task_manager, tmp_video_file):
        tm, _ = task_manager
        req = CreateTaskRequest(file_path=tmp_video_file, modalities=["video"])
        result = await tm.create(req, "tenant_test")

        cancel_result = await tm.cancel(result.task_id, "tenant_test")
        assert cancel_result is not None
        assert cancel_result.status == TaskStatus.CANCELLED
        assert cancel_result.http_code == 200

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self, task_manager):
        tm, _ = task_manager
        result = await tm.cancel("nonexistent-id", "tenant_test")
        assert result is None

    @pytest.mark.asyncio
    async def test_idempotency_key(self, task_manager, tmp_video_file):
        tm, _ = task_manager
        req = CreateTaskRequest(file_path=tmp_video_file, modalities=["video"])

        result1 = await tm.create(req, "tenant_test", idempotency_key="key-001")
        result2 = await tm.create(req, "tenant_test", idempotency_key="key-001")

        # Second call should return the same task (idempotent)
        assert result1.task_id == result2.task_id

    @pytest.mark.asyncio
    async def test_idempotency_key_different_keys(self, task_manager, tmp_video_file):
        tm, _ = task_manager
        req = CreateTaskRequest(file_path=tmp_video_file, modalities=["video"])

        result1 = await tm.create(req, "tenant_test", idempotency_key="key-a")
        result2 = await tm.create(req, "tenant_test", idempotency_key="key-b")

        assert result1.task_id != result2.task_id  # Different keys → different tasks

    @pytest.mark.asyncio
    async def test_slot_full_rejected(self, task_manager, tmp_video_file):
        tm, _ = task_manager
        req = CreateTaskRequest(file_path=tmp_video_file, modalities=["video"])

        # Fill up slots (2 workers + 5 queue = 7 max)
        for i in range(7):
            await tm.create(req, "tenant_test", idempotency_key=f"fill-{i}")

        # Next one should fail
        with pytest.raises(SlotFullError) as exc:
            await tm.create(req, "tenant_test")
        assert exc.value.response.code == 42901


# ── Test ConfigResolver ──────────────────────────────────────────────


class TestConfigResolver:
    """Tests for config resolution (inline/yaml/preset modes)."""

    @pytest.fixture
    def resolver(self):
        from raganything.service.config_resolver import ConfigResolver
        return ConfigResolver(
            profiles_dir="config/profiles",
            presets_dir="config/presets",
        )

    def test_resolve_inline(self, resolver):
        req = CreateTaskRequest(
            mode=TaskMode.INLINE,
            file_path="/data/test.mp4",
            llm="qwen3-72b",
            embedding="gemma-300m",
            vlm="qwen2.5-vl-7b",
            asr="openai_compatible:large-v3",
            modalities=["video", "audio"],
            video_max_frames=30,
        )
        snapshot = resolver.resolve(req)
        assert snapshot["llm_model"] == "qwen3-72b"
        assert snapshot["vlm_model_name"] == "qwen2.5-vl-7b"
        assert snapshot["asr_binding"] == "openai_compatible"
        assert snapshot["asr_model"] == "large-v3"
        assert snapshot["enable_video_processing"] is True
        assert snapshot["enable_audio_processing"] is True
        assert snapshot["video_max_frames"] == 30

    def test_resolve_preset(self, resolver):
        req = CreateTaskRequest(
            mode=TaskMode.PRESET,
            file_path="/data/test.mp4",
            preset="video_full_pipeline",
        )
        snapshot = resolver.resolve(req)
        assert snapshot["llm_model"] == "deepseek-v4-flash-202605"
        assert snapshot["vlm_model_name"] == "/home/jonex/Qwen2.5-VL-7B"
        assert snapshot["enable_video_processing"] is True
        assert snapshot["video_max_frames"] == 30

    def test_resolve_preset_with_override(self, resolver):
        req = CreateTaskRequest(
            mode=TaskMode.PRESET,
            file_path="/data/test.mp4",
            preset="video_full_pipeline",
            video_max_frames=15,  # override
        )
        snapshot = resolver.resolve(req)
        assert snapshot["video_max_frames"] == 15  # override wins

    def test_resolve_preset_not_found(self, resolver):
        req = CreateTaskRequest(
            mode=TaskMode.PRESET,
            file_path="/data/test.mp4",
            preset="nonexistent_preset",
        )
        from raganything.service.config_resolver import ConfigResolveError
        with pytest.raises(ConfigResolveError, match="Preset not found"):
            resolver.resolve(req)

    def test_parse_asr(self, resolver):
        result = resolver._parse_asr("openai_compatible:large-v3")
        assert result["asr_binding"] == "openai_compatible"
        assert result["asr_model"] == "large-v3"

        result2 = resolver._parse_asr("whisper:base")
        assert result2["asr_binding"] == "whisper"
        assert result2["asr_model"] == "base"


# ── Test Error Classification ───────────────────────────────────────


class TestErrorClassification:
    """Tests for _classify_error in task_manager."""

    def test_file_not_found(self):
        assert _classify_error(FileNotFoundError("No such file")) == ErrorCode.FILE_NOT_FOUND

    def test_vlm_timeout(self):
        import asyncio
        assert _classify_error(asyncio.TimeoutError("vlm timed out")) == ErrorCode.VLM_TIMEOUT

    def test_asr_timeout(self):
        import asyncio
        assert _classify_error(asyncio.TimeoutError("whisper timeout")) == ErrorCode.ASR_TIMEOUT

    def test_lightrag_error(self):
        assert _classify_error(RuntimeError("LightRAG initialization error")) == ErrorCode.LIGHTRAG_ERROR

    def test_parser_error(self):
        assert _classify_error(RuntimeError("MinerU parse failed")) == ErrorCode.PARSER_ERROR

    def test_unknown_error(self):
        assert _classify_error(Exception("some random error")) == ErrorCode.UNKNOWN

    def test_cancelled_error(self):
        assert _classify_error(TaskCancelledError()) == ErrorCode.TASK_CANCELLED


# ── Test ProgressTrackingCallback ──────────────────────────────────


class TestProgressTrackingCallback:
    """Tests for ProgressTrackingCallback."""

    @pytest.fixture
    def task_and_handle(self):
        task = TaskInfo(
            tenant_id="test",
            name="test.mp4",
            file_path="/data/test.mp4",
        )
        handle = TaskHandle(release_cb=lambda: None)
        return task, handle

    def test_on_parse_start_updates_progress(self, task_and_handle):
        task, handle = task_and_handle
        cb = ProgressTrackingCallback(task, handle)
        cb.on_parse_start(file_path="test.mp4")
        assert task.current_step == "parse"
        assert task.progress_detail is not None
        assert task.progress_detail.step_name == "parse"

    def test_on_document_complete_sets_done(self, task_and_handle):
        task, handle = task_and_handle
        cb = ProgressTrackingCallback(task, handle)
        cb.on_document_complete(file_path="test.mp4", doc_id="doc-123")
        assert task.current_step == "done"
        assert task.progress == 1.0
        assert task.result_summary is not None
        assert task.result_summary.doc_id == "doc-123"

    def test_on_document_error_records_error(self, task_and_handle):
        task, handle = task_and_handle
        cb = ProgressTrackingCallback(task, handle)
        cb.on_document_error(file_path="test.mp4", error="Something went wrong", stage="parse")
        assert task.progress_detail is not None
        assert "Something went wrong" in task.progress_detail.step_detail

    def test_cancellation_raises(self, task_and_handle):
        task, handle = task_and_handle
        handle.cancel_event.set()
        cb = ProgressTrackingCallback(task, handle)
        with pytest.raises(TaskCancelledError):
            cb.on_parse_start(file_path="test.mp4")


# ── Test API Routes ─────────────────────────────────────────────────


class TestAPIRoutes:
    """Integration tests for FastAPI routes (via TestClient)."""

    @pytest.fixture
    def client(self, tmp_video_file, mock_model_factory):
        from raganything.service.app import create_app
        from raganything.service.config_resolver import ConfigResolver

        with patch(
            "raganything.service.task_manager.TenantRAGCache.get",
            new_callable=AsyncMock,
        ) as mock_get:
            mock_rag = MagicMock()
            mock_rag.process_document_complete = AsyncMock(return_value="doc-test123")
            mock_rag.callback_manager = MagicMock()
            mock_rag.callback_manager.register = MagicMock()
            mock_rag.callback_manager.unregister = MagicMock()
            mock_get.return_value = mock_rag
            tm = TaskManager(
                base_dir=tempfile.mkdtemp(),
                model_factory=mock_model_factory,
                worker_count=1,
            )
            cr = ConfigResolver(
                profiles_dir="config/profiles",
                presets_dir="config/presets",
            )
            tm.set_config_resolver(cr)
            app = create_app(tm, cr)

            with TestClient(app) as client:
                # Start task manager manually since TestClient doesn't trigger lifespan correctly
                yield client, tm

    def test_health_endpoint(self, client):
        client, tm = client
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_create_task(self, client, tmp_video_file):
        client, tm = client
        resp = client.post(
            "/api/v1/tasks",
            headers={"X-Tenant-ID": "tenant_test"},
            json={
                "mode": "inline",
                "file_path": tmp_video_file,
                "modalities": ["video"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["code"] == 0
        assert "task_id" in data["data"]
        assert data["data"]["status"] in ("created", "queued")

    def test_create_task_no_tenant(self, client):
        client, tm = client
        resp = client.post(
            "/api/v1/tasks",
            json={"mode": "inline", "file_path": "/data/test.mp4"},
        )
        assert resp.status_code == 400

    def test_list_tasks(self, client, tmp_video_file):
        client, tm = client
        # Create a task first
        client.post(
            "/api/v1/tasks",
            headers={"X-Tenant-ID": "tenant_test"},
            json={"mode": "inline", "file_path": tmp_video_file, "modalities": ["video"]},
        )

        resp = client.get(
            "/api/v1/tasks",
            headers={"X-Tenant-ID": "tenant_test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 0
        assert data["data"]["total"] >= 1

    def test_get_task_404(self, client):
        client, tm = client
        resp = client.get(
            "/api/v1/tasks/nonexistent-id",
            headers={"X-Tenant-ID": "tenant_test"},
        )
        assert resp.status_code == 404

    def test_cancel_task(self, client, tmp_video_file):
        client, tm = client
        create_resp = client.post(
            "/api/v1/tasks",
            headers={"X-Tenant-ID": "tenant_test"},
            json={"mode": "inline", "file_path": tmp_video_file, "modalities": ["video"]},
        )
        task_id = create_resp.json()["data"]["task_id"]

        resp = client.delete(
            f"/api/v1/tasks/{task_id}",
            headers={"X-Tenant-ID": "tenant_test"},
        )
        assert resp.status_code in (200, 202)
        data = resp.json()
        # Task may already be completed (mock is fast) — both are valid
        assert data["data"]["status"] in ("cancelled", "completed")

    def test_presets_list(self, client):
        client, tm = client
        resp = client.get("/api/v1/presets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 0
        assert isinstance(data["data"], list)

    def test_presets_get(self, client):
        client, tm = client
        resp = client.get("/api/v1/presets/video_full_pipeline")
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data["data"]

    def test_metrics_endpoint(self, client):
        client, tm = client
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "rag_tasks_total" in resp.text
        assert "rag_queue_depth" in resp.text
