#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""落点 agent 运行记录仓储。

写/改走 `text()` + 具名参数（与 `data_governance` 的绑定仓储同一手法）：单测里一个
只记 `(sql, params)` 的假 session 就能断言「改了哪几列、换成了什么值」，不必起库。
读走 `select()`，直接回 ORM 对象（`to_dict()` 在模型上）。

所有写语句都带 `tenant_id` + `is_deleted=0`：run 由后台任务按 run_id 推进，
它是**唯一**不经过用户请求上下文的写路径 —— 少一个租户谓词就是跨租户改行。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from sqlalchemy import select, text

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.abstract_landing_run import (
    ACTIVE_RUN_STATUSES,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    AbstractLandingRun,
)

logger = logging.getLogger(__name__)

_TABLE = "knowledge_base.abstract_landing_runs"

#: 占位 → 运行中。`started_at` 用 COALESCE：重试 / 重复调度不能把「开始时间」
#: 改写成第二次的时间（排障时那个时间点是唯一的）。
#:
#: `AND status='queued'` 是状态机的守卫，不是可选的优化：重复投递 / 迟到的重试
#: 若把已经 `succeeded`（或 `failed`）的行改回 `running`，部分唯一索引会一直认为
#: 该 KB 有占用中的 run —— v1 没有取消也没有 stale 回收，这个 KB 就再也开不了新
#: run，而且界面上永远显示「跑着」。终态行只能被读取，不能被这条语句复活。
_SET_RUNNING_SQL = f"""
    UPDATE {_TABLE}
       SET status=:status,
           started_at=COALESCE(started_at, now()),
           updated_at=now()
     WHERE tenant_id=:tenant_id AND id=:run_id
       AND status='{RUN_STATUS_QUEUED}' AND is_deleted=0
"""

#: 进度只覆盖 phase / progress，不碰终态列 —— 进度回写与收口是两个独立动作，
#: 后台任务晚到一个进度包时不能把已经写好的 finished_at 覆盖掉。
_UPDATE_PROGRESS_SQL = f"""
    UPDATE {_TABLE}
       SET phase=:phase,
           progress=CAST(:progress AS JSONB),
           updated_at=now()
     WHERE tenant_id=:tenant_id AND id=:run_id AND is_deleted=0
"""

# `finish` 用动态 SET，是为了让「没给的列完全不出现在语句里」—— 成功收口不该把
# 上一轮的 error 清成 NULL，失败收口也不该把 summary 清掉。SET 里的列名全部来自
# 本函数的字面量，不接受调用方传列名。
#
# 终态守卫：**只有占用中的行能被收口**。存在两条真实竞态：① stale 回收判某行
# 「30 分钟没进度」，但那条 run 其实刚好跑完并写了 succeeded —— 晚到的 failed
# 收口会把成功盖成失败；② 收口与重复投递的 execute 交错时，反向也会发生。谓词
# 放在 SQL 里由库判定，应用层的 `if` 挡不住这两个窗口。
_FINISH_ACTIVE_PREDICATE = (
    f"status IN ('{RUN_STATUS_QUEUED}', '{RUN_STATUS_RUNNING}')"
)


def _warn_if_no_row(
    result, *, action: str, tenant_id: str, run_id: str,
) -> None:
    """0 行受影响只告警、不抛。

    后台任务是「按 run_id 推进」的：行可能已被回收、软删、租户不符，或状态已被
    另一条路径改走。这类情况抛异常会把「一次无害的迟到写入」升级成任务失败；
    但完全沉默又会让持久化失败被当成成功，所以至少留一条 warning。
    `rowcount` 缺失（例如驱动不提供）时不做判断。
    """
    if getattr(result, "rowcount", None) == 0:
        logger.warning(
            "落点 run 写入未命中（%s）：tenant=%s run=%s —— "
            "行不存在/已软删/状态已变，本次写入按无操作处理",
            action, tenant_id, run_id,
        )


