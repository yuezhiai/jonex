# -*- coding:utf-8 -*-
"""演化台账：落库读写（append / list）。

`list` 返回旧桥接形状（`path` / `exists` / `mtime` / `items`），前端零改动：
`items` 直接是 entry 列表（不带 run_id 包装），`mtime` 取该组最新 `created_at`。
"""
from __future__ import annotations

import uuid

from sqlalchemy import or_, select

from ...models.evolution import EvolutionLedger

# (组名, 台账路径, 表内 ledger_type)；表内出现未知 ledger_type 时静默跳过
_GROUPS = (
    ("alias", "db://evolution_ledgers/alias", "alias"),
    ("query_rule", "db://evolution_ledgers/query_rule", "query_rule"),
    ("rejects", "db://evolution_ledgers/reject", "reject"),
)

#: 演化 kind → 表内 ledger_type（R2/R4：`relation` / `constraint` 不新增账本，
#: 都落 query_rule 那本；由 entry.kind 区分）。账本分组仍只由 ledger_type 决定。
#: `mapping` / `tool_schema` 写抽象 schema，与 `alias` 同落 alias 那本
#: （P1-5 / Tool-Schema 线）。`synonym`（值同义词线）同样落 alias 那本，
#: 用 `entry.kind="synonym"` 区分（不新增 ledger_type / 迁移）。
_KIND_TO_LEDGER_TYPE = {"alias": "alias", "mapping": "alias",
                        "tool_schema": "alias", "synonym": "alias",
                        "query_rule": "query_rule",
                        "relation": "query_rule", "constraint": "query_rule"}


def ledger_type_for_kind(kind: str) -> str:
    """演化 kind → 台账 ledger_type；未知 kind 抛 ValueError（不猜）。"""
    try:
        return _KIND_TO_LEDGER_TYPE[str(kind)]
    except KeyError:
        raise ValueError(f"未知的演化类型: {kind}") from None


class LedgerService:
    def __init__(self, session) -> None:
        self.session = session

    async def append(self, tenant_id: str, ledger_type: str, entry: dict,
                     run_id: str | None = None) -> None:
        try:
            self.session.add(EvolutionLedger(
                id=uuid.uuid4().hex, tenant_id=tenant_id, ledger_type=ledger_type,
                entry=entry, run_id=run_id,
            ))
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def has_entries(self, tenant_id: str, run_id: str) -> bool:
        """该 run 是否已有台账行（采纳幂等重放时检测「台账是否真的落了」）。

        只读、带租户条件；没有返回 False。读失败上抛，由调用方按「未知」处理。
        """
        stmt = select(EvolutionLedger.id).where(
            EvolutionLedger.tenant_id == tenant_id,
            EvolutionLedger.run_id == run_id,
        ).limit(1)
        return (await self.session.execute(stmt)).scalars().first() is not None

    async def has_rollback_entry(self, tenant_id: str, run_id: str) -> bool:
        """该 run 是否已有**回滚**台账行（`entry.kind == "rollback"` 或
        `entry.op == "rollback"`）。

        回滚重放不能复用 `has_entries`：采纳成功必然写过 alias/query_rule
        台账，run 级判据会把「回滚台账丢失」误判成「已存在」而永不补写。
        非 alias 的 compiled 家族（`relation` / `constraint`）回滚行按 R2/R4
        记为与采纳行同 kind，另带 `op="rollback"` 作为回滚判据 —— 采纳行没有
        `op="rollback"`（constraint 采纳行的 `op` 是 add/update/disable/enable），
        不会误判。
        只读、带租户条件；读失败上抛，由调用方按「未知」处理。
        """
        stmt = select(EvolutionLedger.id).where(
            EvolutionLedger.tenant_id == tenant_id,
            EvolutionLedger.run_id == run_id,
            or_(EvolutionLedger.entry["kind"].astext == "rollback",
                EvolutionLedger.entry["op"].astext == "rollback"),
        ).limit(1)
        return (await self.session.execute(stmt)).scalars().first() is not None

    async def list(self, tenant_id: str) -> dict:
        stmt = select(EvolutionLedger).where(
            EvolutionLedger.tenant_id == tenant_id
        ).order_by(EvolutionLedger.created_at.desc(), EvolutionLedger.id.desc())
        rows = (await self.session.execute(stmt)).scalars().all()

        grouped: dict[str, list] = {key: [] for key, _, _ in _GROUPS}
        for row in rows:
            bucket = next((key for key, _, lt in _GROUPS if lt == row.ledger_type),
                          None)
            if bucket is None:
                continue
            grouped[bucket].append(row)

        out: dict[str, dict] = {}
        for key, path, _ in _GROUPS:
            group = grouped[key]
            stamps = [r.created_at for r in group if r.created_at is not None]
            out[key] = {
                "path": path,
                "exists": bool(group),
                "mtime": max(stamps).isoformat(timespec="seconds") if stamps else None,
                "items": [r.entry for r in group],
            }
        return out
