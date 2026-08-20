"""
This module contains all document-related routes for the LightRAG API.
"""

import asyncio
import re  # [jonex]
import time
from uuid import uuid4
from functools import lru_cache
from lightrag.utils import logger, get_pinyin_sort_key, performance_timing_log
import aiofiles
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Literal
from io import BytesIO
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,  # [jonex]
    Request,  # [jonex]
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lightrag import LightRAG
from lightrag.base import DeletionResult, DocProcessingStatus, DocStatus
from lightrag.utils import (
    generate_track_id,
    compute_mdhash_id,
    sanitize_text_for_encoding,
)
from lightrag.api.utils_api import get_combined_auth_dependency
from lightrag.api.workspace_manager import get_workspace_from_request  # [jonex]
from ..config import global_args


@lru_cache(maxsize=1)
def _is_docling_available() -> bool:
    """Check if docling is available (cached check).

    This function uses lru_cache to avoid repeated import attempts.
    The result is cached after the first call.

    Returns:
        bool: True if docling is available, False otherwise
    """
    try:
        import docling  # noqa: F401  # type: ignore[import-not-found]

        return True
    except ImportError:
        return False


# Function to format datetime to ISO format string with timezone information
def format_datetime(dt: Any) -> Optional[str]:
    """Format datetime to ISO format string with timezone information

    Args:
        dt: Datetime object, string, or None

    Returns:
        ISO format string with timezone information, or None if input is None
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt

    # Check if datetime object has timezone information
    if isinstance(dt, datetime):
        # If datetime object has no timezone info (naive datetime), add UTC timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

    # Return ISO format string with timezone information
    return dt.isoformat()


# NOTE: the APIRouter instance is created INSIDE `create_document_routes`
# (not at module scope). A module-level router is shared across processes,
# and re-running the factory — which the test suite does to validate
# create_app for different `--api-prefix` values — would re-decorate the
# same router each time, accumulating duplicate routes and triggering
# FastAPI's "Duplicate Operation ID" warnings.

# Temporary file prefix
temp_prefix = "__tmp__"
UNKNOWN_FILE_SOURCE = "unknown_source"
LEGACY_EMPTY_FILE_PATH_SENTINELS = {"", "no-file-path"}


def normalize_file_path(file_path: str | None) -> str:
    """Normalize missing document sources to a single non-null sentinel."""
    if file_path is None:
        return UNKNOWN_FILE_SOURCE

    normalized = file_path.strip()
    if normalized in LEGACY_EMPTY_FILE_PATH_SENTINELS:
        return UNKNOWN_FILE_SOURCE

    return normalized


def sanitize_filename(filename: str, input_dir: Path) -> str:
    """
    Sanitize uploaded filename to prevent Path Traversal attacks.

    Args:
        filename: The original filename from the upload
        input_dir: The target input directory

    Returns:
        str: Sanitized filename that is safe to use

    Raises:
        HTTPException: If the filename is unsafe or invalid
    """
    # Basic validation
    if not filename or not filename.strip():
        raise HTTPException(status_code=400, detail="Filename cannot be empty")

    # Remove path separators and traversal sequences
    clean_name = filename.replace("/", "").replace("\\", "")
    clean_name = clean_name.replace("..", "")

    # Remove control characters and null bytes
    clean_name = "".join(c for c in clean_name if ord(c) >= 32 and c != "\x7f")

    # Remove leading/trailing whitespace and dots
    clean_name = clean_name.strip().strip(".")

    # Check if anything is left after sanitization
    if not clean_name:
        raise HTTPException(
            status_code=400, detail="Invalid filename after sanitization"
        )

    # Verify the final path stays within the input directory
    try:
        final_path = (input_dir / clean_name).resolve()
        if not final_path.is_relative_to(input_dir.resolve()):
            raise HTTPException(status_code=400, detail="Unsafe filename detected")
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid filename")

    return clean_name


class ScanResponse(BaseModel):
    """Response model for document scanning operation

    Attributes:
        status: Status of the scanning operation
        message: Optional message with additional details
        track_id: Tracking ID for monitoring scanning progress
    """

    status: Literal["scanning_started"] = Field(
        description="Status of the scanning operation"
    )
    message: Optional[str] = Field(
        default=None, description="Additional details about the scanning operation"
    )
    track_id: str = Field(description="Tracking ID for monitoring scanning progress")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "scanning_started",
                "message": "Scanning process has been initiated in the background",
                "track_id": "scan_20250729_170612_abc123",
            }
        }
    )


class ReprocessResponse(BaseModel):
    """Response model for reprocessing failed documents operation

    Attributes:
        status: Status of the reprocessing operation
        message: Message describing the operation result
        track_id: Always empty string. Reprocessed documents retain their original track_id.
    """

    status: Literal["reprocessing_started"] = Field(
        description="Status of the reprocessing operation"
    )
    message: str = Field(description="Human-readable message describing the operation")
    track_id: str = Field(
        default="",
        description="Always empty string. Reprocessed documents retain their original track_id from initial upload.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "reprocessing_started",
                "message": "Reprocessing of failed documents has been initiated in background",
                "track_id": "",
            }
        }
    )


class CancelPipelineResponse(BaseModel):
    """Response model for pipeline cancellation operation

    Attributes:
        status: Status of the cancellation request
        message: Message describing the operation result
    """

    status: Literal["cancellation_requested", "not_busy"] = Field(
        description="Status of the cancellation request"
    )
    message: str = Field(description="Human-readable message describing the operation")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "cancellation_requested",
                "message": "Pipeline cancellation has been requested. Documents will be marked as FAILED.",
            }
        }
    )


class InsertTextRequest(BaseModel):
    """Request model for inserting a single text document

    Attributes:
        text: The text content to be inserted into the RAG system
        file_source: Source of the text (optional)
    """

    text: str = Field(
        min_length=1,
        description="The text to insert",
    )
    file_source: Optional[str] = Field(
        default=None, min_length=0, description="File Source"
    )

    @field_validator("text", mode="after")
    @classmethod
    def strip_text_after(cls, text: str) -> str:
        return text.strip()

    @field_validator("file_source", mode="before")
    @classmethod
    def normalize_source_before(cls, file_source: Optional[str]) -> str:
        return normalize_file_path(file_source)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "text": "This is a sample text to be inserted into the RAG system.",
                "file_source": "Source of the text (optional)",
            }
        }
    )


class InsertTextsRequest(BaseModel):
    """Request model for inserting multiple text documents

    Attributes:
        texts: List of text contents to be inserted into the RAG system
        file_sources: Sources of the texts (optional)
    """

    texts: list[str] = Field(
        min_length=1,
        description="The texts to insert",
    )
    file_sources: Optional[list[str]] = Field(
        default=None, min_length=0, description="Sources of the texts"
    )

    @field_validator("texts", mode="after")
    @classmethod
    def strip_texts_after(cls, texts: list[str]) -> list[str]:
        return [text.strip() for text in texts]

    @field_validator("file_sources", mode="before")
    @classmethod
    def normalize_sources_before(
        cls, file_sources: Optional[list[str]]
    ) -> Optional[list[str]]:
        if file_sources is None:
            return None

        return [normalize_file_path(file_source) for file_source in file_sources]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "texts": [
                    "This is the first text to be inserted.",
                    "This is the second text to be inserted.",
                ],
                "file_sources": [
                    "First file source (optional)",
                ],
            }
        }
    )


class InsertResponse(BaseModel):
    """Response model for document insertion operations

    Attributes:
        status: Status of the operation (success, duplicated, partial_success, failure)
        message: Detailed message describing the operation result
        track_id: Tracking ID for monitoring processing status
    """

    status: Literal["success", "duplicated", "partial_success", "failure"] = Field(
        description="Status of the operation"
    )
    message: str = Field(description="Message describing the operation result")
    track_id: str = Field(description="Tracking ID for monitoring processing status")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "success",
                "message": "File 'document.pdf' uploaded successfully. Processing will continue in background.",
                "track_id": "upload_20250729_170612_abc123",
            }
        }
    )


# ── [jonex] Custom chunks 批量入库 ──

class ChunkItem(BaseModel):
    """Single chunk with optional positional metadata."""
    content: str = Field(min_length=1, description="Chunk text content")
    page_idx: Optional[int] = Field(default=None, description="Page index (0-based)")
    line_start: Optional[int] = Field(default=None, description="Start line in full text (1-based)")
    line_end: Optional[int] = Field(default=None, description="End line in full text (1-based)")


class InsertCustomChunksRequest(BaseModel):
    """Batch insert pre-chunked text with metadata.

    Bypasses LightRAG's internal token-based splitting. Each chunk
    is stored as-is with its positional metadata preserved.
    """
    full_text: str = Field(min_length=1, description="Complete original text (stored in full_docs)")
    file_path: str = Field(default="", description="Original file name for reference")
    chunks: list[ChunkItem] = Field(min_length=1, description="Pre-chunked text with metadata")
    doc_id: Optional[str] = Field(default=None, description="Document ID (auto-generated from full_text hash if omitted)")


class ClearDocumentsResponse(BaseModel):
    """Response model for document clearing operation

    Attributes:
        status: Status of the clear operation
        message: Detailed message describing the operation result
    """

    status: Literal["success", "partial_success", "busy", "fail"] = Field(
        description="Status of the clear operation"
    )
    message: str = Field(description="Message describing the operation result")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "success",
                "message": "All documents cleared successfully. Deleted 15 files.",
            }
        }
    )


class ClearCacheRequest(BaseModel):
    """Request model for clearing cache

    This model is kept for API compatibility but no longer accepts any parameters.
    All cache will be cleared regardless of the request content.
    """

    model_config = ConfigDict(json_schema_extra={"example": {}})


class ClearCacheResponse(BaseModel):
    """Response model for cache clearing operation

    Attributes:
        status: Status of the clear operation
        message: Detailed message describing the operation result
    """

    status: Literal["success", "fail"] = Field(
        description="Status of the clear operation"
    )
    message: str = Field(description="Message describing the operation result")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "success",
                "message": "Successfully cleared cache for modes: ['default', 'naive']",
            }
        }
    )


"""Response model for document status

Attributes:
    id: Document identifier
    content_summary: Summary of document content
    content_length: Length of document content
    status: Current processing status
    created_at: Creation timestamp (ISO format string)
    updated_at: Last update timestamp (ISO format string)
    chunks_count: Number of chunks (optional)
    error: Error message if any (optional)
    metadata: Additional metadata (optional)
    file_path: Path to the document file
