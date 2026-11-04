#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP Server - Tool 处理器 + Gateway HTTP 客户端。

单一文件包含 4 个逻辑块：
  1. HTTP 客户端 getter（参照 db._pool 模式）
  2. call_gateway() — POST Gateway /internal/kb/invoke
  3. tool handler 函数（由 app.py 通过 @mcp.tool() 注册）
  4. __all__ 导出

约束：
  - 零 import jonex_core——所有 KB 交互通过 HTTP 调 Gateway
  - Tool handler 签名不含 request、tenant_id、mcp_key_id——从 contextvar 获取
"""
import asyncio
import logging
import os
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from uuid import uuid4

import httpx
from mcp.types import CallToolResult, TextContent

from auth import (
    SYSTEM_SERVICE_ID,
    McpAuthContext,
    McpAuthError,
    require_kb_scope,
    require_mcp_auth,
    require_write_scope,
    touch_service_call_async,
)
from config import settings

logger = logging.getLogger("mcp.tools")


# ============================================================================
# Block 0: 异常类
# ============================================================================


class McpToolError(Exception):
    """MCP Tool 执行异常——参数校验、上游服务、限流等非鉴权错误。

    与 McpAuthError 语义严格分离：
      - McpAuthError: 仅鉴权/授权失败（auth.py 内部，被中间件捕获返回 401）
      - McpToolError: tool 执行层面的所有非鉴权错误（参数校验、Gateway 故障、限流等）

    code 使用 JSON-RPC error code（MCP 协议标准）。
    data 用于传递结构化错误上下文（如 retry_after 秒数）。
    """

    def __init__(self, code: int, message: str, data: dict | None = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


# ============================================================================
# Block 0.5: MCP 工具名请求级上下文
# ============================================================================

# 当前正在执行的 MCP 公开工具名。工具入口装饰器负责 set/reset，
# `call_gateway_ext()` 在出口读取并上报；handler 签名因此无需新增参数，
# fallback / 多 KB 循环也自动继承同一个工具名。
_current_mcp_tool: ContextVar[str | None] = ContextVar("current_mcp_tool", default=None)


def _set_current_mcp_tool(name: str | None):
    """写入当前工具名，返回供复位用的 token。"""
    return _current_mcp_tool.set(name)


def _reset_current_mcp_tool(token) -> None:
    """按 token 复位当前工具名（成功与异常路径都必须成对调用）。"""
    _current_mcp_tool.reset(token)


def _mcp_error_result(code: int, message: str, data: dict | None = None) -> CallToolResult:
    """把工具侧错误翻译成带机器可读码值的 ``CallToolResult``。

    为什么不是 JSON-RPC error 对象：MCP 规范里 tool 执行错误本就该走
    ``isError=true``，JSON-RPC error 留给协议层错误。MCP Python SDK 只有一个
    逃生口 —— ``UrlElicitationRequiredError``（-32042）会被 lowlevel server
    re-raise 成 JSON-RPC error，但劫持它语义错误（客户端会误触发 URL
    elicitation 流程），所以不走。

    SDK 的 ``Tool.run`` 会把未捕获异常包成 ``ToolError("Error executing tool
    xxx: ...")`` —— 文本没了码值、还多一层前缀；lowlevel server 随后只把它
    转成 ``CallToolResult(isError=True)``，没有 code 位置。把 tool 返回的
    ``CallToolResult`` 原样透传（``func_metadata.convert_result`` 与 lowlevel
    server 两条路径都认）是唯一不改 SDK 又能带上码值的出口。

    ``structuredContent`` 承载 ``{code, message, data}``，``content`` 保留人类
    可读文本以兼容只读文本的老客户端。``data`` 缺省补 ``{}``，避免客户端读
    不到键；限流场景的 ``retry_after`` 就在 ``data`` 里。

    接收通用参数而不是异常对象，是因为两类异常要共用同一出口：
      - ``McpToolError``：码值自带（-32602 / -32000 / -32001 / -32002 / -32003），
        ``data`` 原样透传（如 ``retry_after``）；
      - ``McpAuthError``：只有 HTTP ``status_code``（401/403），统一映射为契约
        约定的 ``-32002``（「认证 / 授权失败」），原始状态码放
        ``data.status_code`` 供客户端区分「不在授权范围」(401) 与「权限级别
        不够」(403)；不把 HTTP 码当 MCP 码用，避免两套语义混在一起。
    """
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structuredContent={"code": code, "message": message, "data": data or {}},
        isError=True,
    )


def _inject_mcp_tool(name: str):
    """装饰 MCP 工具入口：执行期间注入工具名，返回前复位；统一翻译工具错误。

    ``name`` 必须与 ``app.py`` 中 ``@mcp.tool()`` 注册的公开名一致；
    装饰器把它写入 ``__mcp_tool_name__``，供静态覆盖测试断言「新增工具漏注入」。

    全部 15 个工具都罩在这层下，故工具错误的线上翻译只此一处，两类异常共用
    ``_mcp_error_result``：
      - ``McpToolError`` → 用它自带的码值 / data；
      - ``McpAuthError``（handler 内 ``require_mcp_auth`` / ``require_write_scope``
        / ``_check_service_scope`` 抛出）→ 统一 ``-32002``，原始 401/403 放
        ``data.status_code``。

    ⚠ 边界：``McpAuthMiddleware`` 在工具装饰器作用域**之外**（MCP 请求分发前），
    它捕获 ``McpAuthError`` 后仍返回 HTTP 401 JSON，本装饰器不接触那条路径；
    这里只翻译「工具 handler 执行期间」抛出的鉴权异常。

    ``finally`` 必须留在两个 ``except`` 之后，保证异常路径也按 token 复位
    ContextVar，不污染同请求内的后续调用。
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            token = _set_current_mcp_tool(name)
            try:
                return await func(*args, **kwargs)
            except McpToolError as e:
                return _mcp_error_result(e.code, e.message, e.data)
            except McpAuthError as e:
                return _mcp_error_result(
                    _ERROR_AUTH, e.message, {"status_code": e.status_code}
                )
            finally:
                _reset_current_mcp_tool(token)

        wrapper.__mcp_tool_name__ = name
        return wrapper

    return decorator


# ============================================================================
# Block 1: HTTP 客户端 getter（参照 db._pool 模式）
# ============================================================================

_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    """获取已初始化的 httpx 客户端（lifespan 中由 app.py 注入）。

    参照 db.get_pool() 模式：模块级变量 + None 校验守卫。

    Returns:
        httpx.AsyncClient: 共享的异步 HTTP 客户端

    Raises:
        RuntimeError: 客户端未初始化（lifespan 尚未执行或已关闭）
    """
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized")
    return _http_client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """设置模块级 HTTP 客户端（由 app.py lifespan 调用）。"""
    global _http_client
    _http_client = client


# ============================================================================
# Block 2: call_gateway() — POST Gateway /internal/kb/invoke
# ============================================================================

# 业务码：内容重复（ResourceConflictError，jonex_core/common/exceptions.py:86）
_BIZ_CODE_CONTENT_DUPLICATE = 1003


def _resp_biz_code(resp) -> int | None:
    """从 HTTP 错误响应体里取业务码；取不到返回 None（绝不让解析失败顶掉原有兜底分支）。"""
    try:
        body = resp.json()
    except Exception:
        return None
    if isinstance(body, dict) and isinstance(body.get("code"), int):
        return body["code"]
    return None


async def call_gateway_ext(
    action: str,
    tenant_id: str,
    mcp_key_id: str,
    data: dict | None = None,
    *,
    report_tool: bool = True,
) -> tuple[dict, str | None]:
    """向 Gateway /internal/kb/invoke 发送请求，返回 (capability 响应 data, request_id)。

    `request_id` 取自 Gateway 响应顶层，与 Sidecar 能力信封及
    `search_request_event.request_id` 同源；检索工具把它返回给调用方，
    作为提交该次检索反馈的锚点。旧 Gateway 响应不带顶层 request_id 时为 None。

    异常统一转换为 McpToolError(code=-32000, message="知识库服务暂不可用")，
    消息脱敏，不暴露 Gateway URL、堆栈、内部错误详情。

    Args:
        action: capability action 名称（list_knowledge_info/search/get_raw_url）
        tenant_id: 租户 ID（从 McpAuthContext 获取）
        mcp_key_id: MCP Key ID（从 McpAuthContext 获取，用于审计）
        data: action 所需参数（可选）
        report_tool: 是否上报当前 MCP 工具名（默认 True）。工具 handler 内部
            的前置校验调用（如 ``get_service`` 取 kb_ids）传 False。
            ⚠ 语义归属在 Gateway（``_INTERNAL_CHECK_ACTIONS`` 会省略
            ``context.source="mcp"``，使其落成普通 invoke 记录）；MCP 侧这层
            只是 belt-and-braces 双保险，任一层回归都不会让同名记录复发。
            真实业务 action 调用保持默认 True，同一工具的多 action / 多 KB
            循环仍归属该工具。

    Returns:
        tuple: (capability 响应数据, 能力信封 request_id 或 None)

    Raises:
        McpToolError(code=-32000): 知识库服务不可用（HTTP 错误 / 超时 / 传输错误 / 业务失败）
        McpToolError(code=-32003): 内容重复（409 + 业务码 1003），调用方应按「已存在」处理而非重试
    """
    client = await get_http_client()
    body: dict = {
        "action": action,
        "tenant_id": tenant_id,
        "mcp_key_id": mcp_key_id,
        "data": data or {},
    }
    # 工具名只在 MCP 工具调用栈内上报；无值时完全省略该键，
    # 兼容尚未识别该字段的旧网关（不要把 null 发出去）。``report_tool=False``
    # 用于工具内部前置校验调用：仍走同一出口与错误映射，但不打工具标记。
    mcp_tool = _current_mcp_tool.get() if report_tool else None
    if mcp_tool:
        body["mcp_tool"] = mcp_tool
    try:
        resp = await client.post(
            "/internal/kb/invoke",
            json=body,
            headers={
                "X-Internal-API-Key": settings.INTERNAL_API_KEY,
                "X-Tenant-ID": tenant_id,
            },
        )
        resp.raise_for_status()
        result = resp.json()
    except httpx.HTTPStatusError as e:
        # SEC-03: Sidecar 429 限流 → MCP JSON-RPC error（含 retry_after）
        if e.response.status_code == 429:
            retry_after_raw = e.response.headers.get("Retry-After", "60")
            try:
                retry_after = int(retry_after_raw)
            except (ValueError, TypeError):
                # HTTP-date 格式: "Thu, 01 Jan 2026 00:00:00 GMT"
                try:
                    retry_dt = parsedate_to_datetime(retry_after_raw).replace(
                        tzinfo=timezone.utc
                    )
                    retry_after = max(
                        0, int((retry_dt - datetime.now(timezone.utc)).total_seconds())
                    )
                except (ValueError, TypeError):
                    retry_after = 60
            raise McpToolError(
                code=_ERROR_RATE_LIMITED,
                message=f"Rate limit exceeded. Retry after {retry_after} seconds.",
                data={"retry_after": retry_after},
            )
        if 400 <= e.response.status_code < 500:
            # 认证/授权失败（401/403）→ 不应触发管线 fallback
            if e.response.status_code in (401, 403):
                raise McpToolError(
                    code=_ERROR_AUTH,
                    message="认证失败或权限不足",
                )
            # 内容重复（409 + 业务码 1003）→ 独立分档。它被压在「参数有误」里会被
            # 调用方误读成「调参后重试即可」，而重复上传重试多少次都是同一堵墙。
            # 注意：400/404/422 仍映射 _ERROR_INVALID_PARAMS —— 搜索链路的
            # fallback 判据依赖它（见 search_llmwiki / search_jonex_wiki）。
            if (
                e.response.status_code == 409
                and _resp_biz_code(e.response) == _BIZ_CODE_CONTENT_DUPLICATE
            ):
                raise McpToolError(
                    code=_ERROR_CONFLICT,
                    message="知识库中已存在相同内容的文档，请勿重复上传",
                )
            # 其他客户端错误（400/404/422）→ 参数或管线问题，允许上游 safe-fallback
            raise McpToolError(
                code=_ERROR_INVALID_PARAMS,
                message="知识库服务请求参数有误",
            )
        # 500+ → 服务端错误
        logger.error(
            "Gateway HTTP error: action=%s tenant_id=%s status=%d",
            action,
            tenant_id,
            e.response.status_code,
        )
        raise McpToolError(code=_ERROR_INTERNAL, message="知识库服务暂不可用")
    except (httpx.TimeoutException, httpx.TransportError) as e:
        logger.error(
            "Gateway transport error: action=%s tenant_id=%s type=%s",
            action,
            tenant_id,
            type(e).__name__,
        )
        raise McpToolError(code=_ERROR_INTERNAL, message="知识库服务暂不可用")
    except httpx.RequestError as e:
        # Catch-all for any httpx errors not covered above
        logger.error(
            "Gateway request error: action=%s tenant_id=%s type=%s",
            action,
            tenant_id,
            type(e).__name__,
        )
        raise McpToolError(code=_ERROR_INTERNAL, message="知识库服务暂不可用")

    # 业务失败：响应 code 非 0
    if isinstance(result, dict) and result.get("code", 0) != 0:
        biz_code = result.get("code")
        logger.error(
            "Gateway business error: action=%s tenant_id=%s code=%s",
            action,
            tenant_id,
            biz_code,
        )
        # 内容重复：与上面的 409 分支保持同一分档（防中间层把 409 吞成 200 的情形）
        if biz_code == _BIZ_CODE_CONTENT_DUPLICATE:
            raise McpToolError(
                code=_ERROR_CONFLICT,
                message="知识库中已存在相同内容的文档，请勿重复上传",
            )
        # 1xxx = 通用参数/管线不匹配错误，映射为 INVALID_PARAMS 以便上游安全 fallback
        mcp_code = _ERROR_INVALID_PARAMS if isinstance(biz_code, int) and 1000 <= biz_code < 2000 else _ERROR_INTERNAL
        raise McpToolError(code=mcp_code, message="知识库服务暂不可用")

    request_id = result.get("request_id") if isinstance(result, dict) else None

    # 提取 data 字段（标准 success_response 格式），否则返回原始结果
    if isinstance(result, dict) and "data" in result:
        return result["data"], request_id
    return result, request_id


async def call_gateway(
    action: str,
    tenant_id: str,
    mcp_key_id: str,
    data: dict | None = None,
    *,
    report_tool: bool = True,
) -> dict:
    """向 Gateway /internal/kb/invoke 发送请求，只返回 capability 响应 data 部分。

    需要能力信封 `request_id` 时用 `call_gateway_ext`。错误映射与
    `call_gateway_ext` 完全一致。``report_tool`` 语义见 `call_gateway_ext`：
    工具内部前置校验调用传 False，避免被当成一次真实 MCP 工具调用。
    """
    result, _ = await call_gateway_ext(
        action, tenant_id, mcp_key_id, data, report_tool=report_tool
    )
    return result


async def _call_search_gateway(action: str, auth, payload: dict) -> dict:
    """检索类 action 的统一出口：调 Gateway 并把能力信封 request_id 附在响应顶层。

    `request_id` 是本次检索在 `search_request_event` 里的锚点，调用方提交
    检索反馈时回传它。
    """
    # 服务目录「最近调用时间」：所有检索类工具都带 service_id
    touch_service_call_async(auth.tenant_id, (payload or {}).get("service_id"))
    result, request_id = await call_gateway_ext(
        action, auth.tenant_id, auth.key_id, payload
    )
    if isinstance(result, dict):
        result["request_id"] = request_id
    return result


# ============================================================================
# Block 3: 3 个 tool handler 函数 + C3 交集校验
# ============================================================================


# ── Service 级权限层级: call(1) > view(0)（write 已移交写 Key）──
_PERMISSION_LEVEL_RANK = {"view": 0, "call": 1}

# 遗留 service 映射权限值降级（write/*/read → call/view，
# 与 capabilities/platform/services/mcp_key_service.py::_normalize_key_permissions 一致）
_LEGACY_PERMISSION_LEVEL_MAP = {"write": "call", "read": "view", "*": "call"}


async def _check_service_scope(
    auth: McpAuthContext, service_id: str, required_permission: str = "call"
) -> list[str]:
    """C3: 校验 service 下的服务级权限级别。

    权限级别层级（向下兼容，收敛为 call/view 两级）：
      - call:  需 "call"（含降级后的遗留 write/*）
      - view:  "view" 或 "call" 可通过
      - 映射不存在时 raise McpAuthError(401, "不在授权范围内")

    遗留值降级迁移：write→call、read→view、*→call（写权限已移交写 Key，
    * 不再全通配）。未知值落到 rank -1 → 403（安全默认拒绝）。

    统一 Key 重构后，KB 范围不再由 allowed_kb_ids 表达——空间隔离已在授权期
    由 mcp_key_service 强制（create/update 只允许把 Key 空间内的服务写进映射），
    故此处只做服务级权限检查，恒返回 []（空 = 全部 KB 允许），调用方自行获取
    service 的 kb_ids。

    Returns:
        list[str]: 恒为 []（表示「全部 KB 允许」，调用方自行获取 service 的 kb_ids）
    """
    perm_level = auth.service_permissions.get(service_id)

    if perm_level is None:
        raise McpAuthError(401, "不在授权范围内")

    # 遗留值降级迁移：write→call、read→view、*→call（与 key 级归一化规则一致）
    perm_level = _LEGACY_PERMISSION_LEVEL_MAP.get(perm_level, perm_level)

    required_rank = _PERMISSION_LEVEL_RANK.get(required_permission, 0)
    actual_rank = _PERMISSION_LEVEL_RANK.get(perm_level, -1)
    if actual_rank < required_rank:
        _LABELS: dict[str, str] = {"call": "可调用", "view": "仅查看"}
        raise McpAuthError(
            403,
            f"需要'{_LABELS.get(required_permission, required_permission)}'权限，"
            f"当前为'{_LABELS.get(perm_level, perm_level)}'权限",
        )

    return []


def _validate_file_name(file_name: str) -> str:
    """校验 file_name 安全性：非空、不含路径穿越字符。

    Args:
        file_name: MCP 客户端上传的文件名（不可信输入）。

    Returns:
        str: 去除首尾空格后的安全文件名。

    Raises:
        McpToolError(code=-32602): file_name 为空或包含路径穿越字符。
    """
    name = file_name.strip()
    if not name:
        raise McpToolError(code=-32602, message="file_name 为必填参数，不能为空")
    if _PATH_TRAVERSAL_RE.search(name) or "/" in name or "\\" in name:
        raise McpToolError(code=-32602, message="file_name 包含非法字符（路径穿越拒绝）")
    if not _VALID_FILENAME_RE.match(name):
        raise McpToolError(code=-32602, message="file_name 包含非法字符")
    return name


def _grant_mode_for_kb(auth: McpAuthContext, kb_id: str) -> str | None:
    """返回写 Key 对指定 kb 的授权模式："all" | "specified" | None（未授权）。

    方案 1：specified 目录级授权暂不支持 MCP 上传/查状态，
    handler 据此显式拒绝，避免「能建不能用」的静默失效与目录级越权。
    """
    for g in auth.write_grants:
        if g.get("kb") == kb_id:
            return g.get("mode")
    return None


@_inject_mcp_tool("confirm_upload")
async def confirm_upload(
    service_id: str,
    file_name: str,
    knowledge_base_id: str,
    storage_key: str,
    doc_id: str = "",
    mime_type: str = "",
) -> dict:
    """确认 COS 直传并触发解析/索引（直传闭环的确认步骤）。

    直传闭环（真实文件上传推荐走这条链路）：
      1) 先调 generate_upload_url 拿预签名 PUT URL + storage_key；
      2) 客户端把字节直传对象存储；
      3) 再调本工具传 storage_key 确认入库并触发解析/索引。

    原 base64 内联路径已移除——它受模型输出墙限制仅支持几 KB 级文本，
    对真实文档不适用。

    Args:
        service_id: 领域服务 ID（写 Key 场景保留签名兼容，不再用于定位 KB）。
        file_name: 原始文件名（不可信输入，需做路径穿越校验）。
        knowledge_base_id: 目标知识库 ID。
        storage_key: COS storage key（generate_upload_url 返回），直传后确认入库。
        doc_id: generate_upload_url 返回的文档 ID（可选，确认入库元数据）。
        mime_type: 可选 MIME 类型（默认 ""）。

    Returns:
        dict: 文档元数据（doc_id、file_name、knowledge_base_id、status）。

    Raises:
        McpToolError(code=-32602): 参数校验失败。
        McpAuthError(401): 未认证 / 缺少 "write" 权限 / KB 不在授权范围。
        McpToolError(code=-32000): Gateway 错误 / 超时 / 传输失败。
    """
    # Step 1 — Parameter non-empty validation
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not knowledge_base_id or not knowledge_base_id.strip():
        raise McpToolError(code=-32602, message="knowledge_base_id 为必填参数，不能为空")
    if not storage_key or not storage_key.strip():
        raise McpToolError(code=-32602, message="storage_key 为必填参数，不能为空")

    # Step 2 — file_name security validation
    file_name = _validate_file_name(file_name)

    # Step 3 — Auth + write scope（写 Key：grants 级校验，替代 service scope）
    # 写 Key 无 mcp_key_service_mappings，_check_service_scope 恒 401；写入范围由 grants 表达。
    auth = require_mcp_auth("write")  # 写授权看 write_grants
    # 服务目录「最近调用时间」：写工具统一归因到内置「知识写入」服务
    touch_service_call_async(auth.tenant_id, SYSTEM_SERVICE_ID)
    kb_id = knowledge_base_id.strip()
    # 方案 1：specified 目录级写入暂不支持 MCP 上传，显式拒绝而非静默 401
    if _grant_mode_for_kb(auth, kb_id) == "specified":
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS,
            message="目录级写入（specified 模式）暂不支持 MCP 上传，请改用 all 模式写 Key",
        )
    require_write_scope(auth, kb_id)  # grants 级：kb 在 grants 且 mode=all，否则 401

    # Step 4 — 确认入库（直传闭环）
    # [jonex] S3 兼容：storage_backend 跟随平台全局配置，不硬编码 cos——
    # 否则 S3 部署下上传的文档被标成 cos，后续取原文会用错客户端。
    _backend = (os.getenv("OBJECT_STORAGE_BACKEND", "local") or "local").strip().lower()
    data: dict = {
        "knowledge_base_id": kb_id,
        "storage_key": storage_key,
        "file_name": file_name,
        "storage_backend": _backend if _backend in ("cos", "s3") else "local",
    }
    if doc_id:
        data["doc_id"] = doc_id
    if mime_type:
        data["mime_type"] = mime_type
    return await call_gateway(
        "upload_document",
        auth.tenant_id,
        auth.key_id,
        data,
    )


