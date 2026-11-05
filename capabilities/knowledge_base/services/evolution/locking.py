# -*- coding:utf-8 -*-
"""版本链并发锁（演化台信任闭环 P0）。

一条版本链 = `(tenant_id, kb, scope, ds)`：`abstract` 链（KB 级，`ds=None`）
与每个数据源的 `compiled` 链。采纳、回滚、runner 的「应用 → 候选评测 →
回滚」窗口都必须在**同一条链**上串行，否则会互相看到半应用状态。

为什么用 **session-level** advisory lock 而不是事务锁：
`AbstractSchemaService` / `OntologyCompiler` 各自开独立 session 写 schema，
`pg_advisory_xact_lock` 锁不住它们的写入；且调用方会话在流程中途 commit，
连接可能归还连接池，事务级锁会随事务结束消失。session-level 锁必须由一条
**独立、不 commit 的会话**全程持有，退出时显式 `pg_advisory_unlock`。

键：PostgreSQL advisory lock 收 64 位整数。这里用
`sha1("{tenant}|{kb}|{scope}|{ds or ''}")` 的 hex 前 15 位转 int
（< 2^63，跨进程稳定）。碰撞只会让两条无关链多串行，不会写错数据；
**不要**用 `hashtext(text)`（`pg_advisory_lock` 不接受 text，且 hashtext
是 32 位、碰撞率更高）。
"""
from __future__ import annotations

import contextlib
import hashlib
import logging

from sqlalchemy import text

from jonex_core.common.database import get_db_session

logger = logging.getLogger(__name__)

#: 演化 kind ↔ 版本链 scope（只在 locking 定义一次，schema_io 从这里导入）。
#: `relation` / `constraint` 与 `query_rule` **共用** compiled 链（P1-3 R1 /
#: P1-4 R5）：同 scope、同锁、同 active 唯一键，不新增 scope / 迁移。
#: `mapping` 与 `alias` 共用 abstract 链（P1-5：KB 级抽象 schema 的落点）；
#: `tool_schema`（Tool-Schema 线）写抽象 schema 的 `tools` 覆盖块，同样属
#: abstract 家族（同链、同锁、`ds=None`）。`synonym`（值同义词线）是**自己的
#: 版本链 / 自己的锁**（`ds=None`）：不写抽象 schema，不能与 alias 等互斥。
_KIND_TO_SCOPE = {"alias": "abstract", "mapping": "abstract",
                  "tool_schema": "abstract",
                  "synonym": "synonym",
                  "query_rule": "compiled", "relation": "compiled",
                  "constraint": "compiled"}
#: scope → 默认 kind（**兼容口径**）：恢复/台账等只有 scope、没有 run 行的路径
#: 仍把 compiled 标成 `query_rule`。不要改成 `_KIND_TO_SCOPE` 的反转 ——
#: 反转会让 `kind_for_scope("compiled") == "relation"`，破坏既有回执/台账标签。
_SCOPE_TO_KIND = {"abstract": "alias", "compiled": "query_rule",
                  "synonym": "synonym"}
#: scope → 该链上的全部 kind（同链并发判定用；三条链各自互斥）。
_KINDS_BY_SCOPE = {"abstract": ("alias", "mapping", "tool_schema"),
                   "compiled": ("query_rule", "relation", "constraint"),
                   "synonym": ("synonym",)}

#: compiled 家族的 kind（存储分支共用；schema_io / adopt_common 从这里导入）。
COMPILED_KINDS = frozenset(_KINDS_BY_SCOPE["compiled"])
#: abstract 家族的 kind（`alias` / `mapping` / `tool_schema`：KB 级同一
#: 存储分支/版本链）。
ABSTRACT_KINDS = frozenset(_KINDS_BY_SCOPE["abstract"])
#: synonym 家族的 kind（值同义词线；KB 级、独立版本链）。
SYNONYM_KINDS = frozenset(_KINDS_BY_SCOPE["synonym"])


def scope_for_kind(kind: str) -> str:
    """`alias` / `mapping` / `tool_schema` → `abstract`（KB 级），`synonym`
    → `synonym`（KB 级、独立链），`query_rule` / `relation` / `constraint`
    → `compiled`（DS 级）。"""
    try:
        return _KIND_TO_SCOPE[str(kind)]
    except KeyError:
        raise ValueError(f"未知的演化类型: {kind}") from None


