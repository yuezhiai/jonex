# -*- coding: utf-8 -*-
"""W4.7 澄清与消歧（MVP）：显式问题池（PLANPOOL）。

设计：docs/superpowers/specs/2026-11-01-w4.7-clarification-pool-design.md（工作区文档，不入库）。

原则：
- **不猜**：有歧义就出问题，不自动挑一个；
- **显式池**：调用方持有池子（无服务端会话），只支持 `ask` / `drop` / `add`；
- **池空才提交**：`submit_ready = len(pool) == 0`，由调用方自己把关；
- 问题 ID 确定性（同输入同 ID），便于跨请求携带与审计。

问题形状：
```json
{"id": "cl_ab12cd34ef56", "kind": "metric|source|entity|unit|time",
 "field": "metric", "question": "你要问哪个指标？",
 "options": [{"label": "销售额", "value": "销售额"}], "required": true}
```
"""
from __future__ import annotations

import hashlib
import logging

from jonex_core.common.exceptions import InvalidParameterError

logger = logging.getLogger(__name__)

ACTIONS = ("ask", "drop", "add")


def _qid(kind: str, field: str, question: str, options) -> str:
    payload = "|".join([
        kind, field, question,
        ",".join(str(item.get("value") or "") for item in options or []
                 if isinstance(item, dict)),
    ])
    return "cl_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def make_question(kind: str, field: str, question: str,
                  options=None, *, required: bool = True) -> dict:
    opts = [{"label": str(item.get("label") or item.get("value") or ""),
             "value": str(item.get("value") or item.get("label") or "")}
            for item in options or [] if isinstance(item, dict)]
    return {"id": _qid(kind, field, question, opts),
            "kind": kind, "field": field, "question": question,
            "options": opts, "required": required}


def metric_question(candidates) -> dict | None:
    names = [str(item.get("name") or "") for item in candidates or []
             if isinstance(item, dict) and str(item.get("name") or "").strip()]
    if len(names) < 2:
        return None
    return make_question("metric", "metric", "你要问哪个指标？",
                         [{"label": name, "value": name} for name in names])


def source_question(sources, metric_name: str = "") -> dict | None:
    values = [str(item) for item in sources or [] if str(item).strip()]
    if len(values) < 2:
        return None
    suffix = f"（指标：{metric_name}）" if metric_name else ""
    return make_question("source", "data_source_id",
                         f"该指标在多个数据源有落点，用哪个？{suffix}",
                         [{"label": value, "value": value} for value in values])


def entity_question(entities) -> dict | None:
    values = [str(item) for item in entities or [] if str(item).strip()]
    if len(values) < 2:
        return None
    return make_question("entity", "entity_name",
                         "该指标对应多个实体，用哪个？",
                         [{"label": value, "value": value} for value in values])


def unit_question(units) -> dict | None:
    values = [str(item) for item in units or [] if str(item).strip()]
    if len(values) < 2:
        return None
    return make_question("unit", "unit", "结果用哪个单位？",
                         [{"label": value, "value": value} for value in values])


def time_question() -> dict:
    return make_question(
        "time", "time_window",
        "对比需要一个时间窗口，请给出起止日期",
        [{"label": "上个月", "value": "last_month"},
         {"label": "今年", "value": "this_year"}],
    )


def build_pool(*, metrics=None, sources=None, entities=None, units=None,
               need_time: bool = False, metric_name: str = "") -> list[dict]:
    """按固定顺序拼问题池（metric → source → entity → unit → time）。"""
    pool: list[dict] = []
    for question in (
        metric_question(metrics),
        source_question(sources, metric_name),
        entity_question(entities),
        unit_question(units),
        time_question() if need_time else None,
    ):
        if question and question["id"] not in {item["id"] for item in pool}:
            pool.append(question)
    return pool


def _normalize_pool(pool) -> list[dict]:
    out: list[dict] = []
    for item in pool or []:
        if not isinstance(item, dict):
            raise InvalidParameterError(message="pool 必须是问题对象数组")
        qid = str(item.get("id") or "").strip()
        if not qid:
            raise InvalidParameterError(message="pool 中的问题缺少 id")
        out.append(dict(item))
    return out


def apply_pool_action(pool, action: str, *, question_id: str = "",
                      text: str = "") -> dict:
    """`ask` 原样返回；`drop` 移除；`add` 追加自定义问题（ID 确定性）。"""
    current = _normalize_pool(pool)
    action = str(action or "ask").strip().lower()
    if action not in ACTIONS:
        raise InvalidParameterError(
            message="action 必须是 ask/drop/add",
            details={"action": action})
    if action == "drop":
        qid = str(question_id or "").strip()
        if not qid:
            raise InvalidParameterError(message="drop 需要 question_id")
        current = [item for item in current if item["id"] != qid]
    elif action == "add":
        body = str(text or "").strip()
        if not body:
            raise InvalidParameterError(message="add 需要 text")
        question = make_question("custom", "custom", body, [], required=False)
        if question["id"] not in {item["id"] for item in current}:
            current.append(question)
    return {
        "action": action,
        "pool": current,
        "pending": bool(current),
        "submit_ready": not current,
    }


__all__ = [
    "ACTIONS", "apply_pool_action", "build_pool", "entity_question",
    "make_question", "metric_question", "source_question", "time_question",
    "unit_question",
]
