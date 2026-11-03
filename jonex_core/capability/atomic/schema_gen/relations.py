"""三源关系合并 —— 声明外键 / 注释声明 / 命名推导。

这个库的现实：声明外键 8 条，注释自称关联 68 条，命名推导 30 条。
只吃声明外键会丢掉 88% 的关系。所以三个来源都要，但不能简单叠加——
它们会在同一列上打架。

合并规则：
  按 (源表, 源列) 归并。优先级 declared > comment > derived。
  落选的**不丢弃**，并排记进 `also_seen` 抛给人工。冲突本身是信息：
  `t_asset_tag_rel.asset_id` 注释说是 `t_asset_instance`，命名看着像 `t_asset`，
  哪个对只有业务知道——这正是 nexonto 画布存在的意义。

目标为空时不留死胡同：出一份带理由的候选短名单。
"""

from __future__ import annotations

import re

from .comments import TableIndex, parse
from .ir import Candidate, Relation, Schema

# 列名后缀，剥掉之后才是「它指向什么」。
# 只收 _id / _code：`sort_no`、`version_no` 这类是业务编号不是外键，
# `_key` 语义太杂。收窄词表比放宽匹配更能压住误报。
_REL_SUFFIX = re.compile(r"_(id|code)$", re.I)

_CANDIDATE_LIMIT = 5

# 候选理由的排序权重
_SCORE_EXACT = 100

# 后缀匹配要求的最短主干。`bu`、`bg` 这种两三字的缩写撞上 `budget`、
# 表名里任意含 `bg` 的表，全是噪声
_MIN_STEM_FOR_SUFFIX = 4


def _stem(column: str) -> str | None:
    """`asset_type_id` → `asset_type`。剥不掉后缀就说明这列不像是外键。"""
    m = _REL_SUFFIX.search(column)
    if not m:
        return None
    stem = column[: m.start()]
    return stem or None


def _table_stem(table: str) -> str:
    return table[2:] if table.startswith("t_") else table


def _prefix_tokens(table: str) -> set[str]:
    """表名的中文分词前缀集。`t_asset_uc_develop_resource` → {t_asset, t_asset_uc, t_asset_uc_develop}"""
    parts = table.split("_")
    return {"_".join(parts[:i]) for i in range(2, len(parts))}


def _affinity(a: str, b: str) -> int:
    """两表共享的最长前缀有几个词。同族的表通常指向彼此。"""
    shared = _prefix_tokens(a) & _prefix_tokens(b)
    return max((len(s.split("_")) for s in shared), default=0)


def _find_target(
    stem: str, index: TableIndex, self_table: str
) -> tuple[str | None, list[Candidate], str | None]:
    """把列名主干撞到表名上。返回 (目标, 候选, 匹配方式)。

    只有两种匹配算数：
      精确 —— 表名主干与列名主干完全相等（`asset_type` → `t_asset_type`）
      后缀 —— 表名主干以 `_<主干>` 结尾（`instance` → `t_asset_instance`）

    刻意**不做**子串包含匹配。曾经加过，结果 27 个 `bu_id` 撞上了 `t_budget`。
    短主干（`bu`/`bg`）做子串匹配等于随机连线，宁可留成待定让人工来。

    后缀命中多于一个时不算歧义到底——按表名前缀亲缘性排序，把同族的排前面：
    `t_asset_uc_develop_resource.apply_id` 的候选里，
    `t_asset_uc_develop_apply` 比 `t_asset_deactivate_apply` 更可能是对的。
    """
    exact: list[str] = []
    suffix: list[str] = []

    if len(stem) >= _MIN_STEM_FOR_SUFFIX:
        tail = "_" + stem
        for t in index.tables:
            if t == self_table:
                continue
            ts = _table_stem(t)
            if ts == stem:
                exact.append(t)
            elif ts.endswith(tail):
                suffix.append(t)

    if len(exact) == 1:
        return exact[0], [], "exact"
    if len(exact) > 1:
        return None, [Candidate(t, "表名主干完全匹配", _SCORE_EXACT - i) for i, t in enumerate(exact)], None

    if len(suffix) == 1:
        return suffix[0], [], "suffix"
    if suffix:
        ranked = sorted(suffix, key=lambda t: (-_affinity(t, self_table), t))
        cands: list[Candidate] = []
        for i, t in enumerate(ranked[:_CANDIDATE_LIMIT]):
            aff = _affinity(t, self_table)
            reason = f"表名尾部与列名主干同名，且与源表共享 {aff} 层前缀" if aff else "表名尾部与列名主干同名"
            cands.append(Candidate(t, reason, _SCORE_EXACT - 10 - i))
        return None, cands, None
    return None, [], None


