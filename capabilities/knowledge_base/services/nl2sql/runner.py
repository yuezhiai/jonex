#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行一条已经过 guard 的只读 SQL。

这一层只做两件事：把超时从配置里取出来（**唯一的出处**）、把适配器的结果原样交回。
安全判定在 guard、凭据解析在 db_config —— 这一层不重复判据。
"""
from __future__ import annotations

import asyncio

from jonex_core.common.config import config

from ..schema_source.base import SchemaSource  # 仅用于类型标注
from .guard import normalize_dialect_sql


async def run_readonly(source: SchemaSource, cfg: dict, sql: str, *,
                       timeout: float | None = None) -> dict:
    """跑一条只读 SQL。**语句级超时兜在这一层**，异常不吞（由 service 归类成 `exec_failed`）。

    ⚠️ 为什么不能只靠 `run_query(timeout=…)`：三个适配器把 timeout 传给了**连接建立**
    （asyncpg `connect(timeout=)` / aiomysql `connect_timeout` / pymssql `login_timeout`）——
    那只管连得上，**慢查询本身没有上界**：一条打在大表上的 `SELECT` 能把请求挂到天荒地老，
    而连接超时早过了。spec §六 要的是「连接级 + 语句级」两层，语句级就落在这里
    （Task 3 的评审挑出来的偏差）。
    """
    # 执行前的最后一道确定性方言归一：模型偶发写 SQLite 的 logical_and/logical_or，
    # 在 Postgres 上直接 `function does not exist`。放在执行口而不是只放 service，
    # 是因为 service 里还有重试/多轮等分支，任何一条漏掉都会把跨方言函数名带到驱动层。
    sql = normalize_dialect_sql(sql, str(cfg.get("engine") or ""))
    # W4.2：允许按请求覆盖语句级超时（成本护栏）；缺省仍取配置唯一出处。
    try:
        budget = float(timeout) if timeout is not None else float(
            config.NL2SQL_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        budget = float(config.NL2SQL_TIMEOUT_SECONDS)
    if budget <= 0:
        budget = float(config.NL2SQL_TIMEOUT_SECONDS)
    return await asyncio.wait_for(
        source.run_query(cfg, sql, timeout=budget),
        timeout=budget,
    )
