"""血缘路径查询（P2b Task 1）：在「该数据源的 join 边 + 血缘边」混合图上做多跳搜索。

**只读**：不写库、不触达任何投影/重建逻辑。读路径与 ② NL→SQL 的路径搜索**完全同源**
——同一份编译 schema 行、同一套 `joins.all_join_edges` / `lineage_source.load_lineage_edges_from_db`
边池、同一个纯函数 `paths.search_paths`。两处若各写一份取边/打分逻辑，画布上的高亮
就会与模型实际走的路分叉（那是这一层最贵的错误）。

口径：

- 边池 = `all_join_edges(row.relation_types, row.entity_types)`（**全量** join 边，
  不按锚点裁剪、不截断）+ 该 KB/该源的活血缘边。截断只发生在路径层
  （`max_hops` / `max_paths` / 每跳上限），与 spec §6.1 一致。
- `unknown=True` 只有一种含义：**该数据源没有 active 的编译 schema 行**（与
  「schema 在、但锚点不在图里」区分 —— 后者返回空 `paths` + `unknown=False`）。
- 数值参数取**夹取**而非报错（`max_hops` 1..3、`max_paths` 1..10、`min_hops` 1..3）：
  与姊妹只读查询 `ontology_data_assets.get_data_assets` 的 `hops` 同口径 ——
  网关已有 `ge/le` 校验，服务层的夹取是能力直调（绕过网关）时的兜底，
  不是调用方错误的信号。**非整数**才按 1001 拒（那没法夹）。
- 空白的 `data_source_id` / `anchor` 一律 1001：空 ds 会让作用域退化成「同 KB 全部源」，
  空锚点则会让 `search_paths` 静默返回空图 —— 两者都是「看着像没数据」的静默错误。
- 串行化时 `steps` 的端点取**边的数据流端点**（`Edge.from_* → Edge.to_*`），
  `reversed` 单独标记遍历方向。前端渲染箭头/高亮时要的是数据流方向；
  若把遍历方向当端点写出去，反向走过的边会在画布上指向错误的一侧。
"""
from __future__ import annotations

from typing import Any, Optional

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ..repository.ontology_schema_repository import OntologySchemaRepository
from .nl2sql.joins import all_join_edges
from .nl2sql.lineage_source import load_lineage_edges_from_db
from .nl2sql.paths import (
    EDGE_JOIN,
    FALLBACK_ORIGIN,
    Edge,
    PathPlan,
    search_paths,
)

#: 入参边界（与网关的 ge/le、`paths.MAX_HOPS` 一致）。
MAX_HOPS_LIMIT = 3
MAX_PATHS_LIMIT = 10
MIN_HOPS_LIMIT = 3


def _clamp_int(value: Any, *, default: int, low: int, high: int, name: str) -> int:
    """`None`/缺省 → `default`；整数 → 夹到 `[low, high]`；非整数 → 1001。

    不把越界当错误：网关已用 `ge/le` 拦过一轮，能力直调属于内部调用方，
    夹取让「多传了一点」退化成可预期的有界结果，而不是整条查询失败。
    """
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParameterError(message=f"{name} 必须是整数") from exc
    return max(low, min(number, high))


def serialize_path_plan(plan: PathPlan) -> list[dict]:
    """`PathPlan.paths` → 前端契约里的 `paths` 列表（纯函数，最该被单测钉死）。

    字段顺序即契约顺序（前端按名字读，顺序只影响可读性/快照测试）：
    `score` / `transfers` / `hits_target` / `steps`；每个 step 是
    `type` / `from_table` / `from_column` / `to_table` / `to_column` / `origin` / `reversed`。

    ⚠️ **端点取边的数据流端点，不是遍历端点**：`Step.enter_* / leave_*` 已按
    `reversed` 解析过，直接写出去会让反向遍历的边在画布上方向反掉。
    """
    return [
        {
            "score": path.score,
            "transfers": path.transfers,
            "hits_target": path.hits_target,
            "steps": [
                {
                    "type": step.edge.type,
                    "from_table": step.edge.from_table,
                    "from_column": step.edge.from_column,
                    "to_table": step.edge.to_table,
                    "to_column": step.edge.to_column,
                    "origin": step.edge.origin,
                    "reversed": step.reversed,
                }
                for step in path.steps
            ],
        }
        for path in plan.paths
    ]


