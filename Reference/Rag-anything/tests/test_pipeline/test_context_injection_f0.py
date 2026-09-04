"""F0 上下文注入链路修复（docs/image-reference-accuracy-fix-plan.md §20）回归单测。

验证三处修复：
- F0-1：MultimodalStage._process_one 调 generate_description_only 传 item_info=item
- F0-2：MultimodalStage.execute 把 ctx.content_list 设为各 processor 的 content_source
- F0-3：_extract_page_context 的 original 兜底引用 current_item_info（修复前是
  未定义的 current_item，item_info 无 page_idx 时 NameError）

并验证修复后的选择链：context 非空 → _pick_prompt 选中 with_context 变体。
所有断言不发起任何真实网络/VLM 调用。
"""

import asyncio
import types

import pytest

from raganything.modalprocessors import (
    BaseModalProcessor,
    ContextConfig,
    ContextExtractor,
    _pick_prompt,
)
from raganything.pipeline.base import PipelineContext, PipelineServices
from raganything.pipeline.stages import MultimodalStage
from raganything.pipeline_mode import PipelineMode

CONTENT_LIST = [
    {"page_idx": 1, "type": "text", "text": "第一页文本：岭南画派概述"},
    {"page_idx": 2, "type": "text", "text": "第二页文本：高剑父花瓜鱼蟹四屏"},
    {"page_idx": 3, "type": "text", "text": "第三页文本：居廉写生启蒙"},
]


class _FakeImageProc:
    """模拟 image processor：实现 set_content_source 并记录调用参数。"""

    def __init__(self) -> None:
        self.content_source = None
        self.content_format = "auto"
        self.calls = []

    def set_content_source(self, content_source, content_format="auto"):
        self.content_source = content_source
        self.content_format = content_format

    async def generate_description_only(
        self,
        modal_content=None,
        content_type=None,
        item_info=None,
        prompt_overrides=None,
    ):
        self.calls.append({"modal_content": modal_content, "item_info": item_info})
        return "desc", {}


@pytest.mark.asyncio
async def test_stage_passes_item_info_and_sets_content_source():
    """F0-1 + F0-2：execute 设置 content_source，_process_one 传 item_info=item。"""
    proc = _FakeImageProc()
    services = PipelineServices(
        config=types.SimpleNamespace(),
        lightrag=None,
        doc_parser=None,
        modal_processors={"image": proc},
    )
    ctx = PipelineContext(
        file_path="/tmp/fake.pdf", file_name="fake.pdf", content_list=CONTENT_LIST
    )
    img_item = {"type": "image", "page_idx": 3, "img_path": "/tmp/x.jpg", "index": 0}
    ctx.multimodal_items = [img_item]
    stage = MultimodalStage(PipelineMode.STANDALONE)
    result = await stage.execute(ctx, services)

    # F0-2：content_source 已设为完整 content_list，格式 minerU
    assert proc.content_source is CONTENT_LIST
    assert proc.content_format == "minerU"
    # F0-1：item_info 传的是 item 本身（含 page_idx 供 context 提取）
    assert len(proc.calls) == 1
    assert proc.calls[0]["item_info"] is img_item
    assert len(result.multimodal_results) == 1


def test_extract_page_context_original_fallback_no_nameerror():
    """F0-3：item_info 无 page_idx 时走 original 兜底，不再 NameError。"""
    extractor = ContextExtractor(ContextConfig(context_window=2))
    context = extractor._extract_page_context(
        CONTENT_LIST, {"original": {"page_idx": 2}}
    )
    # 当前页(page 2)文本无前缀，前后页带 [Page N] 前缀
    assert "第二页文本" in context
    assert "[Page 1]" in context
    assert "[Page 3]" in context


def test_processor_context_chain_and_prompt_selection():
    """§20.5 完整选择链：content_source + item_info → context 非空 → with_context。"""
    proc = object.__new__(BaseModalProcessor)  # 跳过需真实 LightRAG 的 __init__
    proc.context_extractor = ContextExtractor(ContextConfig(context_window=2))
    proc.content_source = CONTENT_LIST
    proc.content_format = "minerU"

    context = proc._get_context_for_item({"page_idx": 2})
    assert context  # 修复前 content_source=None → 恒 ""
    assert "[Page 1]" in context and "[Page 3]" in context

    picked = _pick_prompt(None, "vision_prompt", bool(context), "BASE", "WITH_CTX")
    assert picked == "WITH_CTX"  # context 非空 → 选 with_context 变体