def kind_for_scope(scope: str) -> str:
    """`abstract` → `alias`，`compiled` → `query_rule`（兼容默认标签），
    `synonym` → `synonym`。"""
    try:
        return _SCOPE_TO_KIND[str(scope)]
    except KeyError:
        raise ValueError(f"未知的版本 scope: {scope}") from None


def kinds_for_scope(scope: str) -> tuple[str, ...]:
    """该版本链上的全部 kind（abstract 链 = alias / mapping / tool_schema；
    compiled 链 = `query_rule` + `relation` + `constraint`；synonym 链 =
    `synonym`）。"""
    try:
        return _KINDS_BY_SCOPE[str(scope)]
    except KeyError:
        raise ValueError(f"未知的版本 scope: {scope}") from None


def is_compiled_kind(kind: str) -> bool:
    """kind 是否落在 compiled 家族（`query_rule` / `relation` / `constraint`）。"""
    return str(kind) in COMPILED_KINDS


def is_abstract_kind(kind: str) -> bool:
    """kind 是否落在 abstract 家族（`alias` / `mapping` / `tool_schema`）。"""
    return str(kind) in ABSTRACT_KINDS


def advisory_key(tenant_id: str, kb: str | None, scope: str,
                 ds: str | None = None) -> int:
    """版本链的稳定 64 位 advisory lock 键（< 2^63）。"""
    raw = f"{tenant_id}|{kb or ''}|{scope}|{ds or ''}"
    return int(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:15], 16)


async def lock_chain(session, key: int) -> None:
    """在给定会话上获取 session-level advisory lock（阻塞直到拿到）。"""
    await session.execute(
        text("SELECT pg_advisory_lock(:key)"), {"key": int(key)})


async def unlock_chain(session, key: int) -> None:
    """释放给定会话上的 advisory lock（幂等：未持有时 PostgreSQL 返回 false）。"""
    await session.execute(
        text("SELECT pg_advisory_unlock(:key)"), {"key": int(key)})


@contextlib.asynccontextmanager
async def chain_lock(key: int):
    """持有版本链锁的异步上下文管理器。

    使用**独立会话**（`get_db_session()`）且全程不 commit：连接会稳定留在
    这里直到退出，`finally` 里先解锁再关闭会话。调用方自己的 `session` 在
    流程中会 commit/rollback，不能用来持锁。
    """
    async with get_db_session() as lock_session:
        await lock_chain(lock_session, key)
        try:
            yield
        finally:
            try:
                await unlock_chain(lock_session, key)
            except Exception as exc:  # noqa: BLE001 —— 解锁失败不能盖住业务异常
                logger.warning("释放演化版本链锁失败 key=%s: %s", key, exc)


@contextlib.asynccontextmanager
async def try_chain_lock(key: int):
    """**非阻塞**尝试持有版本链锁的异步上下文管理器（`yield bool`）。

    与 `chain_lock` 同构（独立会话、全程不 commit），但用
    `pg_try_advisory_lock`：拿到锁才 `yield True` 并在 `finally` 解锁；
    没拿到时 `yield False` 且**不解锁**（锁根本不在本会话上，解锁会把
    别人的锁放掉）。

    给崩溃恢复用：恢复是后台扫尾，链上正有采纳在跑时应该「下次再收」，
    而不是把重启恢复流程阻塞在 `pg_advisory_lock` 上。
    """
    async with get_db_session() as lock_session:
        acquired = bool((await lock_session.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": int(key)})).scalar())
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            try:
                await unlock_chain(lock_session, key)
            except Exception as exc:  # noqa: BLE001 —— 解锁失败不能盖住业务异常
                logger.warning("释放演化版本链锁失败 key=%s: %s", key, exc)


__all__ = [
    "ABSTRACT_KINDS",
    "COMPILED_KINDS",
    "SYNONYM_KINDS",
    "advisory_key",
    "chain_lock",
    "is_abstract_kind",
    "is_compiled_kind",
    "kind_for_scope",
    "kinds_for_scope",
    "lock_chain",
    "scope_for_kind",
    "try_chain_lock",
    "unlock_chain",
]
