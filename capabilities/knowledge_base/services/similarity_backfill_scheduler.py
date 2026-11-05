"""相似签名周期回填：开关开启的 KB 里，缺签名/哨兵超时的文档分批补算。"""
from __future__ import annotations

import asyncio
import logging

from jonex_core.common.config import get_config

from .document_similarity_service import (
    DocumentSimilarityService,
    _get_scan_sem,
)

logger = logging.getLogger(__name__)
_scheduler: asyncio.Task | None = None


async def _loop() -> None:
    interval = int(get_config().DOCUMENT_SIMILARITY_BACKFILL_INTERVAL_SECONDS)
    if interval <= 0:
        logger.info("[SimilarityBackfill] 已关闭（interval<=0）")
        return
    logger.info("[SimilarityBackfill] 已启动（间隔 %.0fs）", interval)
    while True:
        try:
            # 与 READY 后扫描共用并发闸：回填不挤占即时检测的名额。
            async with _get_scan_sem():
                done = await DocumentSimilarityService().backfill_once()
            if done:
                logger.info("[SimilarityBackfill] 本轮补 %d 篇", done)
        except Exception:
            logger.warning("[SimilarityBackfill] 本轮失败", exc_info=True)
        await asyncio.sleep(interval)


async def start_scheduler() -> None:
    global _scheduler
    if _scheduler is None or _scheduler.done():
        _scheduler = asyncio.create_task(_loop())


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.cancel()
        try:
            await _scheduler
        except asyncio.CancelledError:
            pass
        _scheduler = None
