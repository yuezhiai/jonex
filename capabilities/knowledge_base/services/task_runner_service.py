#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心接入：运行方的 handler（`parse` / `compile`，外加只服务遗留任务的 `ingest`）
+ `remove_task`。

继承 `RunnerBase`，只登记「哪个 `step_code` 干什么」（`handlers()`）与「删任务时清什么」
（`remove_task`）；`execute` 那一套（`step_code` 分流、重派幂等、登记撤销、上下文进出）
由基类兜住，这里一个字都不写。

本模块同时是**发起侧**的唯一出口：`submit_ontology_task()`（§4.2 ②）。上传路径
（`document_service._upload_via_task_center`）与看门狗补交（`task_watch_service`）
共用它 —— 「哪一档算成」（`submission_accepted`）这种最容易判反的判据只能有一份。

## 这一步在整条链上的位置

```text
任务中心派活 execute(execution)
  └─ RunnerBase.execute          认重派（执行登记）→ 绑上下文 → 分流到下面某个 handler
       └─ TaskRunnerService._on_parse / _on_ingest / _on_compile
            ├─ 读接管行、读文档、校验（不合格 → 显式拒）
            ├─ 写接管行「本步开始」        ← 必须在调 atomic-rag 之前
            ├─ 把这一步发给下游            ← 拿到 task_id 就完事，**不等它跑完**
            └─ 回 ApiResponse.ok()          ← 含义是「我收下了」，不是「我干完了」
```

## 四条编排（第一步交给谁干）

**三类库全都经任务中心**（分叉点是 `document_service.upload_document` 里
`kb_type in SUPPORTED_KB_TYPES` 那一处），各自落到一条编排上。三份平台级定义**都是
两步 `parse_1 → compile_1`**：

| 库类型 | 编排定义 | 第一步 `parse` | 第二步 `compile` |
| --- | --- | --- | --- |
| `lightrag`（Ontology） | `definition-file-processing-ontology` | atomic-rag `full`（上传）/ `reparse_strict`（重新解析）—— **解析 + 推切片一趟干完** | atomic-rag `ontology_only`（抽本体） |
| `openkb`（Ontology Wiki） | `definition-file-processing-ontology-wiki` | atomic-rag `parse_only`（落 `content.md`） | Wiki 编译（`recompile_openkb`） |
| `jonex_wiki`（Jonex Wiki） | `definition-file-processing-jonex-wiki` | atomic-rag `parse_only`（落 `content_list.json`） | Wiki 编译（`recompile_jonex_wiki`） |

链的选择（后端取值）在**提交那一刻定死**、写进文档行的粘性值
（`extra_metadata.ingest_backend`），运行方与看门狗都按它分流（`_prepare_step` 取进
快照）—— 改库设置只影响下一次提交。

⚠️ **`rag` 链曾经是三步**（`parse` = `prepare_only` 只解析 → `ingest` = `push_only`
推切片 → `compile`）。那两步拆的是**本来就在同一个引擎调用里**的事（`full` /
`reparse_strict` 本身就含推切片），拆开只为了任务中心多显示一行，而「知识抽取」这个
名字指的又是推切片、不是抽本体。2026-09-29 合回两步（定义原地改，见
`008_task_center.sql` 的预置数据段）。**`ingest` 这个 `step_code` 保留**：改版前
遗留的在途任务子任务已经固化在库里，handler 留着它们才跑得完。


**终态不在这里上报。** 这一步到底成没成，由 `TaskWatchService`（30 秒看门狗）盯着
atomic-rag 的任务状态来判、再由它 `report_terminal`。理由见本模块下面「为什么不在
handler 里上报」。

## 两个按钮：重跑步骤，**不是**新任务

知识库页面上的「重新解析 / 重新编译」也由本模块承接（入口在 `document_service` /
`ontology_service`，出口是 `retry_subtask_for_step` / `retry_subtasks_for_steps`）。它们
**不新建任务、不新建编排**：调的是任务中心现成的「重跑某一步」/「重跑某几步」
（`retry_subtask` / `retry_subtasks`，按 `node_id` 寻址）—— 同一条任务、同一个
`task_id`，把那些步骤原地重置、由它重新派活（§4.2 ③ / ④）。

| 按钮 | 重跑哪几步 | 这次是哪种操作（`tc_intent`） |
| --- | --- | --- |
| 重新编译 | `compile_1`（**单步**，`retry_subtask_for_step`） | 不变（`compile` 步对三种来头语义相同） |
| 重新解析 | `parse_1`（外加 `compile_1`，**当它已终态时**；一次调用交出去，`retry_subtasks_for_steps`） | `reparse` —— lightrag 触发 `_submit_reparse`（`reparse_strict`）；两类 wiki 库都**没有严格重解析那个档**（它们不推切片）：都走 `parse_only`，差异只在 `document_service` 那一侧（代次 +1、编译标 `stale`） |

⚠️ **「重新解析」一口气把名单交出去**（不是「只交 `parse_1`、靠连带带出 `compile_1`」），
**但名单里只放已终态的节点**（2026-10-14 修，判据收在 `reparse_step_codes`）：

- 用户按一下按钮期望的就是「解析和本体**一起**重来」，一次提交把这件事**显式**表达出来，
  比「重跑一步、指望它连带」直白；
- 批量口把**先后写进依赖**（第 i 步等第 i-1 步成功），同一个事务里两支都重置 ——
  `compile_1` 不会跟那个正在重建切片的解析步抢跑，中间也没有「上游重置了、下游还没」的
  半成品窗口。逐条论证见 `retry_subtasks_for_steps`；
- ⚠️ 但名单里**不能带上还没跑完的节点**：任务中心对名单里**每一行**都要求「那一步已
  终态」，带上一个 `pending` 的 `compile_1`（= 从未编译过的文档，真机那一档）就是
  **整批被拒** ⇒ 主用例「解析失败后重试」100% 不可用。判据（`pending` 不带 / `running`
  直接拒 / 读不到按保守侧）与逐条论证见 `reparse_step_codes`。

（任务中心的单步重跑从 2026-10-11 起也会**连带下游**了 —— 所以「只交 `parse_1`」现在
也能达到同样效果；这里仍然用批量口，是为了把「这两步是一件事」写死在调用点上，
不依赖下游那一侧的连带规则。）

⚠️ 于是**同一个 `parse` 步背上了两种含义**（上传 → `full` / 重新解析 → `reparse_strict`，
后者的差别是「先删旧切片」）。分流的唯一判据是文档行上的 `tc_intent`，**不是 `step_code`
的字面量** —— 判反了不会报错，只会让一次「重新解析」白跑一遍（`task_intent` 模块
docstring）。

## 三个坑（`runner.py` 的 docstring 里明写过，这里逐条对上）

- **`ApiResponse.ok()` 不传位置参数** —— 签名是 `ok(data=None, message="")`，传个字符串
  会落进 `data`，而派活侧读的是 `success` / `code`，不报错但语义错了（§4.1 ① 要求
  `data: null`）。
- **返回值必须是 `ApiResponse`** —— 裸 dict 会被基类判成「没送达」→ 传送层重发，
  同一个 `execution_id` 最多被调 3 次。
- **异常直接抛** —— 基类会撤销登记再抛；只有**业务上明确要拒**（接管行状态不对、
  文档不存在、`kb_type` 不符）才返回 `ApiResponse.fail(...)`。

## 为什么不在 handler 里上报终态

`RunnerBase.report_success` 的前提是「活已经干完了」。但这一步只是**把任务发给
atomic-rag**，真正干活的是它、跑几十分钟；handler 里等它跑完会占着派活请求不放
（派活侧有 `TASK_CENTER_RUNNER_TIMEOUT` 默认 30 秒的读超时），超时就被判
`DELIVERY_UNKNOWN` → 重发 → 同一个 `execution_id` 被调三次，每次都白等 30 秒。

