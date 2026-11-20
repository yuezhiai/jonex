#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""触发表仓储（空间级）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.3、§4.3、§7.5 ⓪。

触发器**建在某个空间下**（`tenant_id` + `space_id` 都 NOT NULL，§3.1）——这个空间
既决定"触发出来的任务落在哪"，也是它能不能引用某个定义（尤其 `scope=space` 的）的判据。
所以唯一名也是**同租户 + 同空间**内不重复（`uq_tc_trigger_key`）。

删除走状态位（`status='deleted'`），不真删；唯一索引是
`WHERE status <> 'deleted'` 的部分索引，所以「删掉触发名再建同名」是允许的。

**触发器只等于定时**（§3.3）：`manual` 类型已砍、`trigger_type` 列已删，
触发器统一由**后台调度循环**驱动（§7.5 ⓪），这里给它三个方法：

- `list_due_cron` —— 扫「到点了、还开着」的（跨租户，只读）；
- `claim_fire` —— **抢占 CAS**：把 `next_fire_at` 推到下一次 + 记 `last_fired_at`，
  一次写入。**它刻意不比较 `state_version`**（条件只看 `next_fire_at` + `status`）；
- `pause_on_cron_failure` —— 「算不出下一次」的兜底：置 `paused`。

⚠️ **`last_fired_at` 只由 `claim_fire` 这一处写**（§7.5 ⓪）：它的语义是「上一次
**到点处理**的时间」，`misfire_policy=skip` 那次没起任务、也照样刷。所以
**`trig.fire` 不碰它** —— 要"上次真正触发任务的时间"看 `trig.runs` 最后一条。
`state_version` 这一列因此有**三个**写入点：`claim_fire`（顺手 `+1`）、
`update_fields_cas`（改 `next_fire_at` 时带上条件）、`pause_on_cron_failure`
（置 `paused` 时也 `+1`）。⚠️ **别漏掉第三个** —— 这句话是「谁会让我的
`expect_version` 失效」的唯一出处，漏一处会让人以为
`pause_on_cron_failure` 之后 `update_fields_cas` 还能命中。
（反例：`update_fields` **不写这一列** —— 它改的是「不会被并发抢占改写」的字段，
所以 `trig.pause` / `trig.resume` 走它、不动版本号。）
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.repository import BaseRepository

from ..models import (
    DefinitionStatus,
    TaskDefinition,
    TaskTrigger,
    TriggerStatus,
)


