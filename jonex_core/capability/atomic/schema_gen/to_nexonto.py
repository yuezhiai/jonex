"""Schema IR → nexonto ExportSchemaV1 YAML。

生成前跑一遍 nexonto 自己的 mustFix 规则（从 packages/domain/src/checks.ts 抄的），
保证导进去是 0 个红色阻断项。宁可在这里报错，也不要让用户导入时才发现。

一个刻意的取舍：**列的语义不塞进 entity.aliases**。
用户要的是「按字段名/字段注释也能召回到表」，但直接把 43 列的注释全灌进 aliases
会让别名表膨胀到几百条、触发一片 aliasConflict 告警，nexonto 画布上人会看瞎。
列的语义写在 attribute.name / attribute.description 里（本来就是它该在的地方），
由 M3 灌库时再并进 Jonex 的全文索引——那边才是检索真正发生的地方。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .exclude import exclusion_reason
from .ir import Schema, Table
from .naming import (
    RelationNamer,
    attribute_name,
    cardinality_of,
    entity_name,
    inverse_name,
    relation_basename,
    relation_description,
)
from .typemap import map_type
from .zh import short_alias, variants

# nexonto DomainIdSchema
_DOMAIN_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,47}$")

# nexonto checks.ts 里的 recommendedAttributeName
_ATTR_STYLE = re.compile(r"^[a-z][a-z0-9_]*$")

# nexonto: hasIriCharacters = 含 CJK 或 ASCII 字母数字
_CJK_OR_ALNUM = re.compile(r"[一-鿿]|[a-zA-Z0-9]")


@dataclass
class Issue:
    code: str
    level: str  # mustFix | confirm
    target: str


@dataclass
class Loss:
    """有损转换记录。降级不静默——这份清单要随产物一起交出去。"""

    kind: str
    where: str
    detail: str


@dataclass
class GenResult:
    document: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)
    losses: list[Loss] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def must_fix(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "mustFix"]

    @property
    def confirms(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "confirm"]


def table_ref(t: Table) -> str:
    """物理表的限定名 `<schema>.<table>`。

    ② NL→SQL 拿它当表名去查库，所以**必须是限定名** —— 表不在默认 schema 时，
    光给表名照样查不到（真机实测 `relation "knowledge_data_sources" does not exist`）。
    schema 缺省时退到 `public`（IR 的默认值，与 `schema_gen_api._to_ir` 的兜底一致）。
    """
    return f"{(t.schema or 'public').strip()}.{t.name}"


def generate(schema: Schema, domain_id: str, domain_name: str | None = None) -> GenResult:
    res = GenResult(document={})
    losses: list[Loss] = []
    issues: list[Issue] = []

    # ── 先剔除框架表与备份表 ──
    kept: list[Any] = []
    excluded: list[str] = []
    for t in schema.tables:
        reason = exclusion_reason(t.name)
        if reason:
            excluded.append(f"{t.name}（{reason}）")
            losses.append(Loss("表被排除", t.name, reason))
        else:
            kept.append(t)
    kept_names = {t.name for t in kept}

    # ── 短别名冲突检测 ──
    # `t_resource_summary` 和 `t_resource_summary_detail` 在源库里注释一模一样，
    # 短别名必然撞。撞了就不硬塞——退到完整注释；完整注释也撞，就只用表名。
    # 源库的数据质量问题应当在检索结果里显形，而不是被我们悄悄抹平。
    shorts = {t.name: short_alias(t.comment) for t in kept}
    short_use: dict[str, list[str]] = {}
    for name, s in shorts.items():
        if s:
            short_use.setdefault(s.lower(), []).append(name)
    full_use: dict[str, list[str]] = {}
    for t in kept:
        if t.comment:
            full_use.setdefault(t.comment.lower(), []).append(t.name)

    alias_fixups: list[str] = []

    # ── 实体 ──
    entity_types: list[dict[str, Any]] = []
    table_to_entity: dict[str, str] = {}

    for t in kept:
        ename = entity_name(t.name)
        table_to_entity[t.name] = ename

        attrs: list[dict[str, Any]] = []
        for c in t.columns:
            aname, renamed = attribute_name(c.name)
            if renamed:
                losses.append(Loss("属性名改写", f"{t.name}.{c.name}", f"→ `{aname}`（不符合 nexonto 属性名规范）"))

            base_type, lossy = map_type(c.pg_type)
            if lossy:
                losses.append(Loss("类型降级", f"{t.name}.{c.name}", f"{c.pg_type} → {base_type}"))

            attr: dict[str, Any] = {
                "name": aname,
                # 物理列名：`attribute_name()` 可能把 `"Academic Year"` 改写成
                # `academic_year`，但 ② NL→SQL 生成 SQL 必须用物理名 —— 否则
                # 查的是一张没有该列的表（2026-10-23 BIRD 实测 17 条 exec_failed）。
                "physical_name": c.name,
                "type": base_type,
                "required": (not c.nullable) or c.is_pk,
            }
            if c.is_pk:
                # nexonto: primaryKey && !required 是 mustFix，这里 required 已保证为真
                attr["is_primary_key"] = True
            if c.desc_core or c.comment:
                attr["description"] = c.desc_core or c.comment

            if c.enum_values:
                # 注释里内联了声明的取值 → 升格为 enum，取值随行
                vals = _dedupe_ci(c.enum_values)
                if len(vals) >= 2:
                    attr["type"] = "enum"
                    attr["enum_values"] = vals
                    losses.append(
                        Loss("类型升格", f"{t.name}.{c.name}", f"{base_type} → enum（注释内联了 {len(vals)} 个取值）")
                    )

            attrs.append(attr)

        # variants 自己会展开简体/繁体三态，这里只给候选片段。
        # 表名始终作为别名——用户很可能直接搜 `t_asset`
        #
        # ⚠️ display_name 必须走**同一套冲突判定**。先前只判了 aliases，
        # display_name 仍无条件取 short，于是 `t_resource_summary` 与
        # `t_resource_summary_detail`（源库里注释完全相同）拿到同一个显示名——
        # nexonto 不拦，但 **Jonex 的 TBox 导入把显示名也当 object name 校验**，
        # 直接 400：`Duplicate object name: 平台资源管理汇总`。
        short = shorts[t.name]
        label: str  # display_name 的值，必须唯一
        if short and len(short_use.get(short.lower(), [])) == 1:
            aliases = variants(short, t.name)
            label = short
        elif t.comment and len(full_use.get(t.comment.lower(), [])) == 1:
            aliases = variants(t.comment, t.name)
            label = t.comment
            if short:
                alias_fixups.append(f"{t.name}：短别名「{short}」与他表冲突，已退回完整注释")
        else:
            aliases = variants(t.name)
            # 注释也不唯一 → 显示名退回实体名（构造上已保证唯一）
            label = ename
            if short:
                alias_fixups.append(f"{t.name}：短别名「{short}」与他表冲突，且注释也不唯一，仅保留表名")

        ent: dict[str, Any] = {
            "name": ename,
            # 物理表标识：② NL→SQL 要拿它当表名去查。**必须是限定名** ——
            # 表不在默认 schema 时，光给表名照样查不到（真机实测：
            # `relation "knowledge_data_sources" does not exist`）。
            "source_object_id": table_ref(t),   # f"{t.schema}.{t.name}"，schema 缺失时用引擎默认
            "aliases": aliases,
            "attributes": attrs,
        }
        if t.comment:
            ent["display_name"] = label
            ent["description"] = t.comment
        entity_types.append(ent)

    # ── 关系（只收有目标的；待定的留给人工在画布上连） ──
    namer = RelationNamer()
    relation_types: list[dict[str, Any]] = []
    skipped_pending: list[str] = []

    for r in schema.relations:
        if not r.target_table:
            skipped_pending.append(f"{r.source_table}.{r.source_column}（{r.semantic_hint or '无提示'}）")
            continue
        if r.source_table not in kept_names or r.target_table not in kept_names:
            gone = r.target_table if r.target_table not in kept_names else r.source_table
            losses.append(
                Loss("关系丢弃", f"{r.source_table}.{r.source_column}", f"端点 {gone} 已被排除出本体")
            )
            continue

        src_e = table_to_entity.get(r.source_table)
        dst_e = table_to_entity.get(r.target_table)
        if not src_e or not dst_e:
            losses.append(Loss("关系丢弃", f"{r.source_table}.{r.source_column}", f"目标 {r.target_table} 不在实体集内"))
            continue

        col = None
        if (t := schema.table(r.source_table)) is not None:
            col = t.column(r.source_column)

        rname = namer.reserve(relation_basename(r.source_column), src_e)
        rel: dict[str, Any] = {
            "name": rname,
            "source": src_e,
            "target": dst_e,
            "description": relation_description(
                r.source_table, r.source_column, col.comment if col else None, r.target_table, r.source, r.evidence
            ),
            "cardinality": cardinality_of(bool(col and (col.is_unique or col.is_pk))),
            # 逆关系名：**纯声明**（Jonex 侧 `_build_inverse_map` 只拿它给 incoming 边
            # 换个读法，不建反向边；nexonto 侧注释同样写着「物理只存 canonical 方向」）。
            #
            # 这里给的是机械默认值，**供人工在 nexonto 的「逆关系名」输入框里改**
            # （Jonex 自己的约定是语义命名：HAS_STEP ↔ STEP_OF）。
            # 逆名不参与任何唯一性约束——它只是给作答 LLM 看的标签，
            # 所以同名无害，不需要像关系名那样全局唯一。
            "inverse_of": inverse_name(src_e),
            # join 键：两个端点的列名原样搬过去。`target_column` **可能为 None**
            # （命名推导找不到目标列时就是 None）—— 这里**不臆造**，由消费方
            # （Jonex ② NL→SQL）自己回落到目标实体的主键。DI 那边 `or "id"` 是
            # DI 的口径，不搬到这条链路上来。
            "source_column": r.source_column,
            "target_column": r.target_column,
            # 三源合并的来源档（declared 声明外键 / comment 注释声明 / derived 命名推导）：
            # 消费方按它排序截断，也按它判「这条边是声明的还是猜的」。
            "origin": r.source,
        }
        relation_types.append(rel)

    # ── 约束 ──
    constraints: list[dict[str, Any]] = []
    for t in kept:
        ename = table_to_entity[t.name]
        for c in t.columns:
            if c.is_unique and not c.is_pk:
                aname, _ = attribute_name(c.name)
                constraints.append(
                    {"type": "unique_attribute", "entity": ename, "attribute": aname, "severity": "error"}
                )

    doc: dict[str, Any] = {
        "version": 1,
        "domain": domain_id,
        "entity_types": entity_types,
        "relation_types": relation_types,
        "constraints": constraints,
        "disambiguation": {"case_insensitive": True, "alias_merge": True},
    }
    if domain_name:
        # nexonto 的导入端只认 domain id，显示名走 API 参数；这里保留信息供参考
        doc["_display_name"] = domain_name

    res.document = doc
    res.issues = _selfcheck(doc) if domain_id and _DOMAIN_ID.match(domain_id) else issues + [
        Issue("checks.domain.invalidId", "mustFix", domain_id)
    ]
    res.losses = losses
    res.stats = {
        "entities": len(entity_types),
        "attributes": sum(len(e["attributes"]) for e in entity_types),
        "enums": sum(1 for e in entity_types for a in e["attributes"] if a.get("type") == "enum"),
        "relations": len(relation_types),
        "relations_skipped_pending": len(skipped_pending),
        "constraints": len(constraints),
        "losses": len(losses),
        "tables_excluded": len(excluded),
        "alias_fixups": len(alias_fixups),
        "must_fix": len(res.must_fix),
        "confirm": len(res.confirms),
    }
    res._excluded = excluded  # type: ignore[attr-defined]
    res._alias_fixups = alias_fixups  # type: ignore[attr-defined]
    res._skipped_pending = skipped_pending  # type: ignore[attr-defined]
    return res


def _dedupe_ci(items: list[str]) -> list[str]:
    """nexonto 的 checks.attribute.duplicateEnum 是 mustFix：enum 取值不许大小写不敏感重复。"""
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        k = it.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _selfcheck(doc: dict[str, Any]) -> list[Issue]:
    """照搬 nexonto packages/domain/src/checks.ts 的判据，在我们这边先跑一遍。"""
    issues: list[Issue] = []
    if not _DOMAIN_ID.match(doc["domain"]):
        issues.append(Issue("checks.domain.invalidId", "mustFix", doc["domain"]))
    if not doc["entity_types"]:
        issues.append(Issue("checks.schema.empty", "confirm", doc["domain"]))

    entity_names: set[str] = set()
    for e in doc["entity_types"]:
        n = e["name"]
        if not n.strip():
            issues.append(Issue("checks.entity.nameRequired", "mustFix", "?"))
        if n.lower() in entity_names:
            issues.append(Issue("checks.entity.nameDuplicate", "mustFix", n))
        entity_names.add(n.lower())
        if not _CJK_OR_ALNUM.search(n):
            issues.append(Issue("checks.entity.nameStyle", "confirm", n))
        if not e["attributes"] and not e.get("description") and not e["aliases"]:
            issues.append(Issue("checks.entity.incomplete", "confirm", n))

        attr_names: set[str] = set()
        for a in e["attributes"]:
            an = a["name"]
            if an.lower() in attr_names:
                issues.append(Issue("checks.attribute.nameDuplicate", "mustFix", f"{n}.{an}"))
            attr_names.add(an.lower())
            if not _ATTR_STYLE.match(an):
                issues.append(Issue("checks.attribute.nameStyle", "confirm", f"{n}.{an}"))
            if a.get("is_primary_key") and not a.get("required"):
                issues.append(Issue("checks.attribute.primaryKeyRequired", "mustFix", f"{n}.{an}"))
            if a.get("type") == "enum":
                vals = a.get("enum_values") or []
                if not vals:
                    issues.append(Issue("checks.attribute.emptyEnum", "confirm", f"{n}.{an}"))
                if len({v.lower() for v in vals}) != len(vals):
                    issues.append(Issue("checks.attribute.duplicateEnum", "mustFix", f"{n}.{an}"))

    # 实体名大小写不敏感唯一已保证；关系名由 RelationNamer 保证，这里复核
    rel_names: set[str] = set()
    for r in doc["relation_types"]:
        rn = r["name"]
        if rn.lower() in rel_names:
            issues.append(Issue("checks.relation.duplicate", "mustFix", rn))
        rel_names.add(rn.lower())
        # entity_names 存的是小写（去重就按小写做的），比对时也要小写
        if r["source"].lower() not in entity_names:
            issues.append(Issue("checks.relation.missingEntity", "mustFix", f"{rn} source={r['source']}"))
        if r["target"].lower() not in entity_names:
            issues.append(Issue("checks.relation.missingEntity", "mustFix", f"{rn} target={r['target']}"))
        if not r.get("description"):
            issues.append(Issue("checks.relation.descriptionMissing", "confirm", rn))

    seen_c: set[str] = set()
    for c in doc["constraints"]:
        key = f"{c['type']}|{c.get('entity')}|{c.get('attribute')}"
        if key in seen_c:
            issues.append(Issue("checks.constraint.duplicate", "mustFix", key))
        seen_c.add(key)

    return issues


def dump_yaml(doc: dict[str, Any]) -> str:
    clean = {k: v for k, v in doc.items() if not k.startswith("_")}
    return yaml.safe_dump(
        clean, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096
    )
