#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""N1-3：② 的 run 级 `Scope` —— 一次提问里所有「能不能看 / 能不能查」的判断单独成对象。

收敛之前，同一批判断散在五处：`load_governance`（标签/策略/密级）、
`filter_secret_columns` / `filter_secret_joins`（生成前预防）、`guard_sql` 的
`allowed_tables` / `forbidden_pairs`（安全闸）、`exec_allowed_tables`（探索工具白名单）。
它们都从同一份输入（tenant / kb / ds / user）出发，却各自被传六个参数、在三个层次里
重复传参 —— 任何一处漏传就是一条静默放行。

本模块把输入与判定收进一个 `Nl2SqlScope`：

- **构建一次**（`build_scope`：读禁止对 + 治理 + 编译 schema），同一个对象贯穿
  候选筛选、生成前过滤、安全闸、治理改写、探索工具；
- 方法只做转发（`guard` / `rewrite` / `filter_*` / `allowed_tables`），**不改变任何判定**；
- 既有的纯函数（`governance_*` / `filter_secret_*` / `exec_allowed_tables`）原样搬来，
  `service.py` 通过别名继续导出 —— 既有测试与调用点零改动。

Scope 是**不可变**的（frozen dataclass）：一次提问内不会出现「半路换了治理快照」。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from jonex_core.common.config import config
from jonex_core.common.database import get_db_session

from . import schema_read
from .forbidden import filter_joins
from .guard import guard_sql
from .joins import JoinPlan
from .lineage_source import load_forbidden_pairs_from_db

logger = logging.getLogger(__name__)

__all__ = [
    "Nl2SqlScope",
    "build_scope",
    "edge_ends",
    "exec_allowed_tables",
    "filter_secret_columns",
    "filter_secret_joins",
    "governance_decide",
    "governance_level",
    "governance_tables",
    "governance_tags",
    "load_forbidden_pairs",
    "load_governance",
]


# ---------------------------------------------------------------------------
# 治理判定（原 service.py 的同名私有函数，逐字搬入）
# ---------------------------------------------------------------------------

def governance_tables(gov: dict, table: str) -> list[str]:
    """SQL 表名 → 标签表名候选（精确优先，否则末段匹配）。"""
    names = set(gov.get("table_levels") or {})
    names |= {t for t, _ in (gov.get("col_levels") or {})}
    if table in names:
        return [table]
    last = table.split(".")[-1].lower()
    hits = [n for n in names if n.split(".")[-1].lower() == last]
    return hits or [table]


def governance_level(gov: dict, table: str, column: str | None) -> str | None:
    from ..governance.levels import max_level
    levels: list[str | None] = []
    for t in governance_tables(gov, table):
        if column is not None:
            levels.append((gov.get("col_levels") or {}).get((t, column)))
        levels.append((gov.get("table_levels") or {}).get(t))
    found = [lv for lv in levels if lv]
    return max_level(*found) if found else None


def governance_tags(gov: dict, table: str, column: str | None) -> list[str]:
    out: set[str] = set()
    for t in governance_tables(gov, table):
        out |= set((gov.get("tags") or {}).get((t, column)) or ())
        out |= set((gov.get("tags") or {}).get((t, None)) or ())
    return sorted(out)


def governance_decide(gov: dict, table: str, column: str | None, action: str):
    from ..governance.pdp import decide
    return decide(
        clearance=str(gov.get("clearance") or "public"),
        level=governance_level(gov, table, column),
        tags=governance_tags(gov, table, column),
        policies=list(gov.get("policies") or []),
        action=action, table=table, column=column)


def filter_secret_columns(columns: list[dict], gov: dict, table: str) -> list[dict]:
    """生成前预防：密级高于调用方 clearance 的 `secret` 列不进列清单。"""
    from ..governance.levels import level_rank
    clearance = str(gov.get("clearance") or "public")
    out = []
    for c in columns or ():
        # 治理标签按**本体属性名**登记（logical）；列清单的 `name` 可能是物理名。
        level = governance_level(
            gov, table, str(c.get("logical_name") or c.get("name") or ""))
        if level == "secret" and level_rank(clearance) < level_rank("secret"):
            continue
        out.append(c)
    return out


def filter_secret_joins(plan: JoinPlan, gov: dict, anchor_table: str) -> JoinPlan:
    """生成前预防：跨 `secret` 列的 join 边不当证据递出去。"""
    from ..governance.levels import level_rank
    clearance = str(gov.get("clearance") or "public")
    if level_rank(clearance) >= level_rank("secret"):
        return plan
    kept = []
    for j in plan.joins:
        if (governance_level(gov, str(j.get("from_table") or ""),
                             str(j.get("from_column") or "")) == "secret"
                or governance_level(gov, str(j.get("to_table") or ""),
                                    str(j.get("to_column") or "")) == "secret"):
            continue
        kept.append(j)
    if len(kept) == len(plan.joins):
        return plan
    tables = {str(j.get("to_table") or "") for j in kept}
    related = [r for r in (plan.related_tables or [])
               if str(r.get("table") or "") in tables]
    return JoinPlan(joins=kept, related_tables=related, truncated=plan.truncated)


