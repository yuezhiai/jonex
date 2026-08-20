"""
Knowledge Base 能力 API 路由（注册到自身 FastAPI app）
"""
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile

from jonex_core.common.exceptions import InvalidParameterError, JonexException
from jonex_core.common.i18n import translate
from jonex_core.common.response import error_response, success_response
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.security.user_auth import get_current_user

from ..dtos import (
    BatchMoveDocumentsRequest,
    DocumentParseResultRequest,
    DocumentScopeRequest,
    OntologyRetryRequest,
    SetDocumentFolderRequest,
    ParseResultDocumentListRequest,
    ParseResultEntityListRequest,
    ParseResultGraphRequest,
    ParseResultRelationshipListRequest,
    ParseResultScopeRequest,
    SearchFeedbackToggleAdoptRequest,
    SearchHistoryCreateRequest,
    SearchHistoryDeleteRequest,
    SearchRequest,
)
from ..dtos.ontology_schema import (
    BindTemplateRequest,
    CompileRequest,
    ReseedCompiledSchemaRequest,
    RecompileRequest,
    SaveCompiledSchemaRequest,
)
from ..services import KnowledgeBaseService
from ..services.ontology_compiler import OntologyCompiler


def _save_upload_file(file: UploadFile, tenant_id: str, content: bytes) -> str:
    base_dir = Path(os.getenv("KB_INPUT_DIR", "/app/inputs"))
    tenant_dir = base_dir / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "unnamed").name
    target = tenant_dir / f"{uuid.uuid4().hex[:12]}_{safe_name}"
    target.write_bytes(content)
    return str(target)


def _schema_payload(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


router = APIRouter()
_service = KnowledgeBaseService()
_ontology_compiler = OntologyCompiler()


# ── 文档管理 ──────────────────────────────────────────────


@router.post("/documents/upload", summary="上传知识文档")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    knowledge_base_id: str = Form(..., min_length=1, max_length=128),
    folder_id: Optional[str] = Form(None),
    metadata: Optional[str] = Form(None),
):
    tenant_id = extract_tenant_id(request)
    content = await file.read()
    if not content:
        raise InvalidParameterError()  # 原消息: 上传文件不能为空（塌缩为 default_message）

    payload: dict[str, Any] = {
        "file_name": file.filename or "unnamed",
        "file_path": _save_upload_file(file, tenant_id, content),
        "file_size": len(content),
        "mime_type": file.content_type,
        "knowledge_base_id": knowledge_base_id,
    }
    if folder_id:
        payload["folder_id"] = folder_id
    if metadata:
        payload["metadata"] = {"raw": metadata}

    try:
        result = await _service.documents.upload_document(tenant_id, payload)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/documents", summary="查询知识文档列表")
