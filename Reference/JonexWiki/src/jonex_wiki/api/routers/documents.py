import asyncio
import hashlib
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from jonex_wiki.api.deps import require_tenant
from jonex_wiki.domain.engine.compile.source_prep.blocks import InputRoute
from jonex_wiki.domain.engine.compile.source_prep.classify import classify_input, validate_content
from jonex_wiki.kernel.errors import (ConflictError, DomainError, NotFoundError, ValidationError)
from jonex_wiki.kernel.task import TaskKind, TaskStatus
from jonex_wiki.storage.layout import Layout
from jonex_wiki.storage.models import DocumentRow
from jonex_wiki.storage.repos import DocumentRepo, TaskRepo

router = APIRouter(prefix="/api/v1/kbs/{kb_id}", tags=["documents"])


@router.post("/documents", status_code=202)
async def upload_document(kb_id: str, request: Request, file: UploadFile,
                          tenant_id: str = Depends(require_tenant)):
    sf = request.app.state.session_factory
    executor = request.app.state.executor
    settings = request.app.state.settings
    filename = file.filename or ""
    # 穿越序列/路径成分拒绝(dev sanitize_suffix 语义;classify_input 只按 basename 分类)
    normalized = filename.replace("\\", "/")
    if normalized != Path(normalized).name:
        raise ValidationError(f"文件名 {filename!r} 含路径成分,拒绝")
    # 分类(无后缀/未知后缀 → ValueError → 422 fail-closed)
    try:
        ref, route = classify_input(filename)
    except ValueError as e:
        raise ValidationError(str(e))
    suffix = ref.extension
    # 大小上限(流式读取并计数,超限 413)
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise DomainError("payload_too_large", f"文件超过 {settings.max_upload_bytes} 字节", 413)
    # 上传时容器/扩展名冲突预检(直通路由;validate_content 收 path,先写临时文件再 validate)
    if route is not InputRoute.MINERU:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(data)
            tmp_name = tf.name
        try:
            await asyncio.to_thread(validate_content, Path(tmp_name))
        except ValueError as e:
            raise ValidationError(str(e))
        finally:
            Path(tmp_name).unlink(missing_ok=True)

    async with sf() as s:
        repo = DocumentRepo()
        # 文档数硬闸(spec §4.1/§7.3)
        if await repo.count_active(s, tenant_id, kb_id) >= settings.compile_max_documents:
            raise ValidationError(f"该知识库文档数已达上限 {settings.compile_max_documents}")
        # 去重:同 kb 同名未删文档已存在 → 409
        dup = (await s.execute(select(DocumentRow).where(
            DocumentRow.tenant_id == tenant_id, DocumentRow.kb_id == kb_id,
            DocumentRow.source_name == filename, DocumentRow.deleted_at.is_(None)))).scalars().first()
        if dup is not None:
            raise ConflictError(f"同知识库已存在同名文档 {filename}(document_id={dup.document_id})")
        sha = hashlib.sha256(data).hexdigest()
        row = await repo.add(s, tenant_id=tenant_id, kb_id=kb_id, source_name=filename,
                             source_sha256=sha, route=route.value,
                             parser_id="", parser_version="")
        layout = Layout(settings.data_root)
        path = layout.upload_path(tenant_id, kb_id, row.document_id, suffix)
        await asyncio.to_thread(lambda: (path.parent.mkdir(parents=True, exist_ok=True),
                                         path.write_bytes(data)))
        task = await TaskRepo().create(s, tenant_id=tenant_id, kb_id=kb_id,
                                       kind=TaskKind.parse, document_id=row.document_id)
        await s.commit()
        executor.submit(task)
        return {"task_id": task.task_id, "document_id": row.document_id,
                "status": TaskStatus.queued.value}


@router.delete("/documents/{document_id}", status_code=202)
async def delete_document(kb_id: str, document_id: str, request: Request,
                          tenant_id: str = Depends(require_tenant)):
    """spec §7.2/§7.3:逻辑删除 + 自动触发编译(合并触发);删除豁免成本闸门。"""
    return await request.app.state.app.remove_document(tenant_id, kb_id, document_id)


