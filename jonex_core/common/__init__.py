#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 通用工具模块

提供数据库连接、缓存、日志、配置等基础功能
"""

from .config import Settings, get_config, reload_config
from .database import (
    Base,
    get_db,
    get_db_session,
    init_database,
    close_database,
    check_db_health,
    AsyncSessionLocal,
)
from .tenant import (
    DEFAULT_TENANT_IDS,
    TenantContext,
    extract_tenant_id,
    is_default_tenant,
    require_tenant,
    tenant_scope,
)
from .entity import (
    AuditMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
)
from .repository import BaseRepository
from .cache import (
    CacheUtil,
    TenantCache,
    get_redis_client,
    RedisPoolManager,
    check_redis_health,
)
from .vector import (
    MilvusClient,
    get_milvus_client,
    check_milvus_health,
    milvus_context,
    MILVUS_AVAILABLE,
)
from .logger import (
    get_logger,
    setup_logging,
    set_request_id,
    LogContext,
    log_execution_time,
)
from .exceptions import (
    JonexException,
    InternalError,
    InvalidParameterError,
    ResourceNotFoundError,
    ResourceConflictError,
    OperationNotSupportedError,
    CapabilityError,
    CapabilityNotFoundError,
    CapabilityInvokeError,
    CapabilityTimeoutError,
    CapabilityValidationError,
    CapabilityIdFormatError,
    AuthError,
    MissingApiKeyError,
    InvalidApiKeyError,
    InvalidCredentialsError,
    TokenExpiredError,
    InternalAuthError,
    TenantIsolationError,
    PermissionDeniedError,
    RateLimitExceededError,
    DataError,
    DatabaseError,
    CacheError,
    DataIntegrityError,
    ServiceError,
    ServiceUnavailableError,
    ServiceDiscoveryError,
    UpstreamServiceError,
    ServiceTimeoutError,
    get_exception_class,
)
from .response import (
    StandardResponse,
    success_response,
    error_response,
)
from .audit import emit_audit, audit_action
from .exception_handler import register_exception_handlers
from .i18n import (
    LocaleContext,
    extract_locale,
    get_current_locale,
    translate,
    install_locale_middleware,
    transmit_locale_header,
)
from .object_storage import get_object_storage
from .neo4j_client import (
    get_neo4j_driver,
    close_neo4j_driver,
    ensure_ontology_schema,
)

__all__ = [
    # 配置
    "Settings",
    "get_config",
    "reload_config",
    # 数据库
    "Base",
    "get_db",
    "get_db_session",
    "init_database",
    "close_database",
    "check_db_health",
    "TenantContext",
    "AsyncSessionLocal",
    "DEFAULT_TENANT_IDS",
    "extract_tenant_id",
    "is_default_tenant",
    "require_tenant",
    "tenant_scope",
    "AuditMixin",
    "SoftDeleteMixin",
    "TenantMixin",
    "TimestampMixin",
    "BaseRepository",
    # 缓存
    "CacheUtil",
    "TenantCache",
    "get_redis_client",
    "RedisPoolManager",
    "check_redis_health",
    # 向量数据库
    "MilvusClient",
    "get_milvus_client",
    "check_milvus_health",
    "milvus_context",
    "MILVUS_AVAILABLE",
    # 日志
    "get_logger",
    "setup_logging",
    "set_request_id",
    "LogContext",
    "log_execution_time",
    # 异常类
    "JonexException",
    "InternalError",
    "InvalidParameterError",
    "ResourceNotFoundError",
    "ResourceConflictError",
    "OperationNotSupportedError",
    "CapabilityError",
    "CapabilityNotFoundError",
    "CapabilityInvokeError",
    "CapabilityTimeoutError",
    "CapabilityValidationError",
    "CapabilityIdFormatError",
    "AuthError",
    "MissingApiKeyError",
    "InvalidApiKeyError",
    "InvalidCredentialsError",
    "TokenExpiredError",
    "InternalAuthError",
    "TenantIsolationError",
    "PermissionDeniedError",
    "RateLimitExceededError",
    "DataError",
    "DatabaseError",
    "CacheError",
    "DataIntegrityError",
    "ServiceError",
    "ServiceUnavailableError",
    "ServiceDiscoveryError",
    "UpstreamServiceError",
    "ServiceTimeoutError",
    "get_exception_class",
    # 响应格式
    "StandardResponse",
    "success_response",
    "error_response",
    # 异常处理器
    "register_exception_handlers",
    # 国际化
    "LocaleContext",
    "extract_locale",
    "get_current_locale",
    "translate",
    "install_locale_middleware",
    "transmit_locale_header",
    # 审计日志
    "emit_audit",
    "audit_action",
    # 用户认证（__getattr__ 懒加载，避免 security → common 循环导入）
    "require_admin",
    "require_role",
]


def __getattr__(name: str):
    """模块级懒加载，解决 jonex_core.common ↔ jonex_core.security 循环导入。

    common.audit → security.internal_auth → common 是已有循环；
    若在模块顶层 from jonex_core.security.user_auth import require_admin，
    会在 security.__init__ 初始化期间再次进入 common → ImportError。
    用 __getattr__ 将 import 延迟到首次访问时，避开初始化顺序问题。
    """
    if name == "require_admin":
        from jonex_core.security.user_auth import require_admin as _ra
        return _ra
    if name == "require_role":
        from jonex_core.security.user_auth import require_role as _rr
        return _rr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
