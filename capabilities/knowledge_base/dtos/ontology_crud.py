#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""本体实例/关系 CRUD Pydantic DTO（Pydantic v1 兼容）。

提供 6 个请求 DTO：
- CreateOntologyInstanceRequest / UpdateOntologyInstanceRequest / DeleteOntologyInstanceRequest
- CreateOntologyRelationRequest / UpdateOntologyRelationRequest / DeleteOntologyRelationRequest

替代原先 service 层裸 dict 传参，使 Swagger 能正确展示参数结构。
"""

from typing import Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


class CreateOntologyInstanceRequest(BaseModel):
    """创建本体实例请求。

    `document_id` 区分两个入口的语义（**可选**）：
    - 从**文档结果页**建（带 docId）→ 写进节点的 `doc_ids`，该文档的本体视图
      （`GET /ontology/instances?document_id=` 按 `$doc_id IN e.doc_ids` 过滤）就能看到；
    - 从 **KB 级「编译资产」tab** 建（不带）→ `doc_ids=[]`，属于全库实例，不归属任何文档。
    """

    knowledge_base_id: str = Field(..., min_length=1, description="知识库 ID")
    entity_type: str = Field(..., min_length=1, description="实体类型")
    name: str = Field(..., min_length=1, description="实例规范名称")
    aliases: Optional[list[str]] = Field(default=None, description="别名列表")
    description: Optional[str] = Field(default=None, description="实例描述")
    attributes: Optional[dict] = Field(default=None, description="扩展属性")
    document_id: Optional[str] = Field(
        default=None, description="来源文档 ID（从文档页创建时带；写入 doc_ids 关联）"
    )


class UpdateOntologyInstanceRequest(BaseModel):
    """更新本体实例请求。"""

    knowledge_base_id: str = Field(..., min_length=1, description="知识库 ID")
    entity_type: str = Field(..., min_length=1, description="实体类型")
    canonical_name: str = Field(..., min_length=1, description="当前规范名称")
    updates: dict = Field(..., description="待更新字段（name/aliases/description/attributes）")


class DeleteOntologyInstanceRequest(BaseModel):
    """删除本体实例请求。"""

    knowledge_base_id: str = Field(..., min_length=1, description="知识库 ID")
    entity_type: str = Field(..., min_length=1, description="实体类型")
    canonical_name: str = Field(..., min_length=1, description="规范名称")


class CreateOntologyRelationRequest(BaseModel):
    """创建本体关系请求。

    `document_id` 与实例同口径：文档页建的关系写进 `r.doc_ids`（该文档的关系视图按
    `$doc_id IN r.doc_ids` 过滤），KB 级资产页建的写 `[]`。两端自动补的 stub 实体也
    沿用同一个 doc（与抽取侧 merge_relation 一致）。
    """

    knowledge_base_id: str = Field(..., min_length=1, description="知识库 ID")
    source_entity_type: str = Field(..., min_length=1, description="源实体类型")
    source_canonical_name: str = Field(..., min_length=1, description="源实体规范名称")
    relation_type: str = Field(..., min_length=1, description="关系类型")
    target_entity_type: str = Field(..., min_length=1, description="目标实体类型")
    target_canonical_name: str = Field(..., min_length=1, description="目标实体规范名称")
    attributes: Optional[dict] = Field(default=None, description="扩展属性")
    document_id: Optional[str] = Field(
        default=None, description="来源文档 ID（从文档页创建时带；写入 doc_ids 关联）"
    )


class UpdateOntologyRelationRequest(BaseModel):
    """更新本体关系请求。"""

    knowledge_base_id: str = Field(..., min_length=1, description="知识库 ID")
    source_entity_type: str = Field(..., min_length=1, description="源实体类型")
    source_canonical_name: str = Field(..., min_length=1, description="源实体规范名称")
    relation_type: str = Field(..., min_length=1, description="当前关系类型")
    target_entity_type: str = Field(..., min_length=1, description="目标实体类型")
    target_canonical_name: str = Field(..., min_length=1, description="目标实体规范名称")
    updates: dict = Field(..., description="待更新字段（relation_type/attributes）")


class DeleteOntologyRelationRequest(BaseModel):
    """删除本体关系请求。"""

    knowledge_base_id: str = Field(..., min_length=1, description="知识库 ID")
    source_entity_type: str = Field(..., min_length=1, description="源实体类型")
    source_canonical_name: str = Field(..., min_length=1, description="源实体规范名称")
    relation_type: str = Field(..., min_length=1, description="关系类型")
    target_entity_type: str = Field(..., min_length=1, description="目标实体类型")
    target_canonical_name: str = Field(..., min_length=1, description="目标实体规范名称")


__all__ = [
    "CreateOntologyInstanceRequest",
    "UpdateOntologyInstanceRequest",
    "DeleteOntologyInstanceRequest",
    "CreateOntologyRelationRequest",
    "UpdateOntologyRelationRequest",
    "DeleteOntologyRelationRequest",
]
