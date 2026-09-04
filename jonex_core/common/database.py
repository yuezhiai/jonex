#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 数据库连接模块

基于 SQLAlchemy 2.0 + asyncpg，支持：
- 异步数据库操作
- 多租户隔离
- 连接池管理
- 读写分离（可选）
- 会话自动管理
"""

import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from urllib.parse import quote

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker
)
from sqlalchemy.orm import declarative_base
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import JonexException
from jonex_core.common.tenant import TenantContext

logger = logging.getLogger(__name__)

# ==================== 基础配置 ====================
_config = get_config()

# 数据库连接 URL
_encoded_password = quote(_config.DB_PASSWORD.encode('utf-8'), safe='')
SQLALCHEMY_DATABASE_URL = (
    f"postgresql+asyncpg://{_config.DB_USERNAME}:{_encoded_password}@"
    f"{_config.DB_HOST}:{_config.DB_PORT}/{_config.DB_NAME}"
)

# ==================== 懒加载引擎与会话工厂 ====================
_async_engine = None
_async_session_factory = None
_initialized = False


def _initialize_engine():
    """初始化数据库引擎（懒加载）"""
    global _async_engine, _initialized
    if _async_engine is None:
        logger.info("初始化数据库连接池...")
        _async_engine = create_async_engine(
            SQLALCHEMY_DATABASE_URL,
            pool_size=_config.DB_POOL_SIZE,
            max_overflow=_config.DB_MAX_OVERFLOW,
            pool_timeout=_config.DB_POOL_TIMEOUT,
            pool_recycle=_config.DB_POOL_RECYCLE,
            pool_pre_ping=True,
            echo=_config.DB_ECHO,
            json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),
            isolation_level="READ COMMITTED",
            future=True,
        )
        _initialized = True


def _get_engine():
    """获取数据库引擎"""
    _initialize_engine()
    return _async_engine


def _get_session_factory():
    """获取异步会话工厂"""
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=_get_engine(),
            class_=AsyncSession,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        # 注册事件监听器
        _register_session_events(_async_session_factory)
    return _async_session_factory


def _register_session_events(factory):
    """注册会话事件监听器"""
    # SQLAlchemy 事件需要注册到实际的会话类
    pass


# 基础模型类
Base = declarative_base()


# 导出供外部使用（兼容旧代码，实际调用时会创建会话）
def AsyncSessionLocal():
    """获取异步会话实例（兼容旧代码调用方式）"""
    factory = _get_session_factory()
    return factory()


# ==================== 数据库会话获取 ====================
@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    获取数据库会话（上下文管理器）

    使用示例:
        async with get_db_session() as session:
            result = await session.execute(query)
    """
    factory = _get_session_factory()
    session = factory()
    session.info["tenant_id"] = TenantContext.get()
    try:
        yield session
        await session.commit()
    except JonexException:
        # 业务异常（ResourceConflictError / NotFoundException 等）是正常业务流程的一部分，
        # 会被全局异常处理器转换为对应 HTTP 响应；此处仅回滚保证数据安全，不打 ERROR 告警日志。
        await session.rollback()
        raise
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"数据库操作异常, 已回滚: {str(e)}")
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"未知异常, 已回滚: {str(e)}")
        raise
    finally:
        await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    依赖注入用的数据库会话获取

    FastAPI Depends 使用示例:
        @app.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with get_db_session() as session:
        yield session


# ==================== 数据库初始化 ====================
async def init_database(drop_existing: bool = False):
    """
    初始化数据库表结构

    Args:
        drop_existing: 是否先删除已存在的表（仅开发环境使用）
    """
    engine = _get_engine()

    if drop_existing and _config.ENV != "production":
        logger.warning("⚠️  删除已存在的数据库表（开发环境）")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✅ 数据库表结构初始化完成")


async def close_database():
    """关闭数据库连接池"""
    global _async_engine, _async_session_factory, _initialized
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
        _async_session_factory = None
        _initialized = False
        logger.info("✅ 数据库连接池已关闭")


# ==================== 健康检查 ====================
async def check_db_health() -> bool:
    """检查数据库连接健康状态"""
    try:
        async with get_db_session() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"数据库健康检查失败: {e}")
        return False


# ==================== 同步支持（兼容原有代码） ====================
def get_sync_session():
    """
    获取同步会话（用于兼容旧代码，新代码请使用异步版本）

    注意：需要创建同步引擎，这里暂不实现
    """
    raise NotImplementedError(
        "同步数据库会话已废弃，请使用异步版本 get_db_session()"
    )
