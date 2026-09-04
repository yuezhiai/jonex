#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 日志配置模块

支持：
- 多级别日志（DEBUG/INFO/WARNING/ERROR/CRITICAL）
- 控制台输出 + 文件输出
- 日志轮转（按大小、按日期）
- JSON 格式日志（用于日志收集）
- 日志过滤（如健康检查）
- 请求 ID 追踪
"""

import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# 可选依赖：python-json-logger
try:
    from pythonjsonlogger import jsonlogger
    JSON_LOGGER_AVAILABLE = True
except ImportError:
    jsonlogger = None
    JSON_LOGGER_AVAILABLE = False

from jonex_core.common.config import get_config

config = get_config()


# ==================== 日志过滤器 ====================
class HealthCheckFilter(logging.Filter):
    """过滤健康检查请求的日志"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        # 过滤健康检查相关的日志
        if "/health" in message or "health_check" in message:
            return False
        return True


class RequestIdFilter(logging.Filter):
    """注入请求 ID 到日志"""

    def __init__(self, request_id: Optional[str] = None):
        super().__init__()
        self._request_id = request_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = self._request_id or "N/A"
        return True

    def set_request_id(self, request_id: str):
        self._request_id = request_id


# 全局请求 ID 过滤器
request_id_filter = RequestIdFilter()


def set_request_id(request_id: str):
    """设置当前请求的 ID（用于日志追踪）"""
    request_id_filter.set_request_id(request_id)


# ==================== 日志格式配置 ====================
# 文本格式（控制台）
TEXT_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | "
    "%(filename)s:%(lineno)d | %(message)s"
)

# 带 request_id 的文本格式
TEXT_FORMAT_WITH_REQUEST_ID = (
    "%(asctime)s | %(levelname)-8s | %(name)s | "
    "%(request_id)s | %(filename)s:%(lineno)d | %(message)s"
)

# JSON 格式（文件输出，用于 ELK 等日志收集系统）
JSON_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "%(filename)s %(lineno)d %(message)s"
)


def get_log_formatter(json_output: bool = False, include_request_id: bool = False) -> logging.Formatter:
    """
    获取日志格式化器

    Args:
        json_output: 是否输出 JSON 格式
        include_request_id: 是否包含 request_id 字段
    """
    if json_output and JSON_LOGGER_AVAILABLE:
        return jsonlogger.JsonFormatter(
            JSON_FORMAT,
            rename_fields={"levelname": "level", "asctime": "timestamp"},
        )
    else:
        fmt = TEXT_FORMAT_WITH_REQUEST_ID if include_request_id else TEXT_FORMAT
        return logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")


def _build_process_log_path(log_path: str) -> str:
    """
    为本地多进程开发场景生成按进程隔离的日志文件路径。

    这样可以避免 uvicorn reload / 多服务并发时多个进程竞争同一个日志文件，
    导致 Windows 下文件被占用但没有实际写入。
    """
    path = Path(log_path)
    return str(path.with_name(f"{path.stem}.{os.getpid()}{path.suffix}"))


