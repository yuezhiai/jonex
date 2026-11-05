#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""离线验证 P2-3 选择器：给定一份编译 schema，测「金标表是否被选中」。

不连库、不调 LLM、可重复。输入来自 `ontology_compiled_schemas` 导出的
`{"entities": [...], "relations": [...]}`。

用法：

    PYTHONPATH=. python scripts/eval_schema_select.py schema.json [--max-tables 24] [--budget 12000]

做法：对每条 `target_column` 非空的关系造一道题——

- 问句：`<target 表> 中每个 <source 表> 有多少条记录`（中英混合，含两张表名，
  ���拟用户点名的场景）；
- 锚点：`target`；金标表：`{source, target}` 的物理表；
- 跑 `schema_select.select_schema`，看金标表是否都在选中集合里。

指标：**表对召回率**（两张表都选中）、平均选中表数、全量 vs 选择的 token 估算。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from capabilities.knowledge_base.services.nl2sql.schema_select import (  # noqa: E402
    CHARS_PER_TOKEN, DEFAULT_MAX_SELECTED_TABLES, DEFAULT_SCHEMA_BUDGET_TOKENS,
    estimate_schema_chars, select_schema)


def _table_of(entity: dict) -> str:
    return str(entity.get("source_object_id") or "").strip() or str(entity.get("name") or "")


def build_cases(schema: dict) -> list[dict]:
    entities = [e for e in schema.get("entities") or [] if isinstance(e, dict)]
    relations = [r for r in schema.get("relations") or [] if isinstance(r, dict)]
    cases: list[dict] = []
    for rel in relations:
        source = str(rel.get("source") or "").strip()
        target = str(rel.get("target") or "").strip()
        target_column = str(rel.get("target_column") or "").strip()
        if not source or not target or source == target:
            continue
        case = {
            "source": source, "target": target,
            "question": f"{target} 中每个 {source} 有多少条记录，按 {target} 分组计数",
            "gold": {source, target},
        }
        # 只要求 source 物理表在选中集合（target 是锚点，必然在）。
        cases.append(case)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("schema")
    parser.add_argument("--max-tables", type=int, default=DEFAULT_MAX_SELECTED_TABLES)
    parser.add_argument("--budget", type=int, default=DEFAULT_SCHEMA_BUDGET_TOKENS)
    args = parser.parse_args()

    schema = json.loads(pathlib.Path(args.schema).read_text(encoding="utf-8"))
    entities = [e for e in schema.get("entities") or [] if isinstance(e, dict)]
    relations = schema.get("relations") or []
    by_name = {str(e.get("name") or ""): e for e in entities}

    full_chars = estimate_schema_chars(entities)
    full_tokens = full_chars / CHARS_PER_TOKEN
    print(f"schema：{len(entities)} 表 / {len(relations)} 关系；"
          f"全量估算 {full_chars} 字符 ≈ {full_tokens:.0f} token")

    cases = build_cases(schema)
    if not cases:
        print("没有可造的 join 题")
        return 1

    hit_pairs = 0
    hit_source = 0
    selected_total = 0
    missed: list[str] = []
    for case in cases:
        preview, tables = select_schema(
            entities, relations, question=case["question"],
            anchor=case["target"], budget_tokens=args.budget,
            max_tables=args.max_tables)
        selected_total += len(preview)
        source_entity = by_name.get(case["source"])
        target_entity = by_name.get(case["target"])
        source_table = _table_of(source_entity) if source_entity else ""
        target_table = _table_of(target_entity) if target_entity else ""
        source_ok = source_table in tables
        target_ok = target_table in tables
        if source_ok and target_ok:
            hit_pairs += 1
        if source_ok:
            hit_source += 1
        else:
            missed.append(f"{case['source']} -> {case['target']}")

    n = len(cases)
    print(f"题数：{n}")
    print(f"表对召回：{hit_pairs}/{n}（{hit_pairs / n:.1%}）")
    print(f"源表召回：{hit_source}/{n}（{hit_source / n:.1%}）")
    print(f"平均选中表数：{selected_total / n:.1f}（上限 {args.max_tables}）")
    if missed:
        print("未召回：")
        for item in missed[:15]:
            print("  ", item)
        if len(missed) > 15:
            print(f"   … 另有 {len(missed) - 15} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
