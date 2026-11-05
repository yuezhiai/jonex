"""判分器：**结果表**判分，不比 SQL 字面。纯函数、**只用标准库**。

规约搬自 BI-Agent（`bi-agent-release/post-train/.../bi_lib/bi_utils.py:193-282` 的
`compare_dataframes_with_cell_sets` / `safe_compare` / `clean_flat_values`）：
多真值（任一匹配即算过）、1×1 宽档（真值一个数时，答案表任意格命中即算过）、
元胞多重集匹配（行序/列序无关、每个元胞只能配一次）、数值容差、缺失归一。

**为什么不引 numpy**：评测件不该给生产环境加依赖，也不该因为跑分器起不来把「门」卡住。
容差用 `math.isclose`，写法与 `np.isclose(rtol=1e-2, atol=0.5)` 等价。

**三档结局**（`judge` 的返回值）：

- `pass` —— 答对了（`expect: answer` 且结果表匹配；或 `expect: refuse` 且合规拒答）
- `fail` —— **答错了**（该答没答对；或该拒却出了数字；或模型写了不安全 SQL 被 guard 拦下）
- `error` —— **根本没答成**（环境/链路故障）。**不入准确率的分母**，单列 ——
  否则一次网关抖动会静默拉低准确率，看起来像「模型变笨了」。
"""
from __future__ import annotations

import math

#: 记 `error`（不入分母）的码：这些是**环境/链路**问题，与模型水平无关。
ERROR_REASON_CODES = frozenset({
    "llm_failed",              # 生成 SQL 那次调用失败（超时/传输/坏答案）
    "exec_failed",             # 执行失败（连不上/超时/方言/权限）
    "no_readonly_creds",       # 该源没登记只读凭据
    "not_a_database_source",   # 源不是 database 类型 / 引擎不支持
    "source_gone",             # 源被删
    "entity_gone",             # 实体在该源里已消失（多半是语料过期或 schema 重生成过）
})

#: 算「合规拒答」的码：`expect: refuse` 的题命中这三个之一即算过。
REFUSE_OK_REASON_CODES = frozenset({"not_answerable", "ambiguous", "no_candidate"})


def normalize_cell(value):
    """缺失归一：`None` / `NaN` / 空串 → `0.0`（`clean_flat_values` 的口径）；其余原样。"""
    if value is None:
        return 0.0
    if isinstance(value, float) and math.isnan(value):
        return 0.0
    if isinstance(value, str) and not value.strip():
        return 0.0
    return value


def _is_integral(value) -> bool:
    """这个值是不是「整数」（`int` / 值为整数的 `float` / 数字字符串）。`bool` 不算。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    try:
        return float(str(value)).is_integer()
    except (TypeError, ValueError):
        return False


def cell_equal(a, b) -> bool:
    """两个元胞相等吗。

    **整数要精确**：语料大半是计数题，`rtol=1e-2` 会让「2225 答成 2203」也算对 ——
    门就形同虚设（BI-Bench 那条相对容差是给财务聚合值定的，这里只给浮点留）。
    字符串 trim 后比（大小写敏感），其余按字符串比。
    """
    a, b = normalize_cell(a), normalize_cell(b)
    if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool) and not isinstance(b, bool):
        # 两个真 `int` 直接比：`int(float(str(x)))` 在 >2**53 后会丢精度
        # （真值里已经有 20 位纯数字的营业执照号），`10**400` 那种还会抛 OverflowError。
        return a == b
    if _is_integral(a) and _is_integral(b):
        return int(float(str(a))) == int(float(str(b)))
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-2, abs_tol=0.5)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _row_equal(row_a: list, row_b: list) -> bool:
    """两行是不是「同一行」：**行内列序无关**，但每个元胞只能配一次。"""
    if len(row_a) != len(row_b):
        return False
    used = [False] * len(row_b)
    for want in row_a:
        hit = -1
        for i, got in enumerate(row_b):
            if not used[i] and cell_equal(want, got):
                hit = i
                break
        if hit == -1:
            return False
        used[hit] = True
    return True


def match_table(truth_rows: list[list], answer_rows: list[list]) -> bool:
    """一张真值表与答案表的匹配。

    1×1 真值走宽档（值出现在任意格即算过，BI-Agent 原规约）；
    其余按**行级**多重集：行序无关、行内列序无关，但**行与行必须配上对** ——
    `[深圳,9],[东莞,4]` 与 `[深圳,4],[东莞,9]` **不是**同一张表
    （BI-Agent 的全局元胞匹配会把它们判等，那等于放过一个数字全错的答案）。
    """
    if not truth_rows or not answer_rows:
        return False
    flat_truth = [cell for row in truth_rows for cell in row]

    if len(flat_truth) == 1:
        return any(cell_equal(flat_truth[0], cell) for row in answer_rows for cell in row)

    if [len(r) for r in truth_rows] != [len(r) for r in answer_rows]:
        return False

    used = [False] * len(answer_rows)
    for want in truth_rows:
        hit = -1
        for i, got in enumerate(answer_rows):
            if not used[i] and _row_equal(want, got):
                hit = i
                break
        if hit == -1:
            return False
        used[hit] = True      # 一行只能配一次
    return True


def judge(case: dict, outcome: dict, truths: list[list[list]]) -> dict:
    """判一条。`outcome` 是 ② 的**原始返回体**；`truths` 是**已执行**的 control 结果表。"""
    expect = str(case.get("expect") or "answer")
    ok = bool(outcome.get("ok"))
    reason_code = str(outcome.get("reason_code") or "")

    if not ok and reason_code in ERROR_REASON_CODES:
        return {"outcome": "error", "why": f"{reason_code}（没答成，不入分母）"}

    if expect == "refuse":
        if not ok and reason_code in REFUSE_OK_REASON_CODES:
            return {"outcome": "pass", "why": f"合规拒答（{reason_code}）"}
        if ok:
            return {"outcome": "fail", "why": f"该拒答却给了结果：{outcome.get('sql') or ''}"[:200]}
        return {"outcome": "fail", "why": f"拒答出口不合法：{reason_code or '（无 reason_code）'}"}

    if not ok:
        # 走到这里说明不是 error 档 —— 含 `rejected_unsafe_sql`（模型写了不安全 SQL，是模型的错）
        return {"outcome": "fail", "why": f"没答上来：{reason_code or '（无 reason_code）'}"}

    answer_rows = outcome.get("rows") or []
    for truth in truths:
        if match_table(truth, answer_rows):
            return {"outcome": "pass", "why": "结果表匹配"}
    return {"outcome": "fail", "why": f"结果表与 {len(truths)} 张真值表都不匹配：{answer_rows}"[:200]}
