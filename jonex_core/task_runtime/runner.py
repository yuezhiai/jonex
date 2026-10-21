#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""`RunnerBase`：§4.1 的落地基类 —— **运行方继承它，只写每个 step 干什么**。

```python
from jonex_core.capability.response import ApiResponse
from jonex_core.task_runtime import RunnerBase, require_execution_context

class ExampleRunner(RunnerBase):
    runner_code = "example_runner"

    def handlers(self):
        return {"parse": self._parse, "ingest": self._ingest, "compile": self._compile}

    async def _parse(self, execution):
        ctx = require_execution_context()      # 幂等键用它：f"task-execution:{ctx.execution_id}"
        internal_task_id = await self.launch_internal_work(execution.inputs)
        await self.report_success({"internal_task_id": internal_task_id})
        return ApiResponse.ok()

    async def remove_task(self, tenant_id, space_id, task_id):
        ...                                    # 清掉本运行方侧这个任务的全部残留，必须幂等

# 装配（在能力的 _build_dispatch() 里）—— 就这一行：
dispatch.update(ExampleRunner().bind())
```

基类兜住五件事，运行方一个都不用自己写：

1. **`step_code` 分流** —— 运行方用 `handlers()` 登记；
2. **「受理没成立」一律显式拒绝 + 撤销登记** —— 三种都算：未知 `step_code`、
   handler 抛异常、handler **返回值不是 `ApiResponse`**（忘了 `return`、或按能力层
   `dispatch` 的直觉回了裸 dict）。既不静默跳过，**也不让它们变成 500**
   （500 会被派活侧读成「没够着运行方」→ 传送层重发，真因全丢）。
   ⚠️ **这一条只管「受理没成立」那三种**：基类**自己**出的意外故障（登记落库失败、
   报文构造时抛的其它异常…）照旧会抛成 500 —— 那不是「收不下」的表态，运行方侧
   真的是炸了，按「未知」重发正是对的；**别指望 SDK 把所有异常都吞成失败壳**；
3. **重复派同一个 `execution_id` 幂等受理** —— 管理方**会重派**（见 `execute`），
   认出来一律回成功，不重复干活；
4. **执行上下文进出** —— handler 里随时 `require_execution_context()`；
5. **能力层装配的形状转换** —— 用 `bind()`，**别自己写 `dispatch` 条目**。

### 为什么 `bind()` 非有不可（两个都会静默坏掉，不是报错）

- **签名**：能力层的分发约定是 `handler(request, data)` **两个位置参数**（进 handler 前
  还要过 `validate_input`，要求 `action in _dispatch` 且 `data` 是 dict）。
  `dispatch["execute"] = runner.execute` 是 `TypeError` → 被兜成 500 → 派活侧判
  「运行方收不了」→ **子任务直接终态 `failed`**。
- **回执形状**：派活侧判回执用的是 `isinstance(receipt, dict) and "success" in receipt`。
  回一个 pydantic 对象时 `isinstance(obj, dict)` 为 False → 落到 `ApiResponse.ok()` →
  **「未知步骤显式拒绝」被读成成功**，子任务停在 `running` 直到超时。而远端那条路经过
  JSON 会转成 dict，所以**同一个运行方 LOCAL 与 REMOTE 两副面孔** —— 正是
  「切换部署画像不改业务代码」要避免的东西。

`bind()` 的返回值签名对得上、已经是 `.to_dict()` 过的裸 dict，这两个坑在 SDK 里就没了。
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, ClassVar, Dict, List, Optional

from jonex_core.capability.response import ApiResponse
from jonex_core.common.i18n import translate
from jonex_core.task_protocol.models import (
    STEP_STATUS_VALUES,
    ExecutionDispatch,
    ExecutionEnvelope,
    RemoveTaskDispatch,
    StepStatusQuery,
    TaskStoppedDispatch,
)
from jonex_core.task_protocol.results import TerminalSubmitResult
from jonex_core.task_runtime.context import acquire_execution, require_execution_context
from jonex_core.task_runtime.registration import (
    ClaimOutcome,
    ExecutionRegistry,
    InMemoryExecutionRegistry,
)
from jonex_core.task_runtime.runtime import TaskRuntime, TerminalReport, get_task_runtime

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import ValidationError
except ImportError:  # pragma: no cover - 仅 pydantic v1 原生安装时走到
    from pydantic import ValidationError


