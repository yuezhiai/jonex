#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行侧 ORM：任务实例 / 任务尝试 / 子任务 / 子任务尝试历史。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.5。

两条贯穿全篇的约定：

- `Task` 存「稳定身份 + 最新一次运行的状态快照」，重跑不新建行、只更新快照；
  每次运行的历史在 `TaskAttempt`，每次运行还会挂一套全新的 `Subtask`。
- `Subtask` 一行存「该子任务最新一次尝试」，每次尝试的历史在 `SubtaskAttempt`。
  两者都带 `state_version`（防重复改，§7.2）与 `current_attempt`（当前第几次尝试）。

执行侧四张表**一律空间级**（`tenant_id` + `space_id` 都 NOT NULL，§3.5）。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import AuditMixin, TimestampMixin

from .base import iso
from .enums import AttemptStatus, SubtaskAttemptStatus, SubtaskStatus, TaskStatus


class Task(Base, TimestampMixin, AuditMixin):
    """任务实例：一次触发的产物，是子任务树的根，也是这条任务的稳定身份。

    `created_by` / `updated_by` 存的是 **`user_id`（不是用户名）**，空值合法：

    - **创建人**只在任务诞生那一刻写一次，之后不再变。触发器自动触发、内部系统调用
      这类「不是人点的」任务留空 —— 不是「没记上」，本来就没有人。
    - **更新人**只被「用户主动写这条任务」的操作写（如 `task.stop`）。**系统自己推进
      状态绝不写它**（派活抢占 / 消化上报 / 超时巡检都是后台循环干的），否则最后一次
      自动巡检会把真正的操作人盖掉。
    """

    __tablename__ = "tasks"
    __table_args__ = (
        # 后台循环按「租户 + 状态」扫任务
        Index("idx_tc_task_tenant_status", "tenant_id", "status"),
        Index("idx_tc_task_def", "definition_id"),
        Index("idx_tc_task_trigger", "trigger_id"),
        # ⚠️ **本类有意不走 `TenantMixin` / `SoftDeleteMixin`**（公共内核
        # `jonex_core/common/entity.py`）：那两个 mixin 给 `tenant_id` / `is_deleted`
        # 各带一条 `index=True`，而 DDL（`008_task_center.sql`）**有意不给这两列单独建索引** ——
        # `tenant_id` 的查询已被上面那条 `idx_tc_task_tenant_status(tenant_id, status)`
        # 的前缀覆盖，`is_deleted` 单独一列几乎用不上（软删在 DDL 里要么进复合索引、
        # 要么做部分索引的 WHERE 谓词）。走 mixin 的话 `create_all` 会多建
        # `ix_task_center_tasks_tenant_id` / `ix_task_center_tasks_is_deleted` 两条，
        # 于是**同一个模型在两条建表路径上形状不同** —— 对账测试
        # （`tests/unit/task_center/test_task_center_schema_parity.py`）把索引也纳入
        # 比对之后，那就是红的。两列改为在下面直接声明，**定义照抄 mixin**
        # （类型 / 可空性 / 默认值必须一致，对账测试也盯着这三样）。
        #
        # 下面这条 CHECK 同 `TaskDefinition.__table_args__`：出处是 DDL，重复一份只为让
        # `create_all` 跟 DDL 对齐 —— 少了它，本地库会少一道生产有的闸。
        CheckConstraint(
            "status IN ('pending','running','completed','failed','stopped')",
            name="ck_tc_task_status",
        ),
        # 任务来源的取值范围（2026-10-13 加）：**出处是 DDL**（`update/050_task_source.sql`），
        # 这里重复一份只为让 `create_all` 跟 DDL 对齐 —— 少了它，本地库会少一道生产有的闸。
        # 取值与协议层 `TASK_SOURCES` 同源；对账用例（`_ENUM_BY_CONSTRAINT` 里那条
        # `ck_tc_task_source`）会把 DDL 侧的取值逐个对照那个集合。
        CheckConstraint(
            "task_source IN ('knowledge_base.file.manual_upload','knowledge_base.file.api_pull',"
            "'knowledge_base.file.api_push','knowledge_base.file.storage_upload')",
            name="ck_tc_task_source",
        ),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    # 见上面 `__table_args__` 里那段：这两列**不走 mixin**（DDL 不给它们单独建索引），
    # 声明照抄 `TenantMixin` / `SoftDeleteMixin` 的定义。
    tenant_id = Column(String(64), nullable=False)
    is_deleted = Column(SmallInteger, default=0, nullable=False)
    definition_id = Column(String(64), nullable=False)
    # 用的是哪个版本号（钉死，**仅作溯源** —— 编排内容本身见下面的 definition_config）
    definition_version_no = Column(Integer, nullable=False)
    # 编排快照：建任务那一刻把 `task_definition_versions.config` 整份抄下来。
    #
    # **为什么冗余而不反查**：版本会被「删定义」带成假删（`get_version` 把
    # `is_deleted=1` 当找不到），而那时任务多半还在跑 —— 反查一断，`_load_context`
    # 返回 None，**消化上报 / 停止任务 / 超时巡检三条路全灭**，那条 `running` 子任务
    # 永远收不掉、并发名额（该目录上限 10）永久泄漏。编排是这条任务的「施工图纸」，
    # 任务行自己带一份，就不怕图纸被归档。
    definition_config = Column(JSONB, nullable=False)
    # 内置清单编码（`task_definitions.task_catalog_code`）的快照。理由同上，而且是**更隐蔽**
    # 的一条：反查定义断掉之后 `catalog_steps` 会静默变成空字典 —— 并发上限 / `runner_code`
    # / 超时时间全取不到，却**不报错、也不返回「找不到」**，任务就这么带着空值继续跑。
    task_catalog_code = Column(String(64), nullable=False)
    # 空间 ID：这次运行落在哪个空间（§3.5）
    space_id = Column(String(64), nullable=False)
    # 任务实例名：调用方在 `init_task` / `fire` 传了 `name` 就用它，没传（或只给空白）退回定义名 `task_definitions.name`
    task_name = Column(String(256), nullable=True)
    # 是哪个触发触发的；只有直接 fire 不挂触发器时才为 NULL
    trigger_id = Column(String(64), nullable=True)
    # **任务来源**（2026-10-13 加）：这条任务是哪个入口来的。取值是**协议层写死的枚举**
    # （`jonex_core.task_protocol.models.TASK_SOURCES`，格式 `{运行方 code}.{对象}.{通道}`，
    # 如 `knowledge_base.file.manual_upload`）—— 运行方填、这里存**原码**、前端按码映射文案。
    #
    # ⚠️ **可空，而且空是有意义的**（不是漏记，也**不要**给它加 NOT NULL）：
    #   · 平台侧自己建的任务（定时触发 `fire_trigger`、人点「立即触发」）不是运行方发起的，
    #     本来就没有「来源入口」这个概念，留空是对的；
    #   · 这列诞生之前的存量任务行也没值。回填不了、也不该编一个。
    # 「必传」只落在 `init_task` 那条路上 —— 由 `TaskService.init_task` 用协议层的
    # `check_task_source` 判（非法值 1001），`fire` 本身不强制（`task.fire` 那条内部
    # 触发路径不传）。
    task_source = Column(String(64), nullable=True)
    # 这次运行的任务级入参（按 config.input_schema 收口后的实值）。
    # 必须落库：编排里的 `@task.inputs.<key>` 在「下游某步依赖齐了、轮到填它的值」时才求值，
    # 那一刻离 fire 已经过去很久，只有这一列能给出任务级入参（§6.2）。
    inputs = Column(JSONB, nullable=True)
    # 这条任务**实际用到哪些运行方**（创建时算好快照下来的 capability_id 列表，去重）。
    #
    # 为什么要在创建时钉死、而不是删任务时现查：`task.delete` 要逐个请运行方清掉自己侧
    # 的残留（§4.3），而「用到哪些运行方」是从 `task_definition_versions.config.steps`
    # → `task_catalog_steps.runner_code` 推出来的。**删定义会把它的版本全部假删**
    # （`TaskDefinitionRepository.get_version` 把 `is_deleted=1` 当找不到），于是
    # 「先删定义、再删已到终态的任务」——一条完全正常的顺序——现查会得到空列表，
    # 一次 `remove_task` 都发不出去，管理方把任务删了、运行方残留一件没清，两边都不报错。
    #
    # 创建任务那一刻版本**必然**在，是唯一可靠的快照时机。
    runner_capability_ids = Column(JSONB, nullable=True)
    # 当前跑到第几次运行（指针，指向 task_attempts.attempt）；首次=1，retry_task 时 +1
    current_attempt = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default=TaskStatus.PENDING.value)
    error_message = Column(Text, nullable=True)
    # 失败原因的**代号 + 参数**（2026-10-10 加）：可翻译的那半。管理方存代号，
    # **读的时候**按请求的 `X-Lang` 现场渲染 —— 写它的多半是后台循环（超时 / 防僵尸 /
    # 派活判死），那些帧里没有请求上下文、`translate()` 只能拿到默认语言，所以
    # 「在写的那一刻翻译」这条路治不好，只能存代号延后渲染。
    # `error_message` **不被替换**：它继续当「原文槽」（引擎/下下游的自由文本，永远没有
    # 代号）+ 老数据兜底 + 排障快照。判据见协议层 `check_error_reason`（128 = 它的上限）。
    error_code = Column(String(128), nullable=True)
    error_params = Column(JSONB, nullable=True)
    state_version = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "definition_id": self.definition_id,
            "definition_version_no": self.definition_version_no,
            "space_id": self.space_id,
            "task_name": self.task_name,
            "trigger_id": self.trigger_id,
            "task_source": self.task_source,
            "inputs": self.inputs or {},
            "current_attempt": self.current_attempt,
            "status": self.status,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "error_params": self.error_params,
            "state_version": self.state_version,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": iso(self.created_at),
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "updated_at": iso(self.updated_at),
        }


