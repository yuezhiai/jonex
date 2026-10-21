#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""触发服务（§4.3 `trig.*`、§3.3）。

「什么时候、用什么参数把任务跑起来」这一摊。几条要守的线：

- **触发器只等于定时**（§3.3）：`manual` 类型已砍、`trigger_type` 这一列整个删掉，
  所以**这里没有类型可分** —— 想「手动跑一次」走 `task.fire`（不经触发器、
  `tasks.trigger_id` 记 NULL），不该建触发器。**定时的扫库与抢占不在这里** ——
  那是后台调度循环的事（§7.5 ⓪，见 `scheduler_service`）；本服务管的是「配置侧」的
  建 / 改 / 查 / 暂停 / 恢复，外加触发这个动作本身。
- **`cron_expr` 的解析与 `next_fire_at` 的计算全在 `cron.py`**（规格见 §3.3.2 ①~⑦），
  **四处都调同一个函数**：`trig.create` 建首个、`trig.update` 改了 cron 字段时重算、
  `trig.resume` 恢复时重算、每次触发抢占后推下一次（最后那处在调度循环里）。
  这里只管前两处 + `resume`。
- **触发器建在某个空间下**（`space_id` 必填，§3.3）：它触发出来的任务就落那个空间，
  所以 `trig.fire` 把触发器自己的 `space_id` 原样交给 `fire`——「按这个预设入口跑一次」。
  也是它能不能引用某个定义的判据（`scope=space` 的定义只认自己那个空间）。
- **`space_id` 建好就绑死**：`trig.update` 改不了它（§4.3 M5）—— 换空间等于换了一个
  触发器，该**删了重建**。这个字段仍然出现在入参里，是为了**让服务层能察觉「调用方
  试图改」**：传了不一样的值就明确报错（**不静默忽略**），传了但跟原来一样当没传。
- **`status` 同样不从 `trig.update` 改**（同一条 M5 的入口唯一原则）：改状态只有
  `trig.pause` / `trig.resume` 两个入口 —— `resume` 还要按「现在」重算 `next_fire_at`、
  算不出得拒绝（M15②），那是「只改字段」的 `update` 做不到的。传了不一样的值报错。
- **触发只是「把入参交给 fire」**，本身不碰执行状态。真正生成任务、建子任务树的是
  `TaskService.fire`，这里只负责挑版本、备入参。
- **`last_fired_at` 不归这里写**（§3.3 / §4.3 P7）：它的语义是「上一次**到点处理**的
  时间」，**只由 §7.5 ⓪ 的抢占 CAS 写**（`misfire_policy=skip` 那次没起任务也照刷）。
  所以 `trig.fire` **碰都不碰它**，也**不改 `next_fire_at`** —— 手动触发只是"临时补跑
  一次"，不能把定时计划打乱。要"上次真正触发任务的时间"，看 `trig.runs` 最后一条。

删除走状态位（`status='deleted'`），不真删；唯一索引是 `WHERE status <> 'deleted'`
的部分索引，所以「删掉触发名再建同名」是允许的。

---

**两个曾经的缺口（2026-09-29 审查查出 → 2026-10-09 已补，留档说明「为什么当初要写成这样」）**：

**① 「触发器配得比定义发版早」→ 它会永远响不起来，而且没人告诉你。**（**已补**）
建触发器**不拦**「这个定义一版都没发过」（`_normalize_inputs` 挑不出可用版本时只跳过
收口，原样存下），这是**刻意的**（§3.3：允许先把触发器配好、定义随后再发版）。但后面
那条链**从前没有任何熔断**：到点 → 抢占成功 → `fire` 按锁定版本再收一次口 → 缺必填直接
抛 1001 → `fire_trigger` 只记一条 warning，而抢占 CAS **已经提交、点已经推走** ——
于是 `last_fired_at` 照常刷新、`trig.runs` 永远为空，用户看「上次触发时间」一直在动、
实际一条任务都没起过。
**现在**：`_fire_one` 里那一档接了**现成**的那套熔断（`pause_on_cron_failure` 置 `paused`
+ 告警），判据是「异常码属于配置/数据类（1001 参数、1002 找不到）」—— 那类失败重试一万次
还是同一句话；其它异常按瞬态处置、**不暂停**（暂停一个健康触发器代价更大）。
⚠️ **没有做成「连续 N 次才暂停」**：本表没有失败计数器列，做它要加列 + 迁移；
按「配置类」判已经能精确命中这一档，且不会误伤瞬态。

**② `trig.update(inputs=...)` 在「锁定版本查不到」时会把整份入参静默清成 `{}`。**（**已补**）
`_normalize_inputs` 里从前是 `schema = (version.config... if version else None) or {}` ——
`version` 为 `None` 时 schema 成了空表，`normalize_by_schema` 于是「什么都不装、也不报
issue」，返回空 dict，调用方看到的是 `issues=[]`（**看起来成功了**）。
前提是那一行 `fixed_version` 指的版本查不到（`_resolve_version_no` **刻意**对这类脏数据
宽容，理由见那里）。
**现在**：`version is None` 时**直接拒绝这次修改**（`InvalidParameterError`），不再静默清空。
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..dtos.execution import TaskList
from ..dtos.trigger import TaskTriggerCreate, TaskTriggerItem, TaskTriggerList
from ..models import (
    DefinitionScope,
    MisfirePolicy,
    TriggerStatus,
    VersionStatus,
)
from ..repository.definition_repository import (
    TaskDefinitionRepository,
    TaskDefinitionVersionRepository,
)
from ..repository.trigger_repository import TaskTriggerRepository
from .cron import CronExprError, compute_next_fire_at
from .flow_validator import normalize_by_schema
from .space_guard import require_owned_space
from .task_service import TaskService

logger = logging.getLogger(__name__)

# 改 `next_fire_at` 走的是带 `state_version` 条件的更新（§4.3 M5b）：命中 0 行说明
# 抢占刚推过后把版本号 +1 了，**重读再来**——最多试这么多次，还不行就按冲突回。
# 次数来自配置（`TASK_CENTER_CAS_ATTEMPTS`，默认 3）；`get_config()` 是缓存单例，
# 模块级读一次跟调用时读等价。
_CAS_ATTEMPTS = get_config().TASK_CENTER_CAS_ATTEMPTS

# 「触发器状态能筛哪些值」—— `TriggerStatus` 的**全部取值**（**含 `deleted`**）：
# §4.3 `trig.list` 明写「默认不含已删的，要看已删的得**显式传 `status='deleted'`**」，
# 所以已删是个**合法的筛选值**，别把它当非法值拦掉。
_TRIGGER_STATUS_VALUES: tuple = tuple(status.value for status in TriggerStatus)


