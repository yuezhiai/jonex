"""Schema 中间表示（IR）。

三层来源汇聚到这里：
  ① 数据库 catalog（表/列/主键/外键/COMMENT）—— 可验证的事实
  ② 命名约定推导（`asset_type_id` → `t_asset_type`）—— 猜测，但可解释
  ③ 中文注释解析（`关联資產ID（外键，引用 t_asset 表）`）—— 猜测，但带证据

IR 刻意保留每条关系的来源与原文证据，因为下游要在 nexonto 画布上人工审，
审的人需要知道「这条边是数据库声明的、猜的、还是注释里写的」。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

# ── 关系来源 ────────────────────────────────────────────────────────────
# declared: information_schema 里声明的外键约束 —— 可验证
# comment:  列注释里写明的关联 —— 有原文证据
# derived:  命名约定推导 —— 纯猜测
RelSource = Literal["declared", "comment", "derived"]

# 关系可用性。pending 的 target_table 为空，是留给人工在 nexonto 上连线的「待定关联」
RelStatus = Literal["certain", "likely", "pending"]


@dataclass
class EnumParse:
    """从列注释里解析出的内联枚举。"""

    values: list[str]
    # 注释里列举取值的那一段原文，例如 "狀態：1-正常 / 2-已回收 / 3-已刪除"
    evidence: str


@dataclass
class RefHint:
    """从列注释里解析出的关联声明。target_table 为空表示注释只写了语义没写目标。"""

    raw: str
    target_table: str | None = None
    target_column: str | None = None
    # 没写目标表时，注释里的语义提示，例如「申请」——供人工连线时参考
    semantic_hint: str | None = None


@dataclass
class Column:
    name: str
    # 原始 PG 类型，如 "character varying(128)"
    pg_type: str
    # 归一化后的基础类型，由 typemap 填充
    base_type: str = "text"
    nullable: bool = True
    default: str | None = None
    comment: str | None = None

    is_pk: bool = False
    is_unique: bool = False

    # ── 注释解析产物 ──
    enum_values: list[str] = field(default_factory=list)
    enum_evidence: str | None = None
    ref_hint: RefHint | None = None
    # 剔除枚举取值段与关联括注后的核心描述，用于给人工看
    desc_core: str | None = None


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class Table:
    name: str
    schema: str = "public"
    comment: str | None = None
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    uniques: list[list[str]] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    def column(self, name: str) -> Column | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None


@dataclass
class Candidate:
    """待定关联的候选目标。人工在 nexonto 上连线时，看到的是这份带理由的短名单，
    而不是空白的「请自行判断」。"""

    table: str
    reason: str
    score: int


@dataclass
class Conflict:
    """三源对同一列给出了不同的目标。不自动裁决，原样抛给人工。"""

    source_table: str
    source_column: str
    winner: str
    loser: str
    detail: str


@dataclass
class Relation:
    """合并后的关系边。三源去重后每 (source_table, source_column) 至多一条。"""

    source_table: str
    source_column: str
    target_table: str | None
    target_column: str | None

    source: RelSource
    status: RelStatus
    # 原文证据：声明的约束名、推导的规则、或注释片段
    evidence: str
    # 注释里读到的语义提示，用于给边起名字
    semantic_hint: str | None = None

    # 三源都对这一列有话说时，把落选的并排记下来。人工审的就是这些
    also_seen: list[str] = field(default_factory=list)
    # target_table 为空时的候选短名单
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class Schema:
    database: str
    schema_name: str
    tables: list[Table] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    taken_at: str = ""

    def table(self, name: str) -> Table | None:
        for t in self.tables:
            if t.name == name:
                return t
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ── 统计，供报告使用 ──
    def stats(self) -> dict[str, Any]:
        cols = [c for t in self.tables for c in t.columns]
        by_src: dict[str, int] = {}
        for r in self.relations:
            by_src[r.source] = by_src.get(r.source, 0) + 1
        return {
            "tables": len(self.tables),
            "columns": len(cols),
            "tables_with_comment": sum(1 for t in self.tables if t.comment),
            "columns_with_comment": sum(1 for c in cols if c.comment),
            "columns_with_enum": sum(1 for c in cols if c.enum_values),
            "columns_with_ref_hint": sum(1 for c in cols if c.ref_hint),
            "declared_fks": sum(len(t.foreign_keys) for t in self.tables),
            "relations_total": len(self.relations),
            "relations_by_source": by_src,
            "relations_pending": sum(1 for r in self.relations if r.status == "pending"),
        }