def _empty_result(anchor: str, target: Optional[str]) -> dict:
    """`unknown=True` 的唯一成因：该数据源没有 active 编译 schema 行。"""
    return {
        "anchor": anchor,
        "target": target,
        "unknown": True,
        "explored": 0,
        "truncated": False,
        "paths": [],
    }


async def _compiled_row(session, tenant_id: str, kb_id: str, data_source_id: str):
    """取**该源那一行** active 编译 schema —— 与 `nl2sql.service._compiled_row` 同源同口径。

    抽成独立函数是为了可插桩：单测不能连库，用例 patch 本方法/仓储即可。
    `list_compiled_schemas` 已按 `status == "active"` 过滤（仓储里写死）。
    """
    rows = await OntologySchemaRepository(session).list_compiled_schemas(tenant_id, kb_id)
    return next((r for r in rows if r.data_source_id == data_source_id), None)


def _join_edges(row) -> list[Edge]:
    """编译 schema 行 → 全量 join 边（`all_join_edges` 的 dict 与 `Edge` 字段同名）。"""
    return [
        Edge(
            type=EDGE_JOIN,
            from_table=str(item["from_table"]),
            from_column=str(item["from_column"]),
            to_table=str(item["to_table"]),
            to_column=str(item["to_column"]),
            origin=str(item.get("origin") or FALLBACK_ORIGIN),
        )
        for item in all_join_edges(row.relation_types or [], row.entity_types or [])
    ]


async def get_lineage_paths(
    tenant_id: str,
    kb_id: str,
    data_source_id: str,
    anchor: str,
    *,
    target: Optional[str] = None,
    max_hops: int = MAX_HOPS_LIMIT,
    max_paths: int = 5,
    min_hops: int = 1,
) -> dict:
    """该 KB + 数据源作用域内、从 `anchor` 出发的评分路径（只读）。

    返回 `{anchor, target, unknown, explored, truncated, paths}`：

    - `unknown=True`：该源没有 active 编译 schema 行（空 `paths`、`explored=0`）。
    - `explored` / `truncated` 原样来自 `search_paths`（前者是每跳实际展开数之和，
      后者表示「按分数取前 `max_paths` 时有被砍掉的路径」）。
    """
    tenant_id = require_tenant(tenant_id)
    data_source_id = str(data_source_id or "").strip()
    anchor = str(anchor or "").strip()
    if not data_source_id:
        raise InvalidParameterError(message="缺少 data_source_id")
    if not anchor:
        raise InvalidParameterError(message="缺少 anchor")
    # 空白 target 等价于没给（不把 `"  "` 当作一个永远命不中的目标）。
    target = str(target).strip() if target is not None else None
    target = target or None

    max_hops = _clamp_int(max_hops, default=MAX_HOPS_LIMIT, low=1, high=MAX_HOPS_LIMIT,
                          name="max_hops")
    max_paths = _clamp_int(max_paths, default=5, low=1, high=MAX_PATHS_LIMIT,
                           name="max_paths")
    min_hops = _clamp_int(min_hops, default=1, low=1, high=MIN_HOPS_LIMIT,
                          name="min_hops")
    # 独立夹取后仍可能 min_hops > max_hops（如 max_hops=1、min_hops=3）：
    # `search_paths` 会安静地一条都返回不了，看起来像「没有路径」。这不是
    # 有意义的查询语义，收紧到 max_hops 而不是让整次查询退化成空集。
    min_hops = min(min_hops, max_hops)

    async with get_db_session() as session:
        row = await _compiled_row(session, tenant_id, str(kb_id or ""), data_source_id)
        if row is None:
            return _empty_result(anchor, target)
        edges = _join_edges(row)
        # 血缘读取**只在 schema 行存在时**发生：没有 schema 就没有边池可言，
        # 这一跳还能省掉一次对 `lineage_edges` 的无意义查询。
        edges.extend(await load_lineage_edges_from_db(
            session, tenant_id, str(kb_id or ""), data_source_id))

    plan = search_paths(
        edges,
        anchor,
        targets=[target] if target else None,
        max_hops=max_hops,
        max_paths=max_paths,
        min_hops=min_hops,
    )
    return {
        "anchor": anchor,
        "target": target,
        "unknown": False,
        "explored": plan.explored,
        "truncated": plan.truncated,
        "paths": serialize_path_plan(plan),
    }
