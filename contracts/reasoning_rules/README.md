# 本体推理契约（reasoning_rules）

> `contract_version = 1`
> 归属：平台仓 `jonex-platform`；Studio 仓 `nexonto` 复制同一份契约与用例。

本目录是「类层次 / 属性层次 / 推理规则」两侧实现的**唯一契约源**。平台（Python）与
Studio（TypeScript）各自实现解析与校验，但必须对同一份 `conformance.json` 得出相同结论。

## 文件

| 文件 | 作用 |
| --- | --- |
| `axioms.schema.json` | 公理导入/导出文件（`kind: ontology_axioms`）的 JSON Schema |
| `rules.schema.json` | 推理规则导入/导出文件（`kind: reasoning_rules`）的 JSON Schema |
| `issues.schema.json` | 校验/冲突问题列表的 JSON Schema |
| `conformance.schema.json` | `conformance.json` 的元 Schema |
| `conformance.json` | 两侧 CI 共跑的一致性用例 |
| `grammar.md` | 规则 DSL v1 文法、保留字、引号与逆名归一 |
| `rule-ids.md` | 校验与冲突规则 id、级别、判据 |

## 变更流程

1. 先改本目录的 Schema / 文法 / 用例，升 `contract_version`；
2. 平台与 Studio 的测试都会因版本不一致或新用例失败，提醒两边同步实现；
3. 平台 `GET /ontology/reasoning/contract` 返回 `{contract_version, axiom_kinds, rule_grammar_version}`；
   导入、merge、发布响应也回带 `contract_version`；
4. Studio 发布前先核对版本，不一致直接失败。

## 同步到 Studio

平台仓是源。`contracts/reasoning_rules/` 整体复制到 Studio 仓 `packages/domain/src/rules/__fixtures__/`
（保留 `conformance.json` 与 Schema），并在两侧 CI 校验 `conformance.json` 的 SHA-256 一致。

> 注意：平台仓的 `tests/` 被 `.gitignore` 忽略，契约与用例**不能**放在 `tests/` 下；本目录是 tracked 路径。

## 级别映射

契约内的 `severity` 使用中性词：

- `Violation`：阻断保存 / 导入 / 启用 / 发布；
- `Warning`：报告但放行；
- `Info`：仅提示。

Studio 的 `ExportIssueLevel` 映射：`Violation → mustFix`、`Warning → confirm`、`Info → info`。
平台错误响应仍用现有错误码（1001 参数/校验、1003 冲突、1004 开关关闭）。

## 一致性比较口径

- `expect.issues` 按 `(code, severity)` 去重后比较；用例给了 `target` / `params` 时，两边也必须一致；
- `expect.ast` 比较归一后的 AST（变量名、逆名、引号写法都已归一）；
- 同一输入可能产生多条同 code 的问题（例如多个环），一致性用例只要求该 code 至少出现一次；具体条数与定位由各自单测覆盖；
- 不用 `message` 做比较，文案按 locale 生成。

## 引用写法

- 公理引用：`{kind: "entity" | "relation" | "attribute", ref: "<名称>", owner?: "<实体名>"}`；
  属性引用**必须**带 `owner`（属性名只在实体内唯一）。
- 规则中的关系/类引用：写名称或别名；属性层次的引用同样用 `owner` 消歧。
- 变量名在 AST 中写裸名（`a`），在文本里写 `?a`。
