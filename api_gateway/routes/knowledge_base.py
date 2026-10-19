#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base API routes.

The Gateway is a thin REST adapter. Tenant identity comes from the current
Authorization token and is forwarded to Sidecar; request bodies never accept
``tenant_id``.
"""

import asyncio
import base64
import os
import random
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, List, Optional
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Depends, File, Form, Header, Path, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from api_gateway.deps import require_auth_header, raise_from_capability_result
# 身份解析统一在 `api_gateway/identity.py`（原先这里和 business_domain.py 各抄了一份）。
# 别名导入，是为了不动下面 20 多处既有的 `_extract_*` 调用点。
from api_gateway.identity import (
    extract_impersonation as _extract_impersonation,
    extract_user_id as _extract_user_id,
    extract_username as _extract_username,
)
from jonex_core.common.crypto import (
    generate_query_image_token,
    generate_view_token,
    verify_query_image_token,
    verify_view_token,
)
from jonex_core.common.monitor_collector import attempt_span
from pydantic.v1 import BaseModel, Field

from jonex_core.common.object_storage import build_object_key, get_object_storage, get_object_storage_for
from capabilities.knowledge_base.dtos import (
    AddDocumentTagRequest,
    AnswerFeedbackToggleAdoptRequest,
    BatchMoveDocumentsRequest,
    CreateOntologyInstanceRequest,
    DocumentParseResultRequest,
    DocumentScopeRequest,
    FolderCreateRequest,
    FolderUpdateRequest,
    OntologyEntitySearchRequest,
    OntologyGraphRequest,
    OntologyInstanceListRequest,
    OntologyNeighborRequest,
    OntologyRelationListRequest,
    OntologyRetryRequest,
    JonexWikiSearchRequest,
    LlmWikiSearchRequest,
    MixSearchRequest,
    NativeOntologyExtractRequest,
    OntologySearchRequest,
    OntologyStatsRequest,
    DeepSearchRequest,
    ParseResultDocumentListRequest,
    ParseResultEntityListRequest,
    ParseResultGraphRequest,
    ParseResultRelationshipListRequest,
    ParseResultScopeRequest,
    ReferenceResolveRequest,
    SearchHistoryCreateRequest,
    SearchHistoryDeleteRequest,
    SearchRequest,
    SetDocumentFolderRequest,
    SetDocumentTagsRequest,
    SubmitAnswerFeedbackRequest,
    TagCreateRequest,
    TagUpdateRequest,
)
from jonex_core.common import CapabilityInvokeError, InvalidApiKeyError, InvalidParameterError, JonexException
from jonex_core.common import get_config, get_logger, success_response, transmit_locale_header
from jonex_core.common.i18n import translate
from jonex_core.common.file_source_util import parse_file_source
from jonex_core.common.tenant import extract_tenant_id

logger = get_logger("api_knowledge_base")

router = APIRouter(dependencies=[Depends(require_auth_header)])

# 文本类 MIME：本地代理原文时需补 charset=utf-8，否则浏览器按本地编码解码导致中文乱码
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
}


def _media_type_with_charset(mime: str | None) -> str:
    """对文本类 MIME 追加 charset=utf-8，避免原文查看中文乱码。"""
    mime = (mime or "application/octet-stream").strip()
    if "charset=" in mime.lower():
        return mime
    base = mime.split(";", 1)[0].strip().lower()
    if base.startswith(_TEXT_MIME_PREFIXES) or base in _TEXT_MIME_EXACT:
        return f"{mime}; charset=utf-8"
    return mime


# 可执行脚本的 MIME：同源 inline 打开有 XSS 风险，降级为纯文本，杜绝脚本执行
_EXECUTABLE_MIME = {"text/html", "application/xhtml+xml", "image/svg+xml"}


def _safe_inline_media_type(mime: str | None) -> str:
    """本地同源 inline 返回时的安全 Content-Type：

    - text/html、svg 等可执行类型 → 降级 text/plain（配合 nosniff 杜绝脚本执行）；
    - 其余文本补 charset=utf-8；二进制原样。
    """
    base = (mime or "").split(";", 1)[0].strip().lower()
    if base in _EXECUTABLE_MIME:
        return "text/plain; charset=utf-8"
    return _media_type_with_charset(mime)



def _capability_error_keys(capability_id: str) -> tuple[str, str]:
    """能力错误 i18n 键分流:atomic-rag 与 knowledge-base 各用一套文案。"""
    if capability_id == "atomic.rag.lightrag.v1":
        return "err.capability.rag_timeout", "err.capability.rag_unavailable"
    return "err.capability.kb_timeout", "err.capability.kb_unavailable"


@asynccontextmanager
async def _sidecar_client(timeout, client=None):
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=timeout) as owned:
            yield owned


async def _invoke_sidecar_capability(
    request: Request,
    action: str,
    payload: dict[str, Any],
    *,
    capability_id: str = "business.knowledge_base.v1",
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    timeout: Optional[float] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """经 Sidecar invoke 转发到指定能力服务(Gateway 唯一能力调用出口)。"""
    config = get_config()
    tenant_id = tenant_id or extract_tenant_id(request)
    request_id = getattr(request.state, "request_id", "")
    resolved_user_id = user_id or _extract_user_id(request)
    resolved_username = _extract_username(request)
    _impersonated, _perms, _original_tenant_id = _extract_impersonation(request)

    sidecar_payload: dict[str, Any] = {
        "capability_id": capability_id,
        "payload": {"action": action, "data": payload},
        "tenant_id": tenant_id,
    }
    if resolved_user_id:
        sidecar_payload["user_id"] = resolved_user_id
    if resolved_username:
        sidecar_payload["username"] = resolved_username
    if _impersonated:
        sidecar_payload["impersonated"] = True
        sidecar_payload["perms"] = _perms
        if _original_tenant_id:
            sidecar_payload["original_tenant_id"] = _original_tenant_id

    headers = {
        "X-API-Key": config.GATEWAY_API_KEY,
        "X-Request-ID": request_id,
        "X-Tenant-ID": tenant_id,
        "X-Forwarded-For": request.client.host if request.client else "",
    }
    transmit_locale_header(headers)

    # Sidecar 瞬态连接错误（重启/DNS/端口未就绪）做指数退避 + 全抖动重试
    max_retries = int(os.getenv("GATEWAY_SIDECAR_RETRIES", "2"))
    base_delay = float(os.getenv("GATEWAY_SIDECAR_BASE_DELAY", "1.0"))
    max_delay = float(os.getenv("GATEWAY_SIDECAR_MAX_DELAY", "8.0"))
    timeout = timeout if timeout is not None else float(os.getenv("GATEWAY_SIDECAR_TIMEOUT", "180"))

    last_exc: BaseException | None = None
    wait_before_ms: Optional[int] = None
    for attempt in range(max_retries + 1):
        # N2 物理 Attempt 埋点（监控旁路）。kb / rag 两条链路共用本循环，故一处埋点即覆盖两条。
        with attempt_span(
            request_id=request_id,
            tenant_id=tenant_id,
            attempt_no=attempt + 1,
            # 先按「最后一次尝试」记；只有下面真会重试的 transport 分支才降级为 False。
            is_final=True,
            wait_before_ms=wait_before_ms,
        ) as att:
            try:
                async with _sidecar_client(timeout, http_client) as client:
                    response = await client.post(
                        f"{config.SIDECAR_URL}/invoke",
                        json=sidecar_payload,
                        headers=headers,
                    )
                result = response.json()
                if response.status_code != 200 or not result.get("success", False):
                    raise_from_capability_result(result)
                return result.get("data") or {}
            except httpx.TimeoutException as e:
                att.fail(error_code=504, error_message=e)
                timeout_key, _ = _capability_error_keys(capability_id)
                raise CapabilityInvokeError(
                    message=translate(timeout_key, fallback="能力调用超时"),
                    details={"action": action, "capability_id": capability_id},
                )
            except httpx.TransportError as e:
                att.fail(error_code=502, error_message=e)
                last_exc = e
                if attempt < max_retries:
                    # 还会重试 ⇒ 本次不是最后一次尝试。is_final 无 setter，就地降级快照。
                    att.mark_final(False)
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    jitter = random.uniform(0, delay)
                    wait_before_ms = int(jitter * 1000)
                    logger.warning(
                        "Sidecar 调用 %s/%s 瞬断 (attempt %s/%s)，"
                        "%.1fs 后重试: %s",
                        capability_id, action, attempt + 1, max_retries + 1, jitter, e,
                    )
                    await asyncio.sleep(jitter)
                else:
                    logger.error(
                        "Sidecar 调用 %s/%s 失败，已重试 %s 次: %s",
                        capability_id, action, max_retries, e,
                    )
                    _, unavailable_key = _capability_error_keys(capability_id)
                    raise CapabilityInvokeError(
                        message=translate(unavailable_key, fallback="能力不可用"),
                        details={"action": action, "capability_id": capability_id},
                    )
            except JonexException:
                raise
            except Exception as exc:
                if hasattr(exc, "code") and hasattr(exc, "status_code"):
                    raise
                logger.error(
                    "Sidecar capability call failed: capability_id=%s action=%s error=%s",
                    capability_id, action, exc,
                )
                _, unavailable_key = _capability_error_keys(capability_id)
                raise CapabilityInvokeError(
                    message=translate(unavailable_key, fallback="能力不可用"),
                    details={"action": action, "capability_id": capability_id},
                )


async def _call_kb_capability(
    request: Request,
    action: str,
    payload: dict[str, Any],
    *,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """知识库能力 invoke 转发(向后兼容委托,capability_id 固定 business.knowledge_base.v1)。"""
    return await _invoke_sidecar_capability(
        request, action, payload,
        capability_id="business.knowledge_base.v1",
        user_id=user_id, tenant_id=tenant_id, http_client=http_client,
    )


async def _call_rag_capability(
    request: Request,
    action: str,
    payload: dict[str, Any],
    *,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> dict:
    """atomic-rag 能力 invoke 转发(独立本体抽取等)。

    LLM 抽取耗时远高于普通 CRUD(60 chunk ≈ 30+ 次 LLM 调用),单独放宽超时;
    注意 Sidecar 侧 SIDECAR_PROXY_TIMEOUT 为全局 env,慢上游时需同步调大。
    """
    timeout = float(os.getenv("GATEWAY_RAG_EXTRACT_TIMEOUT", "600"))
    return await _invoke_sidecar_capability(
        request, action, payload,
        capability_id="atomic.rag.lightrag.v1",
        user_id=user_id, tenant_id=tenant_id,
        timeout=timeout,
    )


@router.post("/documents/upload", summary="上传知识文档")
async def upload_document(
    request: Request,
    file: UploadFile = File(None, description="上传的文件（COS 直传模式可不传）"),
    knowledge_base_id: str = Form(..., min_length=1, max_length=128, description="知识库 ID"),
    storage_key: Optional[str] = Form(None, description="COS 预上传后的对象键（COS 直传模式）"),
    storage_backend: Optional[str] = Form(None, description="存储后端，cos 或 local"),
    doc_id: Optional[str] = Form(None, description="文档 ID（COS 直传模式，与 generate-upload-url 返回一致）"),
    file_name: Optional[str] = Form(None, description="文件名（COS 直传模式必传）"),
    mime_type: Optional[str] = Form(None, description="文件 MIME 类型"),
    folder_id: Optional[str] = Form(None, description="文件夹 ID（归属到指定文件夹）"),
    metadata: Optional[str] = Form(None, description="额外元数据（JSON 字符串）"),
):
    """上传文档到知识库。

    两种模式：
    - 传统模式：Gateway 接收文件字节，保存到本地后传路径给能力层
    - COS 直传：客户端先通过 generate-upload-url 上传到 COS，再传 storage_key 确认
    """
    tenant_id = extract_tenant_id(request)

    payload: dict[str, Any] = {
        "knowledge_base_id": knowledge_base_id,
    }
    if folder_id:
        payload["folder_id"] = folder_id

    if storage_key:
        # 对象存储直传模式（cos/s3）：文件已在对象存储上，只需确认
        if doc_id:
            payload["doc_id"] = doc_id
        payload["file_name"] = file_name or storage_key.rsplit("/", 1)[-1]
        payload["file_path"] = storage_key
        payload["storage_key"] = storage_key
        # [jonex] S3 兼容：前端未显式指定时跟随平台全局配置，不硬编码 cos——
        # 否则 S3 部署下直传确认会把文档标成 cos，后续取原文用错客户端。
        _env_backend = (os.getenv("OBJECT_STORAGE_BACKEND", "local") or "local").strip().lower()
        payload["storage_backend"] = storage_backend or (
            _env_backend if _env_backend in ("cos", "s3") else "local"
        )
        if mime_type:
            payload["mime_type"] = mime_type
    else:
        # 传统模式：Gateway 接收文件字节，统一写入对象存储（local / cos 同一套 key 方案）
        if file is None:
            raise InvalidParameterError(message=translate("err.kb.cos_upload_or_storage_key", fallback="传统模式必须上传文件，或 COS 直传模式请传入 storage_key"))
        content = await file.read()
        if not content:
            raise InvalidParameterError(message=translate("err.kb.upload_file_required", fallback="上传文件不能为空"))

        backend = os.getenv("OBJECT_STORAGE_BACKEND", "local").strip().lower()
        doc_id = str(uuid4())
        new_key = build_object_key(tenant_id, knowledge_base_id, doc_id, file.filename)
        await get_object_storage().put_bytes(
            new_key, content, content_type=_media_type_with_charset(file.content_type)
        )

        payload["doc_id"] = doc_id
        payload["file_name"] = file.filename or "unnamed"
        payload["mime_type"] = file.content_type
        payload["file_size"] = len(content)
        payload["storage_key"] = new_key
        payload["storage_backend"] = backend
        # file_path 不在网关计算：由能力层 upload_document 按后端推导
        #   （local=共享卷绝对路径 / 对象存储=storage_key）
        logger.info("Gateway 上传文件: backend=%s key=%s (%d bytes)", backend, new_key, len(content))

    if metadata:
        payload["metadata"] = {"raw": metadata}

    result = await _call_kb_capability(request, "upload_document", payload)
    return success_response(data=result)


@router.post("/documents/generate-upload-url", summary="生成预签名上传 URL（COS 直传）")
async def generate_upload_url(
    request: Request,
    knowledge_base_id: str = Body(..., min_length=1, max_length=128),
    file_name: str = Body(..., min_length=1, max_length=512),
    content_type: Optional[str] = Body(None, max_length=128),
    file_size: Optional[int] = Body(None, ge=1),
):
    """生成 COS 预签名 PUT URL，前端直传字节到 COS（不经 Sidecar）。

    D9：大文件不经 Sidecar 透传字节，用预签名 URL 直传 COS。
    上传完成后调 upload_document 只传 storage_key + 元数据确认。
    file_size（可选）为待上传字节数：传时服务端校验上限并签入 Content-Length。
    """
    result = await _call_kb_capability(
        request,
        "generate_upload_url",
        {
            "knowledge_base_id": knowledge_base_id,
            "file_name": file_name,
            "content_type": content_type,
            "file_size": file_size,
        },
    )
    return success_response(data=result)


@router.get("/documents/{document_id}/view-ticket", summary="签发原文短时查看票据（音视频/PDF/图片直连）")
async def get_view_ticket(request: Request, document_id: str):
    """签发只读、单文档、短时效的查看 URL。

    用于 `<video>/<audio>` 等无法携带 Authorization 头的直连流式播放，
    以及 PDF/图片新标签内联预览。token 绑定当前租户 + 该文档 + TTL。
    """
    # [jonex] B6 §5.2：先校验文档可读（租户 + KB 空间/授权 + 密级），
    # 通过后才签 token；被拦截时 404 原样透传，绝不签发。
    await _call_kb_capability(
        request, "assert_document_readable", {"document_id": document_id},
    )
    tenant_id = extract_tenant_id(request)
    ttl = int(os.getenv("RAW_VIEW_TOKEN_TTL", "300"))
    token = generate_view_token(tenant_id, document_id, ttl)
    return success_response(
        data={
            "url": f"/api/v1/knowledge-base/documents/{document_id}/raw?token={token}",
            "expires_in": ttl,
        }
    )


@router.get("/documents", summary="查询知识文档列表")
async def list_documents(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    status: Optional[str] = Query(None, description="文档状态过滤"),
    ontology_status: Optional[str] = Query(None, description="本体状态过滤"),
    phase: Optional[list[str]] = Query(
        None,
        description="线性状态多选（优先于 status/ontology_status）：pending_parse/parsing/ingesting/parse_failed/pending_compile/compiling/compiled/compile_failed",
    ),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    folder_id: Optional[str] = Query(None, description="文件夹筛选（仅返回该文件夹下的文档，不传为全部文档）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """查询当前租户的知识文档列表"""
    payload: dict[str, Any] = {
        "knowledge_base_id": knowledge_base_id,
        "status": status,
        "ontology_status": ontology_status,
        "keyword": keyword,
        "page": page,
        "page_size": page_size,
    }
    if phase:
        payload["phase"] = phase
    if folder_id:
        payload["folder_id"] = folder_id
    result = await _call_kb_capability(request, "list_documents", payload)
    return success_response(data=result)


@router.get("/documents/stats", summary="文档处理状态统计")
async def documents_stats(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """按线性状态口径统计某 KB 的文档数（total/searchable/processing/parse_failed/compile_failed）。

    注意：本路由必须声明在 `/documents/{document_id}` 之前，否则 `stats` 会被当作 document_id。
    """
    result = await _call_kb_capability(
        request, "documents_stats", {"knowledge_base_id": knowledge_base_id}
    )
    return success_response(data=result)


@router.get("/documents/{document_id}", summary="获取知识文档详情")
async def get_document(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """获取指定知识文档的详细信息"""
    payload = DocumentScopeRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(
        request,
        "get_document",
        {"document_id": document_id, **_schema_payload(payload)},
    )
    return success_response(data=result)


class DocumentQualityReportsRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    document_ids: List[str] = Field(..., min_items=1, max_items=100)


@router.get("/documents/{document_id}/quality-report", summary="获取文档解析质量报告")
async def get_document_quality_report(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    """单文档质量报告（含 md 证据的预签名 URL）。

    非引擎链文档返回 `supported=false`（不报错）；从未摄取的文档返回 404。
    """
    result = await _call_kb_capability(
        request,
        "get_document_quality_report",
        {"document_id": document_id, "knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


@router.post("/documents/quality-reports", summary="批量获取文档质量摘要")
async def get_document_quality_reports(
    request: Request, body: DocumentQualityReportsRequest,
):
    """批量质量摘要（列表页用）：只回档位与原因，不签 md URL。"""
    result = await _call_kb_capability(
        request,
        "get_document_quality_reports",
        {
            "knowledge_base_id": body.knowledge_base_id,
            "document_ids": body.document_ids,
        },
    )
    return success_response(data=result)


@router.get("/documents/{document_id}/chunks", summary="查看文档 Chunk 列表")
async def get_document_chunks(request: Request, document_id: str):
    """按文档 id 查看 chunk 列表（含行号/时间轴位置元数据）。

    doc_id 为 KB 文档 id；所属知识库由服务端按文档归属解析，无需前端传入。
    """
    result = await _call_kb_capability(
        request,
        "get_document_chunks",
        {"document_id": document_id},
    )
    return success_response(data=result)


@router.get("/documents/{document_id}/source-spans", summary="查看引用片段的原文坐标")
async def get_document_source_spans(
    request: Request,
    document_id: str,
    chunk_index: Optional[int] = Query(None),
    page_no: Optional[int] = Query(None),
    table_idx: Optional[int] = Query(None),
    row_start: Optional[int] = Query(None),
    row_end: Optional[int] = Query(None),
    image_idx: Optional[int] = Query(None),
):
    """按 chunk 与引用侧位置锚点读取原文 bbox span（打开原文时的框选定位）。

    `expect` 只携带传入的定位键；服务端拿它与 chunk 的 `file_source` 比对，
    对不上时降级为只跳页，不抛错。
    """
    expect = {
        key: value
        for key, value in (
            ("page_no", page_no),
            ("table_idx", table_idx),
            ("row_start", row_start),
            ("row_end", row_end),
            ("image_idx", image_idx),
        )
        if value is not None
    }
    data = {"document_id": document_id}
    if chunk_index is not None:
        data["chunk_index"] = chunk_index
    if expect:
        data["expect"] = expect
    result = await _call_kb_capability(
        request,
        "get_document_source_spans",
        data,
    )
    return success_response(data=result)


@router.get("/documents/{document_id}/chunks/{chunk_id}", summary="查看单个 Chunk 内容")
async def get_chunk(request: Request, document_id: str, chunk_id: str):
    """按 chunk_id 精确查单片 chunk 完整内容（含位置元数据）。

    chunk_id 为 LightRAG 内容哈希主键（chunk-<md5>），召回明细/引用锚点直接给出。
    """
    result = await _call_kb_capability(
        request, "get_chunk", {"document_id": document_id, "chunk_id": chunk_id},
    )
    return success_response(data=result)


# ── [jonex] §table-grid-v2 O7: 新旧格式残留扫描与定点清理 ──


@router.get("/documents/{document_id}/stale-chunks", summary="扫描新旧格式 chunk 残留（O7）")
async def scan_stale_chunks(request: Request, document_id: str):
    """按格式指纹扫描文档的旧格式表格 chunk 残留（col_ 占位 / 裸 HTML 残片）。

    reparse 后旧 doc 删除失败时新旧格式在向量库共存，本接口是唯一可观测通道。
    返回 {doc_id, total, stale_chunk_count, stale_chunks}。
    """
    result = await _call_kb_capability(
        request, "scan_stale_chunks", {"document_id": document_id},
    )
    return success_response(data=result)


@router.post("/documents/{document_id}/purge-stale-chunks", summary="定点清理旧格式 chunk（O7）")
async def purge_stale_chunks(request: Request, document_id: str):
    """按格式指纹定点删除旧格式 chunk，避免整篇重推（reparse 的 LLM 抽取全量重跑）。

    返回 {doc_id, purged, failed, total}。
    """
    result = await _call_kb_capability(
        request, "purge_stale_chunks", {"document_id": document_id},
    )
    return success_response(data=result)


class ReparseRequest(BaseModel):
    force: bool = False


@router.post("/documents/{document_id}/reparse", summary="重新解析文档")
async def reparse_document(request: Request, document_id: str, body: ReparseRequest = ReparseRequest()):
    """按文档 id 重新解析（force_reparse）。

    复用文档已存的存储/所属知识库信息重新入库，无需前端传其他参数。

    传 ``force=true`` 可跳过 R1-c 源文件 hash 检查，强制重跑解析
    （用于仅修改解析配置/提示词/本体 schema 的场景）。
    """
    result = await _call_kb_capability(
        request,
        "reparse_document",
        {"document_id": document_id, "force": body.force},
    )
    return success_response(data=result)


@router.get("/documents/{document_id}/versions", summary="文档版本历史")
async def list_document_versions(request: Request, document_id: str):
    result = await _call_kb_capability(
        request, "list_document_versions", {"document_id": document_id},
    )
    return success_response(data=result)


@router.post("/documents/{document_id}/versions", summary="上传文档新版本")
async def upload_document_version(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Form(..., min_length=1, max_length=128),
    file: UploadFile = File(None),
    storage_key: Optional[str] = Form(None),
    storage_backend: Optional[str] = Form(None),
    file_name: Optional[str] = Form(None),
    mime_type: Optional[str] = Form(None),
):
    """上传新版本（新版生效、旧版存档）。

    两种模式与首次上传一致：multipart 文件（网关写对象存储）或 COS 直传后确认。
    ⚠ 版本文件必须使用**独占 key**，否则会覆盖旧版本仍在引用的对象。
    """
    tenant_id = extract_tenant_id(request)
    payload: dict[str, Any] = {
        "document_id": document_id,
        "knowledge_base_id": knowledge_base_id,
    }

    if storage_key:
        payload["storage_key"] = storage_key
        payload["file_name"] = file_name or storage_key.rsplit("/", 1)[-1]
        _env_backend = (os.getenv("OBJECT_STORAGE_BACKEND", "local") or "local").strip().lower()
        payload["storage_backend"] = storage_backend or (
            _env_backend if _env_backend in ("cos", "s3") else "local"
        )
        if mime_type:
            payload["mime_type"] = mime_type
    else:
        if file is None:
            raise InvalidParameterError(
                message=translate(
                    "err.kb.upload_file_required",
                    fallback="上传文件不能为空",
                )
            )
        content = await file.read()
        if not content:
            raise InvalidParameterError(
                message=translate(
                    "err.kb.upload_file_required",
                    fallback="上传文件不能为空",
                )
            )
        backend = os.getenv("OBJECT_STORAGE_BACKEND", "local").strip().lower()
        new_key = build_object_key(
            tenant_id,
            knowledge_base_id,
            document_id,
            f"{uuid4().hex}_{file.filename or 'unnamed'}",
        )
        await get_object_storage().put_bytes(
            new_key, content, content_type=_media_type_with_charset(file.content_type),
        )
        payload.update({
            "storage_key": new_key,
            "storage_backend": backend,
            "file_name": file.filename or "unnamed",
            "mime_type": file.content_type,
            "file_size": len(content),
        })

    result = await _call_kb_capability(request, "upload_document_version", payload)
    return success_response(data=result)


@router.get(
    "/documents/{document_id}/versions/{version_no}/url",
    summary="文档历史版本下载地址",
)
async def get_document_version_url(
    request: Request, document_id: str, version_no: int,
):
    result = await _call_kb_capability(
        request, "get_document_version_url",
        {"document_id": document_id, "version_no": version_no},
    )
    return success_response(data=result)


@router.delete("/documents/{document_id}", summary="删除知识文档")
async def delete_document(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """软删除指定知识文档"""
    payload = DocumentScopeRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(
        request,
        "delete_document",
        {"document_id": document_id, **_schema_payload(payload)},
    )
    return success_response(data=result)


# ── 第 3 期生命周期：归档 / 恢复 / 销毁（KB 能力执行）──────────────
# 三条都是薄转发；权限口径在能力层（归档/恢复按空间写权限，销毁额外要求租户管理员，
# 拒绝时由 service 抛 403 经现有能力错误映射上浮）。


@router.post("/documents/{document_id}/archive", summary="归档文档")
async def archive_document(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """把文档移出检索但保留原文与元数据（可恢复）。"""
    payload = DocumentScopeRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(
        request,
        "archive_document",
        {"document_id": document_id, **_schema_payload(payload)},
    )
    return success_response(data=result)


@router.post("/documents/{document_id}/restore", summary="恢复已归档文档")
async def restore_document(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """恢复已归档文档（按归档前状态重新可检索）。"""
    payload = DocumentScopeRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(
        request,
        "restore_document",
        {"document_id": document_id, **_schema_payload(payload)},
    )
    return success_response(data=result)


@router.post("/documents/{document_id}/destroy", summary="销毁已归档文档")
async def destroy_document(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """彻底销毁已归档文档（仅租户管理员；不可恢复）。"""
    payload = DocumentScopeRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(
        request,
        "destroy_document",
        {"document_id": document_id, **_schema_payload(payload)},
    )
    return success_response(data=result)


@router.put("/documents/{document_id}/folder", summary="设置或清除文档所属文件夹")
async def set_document_folder(
    request: Request,
    document_id: str,
    body: SetDocumentFolderRequest,
):
    """设置文档的文件夹归属；folder_id 为 null 时清除归属。"""
    result = await _call_kb_capability(
        request,
        "set_document_folder",
        {"document_id": document_id, **_schema_payload(body)},
    )
    return success_response(data=result)


@router.put("/documents/folder", summary="批量设置文档所属文件夹")
async def batch_set_document_folder(request: Request, body: BatchMoveDocumentsRequest):
    """批量把多个文档移动到目标文件夹；folder_id 为 null 时移出到未分类。"""
    result = await _call_kb_capability(
        request,
        "batch_set_document_folder",
        _schema_payload(body),
    )
    return success_response(data=result, message="文档已移动")


# ── 文件夹 CRUD ──────────────────────────────────────────


@router.get("/folders", summary="列出知识库文件夹")
async def list_folders(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """列出指定知识库的所有文件夹（一层层级，按预设/名称/创建时间排序）。"""
    result = await _call_kb_capability(
        request,
        "list_folders",
        {"knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


@router.post("/folders", summary="创建文件夹")
async def create_folder(request: Request, body: FolderCreateRequest):
    """在知识库中创建文件夹（同知识库内名称唯一）。"""
    result = await _call_kb_capability(request, "create_folder", _schema_payload(body))
    return success_response(data=result, message="文件夹已创建")


@router.patch("/folders/{folder_id}", summary="重命名文件夹")
async def rename_folder(
    request: Request,
    folder_id: str,
    body: FolderUpdateRequest,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """重命名指定文件夹（新名称在同知识库内唯一）。"""
    payload = {"folder_id": folder_id, "knowledge_base_id": knowledge_base_id, **_schema_payload(body)}
    result = await _call_kb_capability(request, "rename_folder", payload)
    return success_response(data=result, message="文件夹已重命名")


@router.delete("/folders/{folder_id}", summary="删除文件夹")
async def delete_folder(
    request: Request,
    folder_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """删除文件夹（关联文档的 folder_id 置 NULL，文档不删除）。"""
    result = await _call_kb_capability(
        request,
        "delete_folder",
        {"folder_id": folder_id, "knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result, message="文件夹已删除")


# ── 标签 CRUD ─────────────────────────────────────────────


@router.get("/tags", summary="列出知识库标签")
async def list_tags(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """列出指定知识库的所有标签（按创建时间降序）。"""
    result = await _call_kb_capability(
        request,
        "list_tags",
        {"knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


@router.post("/tags", summary="创建标签")
async def create_tag(request: Request, body: TagCreateRequest):
    """在知识库中创建标签（同知识库内名称唯一）。"""
    result = await _call_kb_capability(request, "create_tag", _schema_payload(body))
    return success_response(data=result, message="标签已创建")


@router.put("/tags/{tag_id}", summary="更新标签")
async def update_tag(
    request: Request,
    tag_id: str,
    body: TagUpdateRequest,
):
    """更新标签名称和/或颜色。"""
    payload = {"tag_id": tag_id, **_schema_payload(body)}
    result = await _call_kb_capability(request, "update_tag", payload)
    return success_response(data=result, message="标签已更新")


@router.delete("/tags/{tag_id}", summary="删除标签")
async def delete_tag(
    request: Request,
    tag_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """删除指定标签（解除关联文档的标签绑定）。"""
    result = await _call_kb_capability(
        request, "delete_tag", {"tag_id": tag_id, "knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result, message="标签已删除")


# ── 文档-标签关联 ────────────────────────────────────────


@router.put("/documents/{document_id}/tags", summary="设置文档标签列表（全量替换）")
async def set_document_tags(
    document_id: str,
    request: Request,
    body: SetDocumentTagsRequest,
):
    """全量替换文档的标签列表。"""
    result = await _call_kb_capability(
        request,
        "set_document_tags",
        {
            "document_id": document_id,
            "knowledge_base_id": body.knowledge_base_id,
            "tag_ids": body.tag_ids,
        },
    )
    return success_response(data=result, message="文档标签已更新")


@router.get("/documents/{document_id}/tags", summary="查询文档标签列表")
async def get_document_tags(
    document_id: str,
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """查询指定文档关联的所有标签。"""
    result = await _call_kb_capability(
        request,
        "get_document_tags",
        {"document_id": document_id, "knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


@router.post("/documents/{document_id}/tags", summary="添加标签到文档")
async def add_document_tag(
    document_id: str,
    request: Request,
    body: AddDocumentTagRequest,
):
    """为文档添加一个标签（已存在则跳过）。"""
    result = await _call_kb_capability(
        request,
        "add_document_tag",
        {
            "document_id": document_id,
            "knowledge_base_id": body.knowledge_base_id,
            "tag_id": body.tag_id,
        },
    )
    return success_response(data=result, message="标签已添加到文档")


@router.delete("/documents/{document_id}/tags/{tag_id}", summary="从文档移除标签")
async def remove_document_tag(
    document_id: str,
    tag_id: str,
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """从指定文档移除一个标签。"""
    result = await _call_kb_capability(
        request,
        "remove_document_tag",
        {"document_id": document_id, "knowledge_base_id": knowledge_base_id, "tag_id": tag_id},
    )
    return success_response(data=result, message="标签已从文档移除")


def _schema_payload(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


@router.post("/search", summary="知识库语义检索")
async def search(request: Request, payload: SearchRequest):
    """语义搜索知识库（标准 RAG 检索）"""
    result = await _call_kb_capability(
        request,
        "search",
        _schema_payload(payload),
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.post("/search/enhanced", summary="知识库增强检索")
async def search_enhanced(request: Request, payload: SearchRequest):
    """增强语义搜索（RAG + 本体实例混合检索）"""
    result = await _call_kb_capability(
        request,
        "search_enhanced",
        _schema_payload(payload),
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.post("/search/ontology", summary="本体优先检索（Ontology → RAG fallback，多 KB）")
async def search_ontology(request: Request, payload: OntologySearchRequest):
    """本体优先分流查询（多 KB）：Neo4j 全文命中阈值以上走本体 LLM 回答，否则降级 RAG。"""
    result = await _call_kb_capability(
        request,
        "query_with_ontology",
        _schema_payload(payload),
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


# ── [jonex] openkb 分流 — 新增端点 ──

@router.post("/search/llmwiki", summary="OpenKB Wiki 检索（只查 openkb 管线）")
async def search_llmwiki(request: Request, payload: LlmWikiSearchRequest):
    """只查 OpenKB Wiki 编译产物的检索端点。含 lightrag KB 时显式报错，指路 /search/mix。"""
    result = await _call_kb_capability(
        request,
        "search_llmwiki",
        _schema_payload(payload),
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.post("/search/jonex_wiki", summary="jonex_wiki 管线检索（只查 jonex_wiki 管线）")
async def search_jonex_wiki(request: Request, payload: JonexWikiSearchRequest):
    """只查 jonex_wiki 编译产物的检索端点。含 lightrag/openkb KB 时显式报错，指路对应端点。"""
    result = await _call_kb_capability(
        request,
        "search_jonex_wiki",
        _schema_payload(payload),
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.post("/search/jonex_wiki/stream", summary="Wiki 检索实时进度")
async def stream_jonex_wiki(request: Request, payload: JonexWikiSearchRequest):
    import json
    user_id = _extract_user_id(request)
    client = httpx.AsyncClient(timeout=180)
    try:
        started = await _call_kb_capability(request, "start_jonex_wiki_search",
                                          _schema_payload(payload), user_id=user_id, http_client=client)
    except BaseException:
        await client.aclose()
        raise
    session_data = {"session_id": started["session_id"]}

    async def events():
        terminal = False
        previous = None
        try:
            yield "event: progress\ndata: " + json.dumps({"events": [], "status": "running",
                                                                     "session_id": started["session_id"]}) + "\n\n"
            for _ in range(1200):
                if await request.is_disconnected():
                    break
                state = await _call_kb_capability(request, "poll_jonex_wiki_search", session_data, user_id=user_id, http_client=client)
                status = state["status"]
                if status == "done":
                    terminal = True
                    public = json.dumps({"events": state["events"], "status": status}, ensure_ascii=False)
                    if public != previous:
                        yield "event: progress\ndata: " + public + "\n\n"
                    yield "event: complete\ndata: " + json.dumps(state["result"], ensure_ascii=False) + "\n\n"
                    return
                if status in ("failed", "cancelled"):
                    terminal = True
                    yield "event: error\ndata: " + json.dumps({"message": state.get("error") or status}) + "\n\n"
                    return
                public = json.dumps({"events": state["events"], "status": status}, ensure_ascii=False)
                if public != previous:
                    yield "event: progress\ndata: " + public + "\n\n"
                    previous = public
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
            yield 'event: error\ndata: {"message":"Wiki search timed out"}\n\n'
        finally:
            async def cleanup():
                try:
                    if not terminal:
                        await _call_kb_capability(request, "cancel_jonex_wiki_search", session_data,
                                                  user_id=user_id, http_client=client)
                finally:
                    await client.aclose()
            # Both native cancellation and socket cleanup outlive the browser disconnect.
            pending = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                pass

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class WikiCancelRequest(BaseModel):
    session_id: str = Field(..., regex=r"^[a-f0-9]{32}$")


@router.post("/search/jonex_wiki/cancel", summary="停止本人发起的 Wiki 检索")
async def cancel_jonex_wiki(request: Request, payload: WikiCancelRequest):
    result = await _call_kb_capability(request, "cancel_jonex_wiki_search", _schema_payload(payload),
                                      user_id=_extract_user_id(request))
    return success_response(data=result)


class WikiEvidenceRequest(BaseModel):
    knowledge_base_id: str
    query_id: str
    handle: str


@router.post("/search/jonex_wiki/evidence", summary="查看 Wiki 查询的原文证据位置")
async def wiki_evidence(request: Request, payload: WikiEvidenceRequest):
    result = await _call_kb_capability(request, "resolve_wiki_evidence",
                                      _schema_payload(payload), user_id=_extract_user_id(request))
    return success_response(data=result)


@router.post("/search/mix", summary="混合管线检索统一入口（分流器）")
async def search_mix(request: Request, payload: MixSearchRequest):
    """统一检索入口：按 pipeline_type 分组扇出到 LightRAG / OpenKB，支持混合选库。"""
    result = await _call_kb_capability(
        request,
        "search_mix",
        _schema_payload(payload),
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.post("/search/deep", summary="深度查询（意图路由 + 分解编排 + 汇总计算）")
async def search_deep(request: Request, payload: DeepSearchRequest):
    """深度查询：simple 问题直连本体优先，complex 问题走分解-取证-汇总编排。"""
    result = await _call_kb_capability(
        request,
        "deep_query",
        _schema_payload(payload),
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.post("/qa/ask", summary="问答查询（基于 RAG）")
async def qa_ask(
    request: Request,
    question: str = Body(..., embed=True, description="问题内容"),
    mode: str = Body("hybrid", embed=True, description="检索模式: naive/local/global/hybrid"),
    top_k: int = Body(5, embed=True, description="返回结果数量"),
):
    """简易问答查询（兼容前端 QA 接口）"""
    result = await _call_kb_capability(
        request,
        "search",
        {"query": question, "mode": mode, "top_k": top_k, "knowledge_base_id": ""},
    )
    return success_response(data=result)


@router.get("/documents/search/stream", summary="流式语义搜索（OpenAI 兼容 SSE）")
async def search_documents_stream(
    request: Request,
    query: str = Query(..., description="查询问题"),
    mode: str = Query("hybrid", description="检索模式: naive/local/global/hybrid/mix/bypass"),
    top_k: int = Query(5, ge=1, le=50, description="返回结果数量"),
    domain_id: str | None = Query(None, description="领域 ID"),
    knowledge_base_id: str = Query("", description="知识库 ID（必填，按知识库隔离检索）"),
):
    """流式语义搜索，返回 OpenAI 兼容的 SSE 流（delta 格式）。

    ⚠ 本路由直连 Sidecar `/invoke/stream/rag`，不经过能力层 dispatch；
    因此必须在下发流之前调用 ``assert_kb_searchable``，让能力层按**检索口径**
    完成空间 member / KB 授权与密级校验（超密级 404），否则流式检索会成为
    密级绕过通道（B2）。不能改用 ``get_knowledge_info``：那是 KB 浏览口径，
    不含仅凭空间成员身份检索的场景。
    """
    from fastapi.responses import StreamingResponse
    import httpx, json, time, uuid, re

    kb_id = (knowledge_base_id or "").strip()
    if not kb_id:
        raise InvalidParameterError(
            message=translate(
                "err.kb.stream_kb_required",
                fallback="流式检索必须指定 knowledge_base_id",
            )
        )

    # 发流前的权限/密级预检：失败（含 404）直接以普通 HTTP 错误返回，
    # 不会进入 SSE 生成器。`_call_kb_capability` 会从 Authorization 解出
    # user_id 透传，密级守卫才能拿到真实用户身份。
    await _call_kb_capability(
        request, "assert_kb_searchable", {"knowledge_base_id": kb_id},
    )

    # 流式检索请求埋点：本路由直连 Sidecar，不经过能力层 execute，若不再补一笔
    # 「检索次数/入口」指标会永久漏计。失败不阻断检索。
    try:
        await _call_kb_capability(
            request, "record_stream_search",
            {"knowledge_base_id": kb_id, "query": query, "mode": mode, "top_k": top_k},
        )
    except Exception as exc:  # noqa: BLE001 —— 埋点失败不影响检索
        logger.warning("流式检索埋点失败（不影响检索）: %s", exc)

    config = get_config()
    sidecar_url = config.SIDECAR_URL
    tenant_id = extract_tenant_id(request)
    request_id = getattr(request.state, "request_id", "")
    stream_params = {"query": query, "mode": mode, "top_k": top_k}
    if domain_id and domain_id != "all":
        stream_params["domain_id"] = domain_id
    # 知识库作用域：透传到 sidecar → atomic，按 (tenant, kb) workspace 隔离检索
    stream_params["knowledge_base_id"] = kb_id

    _RESPONSE_RE = re.compile(r'"response"\s*:\s*"((?:[^"\\]|\\.)*)"')
    # 急切捕获 locale：流式生成器 body 在中间件 reset 后迭代，惰性读会拿到 None
    _locale = getattr(request.state, "locale", None)

    async def _generate():
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        _base_choice = {"index": 0, "delta": {}, "finish_reason": None}
        _base_payload = {
            "id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": "lightrag", "choices": [None],
        }
        _stream_headers = {
            "X-API-Key": config.GATEWAY_API_KEY,
            "X-Request-ID": request_id,
            "X-Tenant-ID": tenant_id,
        }
        if _locale:
            _stream_headers["X-Lang"] = _locale

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "GET",
                    f"{sidecar_url}/invoke/stream/rag",
                    params=stream_params,
                    headers=_stream_headers,
                ) as resp:
                    resp.raise_for_status()
                    choice0 = dict(_base_choice)
                    choice0["delta"] = {"role": "assistant", "content": ""}
                    _base_payload["choices"][0] = choice0
                    yield f"data: {json.dumps(_base_payload, ensure_ascii=False)}\n\n"

                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        m = _RESPONSE_RE.search(line)
                        if m:
                            content = json.loads('"' + m.group(1) + '"')
                            choice = dict(_base_choice)
                            choice["delta"] = {"content": content}
                            _base_payload["choices"][0] = choice
                            yield f"data: {json.dumps(_base_payload, ensure_ascii=False)}\n\n"
                            continue
                        if '"references"' in line:
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if data.get("references"):
                                # 向后兼容：继续下发文本格式
                                refs_text = "\n".join(f"- {r.get('file_path', '')}" for r in data["references"])
                                choice = dict(_base_choice)
                                choice["delta"] = {"content": refs_text + "\n\n"}
                                _base_payload["choices"][0] = choice
                                yield f"data: {json.dumps(_base_payload, ensure_ascii=False)}\n\n"

                                # 结构化引用事件（D7：gateway 不连 DB，只解析 file_source）
                                if os.getenv("STREAM_REF_STRUCTURED", "").lower() in ("1", "true", "yes"):
                                    parsed = [
                                        parse_file_source(r.get("file_path", ""))
                                        for r in data["references"]
                                    ]
                                    parsed = [p for p in parsed if p.get("doc_id")]
                                    if parsed:
                                        ref_payload = dict(_base_payload)
                                        ref_payload["object"] = "references"
                                        ref_payload["references"] = parsed
                                        if "choices" in ref_payload:
                                            del ref_payload["choices"]
                                        yield f"data: {json.dumps(ref_payload, ensure_ascii=False)}\n\n"

            choice = dict(_base_choice)
            choice["delta"] = {}
            choice["finish_reason"] = "stop"
            _base_payload["choices"][0] = choice
            yield f"data: {json.dumps(_base_payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.warning(f"流式搜索中断: query={query[:50]}, error={e}")
            error_choice = {
                "index": 0,
                "delta": {"content": f"检索服务暂时不可用：{e}"},
                "finish_reason": "error",
            }
            _base_payload["choices"][0] = error_choice
            yield f"data: {json.dumps(_base_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


@router.post("/documents/references/resolve", summary="引用富化/预签名")
async def resolve_references(request: Request, payload: ReferenceResolveRequest):
    """对流式或检索路径返回的引用做 DB 富化 + 预签名。

    D7：gateway 不连 DB，只解析 file_source 后转发至此端点统一富化。
    支持 doc_ids（文档级）和 refs（保留位置信息）两种输入。
    """
    ref_dicts = [r.dict() for r in payload.refs] if payload.refs else None
    doc_ids = payload.doc_ids or None
    result = await _call_kb_capability(
        request,
        "resolve_references",
        {"refs": ref_dicts, "doc_ids": doc_ids},
    )
    return success_response(data=result)


# ══════════════════════════════════════════════════════════════
# [jonex] 以图问知识（图片 → 查询文本 → 复用现有检索链路）
# 设计：docs/knowledge-base/image-query-search-execution-plan.md §4-B6
#
# 三步式的第一步只在这里：把图交给能力服务识别成一段文本。**检索不在这里** ——
# 前端拿到 `query` 后照常调 `/search/mix` 等现有接口（方案 D1 两步式），
# 所以现有检索路由与 DTO 一行未改。
# ══════════════════════════════════════════════════════════════


def _image_query_message(key: str, fallback: str, **params: Any) -> str:
    return translate(key, params=params or None, fallback=fallback)


@router.post("/search/image-query", summary="以图问知识：图片转查询文本")
async def image_query(
    request: Request,
    files: List[UploadFile] = File(..., description="图片文件（可多张，顺序即展示顺序）"),
    question: Optional[str] = Form(None, max_length=500, description="可选的提问文字"),
):
    """把图片识别成一段可直接用于检索的文本。

    Gateway 在这里只做四件事：**逐张读字节、按同一份配置挡上限、base64 编码、转发**。
    识别与拼 query 全在能力服务里（`ImageQueryService.resolve`）。

    ⚠️ **不落盘**：与上传文档那条路有意不同 —— 那里的字节会在网关写进对象存储，
    这里不会。识别成功后字节只暂存在能力侧的 Redis，等前端保存检索历史时才由
    能力服务写对象存储（D4：「识别了但没检索」的图两个系统里都不留痕）。
    网关若先落盘，就绕开了这条规则、也绕开了 magic bytes 校验（未校验的字节进桶）。

    ⚠️ 超时用 `IMAGE_QUERY_GATEWAY_TIMEOUT`（默认 55s），**必须低于本地
    dev-gateway 写死的 60s**，否则本地联调会被它先掐成 502（F5）。
    `_invoke_sidecar_capability` 已有 `timeout:` 参数，直接传，不改签名。
    """
    config = get_config()
    max_images = int(config.IMAGE_QUERY_MAX_IMAGES)
    max_bytes = int(config.IMAGE_QUERY_MAX_BYTES)
    max_total = int(config.IMAGE_QUERY_MAX_TOTAL_BYTES)

    if not files:
        raise InvalidParameterError(
            message=_image_query_message("err.image_query.empty_result", "未识别出可检索的内容")
        )
    if len(files) > max_images:
        raise InvalidParameterError(
            message=_image_query_message(
                "err.image_query.too_many", f"最多支持 {max_images} 张图片",
                max_images=max_images,
            ),
            details={"max_images": max_images},
        )

    images: list[dict[str, Any]] = []
    total = 0
    for index, upload in enumerate(files):
        raw = await upload.read()
        # 读到超总量就**停止继续读**：后面还有几张更大的图没必要全读进内存再拒
        total += len(raw)
        if len(raw) > max_bytes:
            raise InvalidParameterError(
                message=_image_query_message("err.image_query.too_large", "图片超过大小限制"),
                details={"index": index, "max_bytes": max_bytes},
            )
        if total > max_total:
            raise InvalidParameterError(
                message=_image_query_message("err.image_query.too_large_total", "图片总大小超过限制"),
                details={"max_total_bytes": max_total},
            )
        if not raw:
            raise InvalidParameterError(
                message=_image_query_message("err.image_query.unsupported_type", "不支持的图片格式"),
                details={"index": index},
            )
        images.append(
            {
                "image_base64": base64.b64encode(raw).decode("ascii"),
                "mime_type": (upload.content_type or "").split(";", 1)[0].strip().lower() or "image/jpeg",
                "file_size": len(raw),
            }
        )

    # 不打印请求体（里面是 base64）
    logger.info("以图问知识：网关收到 %d 张图（共 %d 字节）", len(images), total)

    data: dict[str, Any] = {"images": images}
    if question and question.strip():
        data["question"] = question.strip()

    result = await _invoke_sidecar_capability(
        request,
        "image_to_query",
        data,
        timeout=float(config.IMAGE_QUERY_GATEWAY_TIMEOUT),
    )
    return success_response(data=result)


def _query_image_raw_url(image_id: str, token: str) -> str:
    return f"/api/v1/knowledge-base/search/query-images/{image_id}/raw?token={token}"


def _sign_query_images(request: Request, items: Any) -> None:
    """给历史条目里的 `query_images[]` 补签 `view_url`（就地改）。

    纯签发，与 `view-ticket` 同性质；归属已由能力服务的列表查询按 user 过滤过，
    这里不再校验（真要看图还得过 raw 端点那道 token 校验，D10）。
    """
    ttl = int(get_config().IMAGE_QUERY_VIEW_TOKEN_TTL)
    tenant_id = extract_tenant_id(request)
    user_id = _extract_user_id(request)
    if not user_id:
        return
    for item in items or []:
        images = item.get("query_images") if isinstance(item, dict) else None
        if not images:
            continue
        for image in images:
            image_id = image.get("id")
            if not image_id:
                continue
            token = generate_query_image_token(tenant_id, user_id, image_id, ttl)
            image["view_url"] = _query_image_raw_url(image_id, token)


@router.get("/search/query-images/{image_id}/view-ticket", summary="签发查询图片查看票据（以图问知识）")
async def get_query_image_view_ticket(request: Request, image_id: str):
    """签发只读、单图、短时效的查看 URL。

    与文档原文那条 `view-ticket` 的两处差异，都是有意为之：
    - **票里带 user**（查询图片按 tenant + user 隔离，只绑租户等于同租户人人可看）；
    - 归属校验**走能力服务**（本人 + 仍被未删除历史引用），不是只签不发。
    """
    ttl = int(get_config().IMAGE_QUERY_VIEW_TOKEN_TTL)
    # 能力侧只认它自己存的 user_id；这里解出来的就是「谁在请求」，
    # 由能力侧判「这张图是不是他的」（不是他的会 404，不暴露存在性）
    await _call_kb_capability(
        request, "get_query_image_ticket", {"image_id": image_id},
        user_id=_extract_user_id(request),
    )
    token = generate_query_image_token(
        extract_tenant_id(request), _extract_user_id(request) or "", image_id, ttl
    )
    return success_response(data={"url": _query_image_raw_url(image_id, token), "expires_in": ttl})


@router.get("/search/overview", summary="知识检索概览")
async def get_search_overview(
    request: Request,
    knowledge_base_id: str = Query("", min_length=0, max_length=128, description="知识库 ID（空表示全局）"),
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（按空间隔离历史数据）"),
):
    """获取知识检索页概览统计数据"""
    result = await _call_kb_capability(
        request,
        "get_search_overview",
        {"knowledge_base_id": knowledge_base_id, "domain_space_id": domain_space_id},
        user_id=_extract_user_id(request),
    )
    # [jonex] 以图问知识：概览里的近期条目同样带图，补签 view_url（纯签发）
    _sign_query_images(request, result.get("recent_items"))
    return success_response(data=result)


@router.get("/search/history", summary="查询检索历史（全局可不传 kb_id；传 history_id 则单条取快照）")
async def list_search_history(
    request: Request,
    knowledge_base_id: str = Query("", min_length=0, max_length=128, description="知识库 ID（空表示全局）"),
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（按空间隔离历史数据）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    # [jonex] 传了只取这一条并带快照（answer / references / reasoning），供点击历史时回放；
    # 不传就是列表，列表**不带**快照 —— 快照占响应体积 83%（实测 222 KB 中的 185 KB），侧栏不用。
    history_id: Optional[str] = Query(None, max_length=64, description="历史 ID（传了则只取这一条并带快照）"),
):
    """查询当前租户当前用户的检索历史"""
    result = await _call_kb_capability(
        request,
        "list_search_history",
        {
            "knowledge_base_id": knowledge_base_id,
            "domain_space_id": domain_space_id,
            "page": page,
            "page_size": page_size,
            "history_id": history_id,
        },
        user_id=_extract_user_id(request),
    )
    # [jonex] 以图问知识：给每条历史的 `query_images[]` 补签 view_url（纯签发）。
    # 归属已由能力服务的列表查询按 user 过滤过；真看图还要过 raw 端点那道 token 校验。
    _sign_query_images(request, result.get("items"))
    return success_response(data=result)


@router.post("/search/history", summary="保存检索历史")
async def save_search_history(request: Request, payload: SearchHistoryCreateRequest):
    """保存一条检索历史记录"""
    result = await _call_kb_capability(
        request,
        "save_search_history",
        _schema_payload(payload),
        user_id=_extract_user_id(request),
    )
    # [jonex] 以图问知识：**保存接口同样要补签** `view_url`。
    #
    # ⚠️ 这里漏签过一次，现象很有辨识度：以图检索完，侧栏那条历史的缩略图是**空白**，
    #    **刷新页面**（走 list 接口）才出现 —— 因为前端拿保存回包直接 `setHistory`
    #    插进侧栏，而能力侧回的 `view_url` 恒为 None（`row.to_ref()` 写死），
    #    补签本来就是 Gateway 的职责。
    # 教训：凡是**会返回历史条目**的接口都要补签，三个（save / list / overview）一个不能漏；
    # `tests/integration/test_api_gateway_image_query.py::test_save_history_signs_view_url`
    # 锁住这一条。
    _sign_query_images(request, [result])
    return success_response(data=result)


@router.delete("/search/history/{history_id}", summary="删除检索历史")
async def delete_search_history(
    request: Request,
    history_id: str,
    knowledge_base_id: str = Query("", min_length=0, max_length=128, description="知识库 ID"),
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（按空间隔离校验）"),
):
    """软删除指定检索历史记录"""
    payload = SearchHistoryDeleteRequest(
        knowledge_base_id=knowledge_base_id, domain_space_id=domain_space_id,
    )
    result = await _call_kb_capability(
        request,
        "delete_search_history",
        {"history_id": history_id, **_schema_payload(payload)},
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.delete("/search/history", summary="清空检索历史")
async def clear_search_history(
    request: Request,
    knowledge_base_id: str = Query("", min_length=0, max_length=128, description="知识库 ID"),
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（按空间隔离清理）"),
):
    """清空当前租户当前用户的全量检索历史（软删除）"""
    result = await _call_kb_capability(
        request,
        "clear_search_history",
        {"knowledge_base_id": knowledge_base_id, "domain_space_id": domain_space_id},
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 搜索结果反馈 /api/v1/knowledge-base/search/feedback
# ════════════════════════════════════════════════════════════


@router.post("/search/feedback", summary="提交搜索结果反馈")
async def submit_search_feedback(request: Request, payload: dict = Body(...)):
    """提交「有帮助/无帮助」反馈。每条记录按引用的知识库分别存储。"""
    result = await _call_kb_capability(
        request,
        "submit_search_feedback",
        payload,
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.delete("/search/feedback", summary="取消搜索结果反馈")
async def cancel_search_feedback(request: Request, payload: dict = Body(...)):
    """取消指定的搜索结果反馈。"""
    result = await _call_kb_capability(
        request,
        "cancel_search_feedback",
        payload,
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/search/feedback", summary="查询知识库的反馈记录列表")
async def list_search_feedback(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    feedback_type: str = Query(None, regex="^(like|dislike)?$", description="反馈类型过滤"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=100, description="每页条数"),
):
    """分页查询指定知识库的反馈记录，支持按反馈类型过滤。"""
    result = await _call_kb_capability(
        request,
        "list_search_feedback",
        {
            "knowledge_base_id": knowledge_base_id,
            "feedback_type": feedback_type,
            "page": page,
            "page_size": page_size,
        },
    )
    return success_response(data=result)


@router.post("/search/feedback/toggle-adopt", summary="切换反馈采纳状态")
async def toggle_search_feedback_adopt(request: Request, payload: dict = Body(...)):
    """切换指定反馈记录的采纳状态。"""
    result = await _call_kb_capability(
        request,
        "toggle_search_feedback_adopted",
        payload,
    )
    return success_response(data=result)


@router.get("/search/feedback/stats", summary="获取知识库的反馈统计")
async def get_search_feedback_stats(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """获取知识库反馈统计（总数、点赞数、踩数）。"""
    result = await _call_kb_capability(
        request,
        "get_search_feedback_stats",
        {"knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 回答级反馈 /api/v1/knowledge-base/search/answer-feedback
# 锚点 history_id = 检索历史 id；主记录保存最新反馈，事件表 operation_id 幂等
# ════════════════════════════════════════════════════════════


@router.post("/search/answer-feedback", summary="提交/更新回答反馈（点赞/点踩）")
async def submit_answer_feedback(request: Request, payload: SubmitAnswerFeedbackRequest):
    """提交回答级反馈：like/dislike + 原因 + 补充说明，按 history_id 锚定问答记录。"""
    result = await _call_kb_capability(
        request,
        "submit_answer_feedback",
        _schema_payload(payload),
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/search/answer-feedback", summary="回显回答反馈状态")
async def get_answer_feedback(
    request: Request,
    history_id: str = Query(..., min_length=1, max_length=64, description="问答记录 ID"),
):
    """按 history_id 查询当前反馈状态（无反馈或非本人返回 null）。"""
    result = await _call_kb_capability(
        request,
        "get_answer_feedback",
        {"history_id": history_id},
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/search/answer-feedback/list", summary="按知识库聚合查询回答反馈列表")
async def list_answer_feedback(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    feedback_type: Optional[str] = Query(None, regex="^(like|dislike)?$", description="按反馈类型过滤"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=100, description="每页条数"),
):
    """按知识库聚合查询回答反馈列表（含统计）。"""
    result = await _call_kb_capability(
        request,
        "list_answer_feedback",
        {
            "knowledge_base_id": knowledge_base_id,
            "feedback_type": feedback_type,
            "page": page,
            "page_size": page_size,
        },
    )
    return success_response(data=result)


@router.get("/search/answer-feedback/stats", summary="按知识库聚合统计回答反馈")
async def get_answer_feedback_stats(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """获取知识库回答反馈统计（总数、点赞数、点踩数）。"""
    result = await _call_kb_capability(
        request,
        "get_answer_feedback_stats",
        {"knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


@router.post("/search/answer-feedback/toggle-adopt", summary="切换回答反馈采纳状态")
async def toggle_answer_feedback_adopted(request: Request, payload: AnswerFeedbackToggleAdoptRequest):
    """切换指定回答反馈记录的采纳状态。"""
    result = await _call_kb_capability(
        request,
        "toggle_answer_feedback_adopted",
        _schema_payload(payload),
    )
    return success_response(data=result)


@router.delete("/search/answer-feedback/{feedback_id}", summary="删除回答反馈记录")
async def delete_answer_feedback(
    request: Request,
    feedback_id: str,
):
    """软删除指定回答反馈记录。"""
    result = await _call_kb_capability(
        request,
        "delete_answer_feedback",
        {"feedback_id": feedback_id},
    )
    return success_response(data=result)


@router.get("/parse-results/summary", summary="解析结果概要")
async def get_parse_result_summary(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """获取知识库解析结果摘要统计"""
    payload = ParseResultScopeRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(
        request,
        "get_parse_result_summary",
        _schema_payload(payload),
    )
    return success_response(data=result)


@router.get("/parse-results/documents", summary="解析文档列表")
async def get_parse_result_documents(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    status: Optional[str] = Query(None, description="状态过滤"),
):
    """获取知识库解析结果中的文档列表"""
    payload = ParseResultDocumentListRequest(
        knowledge_base_id=knowledge_base_id,
        page=page,
        page_size=page_size,
        keyword=keyword,
        status=status,
    )
    result = await _call_kb_capability(
        request,
        "get_parse_result_documents",
        _schema_payload(payload),
    )
    return success_response(data=result)


@router.get("/parse-results/entities", summary="解析实体列表")
async def get_parse_result_entities(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    entity_type: Optional[str] = Query(None, description="实体类型过滤"),
    file_path: Optional[str] = Query(None, description="来源文件路径"),
    document_id: Optional[str] = Query(None, description="文档 ID"),
):
    """获取知识库解析结果中的实体列表"""
    payload = ParseResultEntityListRequest(
        knowledge_base_id=knowledge_base_id,
        page=page,
        page_size=page_size,
        keyword=keyword,
        entity_type=entity_type,
        file_path=file_path,
        document_id=document_id,
    )
    result = await _call_kb_capability(
        request,
        "get_parse_result_entities",
        _schema_payload(payload),
    )
    return success_response(data=result)


@router.get("/parse-results/relationships", summary="解析关系列表")
async def get_parse_result_relationships(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    file_path: Optional[str] = Query(None, description="来源文件路径"),
    document_id: Optional[str] = Query(None, description="文档 ID"),
    source_entity: Optional[str] = Query(None, description="源实体名称"),
    target_entity: Optional[str] = Query(None, description="目标实体名称"),
):
    """获取知识库解析结果中的关系列表"""
    payload = ParseResultRelationshipListRequest(
        knowledge_base_id=knowledge_base_id,
        page=page,
        page_size=page_size,
        keyword=keyword,
        file_path=file_path,
        document_id=document_id,
        source_entity=source_entity,
        target_entity=target_entity,
    )
    result = await _call_kb_capability(
        request,
        "get_parse_result_relationships",
        _schema_payload(payload),
    )
    return success_response(data=result)


@router.get("/parse-results/graph-summary", summary="解析图谱概要")
async def get_parse_result_graph_summary(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """获取知识库图谱摘要统计"""
    payload = ParseResultScopeRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(
        request,
        "get_parse_result_graph_summary",
        _schema_payload(payload),
    )
    return success_response(data=result)


@router.get("/parse-results/graph", summary="解析图谱")
async def get_parse_result_graph(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    file_path: Optional[str] = Query(None, description="来源文件路径"),
    document_id: Optional[str] = Query(None, description="文档 ID"),
    limit: int = Query(200, ge=1, le=1000, description="返回节点数量上限"),
):
    """获取知识库完整图谱数据（节点 + 关系）"""
    payload = ParseResultGraphRequest(
        knowledge_base_id=knowledge_base_id,
        keyword=keyword,
        file_path=file_path,
        document_id=document_id,
        limit=limit,
    )
    result = await _call_kb_capability(
        request,
        "get_parse_result_graph",
        _schema_payload(payload),
    )
    return success_response(data=result)


# ── [jonex] OpenKB Wiki 阅读模式（编译结果页）──


@router.get("/parse-results/wiki-page", summary="读取 OpenKB Wiki 页面内容")
async def get_parse_result_wiki_page(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    path: str = Query(..., min_length=1, max_length=256, description="wiki 相对路径，如 entities/xxx"),
):
    """读取单个 Wiki 页面 markdown（实体/概念/摘要页正文）。"""
    result = await _call_kb_capability(
        request, "get_wiki_page",
        {"knowledge_base_id": knowledge_base_id, "path": path},
    )
    return success_response(data=result)


@router.get("/wiki/compilation", summary="读取 Wiki 编译状态")
async def get_wiki_compilation_state(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    result = await _call_kb_capability(request, "get_wiki_compilation_state",
                                       {"knowledge_base_id": knowledge_base_id})
    return success_response(data=result)


@router.post("/wiki/compilation/pause", summary="暂停 Wiki 编译并保留结果")
async def pause_wiki_compilation(request: Request, payload: ParseResultScopeRequest = Body(...)):
    result = await _call_kb_capability(request, "pause_wiki_compilation", _schema_payload(payload))
    return success_response(data=result)


@router.post("/wiki/compilation/resume", summary="恢复 Wiki 编译")
async def resume_wiki_compilation(request: Request, payload: ParseResultScopeRequest = Body(...)):
    result = await _call_kb_capability(request, "resume_wiki_compilation", _schema_payload(payload))
    return success_response(data=result)


@router.get("/parse-results/wiki-contents", summary="列出 OpenKB Wiki 页面树（按文档过滤，缺省=KB 级）")
async def get_parse_result_wiki_contents(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    document_id: Optional[str] = Query(default=None, max_length=64, description="文档 ID；缺省/空=KB 级全库 Wiki"),
    view: Optional[str] = Query(default=None, max_length=16,
                                description='视图：counts=只回三类页数（统计卡/tab 计数用），缺省=页面条目'),
):
    """列出 Wiki 页面树（摘要/实体/概念三组）：document_id 缺省 = KB 级全库（compile-results 页）。

    ``view=counts`` 时只返回 ``{summaries, concepts, entities}`` 三个数字加
    ``document_id``——给「只要条数」的调用方（编译资产 tab 的统计卡与 tab 计数），
    避免为三个数字搬运上千个页面对象。
    """
    payload: dict = {"knowledge_base_id": knowledge_base_id}
    if document_id:
        payload["document_id"] = document_id
    if view:
        payload["view"] = view
    result = await _call_kb_capability(request, "list_wiki_contents", payload)
    return success_response(data=result)


# ── [jonex] Wiki 阅读目录（jonex_wiki：Topic/Section/Entry，LLM 起草 + LLM 调整）──
# 目录是**纯展示投影**：落引擎库（jonex_wiki.wiki_outlines，目录功能已下沉引擎），不参与检索。
# 查询参数式风格与上方 wiki 阅读接口保持一致。


@router.get("/wiki/outline", summary="读取 Wiki 阅读目录 + 页面清单")
async def get_wiki_outline(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    document_id: Optional[str] = Query(
        None, max_length=64,
        description="文档 ID：非空则返回该文档的页面子集与目录投影（文档详情页的 Wiki 界面）",
    ),
):
    """返回目录（未生成时 outline=null）与扁平页面清单（阅读页/搜索/链接校验共用）。"""
    payload: dict[str, Any] = {"knowledge_base_id": knowledge_base_id}
    if document_id:
        payload["document_id"] = document_id
    result = await _call_kb_capability(request, "get_wiki_outline", payload)
    return success_response(data=result)


@router.post("/wiki/outline/draft", summary="LLM 起草 Wiki 阅读目录")
async def draft_wiki_outline(
    request: Request,
    payload: dict[str, Any] = Body(..., description="DraftWikiOutlineRequest"),
):
    """按页面清单让 LLM 起草主题→小节→页面目录并留档（覆盖旧 active 版本）。"""
    result = await _call_kb_capability(request, "draft_wiki_outline", payload)
    return success_response(data=result)


@router.post("/wiki/outline/adjust", summary="LLM 按指令调整 Wiki 阅读目录")
async def adjust_wiki_outline(
    request: Request,
    payload: dict[str, Any] = Body(..., description="AdjustWikiOutlineRequest"),
):
    """按自然语言要求调整现有目录（未提及部分保持原样；覆盖率暴跌则整次失败不落库）。"""
    result = await _call_kb_capability(request, "adjust_wiki_outline", payload)
    return success_response(data=result)


# ── [jonex] Wiki 阅读侧：反向链接（链入本页）/ claim 级溯源审计（列式视图）──


@router.get("/wiki/backlinks", summary="读取 Wiki 页面的链入来源（反向链接）")
async def get_wiki_backlinks(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    path: str = Query(..., min_length=1, max_length=256, description="页面 canonical 路径，如 corporate-governance/audit-committee.md"),
    document_id: Optional[str] = Query(default=None, max_length=64, description="仅统计来自该文档的页面"),
):
    """「链入本页」：frontmatter 页间链接 + 正文首次提及（与正文蓝链同口径）。"""
    payload: dict = {"knowledge_base_id": knowledge_base_id, "path": path}
    if document_id:
        payload["document_id"] = document_id
    result = await _call_kb_capability(request, "get_wiki_backlinks", payload)
    return success_response(data=result)


@router.get("/wiki/claims", summary="读取 claim 级溯源明细（列式视图·溯源审计）")
async def get_wiki_claims(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    path: Optional[str] = Query(default=None, max_length=256, description="页面 canonical 路径（按页面审计）"),
    document_id: Optional[str] = Query(default=None, max_length=64, description="来源文档 ID（按文档审计）"),
    limit: int = Query(default=500, ge=1, le=500, description="返回条数上限"),
):
    """按页面或来源文档取 claim 明细：断言 + 摘录 + 字符区间 + 支持状态。"""
    payload: dict = {"knowledge_base_id": knowledge_base_id, "limit": limit}
    if path:
        payload["path"] = path
    if document_id:
        payload["document_id"] = document_id
    result = await _call_kb_capability(request, "get_wiki_claims", payload)
    return success_response(data=result)


@router.get("/documents/{document_id}/parse-result", summary="获取单文档解析结果")
async def get_document_parse_result(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """获取指定文档的解析结果"""
    payload = DocumentParseResultRequest(
        document_id=document_id,
        knowledge_base_id=knowledge_base_id,
    )
    result = await _call_kb_capability(
        request,
        "get_document_parse_result",
        _schema_payload(payload),
    )
    return success_response(data=result)


@router.post("/documents/{document_id}/retry-ontology", summary="重试本体抽取")
async def retry_ontology_extract(
    request: Request,
    document_id: str,
    payload: OntologyRetryRequest = Body(...),
):
    """重试指定文档的本体抽取（文档须处于解析完成状态）"""
    result = await _call_kb_capability(
        request,
        "retry_ontology_extract",
        {
            "document_id": document_id,
            "knowledge_base_id": payload.knowledge_base_id,
        },
    )
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# [jonex] LLM-Wiki Schema 编译设置（方案 llmwiki-schema-settings-execution-plan §9）
# 风格对齐现有本体 API 的查询参数式（不引入路径参数新风格）
# ════════════════════════════════════════════════════════════

@router.get("/llm-wiki/schema", summary="获取 LLM-Wiki Schema（缺省自动创建默认）")
async def get_llm_wiki_schema(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    result = await _call_kb_capability(
        request, "get_llm_wiki_schema",
        {"knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


@router.put("/llm-wiki/schema", summary="保存 LLM-Wiki Schema（CAS 留档 + apply 投影）")
async def save_llm_wiki_schema(
    request: Request,
    payload: dict[str, Any] = Body(..., description="SaveLlmWikiSchemaRequest"),
):
    result = await _call_kb_capability(
        request, "save_llm_wiki_schema", payload,
    )
    return success_response(data=result)


@router.post("/llm-wiki/schema/apply", summary="同步 LLM-Wiki Schema 到 OpenKB 引擎")
async def apply_llm_wiki_schema(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    result = await _call_kb_capability(
        request, "apply_llm_wiki_schema",
        {"knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


@router.get("/llm-wiki/schema/export", summary="导出 LLM-Wiki Schema YAML")
async def export_llm_wiki_schema_yaml(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    result = await _call_kb_capability(
        request, "export_llm_wiki_schema_yaml",
        {"knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


@router.post("/llm-wiki/schema/import", summary="导入 LLM-Wiki Schema YAML（CAS 全量替换）")
async def import_llm_wiki_schema_yaml(
    request: Request,
    payload: dict[str, Any] = Body(..., description="ImportLlmWikiSchemaRequest"),
):
    result = await _call_kb_capability(
        request, "import_llm_wiki_schema_yaml", payload,
    )
    return success_response(data=result)


@router.post("/llm-wiki/schema/recompile-outdated", summary="全库重编译过期文档（一次性提交）")
async def recompile_llm_wiki_schema_outdated(
    request: Request,
    payload: dict[str, Any] = Body(..., description="RecompileOutdatedRequest"),
):
    result = await _call_kb_capability(
        request, "recompile_llm_wiki_schema_outdated_documents", payload,
    )
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 系统配额查询 /api/v1/knowledge-base/quota
# ════════════════════════════════════════════════════════════

@router.get("/quota", summary="系统配额用量查询")
async def get_quota_view(
    request: Request,
    knowledge_base_id: Optional[str] = Query(None, description="知识库 ID（可选，传则 knowledgeBaseDocumentLimit 按该 KB 文档数统计）"),
):
    """查询当前租户的 12 项系统配额 used/reserved/limit/unit。

    存储类（*MiB）limit/used 返回 Byte，前端除以 1048576 展示；单位 unit 仅作标签。
    """
    payload: dict[str, Any] = {}
    if knowledge_base_id:
        payload["knowledge_base_id"] = knowledge_base_id
    result = await _call_kb_capability(request, "get_quota_view", payload)
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 知识库信息管理（KnowledgeInfo CRUD）
# ════════════════════════════════════════════════════════════

@router.get("/kb-types", summary="可创建的知识库类型（部署级配置）")
async def get_kb_type_options(request: Request):
    """本部署允许**创建**哪些知识库类型（由 env `KB_ENABLED_TYPES` 控制）。

    前端「新建知识库」弹窗据此过滤类型选项。返回**只有 code**、不带文案 ——
    i18n 的 label/description 留在前端（`locales/{zh,en}.json` 本来就有一份）::

        {"kb_types": ["lightrag", "jonex_wiki"], "default": "lightrag"}

    ⚠ 三条边界：
    * 只管「能造哪些」。被禁类型的**存量库照常出现在列表里、照常可读可检索可上传**，
      本接口不参与任何「隐藏老库」的判定。
    * 这是**下发口**，不是闸门。真正的拦截在 `KnowledgeInfoService.create()`
      （`resolve_creatable_kb_type`）—— 直接 POST 一个被禁类型照样会被拒。
    * 变量未设置/留空 = 三种全开（升级零影响）。
    """
    result = await _call_kb_capability(request, "get_kb_type_options", {})
    return success_response(data=result)


@router.get("/knowledge-info", summary="知识库列表")
async def list_knowledge_info(
    request: Request,
    space_id: Optional[str] = Query(None, description="所属空间 ID"),
    status: Optional[str] = Query(None, description="状态过滤"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
    scope: Optional[str] = Query("manage", description="manage=管理视角 | search=检索视角"),
):
    result = await _call_kb_capability(request, "list_knowledge_info", {
        "space_id": space_id, "status": status, "keyword": keyword,
        "offset": offset, "limit": limit, "scope": scope,
    })
    return success_response(data=result)


@router.post("/knowledge-info", summary="创建知识库")
async def create_knowledge_info(request: Request, payload: dict = Body(...)):
    result = await _call_kb_capability(request, "create_knowledge_info", payload)
    return success_response(data=result)


@router.get("/knowledge-info/{kb_id}", summary="知识库详情")
async def get_knowledge_info(request: Request, kb_id: str,
                             with_stats: bool = Query(False)):
    result = await _call_kb_capability(request, "get_knowledge_info", {
        "kb_id": kb_id, "with_stats": with_stats,
    })
    return success_response(data=result)


@router.patch("/knowledge-info/{kb_id}", summary="更新知识库")
async def update_knowledge_info(request: Request, kb_id: str, payload: dict = Body(...)):
    payload["kb_id"] = kb_id
    result = await _call_kb_capability(request, "update_knowledge_info", payload)
    return success_response(data=result)


@router.delete("/knowledge-info/{kb_id}", summary="删除知识库")
async def delete_knowledge_info(request: Request, kb_id: str):
    result = await _call_kb_capability(request, "delete_knowledge_info", {"kb_id": kb_id})
    return success_response(data=result)


@router.get("/knowledge-info/{kb_id}/permissions", summary="知识库授权成员列表")
async def get_kb_permissions(request: Request, kb_id: str):
    result = await _call_kb_capability(request, "get_kb_permissions", {"kb_id": kb_id})
    return success_response(data=result)


@router.put("/knowledge-info/{kb_id}/permissions", summary="设置知识库授权成员")
async def set_kb_permissions(request: Request, kb_id: str, payload: dict = Body(...)):
    """设置指定知识库的授权成员（editor/viewer 叠加授权）"""
    payload["kb_id"] = kb_id
    result = await _call_kb_capability(request, "set_kb_permissions", payload)
    return success_response(data=result)


@router.get("/knowledge-info/{kb_id}/permission-candidates", summary="知识库添加成员候选用户")
async def get_kb_permission_candidates(request: Request, kb_id: str):
    """父空间成员池中可被加入该知识库的用户（排除已有 KB 成员与操作者本人）"""
    result = await _call_kb_capability(request, "get_kb_permission_candidates", {"kb_id": kb_id})
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 领域空间 /api/v1/knowledge-base/spaces  ->  business.knowledge_base.v1
# ════════════════════════════════════════════════════════════

@router.get("/spaces", summary="获取领域空间列表")
async def list_spaces(
    request: Request,
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取领域空间分页列表"""
    result = await _call_kb_capability(request, "list_spaces", {"offset": offset, "limit": limit})
    return success_response(data=result)


