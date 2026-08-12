#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base API routes.

The Gateway is a thin REST adapter. Tenant identity comes from the current
Authorization token and is forwarded to Sidecar; request bodies never accept
``tenant_id``.
"""

import asyncio
import os
import random
import re
import uuid
from typing import Any, Optional
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from api_gateway.deps import require_auth_header, raise_from_capability_result
from jonex_core.common.crypto import generate_view_token, verify_view_token
from pydantic.v1 import BaseModel

from jonex_core.common.object_storage import build_object_key, get_object_storage, get_object_storage_for
from capabilities.knowledge_base.dtos import (
    AddDocumentTagRequest,
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
    LlmWikiSearchRequest,
    MixSearchRequest,
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



def _extract_user_id(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    config = get_config()
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
        return payload.get("sub") or None
    except jwt.PyJWTError:
        return None


def _extract_username(request: Request) -> str | None:
    """从请求 Authorization 头的 JWT 中提取 username。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    config = get_config()
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
        return payload.get("username") or None
    except jwt.PyJWTError:
        return None


async def _call_kb_capability(
    request: Request,
    action: str,
    payload: dict[str, Any],
    *,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> dict:
    config = get_config()
    tenant_id = tenant_id or extract_tenant_id(request)
    request_id = getattr(request.state, "request_id", "")
    resolved_user_id = user_id or _extract_user_id(request)
    resolved_username = _extract_username(request)

    sidecar_payload: dict[str, Any] = {
        "capability_id": "business.knowledge_base.v1",
        "payload": {"action": action, "data": payload},
        "tenant_id": tenant_id,
    }
    if resolved_user_id:
        sidecar_payload["user_id"] = resolved_user_id
    if resolved_username:
        sidecar_payload["username"] = resolved_username

    headers = {
        "X-API-Key": "jonex_test_gateway",
        "X-Request-ID": request_id,
        "X-Tenant-ID": tenant_id,
        "X-Forwarded-For": request.client.host if request.client else "",
    }
    transmit_locale_header(headers)

    # Sidecar 瞬态连接错误（重启/DNS/端口未就绪）做指数退避 + 全抖动重试
    max_retries = int(os.getenv("GATEWAY_SIDECAR_RETRIES", "2"))
    base_delay = float(os.getenv("GATEWAY_SIDECAR_BASE_DELAY", "1.0"))
    max_delay = float(os.getenv("GATEWAY_SIDECAR_MAX_DELAY", "8.0"))
    timeout = float(os.getenv("GATEWAY_SIDECAR_TIMEOUT", "120"))

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{config.SIDECAR_URL}/invoke",
                    json=sidecar_payload,
                    headers=headers,
                )
            result = response.json()
            if response.status_code != 200 or not result.get("success", False):
                raise_from_capability_result(result)
            return result.get("data") or {}
        except httpx.TimeoutException:
            raise CapabilityInvokeError(
                message=translate("err.capability.kb_timeout", fallback="知识库能力调用超时"),
                details={"action": action},
            )
        except httpx.TransportError as e:
            last_exc = e
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                jitter = random.uniform(0, delay)
                logger.warning(
                    "Knowledge Base Sidecar 调用 %s 瞬断 (attempt %s/%s)，"
                    "%.1fs 后重试: %s",
                    action, attempt + 1, max_retries + 1, jitter, e,
                )
                await asyncio.sleep(jitter)
            else:
                logger.error(
                    "Knowledge Base Sidecar 调用 %s 失败，已重试 %s 次: %s",
                    action, max_retries, e,
                )
                raise CapabilityInvokeError(
                    message=translate("err.capability.kb_unavailable", fallback="知识库能力不可用"),
                    details={"action": action},
                )
        except JonexException:
            raise
        except Exception as exc:
            if hasattr(exc, "code") and hasattr(exc, "status_code"):
                raise
            logger.error("Knowledge Base capability call failed: action=%s error=%s", action, exc)
            raise CapabilityInvokeError(
                message=translate("err.capability.kb_unavailable", fallback="知识库能力不可用"),
                details={"action": action},
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
        # COS 直传模式：文件已在 COS 上，只需确认
        if doc_id:
            payload["doc_id"] = doc_id
        payload["file_name"] = file_name or storage_key.rsplit("/", 1)[-1]
        payload["file_path"] = storage_key
        payload["storage_key"] = storage_key
        payload["storage_backend"] = storage_backend or "cos"
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
):
    """生成 COS 预签名 PUT URL，前端直传字节到 COS（不经 Sidecar）。

    D9：大文件不经 Sidecar 透传字节，用预签名 URL 直传 COS。
    上传完成后调 upload_document 只传 storage_key + 元数据确认。
    """
    result = await _call_kb_capability(
        request,
        "generate_upload_url",
        {"knowledge_base_id": knowledge_base_id, "file_name": file_name, "content_type": content_type},
    )
    return success_response(data=result)


