#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Knowledge Base references DTOs（原文引用数据结构）。

用于搜索响应（标准 /search、本体 /search/ontology、流式 stream）
以及 POST /documents/references/resolve 请求/响应。
"""

from typing import Any, List, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


class SourceLocation(BaseModel):
    """知识来源在原文档中的精确位置。"""

    type: str = "chunk"  # chunk | char | page | timestamp | table_row
    chunk_index: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    page_no: Optional[int] = None
    time_start: Optional[float] = None
    time_end: Optional[float] = None
    # [jonex] §table-chunking: table row-level positioning
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    table_idx: Optional[int] = None
    text: Optional[str] = None  # 命中片段的原文文本（RAG 链路带 chunk content 时填充）


class SourceReference(BaseModel):
    """结构化原文引用（唯一标识一个文档及其位置集合）。

    D6：以 doc_id 聚合去重；同一文档多个命中片段合并为一个 reference，
    locations[] 收纳多个位置信息。
    """

    doc_id: str
    kb_id: Optional[str] = None
    file_name: str
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    media_type: str = "other"  # text | pdf | audio | video | image | other
    raw_url: Optional[str] = None  # COS 预签名 URL（P0 阶段为 null，P3 COS 改造后可用）
    wiki_path: Optional[str] = None  # [jonex] OpenKB wiki 页路径（如 summaries/<uuid>.md）
    locations: List[SourceLocation] = Field(default_factory=list)


class ParsedRef(BaseModel):
    """流式链路解析出的原始引用片段（前端可原样回传以保留位置信息）。"""

    doc_id: str
    kb_id: Optional[str] = None
    chunk_index: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    page_no: Optional[int] = None
    time_start: Optional[float] = None
    time_end: Optional[float] = None
    # [jonex] §table-chunking
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    table_idx: Optional[int] = None


class ReferenceResolveRequest(BaseModel):
    """引用富化请求（二选一：仅传 doc_ids 文档级，或回传 refs 保留位置信息）。

    D7：流式 gateway 不连 DB，只解析 file_source；前端调此端点富化。
    """

    doc_ids: List[str] = Field(default_factory=list)
    refs: List[ParsedRef] = Field(default_factory=list)


__all__ = [
    "ParsedRef",
    "ReferenceResolveRequest",
    "SourceLocation",
    "SourceReference",
]
