"""core 引擎 compile 分支接线（§6.3）。

旧 dev 的 allocator/page_compiler/gates/incremental/indexes 编译管线整体替换为
``LLMWikiV1Engine.compile``：manifest + ports → 引擎 → 空产出守卫 → 发布 → summary。
"""
import asyncio
import json
import logging
import shutil
import time
from types import SimpleNamespace
from sqlalchemy import select

from jonex_wiki.adapters.artifact_store import ArtifactStoreAdapter
from jonex_wiki.adapters.cancel import CancelPortAdapter
from jonex_wiki.adapters.manifest import ManifestBuilder
from jonex_wiki.adapters.model_port import ModelPortAdapter
from jonex_wiki.adapters.progress import ProgressSinkAdapter
from jonex_wiki.adapters.source_port import SourceArtifactPortAdapter
from jonex_wiki.domain.engine.compile.engine import LLMWikiV1Engine
from jonex_wiki.domain.prompts import COMPILE_MAIN_SKELETON, resolve_prompt
from jonex_wiki.kernel.errors import ConflictError, ValidationError
from jonex_wiki.kernel.runtime_env import runtime_environment
from jonex_wiki.kernel.task import TaskKind, TaskStatus
from jonex_wiki.kernel.telemetry import RuntimeTelemetryRecorder, bind_runtime_telemetry
from jonex_wiki.storage.layout import Layout
from jonex_wiki.storage.models import DocumentRow, QueryTaskRow, TaskRow
from jonex_wiki.storage.repos import DocumentRepo, TaskRepo

log = logging.getLogger(__name__)


class _StaleCompileInput(ConflictError):
    """The source or compile settings changed before publication."""

    def __init__(self, message, staging_key):
        super().__init__(message)
        self.staging_key = staging_key


async def _wait_for_parses(session_factory, tenant_id: str, kb_id: str) -> None:
    """Let an upload batch finish parsing before starting another expensive compile."""
    while True:
        async with session_factory() as s:
            pending = (await s.execute(select(TaskRow.task_id).where(
                TaskRow.tenant_id == tenant_id,
                TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.parse.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.parsing.value]),
            ).limit(1))).first()
        if pending is None:
            return
        await asyncio.sleep(0.2)


