#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""② 值同义词：分页读取 + 进程内缓存 + 问句匹配（P1 注入）。

`ontology_synonyms` 存 KB 级 `terms → canonical`（手工登记 / 演化采纳）。② 在组装
SQL 生成 payload 前**按需**取与问句词面命中的组注入 `value_aliases`，让
「华东环保 → 华东环保集团」这类值写法立即对问数生效。同一份组还会在**候选召回之前**
把问句里的 variant 改写成 canonical（`rewrite_question`）—— 召回比生成更早，
只看注入的话「华东环保」仍选不中 `GroupMaster`。

读取与缓存（对齐 `schema_read` 的口径）：
- 分页读取：`OntologySynonymRepository.list_by_kb(tenant, kb, offset, 500)` 循环到
  `total` —— 几千组时不用一次大查询；
- 进程内缓存：键 `(tenant, kb, generation)`，TTL =
  `config.NL2SQL_SYNONYM_CACHE_TTL_SECONDS`（默认 60，`0` = 关闭）；
- 写代号：`bump_write_generation` 在**每次成功的同义词写入**后 +1（CRUD / 导入 /
  `synonym` 演化采纳），同进程的写后读立刻重读；多 worker 靠 TTL 兜底。
- 失败姿态：同义词注入是增能项，读库失败按「无同义词」处理（只 warning），
  绝不拖垮提问；`0` 关闭时每次现读。

匹配：归一化 `strip().lower()`；CJK 词按**子串**命中，ASCII 词短于 3 个字符时
要求独立词边界（`IT 部门` 命中、`with` 不命中），≥3 个字符仍按子串。命中按 term
长度降序（同长按输入顺序）取前 `limit`（`limit <= 0` 不限）；每组只记**最长命中词**。
注入上限只限制条数，不限制读取/缓存规模。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from functools import lru_cache

from jonex_core.common.config import config
from jonex_core.common.database import get_db_session

from ...repository.ontology_synonym_repository import OntologySynonymRepository

logger = logging.getLogger(__name__)

#: 每页读取的组数。上限是「单次查询重量」，不是注入上限。
PAGE_SIZE = 500

#: 缓存条目上限（防御性上界）：正常规模 = 活跃 (tenant, kb) 数，过期项惰性删除。
_MAX_CACHE_ENTRIES = 256

#: `(tenant, kb, generation)` → `(expires_at, groups)`。
_cache: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}
#: `(tenant, kb)` → 写代号（只增不减）。
_generations: dict[tuple[str, str], int] = {}
_lock = threading.Lock()


# ────────────────────────────────────────────────────────────────────────────
# 配置
# ────────────────────────────────────────────────────────────────────────────


