#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""协议层的四类结果与错误码判定（§4.0、§4.1、§4.2）。

**调用方不该自己读错误码集合**：`ApiResponse.success=False` 时不带原因，「要不要原样
重发」只能靠 `code` 判 —— 这套判定收口在本模块，接入方拿到的是一句话能懂的
disposition（「未知」和「拒绝」是两回事，见下）。

四类结果对应 SDK 的四个出口：

| 结果 | 谁产出 | 对应动作 |
| --- | --- | --- |
| `SubmissionResult` | `TaskRuntime.submit` | 发起方提交一条任务（§4.2 ②） |
| `DispatchResult` | 运行方 `execute` 的调用方 | 管理方派活给运行方（§4.1 ①） |
| `TerminalSubmitResult` | `TaskRuntime.report_terminal` | 运行方上报终态（§4.2 ①） |
| `SubtaskRetryResult` | `TaskRuntime.retry_subtask` | 运行方要求重跑某一步（§4.2 ③） |

**这里全是本地返回值，不跨进程序列化**，所以用 dataclass 而不是 pydantic —— 不需要
校验，也不需要 `dict()`。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Sequence

from jonex_core.common.exceptions import (
    CacheError,
    DatabaseError,
    InvalidParameterError,
    OperationNotSupportedError,
    PermissionDeniedError,
    RateLimitExceededError,
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceDiscoveryError,
    ServiceTimeoutError,
    ServiceUnavailableError,
    TenantIsolationError,
    UpstreamServiceError,
)
from jonex_core.task_protocol.errors import TaskProtocolError

#: 错误码：**可以原样重发**。结果未定（超时 / 连不上 / 上游 5xx）与暂时性故障都在这里，
#: 重发是对的。
#:
#: 取的是异常类的 `code` **属性**而不是写死数字：码值是那一个出处的定义，换码时这个
#: 集合自动跟上，不会两边悄悄分叉。
TASK_CENTER_RETRYABLE_CODES = frozenset(
    {
        DatabaseError.code,  # 4001
        CacheError.code,  # 4002
        RateLimitExceededError.code,  # 3007
        ServiceUnavailableError.code,  # 5001
        ServiceDiscoveryError.code,  # 5002
        UpstreamServiceError.code,  # 5003
        ServiceTimeoutError.code,  # 5004
    }
)

#: 错误码：**「这个 id 已经在了」**—— 只有提交方向有这一档。
#:
#: 它**不属于「拒绝」**：提交时自带 `task_id` 的调用方收到它，意味着**上一次调用其实
#: 建成了、只是响应没回来**（重发撞上了自己上次建的那条）。所以它是「带 id 重发」这条
#: 路的**终点**，落到 `SubmissionDisposition.REUSED` 收工 —— 判成拒绝会把调用方引去
#: 「换一个 id 再来」，同一条任务于是被建第二遍。
#:
#: 单独一个集合而不是并进 `TASK_CENTER_REJECTED_CODES`：同一个码在不同方向含义相反，
#: 派活 / 上报方向根本没有这条路（见 `dispatch_disposition` / `terminal_disposition`）。
TASK_CENTER_ALREADY_EXISTS_CODES = frozenset(
    {
        ResourceAlreadyExistsError.code,  # 1006（调用方指定的 id 被占了）
    }
)

#: 错误码：**明确的业务拒绝**。原样重发结果一样，得改参数或改数据再来。
TASK_CENTER_REJECTED_CODES = frozenset(
    {
        InvalidParameterError.code,  # 1001
        ResourceNotFoundError.code,  # 1002（找不到 / 越权按找不到）
        ResourceConflictError.code,  # 1003
        OperationNotSupportedError.code,  # 1004
        TenantIsolationError.code,  # 3005
        PermissionDeniedError.code,  # 3006
    }
)

