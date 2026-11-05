"""把物理库的**声明外键**补进编译 schema 的 `relation_types`（2026-10-30）。

生成器（onto-intelligent）产出的关系图以语义关系为主，物理外键可能漏。
② 的 join 图只认 `relation_types`，漏一条 FK 就少一条可用 join 边。
本模块在 `generate_schema` 保存前做一次**确定性合并**：

- 只补 catalog 里真实声明的 FOREIGN KEY，`origin="declared"`；
- 表 → 实体按 `source_object_id` 精确匹配（缺失时回退实体名；同名歧义不猜）；
- 已存在同向或反向边就跳过（join 图会按锚点翻正，不重复加）；
- 自环跳过；名称冲突时追加 `_2`、`_3`。

纯函数，不连库；失败（结构不对）由调用方决定是否吞掉 —— 这里只处理「能解析的部分」。
"""
from __future__ import annotations


def _norm(value: str) -> str:
    return str(value or "").strip().strip('"`[]').casefold()


def _tail(value: str) -> str:
    return _norm(value).split(".")[-1]


def _entity_index(entity_types: list) -> tuple[dict, dict]:
    """`(精确键→实体, 末段键→[实体])`；同一末段多个实体时不猜。"""
    exact: dict[str, str] = {}
    tails: dict[str, list[str]] = {}
    for entity in entity_types or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        key = _norm(entity.get("source_object_id") or "") or _norm(name)
        if key:
            exact.setdefault(key, name)
        tails.setdefault(_tail(key), []).append(name)
    return exact, tails


def _resolve(table_name: str, schema: str, exact: dict, tails: dict) -> str:
    for candidate in (f"{schema}.{table_name}" if schema else "", table_name):
        key = _norm(candidate)
        if key and key in exact:
            return exact[key]
    matches = {m for m in tails.get(_tail(table_name), [])}
    return next(iter(matches)) if len(matches) == 1 else ""


def merge_declared_fks(entity_types: list, relation_types: list,
                       catalog: dict | None) -> tuple[list[dict], int]:
    """返回 `(merged_relations, added_count)`；catalog 结构见 `read_catalog`。"""
    relations = [dict(r) for r in (relation_types or []) if isinstance(r, dict)]
    exact, tails = _entity_index(entity_types)
    seen: set[tuple[str, str, str, str]] = set()
    names = {str(r.get("name") or "") for r in relations}
    for rel in relations:
        key = (_norm(rel.get("source")), _norm(rel.get("source_column")),
               _norm(rel.get("target")), _norm(rel.get("target_column")))
        seen.add(key)
        seen.add((key[2], key[3], key[0], key[1]))   # 反向也算已存在

    added = 0
    for table in (catalog or {}).get("tables") or []:
        if not isinstance(table, dict):
            continue
        source_entity = _resolve(str(table.get("name") or ""),
                                 str(table.get("schema") or ""), exact, tails)
        if not source_entity:
            continue
        for fk in table.get("foreign_keys") or []:
            if not isinstance(fk, dict):
                continue
            column = str(fk.get("column") or "").strip()
            ref_table = str(fk.get("ref_table") or "").strip()
            ref_column = str(fk.get("ref_column") or "").strip()
            if not column or not ref_table or not ref_column:
                continue
            target_entity = _resolve(ref_table, "", exact, tails)
            if not target_entity or target_entity == source_entity:
                continue
            key = (_norm(source_entity), _norm(column),
                   _norm(target_entity), _norm(ref_column))
            if key in seen:
                continue
            base = f"FK_{source_entity}_{column}_{target_entity}_{ref_column}".upper()
            base = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in base)[:56]
            name = base
            suffix = 2
            while name in names:
                name = f"{base}_{suffix}"
                suffix += 1
            names.add(name)
            relations.append({
                "name": name,
                "source": source_entity,
                "target": target_entity,
                "source_column": column,
                "target_column": ref_column,
                "origin": "declared",
                "cardinality": "many_to_one",
            })
            seen.add(key)
            seen.add((key[2], key[3], key[0], key[1]))
            added += 1
    return relations, added


__all__ = ["merge_declared_fks"]
