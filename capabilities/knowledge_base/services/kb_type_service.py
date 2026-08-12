# [jonex] 知识库类型查询 — 公共 helper
"""统一查询 KnowledgeInfo.kb_type（单 KB + 批量）。

替代此前分散在 4 个 service（document / search / ontology / parse_result）
中各写一份的 _get_pipeline_type。原名 pipeline_type，迁移后重命名为 kb_type。
"""
from sqlalchemy import select

from jonex_core.common.database import get_db_session

from ..models.knowledge_info import KnowledgeInfo

DEFAULT_KB_TYPE = "lightrag"
VALID_KB_TYPES = frozenset({"lightrag", "openkb"})


async def get_kb_type(tenant_id: str, kb_id: str) -> str:
    """查询单个 KB 的 kb_type，未命中/空 kb_id → 'lightrag'。"""
    if not kb_id:
        return DEFAULT_KB_TYPE
    async with get_db_session() as session:
        result = await session.execute(
            select(KnowledgeInfo.kb_type).where(
                KnowledgeInfo.tenant_id == tenant_id,
                KnowledgeInfo.id == kb_id,
                KnowledgeInfo.is_deleted == 0,
            )
        )
        row = result.first()
    return row[0] if row else DEFAULT_KB_TYPE


async def get_kb_types(tenant_id: str, kb_ids: list[str]) -> dict[str, str]:
    """批量查询 KB 的 kb_type。未命中默认 'lightrag'。

    WHERE 带 tenant_id（安全性：跨租户 kb_id 查不到行 → 默认 lightrag）。
    """
    if not kb_ids:
        return {}
    async with get_db_session() as session:
        result = await session.execute(
            select(KnowledgeInfo.id, KnowledgeInfo.kb_type).where(
                KnowledgeInfo.tenant_id == tenant_id,
                KnowledgeInfo.id.in_(kb_ids),
                KnowledgeInfo.is_deleted == 0,
            )
        )
        rows = {row[0]: row[1] for row in result.all()}
    return {k: rows.get(k) or DEFAULT_KB_TYPE for k in kb_ids}
