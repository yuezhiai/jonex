#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""把现有的 `TaskCenterClient`（§4.4）收窄成 SDK 要的 `ControlPlaneClient` 端口。

**为什么不直接用 `TaskCenterClient`**：它是个**双向**契约 —— 方法里有几个
（`execute` / `remove_task`）的方向是「管理方 → 运行方」，实现方是运行方自己，真被调到时
只回一个「方向不对」的失败壳。运行方拿这个对象去装配，`dispatch` 里很容易顺手写错
（把 `client.execute` 挂上去，或者反过来把 SDK 的 `runner.execute` 挂到 client 上）。
适配器把视野**物理收窄到运行方该调的那几个方法**：运行方从 SDK 拿到的控制面端口里，
压根没有 `execute` 这个属性，写错是 `AttributeError` 而不是一个静默的失败壳。

**这一层不做任何加工**：不改参数、不改返回值、不吞异常 —— 传送层的错误码映射
（超时 5004 / 连不上 5001 / 响应不合法 5003）已经由 `RemoteTaskCenterClient._call`
做完了，那是它该管的事。适配器只做「转发 + 收窄」。

`isinstance(adapter, ControlPlaneClient)` 为真 —— 端口是 `runtime_checkable` 的
结构契约，不要求显式继承。
"""

from typing import Any, Dict, List, Optional

from jonex_core.capability.business.task_center.client import (
    TaskCenterClient,
    get_task_center_client,
)
from jonex_core.capability.response import ApiResponse
from jonex_core.task_protocol.models import TaskItemResponse


class LegacyControlPlaneAdapter:
    """`TaskCenterClient` → `ControlPlaneClient`（只转发 `init_task` / `report_status` /
    `retry_subtask` / `retry_subtasks`）。

    `client=None` 就走 `get_task_center_client()`（能力清单决定 LOCAL / REMOTE / MOCK），
    所以绝大多数装配点连参数都不用传：`build_task_runtime()` 默认就是这条路。
    """

    def __init__(self, client: Optional[TaskCenterClient] = None) -> None:
        self._client = client if client is not None else get_task_center_client()

    @property
    def client(self) -> TaskCenterClient:
        """底下那个真 client —— 给需要它的地方用（比如按部署画像做自检）。"""
        return self._client

    async def init_task(
        self,
        tenant_id: str,
        definition_id: str,
        space_id: str,
        inputs: Optional[Dict[str, Any]] = None,
        *,
        version_no: Optional[int] = None,
        name: Optional[str] = None,
        operator_id: Optional[str] = None,
        task_id: Optional[str] = None,
        task_source: Optional[str] = None,
    ) -> ApiResponse[TaskItemResponse]:
        """起一条新任务（§4.2 ②）。原样转发 —— 返回的 `data` 是不是模型由调用方解析，
        见 `TaskRuntime.submit` 里那句 `TaskRef.from_payload`。"""
        return await self._client.init_task(
            tenant_id,
            definition_id,
            space_id,
            inputs,
            version_no=version_no,
            name=name,
            operator_id=operator_id,
            task_id=task_id,
            task_source=task_source,
        )

    async def report_status(
        self,
        tenant_id: str,
        space_id: str,
        execution_id: str,
        task_id: str,
        status: str,
        outputs: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        ts: Optional[int] = None,
        output_refs: Optional[List[Dict[str, Any]]] = None,
        error_code: Optional[str] = None,
        error_params: Optional[Dict[str, Any]] = None,
        input_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> ApiResponse[None]:
        """上报终态（§4.2 ①）。"""
        return await self._client.report_status(
            tenant_id,
            space_id,
            execution_id,
            task_id,
            status,
            outputs,
            error_message,
            ts,
            output_refs,
            # 与端口同口径：新字段走关键字，不进那条承重的位置参数序列。
            error_code=error_code,
            error_params=error_params,
            input_refs=input_refs,
        )

    async def retry_subtask(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_id: str,
        *,
        operator_id: Optional[str] = None,
    ) -> ApiResponse[None]:
        """要求重跑某一步（§4.2 ③）。"""
        return await self._client.retry_subtask(
            tenant_id,
            space_id,
            task_id,
            node_id,
            operator_id=operator_id,
        )

    async def retry_subtasks(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_ids: List[str],
        *,
        operator_id: Optional[str] = None,
    ) -> ApiResponse[None]:
        """要求重跑某几步（§4.2 ④）—— `node_ids` **顺序即执行顺序**，原样转发。"""
        return await self._client.retry_subtasks(
            tenant_id,
            space_id,
            task_id,
            node_ids,
            operator_id=operator_id,
        )

    async def get_task(
        self, tenant_id: str, task_id: str
    ) -> ApiResponse[Dict[str, Any]]:
        """读整条任务现在的状态（§4.3 `task.get`）。**原样转发，不做任何加工。**"""
        return await self._client.get_task(tenant_id, task_id)

    async def get_subtask(
        self,
        tenant_id: str,
        task_id: str,
        node_id: str,
        *,
        attempt: Optional[int] = None,
    ) -> ApiResponse[Dict[str, Any]]:
        """读某一步现在的状态（§4.3 `task.subtask_get`）。**原样转发，不做任何加工。**"""
        return await self._client.get_subtask(
            tenant_id, task_id, node_id, attempt=attempt
        )

    async def aclose(self) -> None:
        """释放底下 client 的连接池（只有 REMOTE 实现有 `aclose`，其余是 no-op）。

        端口上**故意不定义** `aclose` —— 端口是最小契约，不该逼 `MOCK` / 假实现也写一个。
        所以这里是探测式调用，`TaskRuntime.aclose()` 照这个形状做。
        """
        aclose = getattr(self._client, "aclose", None)
        if aclose is not None:
            await aclose()


__all__ = ["LegacyControlPlaneAdapter"]
