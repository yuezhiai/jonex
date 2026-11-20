#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""定义管理服务：内置清单查询 + 定义增删查 + 版本管理与发版（§4.3 `def.*`、§6.5、§6.6）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §4.3、§6.5、§6.6、§九。

两条贯穿全篇的规矩：

- **定义分三级**（§3.1）：`system` 无租户无空间、`tenant` 有租户无空间、`space` 两个都有。
  读路径 `scope='system'` 对所有租户可见（当公共模板用）；写路径则按「谁能改」分流
  （§四权限表）——带租户上下文的写只碰本租户的 `tenant` / `space` 级，`system` 级
  只有平台自己那一侧（没有租户上下文时）才动得了。
- **版本状态机只有一条正向路**：`draft → published`，没有回头路。改只有草稿能改，
  发版只有草稿能发；已发布版本的 config 永远动不了（§6.6 三条铁律）。

版本的 `scope` / `tenant_id` / `space_id` **一律跟所属定义一致**（§3.2）：这层不另收，
`create_version` 的 `space_id` 参数只用来做一致性校验——空间级必须给且必须跟定义对得上，
非空间级给了值就是参数非法。`tasks.space_id` 也据此推导（§3.5）。
"""

import copy
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import (
    InvalidParameterError,
    OperationNotSupportedError,
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import TenantContext, require_tenant

from ..dtos.catalog import TaskCatalogStepItem
from ..dtos.definition import (
    DefinitionInputSchema,
    FlowConfig,
    TaskDefinitionDetail,
    TaskDefinitionItem,
    TaskDefinitionList,
    VersionDetail,
    VersionSummary,
    upgrade_flow_config,
)
from ..models import DefinitionScope, DefinitionStatus, VersionStatus
from ..repository.catalog_repository import TaskCatalogStepRepository
from ..repository.definition_repository import (
    TaskDefinitionRepository,
    TaskDefinitionVersionRepository,
)
from ..repository.execution_repository import TaskRepository
from ..repository.kb_document_repository import document_usage
from ..repository.runner_repository import RunnerDefinitionRepository
from ..repository.trigger_repository import TaskTriggerRepository
from ..repository.user_repository import user_names_by_ids
from .flow_validator import validate_flow_config
from .space_guard import (
    require_owned_space,
    require_space_manager,
    require_space_visible,
    visible_space_ids_for,
)
from .task_lock import definition_lock

#: 「这个参数根本没传」的哨兵：与 `None`（显式清空）区分开，用在 `update_definition`
#: 这类 PATCH 语义的方法上。同 `task_trigger_service` 的既定口径。
_UNSET: Any = object()


def empty_flow_config() -> Dict[str, Any]:
    """一版空编排：建空草稿时用它起手（§6.1 的形状，内容留白）。

    2026-11-18 起是 `start/steps/end` 三段结构：开始 / 结束只定义参数、没有
    `step_code`，`entry` 已退场（入口由 `depends_on` 为空的节点推导）。

    ⚠️ **用类型外壳构造、不写字面量**：这份骨架的形状由 `FlowConfig` 一处定义 ——
    谁给编排加字段，这里自动跟上，不会再出现「外壳加了一列、起手骨架还是老的」。
    """
    return FlowConfig().dict()


def _require_config_object(config: Any) -> None:
    """`config` 必须是个**对象**（JSON object），否则 1001（§4.0：参数错 → 参数非法）。

    ── 为什么非要有这一道 ──

    `config` 落的是 `task_definition_versions.config`（`JSONB NOT NULL`）——**JSON 数组、
    字符串、数字它都照收**（不是 DDL 拦得住的）。而读侧 `VersionDetail.config` 声明的是
    `Dict[str, Any]`，于是那条路是**两副面孔**：

    - `def.version_create` / `def.version_update` 传了个数组：插入/更新"成功"，随后
      **拼 DTO 那一刻**才抛 pydantic `ValidationError`。它不是 `JonexException`，会漏到
      能力入口的兜底 `except Exception` → `_internal_error_code` 认不出类型 →
      **`InternalError`（1000 / HTTP 500）「执行失败」**，而正确答案是 1001 / 400；
      而且 1000 **不在任何一张码表里**，SDK 会把它判成「未知 → 可以原样重发」。
      （好在事务会回滚，库里不留东西。）
    - `def.create(initial_config=[...])`：那一版草稿**正常落库**（`create_definition`
      只拼 `VersionSummary`、压根不碰 `config`），此后对它调 `def.version_get`
      **恒回 1000** —— 一个**永久读不出来**的版本躺在库里。发布它倒是干净的 1001
      （`validate_flow_config` 那句「config 必须是一个对象」），所以还能靠
      `def.version_update` 改回对象自救。

    ⚠️ **判据是 `isinstance(config, dict)`，不是 `if not config`** —— 空对象 `{}` 是
    **合法**编排（`empty_flow_config` 就是它），而 `[]` / `""` / `0` 不是。
    """
    if not isinstance(config, dict):
        raise InvalidParameterError(
            message=translate(
                "err.task_center.def.config_not_object",
                params={"got": type(config).__name__},
                fallback=(
                    f"编排 config 必须是一个对象（JSON object），"
                    f"收到 {type(config).__name__}"
                ),
            ),
            details={"config_type": type(config).__name__},
        )


def _require_version_no(value: Any, field: str = "version_no") -> int:
    """版本号必须是**正整数**，否则 1001。

    ⚠️ `bool` 是 `int` 的子类，必须单独排掉 —— 不排的话 `True` 会被当成版本 1
    静默查下去。不挡类型的话 `"1"` / `1.5` 会一路带进 SQL，asyncpg 抛类型错，
    最终被兜底成「执行失败」+ 内部码；`0` / 负数则查不到行、报「版本不存在」——
    把调用方的参数错说成了事实错。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidParameterError(
            message=translate(
                "err.task_center.def.bad_version_no",
                params={"field": field, "got": repr(value)},
                fallback=f"{field} 必须是正整数（版本号从 1 起），收到 {value!r}",
            ),
            details={"field": field, "value": repr(value)},
        )
    return value


