#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""W1.1 EngineProfile：引擎能力矩阵与注册表。

设计见 `docs/superpowers/specs/2026-11-01-w1-engine-basis-design.md`。

定位：把「这个引擎能做什么」从散落的 if/字典收成一份**只读事实**，供
schema 生成（无外键的星型）、SQL 生成提示（CTE/窗口/近似聚合）、安全闸
（方言键）与护栏（时序必须带时间过滤）共同读取。

**关档不变**：现有 postgres / mysql / sqlserver 的 dialect 映射与今天
`guard.py::_DIALECT_BY_ENGINE`、`schema_source::normalize_engine` 完全一致；
未知引擎给保守 profile（与今天的保守行为一致）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

#: 规范化引擎键（小写、去空白）。别名在 `normalize()` 里收口。
_ALIASES = {
    "postgresql": "postgres",
    "pg": "postgres",
    "mariadb": "mysql",
    "mssql": "sqlserver",
    "ch": "clickhouse",
    "clickhouse": "clickhouse",
    "influx": "influx_v3",
    "influxdb": "influx_v3",
    "influxdb3": "influx_v3",
    "influx_v3": "influx_v3",
}


@dataclass(frozen=True)
class EngineCapabilities:
    kind: str                      # oltp | olap | time_series | unknown
    dialect: str | None            # sqlglot 方言键；None = 不做方言解析
    cte: bool = True
    window_functions: bool = True
    arrays: bool = False
    approx_aggregates: bool = False
    limit_syntax: str = "limit"    # limit | top | fetch | none
    supports_fk: bool = True
    supports_column_comments: bool = True
    requires_time_filter: bool = False
    #: FULL OUTER JOIN（RATIO/DERIVED 的多输入合并需要）。MySQL 不支持。
    full_outer_join: bool = True


@dataclass(frozen=True)
class EngineProfile:
    id: str                        # 规范化键
    protocol: str                  # http | postgres | mysql | tsql
    capabilities: EngineCapabilities
    default_schema: str | None = None
    quote: str = '"'


#: 现有三家的能力位保持「与今天一致」；OLAP/时序按 spec §5。
_PROFILES: dict[str, EngineProfile] = {
    "postgres": EngineProfile(
        id="postgres", protocol="postgres",
        capabilities=EngineCapabilities(
            kind="oltp", dialect="postgres", arrays=True),
        default_schema="public", quote='"',
    ),
    "mysql": EngineProfile(
        id="mysql", protocol="mysql",
        capabilities=EngineCapabilities(
            kind="oltp", dialect="mysql", arrays=False,
            full_outer_join=False),
        quote="`",
    ),
    "sqlserver": EngineProfile(
        id="sqlserver", protocol="tsql",
        capabilities=EngineCapabilities(
            kind="oltp", dialect="tsql", limit_syntax="top"),
        default_schema="dbo", quote='"',
    ),
    "clickhouse": EngineProfile(
        id="clickhouse", protocol="http",
        capabilities=EngineCapabilities(
            kind="olap", dialect="clickhouse", arrays=True,
            approx_aggregates=True, supports_fk=False),
        default_schema="default", quote='"',
    ),
    "influx_v3": EngineProfile(
        id="influx_v3", protocol="http",
        capabilities=EngineCapabilities(
            kind="time_series", dialect=None, cte=False,
            arrays=False, approx_aggregates=True, supports_fk=False,
            supports_column_comments=False, requires_time_filter=True),
        quote='"',
    ),
}

#: 未知引擎：保守（与今天「方言不明」那一档一致）。
_UNKNOWN = EngineProfile(
    id="", protocol="", capabilities=EngineCapabilities(kind="unknown", dialect=None),
)


def normalize(engine: str | None) -> str:
    key = str(engine or "").strip().lower()
    return _ALIASES.get(key, key)


def register(profile: EngineProfile) -> None:
    """注册/覆盖一个 profile（测试与后续适配器使用）。"""
    _PROFILES[normalize(profile.id)] = replace(profile, id=normalize(profile.id))


def get_profile(engine: str | None) -> EngineProfile:
    return _PROFILES.get(normalize(engine), _UNKNOWN)


def supported_engines() -> list[str]:
    return sorted(_PROFILES)


def dialect_for(engine: str | None) -> str | None:
    return get_profile(engine).capabilities.dialect


__all__ = [
    "EngineCapabilities", "EngineProfile", "normalize", "register",
    "get_profile", "supported_engines", "dialect_for",
]
