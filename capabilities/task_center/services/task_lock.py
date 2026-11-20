#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务锁：任务中心并发控制的第一道闸（§7.2 ①）。

`state_version` 是最后一道防线，这里这把 Redis 锁是**第一道** ——
**对任务的任何更新，先拿这把锁**。

## 粒度与做法

**粒度：按任务** —— 一把锁盖住「这条任务的全部数据」（`tasks` / `task_attempts` /
`subtasks` / `subtask_attempts` 四张表里属于它的行）。**锁在「任务」这一层、不在「子任务」层**：
改一个子任务常常连带要动 `tasks` 快照（转 `running` / 收尾），分两把锁反而要处理锁顺序、
容易死锁。代价是同一条任务上的更新串行化，但一条任务的更新本来就不频繁。

| 环节 | 做法 |
|------|------|
| key | `task_center:lock:task:{task_id}` |
| 获取 | `SET key <token> NX PX 30000` —— `NX` 保证只有一个能拿到；`PX` 过期是为了防「持锁进程崩了导致死锁」 |
| token | 每次获取生成一个随机 UUID，**存在本地、不共享** |
| 释放 | **必须先比对 token 再删**（Lua）—— 防「A 的锁超时自动过期、B 拿到了，A 干完回来把 B 的锁删了」 |

上面这套 `CacheUtil.acquire_lock` / `CacheUtil.release_lock` 已经实现好了（UUID token +
`set(nx=True, px=...)` + Lua 比对删除），本模块只是**在它外面套一层任务中心的口径**：
key 前缀、重试窗口、以及拿不到锁时该抛什么。

## 拿不到锁怎么处置（**按调用方分三类**，§7.2 ① 的「这一点很关键」）

`task_lock()` 先做**短重试**（`TASK_CENTER_LOCK_RETRY`，默认 3 次 × 50ms），
仍拿不到就抛 `TaskLockBusy`，**由调用方按自己的类别处置**：

- **调度循环里「跳过也没关系」的那几趟**（派活抢占 `_dispatch_one`、消化上报
  `_report_one`、超时巡检、防僵尸）→ 捕获后**本轮跳过**（下一轮还会扫到，天然可重来）。
  这是默认口径，**不传 `patient`**；
- **调度循环里「跳过会丢东西」的那一趟**（派活收尾 `_finalize_one`：出站调用已经正常
  返回、运行方很可能已经收下这一步）→ 传 `patient=True` **有心等**（有界窗口
  ≈3.15 秒，见下）；窗口内仍拿不到**只记一条 warning、不往调用方抛** —— 子任务留在
  `running`，等运行方上报正常推进，真没动静由超时巡检按 `timeout_seconds` 兜底（§7.4）；
- **调用方主动发起的写操作**（`retry_task` / `retry_subtask` / `retry_subtasks` /
  `stop_task` / `stop_subtask` / `delete_task` 这**六个** —— 人或运维点出来的）→
  **绝不能跳过**：传 `patient=True`，
  **继续等锁**（同一个窗口：`TASK_CENTER_LOCK_WAIT`，默认 30 次 × 100ms）；
  仍拿不到就**抛 `TaskLockBusy`**（= 3007，天然可重试），由 service 层翻成
  `success=false` + 可重试的错误码，让**调用方自己重试**。

> **为什么「绝不能跳过」**：跳过 = 调用方以为成了、事却没做 ——
> 重试 / 删除 / 停止丢了页面却显示成功。**不能静默丢弃。**
>
> ⚠️ **`patient=True` 是「有心等」，不是「等到底」**：它是 `1 + 3×50ms + 30×100ms
> ≈ 3.15 秒` 的**有界窗口**，窗口内等不到照样抛 `TaskLockBusy`（怎么处置看上面三类）。

`TaskLockBusy` 继承 `RateLimitExceededError`（`code=3007`）：文档把「拿不到锁」写死映射到它
（语义正是「暂时挤不进去、稍后重发」），这样调用方拿到的 `code` 天然落在
`TASK_CENTER_RETRYABLE_CODES` 里。

## 不拿锁的三类操作（别漏也别多，§7.2 ①）

- **`report_status` 上报**：同步那一半**只校验报文 + 入队**（不拿锁、不写库），
  真正的裁决与落库在**后台循环消化那一趟**（`_report_one`）里拿锁做 —— 所以
  `report_status` **自己永远不碰这把锁**。它从前是拿的（等不到就回可重试码、让运行方
  自己重发），改成入队之后就不拿了；
- **`fire` / `trig.fire` 落库**：**新任务还没有 `task_id`，没有可锁的对象** ——
  靠「先建 `tasks` 拿到 `id`、再在同一事务里建另外三张表」保证原子性；
- **`cron` 触发抢占**：抢的是「这条触发器这次归谁触发」，**触发器不是任务** ——
  用 `task_triggers.state_version` 的 CAS 就够（§7.5 ⓪）。

`task.delete` 整条流程**是要拿锁的**（假删那一步要写库，拿不到锁就回可重试码、
不跳过），只是它调运行方 `remove_task` **那一下不持锁**。

