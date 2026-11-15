#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行期错误的分类（N0-2）：拒绝/超时优先，其次才是「可修复」白名单。

分类顺序是**安全语义**，不是偏好；先用 errno / SQLSTATE / 异常类型等**结构证据**，
结构都不命中时才看文案（否则 `Unknown column 'timeout'` 会被超时词误伤）：

1. `permission`：命令/对象级拒绝（errno 1142/1143/1227/1370、229/262/300/916、
   SQLSTATE 42501）。即使 SQLSTATE 是 `42000`（MySQL 的语法类前缀），结构权限码
   也必须先判权限 —— 改名字救不了权限，不能重试。
2. `timeout`：`asyncio.TimeoutError` / `TimeoutError`、SQLSTATE 57014；不重试。
3. `connection`：连接/认证类（08 前缀、28000/28P01、1044/1045、18456）；不重试。
4. `syntax`：名称解析/语法类（原 B 白名单）；这是唯一可重试的一类。
5. `unknown`：其余一律不重试。

对外只暴露 `safe_message`；原始异常文本留在审计/观测的 `exec_error.detail`。
判据**不 import 任何驱动库**（按属性/参数形态判），且本模块自身不抛。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

#: SQL Server：207 Invalid column name / 208 Invalid object name / 102 语法 /
#: 105 未闭合引号 / 156 关键字附近语法错 —— 都是「改对名字/语法就能过」。
_MSSQL_FIXABLE = frozenset({207, 208, 102, 105, 156})

#: MySQL：1054 Unknown column / 1146 Table doesn't exist / 1064 语法。
_MYSQL_FIXABLE = frozenset({1054, 1146, 1064})

#: 权限/拒绝类 errno（MySQL / SQL Server）：命令/对象级拒绝。
_PERMISSION_ERRNOS = frozenset({
    1142, 1143, 1227, 1370,               # MySQL: command denied / access denied
    229, 262, 300, 916,                   # SQL Server: permission / denied
})

#: 权限类 SQLSTATE（PG / 通用）：只留 insufficient_privilege。
_PERMISSION_STATES = frozenset({"42501"})

#: 连接/认证类 SQLSTATE 前缀 / errno（认证与连接归一类）。
_CONNECTION_STATES = ("08", "28000", "28P01")
_CONNECTION_ERRNOS = frozenset({
    1044, 1045,                           # MySQL: access denied (auth/db)
    20009, 18456, 4060,                   # SQL Server: connect / login failed
})

_PERMISSION_WORDS = (
    "permission denied", "not authorized", "insufficient privilege",
    "command denied", "拒绝访问", "没有权限", "权限不足",
)
_TIMEOUT_WORDS = ("timeout", "timed out", "超时")
_CONNECTION_WORDS = (
    "could not connect", "connection refused", "connection reset",
    "connection closed", "server not available", "access denied",
    "login failed", "authentication failed", "无法连接", "连接失败", "认证失败",
)


@dataclass(frozen=True)
class ExecErrorInfo:
    """一次执行失败的分类结果。`detail` 只进审计/观测，不进对外响应。"""

    category: str          # permission | timeout | connection | syntax | unknown
    retryable: bool
    safe_message: str
    detail: str = ""


_SAFE_MESSAGES = {
    "permission": "没有权限访问目标数据",
    "timeout": "查询超时（数据源未在限定时间内返回）",
    "connection": "无法连接或认证数据源",
    "syntax": "SQL 名称或语法错误",
    "unknown": "SQL 执行失败",
}


def _errno(exc: BaseException) -> int | None:
    args = getattr(exc, "args", ()) or ()
    first = args[0] if args else None
    if isinstance(first, bool) or not isinstance(first, int):
        return None
    return first


def _sqlstate(exc: BaseException) -> str:
    value = getattr(exc, "sqlstate", None)
    if value is None:
        return ""
    return str(value).strip().upper()


def _message(exc: BaseException) -> str:
    return str(exc or "").lower()


def classify_exec_error(exc: BaseException) -> ExecErrorInfo:
    """异常 → 分类；**本函数自身不抛**（失败路径不能再炸一次）。"""
    try:
        errno = _errno(exc)
        sqlstate = _sqlstate(exc)
        message = _message(exc)

        # 先用**结构证据**判：errno / SQLSTATE / 异常类型。结构证据优先于文案，
        # 否则 `Unknown column 'timeout'` 会被超时词误伤、丢掉 B 重试。
        if errno in _PERMISSION_ERRNOS or sqlstate in _PERMISSION_STATES:
            return ExecErrorInfo("permission", False,
                                 _SAFE_MESSAGES["permission"], str(exc)[:200])
        if (isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                or sqlstate == "57014"):
            return ExecErrorInfo("timeout", False,
                                 _SAFE_MESSAGES["timeout"], str(exc)[:200])
        if (errno in _CONNECTION_ERRNOS
                or any(sqlstate.startswith(prefix) for prefix in _CONNECTION_STATES)):
            return ExecErrorInfo("connection", False,
                                 _SAFE_MESSAGES["connection"], str(exc)[:200])
        if sqlstate.startswith("42") or sqlstate == "22012":
            return ExecErrorInfo("syntax", True,
                                 _SAFE_MESSAGES["syntax"], str(exc)[:200])
        if errno in _MSSQL_FIXABLE or errno in _MYSQL_FIXABLE:
            return ExecErrorInfo("syntax", True,
                                 _SAFE_MESSAGES["syntax"], str(exc)[:200])

        # 无结构证据时才看文案（驱动包装异常）；权限 > 超时 > 连接。
        if any(word in message for word in _PERMISSION_WORDS):
            return ExecErrorInfo("permission", False,
                                 _SAFE_MESSAGES["permission"], str(exc)[:200])
        if any(word in message for word in _TIMEOUT_WORDS):
            return ExecErrorInfo("timeout", False,
                                 _SAFE_MESSAGES["timeout"], str(exc)[:200])
        if any(word in message for word in _CONNECTION_WORDS):
            return ExecErrorInfo("connection", False,
                                 _SAFE_MESSAGES["connection"], str(exc)[:200])
        if "division by zero" in message:
            return ExecErrorInfo("syntax", True,
                                 _SAFE_MESSAGES["syntax"], str(exc)[:200])

        return ExecErrorInfo("unknown", False,
                             _SAFE_MESSAGES["unknown"], str(exc)[:200])
    except Exception:  # noqa: BLE001 —— 判定器自身不许炸
        return ExecErrorInfo("unknown", False,
                             _SAFE_MESSAGES["unknown"], "")


def is_fixable_exec_error(exc: BaseException) -> bool:
    """`exec_failed` 的异常 → 是否属于「模型能改对」类（真 = 值得重试一次）。"""
    return classify_exec_error(exc).retryable
