#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
悦溪平台 - MCP Server 数据库连接模块

基于 asyncpg 原始连接池，支持：
- 异步数据库操作
- 连接池管理（min=2, max=5）
"""
import json

import asyncpg

from config import settings

# ==================== 懒加载连接池 ====================
_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """注册 JSON/JSONB codec，使 asyncpg 自动反序列化为 Python 对象。

    不注册 codec 时 asyncpg 将 JSONB 列作为原始字符串返回（如 '[]' 而非 []），
    导致 truthiness 判断异常（非空字符串 '[]' 为 True）→ require_kb_scope 误判。
    """
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool() -> asyncpg.Pool:
    """创建 asyncpg 连接池（min_size=2, max_size=5）"""
    global _pool
    if _pool is not None:
        await _pool.close()
    _pool = await asyncpg.create_pool(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USERNAME,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        min_size=2,
        max_size=5,
        init=_init_connection,
    )
    return _pool


async def get_pool() -> asyncpg.Pool:
    """获取已初始化的连接池"""
    if _pool is None:
        raise RuntimeError("Database pool not initialized")
    return _pool


async def get_service_permission_level(pool, key_id: str, service_id: str) -> str | None:
    """查询 mcp_key_service_mappings 的 permission_level。

    返回 "call" / "view" / None（映射不存在）。层级: call > view。
    （write 已移交写 Key，per jonex-29l，mapping 层不再返回 write）

    Args:
        pool: asyncpg 连接池
        key_id: MCP Key ID
        service_id: 领域服务 ID

    Returns:
        str | None: permission_level 值，或 None 表示映射不存在
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT permission_level FROM platform.mcp_key_service_mappings "
            "WHERE mcp_key_id = $1 AND service_id = $2",
            key_id, service_id,
        )
        return row["permission_level"] if row else None


async def close_pool() -> None:
    """关闭连接池"""
    if _pool:
        await _pool.close()
