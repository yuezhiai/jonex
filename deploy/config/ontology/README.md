# 本体建模文件（TBox + 公理 + 推理规则）

本目录存放可导入的 TBox 建模文件。每套建模由**两个文件**组成：

| 文件 | 作用 | 导入接口 |
| --- | --- | --- |
| `<domain>.yaml` | TBox：实体类型、关系类型、约束，以及顶层 `axioms:`（类层次 / 关系层次 / 数据属性层次 / 特征 / propertyChain / datatypeFacet） | `POST /api/v1/knowledge-base/ontology/compiled-schema/yaml/import`（multipart） |
| `<domain>.rules.yaml` | 推理规则（`version: 2 / kind: reasoning_rules / contract_version: 1`） | `POST /api/v1/knowledge-base/ontology/reasoning-rules/import`（JSON） |

> `axioms` 必须放在 TBox 文件里，随编译 YAML 导入一起落到 `knowledge_base.ontology_axioms`；
> 抽象 schema 导入（`/ontology/abstract-schema/import`）**不处理** `axioms` 段。
> 规则是独立的 B 档内容，只走规则导入接口。
>
> 导入后可在「编译配置 → 公理」子页签只读查看并启停：停用 = `status: proposed`，
> 启用前/停用前会返回读时展开影响面并重校验规则；新增规则 Violation 默认阻断。

## 文件清单

| 建模 | TBox | 规则 | 定位 |
| --- | --- | --- | --- |
| 通用企业 | `generic_enterprise.yaml` | `generic_enterprise.rules.yaml` | 跨行业通用层：组织 / 人 / 产品 / 事件 / 地点 / 文档 / 概念 / 方法 / 规程 |
| 科技产品 | `technology.yaml` | `technology.rules.yaml` | 消费电子、计算硬件、软件工具链、AI 模型、机器人 |
| 图书出版 | `nanfang_publishing.yaml` | `nanfang_publishing.rules.yaml` | 书系 / 分卷 / 单册、图版与画作、出版流程、版权 |
| 奥特莱斯零售 | `sasseur_fashion.yaml` | `sasseur_fashion.rules.yaml` | 商场 / 楼层 / 门店、品牌与活动、促销、服装与穿搭 |
| 硬件互联网财报 | `hardware_internet_finance.yaml` | `hardware_internet_finance.rules.yaml` | 上市公司 / 子公司 / 分部 / 产品线、财务与运营指标、ESG、风险 |

`default.yaml` 仍是抽取兜底本体，保持轻量、不承载层级；本目录新增的通用层需要显式导入。

## 导入顺序

1. **先导入 TBox**（实体/关系/约束 + 公理）；
2. **再导入规则**（规则引用 TBox 中的类型和关系名，顺序反了会报 `checks.rule.ref_unknown`）。

建议先用 `dry_run` 确认差异，再正式写入：

```bash
# 1) TBox（multipart；首次导入 expected_schema_version=0）
curl -X POST "http://<gateway>/api/v1/knowledge-base/ontology/compiled-schema/yaml/import" \
  -H "Authorization: Bearer <token>" \
  -H "X-Tenant-ID: tenant_jonex_demo" \
  -F "knowledge_base_id=<kb_id>" \
  -F "expected_schema_version=0" \
  -F "dry_run=true" \
  -F "file=@deploy/config/ontology/generic_enterprise.yaml"

# 2) 规则（JSON；dry_run 只算 plan，不写库）
python - <<'PY'
import json, pathlib
payload = {
    "knowledge_base_id": "<kb_id>",
    "yaml_text": pathlib.Path("deploy/config/ontology/generic_enterprise.rules.yaml").read_text(encoding="utf-8"),
    "dry_run": True,
    "mode": "merge",
}
print(json.dumps(payload, ensure_ascii=False))
PY
# 将上面的 JSON 作为 body POST 到 /ontology/reasoning-rules/import
```

正式导入时 `dry_run=false`；`mode` 可选 `merge`（默认）或 `replace`。

## 层级表达口径

- **类层次**：`subClassOf`；大类型之间用 `disjointWith` 标互斥；不把别名写成 `equivalentClass`。
- **关系层次**：`subPropertyOf` 构造有意义的树（如 `HAS_VOLUME ⊑ HAS_PART`、`SUPPORTS_TOOLKIT ⊑ SUPPORTS`）；
  方向相反的一对关系（`HAS_PART` / `PART_OF`）**不互写 `inverse_of`**——编译器要求 `inverse_of` 是
  虚拟名，不能与已有关系名冲突，两者按独立关系声明即可。
- **数据属性层次**：同实体或子实体上的属性 `subPropertyOf`；父属性必须在父实体上真实存在。
- **关系特征**：`transitive` / `symmetric` / `irreflexive` / `asymmetric` 按语义声明。
- **propertyChain / datatypeFacet**：A 档公理，只展示与冲突检测，不参与试跑执行。
- **规则**：B 档，试跑/推理链只执行 `enabled` 且 `status=active` 的规则；推导不写图、不进作答。

## 规则编写约束

- 前提 1–4 条、变量最多 4 个、结论单原子；结论变量必须出现在前提中。
- 跨规则递归禁止；单条规则自身的**传递形式**允许（如 `?a P ?b AND ?b P ?c ⇒ ?a P ?c`）。
- 对称/链式规则如果结论谓词同时出现在前提中，会被 `checks.rule.recursion` 判为递归；
  本目录的规则已避开该形态（改用新的派生关系或类型结论）。
- 规则文件同时写 `text` 与 `premises/conclusion` AST；导入时会校验 `text_ast_drift`。

## 校验

本目录文件已用平台真实校验器跑过（`parse_yaml`、`check_axioms`、`check_rules`、
`check_rule_conflicts`、`OntologyCompiler._normalize_entity_types/_normalize_relation_types`），
当前结果：**0 Violation / 0 Warning**。

修改文件后建议重跑：

```bash
python -m compileall capabilities jonex_core
pytest tests/unit/test_rule_conformance.py tests/unit/test_rule_service.py -q
```

仓库内 `tests/` 与 `scripts/` 被 `.gitignore` 忽略；需要长期保留的校验脚本请放在 `deploy/` 下。
