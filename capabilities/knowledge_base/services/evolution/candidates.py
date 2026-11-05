# -*- coding:utf-8 -*-
"""约束候选生成：从编译 schema 的列注释里抽**码表**，产出 `query_rule` 草稿。

逐字移植自 `tests/eval/nl2sql/constraint_candidates.py`（2026-10-18 服务化迁移）：
`extract_codes` / `scan_entities` 及其正则常量原样复制（纯函数，行为必须一致）。
唯一改动是数据来源：旧 CLI 走 Gateway HTTP + 登录，这里直接
`schema_io.get_row(tenant, "query_rule", kb, ds)` 读编译 schema 行 ——
不 HTTP、不 import tests 包。

识别形态（真机样本）：
    智能发布 0未开启 1手动 2自动          → 0=未开启 1=手动 2=自动
    是否已经支付：0.未支付; 1.已支付      → 0=未支付 1=已支付
    '0': '元 / 时'; '1': '元 / 月' …      → 0=元 / 时 …
    0:通知，1：任务                       → 0=通知 1=任务
⚠️ 草稿 expression **必须内置 P1-2 的教训句**（「这只是该列的取值口径、不是整表默认过滤」——
不写适用范围，模型会把口径当表级过滤，A3 臂实测误伤 4 条老题）。
"""
from __future__ import annotations

import re

from .schema_io import get_row

#: 数字+标签对：数字（1-2 位、**可带负号**——真机有 `-1 -- 未知` 这类码）后可跟任一分隔符，
#: 标签取到下一个数字/边界前。负号不接住的话 `-1` 会被截成 `1`，再撞上真正的 `1=微信`
#: 被 `setdefault` 吞掉 —— 直接产出**错映射**（真机样本：UserStockHistory.type / StockSell.state）。
#: 标签段里**后随数字的 `-` 不许吃**（`…， -1：撤销的` 会被前一个标签的贪婪匹配咬掉负号）；
#: 其余位置的 `-` 照收（标签本身可能带横杠）。
_PAIR = re.compile(r"(-?\d{1,2})\s*[=:：]?\s*((?:[^\d-]|-(?!\d)){0,24})")
#: 标签清洗：剥掉引号/点号/冒号/分号/空白/短横/书名框等装饰（`'.未处理'`→`未处理`，
#: `'元 / 时'`→`元 / 时`，`'-- 成功'`→`成功` —— `0 -- 成功` 式的 `--` 分隔符不该进标签）。
_LABEL_TRIM = " '\"`.。、:：；;，,＝=()（）[]【】-"
#: 负号合法性后验：`-` 前面是这些字符（或行首）才算**负数码**（`，-1：撤销的`）；
#: 跟在汉字/字母后的 `-` 是**分隔短横**（`1-角色2-部门-3用户` 的码是 3，不是 -3）。
_DASH_OK_PREV = "，,;；:：、()（）【】[]\"'"
#: 标签不允许长得像日期/纯数字串的一部分（`2024 年` 这类不是码表）。
_DATEISH = re.compile(r"^(年|月|日|岁|位|个)?$")
#: 码数上限（超过的多半是结构化清单而不是「怎么读码」）。
_MAX_CODES = 12
#: 只扫这几种列型（datetime/blob 的注释不可能是码表）。
_SKIP_TYPES = {"datetime", "date", "time", "timestamp", "blob", "bytes", "binary"}


def extract_codes(description: str) -> dict[str, str]:
    """从一段注释里抽 `{码: 标签}`（清洗后**去重**；同名标签只留首个码）。"""
    text = str(description or "")
    codes: dict[str, str] = {}
    seen_labels: set[str] = set()
    for m in _PAIR.finditer(text):
        code, raw = m.group(1), m.group(2)
        # 负号后验：`-` 前是行首/空白/标点才算负数；否则是分隔短横（`2-部门-3用户`），剥掉。
        if code.startswith("-"):
            prev = text[m.start() - 1] if m.start() > 0 else None
            if prev is not None and not (prev.isspace() or prev in _DASH_OK_PREV):
                code = code[1:]
        label = raw.strip().strip(_LABEL_TRIM).strip()
        # 标签里再切一刀：遇到第二个数字前的残留分隔符（如 `；正常`）
        label = re.split(r"\d", label)[0].strip(_LABEL_TRIM).strip()
        if not label or _DATEISH.match(label):
            continue
        if len(label) > 20 or label in seen_labels:
            continue
        seen_labels.add(label)
        codes.setdefault(code, label)
        if len(codes) > _MAX_CODES:
            return {}
    return codes


def scan_entities(entities: list[dict]) -> list[dict]:
    """编译 schema 的实体清单 → 候选（每列一条；≥2 个不同码才算码表）。"""
    out: list[dict] = []
    for e in entities or []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        for a in e.get("attributes") or []:
            if not isinstance(a, dict) or not a.get("name"):
                continue
            if str(a.get("type") or "").lower() in _SKIP_TYPES:
                continue
            desc = str(a.get("description") or "").strip()
            if not desc:
                continue
            codes = extract_codes(desc)
            if len(codes) < 2:
                continue
            target = f"{e['name']}.{a['name']}"
            code_text = "；".join(f"`{c}`={label}" for c, label in codes.items())
            out.append({
                "entity": str(e["name"]), "attribute": str(a["name"]),
                "target_code": target, "codes": codes, "raw_description": desc,
                # 草稿 expression：**内置适用范围句**（P1-2 的血汗教训，别删）
                "draft_expression": (
                    f"库口径（`{target}` 的取值怎么读）：{code_text}。"
                    f"⚠️ 这只是这一列的取值口径、**不是整张表的默认过滤**："
                    f"问句没有问到这个列时，不要加任何以它为准的过滤条件。"),
            })
    out.sort(key=lambda x: x["target_code"])
    return out


async def build_candidates(tenant_id: str, kb: str, ds: str | None) -> dict:
    """读该源编译 schema → 候选（已有 `query_rule` 的列不重复列）。

    返回旧 CLI JSON 产物同形：`{"kb", "ds_id", "candidates"}`。
    """
    row = await get_row(tenant_id, "query_rule", kb, ds)
    row = row or {}
    existing = {str(c.get("target_code") or "")
                for c in row.get("constraints") or []
                if isinstance(c, dict)
                and str(c.get("constraint_type") or "") == "query_rule"}
    return {"kb": kb, "ds_id": ds,
            "candidates": [c for c in scan_entities(row.get("entity_types") or [])
                           if c["target_code"] not in existing]}


__all__ = ["build_candidates", "extract_codes", "scan_entities"]
