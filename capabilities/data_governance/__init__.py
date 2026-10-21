"""数据治理能力包。

启动脚本（`deploy/start_capability.py`）从本包导入 `DataGovernanceCapability`，
导出缺失会导致 `CAPABILITY_NAME=data_governance` 的容器启动失败。
"""

from .capability import DataGovernanceCapability

__all__ = ["DataGovernanceCapability"]