def merge(schema: Schema, index: TableIndex) -> list[Relation]:
    """跑三源，归并，返回去重后的关系表。同时把 relationship 冲突写回 schema。"""
    # (源表, 源列) → 各来源的提案
    proposals: dict[tuple[str, str], dict[str, Relation]] = {}

    def put(rel: Relation, key: str) -> None:
        proposals.setdefault((rel.source_table, rel.source_column), {})[key] = rel

    for t in schema.tables:
        for c in t.columns:
            # ── 源 A：声明外键（权威） ──
            for fk in t.foreign_keys:
                if fk.column != c.name:
                    continue
                put(
                    Relation(
                        source_table=t.name,
                        source_column=c.name,
                        target_table=fk.ref_table,
                        target_column=fk.ref_column,
                        source="declared",
                        status="certain",
                        evidence="pg_constraint FOREIGN KEY",
                    ),
                    "declared",
                )

            # ── 源 B：注释声明 ──
            p = parse(c.comment, index, t.name)
            if p.ref:
                if p.ref.target_table:
                    put(
                        Relation(
                            source_table=t.name,
                            source_column=c.name,
                            target_table=p.ref.target_table,
                            target_column=p.ref.target_column,
                            source="comment",
                            status="likely",
                            evidence=p.ref.raw,
                            semantic_hint=p.ref.semantic_hint,
                        ),
                        "comment",
                    )
                else:
                    # 注释只写了语义没写目标。留个占位，等源 C 来补
                    put(
                        Relation(
                            source_table=t.name,
                            source_column=c.name,
                            target_table=None,
                            target_column=None,
                            source="comment",
                            status="pending",
                            evidence=p.ref.raw,
                            semantic_hint=p.ref.semantic_hint,
                        ),
                        "comment",
                    )

            # ── 源 C：命名推导 ──
            stem = _stem(c.name)
            if stem:
                target, cands, kind = _find_target(stem, index, t.name)
                why = {
                    "exact": f"列名主干 `{stem}` 与表名主干完全一致",
                    "suffix": f"列名主干 `{stem}` 与表名尾部同名",
                }.get(kind or "", f"列名主干 `{stem}` 未能匹配到表")
                put(
                    Relation(
                        source_table=t.name,
                        source_column=c.name,
                        target_table=target,
                        target_column="id" if target else None,
                        source="derived",
                        status="likely" if target else "pending",
                        evidence=why,
                        candidates=cands,
                    ),
                    "derived",
                )

    return _resolve(proposals)


_PRIORITY = {"declared": 0, "comment": 1, "derived": 2}


def _resolve(proposals: dict[tuple[str, str], dict[str, Relation]]) -> list[Relation]:
    out: list[Relation] = []
    for by_src in proposals.values():
        ordered = sorted(by_src.values(), key=lambda r: _PRIORITY[r.source])
        primary = ordered[0]
        win = primary
        # 被借用的那个来源，记账时要排除——它贡献了目标，不是落选者
        borrowed: Relation | None = None

        # 优先级最高的来源没给出目标时，向更低优先级借一个——
        # 注释写「关联申请ID」没指明表，命名推导指出 `t_xxx_apply`，这种合流是有价值的
        if win.target_table is None:
            for alt in ordered[1:]:
                if alt.target_table:
                    borrowed = alt
                    win = Relation(
                        source_table=primary.source_table,
                        source_column=primary.source_column,
                        target_table=alt.target_table,
                        target_column=alt.target_column,
                        source=primary.source,  # 保留更强的证据等级
                        status="likely",
                        evidence=f"{primary.evidence}；目标由{_SRC_CN[alt.source]}补全",
                        semantic_hint=primary.semantic_hint,
                        candidates=alt.candidates,
                    )
                    break

        # 落选者：给出了目标、且与最终目标不同的那些来源。
        # 「没指明目标」的不算落选——那是信息缺失，不是分歧
        final = win.target_table
        win.also_seen = [
            f"{_SRC_CN[a.source]}：{a.target_table}"
            for a in ordered
            if a is not primary and a is not borrowed and a.target_table and a.target_table != final
        ]

        # 仍是悬空的，把三方的候选并起来去重
        if win.target_table is None:
            seen: set[str] = set()
            merged: list[Candidate] = []
            for a in ordered:
                for c in a.candidates:
                    if c.table in seen:
                        continue
                    seen.add(c.table)
                    merged.append(c)
            win.candidates = sorted(merged, key=lambda c: -c.score)[:_CANDIDATE_LIMIT]

        out.append(win)

    return sorted(out, key=lambda r: (r.source_table, r.source_column))


_SRC_CN = {"declared": "声明外键", "comment": "列注释", "derived": "命名推导"}
