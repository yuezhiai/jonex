"""Unit tests for raganything.models — Phase 0: data types, drivers, registry."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from raganything.models import adapters
from raganything.models.adapters import (
    base64_caption_adapter,
    legacy_vlm_adapter,
)
from raganything.models.types import (
    BoundModel,
    ModelCapability,
    ModelContentFilterError,
    ModelDriverError,
    ModelResponse,
    ModelSpec,
    Usage,
)
from raganything.models.driver import BaseModelDriver
from raganything.models.drivers.openai import OpenAIDriver
from raganything.models.drivers.echo import EchoDriver
from raganything.models.parser import extract_openai_usage
from raganything.models.registry import BINDING_ALIASES, ModelRegistry, RegistryError


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def openai_driver():
    return OpenAIDriver()


@pytest.fixture
def echo_driver():
    return EchoDriver()


@pytest.fixture
def default_capability():
    return ModelCapability()


@pytest.fixture
def vlm_capability():
    return ModelCapability(
        supports_vision=True,
        supports_vision_base64=True,
        supports_vision_url=True,
    )


@pytest.fixture
def thinking_capability():
    return ModelCapability(supports_thinking=True)


@pytest.fixture
def base64_only_capability():
    """Model that supports base64 but NOT url images (like some local VLMs)."""
    return ModelCapability(
        supports_vision=True,
        supports_vision_base64=True,
        supports_vision_url=False,
    )


@pytest.fixture
def qwen_spec():
    return ModelSpec(
        model_id="qwen3-72b",
        binding="vllm",
        host="http://127.0.0.1:8000/v1",
        api_key="not-needed",
    )


@pytest.fixture
def vl_spec():
    return ModelSpec(
        model_id="qwen2.5-vl-7b",
        binding="vllm",
        host="http://127.0.0.1:8000/v1",
        capability=ModelCapability(supports_vision=True),
    )


@pytest.fixture
def echo_spec():
    return ModelSpec(
        model_id="echo-model",
        binding="echo",
        host="http://localhost:9999",
    )


# ── Test ModelCapability ─────────────────────────────────────────────


class TestModelCapability:
    def test_defaults_are_conservative(self):
        cap = ModelCapability()
        assert cap.supports_vision is False
        assert cap.supports_audio is False
        assert cap.supports_text is True
        assert cap.supports_thinking is False
        assert cap.max_context_tokens == 32768

    def test_vlm_capability(self):
        cap = ModelCapability(supports_vision=True)
        assert cap.supports_vision is True
        assert cap.supports_vision_base64 is True
        assert cap.supports_vision_url is True

    def test_thinking_derives_parser(self, thinking_capability):
        """SSOT: supports_thinking drives parser, no independent config."""
        assert thinking_capability.supports_thinking is True


# ── Test ModelResponse & Usage ────────────────────────────────────────


class TestModelResponse:
    def test_basic_response(self):
        r = ModelResponse(text="Hello")
        assert r.text == "Hello"
        assert r.reasoning is None
        assert r.usage is None
        assert r.tool_calls == []

    def test_with_usage(self):
        r = ModelResponse(
            text="Hello",
            usage=Usage(prompt_tokens=10, completion_tokens=5),
        )
        assert r.usage.prompt_tokens == 10
        assert r.usage.completion_tokens == 5
        assert r.usage.reasoning_tokens == 0

    def test_with_reasoning(self):
        r = ModelResponse(
            text="Answer",
            reasoning="Step 1: ...\nStep 2: ...",
        )
        assert r.text == "Answer"
        assert r.reasoning == "Step 1: ...\nStep 2: ..."

    def test_metadata_no_raw_by_default(self):
        """raw response NOT stored by default (memory safety)."""
        r = ModelResponse(text="x", metadata={"model": "test"})
        assert "raw" not in r.metadata


# ── Test BoundModel ──────────────────────────────────────────────────


class TestBoundModel:
    @pytest.mark.asyncio
    async def test_complete_delegates_to_driver(self, qwen_spec, echo_driver):
        bound = BoundModel(spec=qwen_spec, driver=echo_driver)
        resp = await bound.complete([
            {"role": "user", "content": "Hello world"},
        ])
        assert isinstance(resp, ModelResponse)
        assert "Hello world" in resp.text

    @pytest.mark.asyncio
    async def test_callable_backward_compat(self, qwen_spec, echo_driver):
        bound = BoundModel(spec=qwen_spec, driver=echo_driver)
        resp = await bound([
            {"role": "user", "content": "Test"},
        ])
        assert isinstance(resp, ModelResponse)

    def test_capability_access(self, qwen_spec, echo_driver):
        bound = BoundModel(spec=qwen_spec, driver=echo_driver)
        assert isinstance(bound.capability, ModelCapability)
        assert bound.capability.supports_vision is False


# ── Test OpenAIDriver._parse ──────────────────────────────────────────


class TestOpenAIDriverParse:
    """Owner-driver parsing — no global parser."""

    def test_parse_standard_response(self, openai_driver, qwen_spec):
        raw = {
            "choices": [{"message": {"content": "平衡力是..."}, "finish_reason": "stop"}],
            "model": "qwen3-72b",
            "usage": {"prompt_tokens": 42, "completion_tokens": 30},
        }
        resp = openai_driver._parse(raw, qwen_spec)
        assert resp.text == "平衡力是..."
        assert resp.reasoning is None
        assert resp.usage.prompt_tokens == 42
        assert resp.metadata["finish_reason"] == "stop"
        assert resp.metadata["model"] == "qwen3-72b"

    def test_parse_thinking_response(self, openai_driver):
        spec = ModelSpec(
            model_id="deepseek-r1", binding="vllm", host="http://gpu:8000/v1",
            capability=ModelCapability(supports_thinking=True),
        )
        raw = {
            "choices": [{"message": {
                "content": "最终答案",
                "reasoning_content": "让我分析这个问题...",
            }}],
        }
        resp = openai_driver._parse(raw, spec)
        assert resp.text == "最终答案"
        assert resp.reasoning == "让我分析这个问题..."

    def test_parse_thinking_model_without_thinking_flag(self, openai_driver, qwen_spec):
        """If supports_thinking=False, reasoning_content is ignored."""
        raw = {
            "choices": [{"message": {
                "content": "最终答案",
                "reasoning_content": "思考过程...",
            }}],
        }
        resp = openai_driver._parse(raw, qwen_spec)
        assert resp.text == "最终答案"
        assert resp.reasoning is None  # suppressed

    def test_parse_content_filter_raises(self, openai_driver, qwen_spec):
        raw = {
            "choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}],
        }
        with pytest.raises(ModelContentFilterError, match="Content filtered"):
            openai_driver._parse(raw, qwen_spec)

    def test_parse_string_raw(self, openai_driver, qwen_spec):
        resp = openai_driver._parse("Plain text response", qwen_spec)
        assert resp.text == "Plain text response"

    def test_parse_empty_dict(self, openai_driver, qwen_spec):
        resp = openai_driver._parse({}, qwen_spec)
        assert resp.text == ""

    def test_raw_stored_only_in_debug_mode(self, openai_driver, qwen_spec, monkeypatch):
        monkeypatch.setenv("MODEL_DEBUG_RAW", "true")
        raw = {"choices": [{"message": {"content": "x"}}], "model": "test"}
        resp = openai_driver._parse(raw, qwen_spec)
        assert "raw" in resp.metadata
        assert resp.metadata["raw"] == raw


# ── Test EchoDriver ───────────────────────────────────────────────────


class TestEchoDriver:
    @pytest.mark.asyncio
    async def test_returns_echoed_content(self, echo_driver, echo_spec):
        resp = await echo_driver.complete(
            [{"role": "user", "content": "Hello world"}],
            echo_spec,
        )
        assert "Hello world" in resp.text
        assert resp.usage is not None
        assert resp.metadata["driver"] == "EchoDriver"

    @pytest.mark.asyncio
    async def test_handles_multimodal_content(self, echo_driver, echo_spec):
        resp = await echo_driver.complete(
            [{"role": "user", "content": [
                {"type": "text", "text": "Describe this"},
                {"type": "image_url", "image_url": {"url": "file:///tmp/test.jpg"}},
            ]}],
            echo_spec,
        )
        assert "Describe this" in resp.text
        assert "[image:" in resp.text  # image URL is included as text


# ── Test normalize_images ─────────────────────────────────────────────


class TestNormalizeImages:
    def test_passthrough_text_only(self, openai_driver, default_capability):
        messages = [{"role": "user", "content": "Hello"}]
        result = openai_driver.normalize_images(messages, default_capability)
        assert result == messages

    def test_file_scheme_to_base64(self, openai_driver, vlm_capability):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # minimal JPEG header
            tmp_path = f.name

        try:
            messages = [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"file://{tmp_path}"}},
            ]}]
            result = openai_driver.normalize_images(messages, vlm_capability)
            url = result[0]["content"][0]["image_url"]["url"]
            assert url.startswith("data:image/jpeg;base64,")
        finally:
            os.unlink(tmp_path)

    def test_file_scheme_unsupported_raises(self, openai_driver, default_capability):
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "file:///tmp/test.jpg"}},
        ]}]
        with pytest.raises(ModelDriverError, match="does not support vision"):
            openai_driver.normalize_images(messages, default_capability)

    def test_data_uri_passthrough(self, openai_driver, vlm_capability):
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abcd"}},
        ]}]
        result = openai_driver.normalize_images(messages, vlm_capability)
        assert result[0]["content"][0]["image_url"]["url"] == "data:image/jpeg;base64,abcd"

    def test_data_uri_rejected_when_unsupported(self, openai_driver, base64_only_capability):
        """Http URL rejected when model only supports base64."""
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/img.jpg"}},
        ]}]
        with pytest.raises(ModelDriverError, match="does not support URL"):
            openai_driver.normalize_images(messages, base64_only_capability)

    def test_http_url_passthrough(self, openai_driver, vlm_capability):
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://cos.example.com/img.jpg"}},
        ]}]
        result = openai_driver.normalize_images(messages, vlm_capability)
        assert result[0]["content"][0]["image_url"]["url"] == "https://cos.example.com/img.jpg"


# ── Test ModelRegistry ────────────────────────────────────────────────


class TestModelRegistry:
    def test_binding_aliases(self):
        assert BINDING_ALIASES["vllm"] == "openai"
        assert BINDING_ALIASES["ollama"] == "openai"
        assert BINDING_ALIASES["lmstudio"] == "openai"

    def test_register_and_bind(self, openai_driver, qwen_spec):
        registry = ModelRegistry()
        registry.register_driver(openai_driver)
        # Manually add a spec
        registry._specs["qwen3-72b"] = qwen_spec
        bound = registry.bind("qwen3-72b")
        assert isinstance(bound, BoundModel)
        assert bound.spec.model_id == "qwen3-72b"

    def test_bind_unknown_model_raises(self, openai_driver):
        registry = ModelRegistry()
        registry.register_driver(openai_driver)
        with pytest.raises(RegistryError, match="not found in registry"):
            registry.bind("nonexistent-model")

    def test_bind_no_driver_raises(self, qwen_spec):
        registry = ModelRegistry()
        registry._specs["qwen3-72b"] = qwen_spec
        with pytest.raises(RegistryError, match="No driver registered"):
            registry.bind("qwen3-72b")

    def test_binding_alias_routing(self, echo_driver):
        """vllm binding → routed to openai driver via BINDING_ALIASES."""
        # But here we test echo driver registered under "echo"
        registry = ModelRegistry()
        registry.register_driver(echo_driver)
        spec = ModelSpec(model_id="echo-model", binding="echo", host="http://localhost")
        registry._specs["echo-model"] = spec
        bound = registry.bind("echo-model")
        assert isinstance(bound.driver, EchoDriver)

    def test_list_models_filter_by_capability(self, openai_driver):
        registry = ModelRegistry()
        registry.register_driver(openai_driver)
        registry._specs["llm"] = ModelSpec(
            model_id="llm", binding="vllm", host="http://h",
            capability=ModelCapability(supports_vision=False),
        )
        registry._specs["vlm"] = ModelSpec(
            model_id="vlm", binding="vllm", host="http://h",
            capability=ModelCapability(supports_vision=True),
        )
        vlms = registry.list_models(supports_vision=True)
        assert len(vlms) == 1
        assert vlms[0].model_id == "vlm"

    def test_parse_binding_url(self):
        binding, host = ModelRegistry._parse_binding_url(
            "vllm://http://127.0.0.1:8000/v1"
        )
        assert binding == "vllm"
        assert host == "http://127.0.0.1:8000/v1"

    def test_parse_binding_url_with_params_stripped(self):
        """Query params (e.g. ?dim=768) are stripped — belong in models.yaml."""
        binding, host = ModelRegistry._parse_binding_url(
            "lmstudio://http://localhost:1234/v1?dim=768"
        )
        assert binding == "lmstudio"
        assert host == "http://localhost:1234/v1"

    def test_resolve_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("QWEN3_72B_API_KEY", "sk-test-123")
        key = ModelRegistry._resolve_api_key("qwen3-72b", "vllm")
        assert key == "sk-test-123"

    def test_resolve_api_key_fallback_to_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback")
        key = ModelRegistry._resolve_api_key("unknown-model", "vllm")
        assert key == "sk-fallback"


# ── Test VLM adapters reasoning_effort ─────────────────────────────────
# [jonex] RAG_VLM_REASONING_EFFORT 思考深度门控回归测试。


class _SpyDriver(BaseModelDriver):
    """Spy driver：捕获 complete 收到的 kwargs，不做任何网络请求。"""

    bindings = ("spy",)

    def __init__(self):
        self.captured_kwargs = []

    def normalize_images(self, messages, cap):
        # 不读文件（file:// 指向不存在的路径），原样透传
        return messages

    async def complete(self, messages, spec, **kwargs):
        self.captured_kwargs.append(kwargs)
        return ModelResponse(text="ok")


@pytest.fixture
def spy_bound():
    spec = ModelSpec(
        model_id="spy-vlm",
        binding="spy",
        host="http://localhost",
        capability=ModelCapability(supports_vision=True),
    )
    return BoundModel(spec=spec, driver=_SpyDriver())


class TestVlmReasoningEffort:
    @pytest.mark.asyncio
    async def test_legacy_vlm_defaults_to_none(self, spy_bound, monkeypatch):
        monkeypatch.delenv("RAG_VLM_REASONING_EFFORT", raising=False)
        vlm = legacy_vlm_adapter(spy_bound)
        await vlm("/tmp/fake.jpg", "describe")
        assert spy_bound.driver.captured_kwargs[0]["reasoning_effort"] == "none"

    @pytest.mark.asyncio
    async def test_base64_caption_defaults_to_none(self, spy_bound, monkeypatch):
        monkeypatch.delenv("RAG_VLM_REASONING_EFFORT", raising=False)
        vlm = base64_caption_adapter(spy_bound)
        await vlm("describe", image_data="/9j/AAAA", system_prompt="sys")
        assert spy_bound.driver.captured_kwargs[0]["reasoning_effort"] == "none"

    @pytest.mark.asyncio
    async def test_env_override(self, spy_bound, monkeypatch):
        monkeypatch.setenv("RAG_VLM_REASONING_EFFORT", "low")
        vlm = base64_caption_adapter(spy_bound)
        await vlm("describe", image_data="/9j/AAAA")
        assert spy_bound.driver.captured_kwargs[0]["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_caller_kwarg_wins_over_env(self, spy_bound, monkeypatch):
        """调用方显式传 reasoning_effort 时不被 setdefault 覆盖。"""
        monkeypatch.setenv("RAG_VLM_REASONING_EFFORT", "low")
        vlm = base64_caption_adapter(spy_bound)
        await vlm("describe", image_data="/9j/AAAA", reasoning_effort="high")
        assert spy_bound.driver.captured_kwargs[0]["reasoning_effort"] == "high"


# ── Test extract_openai_usage ──────────────────────────────────────────


class TestExtractUsage:
    def test_extracts_standard_usage(self):
        raw = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        u = extract_openai_usage(raw)
        assert u.prompt_tokens == 10
        assert u.completion_tokens == 5

    def test_extracts_with_reasoning_tokens(self):
        raw = {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 50,
                "completion_tokens_details": {"reasoning_tokens": 40},
            }
        }
        u = extract_openai_usage(raw)
        assert u.reasoning_tokens == 40

    def test_returns_none_when_missing(self):
        assert extract_openai_usage({}) is None
        assert extract_openai_usage({"usage": None}) is None


# ── Test Error hierarchy ───────────────────────────────────────────────


class TestErrors:
    def test_error_is_exception(self):
        assert issubclass(ModelDriverError, Exception)

    def test_auth_error(self):
        err = ModelDriverError()
        assert isinstance(err, Exception)

    def test_content_filter_error_is_driver_error(self):
        assert issubclass(ModelContentFilterError, ModelDriverError)