def _require_trigger_status_filter(status: Optional[str]) -> Optional[str]:
    """校验 `trig.list` 的 `status` 筛选值；没传就是 `None`（= 仓储层默认口径）。

    值不在册的**直接拒（1001）** —— 与 `task.list` 的 `status` 对齐
    （`TaskService._normalize_status_filter` 同款；那边非法值也是当场 1001）。
    **为什么非拒不可**：等值筛不中会**静默回空列表**，调用方看到的是一句
    「这个空间没有触发器」的谎话，比报错难查得多。

    ⚠️ **只管 `status`，不管 `definition_id`**（§4.3 `trig.list` 的 ⚠️ 写着这层差异）：
    定义 id 是自由输入、不是枚举，拼错与「这个定义确实没有触发器」在等值筛下
    本就分不开；要分开就得每次多查一次定义表，而「定义已删、触发器还在」还要额外
    定一条口径 —— 收益不抵成本，所以留着不校验。
    """
    if status is None:
        return None
    value = str(status).strip()
    if value not in _TRIGGER_STATUS_VALUES:
        raise InvalidParameterError(
            message=translate(
                "err.task_center.trigger.bad_filter_status",
                params={
                    "status": value,
                    "allowed": "/".join(_TRIGGER_STATUS_VALUES),
                },
                fallback=(
                    f"触发器状态只能是 {'/'.join(_TRIGGER_STATUS_VALUES)}，收到 {value}"
                ),
            ),
            details={"status": value},
        )
    return value