@router.post("/documents/{document_id}/reparse", status_code=202)
async def reparse_document(kb_id: str, document_id: str, request: Request,
                           tenant_id: str = Depends(require_tenant)):
    """spec §4.6:重新解析原上传文件;代次切换在解析成功之后(parse runner 内保证)。"""
    return await request.app.state.app.reparse_document(tenant_id, kb_id, document_id)


@router.get("/documents")
async def list_documents(kb_id: str, request: Request, limit: int = 50, offset: int = 0,
                         tenant_id: str = Depends(require_tenant)):
    sf = request.app.state.session_factory
    async with sf() as s:
        rows = (await s.execute(select(DocumentRow).where(
            DocumentRow.tenant_id == tenant_id, DocumentRow.kb_id == kb_id,
            DocumentRow.deleted_at.is_(None)).limit(limit).offset(offset))).scalars().all()
        return {"documents": [{"document_id": r.document_id, "source_name": r.source_name,
                               "status": r.status, "content_generation": r.content_generation}
                              for r in rows]}


@router.get("/documents/parse-status")
async def parse_status(kb_id: str, request: Request,
                       tenant_id: str = Depends(require_tenant)):
    return await request.app.state.app.parse_status(tenant_id, kb_id)


@router.get("/documents/{document_id}")
async def get_document(kb_id: str, document_id: str, request: Request,
                       tenant_id: str = Depends(require_tenant)):
    sf = request.app.state.session_factory
    async with sf() as s:
        r = await DocumentRepo().get(s, tenant_id, kb_id, document_id)
        if r is None or r.deleted_at is not None:
            raise NotFoundError(f"文档 {document_id} 不存在")
        return {"document_id": r.document_id, "source_name": r.source_name, "status": r.status,
                "route": r.route, "parser_id": r.parser_id, "content_generation": r.content_generation,
                "error": r.error}


@router.get("/documents/{document_id}/chunks")
async def get_document_chunks(kb_id: str, document_id: str, request: Request,
                              tenant_id: str = Depends(require_tenant)):
    return await request.app.state.app.get_document_chunks(tenant_id, kb_id, document_id)


@router.post("/documents/{document_id}/recompile", status_code=202)
async def recompile_document(kb_id: str, document_id: str, request: Request,
                             tenant_id: str = Depends(require_tenant)):
    """单文档重新编译:recompile_generation +1(参与指纹)→ 增量编译只重编该文档贡献的页面,
    其他文档页面重放 0 成本。自动触发编译(合并语义,同删除)。"""
    return await request.app.state.app.recompile_document(tenant_id, kb_id, document_id)


class RenameDocumentBody(BaseModel):
    source_name: str


@router.patch("/documents/{document_id}/name", status_code=202)
async def rename_document(kb_id: str, document_id: str, body: RenameDocumentBody,
                          request: Request, tenant_id: str = Depends(require_tenant)):
    """Display-name-only rename; document_id, source_id and parsed content remain stable."""
    return await request.app.state.app.rename_document(
        tenant_id, kb_id, document_id, body.source_name)


class IngestRequest(BaseModel):
    """平台已解析产物入库请求:source_path 指向共享卷内已解析 .md(platform parse_only)。"""
    source_path: str                 # 相对共享卷根的相对路径(如 parsed/{doc_id}/content.md)
    source_name: str                 # 原始文件名(去重 + 展示)
    document_id: str | None = None   # 透传平台文档 id;缺省由引擎生成


@router.post("/ingest", status_code=202)
async def ingest_parsed_document(kb_id: str, request: Request, payload: IngestRequest,
                                 tenant_id: str = Depends(require_tenant)):
    """平台侧解析(parse_only)产物入库:复制 .md 到 gen-1/raw 后直接触发编译,跳过 parse task。

    - 平台已用 MinerU/RAG-Anything 解析成 markdown,引擎无需再解析(.md 直通)
    - 复用去重 409 / 文档数闸 / compile task 编排;不写 uploads、不 classify/validate
    """
    return await request.app.state.app.ingest_parsed_document(
        tenant_id, kb_id, payload.source_path, payload.source_name, payload.document_id)
