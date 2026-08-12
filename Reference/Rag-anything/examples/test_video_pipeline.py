#!/usr/bin/env python
"""
完整视频处理链路测试 — vLLM Qwen2.5-VL-7B + LM Studio Embedding + Whisper ASR

服务端点:
  - LLM+VLM:    http://172.16.13.125:8000/v1  (vLLM OpenAI-compatible)
                 Qwen2.5-VL-7B — 文本 + 视觉，无 thinking 模式
  - Embedding:  http://localhost:1234/v1 (LM Studio)
                 text-embedding-embeddinggemma-300m (dim=768)
  - ASR/Whisper: http://172.16.13.125:9090/v1
                 large-v3 (openai_compatible)

Usage:
    python examples/test_video_pipeline.py
    python examples/test_video_pipeline.py "path/to/other_video.mp4"
"""

import asyncio
import base64
import os
import sys
import time
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc, logger
from raganything import RAGAnything, RAGAnythingConfig

# ── 固定配置 ──────────────────────────────────────────────────────────
VIDEO_PATH = r"C:\work\项目文件\DataAI\dataset\DM_20260526140158_001.mp4"
WORKING_DIR = "./rag_storage_video_test"

# vLLM (LLM + VLM, OpenAI-compatible)
VLLM_BASE = "http://172.16.13.125:8000/v1"
VLLM_MODEL = "/home/yuexi/Qwen2.5-VL-7B"
VLLM_KEY = "not-needed"

# LM Studio 本地 Embedding
EMBEDDING_BASE = "http://localhost:1234/v1"
EMBEDDING_MODEL = "text-embedding-embeddinggemma-300m"
EMBEDDING_DIM = 768

# Whisper ASR
WHISPER_BASE = "http://172.16.13.125:9090/v1"
WHISPER_MODEL = "large-v3"
WHISPER_KEY = "not-needed"

# VLM 控制
_vlm_cfg = {"max_frames": 10}
VLM_TIMEOUT = 120
# ──────────────────────────────────────────────────────────────────────


def check_services():
    """启动前探测所有依赖服务"""
    import requests

    results = {}

    # 1. vLLM
    try:
        r = requests.get(f"{VLLM_BASE}/models", timeout=10)
        results["vllm"] = (r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:
        results["vllm"] = (False, str(e))

    # 2. Embedding (LM Studio)
    try:
        r = requests.post(
            f"{EMBEDDING_BASE}/embeddings",
            json={"model": EMBEDDING_MODEL, "input": "test"},
            timeout=30,
        )
        ok = r.status_code == 200 and "data" in r.json()
        results["embedding"] = (ok, f"HTTP {r.status_code}")
    except Exception as e:
        results["embedding"] = (False, str(e))

    # 3. Whisper/ASR
    try:
        r = requests.post(
            f"{WHISPER_BASE}/audio/transcriptions",
            data={"model": WHISPER_MODEL, "language": "zh"},
            timeout=10,
        )
        results["whisper"] = (r.status_code in (400, 422), f"HTTP {r.status_code}")
    except Exception as e:
        results["whisper"] = (False, str(e))

    return results


# ── LLM 函数 (标准 OpenAI 兼容，无 workaround) ──────────────────────

def make_llm_func():
    """标准 openai_complete_if_cache，vLLM 完美兼容。"""

    def _llm_func(prompt, system_prompt=None, history_messages=None, **kwargs):
        return openai_complete_if_cache(
            VLLM_MODEL,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            api_key=VLLM_KEY,
            base_url=VLLM_BASE,
            **kwargs,
        )

    return _llm_func


# ── VLM 函数 (base64 图片，OpenAI 兼容格式) ─────────────────────────

def make_vlm_func(max_frames: int = 10):
    """VLM via vLLM OpenAI-compatible API with base64 images."""
    call_count = [0]

    async def _vlm_func(image_path: str, prompt: str) -> str:
        import time as _time
        call_count[0] += 1
        idx = call_count[0]

        if idx > max_frames:
            logger.info(f"VLM frame {idx}: limit ({max_frames}), mock")
            return f"(mock) Keyframe {idx}."

        # Read + encode image
        t0 = _time.time()
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.error(f"VLM frame {idx}: read error: {e}")
            return f"(error) Read failed for keyframe {idx}."
        read_ms = (_time.time() - t0) * 1000

        t_api = _time.time()
        try:
            result = await openai_complete_if_cache(
                VLLM_MODEL,
                "",  # prompt not used when messages kwarg is present
                api_key=VLLM_KEY,
                base_url=VLLM_BASE,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    ],
                }],
                max_tokens=512,
            )
            api_ms = (_time.time() - t_api) * 1000
            if result and result.strip():
                logger.info(
                    f"VLM frame {idx}: {len(result)} chars "
                    f"(read={read_ms:.0f}ms, api={api_ms/1000:.1f}s)"
                )
                return result
            else:
                logger.warning(f"VLM frame {idx}: empty (api={api_ms/1000:.1f}s)")
                return f"(mock) Keyframe {idx}."
        except Exception as e:
            logger.error(f"VLM frame {idx}: error: {e}")
            return f"(error) VLM failed for keyframe {idx}."

    _vlm_func.vlm_model = VLLM_MODEL
    _vlm_func.call_count = call_count
    return _vlm_func


