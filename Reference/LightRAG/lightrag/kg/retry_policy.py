"""LightRAG PostgreSQL 重试判定（纯标准库实现，可独立导入与测试）。

这里要解决的坑：``TimeoutError`` 不能靠「从可重试元组里删掉」来排除。

* Python 3.10+ 中 ``asyncio.TimeoutError`` 与内建 ``TimeoutError`` 是同一个类；
* 自 Python 3.3 起 ``TimeoutError`` 又是 ``OSError`` 的子类。
* 因此只要元组里还有 ``OSError``，超时仍会被 ``isinstance`` 匹配进来。

旧行为把超时当瞬时故障重试，且每次重试前丢弃整个连接池，一次 60s 超时
会被放大成「多次尝试 + 多次拆池」。本模块把判定收敛成一个纯函数：

* 超时（``asyncio.TimeoutError`` / ``TimeoutError``）→ 不可重试，立即上抛；
* 连接类瞬时故障（``ConnectionError``、非超时的 ``OSError``，以及调用方
  追加的 asyncpg 连接异常）→ 可重试，保持原有语义；
* 其他异常 → 不可重试，与原 ``retry_if_exception_type`` 一致。

模块不依赖 asyncpg / tenacity / numpy，便于在裸环境单独跑测试。
"""

from __future__ import annotations

import asyncio
from typing import Iterable, Tuple, Type

# Python 3.10+ 这两个名字可能指向同一个类；重复不影响 isinstance。
_TIMEOUT_TYPES: Tuple[Type[BaseException], ...] = (
    asyncio.TimeoutError,
    TimeoutError,
)

# 标准库层面的瞬时连接故障。仍保留 OSError：非超时的 OSError（如连接重置）
# 本就应该重试，超时由 is_timeout() 在 is_retryable() 里先行排除。
DEFAULT_RETRYABLE_TYPES: Tuple[Type[BaseException], ...] = (
    ConnectionError,
    OSError,
)


def is_timeout(exc: BaseException | None) -> bool:
    """``exc`` 是否为超时类异常。"""
    return isinstance(exc, _TIMEOUT_TYPES)


def is_retryable(
    exc: BaseException,
    retryable_types: Iterable[Type[BaseException]] | None = None,
) -> bool:
    """判断 ``exc`` 是否应进入重试循环。

    Args:
        exc: 待判定的异常实例。
        retryable_types: 可重试异常类型白名单（如 asyncpg 的连接异常）。
            传 ``None`` 时使用 :data:`DEFAULT_RETRYABLE_TYPES`。

    Returns:
        可重试返回 ``True``；超时与白名单之外的异常返回 ``False``，
        由 tenacity 的 ``reraise=True`` 立即上抛，不再拆连接池。
    """
    if is_timeout(exc):
        return False
    if retryable_types is None:
        return isinstance(exc, DEFAULT_RETRYABLE_TYPES)
    return isinstance(exc, tuple(retryable_types))