@_inject_mcp_tool("generate_upload_url")
async def generate_upload_url(
    service_id: str,
    knowledge_base_id: str,
    file_name: str,
    content_type: str = "",
    file_size: int = 0,
) -> dict:
    """生成 COS 预签名上传 URL，用于文件直传对象存储。

    返回预签名 PUT URL，MCP 客户端可把大文件直接传到对象存储
    （例如 ``curl -X PUT --upload-file <file> <upload_url>``）。
    字节上传完成后，必须调用 confirm_upload 确认入库并触发解析/索引。

    Args:
        service_id: 领域服务 ID（写 Key 场景保留签名兼容，不再用于定位 KB）。
        knowledge_base_id: 目标知识库 ID。
        file_name: 原始文件名（不可信输入，需做路径穿越校验）。
        content_type: 可选 MIME 类型（默认 ""）。
        file_size: 可选，待上传字节数（默认 0 = 未知）。大于 0 时，
            服务端会校验上限并把 Content-Length 签入预签名，
            锁死 PUT 的精确字节数。

    Returns:
        dict: {doc_id, storage_key, upload_url, storage_backend}。

    Raises:
        McpToolError(code=-32602): 参数校验失败。
        McpAuthError(401): 未认证 / 缺少 "write" 权限 / KB 不在授权范围。
        McpToolError(code=-32000): Gateway 错误 / 超时 / 传输失败。
    """
    # Step 1 — Parameter non-empty validation
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not knowledge_base_id or not knowledge_base_id.strip():
        raise McpToolError(code=-32602, message="knowledge_base_id 为必填参数，不能为空")
    if not file_name or not file_name.strip():
        raise McpToolError(code=-32602, message="file_name 为必填参数，不能为空")
    if file_size < 0:
        raise McpToolError(code=-32602, message="file_size 不能为负数")

    # Step 2 — file_name security validation（路径穿越 / 非法字符）
    file_name = _validate_file_name(file_name)

    # Step 3 — Auth + write scope（写 Key：grants 级校验，替代 service scope）
    # 写 Key 无 mcp_key_service_mappings，_check_service_scope 恒 401；写入范围由 grants 表达。
    auth = require_mcp_auth("write")  # 写授权看 write_grants
    # 服务目录「最近调用时间」：写工具统一归因到内置「知识写入」服务
    touch_service_call_async(auth.tenant_id, SYSTEM_SERVICE_ID)
    kb_id = knowledge_base_id.strip()
    # 方案 1：specified 目录级写入暂不支持 MCP 直传，显式拒绝而非静默 401
    if _grant_mode_for_kb(auth, kb_id) == "specified":
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS,
            message="目录级写入（specified 模式）暂不支持 MCP 直传，请改用 all 模式写 Key",
        )
    require_write_scope(auth, kb_id)  # grants 级：kb 在 grants 且 mode=all，否则 401

    # Step 4 — Invoke capability generate_upload_url via Gateway
    data: dict = {
        "knowledge_base_id": kb_id,
        "file_name": file_name,
    }
    if content_type:
        data["content_type"] = content_type.strip()
    if file_size and file_size > 0:
        data["file_size"] = file_size

    return await call_gateway(
        "generate_upload_url",
        auth.tenant_id,
        auth.key_id,
        data,
    )