## 锁的持有范围：跨不跨出站调用（**写死，否则「短事务」这个前提不成立**）

有些更新**中间要调运行方**（网络调用，可能几百毫秒到几秒）：`execute` 派活、
`remove_task` 删整个任务。**规则：锁只护「读—改—写」这段数据库操作，不跨出站调用拿。**

1. **调运行方之前**：拿锁 → 把要写的东西（如把子任务抢占成 `running`、回填 `execution_id`）
   写进库 → **释放锁**；
2. **调运行方之后**：**重新拿锁**（如果还要写库，比如 `execute` 明确失败要把子任务置 `failed`）
   → 写完 → 释放。

**为什么不能跨**：网络调用耗时不可控（运行方卡 30 秒 → 锁自动过期 → 等于没锁，别的副本会
同时进来改）；而且持锁做网络 IO 会把整条任务的更新都堵住。
**代价与兜底**：两次拿锁之间别的副本可能插进来 —— 但 `state_version` 的 CAS 是最后一道防线：
第二次拿锁后仍带 `WHERE state_version = :原值`，值变了就重读再来、**不会写脏**。

> 用法上就体现为：`async with task_lock(task_id): ...` 里**不要 await 出站调用**，
> 出站调用挪到 `async with` 块**外面**。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.exceptions import RedisError

from jonex_core.common.cache import CacheUtil
from jonex_core.common.config import get_config
from jonex_core.common.exceptions import CacheError, RateLimitExceededError

logger = logging.getLogger(__name__)

# 锁的键前缀：带业务前缀，避免跟别的模块撞（§7.2 ①）
LOCK_KEY_PREFIX = "task_center:lock:task:"

#: 定义锁前缀（2026-11-18 加，需求 12）：编排保存 / 发布 / 停用 / 删除按定义串行。
DEFINITION_LOCK_KEY_PREFIX = "task_center:lock:definition:"


def definition_lock_key(definition_id: str) -> str:
    """`task_center:lock:definition:{definition_id}`（需求 12/13/14）。"""
    return f"{DEFINITION_LOCK_KEY_PREFIX}{definition_id}"


def lock_key(task_id: str) -> str:
    """`task_center:lock:task:{task_id}`（§7.2 ①）。"""
    return f"{LOCK_KEY_PREFIX}{task_id}"


class TaskLockBusy(RateLimitExceededError):
    """重试窗口内一直没拿到任务锁。

    继承 `RateLimitExceededError`（`code=3007`）是**文档写死的映射**
    （§4.0：「拿不到锁」→ `RateLimitExceededError`），
    这样它天然落在「可重试」那一类里，调用方拿 `code` 就能判该不该重发。
    """

    default_message = "任务正在被处理，请稍后重试"


async def _try_acquire(task_id: str) -> str:
    """抢一次锁。返回 token；返回 `""` = 这一轮没抢到（被别人持有）。

    **Redis 故障不在这里吞掉**：文档要求「内部异常按实际类型给」——
    Redis → `CacheError`（4xxx、可重试）。`CacheUtil` 的重试装饰器最终抛的是裸的
    `RedisError`，这里补上这一层翻译，免得裸异常漏到调用方那边、连「可不可重试」都判不出来。
    """
    try:
        return await CacheUtil.acquire_lock(
            lock_key(task_id), get_config().TASK_CENTER_LOCK_TIMEOUT
        )
    except (RedisError, ConnectionError, TimeoutError, OSError) as exc:
        raise CacheError(f"任务锁获取失败（Redis 不可用）：{exc}") from exc


async def acquire_task_lock(task_id: str, *, patient: bool = False) -> str:
    """按文档的重试窗口抢任务锁。返回 token；**返回 `""` = 没抢到**。

    `patient=False`（跳过也没关系的那几趟，见模块 docstring 第一类）：首次 + 最多
    `TASK_CENTER_LOCK_RETRY` 次重试，每次间隔 `TASK_CENTER_LOCK_RETRY_INTERVAL`
    （默认 3 次 × 50ms）；
    `patient=True`（有心等的调用方：用户主动写操作 + 派活收尾）：上面没抢到**不是结束** ——
    再拉长窗口等 `TASK_CENTER_LOCK_WAIT` 次 × `TASK_CENTER_LOCK_WAIT_INTERVAL`
    （默认 30 次 × 100ms），仍拿不到才算没抢到。

    一般不用直接调它，用 `task_lock()`（保证释放）。
    """
    config = get_config()

    token = await _try_acquire(task_id)
    if token:
        return token

    for _ in range(max(0, config.TASK_CENTER_LOCK_RETRY)):
        await asyncio.sleep(config.TASK_CENTER_LOCK_RETRY_INTERVAL)
        token = await _try_acquire(task_id)
        if token:
            return token

    if not patient:
        return ""

    # 「有心等」的调用方：不在这里收工，继续等（拉长窗口）
    logger.warning(
        "[TaskCenter] 任务 %s 的锁短重试后仍未拿到，拉长窗口继续等（patient）", task_id
    )
    for _ in range(max(0, config.TASK_CENTER_LOCK_WAIT)):
        await asyncio.sleep(config.TASK_CENTER_LOCK_WAIT_INTERVAL)
        token = await _try_acquire(task_id)
        if token:
            return token
    return ""


