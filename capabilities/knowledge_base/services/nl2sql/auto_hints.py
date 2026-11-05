#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""生产侧 hints 自动组装（A2 的第二半）。

**为什么在 Jonex 而不是 router**：权威 schema（compiled / abstract）住在 Jonex，
router 没有库表结构；让 router 每次提问回查两个 schema 端点会多两次 HTTP +
一套缓存，且所有入口（router、演化台、直连 ask）都得各自接一遍。这里用**同一份
已经读进内存的** schema 行组装，天然覆盖所有调用方。

来源与优先级（高 → 低）：
1. 锚点实体的 query_rule 口径（人审确认过的业务规则）；
2. 指标定义（抽象 schema 的 metrics，按问句词面命中）；
3. 实体别名 / 抽象实体描述（术语对齐）；
4. 属性描述 + 枚举值（BIRD-style evidence：`values: ...`、列语义）；
5. 1 跳相关实体的属性描述（补 join 表的列语义）。

只保留**本次锚点实体**及其 1 跳邻居，避免把整库 schema 灌进提示词
（论文实测全量 schema 反而降分）。输出满足 Jonex `hints` 的边界：
≤10 条、单条 ≤500 字、合计 ≤2000 字。
"""
from __future__ import annotations

import re

from .hints import MAX_HINT_CHARS, MAX_HINTS, MAX_HINTS_TOTAL_CHARS

#: 锚点实体之外最多再带几个 1 跳邻居的属性说明。
MAX_RELATED_ENTITIES = 2
#: 属性描述截断上限（再长对模型只增噪声）。
MAX_DESCRIPTION_CHARS = 320
#: 单条属性 hint 的硬上限（与 hints 通道一致）。
MAX_ATTRIBUTE_HINT_CHARS = MAX_HINT_CHARS


def _as_text(value) -> str:
    return str(value or "").strip()


def _clean_description(text: str) -> str:
    """去掉生成器拼进来的 `commonsense evidence:` 前缀并压空白/截断。"""
    text = " ".join(_as_text(text).split())
    marker = "commonsense evidence:"
    idx = text.lower().find(marker)
    if idx != -1:
        text = text[idx + len(marker):].strip()
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return text


def _matches(question: str, *terms: str) -> bool:
    """问句词面命中（大小写不敏感；中文直接子串）。空词不命中。"""
    q = question.casefold()
    for term in terms:
        t = _as_text(term).casefold()
        if t and t in q:
            return True
    return False


_STOPWORDS = {
    "the", "and", "for", "are", "was", "were", "with", "that", "this", "from",
    "have", "has", "had", "not", "but", "all", "any", "its", "his", "her",
    "their", "them", "they", "you", "your", "can", "will", "one", "two", "per",
    "into", "over", "under", "each", "every", "which", "what", "who", "how",
    "many", "much", "where", "when", "name", "names", "type", "value", "values",
}


def _description_matches(question: str, description: str) -> bool:
    """描述里的英文实词命中问句（`M = male, F = female` 对 `male clients`）。

    ⚠️ 必须过滤停用词：`the` 这类 3 字母词几乎命中所有问句，会把**每条**属性描述
    都抬成高优先级，related 表的列反而被挤出 10 条上限（BIRD 实测的全量噪声源）。
    """
    q = question.casefold()
    words = [w for w in re.findall(r"[a-z]{4,}", description.casefold())
             if w not in _STOPWORDS]
    return any(word in q for word in words)


def _find_entity(entities: list, name: str) -> dict | None:
    target = _as_text(name).casefold()
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        if _as_text(entity.get("name")).casefold() == target:
            return entity
    return None


def _abstract_for(abstract_entities: list, name: str) -> dict | None:
    entity = _find_entity(abstract_entities, name)
    if entity is not None:
        return entity
    target = _as_text(name).casefold()
    for candidate in abstract_entities or []:
        if not isinstance(candidate, dict):
            continue
        aliases = {_as_text(a).casefold() for a in (candidate.get("aliases") or [])}
        if target in aliases:
            return candidate
    return None


def _entity_aliases(abstract: dict | None, entity_name: str) -> list[str]:
    if not isinstance(abstract, dict):
        return []
    out: list[str] = []
    for alias in abstract.get("aliases") or []:
        text = _as_text(alias)
        if text and text.casefold() != entity_name.casefold() and text not in out:
            out.append(text)
    return out


def _attribute_hints(question: str, entity: dict, abstract: dict | None,
                     *, boost: int = 0) -> list[tuple[int, int, str]]:
    """一个实体的属性 → `(优先级, 序, 文本)`；优先级越高越先保留。"""
    entity_name = _as_text(entity.get("name"))
    abstract_attrs: dict[str, dict] = {}
    if isinstance(abstract, dict):
        for attr in abstract.get("attributes") or []:
            if isinstance(attr, dict) and _as_text(attr.get("name")):
                abstract_attrs[_as_text(attr["name"]).casefold()] = attr
    out: list[tuple[int, int, str]] = []
    for idx, attr in enumerate(entity.get("attributes") or []):
        if not isinstance(attr, dict):
            continue
        logical = _as_text(attr.get("name"))
        if not logical:
            continue
        physical = _as_text(attr.get("physical_name")) or logical
        abs_attr = abstract_attrs.get(logical.casefold()) or {}
        description = _clean_description(
            attr.get("description") or abs_attr.get("description") or "")
        enum_values = [str(v) for v in (abs_attr.get("enum_values")
                                        or attr.get("enum_values") or [])
                       if str(v).strip()]
        if not description and not enum_values:
            continue
        # 问句命中属性名/物理名/枚举值的排前面；同分保持 schema 顺序。
        priority = boost
        if _matches(question, logical, physical,
                    _as_text(attr.get("display_name")),
                    _as_text(abs_attr.get("display_name"))):
            priority += 3
        # 纯数字枚举值不做词面加权：问句里的 `3-D Man` 会让 `取值 1,2,3` 全部命中。
        match_values = [v for v in enum_values if not v.isdigit()]
        if match_values and _matches(question, *match_values):
            priority += 2
        if description and _description_matches(question, description):
            priority += 2
        parts = [f"{entity_name}.{logical}（列 {physical}）"]
        if description:
            parts.append(f"- {description}")
        if enum_values:
            parts.append(f"取值：{', '.join(enum_values[:20])}")
        text = " ".join(parts)[:MAX_ATTRIBUTE_HINT_CHARS]
        out.append((priority, idx, text))
    return out


def _metric_hints(question: str, metrics: list, abstract_entities: list) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    for idx, metric in enumerate(metrics or []):
        if not isinstance(metric, dict):
            continue
        name = _as_text(metric.get("name") or metric.get("code"))
        if not name:
            continue
        aliases = [_as_text(a) for a in (metric.get("aliases") or []) if _as_text(a)]
        definition = _clean_description(
            metric.get("definition") or metric.get("description") or "")
        formula = _as_text(metric.get("formula") or metric.get("expression"))
        if not definition and not formula and not aliases:
            continue
        priority = 4 if _matches(question, name, *aliases) else 0
        if priority == 0:
            continue  # 指标不命中就不带（避免把整库指标灌进去）
        text = f"指标 {name}"
        if aliases:
            text += f"（{'、'.join(aliases[:5])}）"
        if definition:
            text += f"：{definition}"
        if formula:
            text += f"；公式：{formula}"
        out.append((priority, idx, text[:MAX_ATTRIBUTE_HINT_CHARS]))
    return out


def build_auto_hints(*, question: str, entity_name: str,
                     entity_types: list, abstract_entities: list,
                     constraints: list | None = None,
                     metrics: list | None = None,
                     related: list[tuple[dict, dict | None]] | None = None) -> list[str]:
    """组装本次提问的 hints（无可用材料 → `[]`，行为与没有 hints 相同）。

    `related` 是 1 跳邻居的 `(compiled_entity, abstract_entity)` 对，由调用方挑好
    （最多 `MAX_RELATED_ENTITIES` 个）；`constraints` 是已按锚点过滤过的口径约束。
    """
    question = _as_text(question)
    entity_name = _as_text(entity_name)
    if not entity_name:
        return []
    anchor = _find_entity(entity_types, entity_name)
    if anchor is None:
        return []
    abstract = _abstract_for(abstract_entities, entity_name)
    candidates: list[tuple[int, int, str]] = []

    # 1. 锚点口径（人审规则）——最高优先级。
    for idx, constraint in enumerate(constraints or []):
        if not isinstance(constraint, dict):
            continue
        expression = _as_text(constraint.get("expression"))
        if not expression:
            continue
        name = _as_text(constraint.get("name"))
        text = f"口径 {name}：{expression}" if name else f"口径：{expression}"
        candidates.append((6, idx, text[:MAX_ATTRIBUTE_HINT_CHARS]))

    # 2. 指标（只带命中问句的）。
    candidates.extend(_metric_hints(question, metrics or [], abstract_entities))

    # 3. 实体别名 / 抽象实体描述。
    aliases = _entity_aliases(abstract, entity_name)
    entity_desc = _clean_description((abstract or {}).get("description") or
                                     anchor.get("description") or "")
    if aliases or entity_desc:
        text = f"实体 {entity_name}"
        if aliases:
            text += f" 的别名：{'、'.join(aliases[:8])}"
        if entity_desc:
            text += f"；{entity_desc}"
        priority = 5 if _matches(question, *aliases) else 2
        candidates.append((priority, 0, text[:MAX_ATTRIBUTE_HINT_CHARS]))

    # 4. 锚点属性（含枚举值）。
    candidates.extend(_attribute_hints(question, anchor, abstract, boost=1))

    # 5. 1 跳邻居属性（只补列语义；boost 低一档）。
    for order, (rel_compiled, rel_abstract) in enumerate(related or []):
        if not isinstance(rel_compiled, dict):
            continue
        candidates.extend(_attribute_hints(question, rel_compiled, rel_abstract,
                                           boost=0))

    # 去重（保序）→ 按优先级稳定排序 → 套 hints 通道的条数/长度边界。
    seen: set[str] = set()
    unique: list[tuple[int, int, str]] = []
    for priority, idx, text in candidates:
        if text in seen:
            continue
        seen.add(text)
        unique.append((priority, idx, text))
    unique.sort(key=lambda item: (-item[0], item[1]))

    out: list[str] = []
    total = 0
    for _priority, _idx, text in unique:
        text = text.strip()
        if not text or text in out:
            continue
        if len(out) >= MAX_HINTS:
            break
        if total + len(text) > MAX_HINTS_TOTAL_CHARS:
            continue
        out.append(text)
        total += len(text)
    return out


__all__ = ["build_auto_hints", "MAX_RELATED_ENTITIES", "MAX_DESCRIPTION_CHARS"]
