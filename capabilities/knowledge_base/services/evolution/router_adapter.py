# -*- coding:utf-8 -*-
"""演化台 W4 Task 2：router `/search/data-only` 响应 → 演化 outcome 的**纯映射层**。

`gate` / `bench` / `scorer` 只认顶层 `ok / rows / sql / reason_code`，而 router 的
data-only 响应是一层 `{success, data:{data_blocks:[...]}}` 信封 → 这里做一次性适配，
让 eval / smoke / gate / bench **零改动**（形状与 `Nl2SqlService.ask_data_source` 对齐）。

本模块只做纯映射：不发请求、不读写库、**不抛异常**（畸形 payload 一律按「无数据块」处理）。

控制器裁定（优先于设计稿的含糊处）：
- `table` 缺失/`None`，或 `table["rows"]` **键**缺失 → 视为「router 未返回行」；
  `table["rows"] == []`（键在、空列表）是**合法的零行成功**，由 scorer/gate 判；
- 第一个 `kind=="answer"` 块获胜（即使它没有行，也不再往后找）；
- 失败块 reason 形如 `"<reason_code>: <中文短句>"`：**已知码原样透传**（来自
  `scorer` 的 `ERROR_REASON_CODES` / `REFUSE_OK_REASON_CODES`，外加 ② 的两个拒答码
  `rejected_unsafe_sql` / `rejected_sensitive`）。这些码决定 error 档（不入分母）与
  refuse 档（合规拒答），**绝不能塌成 `not_answerable`**，否则 router ≡ 进程内不成立；
- 只有**不认识**的 reason 才走保守 needle 映射，仍不认识则落 `not_answerable` ——
  **绝不发明 gate 不认识的新 reason_code**。
"""
from __future__ import annotations

import re
from typing import Any

from .scorer import ERROR_REASON_CODES, REFUSE_OK_REASON_CODES

#: 失败块 reason 里允许**原样透传**的码 = `scorer` 的
#: `ERROR_REASON_CODES ∪ REFUSE_OK_REASON_CODES`，外加 ② 的两个拒答码
#: `rejected_unsafe_sql` / `rejected_sensitive`。**这不是 ② 的全部出口**：
#: 其它 ② 码（如 `skipped`、`structured_disabled`）没有登记，一律塌成
#: `not_answerable`。
#: `ERROR_REASON_CODES` = 环境/链路错（scorer 记 error、不入分母）；
#: `REFUSE_OK_REASON_CODES` = 合规拒答（`expect: refuse` 命中即 pass）；
#: 两个 ② 拒答码**不是** refuse 码，scorer 有意当普通失败处理（模型写了不安全
#: SQL 是模型的错），透传才能保持同档。
_KNOWN_REASON_CODES = frozenset(
    ERROR_REASON_CODES | REFUSE_OK_REASON_CODES | {"rejected_unsafe_sql", "rejected_sensitive"}
)

#: 保守 needle 映射（仅在 reason 前缀**不是**已知码时使用）：(code, 命中子串)。
#: 匹配的 haystack = `reason + text`（Jonex 错误信封的中文原因在 `text` 里，
#: 如 `"答不了：NL→SQL 调用出错（…）"`，只读 `reason` 会漏判）。
#: 顺序敏感，先匹配先返回；仍映射不到一律 `not_answerable`。
_REASON_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rejected_unsafe_sql", ("rejected_unsafe_sql", "被拒 sql", "被拒sql")),
    ("no_candidate", ("no_candidate",)),
    # 块级传送失败（② 的传输层错误 / Jonex 错误信封的 `message`）没有
    # `<code>: ` 前缀：环境错必须落 `llm_failed`（error 档、不入分母），
    # 否则会被当成 `not_answerable`（合规拒答）混过 `expect: refuse` 题。
    ("llm_failed", ("调用出错", "引擎异常", "引擎无响应", "缺 knowledge_base_id")),
)

#: 兜底「答不了」码（gate 的合规拒答集合成员）。
_NOT_ANSWERABLE = "not_answerable"

#: reason 前缀必须是纯小写标识符，才可能与已知码比对。
_CODE_RE = re.compile(r"^[a-z_]+$")


def _text(value: Any) -> str:
    """非空字符串原样返回；其余（None/数字/空串）返回空串。"""
    return value if isinstance(value, str) and value.strip() else ""


def _reason_code(haystack: str) -> str:
    """保守映射：已知子串 → gate 认识的码；映射不到 → `not_answerable`。

    `haystack` 由调用方拼成 `reason + text`（见 `_REASON_MAP` 注释）。
    """
    text = _text(haystack).lower()
    for code, needles in _REASON_MAP:
        if any(needle in text for needle in needles):
            return code
    return _NOT_ANSWERABLE


def _code_from_reason(reason: str) -> str | None:
    """从 `"<code>: <中文短句>"` 提取**已知**原因码；不是已知码则 `None`。

    只认第一个 `:` / `：` 之前、纯 `[a-z_]+` 且落在 `_KNOWN_REASON_CODES` 的前缀。
    透传已知码是「router ≡ 进程内」的关键：`exec_failed` 落 error 档不进分母，
    `no_candidate` 在 `expect: refuse` 题里算合规拒答。
    """
    head = re.split(r"[:：]", _text(reason), maxsplit=1)[0].strip()
    if head and _CODE_RE.match(head) and head in _KNOWN_REASON_CODES:
        return head
    return None


