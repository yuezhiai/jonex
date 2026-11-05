# -*- coding:utf-8 -*-
"""演化运行后台执行器：抢占 → 全流程 → 状态收口（异常不得留 running）。

状态机收口分两层：
- `run_pipeline` 负责正常推进并写出 `evaluated` / `dry-run` / `empty-draft`；
- `EvolutionRunner.execute` 是唯一的失败收口点：任何异常一律
  `status=failed`，失败阶段写进 `error` 前缀。迁移 016 的 stage CHECK
  只接受固定白名单，故 **绝不写 `failed@xxx`**，stage 保持最后合法值。

两道门防「回收后回写」：
- 所有写库都带 `expect_statuses={"running"}` 状态 CAS（pipeline 见 `_advance`）；
- 失败收口同样 CAS —— `recover_stale` 已把超时运行置 `failed` 时，这次
  `failed` 写入命中 0 行，**只记日志、不写任何字段**，更不能把 `failed`
  翻成 `evaluated`。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

#: `run_pipeline` 已经写库、runner 不得再覆盖的终态。
_SETTLED_STATUSES = frozenset({"evaluated", "dry-run", "empty-draft"})


class EvolutionRunner:
    """单进程内的后台执行器；多副本去重靠 `RunStore.claim` 抢占。"""

    def __init__(self, store) -> None:
        self.store = store
        self._tasks: set[asyncio.Task] = set()

    def schedule(self, tenant_id: str, run_id: str) -> None:
        """`evolution_start_run` 插行成功后调用，不阻塞请求。"""
        task = asyncio.create_task(self.execute(tenant_id, run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _log(self, tenant_id: str, run_id: str, line: str) -> None:
        """运行日志兼心跳：写失败只记 warning，不影响状态收口。"""
        try:
            await self.store.log(tenant_id, run_id, line)
        except Exception as exc:  # noqa: BLE001 —— 日志不是关键路径
            logger.warning("演化运行日志写入失败 run=%s: %s", run_id, exc)

    async def _close_failed(self, tenant_id: str, run_id: str, error: str) -> None:
        """失败收口：状态 CAS 在 `running` 上；未命中（已被回收）只记日志，不写库。"""
        written = await self.store.update(
            run_id, status="failed", error=error, expect_statuses={"running"})
        if written:
            await self._log(tenant_id, run_id, f"failed: {error[:500]}")
            return
        logger.warning("演化运行已被回收/结束，跳过失败收口 run=%s", run_id)
        await self._log(tenant_id, run_id,
                        f"failed: 运行已被回收/结束（未写库）{error[:500]}")

    async def execute(self, tenant_id: str, run_id: str) -> None:
        if not await self.store.claim(tenant_id, run_id):
            return  # 别的副本已抢到（或该行已不是 queued）
        await self._log(tenant_id, run_id, "running")

        stage = "created"

        def on_stage(new_stage: str) -> None:
            nonlocal stage
            stage = new_stage

        try:
            run = await self.store.get(tenant_id, run_id)
            outcome = await self._engine(tenant_id, run, on_stage=on_stage)
        except Exception as exc:  # noqa: BLE001 —— 失败必须收口
            logger.warning("演化运行失败 run=%s stage=%s err=%s", run_id, stage, exc)
            error = f"{stage}: {str(exc)[:1000]}"
            await self._close_failed(tenant_id, run_id, error)
            return

        status = outcome.get("status") if isinstance(outcome, dict) else None
        if status in _SETTLED_STATUSES:
            # dry-run / empty-draft / evaluated 已由 pipeline 写库，这里只补终态日志。
            await self._log(tenant_id, run_id, str(status))
            return
        # 只认 `{"status": "evaluated"}`：None / 未知值是 pipeline 契约被破坏的信号，
        # 绝不能当成功（否则 runner 会替它写 evaluated、把异常运行伪装成正常收口）。
        logger.warning("演化运行 pipeline 返回未知结果 run=%s outcome=%r",
                       run_id, outcome)
        await self._close_failed(tenant_id, run_id, "pipeline 返回未知结果")

    async def _engine(self, tenant_id: str, run: dict, *, on_stage=None):
        from .engine import run_pipeline

        return await run_pipeline(tenant_id, run, store=self.store,
                                  on_stage=on_stage)