所以分工是：**handler 负责「发出去」，看门狗负责「盯结果 + 上报」**。
"""

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.capability.response import ApiResponse
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InternalError, ResourceConflictError
from jonex_core.common.i18n import translate
from jonex_core.common.object_storage import get_object_storage_for
from jonex_core.common.tenant import require_tenant
from jonex_core.task_protocol.errors import TaskRuntimeNotBuiltError
from jonex_core.task_protocol.models import (
    STEP_STATUS_FAILED,
    STEP_STATUS_PENDING_DISPATCH,
    STEP_STATUS_RUNNING,
    STEP_STATUS_SUCCEEDED,
    STEP_STATUS_UNKNOWN,
    ExecutionDispatch,
    ExecutionEnvelope,
    StepStatusQuery,
    TaskSubmission,
)
from jonex_core.task_protocol.results import (
    SubmissionDisposition,
    SubmissionResult,
    SubtaskRetryDisposition,
    SubtaskRetryResult,
)
from jonex_core.task_runtime import (
    RunnerBase,
    build_task_runtime,
    get_task_runtime,
    require_execution_context,
)

from . import ingest_backend, task_intent
from ..models import DocStatus
from ..models.task_center import ACTIVE_STATES, TaskCenterDocState
from ..repository.document_repository import KnowledgeDocumentRepository
from ..repository.processing_task_repository import (
    TASK_STATUS_RUNNING,
    TASK_TYPE_PARSE,
    record_processing_task,
)
from ..repository.search_event_repository import safe_record_asset_status_event
from ..repository.task_center_repository import TaskCenterDocumentRepository

logger = logging.getLogger(__name__)

#: 本运行方认的步骤 —— 与内置清单里的三个 `step_code` 一一对应。
#: **运行方只认这三个字面量**，收到别的一律由基类回失败壳。
#:
#: ⚠️ 现役编排（`definition-file-processing-ontology` 等三条）只用
#: `parse` + `compile`；`ingest` 留着**只服务改版前遗留的在途任务**（它们的子任务行
#: 已经固化在 `task_center.subtasks` 里，编排改了两步也拦不住那些行被派活）。
STEP_PARSE = "parse"
STEP_INGEST = "ingest"
STEP_COMPILE = "compile"

#: 引用（`output_refs` / `input_refs`）里 `kind` 的**运行方前缀** = 本运行方在
#: `task_center.runner_definitions` 里的 `code`。任务中心是**多运行方**的，`kind` 不带
#: 前缀迟早和别人撞（谁都在切块）—— 协议层那两张枚举表就是按 `{前缀}.{资源}` 编的。
#: 定义在这个模块：看门狗（`task_watch_service`）已经依赖本模块，反过来 import 会成环。
REF_KIND_PREFIX = "knowledge_base"


async def load_document_name(tenant_id: str, document_id: str) -> Optional[str]:
    """取这份文档的**文件名**（`input_refs` 的 `document_name` 的来源）。

    为什么不复用看门狗那个 `_doc_facts`：那一处顺带要算 backend / kb_type / 来源方式
    （建任务时确实都要），而这里**只要名字** —— 多算的那几项要么白算、要么再查一次库。
    单行读、不跨事务，也就没有「两次读之间文档被删」的顾虑（这里只读一次）。

    查不到（文档已删）回 `None`，由调用方兜（见 `build_input_refs`：回落成 `document_id`）。

    ⚠️ **读失败一律吞成 `None` + 告警**（写这条注释是为了让它别被"顺手"改成往上抛）：
    名字是**展示用**的，而调用它的两条路都在**上报**里 —— 为了一个展示字段把整条上报
    炸掉，等于让一次本来能收口的终态永远收不了口（任务挂到超时）。同一条口径见
    `_master_already_settled` 那句「探查失败不改行为」。
    """
    try:
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                document_id, tenant_id
            )
            return getattr(doc, "file_name", None) if doc is not None else None
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：展示字段不阻塞上报
        logger.warning(
            "读文档名失败（input_refs 的名字回落成 document_id）: doc=%s err=%s",
            document_id,
            exc,
        )
        return None


def build_input_refs(
    *,
    knowledge_base_id: str,
    current_step: str,
    document_id: str,
    document_name: Optional[str],
) -> List[Dict[str, Any]]:
    """输入「**从哪儿来**」的引用（§4.2 ① 的 `input_refs`，2026-10-16 加）。

    与 `output_refs` 是**对称的两份**：那份说「这一步的成果去哪儿看」，这份说「这一步吃的
    是什么」。同样**由运行方填**（只有我们知道自己的输入对应哪个页面）、同样只有
    `kind` + `params`、不带地址，`kind` 带运行方前缀。

    按 `current_step` 分派（2026-10-16 用户拍板）：

      · `parse` / 遗留 `ingest` —— **1 条**：那份文档（`{前缀}.document`）；
      · `compile` —— **2 条**：文档 + 切片（`{前缀}.chunks`）。为什么要两条：编译这一步
        既要知道「编的是哪份文档」，也要能直接跳到「它吃的那批切片」；而切片正是上一步
        解析的产物（上一步的 `output_refs` 就是它）。
      · 其它步骤（认不出的）—— 不给入口（与产出那份的收尾口径一致：宁可不显示，
        也不给一个点进去 404 的按钮）。

    ⚠️ **`document_name` 拿不到时回落成 `document_id`**：协议层要求 `params` 的值是
    **非空标量**，占位名（空串 / `None`）会被**当场拒** —— 那等于我们自己把上报搞坏，
    比少显示一个名字严重得多。回落之后前端至少还能显示点什么。

    ⚠️ 放在本模块（而不是看门狗里）是因为**两处都要用**：看门狗的成功/失败上报，以及
    `remove_task` 那条「用户停止、运行方弃单」的失败上报 —— 而看门狗已经依赖本模块，
    公共函数只能待在依赖的**上游**这一侧。
    """
    if current_step not in (STEP_PARSE, STEP_INGEST, STEP_COMPILE):
        return []
    document = {
        "kind": f"{REF_KIND_PREFIX}.document",
        "params": {
            "knowledge_base_id": knowledge_base_id,
            "document_id": document_id,
            "document_name": document_name or document_id,
        },
    }
    if current_step in (STEP_PARSE, STEP_INGEST):
        return [document]
    # `compile`：文档 + 它吃的那批切片（切片 = 上一步解析的产出）。
    return [
        document,
        {
            "kind": f"{REF_KIND_PREFIX}.chunks",
            "params": {
                "knowledge_base_id": knowledge_base_id,
                "document_id": document_id,
            },
        },
    ]

#: 编排里的**节点 id**（`008_task_center.sql` 的预置定义段）—— 「重新解析 / 重新编译」
#: 靠它指认要重跑哪一步。
#:
#: ⚠️ **重跑一步按 `node_id` 寻址、不按 `step_code`**（`retry_subtask` 的契约：
#: 同一个 `step_code` 可以在编排里出现多次，`parse_1` / `parse_2` 都是 `parse`，
#: 能唯一指认一步的只有节点 id）。所以这里必须写死节点名，不能用 `step_code` 去拼。
#:
#: 名字来自平台级预置定义、且是**用户定的**（沿用 `parse`、不新加第四个步骤代号）：
#: 现役编排的节点是 `parse_1 → compile_1`（`ingest_1` 只剩改版前遗留的在途任务在用，
#: 不在 `_NODE_BY_STEP` 里 —— 它没有重跑入口，也不需要有）。断言只写在这一处 ——
#: 与 `008_task_center.sql` 的一致性由
#: `tests/unit/kb_integration/test_kb_task_runner_service.py` 直接从 SQL 文本里核对。
#:
#: ⚠️ 定义被改过（节点改名 / 增删）时，症状是**重跑一步被明确拒绝**（「节点不存在」），
#: 不是静默走错 —— 那正是 `retry_subtask` 按 `node_id` 寻址换来的好处，别把它改成
#: 「找不到就按 step_code 猜一个」。
NODE_PARSE = "parse_1"
NODE_INGEST = "ingest_1"  # 只服务改版前遗留的在途任务：没有重跑入口，故不在下面那张表里
NODE_COMPILE = "compile_1"

#: `step_code` → 节点名。**只列两个按钮用得到的那两步** —— 它们是对外入口
#: （重新解析 / 重新编译），另一步的补跑不存在。用 `dict` 而不是 `if/else`：
#: 漏一个取值会 `KeyError` 当场炸，而不是静默拼出一个不存在的节点名（那会被任务中心
#: 回「节点不存在」，错误信息指不到这里）。
_NODE_BY_STEP: Dict[str, str] = {
    STEP_PARSE: NODE_PARSE,
    STEP_COMPILE: NODE_COMPILE,
}


def node_id_for_step(step_code: str) -> str:
    """`step_code` → 编排里的节点名（`_NODE_BY_STEP` 是唯一事实来源）。"""
    try:
        return _NODE_BY_STEP[step_code]
    except KeyError:
        # 走不到：两个调用点传的都是字面量常量。留这一道是因为静默拼一个错的节点名
        # 会以「任务中心说节点不存在」的形式出现，与本模块毫无关系。
        raise InternalError(
            message=(
                f"步骤 {step_code!r} 没有对应的编排节点"
                f"（可选 {sorted(_NODE_BY_STEP)}）"
            ),
        ) from None

#: `insert` 那条路（`_submit_insert`）里各步对应的 atomic-rag 执行模式：
#: **键是 (步骤, 库类型)** —— 同一个 `parse` 在各类库上档位不同：
#:
#: - (`parse`, `lightrag`)   → `full`：解析 + 多模态 + **推切片**，一趟干完 —— 与接入任务
#:   中心之前的上传是同一个档位。个别切片推失败仍算整体成功（宽容）。
#: - (`parse`, `openkb`)     → `parse_only`：只解析出 `content.md`（**不推切片**、不写
#:   LightRAG），产物落共享卷喂给「知识编译」那一步。
#: - (`parse`, `jonex_wiki`) → `parse_only`：同样只解析、不推切片、不写 LightRAG；
#:   **产物形状不同**（MinerU 结构化 `content_list.json`，不是重组过的 markdown）——
#:   这个差别由引擎按随请求下发的 `kb_type` 决定，这里只管档位。
#: - (`ingest`, `lightrag`)  → `push_only`：只服务**改版前遗留的在途任务**。
#: - `compile` → 三类库都不走 `insert`：lightrag 走本体抽取（`ontology_only`）、
#:   openkb / jonex_wiki 各走自己的 Wiki 编译。
#:
#: ⚠️ **这张表只覆盖「上传」那一次解析**。lightrag 的同一个 `parse` 步在「重新解析」时
#: 发的是 `reparse_strict`（解析 + 删旧切片 + 推新切片，走 `_submit_reparse`）—— 判据是
#: 文档行上的 `tc_intent`。**两类 wiki 库都没有这个分叉**（它们不推切片）：都发
#: `parse_only` —— 差异全在 `document_service` 那一侧：代次 +1、编译标 `stale`。
_INSERT_MODES: Dict[tuple, str] = {
    (STEP_PARSE, "lightrag"): "full",
    (STEP_PARSE, "openkb"): "parse_only",
    (STEP_PARSE, "jonex_wiki"): "parse_only",
    (STEP_INGEST, "lightrag"): "push_only",
}


def insert_mode_for(step_code: str, kb_type: str) -> str:
    """`(step_code, kb_type)` → atomic-rag 执行模式（`_INSERT_MODES` 是唯一事实来源）。

    取不到就**抛**，不猜一个档位：发错的症状是「切片没推」或「推了不该推的」，
    两者都不报错、只在查询时才浮出来。
    """
    try:
        return _INSERT_MODES[(step_code, kb_type)]
    except KeyError:
        raise InternalError(
            message=(
                f"{kb_type} 库的 {step_code!r} 步没有对应的执行模式"
                f"（可选 {sorted(_INSERT_MODES)}）"
            ),
        ) from None


#: 本运行方认的**库类型**（`kb_type`）。三类库全部接入：
#:   · `lightrag`   —— 页面上的「Ontology」，走 atomic-rag 那条链；
#:   · `openkb`     —— 页面上的「Ontology Wiki」，走 wiki 链（`parse_only` + Wiki 编译）；
#:   · `jonex_wiki` —— 页面上的「Jonex Wiki」，两步形状与 openkb 相同（parse + Wiki
#:     编译），差别是产物形状（`content_list.json` vs `content.md`）与编译入口 / 巡检
#:     各自一个（见下面那两个定义常量与 `_submit_jonex_wiki_compile`）。
#: 域外的值进来一律显式拒 —— 拒比「按 lightrag 的模式去解析一个本该走 parse_only 的库」
#: 安全（那会把它的产物口径写坏）。
_KB_TYPE_LIGHTRAG = "lightrag"
_KB_TYPE_OPENKB = "openkb"
_KB_TYPE_JONEX_WIKI = "jonex_wiki"
SUPPORTED_KB_TYPES = (_KB_TYPE_LIGHTRAG, _KB_TYPE_OPENKB, _KB_TYPE_JONEX_WIKI)

#: 本链用的平台级编排定义（`008_task_center.sql` 预置：`scope=system`、`space_id=NULL`，
#: 现为两步 `parse → compile` —— 2026-09-29 由三步合回，定义原地改，见
#: `008_task_center.sql` 的预置数据段）。上传路径与看门狗补交都要用它 ——
#: **只能有一处字面量**：两处各写一遍的话，改定义 id 时漏掉的那一处会表现为
#: 「任务建不上」，而错误信息是任务中心那句「找不到编排定义」，与本模块毫无关系。
ONTOLOGY_DEFINITION_ID = "definition-file-processing-ontology"

#: openkb（Ontology Wiki）那条链的编排定义（`008_task_center.sql` 预置：两步
#: `parse_1 → compile_1`）。它与另一条 wiki 链（`-jonex-wiki`）逐字同形 —— 差别全在
#: 运行方内部按 `kb_type` 分流（openkb 落 `content.md`、jonex_wiki 落
#: `content_list.json`；编译入口与巡检也各一个），**编排不承载这个差别**。
ONTOLOGY_WIKI_DEFINITION_ID = "definition-file-processing-ontology-wiki"

#: jonex_wiki（Jonex Wiki）那条链的编排定义（`008_task_center.sql` 预置：两步
#: `parse_1 → compile_1`，与上面那条逐字同形）。它是**独立一份定义**而不是复用
#: `-ontology-wiki`：定义名就是任务中心页面上「这条任务属于哪类库」，混用会让用户
#: 分不出这条链跑的是哪一类库。
ONTOLOGY_JONEX_WIKI_DEFINITION_ID = "definition-file-processing-jonex-wiki"

#: 后端取值。**字面量与 `ingest_backend.VALID_BACKENDS` 一致**（那边是权威），
#: 这里给常量只是为了调用点不出现裸字符串 —— 拼错一个字母的话，`_DEFINITION_BY_BACKEND`
#: 会 `KeyError`（当场炸），而不是静默按 `rag` 发下游。
_BACKEND_RAG = "rag"

#: 后端取值 → 走哪条编排。取值名说的是「用哪个引擎跑第一步」。
#:
#:   · `rag` → `definition-file-processing-ontology`（两步），第一步由
#:     atomic-rag 干（`full` / `reparse_strict` —— 解析 + 推切片一趟）
#:
#: 用 `dict` 而不是 `if/else`：漏一个取值会 `KeyError` 当场炸，而不是静默落到默认那条
#: （静默落错的定义 → 第一步按错误引擎发下游 → 两边都不认）。
_DEFINITION_BY_BACKEND: Dict[str, str] = {
    _BACKEND_RAG: ONTOLOGY_DEFINITION_ID,
}


def definition_for_kb_type(kb_type: str, backend: str) -> str:
    """按**库类型**（+ lightrag 的后端取值）选编排定义 —— 提交那一刻的判据。

    `lightrag` 走 atomic-rag 那条（后端取值从 `backend` 传进来，当前恒为 `rag`）；
    两类 wiki 库各只有一条（各自一条 wiki 链）。
    域外取值（任何未知值）**抛** —— 提交侧不能依赖 `_prepare_step` 的闸门，
    那一刻还没有人派活、闸门根本没跑到。
    """
    if kb_type == _KB_TYPE_LIGHTRAG:
        return definition_for_backend(backend)
    if kb_type == _KB_TYPE_OPENKB:
        return ONTOLOGY_WIKI_DEFINITION_ID
    if kb_type == _KB_TYPE_JONEX_WIKI:
        return ONTOLOGY_JONEX_WIKI_DEFINITION_ID
    raise InternalError(
        message=f"库类型 {kb_type!r} 还没接任务中心（可选 {sorted(SUPPORTED_KB_TYPES)}）",
    )


def definition_for_backend(backend: str) -> str:
    """把开关的取值翻成编排定义 id（`_DEFINITION_BY_BACKEND` 是唯一事实来源）。

    调用点有两个（上传路径与看门狗补交），**都必须用它** —— 两处各写一份 `if` 的话，
    开关多一个取值时漏掉的那一处会把任务提交到**另一条**编排上：任务照样建得起来
    （定义都存在），只是第一步走了错的引擎，症状要到第一步派活才看得见。
    """
    try:
        return _DEFINITION_BY_BACKEND[backend]
    except KeyError:
        # 走不到：`ingest_backend.resolve_backend` 已经把取值校验过了。留这一道是因为
        # 静默落到某条默认编排的后果要到「第一步按错的引擎发下游」才看得见 ——
        # 而且是**配置**问题（服务端取值），不是调用方参数问题。
        raise InternalError(
            message=(
                f"KB_INGEST_BACKEND 取值 {backend!r} 没有对应的编排定义"
                f"（可选 {sorted(_DEFINITION_BY_BACKEND)}）"
            ),
        ) from None


#: 文档**来源方式**（`knowledge_documents.data_source_type` 的现役四值）→ **任务来源码**。
#:
#: ⚠️ **右边是协议层写死的枚举**（`jonex_core/task_protocol/models.py` 的
#: `TASK_SOURCES`），运行方不许自创；左边那四个字面量才是本模块自己的词汇
#: （`dtos/data_source.py` 的 `ACCESS_TYPES` 是同一套值）。两边对得上由
#: `tests/unit/kb_integration/test_kb_task_source_mapping.py` 那条「映射值 ∈
#: `TASK_SOURCES`」的用例钉住 —— 改了这里忘了改协议层，任务会一律提交不出去（1001）。
#:
#: **为什么不另存一列来表达来源**：`data_source_type` 是文档行上早就有的真实列
#: （统计已按它分组），四种接入方式与它**一一对应**；再存一份必然漂移。
_TASK_SOURCE_BY_ACCESS_TYPE = {
    "file": "knowledge_base.file.manual_upload",  # 手动上传（含 COS 预签名直传确认）
    "api": "knowledge_base.file.api_pull",  # API 接入：**我方出站**拉第三方 REST
    "api_push": "knowledge_base.file.api_push",  # API 开放：第三方**入站推送**
    "storage": "knowledge_base.file.storage_upload",  # 文件存储直连：MinIO / S3 出站拉
}


def task_source_for_access_type(access_type: Optional[str]) -> str:
    """把「文档来源方式」翻成协议层的「任务来源」码（提交任务时必须带上）。

    **域外取值一律抛**（照 `definition_for_kb_type` / `resolve_backend` 的同一条纪律）：
    静默挑一个默认来源，会把「这条任务到底从哪个入口来的」**记成假的** —— 而这一列
    存在的全部意义就是回答那个问题，记错比记不出来更坏。

    ⚠️ **空值（`None` / 空白）同样抛**：`upload_document` 给所有入库路径都写过
    `data_source_type`（手动上传那条也回落成 `file`），拿不到值说明是**装配错了**，
    不能当成「手动上传」蒙混过去。
    """
    try:
        return _TASK_SOURCE_BY_ACCESS_TYPE[(access_type or "").strip()]
    except KeyError:
        raise InternalError(
            message=(
                f"文档来源方式 {access_type!r} 没有对应的任务来源"
                f"（可选 {sorted(_TASK_SOURCE_BY_ACCESS_TYPE)}）"
            ),
        ) from None


def _ensure_task_runtime() -> None:
    """进程里没有任务运行时的话建一个。

    **为什么是「没有才建」**：`jonex_core.task_runtime.runtime` 的模块 docstring 要求
    「在应用启动入口建好，业务代码里不要随手 build」。但知识库的装配点做不到那一点 ——
    `capabilities/knowledge_base/api/__init__.py` 有一句**模块级**
    `_service = KnowledgeBaseService()`，它导入即执行、早于任何启动钩子，而
    `KnowledgeBaseService` 里就要构造本类（`RunnerBase.__init__` 会**当场**取运行时，
    取不到就抛 `TaskRuntimeNotBuiltError`，这是刻意设计的「早失败」）。

    所以这里退一步：**只补缺、不覆盖**。已经被别人（比如同进程的任务中心）建好就用它的 ——
    运行时是「谁建的不是关键、service_name 只进日志」的门面，复用没有副作用。
    """
    try:
        get_task_runtime()
    except TaskRuntimeNotBuiltError:
        build_task_runtime(service_name="knowledge_base")


class TaskRunnerService(RunnerBase):
    """运行方：把编排里发过来的每一步转成一次下游调用（atomic-rag）。"""

    runner_code = "knowledge_base"

    def __init__(self, runtime=None, registry=None) -> None:
        # 装配顺序要紧：`super().__init__` 会立刻取进程单例，所以先补缺。
        _ensure_task_runtime()
        # ⚠️ **`registry` 必须给持久实现**（默认给的是内存实现：重启与多副本都失效 →
        # 重派会重复建下游任务）。真正的兜底是 `KnowledgeBaseCapability.initialize()`
        # 里那句 `registry.persistent` 自检 —— 那里没通过会拒绝启动。
        super().__init__(runtime=runtime, registry=registry)

    # ========================================================
    # §4.1 ① 派活：登记三个步骤
    # ========================================================

    def handlers(self) -> Dict[str, Callable[[ExecutionDispatch], Awaitable[ApiResponse]]]:
        """**每次派活都会调一次**，所以返回字面量 dict、不在里面查库（基类的要求）。

        `ingest` 留着**只服务改版前遗留的在途任务**（现役两条编排都只有 `parse` +
        `compile`）。删掉它的症状不是报错而是**那些遗留任务这一步回失败壳被就地判死**
        —— 它们本来能跑完，只是排在改版之后。
        """
        return {
            STEP_PARSE: self._on_parse,
            STEP_INGEST: self._on_ingest,
            STEP_COMPILE: self._on_compile,
        }

    async def _on_parse(self, execution: ExecutionDispatch) -> ApiResponse:
        return await self._handle_step(execution, STEP_PARSE)

    async def _on_ingest(self, execution: ExecutionDispatch) -> ApiResponse:
        return await self._handle_step(execution, STEP_INGEST)

    async def _on_compile(self, execution: ExecutionDispatch) -> ApiResponse:
        return await self._handle_step(execution, STEP_COMPILE)

    # ========================================================
    # 三个 handler 的公共骨架
    # ========================================================

    async def _handle_step(
        self, execution: ExecutionDispatch, step_code: str
    ) -> ApiResponse:
        ctx = require_execution_context()
        # 身份只从执行上下文取（它是 `_on_execute` 从派活报文构造、由
        # `acquire_execution` 绑进来的那一份），不从 `data` 里另拼一份。
        tenant_id = require_tenant(ctx.tenant_id)

        # ── 第一段事务：校验 + 记「本步开始」──
        # 单独一段是有意的：① 记账必须在调 atomic-rag **之前**提交（看门狗的计时起点
        # 不能晚于下游任务的存在）；② 不能让一笔事务跨越那个 HTTP 调用、白占连接。
        snapshot = await self._prepare_step(execution, step_code, tenant_id)
        if isinstance(snapshot, ApiResponse):
            return snapshot  # 校验没过，回的是显式拒

        # ── 第二段：发下游（事务已提交，这里不持有任何 DB 连接）──
        try:
            rag_task_id = await self._submit_downstream(step_code, snapshot)
        except Exception as exc:  # noqa: BLE001
            # 发失败 → **显式拒**（子任务直接终态 failed，不回退 pending 重试）。
            # 回退重试在这里没有意义：下游到底是「没建」还是「建了但响应丢了」我们
            # 分不清，重来一次分不清的还是分不清。
            # ⚠️ 但「重来一次」**确实会发生**，只是不由这个失败壳触发：管理方把
            # `success=false` 判成 `REJECTED`（子任务终态、不再重派），可要是这个失败壳
            # 本身没送达（传送层挂了），它判的是 `DELIVERY_UNKNOWN` —— 当轮原地重发同一个
            # `execution_id` 最多 3 次。那几次会重新进 `begin_step`、重新调下游，
            # 由幂等键 `task-execution:{execution_id}` 收口（引擎侧同键复用同一任务行）。
            logger.exception(
                "任务中心 %s 步提交下游失败: doc=%s execution=%s",
                step_code, snapshot.document_id, ctx.execution_id,
            )
            async with get_db_session() as session:
                # ── ⚠️ 顺手把文档也落成 `FAILED`（`compile` 步除外，见下）──
                # 这一步已经判死、不会再重试，可文档要是留在 `PARSING`，用户看到的是
                # 「任务失败、文档还在解析中」，**而且两个出口都走不通**：
                # `reparse_document` 与 `delete_document` 的守卫都按 `parsing` 拒
                # （「文档正在解析/入库中，请等待完成后再…」），他只能等既有巡检兜底
                # （软 1 小时探活 / 硬 6 小时判死）。落 `FAILED` 之后：用户马上看到真实
                # 状态，而且 `FAILED` 是**可以重新解析、也可以删除**的（那两条守卫只拦
                # 解析中 / 入库中）。
                #
                # 口径与看门狗的 `_fail_step` 一致：**`compile` 步不动文档状态**
                # （本体 / Wiki 编译失败时文档本身仍是可用的，缺的只是图或 Wiki）。
                # 落库失败只告警、绝不拦住交还（交还是用户的解封点，与看门狗同一条纪律）。
                err_text = f"{step_code} 步骤未能提交下游任务：{exc}"
                if step_code != STEP_COMPILE:
                    try:
                        doc = await KnowledgeDocumentRepository(session).get_by_id(
                            snapshot.document_id, snapshot.tenant_id,
                        )
                        if doc is not None:
                            await KnowledgeDocumentRepository(session).set_status(
                                doc, DocStatus.FAILED, error_message=err_text[:2000],
                            )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "任务中心 %s 步失败后落文档状态没成功（不影响交还）: doc=%s",
                            step_code, snapshot.document_id, exc_info=True,
                        )
                await TaskCenterDocumentRepository(session).mark_released(
                    snapshot.document_id,
                    error_message=err_text[:2000],
                    # ⚠️ **也得写失败报文**（2026-10-11 补）：上面那句注释说「口径与看门狗的
                    # `_fail_step` 一致」，但在 `last_report` 这一维上原先并不一致 ——
                    # 不写的话，管理方事后主动来问只会得到 `unknown`（「这活不归我」），
                    # 把它当**配置错**处置，而其实这是一次**真实的业务失败**（下游压根没建出来）。
                    # ⚠️ **代号与参数一并存**（2026-10-13 补）：另外两个判死点
                    # （`_fail_step` / `task_stopped`）都存了 `error_code` + `error_params`，
                    # 只有这一处漏了 —— 管理方 `query_step_status` 会把 `last_report`
                    # **原样带回**，读的人按自己的语言渲染；缺了代号，这条链在这一处
                    # **静默退化成一句冻死的中文文本**（「存代号+参数、读时渲染」断了一节）。
                    report={
                        "status": "failed",
                        "error_message": err_text[:2000],
                        "error_code": "err.kb.task_center.downstream_submit_failed",
                        "error_params": {"step": step_code, "error": str(exc)[:500]},
                    },
                )
            return ApiResponse.fail(
                translate(
                    "err.kb.task_center.downstream_submit_failed",
                    params={"step": step_code, "error": str(exc)},
                    fallback=f"{step_code} 步骤未能提交下游任务：{exc}",
                )
            )

        # ── 第三段事务：回写下游任务号 ──
        if (step_code == STEP_COMPILE and snapshot.kb_type == _KB_TYPE_JONEX_WIKI
                and rag_task_id is None):
            # Accepted durably, but no engine task exists until the KB window closes.
            # The watch binds the eventual real task id and reports its real result.
            return ApiResponse.ok()
        async with get_db_session() as session:
            written = await TaskCenterDocumentRepository(session).set_rag_task_id(
                snapshot.document_id,
                execution_id=ctx.execution_id,
                rag_task_id=rag_task_id,
            )
        if not written:
            # 回写落空 = 在发下游那次 HTTP 的宽度里，这一行被 release 了、或换了执行编号
            # （`remove_task` / 看门狗判死 / 另一次执行接手）。仓储的 docstring 明写这时
            # 「那意味着下游任务是个没人认领的产物，调用方应当放弃继续推进这一步，
            # **而不是把它当成成功**」。
            # ⚠️ 必须回**显式拒**、不能回 `ok()`：回 ok 等于告诉基类「这次受理成立了」，
            # 它就不会撤销执行登记 —— 于是 ① 同一个 `execution_id` 的登记被**永久占住**
            # （管理方重派全被 `ALREADY_TAKEN` 挡回、一个字都不做就回成功，那个子任务
            # 一直挂到编排超时）；② 下游那个**真建出来了**的任务没人认领（接管行已
            # released、`list_active` 扫不到它），既不推进也不判死，还白扣配额。
            # **这里不调 `mark_released`**：行可能已经属于另一次执行（state 仍是
            # `managed`、`execution_id` 换成了别人），按 document_id 释放会把别人的活拆掉。
            logger.warning(
                "任务中心 %s 步回写下游任务号落空（接管行已交还或已换执行）: "
                "doc=%s execution=%s rag_task=%s",
                step_code, snapshot.document_id, ctx.execution_id, rag_task_id,
            )
            return ApiResponse.fail(
                translate(
                    "err.kb.task_center.downstream_unclaimed",
                    params={"step": step_code, "rag_task_id": rag_task_id},
                    fallback=(
                        f"{step_code} 步骤的下游任务 {rag_task_id} 无人认领："
                        f"接管记录已交还或已被另一次执行接手"
                    ),
                )
            )
        logger.info(
            "任务中心 %s 步已投递: doc=%s kb=%s execution=%s rag_task=%s",
            step_code, snapshot.document_id, snapshot.knowledge_base_id,
            ctx.execution_id, rag_task_id,
        )
        # **这一步的终态不在这里报**（见模块 docstring）：回 ok 的含义是「我收下了」。
        return ApiResponse.ok()

    async def _rearm_after_console_retry(
        self,
        session: Any,
        *,
        row: Any,
        execution: ExecutionDispatch,
        step_code: str,
        tenant_id: str,
    ) -> Optional[ApiResponse]:
        """任务中心的「**任务重试**」：把已交还的接管行就地重新武装，并按「重新解析」重置文档行。

        ## 为什么要在这里做

        接管行是运行方判断「这一步该不该受理」的**唯一凭据**，而它被**任何一条收口出口**
        写成 `released`（跑完 / 判死 / 用户停止 / 任务被删）。任务中心的「任务重试」
        （`task.console_task_retry`）是**整条任务原地重跑**：它重置自己的子任务、重新派活，
        但**一个字都不碰这张表、也不出站通知运行方**（对比 `stop_task` 会 `_notify_stopped`）。
        于是重跑后的第一枪必然撞上 `released` → 被老口径当场拒（
        `err.kb.task_center.takeover_closed`），任务从 success 变成 failed。

        ⚠️ **不只是「停过的」**：上传成功后接管行本来就是 `released` —— 三类库
        （`lightrag` / `openkb` / `jonex_wiki`）的**任何**文档点它都会这样。两个按钮
        （重新解析 / 重新编译）不受影响：它们走 `rearm_for_rerun` 先武装再请重跑。

        ## 判据为什么够用

        「`released` + 这条 `task_id` 又来派活」= 这条任务又被跑起来了。别的可能：
        · **任务被删**（`remove_task` 也把行写成 `released`）—— 但它不再派活；
        · **用户点停止**（`task_stopped` 同）—— 任务已收成 `failed`，也不再派活。
        所以派活本身就是「这条任务在跑」的证据，不需要再向管理方查一次。

        ## 顺序与事务

        「武装接管行 + 重置文档行」**必须与后面的 `begin_step` 同一次提交**（`session`
        是调用方那一笔事务）：
        · 分开提交的话，中间崩一下会留下「接管行说重跑、文档行还是上一轮的终态」——
          而**上一轮的 `ontology_status=READY` 会让 `compile` 步当场判成功**（假成功）；
        · 更要紧的是 `begin_step` 自带 `state IN 在途` 的 CAS：武装与它之间只要被别人
          插进来，这次派活就没有落点了。

        ## 只做「武装」这一件事 —— **不在这里动文档行**

        后面那几道闸门（派活报文与接管行交叉核对 / 文档还在不在 / 库类型对不对）
        全是主流程**已经有的**，让它们照常跑：它们回绝时会「**先交还再拒**」，
        而那时行已经是 `managed`、正好被交还回去，净效果与「行本来就是 `released`」
        一模一样（文档行一个字没动）。文档行的重置排在**所有这些闸门之后**
        —— 即 `begin_step` 成功之后（见 `_reset_document_after_console_retry`）。
        排在前面的话，任何一条回绝路径都会留下「文档说在重解析、却没有任何人在跑」的
        假状态（接管行在途、文档 `parsing`、任务中心那边那一步却是失败的）。

        返回 `None` = 武装好了、**继续走主流程**（后面还有那几道闸门要过）；
        返回 `ApiResponse` = 明确拒绝（CAS 没抢到）。
        """
        # 抢 CAS 把接管行拿回来。`rearm_for_step` 的条件是 `state='released'` ——
        # 它就是这次处置的**并发安全闸**：抢不到说明这行在「读到写」之间被别处动过
        # （另一次上传接管了它 / 任务被删了 / 又回到在途），按老口径明确拒绝。
        # `clear_handled=True`：上一轮的收口痕迹必须清（见仓储那处的两条理由）。
        armed = await TaskCenterDocumentRepository(session).rearm_for_step(
            row.document_id,
            tenant_id=tenant_id,
            task_id=row.task_id,
            step_code=step_code,
            clear_handled=True,
        )
        if not armed:
            return ApiResponse.fail(
                translate(
                    "err.kb.task_center.takeover_closed",
                    params={"task_id": execution.task_id, "state": "<changed>"},
                    fallback=f"任务 {execution.task_id} 的接管记录已变更，不再受理这一步",
                )
            )
        return None

    async def _reset_document_after_console_retry(
        self,
        session: Any,
        *,
        doc: Any,
        step_code: str,
        kb_type: str,
        task_id: str,
        document_id: str,
    ) -> None:
        """「任务重试」受理之后：把文档行按「重新解析」的口径重置。

        **调用点排在 `begin_step` 成功之后**（理由见 `_rearm_after_console_retry`
        与那里的调用点注释）—— 到这一步为止，闸门全过了、这次派活真的成立了。

        与用户点「重新解析」**共用同一个函数**（`reset_document_for_rerun`），
        所以两条路在文档侧留下的形状一定一样。
        """
        from .kb_type_service import LLM_WIKI_TYPES

        if step_code != STEP_PARSE:
            # 重跑的第一步**不是** `parse`：文档行一个字不动。
            #
            # 为什么不动：`reset_document_for_rerun` 会**把代次 +1** = 「重新解析」的语义，
            # 而它必须与「第一步会重推切片」配套（引擎按代次 fencing 掉在途的旧结果）。
            # 单独重跑 `compile` 时切片还是上一代推的，代次 +1 会让那份**正在被用的**
            # 切片被判成过期产物 —— 那是静默的数据损坏，比「这步跑不出想要的结果」糟得多。
            # 实际路径上走不到这里（「任务重试」的第一枪永远是 `parse_1`，它跑完会把行交回
            # `managed`，后面的 `compile` 就进不了那道闸）—— 留着是因为**编排定义是数据、
            # 可以被改**，猜不得。
            logger.warning(
                "任务重试：重跑的第一步是 %s（不是 parse）—— 只重新武装接管行、不动文档行: "
                "doc=%s task=%s",
                step_code, document_id, task_id,
            )
            return

        from .document_service import reset_document_for_rerun

        rerun_rollback = reset_document_for_rerun(doc, is_llm_wiki=kb_type in LLM_WIKI_TYPES)

        # 第 3 期：这次 `-> parsing` 的流转留痕（与「重新解析」按钮同一口径，
        # 只是 reason 区分来源）。`reset_document_for_rerun` 是直写属性。
        await safe_record_asset_status_event(
            session, doc.tenant_id,
            asset_id=doc.id,
            status=doc.status,
            previous_status=rerun_rollback.get("status"),
            reason="task_retry",
            kb_id=doc.knowledge_base_id,
        )

        # `tc_intent=reparse` —— 让这一枪 `parse` 发引擎的**严格重解析**
        # （`reparse_strict`：解析 + 删旧切片 + 推新切片），这正是用户要的
        # 「清掉旧的解析数据、重来一次」。
        #
        doc.extra_metadata = task_intent.with_intent(
            doc.extra_metadata, task_intent.INTENT_REPARSE,
        )

        # 埋点：这是**新代次**的一次解析开工。`reset_document_for_rerun` 是直写属性
        # （不走 `set_status`），所以没有自动埋点 —— 不补的话，收口那次
        # （`set_status(READY)`，仍走仓储的自动埋点）会写一条**没有对应 running 行**
        # 的 success 事件，统计里就多一次「无中生有」的解析。
        await record_processing_task(
            session, doc.tenant_id,
            asset_id=doc.id, task_type=TASK_TYPE_PARSE,
            status=TASK_STATUS_RUNNING, doc_id=doc.id,
            kb_id=doc.knowledge_base_id, content_generation=doc.content_generation,
        )
        await session.flush()
        logger.warning(
            "任务重试：接管行已重新武装 + 文档按「重新解析」重置 "
            "（doc=%s task=%s step=%s 新代次=%s）",
            document_id, task_id, step_code, doc.content_generation,
        )

    async def _prepare_step(
        self, execution: ExecutionDispatch, step_code: str, tenant_id: str
    ):
        """校验这次派活、写「本步开始」，返回后续要用的文档快照。

        通过校验时返回 `_StepSnapshot`；**没通过时返回 `ApiResponse`（显式拒）**，
        由调用方原样回给派活侧。用返回值兼做两种含义是刻意的 —— 显式拒与快照都是
        「`_handle_step` 要立刻回给派活侧的东西」，多一条异常通道只会让「拒」和
        「内部错误」在调用点长得一样。
        """
        async with get_db_session() as session:
            tc_repo = TaskCenterDocumentRepository(session)

            # ① 按 task_id 定位接管行 —— `task_id` 是这次派活里唯一**由我们自己写下**
            # 的定位键（`document_id` 是从 `inputs` 来的，见下面的交叉核对）。
            row = await tc_repo.read_by_task_id(execution.task_id, tenant_id)
            if row is None:
                return ApiResponse.fail(
                    translate(
                        "err.kb.task_center.takeover_missing",
                        params={"task_id": execution.task_id},
                        fallback=f"没有找到任务 {execution.task_id} 的知识库接管记录",
                    )
                )
            retrying = row.state not in ACTIVE_STATES
            if retrying:
                # 已经被交还给既有对账（跑完了 / 判死了 / 用户停了 / 任务被删了）。
                #
                # ⚠️ **这里曾经是一句「这次派活来晚了、拒掉」** —— 那对任务中心的
                # 「**任务重试**」是错的：那条路是**整条任务原地重跑**，而接管行早就
                # 被收口出口写成 `released` 了，于是每次重试的第一枪都被当场拒，
                # 任务从 success 变成 failed（三类库的任何文档都这样）。
                # 现在改成「就地重新武装」，判据与事务边界见 `_rearm_after_console_retry`。
                # **返回非 None 才是真拒绝**（CAS 没抢到）。
                rejected = await self._rearm_after_console_retry(
                    session,
                    row=row,
                    execution=execution,
                    step_code=step_code,
                    tenant_id=tenant_id,
                )
                if rejected is not None:
                    return rejected

            # ② 交叉核对：派活报文里的 `document_id` 与接管行必须指同一份文档。
            # `inputs` 由任务中心按上一步的 `outputs` 填好（`bind_inputs`），正常路径
            # 必然一致；不一致说明编排定义被改过或接管行被手工动过 —— 拒绝比「照
            # inputs 干」安全，后者会往一个不属于这条任务的文档上写东西。
            claimed = str((execution.inputs or {}).get("document_id") or "")
            if claimed and claimed != row.document_id:
                # ⚠️ **回绝之前先把接管行交还**（2026-09-30 加，见下面 `doc is None` 那段的
                # 长注释）：不交还的话这份文档会被锁到看门狗那条 30 分钟的兜底才解开，
                # 而任务中心那边**当场**就把这一步判失败了 —— 用户看到「任务失败、文档还在
                # 处理中」且什么都不能做。`only_if_idle=True`：只交还「没人在途」的卡，
                # 避免拆掉另一次派活的活。
                await tc_repo.mark_released(
                    row.document_id,
                    error_message=(
                        f"派活报文里的 document_id（{claimed}）与接管记录不一致"
                    ),
                    only_if_idle=True,
                )
                return ApiResponse.fail(
                    translate(
                        "err.kb.task_center.document_mismatch",
                        params={"expected": row.document_id, "claimed": claimed},
                        fallback=(
                            f"派活报文里的 document_id（{claimed}）与接管记录"
                            f"（{row.document_id}）不一致"
                        ),
                    )
                )

            repo = KnowledgeDocumentRepository(session)
            doc = await repo.get_by_id(row.document_id, tenant_id)
            if doc is None:
                # 文档被删了（软删也算）。拒掉，让子任务终态 failed —— 硬撑下去
                # 只会往一个已经不存在的文档上写状态。
                #
                # ⚠️ **回绝之前先把接管行交还**（2026-09-30 加）。这是一处**一改就能省掉
                # 半小时误锁**的地方 —— 起因：`_prepare_step` 的几个回绝点从前都是
                # 「只回绝、不交还」，于是这张卡留在 `managed`、文档被锁住
                # （不能重新解析 / 删除 / 整库重置），要等看门狗那条
                # `TC_NO_DISPATCH_SEC`（30 分钟）才自动解开；而任务中心那边**当场**就
                # 把这一步判失败了。用户看到的是「任务失败、文档还在处理中」且什么都做不了。
                #
                # 文档都已经不存在了，这张卡是**纯垃圾** —— 交还它是无损的。
                # 仍然带 `only_if_idle=True`：万一另有一次派活正用着这张卡（`execution_id`
                # 非空），那一次自己会收口，不该由这里拆掉。
                await tc_repo.mark_released(
                    row.document_id,
                    error_message="知识文档不存在或已删除",
                    only_if_idle=True,
                )
                return ApiResponse.fail(
                    translate(
                        "err.doc.not_found",
                        fallback=f"知识文档不存在或已删除: {row.document_id}",
                    )
                )

            from .kb_type_service import get_kb_type

            kb_type = await get_kb_type(tenant_id, row.knowledge_base_id)
            if kb_type not in SUPPORTED_KB_TYPES:
                # 另两类库不从这条链走。走到这里说明接管行与知识库对不上，
                # 继续干会按这条链的模式去解析一个本该走 parse_only 的库。
                #
                # ⚠️ 同样**先交还再回绝**（理由见上面 `doc is None` 那段）：这个库类型
                # 这条链永远接不了，卡留着只会把文档锁到 30 分钟兜底。
                await tc_repo.mark_released(
                    row.document_id,
                    error_message=f"知识库类型 {kb_type} 不参与任务中心的文件处理编排",
                    only_if_idle=True,
                )
                return ApiResponse.fail(
                    translate(
                        "err.kb.task_center.kb_type_mismatch",
                        params={
                            "kb_type": kb_type,
                            "expected": " / ".join(SUPPORTED_KB_TYPES),
                        },
                        fallback=(
                            f"该知识库类型为 {kb_type}，不参与任务中心的文件处理编排"
                            f"（现只服务 {' / '.join(SUPPORTED_KB_TYPES)}）"
                        ),
                    )
                )

            # ③ 记「本步开始」。**必须在调 atomic-rag 之前** —— 看门狗以
            #    `step_started_at` 计时，先发下游再写会让计时起点晚于事实。
            started = await tc_repo.begin_step(
                row.document_id,
                tenant_id=tenant_id,
                task_id=execution.task_id,
                step_code=step_code,
                execution_id=execution.execution_id,
            )
            if not started:
                # 从上面读到写之间接管行变了（被删任务 / 被看门狗交还）—— 这次派活
                # 已经没有落点，拒绝比「先干了再说」安全。
                #
                # ⚠️「任务重试」那条路（上面刚武装过）走到这里**理论上不可达**：
                # 那次武装与这次 CAS 在同一笔事务里、条件也重合。真到了这里，
                # 行留在 `managed`、而**文档行一个字都还没动** —— 由看门狗的
                # `TC_NO_DISPATCH_SEC`（30 分钟）兜底交还，不会留下坏数据。
                return ApiResponse.fail(
                    translate(
                        "err.kb.task_center.takeover_closed",
                        params={"task_id": execution.task_id, "state": "<changed>"},
                        fallback=f"任务 {execution.task_id} 的接管记录已变更，不再受理这一步",
                    )
                )

            # ③' 【任务重试】到这里**才**动文档行 —— 与上面那句的顺序是刻意的：
            #     所有闸门都过了、这一步真的受理了，才把文档按「重新解析」重置。
            #     排在任何一条回绝路径之前的话，那份文档会停在
            #     「接管行在途 + 自己 `parsing` + 任务中心那边这一步失败」的假状态，
            #     要等满看门狗的 30 分钟兜底才解开。
            if retrying:
                await self._reset_document_after_console_retry(
                    session,
                    doc=doc,
                    step_code=step_code,
                    kb_type=kb_type,
                    task_id=row.task_id,
                    document_id=row.document_id,
                )

            # ④ 把这一步要用的东西全部拍成快照：事务一关，ORM 对象就 detached 了。
            return _StepSnapshot(
                tenant_id=tenant_id,
                execution_id=execution.execution_id,
                document_id=row.document_id,
                knowledge_base_id=row.knowledge_base_id,
                file_path=doc.file_path or "",
                file_name=doc.file_name or "",
                storage_backend=doc.storage_backend or "local",
                storage_key=doc.storage_key or doc.file_path or "",
                # 图写 fencing 用（P1-E）：上传时由编译出的 schema 落在这列上。
                schema_version=int(getattr(doc, "ontology_target_schema_version", 0) or 0),
                # 这份文档走的是哪条编排（= 第一步交给谁干）。读的是文档行上的**粘性值**
                # —— 上传时与接管行**同一次提交**写下的（见
                # `document_service._upload_via_task_center`）。用粘性值而不是读 env：
                # 开关翻转后在途文档必须继续按它当初提交的那条路走完，这与查询侧
                # 「不读 env、只读行」是同一条纪律。
                backend=ingest_backend.backend_for_document(doc),
                # 这份文档属于哪类库（`get_kb_type` 那一查的结果，与闸门同源）。
                # **下游分派靠它**：同一个 `parse` / `compile` 在 lightrag 与两类 wiki
                # 库上是三件各不相同的事（发什么档位、走哪个编译入口），而 `backend`
                # 胜任不了这个判据 —— 两类 wiki 库的粘性值也是 `rag`，与 lightrag 一样。
                kb_type=kb_type,
                # 这次是哪种操作（`upload` / `reparse`）。与上一项同源、同样是文档行上的
                # **粘性值**；⚠️ **分流只准看它、不准看 `step_code`** —— 同一个 `parse`
                # 在上传时只解析、在重新解析时是「解析 + 推切片 + 替换旧的」
                # （`task_intent` 模块 docstring 的那张表）。
                intent=task_intent.intent_for_document(doc),
                # 重新解析那一步要用的两件（`reparse_strict` 的入参，与老路径
                # `reparse_document` 逐参数对齐）：
                #   ① 代次 —— 引擎按它 fencing 掉在途的旧结果，是「整份替换」的凭据；
                #   ② 旧切片清单 —— 替换时要删的那一半（另一半由引擎现查）。
                # 两件都在**这里**读（派活那一刻），不是提交那一刻 —— 中间没有别的写点
                # 动过这两列（`reparse_document` 只写代次、不清 `rag_doc_ids`）。
                content_generation=int(getattr(doc, "content_generation", 0) or 0),
                old_rag_doc_ids=list(doc.rag_doc_ids or []),
            )

    # ========================================================
    # 下游调用：三个步骤各发各的
    # ========================================================

    async def _submit_downstream(self, step_code: str, snapshot: "_StepSnapshot") -> Optional[str]:
        """提交下游任务；Jonex Wiki 持久化合批请求后返回 None，其余返回 task_id。

        分派是**三维**的：哪一步 × 哪类库（`snapshot.kb_type`）× 这次是哪种操作
        （`snapshot.intent`）。

        - **`kb_type` 必须排在最前面**：两类 wiki 库（`openkb` / `jonex_wiki`）的两步与
          lightrag 是两件完全不同的事 —— 它们的 `parse` 发 `parse_only`（只落解析产物、
          不进 LightRAG）、`compile` 是 **Wiki 编译**（不抽本体、不写 Neo4j）。
        - `compile`：lightrag 走它（交 atomic-rag 抽本体，「重新编译」重跑
          `compile_1` 会重新进这里、**语义零分叉**）；两类 wiki 库各走自己的 Wiki 编译
          （入口与巡检各一个，见 `_submit_wiki_compile` / `_submit_jonex_wiki_compile`）。
        - 差别全在第一步：`rag` 链的 `parse` 是 atomic-rag 的 `full`（解析 + 推切片一趟）。
        - 第三个维度只落在 lightrag 上：`intent == reparse` 时同一个 `parse` 步要发的是
          `reparse_strict`（解析 + 推切片 + 替换旧的）。**两类 wiki 库都没有这个分叉** ——
          它们不推切片，「重新解析」与上传发的是同一个 `parse_only`。
        """
        if step_code == STEP_COMPILE:
            if snapshot.kb_type == _KB_TYPE_OPENKB:
                return await self._submit_wiki_compile(snapshot)
            if snapshot.kb_type == _KB_TYPE_JONEX_WIKI:
                return await self._submit_jonex_wiki_compile(snapshot)
            return await self._submit_ontology_only(snapshot)
        if snapshot.kb_type in (_KB_TYPE_OPENKB, _KB_TYPE_JONEX_WIKI):
            # 两类 wiki 库都只有两步链、且两步都不吃 `tc_intent`（它们没有严格重解析
            # 那个档）。
            # [P3b 复审 fix] 显式门 `step_code == STEP_PARSE`：`compile` 已在本块上游
            # 提前返回，而 `ingest` 只存在于改版前遗留任务（`_INSERT_MODES` 里仅
            # lightrag 有该档）。将来若有人给 wiki 编排加 ingest 节点，落回
            # `_submit_insert` 会因取不到执行模式**显式抛错**；缺了这道门，那个步会
            # 再投一次引擎 insert（重复解析 / 重复扣配额），且完全不报错。
            return await self._submit_insert(step_code, snapshot)
        if step_code == STEP_PARSE and snapshot.intent == task_intent.INTENT_REPARSE:
            return await self._submit_reparse(snapshot)
        return await self._submit_insert(step_code, snapshot)

    async def _submit_insert(self, step_code: str, snapshot: "_StepSnapshot") -> str:
        """`insert` 那条路上的各步（现役只有 `parse`），档位见 `_INSERT_MODES` / `insert_mode_for`。

        **lightrag 的 `parse` = `full`**（解析 + 多模态 + 推切片一趟干完，与接入任务中心
        之前的上传同档位）；**两类 wiki 库的 `parse` = `parse_only`**（只解析出产物）。
        `kb_type` 也随快照下发 —— 引擎按它决定产物形状（openkb → 重组过的 `content.md`；
        jonex_wiki → MinerU 结构化 `content_list.json`）。

        **`ingest` 不重复解析 parser preset**：`push_only` 读的是第一步落在共享卷上的
        产物（只有改版前遗留的在途任务会走到这里），preset 在那一步已经用过了；这里再
        去解析一次，万一那个 preset 期间被停用，只会把一个本来能跑完的第二步拒掉。
        """
        preset: Optional[str] = None
        prompt_ids: list = []
        if step_code == STEP_PARSE:
            # 复用上传路径的同一份解析逻辑（`_compute_config_fingerprint` 也是这么
            # 直接调它这个静态方法的），不另抄一份「按文件类型选 preset」。
            from .document_service import DocumentService

            preset, prompt_config_id = await DocumentService._resolve_parser_preset(
                snapshot.tenant_id,
                snapshot.knowledge_base_id,
                snapshot.file_name,
            )
            prompt_ids = [prompt_config_id] if prompt_config_id else []

        # 幂等键取 `execution_id`：任务中心重发时 `execution_id` 不变，而我们对同一个
        # execution_id 只会走到这里一次（执行登记挡在前面）。所以这个键兜的是
        # 「登记写了、下游建了、回执丢了」这一类 —— 那次重发会被登记挡住、根本不进来。
        # 真正让重发安全的仍然是登记，幂等键是第二道。
        result = await get_rag_client().insert(
            file_path=snapshot.file_path,
            tenant_id=snapshot.tenant_id,
            knowledge_base_id=snapshot.knowledge_base_id,
            document_id=snapshot.document_id,
            ontology_schema=None,  # 解析不抽本体：schema 只在 compile 步用
            storage_backend=snapshot.storage_backend,
            storage_key=snapshot.storage_key,
            preset=preset,
            prompt_ids=prompt_ids,
            schema_version=snapshot.schema_version,
            execution_mode=insert_mode_for(step_code, snapshot.kb_type),
            idempotency_key=f"task-execution:{snapshot.execution_id}",
            kb_type=snapshot.kb_type,
        )
        return _require_task_id(result, step_code)

    async def _submit_reparse(self, snapshot: "_StepSnapshot") -> str:
        """「重新解析」那一步：把「严格重解析（整份替换）」发给 atomic-rag。

        **与老路径 `document_service.reparse_document` 的 `retry(...)` 逐参数对齐** ——
        这不是巧合，是要求：那条路是长期在跑的权威实现，「重新解析」换了个入口（改由
        任务中心派活）不该顺手把语义也换掉。两边不一致的症状是**用户按同一个按钮、
        得到两种结果**（旧的删不干净 → 重复向量 / 新的没推上去 → 搜不到），
        而那些症状要到查询时才浮出来。

        参数逐条对上，差异只有三处、都是刻意的：

        1. **幂等键取 `execution_id`**（`task-execution:{execution_id}`），老路径取
           `insert:{tenant}:{kb}:{doc}:{gen}`。防的不是同一件事：老路径防「同一份文档
           重复点按钮」，这里防「**同一次派活重发**」。而「重复点按钮」在任务中心这条
           路上天然不可能 —— 一次点击 = 一次「重跑 `parse_1`」，任务中心那边那一步
           没跑完之前根本进不来第二次。真正的 fencing 仍然是 `content_generation`。
        2. **preset 在这里现算**（老路径在按钮那一刻算好、顺手传下来）。必须算：这一步
           是派活时重建的，中间可能隔了几十分钟（看门狗补调 compile 那一段），拿按钮
           那一刻的快照会让一次解析用上过期的解析器配置。
        3. **不带 `ontology_schema`**（老路径带）。抽本体已经归「知识编译」那一步 ——
           它自己取 schema、自己抽。这一步再带上，引擎就会在**解析任务里再抽一遍**
           （`ONTOLOGY_EXTRACT_ENABLED` 打开时），而那份产物**没人接**（这份文档被既有
           对账排除在外，收尾的是看门狗，它不看引擎的本体产物）：白烧一轮 LLM，
           然后编译步再抽一遍。`schema_version` 仍然照传 —— 它是给引擎回传做版本
           fencing 的，与「抽不抽」无关。

        ── 后端守卫 ──

        这条下游调用只对 `rag` 编排成立；落到这里的 `snapshot.backend` 不是 `rag`，
        说明有人手工改了文档行的粘性值 —— 拒绝比「按 atomic-rag 的 `reparse_strict`
        去重解析一份不该由它管的文档」安全（那会把旧切片删错、留下重复向量）。
        """
        if snapshot.backend != _BACKEND_RAG:
            raise RuntimeError(
                f"编排 {snapshot.backend} 没有「严格重解析」这条下游调用"
                f"（文档 {snapshot.document_id}）"
            )

        from .document_service import DocumentService

        # 与 `_submit_insert` 的 `parse` 分支同一处解析逻辑，不另抄一份。
        preset, prompt_config_id = await DocumentService._resolve_parser_preset(
            snapshot.tenant_id,
            snapshot.knowledge_base_id,
            snapshot.file_name,
        )
        prompt_ids = [prompt_config_id] if prompt_config_id else []

        # ⚠️ **这里不取编译 schema**（`ontology_schema=None`）：抽本体归「知识编译」那一步，
        # 它自己取、自己抽。这一步带上 schema 会让引擎在解析任务里**再抽一遍**
        # （`ONTOLOGY_EXTRACT_ENABLED` 打开时），而那份产物没人接 —— 白烧一轮 LLM。
        # `schema_version` 照传：它给引擎回传做版本 fencing，与「抽不抽」无关。
        result = await get_rag_client().retry(
            file_path=snapshot.file_path,
            tenant_id=snapshot.tenant_id,
            knowledge_base_id=snapshot.knowledge_base_id,
            document_id=snapshot.document_id,
            ontology_schema=None,
            storage_backend=snapshot.storage_backend,
            storage_key=snapshot.storage_key,
            preset=preset,
            prompt_ids=prompt_ids,
            # 严格重解析：解析 + 删旧切片 + 推新切片，一趟做完。
            execution_mode="reparse_strict",
            strict_push=True,
            # 「整份替换」的凭据：代次一变，在途的旧结果全被 fencing 掉。
            content_generation=snapshot.content_generation,
            schema_version=snapshot.schema_version,
            old_rag_doc_ids=snapshot.old_rag_doc_ids,
            idempotency_key=f"task-execution:{snapshot.execution_id}",
            kb_type=_KB_TYPE_LIGHTRAG,
        )
        return _require_task_id(result, STEP_PARSE)

    async def _submit_wiki_compile(self, snapshot: "_StepSnapshot") -> str:
        """`compile` 步（**openkb / Ontology Wiki**）：发起 Wiki 编译。

        与 lightrag 那一步（`_submit_ontology_only`，抽本体）**完全是两件事**：openkb
        没有本体，它的「编译」是把上一步落在共享卷上的 `content.md` 交给 OpenKB 编译成
        Wiki。入口是 `KnowledgeCompilerService.recompile_openkb` —— 与用户点「重新编译」
        按钮走的是**同一个方法**，差别只在**谁发起**（这里由任务中心派活驱动）。

        ── 为什么传 `from_task_chain=True` ──

        `recompile_openkb` 开头有一道「文档还在解析/入库中就不让编译」的忙校验，防的是
        **用户手点**时撞上 Miner3U 正在覆盖写 `content.md`。链上这一步不需要它，而且会被
        它**误伤**：看门狗是「先上报、后把文档置 READY」，任务中心消化完上报就派这一步，
        两者之间有一个很窄的窗 —— 那一刻文档行可能还停在 `parsing`，忙校验会把一个其实
        已经解析成功的活当场拒掉。链上这一步只可能在解析步**成功之后**才跑到（先后由
        编排的依赖保证），所以跳过它；`DELETING` 仍然拦（那个是真危险）。

        ── 为什么要回头读一次文档行 ──

        看门狗盯的是 `knowledge_documents.llm_wiki_task_id` **这一列**（编译终态由既有
        30 秒巡检 `patrol_openkb_compile` 按它轮询回写），不是某次调用的返回值 ——
        所以读完那一列才算权威。取不到就**当场拒**：没有任务号就没有「跑完没有」的
        观测点，看门狗会一直等到硬超时。
        """
        from .openkb_service import KnowledgeCompilerService

        await KnowledgeCompilerService().recompile_openkb(
            snapshot.tenant_id,
            snapshot.document_id,
            snapshot.knowledge_base_id,
            source_file_path=snapshot.file_path,
            title=snapshot.file_name,
            from_task_chain=True,
        )

        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                snapshot.document_id, snapshot.tenant_id
            )
        wiki_task_id = (getattr(doc, "llm_wiki_task_id", None) or "") if doc is not None else ""
        if not wiki_task_id:
            # 走不到：`recompile_openkb` 提交失败会抛、成功会写这一列。留这一道是因为
            # 静默放过的话，看门狗会拿着空 id 一直等到硬超时，而原因（提交其实没成）
            # 在那一头完全看不出来。
            raise RuntimeError(
                f"{STEP_COMPILE} 步骤提交后文档 {snapshot.document_id} 上仍无 llm_wiki_task_id"
            )
        return wiki_task_id

    async def _submit_jonex_wiki_compile(self, snapshot: "_StepSnapshot") -> None:
        """Accept one compile step without compiling before the upload window closes."""
        from .jonex_wiki_service import JonexWikiService

        await JonexWikiService().queue_task_compile(
            snapshot.tenant_id, snapshot.document_id, snapshot.knowledge_base_id,
            snapshot.execution_id,
        )

    async def _submit_ontology_only(
        self, snapshot: "_StepSnapshot",
    ) -> str:
        """`compile` 步：发起本体抽取（`ontology_only`）。

        **复用他们既有的 `OntologyService.retry_extract`**，语义一个字不改 —— 它自己会
        写 `ontology_status=EXTRACTING` + 新 `rag_task_id` + `++retry_count`，然后由
        **既有对账的 EXTRACTING 分支**轮询收尾（写 Neo4j → 置 READY）。也就是说
        compile 步不需要任何新的轮询机制，本服务只负责发起。

        传 `strict=True`：那两处「提交失败」的默认处置是「打条 warning、原样返回文档」
        （给用户点按钮那条交互路径用的），那我们就会把一个没发出去的事当成收到了 ——
        必须让失败当场抛出来，由 `_handle_step` 转成显式拒。

        ── 为什么回包里取不到、要回头再读一次文档 ──

        `retry_extract` 成功时返回的是 **`doc.to_dict()`**（文档快照），键是 `rag_task_id`
        不是 `task_id`；而那个 `doc` 是它在**自己的 session 里**取的对象，`set_status`
        写的是不是同一个实例、要不要靠 SQLAlchemy identity map 才看得见新值 —— 这种
        「取决于实现细节」的保证不能拿来当接口用。

        更要紧的是**语义**：真正会被收尾的是 `knowledge_documents.rag_task_id` 这一列
        （既有对账的 EXTRACTING 分支轮询的就是它，见 `reconciliation_service.py` 的
        `_retry_ontology_one`）。看门狗要盯的必须是**那一列的值**，而不是某次调用的
        返回值 —— 所以这里回头把那一列读出来，读完即权威。
        """
        from .ontology_service import OntologyService

        await OntologyService().retry_extract(
            snapshot.tenant_id,
            snapshot.document_id,
            snapshot.knowledge_base_id,
            strict=True,
        )

        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                snapshot.document_id, snapshot.tenant_id
            )
        rag_task_id = (doc.rag_task_id if doc is not None else None) or ""
        if not rag_task_id:
            # 走不到：`strict=True` 已经把「没拿到 task_id」变成异常了。留这一道是因为
            # 这里要是静默放过，看门狗会拿着空 id 一直等到硬超时，而原因（提交其实没成）
            # 在那一头完全看不出来。
            raise RuntimeError(
                f"{STEP_COMPILE} 步骤提交后文档 {snapshot.document_id} 上仍无 rag_task_id"
            )
        return rag_task_id

    # ========================================================
    # §4.1 ② 删除任务
    # ========================================================

    async def remove_task(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        """按 `task_id` 清掉本运行方侧的残留（§4.1 ②）。

        **本侧的残留是什么**：一张接管行。它记着「这份文档归任务中心管、走到哪一步」。

        ⚠️⚠️ **已知缺口（2026-10-14）：本方法与新的「归属」口径不自洽，任务删除开放前必须先改这里。**

        2026-10-14 起，「归任务中心管」= 接管行存在（`MANAGED_STATES`，**含 `released`**），
        旧对账据此**不许替它发起任何下游工作**。而本方法把行写成 `released` —— 于是任务明明
        没了，文档却仍算「归任务中心管」：旧对账不肯驱动它，**也没有任务能驱动它**，这份文档
        就此搁置（`READY` 的还能检索，`PARSING` 的会由 `reconcile_documents` 收尾到 FAILED）。
        当前任务删除**未对外开放**，所以这个组合暂时到不了；但它是个**真缺口**，不是"以后再说"。

        开放删除之前的正确改法（二选一，届时再定）：
          · **删掉这张接管行** —— 「有卡 = 归任务中心管、卡没了 = 交回旧对账」的不变式最干净；
          · 或**新增一个「彻底交回」态**（不在 `MANAGED_STATES` 里）—— 保留审计痕迹，
            但要新起一个 DDL 迁移改 `state` 列的 COMMENT，并把 `rearm_for_step` 的 CAS 与
            用户按钮的文案逐处审一遍。
        无论选哪个，**上面「三件必须做对的事」里第 2 条的落点都会跟着变**（见该条）。

        ── 三件必须做对的事 ──

        1. **幂等**：删两次、删不存在的、压根没接管过，**都要回成功**。任务中心对这条路
           **也重发**（`task.delete` 出站没够着时最多 3 次、`task_id` 不变），第二次进来
           看见的可能是「上一次其实已经删干净了、只是回执没回来」。回失败会把这显示成
           「删除失败」，而任务中心那边因为这一下失败**一个字段都不改**、用户还得再点一次。
        2. **不动文档状态**。撤销的是任务中心那侧的编排，不是这份文档 —— 文档留在当前
           状态，本来该由既有对账接手。把它置 FAILED 等于让一次**管理侧**的操作把好数据
           打成失败，而且原因与这份文档毫无关系。

           ⚠️ **但 2026-10-14 之后这一条只兑现了一半**：接手的是**收尾**那趟
           （`reconcile_documents` 轮询在途任务、收敛 `PARSING`），**发起**那趟
           （`patrol_parsing_timeout` 重投解析）**不会再碰它** —— 因为这些行是
           `released`、属于 `MANAGED_STATES`，旧对账不许替它发起。`PARSING` 且**没有**
           在途任务号的文档因此会收敛到 FAILED（而不是被重投）。这正是上面那条缺口的
           表现形式，改法见上。
        3. **不取消 atomic-rag 的活**。`RAGClient` 根本没有取消任务的接口（只有
           `delete` / `delete_orphan_chunk` / `delete_batch`），所以在跑的那个任务会跑完 ——
           它的结果由**既有对账**接手：交还之前这里会把接管行上那个在途任务号抄到文档行上
           （`hand_over_in_flight_task_id`，与 `task_stopped` / `_abandon` 同一形状），
           对账据此按老规矩正常轮询这份解析（跑完 → READY、真失败 → FAILED），
           **不会**按「这份文档没有任务号」连判 3 拍把它判死、更不会跑过 SOFT 再插一份解析
           （那是「重复向量」的来路，见 `hand_over_in_flight_task_id` 的 docstring）。

        `tenant_id` 用于限定范围：拿到的接管行必须属于这个租户，跨租户的 `task_id`
        一次都不该被这次调用改到。
        """
        from ..models.task_center import TaskCenterDocState as _State

        async with get_db_session() as session:
            repo = TaskCenterDocumentRepository(session)
            row = await repo.read_by_task_id(task_id, require_tenant(tenant_id))
            if row is None:
                # 没接管过 / 上一次已经清干净了 → 成功 no-op（第 1 条）。
                return ApiResponse.ok()
            if row.state == _State.RELEASED.value:
                # 再写一次同样的值，保持幂等（第 1 条）。不提前 return，是因为
                # 「已 released 的行上还可能留着 error_message」不值得再写一次 ——
                # 但它已经是终态，直接收工更省一次 UPDATE。
                return ApiResponse.ok()
            # ⚠️ 交还前先把在途任务号交给文档行（与 `task_stopped` / `_abandon` 同一形状）：
            # 不交的话，对账会按「这份文档没有任务号」连判 3 拍把它判死（≈90 秒
            # 「RAG 任务确认丢失…请重新上传」），而那份解析其实还在跑、之后还会推切片 ——
            # 一份能用的文档被打成坏的，且 FAILED 不在 patrol 的扫描面里、再也不会被重试。
            await hand_over_in_flight_task_id(
                session,
                document_id=row.document_id,
                tenant_id=row.tenant_id,
                rag_task_id=row.rag_task_id,
                step_code=row.current_step,
            )
            await repo.mark_released(
                row.document_id, error_message="任务已被删除，接管交还给既有对账"
            )
        logger.info(
            "任务中心 remove_task: task=%s doc=%s 接管行已交还", task_id, row.document_id
        )
        return ApiResponse.ok()

    async def task_stopped(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        """管理方**通知**：这条任务被用户停了（§4.1 ③，2026-09-30 加）。

        **我们怎么回应**：把那张接管行交还出去 —— **那正是解开文档锁的那一步**。

        为什么非要有它：`task.stop` **只在任务中心内部办**（判死还没开始的步骤 + 把任务
        就地收成 `failed`）—— **2026-09-30 之前它不出站、也不通知运行方**。所以在那之前我们
        压根不知道这条任务已经
        终止 —— 看门狗继续盯着那份被接管的文档、继续等一个已经不会再有下一步派活的任务；
        而用户此时想**重新解析 / 删除文档 / 整库重置**，三个入口**全被拦**（它们都按
        「文档正在处理中」拒），要等那个还在跑的活自己结束（再等 30 分钟兜底）、
        或最长到 **6 小时硬超时**。用户看到的是「任务页说已停、文档页说还在处理中」。

        ── 与 `remove_task` 三条相同的纪律（理由逐条见那边）──

        1. **幂等**：任务不存在 / 已经交还过 / 压根没接管过，**都要回成功**。
           ⚠️ 管理方今天**单次发送、不重发**（尽力而为），但运维手工重放、或将来改成带
           重发，都靠这一条兜。
        2. **不动文档状态**：交还的是「任务中心那侧的编排」，不是这份文档 —— 文档留在当前
           状态，下一个 30 秒由既有对账接手（`PARSING` 由 `patrol_parsing_timeout` 兜住、
           `READY` 是稳定态）。置 FAILED 等于让一次**管理侧**的操作把好数据打成失败，
           而原因与这份文档毫无关系。
        3. **不取消 atomic-rag 的活**：`RAGClient` 根本没有取消任务的接口（只有
           `delete` / `delete_orphan_chunk` / `delete_batch`），所以在跑的那个会跑完 ——
           但结果落不到任何地方（接管行已 `released`、看门狗不再上报）。

        ⚠️ **交还**这一步**不带** `only_if_idle`（与 `remove_task` 同）：卡上多半正挂着
        一个在途执行（那正是在跑的那一步），而**就是要把它交还掉**、让文档解锁 ——
        与「运行方自己放弃一张没收下的卡」那种场景不是一回事（那种才需要那道闸）。

        ⚠️ **别把这条通知当成一定会到**：管理方发失败只记 warning（不让用户的「停止」
        失败），**收不到是可能的** —— 真到不了时，今天那条 30 分钟的看门狗兜底
        （`TC_NO_DISPATCH_SEC` 那个分支）仍然在。
        """
        from jonex_core.task_protocol.results import TerminalSubmitDisposition
        from jonex_core.task_runtime import (
            ExecutionContext,
            TerminalReport,
            get_task_runtime,
        )

        from ..models.task_center import TaskCenterDocState as _State

        # ── ① 只读定位（**短事务**）：只取「有没有在途、要上报给谁」这几个**标量** ──
        # ⚠️ 值必须在这一段里就抄出来：事务一关 ORM 行就 detached / expired，出了这个
        #    `async with` 再读 `row.space_id` 这类属性会炸（同 `_StepSnapshot` 的理由）。
        async with get_db_session() as session:
            row = await TaskCenterDocumentRepository(session).read_by_task_id(
                task_id, require_tenant(tenant_id)
            )
            if row is None:
                # 没接管过 / 上一次已经清干净了 → 成功 no-op（第 1 条）。
                return ApiResponse.ok()
            if row.state == _State.RELEASED.value:
                # 已是终态，直接收工（第 1 条）。
                return ApiResponse.ok()
            doc_id = row.document_id
            # `input_refs` 要的两个上下文事实（步骤在下面 carrier 里已经有了）
            kb_id = row.knowledge_base_id
            carrier = ExecutionContext(
                tenant_id=row.tenant_id,
                space_id=row.space_id,
                task_id=row.task_id,
                step_code=row.current_step or "",
                execution_id=row.execution_id or "",
            )

        # ── ② 上报（**不占事务、不拿锁**）——⚠️ 绝不能再包进上一段的 `async with` 里 ──
        # 这条纪律同文件 `_handle_step` 明写着：「不能让一笔事务跨越那个 HTTP 调用」。
        # 上报最长 3 次 × `TASK_CENTER_RUNNER_TIMEOUT`（默认 30 秒）≈ 91 秒，包在事务里
        # 会把数据库连接和事务一起吊住那么久（而且那段里 handler 可能正在写这一行）。
        #
        # **顺序不能反**：本模块与 `task_watch_service` 都写着同一条纪律（那边
        # `_finish_step` 有一整段注释）——**上报由看门狗发、不由干活的那一步发**，而
        # `mark_released` 会把这一行清成不在途、**从此再也进不了看门狗视野**。所以先交还
        # 的话，这一步的成败就**永远没人上报**：任务中心那侧它一直挂在 `running`，直到
        # 编排超时（预置 25200 秒 ≈ 7 小时）被超时巡检判死；这 7 小时里用户在文档页点
        # 「重新解析 / 重新编译」还会被拒（那一步没结束），理由看不出是停止造成的。
        #
        # ⚠️ **只报「有派活在途」的行**（`execution_id` 非空）：它等同于「任务中心那侧这一步
        # 还是 `running`」（`execution_id` 是派活抢占时写上的）。`execution_id` 为空的行是
        # 「两步之间、正等下一步派活」，任务中心那侧本来就是 `pending`、**已经被
        # `task.stop` 判死过了**，没什么可报的。
        #
        # ⚠️ 报了失败之后任务中心会走一遍 `_aggregate`；任务快照那时已经是终态
        # （`task.stop` 收的口），`_aggregate` 开头那道闸直接返回 —— 不会把「用户手动停止」
        # 这个原因改写掉。
        if carrier.execution_id:
            reason = "用户停止了这条任务，本步不再等待结果（运行方主动弃单）"
            # 失败也带输入引用（2026-10-16 用户点名「失败肯定还是要上报的」）：用户看这一步
            # 为什么停的时候，同样想知道它当时吃的是哪份东西。与看门狗那两处**共用同一份
            # 构造**（`build_input_refs`），不在这里另写一套分派规则。
            in_refs = build_input_refs(
                knowledge_base_id=kb_id,
                current_step=carrier.step_code,
                document_id=doc_id,
                document_name=await load_document_name(
                    require_tenant(tenant_id), doc_id
                ),
            )
            result = await get_task_runtime().report_terminal(
                carrier,
                TerminalReport.failed(
                    reason,
                    input_refs=in_refs,
                    # 代号 + 参数：这句原来是**硬编码中文**（库里那批就这么进去的），
                    # 带上代号之后管理方就能按读的人的语言渲染它。
                    error_code="err.kb.task_center.runner_abandoned",
                ),
            )
            # **没被持久受理也照样往下走**（与 `_finish_step` 的处置不同，理由不同）：
            # 那一步在任务中心已经是「任务已停止」的一部分，最坏退回原来的样子
            # （挂到超时巡检），而**文档必须解锁**才是这次通知的全部意义。
            if result.disposition is not TerminalSubmitDisposition.DURABLY_ACCEPTED:
                logger.warning(
                    "task_stopped 上报未被持久受理（不影响交还）: task=%s step=%s "
                    "execution=%s disposition=%s",
                    task_id, carrier.step_code, carrier.execution_id, result.disposition,
                )

        # ── ③ 一个写事务收尾：**重读这一行** → 把在途任务号交给文档行 → 交还 ──
        # ⚠️ **重读不是多余的**：② 那一段最长 91 秒，期间那个 handler 可能刚
        # `set_rag_task_id` 写下在途任务号（它正卡在「发下游」那一步）。拿 ① 那份旧快照去
        # `mark_released`，会把 handler 刚写的任务号**顺手清成 NULL** —— 文档于是
        # 「在途、却没有任务号」，对账 90 秒后按「任务确认丢失」把它判死（一份之后还会被
        # 推进切片的文档被打成坏的）。重读一行就消掉这个窗口。
        async with get_db_session() as session:
            repo = TaskCenterDocumentRepository(session)
            fresh = await repo.read_by_task_id(task_id, require_tenant(tenant_id))
            if fresh is None or fresh.state == _State.RELEASED.value:
                # ② 那一段里被别的路交还了（或行没了）→ 已经不用我们交，收工。
                return ApiResponse.ok()
            # 交任务号与交还**同一个事务、且顺序不能反**：`mark_released` 会清掉行上的
            # `rag_task_id`，先交还就再也抄不出来了（理由见 `hand_over_in_flight_task_id`）。
            await hand_over_in_flight_task_id(
                session,
                document_id=fresh.document_id,
                tenant_id=fresh.tenant_id,
                rag_task_id=fresh.rag_task_id,
                step_code=fresh.current_step,
            )
            await repo.mark_released(
                fresh.document_id,
                error_message="任务被用户停止，接管交还给既有对账",
                # ⚠️ **也得写失败报文**（2026-10-11 补）：用户点停止时，运行方是**真弃单**
                # （`runner_abandoned`），不是「这活不归我」。而这条通知是**单发、不重发、
                # 失败只记 warning** 的 —— 如果那次上报丢了，本该由管理方的对账兜住；
                # 不写报文的话对账只能问出 `unknown`，管理方按**配置错**处置、不塞收件箱，
                # 那一步就要挂到硬超时（还占着本目录共用的并发名额）。
                report={
                    "status": "failed",
                    "error_message": "任务被用户停止，接管交还给既有对账",
                    "error_code": "err.kb.task_center.runner_abandoned",
                },
            )
            doc_id = fresh.document_id
        logger.info(
            "任务中心 task_stopped: task=%s doc=%s 接管行已交还（任务被用户停止）",
            task_id,
            doc_id,
        )
        return ApiResponse.ok()

    async def query_step_status(
        self, envelope: ExecutionEnvelope
    ) -> Optional[StepStatusQuery]:
        """**管理方主动问**：这个子任务现在什么情况（§4.1 ④，2026-10-11 加）。

        ## 只读

        这是「看一眼」，不是「推一把」—— 整个方法**不写一个字**。想改状态的走它自己的
        收口路径（`mark_step_handled` / `mark_released`）。

        ## 靠接管行一行就能回答

        接管行（`task_center_documents`）本来就记着管理方要的全部东西：

        | 接管行 | 回答 |
        | --- | --- |
        | 在途 + `execution_id` **空** | `pending_dispatch`（收下了、还没等到派活） |
        | 在途 + `execution_id` **非空** | `running` |
        | 已交还 + `last_report` 里 `status='success'` | `succeeded` |
        | 已交还 + `last_report` 里 `status='failed'` | `failed` |
        | 没有接管行 / 已交还但**没有结论** | **`unknown`** |

        ⚠️ **最后一档不是「失败」**：`unknown` 的语义是「**这活不归我 / 我说不清**」——
        管理方对它有**独立处置**（记明确原因，不聚合进业务失败）。最典型的来源是
        「任务被删过」（`remove_task` 交还时**不表态**，`last_report` 留 NULL）。
        把它读成失败，等于把一次**管理侧的操作**算成**运行方的业务失败**。

        ⚠️ **`envelope.step_code` / `node_id` 这里不参与判断**：运行方手里**一份文档
        只有一条接管行**，它记的是「**最近**跑到哪一步」；管理方来问的必然是
        **在途或刚收口**的那一步，与接管行对得上。真要按 `node_id` 分别记账，
        得另开一张表 —— 那是另一种设计，本方法刻意不往那个方向走。
        """
        tenant_id = require_tenant(envelope.tenant_id)
        async with get_db_session() as session:
            row = await TaskCenterDocumentRepository(session).read_by_task_id(
                envelope.task_id, tenant_id,
            )
            if row is None:
                return StepStatusQuery(
                    status=STEP_STATUS_UNKNOWN,
                    detail=f"没有任务 {envelope.task_id} 的接管记录",
                )
            # 事务一关 ORM 对象就 detached 了 —— 用到的列**在这里全部取出来**。
            state = row.state
            execution_id = row.execution_id
            current_step = row.current_step or ""
            last_report = row.last_report
            handled_execution_id = row.handled_execution_id

        def _settled() -> Optional[StepStatusQuery]:
            """按**最后一次收口的那份上报报文**答终态（没有报文就说「说不清」）。

            ⚠️ **原样带回 `report`**：管理方拿它当一条上报消化，而「成功」那份里
            **带着产出**（`outputs` / `output_refs`）—— 没有产出，管理方写了「成功」
            也派不出下一步。
            """
            if isinstance(last_report, dict) and last_report.get("status") == "success":
                return StepStatusQuery(status=STEP_STATUS_SUCCEEDED, report=last_report)
            if isinstance(last_report, dict) and last_report.get("status") == "failed":
                return StepStatusQuery(
                    status=STEP_STATUS_FAILED, report=last_report,
                    detail=str(last_report.get("error_message") or "")[:500],
                )
            return None

        # ── ⚠️ **先看「管理方问的这一步是不是已经收口了」**（2026-10-11 加）──
        #
        # 非末步（`parse` / 遗留 `ingest`）收口时是 `release=False` —— 行**仍停在
        # `managed`**、`execution_id` 被清空。只按「在途」判会把它答成
        # `pending_dispatch`（「已受理，等派活」），而**这一步其实已经成功**了。
        #
        # 判据：`envelope.execution_id` 是**管理方派活时给的那个编号**，
        # `handled_execution_id` 是**运行方收口时记下的同一个编号** ——
        # 两者相等就说明「**你问的正是我已经收口的那一次**」，可以直接答终态。
        if envelope.execution_id and envelope.execution_id == handled_execution_id:
            settled = _settled()
            if settled is not None:
                return settled

        if state in ACTIVE_STATES:
            if execution_id:
                return StepStatusQuery(
                    status=STEP_STATUS_RUNNING, detail=f"正在跑 {current_step}".strip(),
                )
            return StepStatusQuery(
                status=STEP_STATUS_PENDING_DISPATCH, detail="已受理，等任务中心派活",
            )
        # 已交还 —— 看那份收口报文
        return _settled() or StepStatusQuery(
            status=STEP_STATUS_UNKNOWN,
            detail="接管记录已交还且没有收口报文（多半是任务被删过）",
        )


class _StepSnapshot:
    """`_prepare_step` 从 DB 里拍下来的、发下游要用的那几个值。

    存在的唯一理由：**事务一关 ORM 对象就 detached**，而「读 + 记账」与「发下游 + 回写」
    必须分在两段事务里（见 `_handle_step` 的注释）。所以中间横着一个普通的值对象，
    而不是让一段事务跨越一个 HTTP 调用。
    """

    __slots__ = (
        "tenant_id",
        "execution_id",
        "document_id",
        "knowledge_base_id",
        "file_path",
        "file_name",
        "storage_backend",
        "storage_key",
        "schema_version",
        "backend",
        "kb_type",
        "intent",
        "content_generation",
        "old_rag_doc_ids",
    )

    def __init__(
        self,
        *,
        tenant_id: str,
        execution_id: str,
        document_id: str,
        knowledge_base_id: str,
        file_path: str,
        file_name: str,
        storage_backend: str,
        storage_key: str,
        schema_version: int,
        backend: str,
        kb_type: str,
        intent: str = task_intent.INTENT_UPLOAD,
        content_generation: int = 0,
        old_rag_doc_ids: Optional[list] = None,
    ) -> None:
        # 前两个来自执行上下文（不是从文档行读的），其余来自接管行 + 文档行。
        # 全部在构造时定死 —— 快照就该是不可变的，不再有「事后补一个字段」这种口子。
        #
        # 后三项（`intent` / `content_generation` / `old_rag_doc_ids`）都给了默认值：
        # **只有「重新解析」那条路用得到它们**，上传那条路（`upload` / `prepare_only`）
        # 一格都不读。给默认值是让「上传那三步」的调用点不必凭空编三个值出来
        # —— 编出来的值（比如 `intent="reparse"`）恰好是会把一次上传变成全量替换的东西。
        self.tenant_id = tenant_id
        self.execution_id = execution_id
        self.document_id = document_id
        self.knowledge_base_id = knowledge_base_id
        self.file_path = file_path
        self.file_name = file_name
        self.storage_backend = storage_backend
        self.storage_key = storage_key
        self.schema_version = schema_version
        self.backend = backend
        self.kb_type = kb_type
        self.intent = intent
        self.content_generation = content_generation
        self.old_rag_doc_ids = list(old_rag_doc_ids or [])


def _require_task_id(result: Any, step_code: str) -> str:
    """从下游回包里取 `task_id`；取不到就抛（由调用方转成显式拒）。

    **取不到不能当成功**：没有 task_id 就没有「这一步跑完了没有」的观测点，
    看门狗会一直等到硬超时。当场拒绝能让用户马上看见，而不是等 6 小时。
    """
    task_id = (result or {}).get("task_id") if isinstance(result, dict) else None
    if not task_id:
        raise RuntimeError(f"{step_code} 步骤的下游未返回 task_id")
    return str(task_id)


# ============================================================
# §4.2 ② 提交：上传路径与看门狗补交**共用这一处**
# ============================================================
#
# 两个调用点（`services/document_service.py::_upload_via_task_center` 与
# `services/task_watch_service.py::_supervise_submission`）做的事完全一样：拿同一个
# `task_id` 提交同一条编排。分开写两份的话，「哪一档算成」这种判据迟早分叉 ——
# 而它正好是最容易判错的地方（`REUSED` 看着像失败，其实是成功）。


async def submit_ontology_task(
    *,
    tenant_id: str,
    space_id: str,
    task_id: str,
    document_id: str,
    knowledge_base_id: str,
    kb_type: str,
    backend: str,
    task_source: str,
    task_name: Optional[str] = None,
    operator_id: Optional[str] = None,
) -> SubmissionResult:
    """把一份接入任务中心的文档提交给任务中心（§4.2 ②）。

    **`task_id` 由调用方提前生成**（`str(uuid4())`）并先落到自己的行上，两个理由：

    1. 它是这条路**唯一的重试前提** —— `submit` 只在自带 `task_id` 时重试（服务端
       `init_task` 不做幂等去重，不带 id 重发就是新建第二条任务）；
    2. 重发撞上自己上一次建的那条会回 1006，SDK 判 `REUSED` —— **那是成功**，不是
       拒绝（详见 `SubmissionDisposition.REUSED`）。

    **租户显式传、不从上下文回落**：两个调用点里，看门狗那一个跑在后台循环那一帧，
    根本没有 `TenantContext`，回落会收出空租户 —— 而 `TenantIsolationError`（3005）
    属「明确拒绝」，重发多少次都是同一句话。

    `inputs` 只放两件东西：`document_id` 与 `knowledge_base_id`。任务的每一步都由
    任务中心按上一步的 `outputs` 往下传（`bind_inputs`），所以这里给的是**链路起点**
    需要的值，不是每一步的入参。

    `kb_type` **必须由调用方显式传**（`lightrag` / `openkb` / `jonex_wiki`）：与
    `backend` 一起决定提交到**哪条编排**（`definition_for_kb_type`）。两类 wiki 库都只有
    一条链（wiki 链），`backend` 对它们无意义 —— 但仍然照传，因为粘性值本来就要落库。

    `backend` **必须由调用方显式传**（当前恒为 `rag`）：它决定提交到**哪条编排**
    （`definition_for_backend`）。**不要传「现读的配置值」** —— 传的是**这份文档当初
    提交时定下的那个**（粘性值）：看门狗补交与上传路径隔了几十分钟，中间库设置可能被
    改过；按改后的值重交，会让一份已经在任务中心链上跑了一半的文档**换一条编排重来**
    （任务中心那边是同一条任务、同一个 `task_id`，但步骤集合变了）。

    ── `task_name`（纯展示）──

    任务实例名，调用方传**文档名**（`document.file_name`）。不传 / 只给空白的话，
    任务中心那边会退回用**编排定义的名字**（`TaskSubmission.name` 的既定语义）——
    于是任务列表里一排「文件处理（Ontology）」，看不出哪条是哪份文档。

    ⚠️ **它只在任务诞生那一刻有意义**，别指望它能被后来的操作改掉：
    「重新解析 / 重新编译」走的是任务中心现成的「重跑某一步」，**任务行一条都不重建**、
    名字自然也不动。在这两个按钮上花心思改任务名是白费力气。

    ⚠️ **它不参与任何判定**（不进 `inputs`、不决定跑哪条编排、不决定跑哪一版）——
    所以调用方读不到文档名时传空即可，不要为了拼一个名字去多查一次库。

    ── `task_source`（**必传**，2026-10-13 加）──

    这条任务是哪个入口来的（如 `knowledge_base.file.manual_upload`）。**必须由调用方显式传，
    不要在这里从 env / 库里现推** —— 两个调用点（上传路径、看门狗补交）拿到的都是
    **文档行上那个粘性值**（`data_source_type`），经 `task_source_for_access_type`
    换算后传进来。在这条公共出口里现推的话，看门狗那一帧要额外查一次库，
    多出来的那次查询没有任何收益。
    """
    from jonex_core.task_runtime import get_task_runtime

    return await get_task_runtime().submit(
        TaskSubmission(
            definition_id=definition_for_kb_type(kb_type, backend),
            space_id=space_id,
            task_id=task_id,
            inputs={
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
            },
            # 任务来源（**任务中心那条路必传**）：协议层写死枚举，由调用方按
            # `data_source_type` 经 `task_source_for_access_type` 换算好传进来。
            # 看门狗补交时从文档行重算同一个值 —— 同一条映射，不会分叉。
            task_source=task_source,
            # 空白名交给任务中心回落成定义名（它的既定语义），这里不做「是不是空的」
            # 判断 —— 判据只能有一份，多一份就多一处会分叉的地方。
            name=task_name or None,
            operator_id=operator_id,
        ),
        tenant_id=tenant_id,
    )


def submission_accepted(result: SubmissionResult) -> bool:
    """这次提交算不算「任务已经在任务中心了」。

    `CREATED` 与 `REUSED` **都算成** —— 后者是「上一次其实已经建成了、只是响应在回来
    的路上丢了」，重发撞上的正是自己上次建的那条。判成失败会让调用方**换一个 id 重来**，
    于是同一条任务被建第二遍 —— 恰好是自带 `task_id` 要防的那件事。

    其余两档（`RETRYABLE_UNKNOWN` / `REJECTED`）都由调用方按「没成」处置：留痕、计数，
    下一轮（或看门狗下一拍）拿**同一个 `task_id`** 再来。
    """
    return result.disposition in (
        SubmissionDisposition.CREATED,
        SubmissionDisposition.REUSED,
    )


# ============================================================
# §4.2 ③ / ④ 重跑步骤：两个按钮的唯一出口
# ============================================================
#
# 「重新解析 / 重新编译」都走这里。**它们不是新任务**（用户定的）：用的是任务中心现成
# 的「重跑某一步」/「重跑某几步」—— 同一条任务、同一个 `task_id`，把那些步骤原地重置、
# 由它重新派活。任务中心那边一个字都不用改，我们只是去调它已有的口。
#
# ⚠️ **任务中心没给运行方开「整条任务重跑」的口**（SDK 里没有 `retry_task`），所以
# 「重跑一步 / 几步」是运行方唯一的重跑入口。
#
# ⚠️ **两者前提相同**（都是明确拒绝，不是静默失败）：任务已经跑完、要重跑的那一步也
# 已经结束、它的上游都成功。后两个正是「重跑 `compile_1`」必须在 `ingest_1` 成功之后
# 才发得出去的原因 —— 上游没成功时任务中心会回「上游还没成功」，我们据此显式报错。
#
# 批量版（④）多两条自己的校验：名单不能空/不能有重复、名单的顺序不能连成环 ——
# 都在任务中心那边判，这里只是原样转发（别排序、别去重）。


async def hand_over_in_flight_task_id(
    session: Any,
    *,
    document_id: str,
    tenant_id: str,
    rag_task_id: Optional[str],
    step_code: Optional[str] = None,
) -> None:
    """交还接管行**之前**，把在途的下游任务号抄到文档行上（2026-09-30 用户拍板，甲案）。

    ## 要治的链（已实测复现）

    运行方有两种「把接管行交还给既有对账」的时刻：用户点停止（`task_stopped`）与看门狗
    自己放弃（`_abandon`，比如「管理方那边这次编排已经结束」）。两处都只 `mark_released`
    —— 而这条链于是断在文档那一侧：

    - **任务中心链的文档行上没有 `rag_task_id`**：下游任务号只写在**接管行**上（上传时
      整段老路径不执行，那句 `set_status(rag_task_id=…)` 从来没跑过）；
    - 既有对账扫 `PARSING` / `INGESTING` 时带 `exclude_task_center_managed=True`，
      **唯一的保护就是「接管行还在途」**（`reconciliation_service.reconcile_documents`
      那段注释自己写明：排除就是为了防止 patrol 判超时后**重新 insert、产出重复向量**）；
    - 接管行一交还，这份文档立刻回到对账视野，而它**没有任务号** →
      `_verify_chain(entry="null_task_id")` → 连续 3 拍（≈90 秒）→
      「**RAG 任务确认丢失…请重新上传**」；跑过 SOFT（3600 秒）还会**再插一份解析**、
      与仍在跑的那份分叉 —— 重复向量。

    ## 抄过去之后

    对账按**老规矩**接着轮询这份解析：还在跑 → 跳过；跑完 → 照常收成 READY（切片确实
    推上去了，`rag_doc_ids` 也会补上）；真失败 → FAILED。**既不误判，也不会重复解析。**

    ## 三条边界

    - **`rag_task_id` 为空就什么都不做**：它非空才等于「这一步的活已经派给下游引擎了」。
      「两步之间、等下一步派活」那种形态不带任务号 —— 那时的文档要么没进 `PARSING`，
      要么由 `ontology_status` / `llm_wiki_compile_status` 单独跟，在文档行上乱写一个
      任务号只会让「重新解析」里那道「有没有在跑的任务」的检查问错对象。
    - **`compile` 步不交**（`step_code == "compile"` 直接返回）：那一步写进接管行的
      不是 atomic-rag 的解析任务号，而是 **openkb 的 Wiki 编译任务号**（`_submit_wiki_compile`），
      口径与文档列的 `rag_task_id` 不符；而编译步跑的时候文档已经是 `READY`，
      **不在既有对账的扫描面里**（它只扫 `PARSING` / `INGESTING`），压根不需要这个号。
    - **只写 `rag_task_id` 一列、不动文档状态**（走
      `KnowledgeDocumentRepository.set_rag_task_id`）：状态归既有对账与用户操作，这次交出去的
      只是「这份内容现在归谁在产」这个事实；而用 `set_status(doc, doc.status, …)` 会把
      刚才读到的状态**回写**一遍，白白多一个丢更新的窗口。

    ⚠️ **必须与 `mark_released` 在同一个会话（同一个事务）里、且排在它前面**：
    `mark_released` 会把接管行上的 `rag_task_id` 清掉，先交还就再也抄不出来了；
    而两次独立提交中间崩一下，就会留下「行已交还、文档行没有任务号」那格 —— 正是本函数
    要治的东西。
    """
    if not rag_task_id or step_code == STEP_COMPILE:
        # 没有在途任务号（「两步之间」）→ 没什么可交的；
        # `compile` 步手上的那个号是 **Wiki 编译任务号**、不是解析任务号（见 docstring），
        # 而且那时文档已 `READY`、不在对账扫描面里 → 不交。
        return
    # 只写这一列：文档行被别的流程同时改状态时，不会拿旧状态盖回去（见仓储那处的说明）。
    # 文档已被删 / 查不到时这条 UPDATE 命中 0 行，什么都不做 —— `mark_released` 该做还做。
    await KnowledgeDocumentRepository(session).set_rag_task_id(
        document_id, tenant_id, rag_task_id
    )
    logger.info(
        "接管行交还前把在途任务号交给文档行 doc=%s rag_task=%s（此后由既有对账接着轮询）",
        document_id,
        rag_task_id,
    )


async def rearm_for_rerun(
    session: Any,
    *,
    tenant_id: str,
    document_id: str,
    step_code: str,
) -> Dict[str, str]:
    """两个按钮共用的第一段：**校验有没有可重跑的任务，并原子地把接管行武装好**。

    返回 `{"task_id": ..., "space_id": ...}`（调用方拿它去调 `retry_subtask_for_step` /
    `retry_subtasks_for_steps`）。**必须在调用方自己的事务里调**（`session` 是它传进来的
    那一个）—— 武装接管行要与「文档行上那些粘性值的写入」同一次提交，分两次的结果见
    `document_service.reparse_document` 里那段 ⚠️。

    ── 两种失败，都是**明确报错**、不是静默兜底 ──

    1. **没有接管行** → `err.kb.task_center.no_task`。成因只有两种：这份文档是在接入
       任务中心**之前**上传的（那时候还没有接管行），或者它的任务被删过（`remove_task`
       会把行留在 `released`、而不是删掉，所以严格说只剩第一种会走到这里）。
       ⚠️ **绝不「顺手新建一条任务」**：用户定的口径是「重新解析 / 重新编译只是重来一次、
       不是新起一个任务」，偷偷建一条等于把一次「重来」变成一份**新的**运行记录，
       配额、并发名额、任务列表都会多出一条没人预期的东西。
    2. **武装不上**（`rearm_for_step` 返回 `False`）→ `err.kb.task_center.rearm_conflict`。
       那个写口的条件是 `state='released'`，所以落空只可能是「从读到写之间行变了」：
       文档正好被接管（另一次上传 / 任务中心刚开始派活），或行已经不指向这条任务。
       ⚠️ 这里**不能再重试**：调用方读到的事实已经过期，重试会拿着旧前提去改当前的行。

    两个错误都用 `ResourceConflictError`（1003）—— 与「文档正在由任务中心处理」那道
    用户拦截**同一个码**：对用户来说这三件事是同一类（「现在这份文档不能这么操作」），
    分三个码只会让前端多三处分支，而处置动作完全一样（等一等 / 重新上传）。
    """
    repo = TaskCenterDocumentRepository(session)
    row = await repo.read(document_id, tenant_id)
    if row is None:
        raise ResourceConflictError(
            message=translate(
                "err.kb.task_center.no_task",
                fallback=(
                    "这份文档没有任务中心任务，无法重跑它的步骤；"
                    "请重新上传这份文档后再试"
                ),
            ),
            details={"document_id": document_id},
        )
    armed = await repo.rearm_for_step(
        document_id,
        tenant_id=tenant_id,
        task_id=row.task_id,
        step_code=step_code,
    )
    if not armed:
        raise ResourceConflictError(
            message=translate(
                "err.kb.task_center.rearm_conflict",
                fallback="这份文档的任务刚刚被别处改动，请稍后再试",
            ),
            details={"document_id": document_id, "state": row.state},
        )
    return {"task_id": row.task_id, "space_id": row.space_id}


async def retry_subtask_for_step(
    *,
    tenant_id: str,
    space_id: str,
    task_id: str,
    step_code: str,
    operator_id: Optional[str] = None,
) -> SubtaskRetryResult:
    """要求任务中心**重跑这一条任务里的某一步**（按 `node_id` 寻址）。

    `step_code` 由调用方给（`parse` / `compile`），节点名在这里翻
    （`node_id_for_step`）—— 调用方不该知道 `parse_1` 这种编排内部的名字。

    **没被受理会在 SDK 里自动重发**（3 次 / 退避 0.5s、1s），所以这里的返回是
    「裁决」，不是「我发过」。`reused`/`retried` 都算成的判据收口在 `retry_accepted`。

    `tenant_id` 与 `space_id` 都**显式传**：一个调用点在用户请求里（两个按钮）、
    另一个在看门狗那一帧（**那里没有 `TenantContext`**）—— 回落会收出空租户，
    而 `TenantIsolationError`（3005）是「明确拒绝」，重发多少次都是同一句话。
    """
    from jonex_core.task_runtime import get_task_runtime

    return await get_task_runtime().retry_subtask(
        tenant_id,
        space_id,
        task_id,
        node_id_for_step(step_code),
        operator_id=operator_id,
    )


async def retry_subtasks_for_steps(
    *,
    tenant_id: str,
    space_id: str,
    task_id: str,
    step_codes: Sequence[str],
    operator_id: Optional[str] = None,
) -> SubtaskRetryResult:
    """要求任务中心**按给的顺序重跑这条任务里的某几步**（§4.2 ④）。

    `step_codes` 由调用方给（目前只有 `[parse, compile]` 一种用法），节点名在这里
    逐个翻（`node_id_for_step`）—— 调用方不该知道 `parse_1` 这种编排内部的名字。

    ── **顺序就是执行顺序，别排序、别去重** ──

    批量口按名单顺序把「先后」写进任务中心的依赖里（第 i 步等第 i-1 步成功），所以
    `[parse, compile]` 的含义是「先重解析、等它成功了再重编译」，不是「两步一起跑」。
    一旦在这里 `.sort()` 或塞进 `set` 去重，**顺序就没了** —— 那会让 `compile_1` 跟
    正在重建切片的解析步抢跑（读到的还是旧切片，重编译出来的是过期的本体），而这是
    **静默**的：两边都成功、没有任何报错。所以本函数对入参**一个字都不动**，原样转发。

    ⚠️ 反过来，名单要是写反了（下游在前），任务中心会**明确拒绝**（那会连成环）——
    这正是「顺序有意义」的证明：它宁可报错也不猜。

    **没被受理会在 SDK 里自动重发**（3 次 / 退避 0.5s、1s），所以这里的返回是
    「裁决」，不是「我发过」。`reused`/`retried` 都算成的判据收口在 `retry_accepted`
    （批量复用同一套判定与同一套枚举，见 SDK `TaskRuntime.retry_subtasks`）。

    `tenant_id` 与 `space_id` 都**显式传**：调用点在用户请求里（「重新解析」按钮），
    那里虽然有 `TenantContext`，但显式传能与另一个按钮（`retry_subtask_for_step`）
    保持同一形状 —— 两处的回落口径不一致是最难查的一类差异。
    """
    from jonex_core.task_runtime import get_task_runtime

    return await get_task_runtime().retry_subtasks(
        tenant_id,
        space_id,
        task_id,
        [node_id_for_step(step_code) for step_code in step_codes],
        operator_id=operator_id,
    )


#: 任务中心那侧「某一步**跑完了**」的四档（`SubtaskStatus` 的结束态）。
#:
#: ⚠️ **不是** KB 侧的 `DocStatus`、也**不是**协议层的 `STEP_STATUS_*`：这是
#: `task.subtask_get` 回包里 `data.status` 的取值域（`SubtaskItemResponse`），
#: 与任务中心 `SUBTASK_TERMINAL_STATUSES` 指的是同一件事 —— 那边住能力层
#: （`capabilities/task_center/`），KB 侧不该反向依赖，所以在这里写一份。
#: 漂移的症状是「名单里多带 / 少带一步」，两种都**不报错**（少带了由依赖兜住、
#: 多带了整批被拒）—— 改任务中心那几档时这里要一起改。
#: ⚠️ **`stopped` 是 2026-10-15 跟任务中心一起加的**：被用户停掉的那一步与失败 / 超时
#: 同档 —— 它同样「跑完了、有明确结论」，重跑它就是「重新排队」。
#: 少了它，KB 侧读到的 `stopped` 会被当成「还在跑」，保守侧会**只交解析**，
#: 于是「用户停过的那一步」永远重跑不到编译那一段。
SUBTASK_TERMINAL_STATUSES = frozenset({"success", "failed", "timeout", "stopped"})

#: 任务中心那侧「整条任务**已经结束**」的三档（`TaskStatus` 的终态）。
#:
#: 与 `SUBTASK_TERMINAL_STATUSES` 同一类拷贝：定义在
#: `capabilities/task_center/models/enums.py` 的 `TaskStatus`，KB 侧不该反向依赖能力层，
#: 所以在这里写一份。`stopped` 必须在内 —— 用户停任务时，正在跑的子任务还会停在
#: `running`，**只有任务这一问看得出已经结束**；少了它，看门狗会一直等到 6 小时硬超时。
#: 机器闸见 `tests/unit/test_reparse_step_codes_constants.py`。
TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped"})

#: 上面那个取值域里「**正在跑**」那一档 —— 唯一一档不能按保守侧处置的
#: （保守侧 = 只交解析；对 `running` 那等同于把两轮叠在同一个节点上）。见
#: `reparse_step_codes`。
SUBTASK_RUNNING = "running"


def _subtask_status_of(resp: Any) -> str:
    """从 `task.subtask_get` 的回包里取那一步的状态；**读不到一律回空串**。

    三种读不到长同一个样：壳说 `success=False`、`data` 不是 dict、`data` 里没有
    `status`。生产代码对它们的处置是同一档（保守），所以在这里收口成同一个信号 ——
    调用方只判一次「有没有拿到状态」。
    """
    if not getattr(resp, "success", False):
        return ""
    data = getattr(resp, "data", None)
    if not isinstance(data, dict):
        return ""
    return str(data.get("status") or "")


async def reparse_step_codes(
    *, tenant_id: str, task_id: str, document_id: str,
) -> Tuple[str, ...]:
    """「重新解析」这次该把哪几步交给任务中心：**名单里只放已终态的节点**（§4.2 ④ 的调用侧）。

    返回的元组直接喂 `retry_subtasks_for_steps`（顺序就是执行顺序，别排序、别去重）。

    ── 为什么名单必须按「那一步现在跑到哪了」定 ──

    任务中心的重跑口有**前提三条**（单步与批量逐条对齐）：① 任务已终态；② **那一步也已
    终态**（`success` / `failed` / `timeout` —— `pending` / `running` 还没结束，没什么可
    重试的）；③ 这一步的上游全都 `success`。批量口是**对名单里每一行**套同一套 ——
    带上一个没跑完的节点，**整批被拒**。

    而「从未成功编译过」的文档（=「解析失败后重试」要覆盖的那一档）其
    `compile_1` **必然是 `pending`**（判据 ⟺ 无 `extract` 事件 ⟺ `ontology_status`
    从未离开 `PENDING` ⟺ 编译从未跑过）⇒ 名单里带上它，主用例「解析失败后重试」
    100% 不可用。真机原文（2026-10-14 复现）：
    `Node compile_1 is currently pending; only finished subtasks … can be retried`。

    ── 判据：`parse` 恒在名单里，`compile` 只在它**已终态**时进名单 ──

    · **已终态**（`success` / `failed` / `timeout`）⇒ 交 `(parse, compile)`：这一步
      **跑过**、手里是上一轮的产出，上游重跑出新产出之后它必须跟着刷新 —— 这就是
      「连带下游」原本的理由，它**只对跑过的下游成立**；
    · **`pending`** ⇒ 只交 `(parse,)`：那一步**从未开始**、没有旧产出可旧，解析成功之后
      任务中心会**按依赖**把它自然派起来（真机实测：只交 `parse_1`，`compile_1` 随后
      `attempt=1` 一次成功、文档转 `ready`）；
    · **`running`** ⇒ **响亮报错、一步都不交**（`ResourceConflictError`）：与 `pending`
      的区别是实打实的 —— 它**正拿着上一轮的输入在干活**（用户把任务停了也一样：
      `task.stop` 不打断正在跑的那一步，那条任务已是终态、这一步仍是 `running`）。
      这时只交解析会被任务中心**受理**（任务终态 + 解析终态两条都过），两轮解析 / 编译
      就叠在同一个节点上；
    · **读不到**（`get_subtask` 抛了 / 回了失败壳 / `data` 里没有 `status`）⇒ 保守地按
      `pending` 那一档处置（**只交 `parse`**）+ warning。两侧的代价不对称：发多了
      **必被拒**（主用例当场不可用，而这是一次失败里最该能自救的那个按钮）；发少了最坏
      只是**少刷新一次下游** —— 本体可能还停在上一轮，但那是**可恢复**的（「重新编译」
      那个单步口还能补），而且它会在 `ontology_status` 上显出来、不会伪装成成功。
      所以保守侧就是「可用」侧。
      ⚠️ 读的异常**不许放出去**：那只值一次「名单要不要带上编译」的判断，不该让整次
      「重新解析」失败。

    ⚠️ **`parse` 自己不按这条判据过滤**：它就是用户按下去的那一步，重跑它正是这次操作
    本身。它要是也没终态，任务中心会按同一套前提**明确拒绝**（与从前逐字相同的行为）——
    本地不去猜它，猜错的方向是「用户点了重新解析、我们却什么都不发」。

    ⚠️ `tenant_id` 显式传，且**只在调用方自己的事务里调**（这个读发生在「武装接管行」
    与「这次是重新解析」那些写入的同一次提交里，见 `document_service.reparse_document`）：
    读回来是 `running` 时那个异常会把整笔写入一起回滚 —— 拒绝得干净，没有半成品窗口。

    ⚠️ `document_id` **不参与判据，只为日志在场**（2026-10-14 上一轮审查 Minor 1 收口）：
    `tenant_id` / `task_id` 都进不了「这是哪份文档」这一问（`task_id` 只能反查），
    而下面三条日志正是排查「名单交了几步 / 为什么少交一步」的唯一入口 —— 换目录、
    翻日志的人不该再去做一次关联查询。
    """
    from jonex_core.task_runtime import get_task_runtime

    node_id = node_id_for_step(STEP_COMPILE)
    try:
        resp = await get_task_runtime().get_subtask(tenant_id, task_id, node_id)
    except Exception as exc:  # noqa: BLE001 —— 读不到不改「能不能重跑」，见 docstring
        # 能抛到这里的不是传送层失败（那种回 `success=false` 的壳），而是「我们自己
        # 写错了」（运行时没装配……）。留 warning 而不是 debug：默认 `LOG_LEVEL=INFO`，
        # debug 等于没记 —— 那会让这个判断**静默**退化成「永远只交解析」。
        logger.warning(
            "重新解析：读不到 %s 现在的状态，按保守侧只交解析步 doc=%s tenant=%s task=%s: %s",
            node_id, document_id, tenant_id, task_id, exc,
        )
        return (STEP_PARSE,)

    status = _subtask_status_of(resp)
    if status == SUBTASK_RUNNING:
        raise ResourceConflictError(
            message=translate(
                "err.kb.task_center.compile_step_running",
                params={"node_id": node_id},
                fallback=(
                    f"节点 {node_id} 正在运行，现在重跑解析会与它叠在同一个节点上；"
                    "请等这一步结束后再试"
                ),
            ),
            details={"task_id": task_id, "node_id": node_id, "status": status},
        )
    if status not in SUBTASK_TERMINAL_STATUSES:
        if status:
            # `pending`（正常：这一步从未跑过）与**未来新增的**非终态取值都走这里。
            logger.info(
                "重新解析：%s 还没到终态（status=%s），名单只交解析步 —— "
                "它跑成后任务中心会按依赖把下一步自然派起来 doc=%s tenant=%s task=%s",
                node_id, status, document_id, tenant_id, task_id,
            )
        else:
            logger.warning(
                "重新解析：%s 的状态读不到（回包里没有 status），按保守侧只交解析步 "
                "doc=%s tenant=%s task=%s",
                node_id, document_id, tenant_id, task_id,
            )
        return (STEP_PARSE,)
    return (STEP_PARSE, STEP_COMPILE)


def retry_accepted(result: SubtaskRetryResult) -> bool:
    """这次「重跑一步 / 某几步」算不算受理了。

    只有 `RETRIED` 一档算成 —— 但它的含义比字面宽：任务中心对这条路的处置是**幂等**的，
    「这一步已经回到队列上了（上一次其实成了、只是响应没回来）」也回 `RETRIED`。
    这正是 SDK 敢在这条路上自动重发的前提（`retry_subtask` 的 docstring）。

    **单步与批量共用这一条**（两边的返回类型、枚举、判定完全同构）—— 判据只有一份，
    改一处就是改两处。

    `RETRYABLE_UNKNOWN` 与 `PERMANENT_REJECTED` 都按「没成」处置：留痕，下一拍拿同一个
    `node_id` 再来（看门狗那一侧），或者当场报给用户（两个按钮那一侧）。

    ⚠️ **`PERMANENT_REJECTED` 不等于「这一步没被重跑」**：响应丢掉之后那一步若已经重跑完
    并回到终态、而任务本身还没终态，重发会撞「任务不是终态」那道前提、回 1003。
    要真相就按 `task_id` 去查那一步的状态 —— 别为了消掉这个误报去放宽判据。
    """
    return result.disposition is SubtaskRetryDisposition.RETRIED


__all__ = [
    "NODE_COMPILE",
    "NODE_INGEST",
    "NODE_PARSE",
    "ONTOLOGY_DEFINITION_ID",
    "ONTOLOGY_JONEX_WIKI_DEFINITION_ID",
    "ONTOLOGY_WIKI_DEFINITION_ID",
    "STEP_COMPILE",
    "STEP_INGEST",
    "STEP_PARSE",
    "SUBTASK_RUNNING",
    "SUBTASK_TERMINAL_STATUSES",
    "TaskRunnerService",
    "SUPPORTED_KB_TYPES",
    "definition_for_backend",
    "definition_for_kb_type",
    "hand_over_in_flight_task_id",
    "insert_mode_for",
    "node_id_for_step",
    "rearm_for_rerun",
    "reparse_step_codes",
    "retry_accepted",
    "retry_subtask_for_step",
    "retry_subtasks_for_steps",
    "submission_accepted",
    "submit_ontology_task",
    "task_source_for_access_type",
]