class TaskTriggerService:
    """触发服务：建 / 改 / 删 / 手动触发一次 / 查触发产生的实例。"""

    def __init__(
        self, session: AsyncSession, *, task_service: Optional[TaskService] = None
    ):
        self.session = session
        self._triggers = TaskTriggerRepository(session)
        self._definitions = TaskDefinitionRepository(session)
        self._versions = TaskDefinitionVersionRepository(session)
        self._task_service = task_service or TaskService(session)

    # ========================================================
    # 增删改（§4.3 trig.create / update / delete）
    # ========================================================

    async def create_trigger(
        self,
        tenant_id: str,
        space_id: str,
        payload: TaskTriggerCreate,
    ) -> TaskTriggerItem:
        """建触发：卡住版本口径 / 定义可见性 / cron 字段配套，再把入参收口一份。

        **版本只有一条口径**（§3.3）：`fixed_version` **填了** → 那版必须**存在且已发布**；
        **没填** → 用最新已发布版。没有 `version_mode` 那一列了（它跟"填没填号"说的是同一件
        事，却额外养出「`latest` 却填了号」这种要单独检查的矛盾），所以这里也就没有
        「参数矛盾」这一类错误。

        **触发器只等于定时**，所以这里没有"类型"可卡：`cron_expr` 与 `misfire_policy`
        是**必填**，必须**同时**给（M9 / M10），且表达式**语法合法**（M9b）、
        **从"现在"往后 9 年内算得出下一次**（M9c）—— 永远不匹配的表达式
        （如 `0 0 30 2 *`）等于建了个不会响的闹钟，当场打回。
        算出来的第一个 `next_fire_at` 随插入一起落库（**秒为 `00`、严格大于现在**）。
        """
        tenant_id = require_tenant(tenant_id)
        space_id = require_tenant(space_id)
        # 空间得先是当前租户自己的，才谈得上「这个空间下引用哪个定义」（§四）。
        await require_owned_space(self.session, tenant_id, space_id)
        await self._require_definition(tenant_id, space_id, payload.definition_id)
        cron_expr, misfire_policy, first_fire_at = self._resolve_cron_fields(
            payload.cron_expr, payload.misfire_policy
        )
        await self._require_fixed_version(payload.definition_id, payload.fixed_version)

        existing = await self._triggers.get_by_trigger_key(
            tenant_id, space_id, payload.trigger_key
        )
        if existing is not None:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.trigger.dup_key",
                    params={"trigger_key": payload.trigger_key},
                    fallback=f"这个空间下触发名 {payload.trigger_key} 已经用过了",
                ),
                details={"trigger_key": payload.trigger_key, "space_id": space_id},
            )

        inputs = await self._normalize_inputs(
            tenant_id,
            payload.definition_id,
            payload.fixed_version,
            payload.inputs or {},
        )
        try:
            row = await self._triggers.create(
                tenant_id=tenant_id,
                space_id=space_id,
                definition_id=payload.definition_id,
                trigger_key=payload.trigger_key,
                name=payload.name,
                status=TriggerStatus.ACTIVE.value,
                fixed_version=payload.fixed_version,
                inputs=inputs,
                cron_expr=cron_expr,
                misfire_policy=misfire_policy,
                next_fire_at=first_fire_at,
                state_version=0,
            )
        except IntegrityError:
            # **上面那个"先查重、再插入"挡不住并发**：两个请求同时查、都查到没有、都去插
            # —— 只有一个能成，另一个撞 `uq_tc_trigger_key`（`WHERE status <> 'deleted'`
            # 的部分唯一索引；§4.3 `trig.create` 的「并发与幂等」就是按这个口径写的）。
            # **这道索引才是最终裁决**，事前的查只是让常见情形有个好文案。
            #
            # 撞了就回**跟上面一模一样**的那个答复（同一个 `code`、同一句文案）：
            # 调用方看到的只有一种结果，不必知道自己是撞在了查那一步还是插那一步。
            # 不兜的话，裸 `IntegrityError` 不是 `JonexException`，会一路漏到能力入口的
            # 兜底 `except`，变成「执行失败」+ 一段带约束名/表名的内部文案（§4.0 要求
            # 内部细节不外传）；而它本质上是「你重名了」这种**调用方自己就能改**的事。
            await self.session.rollback()
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.trigger.dup_key",
                    params={"trigger_key": payload.trigger_key},
                    fallback=f"这个空间下触发名 {payload.trigger_key} 已经用过了",
                ),
                details={"trigger_key": payload.trigger_key, "space_id": space_id},
            )
        return TaskTriggerItem(**row.to_dict())

    async def list_triggers(
        self,
        tenant_id: str,
        space_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> TaskTriggerList:
        """分页列触发（§4.3 `trig.list`）：`space_id` 不传就列该租户所有空间的。

        `space_id` 给了就校验归属（§四）——不给别的租户的空间 ID 当查询条件的余地，
        否则「有没有这个空间」会从返回空列表里漏出去。

        **`status` 给了就校验合法性**（`_require_trigger_status_filter`，非法值当场
        1001）—— 跟 `task.list` 的 `status` 一个口径。**`definition_id` 仍是纯等值筛、
        不校验**，两处差异的缘由见那个函数与 §4.3。

        分页按 §4.0 的统一约定：`page` 从 1 起、`page_size` 不设上限，返回里带 `total`，
        **不做静默截断**。删除走状态位（`status='deleted'`），仓储层默认把已删的排除掉。
        """
        tenant_id = require_tenant(tenant_id)
        status = _require_trigger_status_filter(status)
        if space_id:
            await require_owned_space(self.session, tenant_id, space_id)
        rows = await self._triggers.list_triggers(
            tenant_id,
            space_id,
            definition_id,
            status,
            offset=max(page - 1, 0) * page_size,
            limit=page_size,
        )
        total = await self._triggers.count_triggers(
            tenant_id, space_id, definition_id, status
        )
        return TaskTriggerList(
            items=[TaskTriggerItem(**row.to_dict()) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def get_trigger(
        self, trigger_id: str, tenant_id: Optional[str] = None
    ) -> TaskTriggerItem:
        tenant_id = require_tenant(tenant_id)
        row = await self._require_trigger(tenant_id, trigger_id)
        return TaskTriggerItem(**row.to_dict())

    async def update_trigger(
        self,
        trigger_id: str,
        tenant_id: Optional[str] = None,
        **fields: Any,
    ) -> TaskTriggerItem:
        """改触发。只改给到的字段，没给的不动。

        **`space_id` 改不了**（M5）：传了**不一样**的值就**明确报错** —— 静默忽略会让
        调用方以为改成了；传了跟原来一样的当没传。触发器建好就绑死那个空间，要换就删了
        重建。

        **`fixed_version` 是三态**（§4.3 `trig.update`）：不传 = 不动；传号 = 钉住那一版
        （要求它**真实存在且已发布**，同建触发）；**传 `null` = 清空**（切回「用最新已发布
        版」）。第三态必须有——否则「钉了某一版之后想改回跟着最新走」这条路是堵死的，
        只能删了重建。

        **cron 字段（`cron_expr` / `misfire_policy`）要改就两个一起给**：只给一个 →
        明确报错，**不拿库里的旧值顶上**（那等于替调用方猜一条他没声明的策略）。一起给了
        就重算 `next_fire_at`（M5b）：这条更新走 `update_fields_cas`、**带 `state_version`
        条件** —— 不带的话，一次「读旧值 → 算新值 → 写回」会把抢占刚推后的值**覆盖回旧值**，
        于是同一时刻重复触发。命中 0 行说明抢占刚改过 → **重读再来**，最多 `_CAS_ATTEMPTS` 次。
        """
        tenant_id = require_tenant(tenant_id)
        row = await self._require_trigger(tenant_id, trigger_id)
        self._reject_immutable(row, fields)

        values: Dict[str, Any] = {}
        if fields.get("trigger_key") is not None:
            new_key = fields["trigger_key"]
            clash = await self._triggers.get_by_trigger_key(
                tenant_id, row.space_id, new_key
            )
            if clash is not None and clash.id != row.id:
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.trigger.dup_key",
                        params={"trigger_key": new_key},
                        fallback=f"这个空间下触发名 {new_key} 已经用过了",
                    ),
                    details={"trigger_key": new_key, "space_id": row.space_id},
                )
            values["trigger_key"] = new_key
        if fields.get("name") is not None:
            values["name"] = fields["name"]

        # ⚠️ `fixed_version` 的判据是「**键在不在**」，不是「值是不是 `None`」——
        # 它是本方法里**唯一**这么判的字段，因为**只有它的 `None` 是有意义的取值**：
        #   · 报文里没有这个键   → 不动（沿用库里的值）
        #   · 报文里显式给 `null` → **清空**（切回「用最新已发布版」）
        # 别的字段（`name` / `inputs` / `cron_expr` …）的 `None` 不是合法取值，给了就是
        # 「没改」，所以一律按 `is not None` 判。
        # 之所以分得开：`fields` 来自 pydantic v1 的 `dict(exclude_unset=True)`
        # （见 `capability.py` 里 `trig.update` 的 dispatch）—— **别改成手拼 `d.get(...)`**，
        # 那会把「没传」与「传 null」折叠成同一支，清空这条路就没了。
        if "fixed_version" in fields:
            fixed_version = fields["fixed_version"]
            await self._require_fixed_version(row.definition_id, fixed_version)
            values["fixed_version"] = fixed_version
        else:
            fixed_version = row.fixed_version  # 不动（下面归一化 inputs 时要用）

        if fields.get("inputs") is not None:
            values["inputs"] = await self._normalize_inputs(
                tenant_id, row.definition_id, fixed_version, fields["inputs"]
            )

        # cron 字段（§4.3 M5b）：**两个必须一起给**。
        # **绝不拿库里的旧值顶上** —— 那是替调用方猜一条他这轮没声明的策略；文档的
        # 「所有可能」表把这一格钉死成「拒绝」：`cron_expr` 有、`misfire_policy` 没给
        # （或反之）→ `success=false` + 非 0 `code`。两个都不给才算「没碰 cron」。
        # ⚠️ **判据是「键在不在」，不是「值是不是 `None`」**（2026-10-13 改，与上面
        # `fixed_version` 同款）：报文里**显式给 `null`** 从前被当成「没改」而静默 no-op
        # （回 `code=0`，调用方以为生效了），但它其实与「只给一个 cron 字段」是同一类
        # 输入、结局却一个报错一个静默成功。改判「键在不在」之后，`cron_expr: null`
        # 会落进下面那支 `cron_fields_incomplete`，**明确告诉调用方「要改就得两个一起给」**。
        # （`cron_expr` / `misfire_policy` 的取值都用 `ge` 挡住了 `0` / 空，`None` 不是合法值。）
        has_expr = "cron_expr" in fields
        has_policy = "misfire_policy" in fields
        if has_expr or has_policy:
            if not (has_expr and has_policy):
                raise InvalidParameterError(
                    message=translate(
                        "err.task_center.trigger.cron_fields_incomplete",
                        # 模板里没有占位符，不传 params（缺哪个在下面的 `details` 里）。
                        fallback=(
                            "改 cron 字段必须 cron_expr 和 misfire_policy 一起给 —— "
                            "只给一个就不知道「到点时发现错过了怎么办」，"
                            "管理方不替调用方猜"
                        ),
                    ),
                    details={
                        "cron_expr": fields.get("cron_expr"),
                        "misfire_policy": fields.get("misfire_policy"),
                    },
                )
            return await self._update_with_cron(
                tenant_id,
                trigger_id,
                row,
                fields["cron_expr"],
                fields["misfire_policy"],
                values,
            )

        if not values:
            return TaskTriggerItem(**row.to_dict())
        try:
            updated = await self._triggers.update_fields(tenant_id, trigger_id, **values)
        except IntegrityError:
            # 改名那条路跟 `create_trigger` 撞的是**同一个**唯一索引，同样是「先查重、
            # 再改名」挡不住并发（改的不是 INSERT 但裁决的还是那道索引）。**只在这轮确实
            # 改过 `trigger_key` 时才认领**：`task_triggers` 上没有别的唯一约束，认领不到
            # 就原样抛出去，别把真正的内部错误说成"你重名了"。
            if "trigger_key" not in values:
                raise
            await self.session.rollback()
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.trigger.dup_key",
                    params={"trigger_key": values["trigger_key"]},
                    fallback=f"这个空间下触发名 {values['trigger_key']} 已经用过了",
                ),
                details={
                    "trigger_key": values["trigger_key"],
                    "space_id": row.space_id,
                },
            )
        if updated is None:
            # `_require_trigger` 刚确认它存在且没被删，所以只可能是并发删除。
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.trigger.update_race",
                    params={"trigger_id": trigger_id},
                    fallback=f"触发 {trigger_id} 的状态刚被别处改过，本次修改没生效",
                ),
                details={"trigger_id": trigger_id},
            )
        return TaskTriggerItem(**updated.to_dict())

    async def _update_with_cron(
        self,
        tenant_id: str,
        trigger_id: str,
        row,
        cron_expr: Optional[str],
        misfire_policy: Optional[str],
        values: Dict[str, Any],
    ) -> TaskTriggerItem:
        """改了 cron 字段时的落库：重算 `next_fire_at` + **带 `state_version`** 更新。

        **为什么不能只算一次**：重算的基准是"现在"，而抢占会同时推进 `next_fire_at`
        与 `state_version`。命中 0 行 = 期间被抢过 → 重读那一行（拿到新的 `state_version`）
        再算再写。试满 `_CAS_ATTEMPTS` 次仍撞车就报冲突 —— 让调用方重来，
        **绝不能退化成不带条件的更新**（那正是 M5b 要防的覆盖）。
        """
        for _ in range(_CAS_ATTEMPTS):
            expr, policy, fire_at = self._resolve_cron_fields(
                cron_expr, misfire_policy
            )
            attempt_values = dict(values)
            attempt_values["cron_expr"] = expr
            attempt_values["misfire_policy"] = policy
            attempt_values["next_fire_at"] = fire_at
            try:
                updated = await self._triggers.update_fields_cas(
                    tenant_id,
                    trigger_id,
                    expect_version=row.state_version,
                    **attempt_values,
                )
            except IntegrityError:
                # 这条路上也可能捎带改名（`values` 里带着 `trigger_key` 时），撞的还是
                # `uq_tc_trigger_key`。**不能继续循环重试** —— 撞了唯一索引事务已经废了，
                # 重试只会拿到一串没意义的错；直接按「重名」答复。没改名就原样抛。
                if "trigger_key" not in attempt_values:
                    raise
                await self.session.rollback()
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.trigger.dup_key",
                        params={"trigger_key": attempt_values["trigger_key"]},
                        fallback=(
                            f"这个空间下触发名 {attempt_values['trigger_key']} 已经用过了"
                        ),
                    ),
                    details={
                        "trigger_key": attempt_values["trigger_key"],
                        "space_id": row.space_id,
                    },
                )
            if updated is not None:
                return TaskTriggerItem(**updated.to_dict())
            # 抢占刚推过后把 state_version +1 了 —— 重读拿到新版本号再算一遍。
            row = await self._require_trigger(tenant_id, trigger_id)
        raise ResourceConflictError(
            message=translate(
                "err.task_center.trigger.update_race",
                params={"trigger_id": trigger_id},
                fallback=f"触发 {trigger_id} 的状态刚被别处改过，本次修改没生效",
            ),
            details={"trigger_id": trigger_id, "attempts": _CAS_ATTEMPTS},
        )

    @staticmethod
    def _reject_immutable(row, fields: Dict[str, Any]) -> None:
        """`space_id` / `status` 改不了（§4.3 M5）。

        **传了不一样的值必须明确报错，不静默忽略** —— 调用方以为「改成功了」而库里
        还是老值，是最难查的一类问题。传了跟原来一样当没传（幂等重放友好）。

        ⚠️ **这两个字段的「拒绝」全靠它们先出现在 `TaskTriggerUpdate` 上**：那个 DTO
        是 `extra = "ignore"`，报文里没有对应字段的键会被 pydantic 直接丢掉，走到这里
        时早就没了 —— 于是变成**静默无效还回 `code=0`**。加字段是这两条拒绝的**前提**，
        不是「顺手也校验一下」（详见 `dtos/trigger.py` 的类 docstring）。

        `status` 单独列在这儿而不是靠 `trig.pause` / `trig.resume` 兜，是因为
        「传了 `status` 却被告知成功」比「报错说不该这么改」坏得多 —— 后者调用方当场
        就知道要走哪个接口。
        """
        for name, current in (("space_id", row.space_id), ("status", row.status)):
            given = fields.get(name)
            if given is None or given == current:
                continue
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.trigger.immutable_field",
                    params={"field": name, "current": current, "given": given},
                    fallback=(
                        f"{name} 建好之后不能改（当前是 {current}，传的是 {given}）——"
                        "要换就删了重建"
                    ),
                ),
                details={"field": name, "current": current, "given": given},
            )

    async def delete_trigger(self, trigger_id: str, tenant_id: Optional[str] = None) -> None:
        """删触发：置状态位 `deleted`，行原样留着（它产生过的实例还要能溯源）。

        **幂等成功**（§4.3 `trig.delete` 的「已经删过了」那一格）：第二次调**不做任何
        改动**、照回 `code=0`。所以这里**不能走 `_require_trigger`** —— 它底下是
        `get_trigger`，已删的当不存在，第二次会抛「找不到」，正好跟文档相反。

        **「已删」与「根本没有」必须分开**：前者幂等成功；后者（不存在 / 越权）按
        「找不到」处理 —— 那是另一回事，不能一并当成功悄悄咽掉，否则越权探测就分不出
        「这个 id 存在但你无权」和「这个 id 不存在」了。
        """
        tenant_id = require_tenant(tenant_id)
        row = await self._triggers.get_trigger_any(tenant_id, trigger_id)
        if row is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.trigger.not_found",
                    params={"trigger_id": trigger_id},
                    fallback=f"触发 {trigger_id} 不存在",
                ),
                details={"trigger_id": trigger_id},
            )
        if row.status == TriggerStatus.DELETED.value:
            return  # 已经删过了：幂等成功、不做任何改动（§4.3）
        marked = await self._triggers.mark_deleted(tenant_id, trigger_id)
        if not marked:
            # 只可能是「读完之后、删下去之前被别的副本删了」—— 落库结果是同一个终态，
            # 按同一条幂等语义当成功，不必报冲突。
            logger.info(
                "[TaskCenter] 触发 %s 已被别处删除，本次删除按幂等成功处理", trigger_id
            )

    # ========================================================
    # 手动触发一次（§4.3 trig.fire）
    # ========================================================

    async def fire_trigger(
        self,
        trigger_id: str,
        tenant_id: Optional[str] = None,
        *,
        # `origin` 是**日志标签**，不是触发器类型（`trigger_type` 这一列已经不存在了）：
        # 它答的是「这次是谁扣的扳机」，一个字节都不落库。裸字面量 `"manual"` 就是它的
        # 默认值 —— 别拿它去反推「这是个手动触发器」。
        origin: str = "manual",
    ) -> Dict[str, Any]:
        """触发一次：按触发器上钉的版本挑版本（没钉就用最新已发布版）、带上它的入参起一个任务。

        任务落在**触发器自己的空间**下（§4.3：相当于「按这个预设入口跑一次」）。
        已删（`status='deleted'`）的触发器拒绝（M6）—— `_require_trigger` 已经不返回它了。

        **`paused` 允许触发**（P6）：暂停管的是"定时那一路还要不要自动跑"
        （`list_due_cron` 的扫描条件带 `status='active'`），不妨碍谁手动补一次。

        **这里不碰 `next_fire_at`，也不碰 `last_fired_at`**（P7）：
        `last_fired_at` 只由 ⓪ 抢占 CAS 写（§7.5 ⓪），语义是「上一次**到点处理**的时间」，
        手动触发不是"到点"。同理**不改 `next_fire_at`** —— 手动只是"临时补跑一次"，
        不能把定时计划打乱；要"上次真正触发任务的时间"看 `trig.runs` 最后一条。

        **不拿任务锁、也不做幂等去重**：这是"再跑一次"的正规入口，重复调用就该起多个任务。

        `origin` 只影响日志（`manual` / `cron`）：调度循环走的是同一个方法
        （§7.5 ⓪ 第 2 步），传 `cron` 好让日志分得清"这是定时到的还是人点的"。
        """
        tenant_id = require_tenant(tenant_id)
        row = await self._require_trigger(tenant_id, trigger_id)
        version_no = await self._resolve_version_no(
            tenant_id, row.definition_id, row.fixed_version
        )
        task = await self._task_service.fire(
            tenant_id,
            row.definition_id,
            row.space_id,
            dict(row.inputs or {}),
            version_no=version_no,
            trigger_id=row.id,
            origin=origin,
        )
        logger.info(
            "[TaskCenter] 触发 trigger=%s definition=%s task=%s origin=%s",
            row.trigger_key,
            row.definition_id,
            task.id,
            origin,
        )
        return task.dict()

    # ========================================================
    # 暂停 / 恢复（§4.3 trig.pause / trig.resume）
    # ========================================================

    async def pause_trigger(
        self, trigger_id: str, tenant_id: Optional[str] = None
    ) -> TaskTriggerItem:
        """暂停触发（`trig.pause`）：置 `status='paused'`，**`next_fire_at` 原样留着**。

        **幂等**（M12）：本来就是 `paused` 就当成功返回，不改动、不报错 ——
        「暂停一个已经暂停的」是调用方重放，不是错误。

        **暂停后**：⓪ 扫库（`list_due_cron` 条件带 `status='active'`）扫不到它，
        定时那一路自动停；**手动 `trig.fire` 仍然可以**（P6）—— `trig.fire` 不看
        `status`（只看是不是已删），所以"临时补一次但不恢复调度"正是它的用法。

        **`next_fire_at` 不动**：恢复时要按"那时的时间"重算（M14），不认这个旧值。
        """
        tenant_id = require_tenant(tenant_id)
        row = await self._require_trigger(tenant_id, trigger_id)
        if row.status == TriggerStatus.PAUSED.value:
            return TaskTriggerItem(**row.to_dict())
        updated = await self._triggers.update_fields(
            tenant_id, trigger_id, status=TriggerStatus.PAUSED.value
        )
        if updated is None:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.trigger.update_race",
                    params={"trigger_id": trigger_id},
                    fallback=f"触发 {trigger_id} 的状态刚被别处改过，本次修改没生效",
                ),
                details={"trigger_id": trigger_id},
            )
        return TaskTriggerItem(**updated.to_dict())

    async def resume_trigger(
        self, trigger_id: str, tenant_id: Optional[str] = None
    ) -> TaskTriggerItem:
        """恢复触发（`trig.resume`）：置回 `status='active'`，**并按"现在"重算 `next_fire_at`**。

        **幂等**（M12）：本来就是 `active` 就当成功返回，不改动、不报错。
        注意"幂等"是在**状态**上幂等的 —— 已经 `active` 时**不重算 `next_fire_at`**
        （否则每次重放都把计划往后推，等于变相拖延）。

        **重算而不是用暂停时的旧值**（M14）：暂停期间可能已经错过了好几个点，
        拿旧值恢复会立刻补一堆积压的触发。重算 = "从现在起按节奏来"。

        **算不出就拒绝**（M15②）：`cron_expr` 在往后 9 年内算不出下一次
        （例如改过表达式、或日期类表达式已过期）→ **拒绝恢复**、保持 `paused`，
        并记一条告警日志。不置 `active` 是为了不把一个必然算不出 `next_fire_at`
        的触发器放回 scan 里 —— 那会让 ⓪ 每轮都撞一次。
        """
        tenant_id = require_tenant(tenant_id)
        row = await self._require_trigger(tenant_id, trigger_id)
        if row.status == TriggerStatus.ACTIVE.value:
            return TaskTriggerItem(**row.to_dict())

        computed = self._require_next_cron_fire(row)

        # 改 `next_fire_at` 一律带 `state_version`（M5b）：期间可能被抢占推过，
        # 命中 0 行就重读再算，别把抢占推后的值覆盖回旧值。
        for _ in range(_CAS_ATTEMPTS):
            updated = await self._triggers.update_fields_cas(
                tenant_id,
                trigger_id,
                expect_version=row.state_version,
                status=TriggerStatus.ACTIVE.value,
                next_fire_at=computed,
            )
            if updated is not None:
                return TaskTriggerItem(**updated.to_dict())
            row = await self._require_trigger(tenant_id, trigger_id)
            # ⚠️ 重读之后**先看状态**：CAS 落空未必是「被抢占推过版本」，也可能是
            # **另一个副本刚把这条恢复成 `active` 了**（两个 `resume` 并发）。
            # 那种情况按 M12 的「已经 `active` 就不改动」幂等成功返回 —— 若继续往下走，
            # 本副本会拿自己算的 `next_fire_at` 再写一遍（两个副本各自按自己的 `now` 算，
            # 跨过某个触发点时算出的值会差**整整一轮**），
            # 把 M12 的承诺落空。判据与函数开头那次早返回**必须一致**。
            if row.status == TriggerStatus.ACTIVE.value:
                return TaskTriggerItem(**row.to_dict())
            # 重读之后**要重判能不能算**：期间 `trig.update` 可能刚把表达式改成不可算的
            # （如 `0 0 30 2 *` 是拦住了，但「改完放一阵子、跨过有效期」这类拦不住）。
            # 漏判就会把 `None` 写进 `next_fire_at`，被那一列的 `NOT NULL` 兜成 500。
            computed = self._require_next_cron_fire(row)
        raise ResourceConflictError(
            message=translate(
                "err.task_center.trigger.update_race",
                params={"trigger_id": trigger_id},
                fallback=f"触发 {trigger_id} 的状态刚被别处改过，本次修改没生效",
            ),
            details={"trigger_id": trigger_id, "attempts": _CAS_ATTEMPTS},
        )

    async def list_trigger_runs(
        self,
        trigger_id: str,
        page: int = 1,
        page_size: int = 20,
        tenant_id: Optional[str] = None,
    ) -> TaskList:
        """这个触发产生过的实例（按最新快照列的 `tasks`）。

        **触发器已被假删也照样查得到**（`include_deleted=True`）：删除是假删、行原样
        留着，`tasks.trigger_id` 就是为了「它产生过的实例还能溯源」才记的（§3.3）——
        在这里把已删的当「找不到」，等于用户点完删除之后那批实例再没有任何入口。
        别的入口（`trig.get` / `trig.update` / `trig.delete`）**仍然把已删的当不存在**。
        """
        tenant_id = require_tenant(tenant_id)
        await self._require_trigger(tenant_id, trigger_id, include_deleted=True)
        return await self._task_service.list_tasks(
            tenant_id,
            {"trigger_id": trigger_id},
            page=page,
            page_size=page_size,
        )

    # ========================================================
    # 内部
    # ========================================================

    async def _normalize_inputs(
        self,
        tenant_id: str,
        definition_id: str,
        fixed_version: Optional[int],
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """按「触发时会用到的那一版」的 `input_schema` 收口一份入参（§3.3.1）。

        还挑不出可用版本（定义一次都没发过版）时不拦：此时编排放着也没法跑，
        拦在触发这一层只会把「先把触发配好、再发版」的顺序堵死——真正跑的时候
        `fire` 还会按锁定版本再收一次口。

        ⚠️ **那一版查不到时直接拒绝**（2026-10-09 补，原先的缺口是「静默清空入参」）：
        `version is None` → 不拿空 schema 去筛（空表筛什么都返回空、还不报 issue，
        调用方会以为成了，实际入参整份没了）→ 抛 `InvalidParameterError` 让调用方看见。

        ⚠️ **「版本在、但这一版没声明 `input_schema`」是另一回事、不拦**（2026-10-13 澄清）：
        那是**定义本身就没声明入参**（发版校验 N8b 保证没人引用得到它们），收口成空集是
        **对的**，与 `fire` 那条路同一套口径（`config.get("input_schema") or {}`），两处一致。
        上面那句「缺口」**只指 `version is None` 这一扇门** —— 别读成「所有静默清空都堵了」。
        """
        version_no = await self._resolve_version_no(
            tenant_id, definition_id, fixed_version, required=False
        )
        if not version_no:
            return dict(values or {})
        version = await self._versions.get_version(definition_id, version_no)
        if version is None:
            # ⚠️ **查不到版本就拒绝这次修改，绝不拿空 schema 去筛入参**（2026-10-09 补）：
            # 空表筛任何东西**结果都是空、而且不报 issue** —— 调用方看到 `issues=[]` 以为
            # 成了，实际整份入参被**静默清空**。之后每次到点都因必填缺失起不来（落到
            # `_fire_one` 那条「只记一条日志」的路上，见那里的配置类熔断）。
            # 成因是 `fixed_version` 指着一条查不到的版本（`_resolve_version_no` 对这类
            # 脏数据刻意宽容，不在这里重复判它）。改掉 `fixed_version` 或重建版本即可恢复。
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.version_not_found",
                    params={"definition_id": definition_id, "version_no": version_no},
                    fallback=(
                        f"定义 {definition_id} 查不到版本 {version_no}，"
                        "没法按它校验入参（先把 fixed_version 改对，或重建那一版）"
                    ),
                ),
                details={"definition_id": definition_id, "version_no": version_no},
            )
        schema = (version.config or {}).get("input_schema") or {}
        # ⚠️ **schema 里没声明的键当场拒**（2026-10-13 补）：`normalize_by_schema` 对这类键是
        # **静默丢弃**（既不报错、也不保留）—— 触发入参多写一个字段名（拼错、或定义改版后
        # 字段被删）时接口会回 `code=0`，那个键被**无声删掉**，到点触发时下游
        # `@task.inputs.<key>` 变 MISSING。这正是发版校验 **N11** 专门防的「静默失踪」，
        # 触发这一层从前没有等价闸门。判据只补在这里；`fire` 那条路自带收口，不受影响。
        unknown_keys = sorted(set(values or {}) - set(schema or {}))
        if unknown_keys:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.param.invalid",
                    params={
                        "errors": f"触发入参里有没声明的字段：{', '.join(unknown_keys)}"
                    },
                    fallback=(
                        "参数没通过校验：触发入参里有没声明的字段："
                        f"{', '.join(unknown_keys)}"
                    ),
                ),
                details={"definition_id": definition_id, "unknown_keys": unknown_keys},
            )
        normalized, issues = normalize_by_schema(
            schema, values, path=f"触发 {definition_id} 的入参"
        )
        if issues:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.trigger.inputs_invalid",
                    params={"count": len(issues)},
                    fallback=f"触发入参没通过校验（{len(issues)} 条问题）",
                ),
                details={"definition_id": definition_id, "issues": issues},
            )
        return normalized

    async def _resolve_version_no(
        self,
        tenant_id: str,
        definition_id: str,
        fixed_version: Optional[int],
        *,
        required: bool = True,
    ) -> Optional[int]:
        """挑出这次要用哪一版（§3.3）。

        **`fixed_version` 填了** → 钉死那一版（建 / 改时已经验过它存在且已发布）；
        **没填** → 用定义当前的 `current_version_no`，也就是最新已发布版。
        没发过版（`current_version_no=0`）时，`required=True` 直接拒绝——没东西可跑。

        ⚠️ **写入侧拒 `0`、读这一侧却把 `0` 当「没填」，这个不对称是刻意的**：
        写入侧拒是为了不让人「以为自己钉住了什么」（见 `_require_fixed_version`）；
        而这里读的是**已经躺在库里的行**，它可能是这条规则上线前写进去的。为一行脏数据
        让整条触发器再也触发不了，代价比「当没钉版本跑最新版」大得多 —— 所以 `if fixed_version:`
        的假值短路保留，`0` 落到下面的 `current_version_no` 那一支。新数据到不了这一支。

        ⚠️ 从前还有 `version_mode` 参数：`fixed` 但没填号时等价于 `latest`，所以那个参数
        一个字的信息都没多给；砍掉之后「用最新版」与「钉某一版」只看这一列是不是空。
        """
        if fixed_version:
            return fixed_version
        definition = await self._definitions.get_visible(definition_id, tenant_id)
        if definition is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.def.not_found",
                    params={"definition_id": definition_id},
                    fallback=f"任务定义 {definition_id} 不存在",
                ),
                details={"definition_id": definition_id},
            )
        if definition.current_version_no:
            return definition.current_version_no
        if required:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.task.no_published_version",
                    params={"definition_id": definition_id},
                    fallback=f"定义 {definition_id} 还没有任何已发布版本，触发不出任务",
                ),
                details={"definition_id": definition_id},
            )
        return None

    @staticmethod
    def _resolve_cron_fields(
        cron_expr: Optional[str],
        misfire_policy: Optional[str],
        *,
        now: Optional[datetime] = None,
    ) -> Tuple[str, str, datetime]:
        """收口三个 cron 字段，返回 `(cron_expr, misfire_policy, next_fire_at)`。

        **触发器只等于定时**（§3.3），所以这三个字段**无条件必填**、不再按类型分流
        （§4.3 M9 / M10 / M9b / M9c）：

        - `cron_expr` 与 `misfire_policy` 必须**同时**给 —— 只给一个就不知道"迟到怎么办"，
          拒绝（**不替调用方猜默认值**）；`misfire_policy` 取值只能是
          `skip` / `catch_up`；
        - 表达式语法必须合法（M9b）；
        - **从"现在"往后 9 年内必须算得出下一次**（M9c）—— 算不出说明这个表达式
          永远不匹配（如 `0 0 30 2 *`），当场拒绝比建个不会响的闹钟强。
          算出来的值随建 / 改一起落库，**秒为 `00`、严格大于现在**。

        **三个返回值都非空**：`next_fire_at` 是这一列 `NOT NULL` 的直接兑现，
        **没有"返回 `None` 让调用方去落库"这条岔路**（从前 `manual` 类型有，已随
        `manual` 一起砍掉）。
        """
        if not cron_expr or not misfire_policy:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.trigger.cron_fields_incomplete",
                    # 模板里没有占位符，不传 params（缺哪个在下面的 `details` 里）。
                    fallback=(
                        "触发器必须同时给 cron_expr 和 misfire_policy —— "
                        "缺了 misfire_policy 就没法决定「到点时发现错过了怎么办」"
                    ),
                ),
                details={
                    "cron_expr": cron_expr,
                    "misfire_policy": misfire_policy,
                },
            )
        if misfire_policy not in (
            MisfirePolicy.SKIP.value,
            MisfirePolicy.CATCH_UP.value,
        ):
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.trigger.bad_misfire_policy",
                    params={"misfire_policy": misfire_policy},
                    fallback=f"misfire_policy 只能是 skip / catch_up，收到 {misfire_policy}",
                ),
                details={"misfire_policy": misfire_policy},
            )

        try:
            computed = compute_next_fire_at(cron_expr, now)
        except CronExprError as exc:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.trigger.cron_expr_invalid",
                    params={"cron_expr": cron_expr},
                    fallback=f"cron 表达式 {cron_expr} 写法不合法：{exc}",
                ),
                details={"cron_expr": cron_expr},
            ) from exc

        if computed is None:
            # §3.3.2 ⑤：永远不匹配的表达式（如 `0 0 30 2 *`）在建 / 改这一刻拒绝，
            # 而不是等运行时才发现算不出（那时只能告警 + 置 paused）。
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.trigger.cron_uncomputable",
                    params={"cron_expr": cron_expr},
                    fallback=(
                        f"cron 表达式 {cron_expr} 在往后 9 年内算不出任何一次触发时间，"
                        "这个触发器不会有响应，拒绝创建"
                    ),
                ),
                details={"cron_expr": cron_expr},
            )
        return cron_expr, misfire_policy, computed

    @staticmethod
    def _next_cron_fire(cron_expr: Optional[str]) -> Optional[datetime]:
        """算下一次触发时间：**语法非法 / 算不出都返回 `None`**（给 `resume` 用）。

        跟 `_resolve_cron_fields` 的区别：那里是"建 / 改"入口，「算不出」是一条
        **校验结论**（要报错）；这里是"把库里已经存着的表达式重新算一遍"，
        表达式可能是早先存进去的，语法在这一层不该再抛异常——统一按"算不出"处理，
        由调用方决定拒绝还是置 `paused`。
        """
        if not cron_expr:
            return None
        try:
            return compute_next_fire_at(cron_expr)
        except CronExprError:
            return None

    def _require_next_cron_fire(self, row) -> datetime:
        """给 `trig.resume` 用：算出下一次触发时刻，**算不出就拒绝**（M15②）。

        「有调用方在场」的那条路（对上「⓪ 抢占路径」——那边没人在场，只能记告警 +
        置 `paused`）：拒绝恢复、保持 `paused`。**不能退化成把 `next_fire_at` 写成
        `NULL`** —— 那一列是 `NOT NULL`（触发器只等于定时），写下去会被数据库
        直接拒掉、变成一条 500；而静默置 `active` 更糟：那会放一个必然算不出
        `next_fire_at` 的触发器回扫描里，让 ⓪ 每轮都撞一次。

        **单独抽出来是为了 CAS 重试循环里那次重算也走同一条判断** —— 重读之后表达式
        可能已经不可算了（`trig.update` 刚改过），循环里漏判就会写出 `NULL`。
        """
        computed = self._next_cron_fire(row.cron_expr)
        if computed is None:
            logger.warning(
                "[TaskCenter] 恢复触发失败：算不出下一次触发时间 trigger=%s cron_expr=%s",
                row.trigger_key,
                row.cron_expr,
            )
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.trigger.cron_uncomputable",
                    params={"cron_expr": row.cron_expr},
                    fallback=(
                        f"cron 表达式 {row.cron_expr} 在往后 9 年内算不出下一次触发时间，"
                        "不能恢复（先改表达式）"
                    ),
                ),
                details={"trigger_id": row.id, "cron_expr": row.cron_expr},
            )
        return computed

    async def _require_fixed_version(
        self,
        definition_id: str,
        fixed_version: Optional[int],
    ) -> None:
        """填了 `fixed_version` 就必须**是个真版本号、且存在、且已发布**（§3.3 A11 / A12、§4.3）。

        `fixed_version` 的语义就是「钉死某一版」——锁不住就该当场报错：钉一个不存在 /
        没发版的版本，等到触发那天才发现跑不了，谁都解释不了。

        **没填不算错**（A12 原话）：那是「用最新已发布版」，没有要查的对象 ——
        「哪个是最新已发布版」由发版流程保证（`definition.current_version_no`），
        这里没得查，也不该在这一层拦。

        ⚠️ **`0` 不算「没填」**：`if not fixed_version` 会把 `0` 一并放行（`0` 是假值），
        于是 `0` 就这么存进库、触发时又被 `_resolve_version_no` 当假值跳过去 ——
        **一路静默**，调用方以为自己钉住了什么。`0` 不是一个版本号（`current_version_no`
        的 0 表示「一版都没发过」，不是「第 0 版」），所以这里明确判 `is None`，
        非正的号当场回 1001。**不钉版本请传 `null`**。
        （DTO 那层也挂了 `ge=1`，同一件事在边界上先拦一道、报错能指到具体字段。）

        ⚠️ 从前这里还有另一半步：`version_mode=latest` 却填了 `fixed_version` 要算参数
        矛盾（A11b）。`version_mode` 那一列已删，**这对矛盾连同这条检查一起消失** ——
        现在「填了号」本身就是那句声明，没有第二种字段可以跟它打架。
        """
        if fixed_version is None:
            return
        if fixed_version <= 0:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.trigger.fixed_version_invalid",
                    params={"version_no": fixed_version},
                    fallback=(
                        f"fixed_version 必须是大于 0 的版本号，收到 {fixed_version}；"
                        "0 不是「没填」的意思，不钉版本请传 null"
                    ),
                ),
                details={"version_no": fixed_version},
            )
        version = await self._versions.get_version(definition_id, fixed_version)
        if version is None or version.status != VersionStatus.PUBLISHED.value:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.trigger.fixed_version_not_published",
                    params={"definition_id": definition_id, "version_no": fixed_version},
                    fallback=(
                        f"定义 {definition_id} 的版本 {fixed_version} 不存在或还没发版，"
                        "不能钉死成 fixed_version"
                    ),
                ),
                details={
                    "definition_id": definition_id,
                    "version_no": fixed_version,
                    "status": version.status if version is not None else None,
                },
            )

    async def _require_definition(
        self, tenant_id: str, space_id: str, definition_id: str
    ):
        """定义一个触发器能引用哪个定义：**这个租户看得见**，且空间对得上（§3.3）。

        空间是「能否引用某个定义（尤其 `scope=space` 的）的判据」——空间级的定义只认
        自己那个空间；系统级 / 租户级不绑空间，同租户下哪个空间都引用得到。
        空间对不上按「找不到」处理，跟租户隔离同一个口径。
        """
        definition = await self._definitions.get_visible(definition_id, tenant_id)
        if definition is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.def.not_found",
                    params={"definition_id": definition_id},
                    fallback=f"任务定义 {definition_id} 不存在",
                ),
                details={"definition_id": definition_id},
            )
        if (
            definition.scope == DefinitionScope.SPACE.value
            and definition.space_id != space_id
        ):
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.trigger.definition_space_mismatch",
                    params={"definition_id": definition_id, "space_id": space_id},
                    fallback=(
                        f"定义 {definition_id} 是空间级的，不属于空间 {space_id}，"
                        "这个空间下的触发引用不了它"
                    ),
                ),
                details={
                    "definition_id": definition_id,
                    "space_id": space_id,
                    "definition_space_id": definition.space_id,
                },
            )
        return definition

    async def _require_trigger(
        self, tenant_id: str, trigger_id: str, *, include_deleted: bool = False
    ):
        """取触发器，取不到按「找不到」处理（1002）。

        `include_deleted=False`（默认）：**已假删的当作不存在** —— §4.3 的口径
        （「不是自己租户的数据 / 已假删的，一律按找不到处理」）。除下面那个例外，
        所有调用方都用这一档。

        `include_deleted=True`：**连已假删的一起认**。**只有 `trig.runs` 用它** ——
        删除是**假删**（§3.3：《行原样留着，它产生过的实例还要能溯源》），所以
        「查这个触发器跑过什么」不该被「触发器已经删了」挡住；挡掉的话，用户点完删除
        就再也找不到那批实例了。**越权照样拦得住**：两个查询都带 `tenant_id`，
        别人的触发器取不到，走的还是同一条 1002（与「已删」同码，这是有意的，
        见 §4.3 总则）。
        """
        if include_deleted:
            row = await self._triggers.get_trigger_any(tenant_id, trigger_id)
        else:
            row = await self._triggers.get_trigger(tenant_id, trigger_id)
        if row is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.trigger.not_found",
                    params={"trigger_id": trigger_id},
                    fallback=f"触发 {trigger_id} 不存在",
                ),
                details={"trigger_id": trigger_id},
            )
        return row


__all__ = ["TaskTriggerService"]
