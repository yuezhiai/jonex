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

    evidence_ref: Optional[dict[str, Any]] = None
    type: str = "chunk"  # chunk | char | page | timestamp | table_row | image
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
    # [jonex] §C1-bis §19.5①: 行内切分子段的格区间（0-based 右开）
    cell_start: Optional[int] = None
    cell_end: Optional[int] = None
    # [jonex] §image-refs P0-3: 图片级定位（type="image" 时有效）
    image_idx: Optional[int] = None
    # [jonex] §image-refs P0-3/P2-1: 图片本体的访问 URL（COS 预签名；
    # local 后端为空串，走 gateway 图片 raw 路由；无 aext 的存量 chunk 为 None）
    asset_url: Optional[str] = None
    # [jonex] §image-refs B2: 图片资产扩展名（jpg/png/...，白名单化）。
    # 检索响应透出、历史快照持久化；前端 resolve 回传 image_idx + asset_ext
    # 供后端重新推导对象键并生成 asset_url（asset_url 是预签名、会过期，
    # 不持久化，asset_ext 才是持久标识）。
    asset_ext: Optional[str] = None
    text: Optional[str] = None  # 命中片段的原文文本（RAG 链路带 chunk content 时填充）
    # [jonex] S3（§3.3 #4）：统一分显式字段，消灭影子字段。
    # 口径 `score = w_r*rerank + w_e*evidence_prior + w_s*subject_prior`，见
    # docs/ontology/ontology-rag-scoring-unification-and-routing-plan.md §3.2。
    # `RAG_UNIFIED_SCORE_ENABLED=false`（默认）时恒为 None——旧响应形状不变。
    score: Optional[float] = None
    # 分数语义：`rerank` = 真 reranker 相关性（0~1，可比较可判阈值）；
    # `order_decay` = rerank 缺项时的位次衰减占位（**只排序、不得做阈值判定**）。
    # 前端据此区分「相关性分」与「仅为排序的占位分」，不要再靠猜。
    score_kind: Optional[str] = None


class SourceReference(BaseModel):
    """结构化原文引用（唯一标识一个文档及其位置集合）。

    D6：以 doc_id 聚合去重；同一文档多个命中片段合并为一个 reference，
    locations[] 收纳多个位置信息。
    """

    query_id: Optional[str] = None
    evidence_handle: Optional[str] = None
    bundle_sha: Optional[str] = None
    doc_id: str
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None  # [jonex] 知识库名称（冗余，方便前端展示，无需二次查询）
    file_name: str
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    media_type: str = "other"  # text | pdf | audio | video | image | office | other
    raw_url: Optional[str] = None  # COS 预签名 URL（P0 阶段为 null，P3 COS 改造后可用）
    wiki_path: Optional[str] = None  # [jonex] OpenKB wiki 页路径（如 summaries/<uuid>.md）
    # [jonex] S3（§3.3 #4）：文档级统一分——取**组内 location 最高分**。
    # 统一到 chunk 级打分后，doc 分不再依赖 `locations[0].text[:1024]` 这个
    # 代表文本取舍（已删除的旧引用重排自承的局限）。
    score: Optional[float] = None
    score_kind: Optional[str] = None  # rerank | order_decay，语义同 SourceLocation
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
    # [jonex] §C1-bis §19.5①: 行内切分子段的格区间（0-based 右开）
    cell_start: Optional[int] = None
    cell_end: Optional[int] = None
    # [jonex] §image-refs P0-3: image_idx 供 type="image" 定位；
    # asset_ext 是推导对象键的中间量（不进 SourceLocation，只在回传
    # 请求侧存在——前端把检索返回的 refs 原样回传时保留，否则 resolve
    # 推不出 asset_url）
    image_idx: Optional[int] = None
    asset_ext: Optional[str] = None


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