#: 控制面调用的重试预算：首次 + 2 次重试 = 3 次调用。**所有控制面调用共用这一套**，
#: 一共六个方向 —— 运行方侧四个：`submit`（§4.2 ②）、`report_terminal`（§4.2 ①）、
#: `retry_subtask`（§4.2 ③）、`retry_subtasks`（§4.2 ④）；管理方侧两个出站：派活
#: `execute`（§4.1 ①）与 `remove_task`（§4.1 ②，删任务时调，两个方向共用
#: `flows/runner_adapter.py` 的 `call_with_retry`，口径只有一处）。
#:
#: ⚠️ **共用的只有「次数」** —— 退避是**两套**，见下面两个常量：运行方侧那四个方向用
#: `RETRY_BACKOFF_SEC`，管理方那两个出站方向用 `OUTBOUND_RETRY_BACKOFF_SEC`。
#:
#: 放在**协议层**而不是 `task_runtime`：管理方（`capabilities/task_center/`）也要用这
#: 两个常量，而它 import `task_runtime` 会在任务中心进程里把运行方 SDK（`RunnerBase`
#: / `ExecutionRegistry` / `TaskRuntime`）整套拉起来 —— 依赖方向是反的。协议层是两边
#: 都依赖的唯一中立层，三张码表已经住在这儿，重试预算跟它们同属「拿到结果怎么办」。
RETRY_MAX_ATTEMPTS = 3

#: 每次重试前的等待（秒），指数递增。第 N 次重试用第 N-1 个值。
#:
#: **这一套是运行方侧那四个方向（`submit` / `report_terminal` / `retry_subtask` /
#: `retry_subtasks`）用的**：它们要穿过的是运行方自己的进程与本地网络，抖一下就是抖一下，
#: 短退避足够。
RETRY_BACKOFF_SEC = (0.5, 1.0)

#: **管理方两个出站方向（派活 `execute` / `remove_task`）专用的退避** —— 比上面那套长。
#:
#: **为什么这两个方向单独一套**：它们量的是**另一段路**。管理方的出站要先穿过 Sidecar
#: （再可能穿过网关），中途可能撞上的是「Sidecar 正在重启」「网关在过载」这类**按秒计**
#: 的故障 —— 0.5s / 1s 这套退避会让三次尝试**全落在同一段故障里**（首尾相隔才 1.5 秒），
#: 「试满预算」于是退化成「在 1.5 秒里撞三次」，重发本来要给的**第二次机会**根本没给到。
#: 拉长到秒级才有了「等一下再看」的余地。
#:
#: ⚠️ **代价随之变大，且它是可感的**：单条最坏阻塞从 `3 × 出站超时 + 1.5s` 变成
#: `3 × 出站超时 + 7s`（30 秒超时下 91.5 秒 → 97 秒）；派活侧阻塞的是**后台循环的一轮**
#: （一轮最坏 `BATCH ×` 那个数），删除侧阻塞的是**用户那一次请求**。这是有意接受的：
#: 换来的是一条真能自愈的通道，而不是「三次撞在一起、然后判死 / 挂 7 小时」。
OUTBOUND_RETRY_BACKOFF_SEC = (2.0, 5.0)


def retry_delay(attempt: int, backoff: Optional[Sequence[float]] = None) -> float:
    """第 `attempt` 次尝试**失败后**该等多久（`attempt` 从 1 起）。

    退避序列用尽后停在最后一个值上（不无限翻倍）—— 预算本来就只有 3 次。

    `backoff` 不传就用运行方侧那一套（`RETRY_BACKOFF_SEC`）；管理方的两个出站方向
    要显式传 `OUTBOUND_RETRY_BACKOFF_SEC`（理由见那个常量）。
    """
    seq = RETRY_BACKOFF_SEC if backoff is None else backoff
    return seq[min(attempt - 1, len(seq) - 1)]


#: 分类结果：成功 / 结果未知 / 明确拒绝 / id 已被占用。
CLASS_OK = "ok"
CLASS_UNKNOWN = "unknown"
CLASS_REJECTED = "rejected"
CLASS_ALREADY_EXISTS = "already_exists"


def classify_code(success: bool, code: int) -> str:
    """把 `(success, code)` 分成四档之一 —— **本模块与接入方的唯一判定口径**。

    ⚠️ **不在三个集合里的一律归「未知」，不归「拒绝」**：`ApiResponse.fail` 的默认码是
    `-1`（`CODE_FAIL`），它的语义是「未分类的失败」、不是「明确拒绝」。把 `-1` 当拒绝
    会把一次传送抖动记成永久失败 —— 最重的一处是**派活方向**：`REJECTED` 的处置是把
    那个子任务**直接判死**（置 `failed`、不回退、不重发），于是抖一下就判死一个其实
    能跑的活，正是这套三档要防的事。
    **未知 ≠ 拒绝**：未知必须可重发，只是得先查清楚再决定发什么。

    ⚠️ `CLASS_ALREADY_EXISTS` **只在提交方向有意义**（1006 = 复用既有任务）。派活 /
    上报方向收到它按明确拒绝处理，见下面那三个 `*_disposition` 函数。
    """
    if success:
        return CLASS_OK
    if code in TASK_CENTER_RETRYABLE_CODES:
        return CLASS_UNKNOWN
    if code in TASK_CENTER_ALREADY_EXISTS_CODES:
        return CLASS_ALREADY_EXISTS
    if code in TASK_CENTER_REJECTED_CODES:
        return CLASS_REJECTED
    return CLASS_UNKNOWN


