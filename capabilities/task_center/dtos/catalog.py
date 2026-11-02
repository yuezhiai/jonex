#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""内置步骤清单 DTO（§八）。

平台预置、只读；编排时「这版能用哪些节点」就查这一样。
"""

from typing import Any, Dict, Optional

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import BaseModel, Field
except ImportError:  # pragma: no cover
    from pydantic import BaseModel, Field


class TaskCatalogStepItem(BaseModel):
    """内置步骤清单的一项：步骤的「固有形状」都在这。"""

    id: str = Field(description="主键")
    step_code: str = Field(description="步骤名（编排里 steps[].step_code 引用它）")
    name: str = Field(description="给人看的名字")
    runner_code: str = Field(description="这步交给哪个运行方（runner_definitions.code）")
    max_concurrency: Optional[int] = Field(
        default=None, description="这步最多同时跑几个（运行方并发上限）；不填=不限"
    )
    timeout_seconds: int = Field(default=300, description="默认超时时间（秒），建编排时预填")
    input_schema: Dict[str, Any] = Field(default_factory=dict, description="{ 字段名: FieldSpec }")
    output_schema: Dict[str, Any] = Field(default_factory=dict, description="{ 字段名: FieldSpec }")

    class Config:
        extra = "ignore"


__all__ = ["TaskCatalogStepItem"]
