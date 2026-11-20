#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务定义与版本仓储。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.2、§4.3、§6.6。

**定义分三级（`scope`），所以这层的租户条件不是"等于"这么简单**（§3.1）：

- `scope='system'`：无 `tenant_id`、无 `space_id`，**对所有租户可见**（当公共模板用），
  但"只有平台自己能改"；
- `scope='tenant'`：有租户、无空间，只有该租户可见可改；
- `scope='space'`：租户 + 空间都有。⚠️ **但空间目前只是个标记、不参与隔离** —— 「读」与
  「写」两条口径的条件里都只有 `tenant_id`（见下），所以**同租户下的其他空间也能读到、
  改到这条定义**。这不是漏了一层：`def.get` / `def.version_update` / `def.version_publish` /
  `def.version_get` / `def.delete` 的入参里**根本没有 `space_id`**，判权时没有可用的来源
  空间（`capability.py` 的必填字段表）。**根子在模型上：这套模型里没有「用户属于哪个
  空间」这个事实**（用户只属于租户），所以「这是哪个空间在调用」压根判不出来。空间在
  任务中心是**必须前后一致的数据维度、不是权限维度**（「能不能引用它跑任务」那一侧是
  按空间判的 —— 触发器按自己那一行的 `space_id` 判、`task.fire` 按 A13 判，但那是调用方
  必须先「说出」空间，与「判权」是两件事）。要真按空间收权，得先给这几个动作加上来源空间。


因此本类把"读"和"写"分成两条口径（§4 权限规则）：

- `get_visible` / `list_visible`（**读**）：`scope='system' OR tenant_id = :t`；
- `get_owned`（**写**）：`scope IN ('tenant','space') AND tenant_id = :t`——系统级定义
  经租户上下文一律按「找不到」处理，正好落实"只有平台自己能改"。

越权按「找不到」处理（跟租户隔离同一个口径，不暴露"存在但不属于你"）。

**版本表的所有查询都只按 `definition_id` 定位、不带租户条件**：`definition_id` 是全局
唯一主键，权限在定义那一层已经判过（拿到 `definition_id` 的前提就是读得到那个定义），
再叠一层租户条件只会把系统级定义（`tenant_id IS NULL`）的版本挡在外面。

版本状态机只有一条正向路（draft → published），所以这里所有写操作都带
`WHERE status = 'draft'` 这个前置条件——**已发布版本不可改、不可逆回 draft**
这条铁律靠「写入口先判状态 + SQL 再带一次状态条件」双保险，不依赖调用方自觉。

