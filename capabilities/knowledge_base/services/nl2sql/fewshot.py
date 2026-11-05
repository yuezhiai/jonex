"""同库 few-shot（2026-10-29）。

AV-SQL 的 2-shot 是跨库的；对 BIRD 的 11 个库，官方 dev 集（1534 题）里
有大量**同库、同表**的历史问答。评测时把其中 2 条（排除我们自己的 500 题）
注入提示词，帮模型理解「这一列到底是什么意思」（如 NumGE1500 是人数而非分数）。

边界：
- 只从**当前 schema 的表集合**里做词面重叠选择，没有重叠就一条都不给；
- 示例只作列语义/SQL 风格参考，明确告诉模型**不要照抄过滤条件**；
- 语料文件由配置 `NL2SQL_FEWSHOT_PATH` 指定，默认空 = 功能不启用（生产无 BIRD 数据）。
"""
from __future__ import annotations

import json
import re
import time
from functools import lru_cache

from jonex_core.common.database import get_db_session
from sqlglot import exp, parse_one

from ...models.evolution import EvolutionCorpusCase


def _table_tail(name: str) -> str:
    return str(name or "").strip().strip('"`[]').split(".")[-1].casefold()


@lru_cache(maxsize=4)
def load_examples(path: str) -> tuple:
    """读 few-shot 语料（`{db: [{question, sql, tables}, ...]}`）→ 扁平行元组。"""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    rows: list[dict] = []
    if isinstance(raw, dict):
        for db, items in raw.items():
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                q = str(item.get("question") or "").strip()
                sql = str(item.get("sql") or "").strip()
                if not q or not sql:
                    continue
                rows.append({"db_id": db, "question": q, "sql": sql,
                             "tables": [str(t) for t in (item.get("tables") or [])],
                             "cot": str(item.get("cot") or "").strip()})
    return tuple(rows)


_STOP = frozenset({
    "the", "a", "an", "of", "in", "on", "for", "and", "or", "to", "is", "are",
    "what", "which", "who", "how", "many", "much", "list", "give", "show",
    "with", "by", "from", "that", "this", "has", "have", "was", "were",
})


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", str(text or "").lower())
            if len(t) > 1 and t not in _STOP}


def select_examples(rows, tables, *, k: int = 2, question: str = "") -> list[dict]:
    """按「表重叠 + 问题词面相似」选前 k 条；无表重叠返回空。

    表重叠是硬门槛（不同库的同名表不硬凑）；在重叠集合内，问题 token 的
    Jaccard 相似度越高越靠前。`question=""` 时退回原来的确定性排序。
    """
    wanted = {_table_tail(t) for t in (tables or []) if _table_tail(t)}
    if not wanted:
        return []
    q_tokens = _tokens(question)
    scored = []
    for row in rows or []:
        overlap = len(wanted & {_table_tail(t) for t in (row.get("tables") or [])})
        if not overlap:
            continue
        if q_tokens:
            r_tokens = _tokens(row.get("question") or "")
            union = q_tokens | r_tokens
            sim = (len(q_tokens & r_tokens) / len(union)) if union else 0.0
        else:
            sim = 0.0
        scored.append((overlap, sim, row))
    scored.sort(key=lambda item: (-item[0], -item[1],
                                  item[2]["db_id"], item[2]["question"]))
    return [dict(row) for _, _, row in scored[:max(1, int(k))]]


def _tables_from_sql(sql: str) -> list[str]:
    """从一条 SQL 里抽表名（只用于**示例选择**的表重叠，不做安全判定）。"""
    try:
        tree = parse_one(str(sql or ""))
    except Exception:  # noqa: BLE001 —— 语料里的 SQL 五花八门，解析不了就不收
        return []
    return sorted({str(t.name).lower() for t in tree.find_all(exp.Table) if t.name})


def rows_from_cases(cases) -> list[dict]:
    """演化语料 case（`{question, control, ds_id}`）→ few-shot 行（纯函数）。"""
    rows: list[dict] = []
    for case in cases or []:
        question = str((case or {}).get("question") or "").strip()
        control = (case or {}).get("control") or []
        sql = str(control[0]).strip() if control else ""
        if not question or not sql:
            continue
        tables = _tables_from_sql(sql)
        if not tables:
            continue
        rows.append({"db_id": str((case or {}).get("ds_id") or ""),
                     "question": question, "sql": sql, "tables": tables})
    return rows


#: `(tenant, kb, ds)` → (monotonic, rows)；语料低频变更，TTL 内不重复读库。
_CORPUS_CACHE: dict[tuple[str, str, str], tuple[float, tuple]] = {}
_CACHE_MAX = 256


async def load_corpus_examples(tenant_id: str, kb_id: str, ds_id: str, *,
                               ttl: int = 300) -> tuple:
    """从演化台语料（`evolution_corpus_cases`）读同库问答；失败退空，不影响提问。"""
    key = (str(tenant_id or ""), str(kb_id or ""), str(ds_id or ""))
    now = time.monotonic()
    hit = _CORPUS_CACHE.get(key)
    if hit is not None and now - hit[0] < max(0, int(ttl)):
        return hit[1]
    try:
        from sqlalchemy import select

        stmt = select(EvolutionCorpusCase).where(
            EvolutionCorpusCase.tenant_id == key[0],
            EvolutionCorpusCase.kb_id == key[1],
            EvolutionCorpusCase.ds_id == key[2],
            EvolutionCorpusCase.enabled == 1,
        ).order_by(EvolutionCorpusCase.name)
        async with get_db_session() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        cases = [{"question": r.question, "ds_id": r.ds_id,
                  "control": list(r.control or [])}
                 for r in rows if str(getattr(r, "expect", "")) == "answer"]
        out = tuple(rows_from_cases(cases))
    except Exception:  # noqa: BLE001 —— 语料是增能，读不到就当没有
        return tuple()
    if len(_CORPUS_CACHE) >= _CACHE_MAX:
        _CORPUS_CACHE.clear()
    _CORPUS_CACHE[key] = (now, out)
    return out


def clear_cache() -> None:
    """测试与语料写操作后使用。"""
    _CORPUS_CACHE.clear()


__all__ = ["load_examples", "select_examples", "rows_from_cases",
           "load_corpus_examples", "clear_cache"]
