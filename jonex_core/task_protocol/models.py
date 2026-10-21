#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务运行协议模型：跨进程报文的字段定义（§4.1 派活、§4.2 提交与响应）。

**这个模块谁也不依赖** —— 只有标准库、pydantic v1 与 `jonex_core.common.exceptions`。
不 import `capabilities.*`、不 import `jonex_core.capability.*`（含 `ApiResponse`）、
不 import logging。任务中心服务端、运行方 SDK、以后的可观测模块都能安全 import 它。

⚠️ **别拿"能 import 本模块"当作"能脱离 DB / Redis 运行"的凭据**：依赖
`jonex_core.common.exceptions` 意味着必然会连带 `jonex_core.common` 包的全部基础依赖
（那个包的 `__init__.py` 是饿汉式的，会一路拉起 database / cache / vector）。

**报文的定义只留这一份**：`capabilities/task_center/dtos/runner.py` 与
`capabilities/task_center/dtos/execution.py` 里的同名类是对这里的 re-export，
不是第二份定义 —— 两边各抄一份会让「任务中心用它造对象、运行方用它做 isinstance」
这种错位静默发生（都不报错，但语义已经错了）。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import BaseModel, Field
except ImportError:  # pragma: no cover
    from pydantic import BaseModel, Field


class ExecutionEnvelope(BaseModel):
    """派活报文（`execute`）的字段定义（§4.1 ①）。

    一期「管理方 → 运行方」的出站报文只有两个：**派活**（`execute`，套这个壳）与
    **删整个任务**（`remove_task`，粒度是任务、用 `task_id` 一把清，定位不靠具体
    某一步，所以不套这个壳、单独一个 DTO）。

    **`step_code` 管分流、`node_id` 管寻址，两个都得给**：

    `step_code` 是「这个节点用的是哪个内置步骤」（如 `parse`），运行方按它分流到不同的
    执行模式；**同一个 `step_code` 可以出现在多个节点上**（`parse_1` / `parse_2` 都是
    `parse`），所以它做不了唯一键。

    `node_id`（编排里的节点，如 `parse_1`）是**「同一个 `step_code` 出现多次时，唯一能
    指认是哪一步」的那个东西**：`depends_on` / `next_steps` 连线、`@<node_id>.outputs.x`
    引用、以及运行方发起的「重试某一步」（§4.2 ③）都按它定位。所以它进了对外契约
    （§3.2.1、§4.1 ①）。

    ⚠️ **运行方把它当不透明标识**：原样存着、需要时原样带回（`retry_subtask` 的入参），
    **不要按 `parse_1` 这个形状去解析它**（别拆前缀、别假设末尾有编号）—— 形状是管理方
    的事，将来改了不会通知运行方。

    **为什么 `step_code` 必须显式给、不能省**：它推不出来。"运行方按 `inputs` 的形状自己
    判断"这条走不通——**入参形状相同的不同步骤是存在的**：`ingest`（把已解析的内容推
    进检索库）与 `compile`（编译出下游产物）入参都是「哪个对象 + 哪个归属」，形状一模
    一样，光看 `inputs` 分不开。所以由管理方显式给，而不是让运行方猜（§4.1 ①、§3.5）。
    """

    tenant_id: str = Field(description="租户 ID（运行方按租户隔离落数据时要用）")
    space_id: str = Field(description="空间 ID（任务所属空间；运行方按空间落数据时要用）")
    task_id: str = Field(description="任务实例 ID（tasks.id）")
    step_code: str = Field(
        description="这个节点用的是哪个内置步骤（= config.steps[].step_code，如 parse / "
                    "ingest / compile）；运行方按它分流到不同执行模式"
    )
    node_id: str = Field(
        description="这一步在编排里的节点 id（= config.steps[].node_id，如 parse_1）。"
                    "同一个 step_code 可能出现在多个节点上，所以「重试哪一步」只能按它"
                    "指认。⚠️ 当不透明标识用：原样存、原样带回，别解析它的形状"
    )
    execution_id: str = Field(description="管理方生成的受理凭证，上报时原样带回")

    class Config:
        extra = "ignore"


class ExecutionDispatch(ExecutionEnvelope):
    """派活报文：管理方调运行方 `execute(execution)` 的入参（§4.1 ①）。

    `execution_id` 由管理方在派活前生成好、随报文传下；运行方收下后原样记着，
    执行完用 `report_status` 带同一个 id 上报结果。`attempt` 不在这里——那是
    管理方自己的账，运行方不用管。
    """

    inputs: Dict[str, Any] = Field(
        default_factory=dict, description="已经填好的真实入参；没有入参就是空对象"
    )