@_inject_mcp_tool("submit_search_feedback")
async def submit_search_feedback(
    request_id: str,
    service_id: str,
    feedback_type: str,
    feedback_reason: str | None = None,
    feedback_comment: str | None = None,
    operation_id: str | None = None,
    version: int = 1,
) -> dict:
    """提交检索反馈（点赞/点踩）——以检索工具返回的 request_id 锚定本次检索。

    Action: submit_mcp_feedback
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    MCP 检索不写搜索历史，没有 history_id / user_id，因此以 request_id
    （检索响应顶层的反馈锚点）定位检索；归属由服务端按 MCP Key 校验。
    不接收 tenant_id / mcp_key_id / query / answer——身份与租户取自鉴权上下文，
    问答快照不落库。

    Args:
        request_id: 检索响应返回的 request_id（反馈锚点）
        service_id: 领域服务 ID（服务端会与检索记录比对）
        feedback_type: 反馈类型，"like" 或 "dislike"
        feedback_reason: 点踩原因（dislike 时必填，复用现有 6 个原因码）
        feedback_comment: 补充评论（可选，最多 300 字符）
        operation_id: 幂等键（可选，缺省由 MCP 生成 uuid4）
        version: 反馈版本号（默认 1；乱序提交时服务端丢弃旧版本）

    Returns:
        dict: {feedback, superseded, idempotent}

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): request_id / service_id / feedback_type 缺失
        McpToolError(code=-32000): 知识库服务不可用
    """
    if not request_id or not request_id.strip():
        raise McpToolError(code=-32602, message="request_id 为必填参数，不能为空")
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not feedback_type or not feedback_type.strip():
        raise McpToolError(code=-32602, message="feedback_type 为必填参数，不能为空")

    auth = require_mcp_auth("view")
    await _check_service_scope(auth, service_id, required_permission="call")
    touch_service_call_async(auth.tenant_id, service_id.strip())

    payload: dict = {
        "request_id": request_id.strip(),
        "service_id": service_id.strip(),
        "feedback_type": feedback_type.strip(),
        "operation_id": operation_id or uuid4().hex,
        "version": version,
    }
    if feedback_reason is not None:
        payload["feedback_reason"] = feedback_reason
    if feedback_comment is not None:
        payload["feedback_comment"] = feedback_comment

    return await call_gateway(
        "submit_mcp_feedback",
        auth.tenant_id,
        auth.key_id,
        payload,
    )


