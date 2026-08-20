"""LLM-Wiki Schema repository。

留档模型（方案 §3/§7）：每次保存递增 schema_version，
旧 active 行转 archived（历史留档），新行 INSERT 为 active。
partial unique index（WHERE status='active'）保证每 KB 单 active。

CAS 落在 UPDATE 旧行（方案 §7 第 5 步）：
UPDATE ... WHERE id=? AND schema_version=? AND status='active'
rowcount=0 → 版本冲突（而非等 partial unique index 拦第二个 INSERT 抛
DB 唯一约束冲突——错误码语义不对）。
"""
import logging
from typing import Optional

from sqlalchemy import and_, select, text
from sqlalchemy.exc import IntegrityError

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import JonexException
from jonex_core.common.i18n import translate

from ..models import (
    LlmWikiSchema,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    SYNC_SYNCED,
)

logger = logging.getLogger(__name__)


class LlmWikiSchemaRepository:
    async def get_active(self, tenant_id: str, kb_id: str) -> Optional[LlmWikiSchema]:
        """读当前 active 行；无则 None（调用方决定 auto_create）。"""
        async with get_db_session() as session:
            row = (
                await session.execute(
                    select(LlmWikiSchema)
                    .where(
                        LlmWikiSchema.tenant_id == tenant_id,
                        LlmWikiSchema.knowledge_base_id == kb_id,
                        LlmWikiSchema.status == STATUS_ACTIVE,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            # 脱离 session（返回给 service 层继续用字段）
            session.expunge(row)
            return row

    async def create_active(self, tenant_id: str, kb_id: str, *,
                            schema_version: int, schema_name: str, language: str,
                            model: Optional[str], entity_types: list, concept_types: list,
                            agents_md_extra: str,
                            agents_md: str, config_snapshot: dict) -> LlmWikiSchema:
        """INSERT 新 active 行（KB 创建默认 schema / save 留档新行）。"""
        async with get_db_session() as session:
            row = LlmWikiSchema(
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                schema_version=schema_version,
                status=STATUS_ACTIVE,
                sync_status=SYNC_SYNCED,
                schema_name=schema_name,
                language=language,
                model=model,
                entity_types=entity_types,
                concept_types=concept_types,
                agents_md_extra=agents_md_extra,
                agents_md=agents_md,
                config_snapshot=config_snapshot,
            )
            session.add(row)
            await session.commit()
            session.expunge(row)
            return row

    async def save_with_cas(self, tenant_id: str, kb_id: str, *,
                            expected_version: int, new_version: int,
                            schema_name: str, language: str,
                            model: Optional[str], entity_types: list, concept_types: list,
                            agents_md_extra: str,
                            agents_md: str, config_snapshot: dict,
                            edited_by: Optional[str]) -> LlmWikiSchema:
        """CAS 留档保存：UPDATE 旧 active 行（钉 CAS）→ INSERT 新 active 行。

        - UPDATE 条件 `schema_version=:expected AND status='active'`；
          rowcount=0 → 版本冲突（SCHEMA_VERSION_CONFLICT，方案 §7 第 5 步）。
        - 两步同事务：任一步失败整体回滚（partial unique index 只挡
          active，archived 留档不受限）。
        """
        async with get_db_session() as session:
            # ① CAS：旧 active 行转 archived（钉在 UPDATE 上）
            res = await session.execute(
                text(
                    "UPDATE knowledge_base.llm_wiki_schemas"
                    "   SET status = :archived, updated_at = CURRENT_TIMESTAMP"
                    " WHERE tenant_id = :tid AND knowledge_base_id = :kb"
                    "   AND schema_version = :expected AND status = :active"
                ),
                {
                    "archived": STATUS_ARCHIVED,
                    "tid": tenant_id,
                    "kb": kb_id,
                    "expected": expected_version,
                    "active": STATUS_ACTIVE,
                },
            )
            if (res.rowcount or 0) == 0:
                # 版本冲突：并发保存/前端旧版本提交
                from jonex_core.common.exceptions import ResourceConflictError

                await session.rollback()
                raise ResourceConflictError(
                    message=translate(
                        "err.llm_wiki_schema.version_conflict",
                        fallback="LLM-Wiki Schema 已被更新，请刷新后重试",
                    ),
                    details={
                        "error_code": "LLM_WIKI_SCHEMA_VERSION_CONFLICT",
                        "expected_schema_version": expected_version,
                    },
                )

            # ② INSERT 新 active 行（同事务）
            row = LlmWikiSchema(
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                schema_version=new_version,
                status=STATUS_ACTIVE,
                sync_status=SYNC_SYNCED,
                schema_name=schema_name,
                language=language,
                model=model,
                entity_types=entity_types,
                concept_types=concept_types,
                agents_md_extra=agents_md_extra,
                agents_md=agents_md,
                config_snapshot=config_snapshot,
                edited_by=edited_by,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # partial unique index 兜底（理论上 CAS 已挡住，防御性兜底）
                await session.rollback()
                from jonex_core.common.exceptions import ResourceConflictError

                raise ResourceConflictError(
                    message=translate(
                        "err.llm_wiki_schema.version_conflict",
                        fallback="LLM-Wiki Schema 已被更新，请刷新后重试",
                    ),
                    details={"error_code": "LLM_WIKI_SCHEMA_VERSION_CONFLICT"},
                )
            session.expunge(row)
            return row

    async def mark_sync(self, tenant_id: str, kb_id: str, schema_version: int, *,
                        sync_status: str, applied: bool = False) -> None:
        """apply_schema 结果回写（synced / apply_failed）。"""
        from datetime import datetime

        sql = (
            "UPDATE knowledge_base.llm_wiki_schemas"
            "   SET sync_status = :sync"
        )
        params: dict = {"sync": sync_status, "tid": tenant_id, "kb": kb_id,
                        "ver": schema_version}
        if applied:
            sql += ", applied_at = CURRENT_TIMESTAMP"
        sql += (" WHERE tenant_id = :tid AND knowledge_base_id = :kb"
                "   AND schema_version = :ver AND status = :active")
        params["active"] = STATUS_ACTIVE
        async with get_db_session() as session:
            await session.execute(text(sql), params)
            await session.commit()
