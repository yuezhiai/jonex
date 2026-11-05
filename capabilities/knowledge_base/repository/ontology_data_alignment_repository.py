#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""OntologyDataAlignment 仓储（跨模态对齐，人审）。

表：`knowledge_base.ontology_data_alignment`（迁移 011）。
一行由 `(tenant_id, knowledge_base_id, doc_type, data_source_id, entity_name)`
唯一确定；`upsert` 按该五元组幂等写入。

错误码口径（P1.5b）：重复行 / 非法参数用 **1001**，缺失用 **1002**（本层
`set_status`/`get` 找不到返回 `None`，由服务层决定是否升级为 1002）。
"""
from uuid import uuid4

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.ontology_data_alignment import (
    VALID_ALIGNMENT_STATUSES,
    OntologyDataAlignment,
    validate_status,
)


def _require_nonblank(field: str, value: str) -> str:
    """与迁移 011 的 `ck_oda_nonblank` 同口径：三个业务键不得为空白。

    校验通过后返回 **strip 过** 的值：原始值直接入库会让 `"Asset"` 与
    `"Asset "` 绕过五元组唯一索引（`btrim` 只用于 CHECK，不参与唯一键）。
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidParameterError(
            message=f"{field} 不能为空",
            details={"field": field},
        )
    return value.strip()


class OntologyDataAlignmentRepository(BaseRepository[OntologyDataAlignment]):
    model = OntologyDataAlignment

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_kb(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        status: str | None = None,
    ) -> list[OntologyDataAlignment]:
        """该 KB 下的对齐行；`status` 给了就按状态过滤（非法状态 → 1001）。"""
        tid = require_tenant(tenant_id)
        conditions = [
            OntologyDataAlignment.tenant_id == tid,
            OntologyDataAlignment.knowledge_base_id == knowledge_base_id,
        ]
        if status is not None:
            conditions.append(OntologyDataAlignment.status == validate_status(status))
        result = await self.session.execute(
            select(OntologyDataAlignment)
            .where(*conditions)
            .order_by(
                OntologyDataAlignment.created_at.desc(),
                OntologyDataAlignment.id,
            )
        )
        return list(result.scalars())

    async def get(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        alignment_id: str,
    ) -> OntologyDataAlignment | None:
        """按 id 取一行；不同租户 / 不同 KB 一律查不到（返回 None）。"""
        tid = require_tenant(tenant_id)
        result = await self.session.execute(
            select(OntologyDataAlignment).where(
                OntologyDataAlignment.id == alignment_id,
                OntologyDataAlignment.tenant_id == tid,
                OntologyDataAlignment.knowledge_base_id == knowledge_base_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        doc_type: str,
        data_source_id: str,
        entity_name: str,
        status: str = "proposed",
        created_by: str | None = None,
    ) -> OntologyDataAlignment:
        """按五元组插入或更新一行。

        冲突时只更新 `status` / `updated_at`，且 **`active` 不降级**：人审通过
        的行即使收到 `proposed` / `rejected` 的盲写也保持 `active`（要改状态必须
        走 `set_status`）。`created_by` 记的是**提出人**，写一次（重提不覆盖）。
        """
        tid = require_tenant(tenant_id)
        validate_status(status)
        # strip 后再入库：空白差异不得绕过五元组唯一索引（见 _require_nonblank）。
        doc_type = _require_nonblank("doc_type", doc_type)
        data_source_id = _require_nonblank("data_source_id", data_source_id)
        entity_name = _require_nonblank("entity_name", entity_name)

        values = {
            "id": str(uuid4()),
            "tenant_id": tid,
            "knowledge_base_id": knowledge_base_id,
            "doc_type": doc_type,
            "data_source_id": data_source_id,
            "entity_name": entity_name,
            "status": status,
            "created_by": created_by,
        }
        stmt = pg_insert(OntologyDataAlignment).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "tenant_id",
                "knowledge_base_id",
                "doc_type",
                "data_source_id",
                "entity_name",
            ],
            set_={
                "status": case(
                    (OntologyDataAlignment.status == "active", "active"),
                    else_=stmt.excluded.status,
                ),
                "updated_at": func.now(),
            },
        ).returning(OntologyDataAlignment)
        try:
            result = await self.session.execute(stmt)
        except IntegrityError as exc:
            # 正常路径由 ON CONFLICT 吸收；走到这里说明约束层面另有冲突
            # （例如同一 id 撞车），按「重复行」口径给 1001。
            #
            # 若库中缺 uq_oda_triple，PostgreSQL 抛的是 ProgrammingError
            # （"no unique or exclusion constraint matching the ON CONFLICT
            # specification"），**不是** IntegrityError —— 它不在这里被捕获，
            # 直接上抛（fail-loud）：缺索引是部署缺陷，不应伪装成「重复行」。
            raise InvalidParameterError(
                message="该对齐已存在或违反约束（同一 tenant/kb/doc_type/源/实体）",
                details={
                    "knowledge_base_id": knowledge_base_id,
                    "doc_type": doc_type,
                    "data_source_id": data_source_id,
                    "entity_name": entity_name,
                },
                cause=exc,
            ) from exc
        return result.scalar_one()

    async def set_status(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        alignment_id: str,
        status: str,
    ) -> OntologyDataAlignment | None:
        """改状态；非法状态在查库前就抛 1001，行不存在返回 None。"""
        validate_status(status)
        row = await self.get(tenant_id, knowledge_base_id, alignment_id)
        if row is None:
            return None
        row.status = status
        await self.session.flush()
        return row


__all__ = [
    "OntologyDataAlignmentRepository",
    "VALID_ALIGNMENT_STATUSES",
    "validate_status",
]
