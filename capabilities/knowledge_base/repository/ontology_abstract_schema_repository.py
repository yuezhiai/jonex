#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""OntologyAbstractSchema 仓储。"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import ResourceConflictError
from jonex_core.common.i18n import translate
from jonex_core.common.repository import BaseRepository
from jonex_core.common.schema_generation import bump_schema_generation
from jonex_core.common.tenant import require_tenant

from ..models.ontology_abstract_schema import OntologyAbstractSchema


def _stale_version_error(
    expected_version: Optional[int], actual_version: Optional[int],
) -> ResourceConflictError:
    """版本冲突的唯一构造点：1003 + `err.abstract_schema.stale_version`。

    `actual_version` 允许是 None：并发首写那条路上事务已被 PostgreSQL 中止，读不出
    库里的真实版本 —— 那时**不编造**版本号，消息里用 `?` 占位（details 里老实写 None）。
    """
    actual = actual_version if actual_version is not None else "?"
    return ResourceConflictError(
        message=translate(
            "err.abstract_schema.stale_version",
            params={"actual": actual},
            fallback=f"抽象 schema 已被他人修改（当前 v{actual}），请刷新后重试",
        ),
        details={
            "expected_schema_version": expected_version,
            "actual_schema_version": actual_version,
        },
    )


class OntologyAbstractSchemaRepository(BaseRepository[OntologyAbstractSchema]):
    model = OntologyAbstractSchema

    async def get_by_kb(
        self, tenant_id: str, knowledge_base_id: str,
    ) -> Optional[OntologyAbstractSchema]:
        """按 (租户, 知识库) 取那一份 —— 主键是 id，走不了基类的 get_by_id。

        用 `scalar_one_or_none`：一库一份由 `uq_oas_per_kb` 兜底，出现两行是
        数据被破坏，直接抛比默默取第一行好。
        """
        tid = require_tenant(tenant_id)
        stmt = select(OntologyAbstractSchema).where(
            OntologyAbstractSchema.tenant_id == tid,
            OntologyAbstractSchema.knowledge_base_id == knowledge_base_id,
            OntologyAbstractSchema.is_deleted == 0,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_active_by_kbs(
        self, tenant_id: str, knowledge_base_ids: list[str],
    ) -> list[OntologyAbstractSchema]:
        """批量取多个 KB **已确认**的对齐（检索侧一次查完，不按 KB 循环）。

        只回 `status='active'`：未确认的对齐不进检索（决策 4）——
        「人还没看过就影响答案」正是要避免的。

        检索侧按**查询的 KB 集合**取，不是按「命中的那个 KB」：后者是既有
        `top_kb_id` 口径（search_service.py:5236）的多 KB 缺陷，新代码不复刻。
        """
        tid = require_tenant(tenant_id)
        if not knowledge_base_ids:
            return []
        stmt = select(OntologyAbstractSchema).where(
            OntologyAbstractSchema.tenant_id == tid,
            OntologyAbstractSchema.knowledge_base_id.in_(list(knowledge_base_ids)),
            OntologyAbstractSchema.status == "active",
            OntologyAbstractSchema.is_deleted == 0,
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def _current_version(
        self, tenant_id: str, knowledge_base_id: str,
    ) -> Optional[int]:
        """读库里那一行**当前**的 `schema_version`（没有这一行则 None）。

        只在冲突分支用它把「当前 v几」写进错误消息。走 Core `select(列)` 而不是 ORM
        实体是**必须的**：ORM 取对象会命中 identity map 里那份过期快照（正是冲突的
        成因），读回来还是旧版本，等于没读。
        """
        stmt = select(OntologyAbstractSchema.schema_version).where(
            OntologyAbstractSchema.tenant_id == tenant_id,
            OntologyAbstractSchema.knowledge_base_id == knowledge_base_id,
            OntologyAbstractSchema.is_deleted == 0,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        schema: dict,
        status: str = "proposed",
        source_hash: Optional[str] = None,
        edited_by: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> OntologyAbstractSchema:
        """一库一份的 upsert，带乐观锁。

        三个分支：
        - 不存在           → INSERT，`schema_version = 1`（**不看** `expected_version`）
        - 存在、版本对上   → UPDATE，`schema_version = expected + 1`
        - 存在、版本对不上 → `ResourceConflictError`（1003）

        ⚠️ 已存在时 `expected_version` 为 None **拒绝**，不是「不校验」：
        仓储不设防的话，一个漏传版本的调用方就能静默盖掉别人刚编好的对齐，
        而受害者只会看到「我改的东西没了」。

        ⚠️ 版本判定必须**跟着 UPDATE 一起下到 WHERE 里**（CAS），不能只在 Python 里比：
        只比不设防的话，两个都读到 v3 的写者会双双通过比对、双双写 `schema_version = 4`，
        后到的把先到的 schema 盖掉，而版本号事后看不出被盖过 —— 正是本仓储要防的那种
        丢更新。写法与 `OntologySchemaRepository.upsert_compiled_schema` 的 UPDATE 分支一致。

        ⚠️ 首写有**同一条缝**，而且更常见：两个调用方都读到 `old is None` 就会双双 INSERT，
        后到的撞上 `uq_oas_per_kb`。所以 INSERT 也要把 `IntegrityError` 收成同一个 1003。
        """
        tid = require_tenant(tenant_id)
        now = datetime.now(timezone.utc)
        # 时间戳列是 naive 的（TimestampMixin），与 upsert_compiled_schema 同口径写 UTC-naive；
        # 不用 `datetime.utcnow()`（3.12 起它自己就发 DeprecationWarning）。
        now_naive = now.replace(tzinfo=None)
        old = await self.get_by_kb(tid, knowledge_base_id)

        if old is None:
            row = OntologyAbstractSchema(
                id=uuid.uuid4().hex,
                tenant_id=tid,
                knowledge_base_id=knowledge_base_id,
                schema_version=1,
                status=status,
                schema=schema,
                source_hash=source_hash,
                edited_at=now if edited_by else None,
                edited_by=edited_by or None,
            )
            self.session.add(row)
            try:
                await self.session.flush()
            except IntegrityError as exc:
                # 并发首写：有人在我读完之后、我写进去之前抢先建了那一行（撞 uq_oas_per_kb）。
                # 语义与「版本对不上」**完全一样** —— 别人先到，重读再来 —— 所以映射成同一个
                # 1003。不映射的话 `IntegrityError` 会一路裸奔到全局异常处理器：那里没有它的
                # 码可翻，用户看到的是 500（触发场景：生成候选 / 确认被连点两下）。
                # 此刻事务已被 PostgreSQL 中止（后续任何查询都会 PendingRollbackError），所以
                # **读不了**真实版本、也不在这里 rollback —— 回滚由 `get_db_session` 统一做，
                # 仓储只 flush，不 commit 也不 rollback。
                raise _stale_version_error(expected_version, None) from exc
            # P1 版本化读取：写成功后推进进程内代号（② 的行缓存据此立刻换键）。
            # 只在**成功返回路径**上 bump：上面的冲突分支已经 raise，不推进。
            bump_schema_generation()
            return row

        if expected_version is None:
            raise _stale_version_error(None, old.schema_version or 1)

        values: dict = {
            "schema": schema,
            "status": status,
            "source_hash": source_hash,
            "schema_version": expected_version + 1,
            "updated_at": now_naive,
        }
        if edited_by:
            # 不给 edited_by 时**不碰**这两列：保持原语义（纯 LLM 重新生成不抹掉上一次
            # 人工确认的署名）。
            values["edited_at"] = now
            values["edited_by"] = edited_by

        stmt = (
            update(OntologyAbstractSchema)
            .where(
                OntologyAbstractSchema.id == old.id,
                # ← 关键：版本进 WHERE。0 行 = 我读到之后有人写过，冲突成立。
                OntologyAbstractSchema.schema_version == expected_version,
            )
            .values(**values)
            .returning(OntologyAbstractSchema.id)
        )
        result = await self.session.execute(stmt)
        if result.first() is None:
            # 判定已经由 WHERE 做完，这里重取真实版本只为把「当前 v几」写进错误消息。
            raise _stale_version_error(
                expected_version, await self._current_version(tid, knowledge_base_id),
            )
        # Core UPDATE 不会刷新 identity map 里那份已过期的 old，必须 expire 后重取，
        # 否则调用方拿到的是「没有本次改动」的旧对象（同 upsert_compiled_schema）。
        self.session.expire(old)
        row = await self.get_by_kb(tid, knowledge_base_id)
        # P1 版本化读取：UPDATE 成功（rowcount 命中）后推进代号（见插入分支同一注释）。
        bump_schema_generation()
        return row


__all__ = ["OntologyAbstractSchemaRepository"]
