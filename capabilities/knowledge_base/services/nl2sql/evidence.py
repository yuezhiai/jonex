"""把探测到的证据**按需、限量**地喂给模型（报告 B1：禁止全量前置）。

只挑「像枚举」的列 —— 取值域大的列（ID/时间/姓名）给样本没有意义，
而且全塞进去会把 prompt 撑爆（EvoOntology 的负结果：全量前置语义层 −15pp）。

⚠️ **这条线接不接线改过一次，别再按老印象判**：2026-09-30 曾因一次 **n=1** 的真机 A/B
（21/22 → 19/22，归因「模型因此多带一列」）回退过；**2026-10-01 把那两条题钉死形状后**，
**全量 A/B（23 题 × 每题 3 次 × 两臂）给出相反结论**：有注入 **22/23**、无注入 **21/23**，
逐题差异**只有**新加的证据依赖题，其余 22 题两臂逐题一致、**0 抖动** ⇒ **接线保留**。
（早先那句「要重启必须先改注入形态」是回退时的**假设**，已被这次实测推翻。）
读取侧的表键继续用 `evidence_probe.evidence_key`（与写入侧同一份归一化）。
依据：`.superpowers/sdd/progress-evidence.md` 与 `ev-task-7-report.md`。
"""
from __future__ import annotations

#: 取样内不同值超过这个数，就不当「取值域小的列」，不给样本。
MAX_DISTINCT_FOR_SAMPLES = 10

#: 单列最多给几个值（与探测侧的 `MAX_SAMPLES` 对齐）。
MAX_SAMPLES_PER_COLUMN = 5

#: 全部样本值的总字符预算 —— 超了按**列序**截断（确定性，可复现）。
SAMPLES_CHAR_BUDGET = 1200

#: P0-1：列级示例值扩容的预算（开关 `NL2SQL_COLUMN_EXAMPLES` 打开时才用）。
MAX_EXAMPLES_PER_COLUMN = 3
EXAMPLES_CHAR_BUDGET = 2400


def pick_examples(tables_evidence: dict | None, table: str, columns: list[dict],
                  *, max_per_column: int = MAX_EXAMPLES_PER_COLUMN,
                  budget: int = EXAMPLES_CHAR_BUDGET) -> dict[str, list]:
    """给**所有列**最多 `max_per_column` 个示例值（不再按 distinct 门槛筛）。

    值样例是 BIRD/AV-SQL 里最有信息量的 schema 上下文；大域列给 3 个只是样例、
    不是全集。总字符预算按列序截断（确定性、可复现），避免 prompt 被撑爆。
    """
    entry = ((tables_evidence or {}).get(table) or {})
    ev_columns = entry.get("columns") or {}
    picked: dict[str, list] = {}
    for column in columns or []:
        name = str(column.get("name") or "")
        ev = ev_columns.get(name) or {}
        samples = list(ev.get("samples") or [])[:max(1, int(max_per_column))]
        if not samples:
            continue
        cost = sum(len(str(s)) + 2 for s in samples)
        if cost > budget:
            break
        picked[name] = samples
        budget -= cost
    return picked


def pick_samples(tables_evidence: dict | None, table: str, columns: list[dict]) -> dict[str, list]:
    """从证据里挑出「该给模型看的」列样本。返回 `{列名: [值]}`（可能为空）。纯函数。"""
    entry = ((tables_evidence or {}).get(table) or {})
    ev_columns = entry.get("columns") or {}
    picked: dict[str, list] = {}
    budget = SAMPLES_CHAR_BUDGET

    for column in columns or []:
        name = str(column.get("name") or "")
        ev = ev_columns.get(name) or {}
        samples = list(ev.get("samples") or [])[:MAX_SAMPLES_PER_COLUMN]
        if not samples:
            continue
        try:
            distinct = int(ev.get("distinct_in_sample") or 0)
        except (TypeError, ValueError):
            # 判据本身是坏值（手改过的 JSONB / 别的写入方）——**不抛**（抛出去就是整条 ②
            # 请求 500），也不猜：证明不了「取值域小」就不注入（与下面那条同一方向）。
            continue
        if distinct > MAX_DISTINCT_FOR_SAMPLES:
            continue                      # 取值域太大，样本没有信息量
        # 每个值算「字符数 + 2」（JSON 里一对引号）：只算 1 个分隔符的话名义预算 1200
        # 实际最坏约 4.3KB —— 有界、撑不爆，但既然一行能算准就不留这个偏差。
        cost = sum(len(str(s)) + 2 for s in samples)
        if cost > budget:
            break                         # 预算用完即停（按列序）
        picked[name] = samples
        budget -= cost
    return picked