class RemoveTaskDispatch(BaseModel):
    """删整个任务的报文：管理方调运行方 `remove_task(task_id, ...)` 的入参（§4.1 ②）。

    粒度是**整个任务**，所以只要三个字段——运行方按 `task_id` 把该任务在自己侧的
    全部残留一次清干净（§4.1 ②）。管理方删任务时调的就是它（§4.3 `task.delete`）。
    """

    tenant_id: str = Field(description="租户 ID")
    space_id: str = Field(description="空间 ID")
    task_id: str = Field(description="任务实例 ID（tasks.id）—— 按它清掉整个任务的残留")

    class Config:
        extra = "ignore"


# ── 子任务状态查询（§4.1 ④，2026-10-11 加）─────────────────────────────
#
# 「管理方主动问、运行方只回答」这条路的五档回答。入参用 `ExecutionEnvelope`
# （= 派活报文去掉 `inputs`，见 `ports.RunnerPort.query_step_status`）——
# 运行方拿到的定位钥匙与受理派活时**完全一致**。
#
# **为什么要有这条路**：管理方推进编排原来**只能等运行方主动上报**（`report_status`）——
# 上报是「单次尝试 + 最多 3 次重发」，全丢了子任务就一直挂 `running`，直到编排硬超时
# （预置 7 小时）才被兜。而丢的原因多半不在网络：运行方那侧「什么时候该上报」的判断错了、
# 或者它压根没走到那一步。**拉**没有这个问题 —— 丢了再拉一次就自愈（幂等）。

#: 运行方**收下了这活、但还没等到派活**（手里还没有 `execution_id`）。
STEP_STATUS_PENDING_DISPATCH = "pending_dispatch"

#: 运行方**正拿着某个执行编号在跑**。
STEP_STATUS_RUNNING = "running"

#: 上一次跑**成功了**。
STEP_STATUS_SUCCEEDED = "succeeded"

#: 上一次跑**失败了**。
STEP_STATUS_FAILED = "failed"

#: **这活不归我**（没有这个 `task_id` 的接管记录 / 已交还且无结论）。
#:
#: ⚠️ **它不是失败**。管理方按 `runner_definitions` 找运行方，**找错人是可能的**
#: （编排改过、运行方被换、任务被删过）。把它读成「失败」等于把一次**配置错误**
#: 变成一条**业务失败**。管理方对这一档有独立处置（记明确原因，不聚合进业务失败）。
STEP_STATUS_UNKNOWN = "unknown"

#: 值域。**唯一出处** —— 运行方实现与任务中心对账都从这儿取，不出现裸字符串。
STEP_STATUS_VALUES = frozenset({
    STEP_STATUS_PENDING_DISPATCH,
    STEP_STATUS_RUNNING,
    STEP_STATUS_SUCCEEDED,
    STEP_STATUS_FAILED,
    STEP_STATUS_UNKNOWN,
})


class StepStatusQuery(BaseModel):
    """运行方对「这个子任务现在什么情况」的回答（§4.1 ④）。

    ## 五档状态

    `pending_dispatch` / `running` / `succeeded` / `failed` / `unknown`。

    ## ⚠️ `report`：**终态时带回的东西，形状就是「上报接口的入参」**

    这是本 DTO 最关键的一条（用户 2026-10-11 定：**「查询接口的返回应该是上报接口的入参」**）：

    管理方拿到 `succeeded` / `failed` 之后**不能只写个状态** —— 它还要拿**这一步的产出**
    去填下一步的输入（比如「解析」的产出里有 `document_id`，「编译」那步要用它）。
    而**产出只有运行方知道**。

    所以终态时，运行方把**它当初要上报的那份报文**原样带回来：

    ```python
    {
        "status": "success",             # success / failed
        "outputs": {"document_id": ...}, # 成功时必带（下一步的输入从这儿填）
        "output_refs": [...],            # 产出引用（「去哪儿看」）
        "error_message": "...",          # 失败时必带
        "error_code": "...",             # 失败时的协议枚举代号
        "error_params": {...},
    }
    ```

    管理方**直接把它当一条上报塞进收件箱**，走**与上报完全相同**的消化路径：

    - **零转换** —— 形状一样，不用做任何映射；
    - **不可能分叉** —— 「推的」和「拉的」喂的是同一份数据、走同一条收口；
    - **将来自动跟上** —— 上报报文加字段，查询跟着就有，不用两边各改一次。

    ⚠️ **类型是 `dict` 而不是 `ReportStatusRequest`**：那个 DTO 住在管理方
    （`capabilities/task_center/dtos/`），协议层 import 不到、也不该 import。
    这里用**形状约定**（字段名与它逐字一致），管的是「数据长什么样」。

    ## `detail` 是给人看的

    `unknown` 时的一句说明（「没有这个 task_id 的接管记录」）。**别拿它做分支** ——
    判据是 `status`，文案会变、会本地化。
    """

    status: str = Field(
        description="pending_dispatch / running / succeeded / failed / unknown",
    )
    report: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "终态（succeeded / failed）时必带 —— **形状就是上报接口的入参**，"
            "管理方直接拿它去消化。非终态为 None。"
        ),
    )
    detail: str = Field(
        default="", max_length=500,
        description="给人看的补充说明（为什么说不归我）；判据别看它",
    )

    class Config:
        extra = "ignore"