# ---------------------------------------------------------------------------
# 表白名单（探索工具 A3）
# ---------------------------------------------------------------------------

def edge_ends(edge) -> tuple[str, str]:
    if isinstance(edge, dict):
        return (str(edge.get("from_table") or "").strip(),
                str(edge.get("to_table") or "").strip())
    return (str(getattr(edge, "from_table", "") or "").strip(),
            str(getattr(edge, "to_table", "") or "").strip())


def exec_allowed_tables(*, anchor_entity: str, anchor_table: str,
                        related_tables, known_tables, lineage_edges,
                        join_edges=(), max_hops: int = 3) -> set[str]:
    """执行工具的表白名单（A3）：与最终答案**同源**的那几张表。

    - 锚点（**有物理表名就只用物理表名**，缺失时才回退实体名）+ 这次注入的 1 跳 related；
    - 多跳路径表：从锚点出发、沿「join 边 ∪ 血缘边」在 `max_hops` 内**可达**、
      且属于编译 schema 已知表的血缘端点。可达性过滤是必要的——只按「血缘端点 ∩
      已知表」取交会把**与本题无关**的远端表也放进来（审查 #1），而最终答案的白名单
      只含**实际注入的路径表**。

    ⚠️ 2026-11-07（N1-3）：原实现回退分支引用了未定义变量 `start`（锚点/related
    全空时 `NameError`）。现在纯空集 → 空集（没有可给的表，不猜）。
    """
    table = str(anchor_table or "").strip()
    # 实体名与表名同形（`Orders` ↔ `public.orders`）时，裸实体名会被 `archive.orders`
    # 后缀命中 —— 有物理表名就只用物理表名，缺失时才回退实体名。
    tables = {table} if table else {str(anchor_entity or "").strip()}
    for t in related_tables or []:
        tables.add(str((t or {}).get("table") or "").strip())
    known = {str(t).strip() for t in known_tables or () if str(t).strip()}
    adj: dict[str, set[str]] = {}
    for edge in list(join_edges or []) + list(lineage_edges or []):
        a, b = edge_ends(edge)
        if not a or not b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    # 1 跳 related 是**已经交给模型**的表，本身就是可达起点（不一定在边池里）。
    reachable = {t for t in tables if t}
    frontier = set(reachable)
    for _ in range(max(0, int(max_hops))):
        nxt: set[str] = set()
        for t in frontier:
            nxt |= adj.get(t, set())
        nxt -= reachable
        if not nxt:
            break
        reachable |= nxt
        frontier = nxt
    for edge in lineage_edges or []:
        for t in edge_ends(edge):
            if t and t in known and t in reachable:
                tables.add(t)
    tables.discard("")
    return tables


# ---------------------------------------------------------------------------
# 读取入口（构建 Scope 用；原 service.py 的私有 loader，fail-open 语义不变）
# ---------------------------------------------------------------------------

async def load_forbidden_pairs(tenant_id: str, kb_id: str, ds_id: str) -> list:
    """运行时禁止对；**始终加载**（安全闸必须生效，不受 `NL2SQL_JOIN_PATHS` 开关影响）。"""
    try:
        async with get_db_session() as session:
            return await load_forbidden_pairs_from_db(session, tenant_id, kb_id, ds_id)
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 —— 增能约束，会话层失败也退空
        logger.warning("禁止同查表对读表会话失败，本次退空：%s", exc)
        return []