@router.post("/spaces", summary="创建领域空间")
async def create_space(request: Request, payload: dict = Body(...)):
    """创建新的领域空间"""
    result = await _call_kb_capability(request, "create_space", payload)
    return success_response(data=result)


@router.get("/spaces/{space_id}", summary="获取领域空间详情")
async def get_space(request: Request, space_id: str):
    """获取指定领域空间的详细信息"""
    result = await _call_kb_capability(request, "get_space", {"space_id": space_id})
    return success_response(data=result)


@router.patch("/spaces/{space_id}", summary="更新领域空间")
async def update_space(request: Request, space_id: str, payload: dict = Body(...)):
    """更新指定领域空间的配置"""
    payload["space_id"] = space_id
    result = await _call_kb_capability(request, "update_space", payload)
    return success_response(data=result)


@router.delete("/spaces/{space_id}", summary="删除领域空间")
async def delete_space(request: Request, space_id: str):
    """删除指定领域空间"""
    result = await _call_kb_capability(request, "delete_space", {"space_id": space_id})
    return success_response(data=result)


@router.get("/spaces/{space_id}/permissions", summary="获取领域空间权限")
async def get_space_permissions(request: Request, space_id: str):
    """获取指定领域空间的权限设置"""
    result = await _call_kb_capability(request, "get_space_permissions", {"space_id": space_id})
    return success_response(data=result)


