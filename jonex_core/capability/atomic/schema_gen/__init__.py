"""catalog → TBox 的进程内原子能力。

Vendored from onto-intelligent `src/`（2026-11-02，与 125 容器内版本哈希一致）。
**保持原样**：上游变更需手工 re-vendor，并跑 `tests/unit/test_schema_gen_vendored.py`
钉住行为。`schema_gen_api.generate_from_catalog` 是唯一公开入口。
"""

from .schema_gen_api import generate_from_catalog

__all__ = ["generate_from_catalog"]