class RunnerBase(ABC):
    """§4.1 两个方法的落地基类。见模块 docstring 的用法与那两条硬坑。"""

    #: 运行方标识。**装饰性的**：派活侧认的是 DB（`task_catalog_steps.runner_code`
    #: → `runner_definitions.capability_id`），真正的注册动作是往库里插一行，
    #: 不是在 Python 里写这个属性。写上它只是为了让日志和自检认得出来。
    runner_code: ClassVar[str] = ""

    def __init__(
        self,
        runtime: Optional[TaskRuntime] = None,
        registry: Optional[ExecutionRegistry] = None,
    ) -> None:
        # `runtime=None` 走进程单例，**且是构造时就取**（不是用到才取）：忘了在启动入口
        # `build_task_runtime()` 的话，这里当场抛 `TaskRuntimeNotBuiltError` —— 早失败，
        # 而不是等到几十分钟后某一次上报才炸。
        self._runtime: TaskRuntime = runtime if runtime is not None else get_task_runtime()
        # 默认给内存实现（开箱能用），它构造时会打一条 warning 说明重启 / 多副本会失效。
        self._registry: ExecutionRegistry = (
            registry if registry is not None else InMemoryExecutionRegistry()
        )

    @property
    def registry(self) -> ExecutionRegistry:
        """给自检用（比如启动时断言「我用的是不是持久实现」，看 `.persistent`）。"""
        return self._registry

    # ========================================================
    # 装配：运行方在能力的 _build_dispatch() 里写这一行
    # ========================================================

    def bind(self) -> Dict[str, Callable[..., Awaitable[Dict[str, Any]]]]:
        """返回四个可直接 `dispatch.update()` 进去的入口。

        **签名与返回值形状都由 SDK 保证**（见模块 docstring 的两条硬坑）：
        两个位置参数 `(request, data)`，返回**裸 dict**（已 `.to_dict()` 过）。
        运行方不需要知道能力层这两条约定存在。

        （`query_step_status` 是 2026-10-11 加的第四个 —— 老运行方**不用改**
        `_build_dispatch`：`bind()` 返回的 dict 一并更新，它们白捡一个「管理方能来问」的入口。）
        """
        return {
            "execute": self._on_execute,
            "remove_task": self._on_remove_task,
            "task_stopped": self._on_task_stopped,
            "query_step_status": self._on_query_step_status,
        }

    async def _on_execute(self, request: Any, data: Any) -> Dict[str, Any]:
        try:
            execution = ExecutionDispatch(**data)
        except (ValidationError, TypeError) as exc:
            # 报文本身不合法（派活侧发错了 / 字段缺了）：回**规范失败壳**、不要让它变成 500。
            # 500 会让派活侧判「运行方收不了」，效果一样是失败，但丢了原因、也没法区分
            # 「报文错」和「运行方崩了」。
            return ApiResponse.fail(f"派活报文不合法：{exc}").to_dict()
        # ⚠️ **这里刻意没有 `isinstance(result, ApiResponse)` 那道闸**（`_on_remove_task`
        # 有）—— 不是漏了：`self.execute` 是**基类**的实现，它每一条路径（已受理 / handler
        # 拒绝 / handler 抛异常 / handler 返回值形状错）都保证回 `ApiResponse`；
        # 而 `remove_task` 是**运行方覆写**的，返回形状 SDK 管不到，所以那边才要拦。
        # 别为了「两边对称」把这道闸加回来 —— 它永远为真，只会让人以为 `execute` 也可能违规。
        return (await self.execute(execution)).to_dict()

    async def _on_remove_task(self, request: Any, data: Any) -> Dict[str, Any]:
        """⚠️ **只拦「返回值形状不对」，不拦运行方自己抛异常**（与 `_on_execute` 有意不同）。

        运行方的 `remove_task` 抛异常 → 原样冒到能力层 → HTTP 500 → 删除侧按
        「没够着运行方」**重发 3 次**（退避 2s / 5s），用户那一次 `task.delete` 最坏干等
        ≈ 3 × 出站超时 + 7 秒。**不兜成失败壳是刻意的**：抛异常多半意味着「没清干净」，
        按未知重发正是对的（`remove_task` 必须幂等 —— 那是重发能成立的前提）；
        兜成「明确拒绝」反而会让删除**当场中止**、连再试一次的机会都没有。
        """
        try:
            dispatch = RemoveTaskDispatch(**data)
        except (ValidationError, TypeError) as exc:
            return ApiResponse.fail(f"删除报文不合法：{exc}").to_dict()
        result = await self.remove_task(dispatch.tenant_id, dispatch.space_id, dispatch.task_id)
        if not isinstance(result, ApiResponse):
            # 与派活那侧同源的形状错误（`remove_task` 是运行方覆写的，返回值形状 SDK
            # 管不到）。不拦住就是 `AttributeError` → 500 → 删除侧判「没够着运行方」→
            # 白跑 3 次重发。后果比派活轻（`task.delete` 用完预算就整体失败、一个字段
            # 都不改，用户再点一次即可），但没必要 —— 回规范失败壳，真因直接落回执。
            return ApiResponse.fail(
                translate(
                    "err.task_runtime.bad_handler_result",
                    params={"handler": "remove_task"},
                    fallback=(
                        f"remove_task 返回了 {type(result).__name__}，不是 ApiResponse"
                    ),
                )
            ).to_dict()
        return result.to_dict()

    async def _on_task_stopped(self, request: Any, data: Any) -> Dict[str, Any]:
        """管理方**通知**：这条任务被用户停了（§4.1 ③，2026-09-30 加）。**必须幂等。**

        ⚠️ **这是通知，不是命令** —— 管理方只说事实（用户点了停止、任务已就地收成
        `failed`），**停不停、怎么停由运行方自己决定**：能停就停（把下游撤了 / 把轮询停了），
        停不掉（比如下游任务已经发出去了）**就只把接管的文档交还出去，那也是合法的回应**。

        为什么要有它：`task.stop` 从前**不出站、不通知运行方**，于是运行方不知道任务已经
        终止、继续盯着那份被接管的文档 —— 用户在文档页**重新解析 / 删除 / 整库重置**
        三个入口全被拦，要等在跑的活自己结束（再等 30 分钟兜底）或最长 6 小时。

        ⚠️ **管理方是「尽力而为」发的**：单次、不重发、超时短（`TASK_CENTER_NOTIFY_TIMEOUT`），
        而且**发失败只记 warning**（不让用户的「停止」失败）。**所以收不到是可能的** ——
        别把「下一步动作」完全押在它上面：那份文档该交还也得有别的兜底路径。

        ⚠️ 与 `_on_remove_task` 同：**只拦「返回值形状不对」，不拦运行方自己抛异常**。
        这一条比那边更无所谓 —— 管理方**根本不重发**，抛出去就是一次 500 + 两边的日志，
        真因反而更清楚。

        **幂等是硬要求**（写进契约）：任务不存在 / 已经交还过 / 压根没接管过，**都要回成功**
        —— 今天虽然不重发，但运维手工重放、或将来改成带重发，都靠它兜。
        """
        try:
            dispatch = TaskStoppedDispatch(**data)
        except (ValidationError, TypeError) as exc:
            return ApiResponse.fail(f"停止通知报文不合法：{exc}").to_dict()
        result = await self.task_stopped(
            dispatch.tenant_id, dispatch.space_id, dispatch.task_id
        )
        if not isinstance(result, ApiResponse):
            return ApiResponse.fail(
                translate(
                    "err.task_runtime.bad_handler_result",
                    params={"handler": "task_stopped"},
                    fallback=(
                        f"task_stopped 返回了 {type(result).__name__}，不是 ApiResponse"
                    ),
                )
            ).to_dict()
        return result.to_dict()

    # ========================================================
    # §4.1 ④ 子任务状态查询（管理方主动问）
    # ========================================================

    async def _on_query_step_status(self, request: Any, data: Any) -> Dict[str, Any]:
        """管理方**主动问**：这个子任务现在什么情况（§4.1 ④，2026-10-11 加）。

        ## 与「上报」是同一件事的两个方向

        上报是运行方**推**（跑完一步说一声），这个是管理方**拉**（我不知道你那步怎么样了，
        问一句）。**拉的意义是幂等**：上报丢了（单次尝试 + 最多 3 次重发全丢）子任务就挂到
        编排硬超时（预置 7 小时），而**问不到就下一拍再问**，不存在永久丢。

        ## ⚠️ 老运行方**什么都没做，也完全正常**

        基类 `query_step_status` 默认 `raise NotImplementedError`，这里**把它接住并转成
        `data=None`** —— 意思是「**这个运行方不提供查询**」。管理方看到 `None` 就退回原来的
        「等上报」，**行为与从前一字不变**。所以这个方法是**纯加法**，不实现它不会被判错。

        ## 只读

        运行方实现 `query_step_status` 时**不许改任何东西** —— 它是「看一眼」，不是「推一把」。
        想改状态的走它自己的收口路径。
        """
        try:
            envelope = ExecutionEnvelope(**data)
        except (ValidationError, TypeError) as exc:
            return ApiResponse.fail(f"状态查询报文不合法：{exc}").to_dict()

        try:
            result = await self.query_step_status(envelope)
        except NotImplementedError:
            # 「我不提供查询」—— **不是错**，转成 `None` 让管理方走老路。
            result = None

        if result is not None and not isinstance(result, StepStatusQuery):
            return ApiResponse.fail(
                translate(
                    "err.task_runtime.bad_handler_result",
                    params={"handler": "query_step_status"},
                    fallback=(
                        f"query_step_status 返回了 {type(result).__name__}，"
                        "不是 StepStatusQuery"
                    ),
                )
            ).to_dict()
        if result is not None and result.status not in STEP_STATUS_VALUES:
            # 域外取值**当场拒**：管理方靠这一列分支，放行等于让它猜。
            return ApiResponse.fail(
                translate(
                    "err.task_runtime.bad_handler_result",
                    params={"handler": "query_step_status"},
                    fallback=f"query_step_status 返回了域外状态 {result.status!r}",
                )
            ).to_dict()
        # ⚠️ 用 `.dict()`：协议层的 DTO 是 **pydantic v1 的 `BaseModel`**，它**没有
        # `to_dict()`**（那是业务模型的习惯写法）—— 写成 `to_dict` 会在**运行时**才炸，
        # 而语法检查与单测都抓不到（单测走的是替身，不经过这个包装）。
        return ApiResponse.ok(result.dict() if result is not None else None).to_dict()

    async def query_step_status(
        self, envelope: ExecutionEnvelope
    ) -> Optional[StepStatusQuery]:
        """这个子任务现在什么情况（§4.1 ④）。**默认不提供** —— 覆写它就能被管理方问到。

        返回 `None`（或抛 `NotImplementedError`，两条等价）都表示「我不提供查询」，
        管理方据此退回「等上报」。**所以覆写是可选的，不覆写不会出任何错。**

        ⚠️ **必须只读**：这是「看一眼」，不是「推一把」—— 实现里不许改任何状态。
        """
        raise NotImplementedError

    # ========================================================
    # §4.1 ① 派活
    # ========================================================

    async def execute(self, execution: ExecutionDispatch) -> ApiResponse[None]:
        """跑某个节点的活（§4.1 ①）。**运行方通常不覆写它** —— 覆写 `handlers()` 就够了。

        三件事按顺序发生：

        1. **绑上下文** —— handler 里 `require_execution_context()` 拿得到定位字段；
        2. **认重派** —— 同一个 `execution_id` 第二次到达时**一律回成功**，不重复干活（见下）；
        3. **分流** —— 未知 `step_code` 回失败壳（**显式拒绝**，不静默跳过）；handler
           的**返回值形状**不对（忘了 `return`、回了裸 dict）走同一档处置。

        ── 「管理方**确实会重派**」这件事，运行方要知道 ──

        派活是「投递 + 回执」，管理方不等你跑完。出站**没够着运行方**时（连不上 / 读超时 /
        响应不是 JSON —— 也就是它**不知道你到底收没收到**），它会在同一个后台循环里
        **同步重发**，最多 3 次（`capabilities/task_center/services/scheduler_service.py`
        的 `_dispatch_with_retry`）。所以「第二次收到同一个 `execution_id`」是**正常路径**，
        不是异常。

        **要求与之对应**：认出是重派时，**无论是「已经在跑」还是「已经受理过」，都要回
        成功**（`ApiResponse.ok`）—— 回过失败就等于告诉管理方「这一步没人接」，它会把这个
        子任务判死（终态 `failed`、下游一律 hold），而实际上它正在你这里跑。**运行方不用
        自己写任何重试/去重**：这一切由 `_registry.claim()` 那一步兜住。

        ⚠️ **这一切的前提是「登记持久」** —— 判断完全依赖 `ExecutionRegistry`。默认的
        `InMemoryExecutionRegistry` **进程重启即全丢、多副本之间互相看不见**，处在那个
        窗口里的重派会**重复干活**（重复建下游、重复扣配额）。上生产必须换成落运行方
        自己库的持久实现，详见 `registration.py`。
        """
        async with acquire_execution(execution):
            if await self._registry.claim(execution) is ClaimOutcome.ALREADY_TAKEN:
                # **不管是「已经跑完了」「正在跑」还是「受理了还没开跑」，一律回成功**
                # （不是回失败）—— 登记写在干活之前，所以「正在跑的那一次」也已经占着
                # 登记，走的正是这一档。回过失败就等于告诉管理方「这一步没人接」，
                # 它会把子任务判死，而实际上它正在这里跑（详见 `execute` 的说明）。
                # ⚠️ **`message=` 不能省**：`ApiResponse.ok(cls, data=None, message="", ...)`
                # 第一个位置参数是 `data`，写成 `ok("...")` 会把这句话塞进 `data` 里 ——
                # 派活侧读的是 `success` 与 `code`，多一个 `data: "…"` 不至于判错，但
                # `data` 的语义是「这个动作的产出」，这里没有任何产出（§4.1 ① 要求
                # `data: null`）。位置参数会让读回执的人在 `data` 里看到一句中文。
                return ApiResponse.ok(message="这次执行已经受理过，不重复处理")

            try:
                result = await self._dispatch(execution)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                # **取消 / 进程退出**不是「这一步失败了」：登记照撤（不撤的话这次会被永久
                # 记成「已受理」，下次重派直接回成功、**把一次真实的失败吞掉**），
                # 但异常必须**原样往上冒** —— 吞掉取消会让进程停不下来。
                await self._registry.release(execution)
                raise
            except Exception as exc:
                # **handler 自己抛了异常 = 「这次受理没成立」→ 回明确拒绝**（模块 docstring
                # 第 2 条那三种之一：未知 `step_code` / handler 抛异常 / 返回值形状错）。
                #
                # ⚠️ **别改回 `raise`**（2026-09-30 由用户拍板定成现在这样）：抛出去会变成
                # HTTP 500，而派活侧把 500 读成「**没够着运行方**」→ 传送层**原样重发**，
                # 于是这个 handler 会被**连跑 3 遍**（登记已撤，每次重发都是 NEW），
                # 真因也被 500 盖掉。回失败壳则是「运行方明确说这一步没成」→
                # 派活侧判 `REJECTED`、**一次都不重发**。
                #
                # 分工不变：**基类自己**的意外故障（登记落库失败等）在这段 `try` **之外**，
                # 照旧抛成 500 —— 那不是「收不下」的表态。
                await self._registry.release(execution)
                return ApiResponse.fail(
                    translate(
                        "err.task_runtime.handler_raised",
                        params={"handler": execution.step_code, "reason": repr(exc)},
                        fallback=(
                            f"步骤 {execution.step_code} 的 handler 抛了异常"
                            f"（这一步没跑成）：{exc!r}"
                        ),
                    )
                )

            if not isinstance(result, ApiResponse):
                # **返回值形状写错**（最典型：handler 忘了 `return`、回了个 `None`；或者按
                # 能力层 `dispatch` 的直觉回了裸 dict）—— 这也是「这次受理没成立」的一种，
                # 必须和上面两支一样**撤销登记**。
                #
                # ⚠️ 它从前是从这里漏出去的：`result.success` 会抛 `AttributeError`，而
                # 那句在 `try` 之外，于是异常一路上抛 —— ① 登记**没撤销** → 下次重派撞上
                # 它、直接回「已受理」，把这次真实失败吞掉；② 远端画像下 500 被判成
                # 「没够着运行方」→ 传送层原样重发 → 撞上的正是那份没撤销的登记 →
                # 管理方记「已受理」，等一个永远不会来的上报，子任务一直挂到超时。
                # 按「明确拒绝」处置（与不认识 `step_code` 同一档）：回**规范失败壳**，
                # 派活侧当场判 `REJECTED`、真因落 `error_message`。
                await self._registry.release(execution)
                return ApiResponse.fail(
                    translate(
                        "err.task_runtime.bad_handler_result",
                        params={"handler": execution.step_code},
                        fallback=(
                            f"步骤 {execution.step_code} 的 handler 返回了 "
                            f"{type(result).__name__}，不是 ApiResponse"
                        ),
                    )
                )

            if not result.success:
                # 明确拒绝 / 干活失败：同上，撤销登记，重派时重新干活。
                # 注意这里**不回退**成「没受理」以外的任何状态 —— 这次没成就是没成。
                await self._registry.release(execution)
            return result

    async def _dispatch(self, execution: ExecutionDispatch) -> ApiResponse[None]:
        """按 `step_code` 找到 handler 并调用；不认识就**显式拒绝**。"""
        handlers = self.handlers()
        handler = handlers.get(execution.step_code)
        if handler is None:
            # 走 i18n 而不是硬编码中文：这句话会一路落成子任务的 `error_message`、
            # 在任务中心控制台上看得见 —— 是**给用户看的**，闸口那一条同样拦它。
            # 键取 `err.task_runtime.*`（SDK 自己的域），不能借调用方运行方的域键：
            # SDK 不知道对面是哪个能力，借用就等于把 SDK 钉死在某个业务域上。
            return ApiResponse.fail(
                translate(
                    "err.task_runtime.unknown_step",
                    params={
                        "step_code": execution.step_code,
                        "known_steps": sorted(handlers),
                    },
                    fallback=f"不认识的步骤 {execution.step_code}（只认 {sorted(handlers)}）",
                )
            )
        return await handler(execution)

    @abstractmethod
    def handlers(self) -> Dict[str, Callable[[ExecutionDispatch], Awaitable[ApiResponse[None]]]]:
        """登记「哪个 `step_code` 干什么」。

        运行方**只认 `step_code`**（`parse` / `ingest` / `compile`），收到不认识的
        步骤名由基类回失败壳 —— 所以这里返回什么，就决定了什么步骤能被受理。

        每次派活都会调它一次，**保持便宜**（返回一个字面量 dict，别在里面查库）。
        """
        raise NotImplementedError

    # ========================================================
    # §4.1 ② 删除
    # ========================================================

    @abstractmethod
    async def remove_task(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        """按 `task_id` 清掉整个任务在本运行方侧的残留（§4.1 ②）。**必须幂等**。

        「清一个任务」是运行方内部一次自然的操作 —— 可能按任务建了目录、建了下游任务、
        建了临时表。**删两次、删一个不存在的、任务压根没建过，都要回成功。**

        ── 「管理方**确实会重发**」这件事，这里同样适用 ──

        `task.delete` 调它时，出站**没够着管理方那侧**（连不上 / 读超时 / 响应不是 JSON）会
        **原地重发同一个 `task_id`，最多 3 次**（`call_with_retry`，与派活同一份）。
        所以「第二次收到同一个 `task_id`」是**正常路径**：上一次可能**其实已经删干净了、
        只是回执没回来**。

        **要求与之对应**：认出来（**任务不存在 / 已经删掉了**）就**回成功**（`ApiResponse.ok`）
        —— 回过失败会让用户看到「删除失败」，可运行方侧其实早就清没了；而任务中心那边因为
        这一下失败**一个字段都不会改**（K2），任务还留着、用户还得再点一次。

        ⚠️ 与派活那侧的一个差别：这里**没有执行登记那种「认得出是重派」的机制** ——
        `execute` 靠 `ExecutionRegistry` 替你认，而**删除的幂等性完全由你自己实现**
        （清一个已经不存在的任务必须走成功路径，不能报错）。
        """
        raise NotImplementedError

    @abstractmethod
    async def task_stopped(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        """管理方**通知**：这条任务被用户停了（§4.1 ③，2026-09-30 加）。**必须幂等。**

        ⚠️ **它是通知，不是命令** —— 你**自己决定**停不停、怎么停：能停就停（撤下游、
        停轮询……），停不掉就**只把接管的那份文档/那批残留交还出去**，那也是合法的回应。
        管理方不会检查你做了什么，它只是不想再让你**蒙在鼓里继续等一个已经终止的任务**。

        **为什么值得实现**：不实现的话，你那边会继续盯着一个已经不会再有下一步派活的
        任务（`task.stop` 只判死还没开始的步骤、把任务收成 `failed`）—— 典型后果是
        **用户被你自己那道「文档正在处理中」的守卫拦住**，重新解析 / 删除 / 整库重置
        三个入口全用不了，要等在跑的活自己结束、或最长 6 小时硬超时。

        ── 两条要守的线 ──

        - **必须幂等**：任务不存在 / 已经交还过 / 压根没接管过，**一律回成功**。
          管理方今天是**单次发送、不重发**（尽力而为），但运维手工重放、或将来改成带
          重发，都靠这一条兜。
        - **别把它当「一定会到」**：管理方发失败只记 warning（不让用户的「停止」失败），
          **收不到是可能的** —— 该交还的东西也要有别的兜底路径（例如定期问一次管理方，
          见 §4.3 那个读接口）。

        返回 `ApiResponse[None]`（`data` 永远是 `None`）：管理方不看你的产出，只看成没成。
        """
        raise NotImplementedError

    # ========================================================
    # §4.2 的封装：定位字段从上下文取，运行方不用手传
    # ========================================================

    async def report_success(
        self,
        outputs: Dict[str, Any],
        *,
        output_refs: Optional[List[Dict[str, Any]]] = None,
        input_refs: Optional[List[Dict[str, Any]]] = None,
        ts: Optional[int] = None,
    ) -> TerminalSubmitResult:
        """报「跑完了」。`outputs` 必填（§4.2 ①）。

        `output_refs` / `input_refs` / `ts` 可选、**关键字传**（后两者 2026-10-13 /
        2026-10-16 补）：与 `TerminalReport.success` 同一套字段 —— 少了它们，这条
        「handler 内上报」的推荐路径就**带不出「产出去哪儿看 / 输入从哪儿来」**，
        调用方只能绕过本方法、自己去 `require_execution_context()` 再调
        `TaskRuntime.report_terminal`。

        **只能在 `execute` 的 handler 里调** —— 它从 ContextVar 取定位字段；
        巡检 / 后台循环那条路没有这一帧，请改用 `TaskRuntime.report_terminal(carrier, ...)`。

        **没被受理会自动重发**（重复上报在服务端是安全的，详见 `TaskRuntime.report_terminal`），
        handler 里不用自己写重试。返回的档位仍然是「重发完之后」的结论。
        """
        return await self._runtime.report_terminal(
            require_execution_context(),
            TerminalReport.success(
                outputs, ts=ts, output_refs=output_refs, input_refs=input_refs
            ),
        )

    async def report_failed(
        self,
        error_message: str,
        *,
        output_refs: Optional[List[Dict[str, Any]]] = None,
        input_refs: Optional[List[Dict[str, Any]]] = None,
        ts: Optional[int] = None,
        error_code: Optional[str] = None,
        error_params: Optional[Dict[str, Any]] = None,
    ) -> TerminalSubmitResult:
        """报「没跑成」。`error_message` 必填 —— 它是人看失败原因的唯一来源（§4.2 ①）。

        `output_refs` / `ts` / `error_code` / `error_params` 可选、**关键字传**
        （2026-10-13 补）：与 `TerminalReport.failed` 同一套字段 —— 少了
        `error_code` / `error_params`，这条推荐路径只能**冻住一句原文**，
        管理方没法按读的人的语言渲染（两条约定见 `TerminalReport.failed` 的 docstring）。

        `input_refs`（2026-10-16 加，同款关键字传）：**失败时也要带** —— 用户在这一步
        栽了跟头时，最想知道的恰恰是「它当时吃的是哪份东西」。与 `TerminalReport.failed`
        同一口径。

        同上：**只能在 handler 里调**；**没被受理会自动重发**。
        """
        return await self._runtime.report_terminal(
            require_execution_context(),
            TerminalReport.failed(
                error_message,
                ts=ts,
                output_refs=output_refs,
                input_refs=input_refs,
                error_code=error_code,
                error_params=error_params,
            ),
        )


__all__ = ["RunnerBase"]
