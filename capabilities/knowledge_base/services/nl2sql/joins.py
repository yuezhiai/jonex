"""从编译 schema 的关系里挑出「锚点实体 1 跳内、且真能 join」的边。**纯函数**。

与 `guard.py` 同性质：不连库、不调模型，输入就是编译 schema 那一行里的
`relation_types` / `entity_types`（原样传进来，不预先加工），所以最该被单测钉死。

四条硬规矩，都是「宁可不给，也不给错的」：

1. **两端都要有物理表名**（`source_object_id`，限定名）。join 要写两张确定的表；
   这里**不**沿用单表那档的「null 回退实体名」—— 回退会编出一张真源上未必存在的表。
2. **列必须真的存在**：锚点端列不在锚点实体的列清单里 → 丢。对端分两档 ——
   目标列**没说**（None / 空，命名推导拿不准时的正常输入）→ **回落对端实体的主键**
   （读 `is_primary_key` 标记，不是硬编码 `id`），连主键都没有 → 丢；
   目标列**说了、但对端没有这列**（只可能来自脏数据：关系的端点被改过、旧端点的列名
   被带了过来）→ **丢边**。两档混在一起会把脏数据洗成一条**没人声明过**的等值条件。
3. **翻正**：锚点可能是边的 `target` 端，注入的每条边里 `from_*` 恒为锚点那一端。
4. **上限 `MAX_JOINS`**：超了按 `origin`（declared > comment > derived）排序截断，
   并把 `truncated` 带出去 —— 调用方据此告诉模型「这份图是被砍过的」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .forbidden import pair_key

#: 注入给模型的上限。实测 jinpin 的 join 图平均度 2.1、最高 30 —— 绝大多数实体碰不到上限。
MAX_JOINS = 20

#: 截断优先级（小者优先）：
#: **人工声明 / 演化采纳（declared / evolved）** > 注释声明（comment）
#: > 值包含（value，P2-3）> 命名推导（derived）。
#: `evolved` 是人审确认过的演化补边（`origin="evolved"`），与 `declared` 同档、
#: 绝不低于 `derived` —— 否则人工补的边会在 `MAX_JOINS` 截断时先被砍掉。
#: `value` 排 `comment` 之后：注释是人的声明、值包含是统计证据（低基数列上可能巧合）——
#: 两者都比纯命名推导硬。
#: **`origin` 缺失按 `derived` 处理**：既不夸大它的可信度，也让排序稳定。
_ORIGIN_RANK = {"declared": 0, "evolved": 0, "comment": 1, "value": 2,
                "derived": 3}
_FALLBACK_ORIGIN = "derived"


@dataclass(frozen=True)
class JoinPlan:
    """一次提问能用的 join 图。三个字段一起交给注入层，别拆开传。"""

    joins: list[dict] = field(default_factory=list)
    related_tables: list[dict] = field(default_factory=list)
    truncated: bool = False


def _columns(entity: dict) -> dict[str, dict]:
    """实体 → {列名: 属性 dict}。"""
    out: dict[str, dict] = {}
    for attr in entity.get("attributes") or []:
        if isinstance(attr, dict) and str(attr.get("name") or ""):
            out[str(attr["name"])] = attr
    return out


def _as_columns(columns: dict[str, dict]) -> list[dict]:
    return [{"name": name, "type": str(attr.get("type") or "")} for name, attr in columns.items()]


def _primary_key(columns: dict[str, dict]) -> str | None:
    for name, attr in columns.items():
        if attr.get("is_primary_key"):
            return name
    return None


def eligible_joins(
    relations: list[dict] | None,
    entities: list[dict] | None,
    anchor: str,
    *,
    cap: int = MAX_JOINS,
    forbidden_pairs=None,
) -> JoinPlan:
    """锚点实体 1 跳内、可用（两端的列都真的存在）的 join 边。

    `forbidden_pairs`（禁止同查）：端点命中的边**连给都不给** —— 让模型没有
    「直连」这个选项；安全闸是最后的兜底，不是第一道。
    """
    by_name: dict[str, dict] = {}
    for entity in entities or []:
        if isinstance(entity, dict) and str(entity.get("name") or ""):
            by_name[str(entity["name"])] = entity

    anchor_entity = by_name.get(anchor)
    if anchor_entity is None:
        return JoinPlan()

    forbidden_keys = {p.key for p in (forbidden_pairs or ())}

    candidates: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()

    for rel in relations or []:
        if not isinstance(rel, dict):
            continue
        source, target = rel.get("source"), rel.get("target")
        if anchor not in (source, target) or source == target:
            continue
        other_entity = by_name.get(target if source == anchor else source)
        if other_entity is None:
            continue

        from_table = anchor_entity.get("source_object_id")
        to_table = other_entity.get("source_object_id")
        if not from_table or not to_table:
            continue  # 规矩 1：没有物理表名，join 不出来

        if source == anchor:
            from_column, to_column = rel.get("source_column"), rel.get("target_column")
        else:  # 规矩 3：锚点在 target 端 → 翻正
            from_column, to_column = rel.get("target_column"), rel.get("source_column")

        anchor_columns = _columns(anchor_entity)
        other_columns = _columns(other_entity)
        if not from_column or from_column not in anchor_columns:
            continue  # 规矩 2：锚点列不存在
        # 两档必须分开（混在一起会把脏数据洗成一条**没人声明过**的等值条件）：
        #  · 目标列**没说**（None / 空）→ 回落对端主键。这是正常输入：命名推导拿不准目标列时
        #    就是这一档；回落读的是 `is_primary_key` 标记，不是硬编码 `"id"`。
        #  · 目标列**说了、但对端没有这列** → **丢边**。这一档只可能来自脏数据（关系的端点被
        #    改过，旧端点的列名被带过来了）—— 回落主键会把它变成一条模型看不出来源的假条件。
        if not to_column:
            to_column = _primary_key(other_columns)
            if to_column is None:
                continue
        elif to_column not in other_columns:
            continue

        if pair_key(from_table, to_table) in forbidden_keys:
            # 禁止同查：这对表不允许在同一个查询里同时出现 —— 连边都不给，
            # 让模型没有「直连」这个选项（安全闸是最后的兜底，不是第一道）。
            continue

        key = (from_table, from_column, to_table, to_column)
        if key in seen:
            continue
        seen.add(key)

        origin = str(rel.get("origin") or _FALLBACK_ORIGIN)
        candidates.append({
            "rank": _ORIGIN_RANK.get(origin, len(_ORIGIN_RANK)),
            "join": {
                "from_table": from_table,
                "from_column": from_column,
                "to_table": to_table,
                "to_column": to_column,
                "cardinality": rel.get("cardinality"),
                "origin": origin,
            },
            "columns": _as_columns(other_columns),
        })

    # 同档内按表名/列名字典序 —— 截断结果必须可复现，否则同一个问题两次问出不同的图。
    # 键必须覆盖**整个去重键** `(from_table, from_column, to_table, to_column)`：只写后面两项时，
    # 两条只差 `from_column` 的候选会打平，砍哪条取决于输入顺序（不是全序）。
    candidates.sort(key=lambda item: (
        item["rank"], item["join"]["from_table"], item["join"]["from_column"],
        item["join"]["to_table"], item["join"]["to_column"]))

    truncated = len(candidates) > cap
    if not truncated:
        kept = candidates
    else:
        # 截断前先保证**每个目标表**至少一条边，再按原顺序补满剩余名额。
        # 只按四元组字典序砍，万向表（如 BIRD 的 `Player` 有 22 条 Match 平行外键）
        # 会把配额吃光，别的表整张被挤出白名单——模型写 join 后被安全闸判越权。
        def _key(item: dict) -> tuple:
            j = item["join"]
            return (j["from_table"], j["from_column"], j["to_table"], j["to_column"])

        kept = []
        picked: set[tuple] = set()
        seen_tables: set[str] = set()
        for item in candidates:
            table = item["join"]["to_table"]
            if table in seen_tables:
                continue
            seen_tables.add(table)
            kept.append(item)
            picked.add(_key(item))
        for item in candidates:
            if len(kept) >= cap:
                break
            if _key(item) in picked:
                continue
            kept.append(item)
            picked.add(_key(item))
        kept = kept[:cap]

    related: dict[str, list[dict]] = {}
    for item in kept:
        related.setdefault(item["join"]["to_table"], item["columns"])

    return JoinPlan(
        joins=[item["join"] for item in kept],
        related_tables=[{"table": table, "columns": columns} for table, columns in sorted(related.items())],
        truncated=truncated,
    )


def all_join_edges(relations: list[dict] | None,
                   entities: list[dict] | None,
                   *,
                   forbidden_pairs=None) -> list[dict]:
    """**全量** join 边（不按锚点过滤、不截断）—— 专门喂路径搜索。

    spec §6.1：「边池给足，截断落在路径上」。`eligible_joins` 是**锚点 1 跳**的注入用池
    （第一行就把非锚点边滤掉、末尾再砍到 `cap`）—— 拿它当路径边池，等于**先把路径砍了**。

    **刻意不另写一份列校验**：`eligible_joins` 的校验与锚点强耦合（锚点端必须显式给列、
    对端可回落主键），照抄一份必然产生行为漂移。这里改为**对每个实体各跑一次 1 跳再并集**
    —— 等价于「不限锚点」，且每条边都是从它自己那端按既有规则严格校验出来的。
    代价 O(实体数 × 关系数)，与既有 `service.py:651-661` 的扇出同量级。

    **无向去重**：同一对表被两侧各声明一次（`A→B` 与 `B→A`）只留一条 —— join 边本来就对称
    （见 `paths._adjacency`）。同键保留 `origin` **最高档**（`_ORIGIN_RANK` 小者优先）。
    """
    by_name: dict[str, dict] = {}
    for entity in entities or []:
        if isinstance(entity, dict) and str(entity.get("name") or ""):
            by_name[str(entity["name"])] = entity

    cap = max(len(relations or []), 1)   # 不截断：单锚点的候选数不可能超过关系总数
    # ⚠️ **先按 `origin` 档从高到低喂给 `eligible_joins`**：它内部按**有序**四元组去重、
    # 且**先到先得**（`joins.py` 的 `seen`）—— 同一对表被两侧各声明一次（`A→B` 的 `derived`
    # 与 `B→A` 的 `declared`）时，在**同一个锚点**上折叠成同一条有序边，先出现的那条留下。
    # 不排序 ⇒ 低档边先出现会吃掉高档边，本函数承诺的「同键保留最高档」落空
    # （`test_all_join_edges_dedupes_both_directions` 咬这一点）。
    ranked = sorted(
        (rel for rel in (relations or []) if isinstance(rel, dict)),
        key=lambda rel: _ORIGIN_RANK.get(str(rel.get("origin") or ""), len(_ORIGIN_RANK)))
    best: dict[tuple, tuple[int, dict]] = {}
    for anchor in by_name:
        for join in eligible_joins(ranked, entities, anchor, cap=cap,
                                   forbidden_pairs=forbidden_pairs).joins:
            key = tuple(sorted([(join["from_table"], join["from_column"]),
                                (join["to_table"], join["to_column"])]))
            rank = _ORIGIN_RANK.get(str(join.get("origin") or ""), len(_ORIGIN_RANK))
            if key not in best or rank < best[key][0]:
                best[key] = (rank, join)
    return [join for _, join in sorted(
        best.values(),
        key=lambda item: (item[0], item[1]["from_table"], item[1]["from_column"],
                          item[1]["to_table"], item[1]["to_column"]))]