"""


class DeleteDocRequest(BaseModel):
    doc_ids: List[str] = Field(..., description="The IDs of the documents to delete.")
    delete_file: bool = Field(
        default=False,
        description="Whether to delete the corresponding file in the upload directory.",
    )
    delete_llm_cache: bool = Field(
        default=False,
        description="Whether to delete cached LLM extraction results for the documents.",
    )

    @field_validator("doc_ids", mode="after")
    @classmethod
    def validate_doc_ids(cls, doc_ids: List[str]) -> List[str]:
        if not doc_ids:
            raise ValueError("Document IDs list cannot be empty")

        validated_ids = []
        for doc_id in doc_ids:
            if not doc_id or not doc_id.strip():
                raise ValueError("Document ID cannot be empty")
            validated_ids.append(doc_id.strip())

        # Check for duplicates
        if len(validated_ids) != len(set(validated_ids)):
            raise ValueError("Document IDs must be unique")

        return validated_ids


class DeleteEntityRequest(BaseModel):
    entity_name: str = Field(..., description="The name of the entity to delete.")

    @field_validator("entity_name", mode="after")
    @classmethod
    def validate_entity_name(cls, entity_name: str) -> str:
        if not entity_name or not entity_name.strip():
            raise ValueError("Entity name cannot be empty")
        return entity_name.strip()


class DeleteRelationRequest(BaseModel):
    source_entity: str = Field(..., description="The name of the source entity.")
    target_entity: str = Field(..., description="The name of the target entity.")

    @field_validator("source_entity", "target_entity", mode="after")
    @classmethod
    def validate_entity_names(cls, entity_name: str) -> str:
        if not entity_name or not entity_name.strip():
            raise ValueError("Entity name cannot be empty")
        return entity_name.strip()


class DocStatusResponse(BaseModel):
    id: str = Field(description="Document identifier")
    content_summary: str = Field(description="Summary of document content")
    content_length: int = Field(description="Length of document content in characters")
    status: DocStatus = Field(description="Current processing status")
    created_at: str = Field(description="Creation timestamp (ISO format string)")
    updated_at: str = Field(description="Last update timestamp (ISO format string)")
    track_id: Optional[str] = Field(
        default=None, description="Tracking ID for monitoring progress"
    )
    chunks_count: Optional[int] = Field(
        default=None, description="Number of chunks the document was split into"
    )
    error_msg: Optional[str] = Field(
        default=None, description="Error message if processing failed"
    )
    metadata: Optional[dict[str, Any]] = Field(
        default=None, description="Additional metadata about the document"
    )
    file_path: str = Field(description="Path to the document file")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "doc_123456",
                "content_summary": "Research paper on machine learning",
                "content_length": 15240,
                "status": "processed",
                "created_at": "2025-03-31T12:34:56",
                "updated_at": "2025-03-31T12:35:30",
                "track_id": "upload_20250729_170612_abc123",
                "chunks_count": 12,
                "error": None,
                "metadata": {"author": "John Doe", "year": 2025},
                "file_path": "research_paper.pdf",
            }
        }
    )


class DocsStatusesResponse(BaseModel):
    """Response model for document statuses

    Attributes:
        statuses: Dictionary mapping document status to lists of document status responses
    """

    statuses: Dict[DocStatus, List[DocStatusResponse]] = Field(
        default_factory=dict,
        description="Dictionary mapping document status to lists of document status responses",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "statuses": {
                    "PENDING": [
                        {
                            "id": "doc_123",
                            "content_summary": "Pending document",
                            "content_length": 5000,
                            "status": "pending",
                            "created_at": "2025-03-31T10:00:00",
                            "updated_at": "2025-03-31T10:00:00",
                            "track_id": "upload_20250331_100000_abc123",
                            "chunks_count": None,
                            "error": None,
                            "metadata": None,
                            "file_path": "pending_doc.pdf",
                        }
                    ],
                    "PREPROCESSED": [
                        {
                            "id": "doc_789",
                            "content_summary": "Document pending final indexing",
                            "content_length": 7200,
                            "status": "preprocessed",
                            "created_at": "2025-03-31T09:30:00",
                            "updated_at": "2025-03-31T09:35:00",
                            "track_id": "upload_20250331_093000_xyz789",
                            "chunks_count": 10,
                            "error": None,
                            "metadata": None,
                            "file_path": "preprocessed_doc.pdf",
                        }
                    ],
                    "PROCESSED": [
                        {
                            "id": "doc_456",
                            "content_summary": "Processed document",
                            "content_length": 8000,
                            "status": "processed",
                            "created_at": "2025-03-31T09:00:00",
                            "updated_at": "2025-03-31T09:05:00",
                            "track_id": "insert_20250331_090000_def456",
                            "chunks_count": 8,
                            "error": None,
                            "metadata": {"author": "John Doe"},
                            "file_path": "processed_doc.pdf",
                        }
                    ],
                }
            }
        }
    )


class TrackStatusResponse(BaseModel):
    """Response model for tracking document processing status by track_id

    Attributes:
        track_id: The tracking ID
        documents: List of documents associated with this track_id
        total_count: Total number of documents for this track_id
        status_summary: Count of documents by status
    """

    track_id: str = Field(description="The tracking ID")
    documents: List[DocStatusResponse] = Field(
        description="List of documents associated with this track_id"
    )
    total_count: int = Field(description="Total number of documents for this track_id")
    status_summary: Dict[str, int] = Field(description="Count of documents by status")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "track_id": "upload_20250729_170612_abc123",
                "documents": [
                    {
                        "id": "doc_123456",
                        "content_summary": "Research paper on machine learning",
                        "content_length": 15240,
                        "status": "PROCESSED",
                        "created_at": "2025-03-31T12:34:56",
                        "updated_at": "2025-03-31T12:35:30",
                        "track_id": "upload_20250729_170612_abc123",
                        "chunks_count": 12,
                        "error": None,
                        "metadata": {"author": "John Doe", "year": 2025},
                        "file_path": "research_paper.pdf",
                    }
                ],
                "total_count": 1,
                "status_summary": {"PROCESSED": 1},
            }
        }
    )


class DocumentsRequest(BaseModel):
    """Request model for paginated document queries

    Attributes:
        status_filter: Filter by document status, None for all statuses
        page: Page number (1-based)
        page_size: Number of documents per page (10-200)
        sort_field: Field to sort by ('created_at', 'updated_at', 'id', 'file_path')
        sort_direction: Sort direction ('asc' or 'desc')
    """

    status_filter: Optional[DocStatus] = Field(
        default=None, description="Filter by document status, None for all statuses"
    )
    page: int = Field(default=1, ge=1, description="Page number (1-based)")
    page_size: int = Field(
        default=50, ge=10, le=200, description="Number of documents per page (10-200)"
    )
    sort_field: Literal["created_at", "updated_at", "id", "file_path"] = Field(
        default="updated_at", description="Field to sort by"
    )
    sort_direction: Literal["asc", "desc"] = Field(
        default="desc", description="Sort direction"
    )
    doc_id: Optional[str] = Field(
        default=None,
        description="[jonex] Filter to a single document by its file_source doc= anchor",
    )
    file_path: Optional[str] = Field(
        default=None, description="[jonex] Filter by exact file_path"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status_filter": "PROCESSED",
                "page": 1,
                "page_size": 50,
                "sort_field": "updated_at",
                "sort_direction": "desc",
            }
        }
    )


class PaginationInfo(BaseModel):
    """Pagination information

    Attributes:
        page: Current page number
        page_size: Number of items per page
        total_count: Total number of items
        total_pages: Total number of pages
        has_next: Whether there is a next page
        has_prev: Whether there is a previous page
    """

    page: int = Field(description="Current page number")
    page_size: int = Field(description="Number of items per page")
    total_count: int = Field(description="Total number of items")
    total_pages: int = Field(description="Total number of pages")
    has_next: bool = Field(description="Whether there is a next page")
    has_prev: bool = Field(description="Whether there is a previous page")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "page": 1,
                "page_size": 50,
                "total_count": 150,
                "total_pages": 3,
                "has_next": True,
                "has_prev": False,
            }
        }
    )


class PaginatedDocsResponse(BaseModel):
    """Response model for paginated document queries

    Attributes:
        documents: List of documents for the current page
        pagination: Pagination information
        status_counts: Count of documents by status for all documents
    """

    documents: List[DocStatusResponse] = Field(
        description="List of documents for the current page"
    )
    pagination: PaginationInfo = Field(description="Pagination information")
    status_counts: Dict[str, int] = Field(
        description="Count of documents by status for all documents"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "documents": [
                    {
                        "id": "doc_123456",
                        "content_summary": "Research paper on machine learning",
                        "content_length": 15240,
                        "status": "PROCESSED",
                        "created_at": "2025-03-31T12:34:56",
                        "updated_at": "2025-03-31T12:35:30",
                        "track_id": "upload_20250729_170612_abc123",
                        "chunks_count": 12,
                        "error_msg": None,
                        "metadata": {"author": "John Doe", "year": 2025},
                        "file_path": "research_paper.pdf",
                    }
                ],
                "pagination": {
                    "page": 1,
                    "page_size": 50,
                    "total_count": 150,
                    "total_pages": 3,
                    "has_next": True,
                    "has_prev": False,
                },
                "status_counts": {
                    "PENDING": 10,
                    "PROCESSING": 5,
                    "PREPROCESSED": 5,
                    "PROCESSED": 130,
                    "FAILED": 5,
                },
            }
        }
    )


class StatusCountsResponse(BaseModel):
    """Response model for document status counts

    Attributes:
        status_counts: Count of documents by status
    """

    status_counts: Dict[str, int] = Field(description="Count of documents by status")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status_counts": {
                    "PENDING": 10,
                    "PROCESSING": 5,
                    "PREPROCESSED": 5,
                    "PROCESSED": 130,
                    "FAILED": 5,
                }
            }
        }
    )


class PipelineStatusResponse(BaseModel):
    """Response model for pipeline status

    Attributes:
        autoscanned: Whether auto-scan has started
        busy: Whether the pipeline is currently busy
        job_name: Current job name (e.g., indexing files/indexing texts)
        job_start: Job start time as ISO format string with timezone (optional)
        docs: Total number of documents to be indexed
        batchs: Number of batches for processing documents
        cur_batch: Current processing batch
        request_pending: Flag for pending request for processing
        latest_message: Latest message from pipeline processing
        history_messages: List of history messages
        update_status: Status of update flags for all namespaces
    """

    autoscanned: bool = False
    busy: bool = False
    job_name: str = "Default Job"
    job_start: Optional[str] = None
    docs: int = 0
    batchs: int = 0
    cur_batch: int = 0
    request_pending: bool = False
    latest_message: str = ""
    history_messages: Optional[List[str]] = None
    update_status: Optional[dict] = None

    @field_validator("job_start", mode="before")
    @classmethod
    def parse_job_start(cls, value):
        """Process datetime and return as ISO format string with timezone"""
        return format_datetime(value)

    model_config = ConfigDict(extra="allow")


class DocumentManager:
    def __init__(
        self,
        input_dir: str,
        workspace: str = "",  # New parameter for workspace isolation
        supported_extensions: tuple = (
            ".txt",
            ".md",
            ".mdx",  # MDX (Markdown + JSX)
            ".pdf",
            ".docx",
            ".pptx",
            ".xlsx",
            ".rtf",  # Rich Text Format
            ".odt",  # OpenDocument Text
            ".tex",  # LaTeX
            ".epub",  # Electronic Publication
            ".html",  # HyperText Markup Language
            ".htm",  # HyperText Markup Language
            ".csv",  # Comma-Separated Values
            ".json",  # JavaScript Object Notation
            ".xml",  # eXtensible Markup Language
            ".yaml",  # YAML Ain't Markup Language
            ".yml",  # YAML
            ".log",  # Log files
            ".conf",  # Configuration files
            ".ini",  # Initialization files
            ".properties",  # Java properties files
            ".sql",  # SQL scripts
            ".bat",  # Batch files
            ".sh",  # Shell scripts
            ".c",  # C source code
            ".h",  # C header
            ".cpp",  # C++ source code
            ".hpp",  # C++ header
            ".py",  # Python source code
            ".java",  # Java source code
            ".js",  # JavaScript source code
            ".ts",  # TypeScript source code
            ".swift",  # Swift source code
            ".go",  # Go source code
            ".rb",  # Ruby source code
            ".php",  # PHP source code
            ".css",  # Cascading Style Sheets
            ".scss",  # Sassy CSS
            ".less",  # LESS CSS
        ),
    ):
        # Store the base input directory and workspace
        self.base_input_dir = Path(input_dir)
        self.workspace = workspace
        self.supported_extensions = supported_extensions
        self.indexed_files = set()

        # Create workspace-specific input directory
        # If workspace is provided, create a subdirectory for data isolation
        if workspace:
            self.input_dir = self.base_input_dir / workspace
        else:
            self.input_dir = self.base_input_dir

        # Create input directory if it doesn't exist
        self.input_dir.mkdir(parents=True, exist_ok=True)

    def scan_directory_for_new_files(self) -> List[Path]:
        """Scan input directory for new files"""
        new_files = []
        for ext in self.supported_extensions:
            logger.debug(f"Scanning for {ext} files in {self.input_dir}")
            for file_path in self.input_dir.glob(f"*{ext}"):
                if file_path not in self.indexed_files:
                    new_files.append(file_path)
        return new_files

    def mark_as_indexed(self, file_path: Path):
        self.indexed_files.add(file_path)

    def is_supported_file(self, filename: str) -> bool:
        return any(filename.lower().endswith(ext) for ext in self.supported_extensions)


def validate_file_path_security(file_path_str: str, base_dir: Path) -> Optional[Path]:
    """
    Validate file path security to prevent Path Traversal attacks.

    Args:
        file_path_str: The file path string to validate
        base_dir: The base directory that the file must be within

    Returns:
        Path: Safe file path if valid, None if unsafe or invalid
    """
    if not file_path_str or not file_path_str.strip():
        return None

    try:
        # Clean the file path string
        clean_path_str = file_path_str.strip()

        # Check for obvious path traversal patterns before processing
        # This catches both Unix (..) and Windows (..\) style traversals
        if ".." in clean_path_str:
            # Additional check for Windows-style backslash traversal
            if (
                "\\..\\" in clean_path_str
                or clean_path_str.startswith("..\\")
                or clean_path_str.endswith("\\..")
            ):
                # logger.warning(
                #     f"Security violation: Windows path traversal attempt detected - {file_path_str}"
                # )
                return None

        # Normalize path separators (convert backslashes to forward slashes)
        # This helps handle Windows-style paths on Unix systems
        normalized_path = clean_path_str.replace("\\", "/")

        # Create path object and resolve it (handles symlinks and relative paths)
        candidate_path = (base_dir / normalized_path).resolve()
        base_dir_resolved = base_dir.resolve()

        # Check if the resolved path is within the base directory
        if not candidate_path.is_relative_to(base_dir_resolved):
            # logger.warning(
            #     f"Security violation: Path traversal attempt detected - {file_path_str}"
            # )
            return None

        return candidate_path

    except (OSError, ValueError, Exception) as e:
        logger.warning(f"Invalid file path detected: {file_path_str} - {str(e)}")
        return None


def get_unique_filename_in_enqueued(target_dir: Path, original_name: str) -> str:
    """Generate a unique filename in the target directory by adding numeric suffixes if needed

    Args:
        target_dir: Target directory path
        original_name: Original filename

    Returns:
        str: Unique filename (may have numeric suffix added)
    """
    import time

    original_path = Path(original_name)
    base_name = original_path.stem
    extension = original_path.suffix

    # Try original name first
    if not (target_dir / original_name).exists():
        return original_name

    # Try with numeric suffixes 001-999
    for i in range(1, 1000):
        suffix = f"{i:03d}"
        new_name = f"{base_name}_{suffix}{extension}"
        if not (target_dir / new_name).exists():
            return new_name

    # Fallback with timestamp if all 999 slots are taken
    timestamp = int(time.time())
    return f"{base_name}_{timestamp}{extension}"


# Document processing helper functions (synchronous)
# These functions run in thread pool via asyncio.to_thread() to avoid blocking the event loop


def _convert_with_docling(file_path: Path) -> str:
    """Convert document using docling (synchronous).

    Args:
        file_path: Path to the document file

    Returns:
        str: Extracted markdown content
    """
    from docling.document_converter import DocumentConverter  # type: ignore

    converter = DocumentConverter()
    result = converter.convert(file_path)
    return result.document.export_to_markdown()


def _extract_pdf_pypdf(file_bytes: bytes, password: str = None) -> str:
    """Extract PDF content using pypdf (synchronous).

    Args:
        file_bytes: PDF file content as bytes
        password: Optional password for encrypted PDFs

    Returns:
        str: Extracted text content

    Raises:
        Exception: If PDF is encrypted and password is incorrect or missing
    """
    from pypdf import PdfReader  # type: ignore

    pdf_file = BytesIO(file_bytes)
    reader = PdfReader(pdf_file)

    # Check if PDF is encrypted
    if reader.is_encrypted:
        # Try empty password first (covers permission-only encrypted PDFs)
        decrypt_result = reader.decrypt(password or "")
        if decrypt_result == 0:
            if password:
                raise Exception("Incorrect PDF password")
            else:
                raise Exception("PDF is encrypted but no password provided")

    # Extract text from all pages
    content = ""
    for page in reader.pages:
        content += page.extract_text() + "\n"

    return content


def _extract_docx(file_bytes: bytes) -> str:
    """Extract DOCX content including tables in document order (synchronous).

    Args:
        file_bytes: DOCX file content as bytes

    Returns:
        str: Extracted text content with tables in their original positions.
             Tables are separated from paragraphs with blank lines for clarity.
    """
    from docx import Document  # type: ignore
    from docx.table import Table  # type: ignore
    from docx.text.paragraph import Paragraph  # type: ignore

    docx_file = BytesIO(file_bytes)
    doc = Document(docx_file)

    def escape_cell(cell_value: str | None) -> str:
        """Escape characters that would break tab-delimited layout.

        Escape order is critical: backslashes first, then tabs/newlines.
        This prevents double-escaping issues.

        Args:
            cell_value: The cell value to escape (can be None or str)

        Returns:
            str: Escaped cell value safe for tab-delimited format
        """
        if cell_value is None:
            return ""
        text = str(cell_value)
        # CRITICAL: Escape backslash first to avoid double-escaping
        return (
            text.replace("\\", "\\\\")  # Must be first: \ -> \\
            .replace("\t", "&emsp;&emsp;")  # Tab -> \t (visible)
            .replace("\r\n", "<br>")  # Windows newline -> \n
            .replace("\r", "<br>")  # Mac newline -> \n
            .replace("\n", "<br>")  # Unix newline -> \n
        )

    content_parts = []
    in_table = False  # Track if we're currently processing a table

    # Iterate through all body elements in document order
    for element in doc.element.body:
        # Check if element is a paragraph
        if element.tag.endswith("p"):
            # If coming out of a table, add blank line after table
            if in_table:
                content_parts.append("")  # Blank line after table
                in_table = False

            paragraph = Paragraph(element, doc)
            text = paragraph.text
            # Always append to preserve document spacing (including blank paragraphs)
            content_parts.append(text)

        # Check if element is a table
        elif element.tag.endswith("tbl"):
            # Add blank line before table (if content exists)
            if content_parts and not in_table:
                content_parts.append("")  # Blank line before table

            in_table = True
            table = Table(element, doc)
            for row in table.rows:
                row_text = []
                for cell in row.cells:
                    cell_text = cell.text
                    # Escape special characters to preserve tab-delimited structure
                    row_text.append(escape_cell(cell_text))
                # Only add row if at least one cell has content
                if any(cell for cell in row_text):
                    content_parts.append("\t".join(row_text))

    return "\n".join(content_parts)


def _extract_pptx(file_bytes: bytes) -> str:
    """Extract PPTX content (synchronous).

    Args:
        file_bytes: PPTX file content as bytes

    Returns:
        str: Extracted text content
    """
    from pptx import Presentation  # type: ignore

    pptx_file = BytesIO(file_bytes)
    prs = Presentation(pptx_file)
    content = ""
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                content += shape.text + "\n"
    return content


def _extract_xlsx(file_bytes: bytes) -> str:
    """Extract XLSX content in tab-delimited format with clear sheet separation.

    This function processes Excel workbooks and converts them to a structured text format
    suitable for LLM prompts and RAG systems. Each sheet is clearly delimited with
    separator lines, and special characters are escaped to preserve the tab-delimited structure.

    Features:
    - Each sheet is wrapped with '====================' separators for visual distinction
    - Special characters (tabs, newlines, backslashes) are escaped to prevent structure corruption
    - Column alignment is preserved across all rows to maintain tabular structure
    - Empty rows are preserved as blank lines to maintain row structure
    - Uses sheet.max_column to determine column width efficiently

    Args:
        file_bytes: XLSX file content as bytes

    Returns:
        str: Extracted text content with all sheets in tab-delimited format.
             Format: Sheet separators, sheet name, then tab-delimited rows.

    Example output:
        ==================== Sheet: Data ====================
        Name\tAge\tCity
        Alice\t30\tNew York
        Bob\t25\tLondon

        ==================== Sheet: Summary ====================
        Total\t2
        ====================
    """
    from openpyxl import load_workbook  # type: ignore

    xlsx_file = BytesIO(file_bytes)
    wb = load_workbook(xlsx_file)

    def escape_cell(cell_value: str | int | float | None) -> str:
        """Escape characters that would break tab-delimited layout.

        Escape order is critical: backslashes first, then tabs/newlines.
        This prevents double-escaping issues.

        Args:
            cell_value: The cell value to escape (can be None, str, int, or float)

        Returns:
            str: Escaped cell value safe for tab-delimited format
        """
        if cell_value is None:
            return ""
        text = str(cell_value)
        # CRITICAL: Escape backslash first to avoid double-escaping
        return (
            text.replace("\\", "\\\\")  # Must be first: \ -> \\
            .replace("\t", "\\t")  # Tab -> \t (visible)
            .replace("\r\n", "\\n")  # Windows newline -> \n
            .replace("\r", "\\n")  # Mac newline -> \n
            .replace("\n", "\\n")  # Unix newline -> \n
        )

    def escape_sheet_title(title: str) -> str:
        """Escape sheet title to prevent formatting issues in separators.

        Args:
            title: Original sheet title

        Returns:
            str: Sanitized sheet title with tabs/newlines replaced
        """
        return str(title).replace("\n", " ").replace("\t", " ").replace("\r", " ")

    content_parts: list[str] = []
    sheet_separator = "=" * 20

    for idx, sheet in enumerate(wb):
        if idx > 0:
            content_parts.append("")  # Blank line between sheets for readability

        # Escape sheet title to handle edge cases with special characters
        safe_title = escape_sheet_title(sheet.title)
        content_parts.append(f"{sheet_separator} Sheet: {safe_title} {sheet_separator}")

        # Use sheet.max_column to get the maximum column width directly
        max_columns = sheet.max_column if sheet.max_column else 0

        # Extract rows with consistent width to preserve column alignment
        for row in sheet.iter_rows(values_only=True):
            row_parts = []

            # Build row up to max_columns width
            for idx in range(max_columns):
                if idx < len(row):
                    row_parts.append(escape_cell(row[idx]))
                else:
                    row_parts.append("")  # Pad short rows

            # Check if row is completely empty
            if all(part == "" for part in row_parts):
                # Preserve empty rows as blank lines (maintains row structure)
                content_parts.append("")
            else:
                # Join all columns to maintain consistent column count
                content_parts.append("\t".join(row_parts))

    # Final separator for symmetry (makes parsing easier)
    content_parts.append(sheet_separator)
    return "\n".join(content_parts)


async def pipeline_enqueue_file(
    rag: LightRAG, file_path: Path, track_id: str = None
) -> tuple[bool, str]:
    """Add a file to the queue for processing

    Args:
        rag: LightRAG instance
        file_path: Path to the saved file
        track_id: Optional tracking ID, if not provided will be generated
    Returns:
        tuple: (success: bool, track_id: str)
    """

    # Generate track_id if not provided
    if track_id is None:
        track_id = generate_track_id("unknown")

    try:
        content = ""
        ext = file_path.suffix.lower()
        file_size = 0

        # Get file size for error reporting
        try:
            stat = await asyncio.to_thread(file_path.stat)
            file_size = stat.st_size
        except Exception:
            file_size = 0

        file = None
        try:
            async with aiofiles.open(file_path, "rb") as f:
                file = await f.read()
        except PermissionError as e:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "[File Extraction]Permission denied - cannot read file",
                    "original_error": str(e),
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(
                f"[File Extraction]Permission denied reading file: {file_path.name}"
            )
            return False, track_id
        except FileNotFoundError as e:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "[File Extraction]File not found",
                    "original_error": str(e),
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(f"[File Extraction]File not found: {file_path.name}")
            return False, track_id
        except Exception as e:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "[File Extraction]File reading error",
                    "original_error": str(e),
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(
                f"[File Extraction]Error reading file {file_path.name}: {str(e)}"
            )
            return False, track_id

        # Process based on file type
        try:
            match ext:
                case (
                    ".txt"
                    | ".md"
                    | ".mdx"
                    | ".html"
                    | ".htm"
                    | ".tex"
                    | ".json"
                    | ".xml"
                    | ".yaml"
                    | ".yml"
                    | ".rtf"
                    | ".odt"
                    | ".epub"
                    | ".csv"
                    | ".log"
                    | ".conf"
                    | ".ini"
                    | ".properties"
                    | ".sql"
                    | ".bat"
                    | ".sh"
                    | ".c"
                    | ".h"
                    | ".cpp"
                    | ".hpp"
                    | ".py"
                    | ".java"
                    | ".js"
                    | ".ts"
                    | ".swift"
                    | ".go"
                    | ".rb"
                    | ".php"
                    | ".css"
                    | ".scss"
                    | ".less"
                ):
                    try:
                        # Try to decode as UTF-8 (offloaded to thread to avoid blocking the event loop)
                        content = await asyncio.to_thread(file.decode, "utf-8")

                        # Validate content
                        if not content or len(content.strip()) == 0:
                            error_files = [
                                {
                                    "file_path": str(file_path.name),
                                    "error_description": "[File Extraction]Empty file content",
                                    "original_error": "File contains no content or only whitespace",
                                    "file_size": file_size,
                                }
                            ]
                            await rag.apipeline_enqueue_error_documents(
                                error_files, track_id
                            )
                            logger.error(
                                f"[File Extraction]Empty content in file: {file_path.name}"
                            )
                            return False, track_id

                        # Check if content looks like binary data string representation
                        if content.startswith("b'") or content.startswith('b"'):
                            error_files = [
                                {
                                    "file_path": str(file_path.name),
                                    "error_description": "[File Extraction]Binary data in text file",
                                    "original_error": "File appears to contain binary data representation instead of text",
                                    "file_size": file_size,
                                }
                            ]
                            await rag.apipeline_enqueue_error_documents(
                                error_files, track_id
                            )
                            logger.error(
                                f"[File Extraction]File {file_path.name} appears to contain binary data representation instead of text"
                            )
                            return False, track_id

                    except UnicodeDecodeError as e:
                        error_files = [
                            {
                                "file_path": str(file_path.name),
                                "error_description": "[File Extraction]UTF-8 encoding error, please convert it to UTF-8 before processing",
                                "original_error": f"File is not valid UTF-8 encoded text: {str(e)}",
                                "file_size": file_size,
                            }
                        ]
                        await rag.apipeline_enqueue_error_documents(
                            error_files, track_id
                        )
                        logger.error(
                            f"[File Extraction]File {file_path.name} is not valid UTF-8 encoded text. Please convert it to UTF-8 before processing."
                        )
                        return False, track_id

                case ".pdf":
                    try:
                        # Try DOCLING first if configured and available
                        if (
                            global_args.document_loading_engine == "DOCLING"
                            and _is_docling_available()
                        ):
                            content = await asyncio.to_thread(
                                _convert_with_docling, file_path
                            )
                        else:
                            if (
                                global_args.document_loading_engine == "DOCLING"
                                and not _is_docling_available()
                            ):
                                logger.warning(
                                    f"DOCLING engine configured but not available for {file_path.name}. Falling back to pypdf."
                                )
                            # Use pypdf (non-blocking via to_thread)
                            content = await asyncio.to_thread(
                                _extract_pdf_pypdf,
                                file,
                                global_args.pdf_decrypt_password,
                            )
                    except Exception as e:
                        error_files = [
                            {
                                "file_path": str(file_path.name),
                                "error_description": "[File Extraction]PDF processing error",
                                "original_error": f"Failed to extract text from PDF: {str(e)}",
                                "file_size": file_size,
                            }
                        ]
                        await rag.apipeline_enqueue_error_documents(
                            error_files, track_id
                        )
                        logger.error(
                            f"[File Extraction]Error processing PDF {file_path.name}: {str(e)}"
                        )
                        return False, track_id

                case ".docx":
                    try:
                        # Try DOCLING first if configured and available
                        if (
                            global_args.document_loading_engine == "DOCLING"
                            and _is_docling_available()
                        ):
                            content = await asyncio.to_thread(
                                _convert_with_docling, file_path
                            )
                        else:
                            if (
                                global_args.document_loading_engine == "DOCLING"
                                and not _is_docling_available()
                            ):
                                logger.warning(
                                    f"DOCLING engine configured but not available for {file_path.name}. Falling back to python-docx."
                                )
                            # Use python-docx (non-blocking via to_thread)
                            content = await asyncio.to_thread(_extract_docx, file)
                    except Exception as e:
                        error_files = [
                            {
                                "file_path": str(file_path.name),
                                "error_description": "[File Extraction]DOCX processing error",
                                "original_error": f"Failed to extract text from DOCX: {str(e)}",
                                "file_size": file_size,
                            }
                        ]
                        await rag.apipeline_enqueue_error_documents(
                            error_files, track_id
                        )
                        logger.error(
                            f"[File Extraction]Error processing DOCX {file_path.name}: {str(e)}"
                        )
                        return False, track_id

                case ".pptx":
                    try:
                        # Try DOCLING first if configured and available
                        if (
                            global_args.document_loading_engine == "DOCLING"
                            and _is_docling_available()
                        ):
                            content = await asyncio.to_thread(
                                _convert_with_docling, file_path
                            )
                        else:
                            if (
                                global_args.document_loading_engine == "DOCLING"
                                and not _is_docling_available()
                            ):
                                logger.warning(
                                    f"DOCLING engine configured but not available for {file_path.name}. Falling back to python-pptx."
                                )
                            # Use python-pptx (non-blocking via to_thread)
                            content = await asyncio.to_thread(_extract_pptx, file)
                    except Exception as e:
                        error_files = [
                            {
                                "file_path": str(file_path.name),
                                "error_description": "[File Extraction]PPTX processing error",
                                "original_error": f"Failed to extract text from PPTX: {str(e)}",
                                "file_size": file_size,
                            }
                        ]
                        await rag.apipeline_enqueue_error_documents(
                            error_files, track_id
                        )
                        logger.error(
                            f"[File Extraction]Error processing PPTX {file_path.name}: {str(e)}"
                        )
                        return False, track_id

                case ".xlsx":
                    try:
                        # Try DOCLING first if configured and available
                        if (
                            global_args.document_loading_engine == "DOCLING"
                            and _is_docling_available()
                        ):
                            content = await asyncio.to_thread(
                                _convert_with_docling, file_path
                            )
                        else:
                            if (
                                global_args.document_loading_engine == "DOCLING"
                                and not _is_docling_available()
                            ):
                                logger.warning(
                                    f"DOCLING engine configured but not available for {file_path.name}. Falling back to openpyxl."
                                )
                            # Use openpyxl (non-blocking via to_thread)
                            content = await asyncio.to_thread(_extract_xlsx, file)
                    except Exception as e:
                        error_files = [
                            {
                                "file_path": str(file_path.name),
                                "error_description": "[File Extraction]XLSX processing error",
                                "original_error": f"Failed to extract text from XLSX: {str(e)}",
                                "file_size": file_size,
                            }
                        ]
                        await rag.apipeline_enqueue_error_documents(
                            error_files, track_id
                        )
                        logger.error(
                            f"[File Extraction]Error processing XLSX {file_path.name}: {str(e)}"
                        )
                        return False, track_id

                case _:
                    error_files = [
                        {
                            "file_path": str(file_path.name),
                            "error_description": f"[File Extraction]Unsupported file type: {ext}",
                            "original_error": f"File extension {ext} is not supported",
                            "file_size": file_size,
                        }
                    ]
                    await rag.apipeline_enqueue_error_documents(error_files, track_id)
                    logger.error(
                        f"[File Extraction]Unsupported file type: {file_path.name} (extension {ext})"
                    )
                    return False, track_id

        except Exception as e:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "[File Extraction]File format processing error",
                    "original_error": f"Unexpected error during file extracting: {str(e)}",
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(
                f"[File Extraction]Unexpected error during {file_path.name} extracting: {str(e)}"
            )
            return False, track_id

        # Insert into the RAG queue
        if content:
            # Check if content contains only whitespace characters
            if not content.strip():
                error_files = [
                    {
                        "file_path": str(file_path.name),
                        "error_description": "[File Extraction]File contains only whitespace",
                        "original_error": "File content contains only whitespace characters",
                        "file_size": file_size,
                    }
                ]
                await rag.apipeline_enqueue_error_documents(error_files, track_id)
                logger.warning(
                    f"[File Extraction]File contains only whitespace characters: {file_path.name}"
                )
                return False, track_id

            try:
                await rag.apipeline_enqueue_documents(
                    content, file_paths=file_path.name, track_id=track_id
                )

                logger.info(
                    f"Successfully extracted and enqueued file: {file_path.name}"
                )

                # Move file to __enqueued__ directory after enqueuing
                try:
                    enqueued_dir = file_path.parent / "__enqueued__"
                    await asyncio.to_thread(enqueued_dir.mkdir, exist_ok=True)

                    # Generate unique filename to avoid conflicts
                    unique_filename = get_unique_filename_in_enqueued(
                        enqueued_dir, file_path.name
                    )
                    target_path = enqueued_dir / unique_filename

                    # Move the file
                    await asyncio.to_thread(file_path.rename, target_path)
                    logger.debug(
                        f"Moved file to enqueued directory: {file_path.name} -> {unique_filename}"
                    )

                except Exception as move_error:
                    logger.error(
                        f"Failed to move file {file_path.name} to __enqueued__ directory: {move_error}"
                    )
                    # Don't affect the main function's success status

                return True, track_id

            except Exception as e:
                error_files = [
                    {
                        "file_path": str(file_path.name),
                        "error_description": "Document enqueue error",
                        "original_error": f"Failed to enqueue document: {str(e)}",
                        "file_size": file_size,
                    }
                ]
                await rag.apipeline_enqueue_error_documents(error_files, track_id)
                logger.error(f"Error enqueueing document {file_path.name}: {str(e)}")
                return False, track_id
        else:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "No content extracted",
                    "original_error": "No content could be extracted from file",
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(f"No content extracted from file: {file_path.name}")
            return False, track_id

    except Exception as e:
        # Catch-all for any unexpected errors
        try:
            file_size = file_path.stat().st_size if file_path.exists() else 0
        except Exception:
            file_size = 0

        error_files = [
            {
                "file_path": str(file_path.name),
                "error_description": "Unexpected processing error",
                "original_error": f"Unexpected error: {str(e)}",
                "file_size": file_size,
            }
        ]
        await rag.apipeline_enqueue_error_documents(error_files, track_id)
        logger.error(f"Enqueuing file {file_path.name} error: {str(e)}")
        logger.error(traceback.format_exc())
        return False, track_id
    finally:
        if file_path.name.startswith(temp_prefix):
            try:
                file_path.unlink()
            except Exception as e:
                logger.error(f"Error deleting file {file_path}: {str(e)}")


async def pipeline_index_file(rag: LightRAG, file_path: Path, track_id: str = None):
    """Index a file with track_id

    Args:
        rag: LightRAG instance
        file_path: Path to the saved file
        track_id: Optional tracking ID
    """
    try:
        success, returned_track_id = await pipeline_enqueue_file(
            rag, file_path, track_id
        )
        if success:
            await rag.apipeline_process_enqueue_documents()

    except Exception as e:
        logger.error(f"Error indexing file {file_path.name}: {str(e)}")
        logger.error(traceback.format_exc())


async def pipeline_index_files(
    rag: LightRAG, file_paths: List[Path], track_id: str = None
):
    """Index multiple files sequentially to avoid high CPU load

    Args:
        rag: LightRAG instance
        file_paths: Paths to the files to index
        track_id: Optional tracking ID to pass to all files
    """
    if not file_paths:
        return
    try:
        enqueued = False

        # Use get_pinyin_sort_key for Chinese pinyin sorting
        sorted_file_paths = sorted(
            file_paths, key=lambda p: get_pinyin_sort_key(str(p))
        )

        # Process files sequentially with track_id
        for file_path in sorted_file_paths:
            success, _ = await pipeline_enqueue_file(rag, file_path, track_id)
            if success:
                enqueued = True

        # Process the queue only if at least one file was successfully enqueued
        if enqueued:
            await rag.apipeline_process_enqueue_documents()
    except Exception as e:
        logger.error(f"Error indexing files: {str(e)}")
        logger.error(traceback.format_exc())


async def pipeline_index_texts(
    rag: LightRAG,
    texts: List[str],
    file_sources: List[str] = None,
    track_id: str = None,
):
    """Index a list of texts with track_id

    Args:
        rag: LightRAG instance
        texts: The texts to index
        file_sources: Sources of the texts
        track_id: Optional tracking ID
    """
    if not texts:
        return

    normalized_file_sources: list[str] | None = None
    if file_sources:
        normalized_file_sources = [
            normalize_file_path(source) for source in file_sources
        ]
        if len(normalized_file_sources) > len(texts):
            raise ValueError("Number of file sources must not exceed number of texts")
        if len(normalized_file_sources) < len(texts):
            normalized_file_sources.extend(
                [UNKNOWN_FILE_SOURCE] * (len(texts) - len(normalized_file_sources))
            )

    await rag.apipeline_enqueue_documents(
        input=texts, file_paths=normalized_file_sources, track_id=track_id
    )
    await rag.apipeline_process_enqueue_documents()


async def run_scanning_process(
    rag: LightRAG, doc_manager: DocumentManager, track_id: str = None
):
    """Background task to scan and index documents

    Args:
        rag: LightRAG instance
        doc_manager: DocumentManager instance
        track_id: Optional tracking ID to pass to all scanned files
    """
    try:
        new_files = doc_manager.scan_directory_for_new_files()
        total_files = len(new_files)
        logger.info(f"Found {total_files} files to index.")

        if new_files:
            # Check for files with PROCESSED status and filter them out
            valid_files = []
            processed_files = []

            for file_path in new_files:
                filename = file_path.name
                existing_doc_data = await rag.doc_status.get_doc_by_file_path(filename)

                if existing_doc_data and existing_doc_data.get("status") == "processed":
                    # File is already PROCESSED, skip it with warning
                    processed_files.append(filename)
                    logger.warning(f"Skipping already processed file: {filename}")
                else:
                    # File is new or in non-PROCESSED status, add to processing list
                    valid_files.append(file_path)

            # Process valid files (new files + non-PROCESSED status files)
            if valid_files:
                await pipeline_index_files(rag, valid_files, track_id)
                if processed_files:
                    logger.info(
                        f"Scanning process completed: {len(valid_files)} files Processed {len(processed_files)} skipped."
                    )
                else:
                    logger.info(
                        f"Scanning process completed: {len(valid_files)} files Processed."
                    )
            else:
                logger.info(
                    "No files to process after filtering already processed files."
                )
        else:
            # No new files to index, check if there are any documents in the queue
            logger.info(
                "No upload file found, check if there are any documents in the queue..."
            )
            await rag.apipeline_process_enqueue_documents()

    except Exception as e:
        logger.error(f"Error during scanning process: {str(e)}")
        logger.error(traceback.format_exc())


async def background_delete_documents(
    rag: LightRAG,
    doc_manager: DocumentManager,
    doc_ids: List[str],
    delete_file: bool = False,
    delete_llm_cache: bool = False,
    jonex_ctx: dict | None = None,  # [jonex] R4: {tenant_id, kb_id, doc_id, trace_id}
):
    """Background task to delete multiple documents.

    [jonex] R4：在后台任务入口设置计量 contextvar（scene=lightrag_rebuild），
    使删除期间 LLM rebuild 调用的计量维度正确归因（不再误记为 lightrag_query）。
    contextvar 在 asyncio.create_task 时自动复制到子 task，覆盖全部子操作。
    """
    meter_token = None  # [jonex] R4：在 try 内设置，finally 统一清理，避免早返回泄漏

    from lightrag.kg.shared_storage import (
        get_namespace_data,
        get_namespace_lock,
    )

    pipeline_status = await get_namespace_data(
        "pipeline_status", workspace=rag.workspace
    )
    pipeline_status_lock = get_namespace_lock(
        "pipeline_status", workspace=rag.workspace
    )

    total_docs = len(doc_ids)
    successful_deletions = []
    failed_deletions = []
    # [jonex] R2：批末统一 rebuild 累加器
    all_affected_entity_names: set[str] = set()
    all_affected_relation_keys: set[tuple] = set()
    # [jonex] R5-d：删除耗时埋点
    _delete_start_time = time.time()
    _rebuild_entity_count = 0
    _rebuild_relation_count = 0
    _rebuild_start_time = 0.0
    _rebuild_elapsed = 0.0

    # Double-check pipeline status before proceeding
    async with pipeline_status_lock:
        if pipeline_status.get("busy", False):
            logger.warning("Error: Unexpected pipeline busy state, aborting deletion.")
            return  # Abort deletion operation

        # Set pipeline status to busy for deletion
        pipeline_status.update(
            {
                "busy": True,
                # Job name can not be changed, it's verified in adelete_by_doc_id()
                "job_name": f"Deleting {total_docs} Documents",
                "job_start": datetime.now().isoformat(),
                "docs": total_docs,
                "batchs": total_docs,
                "cur_batch": 0,
                "latest_message": "Starting document deletion process",
            }
        )
        # Use slice assignment to clear the list in place
        pipeline_status["history_messages"][:] = ["Starting document deletion process"]
        if delete_llm_cache:
            pipeline_status["history_messages"].append(
                "LLM cache cleanup requested for this deletion job"
            )

    try:
        # [jonex] R4：在 try 内设置计量上下文（离开 try 时 finally 统一清理），
        # 避免 busy 早返回（try 之前 return）导致的 contextvar 泄漏。
        if jonex_ctx and (jonex_ctx.get("tenant_id") or jonex_ctx.get("kb_id")):
            from lightrag.jonex_metering import _jonex_ctx
            meter_token = _jonex_ctx.set({
                **jonex_ctx,
                "user_id": "",
                "scene": "lightrag_rebuild",
            })
            logger.info(
                "[jonex][R4] background_delete_documents 设置计量上下文 tenant=%s kb=%s doc=%s",
                jonex_ctx.get("tenant_id", ""), jonex_ctx.get("kb_id", ""),
                jonex_ctx.get("doc_id", ""),
            )

        # Loop through each document ID and delete them one by one
        for i, doc_id in enumerate(doc_ids, 1):
            # Check for cancellation at the start of each document deletion
            async with pipeline_status_lock:
                if pipeline_status.get("cancellation_requested", False):
                    cancel_msg = f"Deletion cancelled by user at document {i}/{total_docs}. {len(successful_deletions)} deleted, {total_docs - i + 1} remaining."
                    logger.info(cancel_msg)
                    pipeline_status["latest_message"] = cancel_msg
                    pipeline_status["history_messages"].append(cancel_msg)
                    # Add remaining documents to failed list with cancellation reason
                    failed_deletions.extend(
                        doc_ids[i - 1 :]
                    )  # i-1 because enumerate starts at 1
                    break  # Exit the loop, remaining documents unchanged

                start_msg = f"Deleting document {i}/{total_docs}: {doc_id}"
                logger.info(start_msg)
                pipeline_status["cur_batch"] = i
                pipeline_status["latest_message"] = start_msg
                pipeline_status["history_messages"].append(start_msg)

            file_path = "#"
            try:
                # [jonex] R2：整批统一 rebuild —— 逐 doc 跳过 rebuild 并收集受影响实体/关系
                result = await rag.adelete_by_doc_id(
                    doc_id, delete_llm_cache=delete_llm_cache,
                    defer_rebuild=True,
                )
                file_path = (
                    getattr(result, "file_path", "-") if "result" in locals() else "-"
                )
                if result.status == "success":
                    successful_deletions.append(doc_id)
                    # [jonex] R2：累加受影响实体/关系，批末统一 rebuild
                    if hasattr(result, "affected_entity_names"):
                        all_affected_entity_names.update(result.affected_entity_names)
                    if hasattr(result, "affected_relation_keys"):
                        all_affected_relation_keys.update(result.affected_relation_keys)
                    success_msg = (
                        f"Document deleted {i}/{total_docs}: {doc_id}[{file_path}]"
                    )
                    logger.info(success_msg)
                    async with pipeline_status_lock:
                        pipeline_status["history_messages"].append(success_msg)

                    # Handle file deletion if requested and file_path is available
                    if (
                        delete_file
                        and result.file_path
                        and result.file_path != "unknown_source"
                    ):
                        try:
                            deleted_files = []
                            # SECURITY FIX: Use secure path validation to prevent arbitrary file deletion
                            safe_file_path = validate_file_path_security(
                                result.file_path, doc_manager.input_dir
                            )

                            if safe_file_path is None:
                                # Security violation detected - log and skip file deletion
                                security_msg = f"Security violation: Unsafe file path detected for deletion - {result.file_path}"
                                logger.warning(security_msg)
                                async with pipeline_status_lock:
                                    pipeline_status["latest_message"] = security_msg
                                    pipeline_status["history_messages"].append(
                                        security_msg
                                    )
                            else:
                                # check and delete files from input_dir directory
                                if safe_file_path.exists():
                                    try:
                                        safe_file_path.unlink()
                                        deleted_files.append(safe_file_path.name)
                                        file_delete_msg = f"Successfully deleted input_dir file: {result.file_path}"
                                        logger.info(file_delete_msg)
                                        async with pipeline_status_lock:
                                            pipeline_status["latest_message"] = (
                                                file_delete_msg
                                            )
                                            pipeline_status["history_messages"].append(
                                                file_delete_msg
                                            )
                                    except Exception as file_error:
                                        file_error_msg = f"Failed to delete input_dir file {result.file_path}: {str(file_error)}"
                                        logger.debug(file_error_msg)
                                        async with pipeline_status_lock:
                                            pipeline_status["latest_message"] = (
                                                file_error_msg
                                            )
                                            pipeline_status["history_messages"].append(
                                                file_error_msg
                                            )

                                # Also check and delete files from __enqueued__ directory
                                enqueued_dir = doc_manager.input_dir / "__enqueued__"
                                if enqueued_dir.exists():
                                    # SECURITY FIX: Validate that the file path is safe before processing
                                    # Only proceed if the original path validation passed
                                    base_name = Path(result.file_path).stem
                                    extension = Path(result.file_path).suffix

                                    # Search for exact match and files with numeric suffixes
                                    for enqueued_file in enqueued_dir.glob(
                                        f"{base_name}*{extension}"
                                    ):
                                        # Additional security check: ensure enqueued file is within enqueued directory
                                        safe_enqueued_path = (
                                            validate_file_path_security(
                                                enqueued_file.name, enqueued_dir
                                            )
                                        )
                                        if safe_enqueued_path is not None:
                                            try:
                                                enqueued_file.unlink()
                                                deleted_files.append(enqueued_file.name)
                                                logger.info(
                                                    f"Successfully deleted enqueued file: {enqueued_file.name}"
                                                )
                                            except Exception as enqueued_error:
                                                file_error_msg = f"Failed to delete enqueued file {enqueued_file.name}: {str(enqueued_error)}"
                                                logger.debug(file_error_msg)
                                                async with pipeline_status_lock:
                                                    pipeline_status[
                                                        "latest_message"
                                                    ] = file_error_msg
                                                    pipeline_status[
                                                        "history_messages"
                                                    ].append(file_error_msg)
                                        else:
                                            security_msg = f"Security violation: Unsafe enqueued file path detected - {enqueued_file.name}"
                                            logger.warning(security_msg)

                            if deleted_files == []:
                                file_error_msg = f"File deletion skipped, missing or unsafe file: {result.file_path}"
                                logger.warning(file_error_msg)
                                async with pipeline_status_lock:
                                    pipeline_status["latest_message"] = file_error_msg
                                    pipeline_status["history_messages"].append(
                                        file_error_msg
                                    )

                        except Exception as file_error:
                            file_error_msg = f"Failed to delete file {result.file_path}: {str(file_error)}"
                            logger.error(file_error_msg)
                            async with pipeline_status_lock:
                                pipeline_status["latest_message"] = file_error_msg
                                pipeline_status["history_messages"].append(
                                    file_error_msg
                                )
                    elif delete_file:
                        no_file_msg = (
                            f"File deletion skipped, missing file path: {doc_id}"
                        )
                        logger.warning(no_file_msg)
                        async with pipeline_status_lock:
                            pipeline_status["latest_message"] = no_file_msg
                            pipeline_status["history_messages"].append(no_file_msg)
                else:
                    failed_deletions.append(doc_id)
                    error_msg = f"Failed to delete {i}/{total_docs}: {doc_id}[{file_path}] - {result.message}"
                    logger.error(error_msg)
                    async with pipeline_status_lock:
                        pipeline_status["latest_message"] = error_msg
                        pipeline_status["history_messages"].append(error_msg)

            except Exception as e:
                failed_deletions.append(doc_id)
                error_msg = f"Error deleting document {i}/{total_docs}: {doc_id}[{file_path}] - {str(e)}"
                logger.error(error_msg)
                logger.error(traceback.format_exc())
                async with pipeline_status_lock:
                    pipeline_status["latest_message"] = error_msg
                    pipeline_status["history_messages"].append(error_msg)

        # [jonex] R2：批末统一 rebuild —— 所有 doc 删完后一次性重建受影响的实体/关系，
        # 把 800×rebuild 降为 1×rebuild，共享实体只重建一次（~O(N²) → ~O(N)）。
        if all_affected_entity_names or all_affected_relation_keys:
            _rebuild_start_time = time.time()  # [jonex] R5-d
            logger.info(
                "[jonex][R2] batch rebuild start: entities=%d relations=%d",
                len(all_affected_entity_names), len(all_affected_relation_keys),
            )
            try:
                from lightrag.operate import rebuild_knowledge_from_chunks
                from dataclasses import asdict

                # 从存储查询每个受影响实体/关系的当前剩余 chunk（已反映全部删除）
                entities_to_rebuild: dict = {}
                for name in all_affected_entity_names:
                    stored = await rag.entity_chunks.get_by_id(name)
                    if stored and isinstance(stored, dict):
                        chunk_ids = stored.get("chunk_ids", [])
                        if chunk_ids:
                            entities_to_rebuild[name] = [c for c in chunk_ids if c]

                relationships_to_rebuild: dict = {}
                from lightrag.utils import make_relation_chunk_key  # [jonex] R2 fix
                for key in all_affected_relation_keys:
                    # [jonex] R2 fix：relation_chunks 的存储键是 make_relation_chunk_key(src,tgt)
                    # 返回的字符串（GRAPH_FIELD_SEP.join(sorted((src,tgt)))），不能直接用元组。
                    stored = await rag.relation_chunks.get_by_id(make_relation_chunk_key(*key))
                    if stored and isinstance(stored, dict):
                        chunk_ids = stored.get("chunk_ids", [])
                        if chunk_ids:
                            relationships_to_rebuild[key] = [c for c in chunk_ids if c]

                if entities_to_rebuild or relationships_to_rebuild:
                    await rebuild_knowledge_from_chunks(
                        entities_to_rebuild=entities_to_rebuild,
                        relationships_to_rebuild=relationships_to_rebuild,
                        knowledge_graph_inst=rag.chunk_entity_relation_graph,
                        entities_vdb=rag.entities_vdb,
                        relationships_vdb=rag.relationships_vdb,
                        text_chunks_storage=rag.text_chunks,
                        llm_response_cache=rag.llm_response_cache,
                        global_config=asdict(rag),
                        pipeline_status=pipeline_status,
                        pipeline_status_lock=pipeline_status_lock,
                        entity_chunks_storage=rag.entity_chunks,
                        relation_chunks_storage=rag.relation_chunks,
                    )
                _rebuild_entity_count = len(entities_to_rebuild)  # [jonex] R5-d
                _rebuild_relation_count = len(relationships_to_rebuild)  # [jonex] R5-d
                _rebuild_elapsed = time.time() - _rebuild_start_time  # [jonex] R5-d
                logger.info(
                    "[jonex][R2] batch rebuild complete: entities=%d(%d rebuild) relations=%d(%d rebuild) elapsed=%.1fs",
                    len(all_affected_entity_names), _rebuild_entity_count,
                    len(all_affected_relation_keys), _rebuild_relation_count,
                    _rebuild_elapsed,
                )
            except Exception as rebuild_err:
                logger.error(
                    "[jonex][R2] batch rebuild failed: %s", rebuild_err,
                )
                logger.error(traceback.format_exc())
                # 不抛异常：图已更新（source_id 已去删 chunk），重建失败靠后续
                # 操作（再删/查询命中时自动修正）。记录在 pipeline history 中便于排查。
                async with pipeline_status_lock:
                    pipeline_status["history_messages"].append(
                        f"Batch rebuild failed (entities may have stale descriptions): {rebuild_err}"
                    )

    except Exception as e:
        error_msg = f"Critical error during batch deletion: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        async with pipeline_status_lock:
            pipeline_status["history_messages"].append(error_msg)
    finally:
        # Final summary and check for pending requests
        async with pipeline_status_lock:
            pipeline_status["busy"] = False
            pipeline_status["pending_requests"] = False  # Reset pending requests flag
            pipeline_status["cancellation_requested"] = (
                False  # Always reset cancellation flag
            )
            completion_msg = f"Deletion completed: {len(successful_deletions)} successful, {len(failed_deletions)} failed"
            pipeline_status["latest_message"] = completion_msg
            pipeline_status["history_messages"].append(completion_msg)

            # [jonex] R5-d：delete_timing 结构化日志（event=delete_timing）
            _delete_total_ms = int((time.time() - _delete_start_time) * 1000)
            _rebuild_ms = int(_rebuild_elapsed * 1000) if _rebuild_elapsed > 0 else 0
            logger.info(
                "delete_timing total_docs=%d successful=%d failed=%d "
                "rebuild_entities=%d rebuild_relations=%d "
                "total_ms=%d rebuild_ms=%d",
                total_docs, len(successful_deletions), len(failed_deletions),
                _rebuild_entity_count, _rebuild_relation_count,
                _delete_total_ms, _rebuild_ms,
                extra={
                    "event": "delete_timing",
                    "total_docs": total_docs,
                    "successful": len(successful_deletions),
                    "failed": len(failed_deletions),
                    "rebuild_entity_count": _rebuild_entity_count,
                    "rebuild_relation_count": _rebuild_relation_count,
                    "total_ms": _delete_total_ms,
                    "rebuild_ms": _rebuild_ms,
                    "workspace": getattr(rag, "workspace", ""),
                },
            )

            # Check if there are pending document indexing requests
            has_pending_request = pipeline_status.get("request_pending", False)

        # If there are pending requests, start document processing pipeline
        if has_pending_request:
            try:
                logger.info(
                    "Processing pending document indexing requests after deletion"
                )
                await rag.apipeline_process_enqueue_documents()
            except Exception as e:
                logger.error(f"Error processing pending documents after deletion: {e}")

        # [jonex] R4：清理计量 contextvar，避免污染同事件循环的后续任务
        if meter_token is not None:
            from lightrag.jonex_metering import clear_jonex_context
            clear_jonex_context(meter_token)


def create_document_routes(
    manager, doc_manager: DocumentManager, api_key: Optional[str] = None
):
    # [jonex] manager is a WorkspaceRAGManager (not a single LightRAG instance).
    # Each handler resolves the correct rag via LIGHTRAG-WORKSPACE header.
    # Fresh router per call — see the note above the temp_prefix constant.
    router = APIRouter(
        prefix="/documents",
        tags=["documents"],
    )

    async def _resolve_rag(request: Request):
        """[jonex] Resolve LightRAG instance for the current request's workspace."""
        return await manager.get(get_workspace_from_request(request))

    # Create combined auth dependency for document routes
    combined_auth = get_combined_auth_dependency(api_key)

    @router.post(
        "/scan", response_model=ScanResponse, dependencies=[Depends(combined_auth)]
    )
    async def scan_for_new_documents(
        http_request: Request, background_tasks: BackgroundTasks  # [jonex] +Request
    ):
        """
        Trigger the scanning process for new documents.

        This endpoint initiates a background task that scans the input directory for new documents
        and processes them. If a scanning process is already running, it returns a status indicating
        that fact.

        Returns:
            ScanResponse: A response object containing the scanning status and track_id
        """
        # Generate track_id with "scan" prefix for scanning operation
        track_id = generate_track_id("scan")

        # [jonex] Resolve workspace-specific LightRAG instance
        rag = await _resolve_rag(http_request)

        # Start the scanning process in the background with track_id
        background_tasks.add_task(run_scanning_process, rag, doc_manager, track_id)
        return ScanResponse(
            status="scanning_started",
            message="Scanning process has been initiated in the background",
            track_id=track_id,
        )

    @router.post(
        "/upload", response_model=InsertResponse, dependencies=[Depends(combined_auth)]
    )
    async def upload_to_input_dir(
        http_request: Request,  # [jonex] +Request
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
    ):
        """
        Upload a file to the input directory and index it.

        This API endpoint accepts a file through an HTTP POST request, checks if the
        uploaded file is of a supported type, saves it in the specified input directory,
        indexes it for retrieval, and returns a success status with relevant details.

        **File Size Limit:**
        - Configurable via `MAX_UPLOAD_SIZE` environment variable (default: 100MB)
        - Set to `None` or `0` for unlimited upload size
        - Returns HTTP 413 (Request Entity Too Large) if file exceeds limit

        **Duplicate Detection Behavior:**

        This endpoint handles two types of duplicate scenarios differently:

        1. **Filename Duplicate (Synchronous Detection)**:
           - Detected immediately before file processing
           - Returns `status="duplicated"` with the existing document's track_id
           - Two cases:
             - If filename exists in document storage: returns existing track_id
             - If filename exists in file system only: returns empty track_id ("")

        2. **Content Duplicate (Asynchronous Detection)**:
           - Detected during background processing after content extraction
           - Returns `status="success"` with a new track_id immediately
           - The duplicate is detected later when processing the file content
           - Use `/documents/track_status/{track_id}` to check the final result:
             - Document will have `status="FAILED"`
             - `error_msg` contains "Content already exists. Original doc_id: xxx"
             - `metadata.is_duplicate=true` with reference to original document
             - `metadata.original_doc_id` points to the existing document
             - `metadata.original_track_id` shows the original upload's track_id

        **Why Different Behavior?**
        - Filename check is fast (simple lookup), done synchronously
        - Content extraction is expensive (PDF/DOCX parsing), done asynchronously
        - This design prevents blocking the client during expensive operations

        Args:
            background_tasks: FastAPI BackgroundTasks for async processing
            file (UploadFile): The file to be uploaded. It must have an allowed extension.

        Returns:
            InsertResponse: A response object containing the upload status and a message.
                - status="success": File accepted and queued for processing
                - status="duplicated": Filename already exists (see track_id for existing document)

        Raises:
            HTTPException: If the file type is not supported (400), file too large (413), or other errors occur (500).
        """
        try:
            # [jonex] Resolve workspace-specific LightRAG instance
            rag = await _resolve_rag(http_request)

            # Sanitize filename to prevent Path Traversal attacks
            safe_filename = sanitize_filename(file.filename, doc_manager.input_dir)

            if not doc_manager.is_supported_file(safe_filename):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported file type. Supported types: {doc_manager.supported_extensions}",
                )

            # Check file size limit (if configured)
            if (
                global_args.max_upload_size is not None
                and global_args.max_upload_size > 0
            ):
                # Safe access to file size (not available in older Starlette versions)
                file_size = getattr(file, "size", None)

                # Pre-flight size check (only if size is available)
                if file_size is not None:
                    if file_size > global_args.max_upload_size:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File too large. Maximum size: {global_args.max_upload_size / 1024 / 1024:.1f}MB, uploaded: {file_size / 1024 / 1024:.1f}MB",
                        )
                else:
                    # If size not known, we'll check during streaming
                    logger.debug(
                        f"File size not available in UploadFile for {safe_filename}, will check during streaming"
                    )

            # Check if filename already exists in doc_status storage
            existing_doc_data = await rag.doc_status.get_doc_by_file_path(safe_filename)
            if existing_doc_data:
                # Get document status and track_id from existing document
                status = existing_doc_data.get("status", "unknown")
                # Use `or ""` to handle both missing key and None value (e.g., legacy rows without track_id)
                existing_track_id = existing_doc_data.get("track_id") or ""
                return InsertResponse(
                    status="duplicated",
                    message=f"File '{safe_filename}' already exists in document storage (Status: {status}).",
                    track_id=existing_track_id,
                )

            file_path = doc_manager.input_dir / safe_filename
            # Check if file already exists in file system
            if file_path.exists():
                return InsertResponse(
                    status="duplicated",
                    message=f"File '{safe_filename}' already exists in the input directory.",
                    track_id="",
                )

            # Async streaming write with size check
            bytes_written = 0
            chunk_size = 1024 * 1024  # 1MB chunks
            needs_cleanup = False

            async with aiofiles.open(file_path, "wb") as out_file:
                while True:
                    # Read chunk from upload stream
                    chunk = await file.read(chunk_size)
                    if not chunk:
                        break

                    # Check size limit during streaming (if not checked before)
                    if (
                        global_args.max_upload_size is not None
                        and global_args.max_upload_size > 0
                    ):
                        bytes_written += len(chunk)
                        if bytes_written > global_args.max_upload_size:
                            needs_cleanup = True
                            break

                    # Write chunk to file
                    await out_file.write(chunk)

            # Cleanup after file is closed
            if needs_cleanup:
                try:
                    file_path.unlink()
                except Exception as cleanup_error:
                    logger.error(
                        f"Error cleaning up oversized file {safe_filename}: {cleanup_error}"
                    )

                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Maximum size: {global_args.max_upload_size / 1024 / 1024:.1f}MB, uploaded: {bytes_written / 1024 / 1024:.1f}MB",
                )

            track_id = generate_track_id("upload")

            # Add to background tasks and get track_id
            background_tasks.add_task(pipeline_index_file, rag, file_path, track_id)

            return InsertResponse(
                status="success",
                message=f"File '{safe_filename}' uploaded successfully. Processing will continue in background.",
                track_id=track_id,
            )

        except HTTPException:
            # Re-raise HTTP exceptions (400, 413, etc.)
            raise
        except Exception as e:
            logger.error(f"Error /documents/upload: {file.filename}: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/text", response_model=InsertResponse, dependencies=[Depends(combined_auth)]
    )
    async def insert_text(
        request: InsertTextRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,  # [jonex] +Request
    ):
        """
        Insert text into the RAG system.

        This endpoint allows you to insert text data into the RAG system for later retrieval
        and use in generating responses.

        Args:
            request (InsertTextRequest): The request body containing the text to be inserted.
            background_tasks: FastAPI BackgroundTasks for async processing

        Returns:
            InsertResponse: A response object containing the status of the operation.

        Raises:
            HTTPException: If an error occurs during text processing (500).
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]

            # Check if file_source already exists in doc_status storage
            if (
                request.file_source
                and request.file_source.strip()
                and request.file_source != "unknown_source"
            ):
                existing_doc_data = await rag.doc_status.get_doc_by_file_path(
                    request.file_source
                )
                if existing_doc_data:
                    # Get document status and track_id from existing document
                    status = existing_doc_data.get("status", "unknown")
                    # Use `or ""` to handle both missing key and None value (e.g., legacy rows without track_id)
                    existing_track_id = existing_doc_data.get("track_id") or ""
                    return InsertResponse(
                        status="duplicated",
                        message=f"File source '{request.file_source}' already exists in document storage (Status: {status}).",
                        track_id=existing_track_id,
                    )

            # Check if content already exists by computing content hash (doc_id)
            sanitized_text = sanitize_text_for_encoding(request.text)
            content_doc_id = compute_mdhash_id(sanitized_text, prefix="doc-")
            existing_doc = await rag.doc_status.get_by_id(content_doc_id)
            if existing_doc:
                # Content already exists, return duplicated with existing track_id
                status = existing_doc.get("status", "unknown")
                existing_track_id = existing_doc.get("track_id") or ""
                return InsertResponse(
                    status="duplicated",
                    message=f"Identical content already exists in document storage (doc_id: {content_doc_id}, Status: {status}).",
                    track_id=existing_track_id,
                )

            # Generate track_id for text insertion
            track_id = generate_track_id("insert")

            background_tasks.add_task(
                pipeline_index_texts,
                rag,
                [request.text],
                file_sources=[request.file_source],
                track_id=track_id,
            )

            return InsertResponse(
                status="success",
                message="Text successfully received. Processing will continue in background.",
                track_id=track_id,
            )
        except Exception as e:
            logger.error(f"Error /documents/text: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    # ── [jonex] POST /documents/custom-chunks ──────────────────────

    @router.post(
        "/custom-chunks",
        response_model=InsertResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def insert_custom_chunks(
        request: InsertCustomChunksRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,  # [jonex]
    ):
        """Insert pre-chunked text with positional metadata.

        Each chunk is stored directly without further splitting.
        Optional ``page_idx``, ``line_start``, ``line_end`` on each
        chunk are preserved in storage and surfaced via
        ``GET /documents/{doc_id}/chunks`` and ``/export``.
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]

            # Build list[dict] for ainsert_custom_chunks
            chunk_dicts: list[dict] = []
            for c in request.chunks:
                d = {"content": c.content}
                if c.page_idx is not None:
                    d["page_idx"] = c.page_idx
                if c.line_start is not None:
                    d["line_start"] = c.line_start
                if c.line_end is not None:
                    d["line_end"] = c.line_end
                chunk_dicts.append(d)

            track_id = generate_track_id("insert")

            background_tasks.add_task(
                rag.ainsert_custom_chunks,
                request.full_text,
                chunk_dicts,
                doc_id=request.doc_id,
                file_path=request.file_path,
            )

            return InsertResponse(
                status="success",
                message=(
                    f"{len(chunk_dicts)} chunks inserted. "
                    "Processing will continue in background."
                ),
                track_id=track_id,
            )
        except Exception as e:
            logger.error(f"Error /documents/custom-chunks: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/texts",
        response_model=InsertResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def insert_texts(
        request: InsertTextsRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,  # [jonex] +Request
    ):
        """
        Insert multiple texts into the RAG system.

        This endpoint allows you to insert multiple text entries into the RAG system
        in a single request.

        Args:
            request (InsertTextsRequest): The request body containing the list of texts.
            background_tasks: FastAPI BackgroundTasks for async processing

        Returns:
            InsertResponse: A response object containing the status of the operation.

        Raises:
            HTTPException: If an error occurs during text processing (500).
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]

            # Check if any file_sources already exist in doc_status storage
            if request.file_sources:
                for file_source in request.file_sources:
                    if (
                        file_source
                        and file_source.strip()
                        and file_source != "unknown_source"
                    ):
                        existing_doc_data = await rag.doc_status.get_doc_by_file_path(
                            file_source
                        )
                        if existing_doc_data:
                            # Get document status and track_id from existing document
                            status = existing_doc_data.get("status", "unknown")
                            # Use `or ""` to handle both missing key and None value (e.g., legacy rows without track_id)
                            existing_track_id = existing_doc_data.get("track_id") or ""
                            return InsertResponse(
                                status="duplicated",
                                message=f"File source '{file_source}' already exists in document storage (Status: {status}).",
                                track_id=existing_track_id,
                            )

            # Check if any content already exists by computing content hash (doc_id)
            for text in request.texts:
                sanitized_text = sanitize_text_for_encoding(text)
                content_doc_id = compute_mdhash_id(sanitized_text, prefix="doc-")
                existing_doc = await rag.doc_status.get_by_id(content_doc_id)
                if existing_doc:
                    # Content already exists, return duplicated with existing track_id
                    status = existing_doc.get("status", "unknown")
                    existing_track_id = existing_doc.get("track_id") or ""
                    return InsertResponse(
                        status="duplicated",
                        message=f"Identical content already exists in document storage (doc_id: {content_doc_id}, Status: {status}).",
                        track_id=existing_track_id,
                    )

            # Generate track_id for texts insertion
            track_id = generate_track_id("insert")

            background_tasks.add_task(
                pipeline_index_texts,
                rag,
                request.texts,
                file_sources=request.file_sources,
                track_id=track_id,
            )

            return InsertResponse(
                status="success",
                message="Texts successfully received. Processing will continue in background.",
                track_id=track_id,
            )
        except Exception as e:
            logger.error(f"Error /documents/texts: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete(
        "", response_model=ClearDocumentsResponse, dependencies=[Depends(combined_auth)]
    )
    async def clear_documents(http_request: Request):  # [jonex] +Request
        """
        Clear all documents from the RAG system.

        This endpoint deletes all documents, entities, relationships, and files from the system.
        It uses the storage drop methods to properly clean up all data and removes all files
        from the input directory.

        Returns:
            ClearDocumentsResponse: A response object containing the status and message.
                - status="success":           All documents and files were successfully cleared.
                - status="partial_success":   Document clear job exit with some errors.
                - status="busy":              Operation could not be completed because the pipeline is busy.
                - status="fail":              All storage drop operations failed, with message
                - message: Detailed information about the operation results, including counts
                  of deleted files and any errors encountered.

        Raises:
            HTTPException: Raised when a serious error occurs during the clearing process,
                          with status code 500 and error details in the detail field.
        """
        from lightrag.kg.shared_storage import (
            get_namespace_data,
            get_namespace_lock,
        )

        # [jonex] Resolve workspace-specific LightRAG instance
        rag = await _resolve_rag(http_request)

        # Get pipeline status and lock
        pipeline_status = await get_namespace_data(
            "pipeline_status", workspace=rag.workspace
        )
        pipeline_status_lock = get_namespace_lock(
            "pipeline_status", workspace=rag.workspace
        )

        # Check and set status with lock
        async with pipeline_status_lock:
            if pipeline_status.get("busy", False):
                return ClearDocumentsResponse(
                    status="busy",
                    message="Cannot clear documents while pipeline is busy",
                )
            # Set busy to true
            pipeline_status.update(
                {
                    "busy": True,
                    "job_name": "Clearing Documents",
                    "job_start": datetime.now().isoformat(),
                    "docs": 0,
                    "batchs": 0,
                    "cur_batch": 0,
                    "request_pending": False,  # Clear any previous request
                    "latest_message": "Starting document clearing process",
                }
            )
            # Cleaning history_messages without breaking it as a shared list object
            del pipeline_status["history_messages"][:]
            pipeline_status["history_messages"].append(
                "Starting document clearing process"
            )

        try:
            # Use drop method to clear all data
            drop_tasks = []
            storages = [
                rag.text_chunks,
                rag.full_docs,
                rag.full_entities,
                rag.full_relations,
                rag.entity_chunks,
                rag.relation_chunks,
                rag.entities_vdb,
                rag.relationships_vdb,
                rag.chunks_vdb,
                rag.chunk_entity_relation_graph,
                rag.doc_status,
            ]

            # Log storage drop start
            if "history_messages" in pipeline_status:
                pipeline_status["history_messages"].append(
                    "Starting to drop storage components"
                )

            for storage in storages:
                if storage is not None:
                    drop_tasks.append(storage.drop())

            # Wait for all drop tasks to complete
            drop_results = await asyncio.gather(*drop_tasks, return_exceptions=True)

            # Check for errors and log results
            errors = []
            storage_success_count = 0
            storage_error_count = 0

            for i, result in enumerate(drop_results):
                storage_name = storages[i].__class__.__name__
                if isinstance(result, Exception):
                    error_msg = f"Error dropping {storage_name}: {str(result)}"
                    errors.append(error_msg)
                    logger.error(error_msg)
                    storage_error_count += 1
                else:
                    namespace = storages[i].namespace
                    workspace = storages[i].workspace
                    logger.info(
                        f"Successfully dropped {storage_name}: {workspace}/{namespace}"
                    )
                    storage_success_count += 1

            # Log storage drop results
            if "history_messages" in pipeline_status:
                if storage_error_count > 0:
                    pipeline_status["history_messages"].append(
                        f"Dropped {storage_success_count} storage components with {storage_error_count} errors"
                    )
                else:
                    pipeline_status["history_messages"].append(
                        f"Successfully dropped all {storage_success_count} storage components"
                    )

            # If all storage operations failed, return error status and don't proceed with file deletion
            if storage_success_count == 0 and storage_error_count > 0:
                error_message = "All storage drop operations failed. Aborting document clearing process."
                logger.error(error_message)
                if "history_messages" in pipeline_status:
                    pipeline_status["history_messages"].append(error_message)
                return ClearDocumentsResponse(status="fail", message=error_message)

            # Log file deletion start
            if "history_messages" in pipeline_status:
                pipeline_status["history_messages"].append(
                    "Starting to delete files in input directory"
                )

            # Delete only files in the current directory, preserve files in subdirectories
            deleted_files_count = 0
            file_errors_count = 0

            for file_path in doc_manager.input_dir.glob("*"):
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        deleted_files_count += 1
                    except Exception as e:
                        logger.error(f"Error deleting file {file_path}: {str(e)}")
                        file_errors_count += 1

            # Log file deletion results
            if "history_messages" in pipeline_status:
                if file_errors_count > 0:
                    pipeline_status["history_messages"].append(
                        f"Deleted {deleted_files_count} files with {file_errors_count} errors"
                    )
                    errors.append(f"Failed to delete {file_errors_count} files")
                else:
                    pipeline_status["history_messages"].append(
                        f"Successfully deleted {deleted_files_count} files"
                    )

            # Prepare final result message
            final_message = ""
            if errors:
                final_message = f"Cleared documents with some errors. Deleted {deleted_files_count} files."
                status = "partial_success"
            else:
                final_message = f"All documents cleared successfully. Deleted {deleted_files_count} files."
                status = "success"

            # Log final result
            if "history_messages" in pipeline_status:
                pipeline_status["history_messages"].append(final_message)

            # Return response based on results
            return ClearDocumentsResponse(status=status, message=final_message)
        except Exception as e:
            error_msg = f"Error clearing documents: {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            if "history_messages" in pipeline_status:
                pipeline_status["history_messages"].append(error_msg)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            # Reset busy status after completion
            async with pipeline_status_lock:
                pipeline_status["busy"] = False
                completion_msg = "Document clearing process completed"
                pipeline_status["latest_message"] = completion_msg
                if "history_messages" in pipeline_status:
                    pipeline_status["history_messages"].append(completion_msg)

    @router.get(
        "/pipeline_status",
        dependencies=[Depends(combined_auth)],
        response_model=PipelineStatusResponse,
    )
    async def get_pipeline_status(http_request: Request) -> PipelineStatusResponse:  # [jonex] +Request
        """
        Get the current status of the document indexing pipeline.

        This endpoint returns information about the current state of the document processing pipeline,
        including the processing status, progress information, and history messages.

        Returns:
            PipelineStatusResponse: A response object containing:
                - autoscanned (bool): Whether auto-scan has started
                - busy (bool): Whether the pipeline is currently busy
                - job_name (str): Current job name (e.g., indexing files/indexing texts)
                - job_start (str, optional): Job start time as ISO format string
                - docs (int): Total number of documents to be indexed
                - batchs (int): Number of batches for processing documents
                - cur_batch (int): Current processing batch
                - request_pending (bool): Flag for pending request for processing
                - latest_message (str): Latest message from pipeline processing
                - history_messages (List[str], optional): List of history messages (limited to latest 1000 entries,
                  with truncation message if more than 1000 messages exist)

        Raises:
            HTTPException: If an error occurs while retrieving pipeline status (500)
        """
        try:
            from lightrag.kg.shared_storage import (
                get_namespace_data,
                get_namespace_lock,
                get_all_update_flags_status,
            )

            rag = await _resolve_rag(http_request)  # [jonex]

            pipeline_status = await get_namespace_data(
                "pipeline_status", workspace=rag.workspace
            )
            pipeline_status_lock = get_namespace_lock(
                "pipeline_status", workspace=rag.workspace
            )

            # Get update flags status for all namespaces
            update_status = await get_all_update_flags_status(workspace=rag.workspace)

            # Convert MutableBoolean objects to regular boolean values
            processed_update_status = {}
            for namespace, flags in update_status.items():
                processed_flags = []
                for flag in flags:
                    # Handle both multiprocess and single process cases
                    if hasattr(flag, "value"):
                        processed_flags.append(bool(flag.value))
                    else:
                        processed_flags.append(bool(flag))
                processed_update_status[namespace] = processed_flags

            async with pipeline_status_lock:
                # Convert to regular dict if it's a Manager.dict
                status_dict = dict(pipeline_status)

            # Add processed update_status to the status dictionary
            status_dict["update_status"] = processed_update_status

            # Convert history_messages to a regular list if it's a Manager.list
            # and limit to latest 1000 entries with truncation message if needed
            if "history_messages" in status_dict:
                history_list = list(status_dict["history_messages"])
                total_count = len(history_list)

                if total_count > 1000:
                    # Calculate truncated message count
                    truncated_count = total_count - 1000

                    # Take only the latest 1000 messages
                    latest_messages = history_list[-1000:]

                    # Add truncation message at the beginning
                    truncation_message = (
                        f"[Truncated history messages: {truncated_count}/{total_count}]"
                    )
                    status_dict["history_messages"] = [
                        truncation_message
                    ] + latest_messages
                else:
                    # No truncation needed, return all messages
                    status_dict["history_messages"] = history_list

            # Ensure job_start is properly formatted as a string with timezone information
            if "job_start" in status_dict and status_dict["job_start"]:
                # Use format_datetime to ensure consistent formatting
                status_dict["job_start"] = format_datetime(status_dict["job_start"])

            return PipelineStatusResponse(**status_dict)
        except Exception as e:
            logger.error(f"Error getting pipeline status: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    # TODO: Deprecated, use /documents/paginated instead
    @router.get(
        "", response_model=DocsStatusesResponse, dependencies=[Depends(combined_auth)]
    )
    async def documents(http_request: Request) -> DocsStatusesResponse:  # [jonex] +Request
        """
        Get the status of all documents in the system. This endpoint is deprecated; use /documents/paginated instead.
        To prevent excessive resource consumption, a maximum of 1,000 records is returned.

        This endpoint retrieves the current status of all documents, grouped by their
        processing status (PENDING, PROCESSING, PREPROCESSED, PROCESSED, FAILED). The results are
        limited to 1000 total documents with fair distribution across all statuses.

        Returns:
            DocsStatusesResponse: A response object containing a dictionary where keys are
                                DocStatus values and values are lists of DocStatusResponse
                                objects representing documents in each status category.
                                Maximum 1000 documents total will be returned.

        Raises:
            HTTPException: If an error occurs while retrieving document statuses (500).
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]

            statuses = (
                DocStatus.PENDING,
                DocStatus.PROCESSING,
                DocStatus.PREPROCESSED,
                DocStatus.PROCESSED,
                DocStatus.FAILED,
            )

            tasks = [rag.get_docs_by_status(status) for status in statuses]
            results: List[Dict[str, DocProcessingStatus]] = await asyncio.gather(*tasks)

            response = DocsStatusesResponse()
            total_documents = 0
            max_documents = 1000

            # Convert results to lists for easier processing
            status_documents = []
            for idx, result in enumerate(results):
                status = statuses[idx]
                docs_list = []
                for doc_id, doc_status in result.items():
                    docs_list.append((doc_id, doc_status))
                status_documents.append((status, docs_list))

            # Fair distribution: round-robin across statuses
            status_indices = [0] * len(
                status_documents
            )  # Track current index for each status
            current_status_idx = 0

            while total_documents < max_documents:
                # Check if we have any documents left to process
                has_remaining = False
                for status_idx, (status, docs_list) in enumerate(status_documents):
                    if status_indices[status_idx] < len(docs_list):
                        has_remaining = True
                        break

                if not has_remaining:
                    break

                # Try to get a document from the current status
                status, docs_list = status_documents[current_status_idx]
                current_index = status_indices[current_status_idx]

                if current_index < len(docs_list):
                    doc_id, doc_status = docs_list[current_index]

                    if status not in response.statuses:
                        response.statuses[status] = []

                    response.statuses[status].append(
                        DocStatusResponse(
                            id=doc_id,
                            content_summary=doc_status.content_summary,
                            content_length=doc_status.content_length,
                            status=doc_status.status,
                            created_at=format_datetime(doc_status.created_at),
                            updated_at=format_datetime(doc_status.updated_at),
                            track_id=doc_status.track_id,
                            chunks_count=doc_status.chunks_count,
                            error_msg=doc_status.error_msg,
                            metadata=doc_status.metadata,
                            file_path=normalize_file_path(doc_status.file_path),
                        )
                    )

                    status_indices[current_status_idx] += 1
                    total_documents += 1

                # Move to next status (round-robin)
                current_status_idx = (current_status_idx + 1) % len(status_documents)

            return response
        except Exception as e:
            logger.error(f"Error GET /documents: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    class DeleteDocByIdResponse(BaseModel):
        """Response model for single document deletion operation."""

        status: Literal["deletion_started", "busy", "not_allowed"] = Field(
            description="Status of the deletion operation"
        )
        message: str = Field(description="Message describing the operation result")
        doc_id: str = Field(description="The ID of the document to delete")

    @router.delete(
        "/delete_document",
        response_model=DeleteDocByIdResponse,
        dependencies=[Depends(combined_auth)],
        summary="Delete a document and all its associated data by its ID.",
    )
    async def delete_document(
        delete_request: DeleteDocRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,  # [jonex] +Request
    ) -> DeleteDocByIdResponse:
        """
        Delete documents and all their associated data by their IDs using background processing.

        Deletes specific documents and all their associated data, including their status,
        text chunks, vector embeddings, and any related graph data. When requested,
        cached LLM extraction responses are removed after graph deletion/rebuild completes.
        The deletion process runs in the background to avoid blocking the client connection.

        This operation is irreversible and will interact with the pipeline status.

        Args:
            delete_request (DeleteDocRequest): The request containing the document IDs and deletion options.
            background_tasks: FastAPI BackgroundTasks for async processing

        Returns:
            DeleteDocByIdResponse: The result of the deletion operation.
                - status="deletion_started": The document deletion has been initiated in the background.
                - status="busy": The pipeline is busy with another operation.

        Raises:
            HTTPException:
              - 500: If an unexpected internal error occurs during initialization.
        """
        doc_ids = delete_request.doc_ids

        try:
            from lightrag.kg.shared_storage import (
                get_namespace_data,
                get_namespace_lock,
            )

            rag = await _resolve_rag(http_request)  # [jonex]

            pipeline_status = await get_namespace_data(
                "pipeline_status", workspace=rag.workspace
            )
            pipeline_status_lock = get_namespace_lock(
                "pipeline_status", workspace=rag.workspace
            )

            # Check if pipeline is busy with proper lock
            async with pipeline_status_lock:
                if pipeline_status.get("busy", False):
                    return DeleteDocByIdResponse(
                        status="busy",
                        message="Cannot delete documents while pipeline is busy",
                        doc_id=", ".join(doc_ids),
                    )

            # Add deletion task to background tasks
            # [jonex] R4：从 HTTP 请求头提取计量维度，传入后台删除任务，
            # 使 rebuild 路径的 LLM 调用场景标注为 lightrag_rebuild
            jonex_ctx = {
                "tenant_id": (http_request.headers.get("X-Jonex-Tenant-Id") or "").strip(),
                "kb_id": (http_request.headers.get("X-Jonex-Kb-Id") or "").strip(),
                "doc_id": (http_request.headers.get("X-Jonex-Doc-Id") or "").strip(),
                "trace_id": (http_request.headers.get("X-Jonex-Trace-Id") or "").strip(),
            }
            background_tasks.add_task(
                background_delete_documents,
                rag,
                doc_manager,
                doc_ids,
                delete_request.delete_file,
                delete_request.delete_llm_cache,
                jonex_ctx,  # [jonex] R4
            )

            return DeleteDocByIdResponse(
                status="deletion_started",
                message=f"Document deletion for {len(doc_ids)} documents has been initiated. Processing will continue in background.",
                doc_id=", ".join(doc_ids),
            )

        except Exception as e:
            error_msg = f"Error initiating document deletion for {delete_request.doc_ids}: {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=error_msg)

    @router.post(
        "/clear_cache",
        response_model=ClearCacheResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def clear_cache(request: ClearCacheRequest, http_request: Request):  # [jonex] +Request
        """
        Clear all cache data from the LLM response cache storage.

        This endpoint clears all cached LLM responses regardless of mode.
        The request body is accepted for API compatibility but is ignored.

        Args:
            request (ClearCacheRequest): The request body (ignored for compatibility).

        Returns:
            ClearCacheResponse: A response object containing the status and message.

        Raises:
            HTTPException: If an error occurs during cache clearing (500).
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]
            # Call the aclear_cache method (no modes parameter)
            await rag.aclear_cache()

            # Prepare success message
            message = "Successfully cleared all cache"

            return ClearCacheResponse(status="success", message=message)
        except Exception as e:
            logger.error(f"Error clearing cache: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete(
        "/delete_entity",
        response_model=DeletionResult,
        dependencies=[Depends(combined_auth)],
    )
    async def delete_entity(request: DeleteEntityRequest, http_request: Request):  # [jonex] +Request
        """
        Delete an entity and all its relationships from the knowledge graph.

        Args:
            request (DeleteEntityRequest): The request body containing the entity name.

        Returns:
            DeletionResult: An object containing the outcome of the deletion process.

        Raises:
            HTTPException: If the entity is not found (404) or an error occurs (500).
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]
            result = await rag.adelete_by_entity(entity_name=request.entity_name)
            if result.status == "not_found":
                raise HTTPException(status_code=404, detail=result.message)
            if result.status == "fail":
                raise HTTPException(status_code=500, detail=result.message)
            # Set doc_id to empty string since this is an entity operation, not document
            result.doc_id = ""
            return result
        except HTTPException:
            raise
        except Exception as e:
            error_msg = f"Error deleting entity '{request.entity_name}': {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=error_msg)

    @router.delete(
        "/delete_relation",
        response_model=DeletionResult,
        dependencies=[Depends(combined_auth)],
    )
    async def delete_relation(request: DeleteRelationRequest, http_request: Request):  # [jonex] +Request
        """
        Delete a relationship between two entities from the knowledge graph.

        Args:
            request (DeleteRelationRequest): The request body containing the source and target entity names.

        Returns:
            DeletionResult: An object containing the outcome of the deletion process.

        Raises:
            HTTPException: If the relation is not found (404) or an error occurs (500).
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]
            result = await rag.adelete_by_relation(
                source_entity=request.source_entity,
                target_entity=request.target_entity,
            )
            if result.status == "not_found":
                raise HTTPException(status_code=404, detail=result.message)
            if result.status == "fail":
                raise HTTPException(status_code=500, detail=result.message)
            # Set doc_id to empty string since this is a relation operation, not document
            result.doc_id = ""
            return result
        except HTTPException:
            raise
        except Exception as e:
            error_msg = f"Error deleting relation from '{request.source_entity}' to '{request.target_entity}': {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=error_msg)

    @router.get(
        "/track_status/{track_id}",
        response_model=TrackStatusResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def get_track_status(track_id: str, http_request: Request) -> TrackStatusResponse:  # [jonex] +Request
        """
        Get the processing status of documents by tracking ID.

        This endpoint retrieves all documents associated with a specific tracking ID,
        allowing users to monitor the processing progress of their uploaded files or inserted texts.

        Args:
            track_id (str): The tracking ID returned from upload, text, or texts endpoints

        Returns:
            TrackStatusResponse: A response object containing:
                - track_id: The tracking ID
                - documents: List of documents associated with this track_id
                - total_count: Total number of documents for this track_id

        Raises:
            HTTPException: If track_id is invalid (400) or an error occurs (500).
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]

            # Validate track_id
            if not track_id or not track_id.strip():
                raise HTTPException(status_code=400, detail="Track ID cannot be empty")

            track_id = track_id.strip()

            # Get documents by track_id
            docs_by_track_id = await rag.aget_docs_by_track_id(track_id)

            # Convert to response format
            documents = []
            status_summary = {}

            for doc_id, doc_status in docs_by_track_id.items():
                documents.append(
                    DocStatusResponse(
                        id=doc_id,
                        content_summary=doc_status.content_summary,
                        content_length=doc_status.content_length,
                        status=doc_status.status,
                        created_at=format_datetime(doc_status.created_at),
                        updated_at=format_datetime(doc_status.updated_at),
                        track_id=doc_status.track_id,
                        chunks_count=doc_status.chunks_count,
                        error_msg=doc_status.error_msg,
                        metadata=doc_status.metadata,
                        file_path=normalize_file_path(doc_status.file_path),
                    )
                )

                # Build status summary
                # Handle both DocStatus enum and string cases for robust deserialization
                status_key = str(doc_status.status)
                status_summary[status_key] = status_summary.get(status_key, 0) + 1

            return TrackStatusResponse(
                track_id=track_id,
                documents=documents,
                total_count=len(documents),
                status_summary=status_summary,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting track status for {track_id}: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/paginated",
        response_model=PaginatedDocsResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def get_documents_paginated(
        request: DocumentsRequest,
        http_request: Request,  # [jonex] +Request
    ) -> PaginatedDocsResponse:
        """
        Get documents with pagination support.

        This endpoint retrieves documents with pagination, filtering, and sorting capabilities.
        It provides better performance for large document collections by loading only the
        requested page of data.

        Args:
            request (DocumentsRequest): The request body containing pagination parameters

        Returns:
            PaginatedDocsResponse: A response object containing:
                - documents: List of documents for the current page
                - pagination: Pagination information (page, total_count, etc.)
                - status_counts: Count of documents by status for all documents

        Raises:
            HTTPException: If an error occurs while retrieving documents (500).
        """
        rag = await _resolve_rag(http_request)  # [jonex]

        trace_id = uuid4().hex[:8]
        request_start = time.perf_counter()
        status_filter_value = (
            request.status_filter.value if request.status_filter is not None else None
        )

        performance_timing_log(
            "[documents/paginated][%s] Request start workspace=%s status_filter=%s page=%s page_size=%s sort_field=%s sort_direction=%s",
            trace_id,
            rag.workspace,
            status_filter_value,
            request.page,
            request.page_size,
            request.sort_field,
            request.sort_direction,
        )

        try:

            async def _timed_call(operation_name: str, operation):
                operation_start = time.perf_counter()
                performance_timing_log(
                    "[documents/paginated][%s] %s started",
                    trace_id,
                    operation_name,
                )
                try:
                    result = await operation
                except Exception:
                    elapsed = time.perf_counter() - operation_start
                    performance_timing_log(
                        "[documents/paginated][%s] %s failed after %.4fs",
                        trace_id,
                        operation_name,
                        elapsed,
                    )
                    raise

                elapsed = time.perf_counter() - operation_start
                performance_timing_log(
                    "[documents/paginated][%s] %s completed in %.4fs",
                    trace_id,
                    operation_name,
                    elapsed,
                )
                return result

            query_task_create_start = time.perf_counter()
            docs_task = asyncio.create_task(
                _timed_call(
                    "get_docs_paginated",
                    rag.doc_status.get_docs_paginated(
                        status_filter=request.status_filter,
                        page=request.page,
                        page_size=request.page_size,
                        sort_field=request.sort_field,
                        sort_direction=request.sort_direction,
                        doc_id=request.doc_id,
                        file_path=request.file_path,
                    ),
                )
            )
            status_counts_task = asyncio.create_task(
                _timed_call(
                    "get_all_status_counts",
                    rag.doc_status.get_all_status_counts(),
                )
            )
            query_task_create_elapsed = time.perf_counter() - query_task_create_start
            performance_timing_log(
                "[documents/paginated][%s] Query tasks created in %.4fs",
                trace_id,
                query_task_create_elapsed,
            )

            query_await_start = time.perf_counter()
            (documents_with_ids, total_count), status_counts = await asyncio.gather(
                docs_task, status_counts_task
            )
            query_await_elapsed = time.perf_counter() - query_await_start
            performance_timing_log(
                "[documents/paginated][%s] Query tasks awaited in %.4fs",
                trace_id,
                query_await_elapsed,
            )

            # Convert documents to response format
            response_assembly_start = time.perf_counter()
            doc_responses = []
            for doc_id, doc in documents_with_ids:
                doc_responses.append(
                    DocStatusResponse(
                        id=doc_id,
                        content_summary=doc.content_summary,
                        content_length=doc.content_length,
                        status=doc.status,
                        created_at=format_datetime(doc.created_at),
                        updated_at=format_datetime(doc.updated_at),
                        track_id=doc.track_id,
                        chunks_count=doc.chunks_count,
                        error_msg=doc.error_msg,
                        metadata=doc.metadata,
                        file_path=normalize_file_path(doc.file_path),
                    )
                )

            # Calculate pagination info
            total_pages = (total_count + request.page_size - 1) // request.page_size
            has_next = request.page < total_pages
            has_prev = request.page > 1

            pagination = PaginationInfo(
                page=request.page,
                page_size=request.page_size,
                total_count=total_count,
                total_pages=total_pages,
                has_next=has_next,
                has_prev=has_prev,
            )
            response = PaginatedDocsResponse(
                documents=doc_responses,
                pagination=pagination,
                status_counts=status_counts,
            )
            response_assembly_elapsed = time.perf_counter() - response_assembly_start
            total_elapsed = time.perf_counter() - request_start

            performance_timing_log(
                "[documents/paginated][%s] Response assembled in %.4fs",
                trace_id,
                response_assembly_elapsed,
            )
            performance_timing_log(
                "[documents/paginated][%s] Request completed in %.4fs returned_rows=%s total_count=%s status_count_keys=%s",
                trace_id,
                total_elapsed,
                len(doc_responses),
                total_count,
                sorted(status_counts.keys()),
            )

            return response

        except Exception as e:
            total_elapsed = time.perf_counter() - request_start
            performance_timing_log(
                "[documents/paginated][%s] Request failed after %.4fs",
                trace_id,
                total_elapsed,
            )
            logger.error(f"Error getting paginated documents: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.get(
        "/status_counts",
        response_model=StatusCountsResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def get_document_status_counts(http_request: Request) -> StatusCountsResponse:  # [jonex] +Request
        """
        Get counts of documents by status.

        This endpoint retrieves the count of documents in each processing status
        (PENDING, PROCESSING, PROCESSED, FAILED) for all documents in the system.

        Returns:
            StatusCountsResponse: A response object containing status counts

        Raises:
            HTTPException: If an error occurs while retrieving status counts (500).
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]
            status_counts = await rag.doc_status.get_all_status_counts()
            return StatusCountsResponse(status_counts=status_counts)

        except Exception as e:
            logger.error(f"Error getting document status counts: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/reprocess_failed",
        response_model=ReprocessResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def reprocess_failed_documents(background_tasks: BackgroundTasks, http_request: Request):  # [jonex] +Request
        """
        Reprocess failed and pending documents.

        This endpoint triggers the document processing pipeline which automatically
        picks up and reprocesses documents in the following statuses:
        - FAILED: Documents that failed during previous processing attempts
        - PENDING: Documents waiting to be processed
        - PROCESSING: Documents with abnormally terminated processing (e.g., server crashes)

        This is useful for recovering from server crashes, network errors, LLM service
        outages, or other temporary failures that caused document processing to fail.

        The processing happens in the background and can be monitored by checking the
        pipeline status. The reprocessed documents retain their original track_id from
        initial upload, so use their original track_id to monitor progress.

        Returns:
            ReprocessResponse: Response with status and message.
                track_id is always empty string because reprocessed documents retain
                their original track_id from initial upload.

        Raises:
            HTTPException: If an error occurs while initiating reprocessing (500).
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]

            # Start the reprocessing in the background
            # Note: Reprocessed documents retain their original track_id from initial upload
            background_tasks.add_task(rag.apipeline_process_enqueue_documents)
            logger.info("Reprocessing of failed documents initiated")

            return ReprocessResponse(
                status="reprocessing_started",
                message="Reprocessing of failed documents has been initiated in background. Documents retain their original track_id.",
            )

        except Exception as e:
            logger.error(f"Error initiating reprocessing of failed documents: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/cancel_pipeline",
        response_model=CancelPipelineResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def cancel_pipeline(http_request: Request):  # [jonex] +Request
        """
        Request cancellation of the currently running pipeline.

        This endpoint sets a cancellation flag in the pipeline status. The pipeline will:
        1. Check this flag at key processing points
        2. Stop processing new documents
        3. Cancel all running document processing tasks
        4. Mark all PROCESSING documents as FAILED with reason "User cancelled"

        The cancellation is graceful and ensures data consistency. Documents that have
        completed processing will remain in PROCESSED status.

        Returns:
            CancelPipelineResponse: Response with status and message
                - status="cancellation_requested": Cancellation flag has been set
                - status="not_busy": Pipeline is not currently running

        Raises:
            HTTPException: If an error occurs while setting cancellation flag (500).
        """
        try:
            from lightrag.kg.shared_storage import (
                get_namespace_data,
                get_namespace_lock,
            )

            rag = await _resolve_rag(http_request)  # [jonex]

            pipeline_status = await get_namespace_data(
                "pipeline_status", workspace=rag.workspace
            )
            pipeline_status_lock = get_namespace_lock(
                "pipeline_status", workspace=rag.workspace
            )

            async with pipeline_status_lock:
                if not pipeline_status.get("busy", False):
                    return CancelPipelineResponse(
                        status="not_busy",
                        message="Pipeline is not currently running. No cancellation needed.",
                    )

                # Set cancellation flag
                pipeline_status["cancellation_requested"] = True
                cancel_msg = "Pipeline cancellation requested by user"
                logger.info(cancel_msg)
                pipeline_status["latest_message"] = cancel_msg
                pipeline_status["history_messages"].append(cancel_msg)

            return CancelPipelineResponse(
                status="cancellation_requested",
                message="Pipeline cancellation has been requested. Documents will be marked as FAILED.",
            )

        except Exception as e:
            logger.error(f"Error requesting pipeline cancellation: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    # ── [jonex] 自定义 KG 端点 ────────────────────────────────────────

    class ChunkItem(BaseModel):
        """自定义 KG 插入请求中的单个 chunk。"""

        content: str = Field(description="Chunk 文本内容")
        source_id: str = Field(default="", description="来源标识")
        file_path: Optional[str] = Field(default=None, description="来源文件路径")
        chunk_order_index: Optional[int] = Field(
            default=None, description="Chunk 在文档中的顺序"
        )

    class EntityItem(BaseModel):
        """自定义 KG 插入请求中的单个实体。"""

        entity_name: str = Field(description="实体名称")
        entity_type: Optional[str] = Field(default=None, description="实体类型")
        description: Optional[str] = Field(default=None, description="实体描述")
        source_id: str = Field(default="", description="来源标识")
        file_path: Optional[str] = Field(default=None, description="来源文件路径")

    class RelationItem(BaseModel):
        """自定义 KG 插入请求中的单个关系。"""

        src_id: str = Field(description="源实体名称")
        tgt_id: str = Field(description="目标实体名称")
        description: str = Field(default="", description="关系描述")
        keywords: str = Field(default="", description="关系关键词")
        weight: Optional[float] = Field(default=None, description="关系权重")
        source_id: str = Field(default="", description="来源标识")
        file_path: Optional[str] = Field(default=None, description="来源文件路径")

    class CustomKGRequest(BaseModel):
        """POST /documents/custom-kg 请求模型。"""

        chunks: List[ChunkItem] = Field(default_factory=list, description="文本 chunks")
        entities: List[EntityItem] = Field(default_factory=list, description="实体列表")
        relationships: List[RelationItem] = Field(
            default_factory=list, description="关系列表"
        )
        full_doc_id: Optional[str] = Field(
            default=None, description="关联的文档 ID（可选）"
        )

    @router.post(
        "/custom-kg",
        dependencies=[Depends(combined_auth)],
        summary="直接插入自定义知识图谱（chunks + entities + relationships）",
    )
    async def insert_custom_kg(request: CustomKGRequest, http_request: Request):  # [jonex] +Request
        """
        直接将预先构建的知识图谱插入 RAG 系统。

        与标准文档管线（自动通过 LLM 抽取实体/关系）不同，此端点接受已经结构化的
        图数据：文本 chunks、带类型的 entities、带类型的 relationships。

        数据会写入与管线导入文档相同的 vector / graph 存储，后续查询可同时检索。

        **使用场景：**
        - 本体抽取结果回注
        - 第三方知识图谱集成
        - 对已有图谱数据的批量修正
        """
        try:
            rag = await _resolve_rag(http_request)  # [jonex]

            custom_kg = {
                "chunks": [
                    c.model_dump(exclude_none=True) for c in request.chunks
                ],
                "entities": [
                    e.model_dump(exclude_none=True) for e in request.entities
                ],
                "relationships": [
                    r.model_dump(exclude_none=True) for r in request.relationships
                ],
            }
            await rag.ainsert_custom_kg(
                custom_kg=custom_kg,
                full_doc_id=request.full_doc_id,
            )
            return {
                "status": "success",
                "message": "自定义 KG 插入成功",
            }
        except Exception as e:
            logger.error(f"Error /documents/custom-kg: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    # ── [jonex] Chunk update endpoint ──────────────────────────────────

    class UpdateChunkRequest(BaseModel):
        old_chunk_id: str = Field("", description="要修改的 chunk ID (chunk-xxx)")
        new_content: str = Field(..., description="新文本内容")
        file_source: str = Field("", description="file_source 字符串（保留不变）")
        expected_content_hash: str | None = Field(
            None, description="乐观锁校验值（旧内容 MD5 hexdigest）"
        )
        doc_status_id: str = Field(
            "", description="doc-xxx 格式 ID，作为 old_chunk_id 的回退查找方式"
        )

    class UpdateChunkResponse(BaseModel):
        status: str
        old_chunk_id: str
        new_chunk_id: str
        doc_id: str
        tokens: int
        content_length: int
        vector_updated: bool
        affected_entities: int
        updated_at: str

    @router.get(
        "/chunks",
        dependencies=[Depends(combined_auth)],
        summary="[jonex] 按 doc_id 锚点列出文档所有 chunks",
    )
    async def list_document_chunks(
        doc_id: str = Query(..., description="KB 文档 id，匹配 file_path 的 doc= 锚点"),
        http_request: Request = None,  # [jonex] workspace 解析
    ):  # [jonex]
        """[jonex] 按 ``file_path`` 中的 ``doc=<doc_id>`` 锚点枚举 text_chunks。

        用于「查看文档 Chunk 列表」接口。每个 chunk 携带完整的 file_source
        字符串（含 tstart=/tend= 视频/音频时间轴），下游可解析为时间戳定位。
        """
        try:
            rag = await _resolve_rag(http_request)
            chunks = await rag.text_chunks.get_chunks_by_doc_id(doc_id)
            # 清理 content 里的 <!--yx:HASH--> 标记
            clean_chunks = []
            for c in chunks:
                clean = dict(c)
                clean["content"] = re.sub(
                    r"\s*<!--yx:[0-9a-f]+-->\s*", "", clean.get("content") or "",
                )
                clean_chunks.append(clean)
            return {
                "doc_id": doc_id,
                "total": len(clean_chunks),
                "chunks": clean_chunks,
            }
        except Exception as e:
            logger.error(f"Error GET /documents/chunks?doc_id={doc_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get(
        "/chunks/{chunk_id}",
        dependencies=[Depends(combined_auth)],
        summary="[jonex] 按 chunk_id 直查单个 chunk 内容",
    )
    async def get_chunk_by_id(chunk_id: str, http_request: Request):  # [jonex]
        """[jonex] 按 chunk_id 直接从 text_chunks 读取单个 chunk（workspace 内）。

        用于"查看单个 Chunk 内容"的精确直查，避免拉取整个文档 chunk 列表再过滤。
        chunk_id 为 LightRAG 内容哈希主键（chunk-<md5>）。
        """
        try:
            rag = await _resolve_rag(http_request)
            chunk = await rag.text_chunks.get_by_id(chunk_id)
            if chunk is None:
                raise HTTPException(status_code=404, detail=f"chunk_id 不存在: {chunk_id}")
            return {
                "chunk_id": chunk_id,
                "content": chunk.get("content", ""),
                "full_doc_id": chunk.get("full_doc_id", ""),
                "chunk_order_index": chunk.get("chunk_order_index"),
                "file_path": chunk.get("file_path", ""),
                "tokens": chunk.get("tokens"),
                "page_idx": chunk.get("page_idx"),
                "line_start": chunk.get("line_start"),
                "line_end": chunk.get("line_end"),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error GET /chunks/{chunk_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ⚠ [jonex] 路由次序约束：/{doc_id} 是贪婪单段参数路由，必须注册在
    # 所有具名静态 GET 路由之后，否则会吞掉 /pipeline_status 等同级具名路径。
    # 新增 GET 路由（如 /new_endpoint）须注册在本路由之前。
    @router.get(
        "/{doc_id}",
        dependencies=[Depends(combined_auth)],
        summary="[jonex] 按 LightRAG 内部 doc-hash id 直查单个文档状态",
    )
    async def get_document_by_id(doc_id: str, http_request: Request):  # [jonex] P0-1
        """[jonex] 按 LightRAG 内部 doc-hash id（doc-<md5>）查询单个文档处理状态。

        用于 pipeline 轮询层 dup-failed 三态判定：
        原件 processed → 良性成功；原件 pending/processing → 继续等待；
        原件 failed → 硬失败。
        """
        try:
            rag = await _resolve_rag(http_request)
            doc = await rag.doc_status.get_by_id(doc_id)
            if doc is None:
                raise HTTPException(status_code=404, detail=f"doc_id 不存在: {doc_id}")
            return {
                "id": doc_id,
                "status": doc.get("status", ""),
                "content_summary": doc.get("content_summary", ""),
                "content_length": doc.get("content_length", 0),
                "created_at": doc.get("created_at", ""),
                "updated_at": doc.get("updated_at", ""),
                "track_id": doc.get("track_id"),
                "chunks_count": doc.get("chunks_count"),
                "error_msg": doc.get("error_msg"),
                "metadata": doc.get("metadata"),
                "file_path": doc.get("file_path", ""),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error GET /documents/{doc_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/chunks/update",
        response_model=UpdateChunkResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def update_chunk(
        request: UpdateChunkRequest,
        http_request: Request,  # [jonex] workspace isolation
    ):
        """[jonex] 更新单个 chunk 文本内容。

        事务性操作：upsert 新 chunk → delete 旧 chunk → 重写实体/关系引用。
        变更隔离在 workspace 内（通过 LIGHTRAG-WORKSPACE header）。

        与 raganything/service/chunk_repository.py 等效，但运行在服务端。
        """
        from lightrag.constants import GRAPH_FIELD_SEP
        from lightrag.utils import compute_mdhash_id

        MAX_CHUNK_TOKENS = 8000

        try:
            rag = await _resolve_rag(http_request)
            old_chunk_id = request.old_chunk_id
            new_content = request.new_content

            # ── 0. Fallback: resolve chunk_id from doc_status_id ──
            if (not old_chunk_id or not old_chunk_id.startswith("chunk-")) and request.doc_status_id:
                doc_status = await rag.doc_status.get_by_id(request.doc_status_id)
                if doc_status is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"doc_status_id 不存在: {request.doc_status_id}",
                    )
                content = doc_status.get("content_summary", "")
                if content:
                    old_chunk_id = compute_mdhash_id(content, prefix="chunk-")
                else:
                    raise HTTPException(
                        status_code=404,
                        detail="无法从 doc_status 解析 chunk_id（content_summary 为空）",
                    )

            if not old_chunk_id:
                raise HTTPException(
                    status_code=400,
                    detail="old_chunk_id 和 doc_status_id 至少提供一个",
                )

            # ── 1. Validate new content ──
            if not new_content or not new_content.strip():
                raise HTTPException(status_code=400, detail="new_content 不能为空")

            tokens = len(rag.tokenizer.encode(new_content))
            if tokens > MAX_CHUNK_TOKENS:
                raise HTTPException(
                    status_code=400,
                    detail=f"new_content 超过 {MAX_CHUNK_TOKENS} token 上限（当前 {tokens}）",
                )

            # ── 2. Look up old chunk ──
            old = await rag.text_chunks.get_by_id(old_chunk_id)
            if old is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"chunk_id 不存在: {old_chunk_id}",
                )

            # ── 3. Optimistic lock check (optional) ──
            if request.expected_content_hash:
                import hashlib

                actual_hash = hashlib.md5(
                    old.get("content", "").encode("utf-8")
                ).hexdigest()
                if actual_hash != request.expected_content_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="乐观锁 hash 不匹配（内容已被他人修改）",
                    )

            # ── 4. Compute new chunk ID and check for collision ──
            new_chunk_id = compute_mdhash_id(new_content, prefix="chunk-")
            if new_chunk_id != old_chunk_id:
                existing = await rag.text_chunks.get_by_id(new_chunk_id)
                if existing is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="新内容与另一文档的 chunk 完全相同（跨文档碰撞）",
                    )

            doc_id = old.get("full_doc_id", "")
            chunk_order_index = old.get("chunk_order_index", 0)

            # ── 5. Build new chunk entry (preserve positional metadata) ──
            new_chunk = {
                "content": new_content,
                "full_doc_id": doc_id,
                "tokens": tokens,
                "chunk_order_index": chunk_order_index,
                "file_path": old.get("file_path", request.file_source),
            }
            for k in ("page_idx", "line_start", "line_end"):
                if k in old:
                    new_chunk[k] = old[k]

            # ── 6. Vectorize new content ──
            vector_updated = True
            new_vector = None
            try:
                # Use rag's embedding_func (same as other document endpoints)
                if rag.embedding_func is not None:
                    new_vector = await rag.embedding_func([new_content])
                elif hasattr(rag.chunks_vdb, "embedding_func") and rag.chunks_vdb.embedding_func is not None:
                    new_vector = await rag.chunks_vdb.embedding_func([new_content])
                else:
                    logger.warning(
                        "update_chunk: no embedding_func available, skipping vector update"
                    )
                    vector_updated = False
            except Exception as e:
                logger.error(f"update_chunk: vectorize failed for {new_chunk_id}: {e}")
                vector_updated = False

            # ── 7. Write new → delete old (transactional) ──
            await rag.text_chunks.upsert({new_chunk_id: new_chunk})
            await rag.text_chunks.index_done_callback()

            if vector_updated and new_vector is not None and len(new_vector) > 0:
                vdb_data = {new_chunk_id: {**new_chunk, "vector": new_vector[0]}}
                try:
                    await rag.chunks_vdb.upsert(vdb_data)
                    await rag.chunks_vdb.index_done_callback()
                except Exception as e:
                    logger.error(f"update_chunk: vdb upsert failed: {e}")
                    vector_updated = False

            if vector_updated:
                # Full success — delete old data
                await rag.text_chunks.delete([old_chunk_id])
                await rag.text_chunks.index_done_callback()
                await rag.chunks_vdb.delete([old_chunk_id])
                await rag.chunks_vdb.index_done_callback()
            else:
                # vdb failed — rollback new text_chunks, keep old data intact
                await rag.text_chunks.delete([new_chunk_id])
                await rag.text_chunks.index_done_callback()

            # ── 8. Rewrite entity/relation chunk references ──
            affected = 0
            if vector_updated and new_chunk_id != old_chunk_id:
                affected = await _rewrite_chunk_refs(
                    rag, old_chunk_id, new_chunk_id, doc_id,
                )

            return {
                "status": "success",
                "old_chunk_id": old_chunk_id,
                "new_chunk_id": new_chunk_id,
                "doc_id": doc_id,
                "tokens": tokens,
                "content_length": len(new_content),
                "vector_updated": vector_updated,
                "affected_entities": affected,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error /documents/chunks/update: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    # ── [jonex] §table-grid-v2 O7: 孤儿 chunk 强制清理 ──────────────────
    # reparse 删除未收敛的残留 chunk 在 doc_status 中无记录（doc 级删除
    # 「Document xxx not found」直接返回，text_chunks/向量永不清理）。
    # 本端点按 chunk_id 直删三层存储，不依赖 doc_status。

    @router.delete(
        "/chunks/{chunk_id}",
        dependencies=[Depends(combined_auth)],
        summary="[jonex] O7 按 chunk_id 强制清理孤儿 chunk（不依赖 doc_status）",
    )
    async def delete_orphan_chunk(
        chunk_id: str,
        http_request: Request = None,  # [jonex] workspace 解析
    ):
        rag = await _resolve_rag(http_request)
        deleted: dict[str, bool] = {
            "text_chunks": False, "vector": False, "full_docs": False,
        }
        for label, storage in (
            ("text_chunks", rag.text_chunks),
            ("vector", rag.chunks_vdb),
            ("full_docs", rag.full_docs),
        ):
            try:
                await storage.delete([chunk_id])
                deleted[label] = True
            except Exception as e:  # noqa: BLE001 — 分层清理尽力而为
                logger.warning(
                    "[jonex] O7 orphan chunk %s delete %s failed: %s",
                    chunk_id, label, e,
                )
        return {
            "status": "success",
            "chunk_id": chunk_id,
            "deleted": deleted,
        }

    return router


async def _rewrite_chunk_refs(
    rag,
    old_chunk_id: str,
    new_chunk_id: str,
    doc_id: str,
) -> int:
    """Rewrite entity_chunks / relation_chunks / doc_status references.

    LightRAG stores source_id as a GRAPH_FIELD_SEP ("<SEP>") delimited
    string (e.g. "chunk-1<SEP>chunk-2<SEP>chunk-3") because entities
    are extracted from multiple chunks. We split→replace→join rather
    than doing a simple == comparison.
    """
    from lightrag.constants import GRAPH_FIELD_SEP

    count = 0

    # ── entity_chunks ──
    # _data is a dict for JSON storage, but a numpy array for Milvus/PG.
    # numpy array truthiness check causes ValueError — guard against it.
    _raw = getattr(rag.entity_chunks, "_data", None)
    try:
        import numpy as _np
        if _raw is None or isinstance(_raw, _np.ndarray):
            _raw = {}
    except ImportError:
        if _raw is None:
            _raw = {}
    entity_data = _raw
    updated_entities = {}
    for ek, ev in entity_data.items():
        if not isinstance(ev, dict) or not ev.get("source_id"):
            continue
        source_ids = ev["source_id"].split(GRAPH_FIELD_SEP)
        if old_chunk_id in source_ids:
            new_ids = [
                new_chunk_id if cid == old_chunk_id else cid
                for cid in source_ids
            ]
            ev["source_id"] = GRAPH_FIELD_SEP.join(new_ids)
            updated_entities[ek] = ev
            count += 1
    if updated_entities:
        await rag.entity_chunks.upsert(updated_entities)
        await rag.entity_chunks.index_done_callback()

    # ── relation_chunks ──
    # Same numpy guard as entity_chunks above
    _raw_rel = getattr(rag.relation_chunks, "_data", None)
    try:
        import numpy as _np
        if _raw_rel is None or isinstance(_raw_rel, _np.ndarray):
            _raw_rel = {}
    except ImportError:
        if _raw_rel is None:
            _raw_rel = {}
    relation_data = _raw_rel
    updated_relations = {}
    for rk, rv in relation_data.items():
        if not isinstance(rv, dict) or not rv.get("source_id"):
            continue
        source_ids = rv["source_id"].split(GRAPH_FIELD_SEP)
        if old_chunk_id in source_ids:
            new_ids = [
                new_chunk_id if cid == old_chunk_id else cid
                for cid in source_ids
            ]
            rv["source_id"] = GRAPH_FIELD_SEP.join(new_ids)
            updated_relations[rk] = rv
            count += 1
    if updated_relations:
        await rag.relation_chunks.upsert(updated_relations)
        await rag.relation_chunks.index_done_callback()

    # ── doc_status.chunks_list ──
    doc_status = await rag.doc_status.get_by_id(doc_id)
    if doc_status and isinstance(doc_status, dict):
        chunks_list = doc_status.get("chunks_list", [])
        if old_chunk_id in chunks_list:
            new_list = [
                new_chunk_id if cid == old_chunk_id else cid
                for cid in chunks_list
            ]
            doc_status["chunks_list"] = new_list
            await rag.doc_status.upsert({doc_id: doc_status})
            await rag.doc_status.index_done_callback()
            count += 1

    return count