@_inject_mcp_tool("get_upload_status")
async def get_upload_status(service_id: str, document_id: str) -> dict:
    """查询上传后的文档处理状态。

    返回 status、ontology_status、error_message 等元数据，
    供 MCP 客户端跟踪异步处理进度（PENDING → PARSING → INGESTING → READY / FAILED）。

    文档可能处于 ``status=ready`` 且 ``ontology_status=failed`` 的状态：
    正文已解析、可问答，但知识图谱未建成。这属于**部分成功**，
    必须显式提示降级，不能按完全成功汇报。

    Args:
        service_id: 领域服务 ID（写 Key 场景保留签名兼容，不再用于定位 KB）。
        document_id: 文档 ID（confirm_upload 返回）。

    Returns:
        dict: 文档状态字段（doc_id、status、ontology_status、error_message、
              file_name、file_size、knowledge_base_id、created_at、updated_at），
              以及图谱降级提示：ontology_error、ontology_retry_count、
              ontology_retry_limit、degraded、degraded_reason、retry_exhausted。

              当 ``status=ready`` 且 ``ontology_status=failed`` 时，
              ``degraded=true``、``degraded_reason="ontology_failed"``；
              图谱重试次数达到 ``ontology_retry_limit`` 时
              ``retry_exhausted=true``，``status`` 仍为 ``ready``。

    Raises:
        McpAuthError(401): 未认证 / 不是写 Key。
        McpToolError(code=-32602): 参数校验失败。
        McpToolError(code=-32000): Gateway 错误 / 写 Key 无可查询的授权。
    """
    # Step 1 — Parameter non-empty validation
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not document_id or not document_id.strip():
        raise McpToolError(code=-32602, message="document_id 为必填参数，不能为空")

    # Step 2 — Auth + scope（写 Key 专属：上传生命周期，service_id 保留签名但不再用于定位）
    auth = require_mcp_auth("write")  # 写授权看 write_grants
    # 服务目录「最近调用时间」：上传生命周期查询同样归因到「知识写入」服务
    touch_service_call_async(auth.tenant_id, SYSTEM_SERVICE_ID)
    # 方案 1：specified 目录级不支持 MCP，仅 all 模式 kb 可查上传状态，
    # 剔除 specified 的 kb，避免平铺 grants[].kb 导致目录级隔离被突破（越权读他人文档状态）。
    kb_ids_to_check = [
        g["kb"] for g in auth.write_grants if g.get("mode") == "all" and g.get("kb")
    ]

    if not kb_ids_to_check:
        raise McpToolError(
            code=_ERROR_INTERNAL,
            message="写 Key 未授权可查询的知识库（specified 目录级暂不支持 MCP）",
        )

    # Step 3 — Call Gateway (returns document status or raises ResourceNotFoundError)
    return await call_gateway(
        "get_document_status",
        auth.tenant_id,
        auth.key_id,
        {
            "document_id": document_id.strip(),
            "kb_ids": kb_ids_to_check,
        },
    )


@_inject_mcp_tool("list_domain_services")
async def list_domain_services() -> dict:
    """列出 MCP Key 对应租户下所有领域服务。

    Action: list_services
    Guard: require_mcp_auth("view") + 按 Key 授权 service_permissions 过滤目录

    只返回该 Key 可访问（service_permissions 授权范围内）的服务及其库，
    避免目录枚举平铺未授权库、agent 一问即 403。

    Returns:
        dict: 含 items（领域服务列表，已按授权过滤）、total（过滤后总数）字段
    """
    auth = require_mcp_auth("view")
    data = {}
    if auth.space_id:
        data["space_id"] = auth.space_id
    result = await call_gateway(
        "list_services",
        auth.tenant_id,
        auth.key_id,
        data,
    )
    # 上游标准返回 {items: [...], total: N}
    if isinstance(result, dict) and "items" in result:
        authorized_ids = set(auth.service_permissions.keys())
        items = [
            item
            for item in result["items"]
            if isinstance(item, dict) and item.get("id") in authorized_ids
        ]
        return {"items": items, "total": len(items)}
    # 格式意外 → 记录并报错
    logger.error("list_services 返回意外格式: %s", type(result).__name__)
    raise McpToolError(code=-32000, message="领域服务列表返回格式异常")


# ── JSON-RPC 错误码常量（per JSON-RPC 2.0 spec + MCP extension） ──
_ERROR_INVALID_PARAMS = -32602  # 参数校验失败（JSON-RPC standard）
_ERROR_INTERNAL = -32000  # 内部服务错误
_ERROR_RATE_LIMITED = -32001  # 限流（自定义 MCP extension）
_ERROR_AUTH = -32002  # 认证/授权失败（401/403 → 禁止管线 fallback）
_ERROR_CONFLICT = -32003  # 资源冲突/内容重复（409 + 业务码 1003；同样禁止管线 fallback）

# LLM-Wiki 检索开关（Feature Flag，默认开启）。
# ⚠ 只由环境变量控制，**不再由启动健康检查翻转**（同 SEARCH_JONEX_WIKI_ENABLED：
# 依赖晚启动是部署常态，一次性探测失败永久禁用会让工具无法自愈）。
SEARCH_LLMWIKI_ENABLED = os.getenv("SEARCH_LLMWIKI_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def is_search_llmwiki_enabled() -> bool:
    """返回 search_llmwiki 的显式开关（仅由环境变量决定，不含运行时健康状态）。"""
    return SEARCH_LLMWIKI_ENABLED


# [jonex] jonex_wiki 检索开关（Feature Flag，默认开启）。
# ⚠ 只由环境变量控制，**不再由启动健康检查翻转**：jonex-wiki 晚于 mcp-server 就绪
# 是部署常态，一次性探测失败若永久禁用，工具会一直报「暂未开放」且无法自愈。
# 服务真不可用时，调用会返回 -32000 并由 handler 的 fallback 回退到 search_mix。
SEARCH_JONEX_WIKI_ENABLED = os.getenv("SEARCH_JONEX_WIKI_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def is_search_jonex_wiki_enabled() -> bool:
    """返回 search_jonex_wiki 的显式开关（仅由环境变量决定，不含运行时健康状态）。"""
    return SEARCH_JONEX_WIKI_ENABLED


_VALID_SEARCH_MODES = frozenset({"naive", "local", "global", "hybrid", "mix"})
# mix → hybrid 别名兼容（与 capabilities/knowledge_base/dtos/search.py 保持一致）
_SEARCH_MODE_ALIASES = {"mix": "hybrid"}

# Upload document validation constants
# 合法文件名字符：字母数字下划线、中日韩汉字、点、连字符，以及括号/空格/方括号
# （半角 () [] 与全角（）均允许，路径穿越仍由 _PATH_TRAVERSAL_RE 与斜杠检查拦截）
_VALID_FILENAME_RE = re.compile(r'^[\w一-鿿.()\[\]（） -]+$')  # Safe filename characters
_PATH_TRAVERSAL_RE = re.compile(r'\.\./|\.\.\\')  # Path traversal detection


@_inject_mcp_tool("list_documents")
async def list_documents(
    service_id: str,
    file_name: str = "",
) -> dict:
    """列出领域服务下所有知识库的文档。以领域服务为入口，自动解析所属知识库。

    Action: list_documents
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="view")

    解决「文件名 → document_id」桥接缺口：MCP 客户端无需先到 REST API 查文档列表再回来调用 read_source。
    支持可选的 file_name 关键词过滤（模糊匹配文件名和文件路径）。

    Args:
        service_id: 领域服务 ID
        file_name: 可选的文件名关键词过滤（最多 255 字符）

    Returns:
        dict: 含 items（文档列表，每项含 id、file_name、knowledge_base_id、status 等）、
              total 字段。部分知识库不可访问时额外带 ``errors`` 数组，元素为
              ``{knowledge_base_id, code, message}``；全部成功时没有 ``errors`` 键。

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空 service_id / file_name 超长）
        McpToolError(code=-32000): 知识库服务不可用 / 服务下无可用知识库 /
            全部知识库均不可访问且无鉴权失败
        McpToolError(code=-32002): 全部知识库均不可访问且至少一个为鉴权失败
    """
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    file_name = file_name.strip()
    if len(file_name) > 255:
        raise McpToolError(code=-32602, message="file_name 长度不能超过 255 字符")
    auth = require_mcp_auth("view")
    intersection = await _check_service_scope(auth, service_id, required_permission="view")
    touch_service_call_async(auth.tenant_id, service_id.strip())
    if intersection:
        kb_ids_to_query = intersection
    else:
        # 前置校验调用：get_service 不是公开 MCP 工具，不上报工具名。
        # 语义归属在 Gateway（_INTERNAL_CHECK_ACTIONS 记成普通 invoke）；
        # 这里 report_tool=False 是 belt-and-braces 双保险。
        service_detail = await call_gateway(
            "get_service",
            auth.tenant_id,
            auth.key_id,
            {"service_id": service_id},
            report_tool=False,
        )
        kb_ids_to_query = service_detail.get("kb_ids", [])
    if not kb_ids_to_query:
        raise McpToolError(code=-32000, message="服务下无可用知识库")
    all_items: list[dict] = []
    # 逐 KB 收集错误：不能把「不可访问」静默吞成「没有文档」，否则依赖本工具
    # 的调用方（如等待解析完成）会把上游故障/密级拦截误判成空结果并等到超时。
    kb_errors: list[dict] = []
    for kb_id in kb_ids_to_query:
        try:
            data: dict[str, str] = {"knowledge_base_id": kb_id}
            if file_name:
                data["keyword"] = file_name
            result = await call_gateway(
                "list_documents", auth.tenant_id, auth.key_id, data,
            )
            all_items.extend(result.get("items", []))
        except McpToolError as exc:
            logger.warning(
                "list_documents 跳过不可访问的知识库: service_id=%s kb_id=%s code=%s",
                service_id,
                kb_id,
                exc.code,
            )
            kb_errors.append(
                {
                    "knowledge_base_id": kb_id,
                    # 保留原始 code 供诊断：400/404/422 被 call_gateway 映射成
                    # -32602，但这里多半是密级/权限问题，不是调用方参数错。
                    "code": exc.code,
                    # 中性文案：不把上游 404 解释成「KB 不存在」，避免泄露存在性。
                    "message": "该知识库不可访问，已跳过",
                }
            )
    if kb_errors and len(kb_errors) == len(kb_ids_to_query):
        # 全部失败：对外统一归一为 -32002 / -32000，绝不抛 -32602（会把密级
        # 拦截误导成「调用方参数有误」）。任一鉴权失败优先 -32002。
        logger.error(
            "list_documents 全部知识库不可访问: service_id=%s errors=%s",
            service_id,
            kb_errors,
        )
        if any(err["code"] == _ERROR_AUTH for err in kb_errors):
            raise McpToolError(code=_ERROR_AUTH, message="知识库不可访问")
        raise McpToolError(code=_ERROR_INTERNAL, message="知识库服务暂不可用")
    payload: dict = {"items": all_items, "total": len(all_items)}
    if kb_errors:
        # 向后兼容：只在部分失败时出现，旧客户端可忽略。
        payload["errors"] = kb_errors
    return payload


# ── 本体列式浏览：3 个只读工具 ───────────────────────────────────────────
# 只调用白名单内的读 action；KB 归属唯一活路径是 get_service → kb_ids。


def _ontology_required_str(value, field: str, max_len: int) -> str:
    """校验必填字符串参数：非空、去首尾空格、长度上限。"""
    if not isinstance(value, str):
        raise McpToolError(code=_ERROR_INVALID_PARAMS, message=f"{field} 必须为字符串")
    value = value.strip()
    if not value:
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS, message=f"{field} 为必填参数，不能为空"
        )
    if len(value) > max_len:
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS,
            message=f"{field} 长度不能超过 {max_len} 字符",
        )
    return value