class SubmissionDisposition(str, Enum):
    """`TaskRuntime.submit` 的结论（§4.2 ②）。"""

    CREATED = "CREATED"
    """新建了一条任务。"""

    REUSED = "REUSED"
    """**这条任务早就在了，这次没有新建** —— 发起方自带的 `task_id` 已经被占用（1006）。

    它回答的是这样一个问题：*「我带着同一个 id 又提交了一次，服务端告诉我这个 id 有人
    用了 —— 那到底算成功还是失败？」*

    **算成功。** 因为 id 是发起方**自己生成的**（不是服务端分配的），所以「这个 id 已经
    被占」在一个正常运转的流程里只有一种成因：**上一次调用其实已经建成了，只是响应在
    回来的路上丢了**（超时 / 连接断 / 进程重启）。重发撞上的不是别人，正是自己上次建的
    那一条。所以：

    | 调用方看到 | 正确的处置 |
    | --- | --- |
    | `REUSED` | **当成成功收工**，任务 id 就是你自己传的那个。**不要换 id 重来** —— 换 id 会把同一条任务建第二遍，这正是重试本要防的事 |
    | `REJECTED` | 换参数 / 换 id 再来 |

    ⚠️ 两个后缀要读准：

    - 它**不是** `RETRYABLE_UNKNOWN`：那档是「不知道成没成」，这档是**确定成了**，再试
      一遍只会再撞一次同一个 id；
    - 它**不是** `REJECTED`：那档的意思是「这次请求有问题」，而 `REUSED` 的成因恰恰是
      「上次的请求没问题、而且成功了」。

    ⚠️ 服务端 `init_task` 仍**不做幂等去重**（不带 id 时调两次建两条），所以「复用」这件
    事**只可能发生在自带 id 的提交上** —— 不带 id 的提交连触发它的机会都没有（见
    `TaskRuntime.submit` 的重试条件）。
    """

    RETRYABLE_UNKNOWN = "RETRYABLE_UNKNOWN"
    """结果未知（超时 / 连不上 / 上游 5xx）—— **可以原样重发**。"""

    REJECTED = "REJECTED"
    """明确的业务拒绝 —— 原样重发结果一样，得改参数再来。"""


class DispatchDisposition(str, Enum):
    """派活（§4.1 ①）的结论 —— 管理方看运行方回执时用。"""

    ACCEPTED = "ACCEPTED"
    """运行方收下了这次执行。"""

    REJECTED = "REJECTED"
    """运行方明确不收（包含「不认识的 step_code」）—— 原样重派结果一样。"""

    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    """没够着运行方 / 没等到回执 —— 结果未知。**不能当明确失败处理**：
    运行方可能已经在跑了，直接判死会让这一步没人在跑。"""


class TerminalSubmitDisposition(str, Enum):
    """`TaskRuntime.report_terminal` 的结论（§4.2 ①）。"""

    DURABLY_ACCEPTED = "DURABLY_ACCEPTED"
    """上报已被持久受理。⚠️ **只代表报告进了收件箱，不代表任务中心已经处理完** ——
    收到它不等于下一步已经就绪，别据此推进本地状态机。"""

    RETRYABLE_UNKNOWN = "RETRYABLE_UNKNOWN"
    """结果未知 —— 原样重发。"""

    PERMANENT_REJECTED = "PERMANENT_REJECTED"
    """明确拒绝 —— 原样重发结果一样（服务端的判定不会变），得改数据再来。

    ⚠️ **「原样重发结果一样」不等于「SDK 不重发」**：`report_terminal` 的重试范围是
    **除 `DURABLY_ACCEPTED` 之外的全部**，这一档**照样重发**（3 次 / 退避 0.5s、1s）
    —— 判据是 `code` 落在哪张表，不是「重发有没有意义」。3 次都没成才把结论交回
    调用方（见 `TaskRuntime.report_terminal`）。

    ⚠️ 这里说的「当事人自己决定放弃」也**不是收件箱那个死信**：
    `subtask_reports.dead_lettered` 指的是服务端消化连续失败后放弃重试的那条上报
    （§3.5.1）—— 那是上报**已经被收下之后**的事；这一档是服务端当场拒收，
    两者不在同一条路上。
    """