#: `output_refs`（产出「去哪儿看」的引用）的**报文大小上限**（按 `json.dumps` 后的字符数）。
#:
#: 引用是**展示用**的小数据（一个 `kind` + 一组参数），不是产物正文 —— 给它一个硬上限，
#: 是为了从机制上防止有人往上报里塞切片/正文：那种大 payload 会拖慢每次上报与重试。
#:
#: **两侧同一个常量**：SDK 构造 `TerminalReport` 时先本地拦一道（出站前就炸，别白等重试），
#: 服务端 `_check_report_payload` 再拦一道（它才是落库的守门人）。改这里就是改两边。
OUTPUT_REFS_MAX_BYTES = 4096

#: 一条上报最多带几个引用。
OUTPUT_REFS_MAX_ITEMS = 16

#: **`kind` → 这个 kind 的接口要哪些 `params`**（写死的枚举表，2026-10-09 用户拍板）。
#:
#: ⚠️ **`kind` 必须带「运行方」前缀** —— 格式 `{运行方 code}.{产物类型}`：任务中心是**多运行方**
#: 的（`runner_definitions` 里一条一个），裸 `chunks` 这种名字迟早和别人撞（谁都在切块）。
#: 前缀取那条运行方的 `code`（知识库那条就是 `knowledge_base`）。加新运行方 = 前缀换成它的 code。
#:
#: 引用里**不带 `url` / `method`** —— 「这个 kind 该调哪个接口」由**前端**那张映射决定，
#: 平台侧校不了它（报文里压根没有地址）。好处是**注入面归零**：运行方连地址都碰不到，
#: 填不出 `https://别的站` / `javascript:` / `//evil.com` / `..`，也填不出 `DELETE`。
#:
#: ⚠️ 加新 kind = **这里加一行 + 前端加一个映射**（两边靠这份表对齐，不像 url 那样能在报文里自证）。
#: `required` 里的键必须给、且非空；`optional` 给了也行。表外的键（含未知键）**放行**。
OUTPUT_REF_PARAMS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "knowledge_base.chunks": {"required": ("document_id", "knowledge_base_id"), "optional": ()},
    "knowledge_base.ontology": {"required": ("document_id", "knowledge_base_id"), "optional": ()},
    "knowledge_base.wiki": {"required": ("document_id", "knowledge_base_id"), "optional": ()},
}

#: `kind` 的写死枚举。**从 `OUTPUT_REF_PARAMS` 推导** —— 别再单独维护一份，两份必然漂移。
OUTPUT_REF_KINDS: Tuple[str, ...] = tuple(OUTPUT_REF_PARAMS)

#: `input_refs`（**这一步的输入从哪儿来**的引用）的报文上限。
#:
#: 与 `OUTPUT_REFS_MAX_*` **同值**是刻意的：两条链走同一条上报通道、带同一类小数据
#: （一个 kind + 一组参数），给两套数字只会在「凭什么这个能带 20 条」上多出一场讨论。
INPUT_REFS_MAX_BYTES = 4096

#: 一条上报最多带几个输入引用。
INPUT_REFS_MAX_ITEMS = 16

#: **输入引用的 `kind` → 这个 kind 要哪些 `params`**（写死枚举表，2026-10-16 用户拍板）。
#:
#: 与 `OUTPUT_REF_PARAMS` 是**两张表、两条链**：那边管「产出**去哪儿看**」（这一步的成果），
#: 这边管「输入**从哪儿来**」（这一步吃的是什么）。两者**不是互斥的 kind 全集** ——
#: `knowledge_base.chunks` 两边都有（产出侧是「我切出了这些切片」、输入侧是「我吃的是
#: 这些切片」，编译步的输入正是解析步的产出），而 `knowledge_base.document` 只在输入侧
#: （产出不会是「一份文档」）。
#:
#: 前缀规则、`params` 的「非空标量」要求、未知键放行 —— 全部与 `OUTPUT_REF_PARAMS` 同款
#: （见那个常量的注释），理由也一样：**跨进程契约**，运行方 / 管理方 / 前端三方对同一张表。
#:
#: 加新 kind = **这里加一行 + 前端加一个映射**（前端据它决定跳到哪个页面）。
INPUT_REF_PARAMS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    # 那份**文档**：解析步吃它、编译步也要看它（编译那两条里的一条）。
    # `document_name` 必填 —— 用户要的就是「不用再查一次就能显示」的名字；它是**取名那一刻
    # 的快照**，文档后来改名 / 删掉都不影响这条引用还能显示。
    "knowledge_base.document": {
        "required": ("knowledge_base_id", "document_id", "document_name"),
        "optional": (),
    },
    # 切片：产出侧用它表示「我切出了什么」，输入侧用它表示「我吃的是这些切片」。
    "knowledge_base.chunks": {
        "required": ("knowledge_base_id", "document_id"),
        "optional": (),
    },
}

