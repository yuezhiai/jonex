from datetime import datetime, timedelta
from typing import Callable, List, Optional, Sequence

from sqlalchemy import select, func, desc, distinct, or_, delete

from jonex_core.common.tenant import require_tenant

from capabilities.platform.models.audit_log import AuditLog
from capabilities.platform.repository.base import BaseRepository

#: 单批删除条数。每批一次 commit，避免长事务长时间持锁。
DELETE_BATCH_SIZE = 1000


class AuditLogRepository(BaseRepository[AuditLog]):
    model = AuditLog

    def __init__(
        self,
        session,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        """``now_fn`` 注入口：单测替换时钟，与 ``identity_sync_scheduler`` 同构。"""
        super().__init__(session)
        self._now_fn = now_fn or datetime.now

    async def list_by_tenant(
        self,
        tenant_id: str,
        log_type: Optional[str] = None,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        service_name: Optional[str] = None,
        user_id: Optional[int] = None,
        keyword: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        offset: int = 0,
        limit: int = 20,
    ) -> Sequence[AuditLog]:
        tenant_id = require_tenant(tenant_id)
        conditions = [AuditLog.tenant_id == tenant_id]

        if log_type:
            conditions.append(AuditLog.log_type == log_type)
        if action:
            conditions.append(AuditLog.action == action)
        if outcome:
            conditions.append(AuditLog.outcome == outcome)
        if service_name:
            conditions.append(AuditLog.service_name == service_name)
        if user_id:
            conditions.append(AuditLog.user_id == user_id)
        if keyword:
            conditions.append(
                or_(
                    AuditLog.username.ilike(f"%{keyword}%"),
                    AuditLog.resource_id.ilike(f"%{keyword}%"),
                )
            )
        if start_time:
            conditions.append(AuditLog.created_at >= start_time)
        if end_time:
            conditions.append(AuditLog.created_at <= end_time)

        result = await self.session.execute(
            select(AuditLog)
            .where(*conditions)
            .order_by(desc(AuditLog.created_at))
            .offset(offset)
            .limit(limit)
        )
        return result.scalars().all()

    async def count_by_tenant(
        self,
        tenant_id: str,
        log_type: Optional[str] = None,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        service_name: Optional[str] = None,
        user_id: Optional[int] = None,
        keyword: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> int:
        tenant_id = require_tenant(tenant_id)
        conditions = [AuditLog.tenant_id == tenant_id]

        if log_type:
            conditions.append(AuditLog.log_type == log_type)
        if action:
            conditions.append(AuditLog.action == action)
        if outcome:
            conditions.append(AuditLog.outcome == outcome)
        if service_name:
            conditions.append(AuditLog.service_name == service_name)
        if user_id:
            conditions.append(AuditLog.user_id == user_id)
        if keyword:
            conditions.append(
                or_(
                    AuditLog.username.ilike(f"%{keyword}%"),
                    AuditLog.resource_id.ilike(f"%{keyword}%"),
                )
            )
        if start_time:
            conditions.append(AuditLog.created_at >= start_time)
        if end_time:
            conditions.append(AuditLog.created_at <= end_time)

        result = await self.session.execute(
            select(func.count()).select_from(AuditLog).where(*conditions)
        )
        return result.scalar() or 0

    async def add_all(self, entries: List[AuditLog]):
        """批量写入"""
        self.session.add_all(entries)

    async def list_system_events(
        self,
        action: Optional[str] = None,
        service_name: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        offset: int = 0,
        limit: int = 20,
    ) -> Sequence[AuditLog]:
        conditions = [AuditLog.tenant_id.is_(None)]
        if action:
            conditions.append(AuditLog.action == action)
        if service_name:
            conditions.append(AuditLog.service_name == service_name)
        if start_time:
            conditions.append(AuditLog.created_at >= start_time)
        if end_time:
            conditions.append(AuditLog.created_at <= end_time)
        result = await self.session.execute(
            select(AuditLog)
            .where(*conditions)
            .order_by(desc(AuditLog.created_at))
            .offset(offset)
            .limit(limit)
        )
        return result.scalars().all()

    async def count_system_events(
        self,
        action: Optional[str] = None,
        service_name: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> int:
        conditions = [AuditLog.tenant_id.is_(None)]
        if action:
            conditions.append(AuditLog.action == action)
        if service_name:
            conditions.append(AuditLog.service_name == service_name)
        if start_time:
            conditions.append(AuditLog.created_at >= start_time)
        if end_time:
            conditions.append(AuditLog.created_at <= end_time)
        result = await self.session.execute(
            select(func.count()).select_from(AuditLog).where(*conditions)
        )
        return result.scalar() or 0

    async def get_by_id_with_detail(self, log_id: int) -> Optional[AuditLog]:
        """获取单条完整详情（含 response_body / error_stack）"""
        result = await self.session.execute(
            select(AuditLog).where(AuditLog.id == log_id)
        )
        return result.scalar_one_or_none()

    async def delete_expired(
        self, retention_days: int, batch_size: int = DELETE_BATCH_SIZE
    ) -> int:
        """删除超过保留天数的过期记录，返回删除条数。

        口径（第 3 期 · 任务 6/8）：

        - ``retention_days <= 0`` ⇒ **不清理**（0 = 不自动到期，不是「立即删除」）。
        - cutoff 用 ``datetime.now()``（本地 naive）：``audit_logs.created_at`` 由
          ``TimestampMixin`` 的 ``default=datetime.now`` 写入，同为 naive 本地时间；
          用 ``utcnow()`` 会比 cutoff 偏 8 小时。
        - 分批删除（默认每批 1000 条），每批单独 commit 释放锁；循环到删 0 行为止。
          单条 DELETE 在积压量大时会长时间持锁并放大 WAL。

        ⚠️ **本方法会提交调用方的事务**（每批一次 ``commit()``）。只允许在独占
        session 上调用（调度器 ``run_once`` 自己开 session）；不要塞进别人的
        业务事务里，否则会在中途把对方的未提交写入一并提交掉。
        """
        if retention_days <= 0:
            return 0

        cutoff = self._now_fn() - timedelta(days=retention_days)
        total = 0
        while True:
            # ORDER BY 固定锁顺序：多副本/并发清理时降低互相等待与死锁概率。
            ids = (
                select(AuditLog.id)
                .where(AuditLog.created_at < cutoff)
                .order_by(AuditLog.created_at, AuditLog.id)
                .limit(batch_size)
            )
            result = await self.session.execute(
                delete(AuditLog)
                .where(AuditLog.id.in_(ids))
                # 不需要把删除结果同步回 session 里的对象（省掉 RETURNING）。
                .execution_options(synchronize_session=False)
            )
            deleted = max(result.rowcount or 0, 0)
            # 每批 commit：释放行锁，别让一个长事务把整张表的清理拖成锁等待。
            await self.session.commit()
            total += deleted
            if deleted == 0:
                break
        return total

    async def list_distinct_actions(self, tenant_id: str) -> list[str]:
        """查询当前租户所有已使用的操作类型（去重后按字母排序）。"""
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(distinct(AuditLog.action))
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.action)
        )
        return list(result.scalars().all())

    async def list_distinct_resources(self, tenant_id: str) -> list[str]:
        """查询当前租户所有已使用的资源类型（去重后按字母排序）。"""
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(distinct(AuditLog.resource))
            .where(AuditLog.tenant_id == tenant_id)
            .where(AuditLog.resource.isnot(None))
            .order_by(AuditLog.resource)
        )
        return list(result.scalars().all())
