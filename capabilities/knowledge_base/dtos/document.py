"""Knowledge Base document DTOs."""

from datetime import datetime
from typing import Any, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


class DocumentUploadRequest(BaseModel):
    file_name: str = Field(default="unnamed", max_length=512)
    file_path: str = Field(default="", max_length=1024)
    file_size: int = Field(default=0, ge=0)
    mime_type: Optional[str] = Field(default=None, max_length=128)
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    doc_id: Optional[str] = Field(default=None, max_length=64)
    folder_id: Optional[str] = Field(default=None, max_length=64)
    storage_backend: str = Field(default="local", max_length=16)
    storage_key: Optional[str] = Field(default=None, max_length=1024)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentListRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    status: Optional[str] = Field(default=None, max_length=32)
    ontology_status: Optional[str] = Field(default=None, max_length=32)
    # 线性状态多选筛选（优先于 status/ontology_status）：
    # pending_parse / parsing / ingesting / parse_failed / pending_compile / compiling / compiled / compile_failed
    phase: Optional[list[str]] = Field(default=None)
    keyword: Optional[str] = Field(default=None, max_length=255)
    folder_id: Optional[str] = Field(default=None, max_length=64)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class DocumentScopeRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)


class DocumentResponse(BaseModel):
    id: str
    tenant_id: str
    file_name: str
    file_path: str
    file_size: int
    mime_type: Optional[str] = None
    knowledge_base_id: str
    status: str
    storage_backend: str = "local"
    storage_key: Optional[str] = None
    rag_task_id: Optional[str] = None
    rag_doc_ids: list[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    ontology_status: str
    ontology_error: Optional[str] = None
    ontology_retry_count: int = 0
    folder_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime | str] = None
    updated_at: Optional[datetime | str] = None


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class SetDocumentFolderRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    folder_id: Optional[str] = Field(default=None, max_length=64)


class BatchMoveDocumentsRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    document_ids: list[str] = Field(..., min_length=1)
    folder_id: Optional[str] = Field(default=None, max_length=64)


class DocumentDeleteResponse(BaseModel):
    id: str
    deleted: bool = True