class AbstractLandingRunRepository(BaseRepository[AbstractLandingRun]):
    """run 表的读写；业务规则（谁能开跑、并发上限）不在这里。"""

    model = AbstractLandingRun

    async def create(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        run_mode: str,
        created_by: str = "",
        *,
        force: bool = False,
        alias_hints: str = "",
    ) -> AbstractLandingRun:
        """建一条 queued 行。

        默认值**显式写死在实例上**，不依赖列的 `default=`：那要等 flush 才生效，
        而调用方拿到 run 之后立刻要回 `run_id / status` 给前端，也立刻会读
        `progress`（初始必须是 `{}` 而不是 None）。

        `force` / `alias_hints` 是**提交时的构造参数**，必须随行落库：后台
        `execute` 只拿 run_id，靠行上的值决定 full 模式的覆盖语义与业务词表。
        """
        tid = require_tenant(tenant_id)
        row = AbstractLandingRun(
            id=uuid.uuid4().hex,
            tenant_id=tid,
            knowledge_base_id=knowledge_base_id,
            run_mode=run_mode,
            force=bool(force),
            alias_hints=alias_hints or "",
            status=RUN_STATUS_QUEUED,
            phase="",
            progress={},
            summary={},
            created_by=created_by or "",
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(
        self, tenant_id: str, run_id: str,
    ) -> Optional[AbstractLandingRun]:
        """按 (租户, id) 取一行；软删行不返回。"""
        tid = require_tenant(tenant_id)
        stmt = select(AbstractLandingRun).where(
            AbstractLandingRun.id == run_id,
            AbstractLandingRun.tenant_id == tid,
            AbstractLandingRun.is_deleted == 0,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_active(
        self, tenant_id: str, knowledge_base_id: str,
    ) -> Optional[AbstractLandingRun]:
        """该 KB 当前占用中的 run（queued / running），没有则 None。

        部分唯一索引保证至多一条；这里仍带 `LIMIT 1` + 按创建时间倒序，
        是为了索引尚未应用的库上不会因为多行而整条链路报错。
        """
        tid = require_tenant(tenant_id)
        stmt = (
            select(AbstractLandingRun)
            .where(
                AbstractLandingRun.tenant_id == tid,
                AbstractLandingRun.knowledge_base_id == knowledge_base_id,
                AbstractLandingRun.status.in_(ACTIVE_RUN_STATUSES),
                AbstractLandingRun.is_deleted == 0,
            )
            .order_by(AbstractLandingRun.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def set_running(self, tenant_id: str, run_id: str) -> bool:
        """queued → running，并记下首次开始时间；终态行不会被改回来。

        **返回是否真的发生了状态迁移**（rowcount 判定）：重复投递时另一条任务
        已经把行改成 running/终态，本语句 0 行命中。调用方必须据此**放弃执行**
        —— 否则重复投递会把同一批落点重跑一遍。驱动不提供 rowcount 时按
        「迁移成功」处理（不能因为拿不到计数就把正常路径判成重复投递）。
        """
        tid = require_tenant(tenant_id)
        result = await self.session.execute(
            text(_SET_RUNNING_SQL),
            {"tenant_id": tid, "run_id": run_id, "status": RUN_STATUS_RUNNING},
        )
        _warn_if_no_row(result, action="set_running", tenant_id=tid, run_id=run_id)
        return getattr(result, "rowcount", None) != 0

    async def update_progress(
        self, tenant_id: str, run_id: str, *, phase: str, progress: dict,
    ) -> None:
        """覆盖式写进度快照（前端只关心最新全量，不做增量合并）。

        JSONB 参数显式 CAST：asyncpg 无法从 `None` 推断参数类型，而空进度是合法值。
        """
        tid = require_tenant(tenant_id)
        result = await self.session.execute(
            text(_UPDATE_PROGRESS_SQL),
            {
                "tenant_id": tid,
                "run_id": run_id,
                "phase": phase or "",
                "progress": json.dumps(progress or {}, ensure_ascii=False),
            },
        )
        _warn_if_no_row(
            result, action="update_progress", tenant_id=tid, run_id=run_id,
        )

    async def finish(
        self,
        tenant_id: str,
        run_id: str,
        *,
        status: str,
        summary: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> None:
        """写终态。只改 status /（可选）summary /（可选）error + 时间戳。

        状态守卫在 SQL 谓词里：行已是终态（例如被判 stale 后其实跑完并写了
        succeeded）时本语句 0 行命中 —— 告警、不抛、也不改写既有终态。
        """
        assignments = ["status=:status", "finished_at=now()", "updated_at=now()"]
        params = {
            "tenant_id": require_tenant(tenant_id),
            "run_id": run_id,
            "status": status,
        }
        # 只追加调用方明确给了的列：None 的语义是「保持原值」，不是「写 NULL」。
        # summary 的列约束是 NOT NULL，写 NULL 会直接 500；error 传 None 表示
        # 「这次成功、不带错误」，但它同样不该抹掉历史错误文本。progress / phase
        # 同理：收口不负责清理进度，前端要靠它显示最终统计。
        if summary is not None:
            assignments.append("summary=CAST(:summary AS JSONB)")
            params["summary"] = json.dumps(summary, ensure_ascii=False)
        if error is not None:
            assignments.append("error=:error")
            params["error"] = error
        sql = (
            f"UPDATE {_TABLE} SET "
            + ", ".join(assignments)
            + " WHERE tenant_id=:tenant_id AND id=:run_id AND is_deleted=0"
            + f" AND {_FINISH_ACTIVE_PREDICATE}"
        )
        result = await self.session.execute(text(sql), params)
        _warn_if_no_row(
            result, action="finish", tenant_id=params["tenant_id"], run_id=run_id,
        )


__all__ = ["AbstractLandingRunRepository"]
