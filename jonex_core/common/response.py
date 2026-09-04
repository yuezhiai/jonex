#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 统一响应格式

定义所有 API 接口的标准响应结构，确保前后端契约一致
"""

import json as _json
from dataclasses import dataclass, field, asdict
from typing import Optional, Any, Dict
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


@dataclass
class StandardResponse:
    """标准化响应格式"""
    request_id: str
    success: bool
    code: int
    message: str
    data: Optional[Any] = None
    error_details: Optional[Dict] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict:
        """转为字典（None 字段会被过滤，datetime 自动序列化）"""
        result = asdict(self)
        result = {k: v for k, v in result.items() if v is not None}
        # FastAPI jsonable_encoder handles datetime, Pydantic models, etc.
        return jsonable_encoder(result)

    @classmethod
    def ok(
        cls,
        request_id: str = "",
        data: Any = None,
        message: str = "success",
    ) -> "StandardResponse":
        """成功响应"""
        return cls(
            request_id=request_id,
            success=True,
            code=0,
            message=message,
            data=data,
        )

    @classmethod
    def error(
        cls,
        request_id: str,
        code: int,
        message: str,
        details: Optional[Dict] = None,
    ) -> "StandardResponse":
        """错误响应"""
        return cls(
            request_id=request_id,
            success=False,
            code=code,
            message=message,
            error_details=details,
        )


def success_response(
    data: Any = None,
    message: str = "success",
    request_id: str = "",
    status_code: int = 200,
) -> JSONResponse:
    """
    构造成功的 JSONResponse

    Args:
        data: 响应数据
        message: 成功消息
        request_id: 请求 ID
        status_code: HTTP 状态码

    Returns:
        JSONResponse
    """
    response = StandardResponse.ok(request_id=request_id, data=data, message=message)
    return JSONResponse(status_code=status_code, content=response.to_dict())


def error_response(
    code: int,
    message: str,
    request_id: str = "",
    status_code: int = 500,
    details: Optional[Dict] = None,
    *,
    params: Optional[Dict] = None,
    localize: bool = True,
) -> JSONResponse:
    """
    构造错误的 JSONResponse

    Args:
        code: 业务错误码
        message: 错误信息（fallback 原文）
        request_id: 请求 ID
        status_code: HTTP 状态码
        details: 错误详情
        params: 模板占位符实参（与 code 对应语言包模板配合）
        localize: 是否按 code+params 取模板翻译（False=原样使用 message）

    Returns:
        JSONResponse
    """
    from jonex_core.common.i18n import translate  # 局部导入避免循环依赖

    display = translate(code, params=params, fallback=message) if localize else message
    response = StandardResponse.error(
        request_id=request_id,
        code=code,
        message=display,
        details=details,
    )
    return JSONResponse(status_code=status_code, content=response.to_dict())
