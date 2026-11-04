import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from jonex_wiki.api.deps import require_tenant
from jonex_wiki.kernel.errors import ConflictError, NotFoundError, ValidationError
from jonex_wiki.kernel.task import TaskKind, TaskStatus
from jonex_wiki.storage.models import QueryTaskRow, TaskRow
from jonex_wiki.storage.repos import DocumentRepo, TaskRepo

router = APIRouter(prefix="/api/v1/kbs/{kb_id}", tags=["compile"])
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
log = logging.getLogger(__name__)


@router.post("/compile", status_code=202)
async def trigger_compile(kb_id: str, request: Request,
                          tenant_id: str = Depends(require_tenant)):
    sf = request.app.state.session_factory
    executor = request.app.state.executor
    settings = request.app.state.settings
    async with sf() as s:
        # 幂等:queued/compiling → 409
        running = (await s.execute(select(TaskRow).where(
            TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
            TaskRow.kind == TaskKind.compile.value,
            TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.compiling.value])))).scalars().first()
        if running is not None:
            raise ConflictError(f"该知识库已有进行中的编译任务 {running.task_id}")
        docs = await DocumentRepo().list_parsed_for_compile(s, tenant_id, kb_id)
        if not docs:
            raise ValidationError("该知识库没有已解析文档,无法编译")
        if len(docs) > settings.compile_max_documents:
            raise ValidationError(f"编译文档数超限: docs={len(docs)}/{settings.compile_max_documents}")
        # [jonex] P3b（方案 B）：可选 model = 本次编译要用的模型快照；缺省 = 部署默认
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — 空 body / 非 JSON 一律当没带
            body = {}
        compile_model = str((body or {}).get("model") or "").strip()
        task = await TaskRepo().create(s, tenant_id=tenant_id, kb_id=kb_id,
                                       kind=TaskKind.compile, compile_model=compile_model)
        await s.commit()
        executor.submit(task)
        return {"task_id": task.task_id, "status": task.status}


@router.get("/versions")
async def list_versions(kb_id: str, request: Request,
                        tenant_id: str = Depends(require_tenant)):
    layout = request.app.state.app.layout
    async with layout.version_lock(tenant_id, kb_id):
        active = layout.resolve_published(tenant_id, kb_id)
        if active is None or not active.is_dir():
            raise NotFoundError(f"知识库 {kb_id} 无已发布版本")
        versions = layout.kb_dir(tenant_id, kb_id) / "compile" / "versions"
        items = []
        for path in sorted((p for p in versions.iterdir() if p.is_dir()),
                           key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True):
            try:
                revision = json.loads((path / "revision.json").read_text("utf-8"))
            except (OSError, ValueError):
                revision = {}
            items.append({"bundle_sha": path.name, "active": path == active,
                          "revision_id": revision.get("revision_id"),
                          "page_count": revision.get("page_count"),
                          "source_count": revision.get("source_count")})
        return {"active_bundle_sha": active.name, "versions": items}


@router.post("/versions/{bundle_sha}/rollback")
async def rollback_version(kb_id: str, bundle_sha: str, request: Request,
                           tenant_id: str = Depends(require_tenant)):
    if not _SHA_RE.fullmatch(bundle_sha):
        raise ValidationError("bundle_sha 必须是 64 位十六进制哈希")
    layout = request.app.state.app.layout
    target = layout.versions_dir(tenant_id, kb_id, bundle_sha)
    async with layout.version_lock(tenant_id, kb_id):
        if not target.is_dir():
            raise NotFoundError(f"版本 {bundle_sha} 不存在")
        async with request.app.state.session_factory() as s:
            running = (await s.execute(select(TaskRow.task_id).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.compiling.value]),
            ))).first()
            if running is not None:
                raise ConflictError("编译进行中，无法切换版本")
            pins = set((await s.execute(select(QueryTaskRow.bundle_sha).where(
                QueryTaskRow.tenant_id == tenant_id, QueryTaskRow.kb_id == kb_id,
                QueryTaskRow.status.in_([TaskStatus.queued.value, TaskStatus.running.value]),
                QueryTaskRow.bundle_sha.isnot(None),
            ))).scalars())

        from jonex_wiki.adapters.artifact_store import ArtifactStoreAdapter

        def validate_target() -> None:
            view = ArtifactStoreAdapter(layout, tenant_id=tenant_id, kb_id=kb_id).open_revision(bundle_sha)
            if view.tenant_id != tenant_id or view.context_id != kb_id or view.manifest_hash != bundle_sha:
                raise ValueError("版本归属或哈希不匹配")
            view.verify_integrity()

        try:
            await asyncio.to_thread(validate_target)
        except (OSError, ValueError, KeyError) as exc:
            raise ValidationError(f"版本校验失败: {exc}") from exc
        old = layout.resolve_published(tenant_id, kb_id)
        layout.activate_version(tenant_id, kb_id, bundle_sha)
        log.info("bundle rollback tenant=%s kb=%s from=%s to=%s",
                 tenant_id, kb_id, old.name if old else None, bundle_sha)
        try:
            await asyncio.to_thread(layout.prune_versions, tenant_id, kb_id,
                                    request.app.state.settings.keep_bundles, pins)
        except OSError:
            log.exception("bundle retention cleanup failed after rollback kb=%s", kb_id)
    return {"bundle_sha": bundle_sha, "previous_bundle_sha": old.name if old else None,
            "status": "active"}
