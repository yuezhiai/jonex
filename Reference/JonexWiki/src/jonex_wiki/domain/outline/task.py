"""目录自动触发 runner（独立 outline task，§6）。

编译发布后投递一个独立 outline task（``TaskRow`` kind=outline），由 executor
的 outline 分派执行。这里只承载「是否重生成」的判定 + 复用 ``OutlineService``
的 LLM 起草/落库路径；失败降级确定性兜底目录并让 task 标 failed（不阻塞、不回滚
bundle）。

判定语义（决策 4，按触发来源区分）：
- 无目录                       → 自动 draft（初版）；
- 目录存在 & fingerprint 变化  → source=draft 自动 draft 覆盖；source=adjust
  什么都不做（保护人工成果，stale 由读取端算）；
- 目录存在 & fingerprint 未变  → 不动（目录保持稳定）。
"""
from __future__ import annotations

import logging

from jonex_wiki.domain.outline.outline import (
    SOURCE_DRAFT,
    _catalog_snapshot,
    _fallback_topics,
    _fingerprint,
)
from jonex_wiki.domain.outline.service import OutlineService
from jonex_wiki.storage.repos import OutlineRepo

log = logging.getLogger(__name__)

_UNCLASSIFIED = "未归类"


def build_outline_runner(session_factory, settings, layout, gw):
    """对称 ``domain/compile_task.py`` 的 runner 工厂，供 executor.outline_runner 注册。"""
    repo = OutlineRepo()

    async def runner(row) -> None:
        tenant_id, kb_id = row.tenant_id, row.kb_id
        # [jonex] P3b（方案 B）：目录也是编译产物，沿用编译任务带的模型快照
        compile_model = str((row.summary or {}).get("compile_model") or "")
        service = OutlineService(
            session_factory=session_factory, gw=gw, layout=layout, settings=settings)

        pages = await service._load_catalog(tenant_id, kb_id)
        fingerprint = _fingerprint(pages)

        async with session_factory() as s:
            current = await repo.get_active(s, tenant_id, kb_id)

        if current is None:
            _set_summary(row, {"outcome": "draft", "reason": "no_outline"})
            await _auto_draft(
                               service, session_factory, repo, tenant_id, kb_id, pages, compile_model)
            return

        if (current.catalog_fingerprint or "") == fingerprint:
            _set_summary(row, {"outcome": "skip", "reason": "fingerprint_unchanged"})
            log.info("[outline] 指纹未变跳过 kb=%s version=%s", kb_id, current.outline_version)
            return

        # fingerprint 变化
        if current.source == SOURCE_DRAFT:
            _set_summary(row, {"outcome": "draft", "reason": "catalog_changed", "source": current.source})
            await _auto_draft(
                               service, session_factory, repo, tenant_id, kb_id, pages, compile_model)
            return

        # source=adjust：人工编辑保护，什么都不做；stale 由读取端算
        _set_summary(row, {"outcome": "skip", "reason": "source_adjust", "source": current.source})
        log.info(
            "[outline] source=adjust 跳过自动重生成 kb=%s version=%s（stale 由读取端算）",
            kb_id, current.outline_version,
        )

    return runner


def _set_summary(row, summary: dict) -> None:
    row.summary = {**(row.summary or {}), **summary}


async def _auto_draft(service, session_factory, repo, tenant_id, kb_id, pages,
                      compile_model: str = "") -> None:
    """自动 draft：复用 LLM 起草路径；失败降级确定性兜底目录再上抛（task 标 failed）。"""
    try:
        await service.draft(
            tenant_id, kb_id, {"model": compile_model} if compile_model else {})
        log.info("[outline] 自动起草完成 kb=%s pages=%d", kb_id, len(pages))
    except Exception as exc:  # noqa: BLE001 — 自动触发失败不阻塞 bundle，兜底后上抛
        log.exception("[outline] 自动起草失败 kb=%s，降级确定性兜底目录", kb_id)
        outline = {
            "version": 1,
            "overview": "",
            "topics": _fallback_topics(pages, _UNCLASSIFIED),
            "page_count": len(pages),
            "covered_count": len(pages),
        }
        async with session_factory() as s:
            await repo.save_new_active(
                s, tenant_id=tenant_id, kb_id=kb_id,
                source=SOURCE_DRAFT, model=None, instruction=None,
                page_count=len(pages),
                catalog_fingerprint=_fingerprint(pages),
                outline=outline,
                catalog_snapshot=_catalog_snapshot(pages),
                edited_by=None,
            )
            await s.commit()
        raise
