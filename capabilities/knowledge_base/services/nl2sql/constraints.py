"""把编译 schema 里的约束挑成「该给模型看的」那几条（库口径）。**纯函数**。

四条判据，都是「宁可不给，也不给错的」：

1. **停用的不给**：只有 `enabled is False` 才跳过（R1 **fail-open**：缺字段、
   `"false"` 字符串、`None`、`0` 等一律视为启用——存量行与外部写入都不能因为
   写了个怪值就静默停掉人工口径）。这一条放在最前，停用项不占后面的条数与字数预算。
2. **只收锚点实体自己**：`target_type=entity`（`target_code == entity`）与
   `attribute`（`target_code` 以 `entity + "."` 开头，**按段比**——裸 `startswith("Jobs")`
   会把 `JobsX.status` 也算进来）。`relation` 级**本档不收**：判一条关系口径要不要给，
   得先知道这次会 join 哪些表，留下一档。
3. **必须有非空 `expression`**：约束对模型的价值就是那句人写的话；`expression` 为空
   （生成器产的那批 `unique_attribute` 就是）一个字都说不出，塞进 payload 只白烧 token。
4. **坏数据跳过不抛**：非 dict / 认不出的 `target_type` / `target_code` 空 —— 与
   `pick_samples` 对坏 `distinct` 的姿态一致：坏一条不该让整次提问 500。
"""
from __future__ import annotations

#: 单次最多给几条 —— B1「按需取、限量注入」在约束这一档的落点。
#: 2026-10-12（jinpin 口径线）：5 → 8 —— Jobs 4 条既有口径 + 薪资/距离 2 条 = 6 条，
#: 留 2 条裕量；其他实体远低于此。
MAX_CONSTRAINTS = 8

#: 全部约束的 expression 字符预算 —— 超了按**存储顺序**截断（确定性，可复现）。
#: 2026-10-12（jinpin 口径线）：600 → 1500 —— Jobs 4 条 524 字 + 两条新口径（逐字文案
#: 实测 ~628 字）≈ 1152 字（精确值以 Task 4 真机回读为准），留裕量；单轮 payload
#: 增量 ~628 字可接受。`_dump` 2000 字硬截断下 6 条约 1152 字 + JSON 包装 < 2000
#: （键序把 constraints 排 columns 前，约束保全优先）。
MAX_TOTAL_CHARS = 1500


def filter_constraints(constraints: list[dict] | None, entity: str) -> list[dict]:
    """挑出属于 `entity` 的口径约束（**保序**、≤ `MAX_CONSTRAINTS` 条）。

    expression 合计 > `MAX_TOTAL_CHARS` 时按存储顺序截断（`break`，与 `pick_samples`
    同一款「预算用完即停」）：留下的是存储顺序的一个前缀，不跳过放不下的去收后面的。
    预算按 **原样** 长度计 —— 进 payload 的就是这个数，不能按 strip 后长度量（
    `"  正文  "` 一样占 6 个字）；纯空白仍不算 expression（一个字都说不出）。

    返回的是传入条目的**原引用**（不拷贝）：调用方只读（当前链路 `service.py` →
    `ontology_llm.py` → `json.dumps`），**不得原地修改**。
    """
    prefix = f"{entity}."
    picked: list[dict] = []
    used = 0
    for item in constraints or []:
        if not isinstance(item, dict):
            continue
        # 人工停用的口径（R1：**只有显式 False** 算停用；缺字段 / "false" 等一律
        # 视为启用）。必须放在预算与条数判据**之前**——停用项不占字数、不占条数。
        if item.get("enabled") is False:
            continue
        target_type = str(item.get("target_type") or "").strip().lower()
        target_code = str(item.get("target_code") or "").strip()
        if target_type == "entity":
            if target_code != entity:
                continue
        elif target_type == "attribute":
            if not target_code.startswith(prefix):
                continue
        else:
            continue
        raw_expression = str(item.get("expression") or "")
        if not raw_expression.strip():
            continue
        if used + len(raw_expression) > MAX_TOTAL_CHARS:
            break
        used += len(raw_expression)
        picked.append(item)
        if len(picked) >= MAX_CONSTRAINTS:
            break
    return picked
