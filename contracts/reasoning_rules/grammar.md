# 推理规则 DSL v1

`rule_grammar_version = 1`

## 文法

```text
rule   := "IF" atom ("AND" atom)* "THEN" atom
atom   := var REL var          # ?a 隶属于 ?b
        | var "是" CLASS       # ?a 是 锅炉
var    := "?" IDENT
IDENT  := [A-Za-z_][A-Za-z0-9_]*
REL    := 关系名 / 关系别名 / 某个关系的 inverse_of 名
CLASS  := 实体名 / 实体别名
```

示例：

```text
IF ?a 隶属于 ?b AND ?b 隶属于 ?c THEN ?a 隶属于 ?c
```

## 词法与保留字

- 关键字：`IF`、`AND`、`THEN`、`是`（大小写敏感，按完整 token 匹配）。
- 关系名、类名、别名是自由文本。含空格、空串或与关键字同名时，用**双引号**包裹，例如
  `IF ?a "直接 隶属" ?b THEN ?a 隶属于 ?b`。
- 双引号内的 `"` 用 `\"` 转义；不允许跨行。
- 名称解析复用生效 schema 的 `disambiguation`（`case_insensitive`、`alias_merge`）。
- 文本上限 8192 字符；解析器先做长度闸，再做词法分析。

## 逆名归一

图里只存 canonical 方向。`REL` 写的是某个关系的 `inverse_of` 名时，在解析阶段归一为
canonical 关系，并交换两个变量：

```text
# 关系 隶属于 的 inverse_of = 下属
IF ?a 下属 ?b THEN ?a 下属 ?b
# 归一后：
premises:  [{kind: rel, s: b, p: 隶属于, o: a}]
conclusion:{kind: rel, s: b, p: 隶属于, o: a}
```

`text` 保留作者原写法；AST 存归一后的结果。`checks.rule.text_ast_drift` 比较的是
「重新解析并归一后的 AST」，所以逆名写法不会被误报。

## 限制（v1）

- 前提 1–4 条；变量最多 4 个；结论单原子。
- 结论中的变量必须出现在前提中（安全性）。
- 不支持常量、否定、比较/算术内置（如 `?t > 850`）、多原子结论、为属性赋值。
  遇到这些写法报 `checks.rule.syntax`，不静默降级。
- 跨规则递归禁止（`checks.rule.recursion`）；单条规则自身的传递形式允许。

## 错误定位

语法错误必须报告 `line`（1 起）、`column`（1 起）和期望的 token。平台与 Studio 的
行列口径一致：按 Unicode 码点计数，不按字节；`\r\n` 按一个换行处理。
