#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Task Center API routes（§4.4）。

网关是薄 REST 适配层：租户从当前 Authorization token 来、转发给 Sidecar；请求体从不
接 ``tenant_id``。

**任务中心自己不开 REST 端点**，所以这里全部经 Sidecar `/invoke` 走 action，落到
`TaskCenterCapability.execute()` 的 dispatch —— 跟 knowledge_base / business_domain
一个模式，只是 action 名字不同。

这一组是 §4.4「任务中心不提供面向浏览器/前端的网络接口」那条边界**开的口子**：
**四个只读**接口（任务列表 / 子任务列表 / **任务详情 / 子任务详情**）加**四个写接口** ——
停止任务、停止子任务、重试任务、重试子任务（三个写 2026-09-29 加；两个**详情读**
2026-10-13 加）。**删除接口仍然一概没有** —— 再加新接口之前，先回设计文档确认 §4.4 的边界。

**四个写接口都只改任务中心自己的库**：其中三个（停止子任务 / 重试任务 / 重试子任务）
**一律不出站**；`task.stop` 唯一例外 —— 它**收口完成之后**会尽力而为地给运行方发一条
`task_stopped` **通知**（§4.1 ③：单发、不重发、超时 5 秒、失败只记 warning）。但这条
通知**不改变「停止」一族的能力边界**：运行方侧正在跑的子任务**杀不掉**（通知不是命令，
它停不停由它自己决定），它会跑完并照常上报，各归各的（详见 `stop_task` 与
`stop_subtask` 的 docstring）。所以「停止」的成功回执表达的仍是「平台这边不再推进它了」，
不是「对方那边停了」。
"""

import asyncio
import os
import random
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, Query, Request

from api_gateway.deps import raise_from_capability_result, require_auth_header
from api_gateway.identity import identity_fields
from jonex_core.common import (
    CapabilityInvokeError,
    JonexException,
    get_config,
    get_logger,
    success_response,
    transmit_locale_header,
)
from jonex_core.common.i18n import translate
from jonex_core.common.monitor_collector import attempt_span
from jonex_core.common.tenant import extract_tenant_id

logger = get_logger("api_task_center")

router = APIRouter(dependencies=[Depends(require_auth_header)])


async def _call_tc_capability(
    request: Request, action: str, payload: dict[str, Any]
) -> Any:
    """把一次调用转成 Sidecar `/invoke`。

    返回能力的 `data`（**可能是数组**：子任务列表就回裸数组，§4.0 点名的「不设分页」
    接口不包 `{"items": [...]}`）。

    Sidecar 瞬态**连接**错误（拒绝连接 / DNS 解析不了 / 连接被重置）做指数退避 + 全抖动
    重试 —— 跟 `_call_kb_capability` 同一套参数（`GATEWAY_SIDECAR_*` 环境变量）。

    ⚠️ **超时一律不重发**，这是刻意的：底下两个 `except` 是 `TimeoutException` 在前、
    `TransportError` 在后，而 **`TimeoutException` 本身就是 `TransportError` 的子类**
    （实测父类链 `TimeoutException → TransportError → RequestError`）—— **这个先后顺序
    是有作用的，谁把两行调换，超时就会静默开始重发**。不重发的理由：超时意味着「请求
    可能已经送达、只是没等到回包」，而这一组里有四个**写**接口，重发可能让同一个动作做
    两遍（能力侧的幂等档能挡住大部分，但用户会看到一个「明明成功了却报错」的回执）。

    ⚠️ **已知不精确之处**：`ConnectTimeout` 也归在「不重发」这一支（它是 `TimeoutException`
    的子类，实测），而它表达的是「连都没连上」—— 本该跟上面的连接类错误一起重试。
    要收拾它就得把 `ConnectTimeout` 单独拆一支放在最前面，那是**改重发行为**，先记着。
    """
    config = get_config()
    tenant_id = extract_tenant_id(request)
    request_id = getattr(request.state, "request_id", "")

    sidecar_payload: dict[str, Any] = {
        "capability_id": "business.task_center.v1",
        "payload": {"action": action, "data": payload},
        "tenant_id": tenant_id,
    }
    # 调用者身份进 invoke 报文体（用户 JWT 到不了 Sidecar，只能网关自己解，详见
    # `api_gateway/identity.py`）。**八个路由一律带上**：读接口带了没坏处，而四个写接口
    # 都要它 —— 判权看它（`_ACTION_PERMISSIONS` 那张表），`task.stop` / `stop_subtask`
    # 还靠它落 `tasks.updated_by`。⚠️ 漏带的方向是「**悄悄放行**」不是报错
    # （`has_permission` 的口径是「没有 `user_id` 就放行」），加新路由时别忘。
    sidecar_payload.update(identity_fields(request))

    headers = {
        "X-API-Key": config.GATEWAY_API_KEY,
        "X-Request-ID": request_id,
        "X-Tenant-ID": tenant_id,
        "X-Forwarded-For": request.client.host if request.client else "",
    }
    transmit_locale_header(headers)

    max_retries = int(os.getenv("GATEWAY_SIDECAR_RETRIES", "2"))
    base_delay = float(os.getenv("GATEWAY_SIDECAR_BASE_DELAY", "1.0"))
    max_delay = float(os.getenv("GATEWAY_SIDECAR_MAX_DELAY", "8.0"))
    timeout = float(os.getenv("GATEWAY_SIDECAR_TIMEOUT", "180"))

    wait_before_ms: Optional[int] = None
    for attempt in range(max_retries + 1):
        # N2 物理 Attempt 埋点（监控旁路，采不到不影响主链路）。wait_before_ms 为
        # 「进入本次尝试前」等的那段 jitter 毫秒（首传 None）。
        with attempt_span(
            request_id=request_id,
            tenant_id=tenant_id,
            attempt_no=attempt + 1,
            # 先按「最后一次尝试」记；只有下面真会重试的 transport 分支才降级为 False
            # （成功 / 超时 / 其它异常都立即结束、不重试 ⇒ 都是本次逻辑调用的最后一次）。
            is_final=True,
            wait_before_ms=wait_before_ms,
        ) as att:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        f"{config.SIDECAR_URL}/invoke",
                        json=sidecar_payload,
                        headers=headers,
                    )
                result = response.json()
                if response.status_code != 200 or not result.get("success", False):
                    raise_from_capability_result(result)
                return result.get("data")
            except httpx.TimeoutException as e:
                att.fail(error_code=504, error_message=e)
                raise CapabilityInvokeError(
                    message=translate(
                        "err.task_center.console.timeout", fallback="任务中心能力调用超时"
                    ),
                    details={"action": action},
                )
            except httpx.TransportError as e:
                att.fail(error_code=502, error_message=e)
                if attempt < max_retries:
                    # 还会重试 ⇒ 本次不是最后一次尝试。is_final 无 setter，就地降级快照。
                    att.mark_final(False)
                    delay = min(base_delay * (2**attempt), max_delay)
                    jitter = random.uniform(0, delay)
                    wait_before_ms = int(jitter * 1000)
                    logger.warning(
                        "任务中心 Sidecar 调用 %s 瞬断 (attempt %s/%s)，%.1fs 后重试: %s",
                        action,
                        attempt + 1,
                        max_retries + 1,
                        jitter,
                        e,
                    )
                    await asyncio.sleep(jitter)
                else:
                    logger.error(
                        "任务中心 Sidecar 调用 %s 失败，已重试 %s 次: %s",
                        action,
                        max_retries,
                        e,
                    )
                    raise CapabilityInvokeError(
                        message=translate(
                            "err.task_center.console.unavailable",
                            fallback="任务中心能力不可用",
                        ),
                        details={"action": action},
                    )
            except JonexException:
                raise
            except Exception as exc:
                if hasattr(exc, "code") and hasattr(exc, "status_code"):
                    raise
                logger.error(
                    "Task Center capability call failed: action=%s error=%s", action, exc
                )
                raise CapabilityInvokeError(
                    message=translate(
                        "err.task_center.console.unavailable", fallback="任务中心能力不可用"
                    ),
                    details={"action": action},
                )


# 注意：字面量路径要声明在 `{param}` 路径之前（FastAPI 按注册顺序匹配）。
# 这两个深度不同、其实撞不上，但顺序摆对，以后加 `/tasks/stats` 之类才不会踩。


@router.get("/tasks", summary="任务分页列表")
async def list_tasks(
    request: Request,
    keyword: Optional[str] = Query(None, description="模糊匹配任务名或任务 id"),
    start_time: Optional[str] = Query(
        None, description="按建任务时间筛的起始（ISO 8601，含）"
    ),
    end_time: Optional[str] = Query(
        None, description="按建任务时间筛的结束（ISO 8601，含）"
    ),
    status: Optional[list[str]] = Query(
        None,
        description=(
            "任务状态多选，可重复传（?status=pending&status=running）："
            "pending/running/completed/failed；不传为全部"
        ),
    ),
    space_id: Optional[str] = Query(
        None,
        max_length=64,
        description="按任务落在哪个空间筛（精确匹配，字段名与任务行上的 space_id 同名）；不传为全部",
    ),
    task_source: Optional[str] = Query(
        None,
        max_length=64,
        description=(
            "按任务来源筛（∈ 协议层 TASK_SOURCES，如 knowledge_base.file.manual_upload；"
            "精确匹配）；不传为全部。⚠️ 域外值由能力侧当场拒（1001）。"
            "筛它永远筛不到平台侧自己触发的任务 —— 那些行这一列为空"
        ),
    ),
    page: int = Query(1, ge=1, description="页码"),
    # 不设上限：原先卡了 `le=100`，但这是照抄知识库那套路由抄来的，任务中心没有
    # 对应的理由 —— 后台循环跑的是 `TASK_CENTER_SCHEDULER_BATCH`（默认 10），跟这个
    # 分页 DTO 半点关系没有；页大小只该由调用方按自己一次能渲染多少行决定。
    page_size: int = Query(20, ge=1, description="每页条数"),
):
    """任务分页列表：返回任务本身 + 各状态数量。

    `status_counts` 里的数字**只受「除 status 外」的筛选条件影响**（§4.4）——
    前端切状态页签时它们不变，是给页签角标用的。
    """
    payload: dict[str, Any] = {"page": page, "page_size": page_size}
    if keyword:
        payload["keyword"] = keyword
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    if status:
        payload["status"] = status
    if space_id:
        payload["space_id"] = space_id
    if task_source:
        payload["task_source"] = task_source
    result = await _call_tc_capability(request, "task.console_page", payload)
    return success_response(data=result)


@router.get("/tasks/{task_id}", summary="任务详情")
async def get_task(request: Request, task_id: str):
    """任务详情：**返回体与任务列表里的一行完全一致**（§4.4）。

    没有 `<列表行>` 之外的东西 —— 要多给什么字段，改 `dtos/console.py` 的
    `TaskConsoleItem`，**两个口子一起给**（详情不是"另一份 DTO"）。
    """
    result = await _call_tc_capability(request, "task.console_detail", {"task_id": task_id})
    return success_response(data=result)


@router.get("/tasks/{task_id}/subtasks", summary="子任务列表")
async def list_subtasks(
    request: Request,
    task_id: str,
    attempt: Optional[int] = Query(None, ge=1, description="第几次运行；不传为最新一次"),
):
    """任务下的子任务列表。**不分页、回裸数组**（§4.0 点名的「不设分页」接口）。"""
    payload: dict[str, Any] = {"task_id": task_id}
    if attempt is not None:
        payload["attempt"] = attempt
    result = await _call_tc_capability(request, "task.console_subtask_list", payload)
    return success_response(data=result)


@router.get("/tasks/{task_id}/subtasks/{node_id}", summary="子任务详情")
async def get_subtask(request: Request, task_id: str, node_id: str):
    """子任务详情：**返回体与子任务列表里的一行完全一致**（§4.4）。

    ⚠️ 入参**只有 `task_id` + `node_id`**（2026-10-13 定）：页面手上就这两个。

    ⚠️ 按 **`node_id`（节点名，如 `parse_1`）** 寻址，**不是**子任务行的主键 `id`
    —— 与那两条子任务级写接口同口径。取的是**当前那次运行**里的那一行
    （`node_id` 只在同一次运行内唯一，跨运行会重复出现）。
    """
    result = await _call_tc_capability(
        request,
        "task.console_subtask_detail",
        {"task_id": task_id, "node_id": node_id},
    )
    return success_response(data=result)


@router.post("/tasks/{task_id}/stop", summary="停止任务")
async def stop_task(request: Request, task_id: str):
    """停止任务：把还没开始的子任务判死，任务就地收成 `failed`（原因「用户手动停止」）。

    **`POST` 而不是 `PATCH` / `PUT`**：这不是「改某个字段」，是一次动作，没有请求体。

    **一个写接口，做完就完，不幂等**（同 `task.delete` 的脾性）：重复点**不会**回
    「找不到」，而是回「已经结束了，停不了」（1003）—— 第一次已经把这个任务收成
    `failed` 了。前端按 `code` 判就行，不需要自己去重。

    **已经派出去、正在跑的子任务不受影响**（运行方没有「停止」这个动作 —— 这条链只在
    收口之后发一条通知，并不去掐那个正在跑的活）：
    接口成功之后，页面会是「任务已失败 + 某个子任务仍显示运行中」。它跑完照常上报，
    子任务按上报结果落最终状态，**任务层面已经是终态、不会被改回去** —— 派活是「锁外
    出站、回来再收尾」，收尾那一步带着状态闸门（只从 `pending` / `running` 出发），
    任务的终态翻不回来（见 `TaskRepository.mark_running`）。

    **可能被拒的两种情形**（都回 1003 冲突，`code` 见 §4.0）：

    - 任务已经是 `completed` / `failed` —— 已经结束了，停不了；
    - 任务在跑但**一个没开始的子任务都没有** —— 能停的只有正在跑的活，而它们杀不掉，
      这时收下停止等于什么都没停。

    成功时回**收口后的任务对象**（同 `task.get` 的形状），前端可以直接拿它刷新那一行。
    """
    payload: dict[str, Any] = {"task_id": task_id}
    result = await _call_tc_capability(request, "task.stop", payload)
    return success_response(data=result)


# ── 2026-09-29 新增的三个写接口 ───────────────────────────────
#
# 与 `task.stop` 同一族的「管理端写」：都要求 `task_center:write`
# （能力侧 `_ACTION_PERMISSIONS` 登记），都**回任务对象**（同 `task.stop` 的形状），
# 前端拿它直接刷新那一行。三个都**不幂等**：重复点会按当前状态拒掉（1003 / 1002），
# 前端按 `code` 判就行，不需要自己去重。
#
# `node_id` 是**节点名**（编排里 `config.steps[].node_id`，如 `parse_1` / `compile_1`），
# 不是 `step_code` —— 同一个 `step_code` 允许在编排里出现多次，唯一的是 `node_id`。


@router.post("/tasks/{task_id}/retry", summary="重试任务")
async def retry_task(request: Request, task_id: str):
    """整条任务**原地重跑**：同一条任务（`id` 不换）新建一次运行，从头跑一遍。

    **前提是任务已经结束**（`completed` / `failed`）—— 还在跑一律拒（1003）。重跑会
    新建一条 `task_attempts`、为这次运行生成一整套全新子任务，并把过去每一次运行的历史
    原样保留。**不是新建一条任务**：用户口径的「重来一次 ≠ 新起一个」（同上一次接入时
    定的那条，见 `CLAUDE.md` 任务中心段）。

    成功时回**重跑后的任务对象**（`status` 已回到 `pending`）。
    """
    payload: dict[str, Any] = {"task_id": task_id}
    result = await _call_tc_capability(request, "task.console_task_retry", payload)
    return success_response(data=result)


@router.post("/tasks/{task_id}/subtasks/{node_id}/stop", summary="停止子任务")
async def stop_subtask(request: Request, task_id: str, node_id: str):
    """停止某一步：把这个**还没开始**的子任务判死，并让任务按子任务现状收口。

    **只能停 `pending` 的**：`running` 与已终态都拒（1003）。这不是保守 ——
    **运行方没有「停止」这个动作**，已经派出去的活平台杀不掉，收下这次停止等于给用户一个
    「停了」的假象（与整条任务的那个「停止」完全同口径）。想重来请用「重试子任务」。

    判死之后**任务不一定跟着结束**：同一次运行里若还有别的分支在跑或等着跑，任务继续跑；
    都没有了才收敛成 `failed`（`error_message` 带「节点 xxx: 用户手动停止」）。
    返回的那份任务对象就是收口后的实况，前端直接拿它刷新。

    任务已结束（`completed` / `failed`）时拒（1003）—— 任务都收口了，单停一步无从谈起。
    """
    payload: dict[str, Any] = {"task_id": task_id, "node_id": node_id}
    result = await _call_tc_capability(request, "task.console_subtask_stop", payload)
    return success_response(data=result)


@router.post("/tasks/{task_id}/subtasks/{node_id}/retry", summary="重试子任务")
async def retry_subtask(request: Request, task_id: str, node_id: str):
    """重跑某一步：**同一条任务原地重来**，只把这一步重置，不新建任务。

    该步重置（`current_attempt` +1、清掉上次产出与报错），任务与当次运行被拉回「跑着」，
    之后后台循环重新扫到它并派活；这一行的历史留在 `subtask_attempts` 里。

    **前提三条**（任一不满足就拒）：任务已结束（`completed` / `failed`，否则 1003）、
    要重跑的那一步也已结束（`success` / `failed` / `timeout`，否则 1001）、
    **它的上游全部 `success`**（要拿上游产出当入参；否则 1001，`details.upstream` 列出
    哪些没成功 —— 这时要么先把上游重试成功，要么整条任务重跑）。

    ⚠️ **一次只能重跑一步**。要「解析 + 编译都重来」而这两步在编排里不是直连的
    （`compile_1` 依赖的是 `parse_1` 的产出、但两者之间有别的步骤时），**分两次调会出错**：
    先调前面那步会把任务拉回 `running`，紧接着那次会撞「任务还在跑」被拒；反过来先调后面
    那步，它会拿**旧产物**当场跑起来、静默出错。这种「必须一起重来且要保证先后」的场景，
    用内部的批量口 `task.subtask_retry_batch`（运行方 SDK 那条路，目前不对前端开放）。

    成功时回**重跑后的任务对象**（`status` 已回到 `pending` / `running`）。
    """
    payload: dict[str, Any] = {"task_id": task_id, "node_id": node_id}
    result = await _call_tc_capability(request, "task.console_subtask_retry", payload)
    return success_response(data=result)
