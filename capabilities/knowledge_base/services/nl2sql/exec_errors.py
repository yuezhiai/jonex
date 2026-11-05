#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行期错误的「可修复」判定（B 重试的白名单）。

保守原则：**认识才重试，不认识一律不重试** —— 连接/超时/权限类天然不在白名单，
未知驱动/未知错误号也回 False。只列三类引擎的「名字解析 / 语法」错误：
这些错误的正确名字在模型已得的上下文里（列清单 / joins 图 / 工具），重试有救。

判据**不 import 任何驱动库**（按属性/参数形态判）：
- mssql/pymssql 与 mysql/aiomysql：`exc.args[0]` 是 int（错误号 / errno）；
- pg/asyncpg：`exc.sqlstate`（字符串），`42` 开头 = 语法/访问规则类（42501 insufficient_privilege 除外 —— 权限类不重试）；
- `asyncio.TimeoutError` 等无 args / 无 sqlstate 的形态一律 False。
设计出处：docs/superpowers/specs/2026-10-07-nl2sql-anchor-fallback-design.md §3.1（B 白名单）。
"""
from __future__ import annotations

#: SQL Server：207 Invalid column name / 208 Invalid object name / 102 语法 /
#: 105 未闭合引号 / 156 关键字附近语法错 —— 都是「改对名字/语法就能过」。
_MSSQL_FIXABLE = frozenset({207, 208, 102, 105, 156})

#: MySQL：1054 Unknown column / 1146 Table doesn't exist / 1064 语法。
_MYSQL_FIXABLE = frozenset({1054, 1146, 1064})


def is_fixable_exec_error(exc: BaseException) -> bool:
    """`exec_failed` 的异常 → 是否属于「模型能改对」类（真 = 值得重试一次）。

    **本函数自身不抛**（service 在失败路径上调它，它再炸会把归类出口也带走）。
    """
    try:
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate is not None:
            s = str(sqlstate)
            # class 42 = 语法/访问规则类；唯 42501（insufficient_privilege）是**权限**类 ——
            # 保守制排除（spec 决策 3：连接/超时/权限类不在白名单；权限错改名字救不了）。
            if s.startswith("42") and s != "42501":
                return True
            # 22012 = division_by_zero：加 NULLIF 就能修（2026-10-30）。
            if s == "22012":
                return True
        # 无 sqlstate 的驱动（或包装异常）按消息兜底；只认这一条确定性文案。
        if "division by zero" in str(exc).lower():
            return True
        args = getattr(exc, "args", ()) or ()
        first = args[0] if args else None
        if isinstance(first, bool):
            return False
        if isinstance(first, int):
            return first in _MSSQL_FIXABLE or first in _MYSQL_FIXABLE
        return False
    except Exception:  # noqa: BLE001 —— 判定器自身不许炸
        return False