@router.put("/spaces/{space_id}/permissions", summary="设置领域空间权限")
async def set_space_permissions(request: Request, space_id: str, payload: dict = Body(...)):
    """设置指定领域空间的权限"""
    payload["space_id"] = space_id
    result = await _call_kb_capability(request, "set_space_permissions", payload)
    return success_response(data=result)


@router.get("/spaces/{space_id}/permission-candidates", summary="领域空间添加成员候选用户")
async def get_space_permission_candidates(request: Request, space_id: str):
    """本租户活跃用户中可被加入该空间的用户（排除已有空间成员与操作者本人）"""
    result = await _call_kb_capability(request, "get_space_permission_candidates", {"space_id": space_id})
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 领域服务 /api/v1/knowledge-base/services  ->  business.knowledge_base.v1
# ════════════════════════════════════════════════════════════

@router.get("/services", summary="获取领域服务列表")
async def list_services(
    request: Request,
    space_id: Optional[str] = Query(None, description="所属空间 ID"),
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
    scope: Optional[str] = Query("manage", description="manage=管理视角 | search=检索视角"),
):
    """获取领域服务分页列表"""
    result = await _call_kb_capability(request, "list_services", {"space_id": space_id, "offset": offset, "limit": limit, "scope": scope})
    return success_response(data=result)


