#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 统一异常体系

定义平台所有业务异常的层级结构和错误码规范
错误码规范：
  1xxx - 通用错误
  2xxx - 能力相关错误
  3xxx - 认证授权错误
  4xxx - 数据相关错误
  5xxx - 服务依赖错误
"""

from typing import Optional, Dict, Any


class JonexException(Exception):
    """
    Jonex 平台基础异常类

    所有自定义异常必须继承自该类，便于统一处理
    """
    code: int = 1000
    status_code: int = 500
    default_message: str = "服务器内部错误"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None,
        params: Optional[Dict[str, Any]] = None,
    ):
        """
        Args:
            message: 用户友好的错误信息（显式传入时跳过翻译，作为 fallback）
            details: 额外的错误详情（不包含敏感信息）
            cause: 原始异常（用于链式追踪）
            params: 模板占位符实参，供 translate(code, params=...) 渲染含变量消息
        """
        self.message = message or self.default_message
        self.details = details or {}
        self.cause = cause
        self.params = params or {}
        self._message_explicit = message is not None
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        """转为字典格式（用于响应序列化）"""
        result = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        if self.params:
            result["params"] = self.params
        return result


# ==================== 1xxx 通用错误 ====================
class InternalError(JonexException):
    """内部错误"""
    code = 1000
    status_code = 500
    default_message = "服务器内部错误"


class InvalidParameterError(JonexException):
    """参数无效"""
    code = 1001
    status_code = 400
    default_message = "请求参数无效"


class ResourceNotFoundError(JonexException):
    """资源未找到"""
    code = 1002
    status_code = 404
    default_message = "请求的资源不存在"


class ResourceConflictError(JonexException):
    """资源冲突"""
    code = 1003
    status_code = 409
    default_message = "资源状态冲突"


class OperationNotSupportedError(JonexException):
    """操作不支持"""
    code = 1004
    status_code = 405
    default_message = "操作不被支持"


# ==================== 2xxx 能力相关错误 ====================
class CapabilityError(JonexException):
    """能力错误基类"""
    code = 2000
    status_code = 500
    default_message = "能力调用失败"


class CapabilityNotFoundError(CapabilityError):
    """能力未找到"""
    code = 2001
    status_code = 404
    default_message = "请求的能力不存在"


class CapabilityInvokeError(CapabilityError):
    """能力调用失败"""
    code = 2002
    status_code = 500
    default_message = "能力执行失败"


class CapabilityTimeoutError(CapabilityError):
    """能力调用超时"""
    code = 2003
    status_code = 504
    default_message = "能力调用超时"


class CapabilityValidationError(CapabilityError):
    """能力输入验证失败"""
    code = 2004
    status_code = 400
    default_message = "能力输入参数无效"


class CapabilityIdFormatError(CapabilityError):
    """能力 ID 格式错误"""
    code = 2005
    status_code = 400
    default_message = "能力 ID 格式无效"


# ==================== 3xxx 认证授权错误 ====================
class AuthError(JonexException):
    """认证错误基类"""
    code = 3000
    status_code = 401
    default_message = "认证失败"


class MissingApiKeyError(AuthError):
    """缺少 API Key"""
    code = 3001
    status_code = 401
    default_message = "缺少 X-API-Key 请求头"


class InvalidApiKeyError(AuthError):
    """API Key 无效"""
    code = 3002
    status_code = 401
    default_message = "无效的 API Key"


class InvalidCredentialsError(AuthError):
    """登录凭据无效（用户名或密码错误）"""
    code = 3008
    status_code = 401
    default_message = "用户名或密码错误"


class TokenExpiredError(AuthError):
    """Token 已过期"""
    code = 3003
    status_code = 401
    default_message = "认证 Token 已过期"


class InternalAuthError(AuthError):
    """内部服务认证失败"""
    code = 3004
    status_code = 403
    default_message = "内部服务认证失败"


class TenantIsolationError(AuthError):
    """租户隔离违规"""
    code = 3005
    status_code = 403
    default_message = "无权访问该租户资源"


class PermissionDeniedError(AuthError):
    """权限不足"""
    code = 3006
    status_code = 403
    default_message = "权限不足"


class RateLimitExceededError(AuthError):
    """限流超限"""
    code = 3007
    status_code = 429
    default_message = "请求过于频繁，请稍后重试"


class AccountLockedError(AuthError):
    """账户因多次登录失败被锁定"""
    code = 3009
    status_code = 403
    default_message = "账户已锁定，请稍后重试"


class AccountDisabledError(AuthError):
    """账号被管理员停用（users.status != 1）。

    与 InvalidCredentialsError 区分：本异常只在密码已校验通过后抛出，
    调用方已证明知道正确凭据，据此返回明确原因不构成账号枚举泄漏。
    """
    code = 3013
    status_code = 403
    default_message = "账号已停用，请联系管理员"


class ImpersonationForbiddenError(AuthError):
    """租户模拟被拒绝（无 platform:admin 或嵌套切换）"""
    code = 3010
    status_code = 403
    default_message = "无权进行租户模拟"


class ImpersonationTargetError(AuthError):
    """租户模拟目标非法（不存在 / default 占位 / == 当前租户）"""
    code = 3011
    status_code = 400
    default_message = "租户模拟目标非法"


class NotImpersonatedError(AuthError):
    """非模拟态调用退出模拟端点"""
    code = 3012
    status_code = 400
    default_message = "当前未处于租户模拟态"


# ==================== 4xxx 数据相关错误 ====================
class DataError(JonexException):
    """数据错误基类"""
    code = 4000
    status_code = 500
    default_message = "数据处理失败"


class DatabaseError(DataError):
    """数据库错误"""
    code = 4001
    status_code = 500
    default_message = "数据库操作失败"


class CacheError(DataError):
    """缓存错误"""
    code = 4002
    status_code = 500
    default_message = "缓存操作失败"


class DataIntegrityError(DataError):
    """数据完整性错误"""
    code = 4003
    status_code = 400
    default_message = "数据完整性约束违反"


class QuotaExceededError(DataError):
    """配额已用尽（阻断创建/上传）。

    details 固定携带需求字段：
    quotaKey / used / reserved / limit / unit。
    """
    code = 4004
    status_code = 409
    default_message = "配额已用尽"


# ==================== 5xxx 服务依赖错误 ====================
class ServiceError(JonexException):
    """服务错误基类"""
    code = 5000
    status_code = 503
    default_message = "服务不可用"


class ServiceUnavailableError(ServiceError):
    """服务不可用"""
    code = 5001
    status_code = 503
    default_message = "依赖服务不可用"


class ServiceDiscoveryError(ServiceError):
    """服务发现错误"""
    code = 5002
    status_code = 503
    default_message = "服务发现失败"


class UpstreamServiceError(ServiceError):
    """上游服务错误"""
    code = 5003
    status_code = 502
    default_message = "上游服务返回错误"


class ServiceTimeoutError(ServiceError):
    """服务调用超时"""
    code = 5004
    status_code = 504
    default_message = "服务调用超时"


# ==================== 异常映射工具 ====================
EXCEPTION_REGISTRY: Dict[int, type] = {
    # 通用错误（支持平台错误码和 HTTP 状态码双映射）
    400: InvalidParameterError,
    1000: InternalError,
    1001: InvalidParameterError,
    1002: ResourceNotFoundError,
    1003: ResourceConflictError,
    1004: OperationNotSupportedError,
    # 能力相关
    2000: CapabilityError,
    2001: CapabilityNotFoundError,
    2002: CapabilityInvokeError,
    2003: CapabilityTimeoutError,
    2004: CapabilityValidationError,
    2005: CapabilityIdFormatError,
    # 认证授权
    3000: AuthError,
    3001: MissingApiKeyError,
    3002: InvalidApiKeyError,
    3003: TokenExpiredError,
    3004: InternalAuthError,
    3005: TenantIsolationError,
    3006: PermissionDeniedError,
    3007: RateLimitExceededError,
    3008: InvalidCredentialsError,
    3010: ImpersonationForbiddenError,
    3011: ImpersonationTargetError,
    3012: NotImpersonatedError,
    # 数据相关
    4000: DataError,
    4001: DatabaseError,
    4002: CacheError,
    4003: DataIntegrityError,
    4004: QuotaExceededError,
    # 服务依赖
    5000: ServiceError,
    5001: ServiceUnavailableError,
    5002: ServiceDiscoveryError,
    5003: UpstreamServiceError,
    5004: ServiceTimeoutError,
}


def get_exception_class(code: int) -> type:
    """
    根据错误码获取对应的异常类

    Args:
        code: 错误码

    Returns:
        异常类
    """
    return EXCEPTION_REGISTRY.get(code, JonexException)