# ==================== 日志配置 ====================
def setup_logging(
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
    json_output: bool = False,
    enable_console: bool = True,
    enable_file: bool = True,
    max_bytes: int = 100 * 1024 * 1024,  # 100MB
    backup_count: int = 10,
):
    """
    配置日志系统

    Args:
        log_level: 日志级别，默认从配置读取
        log_file: 日志文件路径，默认从配置读取
        json_output: 是否输出 JSON 格式
        enable_console: 是否启用控制台输出
        enable_file: 是否启用文件输出
        max_bytes: 单个日志文件最大大小
        backup_count: 保留的日志文件数量
    """
    # 获取根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level or config.LOG_LEVEL)

    # 清除已存在的 handler（避免重复输出）
    root_logger.handlers.clear()

    # 添加请求 ID 过滤器
    root_logger.addFilter(request_id_filter)

    # 1. 控制台输出
    if enable_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(get_log_formatter(json_output=False, include_request_id=False))
        console_handler.addFilter(request_id_filter)
        console_handler.addFilter(HealthCheckFilter())
        root_logger.addHandler(console_handler)

    # 2. 文件输出
    if enable_file:
        log_path = _build_process_log_path(log_file or config.LOG_FILE_PATH)

        # 确保日志目录存在
        log_dir = os.path.dirname(log_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # 按大小轮转
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(get_log_formatter(json_output=json_output, include_request_id=True))
            file_handler.addFilter(request_id_filter)
            root_logger.addHandler(file_handler)

            # 按日期轮转（可选，用于错误日志）
            error_log_path = f"{os.path.splitext(log_path)[0]}_error{os.path.splitext(log_path)[1]}"
            error_file_handler = logging.handlers.TimedRotatingFileHandler(
                error_log_path,
                when="midnight",
                interval=1,
                backupCount=30,
                encoding="utf-8",
            )
            error_file_handler.setLevel(logging.ERROR)
            error_file_handler.setFormatter(get_log_formatter(json_output=json_output, include_request_id=True))
            error_file_handler.addFilter(request_id_filter)
            root_logger.addHandler(error_file_handler)

            # uvicorn / watchfiles 在本地 reload 模式下会给部分命名 logger 单独挂 handler，
            # 这些 logger 不一定再向 root 传播；把文件 handler 显式补到常用 logger 上，
            # 保证 request_id 链路日志能稳定落盘。
            _attach_file_handlers_to_named_loggers(file_handler, error_file_handler)
        except Exception as e:
            logging.warning(f"无法创建文件日志处理器: {e}")

    # 3. 设置第三方库的日志级别
    _set_third_party_log_levels()


def _set_third_party_log_levels():
    """设置第三方库的日志级别，避免日志过多"""
    # HTTP 相关
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    # 数据库
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    # Redis
    logging.getLogger("redis").setLevel(logging.WARNING)

    # HTTP 客户端
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def _attach_file_handlers_to_named_loggers(
    file_handler: logging.Handler,
    error_file_handler: logging.Handler,
):
    logger_names = (
        "api_gateway",
        "exception_handler",
        "jonex_core",
        "capabilities",
        "uvicorn",
        "uvicorn.error",
        "watchfiles",
        "watchfiles.main",
    )

    for name in logger_names:
        named_logger = logging.getLogger(name)
        existing_files = {
            getattr(handler, "baseFilename", None)
            for handler in named_logger.handlers
        }
        if getattr(file_handler, "baseFilename", None) not in existing_files:
            named_logger.addHandler(file_handler)
        if getattr(error_file_handler, "baseFilename", None) not in existing_files:
            named_logger.addHandler(error_file_handler)


# ==================== 获取日志器 ====================
def get_logger(name: str) -> logging.Logger:
    """
    获取日志器

    Args:
        name: 日志器名称，通常使用 __name__

    Returns:
        logging.Logger 实例
    """
    return logging.getLogger(name)


# ==================== 上下文管理器 ====================
class LogContext:
    """日志上下文管理器，临时添加额外字段"""

    def __init__(self, **kwargs):
        self.extra = kwargs
        self.old_factory = logging.getLogRecordFactory()

    def __enter__(self):
        def record_factory(*args, **kwargs):
            record = self.old_factory(*args, **kwargs)
            for key, value in self.extra.items():
                setattr(record, key, value)
            return record

        logging.setLogRecordFactory(record_factory)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logging.setLogRecordFactory(self.old_factory)


# ==================== 性能日志装饰器 ====================
def log_execution_time(logger: logging.Logger, level: int = logging.INFO):
    """
    记录函数执行时间的装饰器

    Args:
        logger: 日志器实例
        level: 日志级别
    """
    def decorator(func):
        import functools
        import time

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.perf_counter()
            try:
                return await func(*args, **kwargs)
            finally:
                elapsed = (time.perf_counter() - start_time) * 1000
                logger.log(
                    level,
                    f"函数 {func.__name__} 执行耗时: {elapsed:.2f}ms"
                )

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed = (time.perf_counter() - start_time) * 1000
                logger.log(
                    level,
                    f"函数 {func.__name__} 执行耗时: {elapsed:.2f}ms"
                )

        return async_wrapper if func.__code__.co_flags & 0x80 else sync_wrapper

    return decorator


# ==================== 初始化 ====================
# 默认初始化日志（使用配置）
try:
    # 本地开发默认同时输出到控制台和文件，便于按 request_id 回溯问题。
    setup_logging(enable_file=True)
except Exception as e:
    # 配置失败时使用基础配置
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    logging.warning(f"使用默认日志配置，自定义配置加载失败: {e}")