async def list_documents(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
    status: Optional[str] = Query(None),
    ontology_status: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    folder_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.documents.list_documents(tenant_id, {
            "knowledge_base_id": knowledge_base_id,
            "status": status,
            "ontology_status": ontology_status,
            "keyword": keyword,
            "folder_id": folder_id,
            "page": page,
            "page_size": page_size,
        })
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/documents/{document_id}", summary="获取知识文档详情")
async def get_document(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.documents.get_document(
            tenant_id, document_id, {"knowledge_base_id": knowledge_base_id},
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/documents/{document_id}", summary="删除知识文档")
async def delete_document(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.documents.delete_document(
            tenant_id, document_id, {"knowledge_base_id": knowledge_base_id},
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.put("/documents/{document_id}/folder", summary="设置文档所属文件夹")
async def set_document_folder(
    request: Request,
    document_id: str,
    body: SetDocumentFolderRequest,
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.documents.set_document_folder(tenant_id, document_id, body)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.put("/documents/folder", summary="批量设置文档所属文件夹")
async def batch_set_document_folder(
    request: Request,
    body: BatchMoveDocumentsRequest,
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.documents.batch_set_document_folder(tenant_id, body)
        return success_response(data=result, message="文档已移动")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 检索 ──────────────────────────────────────────────────


@router.post("/search", summary="知识库语义检索")
async def search(request: Request, payload: SearchRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.search.search(tenant_id, _extract_user_id_from_request(request), _schema_payload(payload))
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/search/enhanced", summary="知识库增强检索")
async def search_enhanced(request: Request, payload: SearchRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.search.enhanced_search(tenant_id, _extract_user_id_from_request(request), _schema_payload(payload))
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/search/overview", summary="知识检索概览（全局，可不传 kb_id）")
async def get_search_overview(
    request: Request,
    knowledge_base_id: str = Query("", min_length=0, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.history.get_overview(
            tenant_id, _extract_user_id_from_request(request), {"knowledge_base_id": knowledge_base_id},
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 检索历史 ──────────────────────────────────────────────


@router.get("/search/history", summary="查询检索历史")
async def list_search_history(
    request: Request,
    knowledge_base_id: str = Query("", min_length=0, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.history.list_history(
            tenant_id, _extract_user_id_from_request(request),
            {"knowledge_base_id": knowledge_base_id, "page": page, "page_size": page_size},
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/search/history", summary="保存检索历史")
async def save_search_history(request: Request, payload: SearchHistoryCreateRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.history.save_history(
            tenant_id, _extract_user_id_from_request(request), _schema_payload(payload),
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/search/history/{history_id}", summary="删除检索历史")
async def delete_search_history(
    request: Request,
    history_id: str,
    knowledge_base_id: str = Query("", min_length=0, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.history.delete_history(
            tenant_id, _extract_user_id_from_request(request), history_id,
            {"knowledge_base_id": knowledge_base_id},
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/search/history", summary="清空检索历史")
async def clear_search_history(
    request: Request,
    knowledge_base_id: str = Query("", min_length=0, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.history.clear_history(
            tenant_id, _extract_user_id_from_request(request), {"knowledge_base_id": knowledge_base_id},
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 搜索结果反馈 ──────────────────────────────────────────


@router.post("/search/feedback", summary="提交搜索结果反馈（有帮助/无帮助）")
async def submit_search_feedback(request: Request):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.feedback.submit_feedback(
            tenant_id, _extract_user_id_from_request(request), body,
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/search/feedback", summary="取消搜索结果反馈")
async def cancel_search_feedback(request: Request):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.feedback.cancel_feedback(
            tenant_id, _extract_user_id_from_request(request), body,
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/search/feedback", summary="查询知识库的反馈列表")
async def list_search_feedback(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
    feedback_type: Optional[str] = Query(None, regex="^(like|dislike)?$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.feedback.list_feedback(tenant_id, {
            "knowledge_base_id": knowledge_base_id,
            "feedback_type": feedback_type,
            "page": page,
            "page_size": page_size,
        })
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/search/feedback/toggle-adopt", summary="切换反馈采纳状态")
async def toggle_search_feedback_adopt(request: Request, payload: SearchFeedbackToggleAdoptRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.feedback.toggle_adopted(
            tenant_id, _schema_payload(payload),
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/search/feedback/stats", summary="获取知识库的反馈统计")
async def get_search_feedback_stats(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.feedback.get_stats(tenant_id, {
            "knowledge_base_id": knowledge_base_id,
        })
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 解析结果 ──────────────────────────────────────────────


@router.get("/parse-results/summary", summary="解析结果概要")
async def get_parse_result_summary(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.parse_results.get_summary(
            tenant_id, {"knowledge_base_id": knowledge_base_id},
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/parse-results/documents", summary="解析文档列表")
async def get_parse_result_documents(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.parse_results.list_documents(tenant_id, {
            "knowledge_base_id": knowledge_base_id,
            "page": page,
            "page_size": page_size,
            "keyword": keyword,
            "status": status,
        })
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/parse-results/entities", summary="解析实体列表")
async def get_parse_result_entities(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    file_path: Optional[str] = Query(None),
    document_id: Optional[str] = Query(None),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.parse_results.list_entities(tenant_id, {
            "knowledge_base_id": knowledge_base_id,
            "page": page,
            "page_size": page_size,
            "keyword": keyword,
            "entity_type": entity_type,
            "file_path": file_path,
            "document_id": document_id,
        })
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/parse-results/relationships", summary="解析关系列表")
async def get_parse_result_relationships(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = Query(None),
    file_path: Optional[str] = Query(None),
    document_id: Optional[str] = Query(None),
    source_entity: Optional[str] = Query(None),
    target_entity: Optional[str] = Query(None),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.parse_results.list_relationships(tenant_id, {
            "knowledge_base_id": knowledge_base_id,
            "page": page,
            "page_size": page_size,
            "keyword": keyword,
            "file_path": file_path,
            "document_id": document_id,
            "source_entity": source_entity,
            "target_entity": target_entity,
        })
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/parse-results/graph-summary", summary="解析图谱概要")
async def get_parse_result_graph_summary(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.parse_results.get_graph_summary(
            tenant_id, {"knowledge_base_id": knowledge_base_id},
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/parse-results/graph", summary="解析图谱")
async def get_parse_result_graph(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
    keyword: Optional[str] = Query(None),
    file_path: Optional[str] = Query(None),
    document_id: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.parse_results.get_graph(tenant_id, {
            "knowledge_base_id": knowledge_base_id,
            "keyword": keyword,
            "file_path": file_path,
            "document_id": document_id,
            "limit": limit,
        })
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/documents/{document_id}/parse-result", summary="获取单文档解析结果")
async def get_document_parse_result(
    request: Request,
    document_id: str,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    payload = DocumentParseResultRequest(
        document_id=document_id,
        knowledge_base_id=knowledge_base_id,
    )
    try:
        result = await _service.parse_results.get_document_parse_result(
            tenant_id, _schema_payload(payload),
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/documents/{document_id}/retry-ontology", summary="重试本体抽取")
async def retry_ontology_extract(
    request: Request,
    document_id: str,
    payload: OntologyRetryRequest = Body(...),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.ontology.retry_extract(
            tenant_id, document_id, payload.knowledge_base_id,
        )
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 本体 Schema 绑定与编译 ────────────────────────────────


@router.put("/ontology/bindings/{knowledge_base_id}", summary="绑定 KB 到模板")
async def bind_ontology_template(
    knowledge_base_id: str,
    request: Request,
    payload: BindTemplateRequest = Body(...),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _ontology_compiler.bind_template(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            template_domain_id=payload.template_domain_id,
            template_scenario_id=payload.template_scenario_id,
            source_type=payload.source_type,
        )
        return success_response(data=result, message="模板绑定成功")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/ontology/bindings/{knowledge_base_id}", summary="查询 KB 当前绑定")
async def get_ontology_binding(
    knowledge_base_id: str,
    request: Request,
):
    tenant_id = extract_tenant_id(request)
    try:
        from ..repository.ontology_schema_repository import OntologySchemaRepository
        from jonex_core.common.database import get_db_session

        async with get_db_session() as session:
            repo = OntologySchemaRepository(session)
            binding = await repo.get_binding(tenant_id, knowledge_base_id)
            return success_response(data=binding.to_dict() if binding else None)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/ontology/editor-state", summary="查询 KB 本体编辑态")
async def get_ontology_editor_state(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _ontology_compiler.get_editor_state(tenant_id, knowledge_base_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/ontology/compile", summary="编译指定 KB")
async def compile_ontology(request: Request, payload: CompileRequest = Body(...)):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _ontology_compiler.compile_for_knowledge_base(
            tenant_id=tenant_id,
            knowledge_base_id=payload.knowledge_base_id,
            force=False,
        )
        return success_response(data=result, message="本体编译完成")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/ontology/recompile", summary="强制重编译指定 KB")
async def recompile_ontology(request: Request, payload: RecompileRequest = Body(...)):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _ontology_compiler.compile_for_knowledge_base(
            tenant_id=tenant_id,
            knowledge_base_id=payload.knowledge_base_id,
            force=True,
        )
        return success_response(data=result, message="本体重编译完成")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.put("/ontology/compiled-schema", summary="保存 KB 可编辑本体 schema")
async def save_ontology_compiled_schema(request: Request, payload: SaveCompiledSchemaRequest = Body(...)):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _ontology_compiler.save_compiled_schema(
            tenant_id=tenant_id,
            knowledge_base_id=payload.knowledge_base_id,
            entity_types=[item.dict(exclude_none=True) for item in payload.entity_types],
            relation_types=[item.dict(exclude_none=True) for item in payload.relation_types],
            edited_by=_extract_user_id_from_request(request),
            constraints=(
                [c.dict(exclude_none=True) for c in payload.constraints]
                if payload.constraints is not None
                else None
            ),
            expected_schema_version=payload.expected_schema_version,
        )
        return success_response(data=result, message="本体 schema 已保存")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/ontology/compiled-schema/reseed", summary="从模板重新生成 KB compiled schema")
async def reseed_ontology_compiled_schema(request: Request, payload: ReseedCompiledSchemaRequest = Body(...)):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _ontology_compiler.reseed_from_template(
            tenant_id=tenant_id,
            knowledge_base_id=payload.knowledge_base_id,
            template_domain_id=payload.template_domain_id,
            template_scenario_id=payload.template_scenario_id,
            source_type=payload.source_type,
        )
        return success_response(data=result, message="已按模板重新生成本体 schema")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/ontology/compiled-schema", summary="查询 KB compiled schema")
async def get_compiled_schema(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _ontology_compiler.get_compiled_schema(tenant_id, knowledge_base_id)
        # [jonex] 仅在对外下发（atomic-rag 拉取入口）注入 KB 级同义词，供抽取 prompt 使用；
        # kb-service 内部调用不注入，避免污染查询链路。写图端归并另走直查 DB。
        if result:
            result = await _ontology_compiler.inject_synonyms(tenant_id, knowledge_base_id, result)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/ontology/entity-types", summary="查询 KB 实体类型")
async def get_entity_types(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        schema = await _ontology_compiler.get_compiled_schema(tenant_id, knowledge_base_id)
        if schema:
            return success_response(data=schema.get("entity_types", []))
        return success_response(data=[])
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/ontology/relation-types", summary="查询 KB 关系类型")
async def get_relation_types(
    request: Request,
    knowledge_base_id: str = Query(..., min_length=1, max_length=128),
):
    tenant_id = extract_tenant_id(request)
    try:
        schema = await _ontology_compiler.get_compiled_schema(tenant_id, knowledge_base_id)
        if schema:
            return success_response(data=schema.get("relation_types", []))
        return success_response(data=[])
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 知识库信息管理 ──────────────────────────────────────────


@router.get("/knowledge-info", summary="知识库列表")
async def list_knowledge_info(
    request: Request,
    space_id: str = Query(None),
    status: str = Query(None),
    keyword: str = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.knowledge_infos.list(tenant_id, space_id, status, keyword, offset, limit)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/knowledge-info", summary="创建知识库")
async def create_knowledge_info(request: Request):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.knowledge_infos.create(tenant_id, body)
        return success_response(data=result, message="知识库已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/knowledge-info/{kb_id}", summary="知识库详情")
async def get_knowledge_info(kb_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.knowledge_infos.get(kb_id, tenant_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/knowledge-info/{kb_id}", summary="更新知识库")
async def update_knowledge_info(kb_id: str, request: Request):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.knowledge_infos.update(kb_id, tenant_id, body)
        return success_response(data=result, message="知识库已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/knowledge-info/{kb_id}/permissions", summary="知识库授权成员列表")
async def get_kb_permissions(
    kb_id: str, request: Request, current: dict = Depends(get_current_user)
):
    """双入口兜底（设计 §3）：REST 解析用户后传入 service；invoke 走 dispatch 判定。"""
    tenant_id = extract_tenant_id(request)
    user_id = str(current.get("user_id", "")) or None
    try:
        result = await _service.knowledge_infos.get_permissions(kb_id, tenant_id, user_id=user_id)
        return success_response(data={"permissions": result})
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.put("/knowledge-info/{kb_id}/permissions", summary="设置知识库授权成员")
async def set_kb_permissions(
    kb_id: str, request: Request, current: dict = Depends(get_current_user)
):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    user_id = str(current.get("user_id", "")) or None
    try:
        await _service.knowledge_infos.set_permissions(
            kb_id, tenant_id, body.get("permissions", []), user_id=user_id
        )
        return success_response(message="知识库权限已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/knowledge-info/{kb_id}", summary="删除知识库")
async def delete_knowledge_info(kb_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        await _service.knowledge_infos.delete(kb_id, tenant_id)
        return success_response(message="知识库已删除")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── Helper ────────────────────────────────────────────────


def _extract_user_id_from_request(request: Request) -> str:
    import jwt
    from jonex_core.common import get_config
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return "anonymous"
    token = auth[7:]
    config = get_config()
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
        return payload.get("sub") or "anonymous"
    except jwt.PyJWTError:
        return "anonymous"


def create_router() -> APIRouter:
    root = APIRouter()
    root.include_router(router)  # 原有 knowledge_base 路由
    from .spaces import router as spaces_router
    from .services import router as services_router
    from .folders import router as folders_router
    root.include_router(spaces_router)
    root.include_router(services_router)
    root.include_router(folders_router)
    from .tags import router as tags_router
    from .document_tags import router as document_tags_router
    root.include_router(tags_router)
    root.include_router(document_tags_router)
    return root