def _ontology_optional_str(value, field: str, max_len: int) -> str:
    """校验可选字符串参数：允许空串，去首尾空格，长度上限。"""
    if not isinstance(value, str):
        raise McpToolError(code=_ERROR_INVALID_PARAMS, message=f"{field} 必须为字符串")
    value = value.strip()
    if len(value) > max_len:
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS,
            message=f"{field} 长度不能超过 {max_len} 字符",
        )
    return value


def _ontology_int_param(value, field: str, minimum: int, maximum: int) -> int:
    """校验整数参数上下界（MCP 不经 REST 的 le 约束，必须自校验）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise McpToolError(code=_ERROR_INVALID_PARAMS, message=f"{field} 必须为整数")
    if not minimum <= value <= maximum:
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS,
            message=f"{field} 必须在 {minimum}-{maximum} 之间",
        )
    return value


# 单次跨库聚合的 KB 数量上限：超过则要求调用方显式收窄范围。
_ONTOLOGY_MAX_FANOUT_KBS = settings.ONTOLOGY_MAX_FANOUT_KBS

# 同 KB 多跳（D11）：深度上限与多 KB 扇出整体截止时间。
# 上限与 Gateway / capability 读同名 env；deadline 仅 depth>1 的多 KB 扇出生效。
_ONTOLOGY_NEIGHBOR_DEPTH_MAX = settings.ONTOLOGY_NEIGHBOR_DEPTH_MAX
_MULTIHOP_DEADLINE_SECONDS = settings.ONTOLOGY_MULTIHOP_DEADLINE_SECONDS


def _normalize_ontology_kb_ids(value) -> list[str]:
    """校验并归一 ``knowledge_base_ids``：list、元素非空字符串、去重保序、≤配置上限。

    ``None`` 表示未提供（返回空列表）；显式 ``[]`` 视为参数错误，避免被
    「都不传 = 查全服务」静默放大成一次全量扇出。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS, message="knowledge_base_ids 必须为字符串数组"
        )
    ids: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise McpToolError(
                code=_ERROR_INVALID_PARAMS,
                message="knowledge_base_ids 的元素必须为字符串",
            )
        kb_id = raw.strip()
        if not kb_id:
            raise McpToolError(
                code=_ERROR_INVALID_PARAMS,
                message="knowledge_base_ids 不能包含空字符串",
            )
        if len(kb_id) > 128:
            raise McpToolError(
                code=_ERROR_INVALID_PARAMS,
                message="knowledge_base_ids 的单个 ID 长度不能超过 128 字符",
            )
        if kb_id in seen:
            continue
        seen.add(kb_id)
        ids.append(kb_id)
    if not ids:
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS, message="knowledge_base_ids 不能为空列表"
        )
    if len(ids) > _ONTOLOGY_MAX_FANOUT_KBS:
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS,
            message=f"knowledge_base_ids 最多 {_ONTOLOGY_MAX_FANOUT_KBS} 个",
        )
    return ids


def _validate_ontology_kb_selection(
    knowledge_base_id: str,
    knowledge_base_ids,
) -> tuple[str, list[str]]:
    """纯参数校验（先于鉴权）：单值/列表互斥 + 列表归一。

    Returns:
        (single, explicit)：single 为空串表示未提供单值；explicit 为归一后的
        显式列表（未提供时为空列表）。
    """
    explicit = _normalize_ontology_kb_ids(knowledge_base_ids)
    single = (knowledge_base_id or "").strip()
    if single and knowledge_base_ids is not None:
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS,
            message="knowledge_base_id 与 knowledge_base_ids 不能同时提供",
        )
    return single, explicit


async def _resolve_ontology_kb_ids(
    auth: McpAuthContext,
    service_id: str,
    knowledge_base_id: str,
    knowledge_base_ids,
) -> list[str]:
    """解析本次调用要扇出的 KB 列表（唯一 ``get_service`` 调用点）。

    显式单值/列表必须全部属于服务 ``kb_ids``，任一越界整单拒绝；都未提供时
    取服务下全部可读 KB，超过上限则要求调用方显式指定。

    Raises:
        McpToolError(code=-32000): 服务下无可用知识库 / 缺省 KB 超过上限
        McpToolError(code=-32002): 显式知识库不在该服务范围内
    """
    single, explicit = _validate_ontology_kb_selection(
        knowledge_base_id, knowledge_base_ids
    )

    # 前置校验调用：get_service 不是公开 MCP 工具，不上报工具名。
    service_detail = await call_gateway(
        "get_service",
        auth.tenant_id,
        auth.key_id,
        {"service_id": service_id},
        report_tool=False,
    )
    kb_ids = (
        service_detail.get("kb_ids", []) if isinstance(service_detail, dict) else []
    )
    if not kb_ids:
        raise McpToolError(code=_ERROR_INTERNAL, message="服务下无可用知识库")

    requested = [single] if single else explicit
    if requested:
        # 不区分「KB 不存在」与「KB 越界」，避免泄露存在性。
        if any(kb_id not in kb_ids for kb_id in requested):
            raise McpToolError(
                code=_ERROR_AUTH, message="知识库不在该服务范围内"
            )
        return requested

    # 缺省：服务下全部可读 KB，去重保序后受同一上限约束。
    resolved: list[str] = []
    seen: set[str] = set()
    for kb_id in kb_ids:
        if kb_id in seen:
            continue
        seen.add(kb_id)
        resolved.append(kb_id)
    if len(resolved) > _ONTOLOGY_MAX_FANOUT_KBS:
        raise McpToolError(
            code=_ERROR_INTERNAL,
            message=(
                f"服务关联知识库超过 {_ONTOLOGY_MAX_FANOUT_KBS} 个，"
                "请显式指定 knowledge_base_ids"
            ),
        )
    return resolved


async def _fanout_ontology_action(
    action: str,
    auth: McpAuthContext,
    kb_ids: list[str],
    build_payload,
    *,
    deadline_seconds: float | None = None,
) -> list[dict]:
    """逐 KB 串行调用目标 action，独立捕获单库失败。

    ``deadline_seconds`` 非空时对整轮扇出设总预算（D11 多跳 × 多 KB）：每库只用
    「剩余时间」等待，超时库按失败组处理，已完成库照常返回。

    Returns:
        与 ``kb_ids`` 同序的 outcomes；成功项 ``{"knowledge_base_id", "data"}``，
        失败项 ``{"knowledge_base_id", "error": {knowledge_base_id, code,
        message}, "retry_after"}``。``error`` 即对外暴露的中性错误元素。
    """
    outcomes: list[dict] = []
    deadline = (
        time.monotonic() + deadline_seconds if deadline_seconds else None
    )
    for kb_id in kb_ids:
        try:
            coro = call_gateway(
                action, auth.tenant_id, auth.key_id, build_payload(kb_id)
            )
            if deadline is None:
                data = await coro
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                data = await asyncio.wait_for(coro, timeout=remaining)
            outcomes.append({"knowledge_base_id": kb_id, "data": data})
        except asyncio.TimeoutError:
            logger.warning(
                "%s 多 KB 整体 deadline 到期，跳过: kb_id=%s", action, kb_id
            )
            outcomes.append(
                {
                    "knowledge_base_id": kb_id,
                    "error": {
                        "knowledge_base_id": kb_id,
                        "code": _ERROR_INTERNAL,
                        "message": "该知识库不可访问，已跳过",
                    },
                }
            )
        except McpToolError as exc:
            logger.warning(
                "%s 跳过不可访问的知识库: kb_id=%s code=%s", action, kb_id, exc.code
            )
            retry_after = (exc.data or {}).get("retry_after")
            outcomes.append(
                {
                    "knowledge_base_id": kb_id,
                    "error": {
                        "knowledge_base_id": kb_id,
                        "code": exc.code,
                        # 中性文案：不把上游 404 解释成「KB 不存在」。
                        "message": "该知识库不可访问，已跳过",
                    },
                    "retry_after": retry_after,
                }
            )
    return outcomes


