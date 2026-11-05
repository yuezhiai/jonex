# -*- coding: utf-8 -*-
"""指标可执行定义（W3.3）：校验、编译、执行与 OSI 交换。

纯函数优先：本包不访问数据库、不读配置以外的运行时状态；调用方负责取
抽象 schema 行、编译 schema 行与数据源连接。

⚠️ 门面**只**导出 `executable`：`concept_expansion` 会导入本包，
而 `compiler` / `executor` / `osi` 依赖 `nl2sql` 等更重的模块 —— 在这里
提前导入会形成
`concept_expansion → metrics → executor → nl2sql → services.__init__ → concept_expansion`
的循环导入。子模块请直接按路径导入。
"""
from __future__ import annotations

from .executable import (
    ADDITIVITY_VALUES,
    AGGREGATIONS,
    GRANULARITIES,
    METRIC_TYPES,
    normalize_executable,
    validate_executable,
)

__all__ = [
    "ADDITIVITY_VALUES",
    "AGGREGATIONS",
    "GRANULARITIES",
    "METRIC_TYPES",
    "normalize_executable",
    "validate_executable",
]