**版本不可单独删**：`task_definition_versions` 的 `is_deleted` 只由「删定义」带出来
（定义假删时它下面的版本一并假删，§3.2、§4.3）；所以这里所有读操作也一律带上
`is_deleted = 0`，被定义带删的版本按「找不到」处理，不再对外暴露。
"""

from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.repository import BaseRepository

from ..models import DefinitionScope, TaskDefinition, TaskDefinitionVersion, VersionStatus


def _visible_conditions(
    tenant_id: str,
    scope: str | None = None,
    allowed_space_ids: list[str] | None = None,
) -> list:
    """「这个租户能看到哪些定义」的条件（§4 权限规则，2026-11-18 改）。

    不带 `scope`：系统级（对所有租户可见）**或**本租户的空间级。
    带了 `scope`：只看指定的那一级，空间级仍要卡在本租户名下。

    `allowed_space_ids` 是**权限收敛**（不是筛选）：`None` = 不过滤（管理员 / 内部链路）；
    `[]` = 一个都看不到（恒假）；非空 = 只能看这些空间 + 系统级。
    """
    if scope == DefinitionScope.SYSTEM.value:
        conditions = [TaskDefinition.scope == DefinitionScope.SYSTEM.value]
    elif scope == DefinitionScope.SPACE.value:
        conditions = [
            TaskDefinition.scope == DefinitionScope.SPACE.value,
            TaskDefinition.tenant_id == tenant_id,
        ]
    else:
        conditions = [
            or_(
                TaskDefinition.scope == DefinitionScope.SYSTEM.value,
                TaskDefinition.tenant_id == tenant_id,
            )
        ]
    if allowed_space_ids is not None:
        conditions.append(
            or_(
                TaskDefinition.scope == DefinitionScope.SYSTEM.value,
                TaskDefinition.space_id.in_(allowed_space_ids),
            )
        )
    return conditions


def _space_filter(space_id: str):
    """按空间筛选时**系统级仍要返回** —— 它不属于任何空间，是公共模板。

    写成 `space_id == X` 会把 `scope='system'`（`space_id IS NULL`）整批筛掉：
    主应用选了空间之后，列表里就再也看不到预置定义了。
    """
    return or_(
        TaskDefinition.space_id == space_id,
        TaskDefinition.scope == DefinitionScope.SYSTEM.value,
    )


class TaskDefinitionRepository(BaseRepository[TaskDefinition]):
    """任务定义仓储（三级 scope、假删除）。"""

    model = TaskDefinition

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_visible(
        self,
        tenant_id: str,
        scope: str | None = None,
        offset: int = 0,
        limit: int = 20,
        *,
        keyword: str | None = None,
        status: str | None = None,
        space_id: str | None = None,
        allowed_space_ids: list[str] | None = None,
    ) -> list[TaskDefinition]:
        """分页列某租户可见的定义（系统级 + 本租户空间级；可加筛选）。"""
        conditions = [
            TaskDefinition.is_deleted == 0,
            *_visible_conditions(tenant_id, scope, allowed_space_ids),
        ]
        if keyword:
            conditions.append(TaskDefinition.name.ilike(f"%{keyword}%"))
        if status:
            conditions.append(TaskDefinition.status == status)
        if space_id:
            conditions.append(_space_filter(space_id))
        stmt = (
            select(TaskDefinition)
            .where(*conditions)
            .order_by(TaskDefinition.created_at.desc(), TaskDefinition.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_visible(
        self,
        tenant_id: str,
        scope: str | None = None,
        *,
        keyword: str | None = None,
        status: str | None = None,
        space_id: str | None = None,
        allowed_space_ids: list[str] | None = None,
    ) -> int:
        conditions = [
            TaskDefinition.is_deleted == 0,
            *_visible_conditions(tenant_id, scope, allowed_space_ids),
        ]
        if keyword:
            conditions.append(TaskDefinition.name.ilike(f"%{keyword}%"))
        if status:
            conditions.append(TaskDefinition.status == status)
        if space_id:
            conditions.append(_space_filter(space_id))
        stmt = (
            select(func.count())
            .select_from(TaskDefinition)
            .where(*conditions)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def get_by_name(
        self, tenant_id: str, space_id: str, name: str
    ) -> TaskDefinition | None:
        """同租户同空间下的同名定义（名称判重用；软删的不算）。"""
        stmt = select(TaskDefinition).where(
            TaskDefinition.tenant_id == tenant_id,
            TaskDefinition.space_id == space_id,
            TaskDefinition.name == name,
            TaskDefinition.is_deleted == 0,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_fields(self, definition_id: str, **values) -> bool:
        """按主键更新定义字段（名称 / 描述 / 状态 / 操作人）。"""
        values.setdefault("updated_at", datetime.now())
        stmt = (
            update(TaskDefinition)
            .where(
                TaskDefinition.id == definition_id,
                TaskDefinition.is_deleted == 0,
            )
            .values(**values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def get_visible(
        self, definition_id: str, tenant_id: str
    ) -> TaskDefinition | None:
        """**读**取一个定义：系统级对所有租户可见（§4 权限规则）。"""
        stmt = select(TaskDefinition).where(
            TaskDefinition.id == definition_id,
            TaskDefinition.is_deleted == 0,
            *_visible_conditions(tenant_id),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_owned(
        self, definition_id: str, tenant_id: str
    ) -> TaskDefinition | None:
        """**写**取一个定义：带租户上下文时，系统级一律按「找不到」处理。

        「只有平台自己能改」（§4 权限表）——所以带租户的写路径一律排除 `scope='system'`。
        平台自己那一侧的写路径见 `get_system`。
        """
        stmt = select(TaskDefinition).where(
            TaskDefinition.id == definition_id,
            TaskDefinition.is_deleted == 0,
            TaskDefinition.scope == DefinitionScope.SPACE.value,
            TaskDefinition.tenant_id == tenant_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_system(self, definition_id: str) -> TaskDefinition | None:
        """取一个**系统级**定义（平台自己那一侧的读写入口，§4 权限表）。

        系统级定义无租户、无空间，所以这里按主键 + `scope='system'` 定位——
        调用方是用平台身份直接调服务（没有租户上下文），不走租户隔离那套条件。
        """
        stmt = select(TaskDefinition).where(
            TaskDefinition.id == definition_id,
            TaskDefinition.is_deleted == 0,
            TaskDefinition.scope == DefinitionScope.SYSTEM.value,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def bump_current_version(self, definition_id: str, version_no: int) -> bool:
        """发版成功后把定义的 `current_version_no` 推到新版本号。

        只按主键定位：能拿到 `definition_id` 就说明调用方已经过写权限校验
        （`get_owned` / `get_system`），这儿再叠一层租户条件反而会把系统级定义
        （`tenant_id IS NULL`）挡在外面。
        """
        stmt = (
            update(TaskDefinition)
            .where(
                TaskDefinition.id == definition_id,
                TaskDefinition.is_deleted == 0,
            )
            .values(current_version_no=version_no, updated_at=datetime.now())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def mark_deleted(self, definition_id: str) -> bool:
        """假删定义（`def.delete` 用，§4.3）。

        只标 `is_deleted=1`，不真删——已产生的 `tasks` 保留（历史），版本与触发器
        由调用方分别级联处置（见本文件 `mark_deleted_by_definition` 与
        `TaskTriggerRepository.mark_deleted_by_definition`）。同样只按主键定位。
        """
        stmt = (
            update(TaskDefinition)
            .where(
                TaskDefinition.id == definition_id,
                TaskDefinition.is_deleted == 0,
            )
            .values(is_deleted=1, updated_at=datetime.now())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


class TaskDefinitionVersionRepository(BaseRepository[TaskDefinitionVersion]):
    """任务定义版本仓储。

    查询一律只按 `definition_id` 定位（它是全局唯一主键）：权限在**定义**那一层判
    （`TaskDefinitionRepository.get_visible` / `get_owned`），能拿到 `definition_id`
    就说明已经过审；这里再叠租户条件反而会把系统级定义（`tenant_id IS NULL`）的版本
    挡在外面。
    """

    model = TaskDefinitionVersion

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_version(
        self, definition_id: str, version_no: int
    ) -> TaskDefinitionVersion | None:
        """按版本号取一版；被定义带删（`is_deleted=1`）的按「找不到」处理。

        用到版本号的地方一律要校验（§4.3）——指定的版本不存在就报错，不悄悄回落。
        """
        stmt = select(TaskDefinitionVersion).where(
            TaskDefinitionVersion.definition_id == definition_id,
            TaskDefinitionVersion.version_no == version_no,
            TaskDefinitionVersion.is_deleted == 0,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_draft(
        self, definition_id: str
    ) -> TaskDefinitionVersion | None:
        """取当前草稿（唯一索引 `uq_tc_ver_draft` 保证同一定义最多一个）。"""
        stmt = select(TaskDefinitionVersion).where(
            TaskDefinitionVersion.definition_id == definition_id,
            TaskDefinitionVersion.status == VersionStatus.DRAFT.value,
            TaskDefinitionVersion.is_deleted == 0,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest(
        self, definition_id: str
    ) -> TaskDefinitionVersion | None:
        """取**最新版本**（max(version_no)，含草稿；被定义带删的版本不算）。

        ⚠️ 与 `next_version_no` 口径不同：那里**故意不过滤** `is_deleted`（防撞唯一键），
        这里要的是「编辑者看到的最新一版」，必须排除已删版本。
        """
        stmt = (
            select(TaskDefinitionVersion)
            .where(
                TaskDefinitionVersion.definition_id == definition_id,
                TaskDefinitionVersion.is_deleted == 0,
            )
            .order_by(TaskDefinitionVersion.version_no.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_definition(
        self, definition_id: str
    ) -> list[TaskDefinitionVersion]:
        stmt = (
            select(TaskDefinitionVersion)
            .where(
                TaskDefinitionVersion.definition_id == definition_id,
                TaskDefinitionVersion.is_deleted == 0,
            )
            .order_by(TaskDefinitionVersion.version_no.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def next_version_no(self, definition_id: str) -> int:
        """下一个可用版本号 = 现有最大版本号 + 1（没有任何版本时从 1 起）。

        **故意不过滤 `is_deleted`**：唯一索引 `uq_tc_ver_no(definition_id, version_no)`
        不看假删标记，被带删的版本仍占着它的号；这里也按全量取最大，避免复用号撞唯一键。
        """
        stmt = select(func.max(TaskDefinitionVersion.version_no)).where(
            TaskDefinitionVersion.definition_id == definition_id
        )
        result = await self.session.execute(stmt)
        current = result.scalar_one_or_none() or 0
        return current + 1

    async def update_draft_config(
        self, version_id: str, config: dict
    ) -> bool:
        """改草稿编排。带 `status='draft'` 条件，已发布版本命中 0 行、改不动。

        ⚠️ **这里只按 `version_id` 定位、不叠租户条件 —— 安全性来自调用方**：与类 docstring
        同一条道理，`version_id` **必须是由已经过审的 `definition_id` 解出来的**
        （`TaskDefinitionService` 那边一律经 `_require_version(definition_id, version_no)`
        拿它，而 `definition_id` 已过 `_writable_definition`）。**别让外部把 `version_id`
        原样透传进来** —— 它是全局唯一主键，绕过定义那一层的审查就等于跨租户改别人的草稿。
        """
        stmt = (
            update(TaskDefinitionVersion)
            .where(
                TaskDefinitionVersion.id == version_id,
                TaskDefinitionVersion.status == VersionStatus.DRAFT.value,
                TaskDefinitionVersion.is_deleted == 0,
            )
            .values(config=config)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def publish_draft(self, version_id: str) -> bool:
        """草稿转已发布。唯一一次状态变化，且只能从 draft 出发（§6.6）。

        ⚠️ **同样只按 `version_id` 定位、不叠租户条件**，安全性来自调用方 —— 见
        `update_draft_config` 那条 ⚠️（`version_id` 必须由已过审的 `definition_id` 解出来）。
        """
        stmt = (
            update(TaskDefinitionVersion)
            .where(
                TaskDefinitionVersion.id == version_id,
                TaskDefinitionVersion.status == VersionStatus.DRAFT.value,
                TaskDefinitionVersion.is_deleted == 0,
            )
            .values(
                status=VersionStatus.PUBLISHED.value,
                published_at=datetime.now(),
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def mark_deleted_by_definition(self, definition_id: str) -> int:
        """把定义下**所有版本**一并假删（`def.delete` 用，§4.3）。

        版本自己不可单独删，`is_deleted` 只由这里带出来；草稿与已发布版本一视同仁，
        都随定义一起下线（定义不会被再读到，其版本自然也不该被读到）。返回改动行数。
        """
        stmt = (
            update(TaskDefinitionVersion)
            .where(
                TaskDefinitionVersion.definition_id == definition_id,
                TaskDefinitionVersion.is_deleted == 0,
            )
            .values(is_deleted=1)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount


__all__ = ["TaskDefinitionRepository", "TaskDefinitionVersionRepository"]