@router.get("/documents/{document_id}/view-ticket", summary="签发原文短时查看票据（音视频/PDF/图片直连）")
async def get_view_ticket(request: Request, document_id: str):
    """签发只读、单文档、短时效的查看 URL。

    用于 `<video>/<audio>` 等无法携带 Authorization 头的直连流式播放，
    以及 PDF/图片新标签内联预览。token 绑定当前租户 + 该文档 + TTL。
    """
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


@router.get("/documents/{document_id}/chunks/{chunk_id}", summary="查看单个 Chunk 内容")
async def get_chunk(request: Request, document_id: str, chunk_id: str):
    """按 chunk_id 精确查单片 chunk 完整内容（含位置元数据）。

    chunk_id 为 LightRAG 内容哈希主键（chunk-<md5>），召回明细/引用锚点直接给出。
    """
    result = await _call_kb_capability(
        request, "get_chunk", {"document_id": document_id, "chunk_id": chunk_id},
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


# ── 文件夹 CRUD ──────────────────────────────────────────


@router.get("/knowledge-base/folders", summary="列出知识库文件夹")
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


@router.post("/knowledge-base/folders", summary="创建文件夹")
async def create_folder(request: Request, body: FolderCreateRequest):
    """在知识库中创建文件夹（同知识库内名称唯一）。"""
    result = await _call_kb_capability(request, "create_folder", _schema_payload(body))
    return success_response(data=result, message="文件夹已创建")


@router.patch("/knowledge-base/folders/{folder_id}", summary="重命名文件夹")
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


@router.delete("/knowledge-base/folders/{folder_id}", summary="删除文件夹")
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


@router.get("/knowledge-base/tags", summary="列出知识库标签")
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


@router.post("/knowledge-base/tags", summary="创建标签")
async def create_tag(request: Request, body: TagCreateRequest):
    """在知识库中创建标签（同知识库内名称唯一）。"""
    result = await _call_kb_capability(request, "create_tag", _schema_payload(body))
    return success_response(data=result, message="标签已创建")


@router.put("/knowledge-base/tags/{tag_id}", summary="更新标签")
async def update_tag(
    request: Request,
    tag_id: str,
    body: TagUpdateRequest,
):
    """更新标签名称和/或颜色。"""
    payload = {"tag_id": tag_id, **_schema_payload(body)}
    result = await _call_kb_capability(request, "update_tag", payload)
    return success_response(data=result, message="标签已更新")


@router.delete("/knowledge-base/tags/{tag_id}", summary="删除标签")
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
    knowledge_base_id: str = Query("", description="知识库 ID（按知识库隔离检索；空表示沿用默认行为）"),
):
    """流式语义搜索，返回 OpenAI 兼容的 SSE 流（delta 格式）"""
    from fastapi.responses import StreamingResponse
    import httpx, json, time, uuid, re

    config = get_config()
    sidecar_url = config.SIDECAR_URL
    tenant_id = extract_tenant_id(request)
    request_id = getattr(request.state, "request_id", "")
    stream_params = {"query": query, "mode": mode, "top_k": top_k}
    if domain_id and domain_id != "all":
        stream_params["domain_id"] = domain_id
    # 知识库作用域：透传到 sidecar → atomic，按 (tenant, kb) workspace 隔离检索
    if knowledge_base_id:
        stream_params["knowledge_base_id"] = knowledge_base_id

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
            "X-API-Key": "jonex_test_gateway",
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
    return success_response(data=result)