def _raise_if_all_ontology_groups_failed(outcomes: list[dict]) -> None:
    """全部 KB 失败时归一为 -32002 / -32001 / -32000；部分失败不抛。"""
    errors = [o["error"] for o in outcomes if "error" in o]
    if not errors or len(errors) != len(outcomes):
        return
    logger.error("本体浏览全部知识库不可访问: errors=%s", errors)
    if any(err["code"] == _ERROR_AUTH for err in errors):
        raise McpToolError(code=_ERROR_AUTH, message="知识库不可访问")
    if all(err["code"] == _ERROR_RATE_LIMITED for err in errors):
        retry_after = max(
            (o.get("retry_after") or 60 for o in outcomes if "error" in o),
            default=60,
        )
        raise McpToolError(
            code=_ERROR_RATE_LIMITED,
            message=f"Rate limit exceeded. Retry after {retry_after} seconds.",
            data={"retry_after": retry_after},
        )
    # 注意：密级 404 映射来的 -32602 也落到这里 → -32000，绝不归一为参数错误。
    raise McpToolError(code=_ERROR_INTERNAL, message="知识库服务暂不可用")


@_inject_mcp_tool("list_ontology_entity_types")
async def list_ontology_entity_types(
    service_id: str,
    knowledge_base_id: str = "",
    knowledge_base_ids=None,
) -> dict:
    """浏览本体知识库的实体类型目录（本体列式浏览第一列）。

    Action: list_ontology_entity_types
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, "view")
           + 显式 KB（单值/列表）全部 ∈ get_service.kb_ids

    Args:
        service_id: 领域服务 ID
        knowledge_base_id: 单个知识库 ID（可选，与 knowledge_base_ids 互斥）
        knowledge_base_ids: 知识库 ID 列表（可选，去重后不超过配置上限；与单值互斥）；
            两者都不传时查询服务下全部可读知识库（上限同配置）

    Returns:
        dict: 单个 KB 时 {"items": [...], "total": int}（旧形态）；
              多个 KB 时 {"items": [...], "total": int, "groups": [...]}，
              顶层 item 追加 knowledge_base_id，groups 每项含
              knowledge_base_id + items/total，失败组只含 errors。
              item 含 name、display_name、description、status、build_status、
              instance_count、attributes、source_object_id。游离类型
              status="unschematized" 且 build_status="built"。

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败
        McpToolError(code=-32002): 知识库不在该服务范围内
        McpToolError(code=-32000): 知识库服务不可用 / 服务下无可用知识库 /
            缺省知识库超过配置上限
    """
    service_id = _ontology_required_str(service_id, "service_id", 128)
    knowledge_base_id = _ontology_optional_str(
        knowledge_base_id, "knowledge_base_id", 128
    )
    # 纯参数校验先于鉴权与任何上游调用。
    _single, explicit = _validate_ontology_kb_selection(
        knowledge_base_id, knowledge_base_ids
    )

    auth = require_mcp_auth("view")
    await _check_service_scope(auth, service_id, required_permission="view")
    touch_service_call_async(auth.tenant_id, service_id)
    kb_ids = await _resolve_ontology_kb_ids(
        auth, service_id, knowledge_base_id, knowledge_base_ids
    )

    def build_payload(kb_id: str) -> dict:
        return {"knowledge_base_id": kb_id}

    # 返回形态按参数形态判定：显式列表（即使 1 个元素）一律 groups；
    # 显式单值 / 缺省且只解析出 1 个 KB 才逐字节复用旧形态。
    if not explicit and len(kb_ids) == 1:
        return await call_gateway(
            "list_ontology_entity_types",
            auth.tenant_id,
            auth.key_id,
            build_payload(kb_ids[0]),
        )

    outcomes = await _fanout_ontology_action(
        "list_ontology_entity_types", auth, kb_ids, build_payload
    )
    _raise_if_all_ontology_groups_failed(outcomes)

    all_items: list[dict] = []
    groups: list[dict] = []
    for outcome in outcomes:
        kb_id = outcome["knowledge_base_id"]
        if "error" in outcome:
            groups.append({"knowledge_base_id": kb_id, "errors": [outcome["error"]]})
            continue
        data = outcome["data"] or {}
        items = data.get("items", [])
        all_items.extend({**item, "knowledge_base_id": kb_id} for item in items)
        groups.append(
            {
                "knowledge_base_id": kb_id,
                "items": items,
                "total": data.get("total", len(items)),
            }
        )
    return {"items": all_items, "total": len(all_items), "groups": groups}


@_inject_mcp_tool("list_ontology_instances")
async def list_ontology_instances(
    service_id: str,
    knowledge_base_id: str = "",
    entity_type: str = "",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    knowledge_base_ids=None,
) -> dict:
    """分页浏览某个实体类型下的本体实例（本体列式浏览第二列）。

    Action: list_ontology_instances
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, "view")
           + 显式 KB（单值/列表）全部 ∈ get_service.kb_ids

    Args:
        service_id: 领域服务 ID
        knowledge_base_id: 单个知识库 ID（可选，与 knowledge_base_ids 互斥）
        entity_type: 实体类型精确匹配（可选，≤ 128 字符）
        keyword: 名称/别名子串模糊搜索（可选，≤ 255 字符）
        page: 页码（1-1000，默认 1；多 KB 时对每个库独立生效）
        page_size: 每页条数（1-100，默认 20；多 KB 时对每个库独立生效）
        knowledge_base_ids: 知识库 ID 列表（可选，去重后不超过配置上限；与单值互斥）；
            两者都不传时查询服务下全部可读知识库（上限同配置）

    Returns:
        dict: 单个 KB 时 {"items": [...], "total": int, "page": int,
              "page_size": int}（旧形态）；多个 KB 时 {"total": int,
              "groups": [...]}，groups 每项含 knowledge_base_id + items/total/
              page/page_size（各库独立分页），失败组只含 errors；多 KB 时
              **没有**顶层 items/page/page_size。顶层 total 为成功组 total 之和。
              item 含 name、type、aliases、attributes、description、confidence、
              doc_ids —— 没有 id。

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（含上下界越界）
        McpToolError(code=-32002): 知识库不在该服务范围内
        McpToolError(code=-32000): 知识库服务不可用 / 服务下无可用知识库 /
            缺省知识库超过配置上限
    """
    service_id = _ontology_required_str(service_id, "service_id", 128)
    knowledge_base_id = _ontology_optional_str(
        knowledge_base_id, "knowledge_base_id", 128
    )
    entity_type = _ontology_optional_str(entity_type, "entity_type", 128)
    keyword = _ontology_optional_str(keyword, "keyword", 255)
    page = _ontology_int_param(page, "page", 1, 1000)
    page_size = _ontology_int_param(page_size, "page_size", 1, 100)
    # 纯参数校验先于鉴权与任何上游调用。
    _single, explicit = _validate_ontology_kb_selection(
        knowledge_base_id, knowledge_base_ids
    )

    auth = require_mcp_auth("view")
    await _check_service_scope(auth, service_id, required_permission="view")
    touch_service_call_async(auth.tenant_id, service_id)
    kb_ids = await _resolve_ontology_kb_ids(
        auth, service_id, knowledge_base_id, knowledge_base_ids
    )

    def build_payload(kb_id: str) -> dict:
        payload: dict = {
            "knowledge_base_id": kb_id,
            "page": page,
            "page_size": page_size,
        }
        # 空串不传：后端 DTO 对这些字段按缺省处理
        if entity_type:
            payload["entity_type"] = entity_type
        if keyword:
            payload["keyword"] = keyword
        return payload

    # 返回形态按参数形态判定：显式列表（即使 1 个元素）一律 groups；
    # 显式单值 / 缺省且只解析出 1 个 KB 才逐字节复用旧形态。
    if not explicit and len(kb_ids) == 1:
        return await call_gateway(
            "list_ontology_instances",
            auth.tenant_id,
            auth.key_id,
            build_payload(kb_ids[0]),
        )

    outcomes = await _fanout_ontology_action(
        "list_ontology_instances", auth, kb_ids, build_payload
    )
    _raise_if_all_ontology_groups_failed(outcomes)

    groups: list[dict] = []
    total = 0
    for outcome in outcomes:
        kb_id = outcome["knowledge_base_id"]
        if "error" in outcome:
            groups.append({"knowledge_base_id": kb_id, "errors": [outcome["error"]]})
            continue
        data = outcome["data"] or {}
        group_total = int(data.get("total", 0) or 0)
        total += group_total
        groups.append(
            {
                "knowledge_base_id": kb_id,
                "items": data.get("items", []),
                "total": group_total,
                "page": data.get("page", page),
                "page_size": data.get("page_size", page_size),
            }
        )
    # 多 KB：不提供顶层 items / page / page_size（全局分页语义失真）。
    return {"total": total, "groups": groups}