@router.post("/services", summary="创建领域服务")
async def create_service(request: Request, payload: dict = Body(...)):
    """创建新的领域服务"""
    result = await _call_kb_capability(request, "create_service", payload)
    return success_response(data=result)


@router.get("/services/{service_id}", summary="获取领域服务详情")
async def get_service(request: Request, service_id: str):
    """获取指定领域服务的详细信息"""
    result = await _call_kb_capability(request, "get_service", {"service_id": service_id})
    return success_response(data=result)


@router.patch("/services/{service_id}", summary="更新领域服务")
async def update_service(request: Request, service_id: str, payload: dict = Body(...)):
    """更新指定领域服务的配置"""
    payload["service_id"] = service_id
    result = await _call_kb_capability(request, "update_service", payload)
    return success_response(data=result)


@router.delete("/services/{service_id}", summary="删除领域服务")
async def delete_service(request: Request, service_id: str):
    """删除指定领域服务"""
    result = await _call_kb_capability(request, "delete_service", {"service_id": service_id})
    return success_response(data=result)


@router.post("/services/{service_id}/enable", summary="启用领域服务")
async def enable_service(request: Request, service_id: str):
    """启用指定领域服务"""
    result = await _call_kb_capability(request, "enable_service", {"service_id": service_id})
    return success_response(data=result, message="领域服务已启用")


