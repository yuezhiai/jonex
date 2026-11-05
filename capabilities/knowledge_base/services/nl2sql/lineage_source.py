"""血缘边的来源。

**运行时来源 = 表**（`knowledge_base.lineage_edges`，迁移 010）：管理面（KB 设置的
「血缘」页签）往里写，② 的路径搜索从表里读 —— 见 `load_lineage_edges_from_db`。

**文件加载器 `load_lineage_edges()` 保留**，但**不再是运行时注入源**：它是「file 来源」
的解析器（P1 的摄取通道会复用它），现有单测继续钉它的契约（坏输入退空图、四元组去重）。

> 沿革：第一版刻意只读磁盘 JSON，理由是「没有管理面就没有人往表里写，表相比文件只多
> 一层 CRUD 且没有消费者」。**管理面已随本线交付 ⇒ 那个理由不再成立。**
"""
from __future__ import annotations

import json
import logging
import os

from .forbidden import ForbiddenPair
from .paths import EDGE_LINEAGE, Edge, confidence_of

logger = logging.getLogger(__name__)

#: 默认路径；可用环境变量覆盖（换目录 / 测试用）。
LINEAGE_EDGES_PATH = os.environ.get(
    "NL2SQL_LINEAGE_EDGES_PATH",
    "deploy/config/nl2sql/lineage_edges.json",
)

_REQUIRED = ("from_table", "from_column", "to_table", "to_column")

#: 解析结果缓存：`(路径, mtime)` → 边列表。
#:
#: **为什么保留**（不是过早优化）：文件加载器的用途已收窄为「file 来源」的解析器
#: （P1 的摄取通道会复用它）；真实血缘导出可能上万条，同一份导出被反复调用时
#: 没有缓存就是每次重读重解析。⚠️ **运行时（② 提问）已不再走这里** —— 它读表，
#: 见 `load_lineage_edges_from_db`。
#:
#: **键必须是 mtime**：真机走查会**改这个 JSON 文件并期望立刻生效**；
#: 纯模块级缓存不失效，会让人把走查结果误判成失败。
_CACHE: dict[tuple[str, float], list[Edge]] = {}


