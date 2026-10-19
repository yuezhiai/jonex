#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心接管行仓储（`knowledge_base.task_center_documents`）。

表结构与「为什么用独立表」写在 `models/task_center.py`；这里只放读写。

## 本仓储的租户口径与别处不同，写清楚

`BaseRepository._tenant_conditions()` 在**没传 `tenant_id` 时回落到 `TenantContext`**
（contextvar）。那个回落对请求路径是对的，对本表**不能用**：看门狗跑在进程启动时创建的
后台任务里，那条协程没有请求上下文，contextvar 是空的 —— 回落只会得到
`require_tenant(None)` 抛错，或者更糟：在多副本 + 复用事件循环的场景下读到别的请求
留下的陈旧值。

所以本仓储的每个方法**要么显式收 `tenant_id`、要么明确是跨租户扫描**（`list_active`，
与既有 `list_by_status_for_reconciliation` 同一类，只给后台 worker 用），一处都不走
contextvar 回落。这也让「谁在什么身份下读这张表」在调用点就看得见。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import exists, select, update

from jonex_core.common.repository import BaseRepository

from ..models.task_center import (
    ACTIVE_STATES,
    TaskCenterDocState,
    TaskCenterDocument,
)


class TaskCenterDocumentRepository(BaseRepository[TaskCenterDocument]):
    """接管行的读写。

    继承 `BaseRepository`（拿到 `create` / `flush` 这些通用件），但**覆写
    `_primary_key()`** —— 基类取的是 `self.model.id`，而本表的主键是 `document_id`
    （理由见模型 docstring：查它的人手上只有文档 id）。
    """

    model = TaskCenterDocument

    def _primary_key(self):
        return TaskCenterDocument.document_id

    # ---------------------------------------------------------------- 读

    async def read(
        self,
        document_id: str,
        tenant_id: str | None = None,
        *,
        for_update: bool = False,
    ) -> Optional[TaskCenterDocument]:
        """按文档 id 读接管行。

        `tenant_id=None` 表示**不做租户过滤**：只有维护脚本 / 排障会这么调。
        业务路径一律带上它（handler 从执行上下文取、看门狗从行里取）。
        """
        conditions = [TaskCenterDocument.document_id == document_id]
        if tenant_id is not None:
            conditions.append(TaskCenterDocument.tenant_id == tenant_id)
        stmt = select(TaskCenterDocument).where(*conditions)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def read_by_task_id(
        self,
        task_id: str,
        tenant_id: str | None = None,
    ) -> Optional[TaskCenterDocument]:
        """按任务 id 读接管行 —— `remove_task` 唯一的入口。

        报文里只有 `tenant_id` / `space_id` / `task_id` 三件套，而 `task_id` 上有唯一
        索引（`uq_kb_tc_doc_task`），所以这是主键级查询。
        """
        conditions = [TaskCenterDocument.task_id == task_id]
        if tenant_id is not None:
            conditions.append(TaskCenterDocument.tenant_id == tenant_id)
        result = await self.session.execute(select(TaskCenterDocument).where(*conditions))
        return result.scalar_one_or_none()

    async def list_active(self, limit: int = 50) -> list[TaskCenterDocument]:
        """**跨租户**扫描在途接管行 —— 只给后台看门狗用（与
        `KnowledgeDocumentRepository.list_by_status_for_reconciliation` 同一类）。

        「在途」= `state IN ('submitting', 'managed')`，**不含 `released`**：release
        就是交接，交出去的文档归既有对账管，看门狗不再盯。

        按 `updated_at` 升序：卡得最久的先被看。这同时也是**推进保证** ——
        `limit` 不够时最旧的先处理，不会出现「一批新行永远挡在旧行前面」。
        """
        stmt = (
            select(TaskCenterDocument)
            .where(TaskCenterDocument.state.in_(list(ACTIVE_STATES)))
            .order_by(TaskCenterDocument.updated_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def exists_active(
        self,
        document_id: str,
        tenant_id: str | None = None,
    ) -> bool:
        """这份文档还在被接管吗 —— 用户接管拦截（重解析 / 重抽取）的判据。"""
        conditions = [
            TaskCenterDocument.document_id == document_id,
            TaskCenterDocument.state.in_(list(ACTIVE_STATES)),
        ]
        if tenant_id is not None:
            conditions.append(TaskCenterDocument.tenant_id == tenant_id)
        stmt = select(exists().where(*conditions))
        return bool((await self.session.execute(stmt)).scalar())

    async def exists_active_for_kb(self, knowledge_base_id: str, tenant_id: str) -> bool:
        """这个知识库下有没有任何文档还在被接管 —— 知识库级用户接管拦截
        （重置本体 schema）的判据。那种操作按库整批生效，所以拦的粒度也必须是库。"""
        stmt = select(
            exists().where(
                TaskCenterDocument.knowledge_base_id == knowledge_base_id,
                TaskCenterDocument.tenant_id == tenant_id,
                TaskCenterDocument.state.in_(list(ACTIVE_STATES)),
            )
        )
        return bool((await self.session.execute(stmt)).scalar())

    # ---------------------------------------------------------------- 写

    async def mark_managed(
        self,
        document_id: str,
        *,
        tenant_id: str,
        task_id: str,
    ) -> bool:
        """提交成功：`submitting` → `managed`。

        条件里带 `state == submitting`：重复提交成功（看门狗补试与上传路径撞上）时
        第二次是 no-op，不会把已经在跑的行打回起点。返回 `False` 就是「不是我该改的
        那一行」，调用方按 no-op 处理即可。
        """
        stmt = (
            update(TaskCenterDocument)
            .where(
                TaskCenterDocument.document_id == document_id,
                TaskCenterDocument.tenant_id == tenant_id,
                TaskCenterDocument.task_id == task_id,
                TaskCenterDocument.state == TaskCenterDocState.SUBMITTING.value,
            )
            .values(state=TaskCenterDocState.MANAGED.value, error_message=None)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def bump_submit_attempt(self, document_id: str, error_message: str) -> int:
        """记一次提交失败（`submit_attempts += 1`），返回累加后的次数。

        失败原因截断到 2000 字符：`error_message` 是 `TEXT`、不拦长度，但异常串里
        带整段堆栈时它会变成难以阅读的一大坨，排障时反而看不清头几行。
        """
        stmt = (
            update(TaskCenterDocument)
            .where(TaskCenterDocument.document_id == document_id)
            .values(
                submit_attempts=TaskCenterDocument.submit_attempts + 1,
                error_message=(error_message or "")[:2000],
            )
            .returning(TaskCenterDocument.submit_attempts)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(result.scalar_one_or_none() or 0)

    async def begin_step(
        self,
        document_id: str,
        *,
        tenant_id: str,
        task_id: str,
        step_code: str,
        execution_id: str,
    ) -> bool:
        """本步开始：写 `current_step` / `execution_id` / `step_started_at`。

        **必须在调 atomic-rag 之前调**（handler 的第 4 步）：看门狗以
        `step_started_at` 计时，先发下游再写就会让计时起点晚于事实。

        条件里带 `state IN 在途` 与 `task_id`：只有仍在接管的、且属于这条任务的行
        才认这次派活。返回 `False` 时 handler **必须显式拒**（不能当成功收下）——
        那意味着这行已经被 release 或换过任务，继续干就会产出没人认领的下游任务。

        ⚠️ **时间用 Python 的 `datetime.now()`，不用 `func.now()`**：这一列是**唯一
        被作差的列**（看门狗拿 `datetime.now()` 减它），而 PG 的 `now()` 是 timestamptz、
        落进 `TIMESTAMP WITHOUT TIME ZONE` 时按**会话时区**换算 —— 会话时区与进程本地
        时区不一致时会整体偏几小时，看门狗会立刻把所有在途行判死。既有对账代码
        （`reconciliation_service.py` 的 P0-2 注释）也是这个口径：naive 本地时间。
        """
        stmt = (
            update(TaskCenterDocument)
            .where(
                TaskCenterDocument.document_id == document_id,
                TaskCenterDocument.tenant_id == tenant_id,
                TaskCenterDocument.task_id == task_id,
                TaskCenterDocument.state.in_(list(ACTIVE_STATES)),
            )
            .values(
                current_step=step_code,
                execution_id=execution_id,
                step_started_at=datetime.now(),
                # 新一步开始，清掉上一步留下的判死原因 —— 否则一次早已恢复的失败
                # 会一直挂在这一列上，把排障的人引向错误的方向。
                error_message=None,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def set_rag_task_id(
        self,
        document_id: str,
        *,
        execution_id: str,
        rag_task_id: str,
    ) -> bool:
        """回写本步的 atomic-rag 任务号。

        条件带 `execution_id`：如果这中间该行已经被 release 或换了执行编号，这次回写
        就落空（返回 `False`）—— 那意味着下游任务是个没人认领的产物，调用方应当放弃
        继续推进这一步，而不是把它当成成功。
        """
        stmt = (
            update(TaskCenterDocument)
            .where(
                TaskCenterDocument.document_id == document_id,
                TaskCenterDocument.execution_id == execution_id,
            )
            .values(rag_task_id=rag_task_id)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def mark_step_handled(
        self,
        document_id: str,
        *,
        execution_id: str,
        release: bool,
        report: dict | None = None,
    ) -> bool:
        """本步上报成功之后收口（**上报成功才调**，见 `services/task_watch_service.py`）。

        一次 UPDATE 里做**五件事**，**必须一起做**：

        - `handled_execution_id = execution_id` —— 审计 + 防御性幂等判据
        - `execution_id = NULL` —— 下轮看门狗看到的就是「在等下一步派活」
        - `rag_task_id = NULL` / `step_started_at = NULL` —— 让 `TC_NO_DOWNSTREAM_SEC`
          只衡量任务中心的派活延迟，而不是 atomic-rag 的执行时长
        - `release=True` 时 `state = released` —— compile 步专用（链路终点就是交接）
        - `last_report = report` —— **最后一次收口的那份上报报文**（2026-10-11 加）：
          管理方事后主动来问时（`query_step_status`）拿它当一条上报消化。
          ⚠️ 成功收口**必须带上产出**（`outputs`），否则管理方写了「成功」也派不出下一步

        `release=False` 时状态不动（仍是 `managed`）：parse / ingest 后面还有下一步，
        release 了就等于把还在链上的文档提前交还给既有对账，两边会同时动手。

        **也不动 `current_step`**：下一步的名字由任务中心派活时 `begin_step` 重新写上，
        这一列在「等下一步派活」的那段窗口里是**上一步的残留**（看门狗那一档压根不看它）。

        ⚠️ **这里曾经有个 `next_step` 形参**，是给「重新解析」那条链补调第二步用的
        （那时候任务中心的「重跑一步」还不连带下游，只能由看门狗推着走；2026-10-11 起
        `retry_subtask` 自己会连带下游了）。现在那两步由 `retry_subtasks`（§4.2 ④）
        在用户请求里**一次交出去**，先后由任务中心写进依赖 —— 补调这条路已经拆掉了，
        **别再加回来**：两边都推会让同一串步骤重跑两遍。

        条件带 `handled_execution_id IS DISTINCT FROM :execution_id`：上报成功后重复
        走到这里是 no-op（第二次 rowcount=0），这正是「上报与清空之间崩掉」那个
        残余窗口需要的幂等性。
        """
        values = {
            "handled_execution_id": execution_id,
            "execution_id": None,
            "rag_task_id": None,
            "step_started_at": None,
            # 「最后一次收口的那份上报报文」—— 走到这儿就是**成功收口**（失败走
            # `mark_released`）。管理方主动问时（`query_step_status`）读的就是它，
            # 拿去当一条上报消化（所以**必须带上产出**，见模型那一列的说明）。
            "last_report": report,
        }
        if release:
            values["state"] = TaskCenterDocState.RELEASED.value
        stmt = (
            update(TaskCenterDocument)
            .where(
                TaskCenterDocument.document_id == document_id,
                TaskCenterDocument.execution_id == execution_id,
                TaskCenterDocument.handled_execution_id.is_distinct_from(execution_id),
            )
            .values(**values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def rearm_for_step(
        self,
        document_id: str,
        *,
        tenant_id: str,
        task_id: str,
        step_code: str,
        clear_handled: bool = False,
    ) -> bool:
        """把一份**已经交还**的接管行重新武装起来，等某一步被重跑。

        ## 三个调用点

        · 「重新解析 / 重新编译」两个按钮（`rearm_for_rerun`）—— **不是新任务**，
          是「把这条任务里的某一步原地重跑一次」（任务中心 `retry_subtask`，按 `node_id`
          寻址）；
        · 任务中心的「**任务重试**」（`task_runner_service._prepare_step` 撞上一张已交还的
          接管行）—— 整条任务原地重跑，第一个派活又回来了。那种情况下传
          `clear_handled=True`（理由见下）。

        重跑之前必须先让这一行回到 `managed`：handler 的 `_prepare_step` 第一道闸就是
        「`state IN 在途`」，行还停在 `released` 的话，派过来的活会被**显式拒**，
        子任务直接终态 `failed`。

        ## 写进去的每一个值都有理由

        - `state=managed`：回到在途，看门狗重新盯它、handler 才收这次活。
        - `current_step=step_code`：**这一次要跑的是哪一步**。看门狗靠它分流
          （`compile` → 看 `ontology_status`；其余 → 问 atomic-rag）。
        - `execution_id=None`：新一次派活的执行编号还没来。看门狗据此走「在等派活」
          那一档；`set_rag_task_id` / `mark_step_handled` 的条件也靠它做 CAS。
        - `rag_task_id=None` / `step_started_at=None`：**上一次跑留下的痕迹必须清掉**。
          不清的话，新一步刚受理、看门狗就会拿那条**已经跑完的旧任务号**去问状态 ——
          当场把一次刚起头的事判成「已完成」或「已失败」。
        - `error_message=None`：清掉上一次判死的原因，否则一次早已恢复的失败会一直挂在
          这一列上，把排障的人引向错误的方向（`begin_step` 同一条理由）。

        **默认不动 `handled_execution_id`**：它记的是「上一次被收口的执行编号」，是审计值，
        下一次 `mark_step_handled` 会覆盖它。在这里顺手清掉只会白丢一段历史。

        ── `clear_handled`（2026-10-10 加，只服务「任务重试」）──

        `True` 时**连它一起清**。两个理由：

        1. **它在新一轮里是「上一轮的事实」，且恰好与一条巡检的判据同形**。
           `jonex_wiki_service.claim_compile(batch=True)` 有个 carve-out：
           `current_step='parse' AND execution_id IS NULL AND handled_execution_id IS NOT NULL`
           —— 含义是「parse 已经收口、可以派批量编译了」。而**刚重新武装、还没 `begin_step`**
           的行长得一模一样（`current_step` 是上一次的 `parse`、`execution_id` 刚被清空、
           `handled_execution_id` 还留着上轮的值）→ 巡检会拿上轮的审计值当本次事实，
           **提前把编译派出去**。清掉它，这个形状就不成立了。
        2. 语义上它就是该清：重新武装 = **上一轮已经结束、这一轮从头开始**，
           留着上轮的收口痕迹只会让「这一行的历史」和「这一行的现在」互相矛盾。

        ⚠️ 清掉**不破坏幂等**：`mark_step_handled` 的条件是
        `handled_execution_id IS DISTINCT FROM :execution_id`，清成 `NULL` 之后
        第一次上报必然写入（本来就该写）。

        ## 条件带 `state == released` 是**安全闸**，不是形式主义

        调用方在更早的地方用 `exists_active` 拦过一道（在途 = 用户必须等），但那是
        **另一次读**：从那次读到这次写之间，链子可能刚好收口、也可能反过来有别人接手
        （上传路径与看门狗是并发写这张表的）。带上它，写入落空（返回 `False`）就是
        「你读到的那个前提已经变了」—— 调用方按冲突处置，别当成功。

        `task_id` 也进条件：重跑的是**这一条任务**的步骤，指着别的任务的行是调用方拼错
        了参数（`task_id` 上有唯一索引，它才是「这份文档归哪条任务管」的定位键）。
        宁可 no-op 也不能改错行 —— 改错行的表现是「用户点了 A 文档的按钮、B 文档被重跑」。
        """
        values = {
            "state": TaskCenterDocState.MANAGED.value,
            "current_step": step_code,
            "execution_id": None,
            "rag_task_id": None,
            "step_started_at": None,
            "error_message": None,
            # 新一轮开始 —— 上一次的收口报文**作废**（否则管理方来问时会拿到上一轮的
            # 那份报文，把这一次的「还没跑」读成「已经成了」，还会拿旧产出去派下游）。
            "last_report": None,
        }
        if clear_handled:
            values["handled_execution_id"] = None
        stmt = (
            update(TaskCenterDocument)
            .where(
                TaskCenterDocument.document_id == document_id,
                TaskCenterDocument.tenant_id == tenant_id,
                TaskCenterDocument.task_id == task_id,
                TaskCenterDocument.state == TaskCenterDocState.RELEASED.value,
            )
            .values(**values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def mark_released(
        self,
        document_id: str,
        *,
        error_message: str | None = None,
        only_if_idle: bool = False,
        report: dict | None = None,
    ) -> bool:
        """判死 / 放弃接力：把接管行交还给既有对账（`state = released`）。

        **不动文档状态** —— 文档那一侧由调用方在同一次事务里改（`task_watch_service`
        与 `remove_task` 都是这么做的）。这里只管接管行：清了 `execution_id` 这些在途
        痕迹，`error_message` 留痕。

        ── `only_if_idle`（2026-09-30 加）──

        `True` 时**只交还「没有在途执行」的那一行**（`execution_id IS NULL`）。

        ⚠️ **`execution_id` 是「派活时」写上的，所以「它非空」就等于「有另一次派活正在
        用这张卡」** —— 那种情况下按 `document_id` 释放会把**别人的活**拆掉（那边的看门狗
        轮询、`rag_task_id` 全失效）。而「没人在途」的那一行（上一步
        `mark_step_handled` 之后 `execution_id` 已清、正等下一步派活）交还才是安全的。

        **什么时候该传 `True`**：**运行方自己主动放弃一张「它没收下活」的卡**时
        （典型是 `_prepare_step` 的那几个回绝点 —— 它还没 `begin_step`，卡上的
        `execution_id` 若为空就说明确实没人在途）。**什么时候别传**：卡上有**自己这次**
        的执行痕迹时（`_handle_step` 发下游失败那一支、`remove_task`）—— 那些是「我接了活、
        现在放弃」，本来就该释放，加了这道闸反而释放不掉、要白等 30 分钟。

        传 `True` 之后**可能什么都不做**（返回 `False`）—— 那正是想要的：说明另有人在途，
        让它自己收口；真没人管，30 分钟后由看门狗那条 `TC_NO_DISPATCH_SEC` 兜。
        """
        conditions = [
            TaskCenterDocument.document_id == document_id,
            TaskCenterDocument.state.in_(list(ACTIVE_STATES)),
        ]
        if only_if_idle:
            conditions.append(TaskCenterDocument.execution_id.is_(None))
        stmt = (
            update(TaskCenterDocument)
            .where(*conditions)
            .values(
                state=TaskCenterDocState.RELEASED.value,
                execution_id=None,
                rag_task_id=None,
                step_started_at=None,
                error_message=(error_message[:2000] if error_message else None),
                # 「最后一次收口的那份上报报文」由**调用方给**（`report=`）：
                #   判死（`_fail_step` / `_fail_document` / `_abandon`）传**失败报文**；
                #   「任务被删除」那种中性收口**不传** —— 保持 NULL，于是管理方来问时
                #   会得到「这活不归我」（既不算成、也不算败，与它的语义一致）。
                # ⚠️ 不传就是**写 NULL**（不是「别动」）—— 中性收口确实该把上一次的
                # 报文抹掉，否则一份旧的失败报文会一直挂在那儿骗后来的查询。
                last_report=report,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0


__all__ = ["TaskCenterDocumentRepository"]
