"""多跳 join 路径搜索（纯函数）。

与 `joins.py` 同性质：不连库、不调模型 —— 输入就是边池与锚点，所以最该被单测钉死。

设计见 `docs/superpowers/specs/2026-10-16-multihop-join-pathing-design.md`：
- **两类边混图**：`join`（列等值，可写进 ON）+ `lineage`（数据流，只能**换乘**，不可写进 ON）；
- **数值置信度**：序数档 `declared > comment > value > derived` 在路径层没法加权求和，
  所以先映射成数（阶梯见 `ORIGIN_CONFIDENCE`）；
- **乘法打分**：路径每一步都要成立，整体可信度本就是连乘 —— 但连乘**不**惩罚长路径
  （`declared`/`manual` 置信度都是 1.0，纯连乘时一跳与两跳同分），故另加显式跳数惩罚
  （`HOP_PENALTY`，见下），呼应 spec §5.1 的更正；
- **换乘惩罚**：换乘是启发式（「B 由 A 加工而来」不等于 key 语义与粒度被保留），
  每换乘一次扣一档。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 序数档 → 数值置信度。**必须严格单调**（按阶梯从高到低排列）。
#: - `manual` 与 `declared` **同级**：人工在管理面录入的边是「人的声明」，
#:   与外键声明性质相同，不因来自 UI 而降级。
#: - `platform` 落在 `comment` 与 `value` **之间**：平台解析 SQL/ETL 管道得到的是
#:   机器解析的**结构性证据** —— 比统计推断硬，比人写进注释的声明弱。
#: - `value` 在 `comment` 之下是 `joins.py` 既有口径的**原样继承**，不是本模块新判断。
ORIGIN_CONFIDENCE: dict[str, float] = {
    "declared": 1.0,
    "manual": 1.0,
    "comment": 0.8,
    "platform": 0.6,
    "value": 0.4,
    "derived": 0.2,
}

#: `origin` 缺失 / 未知时的档 —— 与 `joins.py` 的 `_FALLBACK_ORIGIN` 同口径：
#: 既不夸大它的可信度，也让排序稳定。
FALLBACK_ORIGIN = "derived"

#: 边类型。`join` 可写进 SQL 的 `ON`；`lineage` 只能换乘（血缘不提供列等值语义）。
EDGE_JOIN = "join"
EDGE_LINEAGE = "lineage"


def confidence_of(origin: str | None) -> float:
    """序数档 → 数值置信度；未知 / 缺失按 `derived`（最保守）。"""
    return ORIGIN_CONFIDENCE.get(str(origin or ""), ORIGIN_CONFIDENCE[FALLBACK_ORIGIN])


@dataclass(frozen=True)
class Edge:
    """一条边。`frozen` 是为了能进 set（去重）与做环检测。"""

    type: str           # EDGE_JOIN | EDGE_LINEAGE
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    origin: str = FALLBACK_ORIGIN

    @property
    def confidence(self) -> float:
        return confidence_of(self.origin)


#: 路径最大跳数。
MAX_HOPS = 3

#: **每跳**保留的节点数上限 —— 防「超级节点」（宽表 / 热点表）把路径数炸成指数。
#: 借自 DataHub `LineageFlags.entitiesExploredPerHopLimit`。
PER_HOP_LIMIT = 200

#: 注入的路径条数上限。克制是刻意的：路径比边信息量大，条数一多模型可能被带偏。
MAX_PATHS = 5

#: 每条血缘换乘的惩罚系数（乘法）。
TRANSFER_PENALTY = 0.5

#: **跳数惩罚系数**（乘法），施加在「额外的」跳上（`^(hops-1)`）。
#: ⚠️ **这个常量是必需的，不是可选的**：`declared` / `manual` 的置信度是 **1.0**，
#: 纯连乘时「一跳 declared」与「两跳 declared」**同分** —— 而 spec §5.2③ 把
#: 「路径更长 → 分更低」定成了**规范要求**。`^(hops-1)` 让单跳退化成纯置信度，
#: 惩罚只落在额外跳上。呼应借来的做法「BFS 默认只走最短路径」。
HOP_PENALTY = 0.9

#: 路径终点命中「目标提示」的加成。
#: ⚠️ 只加分、**不作约束** —— 候选集可能很大（词面命中的都在里面），
#: 当约束会过度收窄，把真正需要的路径筛掉。
TARGET_BONUS = 1.5


@dataclass(frozen=True)
class Step:
    """路径里的一步。方向已解析：从 `enter_*` 走到 `leave_*`。"""

    edge: Edge
    enter_table: str
    enter_column: str
    leave_table: str
    leave_column: str
    reversed: bool = False


@dataclass(frozen=True)
class Path:
    steps: tuple[Step, ...]
    score: float
    transfers: int
    hits_target: bool


@dataclass(frozen=True)
class PathPlan:
    paths: list[Path]
    truncated: bool = False
    explored: int = 0


def _adjacency(edges: list[Edge]) -> dict[str, list[tuple[Edge, bool]]]:
    """表 → [(边, 是否反向走)]。

    ⚠️ **两类边在遍历时都双向**，差别在**语义**而非可达性：
    `join` 边本来就是对称的（`A.k = B.k`）；`lineage` 边有向（上游→下游），
    但**换乘允许双向**（从加工表换乘回上游，也可从上游换乘到下游）。
    方向差异体现在 `Step.reversed` 与最终渲染：`join` 步产出 ON 条件，
    `lineage` 步产出换乘标记。
    """
    adj: dict[str, list[tuple[Edge, bool]]] = {}
    for e in edges:
        # ⚠️ **列映射对不上 ⇒ 该换乘步不成立**（spec §5.3）：血缘必须**列级**才做得了换乘
        # ——「换乘到上游后用哪一列 join」只能由列级映射回答。表级血缘（缺列）**不进图**，
        # 而不是进图后产生一条没法用的路径。
        if e.type == EDGE_LINEAGE and not (e.from_column and e.to_column):
            continue
        adj.setdefault(e.from_table, []).append((e, False))
        adj.setdefault(e.to_table, []).append((e, True))
    return adj


def _step_of(edge: Edge, reversed_: bool) -> Step:
    if reversed_:
        return Step(edge=edge, enter_table=edge.to_table, enter_column=edge.to_column,
                    leave_table=edge.from_table, leave_column=edge.from_column,
                    reversed=True)
    return Step(edge=edge, enter_table=edge.from_table, enter_column=edge.from_column,
                leave_table=edge.to_table, leave_column=edge.to_column, reversed=False)


def _score(steps: tuple[Step, ...], hits_target: bool) -> float:
    score = 1.0
    for s in steps:
        score *= s.edge.confidence
        if s.edge.type == EDGE_LINEAGE:
            score *= TRANSFER_PENALTY
    # ⚠️ 跳数惩罚是**必需的**（见 `HOP_PENALTY` 的注释）：`declared`/`manual` 是 1.0，
    # 纯连乘时一跳与两跳同分，而 spec §5.2③ 要求「更长 → 更低」。
    score *= HOP_PENALTY ** max(len(steps) - 1, 0)
    if hits_target:
        score *= TARGET_BONUS
    return score


def _tiebreak(steps: tuple[Step, ...]) -> tuple:
    """**全序**破平键 —— 覆盖整条边序列（表/列/方向/**来源档**）。

    只写一部分会让两条只差一个字段的路径打平，砍哪条取决于输入顺序
    （`joins.py:138-143` 的既有教训：那不是全序）。
    `origin` 必须在键里：**`search_paths` 的契约是「任意边列表上全序」** —— 它不能假设
    输入已被某个加载器去重。同端点、同列、只有 `origin` 不同的两条边仍可能一起进图
    （测试直接构造 / 将来的平台适配器 / 别的调用方）；而 `declared` 与 `manual` **同档**
    （置信度都是 1.0），分数分不开它们 —— 少了这一档，排序就退回输入顺序
    （同一个问题两次问出不同的图）。
    （血缘加载层另有按列四元组去重 —— 那是**它**的收口，不构成本函数的假设。）
    """
    return tuple(
        (s.edge.type, s.enter_table, s.enter_column, s.leave_table, s.leave_column, s.reversed,
         s.edge.origin)
        for s in steps
    )


def search_paths(
    edges: list[Edge],
    anchor: str,
    *,
    targets: list[str] | None = None,
    max_hops: int = MAX_HOPS,
    per_hop_limit: int = PER_HOP_LIMIT,
    max_paths: int = MAX_PATHS,
    min_hops: int = 1,
) -> PathPlan:
    """从 `anchor` 出发、最多 `max_hops` 跳、**至少 `min_hops` 跳**的可达路径，按分数降序。"""
    target_set = {str(t) for t in (targets or [])}
    adj = _adjacency(edges)
    if anchor not in adj:
        return PathPlan(paths=[])

    explored = 0
    #: 每项 = (steps, visited_tables, score, transfers, hits_target)
    frontier: list[tuple[tuple[Step, ...], frozenset[str], float, int, bool]] = [
        ((), frozenset({anchor}), 1.0, 0, False)
    ]
    collected: list[tuple[tuple[Step, ...], float, int, bool]] = []

    for _ in range(max_hops):
        nxt: list[tuple[tuple[Step, ...], frozenset[str], float, int, bool]] = []
        for steps, visited, _score_so_far, _transfers, _hit in frontier:
            last = steps[-1].leave_table if steps else anchor
            for edge, reversed_ in adj.get(last, []):
                step = _step_of(edge, reversed_)
                if step.leave_table in visited:
                    continue  # 成环不产出
                new_steps = steps + (step,)
                new_visited = visited | {step.leave_table}
                hit = step.leave_table in target_set
                nxt.append((new_steps, new_visited, 0.0, 0, hit))

        # ⚠️ 排序键必须**全序**：先分数（降序）、再用 _tiebreak（升序）。
        nxt.sort(key=lambda item: (-_score(item[0], item[4]), _tiebreak(item[0])))
        nxt = nxt[:per_hop_limit]
        # ⚠️ `explored` 计在**截断之后**：它表达「这一跳实际展开了多少」。
        # 计在截断前的话每跳上限形同虚设（`test_per_hop_limit_bounds_explosion` 必红），
        # 而且变异 #3（删掉 `[:per_hop_limit]`）**也不会变红** —— 那反证了语义是「截断后」。
        explored += len(nxt)
        for steps, visited, _s, _t, hit in nxt:
            collected.append((steps, _score(steps, hit), sum(
                1 for s in steps if s.edge.type == EDGE_LINEAGE), hit))
        frontier = nxt

    # ⚠️ **按 `min_hops` 过滤必须发生在截断之前**：`search_paths` 按分数降序取前 `max_paths` 条，
    # 而 1 跳路径分数**天然最高**（少乘一次 `HOP_PENALTY`）—— 若留给调用方事后过滤，
    # 前 `max_paths` 早被 1 跳占满、真多跳路径一条都剩不下（`test_min_hops_drops_shorter_paths` 咬这一点）。
    floor = max(min_hops, 1)
    collected = [item for item in collected if len(item[0]) >= floor]
    collected.sort(key=lambda item: (-item[1], _tiebreak(item[0])))
    truncated = len(collected) > max_paths
    kept = collected[:max_paths]
    return PathPlan(
        paths=[Path(steps=steps, score=score, transfers=transfers, hits_target=hit)
               for steps, score, transfers, hit in kept],
        truncated=truncated,
        explored=explored,
    )