def _published_page_count(layout: Layout, tenant_id: str, kb_id: str) -> int:
    """上一版已发布 bundle 的 page_count（从 revision.json 读，不建 revisions 表）。"""
    prev_dir = layout.resolve_published(tenant_id, kb_id)
    if prev_dir is None or not (prev_dir / "revision.json").is_file():
        return 0
    try:
        rev = json.loads((prev_dir / "revision.json").read_text(encoding="utf-8"))
        return int(rev.get("page_count") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _published_source_count(layout: Layout, tenant_id: str, kb_id: str) -> int:
    """上一版已发布 bundle 的 source_count（从 revision.json 读，删除豁免用）。"""
    prev_dir = layout.resolve_published(tenant_id, kb_id)
    if prev_dir is None or not (prev_dir / "revision.json").is_file():
        return 0
    try:
        rev = json.loads((prev_dir / "revision.json").read_text(encoding="utf-8"))
        return int(rev.get("source_count") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _query_page_count(revision_dir) -> int:
    """Count the actual query projection, including readable source/table pages."""
    try:
        from jonex_wiki.domain.engine.json_artifacts import load_json_artifact
        navigation = load_json_artifact(lambda path:(revision_dir/path).read_bytes(),
            'query/navigation.json',lazy_fields={'page_index','page_graph','page_links','fact_mentions','page_source_ids'})
        if not isinstance(navigation, dict):
            return 0
        pages = navigation.get("page_index")
        from collections.abc import Mapping
        return len(pages) if isinstance(pages, Mapping) else 0
    except (OSError, ValueError, TypeError):
        return 0


def create_compile_runner(session_factory, settings, layout: Layout, gw, executor=None):
    engine = LLMWikiV1Engine()
    manifest_builder = ManifestBuilder(settings)

    async def runner(row, *, start: float, input_restart_count: int,
                     previous_candidate=None) -> None:
        tenant_id, kb_id = row.tenant_id, row.kb_id
        cancel = CancelPortAdapter(executor, row.task_id) if executor else None
        if cancel:
            cancel.raise_if_cancelled()

        # 1) manifest + 源文档快照（短 session；resolve_prompt 需要 session）
        async with layout.version_lock(tenant_id, kb_id), session_factory() as s:
            manifest = await manifest_builder.build(s, tenant_id=tenant_id, kb_id=kb_id)
            docs = await DocumentRepo().list_parsed_for_compile(s, tenant_id, kb_id)
            has_sources = bool(docs)
            # compile_main = 固定骨架(COMPILE_MAIN_SKELETON) + 用户「编译偏好」suffix。
            # suffix 存 prompt_templates.compile_main 行(默认空串)；同一有效文本
            # 的校验值已进入 manifest，改变偏好后不会误复用旧版。
            rp = await resolve_prompt(s, tenant_id, kb_id, "compile_main")
            suffix = (rp.template or "").strip()
            compile_main_prompt = COMPILE_MAIN_SKELETON + (
                "\n\n" + suffix if suffix else ""
            )
            task = await s.get(TaskRow, row.task_id)
            saved = (task.summary or {}) if task is not None else {}
            snapshot = saved.get("source_snapshot")
            if (input_restart_count == 0 and snapshot is not None
                    and saved.get("compile_manifest") == manifest
                    and saved.get("compile_prompt") == compile_main_prompt
                    and SourceArtifactPortAdapter.dependencies_current(snapshot, docs)):
                fields = ("document_id", "content_generation", "recompile_generation",
                          "source_name", "source_sha256", "parser_id", "parser_version")
                docs = [SimpleNamespace(**dict(zip(fields, item))) for item in snapshot]
                has_sources = bool(docs)
            if task is not None:
                task.summary = {**saved,
                                "source_snapshot": SourceArtifactPortAdapter.snapshot(docs),
                                "compile_manifest": manifest, "compile_prompt": compile_main_prompt}
            await s.commit()

        # 2) ports
        sources = SourceArtifactPortAdapter(session_factory, layout, settings=settings, rows=docs)
        async def wait_resume():
            if executor is not None:
                await executor.wait_compile_resume(row.task_id, tenant_id, kb_id)
        model = ModelPortAdapter(gw, scene=None, tenant_id=tenant_id, kb_id=kb_id,
                                 before_call=wait_resume)
        store = ArtifactStoreAdapter(layout, tenant_id=tenant_id, kb_id=kb_id)
        progress = ProgressSinkAdapter(executor, row.task_id) if executor else None
        prev_dir = layout.resolve_published(tenant_id, kb_id)
        prev_ref = prev_dir.name if prev_dir is not None and prev_dir.is_dir() else None
        publication_parent = await store.published_revision_id(tenant_id, kb_id)
        recorder = RuntimeTelemetryRecorder()

        # 3) core 引擎编译（context_id = kb_id）；运行时旋钮经 Settings 注入
        with runtime_environment(settings.runtime_env()), bind_runtime_telemetry(recorder):
            artifacts, staging_key = await engine.compile(
                kb_id, tenant_id, manifest,
                sources=sources, model=model, store=store, progress=progress,
                artifact_attempt_id=row.task_id, cancel=cancel,
                compile_main_prompt=compile_main_prompt,
                previous_bundle=previous_candidate or prev_ref,
                publication_parent_revision=publication_parent,
                telemetry=recorder,
            )

        # Generated knowledge pages can be absent for native tables or source-only
        # evidence. The engine already validated their query projection; only an
        # actually empty projection is an empty output, not a zero claim/page count.
        reused_unchanged = prev_ref is not None and staging_key == prev_ref
        query_pages = artifacts.page_count
        if has_sources and query_pages == 0:
            candidate = (layout.versions_dir(tenant_id, kb_id, staging_key) if reused_unchanged
                         else layout.staging_dir(tenant_id, kb_id, staging_key))
            query_pages = await asyncio.to_thread(_query_page_count, candidate)
        if has_sources and query_pages == 0:
            raise ValidationError(
                f"编译产出 0 个可检索页面（知识页和原文/原表均为空），但仍有 {len(docs)} 个已解析文档；"
                "拒绝发布空 bundle（保留上一版）")
        prev_pages = _published_page_count(layout, tenant_id, kb_id)
        threshold = float(getattr(settings, "compile_page_drop_threshold", 0.5))
        if prev_pages and artifacts.page_count < prev_pages * (1.0 - threshold):
            # 页面骤降闸门只防「源数增加却页面骤降」的质量事故。两类页面骤降是预期/正常，
            # 不应拦：
            # - 源数下降 = 删除文档，页面随之下降（§6.5 删除豁免）。
            # - 源数不变 = 纯重编译/reparse，页面波动来自 LLM 非确定性与解析粒度差异，
            #   非「源丢失」（本次 5 → 2 即属此类）。
            prev_sources = _published_source_count(layout, tenant_id, kb_id)
            if prev_sources and len(docs) > prev_sources:
                raise ValidationError(
                    f"页面数骤降 {prev_pages} → {artifacts.page_count}，"
                    f"超阈值 {threshold:.0%}；拒绝发布（保留上一版）")

        # 5) 发布（Layout 原子目录发布 + symlink 切换）
        async with layout.publication_lock(tenant_id, kb_id, wait_resume):
            async with session_factory() as s:
                latest_docs = await DocumentRepo().list_parsed_for_compile(s, tenant_id, kb_id)
                latest_manifest = await manifest_builder.build(s, tenant_id=tenant_id, kb_id=kb_id)
                latest_prompt = await resolve_prompt(s, tenant_id, kb_id, "compile_main")
            latest_suffix = (latest_prompt.template or "").strip()
            latest_effective_prompt = COMPILE_MAIN_SKELETON + (
                "\n\n" + latest_suffix if latest_suffix else "")
            if (not SourceArtifactPortAdapter.dependencies_current(sources.source_snapshot, latest_docs)
                    or latest_manifest != manifest
                    or latest_effective_prompt != compile_main_prompt):
                raise _StaleCompileInput(
                    "编译输入在运行期间变化；拒绝发布旧快照，请等待后续编译或重试",
                    staging_key)
            current = layout.resolve_published(tenant_id, kb_id)
            current_ref = current.name if current is not None and current.is_dir() else None
            if current_ref != prev_ref:
                raise ConflictError("已发布版本在编译期间变化；拒绝覆盖，请重新编译")
            if reused_unchanged:
                sha = prev_ref
            else:
                sha = await store.publish_revision(
                    staging_key, tenant_id=tenant_id, context_id=kb_id,
                    revision_id=artifacts.revision_id)
                async with session_factory() as s:
                    pins = set((await s.execute(select(QueryTaskRow.bundle_sha).where(
                        QueryTaskRow.tenant_id == tenant_id, QueryTaskRow.kb_id == kb_id,
                        QueryTaskRow.status.in_([TaskStatus.queued.value, TaskStatus.running.value]),
                        QueryTaskRow.bundle_sha.isnot(None),
                    ))).scalars())
                try:
                    await asyncio.to_thread(layout.prune_versions, tenant_id, kb_id,
                                            settings.keep_bundles, pins)
                except OSError:
                    log.exception("bundle retention cleanup failed kb=%s", kb_id)

        # 5.5) 发布后投递独立 outline task（§6）：目录起草 LLM 可达几十秒，同步等会
        # 占编译并发槽 + 拖长编译时长；独立 task 让编译时长不变、失败可单独重试。
        if executor is not None and not reused_unchanged:
            async with session_factory() as s:
                outline_task = await TaskRepo().create(
                    s, tenant_id=tenant_id, kb_id=kb_id, kind=TaskKind.outline)
                await s.commit()
            executor.submit(outline_task)

        # 6) 结果写 TaskRow.summary
        async with session_factory() as s:
            row2 = await TaskRepo().get(s, tenant_id, row.task_id)
            if row2 is not None:
                row2.summary = {**(row2.summary or {}),
                    "bundle_sha": sha,
                    "revision_id": artifacts.revision_id,
                    "page_count": artifacts.page_count,
                    "claim_count": artifacts.claim_count,
                    "source_count": artifacts.source_count,
                    "open_errors": artifacts.error_count,
                    "wall_ms": int((time.monotonic() - start) * 1000),
                    "input_restart_count": input_restart_count,
                    "reuse_mode": ("unchanged" if reused_unchanged else
                                   "incremental" if (
                                       recorder.work_counts.get("incremental_compile_started", 0)
                                       or recorder.work_counts.get("layered_incremental_attempted", 0))
                                   else "full"),
                    "previous_ref": prev_ref,
                    "work_counts": dict(recorder.work_counts),
                    # Inclusive stage durations may overlap in nested or
                    # concurrent work; compare each stage across runs rather
                    # than adding them to reconstruct wall_ms.
                    "stage_wall_ms": {name: round(elapsed, 3)
                                      for name, elapsed in recorder.stage_wall_ms.items()},
                    "stage_events": recorder.stage_events,
                    "stage_calls": dict(recorder.stage_calls),
                }
            await s.commit()
        async with session_factory() as s:
            deleted = (await s.execute(select(DocumentRow.document_id).where(
                DocumentRow.tenant_id == tenant_id, DocumentRow.kb_id == kb_id,
                DocumentRow.deleted_at.isnot(None)))).scalars().all()
        for document_id in deleted:
            await asyncio.to_thread(shutil.rmtree,
                layout.kb_dir(tenant_id, kb_id) / "parse" / document_id, True)
        log.info("compile done kb=%s sha=%s pages=%d claims=%d",
                 kb_id, sha, artifacts.page_count, artifacts.claim_count)

    async def runner_with_cleanup(row) -> None:
        start = time.monotonic()
        input_restart_count = 0
        previous_candidate = None
        staging = layout.staging_dir(row.tenant_id, row.kb_id, row.task_id)
        try:
            async with layout.compile_lock(row.tenant_id, row.kb_id):
                claimed = False
                while True:
                    if executor is not None:
                        await executor.wait_compile_resume(row.task_id, row.tenant_id, row.kb_id)
                    await _wait_for_parses(session_factory, row.tenant_id, row.kb_id)
                    if not claimed and executor is not None:
                        await executor.acquire_compile_slot(row.task_id)
                        await executor.wait_compile_resume(row.task_id, row.tenant_id, row.kb_id)
                        async with layout.version_lock(row.tenant_id, row.kb_id):
                            if not await executor.claim_compile(row.task_id):
                                raise asyncio.CancelledError()
                        claimed = True
                    try:
                        await runner(row, start=start,
                                     input_restart_count=input_restart_count,
                                     previous_candidate=previous_candidate)
                        return
                    except _StaleCompileInput as exc:
                        # A parse or prompt edit can commit while the model is
                        # working. Keep the task alive and compile the latest
                        # inventory; the publication check remains mandatory.
                        log.info("compile input changed; restarting task_id=%s", row.task_id)
                        input_restart_count += 1
                        # The candidate is complete but unpublished. Reuse its
                        # unchanged sources; engine identity checks still force
                        # a rebuild when compile settings or prompts changed.
                        previous_candidate = exc.staging_key
                        await asyncio.sleep(0.2)
        except BaseException:
            interrupted_pause = (layout.compilation_paused(row.tenant_id, row.kb_id)
                                 and executor is not None and not executor.is_cancelled(row.task_id))
            if staging.exists() and not interrupted_pause:
                await asyncio.to_thread(shutil.rmtree, staging, True)
            raise

    runner_with_cleanup.owns_compile_slot = True
    return runner_with_cleanup