class SubtaskRetryDisposition(str, Enum):
    """`TaskRuntime.retry_subtask` 的结论（§4.2 ③）—— 运行方要求重跑某一步。"""

    RETRIED = "RETRIED"
    """这一步已经回到队列上了（管理方把任务拉回跑着、该步重置成 `pending`）。

    ⚠️ **`RETRIED` 不保证是"这一次调用干成的"**：管理方对这条路的处置是**幂等**的 ——
    任务不是终态、而该步的 `current_attempt` 已经大于 1 且还没跑完（说明上一次调用其实
    已经重试成了、只是响应没回来），一样回成功。这正是 SDK 敢在这条路上重发的前提
    （见 `TaskRuntime.retry_subtask`）。

    ⚠️ 它也**不代表这一步已经跑完**：重试只是把它放回队列，之后照正常派活流程再派一次。
    """

    RETRYABLE_UNKNOWN = "RETRYABLE_UNKNOWN"
    """结果未知（超时 / 连不上 / 上游 5xx / 抢不到任务锁）—— **可以原样重发**。"""

    PERMANENT_REJECTED = "PERMANENT_REJECTED"
    """明确的业务拒绝 —— 原样重发结果一样，得改参数或改数据再来。

    常见几种：任务还没跑完（`completed` / `failed` 之前不许重试单步）、那一步还没结束
    （`pending` / `running`）、它的上游还没成功、`space_id` 对不上、节点不存在。
    """


@dataclass(frozen=True)
class TaskRef:
    """提交成功后回给发起方的**最小任务标识**。

    只留发起方**自己算不出来**的两个值：

    - `definition_version_no`：没指定 `version_no` 时，服务端替它解析并钉死的那个版本；
    - `id`：不传 `task_id` 时由管理方生成的那个（传了就是原值，冗余但便宜）。

    完整快照 `TaskItemResponse` 的**其余字段**，对发起方全是**它自己刚传进去的值**
    （`definition_id` / `space_id` / `inputs` / `name`）或**管理方内部信息**（`task_source`
    / `state_version` / `created_by` / `updated_by` / 四个时间戳），不进 SDK 的返回。
    （**此处刻意不写具体字段数** —— 它已随 `task_source` 之类的新列漂移过一次，
    写死数字只会再漂。）
    """

    id: str
    definition_version_no: int

    @classmethod
    def from_payload(cls, payload: Optional[Dict[str, Any]]) -> "TaskRef":
        """从 `ApiResponse.data` 的裸 dict 造出来。

        `ApiResponse.from_dict` **不转子模型**（`data` 是原样塞进去的），所以这一步必须
        自己解析。缺字段说明服务端回应与契约不符 —— **当场抛，不静默兜个空值**：调用方
        拿着 `id=""` 写进自己的行，那是很久以后才会发现的脏数据。
        """
        data = payload or {}
        task_id = data.get("id")
        version_no = data.get("definition_version_no")
        if not task_id or version_no is None:
            raise TaskProtocolError(
                message="提交已成功，但响应里没有 id / definition_version_no，构造不出 TaskRef",
                details={"data": data},
            )
        return cls(id=str(task_id), definition_version_no=int(version_no))


@dataclass(frozen=True)
class SubmissionResult:
    """`submit` 的返回。"""

    disposition: SubmissionDisposition
    task: Optional[TaskRef] = None
    """成功时是那条任务的最小标识；`REUSED` 与各失败档均为 None
    （重发命中时服务端不回 data，没有可解析的东西）。"""
    message: str = ""