def _ttl() -> int:
    """TTL 秒（`0` = 关闭缓存）。每次现取配置 ⇒ 单测可即时切档。"""
    try:
        return max(0, int(config.NL2SQL_SYNONYM_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        return 60


def _max_groups() -> int:
    """注入上限（`0` = 不限）。"""
    try:
        return int(config.NL2SQL_VALUE_ALIASES_MAX_GROUPS)
    except (TypeError, ValueError):
        return 10


# ────────────────────────────────────────────────────────────────────────────
# 写代号 / 缓存原语
# ────────────────────────────────────────────────────────────────────────────


def _key(tenant_id: str, kb_id: str) -> tuple[str, str]:
    return (str(tenant_id or ""), str(kb_id or ""))


def bump_write_generation(tenant_id: str, kb_id: str) -> None:
    """同义词写入成功后推进该 KB 的写代号（本进程缓存立即失效）。"""
    key = _key(tenant_id, kb_id)
    with _lock:
        _generations[key] = _generations.get(key, 0) + 1


def _generation(key: tuple[str, str]) -> int:
    with _lock:
        return _generations.get(key, 0)


def clear_cache() -> None:
    """清空进程内缓存（测试 / 运维逃生口）。

    写代号一并归零：代号本身没有副作用（缓存已清空），归零让测试与排障从
    确定状态开始。
    """
    with _lock:
        _cache.clear()
        _generations.clear()


def _copy_groups(groups: list[dict]) -> list[dict]:
    """浅层深拷贝：避免调用方改 terms 列表时污染缓存。"""
    out = []
    for g in groups:
        terms = g.get("terms")
        out.append({"terms": list(terms) if isinstance(terms, list) else [],
                    "canonical": g.get("canonical"),
                    "updated_at": g.get("updated_at")})
    return out


def _prune_locked(now: float) -> None:
    """过期项惰性清理；仍超上限则按到期时间（≈写入时间）丢最旧。"""
    expired = [key for key, (expires, _g) in _cache.items() if expires <= now]
    for key in expired:
        _cache.pop(key, None)
    overflow = len(_cache) - _MAX_CACHE_ENTRIES
    if overflow > 0:
        oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:overflow]
        for key, _entry in oldest:
            _cache.pop(key, None)


# ────────────────────────────────────────────────────────────────────────────
# 读取
# ────────────────────────────────────────────────────────────────────────────


def _iso(value):
    if value is None or isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _row_to_group(row) -> dict:
    """ORM 行 / dict → `{terms, canonical, updated_at}`（会话关闭后才可能入缓存）。"""
    if isinstance(row, dict):
        terms = row.get("terms")
        canonical = row.get("canonical")
        updated = row.get("updated_at")
    else:
        terms = getattr(row, "terms", None)
        canonical = getattr(row, "canonical", None)
        updated = getattr(row, "updated_at", None)
    # JSONB 若被写成字符串（脏数据 / 旧导入），逐字符遍历会让「华」「东」命中
    # 一切问句 ⇒ 非 list 一律视为空。
    if not isinstance(terms, list):
        terms = []
    return {
        "terms": [str(term) for term in terms],
        "canonical": str(canonical or ""),
        "updated_at": _iso(updated),
    }


async def _read_all_pages(tenant_id: str, kb_id: str) -> list[dict] | None:
    """按 `PAGE_SIZE` 循环读到 `total`；DB 不可读时返回 `None`（与「真的没有组」区分）。"""
    rows: list = []
    try:
        async with get_db_session() as session:
            repo = OntologySynonymRepository(session)
            offset = 0
            while True:
                items, total = await repo.list_by_kb(
                    tenant_id, kb_id, offset, PAGE_SIZE)
                items = list(items or [])
                rows.extend(items)
                offset += len(items)
                if not items or offset >= int(total or 0):
                    break
    except (AssertionError, NameError, AttributeError, TypeError, KeyError):
        raise                                  # 编程错误照抛（同目录 fail-open 纪律）
    except Exception as exc:  # noqa: BLE001 —— 同义词是增能，读不到就当没有
        logger.warning("值同义词读取失败，按无同义词处理：tenant=%s kb=%s err=%s",
                       tenant_id, kb_id, exc)
        return None
    return [_row_to_group(row) for row in rows]


async def load_groups(tenant_id: str, kb_id: str) -> list[dict]:
    """该 KB 全部未删除同义词组（分页读取 + 进程缓存）。

    代号在**未命中那一刻**采样：读库期间若有并发写入推进代号，这份结果归档在
    旧代下 ⇒ 下一次查找按新代号判定时立刻作废并重读（不会把写前的内容冒充成新代）。
    """
    key = _key(tenant_id, kb_id)
    ttl = _ttl()
    generation = _generation(key)
    if ttl > 0:
        now = time.monotonic()
        with _lock:
            entry = _cache.get((key[0], key[1], generation))
            if entry is not None and entry[0] > now:
                return _copy_groups(entry[1])
            if entry is not None:
                _cache.pop((key[0], key[1], generation), None)

    groups = await _read_all_pages(key[0], key[1])
    if groups is None:
        # **读库失败不落缓存**：否则 DB 瞬断一次，本进程会在整个 TTL 内一直按
        # 「无同义词」回答。失败只代表这一次没有别名，下次请求重新尝试
        # （不做短期负缓存：同义词是增能项，重试的代价只是一次分页查询）。
        return []

    if ttl > 0:
        full = (key[0], key[1], generation)
        expires = time.monotonic() + ttl
        with _lock:
            # 同一 (tenant, kb) 只留当前代（旧代在换键时摘掉）。
            for old in [k for k in _cache if k[0] == key[0] and k[1] == key[1]
                        and k != full]:
                _cache.pop(old, None)
            _cache[full] = (expires, _copy_groups(groups))
            _prune_locked(time.monotonic())
    return _copy_groups(groups)


# ────────────────────────────────────────────────────────────────────────────
# 匹配
# ────────────────────────────────────────────────────────────────────────────


def _norm(value) -> str:
    return str(value or "").strip().lower()


#: ASCII 词的「短词」阈值：短于该长度必须独立成词，不能只靠子串。
_ASCII_MIN_SUBSTRING_LEN = 3


@lru_cache(maxsize=512)
def _ascii_word_pattern(term: str) -> re.Pattern:
    """ASCII 词的独立边界模式（前后都不能是字母/数字/下划线）。"""
    return re.compile(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])")


@lru_cache(maxsize=512)
def _ascii_word_pattern_ci(term: str) -> re.Pattern:
    """同 `_ascii_word_pattern`，但大小写不敏感（改写要保留原文大小写）。"""
    return re.compile(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])",
                      re.IGNORECASE)