@_inject_mcp_tool("expand_ontology_neighbors")
async def expand_ontology_neighbors(
    service_id: str,
    knowledge_base_id: str = "",
    entity_type: str = "",
    canonical_name: str = "",
    limit: int = 50,
    knowledge_base_ids=None,
    depth: int = 1,
) -> dict:
    """展开一个本体实例的邻居（本体列式浏览第三列；depth>1 时同 KB 多跳）。

    Action: expand_ontology_neighbors
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, "view")
           + 显式 KB（单值/列表）全部 ∈ get_service.kb_ids

    Args:
        service_id: 领域服务 ID
        knowledge_base_id: 单个知识库 ID（可选，与 knowledge_base_ids 互斥）
        entity_type: 起点实体类型（必填，≤ 128 字符）
        canonical_name: 起点规范名称（必填，≤ 512 字符）
        limit: 边数上限（1-200，默认 50；多 KB 时对每个库独立生效）；
            节点按 id 去重后不超过 limit
        knowledge_base_ids: 知识库 ID 列表（可选，去重后不超过配置上限；与单值互斥）；
            两者都不传时查询服务下全部可读知识库（上限同配置）

    Returns:
        dict: 单个 KB 时 {"nodes": [...], "edges": [...], "degraded": bool}
              （旧形态），降级时额外含 degraded_reason；多个 KB 时只返回
              {"groups": [...]}，每项含 knowledge_base_id + nodes/edges/
              degraded（+ degraded_reason），失败组只含 errors；多 KB 时
              **没有**顶层 nodes/edges/degraded。node 含稳定 id
              （"类型:名称"，不含知识库），edge 的 source/target 是名称不是节点 id。

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（含上下界越界）
        McpToolError(code=-32002): 知识库不在该服务范围内
        McpToolError(code=-32000): 知识库服务不可用 / 服务下无可用知识库 /
            缺省知识库超过配置上限
    """
    service_id = _ontology_required_str(service_id, "service_id", 128)
    knowledge_base_id = _ontology_optional_str(
        knowledge_base_id, "knowledge_base_id", 128
    )
    # entity_type / canonical_name 因位置参数关系给了空串默认值，
    # 但语义仍是必填：空串一律 -32602。
    entity_type = _ontology_required_str(entity_type, "entity_type", 128)
    canonical_name = _ontology_required_str(canonical_name, "canonical_name", 512)
    limit = _ontology_int_param(limit, "limit", 1, 200)
    depth = _ontology_int_param(depth, "depth", 1, _ONTOLOGY_NEIGHBOR_DEPTH_MAX)
    # 纯参数校验先于鉴权与任何上游调用。
    _single, explicit = _validate_ontology_kb_selection(
        knowledge_base_id, knowledge_base_ids
    )

    auth = require_mcp_auth("view")
    await _check_service_scope(auth, service_id, required_permission="view")
    touch_service_call_async(auth.tenant_id, service_id)
    kb_ids = await _resolve_ontology_kb_ids(
        auth, service_id, knowledge_base_id, knowledge_base_ids
    )

    def build_payload(kb_id: str) -> dict:
        payload = {
            "knowledge_base_id": kb_id,
            "entity_type": entity_type,
            "canonical_name": canonical_name,
            "limit": limit,
        }
        # depth=1 不写 payload：保持单 KB 旧请求与多 KB 精确断言不变。
        if depth > 1:
            payload["depth"] = depth
        return payload

    # 返回形态按参数形态判定：显式列表（即使 1 个元素）一律 groups；
    # 显式单值 / 缺省且只解析出 1 个 KB 才逐字节复用旧形态。
    if not explicit and len(kb_ids) == 1:
        return await call_gateway(
            "expand_ontology_neighbors",
            auth.tenant_id,
            auth.key_id,
            build_payload(kb_ids[0]),
        )

    outcomes = await _fanout_ontology_action(
        "expand_ontology_neighbors",
        auth,
        kb_ids,
        build_payload,
        # 仅多跳 × 多 KB 设整体 deadline；depth=1 不改变既有行为。
        deadline_seconds=_MULTIHOP_DEADLINE_SECONDS if depth > 1 else None,
    )
    _raise_if_all_ontology_groups_failed(outcomes)

    groups: list[dict] = []
    for outcome in outcomes:
        kb_id = outcome["knowledge_base_id"]
        if "error" in outcome:
            groups.append({"knowledge_base_id": kb_id, "errors": [outcome["error"]]})
            continue
        data = outcome["data"] or {}
        group: dict = {
            "knowledge_base_id": kb_id,
            "nodes": data.get("nodes", []),
            "edges": data.get("edges", []),
            "degraded": bool(data.get("degraded", False)),
        }
        if "degraded_reason" in data:
            group["degraded_reason"] = data["degraded_reason"]
        if depth > 1:
            group["depth"] = data.get("depth", depth)
            group["limit"] = data.get("limit", limit)
            group["returned"] = data.get("returned", len(group["edges"]))
            group["truncated"] = bool(data.get("truncated", False))
        groups.append(group)
    # 多 KB：不提供顶层 nodes / edges / degraded（节点 id 不含 kb，跨库会撞 id）。
    return {"groups": groups}


async def _validate_search_params(
    service_id: str,
    query: str,
    mode: str,
    top_k: int,
) -> tuple[str, str, "McpAuthContext", list[str]]:
    """统一的搜索参数校验 + 鉴权。

    校验顺序：mode 规范化 → service_id/query 非空 → query 长度 → mode 白名单 → top_k 范围 → 鉴权。

    Returns:
        (normalized_query, normalized_mode, auth, kb_ids): 规范化后的 query、mode、鉴权上下文和授权的 kb_ids 交集（空列表 = 全部允许）

    Raises:
        McpToolError(code=-32602): 参数校验失败
        McpAuthError(401): 未认证 / service 不在授权范围
    """
    mode = mode.lower()
    mode = _SEARCH_MODE_ALIASES.get(mode, mode)

    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not query or not query.strip():
        raise McpToolError(code=-32602, message="query 为必填参数，不能为空")
    query = query.strip()
    if len(query) > 2000:
        raise McpToolError(code=-32602, message="query 长度不能超过 2000 字符")
    if mode not in _VALID_SEARCH_MODES:
        raise McpToolError(
            code=-32602,
            message=f"mode 必须为 {sorted(_VALID_SEARCH_MODES)} 之一，当前值: {mode}",
        )
    # 与 capabilities/knowledge_base/dtos/search.py 的 le=50 对齐：
    # 51-100 若放行会被后端 400 拒绝，再被 call_gateway 误映射成
    # 「知识库服务请求参数有误」，调用方看不出真正的边界。
    if not 1 <= top_k <= 50:
        raise McpToolError(code=-32602, message="top_k 必须在 1-50 之间")

    auth = require_mcp_auth("view")
    kb_ids = await _check_service_scope(auth, service_id, required_permission="call")

    return query, mode, auth, kb_ids


@_inject_mcp_tool("search_ontology")
async def search_ontology(
    service_id: str,
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
    strict_mode: bool = False,
    with_reasoning: bool = False,
) -> dict:
    """本体优先检索——实体匹配→1-hop 邻域→RAG fallback。

    Action: query_with_ontology
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    三步检索管线：1) 实体匹配 2) 1-hop 邻居遍历 3) RAG 融合。
    支持严格模式（strict_mode=True），多次验证循环确保可靠性。

    Args:
        service_id: 领域服务 ID
        query: 搜索查询（最多 2000 字符）
        mode: 搜索模式（naive / local / global / hybrid，默认 "hybrid"）
        top_k: 返回结果数（默认 5，1-50）
        strict_mode: 是否启用严格模式多次验证（默认 False）
        with_reasoning: 是否返回推理链追踪（默认 False）

    Returns:
        dict: 搜索结果，含 answer、references、reasoning 字段

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空值 / 长度超限 / mode 不合法 / top_k 超范围）
        McpToolError(code=-32000): 知识库服务不可用
    """
    query_norm, mode_norm, auth, kb_ids = await _validate_search_params(service_id, query, mode, top_k)
    payload: dict = {
        "service_id": service_id,
        "query": query_norm,
        "mode": mode_norm,
        "top_k": top_k,
        "strict_mode": strict_mode,
        "with_reasoning": with_reasoning,
        # MCP 检索固定不写搜索历史（不提供调用方开关）
        "save_history": False,
    }
    if kb_ids:
        payload["knowledge_base_ids"] = kb_ids
    return await _call_search_gateway("query_with_ontology", auth, payload)