@router.post("/services/{service_id}/disable", summary="停用领域服务")
async def disable_service(request: Request, service_id: str):
    """停用指定领域服务"""
    result = await _call_kb_capability(request, "disable_service", {"service_id": service_id})
    return success_response(data=result, message="领域服务已停用")



@router.get("/services/{service_id}/api-keys", summary="获取 API Key 列表")
async def list_api_keys(request: Request, service_id: str):
    """获取指定领域服务的所有 API Key"""
    result = await _call_kb_capability(request, "list_service_api_keys", {"service_id": service_id})
    return success_response(data=result)


@router.post("/services/{service_id}/api-keys", summary="创建 API Key")
async def create_api_key(request: Request, service_id: str, payload: dict = Body(...)):
    """创建新的 API Key"""
    payload["service_id"] = service_id
    result = await _call_kb_capability(request, "create_service_api_key", payload)
    return success_response(data=result)


@router.delete("/services/{service_id}/api-keys/{key_id}", summary="删除 API Key")
async def delete_api_key(request: Request, service_id: str, key_id: str):
    """删除指定 API Key"""
    result = await _call_kb_capability(request, "delete_service_api_key", {"service_id": service_id, "key_id": key_id})
    return success_response(data=result)


@router.post("/services/{service_id}/api-keys/rotate", summary="轮换服务 API Key")
async def rotate_api_key(request: Request, service_id: str):
    """轮换指定领域服务的 API Key"""
    result = await _call_kb_capability(request, "rotate_service_api_key", {"service_id": service_id})
    return success_response(data=result)


@router.get("/services/{service_id}/configs", summary="获取服务配置")
async def get_service_configs(request: Request, service_id: str):
    """获取指定领域服务的配置项"""
    result = await _call_kb_capability(request, "get_service_configs", {"service_id": service_id})
    return success_response(data=result)


@router.put("/services/{service_id}/configs", summary="更新服务配置")
async def update_service_configs(request: Request, service_id: str, payload: dict = Body(...)):
    """更新指定领域服务的配置项"""
    payload["service_id"] = service_id
    result = await _call_kb_capability(request, "update_service_configs", payload)
    return success_response(data=result)


@router.get("/services/{service_id}/search", summary="搜索领域服务")
async def search_service(
    request: Request,
    service_id: str,
    query: str = Query(..., description="搜索关键词"),
):
    """在指定领域服务中搜索"""
    result = await _call_kb_capability(request, "search_service", {"service_id": service_id, "query": query})
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 独立本体抽取（native ontology extraction，§5.2 外部调用契约）
# ════════════════════════════════════════════════════════════

# options 白名单与上限：封死 LLM 调用放大面（gleaning/sample_runs 均翻倍调用成本）
_OPTION_SPECS: dict[str, tuple[str, tuple]] = {
    "batch_size": ("int", (1, 64)),
    "gleaning": ("int", (0, 2)),
    "sample_runs": ("int", (1, 3)),
    "max_tokens": ("int", (1, 16384)),
    "temperature": ("float", (0.0, 1.5)),
    "json_mode": ("bool", ()),
    "table_objects_handled": ("bool", ()),
}


