#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""跨数据源抽象 schema 的 DTO（Pydantic v1 兼容）。"""
from typing import Any, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field

#: 允许的状态。proposed = LLM 提的候选（不参与检索）；active = 人工确认后生效。
ABSTRACT_STATUSES = {"proposed", "active"}


class AbstractConceptSource(BaseModel):
    """概念在某一个数据源里的落点。"""

    data_source_id: str = Field(..., min_length=1, max_length=64)
    entity_name: str = Field(..., min_length=1, max_length=255)


class AbstractConcept(BaseModel):
    """一个抽象概念：名字 + 别名 + 它在各源里分别叫什么。"""

    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    aliases: list[str] = Field(default_factory=list)
    sources: list[AbstractConceptSource] = Field(default_factory=list)


class AbstractSchemaSaveRequest(BaseModel):
    """保存/确认。`expected_schema_version` 是乐观锁，**必填**。"""

    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    concepts: list[AbstractConcept] = Field(default_factory=list)
    status: str = Field(default="proposed", max_length=32)
    expected_schema_version: Optional[int] = Field(default=None, ge=0)


class AbstractSchemaGenerateRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    #: 已有 active 对齐时，必须显式传 True 才允许覆盖（界面据此弹二次确认）
    force: bool = False


class AbstractSchemaResponse(BaseModel):
    id: str
    knowledge_base_id: str
    schema_version: int = 1
    status: str = "proposed"
    concepts: list[dict[str, Any]] = Field(default_factory=list)
    source_hash: Optional[str] = None
    edited_at: Optional[str] = None
    edited_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        extra = "allow"  # stale_count / dropped_sources 由服务层附加


__all__ = [
    "ABSTRACT_STATUSES",
    "AbstractConcept",
    "AbstractConceptSource",
    "AbstractSchemaGenerateRequest",
    "AbstractSchemaResponse",
    "AbstractSchemaSaveRequest",
]
