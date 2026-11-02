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

from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import (
    InvalidParameterError,
    OperationNotSupportedError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import TenantContext, require_tenant

from ..dtos.catalog import TaskCatalogStepItem
from ..dtos.definition import (
    FlowConfig,
    TaskDefinitionDetail,
    TaskDefinitionItem,
    TaskDefinitionList,
    VersionDetail,
    VersionSummary,
)
from ..models import DefinitionScope, VersionStatus
from ..repository.catalog_repository import TaskCatalogStepRepository
from ..repository.definition_repository import (
    TaskDefinitionRepository,
    TaskDefinitionVersionRepository,
)
from ..repository.execution_repository import TaskRepository
from ..repository.runner_repository import RunnerDefinitionRepository
from ..repository.trigger_repository import TaskTriggerRepository
from .flow_validator import validate_flow_config
from .space_guard import require_owned_space


def empty_flow_config() -> Dict[str, Any]:
    """一版空编排：建空草稿时用它起手（§6.1 的形状，内容留白）。

    `entry` 是**单个 node_id 字符串**，空草稿里是 `None`（还没选入口）；
    一期没有 `fail_policy` —— 失败 / 超时一律 hold，不由编排声明（§6.1、§6.3）。

    ⚠️ **用类型外壳构造、不写字面量**（2026-09-30）：这份骨架的形状从此由
    `FlowConfig` 一处定义 —— 谁给编排加字段，这里自动跟上，不会再出现
    「外壳加了一列、起手骨架还是老的」那种静默分叉。
    返回的形状与从前那个字面量**逐字相同**（`input_schema={}` / `entry=None` / `steps=[]`）。
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
    ) -> TaskDefinitionDetail:
        """建定义：指明 `scope` + 名字即可（§4.3 `def.create`）。

        ⚠️ 2026-10-23 起**不再需要先选一个内置任务**：任务类型这一层已删除，
        一版定义就是一份自由编排（节点从全局步骤清单里选）。历史定义行上那份
        `task_catalog_code` 也随迁移一起丢弃。

        `scope` 与 `tenant_id` / `space_id` 必须配套（跟 DDL 的 `ck_tc_def_scope_fields`
        一个口径，这里先挡一道、给出看得懂的错）：`system` 两个都不给、`tenant` 只给租户、
        `space` 两个都给。`system` 级是**平台自己**的定义——必须有平台身份（没有租户
        上下文）才建得出来，租户侧一律拒绝。

        带 `initial_config` 就顺手建出第一个草稿；不带也行，发版前补草稿即可。
        """
        scope, tenant_id, space_id = self._resolve_scope(scope, tenant_id, space_id)
        # **在写库之前**先卡掉「不是对象」的 `initial_config`：见 `_require_config_object`
        # 的说明（不卡的话这一版草稿会带着一个读不出来的 config 永久留在库里）。
        if initial_config is not None:
            _require_config_object(initial_config)
        if space_id:
            # 空间级定义绑死一个空间，那空间得先是当前租户自己的（§四）。
            await require_owned_space(self.session, tenant_id, space_id)

        definition = await self._definitions.create(
            tenant_id=tenant_id,
            scope=scope,
            space_id=space_id,
            name=name,
            description=description,
            current_version_no=0,
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
    ) -> TaskDefinitionList:
        """分页列某租户可见的定义（§4.3 `def.list`）：系统级 + 本租户的，不含已假删的。

        `scope` 可选：传了就只看那一级。⚠️ **给错值当场拒（1001）**，不让它漏下去 ——
        `_visible_conditions` 里不认识的 `scope` 会落进「系统级 OR 本租户」那条**不筛**的
        兜底分支，调用方以为筛过了、拿到的却是**全量**（比回空列表更难查）。口径与
        `def.create` 同一道校验、以及 `task.list` 的状态筛选一致（那句「静默忽略会让
        人以为筛上了、拿到的却是全量，比报错难查」在这里同样成立）。

        分页按 §4.0 的统一约定：`page` 从 1 起、`page_size` 不设上限（想一次全拉就传大点），
        返回里一定带 `total`，**不做静默截断**。
        """
        tenant_id = require_tenant(tenant_id)
        if scope is not None and scope not in tuple(item.value for item in DefinitionScope):
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.bad_scope",
                    params={"scope": scope},
                    fallback=f"scope 只能是 system / tenant / space 之一，收到 {scope!r}",
                ),
                details={"scope": scope},
            )
        offset = max(page - 1, 0) * page_size
        rows = await self._definitions.list_visible(tenant_id, scope, offset, page_size)
        total = await self._definitions.count_visible(tenant_id, scope)
        return TaskDefinitionList(
            items=[TaskDefinitionItem(**row.to_dict()) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def get_definition(
        self, definition_id: str, tenant_id: Optional[str] = None
    ) -> TaskDefinitionDetail:
        """定义详情（带版本概览与当前草稿号）。系统级对所有租户可见（§四权限表）。"""
        definition = await self._visible_definition(definition_id, tenant_id)
        return await self._definition_detail(definition)

    async def delete_definition(
        self, definition_id: str, tenant_id: Optional[str] = None
    ) -> None:
        """删除定义：**还有没跑完的实例就拒绝**；通过后定义连带其版本、触发器一起下线
        （§4.3 `def.delete`）。

        - 挡：定义下还有 `pending` / `running` 的实例 → 拒绝（先等实例跑完 / 删实例）；
        - 过：`tasks` 保留（历史，供追溯）；`task_definition_versions` 全部假删；
          挂在定义上的触发器一并置 `deleted`，不再被扫到 / 触发。

        ⚠️ **系统级定义（`tenant_id IS NULL`）也要删得掉**（平台身份才走得到这条路，
        见 `_writable_definition`）：它底下那两摊（实例、触发器）**分属各个租户**，所以
        `definition.tenant_id` 的 `None` 一路传下去是**有意的** —— 两个仓储都按
        「`None` = 跨全部租户」处置。别改成 `tenant_id or <某个租户>`，也别在这层
        提前 `require_tenant()`：前者漏掉别的租户、后者直接把这条路堵死。
        """
        definition = await self._writable_definition(definition_id, tenant_id)
        # `definition.tenant_id` 可能是 `None`（系统级）—— 见 docstring 那条 ⚠️。
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
        await self._definitions.mark_deleted(definition.id)
        await self._versions.mark_deleted_by_definition(definition_id)
        await self._triggers.mark_deleted_by_definition(
            definition.tenant_id, definition_id
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
        self._check_version_space(definition, space_id)

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
        return VersionDetail(**row.to_dict())

    async def update_version(
        self,
        definition_id: str,
        version_no: int,
        config: Dict[str, Any],
        tenant_id: Optional[str] = None,
    ) -> VersionDetail:
        """改草稿编排。已发布版本不可改、不可逆，要改请 fork 新版本号（§6.6）。

        上面那道 `row.status != draft` 判的是**读到的快照**；真正把关的是下面
        `update_draft_config` 里 `WHERE status='draft'` 那个条件（§4.3：**`WHERE status='draft'`
        本身就是那把锁**，不是草稿就命中 0 行）。**这里必须判它的返回值**，理由见下。
        """
        _require_config_object(config)
        await self._writable_definition(definition_id, tenant_id)
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
        return VersionDetail(**fresh.to_dict())

    async def get_version(
        self, definition_id: str, version_no: int, tenant_id: Optional[str] = None
    ) -> VersionDetail:
        """取某版详情（草稿、已发布都能取；能读到定义就能读到它的版本）。"""
        await self._visible_definition(definition_id, tenant_id)
        row = await self._require_version(definition_id, version_no)
        return VersionDetail(**row.to_dict())

    async def publish_version(
        self, definition_id: str, version_no: int, tenant_id: Optional[str] = None
    ) -> VersionDetail:
        """发版：§6.5 整体校验全过才放行，然后置 published + 推进定义当前版本号。

        任何一条不过就打回草稿（版本留在 draft 不动，调用方改完再来）。
        """
        definition = await self._writable_definition(definition_id, tenant_id)
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
        return VersionDetail(**row.to_dict())

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
                    fallback=f"scope 只能是 system / tenant / space 之一，收到 {scope!r}",
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

        if scope == DefinitionScope.TENANT.value:
            if space_id:
                raise InvalidParameterError(
                    message=translate(
                        "err.task_center.def.tenant_needs_no_space",
                        fallback="租户级定义没有空间，space_id 不该给",
                    ),
                    details={"space_id": space_id},
                )
            return scope, require_tenant(tenant_id), None

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
        return dict(base.config or {})

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