# ── 主流程 ───────────────────────────────────────────────────────────

async def process_video(file_path: str):
    """完整视频处理链路"""

    config = RAGAnythingConfig(
        working_dir=WORKING_DIR,
        enable_video_processing=True,
        enable_audio_processing=True,
        video_keyframe_interval=10,
        video_max_frames=30,
        video_chunk_token_size=600,
        video_summarize_batch_size=8,
        video_summarize_max_batches=20,
        max_parallel_vlm=1,
        vlm_timeout=VLM_TIMEOUT,
        # ASR
        asr_binding="openai_compatible",
        asr_model=WHISPER_MODEL,
        asr_base_url=WHISPER_BASE,
        asr_api_key=WHISPER_KEY,
        audio_asr_timeout=600,
        max_parallel_asr=1,
        # 只测视频
        enable_image_processing=False,
        enable_table_processing=False,
        enable_equation_processing=False,
    )

    llm_func = make_llm_func()
    vlm_func = make_vlm_func(max_frames=_vlm_cfg["max_frames"])

    embedding_func = EmbeddingFunc(
        embedding_dim=EMBEDDING_DIM,
        max_token_size=8192,
        func=partial(
            openai_embed.func,
            model=EMBEDDING_MODEL,
            api_key="lm-studio",
            base_url=EMBEDDING_BASE,
        ),
    )

    rag = RAGAnything(
        config=config,
        llm_model_func=llm_func,
        vlm_model_func=vlm_func,
        embedding_func=embedding_func,
        lightrag_kwargs={
            "max_extract_input_tokens": 8192,
            "summary_context_size": 8192,
        },
    )

    logger.info("=" * 60)
    logger.info("Video Pipeline Test — vLLM Qwen2.5-VL-7B")
    logger.info(f"  Video:      {Path(file_path).name}")
    logger.info(f"  LLM/VLM:    {VLLM_MODEL.split('/')[-1]} @ {VLLM_BASE}")
    logger.info(f"  Embedding:  {EMBEDDING_MODEL} (dim={EMBEDDING_DIM}) @ {EMBEDDING_BASE}")
    logger.info(f"  ASR:        {WHISPER_MODEL} @ {WHISPER_BASE}")
    logger.info("=" * 60)

    t0 = time.time()

    with rag:
        logger.info("\n[Step 1/3] Processing video...")
        await rag.process_document_complete(file_path)
        elapsed = time.time() - t0
        logger.info(f"[Step 1/3] Done in {elapsed:.1f}s "
                     f"(VLM calls: {vlm_func.call_count[0]})")

        logger.info("\n" + "=" * 60)
        logger.info("[Step 2/3] Queries...")
        logger.info("=" * 60)

        queries = [
            "这个视频的主要内容是什么？",
            "视频中提到了哪些关键话题或场景？",
        ]
        for i, q in enumerate(queries, 1):
            logger.info(f"\n  Query {i}: {q}")
            try:
                result = await rag.aquery(q, mode="hybrid")
                logger.info(f"  Answer: {result}")
            except Exception as e:
                logger.error(f"  Query failed: {e}")

        logger.info("\n" + "=" * 60)
        logger.info("[Step 3/3] Total: %.1fs" % (time.time() - t0))
        logger.info("=" * 60)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Video pipeline test (vLLM)")
    parser.add_argument("file_path", nargs="?", default=VIDEO_PATH)
    parser.add_argument("--mock-vlm", action="store_true")
    parser.add_argument("--max-vlm-frames", type=int, default=_vlm_cfg["max_frames"])
    args = parser.parse_args()

    _vlm_cfg["max_frames"] = 0 if args.mock_vlm else args.max_vlm_frames

    print("=" * 60)
    print("1. Service Connectivity Check")
    print("=" * 60)
    svc = check_services()
    for name, (ok, detail) in svc.items():
        print(f"  {'[OK]' if ok else '[FAIL]'} {name:16s}  {detail}")
    if not all(v[0] for v in svc.values()):
        print("\n[ABORT] Services unreachable.")
        sys.exit(1)

    video_path = Path(args.file_path)
    if not video_path.exists():
        print(f"\n[ABORT] File not found: {args.file_path}")
        sys.exit(1)
    print(f"\n  Video: {video_path.name} ({video_path.stat().st_size / 1024 / 1024:.1f} MB)")
    mf = _vlm_cfg["max_frames"]
    print(f"  VLM:   {'MOCKED' if mf == 0 else f'REAL (max {mf} frames)'}")

    print("\n" + "=" * 60)
    print("2. Starting Pipeline")
    print("=" * 60)
    asyncio.run(process_video(str(video_path)))
    print("\nDone!")


if __name__ == "__main__":
    main()
