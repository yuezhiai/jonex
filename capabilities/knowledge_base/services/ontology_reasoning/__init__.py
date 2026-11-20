"""本体推理（公理 / 规则）服务包。

与 `services/reasoning_trace.py`（推理链采集器）区分：本包负责公理与规则的
存储、校验、导入导出和执行，不用来记录检索轨迹。
"""

from .contract import (
    AXIOM_KINDS,
    CONTRACT_VERSION,
    RULE_GRAMMAR_VERSION,
    contract_payload,
)

__all__ = [
    "AXIOM_KINDS",
    "CONTRACT_VERSION",
    "RULE_GRAMMAR_VERSION",
    "contract_payload",
]