class TaskDefinitionService:
    """任务定义与版本服务。"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._definitions = TaskDefinitionRepository(session)
        self._versions = TaskDefinitionVersionRepository(session)
        self._catalog_steps = TaskCatalogStepRepository(session)
        self._runners = RunnerDefinitionRepository(session)
        self._triggers = TaskTriggerRepository(session)

    # ========================================================
    # 内置步骤清单（§4.3 def.catalog_steps）
    # ========================================================

    async def list_catalog_steps(self) -> List[TaskCatalogStepItem]:
        """列全局可选步骤：编排时「这版能用哪些节点」。

        2026-10-23 起没有「内置任务」这一层，也不再需要先选任务类型 —— 节点清单
        是全局的，按 `step_code` 唯一。
        """
        return [
            TaskCatalogStepItem(**row.to_dict())
            for row in await self._catalog_steps.list_all()
        ]

    # ========================================================
    # 定义（§4.3 def.create / def.list / def.get / def.delete）
    # ========================================================

    async def create_definition(
        self,
        scope: str,
        name: str,
        *,
        tenant_id: Optional[str] = None,
        space_id: Optional[str] = None,
        description: Optional[str] = None,
        initial_config: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> TaskDefinitionDetail:
        """建定义（需求 4）：空间级固定落 `space`，初始状态 `draft`。

        - 空间必须存在且属于本租户（`require_owned_space`）；
        - **空间管理身份**（`require_space_manager`）：`space_manager` 或租户/平台管理员；
        - 名称在同租户同空间内判重（预查给友好文案，并发由 `uq_tc_def_space_name` 兜）；
        - `created_by` 记操作人；不自动建版本（编排由需求 12 的接口保存）。

        `scope='system'` 仍保留给平台内部调用（无租户身份），前端不开放。
        """
        # 去首尾空白后判空（设计 §4.2）：`"   "` 不是合法名字（审计发现原实现放过）。
        name = (name or "").strip()
        if not name:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.empty_name",
                    params={"name": name},
                    fallback="任务定义名称去掉首尾空白后不能为空",
                ),
                details={"name_length": 0},
            )
        scope, tenant_id, space_id = self._resolve_scope(scope, tenant_id, space_id)
        # **在写库之前**先卡掉「不是对象」的 `initial_config`：见 `_require_config_object`
        # 的说明（不卡的话这一版草稿会带着一个读不出来的 config 永久留在库里）。
        if initial_config is not None:
            _require_config_object(initial_config)
        if space_id:
            # 空间级定义绑死一个空间，那空间得先是当前租户自己的（§四）。
            await require_owned_space(self.session, tenant_id, space_id)
            await require_space_manager(self.session, tenant_id, space_id, user_id)
            existing = await self._definitions.get_by_name(tenant_id, space_id, name)
            if existing is not None:
                raise ResourceAlreadyExistsError(
                    message=translate(
                        "err.task_center.def.name_exists",
                        params={"name": name, "space_id": space_id},
                        fallback=f"空间 {space_id} 里已经有同名的任务定义 {name!r}",
                    ),
                    details={"name": name, "space_id": space_id},
                )

        try:
            definition = await self._definitions.create(
                tenant_id=tenant_id,
                scope=scope,
                space_id=space_id,
                name=name,
                description=description,
                status=DefinitionStatus.DRAFT.value,
                current_version_no=0,
                created_by=user_id,
            )
        except IntegrityError:
            # 并发下另一个请求刚插了同名行：唯一索引兜底，翻译成可读的 1006。
            await self.session.rollback()
            raise ResourceAlreadyExistsError(
                message=translate(
                    "err.task_center.def.name_exists",
                    params={"name": name, "space_id": space_id},
                    fallback=f"空间 {space_id} 里已经有同名的任务定义 {name!r}",
                ),
                details={"name": name, "space_id": space_id},
            )

        draft_no: Optional[int] = None
        if initial_config is not None:
            draft = await self._create_version_row(
                definition=definition,
                config=initial_config,
            )
            draft_no = draft.version_no

        return await self._definition_detail(definition, draft_no)

    async def list_definitions(
        self,
        tenant_id: str,
        scope: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        *,
        keyword: Optional[str] = None,
        status: Optional[str] = None,
        space_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> TaskDefinitionList:
        """分页列某租户可见的定义（需求 3）。

        - **系统级必须返回**（只读），空间级按 `visible_space_ids_for` 收敛；
        - 筛选：名称模糊 `keyword`、定义状态 `status`、空间 `space_id`；
        - 返回带统计：执行次数、最近执行时间/结果、创建人名字（批量聚合，不做 N+1）。

        非法 `scope` / `status` 当场 1001 —— 静默忽略会让人以为筛上了、拿到的却是全量。
        """
        tenant_id = require_tenant(tenant_id)
        if scope is not None and scope not in tuple(item.value for item in DefinitionScope):
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.bad_scope",
                    params={"scope": scope},
                    fallback=f"scope 只能是 system / space 之一，收到 {scope!r}",
                ),
                details={"scope": scope},
            )
        if status is not None and status not in tuple(
            item.value for item in DefinitionStatus
        ):
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.bad_status",
                    params={"status": status},
                    fallback=(
                        "status 只能是 draft / published / disabled 之一，"
                        f"收到 {status!r}"
                    ),
                ),
                details={"status": status},
            )

        # 空间维度：显式指定则校验归属与可见性；不指定则收敛到可见空间集合。
        allowed_space_ids: Optional[List[str]] = None
        if space_id:
            await require_owned_space(self.session, tenant_id, space_id)
            await require_space_visible(self.session, tenant_id, space_id, user_id)
        else:
            allowed_space_ids = await visible_space_ids_for(
                self.session, tenant_id, user_id
            )

        offset = max(page - 1, 0) * page_size
        rows = await self._definitions.list_visible(
            tenant_id,
            scope,
            offset,
            page_size,
            keyword=keyword,
            status=status,
            space_id=space_id,
            allowed_space_ids=allowed_space_ids,
        )
        total = await self._definitions.count_visible(
            tenant_id,
            scope,
            keyword=keyword,
            status=status,
            space_id=space_id,
            allowed_space_ids=allowed_space_ids,
        )

        # 统计与创建人名：对**当页**的 id 批量查，各一次，不做 N+1。
        definition_ids = [row.id for row in rows]
        stats = await TaskRepository(self.session).stats_by_definition_ids(
            tenant_id, definition_ids
        )
        names = await user_names_by_ids(
            self.session, tenant_id, [row.created_by for row in rows]
        )

        items: List[TaskDefinitionItem] = []
        for row in rows:
            stat = stats.get(row.id) or {}
            last_at = stat.get("last_at")
            items.append(
                TaskDefinitionItem(
                    **row.to_dict(),
                    execution_count=stat.get("count", 0),
                    last_executed_at=last_at.isoformat() if last_at else None,
                    last_execution_result=stat.get("last_status"),
                    creator_name=names.get(row.created_by or ""),
                )
            )
        return TaskDefinitionList(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    async def update_definition(
        self,
        definition_id: str,
        *,
        name: str,
        description: Any = _UNSET,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> TaskDefinitionDetail:
        """编辑定义的名称/描述（需求 5）：名称判重**排除自己**；不改状态与空间。

        `description` 用哨兵区分三态（PATCH 惯例，与 `trig.update` 同口径）：
        - **没传**（`_UNSET`）→ 保持原值不动；
        - 显式 `null` → 清空；
        - 字符串 → 覆盖。
        默认值写成 `None` 会把「没传」静默变成「清空」——改个名字顺手清掉描述。
        """
        # 与 create 同口径的形状闸（审计发现：def.update 原来完全不校验，超长名会
        # 落成 4001 可重试、空名会直接入库）。
        name = (name or "").strip()
        if not name or len(name) > 128:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.bad_name",
                    params={"name": name},
                    fallback="任务定义名称必须是 1~128 个字符（去掉首尾空白后不能为空）",
                ),
                details={"name_length": len(name)},
            )
        if description is not _UNSET and description is not None and len(description) > 512:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.bad_description",
                    fallback="任务定义描述不能超过 512 个字符",
                ),
                details={"description_length": len(description)},
            )
        definition = await self._writable_definition(definition_id, tenant_id)
        if definition.scope == DefinitionScope.SPACE.value:
            await require_space_manager(
                self.session, definition.tenant_id, definition.space_id, user_id
            )
            duplicate = await self._definitions.get_by_name(
                definition.tenant_id, definition.space_id, name
            )
            if duplicate is not None and duplicate.id != definition_id:
                raise ResourceAlreadyExistsError(
                    message=translate(
                        "err.task_center.def.name_exists",
                        params={"name": name, "space_id": definition.space_id},
                        fallback=(
                            f"空间 {definition.space_id} 里已经有同名的任务定义 {name!r}"
                        ),
                    ),
                    details={"name": name, "space_id": definition.space_id},
                )
        fields: Dict[str, Any] = {"name": name, "updated_by": user_id}
        if description is not _UNSET:
            fields["description"] = description
        try:
            await self._definitions.update_fields(definition_id, **fields)
        except IntegrityError:
            # 预查与 UPDATE 之间的并发窗口：两个定义同时改名成同一个新名字，
            # 由部分唯一索引 `uq_tc_def_space_name` 裁决。翻译成 1006（与 create
            # 同口径），不能让裸 IntegrityError 漏成 4001「执行失败」。
            await self.session.rollback()
            raise ResourceAlreadyExistsError(
                message=translate(
                    "err.task_center.def.name_exists",
                    params={"name": name, "space_id": definition.space_id or ""},
                    fallback=f"空间里已经有同名的任务定义 {name!r}",
                ),
                details={"name": name, "space_id": definition.space_id},
            )
        fresh = await self._visible_definition(definition_id, tenant_id)
        return await self._definition_detail(fresh)

    async def get_definition(
        self,
        definition_id: str,
        tenant_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> TaskDefinitionDetail:
        """定义详情（带版本概览与当前草稿号）。系统级对所有租户可见（§四权限表）。"""
        definition = await self._visible_definition(definition_id, tenant_id)
        await self._require_read_space(definition, user_id)
        return await self._definition_detail(definition)

    async def get_input_schema(
        self,
        definition_id: str,
        tenant_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> DefinitionInputSchema:
        """**执行前置**：**最新已发布编排**的开始节点参数定义（2026-11-20 新接口）。

        前端渲染「执行」弹窗需要的字段规格（含 `file` 类型与 `required`）不再要求
        调用方两跳（详情拿版本号 → 版本详情解析 config）：这里一次给全。

        取的是**最新已发布版本**（`status=published` 里 `version_no` 最大的一版），
        与执行时 `TaskService.fire` 使用的版本口径一致；草稿不参与。
        没有已发布版本时 `input_schema` 为空对象、`version_no` 为 `None`
        （前端据此提示「先发布编排」）。读权限与 `def.get` 同口径。
        """
        definition = await self._visible_definition(definition_id, tenant_id)
        await self._require_read_space(definition, user_id)

        input_schema: Dict[str, Any] = {}
        version_no: Optional[int] = None
        rows = await self._versions.list_by_definition(definition_id)
        # `list_by_definition` 按 version_no 降序 —— 第一个 published 就是最新已发布版本
        published = next(
            (row for row in rows if row.status == VersionStatus.PUBLISHED.value),
            None,
        )
        if published is not None:
            version_no = published.version_no
            config = upgrade_flow_config(published.config or {})
            start = config.get("start") if isinstance(config, dict) else None
            inputs = start.get("inputs") if isinstance(start, dict) else None
            if isinstance(inputs, dict):
                input_schema = inputs
        return DefinitionInputSchema(
            definition_id=definition_id,
            definition_status=definition.status,
            version_no=version_no,
            input_schema=input_schema,
        )

    async def delete_definition(
        self,
        definition_id: str,
        tenant_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> None:
        """删除定义（需求 8）：**草稿 / 已停用**才可删，且还有没跑完的实例就拒绝。

        - 状态闸：`published` → 1003（先停用）；
        - 实例闸：定义下还有 `pending` / `running` → 1003（先等实例跑完 / 删实例）；
        - 通过后：定义 → 版本 → 触发器一整摊软删，`tasks` 保留（历史）。

        全程持定义锁（需求 8/12：删除与编排修改互斥）。
        """
        definition = await self._writable_definition(definition_id, tenant_id)
        if definition.scope == DefinitionScope.SPACE.value:
            await require_space_manager(
                self.session, definition.tenant_id, definition.space_id, user_id
            )
        self._require_deletable_status(definition)

        async with definition_lock(definition_id):
            # 锁内重读：别的副本可能刚发布/刚删。
            # ⚠️ 先 `expire_all()`：同一 Session 的身份映射会直接返回锁外那次读到的
            # 旧对象（2026-11-19 审计：并发 publish 后 delete 仍看到 draft，把刚发布的
            # 定义连同触发器一起删掉）。过期后重读才会真的 SELECT 库上最新状态。
            self.session.expire_all()
            definition = await self._writable_definition(definition_id, tenant_id)
            self._require_deletable_status(definition)
            pending = await TaskRepository(self.session).count_unfinished_by_definition(
                definition.tenant_id, definition_id
            )
            if pending:
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.def.has_unfinished_tasks",
                        params={"definition_id": definition_id, "count": pending},
                        fallback=f"定义 {definition_id} 还有 {pending} 个没跑完的实例，不能删",
                    ),
                    details={"definition_id": definition_id, "unfinished": pending},
                )

            # 定义 → 版本 → 触发器一整摊一起下线（§4.3）：版本自己不可单独删，`is_deleted`
            # 只由这里带出来；触发器走状态位 `deleted`，已产生的实例全部保留（历史）。
            deleted = await self._definitions.mark_deleted(definition.id)
            if not deleted:
                await self.session.rollback()
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.def.write_race_delete",
                        params={"definition_id": definition_id},
                        fallback=(
                            f"定义 {definition_id} 在删除过程中被并发改动，本次删除未生效"
                        ),
                    ),
                    details={"definition_id": definition_id},
                )
            await self._versions.mark_deleted_by_definition(definition_id)
            await self._triggers.mark_deleted_by_definition(
                definition.tenant_id, definition_id
            )
            # 定义锁覆盖到提交（审计发现：锁先释放、提交后到，并发 save 可对已删定义
            # 写入孤儿版本）。锁块内显式提交。
            await self.session.commit()

    @staticmethod
    def _require_deletable_status(definition) -> None:
        if definition.status not in (
            DefinitionStatus.DRAFT.value,
            DefinitionStatus.DISABLED.value,
        ):
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.def.delete_requires_draft_or_disabled",
                    params={
                        "definition_id": definition.id,
                        "status": definition.status,
                    },
                    fallback=(
                        f"定义 {definition.id} 当前是 {definition.status}，"
                        "只有草稿 / 已停用才能删除（已发布的先停用）"
                    ),
                ),
                details={
                    "definition_id": definition.id,
                    "status": definition.status,
                },
            )

    # ========================================================
    # 版本（§4.3 def.version_*）
    # ========================================================

    async def create_version(
        self,
        definition_id: str,
        space_id: Optional[str] = None,
        base_version_no: Optional[int] = None,
        config: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> VersionDetail:
        """开新草稿：`base_version_no` 填了就从该版复制，不填则建空草稿（§4.3）。

        `space_id` 是文档要求的参数，但版本的归属**一律跟所属定义一致**（§3.2），
        所以它只做一致性校验：空间级定义必须给、且必须与定义的那个空间相同；
        租户级 / 系统级定义本就不绑空间，给了值就按参数非法打回。

        同一定义同时只能有一个草稿（唯一索引 `uq_tc_ver_draft` 兜底），
        已经有草稿时**明确拒绝**，而不是悄悄返回旧草稿——调用方得先决定
        「发版」还是「接着改那个草稿」。
        """
        definition = await self._writable_definition(definition_id, tenant_id)
        if definition.scope == DefinitionScope.SPACE.value:
            await require_space_manager(
                self.session, definition.tenant_id, definition.space_id, user_id
            )
        self._check_version_space(definition, space_id)
        # 基准版本号同样在查库前判类型：`"1"` 会带着类型错进 SQL、`0` 会假报
        # 「版本不存在」，两者都把参数错包装成了别的东西。
        if base_version_no is not None:
            _require_version_no(base_version_no, "base_version_no")

        existing = await self._versions.get_draft(definition_id)
        if existing is not None:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.def.draft_exists",
                    params={
                        "definition_id": definition_id,
                        "version_no": existing.version_no,
                    },
                    fallback=(
                        f"定义 {definition_id} 已经有草稿（版本 {existing.version_no}），"
                        "先发版或改那个草稿，别建第二个"
                    ),
                ),
                details={
                    "definition_id": definition_id,
                    "draft_version_no": existing.version_no,
                },
            )

        # 版本号一律要校验（§4.3）：只要指定了 `base_version_no` 就先查它存不存在——
        # 绝不悄悄忽略、也不悄悄回落。
        base_config = await self._config_from_base(definition_id, base_version_no)
        if config is None:
            config = base_config

        row = await self._create_version_row(definition=definition, config=config)
        return self._version_detail(row)

    async def update_version(
        self,
        definition_id: str,
        version_no: int,
        config: Dict[str, Any],
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> VersionDetail:
        """改草稿编排。已发布版本不可改、不可逆，要改请 fork 新版本号（§6.6）。

        上面那道 `row.status != draft` 判的是**读到的快照**；真正把关的是下面
        `update_draft_config` 里 `WHERE status='draft'` 那个条件（§4.3：**`WHERE status='draft'`
        本身就是那把锁**，不是草稿就命中 0 行）。**这里必须判它的返回值**，理由见下。
        """
        _require_config_object(config)
        # 旧结构（input_schema / entry）写入前升级成 start/steps/end —— 库里从此只有
        # 新结构的历史行 + 旧结构的老行，前端/校验器不用各自再判一遍。
        config = upgrade_flow_config(config)
        definition = await self._writable_definition(definition_id, tenant_id)
        if definition.scope == DefinitionScope.SPACE.value:
            await require_space_manager(
                self.session, definition.tenant_id, definition.space_id, user_id
            )
        row = await self._require_version(definition_id, version_no)
        if row.status != VersionStatus.DRAFT.value:
            raise OperationNotSupportedError(
                message=translate(
                    "err.task_center.def.published_immutable",
                    params={"version_no": version_no},
                    fallback=(
                        f"版本 {version_no} 已发布，不可改、不可逆回草稿；"
                        "要改请从它 fork 一个新的草稿版本"
                    ),
                ),
                details={"definition_id": definition_id, "version_no": version_no},
            )

        updated = await self._versions.update_draft_config(row.id, config)
        if not updated:
            # 「读到它」和「写它」之间被别人发版了（§4.3 那张表最后一行：命中 0 行 →
            # **拒绝**，重读再来、不硬写）。
            #
            # **⚠️ 这里以前不判返回值、还顺手 `row.config = config`** —— 那行赋值把
            # ORM 对象标脏，**提交时会在 CAS 条件之外再写一次**，等于把「已发布版本
            # 不可改」这条规则整个绕过去，而且接口回的还是成功。**别再写回 `row`**。
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.def.draft_conflict",
                    params={"version_no": version_no},
                    fallback=(
                        f"版本 {version_no} 刚被别处改过（多半是发版了），这次改动没落上；"
                        "重读一次再来"
                    ),
                ),
                details={"definition_id": definition_id, "version_no": version_no},
            )
        fresh = await self._require_version(definition_id, version_no)
        return self._version_detail(fresh)

    async def list_versions(
        self,
        definition_id: str,
        tenant_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """编排版本查询（需求 11）：所有版本号+状态、最新版本号+状态、执行用版本号。"""
        definition = await self._visible_definition(definition_id, tenant_id)
        await self._require_read_space(definition, user_id)
        rows = await self._versions.list_by_definition(definition_id)
        latest = rows[0] if rows else None
        return {
            "definition_id": definition_id,
            "definition_status": definition.status,
            "current_version_no": definition.current_version_no,
            "latest_version_no": latest.version_no if latest else None,
            "latest_version_status": latest.status if latest else None,
            "versions": [
                {
                    "version_no": row.version_no,
                    "status": row.status,
                    "created_at": row.to_dict()["created_at"],
                    "published_at": row.to_dict()["published_at"],
                }
                for row in rows
            ],
        }

    async def get_version(
        self,
        definition_id: str,
        version_no: int,
        tenant_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> VersionDetail:
        """取某版详情（草稿、已发布都能取；能读到定义就能读到它的版本）。"""
        definition = await self._visible_definition(definition_id, tenant_id)
        await self._require_read_space(definition, user_id)
        row = await self._require_version(definition_id, version_no)
        return self._version_detail(row)

    async def publish_version(
        self,
        definition_id: str,
        version_no: int,
        tenant_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> VersionDetail:
        """发版：§6.5 整体校验全过才放行，然后置 published + 推进定义当前版本号。

        任何一条不过就打回草稿（版本留在 draft 不动，调用方改完再来）。
        """
        definition = await self._writable_definition(definition_id, tenant_id)
        if definition.scope == DefinitionScope.SPACE.value:
            await require_space_manager(
                self.session, definition.tenant_id, definition.space_id, user_id
            )
        row = await self._require_version(definition_id, version_no)
        if row.status != VersionStatus.DRAFT.value:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.def.not_a_draft",
                    params={"version_no": version_no},
                    fallback=f"版本 {version_no} 不是草稿，只有草稿能发版",
                ),
                details={
                    "definition_id": definition_id,
                    "version_no": version_no,
                    "status": row.status,
                },
            )

        issues = await self._validate(definition, row.config or {})
        if issues:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.publish_rejected",
                    params={"version_no": version_no, "count": len(issues)},
                    fallback=f"版本 {version_no} 没通过发版校验（{len(issues)} 条问题），已打回草稿",
                ),
                details={
                    "definition_id": definition_id,
                    "version_no": version_no,
                    "issues": issues,
                },
            )

        published = await self._versions.publish_draft(row.id)
        if not published:
            # 走到这里说明有别的副本刚把它发掉了（或并发改过），按冲突回。
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.def.publish_race",
                    params={"version_no": version_no},
                    fallback=f"版本 {version_no} 的发版没抢到（可能已被并发发布）",
                ),
                details={"definition_id": definition_id, "version_no": version_no},
            )

        await self._definitions.bump_current_version(definition_id, version_no)

        row.status = VersionStatus.PUBLISHED.value
        return self._version_detail(row)

    # ========================================================
    # 需求 6 / 7 / 12 / 13 / 14：执行、复制、编排保存、发布、停用
    # ========================================================

    async def run_definition(
        self,
        definition_id: str,
        inputs: Optional[Dict[str, Any]] = None,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        """执行定义（需求 6）：**必须先发布**，空间取定义自带的空间。

        `inputs` 按当前已发布版本的 `start.inputs` 校验（在 `TaskService.fire` 里）；
        文件变量传 `{document_id, file_name}`，转换在派活时统一做。
        """
        definition = await self._visible_definition(definition_id, tenant_id)
        if definition.status != DefinitionStatus.PUBLISHED.value:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.def.not_runnable",
                    params={"definition_id": definition_id, "status": definition.status},
                    fallback=(
                        f"定义 {definition_id} 当前是 {definition.status}，"
                        "只有已发布的任务定义才能执行"
                    ),
                ),
                details={"definition_id": definition_id, "status": definition.status},
            )
        if definition.scope != DefinitionScope.SPACE.value or not definition.space_id:
            raise OperationNotSupportedError(
                message=translate(
                    "err.task_center.def.system_not_runnable",
                    fallback="系统级定义由平台内部触发，不能从任务中心手动执行",
                ),
                details={"definition_id": definition_id},
            )
        # 执行 = **代用户触发知识库写**（运行方以服务身份跑 parse/compile，重解析、
        # 重建切片与本体图）。所以权限闸与 KB 的写动作（`_SPACE_WRITE_ACTIONS`）同口径：
        # 空间管理者或租户/平台管理员。此前只做 `require_space_visible`，空间内只读
        # member 也能对任意 KB 文档发起解析/编译（2026-11-19 审计确认的越权）。
        # 能力级 `task_center:write` 仍**刻意不登记**：它挡的是「改定义」，不是「跑定义」。
        await require_space_manager(
            self.session, definition.tenant_id, definition.space_id, user_id
        )
        # 需求 6 的前置校验（方案 §4.4 第 3 步）：按**当前已发布版本**的 parse 绑定
        # 反推这次实际要处理的文档与知识库 —— 开始节点的变量名允许用户自定义，
        # 硬编码 `document` / `knowledge_id` 会整段跳过校验（审计发现）。
        version = None
        if definition.current_version_no:
            version = await self._versions.get_version(
                definition_id, definition.current_version_no
            )
        version_config = upgrade_flow_config((version.config if version else {}) or {})
        doc_ref, knowledge_ref = self._extract_file_refs(version_config, inputs or {})
        await self._check_document_usage(definition, doc_ref, knowledge_ref)
        from .task_service import TaskService

        return await TaskService(self.session).fire(
            definition.tenant_id,
            definition_id,
            definition.space_id,
            inputs or {},
            origin="manual",
            operator_id=user_id,
        )

    @staticmethod
    def _extract_file_refs(
        config: Dict[str, Any], inputs: Dict[str, Any]
    ) -> tuple[Any, Any]:
        """从当前版本的 parse 节点绑定里反推「这次要处理的文档 / 知识库」。

        `bind_inputs` 的目标键是内置步骤的固定键（`document` / `knowledge_id`），
        值是来源表达式（`@task.inputs.<用户自定义变量名>` 或常量）。这样即使用户把
        开始变量命名成 `file` / `kb`，执行前校验也能拿到真实引用（审计发现：硬编码
        键名会让校验整段静默跳过）。
        """
        steps = config.get("steps") if isinstance(config, dict) else None
        parse = next(
            (
                step
                for step in (steps or [])
                if isinstance(step, dict) and step.get("step_code") == "parse"
            ),
            None,
        )
        bind = (parse or {}).get("bind_inputs") or {}

        def _resolve(keys: tuple[str, str]):
            for key in keys:
                expr = bind.get(key)
                if isinstance(expr, str):
                    if expr.startswith("@task.inputs."):
                        return inputs.get(expr[len("@task.inputs.") :])
                    if expr and not expr.startswith("@"):
                        return expr
                elif expr is not None:
                    return expr
            return None

        doc_ref = _resolve(("document", "document_id"))
        if doc_ref is None:
            doc_ref = inputs.get("document") or inputs.get("document_id")
        knowledge_ref = _resolve(("knowledge_id", "knowledge_base_id"))
        if knowledge_ref is None:
            knowledge_ref = inputs.get("knowledge_id") or inputs.get(
                "knowledge_base_id"
            )
        return doc_ref, knowledge_ref

    async def _check_document_usage(
        self, definition, doc_ref: Any, knowledge_id: Any
    ) -> None:
        """执行前的文件引用校验（文档存在、属于该 KB、未被在途任务接管）。"""
        if not doc_ref or not knowledge_id:
            return
        document_id = (
            doc_ref.get("document_id")
            if isinstance(doc_ref, dict)
            else str(doc_ref)
        )
        document_id = (document_id or "").strip()
        if not document_id:
            # 交给 `TaskService.fire` 的 `validate_file_inputs` 报（它给的是 N12 口径）。
            return
        usage = await document_usage(
            self.session, definition.tenant_id, str(knowledge_id), document_id
        )
        if usage is None or usage["is_deleted"]:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.task.document_not_found",
                    params={
                        "document_id": document_id,
                        "knowledge_id": knowledge_id,
                    },
                    fallback=(
                        f"文档 {document_id} 不存在，或者不属于知识库 {knowledge_id}"
                    ),
                ),
                details={
                    "document_id": document_id,
                    "knowledge_id": knowledge_id,
                },
            )
        # 跨空间执行（审计发现）：任务落在定义空间，产物却写进另一空间的库。
        # 只允许「定义空间 == 知识库空间」——同租户跨空间在任务中心没有授权模型。
        kb_space_id = usage.get("kb_space_id")
        if (
            definition.scope == DefinitionScope.SPACE.value
            and definition.space_id
            and kb_space_id
            and kb_space_id != definition.space_id
        ):
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.task.document_space_mismatch",
                    params={
                        "document_id": document_id,
                        "kb_space_id": kb_space_id,
                        "definition_space_id": definition.space_id,
                    },
                    fallback=(
                        f"文档 {document_id} 所在知识库属于空间 {kb_space_id}，"
                        f"与任务定义所在空间 {definition.space_id} 不一致"
                    ),
                ),
                details={
                    "document_id": document_id,
                    "kb_space_id": kb_space_id,
                    "definition_space_id": definition.space_id,
                },
            )
        if usage["active_task_id"]:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.task.document_taken",
                    params={
                        "document_id": document_id,
                        "task_id": usage["active_task_id"],
                    },
                    fallback=(
                        f"文档 {document_id} 正被任务 {usage['active_task_id']} 处理，"
                        "不能同时再执行一次"
                    ),
                ),
                details={
                    "document_id": document_id,
                    "task_id": usage["active_task_id"],
                },
            )

    async def copy_definition(
        self,
        definition_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> TaskDefinitionDetail:
        """复制定义（需求 7）：同空间、新定义草稿、编排与全部历史版本原样复制。

        系统级定义没有空间可落，**不可复制**（1003）。
        """
        source = await self._visible_definition(definition_id, tenant_id)
        if source.scope != DefinitionScope.SPACE.value or not source.space_id:
            raise OperationNotSupportedError(
                message=translate(
                    "err.task_center.def.system_not_copyable",
                    fallback="系统级任务定义没有所属空间，不能复制",
                ),
                details={"definition_id": definition_id},
            )
        await require_space_manager(
            self.session, source.tenant_id, source.space_id, user_id
        )

        # 名称列宽 128，源名可以合法接近上限 —— 直接拼「-副本」会超长、落成 4001
        # 可重试（审计发现）。按后缀长度预留、截断源名部分。
        def _copy_name(suffix_text: str) -> str:
            base = (source.name or "任务定义")[: max(1, 128 - len(suffix_text))]
            return f"{base}{suffix_text}"

        name = _copy_name("-副本")
        counter = 2
        while (
            await self._definitions.get_by_name(
                source.tenant_id, source.space_id, name
            )
            is not None
        ):
            if counter > 100:
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.def.copy_name_exhausted",
                        params={"name": name},
                        fallback=f"副本名称 {name} 已经用到第 100 个，请先清理",
                    ),
                    details={"name": name},
                )
            name = _copy_name(f"-副本{counter}")
            counter += 1
        # 兜底：极端情况下（源名 + 后缀）仍超列宽就明确回 1001，而不是 4001。
        if len(name) > 128:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.copy_name_too_long",
                    params={"name": name},
                    fallback="副本名称超过 128 个字符，请先缩短源定义名称",
                ),
                details={"name_length": len(name)},
            )

        copied = await self._definitions.create(
            tenant_id=source.tenant_id,
            scope=source.scope,
            space_id=source.space_id,
            name=name,
            description=source.description,
            status=DefinitionStatus.DRAFT.value,
            current_version_no=source.current_version_no,
            created_by=user_id,
        )
        for row in await self._versions.list_by_definition(definition_id):
            await self._versions.create(
                scope=row.scope,
                tenant_id=row.tenant_id,
                definition_id=copied.id,
                space_id=row.space_id,
                version_no=row.version_no,
                status=row.status,
                config=copy.deepcopy(row.config or {}),
                published_at=row.published_at,
            )
        return await self._definition_detail(copied)

    async def save_orchestration(
        self,
        definition_id: str,
        config: Dict[str, Any],
        status: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> VersionDetail:
        """编排保存（需求 12）：四分支 + 定义锁 + 发版校验。

        | 最新版本 | 本次保存为 | 动作 |
        | --- | --- | --- |
        | draft | draft | 原地改 |
        | draft | published | 原地改 → 发版 → 推进 current_version_no |
        | published | draft | 新建 version_no+1 草稿 |
        | published | published | 新建 version_no+1 **直接发布** |
        """
        _require_config_object(config)
        # 写入前统一成新结构（前端可能仍按旧结构回传；保存一次后就只剩新结构）。
        config = upgrade_flow_config(config)
        if status not in (VersionStatus.DRAFT.value, VersionStatus.PUBLISHED.value):
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.bad_version_status",
                    params={"status": status},
                    fallback=f"status 只能是 draft / published，收到 {status!r}",
                ),
                details={"status": status},
            )
        definition = await self._writable_definition(definition_id, tenant_id)
        if definition.scope == DefinitionScope.SPACE.value:
            await require_space_manager(
                self.session, definition.tenant_id, definition.space_id, user_id
            )

        async with definition_lock(definition_id):
            definition = await self._writable_definition(definition_id, tenant_id)
            # 校验用**定义所属租户**（系统级为 None；catalog/runners 是全局表）
            issues = await self._validate(definition, config)
            if issues:
                raise InvalidParameterError(
                    message=translate(
                        "err.task_center.def.orchestration_invalid",
                        params={"definition_id": definition_id, "count": len(issues)},
                        fallback=f"编排校验没通过（{len(issues)} 条问题），未保存",
                    ),
                    details={"definition_id": definition_id, "issues": issues},
                )

            latest = await self._versions.get_latest(definition_id)
            # ⚠️ 2026-11-19 审计提过「保存并发布」双击会产出两版；但**内容相同不等于
            # 重复请求** —— 用户有意「再存一版」也是同一份 config、期望版本号 +1
            # （接口全场景 C4 就是这条）。没有请求幂等键就无法区分两者，故不做内容
            # 去重；要真正幂等需前端/网关提供 idempotency-key，再按 key 落去重表。
            if latest is None or latest.status == VersionStatus.PUBLISHED.value:
                # 分支 3 / 4：新建下一版（4 是「直接发布」，不经过草稿）
                try:
                    row = await self._versions.create(
                        scope=definition.scope,
                        tenant_id=definition.tenant_id,
                        definition_id=definition.id,
                        space_id=definition.space_id,
                        version_no=await self._versions.next_version_no(definition_id),
                        status=status,
                        config=config,
                        published_at=(
                            datetime.now()
                            if status == VersionStatus.PUBLISHED.value
                            else None
                        ),
                    )
                except IntegrityError:
                    # 并发建版本/唯一草稿索引冲突：翻译成可重试的 1003，
                    # 不要让它漏成 4001「执行失败」（审计发现：重试永远不会成功）。
                    await self.session.rollback()
                    raise ResourceConflictError(
                        message=translate(
                            "err.task_center.def.version_insert_race",
                            params={"definition_id": definition_id},
                            fallback=(
                                f"定义 {definition_id} 上另一次建版本的动作刚先落库，"
                                "这次没抢到（并发冲突）；重读一次再来"
                            ),
                        ),
                        details={"definition_id": definition_id},
                    )
            else:
                # 分支 1 / 2：改最新草稿；2 再发版
                updated = await self._versions.update_draft_config(latest.id, config)
                if not updated:
                    raise ResourceConflictError(
                        message=translate(
                            "err.task_center.def.draft_conflict",
                            params={"version_no": latest.version_no},
                            fallback=f"版本 {latest.version_no} 刚被别处改过，这次改动没落上",
                        ),
                        details={"definition_id": definition_id},
                    )
                if status == VersionStatus.PUBLISHED.value:
                    published = await self._versions.publish_draft(latest.id)
                    if not published:
                        raise ResourceConflictError(
                            message=translate(
                                "err.task_center.def.publish_race",
                                params={"version_no": latest.version_no},
                                fallback=f"版本 {latest.version_no} 的发版没抢到（可能已被并发发布）",
                            ),
                            details={"definition_id": definition_id},
                        )
                row = await self._versions.get_version(
                    definition_id, latest.version_no
                )

            if status == VersionStatus.PUBLISHED.value:
                bumped = await self._definitions.bump_current_version(
                    definition_id, row.version_no
                )
                touched = await self._definitions.update_fields(
                    definition_id,
                    status=DefinitionStatus.PUBLISHED.value,
                    updated_by=user_id,
                )
            else:
                bumped = True
                touched = await self._definitions.update_fields(
                    definition_id, updated_by=user_id
                )
            if not bumped or not touched:
                # 锁内重读之后、写定义之前，定义被并发删除/改成别的状态 ——
                # 不能再回 success（那会留下「定义已删、版本还在」的孤儿，审计发现）。
                await self.session.rollback()
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.def.write_race_save",
                        params={"definition_id": definition_id},
                        fallback=(
                            f"定义 {definition_id} 在保存过程中被并发改动（可能被删除），"
                            "本次保存未生效"
                        ),
                    ),
                    details={"definition_id": definition_id},
                )
            # 定义锁必须覆盖到事务提交（审计发现：锁先释放、提交后到，
            # 并发 save/disable 会互相覆盖）。锁块内显式提交。
            await self.session.commit()
            return self._version_detail(row)

    async def publish_definition(
        self,
        definition_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> TaskDefinitionDetail:
        """发布定义（需求 13）：草稿 / 已停用 → 已发布，前提是有已发布版本。"""
        definition = await self._writable_definition(definition_id, tenant_id)
        if definition.scope == DefinitionScope.SPACE.value:
            await require_space_manager(
                self.session, definition.tenant_id, definition.space_id, user_id
            )
        async with definition_lock(definition_id):
            definition = await self._writable_definition(definition_id, tenant_id)
            if definition.status not in (
                DefinitionStatus.DRAFT.value,
                DefinitionStatus.DISABLED.value,
            ):
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.def.publish_requires_draft_or_disabled",
                        params={"definition_id": definition_id, "status": definition.status},
                        fallback=(
                            f"定义 {definition_id} 当前是 {definition.status}，"
                            "只有草稿 / 已停用才能发布"
                        ),
                    ),
                    details={"definition_id": definition_id, "status": definition.status},
                )
            versions = await self._versions.list_by_definition(definition_id)
            if not any(
                row.status == VersionStatus.PUBLISHED.value for row in versions
            ):
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.def.no_published_version",
                        params={"definition_id": definition_id},
                        fallback=(
                            f"定义 {definition_id} 还没有已发布的编排版本，"
                            "先在编制页保存为发布"
                        ),
                    ),
                    details={
                        "definition_id": definition_id,
                        "reason": "no_published_version",
                    },
                )
            touched = await self._definitions.update_fields(
                definition_id,
                status=DefinitionStatus.PUBLISHED.value,
                updated_by=user_id,
            )
            if not touched:
                await self.session.rollback()
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.def.write_race_publish",
                        params={"definition_id": definition_id},
                        fallback=(
                            f"定义 {definition_id} 在发布过程中被并发改动（可能被删除），"
                            "本次发布未生效"
                        ),
                    ),
                    details={"definition_id": definition_id},
                )
            # 定义锁覆盖到提交（审计发现：锁先释放会被并发覆盖）。
            await self.session.commit()
        fresh = await self._visible_definition(definition_id, tenant_id)
        return await self._definition_detail(fresh)

    async def disable_definition(
        self,
        definition_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> TaskDefinitionDetail:
        """停用定义（需求 14）：已发布 → 已停用。

        触发器**不改行**：在 `TaskService.fire` 的统一闸与调度扫描处跳过
        （见设计文档 §4.10 —— 改行状态后无法区分「人工暂停」与「因停用暂停」）。
        """
        definition = await self._writable_definition(definition_id, tenant_id)
        if definition.scope == DefinitionScope.SPACE.value:
            await require_space_manager(
                self.session, definition.tenant_id, definition.space_id, user_id
            )
        async with definition_lock(definition_id):
            definition = await self._writable_definition(definition_id, tenant_id)
            if definition.status != DefinitionStatus.PUBLISHED.value:
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.def.disable_requires_published",
                        params={"definition_id": definition_id, "status": definition.status},
                        fallback=(
                            f"定义 {definition_id} 当前是 {definition.status}，"
                            "只有已发布的任务定义才能停用"
                        ),
                    ),
                    details={"definition_id": definition_id, "status": definition.status},
                )
            touched = await self._definitions.update_fields(
                definition_id,
                status=DefinitionStatus.DISABLED.value,
                updated_by=user_id,
            )
            if not touched:
                await self.session.rollback()
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.def.write_race_disable",
                        params={"definition_id": definition_id},
                        fallback=(
                            f"定义 {definition_id} 在停用过程中被并发改动（可能被删除），"
                            "本次停用未生效"
                        ),
                    ),
                    details={"definition_id": definition_id},
                )
            # 定义锁覆盖到提交（审计发现：锁先释放会被并发覆盖）。
            await self.session.commit()
        fresh = await self._visible_definition(definition_id, tenant_id)
        return await self._definition_detail(fresh)

    # ========================================================
    # 内部：scope 与权限
    # ========================================================

    @staticmethod
    def _resolve_scope(
        scope: str, tenant_id: Optional[str], space_id: Optional[str]
    ) -> tuple[str, Optional[str], Optional[str]]:
        """按 `scope` 收口 `tenant_id` / `space_id`，并做平台身份校验（§3.1、§四权限表）。

        `system` 级是**平台自己**的定义：要求当前**没有租户上下文**（平台身份）——
        带着租户上下文来建、或者带租户来建，一律按越权拒绝。
        """
        if scope not in tuple(item.value for item in DefinitionScope):
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.bad_scope",
                    params={"scope": scope},
                    fallback=f"scope 只能是 system / space 之一，收到 {scope!r}",
                ),
                details={"scope": scope},
            )

        if scope == DefinitionScope.SYSTEM.value:
            if tenant_id or space_id:
                raise InvalidParameterError(
                    message=translate(
                        "err.task_center.def.system_needs_no_tenant",
                        fallback="系统级定义没有租户、也没有空间，tenant_id / space_id 都不该给",
                    ),
                    details={"tenant_id": tenant_id, "space_id": space_id},
                )
            if TenantContext.get():
                raise OperationNotSupportedError(
                    message=translate(
                        "err.task_center.def.system_platform_only",
                        fallback="系统级定义只有平台自己能建，带着租户上下文不能建",
                    ),
                    details={"scope": scope},
                )
            return scope, None, None

        # scope == space
        # ⚠️ `space_id` 缺了要**在这里挡**、报「参数非法」（1001）。以前直接 `require_tenant(space_id)`
        #    —— `None` 进去走的是那段「禁止使用默认租户」的通用判断，于是调用方少给一个
        #    `space_id`，拿到的却是一句跟租户有关的错，跟 `system` / `tenant` 两个兄弟分支
        #    （都报 1001）也不一致。`_REQUIRED_KEYS` 的口径写着「少给参数 = 调用方自己能改的事 = 1001」。
        if not space_id:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.space_needs_id",
                    fallback=(
                        "空间级定义必须给出 space_id —— 空间级是「租户 + 空间」两级都有，"
                        "缺了空间就不知道建在哪儿"
                    ),
                ),
                details={"scope": scope, "space_id": space_id},
            )
        return scope, require_tenant(tenant_id), require_tenant(space_id)

    @staticmethod
    def _check_version_space(definition, space_id: Optional[str]) -> None:
        """校验 `def.version_create` 的 `space_id` 与所属定义一致（§3.2）。

        版本的 `scope` / `tenant_id` / `space_id` 一律跟定义一致，所以这里不另收归属，
        只保证"调用方给的跟定义对得上"：空间级定义必须给且相同，其余级别不该给。
        """
        expects_space = definition.scope == DefinitionScope.SPACE.value
        given = (space_id or "").strip() or None
        if expects_space and not given:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.space_required",
                    fallback="空间级定义开新版本必须给出 space_id，且要与定义所在空间一致",
                ),
                details={"definition_id": definition.id, "space_id": space_id},
            )
        if given and given != definition.space_id:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.space_mismatch",
                    params={"definition_id": definition.id, "space_id": given},
                    fallback=(
                        f"space_id 跟定义 {definition.id} 所属的空间对不上"
                        "（版本的归属必须与定义一致）"
                    ),
                ),
                details={
                    "definition_id": definition.id,
                    "space_id": given,
                    "definition_space_id": definition.space_id,
                },
            )
        if not expects_space and given:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.nonspace_no_space",
                    params={"definition_id": definition.id, "scope": definition.scope},
                    fallback="租户级 / 系统级定义不绑空间，不该给 space_id",
                ),
                details={"definition_id": definition.id, "space_id": given},
            )

    def _caller_tenant(self, tenant_id: Optional[str]) -> Optional[str]:
        """调用方租户：显式传的优先，没传看上下文；两边都没有 = **平台身份**（None）。

        平台身份是"没有租户上下文"的那条路——只有它能碰系统级定义（§四权限表）。
        """
        return tenant_id or TenantContext.get()

    async def _require_read_space(self, definition, user_id: Optional[str]) -> None:
        """读权限的空间半边：空间级定义要求用户对该空间可见（系统级跳过）。

        能力入口已判 `task_center:read`；这里只判「这个人能不能看这个空间」。
        """
        if (
            definition.scope == DefinitionScope.SPACE.value
            and definition.space_id
        ):
            await require_space_visible(
                self.session, definition.tenant_id, definition.space_id, user_id
            )

    async def _visible_definition(self, definition_id: str, tenant_id: Optional[str]):
        """**读**取一个定义：带租户时系统级也可见；平台身份下只看系统级。"""
        caller = self._caller_tenant(tenant_id)
        if caller:
            definition = await self._definitions.get_visible(definition_id, caller)
        else:
            definition = await self._definitions.get_system(definition_id)
        if definition is None:
            raise self._not_found(definition_id)
        return definition

    async def _writable_definition(self, definition_id: str, tenant_id: Optional[str]):
        """**写**取一个定义：租户只能改自己名下的，系统级只有平台身份改得了。

        越权一律按「找不到」处理（§四：跟租户隔离同一个口径，不暴露"存在但不属于你"）。
        """
        caller = self._caller_tenant(tenant_id)
        if caller:
            definition = await self._definitions.get_owned(definition_id, caller)
        else:
            definition = await self._definitions.get_system(definition_id)
        if definition is None:
            raise self._not_found(definition_id)
        return definition

    @staticmethod
    def _not_found(definition_id: str) -> ResourceNotFoundError:
        return ResourceNotFoundError(
            message=translate(
                "err.task_center.def.not_found",
                params={"definition_id": definition_id},
                fallback=f"任务定义 {definition_id} 不存在",
            ),
            details={"definition_id": definition_id},
        )

    # ========================================================
    # 内部：取数与建行
    # ========================================================

    async def _require_version(self, definition_id: str, version_no: int):
        # 类型/正负在**查库之前**判：非整数值带进 SQL 会变成数据库类型错（内部码），
        # 而 `0` / 负数只会反复报「版本不存在」，两种都在误导调用方。
        version_no = _require_version_no(version_no)
        row = await self._versions.get_version(definition_id, version_no)
        if row is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.def.version_not_found",
                    params={"definition_id": definition_id, "version_no": version_no},
                    fallback=f"定义 {definition_id} 没有版本 {version_no}",
                ),
                details={"definition_id": definition_id, "version_no": version_no},
            )
        return row

    async def _config_from_base(
        self, definition_id: str, base_version_no: Optional[int]
    ) -> Dict[str, Any]:
        """建新草稿时的编排来源：给了基准版就复制它，没给就空编排。

        `base_version_no` 填了但那一版不存在时 `_require_version` 直接报错（§4.3）。
        """
        if base_version_no is None:
            return empty_flow_config()
        base = await self._require_version(definition_id, base_version_no)
        # 从旧结构版本 fork 时也升级：否则新草稿会把旧结构一路带下去。
        return upgrade_flow_config(dict(base.config or {}))

    async def _create_version_row(self, *, definition, config: Dict[str, Any]):
        """按所属定义建一版草稿（`scope` / `tenant_id` / `space_id` 一律跟着定义走）。

        **`create_version` 上面那个「先查有没有草稿」挡不住并发**：两个请求同时查、
        都查到没有、两条都去插 —— 最终裁决是**索引**，不是那次查（§4.3 `def.version_create`
        的「并发与幂等」就是这么写的）。
        这里撞的可能是两道索引中的任意一道，两道都指向同一件事「同一定义上另一次建版本
        动作刚先落库了」：

        - `uq_tc_ver_draft`（部分唯一索引，`definition_id WHERE status='draft'`）
        - `uq_tc_ver_no`（`next_version_no` 取的是 `MAX+1`，并发下两条算出同一个号）

        所以这里只用**一句**文案兜两道，不去分辨是哪一道 —— 分辨出来对调用方也没有
        区别（要做的事都是"重读一次再来"）；**不去猜**是因为 PostgreSQL 报哪一道
        不确定，硬猜会给出一个时对时错的诊断。不兜的话裸 `IntegrityError` 会漏到能力
        入口的兜底 `except`，变成「执行失败」+ 一段带约束名/表名的内部文案（§4.0）。
        """
        # **写侧唯一的一道 type 闸**（`create_definition` / `create_version` 两条路都经过
        # 这里，`update_version` 另有一道）。放在这里而不是 DTO 上：DTO 那层管不到
        # `def.create` 的 `initial_config`（它不在 `VersionCreate` 里），而且「值对不对」
        # 本来归服务层。见 `_require_config_object`。
        _require_config_object(config)
        # 起手草稿一律写新结构（`def.create(initial_config=...)` 的调用方可能还在传旧结构）。
        config = upgrade_flow_config(config)
        version_no = await self._versions.next_version_no(definition.id)
        try:
            return await self._versions.create(
                scope=definition.scope,
                tenant_id=definition.tenant_id,
                definition_id=definition.id,
                space_id=definition.space_id,
                version_no=version_no,
                status=VersionStatus.DRAFT.value,
                config=config,
            )
        except IntegrityError:
            await self.session.rollback()
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.def.version_insert_race",
                    params={"definition_id": definition.id},
                    fallback=(
                        f"定义 {definition.id} 上另一次建版本的动作刚先落库，这次没抢到"
                        "（并发冲突）；重读一次再来"
                    ),
                ),
                details={"definition_id": definition.id, "version_no": version_no},
            )

    async def _validate(self, definition, config: Dict[str, Any]) -> List[str]:
        """发版前的 §6.5 整体校验（交给校验器，服务层只负责把料凑齐）。"""
        catalog_steps = await self._catalog_steps.map_by_step_code()
        runners = await self._runners.map_by_code()
        return validate_flow_config(
            config,
            catalog_steps=catalog_steps,
            runners=runners,
        )

    @staticmethod
    def _version_detail(row) -> VersionDetail:
        """版本行的对外 DTO：**config 一律规范化成新结构**（`start/steps/end`）。

        旧已发布版本（v1/v2）在库里仍是 `input_schema/entry` 结构 —— 前端编排编辑器
        只认新结构，所以读接口必须在这里升级；写入口（save_orchestration 等）写入前
        也会升级，库里从此只有新结构的历史行 + 旧结构的老行。
        """
        data = row.to_dict()
        data["config"] = upgrade_flow_config(data.get("config") or {})
        return VersionDetail(**data)

    async def _definition_detail(
        self, definition, draft_version_no: Optional[int] = None
    ) -> TaskDefinitionDetail:
        rows = await self._versions.list_by_definition(definition.id)
        if draft_version_no is None:
            for row in rows:
                if row.status == VersionStatus.DRAFT.value:
                    draft_version_no = row.version_no
                    break
        return TaskDefinitionDetail(
            definition=TaskDefinitionItem(**definition.to_dict()),
            space_id=definition.space_id,
            versions=[
                VersionSummary(
                    version_no=row.version_no,
                    scope=row.scope,
                    space_id=row.space_id,
                    status=row.status,
                    published_at=row.to_dict()["published_at"],
                    created_at=row.to_dict()["created_at"],
                )
                for row in rows
            ],
            draft_version_no=draft_version_no,
        )


__all__ = ["TaskDefinitionService", "empty_flow_config"]