async def release_task_lock(task_id: str, lock_id: str) -> bool:
    """释放任务锁（Lua 比对 token 再删）；`lock_id` 对不上时返回 `False`、不删。

    **释放失败不在调用方抛异常**：锁会靠 `PX 30000` 自然过期，而「释放失败」这件事
    本身不该盖掉块内正在抛的业务异常（在 `finally` 里再抛一个，原始异常就丢了）。
    """
    try:
        return await CacheUtil.release_lock(lock_key(task_id), lock_id)
    except (RedisError, ConnectionError, TimeoutError, OSError) as exc:
        logger.warning("[TaskCenter] 任务 %s 的锁释放失败（会等 PX 自然过期）：%s", task_id, exc)
        return False


async def _acquire_definition_lock(definition_id: str) -> str:
    try:
        return await CacheUtil.acquire_lock(
            definition_lock_key(definition_id), get_config().TASK_CENTER_LOCK_TIMEOUT
        )
    except (RedisError, ConnectionError, TimeoutError, OSError) as exc:
        raise CacheError(f"定义锁获取失败（Redis 不可用）：{exc}") from exc


@asynccontextmanager
async def definition_lock(
    definition_id: str, *, patient: bool = True
) -> AsyncIterator[None]:
    """定义锁的上下文管理器（编排保存 / 发布 / 停用 / 删除共用）。

    **默认 `patient=True`**：这些动作都是人点出来的、绝不能静默跳过 —— 拿不到锁就抛
    `TaskLockBusy`（3007，可重试），让调用方自己重来。窗口与任务锁同一个
    `TASK_CENTER_LOCK_*` 配置。

    ⚠️ 锁只护「读—改—写」这段数据库操作；不要在块内 `await` 出站调用
    （与任务锁同一条纪律，见模块 docstring）。
    """
    lock_id = await acquire_definition_lock(definition_id, patient=patient)
    if not lock_id:
        raise TaskLockBusy(f"任务定义 {definition_id} 正在被修改，没拿到锁")
    try:
        yield
    finally:
        await release_definition_lock(definition_id, lock_id)


async def acquire_definition_lock(definition_id: str, *, patient: bool = True) -> str:
    """抢定义锁；`patient` 的窗口语义与 `acquire_task_lock` 完全一致。"""
    config = get_config()
    token = await _acquire_definition_lock(definition_id)
    if token:
        return token
    for _ in range(max(0, config.TASK_CENTER_LOCK_RETRY)):
        await asyncio.sleep(config.TASK_CENTER_LOCK_RETRY_INTERVAL)
        token = await _acquire_definition_lock(definition_id)
        if token:
            return token
    if not patient:
        return ""
    for _ in range(max(0, config.TASK_CENTER_LOCK_WAIT)):
        await asyncio.sleep(config.TASK_CENTER_LOCK_WAIT_INTERVAL)
        token = await _acquire_definition_lock(definition_id)
        if token:
            return token
    return ""


async def release_definition_lock(definition_id: str, lock_id: str) -> bool:
    try:
        return await CacheUtil.release_lock(definition_lock_key(definition_id), lock_id)
    except (RedisError, ConnectionError, TimeoutError, OSError) as exc:
        logger.warning(
            "[TaskCenter] 定义 %s 的锁释放失败（会等 PX 自然过期）：%s",
            definition_id,
            exc,
        )
        return False


@asynccontextmanager
async def task_lock(task_id: str, *, patient: bool = False) -> AsyncIterator[None]:
    """任务锁的上下文管理器。没抢到就抛 `TaskLockBusy`（调用方按类别处置）。

    ```python
    # 调度循环类：抢不到 → 本轮跳过
    try:
        async with task_lock(subtask.task_id):
            ...  # 只做「读—改—写」，这段里不要 await 出站调用
    except TaskLockBusy:
        continue

    # 调用方主动发起的写操作：抢不到 → 回可重试码（patient 会多等一轮长窗口）
    async with task_lock(task_id, patient=True):
        ...
    ```
    """
    lock_id = await acquire_task_lock(task_id, patient=patient)
    if not lock_id:
        raise TaskLockBusy(f"任务 {task_id} 正在被处理，没拿到锁")
    try:
        yield
    finally:
        await release_task_lock(task_id, lock_id)


__all__ = [
    "DEFINITION_LOCK_KEY_PREFIX",
    "LOCK_KEY_PREFIX",
    "TaskLockBusy",
    "acquire_definition_lock",
    "acquire_task_lock",
    "definition_lock",
    "definition_lock_key",
    "lock_key",
    "release_definition_lock",
    "release_task_lock",
    "task_lock",
]
