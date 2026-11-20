#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""知识库文档占用探针（跨 schema 只读，2026-11-18 加）。

任务定义执行时，`inputs.document` 里的文档必须：
1. 存在、属于当前租户、属于 `inputs.knowledge_id` 指向的知识库、未删除；
2. **尚未被任何在途任务接管**（`task_center_documents` 的 `submitting` / `managed`）。

⚠️ 与 `SpaceRepository` 同一模式：**不 import 知识库服务**，只读两张表。
这里只是**提前拒绝**，最终裁决仍在运行方的懒建接管行那一步（并发窗口由它兜）。
"""

from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_STMT = text(
    "SELECT d.is_deleted, "
    "       ki.space_id AS kb_space_id, "
    "       (SELECT tc.task_id "
    "          FROM knowledge_base.task_center_documents tc "
    "         WHERE tc.document_id = d.id "
    "           AND tc.state IN ('submitting', 'managed') "
    "         LIMIT 1) AS active_task_id "
    "  FROM knowledge_base.knowledge_documents d "
    "  LEFT JOIN knowledge_base.knowledge_info ki "
    "         ON ki.id = d.knowledge_base_id "
    "        AND ki.tenant_id = d.tenant_id "
    "        AND ki.is_deleted = 0 "
    " WHERE d.id = :document_id "
    "   AND d.tenant_id = :tenant_id "
    "   AND d.knowledge_base_id = :knowledge_base_id "
    " LIMIT 1"
)


async def document_usage(
    session: AsyncSession,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
) -> Optional[Dict[str, Any]]:
    """返回 `{is_deleted, active_task_id}`；文档不存在/不属于该租户或该库时返回 `None`。"""
    row = (
        await session.execute(
            _STMT,
            {
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "document_id": document_id,
            },
        )
    ).first()
    if row is None:
        return None
    return {
        "is_deleted": bool(row[0]),
        "kb_space_id": row[1],
        "active_task_id": row[2],
    }


__all__ = ["document_usage"]
