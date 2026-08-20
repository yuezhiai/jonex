# -*- coding: utf-8 -*-
"""[jonex] 表级判定 prompt（table-parsing-retrieval-governance-plan.md §11.2 增补二）。

归属平台侧而非 vendored：避免与上游 LightRAG/raganything 升级冲突。

两个 prompt 均要求严格 JSON 输出；解析失败时调用方退回纯规则
（词表匹配 + 高基数列），不阻塞入库。
结果按 `hash(表标题 + 列名签名)` 缓存，命中零 LLM 开销。

2026-08-14 增补十分层架构：表头判定模型化合并进本调用（第 ②③ 层）——
同一次 LLM 调用输出 `header_row_indices` / `note_row_indices` / `levels`，
与对象类型/主键判定信息互补（知道表头才好判主键列）。
"""

TABLE_OBJECT_TYPE_PROMPT = """你是表格结构分析专家。请分析以下表格的结构信息，并严格按 JSON 格式输出（不得包含 JSON 以外的任何文字）：

{{
  "header_row_indices": [0, 1],
  "note_row_indices": [0],
  "levels": 2,
  "object_type": "品种",
  "pk_columns": ["代码及合约"],
  "qualifier_columns": ["品种"]
}}

字段说明：
- header_row_indices：表头行号（0 起，按输入的行顺序）。首行即表头则 [0]；说明性行（长文本/合并说明）不要列为表头，列入 note_row_indices。
- note_row_indices：表前说明行号（长文本、句末标点的说明文字），不计入表头与数据。无则 []。
- levels：表头层数（等于 header_row_indices 的长度）。
- object_type：每行数据代表的实体类型（如 品种/营业部/错误码/员工/客户/机构），使用简洁中文名词，映射到本体实体类型。
- pk_columns：能唯一标识一行实体的列名（复合主键可多列，按顺序给出）。
- qualifier_columns：辅助区分同名实体的限定列名（如「品种」限定「代码及合约」），无则 []。

表格信息：
表标题：{table_caption}
列名清单：{column_list}
数据行数：{row_count}
前 {sample_rows} 行样例（含行号）：
{row_samples}
"""

TABLE_COLUMN_MAPPING_PROMPT = """你是表格列名规范化专家。请把表格原始列名映射到标准属性名，严格按 JSON 格式输出（不得包含 JSON 以外的任何文字）：

{{
  "mapping": {{"业务联系人": "联系人", "营业部简称": "简称"}}
}}

规则：
- 只做归一化映射（同义列名合并），不要臆造新属性名；
- 原始列名与标准属性名相同时可省略该条目；
- 无法归一的列名保持原样。

原始列名清单：{column_list}
可选标准属性词表：{tbox_attributes}
"""