async def load_governance(tenant_id: str, kb_id: str, ds_id: str,
                          user_id: str | None) -> dict:
    """读取本次提问的标签/策略/密级 + 编译 schema；失败 fail-open 到「无治理」。"""
    try:
        async with get_db_session() as session:
            from ..governance.label_service import GovernanceLabelService
            from ..governance.policy_service import GovernancePolicyService
            from ..governance.clearance_service import GovernanceClearanceService
            from ..governance.levels import effective_level, level_rank
            labels = (await GovernanceLabelService(session).list(
                tenant_id, kb_id, ds_id))["items"]
            policies = (await GovernancePolicyService(session).list(
                tenant_id, kb_id))["items"]
            clearance = await GovernanceClearanceService(session).get(
                tenant_id, user_id)
        # 编译 schema **必须走版本化读口**（P1）：否则同一次提问会混用「缓存的
        # schema 视图」与「现读的治理视图」—— 列集合不一致会产出假的
        # `rejected_sensitive` / guard 拒绝，最长持续一个 TTL。放在治理会话**之外**
        # 读：别占着连接池的一条连接再开第二个会话。
        # 行不存在时 `tables_from_compiled_row` 抛 `InvalidParameterError`
        # （与旧 `schema_tables` 同型）→ 下面同一条 fail-open 接住。
        schema = schema_read.tables_from_compiled_row(
            await schema_read.load_compiled_row(tenant_id, kb_id, ds_id))
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 —— 治理是增能约束，读失败退空
        # kb/ds 一并记：行缺失那条的异常文案不再带 ds（投影函数签名里没有它）。
        logger.warning("治理数据读取失败，本次退空：kb=%s ds=%s err=%s",
                       kb_id, ds_id, exc)
        return {"active": False, "policies": [], "clearance": "public", "schema": {}}

    grouped: dict[tuple[str, str | None], list[dict]] = {}
    for r in labels:
        grouped.setdefault((r["table_name"], r["column_name"]), []).append(r)
    col_levels: dict[tuple[str, str], str] = {}
    table_levels: dict[str, str] = {}
    tags: dict[tuple[str, str | None], set[str]] = {}
    for (table, col), rows in grouped.items():
        level = effective_level(rows)
        if col is None:
            table_levels[table] = level
        else:
            col_levels[(table, col)] = level
        bucket: set[str] = set()
        for r in rows:
            bucket.update(str(t) for t in (r.get("tags") or ()) if str(t).strip())
        tags[(table, col)] = bucket
    # 表级是列级的下限
    for (table, col), level in list(col_levels.items()):
        floor = table_levels.get(table)
        if floor and level_rank(floor) > level_rank(level):
            col_levels[(table, col)] = floor
    return {
        "active": bool(labels or policies),
        "col_levels": col_levels, "table_levels": table_levels,
        "tags": tags, "policies": policies,
        "clearance": clearance, "schema": schema,
    }


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Nl2SqlScope:
    """一次提问的权限/治理上下文（构建后不可变）。"""

    tenant_id: str
    kb_id: str
    ds_id: str
    user_id: Optional[str]
    gov: dict
    gov_active: bool
    forbidden_pairs: tuple = ()

    @property
    def clearance(self) -> str:
        return str(self.gov.get("clearance") or "public")

    def level(self, table: str, column: str | None = None) -> str | None:
        return governance_level(self.gov, table, column)

    def tags(self, table: str, column: str | None = None) -> list[str]:
        return governance_tags(self.gov, table, column)

    def decide(self, table: str, column: str | None, action: str):
        return governance_decide(self.gov, table, column, action)

    def filter_columns(self, columns: list[dict], table: str) -> list[dict]:
        return filter_secret_columns(columns, self.gov, table)

    def filter_joins(self, plan: JoinPlan, anchor_table: str) -> JoinPlan:
        return filter_secret_joins(plan, self.gov, anchor_table)

    def guard(self, sql: str, *, engine: str, max_rows: int,
              allowed_tables: Optional[set[str]] = None,
              offset: int = 0):
        """安全闸：同一把禁止同查 + 同一份表白名单（探索与最终答案共用）。"""
        return guard_sql(sql, engine=engine, max_rows=max_rows,
                         allowed_tables=allowed_tables,
                         forbidden_pairs=list(self.forbidden_pairs),
                         offset=offset)

    def rewrite(self, sql: str, *, engine: str):
        """治理改写（mask / deny / row_filter）；`gov_active=False` 时调用方跳过。"""
        from ..governance.sql_rewrite import apply_governance
        return apply_governance(
            sql, engine=engine,
            decide_fn=lambda t, c, a: self.decide(t, c, a),
            schema=self.gov.get("schema") or {})

    def allowed_tables(self, **kwargs) -> set[str]:
        return exec_allowed_tables(**kwargs)


async def build_scope(tenant_id: str, kb_id: str, ds_id: str,
                      user_id: str | None) -> Nl2SqlScope:
    """一次提问构建一次：禁止对 + 治理快照 + 生效判定。"""
    forbidden = await load_forbidden_pairs(tenant_id, kb_id, ds_id)
    gov = await load_governance(tenant_id, kb_id, ds_id, user_id)
    return Nl2SqlScope(
        tenant_id=tenant_id, kb_id=kb_id, ds_id=ds_id, user_id=user_id,
        gov=gov,
        gov_active=bool(config.GOVERNANCE_ENFORCEMENT_ENABLED and gov.get("active")),
        forbidden_pairs=tuple(forbidden or ()),
    )