class TaskTriggerRepository(BaseRepository[TaskTrigger]):
    """触发仓储。"""

    model = TaskTrigger

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    def _filter_conditions(
        self,
        space_id: str | None = None,
        definition_id: str | None = None,
        status: str | None = None,
    ) -> list:
        conditions = []
        if space_id:
            conditions.append(TaskTrigger.space_id == space_id)
        if definition_id:
            conditions.append(TaskTrigger.definition_id == definition_id)
        # 删除走状态位（`status='deleted'`）、不真删，所以基类那套 `is_deleted` 软删条件
        # 拦不住已删的触发器——「没点名要已删的」就在这里一律排除，跟 `def.list` /
        # `task.list` 的口径一致（§4.3：已假删的按「找不到」处理）。
        if status:
            conditions.append(TaskTrigger.status == status)
        else:
            conditions.append(TaskTrigger.status != TriggerStatus.DELETED.value)
        return conditions

    async def list_triggers(
        self,
        tenant_id: str,
        space_id: str | None = None,
        definition_id: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[TaskTrigger]:
        """列触发（§4.3 `trig.list`）：`space_id` 不传就列该租户所有空间的。

        `limit` 由调用方按 `page_size` 给足——**这里不设隐含上限**（§4.0 分页约定：
        不许写死一个 limit 把多余数据悄悄丢掉）。
        """
        return await self.list_all(
            tenant_id=tenant_id,
            offset=offset,
            limit=limit,
            extra_conditions=self._filter_conditions(space_id, definition_id, status),
        )

    async def count_triggers(
        self,
        tenant_id: str,
        space_id: str | None = None,
        definition_id: str | None = None,
        status: str | None = None,
    ) -> int:
        """配套 `list_triggers` 的总数（§4.0：返回里一定有 `total`）。"""
        return await self.count(
            tenant_id=tenant_id,
            extra_conditions=self._filter_conditions(space_id, definition_id, status),
        )

    async def get_trigger(self, tenant_id: str, trigger_id: str) -> TaskTrigger | None:
        """按 id 取触发：已删的当作不存在（不返回给调用方）。"""
        stmt = select(TaskTrigger).where(
            TaskTrigger.id == trigger_id,
            TaskTrigger.tenant_id == tenant_id,
            TaskTrigger.status != TriggerStatus.DELETED.value,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_trigger_any(
        self, tenant_id: str, trigger_id: str
    ) -> TaskTrigger | None:
        """按 id 取触发，**连已假删的一起取**。

        **只有两个调用方**（两个查询都带 `tenant_id`，所以**越权一样取不到** ——
        走的是与「已删」同一条 1002）：

        - **`trig.delete` 判「已经删过了」** —— 那一格是**幂等成功**，而「根本没有」
          要按「找不到」处理，两者必须分得开，所以那条路绕不开已删的行；
        - **`trig.runs` 查历史** —— 删除是**假删**、行原样留着，「它产生过的实例还要能
          溯源」（§3.3，`tasks.trigger_id` 就是为此记的）；把已删的当「找不到」等于
          用户点完删除之后那批实例再没有任何入口。

        **其余调用方一律用 `get_trigger`**：那边「已删当作不存在」是有意的，顺手换成
        这个会把已删的触发器又当成活的（`trig.get` / `trig.update` / `trig.pause` /
        `trig.resume` / `trig.fire` 都走 `_require_trigger` 的默认档）。
        """
        stmt = select(TaskTrigger).where(
            TaskTrigger.id == trigger_id,
            TaskTrigger.tenant_id == tenant_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_trigger_key(
        self, tenant_id: str, space_id: str, trigger_key: str
    ) -> TaskTrigger | None:
        """按「租户 + 空间 + 触发名」取：只认活着的（已删的让位给同名新建）。"""
        stmt = select(TaskTrigger).where(
            TaskTrigger.tenant_id == tenant_id,
            TaskTrigger.space_id == space_id,
            TaskTrigger.trigger_key == trigger_key,
            TaskTrigger.status != TriggerStatus.DELETED.value,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_fields(
        self, tenant_id: str, trigger_id: str, **values
    ) -> TaskTrigger | None:
        """改触发（`trig.update`）。已删的改不动（条件里带 `status <> 'deleted'`）。

        **`space_id` 不在这里**：触发器建好就绑死那个空间，要换空间就删了重建（§4.3）
        —— 这一条由服务层先把关（`space_id` 传了不一样的值就拒绝），仓储层也不给改。

        **这条不带 `state_version` 条件**：它改的是"不会被并发抢占改写"的字段（名称 /
        `inputs` / `status`）。**凡是会碰 `next_fire_at` 的改动一律走
        `update_fields_cas`**（§4.3 `trig.update` / `trig.resume`）。

        命中 0 行（不存在 / 已删）返回 `None`，由调用方按「找不到」处理。
        """
        stmt = (
            update(TaskTrigger)
            .where(
                TaskTrigger.id == trigger_id,
                TaskTrigger.tenant_id == tenant_id,
                TaskTrigger.status != TriggerStatus.DELETED.value,
            )
            .values(updated_at=datetime.now(), **values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        if result.rowcount == 0:
            return None
        return await self.get_trigger(tenant_id, trigger_id)

    async def update_fields_cas(
        self, tenant_id: str, trigger_id: str, *, expect_version: int, **values
    ) -> TaskTrigger | None:
        """**带 `state_version` 条件**的更新：只有会碰 `next_fire_at` 的改动走这里。

        用在 `trig.update`（改了 `cron_expr` / `misfire_policy`）和 `trig.resume`
        （按当前时间重算）——**这两处必须带 `state_version`**（§4.3）。

        **为什么必须带**：抢占（`claim_fire`）刚把 `next_fire_at` 推后并 `+1` 过。
        没有这个条件，一次"读旧值 → 算新值 → 写回"就会**把抢占推后的值覆盖回旧值**，
        于是**同一时刻重复触发**。带上之后这种覆盖会命中 0 行，调用方重读再来。

        **⚠️ 反过来别套到抢占上**：§7.5 ⓪ 的 CAS 条件是
        `WHERE next_fire_at = :原值 AND status='active'`，**它不比较 `state_version`**
        （见 `claim_fire`）。两处的条件长得像，但管的事不同：这里是"别覆盖别人的写"，
        那里是"这一条触发器这一次归谁触发"。

        命中 0 行（不存在 / 已删 / 版本对不上）返回 `None`，由调用方决定重读还是收手。
        """
        stmt = (
            update(TaskTrigger)
            .where(
                TaskTrigger.id == trigger_id,
                TaskTrigger.tenant_id == tenant_id,
                TaskTrigger.status != TriggerStatus.DELETED.value,
                TaskTrigger.state_version == expect_version,
            )
            .values(
                state_version=TaskTrigger.state_version + 1,
                updated_at=datetime.now(),
                **values,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        if result.rowcount == 0:
            return None
        return await self.get_trigger(tenant_id, trigger_id)

    async def mark_deleted(self, tenant_id: str, trigger_id: str) -> bool:
        """删触发（`trig.delete`）：走状态位，不真删（§3.1）。"""
        stmt = (
            update(TaskTrigger)
            .where(
                TaskTrigger.id == trigger_id,
                TaskTrigger.tenant_id == tenant_id,
                TaskTrigger.status != TriggerStatus.DELETED.value,
            )
            .values(status=TriggerStatus.DELETED.value, updated_at=datetime.now())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def mark_deleted_by_definition(
        self, tenant_id: str | None, definition_id: str
    ) -> int:
        """把定义下挂的触发器一并置 `deleted`（`def.delete` 用，§4.3）。

        删定义是「整摊下线」，挂在它上面的触发不该再被触发，所以走状态位
        `status='deleted'`（不真删，保留历史）；已 `deleted` 的不重复改。返回改动行数。

        ⚠️ **`tenant_id=None` 是合法入参、语义是「所有租户」**：`scope='system'` 的定义
        是公共模板（§3.1），**各个租户**都能在它上面挂触发器。若照旧拿 `None` 去比
        `tenant_id`，SQLAlchemy 渲染出来是 `tenant_id IS NULL`，而这一列 `NOT NULL` ——
        命中 0 行、**且不报错**：级联下线静默失效，别的租户那些 `active` 触发器照旧被
        `list_due_cron` 扫到并触发，指向一个**已经删掉的定义**。
        """
        conditions = [
            TaskTrigger.definition_id == definition_id,
            TaskTrigger.status != TriggerStatus.DELETED.value,
        ]
        if tenant_id is not None:
            conditions.insert(0, TaskTrigger.tenant_id == tenant_id)
        stmt = (
            update(TaskTrigger)
            .where(*conditions)
            .values(status=TriggerStatus.DELETED.value, updated_at=datetime.now())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    # ========================================================
    # ⓪ 触发抢占（cron 到点）—— §7.5 ⓪
    # ========================================================

    async def list_due_cron(
        self, now: Optional[datetime] = None, limit: int = 10
    ) -> list[TaskTrigger]:
        """扫「到点了、还开着」的触发器（**跨租户**，后台循环用，§7.5 ⓪）。

        条件就是 §7.5 ⓪ 那条 SQL：`status='active'` + `next_fire_at <= 现在`，
        按 `next_fire_at` 升序取 `LIMIT :limit`（`:limit` = `TASK_CENTER_SCHEDULER_BATCH`，
        默认 10 —— **跟 ① 派活共用这一个配置项，但各算各的**）。

        **从前谓词里还有一个 `trigger_type='cron'`** —— `trigger_type` 列已删（触发器只
        等于定时），那一半条件随之消失；剩下的 `status='active'` 才是它真正在管的事。

        **扫描条件带 `status='active'`，所以 `paused` 的不会被扫到**（§7.5 ⓪ 的
        P1 / P6）：想让某个定时任务临时停下，`trig.pause` 即可（`next_fire_at` 保留着）。
        跟 ① 派活那批扫描一样，**这里故意不带租户条件**，拿到的每行自带 `tenant_id`，
        后续动作按行内的租户显式走。
        """
        now = now or datetime.now()
        # 2026-11-18：**停用 / 已删的定义不扫** —— 停在 fire 里拦会把「定义已停用」
        # 误判成配置类失败、被 `pause_on_cron_failure` 永久暂停（见 §13 R5）。
        published_definitions = select(TaskDefinition.id).where(
            TaskDefinition.is_deleted == 0,
            TaskDefinition.status == DefinitionStatus.PUBLISHED.value,
        )
        stmt = (
            select(TaskTrigger)
            .where(
                TaskTrigger.status == TriggerStatus.ACTIVE.value,
                TaskTrigger.next_fire_at <= now,
                TaskTrigger.definition_id.in_(published_definitions),
            )
            .order_by(TaskTrigger.next_fire_at)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def claim_fire(
        self,
        tenant_id: str,
        trigger_id: str,
        *,
        expect_next_fire_at: datetime,
        next_fire_at: datetime,
        fired_at: Optional[datetime] = None,
    ) -> bool:
        """**触发抢占的 CAS**：推 `next_fire_at` + 记 `last_fired_at` + `state_version+1`，
        **三件事一次写**（§7.5 ⓪ 第 1 步）。

        ```sql
        UPDATE task_triggers
           SET next_fire_at = :新值, last_fired_at = :现在, state_version = state_version + 1
         WHERE id = :id AND next_fire_at = :原值 AND status = 'active'
        ```

        **命中 0 行 = 别的副本刚抢走 → 本轮跳过这条**。多个副本都会扫到「这条到点了」，
        不抢占就会**同一时刻触发多次** —— `next_fire_at = :原值` 保证只有一个副本改得成。

        **⚠️ 这里刻意不比较 `state_version`**：抢的是「这一条触发器**这一次**归谁触发」，
        `next_fire_at` 就是那个"这一次"的凭据；`state_version` 只是顺手 `+1`，好让
        `update_fields_cas`（改 `next_fire_at` 的那条路）**感知到被抢过**。

        **先算好 `:新值` 再抢占**（调用方负责算）：抢占成功时 `next_fire_at` 已经推到
        下一次，所以即使紧接着的 `trig.fire` 失败，也**不会同一时刻反复重触发**
        （漏了就等下一个点，§7.5 ⓪ 的 P3）。
        """
        fired_at = fired_at or datetime.now()
        stmt = (
            update(TaskTrigger)
            .where(
                TaskTrigger.id == trigger_id,
                TaskTrigger.tenant_id == tenant_id,
                TaskTrigger.next_fire_at == expect_next_fire_at,
                TaskTrigger.status == TriggerStatus.ACTIVE.value,
            )
            .values(
                next_fire_at=next_fire_at,
                last_fired_at=fired_at,
                state_version=TaskTrigger.state_version + 1,
                updated_at=fired_at,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def pause_on_cron_failure(self, tenant_id: str, trigger_id: str) -> bool:
        """抢占路上「**算不出下一次**」的兜底：置 `paused` + `state_version + 1`（§3.3.2 ⑤）。

        ```sql
        UPDATE task_triggers SET status='paused', state_version = state_version + 1
         WHERE id = :id AND status = 'active'
        ```

        **`next_fire_at` 不动、`last_fired_at` 也不刷** —— 这一轮根本没触发任何东西
        （§7.5 ⓪ 第 1 步）；算不出下一次是理论上不可达的兜底（规格见 §3.3.2 ⑤），
        调用方还会记一条告警日志。

        **⚠️ 绝不能写 `next_fire_at = NULL`**：`next_fire_at` 这一列是 `NOT NULL`
        （从前靠建表 `ck_tc_trig_cron` 按类型强制，`trigger_type` 列删掉后就是列约束了），
        那条 UPDATE 会被数据库直接拒绝（附录 B.2 第 5 条）。
        """
        stmt = (
            update(TaskTrigger)
            .where(
                TaskTrigger.id == trigger_id,
                TaskTrigger.tenant_id == tenant_id,
                TaskTrigger.status == TriggerStatus.ACTIVE.value,
            )
            .values(
                status=TriggerStatus.PAUSED.value,
                state_version=TaskTrigger.state_version + 1,
                updated_at=datetime.now(),
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


__all__ = ["TaskTriggerRepository"]