@router.get("/search/history", summary="查询检索历史（全局可不传 kb_id）")
async def list_search_history(
    request: Request,
    knowledge_base_id: str = Query("", min_length=0, max_length=128, description="知识库 ID（空表示全局）"),
    domain_space_id: Optional[str] = Query(None, max_length=64, description="领域空间 ID（按空间隔离历史数据）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
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
        },
        user_id=_extract_user_id(request),
    )
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


@router.get("/parse-results/wiki-contents", summary="列出 OpenKB Wiki 页面树（按文档过滤）")
async def get_parse_result_wiki_contents(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    document_id: str = Query(..., min_length=1, max_length=64, description="文档 ID，按文档过滤编译产物"),
):
    """列出文档级 Wiki 页面树（摘要/实体/概念三组，sources 归属过滤）。"""
    result = await _call_kb_capability(
        request, "list_wiki_contents",
        {"knowledge_base_id": knowledge_base_id, "document_id": document_id},
    )
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
# 知识库信息管理（KnowledgeInfo CRUD）
# ════════════════════════════════════════════════════════════

@router.get("/knowledge-info", summary="知识库列表")
async def list_knowledge_info(
    request: Request,
    space_id: Optional[str] = Query(None, description="所属空间 ID"),
    status: Optional[str] = Query(None, description="状态过滤"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    result = await _call_kb_capability(request, "list_knowledge_info", {
        "space_id": space_id, "status": status, "keyword": keyword,
        "offset": offset, "limit": limit,
    })
    return success_response(data=result)


@router.post("/knowledge-info", summary="创建知识库")
async def create_knowledge_info(request: Request, payload: dict = Body(...)):
    result = await _call_kb_capability(request, "create_knowledge_info", payload)
    return success_response(data=result)


@router.get("/knowledge-info/{kb_id}", summary="知识库详情")
async def get_knowledge_info(request: Request, kb_id: str):
    result = await _call_kb_capability(request, "get_knowledge_info", {"kb_id": kb_id})
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


# ════════════════════════════════════════════════════════════
# 领域服务 /api/v1/knowledge-base/services  ->  business.knowledge_base.v1
# ════════════════════════════════════════════════════════════

@router.get("/services", summary="获取领域服务列表")
async def list_services(
    request: Request,
    space_id: Optional[str] = Query(None, description="所属空间 ID"),
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取领域服务分页列表"""
    result = await _call_kb_capability(request, "list_services", {"space_id": space_id, "offset": offset, "limit": limit})
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


@router.get("/services/{service_id}/permissions", summary="获取领域服务权限")
async def get_service_permissions(request: Request, service_id: str):
    """获取指定领域服务的权限设置"""
    result = await _call_kb_capability(request, "get_service_permissions", {"service_id": service_id})
    return success_response(data=result)


@router.put("/services/{service_id}/permissions", summary="设置领域服务权限")
async def set_service_permissions(request: Request, service_id: str, payload: dict = Body(...)):
    """设置指定领域服务的权限"""
    payload["service_id"] = service_id
    result = await _call_kb_capability(request, "set_service_permissions", payload)
    return success_response(data=result)


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
):
    """获取知识库实体类型定义及其对应的 Neo4j 实例计数"""
    payload = OntologyStatsRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(request, "list_ontology_entity_types", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/relation-types", summary="本体关系类型列表（含实例数）")
async def list_ontology_relation_types(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
):
    """获取知识库关系类型定义及其对应的 Neo4j 关系计数"""
    payload = OntologyStatsRequest(knowledge_base_id=knowledge_base_id)
    result = await _call_kb_capability(request, "list_ontology_relation_types", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/graph", summary="本体图谱数据（nodes + edges + 统计）")
async def get_ontology_graph(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    limit: int = Query(500, ge=1, le=2000, description="节点数量上限（按连接度取 top-N）"),
    entity_types: Optional[list[str]] = Query(None, description="按实体类型过滤，可重复传参"),
):
    """获取知识库图谱数据（按连接度取 top-N 节点），返回 nodes、edges 及全量统计，
    用于前端力导向图渲染与类型筛选/数量提示"""
    payload = OntologyGraphRequest(
        knowledge_base_id=knowledge_base_id,
        limit=limit,
        entity_types=entity_types,
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
):
    """展开指定实体的一跳邻居，返回邻居节点与连接边，用于前端双击节点增量扩展"""
    payload = OntologyNeighborRequest(
        knowledge_base_id=knowledge_base_id,
        entity_type=entity_type,
        canonical_name=canonical_name,
        limit=limit,
    )
    result = await _call_kb_capability(request, "expand_ontology_neighbors", _schema_payload(payload))
    return success_response(data=result)


@router.get("/ontology/entities/search", summary="本体实例名称搜索")
async def search_ontology_entities(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128, description="知识库 ID"),
    keyword: str = Query("", min_length=0, max_length=256, description="搜索关键词（模糊匹配实例名称/别名；空串返回空列表）"),
    limit: int = Query(20, ge=1, le=200, description="返回条数上限"),
):
    """模糊搜索本体实例，用于实例/关系表单中快速定位实体。

    复用 Neo4j 全文索引 ont_entity_ft（cjk analyzer）实现中文模糊匹配。
    按租户和知识库隔离，仅返回非 stub 正式实体。
    """
    payload = OntologyEntitySearchRequest(
        knowledge_base_id=knowledge_base_id,
        keyword=keyword,
        limit=limit,
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
    """创建本体实例（实体节点）。"""
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
    attributes: Optional[dict] = Body(None, description="扩展属性"),
):
    """创建本体关系（实体间关系边）。"""
    payload = {
        "knowledge_base_id": knowledge_base_id,
        "source_entity_type": source_entity_type,
        "source_canonical_name": source_canonical_name,
        "relation_type": relation_type,
        "target_entity_type": target_entity_type,
        "target_canonical_name": target_canonical_name,
    }
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
    headers = {"X-API-Key": "jonex_test_gateway"}
    transmit_locale_header(headers)
    timeout = float(os.getenv("GATEWAY_SIDECAR_TIMEOUT", "120"))
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
    # 外部调用无 tenant_id：用 ds_id 作为对象路径隔离；tenant 落库由能力侧 ds 记录决定
    storage_key = f"{prefix}/ingest/{ds_id}/{doc_id}/{doc_id}_{safe}"
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
):
    """获取文档原文。鉴权二选一：

    - ``?token=``：短时查看票据，从 token 解析租户（音视频 ``<video>`` 直连、PDF/图片新标签预览）；
    - 否则回退 Authorization 头（文本 blob 取数路径，保持兼容）。

    后端分流：
    - cos + ``proxy=0``：302 预签名 URL（浏览器直连，Range/流式最优，适合音视频/图片）；
    - cos + ``proxy=1``：网关流式透传 COS 字节（同源，规避 frame-src CSP 与 COS X-Frame-Options，适合 PDF/文本 iframe）；
    - local：FileResponse 从共享卷流式返回（支持 Range/seek）。
    """
    if token:
        info = verify_view_token(token)
        if not info or info.get("doc_id") != document_id:
            raise InvalidApiKeyError(message=translate("err.kb.view_ticket_invalid", fallback="查看票据无效或已过期"))
        tenant_id = info["tenant_id"]
    else:
        # 兼容旧路径：从 Authorization 头 / X-Tenant-ID 解析租户
        tenant_id = extract_tenant_id(request)

    loc = await _call_kb_capability(
        request, "get_raw_location", {"document_id": document_id}, tenant_id=tenant_id
    )
    presigned = loc.get("presigned_url")
    if presigned:
        if proxy:
            # 同源代理：网关向 COS 预签名 URL 发起流式 GET，转发 Range，原样回传字节。
            # iframe 始终同源 → 不受 frame-src CSP / COS X-Frame-Options / COS CORS 限制。
            range_header = request.headers.get("range")
            upstream_headers = {"Range": range_header} if range_header else {}
            client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)
            upstream = await client.send(
                client.build_request("GET", presigned, headers=upstream_headers),
                stream=True,
            )
            resp_headers = {
                "Content-Disposition": "inline",
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
        content_disposition_type="inline",  # 内联预览而非强制下载
        headers={"X-Content-Type-Options": "nosniff"},  # 同源场景禁 MIME 嗅探，配合 html/svg 降级防 XSS
    )