@dataclass(frozen=True)
class DispatchResult:
    """派活回执的解读结果。"""

    disposition: DispatchDisposition
    message: str = ""
    dead_after_retry: bool = False
    """这一次的 `DELIVERY_UNKNOWN` 属不属于**「重发用尽后判死」**那一类。

    **只对 `DELIVERY_UNKNOWN` 有意义**，其余两档恒为 `False`（那是「运行方给了话」，
    没有「这一次是怎么没够着」这个问题）。

    **为什么要把它记下来**：同为「未知」，成因分两类，**处置相反** ——

    | 成因 | 例子 | 处置 |
    | --- | --- | --- |
    | 请求**可能已经落到运行方**、只是回执没回来 | 连不上 / 读超时 / 响应不是 JSON / 429（被治理层挡了，但对方随时可能收下） | **不能判死** —— 运行方说不定已经在跑了，判死会把那一步做成孤儿 |
    | **对方那一侧持续不通** | ① 网关 / 传送层在报 HTTP 5xx；② 运行方**自己回了失败**（HTTP 200 + **合法信封** `{success: false, code: …}` —— 码是可重试的那档也好、`classify_code` 不认识的未分类码也好） | 重发把预算走完还是这样 → **判死** |

    两类成因**方向正好相反**，但共享同一个结论：**都不是「对方可能已经在跑、只是回包
    丢了」** ——

    - ① 请求**压根没到**：`/invoke` 把 Sidecar 自己与上游的所有错误**一律拍平成
      HTTP 500**（`CapabilityNotFoundError` 2001 也被改写成 2002），这一侧没有通；
    - ② 请求**到了、运行方自己说它失败了**：HTTP 2xx 那一层的 `success=false`
      **只可能来自能力服务本身**（Sidecar 从不改写 2xx 的 body），所以它同样是
      「对方表了态」—— 码落在可重发那一档（5003 这类暂时性故障）是这样，落在
      **`classify_code` 不认识的未分类码**上也是这样（那种码兜底判未知，但「话是
      对方说的」这点没变）。

    ⚠️ **② 有个前提：「合法信封」** —— body 是 dict **且带 `success` 键**。HTTP 2xx 却
    回了**别的形状**（数组 / 字符串 / 没有 `success` 键的 dict）时**不打这个标记**：那说明
    响应根本不是这套契约，连「对方收没收到、听没听懂」都不确定 —— 归上面第一类保守处理。
    判据只写在 `runner_adapter._dispatch_remote` 一处。

    所以判死**不会做成孤儿**，而继续挂着只会白占并发名额到 `timeout_seconds`
    （内置步骤 25200 秒）才有人过问。

    ⚠️ **两类为什么共用一个字段**：它们共享的是**处置**（重发用尽后判死），不是成因 ——
    而处置正是调用方唯一要按它分支的东西。具体是哪一类写在 `message` 与日志里，排查看那里。

    ⚠️ **决定不在这里做**：这个字段只如实记下「这一次的未知属于哪一类」，判不判死由派活侧
    的收尾那一步决定（`TaskService.finalize_dispatch`，§4.1 ①）；`call_with_retry` 一个字
    都不改判决。
    """


@dataclass(frozen=True)
class TerminalSubmitResult:
    """`report_terminal` 的返回。"""

    disposition: TerminalSubmitDisposition
    message: str = ""


@dataclass(frozen=True)
class SubtaskRetryResult:
    """`retry_subtask` 的返回。

    **没有 `data`** —— 重试成不成由 `disposition` 一句话讲完，管理方那边的子任务快照
    是它的内部 DTO、不进对外契约（要读状态请自己按 `task_id` 查，或等后续的只读口子）。
    """

    disposition: SubtaskRetryDisposition
    message: str = ""


def submission_disposition(success: bool, code: int) -> SubmissionDisposition:
    """`(success, code)` → `SubmissionDisposition`。"""
    verdict = classify_code(success, code)
    if verdict == CLASS_OK:
        # 成功壳只说明「这次调用建成了」。是不是复用了既有那条，只有 1006 那条路讲得清
        # ——服务端仍不做幂等去重（见 REUSED 的说明）。
        return SubmissionDisposition.CREATED
    if verdict == CLASS_ALREADY_EXISTS:
        return SubmissionDisposition.REUSED
    if verdict == CLASS_REJECTED:
        return SubmissionDisposition.REJECTED
    return SubmissionDisposition.RETRYABLE_UNKNOWN