@lru_cache(maxsize=512)
def _substring_pattern(term: str) -> re.Pattern:
    """子串模式（大小写不敏感；CJK 没有大小写，等价于普通子串）。"""
    return re.compile(re.escape(term), re.IGNORECASE)


def _term_matches(norm_term: str, normalized_question: str) -> bool:
    """词面命中：CJK 用子串；ASCII 短词要求独立词边界，其余用子串。

    `IT` / `A` 这类 1–2 字符的 ASCII 词是子串误命中的重灾区（`with` 含 `it`）；
    中文没有词边界且单字也可能是有效值，保持原子串口径。
    """
    if not norm_term.isascii() or len(norm_term) >= _ASCII_MIN_SUBSTRING_LEN:
        return norm_term in normalized_question
    return _ascii_word_pattern(norm_term).search(normalized_question) is not None


def match_groups(question: str, groups: list[dict], *, limit: int) -> list[dict]:
    """问句词面命中的组（每组带 `term` = 最长命中词），按 term 长度降序。

    `limit <= 0` 不限；同长按输入顺序（稳定）。返回副本，不改入参。
    """
    normalized_question = _norm(question)
    if not normalized_question:
        return []

    matched: list[tuple[int, int, dict]] = []
    for index, group in enumerate(groups or []):
        if not isinstance(group, dict):
            continue
        terms = group.get("terms")
        if not isinstance(terms, list):      # 非 list（脏 JSONB 字符串）视为无词
            continue
        best_term = ""
        best_len = 0
        for term in terms:
            norm_term = _norm(term)
            if not norm_term or not _term_matches(norm_term, normalized_question):
                continue
            if len(norm_term) > best_len:
                best_term = str(term).strip()
                best_len = len(norm_term)
        if not best_term:
            continue
        matched.append((best_len, index, {**group, "term": best_term}))

    matched.sort(key=lambda item: (-item[0], item[1]))
    if limit is not None and limit > 0:
        matched = matched[:limit]
    return [item[2] for item in matched]


def _term_spans(term: str, question: str) -> list[tuple[int, int]]:
    """`term` 在**原问句**里的所有非重叠命中区间（口径与 `_term_matches` 相同）。"""
    if not term:
        return []
    pattern = (_ascii_word_pattern_ci(term)
               if term.isascii() and len(term) < _ASCII_MIN_SUBSTRING_LEN
               else _substring_pattern(term))
    return [(m.start(), m.end()) for m in pattern.finditer(question)]


def rewrite_question(question: str, groups: list[dict], *,
                     limit: int) -> tuple[str, list[dict]]:
    """把问句里命中的同义词**改写为 canonical**（候选召回 / 生成前）。

    这是 P1 注入之外的另一半：`match_groups` 只把别名喂给 SQL 生成，而候选
    召回更早看到的是原问句 —— 「华东环保」永远选不中 `GroupMaster`。这里在
    召回之前把词面换成 canonical，让同一条问句在两步里看到同一个值。

    - **最长词优先**、非重叠：长的先占位，短的命中若与已占区间重叠就跳过
      （否则 canonical 里包含原词时会被反复改写）。
    - 问句里**已经出现 canonical** 时，先把这些区间占住：canonical 本身就是
      正确值，别名（如「华东环保」⊂「华东环保集团」）不能再在它内部命中，
      否则会变成「华东环保集团集团」。
    - ASCII 词大小写不敏感，且沿用 `match_groups` 的边界规则（<3 字符要求
      独立成词）；CJK 按普通子串。
    - `limit` 限制**被改写的组数**（`<= 0` 不限），与注入上限同口径。
    - 空/空白问句、无组、无命中、`term` 归一后等于 `canonical` ⇒ 原样返回，
      `matched` 为空（审计 / 观测据此判定「是否发生过改写」）。

    返回 `(rewritten_question, matched)`，`matched` 形如
    `[{"term": <命中词>, "canonical": <替换值>}]`，按替换在问句中的位置排序、
    同 (term, canonical) 去重。
    """
    text = str(question or "")
    if not text.strip() or not groups:
        return question, []

    # (词长, 输入顺序, 组号, 展示词, 归一化词, canonical)
    candidates: list[tuple[int, int, int, str, str, str]] = []
    order = 0
    for group_index, group in enumerate(groups or []):
        if not isinstance(group, dict):
            continue
        terms = group.get("terms")
        if not isinstance(terms, list):
            continue
        canonical = str(group.get("canonical") or "").strip()
        if not canonical:
            continue
        canonical_norm = _norm(canonical)
        for term in terms:
            display = str(term or "").strip()
            norm = _norm(display)
            if not norm or norm == canonical_norm:
                continue                         # 空词 / 自己改自己：不算改写
            candidates.append((len(norm), order, group_index, display, norm, canonical))
            order += 1

    candidates.sort(key=lambda item: (-item[0], item[1]))

    # 先占住问句里**已经出现的 canonical**（口径与 term 匹配相同：CJK 子串 /
    # ASCII 边界、大小写不敏感）。canonical 已正确，别名在它内部命中就是重复改写。
    reserved: list[tuple[int, int]] = []
    seen_canonicals: set[str] = set()
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        canonical_norm = _norm(group.get("canonical"))
        if not canonical_norm or canonical_norm in seen_canonicals:
            continue
        seen_canonicals.add(canonical_norm)
        reserved.extend(_term_spans(canonical_norm, text))

    claimed: list[tuple[int, int, str, str]] = []
    occupied: list[tuple[int, int]] = list(reserved)   # 只参与重叠判定，不参与输出
    used_groups: set[int] = set()

    def _overlaps(start: int, end: int) -> bool:
        return any(start < c_end and c_start < end for c_start, c_end in occupied)

    for _length, _order, group_index, display, norm, canonical in candidates:
        if (limit is not None and limit > 0
                and group_index not in used_groups
                and len(used_groups) >= limit):
            continue
        for start, end in _term_spans(norm, text):
            if _overlaps(start, end):
                continue
            occupied.append((start, end))
            claimed.append((start, end, display, canonical))
            used_groups.add(group_index)

    if not claimed:
        return question, []

    claimed.sort(key=lambda item: item[0])
    parts: list[str] = []
    cursor = 0
    matched: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for start, end, display, canonical in claimed:
        parts.append(text[cursor:start])
        parts.append(canonical)
        cursor = end
        key = (_norm(display), _norm(canonical))
        if key not in seen:
            seen.add(key)
            matched.append({"term": display, "canonical": canonical})
    parts.append(text[cursor:])
    return "".join(parts), matched