class TaskAttempt(Base):
    """任务尝试：某任务每一次运行（含首次和每次重跑）的一条记录，只追加不覆盖。"""

    __tablename__ = "task_attempts"
    __table_args__ = (
        # ⚠️ **并发重跑不靠它兜**（§7.3）：真正拦并发的是 `tasks.state_version` 的那道 CAS
        # —— `_do_retry_task` 先抢快照（`reset_for_task_retry`），抢不到就 1003 直接返回，
        # **压根走不到插运行行这一步**。这条索引只是数据库层的**最后一道保险**：
        # 全仓没人 catch `IntegrityError`，真撞上会按 `DatabaseError`(4001，可重试) 冒出去。
        # （这条口径在设计文档「诊断收口之五」② 已更正过，这里是漏网的复制品。）
        Index("uq_tc_ta_attempt", "task_id", "attempt", unique=True),
        Index("idx_tc_ta_task", "task_id"),
        Index("idx_tc_ta_created", "created_at"),
        Index("idx_tc_ta_tenant", "tenant_id"),
        # 同 `Task`：出处是 DDL，重复一份只为让 `create_all` 跟它对齐
        CheckConstraint(
            "status IN ('pending','running','completed','failed','stopped')",
            name="ck_tc_ta_status",
        ),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    # 索引走上面的 idx_tc_ta_tenant，不另开 `index=True`（同 `TaskDefinition.tenant_id` 注）
    tenant_id = Column(String(64), nullable=False)
    # 执行侧一律空间级，跟 tasks 一致（§3.5）
    space_id = Column(String(64), nullable=False)
    task_id = Column(String(64), nullable=False)
    attempt = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default=AttemptStatus.PENDING.value)
    error_message = Column(Text, nullable=True)
    # 失败原因的**代号 + 参数**（2026-10-10 加）：可翻译的那半。管理方存代号，
    # **读的时候**按请求的 `X-Lang` 现场渲染 —— 写它的多半是后台循环（超时 / 防僵尸 /
    # 派活判死），那些帧里没有请求上下文、`translate()` 只能拿到默认语言，所以
    # 「在写的那一刻翻译」这条路治不好，只能存代号延后渲染。
    # `error_message` **不被替换**：它继续当「原文槽」（引擎/下下游的自由文本，永远没有
    # 代号）+ 老数据兜底 + 排障快照。判据见协议层 `check_error_reason`（128 = 它的上限）。
    error_code = Column(String(128), nullable=True)
    error_params = Column(JSONB, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    # 带 Python 侧默认值：`BaseRepository.create` 只 flush 不 refresh，没有默认值的话
    # 插入后直接 `**row.to_dict()` 返回，`created_at` 会是 None（与 TimestampMixin 口径一致）。
    created_at = Column(DateTime, default=datetime.now, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "space_id": self.space_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "status": self.status,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "error_params": self.error_params,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "created_at": iso(self.created_at),
        }


class Subtask(Base, TimestampMixin):
    """子任务实例：挂在某一次运行（task_attempt_id）下的一步。

    `is_deleted`：删任务时它的子任务一并置 1（「任务删了，子任务必定也全删」，§3.5）。
    """

    __tablename__ = "subtasks"
    __table_args__ = (
        # 同一次运行里节点 id 不重复（`node_id` 是编排节点、唯一；`step_code` 只是
        # 「用哪个内置步骤」、同一个 `step_code` 出现在多个节点上是允许的，所以不进唯一键）
        Index("uq_tc_subtask_node", "task_attempt_id", "node_id", unique=True),
        # 受理凭证在「租户 + 空间」内唯一、一行只对应「某次运行里的某个子任务」——
        # 运行方上报时管理方就按 `tenant_id` + `space_id` + `execution_id` 定位到这一行（§7.4）。
        # 没派活时 `execution_id` 为空，所以是**部分**索引（空值不进索引、彼此不冲突）
        Index(
            "uq_tc_subtask_exec",
            "tenant_id",
            "space_id",
            "execution_id",
            unique=True,
            postgresql_where=text("execution_id IS NOT NULL"),
        ),
        Index("idx_tc_subtask_status", "status"),
        Index("idx_tc_subtask_tenant", "tenant_id"),
        Index("idx_tc_subtask_task", "task_id"),
        # ⚠️ 本类同样**不走 `TenantMixin` / `SoftDeleteMixin`** —— 原因、以及
        # `ix_task_center_subtasks_tenant_id` / `ix_task_center_subtasks_is_deleted`
        # 那两条为什么不建，见 `Task.__table_args__` 那段（同一条规矩）。
        # 下面这条 CHECK 同样出自 DDL。
        CheckConstraint(
            "status IN ('pending','running','success','failed','timeout','stopped')",
            name="ck_tc_sub_status",
        ),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    # 见上面 `__table_args__` 里那段：这两列**不走 mixin**（DDL 不给它们单独建索引），
    # 声明照抄 `TenantMixin` / `SoftDeleteMixin` 的定义。
    tenant_id = Column(String(64), nullable=False)
    is_deleted = Column(SmallInteger, default=0, nullable=False)
    # 执行侧一律空间级
    space_id = Column(String(64), nullable=False)
    task_id = Column(String(64), nullable=False)
    task_attempt_id = Column(String(64), nullable=False)
    # 编排里的哪个节点（如 parse_1 / parse_2，对应 config.steps[].node_id）；同一次运行里不重复
    node_id = Column(String(64), nullable=False)
    # 这个节点用的是哪个内置步骤（如 parse）。管理方拿它判并发上限、查内置步骤的
    # schema 与 `runner_code`、查历史，**并把它填进 `execute` 派活报文** ——
    # 运行方按它分流到不同执行模式（入参形状分不出来：ingest 与 compile 的入参一样）
    # （§3.5、§4.1 ①②）。
    step_code = Column(String(64), nullable=False)
    # 给人看的名字：诞生时取内置步骤的 name（按 `step_code` 查 task_catalog_steps.name）
    subtask_name = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False, default=SubtaskStatus.PENDING.value)
    # 诞生时从 config 固化
    depends_on = Column(JSONB, nullable=True)
    next_steps = Column(JSONB, nullable=True)
    sort = Column(Integer, nullable=True)
    # 取编排里 config.steps[].timeout_seconds（必填）的值，诞生时固化
    timeout_seconds = Column(Integer, nullable=False, default=300)
    # bind_inputs 解析填值后的真实入参
    inputs = Column(JSONB, nullable=True)
    outputs = Column(JSONB, nullable=True)
    # 产出「去哪儿看」的引用（展示用，**不参与下游取参**）：运行方终态上报时带的数组，
    # 控制台读接口原样回给前端，前端据此调运行方自己的接口看详情（权限归运行方那侧判）。
    # 形状见 DDL 注释与 `hand_over_in_flight_task_id` 同一族的说明；未知键忽略、可空。
    output_refs = Column(JSONB, nullable=True)
    # 输入「**从哪儿来**」的引用（展示用，**不参与下游取参**，2026-10-16 加）：与
    # `output_refs` 对称的那一份 —— 那边说「这一步的成果去哪儿看」，这边说「这一步吃的是什么」。
    # 形状同款（元素只有 `kind` + `params`，`kind` ∈ 协议层 `INPUT_REF_PARAMS`）、同样不带地址。
    # 可空：老运行方不上报、或将来真没有输入引用的任务。
    input_refs = Column(JSONB, nullable=True)
    # 当前第几次尝试（指针，指向 subtask_attempts.attempt）；首次=1，retry_subtask 时 +1
    current_attempt = Column(Integer, nullable=False, default=1)
    # 管理方生成的受理凭证（派活前生成、随 execute 传给运行方；重试时重新生成）
    execution_id = Column(String(128), nullable=True)
    # **对账上一次问过它的时刻**（2026-10-11 加，只服务 `_query_pass`）。
    #
    # ⚠️ **没有这一列，对账会每拍重问一遍**：`TASK_CENTER_QUERY_QUIET_SEC` 只能当
    # 「**第一次**发问」的门槛 —— 一旦某步沉默超过它，此后**永远**满足条件，
    # 而主循环 3 秒一拍 ⇒ 一个跑几分钟的步骤被问上百次，白打运行方。
    # 更糟的是排序：按 `started_at` 取最老的 N 条，**最老的那批（也最可能一直在跑、
    # 一直答「还在跑」）每拍占满名额**，后面新沉默的**永远排不进来**。
    #
    # 现在判据 / 排序都用 `COALESCE(last_queried_at, started_at)`：
    # 问过就往后排，自然轮转；`_query_one` **无论问到什么都回写**这一列
    # （问到「还在跑」也算问过 —— 否则它还留在队头）。
    last_queried_at = Column(DateTime, nullable=True)
    state_version = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    # 失败原因的**代号 + 参数**（2026-10-10 加）：可翻译的那半。管理方存代号，
    # **读的时候**按请求的 `X-Lang` 现场渲染 —— 写它的多半是后台循环（超时 / 防僵尸 /
    # 派活判死），那些帧里没有请求上下文、`translate()` 只能拿到默认语言，所以
    # 「在写的那一刻翻译」这条路治不好，只能存代号延后渲染。
    # `error_message` **不被替换**：它继续当「原文槽」（引擎/下下游的自由文本，永远没有
    # 代号）+ 老数据兜底 + 排障快照。判据见协议层 `check_error_reason`（128 = 它的上限）。
    error_code = Column(String(128), nullable=True)
    error_params = Column(JSONB, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "space_id": self.space_id,
            "task_id": self.task_id,
            "task_attempt_id": self.task_attempt_id,
            "node_id": self.node_id,
            "step_code": self.step_code,
            "subtask_name": self.subtask_name,
            "status": self.status,
            "depends_on": self.depends_on or [],
            "next_steps": self.next_steps or [],
            "sort": self.sort,
            "timeout_seconds": self.timeout_seconds,
            "inputs": self.inputs or {},
            "outputs": self.outputs,
            "output_refs": self.output_refs,
            "input_refs": self.input_refs,
            "current_attempt": self.current_attempt,
            "execution_id": self.execution_id,
            "state_version": self.state_version,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "error_params": self.error_params,
            "created_at": iso(self.created_at),
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "updated_at": iso(self.updated_at),
            # 假删除标记（删任务时整片置 1，§4.3）。`Task`/`TaskDefinition` 那种「查询里
            # 就滤掉了，不对外暴露」的表可以不吐这列；但 `SubtaskItemResponse` 声明了它，
            # 不吐出来那个字段就恒为 0、永远反映不了删除态，所以这里必须带上。
            "is_deleted": self.is_deleted or 0,
        }


class SubtaskAttempt(Base):
    """子任务尝试历史：某个子任务每一次尝试（含首次和每次重试）的一条记录。"""

    __tablename__ = "subtask_attempts"
    __table_args__ = (
        # 同一个子任务下，每个尝试号唯一（并发重试同理靠它兜，§7.3）
        Index("uq_tc_sa_attempt", "subtask_id", "attempt", unique=True),
        # 受理凭证在「租户 + 空间」内唯一（与 `subtasks` 上那条同款，未派活时为空）
        Index(
            "uq_tc_sa_exec",
            "tenant_id",
            "space_id",
            "execution_id",
            unique=True,
            postgresql_where=text("execution_id IS NOT NULL"),
        ),
        Index("idx_tc_sa_task", "task_id"),
        Index("idx_tc_sa_created", "created_at"),
        # 同 `Task`：出处是 DDL，重复一份只为让 `create_all` 跟它对齐
        CheckConstraint(
            "status IN ('pending','running','success','failed','timeout','stopped')",
            name="ck_tc_sa_status",
        ),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    # 索引由上面的 uq_tc_sa_exec（tenant_id 打头）覆盖，不另开 `index=True`
    tenant_id = Column(String(64), nullable=False)
    # 执行侧一律空间级
    space_id = Column(String(64), nullable=False)
    task_id = Column(String(64), nullable=False)
    subtask_id = Column(String(64), nullable=False)
    attempt = Column(Integer, nullable=False)
    execution_id = Column(String(128), nullable=True)
    status = Column(
        String(16), nullable=False, default=SubtaskAttemptStatus.PENDING.value
    )
    error_message = Column(Text, nullable=True)
    # 失败原因的**代号 + 参数**（2026-10-10 加）：可翻译的那半。管理方存代号，
    # **读的时候**按请求的 `X-Lang` 现场渲染 —— 写它的多半是后台循环（超时 / 防僵尸 /
    # 派活判死），那些帧里没有请求上下文、`translate()` 只能拿到默认语言，所以
    # 「在写的那一刻翻译」这条路治不好，只能存代号延后渲染。
    # `error_message` **不被替换**：它继续当「原文槽」（引擎/下下游的自由文本，永远没有
    # 代号）+ 老数据兜底 + 排障快照。判据见协议层 `check_error_reason`（128 = 它的上限）。
    error_code = Column(String(128), nullable=True)
    error_params = Column(JSONB, nullable=True)
    outputs = Column(JSONB, nullable=True)
    # 这一次尝试的**入参快照**（`bind_inputs` 填出来的那份，2026-10-16 加）。
    # ⚠️ **与 `outputs` 对称的那一格缺口**：产出一直存着、入参却只在 `subtasks` 上（那行是
    # 「最新一次尝试」的现状，重试时会被 `_fill_inputs` **重算**）—— 于是「第 2 次尝试吃的是
    # 什么」在重试之后就查不到了。历史要能复盘，两份都得按次留。
    inputs = Column(JSONB, nullable=True)
    # 这一次尝试的**输入引用**（输入「从哪儿来」的入口，2026-10-16 加）：展示用、
    # **不参与下游取参**（取参走 `subtasks.inputs`）。
    # ⚠️ **这里存一份不是冗余**：重试会把 `subtasks.inputs` 按上游现在的产出**重算**，
    # 于是「第 2 次尝试吃的是什么」在 `subtasks` 那行上看不到 —— 它是「最新一次尝试」的现状。
    # 可空：老运行方不上报、或将来真没有输入引用的任务。
    input_refs = Column(JSONB, nullable=True)
    # 这一次尝试的**产出引用**（产出「去哪儿看」的入口，2026-10-16 加，与 `outputs` 对称）：
    # 同样是「按次留一份」的理由 —— 重试之后要能查到「第 2 次尝试的产物在哪儿」。
    output_refs = Column(JSONB, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    # 带 Python 侧默认值：`BaseRepository.create` 只 flush 不 refresh，没有默认值的话
    # 插入后直接 `**row.to_dict()` 返回，`created_at` 会是 None（与 TimestampMixin 口径一致）。
    created_at = Column(DateTime, default=datetime.now, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "space_id": self.space_id,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "attempt": self.attempt,
            "execution_id": self.execution_id,
            "status": self.status,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "error_params": self.error_params,
            "inputs": self.inputs or {},
            "outputs": self.outputs,
            "input_refs": self.input_refs,
            "output_refs": self.output_refs,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "created_at": iso(self.created_at),
        }


__all__ = ["Subtask", "SubtaskAttempt", "Task", "TaskAttempt"]