def _outcome(*, ok: bool, not_answerable: bool, rows: Any = None,
             columns: Any = None, sql: Any = None, row_count: int = 0,
             truncated: bool = False, reason_code: str = "", reason: str = "",
             source_name: str = "", data_source_id: str = "") -> dict:
    """与 `ask_data_source` 同形的 outcome（失败档也带全键，调用方 `.get` 不踩空）。"""
    return {
        "ok": ok,
        "not_answerable": not_answerable,
        "rows": rows if rows is not None else [],
        "columns": columns if columns is not None else [],
        "sql": sql,
        "row_count": row_count,
        "truncated": bool(truncated),
        "reason_code": reason_code,
        "reason": reason,
        "source_name": source_name,
        "data_source_id": data_source_id,
    }


def _no_blocks(source_name: str = "") -> dict:
    return _outcome(ok=False, not_answerable=True,
                    reason_code="no_candidate", reason="router 无数据块",
                    source_name=source_name)


def _no_judgeable(source_name: str = "") -> dict:
    """有块、但既无可用 answer 行也没有失败原因：显式「判不了」，不伪装成无块。"""
    return _outcome(ok=False, not_answerable=True,
                    reason_code="no_candidate", reason="router 未给出可判定答案",
                    source_name=source_name)


def _failure(reason_code: str, reason: str, *, source_name: str = "",
             data_source_id: str = "") -> dict:
    return _outcome(ok=False, not_answerable=True, reason_code=reason_code,
                    reason=reason, source_name=source_name,
                    data_source_id=data_source_id)


def _answer_outcome(block: dict, fallback_source_name: str) -> dict:
    """第一个 answer 块 → outcome；缺 `table.rows` 显式判「未返回行」。"""
    table = block.get("table")
    rows = table.get("rows") if isinstance(table, dict) else None
    if not isinstance(rows, list):
        return _failure(_NOT_ANSWERABLE, "router 未返回行",
                        source_name=_text(block.get("source_name")) or fallback_source_name,
                        data_source_id=_text(block.get("data_source_id")))

    columns = table.get("columns")
    block_row_count = block.get("row_count")
    row_count = (block_row_count
                 if isinstance(block_row_count, int) and not isinstance(block_row_count, bool)
                 else len(rows))
    return _outcome(
        ok=True,
        not_answerable=False,
        rows=rows,
        columns=columns if isinstance(columns, list) else [],
        sql=block.get("sql") or None,
        row_count=row_count,
        truncated=bool(block.get("truncated")),
        source_name=_text(block.get("source_name")) or fallback_source_name,
        data_source_id=_text(block.get("data_source_id")),
    )


def outcome_from_data_only(payload: dict, *, source_name: str = "") -> dict:
    """router `/search/data-only` 响应 → 演化 outcome（纯函数，从不抛异常）。

    映射顺序（先命中先返回）：
    1. 非 dict payload → 无块；
    2. `success is False` → `llm_failed`（信封错不是业务拒答）；
    3. 第一个 `kind=="answer"` 块 → 有 `table.rows` 判成功（空行也算），否则「未返回行」；
    4. 无 answer 块但存在失败块（`reason` 非空，或 `kind=="error"` 且 `text`
       非空）→ 已知码原样透传，否则用 `reason + text` 做保守 `_REASON_MAP`；
    5. 无块 → `no_candidate` / "router 无数据块"；有块但判不了 → 同码、
       reason 换 "router 未给出可判定答案"。
    """
    if not isinstance(payload, dict):
        return _no_blocks(source_name)

    if payload.get("success") is False:
        error = payload.get("error")
        reason = error.strip() if isinstance(error, str) and error.strip() else "router 调用失败"
        return _failure("llm_failed", reason, source_name=source_name)

    data = payload.get("data")
    blocks = data.get("data_blocks") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return _no_blocks(source_name)

    answer: dict | None = None
    failure_reason = ""
    failure_text = ""
    failure_source_name = ""
    failure_data_source_id = ""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("kind") == "answer":
            answer = block
            break  # 第一个 answer 块获胜
        if failure_reason or failure_text:
            continue
        reason = _text(block.get("reason"))
        text = _text(block.get("text"))
        # Jonex 错误信封把中文原因放在 `text`（`reason` 是上游 message）；
        # 只有 `kind=="error"` 才允许仅凭 `text` 判失败，避免把 clarify 文本当错。
        if reason or (block.get("kind") == "error" and text):
            failure_reason = reason
            failure_text = text
            failure_source_name = _text(block.get("source_name"))
            failure_data_source_id = _text(block.get("data_source_id"))

    if answer is not None:
        return _answer_outcome(answer, source_name)

    if failure_reason or failure_text:
        # 已知前缀码只从 reason 提取（text 是展示文本，可能带「答不了：」前缀）；
        # needle 匹配覆盖 reason + text（信封的中文原因只在 text 里）。
        code = _code_from_reason(failure_reason) or _reason_code(
            f"{failure_reason} {failure_text}")
        return _failure(code, failure_reason or failure_text,
                        source_name=failure_source_name or source_name,
                        data_source_id=failure_data_source_id)

    if blocks:
        return _no_judgeable(source_name)
    return _no_blocks(source_name)


__all__ = ["outcome_from_data_only"]