def dispatch_disposition(success: bool, code: int) -> DispatchDisposition:
    """`(success, code)` → `DispatchDisposition`。

    ⚠️ 1006 在这个方向**不适用** —— 派活没有「自带 id」这回事。真收到就按明确拒绝处理，
    **不给它开重放的口子**：这里没有「重发会撞上自己」那层保护，当可重放就是真重放。

    ⚠️ **只拿它判「Sidecar 外层信封」，不要拿它判运行方自己的回执壳** —— 判错了方向
    正好相反。`runner_adapter._dispatch_remote` 收到的是两层壳：外层是 Sidecar 的
    `{success, code, message, data}`，内层 `data` 才是运行方的回执。两者规则不同：

    | 判的是 | 规则 | 为什么 |
    | --- | --- | --- |
    | **外层信封** | **本函数**（看 `code`） | 这层的 `success=false` 既可能是**能力服务判的业务拒绝**（如 3005 无权访问该租户资源），也可能是**它自己那侧的基础设施故障**（如 5003 上游服务返回错误、4001 数据库操作失败）。`code` 是唯一能区分的信号。⚠️ `raise_for_status()` 先把 4xx / 5xx 拦走了，所以「能力没注册」（2001 / 2002）这类 Sidecar 自己发现的问题**到不了这一层**（那些走的是 HTTP 500 那一档） |
    | **内层回执壳** | **只看 `success`**，不看 `code` | 运行方的拒绝壳由 `ApiResponse.fail()` 生成，`code` 恒为 `-1`；而 `-1` 按 `classify_code` 落 `CLASS_UNKNOWN`。拿它来判会把「运行方**明确**拒收」误判成「结果未知」→ 无谓重发 3 次。**拿到了壳 = 拿到了对方的话**，`success=false` 就是拒绝 |

    所以 `DispatchDisposition` 在管理方那侧有**两个产地**，别混：信封走本函数，
    回执壳走 `runner_adapter._from_receipt()`。
    """
    verdict = classify_code(success, code)
    if verdict == CLASS_OK:
        return DispatchDisposition.ACCEPTED
    if verdict in (CLASS_REJECTED, CLASS_ALREADY_EXISTS):
        return DispatchDisposition.REJECTED
    return DispatchDisposition.DELIVERY_UNKNOWN


def terminal_disposition(success: bool, code: int) -> TerminalSubmitDisposition:
    """`(success, code)` → `TerminalSubmitDisposition`。

    ⚠️ 1006 在这个方向同样**不适用**，按明确拒绝处理（理由见 `dispatch_disposition`）。
    """
    verdict = classify_code(success, code)
    if verdict == CLASS_OK:
        return TerminalSubmitDisposition.DURABLY_ACCEPTED
    if verdict in (CLASS_REJECTED, CLASS_ALREADY_EXISTS):
        return TerminalSubmitDisposition.PERMANENT_REJECTED
    return TerminalSubmitDisposition.RETRYABLE_UNKNOWN


def subtask_retry_disposition(success: bool, code: int) -> SubtaskRetryDisposition:
    """`(success, code)` → `SubtaskRetryDisposition`。

    ⚠️ 1006（「这个 id 已经在了」）在这个方向同样**不适用** —— 重试某一步没有「自带 id」
    这回事，真收到就按明确拒绝处理（理由同 `dispatch_disposition`）。
    """
    verdict = classify_code(success, code)
    if verdict == CLASS_OK:
        return SubtaskRetryDisposition.RETRIED
    if verdict in (CLASS_REJECTED, CLASS_ALREADY_EXISTS):
        return SubtaskRetryDisposition.PERMANENT_REJECTED
    return SubtaskRetryDisposition.RETRYABLE_UNKNOWN


__all__ = [
    "CLASS_ALREADY_EXISTS",
    "CLASS_OK",
    "CLASS_REJECTED",
    "CLASS_UNKNOWN",
    "DispatchDisposition",
    "DispatchResult",
    "OUTBOUND_RETRY_BACKOFF_SEC",
    "RETRY_BACKOFF_SEC",
    "RETRY_MAX_ATTEMPTS",
    "SubmissionDisposition",
    "SubmissionResult",
    "SubtaskRetryDisposition",
    "SubtaskRetryResult",
    "TASK_CENTER_ALREADY_EXISTS_CODES",
    "TASK_CENTER_REJECTED_CODES",
    "TASK_CENTER_RETRYABLE_CODES",
    "TaskRef",
    "TerminalSubmitDisposition",
    "TerminalSubmitResult",
    "classify_code",
    "dispatch_disposition",
    "retry_delay",
    "submission_disposition",
    "subtask_retry_disposition",
    "terminal_disposition",
]