def _validate_native_extract_request(payload: "NativeOntologyExtractRequest") -> None:
    """Gateway 预校验（不浪费 LLM 调用）：chunks 完整性 / tbox 可用性 / options 白名单。

    口径与 NativeOntologyExtractor.extract 的 falsy 判定精确对齐：
    tbox 需含非空 entity_types 或 relation_types（缺失时抽取器 ok=false）。
    """
    chunks = payload.chunks
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise InvalidParameterError(
                message=translate(
                    "err.kb.ontology_extract_chunk_invalid",
                    params={"index": index, "field": "dict"},
                    fallback=f"chunks 第 {index} 项必须是对象",
                )
            )
        for field in ("chunk_id", "content"):
            value = (chunk.get(field) or "").strip()
            if not value:
                raise InvalidParameterError(
                    message=translate(
                        "err.kb.ontology_extract_chunk_invalid",
                        params={"index": index, "field": field},
                        fallback=f"chunks 第 {index} 项缺少 {field} 字段",
                    )
                )

    tbox = payload.tbox or {}
    if not tbox.get("entity_types") and not tbox.get("relation_types"):
        raise InvalidParameterError(
            message=translate(
                "err.kb.ontology_extract_tbox_required",
                fallback="tbox 缺少 entity_types 或 relation_types 定义，无法抽取",
            )
        )

    options = payload.options or {}
    unknown = set(options.keys()) - set(_OPTION_SPECS.keys())
    if unknown:
        raise InvalidParameterError(
            message=translate(
                "err.kb.ontology_extract_options_invalid",
                params={"detail": f"未知参数: {', '.join(sorted(unknown))}"},
                fallback=f"options 参数非法: 未知参数 {', '.join(sorted(unknown))}",
            )
        )
    for key, value in options.items():
        kind, bounds = _OPTION_SPECS[key]
        type_ok = isinstance(value, bool) if kind == "bool" else (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        if not type_ok:
            raise InvalidParameterError(
                message=translate(
                    "err.kb.ontology_extract_options_invalid",
                    params={"detail": f"{key} 类型必须为 {kind}"},
                    fallback=f"options 参数非法: {key} 类型必须为 {kind}",
                )
            )
        if bounds and not (bounds[0] <= value <= bounds[1]):
            raise InvalidParameterError(
                message=translate(
                    "err.kb.ontology_extract_options_invalid",
                    params={"detail": f"{key} 超出范围 {bounds}"},
                    fallback=f"options 参数非法: {key} 超出范围 {bounds}",
                )
            )


@router.post("/ontology/extract", summary="独立本体抽取（chunk + TBox → 本体实体/关系，不入库）")
async def native_ontology_extract(request: Request, payload: NativeOntologyExtractRequest):
    """给定 chunk 原文与 TBox 定义，返回本体实体/关系抽取结果（纯计算、不写任何存储）。

    链路：Sidecar invoke → atomic-rag action `native_ontology_extract`。
    注意：extractor 运行期 ok=false（如全部为表格 chunk）时经 500 返回；
    超时由 GATEWAY_RAG_EXTRACT_TIMEOUT（默认 600s）控制。
    """
    _validate_native_extract_request(payload)
    tenant_id = extract_tenant_id(request)
    data = {
        "chunks": payload.chunks,
        "tbox": payload.tbox,
        "scope": {
            "tenant_id": tenant_id,
            "knowledge_base_id": payload.knowledge_base_id or "",
            "document_id": payload.document_id or "",
            "trace_id": getattr(request.state, "request_id", ""),
        },
        "options": payload.options or {},
    }
    result = await _call_rag_capability(
        request, "native_ontology_extract", data,
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 本体编译 schema 编辑器
# ════════════════════════════════════════════════════════════

@router.get("/ontology/editor-state", summary="获取本体编译编辑状态")
async def get_ontology_editor_state(
    request: Request,
    knowledge_base_id: str = Query(..., description="知识库 ID"),
):
    """获取知识库的本体编译编辑状态（绑定 + 编译 schema + 模板摘要）"""
    result = await _call_kb_capability(request, "get_editor_state", {"knowledge_base_id": knowledge_base_id})
    return success_response(data=result)


@router.get("/ontology/compiled-schema", summary="获取本体编译 schema")
async def get_ontology_compiled_schema(
    request: Request,
    knowledge_base_id: str = Query(..., description="知识库 ID"),
):
    """查询知识库当前 compiled schema。"""
    result = await _call_kb_capability(request, "get_compiled_schema", {"knowledge_base_id": knowledge_base_id})
    return success_response(data=result)


@router.put("/ontology/compiled-schema", summary="保存本体编译 schema")
async def save_ontology_compiled_schema(request: Request, payload: dict = Body(...)):
    """前端编辑器保存实体类型和关系类型（用户手工编辑后写入 compiled_schema）"""
    result = await _call_kb_capability(request, "save_compiled_schema", payload)
    return success_response(data=result)


@router.post("/ontology/compiled-schema/reseed", summary="从模板重新生成编译 schema")
async def reseed_ontology_compiled_schema(request: Request, payload: dict = Body(...)):
    """绑定模板场景并从模板重新编译，覆盖当前 compiled_schema。

    可选 apply_to_documents=true：编译成功后顺带把该 KB 文档置 PENDING（对账按新 schema 重抽）。
    """
    result = await _call_kb_capability(request, "reseed_compiled_schema", payload)
    return success_response(data=result)


@router.post("/ontology/compiled-schema/recompile", summary="从模板/yaml 重新编译 schema")
async def recompile_ontology_compiled_schema(request: Request, payload: dict = Body(...)):
    """从模板/yaml 重新编译 compiled schema（P2-H）。

    - 遇 schema_mode=manual_edited 返回 409（提示改用 reseed）；recompile 永不覆盖人工编辑。
    - force=true：对非人工编辑跳过 source_hash 短路、无条件重生成。
    - apply_to_documents=true：编译成功后顺带触发该 KB 文档批量重抽（对账被动）。
    """
    result = await _call_kb_capability(request, "recompile_schema", payload)
    return success_response(data=result)


@router.get("/ontology/compiled-schema/yaml", summary="导出本体编译 schema 为 YAML")  # [jonex]
async def export_ontology_compiled_schema_yaml(
    request: Request,
    knowledge_base_id: str = Query(..., description="知识库 ID"),
):
    """将当前知识库的 compiled schema 导出为 YAML 文本。

    返回 JSON: { filename, yaml_text, warnings }。
    yaml_text 是完整的 YAML 字符串，可直接保存为 .yaml 文件。
    """
    result = await _call_kb_capability(
        request, "export_compiled_schema_yaml", {"knowledge_base_id": knowledge_base_id},
    )
    return success_response(data=result)


@router.post("/ontology/compiled-schema/yaml/import", summary="从 YAML 导入本体编译 schema")  # [jonex]
async def import_ontology_compiled_schema_yaml(
    request: Request,
    knowledge_base_id: str = Form(..., description="知识库 ID"),
    expected_schema_version: int = Form(..., description="期望的 schema 版本（乐观锁）"),
    file: UploadFile = File(..., description="YAML 文件"),
    dry_run: bool = Form(False, description="是否仅试运行（默认 false，实际写入）"),
):
    """从 YAML 文件导入 compiled schema。

    - Gateway 读取文件内容转为 yaml_text 字符串，经 Sidecar /invoke 传给能力层。
    - dry_run=true：仅解析校验并返回摘要，不写入数据库。
    - dry_run=false：解析后调用 save_compiled_schema 写入。
    - 文件过大（>50MB）或非 UTF-8 编码会返回 400。
    """
    raw = await file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise InvalidParameterError(message=translate("err.kb.yaml_file_too_large", fallback="YAML 文件不能超过 50MB"))
    try:
        yaml_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidParameterError(message=translate("err.kb.yaml_not_utf8", fallback="YAML 文件必须是 UTF-8 编码"))
    if not yaml_text.strip():
        raise InvalidParameterError(message=translate("err.kb.yaml_file_empty", fallback="YAML 文件内容不能为空"))

    result = await _call_kb_capability(
        request,
        "import_compiled_schema_yaml",
        {
            "knowledge_base_id": knowledge_base_id,
            "yaml_text": yaml_text,
            "expected_schema_version": expected_schema_version,
            "dry_run": dry_run,
        },
    )
    return success_response(data=result)


@router.post("/documents/reextract", summary="按新 schema 批量重抽本体")
async def reextract_kb_documents(request: Request, payload: dict = Body(...)):
    """KB 级把文档本体状态置 PENDING（对账被动按当前 compiled schema 重抽，不重解析文件）。

    payload: {knowledge_base_id, document_ids?, only_outdated?, only_ready?}
    """
    result = await _call_kb_capability(request, "reextract_kb_documents", payload)
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 本体同义词（KB 级同义词组）
# ════════════════════════════════════════════════════════════

@router.get("/ontology/synonyms", summary="同义词组列表")
async def list_synonyms(
    request: Request,
    knowledge_base_id: str = Query(..., description="知识库 ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    """分页查询知识库同义词组。"""
    result = await _call_kb_capability(
        request,
        "list_synonyms",
        {"knowledge_base_id": knowledge_base_id, "page": page, "page_size": page_size},
    )
    return success_response(data=result)


@router.post("/ontology/synonyms", summary="新建同义词组")
async def create_synonym(request: Request, payload: dict = Body(...)):
    result = await _call_kb_capability(request, "create_synonym", payload)
    return success_response(data=result)


@router.patch("/ontology/synonyms/{synonym_id}", summary="更新同义词组")
async def update_synonym(request: Request, synonym_id: str, payload: dict = Body(...)):
    payload["synonym_id"] = synonym_id
    result = await _call_kb_capability(request, "update_synonym", payload)
    return success_response(data=result)


@router.delete("/ontology/synonyms/{synonym_id}", summary="删除同义词组")
async def delete_synonym(request: Request, synonym_id: str):
    result = await _call_kb_capability(request, "delete_synonym", {"synonym_id": synonym_id})
    return success_response(data=result)


@router.post("/ontology/synonyms/import", summary="批量导入同义词")
async def import_synonyms(request: Request, payload: dict = Body(...)):
    result = await _call_kb_capability(request, "import_synonyms", payload)
    return success_response(data=result)


@router.get("/ontology/statistics", summary="本体 kb 维度统计")
async def get_ontology_statistics(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """获取知识库的源文件数、本体实例数、本体关系数统计"""
    payload = OntologyStatsRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(request, "get_ontology_statistics", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/instances", summary="本体实例列表（kb 维度，支持按文档过滤）")
async def list_ontology_instances(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    entity_type: Optional[str] = Query(None, description="实体类型过滤"),
    keyword: Optional[str] = Query(None, description="关键词搜索（子串匹配）"),
    document_id: Optional[str] = Query(None, description="按来源文档 ID 过滤"),
):
    """分页查询知识库的本体实例，支持按类型、关键词和文档过滤"""
    payload = OntologyInstanceListRequest(
        knowledge_base_id=knowledge_base_id,
        page=page,
        page_size=page_size,
        entity_type=entity_type,
        keyword=keyword,
        document_id=document_id,
    )
    result = await _call_kb_capability(request, "list_ontology_instances", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/relations", summary="本体关系列表（kb 维度，支持按文档过滤）")
async def list_ontology_relations(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    relation_type: Optional[str] = Query(None, description="关系类型过滤"),
    source_name: Optional[str] = Query(None, description="源实体名称过滤"),
    target_name: Optional[str] = Query(None, description="目标实体名称过滤"),
    source_type: Optional[str] = Query(None, description="源实体类型精确过滤"),
    target_type: Optional[str] = Query(None, description="目标实体类型精确过滤"),
    keyword: Optional[str] = Query(None, description="通用关键词模糊搜索（匹配关系类型+源实体名+目标实体名）"),
    document_id: Optional[str] = Query(None, description="按来源文档 ID 过滤（两端任一节点含该 doc 即命中）"),
):
    """分页查询知识库的本体关系，两端节点均在 kb 内，支持按文档过滤和关键词搜索"""
    payload = OntologyRelationListRequest(
        knowledge_base_id=knowledge_base_id,
        page=page,
        page_size=page_size,
        relation_type=relation_type,
        source_name=source_name,
        target_name=target_name,
        source_type=source_type,
        target_type=target_type,
        keyword=keyword,
        document_id=document_id,
    )
    result = await _call_kb_capability(request, "list_ontology_relations", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/entity-types", summary="本体实体类型列表（含实例数）")
async def list_ontology_entity_types(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    document_id: Optional[str] = Query(None, description="按来源文档过滤计数"),
):
    """获取知识库实体类型定义及其对应的 Neo4j 实例计数"""
    payload = OntologyStatsRequest(knowledge_base_id=knowledge_base_id, document_id=document_id)
    result = await _call_kb_capability(request, "list_ontology_entity_types", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/relation-types", summary="本体关系类型列表（含实例数）")
async def list_ontology_relation_types(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    document_id: Optional[str] = Query(None, description="按来源文档过滤计数"),
):
    """获取知识库关系类型定义及其对应的 Neo4j 关系计数"""
    payload = OntologyStatsRequest(knowledge_base_id=knowledge_base_id, document_id=document_id)
    result = await _call_kb_capability(request, "list_ontology_relation_types", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/graph", summary="本体图谱数据（nodes + edges + 统计）")
async def get_ontology_graph(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    limit: int = Query(500, ge=1, le=2000, description="节点数量上限（按连接度取 top-N）"),
    entity_types: Optional[list[str]] = Query(None, description="按实体类型过滤，可重复传参"),
    document_id: Optional[str] = Query(None, min_length=1, max_length=128, description="按来源文档过滤"),
):
    """获取知识库图谱数据（按连接度取 top-N 节点），返回 nodes、edges 及全量统计，
    用于前端力导向图渲染与类型筛选/数量提示"""
    payload = OntologyGraphRequest(
        knowledge_base_id=knowledge_base_id,
        limit=limit,
        entity_types=entity_types,
        document_id=document_id,
    )
    result = await _call_kb_capability(request, "get_ontology_graph", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/neighbors", summary="实体一跳邻居展开（nodes + edges）")
async def expand_ontology_neighbors(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    entity_type: str = Query(..., min_length=1, max_length=128, description="实体类型"),
    canonical_name: str = Query(..., min_length=1, max_length=512, description="实体规范名"),
    limit: int = Query(50, ge=1, le=200, description="邻居数量上限"),
    document_id: Optional[str] = Query(None, description="按来源文档过滤邻居节点"),
):
    """展开指定实体的一跳邻居，返回邻居节点与连接边，用于前端双击节点增量扩展"""
    payload = OntologyNeighborRequest(
        knowledge_base_id=knowledge_base_id,
        entity_type=entity_type,
        canonical_name=canonical_name,
        limit=limit,
        document_id=document_id,
    )
    result = await _call_kb_capability(request, "expand_ontology_neighbors", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/entities/search", summary="本体实例名称搜索")
async def search_ontology_entities(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    keyword: str = Query("", min_length=0, max_length=256, description="搜索关键词（模糊匹配实例名称/别名；空串返回空列表）"),
    limit: int = Query(20, ge=1, le=200, description="返回条数上限"),
    document_id: Optional[str] = Query(None, description="限定候选实体所属文档 ID（文档页表单带；KB 级表单不带）"),
):
    """模糊搜索本体实例，用于实例/关系表单中快速定位实体。

    复用 Neo4j 全文索引 ont_entity_ft（cjk analyzer）实现中文模糊匹配。
    按租户和知识库隔离，仅返回非 stub 正式实体。
    `document_id` 有值 → 只在该文档关联的实体里选（文档页建关系时不该看到全库实体）。
    """
    payload = OntologyEntitySearchRequest(
        knowledge_base_id=knowledge_base_id,
        keyword=keyword,
        limit=limit,
        document_id=document_id,
    )
    result = await _call_kb_capability(request, "search_ontology_entities", _schema_payload(payload))
    return success_response(data=result, message="success")


# ════════════════════════════════════════════════════════════
# 本体实例/关系 创建、编辑与删除
# ════════════════════════════════════════════════════════════


@router.post("/ontology/instances", summary="创建本体实例")
async def create_ontology_instance(
    request: Request,
    body: CreateOntologyInstanceRequest = Body(..., description="创建本体实例"),
):
    """创建本体实例（实体节点）。

    body 里的 `document_id`（可选）区分入口语义：文档结果页创建时带上 → 节点 `doc_ids`
    含该文档，`GET /ontology/instances?document_id=` 可见；KB 级「编译资产」页不带 → 全库实例。
    """
    result = await _call_kb_capability(request, "create_ontology_instance", _schema_payload(body))
    return success_response(data=result, message="本体实例已创建")


@router.post("/ontology/relations", summary="创建本体关系")
async def create_ontology_relation(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    source_entity_type: str = Query(..., min_length=1, max_length=128, description="源实体类型"),
    source_canonical_name: str = Query(..., min_length=1, max_length=128, description="源实体规范名"),
    relation_type: str = Query(..., min_length=1, max_length=128, description="关系类型"),
    target_entity_type: str = Query(..., min_length=1, max_length=128, description="目标实体类型"),
    target_canonical_name: str = Query(..., min_length=1, max_length=128, description="目标实体规范名"),
    document_id: Optional[str] = Query(None, description="来源文档 ID（从文档页创建时带；写入 doc_ids 关联）"),
    attributes: Optional[dict] = Body(None, description="扩展属性"),
):
    """创建本体关系（实体间关系边）。`document_id` 口径同创建实例。"""
    payload = {
        "knowledge_base_id": knowledge_base_id,
        "source_entity_type": source_entity_type,
        "source_canonical_name": source_canonical_name,
        "relation_type": relation_type,
        "target_entity_type": target_entity_type,
        "target_canonical_name": target_canonical_name,
    }
    if document_id:
        payload["document_id"] = document_id
    if attributes is not None:
        payload["attributes"] = attributes
    result = await _call_kb_capability(request, "create_ontology_relation", payload)
    return success_response(data=result, message="本体关系已创建")


@router.put("/ontology/instances/{entity_type}/{canonical_name}", summary="更新本体实例")
async def update_ontology_instance(
    request: Request,
    entity_type: str,
    canonical_name: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    updates: dict = Body(..., embed=True, description="待更新字段（name/aliases/description/attributes）"),
):
    """更新本体实例的名称/别名/描述/属性。

    entity_type 和 canonical_name 从 URL 路径获取，knowledge_base_id 从查询参数获取，
    请求体只接收 ``updates`` 字典。
    """
    payload = {
        "knowledge_base_id": knowledge_base_id,
        "entity_type": entity_type,
        "canonical_name": canonical_name,
        "updates": updates,
    }
    result = await _call_kb_capability(request, "update_ontology_instance", payload)
    return success_response(data=result, message="本体实例已更新")


@router.delete("/ontology/instances/{entity_type}/{canonical_name}", summary="删除本体实例")
async def delete_ontology_instance(
    request: Request,
    entity_type: str,
    canonical_name: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """删除本体实例及其关联关系。"""
    payload = {
        "knowledge_base_id": knowledge_base_id,
        "entity_type": entity_type,
        "canonical_name": canonical_name,
    }
    result = await _call_kb_capability(request, "delete_ontology_instance", payload)
    return success_response(data=result, message="本体实例已删除")


@router.put("/ontology/relations", summary="更新本体关系")
async def update_ontology_relation(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    source_entity_type: str = Query(..., min_length=1, max_length=128, description="源实体类型"),
    source_canonical_name: str = Query(..., min_length=1, max_length=128, description="源实体规范名"),
    relation_type: str = Query(..., min_length=1, max_length=128, description="当前关系类型"),
    target_entity_type: str = Query(..., min_length=1, max_length=128, description="目标实体类型"),
    target_canonical_name: str = Query(..., min_length=1, max_length=128, description="目标实体规范名"),
    updates: dict = Body(..., description="待更新字段（relation_type/attributes）"),
):
    """更新本体关系的类型或属性。

    通过源对象（类型+名称）、关系名称、目标对象（类型+名称）唯一确定一条关系，
    ``updates`` 字典支持更新 ``relation_type`` 和 ``attributes``。
    """
    payload = {
        "knowledge_base_id": knowledge_base_id,
        "source_entity_type": source_entity_type,
        "source_canonical_name": source_canonical_name,
        "relation_type": relation_type,
        "target_entity_type": target_entity_type,
        "target_canonical_name": target_canonical_name,
        "updates": updates,
    }
    result = await _call_kb_capability(request, "update_ontology_relation", payload)
    return success_response(data=result, message="本体关系已更新")


@router.delete("/ontology/relations", summary="删除本体关系")
async def delete_ontology_relation(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    source_entity_type: str = Query(..., min_length=1, max_length=128, description="源实体类型"),
    source_canonical_name: str = Query(..., min_length=1, max_length=128, description="源实体规范名"),
    relation_type: str = Query(..., min_length=1, max_length=128, description="关系类型"),
    target_entity_type: str = Query(..., min_length=1, max_length=128, description="目标实体类型"),
    target_canonical_name: str = Query(..., min_length=1, max_length=128, description="目标实体规范名"),
):
    """删除本体关系（不删除两端实体）。"""
    payload = {
        "knowledge_base_id": knowledge_base_id,
        "source_entity_type": source_entity_type,
        "source_canonical_name": source_canonical_name,
        "relation_type": relation_type,
        "target_entity_type": target_entity_type,
        "target_canonical_name": target_canonical_name,
    }
    result = await _call_kb_capability(request, "delete_ontology_relation", payload)
    return success_response(data=result, message="本体关系已删除")


# ════════════════════════════════════════════════════════════
# 数据源接入方式 /api/v1/knowledge-base/data-sources
# ════════════════════════════════════════════════════════════

@router.get("/data-sources", summary="数据源列表")
async def list_data_sources(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    result = await _call_kb_capability(request, "list_data_sources", {"knowledge_base_id": knowledge_base_id})
    return success_response(data=result)


@router.post("/data-sources", summary="创建数据源")
async def create_data_source(request: Request, payload: dict = Body(...)):
    result = await _call_kb_capability(request, "create_data_source", payload)
    return success_response(data=result)


@router.get("/data-sources/{ds_id}", summary="数据源详情")
async def get_data_source(request: Request, ds_id: str):
    result = await _call_kb_capability(request, "get_data_source", {"ds_id": ds_id})
    return success_response(data=result)


@router.patch("/data-sources/{ds_id}", summary="更新数据源")
async def update_data_source(request: Request, ds_id: str, payload: dict = Body(...)):
    payload["ds_id"] = ds_id
    result = await _call_kb_capability(request, "update_data_source", payload)
    return success_response(data=result)


@router.delete("/data-sources/{ds_id}", summary="删除数据源")
async def delete_data_source(request: Request, ds_id: str):
    result = await _call_kb_capability(request, "delete_data_source", {"ds_id": ds_id})
    return success_response(data=result)


@router.post("/data-sources/{ds_id}/test", summary="测试连接")
async def test_data_source(request: Request, ds_id: str):
    result = await _call_kb_capability(request, "test_data_source", {"ds_id": ds_id})
    return success_response(data=result)


@router.post("/data-sources/{ds_id}/sync", summary="立即同步")
async def sync_data_source(request: Request, ds_id: str):
    result = await _call_kb_capability(request, "sync_data_source", {"ds_id": ds_id})
    return success_response(data=result)


@router.post("/data-sources/{ds_id}/reset-ingest-key", summary="重置入站推送 Key")
async def reset_ingest_key(request: Request, ds_id: str):
    result = await _call_kb_capability(request, "reset_ingest_key", {"ds_id": ds_id})
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 解析引擎设置 /api/v1/knowledge-base/parser-settings
# ════════════════════════════════════════════════════════════

@router.get("/parser-settings", summary="解析引擎设置列表")
async def list_parser_settings(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    result = await _call_kb_capability(request, "list_parser_settings", {"knowledge_base_id": knowledge_base_id})
    return success_response(data=result)


@router.post("/parser-settings", summary="创建解析引擎设置")
async def create_parser_setting(request: Request, payload: dict = Body(...)):
    result = await _call_kb_capability(request, "create_parser_setting", payload)
    return success_response(data=result)


@router.patch("/parser-settings/{setting_id}", summary="更新解析引擎设置")
async def update_parser_setting(request: Request, setting_id: str, payload: dict = Body(...)):
    payload["setting_id"] = setting_id
    result = await _call_kb_capability(request, "update_parser_setting", payload)
    return success_response(data=result)


@router.delete("/parser-settings/{setting_id}", summary="删除解析引擎设置")
async def delete_parser_setting(request: Request, setting_id: str):
    result = await _call_kb_capability(request, "delete_parser_setting", {"setting_id": setting_id})
    return success_response(data=result)


# ════════════════════════════════════════════════════════════
# 入站推送（API 开放）—— 免用户 JWT，鉴权用 X-Ingest-Key（能力侧校验）
# ════════════════════════════════════════════════════════════

ingest_router = APIRouter()


async def _call_kb_capability_ingest(
    ds_id: str,
    ingest_key: str,
    *,
    storage_key: str,
    file_name: str,
    mime_type: Optional[str],
    file_size: int,
    external_id: Optional[str],
) -> dict:
    """调内部 ingest_push action。从 ingest key 解析租户传给 Sidecar。"""
    from jonex_core.common.crypto import decode_ingest_key
    key_info = decode_ingest_key(ingest_key) or {}
    # 校验 key 中的 ds_id 与 URL 中的一致
    if key_info.get("ds_id") and key_info["ds_id"] != ds_id:
        raise InvalidApiKeyError(message=translate("err.ingest.key_mismatch", fallback="ingest key 与数据源不匹配"))
    tenant_id = key_info.get("tenant_id")
    if not tenant_id:
        raise InvalidApiKeyError(message=translate("err.ingest.key_invalid", fallback="ingest key 无效"))
    config = get_config()
    sidecar_payload = {
        "capability_id": "business.knowledge_base.v1",
        "tenant_id": tenant_id,
        "payload": {
            "action": "ingest_push",
            "data": {
                "ds_id": ds_id,
                "ingest_key": ingest_key,
                "storage_key": storage_key,
                "file_name": file_name,
                "mime_type": mime_type,
                "file_size": file_size,
                "external_id": external_id,
            },
        },
    }
    headers = {"X-API-Key": config.GATEWAY_API_KEY}
    transmit_locale_header(headers)
    timeout = float(os.getenv("GATEWAY_SIDECAR_TIMEOUT", "180"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{config.SIDECAR_URL}/invoke", json=sidecar_payload, headers=headers)
    result = resp.json()
    if resp.status_code != 200 or not result.get("success", False):
        raise_from_capability_result(result)
    return result.get("data") or {}


@ingest_router.post("/ingest/{ds_id}", summary="外部推送文档入库（API 开放）")
async def ingest_push(
    request: Request,
    ds_id: str,
    file: UploadFile = File(...),
    x_ingest_key: str = Header(..., alias="X-Ingest-Key"),
    external_id: Optional[str] = Form(None),
):
    content = await file.read()
    if not content:
        raise InvalidParameterError(message=translate("err.kb.upload_file_required", fallback="上传文件不能为空"))
    file_name = file.filename or "unnamed"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", file_name)
    doc_id = str(uuid4())
    prefix = os.getenv("COS_KEY_PREFIX", "jonex")
    # 提前解析 ingest key 获取自包含租户，把对象纳入 kb/{tenant_id}/ingest/ 租户前缀，
    # 使 STS 临时密钥 policy（kb/{tenant_id}/*）天然覆盖该对象的预签名下载
    from jonex_core.common.crypto import decode_ingest_key
    key_info = decode_ingest_key(x_ingest_key) or {}
    if key_info.get("ds_id") and key_info["ds_id"] != ds_id:
        raise InvalidApiKeyError(message=translate("err.ingest.key_mismatch", fallback="ingest key 与数据源不匹配"))
    tenant_id = key_info.get("tenant_id")
    if not tenant_id:
        raise InvalidApiKeyError(message=translate("err.ingest.key_invalid", fallback="ingest key 无效"))
    storage_key = f"{prefix}/kb/{tenant_id}/ingest/{ds_id}/{doc_id}/{doc_id}_{safe}"
    await get_object_storage().put_bytes(storage_key, content, content_type=file.content_type)

    result = await _call_kb_capability_ingest(
        ds_id,
        x_ingest_key,
        storage_key=storage_key,
        file_name=file_name,
        mime_type=file.content_type,
        file_size=len(content),
        external_id=external_id,
    )
    return success_response(data=result)


@ingest_router.get("/documents/{document_id}/raw", summary="原文查看（token 或 JWT 鉴权；302 / 本地流式 / 同源代理）")
async def get_raw_document(
    request: Request,
    document_id: str,
    token: Optional[str] = Query(None, description="短时查看 token（音视频/PDF/图片直连用）"),
    proxy: bool = Query(False, description="同源代理模式：COS 后端由网关流式透传字节，规避跨域 CSP/X-Frame-Options（PDF/文本 iframe 用）"),
    download: bool = Query(False, description="下载模式：Content-Disposition attachment + 原文件名（仅前端对平台管理员暴露）"),
):
    """获取文档原文。鉴权二选一：

    - ``?token=``：短时查看票据，从 token 解析租户（音视频 ``<video>`` 直连、PDF/图片新标签预览）；
    - 否则回退 Authorization 头（文本 blob 取数路径，保持兼容）。

    后端分流（对象存储 = cos / s3，后者含 MinIO 等 S3 兼容端点）：
    - 对象存储 + ``proxy=0``：302 预签名 URL（浏览器直连，Range/流式最优，适合音视频/图片）。
      注意浏览器会按 CSP 的 img-src / media-src 校验该来源，部署需配 CSP_STORAGE_ORIGIN；
    - 对象存储 + ``proxy=1``：网关流式透传上游字节（同源，规避 frame-src CSP 与上游
      X-Frame-Options，适合 PDF/文本 iframe）；
    - local：FileResponse 从共享卷流式返回（支持 Range/seek）。

    - ``download=1``：响应头改为 attachment + 原文件名（浏览器触发下载）；对象存储直连场景由预签名 URL 的 response-content-disposition 参数承载。
    """
    if token:
        info = verify_view_token(token)
        if not info or info.get("doc_id") != document_id:
            raise InvalidApiKeyError(message=translate("err.kb.view_ticket_invalid", fallback="查看票据无效或已过期"))
        tenant_id = info["tenant_id"]
    else:
        # 兼容旧路径：无票据时必须能解出用户。仅凭 X-Tenant-ID 不能证明用户身份
        # （B6 §5.2）——否则未登录即可按租户头读原文。注意这里必须判「解出 user_id」，
        # 不能只判 Authorization 头存在：`Basic ...`、`Bearer`（空 token）都解不出人。
        if not _extract_user_id(request):
            raise InvalidApiKeyError(message="需要登录或查看票据")
        tenant_id = extract_tenant_id(request)

    loc = await _call_kb_capability(
        request, "get_raw_location", {"document_id": document_id, "download": download}, tenant_id=tenant_id
    )
    presigned = loc.get("presigned_url")
    if presigned:
        # [jonex] 记录上游来源，用于核对 CSP 的 img-src / media-src 该填什么域
        # （virtual-hosted 风格下 host 含 bucket 前缀，与 path-style 不同）。
        # 只记 scheme+host+path，绝不记 query：预签名的 signature 是临时凭证，
        # 落进日志等于泄漏该对象的读权限。
        _up = urlsplit(presigned)
        logger.info(
            "原文预览: doc=%s backend=%s mode=%s upstream=%s://%s path=%s",
            document_id,
            loc.get("storage_backend") or "?",
            "proxy" if proxy else "redirect-302",
            _up.scheme,
            _up.netloc,
            _up.path,
        )
        if proxy:
            # 同源代理：网关向对象存储预签名 URL 发起流式 GET，转发 Range，原样回传字节。
            # iframe 始终同源 → 不受 frame-src CSP / 上游 X-Frame-Options / 上游 CORS 限制。
            range_header = request.headers.get("range")
            upstream_headers = {"Range": range_header} if range_header else {}
            # [jonex] local_address 绑定 IPv4 通配地址，强制 httpx 走 IPv4。
            # 背景：容器内 DNS 会为 S3 端点同时返回 IPv6（Docker Desktop 的 ULA 私有段）
            # 和 IPv4，httpx（httpcore+anyio）优先拨 IPv6 且不 fallback，会在 start_tls
            # 阶段抛空消息的 ConnectError；boto3（urllib3）走 IPv4 正常，故仅代理路径受影响。
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, read=None),
                follow_redirects=True,
                transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
            )
            try:
                upstream = await client.send(
                    client.build_request("GET", presigned, headers=upstream_headers),
                    stream=True,
                )
            except Exception:
                # _stream_upstream 的 finally 只覆盖成功路径，这里兜底避免连接池泄漏
                await client.aclose()
                raise
            disposition = "inline"
            if download:
                disposition = "attachment"
                if loc.get("file_name"):
                    disposition += f"; filename*=UTF-8''{quote(loc.get('file_name'))}"
            resp_headers = {
                "Content-Disposition": disposition,
                "X-Content-Type-Options": "nosniff",
                "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
            }
            if "content-range" in upstream.headers:
                resp_headers["Content-Range"] = upstream.headers["content-range"]
            if "content-length" in upstream.headers:
                resp_headers["Content-Length"] = upstream.headers["content-length"]

            async def _stream_upstream():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()

            return StreamingResponse(
                _stream_upstream(),
                status_code=upstream.status_code,
                media_type=_safe_inline_media_type(loc.get("mime_type")),
                headers=resp_headers,
            )
        # COS：跳第三方域，浏览器直连，天然支持 Range/流式（音视频/图片）
        return RedirectResponse(url=presigned, status_code=302)

    # local：从共享卷直接流式返回（FileResponse 自带 Range，音视频可拖动/边下边播）
    storage_key = loc.get("storage_key")
    backend = (loc.get("storage_backend") or "local").strip().lower()
    fs_path = get_object_storage_for(backend).fs_path(storage_key) if storage_key else None
    if not fs_path or not os.path.exists(fs_path):
        raise InvalidParameterError(message=translate("err.kb.document_source_unavailable", fallback="文档原文不存在或不可访问"))
    return FileResponse(
        path=fs_path,
        media_type=_safe_inline_media_type(loc.get("mime_type")),
        filename=loc.get("file_name") or None,
        content_disposition_type="attachment" if download else "inline",  # download 触发浏览器下载，否则内联预览
        headers={"X-Content-Type-Options": "nosniff"},  # 同源场景禁 MIME 嗅探，配合 html/svg 降级防 XSS
    )


@ingest_router.get("/documents/{document_id}/assets/{image_idx}", summary="图片资产查看（token 或 JWT 鉴权；302 / 本地流式）")
async def get_raw_document_asset(
    request: Request,
    document_id: str,
    image_idx: int,
    ext: Optional[str] = Query(None, description="图片扩展名（检索侧 aext 旁路值；缺省按白名单兜底 png）"),
    token: Optional[str] = Query(None, description="短时查看 token（图片 <img> 直连用）"),
):
    """[jonex] §image-refs P2-4: 获取文档内嵌图片资产原文。

    鉴权与 ``/documents/{document_id}/raw`` 同口径（token 或 JWT 二选一）；
    能力层校验租户归属并派生对象键（build_asset_key：ext 白名单归一、
    image_idx 强校验为整数）。后端分流：
    - cos：302 预签名 URL（``<img>`` 标签跨域加载不受 CORS 限制，无需同源代理态）；
    - local：FileResponse 从共享卷流式返回。
    """
    if token:
        info = verify_view_token(token)
        if not info or info.get("doc_id") != document_id:
            raise InvalidApiKeyError(message=translate("err.kb.view_ticket_invalid", fallback="查看票据无效或已过期"))
        tenant_id = info["tenant_id"]
    else:
        # 同 `/raw`：无票据时必须能解出用户，不能只靠 X-Tenant-ID（B6 §5.2）
        if not _extract_user_id(request):
            raise InvalidApiKeyError(message="需要登录或查看票据")
        tenant_id = extract_tenant_id(request)

    loc = await _call_kb_capability(
        request, "get_asset_raw_location",
        {"document_id": document_id, "image_idx": image_idx, "ext": ext or ""},
        tenant_id=tenant_id,
    )
    presigned = loc.get("presigned_url")
    if presigned:
        # COS：浏览器直连，<img> 加载无 CORS 门槛
        return RedirectResponse(url=presigned, status_code=302)

    storage_key = loc.get("storage_key")
    backend = (loc.get("storage_backend") or "local").strip().lower()
    fs_path = get_object_storage_for(backend).fs_path(storage_key) if storage_key else None
    if not fs_path or not os.path.exists(fs_path):
        raise InvalidParameterError(message=translate("err.kb.document_source_unavailable", fallback="文档原文不存在或不可访问"))
    return FileResponse(
        path=fs_path,
        media_type=_safe_inline_media_type(loc.get("mime_type")),
        filename=loc.get("file_name") or None,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@ingest_router.get(
    "/search/query-images/{image_id}/raw",
    summary="查询图片查看（只认 token；302 / 本地流式）",
)
async def get_query_image_raw(
    request: Request,
    image_id: str,
    token: Optional[str] = Query(None, description="短时查看票据（<img> 直连用）"),
):
    """[jonex] 以图问知识：查看检索历史里的图片。

    挂在 `ingest_router` 上是因为 ``<img src=…>`` **带不了 Authorization 头**
    （同 `/documents/{id}/raw` 的图片/音视频场景）。

    ⚠️ **只认 token，没有回退**：与文档原文那条端点不同 —— 那里无票时回退
    `X-Tenant-ID` 头（历史包袱），这里不回退。少了「无票也能读」的路径，
    与「图片只有本人可看」（D10）一致。

    ⚠️ 身份用 **token 里的** tenant / user 组装，不是从请求头解 ——
    raw 请求本身没有用户 JWT，用头里那点信息会读成别人的图或读不到图。
    """
    info = verify_query_image_token(token or "")
    if not info or info.get("image_id") != image_id:
        raise InvalidApiKeyError(
            message=_image_query_message(
                "err.query_image.view_ticket_invalid", "查看链接无效或已过期"
            )
        )

    loc = await _call_kb_capability(
        request,
        "get_query_image_location",
        {"image_id": image_id},
        user_id=info["user_id"],
        tenant_id=info["tenant_id"],
    )
    presigned = loc.get("presigned_url")
    if presigned:
        # COS/S3：浏览器直连，<img> 加载无 CORS 门槛（部署需配 CSP_STORAGE_ORIGIN）
        # 302 也带上 nosniff：与 local 分支的 FileResponse 同一口径
        return RedirectResponse(
            url=presigned, status_code=302,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    # local：从共享卷流式返回
    storage_key = loc.get("storage_key")
    backend = (loc.get("storage_backend") or "local").strip().lower()
    fs_path = get_object_storage_for(backend).fs_path(storage_key) if storage_key else None
    if not fs_path or not os.path.exists(fs_path):
        raise InvalidParameterError(
            message=_image_query_message("err.query_image.not_found", "图片不存在或已清理")
        )
    return FileResponse(
        path=fs_path,
        media_type=_safe_inline_media_type(loc.get("mime_type")),
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


# ════════════════════════════════════════════════════════════
# 数据运营 /api/v1/knowledge-base/data-operations
# ════════════════════════════════════════════════════════════

# 自定义时间区间的跨度上限。与能力层 `analytics_service._RANGE_MAX_DAYS` 对齐：
# 网关拒绝在前（面向客户端，非法入参要立刻 400），能力侧只做保守夹取。
_TREND_RANGE_MAX_DAYS = 366


def _parse_trend_range(start_date: Optional[date], end_date: Optional[date]):
    """校验数据运营趋势的自定义时间区间，返回 `(start, end)`；都没传时返回 `(None, None)`。

    ⚠️ **成对出现才有意义**：只传一边、或给了个反的区间都直接 400。这里是面向客户端的
    边界，非法入参要立刻拒掉；能力层对同一组参数只做「当没传」的保守降级 —— 它还会被
    内部调用方经 invoke 打进来，不该因为新参数缺一半就 500。

    两个都不传时返回 `(None, None)`，下游照旧走 `days` 的相对窗口（向后兼容）。
    """
    if start_date is None and end_date is None:
        return None, None
    if start_date is None or end_date is None:
        raise InvalidParameterError(
            message=translate("err.kb.trend_range_pair_required",
                              fallback="start_date 与 end_date 必须成对提供（只传一个无法确定时间区间）")
        )
    if start_date > end_date:
        raise InvalidParameterError(
            message=translate("err.kb.trend_range_reversed",
                              fallback="start_date 不能晚于 end_date")
        )
    if (end_date - start_date).days + 1 > _TREND_RANGE_MAX_DAYS:
        raise InvalidParameterError(
            message=translate("err.kb.trend_range_too_long",
                              params={"max_days": _TREND_RANGE_MAX_DAYS},
                              fallback=f"时间区间不能超过 {_TREND_RANGE_MAX_DAYS} 天")
        )
    return start_date, end_date


@router.get("/data-operations/overview", summary="数据运营总览")
async def get_data_operations_overview(
    request: Request,
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    days: int = Query(7, ge=1, le=365, description="统计窗口天数"),
):
    result = await _call_kb_capability(
        request,
        "get_data_operations_overview",
        {"domain_space_id": domain_space_id, "days": days},
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/data-operations/data-assets", summary="数据资产分析")
async def get_data_assets_analytics(
    request: Request,
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    days: int = Query(7, ge=1, le=365, description="统计窗口天数"),
    unused_threshold_days: int = Query(30, ge=1, le=365, description="长期未使用阈值天数"),
    kb_id: Optional[str] = Query(None, max_length=64, description="知识库 ID（空表示空间/租户全量）"),
    start_date: Optional[date] = Query(None, description="自定义起始日期（含），与 end_date 成对；成对给出时覆盖 days"),
    end_date: Optional[date] = Query(None, description="自定义结束日期（含），与 start_date 成对"),
):
    start_date, end_date = _parse_trend_range(start_date, end_date)
    result = await _call_kb_capability(
        request,
        "get_data_assets_analytics",
        {
            "domain_space_id": domain_space_id,
            "days": days,
            "unused_threshold_days": unused_threshold_days,
            "kb_id": kb_id,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/data-operations/knowledge-assets", summary="知识资产分析")
async def get_knowledge_assets_analytics(
    request: Request,
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    days: int = Query(7, ge=1, le=365, description="统计窗口天数"),
    unused_threshold_days: int = Query(30, ge=1, le=365, description="长期未使用阈值天数"),
    start_date: Optional[date] = Query(None, description="自定义起始日期（含），与 end_date 成对；成对给出时覆盖 days"),
    end_date: Optional[date] = Query(None, description="自定义结束日期（含），与 start_date 成对"),
):
    start_date, end_date = _parse_trend_range(start_date, end_date)
    result = await _call_kb_capability(
        request,
        "get_knowledge_assets_analytics",
        {
            "domain_space_id": domain_space_id,
            "days": days,
            "unused_threshold_days": unused_threshold_days,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/data-operations/search-analytics", summary="检索分析")
async def get_search_analytics(
    request: Request,
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    days: int = Query(7, ge=1, le=365, description="统计窗口天数"),
    start_date: Optional[date] = Query(None, description="自定义起始日期（含），与 end_date 成对；成对给出时覆盖 days"),
    end_date: Optional[date] = Query(None, description="自定义结束日期（含），与 start_date 成对"),
):
    start_date, end_date = _parse_trend_range(start_date, end_date)
    result = await _call_kb_capability(
        request,
        "get_search_analytics",
        {
            "domain_space_id": domain_space_id,
            "days": days,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/data-operations/abnormal-assets", summary="异常资产下钻（分页）")
async def get_abnormal_assets_analytics(
    request: Request,
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    kb_id: Optional[str] = Query(None, max_length=64, description="知识库 ID（空表示空间/租户全量）"),
    stage: Optional[str] = Query(None, max_length=64,
                                description="阶段筛选，逗号分隔：parse,extract,compile（空表示全部）"),
    handled: bool = Query(False, description="是否已处置：false=未处置（默认），true=已处置"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """异常资产（按失败任务计数）。

    ⚠️ 与页 1 的 `abnormal_summary` 一样**不受 `days` 限制**：这是待人工复核的队列，
    按窗口过滤会把久未处置的失败藏起来（它们可能发生在窗口之前，但仍然有待处置）。
    """
    result = await _call_kb_capability(
        request,
        "get_abnormal_assets_analytics",
        {
            "domain_space_id": domain_space_id,
            "kb_id": kb_id,
            "stage": stage,
            "handled": handled,
            "page": page,
            "page_size": page_size,
        },
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/data-operations/quality-anomalies", summary="解析质量异常清单")
async def get_quality_anomalies(
    request: Request,
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    kb_id: Optional[str] = Query(None, max_length=64, description="知识库 ID（空表示空间/租户全量）"),
):
    """最近 `verdict='bad'` 的 解析质量异常（观测档，最近 50 条）。

    与 `abnormal-assets` 不同：这是**质量观测**，不是处理失败；档位差不拦截检索。
    """
    result = await _call_kb_capability(
        request,
        "get_quality_anomalies",
        {"domain_space_id": domain_space_id, "kb_id": kb_id},
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


# ── 三期：列表钻取详情（原型「点名称进抽屉」）──
#
# 三个接口都**接受与列表同一组时间/范围参数**（`days` 或 `start_date`+`end_date`，
# 加 `domain_space_id`），抽屉里的「引用次数 / 命中次数 / 检索请求数」才等于列表里
# 那个数字。⚠️ 前端打开抽屉时必须把打开列表时用的那组参数原样带上：不传不会报错，
# 只会按默认 `days=7` 算，用户看到「列表 2,864 / 抽屉 1,208」而**没有任何提示**。


@router.get("/data-operations/assets/{asset_id}", summary="数据资产详情（抽屉 A）")
async def get_data_asset_detail(
    request: Request,
    asset_id: str = Path(..., max_length=64, description="资产 ID（= knowledge_documents.id）"),
    kb_id: Optional[str] = Query(None, max_length=64, description="知识库 ID（可选，传了做交叉校验）"),
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    days: int = Query(7, ge=1, le=365, description="统计窗口天数（与列表一致）"),
    start_date: Optional[date] = Query(None, description="自定义起始日期（含），与 end_date 成对；成对给出时覆盖 days"),
    end_date: Optional[date] = Query(None, description="自定义结束日期（含），与 start_date 成对"),
):
    """数据资产详情。资产不存在 / 已删除 / 不在本次范围内 → 404。"""
    start_date, end_date = _parse_trend_range(start_date, end_date)
    result = await _call_kb_capability(
        request,
        "get_data_asset_detail",
        {
            "asset_id": asset_id,
            "kb_id": kb_id,
            "domain_space_id": domain_space_id,
            "days": days,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/data-operations/ontology-entities/detail", summary="Ontology 对象详情（抽屉 B）")
async def get_data_ontology_entity_detail(
    request: Request,
    kb_id: str = Query(..., max_length=64, description="知识库 ID"),
    entity_type: str = Query(..., max_length=128, description="实体类型（如「金融产品」）"),
    canonical_name: str = Query(..., max_length=512, description="实体规范名（列表里的 canonical_name）"),
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    days: int = Query(7, ge=1, le=365, description="统计窗口天数（与列表一致）"),
    start_date: Optional[date] = Query(None, description="自定义起始日期（含），与 end_date 成对；成对给出时覆盖 days"),
    end_date: Optional[date] = Query(None, description="自定义结束日期（含），与 start_date 成对"),
):
    """Ontology 对象详情。

    ⚠️ 定位键是 **`kb_id` + `entity_type` + `canonical_name` 三元组**，不是列表里的
    `object_id` —— 后者是 `sha256(tenant|kb|type|name)` 的单向哈希，反解不回三元组，
    而且 `canonical_name` 改名后历史哈希指向一个不存在的键。

    ⚠️ 用查询参数而不是路径参数：`canonical_name` 是中文实体名，可能含 `/`、空格。
    """
    start_date, end_date = _parse_trend_range(start_date, end_date)
    result = await _call_kb_capability(
        request,
        "get_data_ontology_entity_detail",
        {
            "kb_id": kb_id,
            "entity_type": entity_type,
            "canonical_name": canonical_name,
            "domain_space_id": domain_space_id,
            "days": days,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/data-operations/kbs/{kb_id}", summary="知识库详情（抽屉 C）")
async def get_data_kb_detail(
    request: Request,
    kb_id: str = Path(..., max_length=64, description="知识库 ID"),
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    days: int = Query(7, ge=1, le=365, description="统计窗口天数（与列表一致）"),
    start_date: Optional[date] = Query(None, description="自定义起始日期（含），与 end_date 成对；成对给出时覆盖 days"),
    end_date: Optional[date] = Query(None, description="自定义结束日期（含），与 start_date 成对"),
):
    """知识库详情。

    ⚠️ 无权限时整体 **404**（不是空壳）：判据复用 `get_knowledge_info` 的防探测口径
    （非空间管理者且无 KB 授权）。前端**不要**把它提示成「无权限」—— 那等于确认了
    这个知识库存在。
    """
    start_date, end_date = _parse_trend_range(start_date, end_date)
    result = await _call_kb_capability(
        request,
        "get_data_kb_detail",
        {
            "kb_id": kb_id,
            "domain_space_id": domain_space_id,
            "days": days,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)


@router.get("/data-operations/services/{service_id}", summary="领域服务详情（抽屉 D）")
async def get_data_service_detail(
    request: Request,
    service_id: str = Path(..., max_length=64, description="领域服务 ID（= service_usage[].service_id）"),
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（空表示租户级全量）"),
    days: int = Query(7, ge=1, le=365, description="统计窗口天数（与列表一致）"),
    start_date: Optional[date] = Query(None, description="自定义起始日期（含），与 end_date 成对；成对给出时覆盖 days"),
    end_date: Optional[date] = Query(None, description="自定义结束日期（含），与 start_date 成对"),
):
    """领域服务详情。

    ⚠️ **本接口不返回 404**（与 A/B/C 三处有意不同）：抽屉的主体是「领域服务使用情况」
    表里那一行**归因记录**，服务实体只是装饰。列表在服务已被删 / 服务实体查不到时
    本来就回落显示 `service_id`，所以「行在、实体不在」是列表已承认的状态（历史回填
    把空间 id 写进 `entry_service_id` 的遗留数据就是这种）。这里报 404 会变成
    「列表上明明有一行，点开说它不存在」。取不到就把 `service_name` 回落成 `service_id`、
    `status` / `enabled` 给 `null`、`knowledge_bases` 给空数组，**使用情况照常返回**。
    """
    start_date, end_date = _parse_trend_range(start_date, end_date)
    result = await _call_kb_capability(
        request,
        "get_data_service_detail",
        {
            "service_id": service_id,
            "domain_space_id": domain_space_id,
            "days": days,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        user_id=_extract_user_id(request),
    )
    return success_response(data=result)
