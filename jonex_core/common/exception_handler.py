#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 全局异常处理器

提供 FastAPI 全局异常处理函数，统一异常到 HTTP 响应的映射
"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from jonex_core.common.exceptions import JonexException, InternalError
from jonex_core.common.response import error_response
from jonex_core.common.i18n import translate
from jonex_core.common.logger import get_logger

logger = get_logger("exception_handler")


def _resolve_error(exc: JonexException):
    """返回 (code, params, fallback, localize) 供 error_response 使用。

    三分支：
    ① 有结构化 params：走模板本地化（含变量的消息也能翻译）
    ② 有显式 message 但无 params：历史/动态原文，原样返回避免丢信息
    ③ 纯默认：按 code 本地化
    """
    if getattr(exc, "params", None):
        return exc.code, exc.params, (exc.message or exc.default_message), True
    if getattr(exc, "_message_explicit", False):
        return exc.code, None, exc.message, False
    return exc.code, None, exc.default_message, True


async def jonex_exception_handler(request: Request, exc: JonexException) -> JSONResponse:
    """
    处理Jonex 平台自定义异常

    Args:
        request: FastAPI 请求对象
        exc: JonexException 异常实例

    Returns:
        统一格式的错误响应
    """
    request_id = getattr(request.state, "request_id", "N/A")
    # 日志固定用原文（default_message / 显式原文），保证可检索
    logger.warning(
        f"[{request_id}] 业务异常: code={exc.code}, message={exc.message}, "
        f"path={request.url.path}"
    )

    if exc.cause:
        logger.debug(f"[{request_id}] 原始异常: {exc.cause}")

    code, params, fallback, localize = _resolve_error(exc)
    return error_response(
        code=code,
        message=fallback,
        params=params,
        localize=localize,
        request_id=request_id,
        status_code=exc.status_code,
        details=exc.details if exc.details else None,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    处理 FastAPI HTTPException

    Args:
        request: FastAPI 请求对象
        exc: HTTPException 异常实例

    Returns:
        统一格式的错误响应
    """
    request_id = getattr(request.state, "request_id", "N/A")
    logger.warning(
        f"[{request_id}] HTTP 异常: status={exc.status_code}, detail={exc.detail}, "
        f"path={request.url.path}"
    )

    return error_response(
        code=exc.status_code,
        message=str(exc.detail),
        request_id=request_id,
        status_code=exc.status_code,
        localize=False,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    处理请求参数验证错误（pydantic 校验异常）

    Args:
        request: FastAPI 请求对象
        exc: RequestValidationError 异常实例

    Returns:
        统一格式的错误响应
    """
    request_id = getattr(request.state, "request_id", "N/A")
    errors = exc.errors()
    logger.warning(
        f"[{request_id}] 参数验证失败: {len(errors)} 个错误, path={request.url.path}"
    )

    # 用独立字符串键避免与 1001 "请求参数无效" 冲突造成 zh-CN 回归
    validation_msg = translate("__validation_failed__", fallback="请求参数验证失败")
    return error_response(
        code=1001,
        message=validation_msg,
        request_id=request_id,
        status_code=422,
        details={"validation_errors": errors},
        localize=False,
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    处理所有未捕获的异常（兜底）

    Args:
        request: FastAPI 请求对象
        exc: 异常实例

    Returns:
        统一格式的 500 错误响应
    """
    request_id = getattr(request.state, "request_id", "N/A")
    logger.exception(
        f"[{request_id}] 未捕获异常: {type(exc).__name__}: {exc}, "
        f"path={request.url.path}"
    )

    internal_error = InternalError(
        message="服务器内部错误，请稍后重试",
        cause=exc,
    )

    return error_response(
        code=internal_error.code,
        message=internal_error.message,
        request_id=request_id,
        status_code=internal_error.status_code,
        localize=True,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    向 FastAPI 应用注册所有全局异常处理器

    Args:
        app: FastAPI 应用实例
    """
    # 业务异常（最具体）
    app.add_exception_handler(JonexException, jonex_exception_handler)
    # HTTP 异常
    app.add_exception_handler(HTTPException, http_exception_handler)
    # 参数验证异常
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    # 兜底异常处理器
    app.add_exception_handler(Exception, general_exception_handler)

    logger.info("全局异常处理器已注册")