#: 输入侧 `kind` 的写死枚举。**从 `INPUT_REF_PARAMS` 推导**（同 `OUTPUT_REF_KINDS`）。
INPUT_REF_KINDS: Tuple[str, ...] = tuple(INPUT_REF_PARAMS)


def _check_refs(
    refs: Optional[List[Dict[str, Any]]],
    *,
    label: str,
    params_table: Dict[str, Dict[str, Tuple[str, ...]]],
    max_items: int,
    max_bytes: int,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """两个 `*_refs` 校验的**公共内核**：返回 `(reason_code, details)`，`None` = 通过。

    `check_output_refs` / `check_input_refs` 只是各带一张表 + 一套上限调它。

    ⚠️ **为什么必须抽成一个内核**：两条链的判据**一字不差**（数组、条数、大小、kind 在表内、
    params 必填键非空标量、未知键放行）。各写一份的下场不是「多几行」，而是**慢慢分叉** ——
    而分叉的方向恰恰是「服务端收了一条 SDK 认为合法、前端却处理不了的引用」。

    `label` 只用在 `not_array` 的 `details` 键名上（`{label}_type`）：两条链的 `reason_code`
    是**共用同一套字符串**的（`not_array` / `too_large` / ...），排障时要一眼看出坏的是哪一份。

    规则（2026-10-09 / 2026-10-16 用户拍板）：
      · 必须是「对象数组」，≤ `max_items` 项，序列化后 ≤ `max_bytes`；
      · 每条**必填** `kind`（∈ `params_table`）与 `params`（对象）；
      · `params` 里该 kind 声明的 `required` 键**必须给**、其值只允许**非空标量**；
        `optional` 里声明过的键，给了也同样按非空标量校验；
      · **未知键放行** —— 顶层与 `params` 内都是「只校验已知字段」，表外的键（运行方自己的
        扩展字段）**连值都不看**，嵌套对象 / 数组一样放行（总大小仍受 `max_bytes` 约束）。
    """
    if refs is None:
        return None
    if not isinstance(refs, list) or any(not isinstance(item, dict) for item in refs):
        return ("not_array", {f"{label}_type": type(refs).__name__})
    if len(json.dumps(refs, ensure_ascii=False)) > max_bytes:
        return ("too_large", {"max": max_bytes})
    if len(refs) > max_items:
        return ("too_many", {"max": max_items, "got": len(refs)})

    for index, item in enumerate(refs):
        kind = item.get("kind")
        if not kind:
            return ("kind_missing", {"index": index})
        if kind not in params_table:
            return (
                "kind_invalid",
                {"index": index, "kind": kind, "valid": list(params_table)},
            )

        params = item.get("params")
        if not isinstance(params, dict):
            return ("params_not_object", {"index": index, "kind": kind})

        spec = params_table[kind]
        for key in spec["required"]:
            if key not in params:
                return ("params_missing_key", {"index": index, "kind": kind, "key": key})
        # ⚠️ **只校验该 kind 在表里声明过的键**（`required` + `optional`）；**表外的键
        #    （含未知键）整条不碰、值长什么样都放行** —— 这就是上面那两张表、
        #    本函数的 docstring 与 `CLAUDE.md` 三处同口的那句「未知键放行」。
        #    别照「遍历 `params.items()`」去理解它：那样会把运行方自己的扩展字段
        #    （典型是嵌套对象）判成 `params_bad_value`，**整条终态上报都发不出去** ——
        #    而 `params` 总大小本来就被 `max_bytes` 卡着。
        for key in (*spec["required"], *spec.get("optional", ())):
            if key not in params:
                continue
            value = params[key]
            # 非空标量：`None` / 空串 / 容器都拒 —— 这些让「参数给了但等于没给」，
            # 而前端拿到空值只能拼出一个查不到的请求。
            if not isinstance(value, (str, int, float)) or (
                isinstance(value, str) and not value.strip()
            ):
                return ("params_bad_value", {"index": index, "kind": kind, "key": key})
    return None


def check_output_refs(
    refs: Optional[List[Dict[str, Any]]],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """校验 `output_refs`（**产出**「去哪儿看」的引用），返回 `(reason_code, details)`。

    **判据只有这一份实现** —— SDK（`task_runtime.runtime._check_output_refs`）与服务端
    （`task_center.services.task_service._check_output_refs`）都调它，各自把 `reason_code`
    翻成自己的异常类型与文案（前者 `TaskProtocolError`，后者 `InvalidParameterError` + i18n）。
    规则说明见 `_check_refs`（两条链共用那一份）。
    """
    return _check_refs(
        refs,
        label="output_refs",
        params_table=OUTPUT_REF_PARAMS,
        max_items=OUTPUT_REFS_MAX_ITEMS,
        max_bytes=OUTPUT_REFS_MAX_BYTES,
    )


def check_input_refs(
    refs: Optional[List[Dict[str, Any]]],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """校验 `input_refs`（**输入**「从哪儿来」的引用），返回 `(reason_code, details)`。

    与 `check_output_refs` **同一个内核**（`_check_refs`）、同一套 `reason_code`，只是换表
    （`INPUT_REF_PARAMS`）—— 所以调用方按 `reason_code` 翻文案时**必须自己知道坏的是哪一份**
    （消息键前缀 `input_ref` / `output_ref` 是分开的）。

    **可选**：`None` = 这一次上报没带，直接放行。老运行方不带它也照常上报 —— 设成必填会把
    「失败上报」本身拒掉，等于把失败原因吃掉（这正是本仓库栽过的那类静默吞掉）。
    """
    return _check_refs(
        refs,
        label="input_refs",
        params_table=INPUT_REF_PARAMS,
        max_items=INPUT_REFS_MAX_ITEMS,
        max_bytes=INPUT_REFS_MAX_BYTES,
    )


#: 失败原因**代号**的长度上限。代号是「消息键」（如
#: `err.task_center.subtask.timed_out`），128 字符是给运行方自己的命名空间留足余量。
ERROR_CODE_MAX_LEN = 128

#: `error_params` 序列化后的字节上限（与 `OUTPUT_REFS_MAX_BYTES` 同量级）。
#: 它装的是**渲染占位符的实参**（还有任务级那条的「零件数组」），不是正文。
ERROR_PARAMS_MAX_BYTES = 4096

#: 失败原因代号的**写死枚举**（2026-10-10 用户拍板）—— 运行方**不许自创**代号。
#:
#: ⚠️ **形状与 `OUTPUT_REF_PARAMS` / `OUTPUT_REF_KINDS` 同构**，理由也一样：代号是**跨进程
#: 契约**（运行方填、管理方渲染），两边必须对同一张表。区别只在于 —— 引用那边管的是
#: 「去哪儿看」，这里管的是「为什么失败」。
#:
#: 为什么**不**放行未知代号（这是与 `output_refs` 的「未知键放行」**不同**的一条）：
#: 引用里放未知键只是多带了一份前端用不上的数据，无害；而**失败原因是要渲染给人看的** ——
#: 放行未知代号 = 允许运行方往管理方的界面上写任意文本（一句自造的话、或一个拼错的键），
#: 读的人看到的是「原因」两个字后面跟着一串谁也不认识的东西。**收成枚举**之后，
#: 填错当场被拒（1001），运行方只能改；管理方渲染时也**保证**能翻出来（词条齐全由
#: `tests/unit/task_center/test_task_center_error_reason_i18n.py` 的对照用例钉住）。
#:
#: 加一个新代号 = **两处一起改**：这里加一行 + `messages/{zh-CN,en-US}.yaml` 各加一条
#: （漏了词条，运行方发过来的那条会上报失败；漏了这里，一样被拒 —— 两边都不会静默）。
ERROR_REASON_CODES: Tuple[str, ...] = (
    # ── 任务中心**自己产生**的（运行方不会发这些）──
    "err.task_center.task.stopped_by_user",
    # 任务级收口那条「零件拼接」的外壳（零件放在 `error_params["parts"]`）
    "err.task_center.task.aggregate_failed",
    "err.task_center.subtask.runner_unbound",
    "err.task_center.subtask.dispatch_failed",
    "err.task_center.subtask.dispatch_exhausted",
    "err.task_center.subtask.timed_out",
    "err.task_center.subtask.pending_stuck",
    # ── 运行方（知识库）**上报**的 ──
    "err.kb.task_center.runner_abandoned",
    "err.kb.task_center.step_hard_timeout",
    "err.kb.task_center.no_downstream",
    "err.kb.task_center.no_dispatch",
    "err.kb.task_center.downstream_failed",
    "err.kb.task_center.compile_failed",
    "err.kb.task_center.wiki_compile_failed",
    "err.kb.task_center.master_settled",
)


def check_error_reason(
    code: Optional[str], params: Optional[Dict[str, Any]]
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """校验失败原因的**代号 + 参数**（§4.2 ① 的扩展，2026-10-10 加）。`None` = 通过。

    ── 为什么要有它（多语言）──

    失败原因原来是**写入时**就 `translate()` 成一句话存库的，而写它的多半是**后台循环 /
    派活时**（超时、防僵尸、派活判死）—— 那些帧里没有请求上下文，`translate()` 只能拿到
    `DEFAULT_LOCALE`，于是**语言在写的那一刻就被冻住**（实测库里 34 条是英文、
    12 条是硬编码中文）。所以改成：**存代号 + 参数，读的时候按请求的 `X-Lang` 现场渲染**。

    `error_message` **不被替换** —— 它仍是「原文槽」（引擎/下下游的自由文本，
    永远没有代号）+ 老数据兜底 + 排障快照。可翻译的那半走这里，不可翻译的那半走它。

    ── 判据只有这一份 ──

    与 `check_output_refs` 同一做法：SDK（`task_runtime.runtime._check_error_reason`）
    与服务端（`task_center.services.task_service._check_report_payload`）**调的是同一个函数**，
    各自把 `reason_code` 翻成自己的异常类型与文案。

    规则：
      · `code` 可以是 `None` —— 老运行方只报 `error_message`，行为一字不变；
      · 给了就必须是**非空字符串**、长度 ≤ `ERROR_CODE_MAX_LEN`；
      · **而且必须 ∈ `ERROR_REASON_CODES`**（写死枚举）—— 见那段常量的说明：
        这里**刻意不放行未知代号**（与 `output_refs` 的「未知键放行」不同），因为失败原因
        是要渲染给人看的，放行等于允许运行方往管理方界面上写任意文本；
      · `params` 可以是 `None` 或 dict；序列化后 ≤ `ERROR_PARAMS_MAX_BYTES`。
    """
    if code is None and params is None:
        return None
    if code is not None:
        if not isinstance(code, str) or not code.strip():
            return ("code_invalid", {"code_type": type(code).__name__})
        if len(code) > ERROR_CODE_MAX_LEN:
            return ("code_too_long", {"max": ERROR_CODE_MAX_LEN, "got": len(code)})
        if code not in ERROR_REASON_CODES:
            return (
                "code_unknown",
                {"code": code, "valid": list(ERROR_REASON_CODES)},
            )
    if params is not None:
        if not isinstance(params, dict):
            return ("params_not_object", {"params_type": type(params).__name__})
        try:
            size = len(json.dumps(params, ensure_ascii=False))
        except (TypeError, ValueError):
            return ("params_not_serializable", {})
        if size > ERROR_PARAMS_MAX_BYTES:
            return ("params_too_large", {"max": ERROR_PARAMS_MAX_BYTES, "got": size})
    return None


class TaskStoppedDispatch(BaseModel):
    """「这条任务被用户停了」的报文：管理方**通知**运行方（§4.1 ③，2026-09-30 加）。

    ⚠️ **这是通知，不是命令** —— 管理方只是把「用户点了停止、这条任务已就地收成
    `failed`」这个**事实**告诉运行方，**停不停、怎么停，由运行方自己决定**：能停就停，
    停不掉（比如下游任务已经发出去了）就**只把接管的文档交还出去**，那也是合法的回应。

    为什么非要有它：`task.stop` 从前**只在任务中心内部办、不出站**（判死还没开始的那几步
    + 把任务与当次运行就地收口），后果是运行方**不知道任务已经终止**，继续盯着那份被接管的
    文档 —— 于是用户在文档页**重新解析 / 删除文档 / 整库重置**三个入口**全被拦**，要等那个
    还在跑的活自己结束（再等 30 分钟兜底）、或最长到 6 小时硬超时。用户看到的是「任务页说
    已停、文档页说还在处理中」。**2026-09-30 起收口之后补发这条通知**，就是来堵这个缺口的；
    它仍然是**尽力而为**的，所以运行方那侧还得有自己的兜底（例如定期读一次管理方状态）。

    粒度是**整个任务**（与 `RemoveTaskDispatch` 同）：`task.stop` 能停的只有**还没开始**
    的那几步，它们**从来没派出去过**、运行方那边没有任何残留需要指认 —— 真正要告诉它的
    是「这条任务整体停了，别再等它了」。所以只要三个字段。

    到达方式：**尽力而为** —— 管理方**单次发送、不重发**（超时见 `TASK_CENTER_NOTIFY_TIMEOUT`），
    失败只记一条 warning，**绝不让「停止」本身失败**（停止是本地的事，不该被一次网络报错
    拖住）。所以丢了也只是退回 30 分钟兜底那条老路，不会更差。

    **幂等是硬要求**：任务不存在 / 已经交还过 / 压根没接管过，运行方**都必须回成功** ——
    今天虽然不重发，但「重复调用不出错」是这条契约的前提（运维手工重放、或将来改成带重发）。
    """

    tenant_id: str = Field(description="租户 ID")
    space_id: str = Field(description="空间 ID")
    task_id: str = Field(description="任务实例 ID（tasks.id）—— 这条任务被用户停止了")

    class Config:
        extra = "ignore"


#: **任务来源的写死枚举**（2026-10-13 用户拍板）—— 格式 `{运行方 code}.{对象}.{通道}`。
#:
#: 与 `OUTPUT_REF_PARAMS` 的 `kind` 同族：任务中心是**多运行方**的，裸 `manual` 这种名字
#: 迟早和别人撞，所以前缀一律带运行方的 code（知识库那条就是 `knowledge_base`）。
#:
#: ⚠️ **运行方不许自创**（与 `ERROR_REASON_CODES` 同一条纪律，与 `output_refs` 的
#: 「未知键放行」相反）：它是**跨进程契约** —— 运行方填、管理方存**原码**、前端按码映射
#: 文案，三方对同一张表。填不在册的值：SDK 出站前当场炸，管理方收到也拒（1001）。
#:
#: 加一个新来源 = **三处一起改**：这里加一行 + `deploy/postgres/update/` 里那条 CHECK
#: 约束 + 前端加一条「码 → 文案」映射（管理方存的是码、不存文案，所以中文名不在后端）。
#:
#: **`task_source` 这一列本身可空**（不是「漏传」）：平台侧自己建的任务（定时触发、
#: 人点「立即触发」）不是运行方发起的，留空。**只有 `init_task` 那条路必传** ——
#: 这个区别由 `check_task_source` 与 `TaskService.fire` 分头负责（见各自说明）。
TASK_SOURCES: Tuple[str, ...] = (
    "knowledge_base.file.manual_upload",  # 手动上传（含 COS 预签名直传确认）
    "knowledge_base.file.api_pull",  # API 接入：**我方出站**拉第三方 REST
    "knowledge_base.file.api_push",  # API 开放：第三方**入站推送**
    "knowledge_base.file.storage_upload",  # 文件存储直连：从 MinIO / S3 出站拉
)

#: 在册取值集合。**从 `TASK_SOURCES` 推导** —— 别再单独维护一份，两份必然漂移。
TASK_SOURCE_VALUES = frozenset(TASK_SOURCES)


def check_task_source(value: Optional[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """校验任务来源，返回 **`(reason_code, details)`**（`None` = 通过）。

    **判据只有这一份**：SDK（`task_runtime.runtime.TaskRuntime.submit`）与服务端
    （`task_center.services.task_service.TaskService.init_task`）调的是同一个函数，
    各自把 `reason_code` 翻成自己的异常类型与文案 —— 与 `check_output_refs` /
    `check_error_reason` 同一做法。两边各抄一遍必然漂移，而漂移的方向恰恰是
    「SDK 认为合法、管理方却拒」，那会让一次提交白等满退避。

    ⚠️ **它只服务 `init_task` 那条路，所以 `None` 是不通过（`missing`）的** ——
    「来源必传」正是这条路的接口约定。平台侧自己建的任务（定时触发 / 人工立即触发）
    **不走这个判据**，它们的列留空是合法的（见 `TASK_SOURCES` 那段说明）。

    规则（2026-10-13 定）：
      · 必须有值、且是**非空字符串**；
      · 必须 ∈ `TASK_SOURCES`（写死枚举）—— **刻意不放行未知值**，理由见那段常量。
    """
    if value is None:
        return ("missing", {})
    if not isinstance(value, str) or not value.strip():
        return ("missing", {"task_source_type": type(value).__name__})
    if value not in TASK_SOURCE_VALUES:
        return (
            "invalid",
            {"task_source": value, "valid": list(TASK_SOURCES)},
        )
    return None


class TaskSubmission(BaseModel):
    """发起方（运行方）要提交的一条任务（§4.2 ② `init_task` 的入参）。

    **不含 `tenant_id`**：租户是身份、不是业务字段，由
    `TaskRuntime.submit(..., tenant_id=...)` 显式给，或回落 `TenantContext`。

    ⚠️ **提交路径不能只靠 `TenantContext`**：后台投递器（参考文档 Task 6 的 outbox
    dispatcher）不是请求路径，那一帧里没有上下文，回落会收出空租户、`require_tenant`
    抛 `TenantIsolationError`（3005）—— 而 3005 是「明确拒绝」，在后台会被判成
    永不重投的死信。**后台调用必须显式传 `tenant_id`。**

    **本版没有幂等键**：服务端 `init_task` 现在不做幂等去重（调两次建两条独立任务），
    所以既不接 `idempotency_key` 也不接 `labels` —— 接了也只能静默忽略，那会让
    「新建」这个结论变成谎话。等服务端幂等落地再按加法补。
    """

    definition_id: str = Field(description="用哪个编排定义")
    space_id: str = Field(description="这次运行落在哪个空间（必须是该版本自带的空间）")
    task_id: Optional[str] = Field(
        default=None,
        description="任务 id 由**发起方自己生成**时填进来；不传 = 管理方生成。"
                    "发起方要先把 id 落到自己的行上（不然只能等回包才知道写什么）就填它 —— "
                    "管理方收下即用；撞了已有任务回 `ResourceAlreadyExistsError`（1006），"
                    "对自带 id 的调用方那是「复用既有任务」（上一次其实建成了、只是响应没回来），"
                    "**不是拒绝** —— 详见 `SubmissionDisposition.REUSED`",
    )
    inputs: Dict[str, Any] = Field(
        default_factory=dict, description="任务级入参；没有就是空对象"
    )
    version_no: Optional[int] = Field(
        default=None, description="钉哪个版本；不传 = 该定义已发布的、版本号最大的那一版"
    )
    name: Optional[str] = Field(
        default=None, description="任务实例名；不传或只给空白 = 退回定义名（纯展示，不影响跑哪一版）"
    )
    operator_id: Optional[str] = Field(
        default=None,
        description="发起这次操作的人（user_id，不是用户名）；拿不到就留空 —— "
                    "空值是合法状态（触发器/内部调用本来就没有人），不是漏记",
    )
    task_source: Optional[str] = Field(
        default=None,
        description="任务来源，∈ 协议层 `TASK_SOURCES`（写死枚举，不允许自创）。"
                    "⚠️ **本字段在 `init_task` 那条路上是必传的** —— 模型上给 `None` 默认值"
                    "只是为了不破坏「先构造再补」的写法，必填由 `check_task_source` 卡"
                    "（`TaskRuntime.submit` 出站前、`init_task` 入口各判一次）",
    )

    class Config:
        extra = "ignore"


class TaskItemResponse(BaseModel):
    """任务实例（读的是 `tasks` 的最新快照）。"""

    id: str = Field(description="主键：任务的稳定身份，重跑也不换")
    tenant_id: Optional[str] = Field(default=None, description="租户")
    definition_id: str = Field(description="用的是哪个定义")
    definition_version_no: int = Field(description="用的是哪个版本号（钉死）")
    space_id: Optional[str] = Field(default=None, description="空间 ID（取自这次运行所用的那个版本）")
    task_name: Optional[str] = Field(default=None, description="任务实例名")
    trigger_id: Optional[str] = Field(default=None, description="是哪个触发触发的")
    task_source: Optional[str] = Field(
        default=None,
        description="任务来源（∈ `TASK_SOURCES`）。**空 = 平台侧自己建的任务**"
                    "（定时触发 / 人工立即触发），或这列诞生之前的存量行；"
                    "运行方发起的任务一律有值。文案由前端按码映射",
    )
    inputs: Dict[str, Any] = Field(
        default_factory=dict, description="这次运行的任务级入参（收口后的实值）"
    )
    current_attempt: int = Field(default=1, description="当前跑到第几次运行（指针，首次=1）")
    status: str = Field(
        default="pending", description="pending / running / completed / failed"
    )
    error_message: Optional[str] = Field(default=None, description="失败原因")
    state_version: int = Field(default=0, description="每行版本号（防重复改）")
    created_by: Optional[str] = Field(
        default=None,
        description="建这条任务的人（user_id，不是用户名）；空 = 不是人发起的（触发器/内部调用）",
    )
    updated_by: Optional[str] = Field(
        default=None,
        description="最后一次「用户主动写」这条任务的人（user_id）；系统自动推进状态不写这一列",
    )
    created_at: Optional[str] = Field(default=None, description="创建时间")
    started_at: Optional[str] = Field(default=None, description="首次开始时间")
    finished_at: Optional[str] = Field(default=None, description="最新一次运行结束时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")

    class Config:
        extra = "ignore"


__all__ = [
    "ExecutionDispatch",
    "ExecutionEnvelope",
    "RemoveTaskDispatch",
    "STEP_STATUS_FAILED",
    "STEP_STATUS_PENDING_DISPATCH",
    "STEP_STATUS_RUNNING",
    "STEP_STATUS_SUCCEEDED",
    "STEP_STATUS_UNKNOWN",
    "STEP_STATUS_VALUES",
    "StepStatusQuery",
    "TASK_SOURCES",
    "TASK_SOURCE_VALUES",
    "TaskItemResponse",
    "TaskStoppedDispatch",
    "TaskSubmission",
    "check_task_source",
]