def _parse(target: str) -> list[Edge]:
    """真正解析文件。**任何读取/解析问题都退回空图，不抛** ——

    「没有血缘来源」是合法状态（血缘为空时路径退化为纯 join 图，主线功能不受影响），
    不该让一个配置文件的毛病把问数打挂。
    """
    try:
        with open(target, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:  # 读不了 / 不是合法 JSON
        logger.warning("lineage edges unreadable at %s: %s", target, exc)
        return []

    rows = raw.get("edges") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        logger.warning("lineage edges at %s: unexpected shape", target)
        return []

    #: 四元组 → 保留下来的那条边（保序：值按首次出现位置排列）。
    best: dict[tuple[str, str, str, str], Edge] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if any(not str(row.get(k) or "").strip() for k in _REQUIRED):
            # 单条脏边跳过、整批不失败。
            continue
        edge = Edge(
            type=EDGE_LINEAGE,
            from_table=str(row["from_table"]),
            from_column=str(row["from_column"]),
            to_table=str(row["to_table"]),
            to_column=str(row["to_column"]),
            # 未知 origin 照收 —— 置信度由 `confidence_of` 兜底（保守档）。
            origin=str(row.get("origin") or ""),
        )
        key = (edge.from_table, edge.from_column, edge.to_table, edge.to_column)
        prev = best.get(key)
        if prev is None or confidence_of(edge.origin) > confidence_of(prev.origin):
            best[key] = edge
    return list(best.values())


def load_lineage_edges(path: str | None = None) -> list[Edge]:
    """读血缘边（带 `(路径, mtime)` 键控缓存）。**签名不变，坏输入一律退空图**。

    加载期按**列四元组**（from_table/from_column/to_table/to_column）去重，同一四元组
    只保留 `origin` 置信度最高的一条：同一个列映射挂两个来源等级在语义上就是脏数据，
    在加载层收口比在排序层兜更对。置信度并列时**先出现者胜**（稳定的确定性规则）。

    缓存语义：
    - 命中 = 路径与 mtime 都不变；文件被改写（mtime 变）→ 重新解析。
    - 文件不存在 / stat 失败 → **清掉该路径的缓存条目**：否则文件被删后又被恢复成
      相同 mtime 时会静默吐陈旧边。
    - **返回浅拷贝**（`list(cached)`）：`Edge` 是 frozen、元素改不动，但缓存里的那个
      *列表*可变 —— 浅拷贝（微秒级）挡住调用方原地改动（append/pop/排序）污染后续调用。
    """
    target = path or LINEAGE_EDGES_PATH
    try:
        mtime = os.stat(target).st_mtime
    except OSError:
        # 文件没了 / stat 不了：清掉**该路径的全部**旧条目（缓存键是 `(路径, mtime)`，
        # 所以要按 `k[0]` 筛，不能 pop 字符串路径），别让陈旧边活着。
        for stale in [k for k in _CACHE if k[0] == target]:
            del _CACHE[stale]
        return []

    key = (target, mtime)
    cached = _CACHE.get(key)
    if cached is not None:
        return list(cached)

    edges = _parse(target)
    _CACHE[key] = edges
    # 同一路径的旧 mtime 条目不再有用 —— 顺手清掉，缓存不会随 mtime 变化无限增长。
    for stale in [k for k in _CACHE if k[0] == target and k != key]:
        del _CACHE[stale]
    return list(edges)


async def load_lineage_edges_from_db(session, tenant_id: str, kb_id: str,
                                     ds_id: str) -> list[Edge]:
    """**运行时**的血缘来源：从 `knowledge_base.lineage_edges` 读该 KB + 该源的活边。

    为什么不做缓存（对比文件加载器的 `(路径, mtime)` 缓存）：这条查询走
    `idx_lg_edge_scope`、一次提问一次、且紧挨着一次 LLM 调用 —— 相对成本可忽略，
    而缓存会引入「管理页刚改完、② 还是旧边」的不一致窗口。**要加缓存先证明成本。**

    失败一律 **fail-open 到空图**（表不存在 / 库抖动）：血缘是**增能项**，
    它坏了不该让提问坏掉（与文件加载器「坏输入退空图」同款）。
    """
    from sqlalchemy import select

    from ...models.lineage_edge import LineageEdge
    try:
        stmt = select(LineageEdge).where(
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.knowledge_base_id == kb_id,
            LineageEdge.data_source_id == ds_id,
            LineageEdge.is_deleted == 0,
        )
        rows = list((await session.execute(stmt)).scalars().all())
        # 行→Edge 映射也在 try 内：它是「读表」的最后一步，出了编程错误
        # （字段名拼错/ORM 形状变了）也应走下面同一套判定，而不是被调用点
        # 的宽泛 fail-open 吞成空图。**编程错误照抛**，否则 bug 会静默伪装成
        # 「没有血缘」。
        return [
            Edge(type=EDGE_LINEAGE, from_table=str(r.from_table), from_column=str(r.from_column),
                 to_table=str(r.to_table), to_column=str(r.to_column),
                 origin=str(r.origin or ""))
            for r in rows
        ]
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 —— 增能项，坏了一律退空图
        logger.warning("血缘边读表失败，本次退空图：%s", exc)
        return []


async def load_forbidden_pairs_from_db(session, tenant_id: str, kb_id: str,
                                       ds_id: str) -> list[ForbiddenPair]:
    """运行时禁止对：只取该源 `status=active` 的活行。

    失败一律 **fail-open 到空列表**（表不存在 / 库抖动）：禁止同查是增能约束，
    它坏了不该让提问坏掉。编程错误照抛（同 `load_lineage_edges_from_db` 的重抛集合）。
    """
    from sqlalchemy import select

    from ...models.lineage_forbidden_pair import LineageForbiddenPair
    try:
        stmt = select(LineageForbiddenPair).where(
            LineageForbiddenPair.tenant_id == tenant_id,
            LineageForbiddenPair.knowledge_base_id == kb_id,
            LineageForbiddenPair.data_source_id == ds_id,
            LineageForbiddenPair.status == "active",
            LineageForbiddenPair.is_deleted == 0,
        )
        rows = list((await session.execute(stmt)).scalars().all())
        return [ForbiddenPair(table_a=str(r.table_a), table_b=str(r.table_b))
                for r in rows]
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 —— 增能项，坏了一律退空
        logger.warning("禁止同查表对读表失败，本次退空：%s", exc)
        return []