@_inject_mcp_tool("search_deep")
async def search_deep(
    service_id: str,
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
    strict_mode: bool = False,
    with_reasoning: bool = False,
) -> dict:
    """深度查询——多轮分解→子问题查询→归并答案。

    Action: deep_query
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    将复杂问题分解为多个子问题，依次查询后归并生成综合答案。
    支持严格模式（strict_mode=True），多次验证循环确保可靠性。
    常规查询请优先使用 search_ontology（本体检索）或 search_llmwiki（OpenKB 检索）。

    Args:
        service_id: 领域服务 ID
        query: 搜索查询（最多 2000 字符）
        mode: 搜索模式（naive / local / global / hybrid，默认 "hybrid"）
        top_k: 返回结果数（默认 5，1-50）
        strict_mode: 是否启用严格模式多次验证（默认 False）
        with_reasoning: 是否返回推理链追踪（默认 False）

    Returns:
        dict: 搜索结果，含 answer、references、reasoning 字段

    Raises:
        McpToolError(code=-32000): 知识库服务不可用
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空值 / 长度超限 / mode 不合法 / top_k 超范围）
    """
    query_norm, mode_norm, auth, kb_ids = await _validate_search_params(service_id, query, mode, top_k)
    payload: dict = {
        "service_id": service_id,
        "query": query_norm,
        "mode": mode_norm,
        "top_k": top_k,
        "strict_mode": strict_mode,
        "with_reasoning": with_reasoning,
        # MCP 检索固定不写搜索历史（不提供调用方开关）
        "save_history": False,
    }
    if kb_ids:
        payload["knowledge_base_ids"] = kb_ids
    return await _call_search_gateway("deep_query", auth, payload)


@_inject_mcp_tool("search_llmwiki")
async def search_llmwiki(
    service_id: str,
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
    with_reasoning: bool = False,
) -> dict:
    """OpenKB Wiki 检索——仅适用于 OpenKB 管线知识库，不适用于 LightRAG 管线。

    Action: search_llmwiki (fallback: search_mix)
    Guard: SEARCH_LLMWIKI_ENABLED flag + require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    通过 OpenKB 编译产物进行 Wiki 风格检索。
    若管线不匹配（code=-32602），自动 fallback 到 search_mix。
    不支持 strict_mode（OpenKB 编译产物无严格模式语义）。

    Args:
        service_id: 领域服务 ID
        query: 搜索查询（最多 2000 字符）
        mode: 搜索模式（naive / local / global / hybrid，默认 "hybrid"）
        top_k: 返回结果数（默认 5，1-50）
        with_reasoning: 是否返回推理链追踪（默认 False）

    Returns:
        dict: 搜索结果，含 answer、references、reasoning 字段

    Raises:
        McpToolError(code=-32000): LLM-Wiki 检索未开放（feature flag 关闭） / 知识库服务不可用
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空值 / 长度超限 / mode 不合法 / top_k 超范围）
    """
    if not SEARCH_LLMWIKI_ENABLED:
        raise McpToolError(code=-32000, message="LLM-Wiki 检索暂未开放")
    query_norm, mode_norm, auth, kb_ids = await _validate_search_params(service_id, query, mode, top_k)
    payload: dict = {
        "service_id": service_id,
        "query": query_norm,
        "mode": mode_norm,
        "top_k": top_k,
        "with_reasoning": with_reasoning,
        # MCP 检索固定不写搜索历史（不提供调用方开关）
        "save_history": False,
    }
    if kb_ids:
        payload["knowledge_base_ids"] = kb_ids
    try:
        return await _call_search_gateway("search_llmwiki", auth, payload)
    except McpToolError as e:
        # 管线不匹配 (-32602，服务含 lightrag 库) 或 OpenKB 运行时不可用 (-32000)
        # → fallback 到 search_mix 统一入口（按 kb_type 分组扇出，正确处理混合库）
        if e.code in (-32602, _ERROR_INTERNAL):
            logger.info(
                "search_llmwiki 不可用（code=%s），自动回退到 search_mix: service_id=%s",
                e.code,
                service_id,
            )
            return await _call_search_gateway("search_mix", auth, payload)
        raise


@_inject_mcp_tool("search_jonex_wiki")
async def search_jonex_wiki(
    service_id: str,
    query: str,
    with_reasoning: bool = False,
) -> dict:
    """Jonex Wiki 检索——仅适用于 jonex_wiki 管线知识库。

    Action: search_jonex_wiki (fallback: search_mix)
    Guard: SEARCH_JONEX_WIKI_ENABLED flag + require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    通过 jonex_wiki 编译产物进行 Wiki 风格检索。
    若管线不匹配（code=-32602），自动 fallback 到 search_mix。
    不支持 strict_mode（jonex_wiki 编译产物无严格模式语义）。

    Args:
        service_id: 领域服务 ID
        query: 搜索查询（最多 2000 字符）
        with_reasoning: 是否返回推理链追踪（默认 False）

    Returns:
        dict: 搜索结果，含 answer、references、reasoning 字段

    Raises:
        McpToolError(code=-32000): jonex_wiki 检索未开放（feature flag 关闭） / 知识库服务不可用
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空值 / 长度超限）
    """
    if not SEARCH_JONEX_WIKI_ENABLED:
        raise McpToolError(code=-32000, message="Jonex Wiki 检索暂未开放")
    query_norm, _, auth, kb_ids = await _validate_search_params(service_id, query, "hybrid", 5)
    payload: dict = {
        "service_id": service_id,
        "query": query_norm,
        "with_reasoning": with_reasoning,
        # MCP 检索固定不写搜索历史（不提供调用方开关）
        "save_history": False,
    }
    if kb_ids:
        payload["knowledge_base_ids"] = kb_ids
    try:
        return await _call_search_gateway("search_jonex_wiki", auth, payload)
    except McpToolError as e:
        # 管线不匹配 (-32602，服务含非 jonex_wiki 库) 或 jonex_wiki 运行时不可用 (-32000)
        # → fallback 到 search_mix 统一入口（按 kb_type 分组扇出，正确处理混合库）
        if e.code in (-32602, _ERROR_INTERNAL):
            logger.info(
                "search_jonex_wiki 不可用（code=%s），自动回退到 search_mix: service_id=%s",
                e.code,
                service_id,
            )
            return await _call_search_gateway("search_mix", auth, payload)
        raise


@_inject_mcp_tool("search_mix")
async def search_mix(
    service_id: str,
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
    with_reasoning: bool = False,
) -> dict:
    """混合管线检索统一入口——同时查询 LightRAG 和 OpenKB 管线知识库。

    Action: search_mix
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    自动按 pipeline_type 分组扇出到 LightRAG / OpenKB，支持混合选库。
    无论 OpenKB 是否可用，均正常返回（纯 LightRAG 知识库不受影响）。

    Args:
        service_id: 领域服务 ID
        query: 搜索查询（最多 2000 字符）
        mode: 搜索模式（naive / local / global / hybrid，默认 "hybrid"）
        top_k: 返回结果数（默认 5，1-50）
        with_reasoning: 是否返回推理链追踪（默认 False）

    Returns:
        dict: 搜索结果，含 answer、references、reasoning 字段

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空值 / 长度超限 / mode 不合法 / top_k 超范围）
    """
    query_norm, mode_norm, auth, kb_ids = await _validate_search_params(service_id, query, mode, top_k)
    payload: dict = {
        "service_id": service_id,
        "query": query_norm,
        "mode": mode_norm,
        "top_k": top_k,
        "with_reasoning": with_reasoning,
        # MCP 检索固定不写搜索历史（不提供调用方开关）
        "save_history": False,
    }
    if kb_ids:
        payload["knowledge_base_ids"] = kb_ids
    return await _call_search_gateway("search_mix", auth, payload)


@_inject_mcp_tool("read_source")
async def read_source(
    service_id: str,
    document_id: str,
) -> dict:
    """获取文档源文件的预签名 URL。以领域服务为入口，自动解析文档所属知识库。

    Action: get_raw_url
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id)

    不经过 MCP 传输大文件内容——仅返回预签名 URL。

    遍历 service 下的所有 KB，依次尝试查找文档，找到后返回预签名 URL。
    若文档不在任何 KB 中，raise McpToolError。

    Args:
        service_id: 领域服务 ID
        document_id: 文档 ID

    Returns:
        dict: 含 url 字段（文档预签名下载 URL）

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32000): 文档不属于该服务下的任何知识库
    """
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not document_id or not document_id.strip():
        raise McpToolError(code=-32602, message="document_id 为必填参数，不能为空")
    auth = require_mcp_auth("view")
    intersection = await _check_service_scope(auth, service_id, required_permission="call")
    touch_service_call_async(auth.tenant_id, service_id.strip())

    if intersection:
        kb_ids_to_try = intersection
    else:
        # allowed_kb_ids 为空 = 全部允许，需要获取 service 的所有 kb_ids。
        # 前置校验调用不上报工具名（语义归属在 Gateway 的
        # _INTERNAL_CHECK_ACTIONS；这里 report_tool=False 是双保险）。
        service_detail = await call_gateway(
            "get_service",
            auth.tenant_id,
            auth.key_id,
            {"service_id": service_id},
            report_tool=False,
        )
        kb_ids_to_try = service_detail.get("kb_ids", [])

    for kb_id in kb_ids_to_try:
        try:
            return await call_gateway(
                "get_raw_url",
                auth.tenant_id,
                auth.key_id,
                {
                    "knowledge_base_id": kb_id,
                    "document_id": document_id,
                },
            )
        except McpToolError:
            continue

    raise McpToolError(code=-32000, message="文档不属于该服务下的任何知识库")


# ============================================================================
# Block 4: __all__ 导出
# ============================================================================

__all__ = [
    "call_gateway",
    "call_gateway_ext",
    "get_http_client",
    "_check_service_scope",
    "_validate_file_name",
    "confirm_upload",
    "generate_upload_url",
    "get_upload_status",
    "list_documents",
    "list_domain_services",
    "list_ontology_entity_types",
    "list_ontology_instances",
    "expand_ontology_neighbors",
    "read_source",
    "submit_search_feedback",
    "search_deep",
    "search_jonex_wiki",
    "search_llmwiki",
    "search_ontology",
]
