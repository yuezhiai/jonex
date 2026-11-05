#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""租户可用性判定 —— 全平台唯一口径。

判定口径与登录链路 ``TenantRepository.list_active_by_ids`` 完全一致：

    platform.tenants.is_deleted = 0 AND platform.tenants.status = 1

* 不校验 ``expire_time``（到期时间下线是另一项工作）。
* 租户不存在 / 已软删 / 已停用一律视为不可用，调用方必须给出**不可区分**的
  失败响应，避免枚举。
* 判定查询失败时异常向上抛出，由调用方 fail-closed（不得当作 active 放行）。

为什么放在 ``jonex_core/common``：Gateway / Sidecar / knowledge_base 三个部署面
都要复用同一语义。这里只用裸 SQL，不 import 任何 capability 的 ORM 模型
（Sidecar 镜像不含 ``capabilities/``）。
"""

from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jonex_core.common.config import get_config
from jonex_core.common.database import SQLALCHEMY_DATABASE_URL, get_db_session
from jonex_core.common.exceptions import TenantIsolationError

#: 唯一租户可用性判定 SQL。
TENANT_ACTIVE_SQL = text(
    "SELECT 1 FROM platform.tenants "
    "WHERE id = :tenant_id AND is_deleted = 0 AND status = 1"
)

#: MCP Key 归属 + 可用性判定（与 ``mcp_server/auth.py::find_by_key_hash`` 同口径，
#: 差别只是按主键 ``k.id`` 而非 ``key_hash`` 查找）。
MCP_KEY_ACTIVE_SQL = text(
    "SELECT 1 FROM platform.mcp_keys k "
    "JOIN platform.tenants t ON t.id = k.tenant_id "
    "WHERE k.id = :mcp_key_id "
    "  AND k.tenant_id = :tenant_id "
    "  AND k.revoked_at IS NULL "
    "  AND k.disabled_at IS NULL "
    "  AND k.is_deleted = 0 "
    "  AND (k.expires_at IS NULL OR k.expires_at > NOW()) "
    "  AND t.is_deleted = 0 "
    "  AND t.status = 1"
)

# ── 小连接池 ─────────────────────────────────────────────────────────────
# Gateway 以 4 worker 启动，默认 DB 池是 50+100/进程，直接复用会把 PG
# （默认 max_connections=100）压满。MCP 路每次调用只做一次主键探测，用
# 5+5 的小池足够，且 4 worker 合计上限 40。
_SMALL_POOL_SIZE = 5
_SMALL_MAX_OVERFLOW = 5

_small_engine = None
_small_session_factory = None


def _get_small_session_factory() -> async_sessionmaker:
    global _small_engine, _small_session_factory
    if _small_session_factory is None:
        cfg = get_config()
        _small_engine = create_async_engine(
            SQLALCHEMY_DATABASE_URL,
            pool_size=_SMALL_POOL_SIZE,
            max_overflow=_SMALL_MAX_OVERFLOW,
            pool_timeout=cfg.DB_POOL_TIMEOUT,
            pool_recycle=cfg.DB_POOL_RECYCLE,
            pool_pre_ping=True,
            isolation_level="READ COMMITTED",
            future=True,
        )
        _small_session_factory = async_sessionmaker(
            bind=_small_engine,
            class_=AsyncSession,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _small_session_factory


@asynccontextmanager
async def get_small_db_session():
    """获取小池会话（只读探测用，调用方负责不写）。"""
    factory = _get_small_session_factory()
    session = factory()
    try:
        yield session
    finally:
        await session.close()


async def _probe(session: AsyncSession, sql, params: dict) -> bool:
    row = (await session.execute(sql, params)).first()
    return row is not None


async def is_tenant_active(
    tenant_id: Optional[str],
    session: Optional[AsyncSession] = None,
) -> bool:
    """租户是否为 active（存在、未软删、未停用）。

    ``session`` 传入时复用调用方会话（便于事务内测试 / 合并查询），
    否则使用进程默认连接池。
    """
    if not tenant_id:
        return False
    if session is not None:
        return await _probe(session, TENANT_ACTIVE_SQL, {"tenant_id": tenant_id})
    async with get_db_session() as own_session:
        return await _probe(
            own_session, TENANT_ACTIVE_SQL, {"tenant_id": tenant_id}
        )


async def require_active_tenant(
    tenant_id: Optional[str],
    session: Optional[AsyncSession] = None,
) -> None:
    """租户不可用时抛 ``TenantIsolationError``（HTTP 403 / code 3005）。"""
    if not await is_tenant_active(tenant_id, session=session):
        raise TenantIsolationError(message="认证失败")


async def is_mcp_key_active(
    *,
    tenant_id: Optional[str],
    mcp_key_id: Optional[str],
    session: Optional[AsyncSession] = None,
) -> bool:
    """MCP Key 是否存在、是否属于该租户、是否仍可用。

    同时校验 Key 自身状态（撤销 / 停用 / 软删 / 过期）与所属租户状态。
    """
    if not tenant_id or not mcp_key_id:
        return False
    params = {"tenant_id": tenant_id, "mcp_key_id": mcp_key_id}
    if session is not None:
        return await _probe(session, MCP_KEY_ACTIVE_SQL, params)
    async with get_small_db_session() as own_session:
        return await _probe(own_session, MCP_KEY_ACTIVE_SQL, params)
