"""Ontology query DTOs for kb statistics, instance list, and relation list."""

from typing import Optional

from pydantic import BaseModel, Field


class OntologyStatsRequest(BaseModel):
    """kb 维度统计请求参数。"""

    knowledge_base_id: str
    include_unknown: bool = True  # 类型目录是否包含未归类(unknown)条目；默认含，便于暴露覆盖率
    document_id: Optional[str] = None  # 按来源文档过滤计数；缺省 = KB 级全量


class OntologyGraphRequest(BaseModel):
    """图谱数据请求参数（力导向图渲染）。"""

    knowledge_base_id: str
    limit: int = 500
    entity_types: Optional[list[str]] = None
    document_id: Optional[str] = None  # 按来源文档过滤节点，只保留 doc_ids 含该文档的实体


class OntologyNeighborRequest(BaseModel):
    """实体一跳邻居展开请求参数。"""

    knowledge_base_id: str
    entity_type: str
    canonical_name: str
    limit: int = 50
    document_id: Optional[str] = None  # 按来源文档过滤邻居节点；缺省 = KB 级全量


class OntologyInstanceListRequest(BaseModel):
    """本体实例列表请求参数。"""

    knowledge_base_id: str
    page: int = 1
    page_size: int = 20
    entity_type: Optional[str] = None
    keyword: Optional[str] = None
    include_unknown: bool = True  # 实例列表是否包含 entity_type=unknown（含 P2C 兜底端点）；默认含
    document_id: Optional[str] = None  # 按来源文档过滤


class OntologyRelationListRequest(BaseModel):
    """本体关系列表请求参数。"""

    knowledge_base_id: str
    page: int = 1
    page_size: int = 20
    relation_type: Optional[str] = None
    source_name: Optional[str] = None
    target_name: Optional[str] = None
    source_type: Optional[str] = None
    target_type: Optional[str] = None
    keyword: Optional[str] = None  # 通用关键词模糊搜索（匹配关系类型+源实体名+目标实体名）
    document_id: Optional[str] = None  # 按来源文档过滤（两端任一节点含该 doc 即命中）


class OntologyEntitySearchRequest(BaseModel):
    """本体实例名称搜索请求参数。

    用于在实例/关系表单中，让用户通过模糊搜索快速定位实体实例。
    复用 Neo4j 全文索引 ont_entity_ft（cjk analyzer）实现中文模糊匹配。

    `document_id`（可选）限定候选范围：从**文档结果页**打开表单时带上，只返回
    `doc_ids` 含该文档的实体（与文档本体视图同一判据 `$doc_id IN e.doc_ids`），
    避免在单篇文档里把整个 KB 的实体都列出来选；KB 级表单不带 → 全库候选。
    """

    knowledge_base_id: str = Field(..., min_length=1, description="知识库 ID")
    keyword: str = Field(..., min_length=0, max_length=256, description="搜索关键词")
    limit: int = Field(20, ge=1, le=200, description="返回条数上限")
    document_id: Optional[str] = Field(
        default=None, description="限定候选实体所属文档 ID（文档页表单带；KB 级表单不带）"
    )