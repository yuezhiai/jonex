"""平台监控三族读接口共用的降级保护层（``_resilient``）。

三族（调用 C / Token K / 任务 T）此前各抄一份装饰器，语义已出现分叉风险。本模块
收口为唯一实现：

- **数据源缺失 / 重算未完成 ⇒ 4100（503）**：表 / 列 / schema 不存在，说明监控
  数据源根本不在位，不能伪装成「查询没命中」的 200 空壳；由 SQLSTATE
  ``42P01``（undefined_table）/ ``42703``（undefined_column）/ ``3F000``
  （invalid_schema_name）识别。跨 schema 查询（``task_center`` / ``metering``）
  缺表同样靠这一路径识别。
- **普通查询失败 ⇒ 200 等结构空壳**：连接抖动、权限、超时等仍按原口径降级，
  只置 ``flags.module_error``，绝不 5xx —— 一次抖动不能让整页变 503。
- **不吞业务异常**：``JonexException``（1001 参数非法 / 1002 资源不存在 / 4004
  超范围 …）原样向上抛，被降级掩盖会变成「静默返回空数据」。

「历史重算未完成」当前没有可检测来源（``recalc_status`` 恒 ``None``、无重算任务），
4100 目前只由「数据源缺失」触发；不伪造状态。
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Iterator, Optional

from jonex_core.common.exceptions import JonexException, MonitorDataUnavailableError

logger = logging.getLogger(__name__)

#: 降级态 ``module_error`` 取值（脱敏：只标「本模块暂不可用」，不带底层异常内容）。
MODULE_ERROR = "module_unavailable"

#: 视为「监控数据源缺失」的 SQLSTATE。
#: - ``42P01`` undefined_table
#: - ``42703`` undefined_column
#: - ``3F000`` invalid_schema_name
_DATA_SOURCE_MISSING_SQLSTATES = frozenset({"42P01", "42703", "3F000"})

#: SQLSTATE 在驱动异常上的常见属性名（asyncpg / psycopg2）。
_SQLSTATE_ATTRS = ("sqlstate", "pgcode")

#: 异常链的遍历起点（SQLAlchemy 的 ``orig`` 与 Python 的显式/隐式因果）。
_CAUSE_ATTRS = ("orig", "__cause__", "__context__")


def _iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """广度优先遍历异常链（去重，防环）。"""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for attr in _CAUSE_ATTRS:
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                stack.append(nested)


def extract_sqlstate(exc: BaseException) -> Optional[str]:
    """从异常链里取第一个非空 SQLSTATE；取不到返回 ``None``。"""
    for item in _iter_exception_chain(exc):
        for attr in _SQLSTATE_ATTRS:
            value = getattr(item, attr, None)
            if isinstance(value, str) and value:
                return value
    return None


def is_data_source_missing(exc: BaseException) -> bool:
    """表 / 列 / schema 不存在 ⇒ 监控数据源确实缺失（应抛 4100）。"""
    return extract_sqlstate(exc) in _DATA_SOURCE_MISSING_SQLSTATES


def resilient(empty_factory):
    """监控读接口降级装饰器。

    ``empty_factory(*args, **kwargs)`` 必须返回与正常态**逐字段对齐**的空壳，
    前端不需要为降级单独写分支（仅凭 ``flags.module_error`` 非空区分）。
    """

    def decorator(method):
        @wraps(method)
        async def wrapper(self, *args, **kwargs):
            try:
                return await method(self, *args, **kwargs)
            except JonexException:
                raise
            except Exception as exc:  # noqa: BLE001 — 读接口失败不得变成 5xx，但必须留日志
                sqlstate = extract_sqlstate(exc)
                if sqlstate in _DATA_SOURCE_MISSING_SQLSTATES:
                    logger.error(
                        "[jonex] 监控数据源缺失：method=%s sqlstate=%s",
                        method.__name__,
                        sqlstate,
                        exc_info=True,
                    )
                    raise MonitorDataUnavailableError(
                        details={
                            "reason": "data_source_missing",
                            "sqlstate": sqlstate,
                        }
                    ) from exc
                logger.error(
                    "[jonex] 监控读接口降级（返回空壳）：method=%s",
                    method.__name__,
                    exc_info=True,
                )
                return empty_factory(*args, **kwargs)

        return wrapper

    return decorator


__all__ = [
    "MODULE_ERROR",
    "extract_sqlstate",
    "is_data_source_missing",
    "resilient",
]
