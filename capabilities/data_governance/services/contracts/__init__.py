"""数据合同（ODCS v3.x 子集）校验与归一化。"""
from .validation import API_VERSIONS, normalize_contract, validate_contract

__all__ = ["API_VERSIONS", "normalize_contract", "validate_contract"]