def _merge_extra(groups: list[dict], extra: list[dict] | None) -> list[dict]:
    """库组 ⊕ 请求级 extra：**同 term 以 extra 为准**（extra 词从库组里摘掉）。

    extra 条目形如 `{term, canonical}`（P2 验真），也接受 `{terms, canonical}`。
    """
    extra_pairs: list[tuple[str, str]] = []
    positions: dict[str, int] = {}
    for item in extra or []:
        if not isinstance(item, dict):
            continue
        raw_terms = item.get("terms")
        if not isinstance(raw_terms, list):
            raw_terms = [item.get("term")] if item.get("term") is not None else []
        canonical = str(item.get("canonical") or "").strip()
        for raw in raw_terms:
            display = str(raw or "").strip()
            norm = _norm(display)
            if not norm:
                continue
            if norm in positions:
                extra_pairs[positions[norm]] = (display, canonical)  # 后到者胜
            else:
                positions[norm] = len(extra_pairs)
                extra_pairs.append((display, canonical))

    if not extra_pairs:
        return list(groups or [])

    overridden = set(positions)
    merged: list[dict] = []
    for group in groups or []:
        terms = group.get("terms")
        if not isinstance(terms, list):
            terms = []
        remaining = [term for term in terms if _norm(term) not in overridden]
        if remaining:
            merged.append({**group, "terms": remaining})
    merged.extend({"terms": [term], "canonical": canonical, "updated_at": None}
                  for term, canonical in extra_pairs)
    return merged


async def resolve_for_question(tenant_id: str, kb_id: str, question: str, *,
                               extra: list[dict] | None = None) -> list[dict]:
    """② 注入用的最终别名：`[{"term": ..., "canonical": ...}]`（空 canonical 丢弃）。

    读库带缓存；`extra` 只作用于本次请求（不写库）。
    """
    if not str(question or "").strip():
        return []                            # 空问句无需冷缓存的一次 DB 往返
    groups = await load_groups(tenant_id, kb_id)
    merged = _merge_extra(groups, extra)
    out: list[dict] = []
    seen: set[str] = set()
    for group in match_groups(question, merged, limit=_max_groups()):
        term = str(group.get("term") or "").strip()
        canonical = str(group.get("canonical") or "").strip()
        if not term or not canonical:
            continue
        norm = _norm(term)
        if norm in seen:
            continue
        seen.add(norm)
        out.append({"term": term, "canonical": canonical})
    return out


__all__ = [
    "PAGE_SIZE",
    "bump_write_generation",
    "clear_cache",
    "load_groups",
    "match_groups",
    "resolve_for_question",
    "rewrite_question",
]
