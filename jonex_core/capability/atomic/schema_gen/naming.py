"""命名 —— 表名/列名 → 本体里的实体名、属性名、关系名。

两条硬约束，破了 nexonto 直接 422：

1. **关系名全局唯一**（大小写不敏感）。nexonto 的注释写得很直白：关系名是全局的，
   同名会被铸成同一个 OWL 属性，下游 YAML 转换也一律按名字索引，
   同名关系会被**静默合并**。所以这里的唯一化不是「锦上添花」，是正确性要求。

2. **属性名须匹配 `^[a-z][a-z0-9_]*$`**（这条是 confirm 级，不阻断导出，但会报一片黄）。

关系名策略：优先用简洁的 `REF_<列主干>`；撞名了再退成 `REF_<源实体>_<列主干>`；
还撞就加序号。这样 89 张表的规模下大部分名字保持可读。
"""

from __future__ import annotations

import re

from .relations import _stem

# 表名主干 → PascalCase 时的缩写保持。只收真正会看错的
_ACRONYMS = {
    "uc": "UC", "vm": "VM", "ai": "AI", "ci": "CI", "llm": "LLM",
    "api": "API", "url": "URL", "http": "HTTP", "mq": "MQ", "db": "DB",
    "id": "ID", "bg": "BG", "bu": "BU", "git": "Git", "k8s": "K8s",
    "saas": "SaaS", "prd": "PRD", "json": "JSON", "sql": "SQL",
}

_ATTR_OK = re.compile(r"^[a-z][a-z0-9_]*$")
_ATTR_BAD = re.compile(r"[^a-z0-9_]")


def table_stem(table: str) -> str:
    """去掉 `t_` / `txn_` 这类前缀，拿到业务主干。"""
    for p in ("t_", "txn_", "tbl_", "tb_"):
        if table.startswith(p):
            return table[len(p) :]
    return table


def entity_name(table: str) -> str:
    """`t_asset_uc_develop_apply` → `AssetUCDevelopApply`"""
    stem = table_stem(table)
    parts = [p for p in stem.split("_") if p]
    return "".join(_ACRONYMS.get(p, p.capitalize()) for p in parts) or stem.capitalize()


def attribute_name(column: str) -> tuple[str, bool]:
    """返回 (属性名, 是否被改写)。改写会被记进损失清单。"""
    if _ATTR_OK.match(column):
        return column, False
    s = _ATTR_BAD.sub("_", column.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = "c_" + s
    return s, s != column


def relation_basename(column: str) -> str:
    """`asset_type_id` → `REF_ASSET_TYPE`"""
    stem = _stem(column) or column
    return "REF_" + re.sub(r"[^A-Za-z0-9]+", "_", stem).upper().strip("_")


def inverse_name(source_entity: str) -> str:
    """逆关系名的**机械默认值**，供人工在 nexonto 画布上改进。

    关系名 `REF_<列主干>` 描述的是**目标**（`REF_ASSET_TYPE` = 指向资产类型），
    从目标那一端读回来就是「被谁引用」。所以默认取
    `REFERENCED_BY_<源实体>`：`AssetType —REFERENCED_BY_ASSET→ Asset`。

    Jonex 自己的 TBox 用的是语义命名（`HAS_STEP` ↔ `STEP_OF`，
    `NEXT_STEP` ↔ `PREV_STEP`），那种读感是手写出来的，机械规则给不了。
    所以这里是**起点不是终点**——nexonto 的关系属性面板有「逆关系名」输入框，
    人可以在那儿改成业务上真正说得通的名字。

    逆名不参与唯一性：Jonex 的 `_build_inverse_map` 只把它当**值**用来给
    incoming 边换标签，不当键。所以两条关系撞同一个逆名无害。
    """
    lead = re.sub(r"[^A-Za-z0-9]+", "_", source_entity).upper().strip("_")
    return f"REFERENCED_BY_{lead}"


def cardinality_of(column_is_unique: bool) -> str:
    """外键列若自身唯一，就是一对一；否则多对一。

    方向按「源 → 目标」读：`Asset.asset_type_id → AssetType` 是 many_to_one，
    即多个 Asset 指向一个 AssetType。
    """
    return "one_to_one" if column_is_unique else "many_to_one"


class RelationNamer:
    """关系名全局唯一化。按需降级，保证名字在简洁与唯一之间取平衡。"""

    def __init__(self) -> None:
        self._taken: set[str] = set()

    def reserve(self, base: str, source_entity: str) -> str:
        for candidate in self._candidates(base, source_entity):
            key = candidate.lower()
            if key not in self._taken:
                self._taken.add(key)
                return candidate
        # 理论上到不了这里：候选序列是无限的
        raise AssertionError(f"无法为 {base} 生成唯一关系名")

    def _candidates(self, base: str, source_entity: str):
        yield base
        # 退一级：把源实体名塞进来，靠「谁引用谁」区分
        lead = re.sub(r"[^A-Za-z0-9]+", "_", source_entity).upper().strip("_")
        stem = base[4:] if base.startswith("REF_") else base
        yield f"REF_{lead}_{stem}"
        # 最后兜底：序号。同名前缀的两条边确实存在时才会走到
        i = 2
        while True:
            yield f"REF_{lead}_{stem}_{i}"
            i += 1


def relation_description(
    source_table: str,
    column: str,
    column_comment: str | None,
    target_table: str | None,
    relation_source: str,
    evidence: str,
) -> str:
    """关系描述。nexonto 的 `checks.relation.descriptionMissing` 是 confirm 级提示，
    但描述本身对人工审阅有用——审的人需要知道这条边是声明的、猜的、还是注释里写的。"""
    origin = {
        "declared": "数据库声明外键",
        "comment": "列注释声明",
        "derived": "命名约定推导",
    }.get(relation_source, relation_source)

    bits = [f"{source_table}.{column} → {target_table or '（待定）'}"]
    if column_comment:
        bits.append(f"注释：{column_comment}")
    bits.append(f"来源：{origin}（{evidence}）")
    return "；".join(bits)
