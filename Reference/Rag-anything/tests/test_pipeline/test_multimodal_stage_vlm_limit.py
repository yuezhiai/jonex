"""MultimodalStage 双层信号量（§image-refs 排查衍生，JONEX_CHANGES §39）单元测试。

验证「VLM 独立限流」方案 B：

- image 项只占 VLM 信号量（config.max_parallel_vlm / env MAX_PARALLEL_VLM），
  不占通用信号量（直连远端 VLM，不消耗 llm-gateway RPM）；
- video 项两层信号量都占（关键帧打 VLM + MapReduce 打 LLM）；
- table / equation / audio 等 LLM 快任务只占通用信号量
  （config.max_parallel_multimodal），不受 VLM 小并发拖累；
- config 缺 max_parallel_vlm 字段时回退 max_parallel_multimodal，两者都缺
  回退默认 2。

并发观测用同步 _Recorder：进入/退出之间无 await，所有记录都在同一事件
循环线程内原子完成，因此无竞态。测试注入模拟 processor（generate_
description_only 记录后短暂睡眠），不发起任何真实网络调用。
"""

import asyncio
import types
from typing import Dict, List, Optional, Set, Tuple

import pytest

from raganything.pipeline.base import PipelineContext, PipelineServices
from raganything.pipeline.stages import MultimodalStage
from raganything.pipeline_mode import PipelineMode


class _Recorder:
    """同步并发记录器：active/max_active/overlaps 全部在无 await 区间更新。"""

    def __init__(self) -> None:
        self.active: Dict[str, int] = {}
        self.max_active: Dict[str, int] = {}
        self.overlaps: Set[Tuple[str, str]] = set()

    def enter(self, kind: str) -> None:
        for other in self.active:
            self.overlaps.add(tuple(sorted((kind, other))))
        self.active[kind] = self.active.get(kind, 0) + 1
        self.max_active[kind] = max(
            self.max_active.get(kind, 0), self.active[kind]
        )

    def exit(self, kind: str) -> None:
        self.active[kind] = max(0, self.active.get(kind, 0) - 1)
        if self.active[kind] == 0:
            self.active.pop(kind, None)


class _Proc:
    """模拟 modal processor：记录并发峰值后短暂睡眠，返回固定描述。"""

    def __init__(self, recorder: _Recorder, kind: str) -> None:
        self._recorder = recorder
        self._kind = kind

    async def generate_description_only(
        self,
        modal_content=None,
        content_type=None,
        item_info=None,
        prompt_overrides=None,
    ):
        self._recorder.enter(self._kind)
        try:
            await asyncio.sleep(0.05)
            return f"desc-{self._kind}", {}
        finally:
            self._recorder.exit(self._kind)


async def _run_stage(config, items) -> Tuple[_Recorder, List[Optional[dict]]]:
    """构造 ctx/services 并执行 MultimodalStage（STANDALONE 模式）。"""
    recorder = _Recorder()
    kinds = {"image", "table", "video"}
    processors = {k: _Proc(recorder, k) for k in kinds}
    services = PipelineServices(
        config=config,
        lightrag=None,
        doc_parser=None,
        modal_processors=processors,
    )
    ctx = PipelineContext(file_path="/tmp/fake.pdf", file_name="fake.pdf")
    ctx.multimodal_items = items
    stage = MultimodalStage(PipelineMode.STANDALONE)
    result = await stage.execute(ctx, services)
    return recorder, result.multimodal_results


@pytest.mark.asyncio
async def test_image_isolated_from_general_semaphore():
    """image 只吃 VLM 信号量：通用=1 时图片仍可 2 并发，且与表格同时进行。"""
    config = types.SimpleNamespace(max_parallel_multimodal=1, max_parallel_vlm=2)
    items = [{"type": "image", "index": i} for i in range(3)]
    items.append({"type": "table", "index": 3})
    recorder, results = await _run_stage(config, items)

    assert recorder.max_active["image"] == 2
    assert recorder.max_active["table"] == 1
    # 图片不受通用信号量（=1）约束——若共享，image+table 不可能同帧运行
    assert ("image", "table") in recorder.overlaps
    assert len(results) == 4


@pytest.mark.asyncio
async def test_video_respects_both_semaphores():
    """video 两层信号量都吃：通用=1 时视频并发被压到 1。"""
    config = types.SimpleNamespace(max_parallel_multimodal=1, max_parallel_vlm=2)
    items = [{"type": "video", "index": i} for i in range(3)]
    recorder, results = await _run_stage(config, items)

    assert recorder.max_active["video"] == 1
    assert len(results) == 3


@pytest.mark.asyncio
async def test_video_limited_by_vlm():
    """video 并发受 VLM 信号量约束：通用=10 时仍不超过 vlm=2。"""
    config = types.SimpleNamespace(max_parallel_multimodal=10, max_parallel_vlm=2)
    items = [{"type": "video", "index": i} for i in range(3)]
    recorder, results = await _run_stage(config, items)

    assert recorder.max_active["video"] == 2
    assert len(results) == 3


@pytest.mark.asyncio
async def test_tables_unaffected_by_tight_vlm_cap():
    """LLM 快任务不受 VLM 小并发拖累：vlm=1 时表格仍可 4 并发。"""
    config = types.SimpleNamespace(max_parallel_multimodal=4, max_parallel_vlm=1)
    items = [{"type": "table", "index": i} for i in range(4)]
    items.append({"type": "image", "index": 4})
    recorder, results = await _run_stage(config, items)

    assert recorder.max_active["table"] == 4
    assert recorder.max_active["image"] == 1
    assert ("image", "table") in recorder.overlaps
    assert len(results) == 5


@pytest.mark.asyncio
async def test_fallback_missing_vlm_field_uses_general_value():
    """config 缺 max_parallel_vlm 字段时回退 max_parallel_multimodal（=5），
    4 张图片可全并发——证明没有隐式 2 上限。"""
    config = types.SimpleNamespace(max_parallel_multimodal=5)
    items = [{"type": "image", "index": i} for i in range(4)]
    recorder, results = await _run_stage(config, items)

    assert recorder.max_active["image"] == 4
    assert len(results) == 4


@pytest.mark.asyncio
async def test_fallback_default_two_when_both_missing():
    """config 两个字段都缺时回退默认 2（与原嵌入模式兜底口径一致）。"""
    config = types.SimpleNamespace()
    items = [{"type": "image", "index": i} for i in range(4)]
    recorder, results = await _run_stage(config, items)

    assert recorder.max_active["image"] == 2
    assert len(results) == 4
