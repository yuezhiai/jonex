# 校验与冲突规则 id（contract_version = 1）

两侧实现必须对同一输入产出相同的 `code`、`severity`（和 target/params 的语义）。
`message` 可按 locale 生成，不参与一致性比较。

## 公理校验

| code | severity | 判据 |
| --- | --- | --- |
| `checks.axiom.kind_whitelist` | Violation | kind 不在白名单 |
| `checks.axiom.status_invalid` | Violation | status 不在 proposed / active / rejected |
| `checks.axiom.source_invalid` | Violation | source 不在 manual / imported / extracted |
| `checks.axiom.origin_invalid` | Violation | origin 不在 studio / platform / imported |
| `checks.axiom.missingRef` | Violation | subject/object/chain 缺失，或引用的 kind 与公理要求不符（如 subClassOf 引用了 relation） |
| `checks.ref.entity_exists` | Violation | 实体引用不存在或已 deprecated |
| `checks.ref.relation_exists` | Violation | 关系引用不存在或已 deprecated |
| `checks.ref.attribute_exists` | Violation | 属性引用（含 owner）不存在或已 deprecated |
| `checks.axiom.chainInvalid` | Violation | propertyChain 为空、短于 2 或 chain 里不是 relation |
| `checks.axiom.facetInvalid` | Violation | datatypeFacet 的 facets 为空或 facet 形状不合法 |
| `checks.axiom.duplicate` | Violation | 同一 KB 内两条公理语义重复（变量/引用相同） |
| `checks.axiom.selfReference` | Violation | `A subClassOf A`、`R subPropertyOf R` 等自反 |
| `checks.hierarchy.cycle` | Violation | `subClassOf` / `subPropertyOf` 各自成环；`equivalentClass` 的 A≡B 对不算环 |
| `checks.axiom.conflict` | Violation | `disjointWith` 与 `equivalentClass` 矛盾；symmetric+asymmetric、reflexive+irreflexive；子关系与父关系特征矛盾 |
| `checks.axiom.characteristic_risk` | Warning | `transitive` + `functional` / `inverseFunctional` |
| `checks.hierarchy.unsatisfiable` | Violation | 闭包上不可满足的类（同时落在互斥类下、subClassOf 与 disjointWith 冲突、等价类与互斥类相交） |
| `checks.hierarchy.property_domain_range` | Violation / Warning | 子属性端点与父属性端点不相交 → Violation；更宽但相交 → Warning |
| `checks.hierarchy.property_datatype` | Violation | 子数据属性与父数据属性的数据类型不兼容 |
| `checks.hierarchy.sub_of_inverse` | Warning | `R subPropertyOf S`，而 S 是 R 的 `inverse_of` |
| `checks.hierarchy.overlaps_relation` | Warning | 类之间既有 subClassOf 又有 `PART_OF / BELONGS_TO` 一类的关系 |

## 规则语法与冲突

| code | severity | 判据 |
| --- | --- | --- |
| `checks.rule.syntax` | Violation | 解析失败；报告行、列和期望 token |
| `checks.rule.ref_unknown` | Violation | 关系名/类名不存在或已 deprecated |
| `checks.rule.unsafe_variable` | Violation | 结论变量未在前提中出现 |
| `checks.rule.type_mismatch` | Violation | 变量在不同原子里的类型约束矛盾（端点 + subClassOf 闭包） |
| `checks.rule.limits` | Violation | 前提 > 4 条或变量 > 4 个 |
| `checks.rule.text_ast_drift` | Warning | `text` 重解析后的 AST 与存储 AST 不一致（以 AST 为准） |
| `checks.rule.duplicate` | Violation | 两条规则变量重命名后 AST 相同 |
| `checks.rule.subsumed` | Warning | 结论相同，一条规则的前提是另一条的超集（前提多的冗余） |
| `checks.rule.redundant_with_axiom` | Warning | 规则等价于已有公理；A 档公理不参与试跑，只提示语义重复 |
| `checks.rule.disjoint_conflict` | Violation | 结论把变量归入 C，而前提已把同一变量限定在与 C disjointWith 的类上 |
| `checks.rule.characteristic_conflict` | Violation | 结论 `?a R ?a` 而 R irreflexive；或由 `?a R ?b` 推出 `?b R ?a` 而 R asymmetric |
| `checks.rule.cardinality_risk` | Warning | 结论关系是 functional 或基数一对一，可能推出多个值 |
| `checks.rule.recursion` | Violation | 规则依赖图成环（单条规则自身的传递形式除外） |
| `checks.rule.depends_on_inactive` | Warning | 前提关系已 deprecated，或依赖的公理已 rejected |

## 执行范围

- 冲突检测只在 `enabled && status == active` 的规则之间进行；
- 停用规则仍参与 `syntax`、`ref_unknown`、`text_ast_drift`；
- 公理校验在导入、`dry_run`、merge 时执行；Violation 拒绝写入。
