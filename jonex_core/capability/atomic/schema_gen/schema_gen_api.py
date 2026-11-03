"""catalog JSON → Jonex TBox 的生成入口（供 HTTP 用）。

Jonex 那侧已经把「读 catalog」移植过去了（`capabilities/knowledge_base/services/schema_source/`），
**生成这一半留在本仓**。本模块把两者接起来：接一份 catalog 结构，吐 entity_types / relation_types。

## 为什么不传数据库凭证

Jonex 的 `_db_catalog_source()` 在自己进程里解密密码、自己连库、自己读 catalog，
只把**结构**送过来。于是：凭证不出 Jonex；本仓不需要装三个数据库驱动、也不需要能连到目标库；
两边的 IR 本来就是对齐的（`schema_source/base.py` 的移植注释写着这事）。

## 生成逻辑一行都没重写

全程复用既有模块，顺序与 `m1_introspect` → `m2_generate` → `to_tbox` 完全一致：

    catalog → comments.build_index / parse → relations.merge → to_nexonto.generate → to_tbox.convert

**关系与枚举的真值在中文注释里**（实测 assets_r2：声明外键只有 8 条，
注释里自称关联的有 68 列），所以第 2 步的注释解析不能省——这正是「只把表变成实体」
那种简化做法会丢掉的东西。

## 已知限制

`typemap.py` 是按 **PostgreSQL 类型名**写的，而 Jonex 的 catalog 刻意保留引擎原始类型串
（`base.py` 注释：「不做归一化——类型映射是下游的事」）。postgres 已实测验证；
**mysql / sqlserver 未验证**，本模块会在响应的 `warnings` 里如实标出，不假装能用。
"""

from __future__ import annotations

from typing import Any

from . import typemap
from .comments import build_index, parse
from .ir import Column, ForeignKey, Schema, Table
from .relations import merge
from .to_nexonto import generate
from .to_tbox import convert

#: 类型映射被校准过的引擎。其余引擎会在 warnings 里提示未验证。
_VERIFIED_ENGINES = {"postgres", "postgresql", "pg"}


def _to_ir(catalog: dict[str, Any]) -> Schema:
    """Jonex `SchemaCatalog` 的 dict → 本仓 `ir.Schema`。

    字段名两边本来就对齐，这里只做搬运，外加补两个 Jonex 侧没有的派生量：

    - `base_type`：由 `typemap.base_of()` 从引擎原始类型串推
    - `is_unique`：Jonex 的 catalog 不读唯一约束（本 POC 也不用它生成别的东西）
    """
    tables: list[Table] = []
    for raw_table in catalog.get("tables") or []:
        columns: list[Column] = []
        for raw_col in raw_table.get("columns") or []:
            engine_type = str(raw_col.get("type") or "")
            columns.append(
                Column(
                    name=str(raw_col["name"]),
                    pg_type=engine_type,
                    base_type=typemap.base_of(engine_type),
                    nullable=bool(raw_col.get("nullable", True)),
                    default=raw_col.get("default"),
                    comment=raw_col.get("comment"),
                    is_pk=bool(raw_col.get("is_pk", False)),
                )
            )

        # 外键三元组缺一不可——服务端读 catalog 时已经保证，这里再挡一次，
        # 免得半条外键让下游关系合并出无目标的边
        foreign_keys = [
            ForeignKey(
                column=str(fk["column"]),
                ref_table=str(fk["ref_table"]),
                ref_column=str(fk["ref_column"]),
            )
            for fk in (raw_table.get("foreign_keys") or [])
            if fk.get("column") and fk.get("ref_table") and fk.get("ref_column")
        ]

        tables.append(
            Table(
                name=str(raw_table["name"]),
                schema=str(raw_table.get("schema") or "public"),
                comment=raw_table.get("comment"),
                columns=columns,
                primary_key=[str(x) for x in (raw_table.get("primary_key") or [])],
                foreign_keys=foreign_keys,
            )
        )

    return Schema(
        database=str(catalog.get("database") or ""),
        schema_name=tables[0].schema if tables else "public",
        tables=tables,
    )


def generate_from_catalog(
    catalog: dict[str, Any],
    domain_id: str,
    domain_name: str = "",
) -> dict[str, Any]:
    """catalog → TBox。

    返回的 `entity_types` / `relation_types` 可以直接喂给 Jonex 的
    `PUT /ontology/compiled-schema`（连同 `knowledge_base_id` 与 `data_source_id`）。

    **失败不抛异常**：探测类接口给可读信息比给堆栈有用（同 di 的 `/test` 风格）。
    """
    warnings: list[str] = []
    engine = str(catalog.get("engine") or "").lower()
    if engine and engine not in _VERIFIED_ENGINES:
        warnings.append(
            f"类型映射按 PostgreSQL 类型名校准，{engine} 的原始类型串可能被误判（本引擎未验证）"
        )

    schema = _to_ir(catalog)
    if not schema.tables:
        return {"ok": False, "error": "catalog 里没有表——先确认数据源能读到库表结构"}

    # 与 m1_introspect.py:28-35 同一套解析，顺序也不能换：
    # 先把解析结果写回列，再合并关系——merge 依赖列上的 ref_hint
    index = build_index(schema.tables)
    for table in schema.tables:
        for column in table.columns:
            parsed = parse(column.comment, index, table.name)
            if parsed.enum:
                column.enum_values = parsed.enum.values
                column.enum_evidence = parsed.enum.evidence
            column.ref_hint = parsed.ref
            column.desc_core = parsed.desc_core
    schema.relations = merge(schema, index)

    result = generate(schema, domain_id, domain_name)
    tbox = convert(result.document)
    document = tbox.document

    return {
        "ok": True,
        "entity_types": document.get("entity_types") or [],
        "relation_types": document.get("relation_types") or [],
        "stats": {**(result.stats or {}), **(tbox.stats or {})},
        "losses": [
            f"{loss.kind} · {loss.where} — {loss.detail}" for loss in (result.losses or [])
        ],
        "warnings": warnings + list(tbox.notes or []),
    }
