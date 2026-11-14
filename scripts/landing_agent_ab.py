#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""落点 A/B 评审脚本：老对齐管线 vs 落点 Agent，出匿名 Markdown 评审表。

用法（57 测试服务器，需平台 DB 环境变量）：

    # 1) 预检（只读、不调任何模型）：概念数 / 源数 / 证据列覆盖 / 已有 agent 落点
    uv run python scripts/landing_agent_ab.py \\
        --tenant-id <t> --kb-id <kb> --dry-run

    # 2) 真跑 A/B（会调用 LLM，仍然只读 DB）
    uv run python scripts/landing_agent_ab.py \\
        --tenant-id <t> --kb-id <kb> --out out/landing_ab_report.md --limit 30

设计约束（为什么这么写）：

- **任何模式都不写库**：只走仓储的读口（`get_by_kb` / `list_compiled_schemas` /
  `list_by_kb`），两条管线都在内存副本上跑。评审脚本绝不能改变被评审的数据。
- **两侧从同一份概念结构出发**：`reset_derived_landings` 清掉非 `manual` 落点
  （保留人工落点）。否则老管线上一轮的产物会变成 agent 的「历史落点」，A/B
  比的是「历史 + 增量」，不是两条管线的真实输出。
- **盲评 + 匿名**：默认（`--blind`）主表只有落点和证据，置信度/依据收在文末附录，
  评审人先逐条打「对/错/说不准」，看完附录再看解密页脚；`--show-provenance`
  （或 `--no-blind`）恢复「依据就地显示」的调试形态。
- 纯函数（转义、摘录、统计、渲染）全部与 DB 解耦，单测见
  `tests/unit/test_landing_ab_report.py`。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as _dt
import pathlib
import random
import sys
from typing import Any, Iterable, Optional

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
# 直接 `python scripts/xxx.py` 时 sys.path[0] 是 scripts/，补仓库根才能 import
# `capabilities` / `jonex_core`（与 scripts/eval_schema_select.py 同口径）。
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: A/B 两侧的管线标识（内部值，报告里只出现 A/B 字母）。
LEGACY = "legacy"
AGENT = "agent"
SIDE_LABELS = ("A", "B")

#: 单条证据摘录的字符上限：评审要看得见「值长什么样」，又不能把表格撑爆。
MAX_EXCERPT_CHARS = 160

#: 单元格里单条依据的字符上限。模型偶尔写很长，不截断会让一行横跨几屏。
MAX_RATIONALE_CELL_CHARS = 400

#: 证据覆盖率低于它就先重跑源 schema 生成（brief 的口径）。
EVIDENCE_THRESHOLD_PCT = 50.0

DEFAULT_LIMIT = 30
DEFAULT_OUT = "out/landing_ab_report.md"


# ══════════════════════════════════════════════════════════════════
# 纯函数：Markdown / 文本
# ══════════════════════════════════════════════════════════════════

def truncate_text(text: Any, limit: int = MAX_EXCERPT_CHARS) -> str:
    """按字符截断并补省略号；`limit <= 0` 表示不截断。

    截断后总长**不超过** limit（省略号占一位）：报告列宽是固定的，超一格就会
    让评审表的对齐肉眼可见地歪掉。
    """
    value = "" if text is None else str(text)
    if not limit or limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def md_cell(value: Any) -> str:
    """任意值 → 可放进 Markdown 表格单元格的文本。

    顺序不能换：

    1. HTML 实体先转义（`&` → `&amp;` 必须最先，否则 `<` 转义出的 `&lt;`
       会被二次转义成 `&amp;lt;`）——概念名/依据/样本都是用户内容，原样
       放进 Markdown 会渲染成 HTML（甚至 `<script>`）。
    2. 反斜杠加倍：否则原文的 `\\|` 会让我们补的 `\\|` 变成
       「转义反斜杠 + 裸竖线」，单元格照样被打断。
    3. `|` → `\\|`。
    4. 换行 → `<br>`（表格里不能有真换行；这是我们自己生成的 HTML，
       所以必须放在第 1 步**之后**，否则会被一起转义掉）。
    """
    text = "" if value is None else str(value)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", "\\\\")
    text = text.replace("|", "\\|")
    text = text.replace("\n", "<br>")
    return text.strip()


# ══════════════════════════════════════════════════════════════════
# 纯函数：概念 / 落点视图
# ══════════════════════════════════════════════════════════════════

def concept_landings(concept: Any) -> list[dict]:
    """概念**生效**的落点（`included: false` 是「人/agent 明确排除」，不参与评审）。"""
    if not isinstance(concept, dict):
        return []
    return [
        source for source in (concept.get("sources") or [])
        if isinstance(source, dict) and source.get("included") is not False
    ]


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold()


def _landing_attribute_count(concept: dict, source: dict) -> int:
    """该落点下已挂的列映射条数（属性级 `sources` 里同一目标）。"""
    ds_id = str(source.get("data_source_id") or "").strip()
    entity = _fold(source.get("entity_name"))
    count = 0
    for attribute in concept.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        for link in attribute.get("sources") or []:
            if not isinstance(link, dict):
                continue
            # 与编译投影同口径：停用的列来源不是「生效落点」，不能计入。
            # 实体级用 `included`，属性级历史/编辑侧用 `enabled`，两个都认。
            if link.get("enabled") is False or link.get("included") is False:
                continue
            if (str(link.get("data_source_id") or "").strip() == ds_id
                    and _fold(link.get("entity_name")) == entity):
                count += 1
    return count


def format_landing_cell(concept: Any, bundles: Any,
                        *, with_provenance: bool = False) -> str:
    """一个概念的落点 → 单元格文本。

    `with_provenance` 只对 agent 侧开：置信度/依据是 agent 的产出，老管线没有
    这些字段；关掉时两列外形一致，评审人不会靠「哪列更花哨」猜身份。
    """
    landings = concept_landings(concept)
    if not landings:
        return "（无落点）"
    parts: list[str] = []
    for source in landings:
        ds_id = str(source.get("data_source_id") or "").strip()
        entity = str(source.get("entity_name") or "").strip()
        bundle = (bundles or {}).get(ds_id) if isinstance(bundles, dict) else None
        source_label = str((bundle or {}).get("name") or ds_id or "(未知源)")
        label = f"{source_label}/{entity}"
        columns = _landing_attribute_count(concept, source)
        if columns:
            label += f" [{columns} 列]"
        if with_provenance:
            confidence = str(source.get("confidence") or "").strip() or "-"
            # **不带 origin**：`origin=agent` 会直接把这一列的身份写出来，
            # 匿名评审就失效了。评审只需要知道这条落点有多确定、凭什么。
            label += f"（置信度 {confidence}）"
            rationale = str(source.get("rationale") or "").strip()
            if rationale:
                # 单条依据截断：模型偶尔写整段推理，不截断会把表格撑爆。
                label += f"：{truncate_text(rationale, MAX_RATIONALE_CELL_CHARS)}"
        parts.append(label)
    return "；".join(parts)


# ══════════════════════════════════════════════════════════════════
# 纯函数：证据
# ══════════════════════════════════════════════════════════════════

#: 证据表键的归一化函数（写侧在 evidence_probe，读侧必须共用同一份实现）。
_EVIDENCE_KEY_FN = None


def _evidence_table_key(entity: dict) -> str:
    """延迟 import：模块导入时不拉起 KB 服务包（那会连带 import 整条 LLM 栈）。

    `--dry-run` 的「不调模型」由调用路径保证；这里只是别让「读脚本」本身把
    模型客户端相关的模块都加载一遍（本地无网关时也更干净）。
    """
    global _EVIDENCE_KEY_FN
    if _EVIDENCE_KEY_FN is None:
        from capabilities.knowledge_base.services.evidence_probe import (
            evidence_key,
        )
        _EVIDENCE_KEY_FN = evidence_key
    return _EVIDENCE_KEY_FN(entity)


def _source_entity(bundle: Any, entity_name: str) -> Optional[dict]:
    entities = (bundle or {}).get("entities") if isinstance(bundle, dict) else None
    if not isinstance(entities, dict) or not entity_name:
        return None
    entity = entities.get(entity_name)
    if isinstance(entity, dict):
        return entity
    wanted = _fold(entity_name)
    for name, value in entities.items():
        if _fold(name) == wanted and isinstance(value, dict):
            return value
    return None


def _evidence_columns(bundle: Any, entity_name: str) -> dict:
    """源实体 → 证据里的列字典（`evidence.tables[table].columns`）。"""
    entity = _source_entity(bundle, entity_name)
    if not entity:
        return {}
    tables = ((bundle.get("evidence") or {}).get("tables")
              if isinstance(bundle, dict) else None)
    if not isinstance(tables, dict):
        return {}
    entry = tables.get(_evidence_table_key(entity))
    if not isinstance(entry, dict):
        # 编译期改过表名/历史证据键不一时，名字兜底一次（读不到只是少一列证据）。
        entry = tables.get(str(entity.get("name") or "").strip())
    columns = entry.get("columns") if isinstance(entry, dict) else None
    return columns if isinstance(columns, dict) else {}


def _samples_of(columns: dict, name: str) -> list[str]:
    """列 → 非空样本值；列名精确匹配优先，大小写不敏感兜底。"""
    if not name:
        return []
    value = columns.get(name)
    if not isinstance(value, dict):
        wanted = _fold(name)
        value = next(
            (item for key, item in columns.items() if _fold(key) == wanted), None)
    if not isinstance(value, dict):
        return []
    return [str(sample) for sample in (value.get("samples") or [])
            if str(sample).strip()]


def evidence_excerpt(bundle: Any, entity_name: str,
                     limit: int = MAX_EXCERPT_CHARS) -> str:
    """一张源表 → `列 = 样本1, 样本2` 摘录（最多 3 列）。

    没有样本/实体不存在回空串（由调用方决定占位文案）——「空」和「没这表」
    在报告里是两种含义，不该在底层被压成同一句。
    """
    columns = _evidence_columns(bundle, entity_name)
    pieces: list[str] = []
    for name in columns:
        samples = _samples_of(columns, name)
        if not samples:
            continue
        pieces.append(f"{entity_name}.{name} = {', '.join(samples)}")
        if len(pieces) >= 3:
            break
    if not pieces:
        return ""
    return truncate_text("；".join(pieces), limit)


def concept_evidence_excerpt(concepts: Iterable[Any], bundles: Any,
                             limit: int = MAX_EXCERPT_CHARS) -> str:
    """概念（两侧任意一侧）的落点 → 第一条有样本的证据摘录。

    证据是**源表**的属性，两侧共享；按 `(ds, entity)` 排序保证同一份输入每次
    生成同一段摘录（评审表可复现）。
    """
    keys: set[tuple[str, str]] = set()
    for concept in concepts or []:
        for source in concept_landings(concept):
            ds_id = str(source.get("data_source_id") or "").strip()
            entity = str(source.get("entity_name") or "").strip()
            if ds_id and entity:
                keys.add((ds_id, entity))
    for ds_id, entity in sorted(keys):
        text = evidence_excerpt((bundles or {}).get(ds_id), entity, limit)
        if text:
            return text
    return "（无样本证据）"


# ══════════════════════════════════════════════════════════════════
# 纯函数：A/B 共同起点
# ══════════════════════════════════════════════════════════════════

def reset_derived_landings(entities: Iterable[Any]) -> list[dict]:
    """深拷贝概念并清掉**非人工**落点（实体级 + 属性级）。

    为什么必须清：`apply_decisions` 对「无 origin 的历史落点」是保护（规则 7），
    agent 只能加不能改。若不清，老管线上一轮的输出会混进 agent 的基线，评审表
    会把「历史遗留」算成 agent 的产出。`manual` 落点是人给的 ground truth，保留。
    """
    out: list[dict] = []
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        item = copy.deepcopy(entity)
        sources = item.get("sources")
        item["sources"] = [
            source for source in (sources if isinstance(sources, list) else [])
            if isinstance(source, dict) and _fold(source.get("origin")) == "manual"
        ]
        for attribute in item.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            links = attribute.get("sources")
            attribute["sources"] = [
                link for link in (links if isinstance(links, list) else [])
                if isinstance(link, dict) and _fold(link.get("origin")) == "manual"
            ]
        out.append(item)
    return out


def compute_agent_landing_concept_count(entities: Iterable[Any]) -> int:
    """已有生效 agent 落点的概念数（dry-run 的「新管线跑到哪了」）。"""
    count = 0
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        if any(_fold(source.get("origin")) == "agent"
               for source in concept_landings(entity)):
            count += 1
    return count


# ══════════════════════════════════════════════════════════════════
# 纯函数：dry-run 统计
# ══════════════════════════════════════════════════════════════════

def source_evidence_summary(bundles: Any) -> dict:
    """逐源统计证据覆盖。

    口径（与 brief 一致）：**列级** —— 该列在 `evidence` 里有非空 `samples`
    才算覆盖；表级另给一列「有样本的表数」（一张表只要有一列有样本就算），
    因为空表/全空列在评审时同样是「没证据」。
    """
    sources: list[dict] = []
    total_columns = 0
    covered_columns = 0
    for ds_id in sorted(bundles or {}):
        bundle = bundles[ds_id]
        entities = (bundle.get("entities") or {}) if isinstance(bundle, dict) else {}
        columns_total = 0
        columns_covered = 0
        entities_with_samples = 0
        for name, entity in entities.items():
            if not isinstance(entity, dict):
                continue
            columns = _evidence_columns(bundle, str(name))
            entity_has_sample = False
            for attribute in entity.get("attributes") or []:
                if not isinstance(attribute, dict):
                    continue
                column = str(attribute.get("name") or "").strip()
                if not column:
                    continue
                columns_total += 1
                if _samples_of(columns, column):
                    columns_covered += 1
                    entity_has_sample = True
            if entity_has_sample:
                entities_with_samples += 1
        total_columns += columns_total
        covered_columns += columns_covered
        sources.append({
            "ds_id": ds_id,
            "name": str((bundle or {}).get("name") or ""),
            "entities_total": len(entities),
            "entities_with_samples": entities_with_samples,
            "columns_total": columns_total,
            "columns_with_samples": columns_covered,
            "coverage_pct": (
                columns_covered / columns_total * 100 if columns_total else 0.0
            ),
        })
    return {
        "sources": sources,
        "columns_total": total_columns,
        "columns_with_samples": covered_columns,
        "coverage_pct": (
            covered_columns / total_columns * 100 if total_columns else 0.0
        ),
    }


def build_dry_run_summary(*, tenant_id: str, kb_id: str,
                          entities: Iterable[Any], bundles: Any,
                          threshold_pct: float = EVIDENCE_THRESHOLD_PCT) -> dict:
    """dry-run 的统计结构（纯数据，格式化交给 `format_dry_run_summary`）。"""
    concepts = [entity for entity in entities or []
                if isinstance(entity, dict) and str(entity.get("name") or "").strip()]
    coverage = source_evidence_summary(bundles)
    return {
        "tenant_id": tenant_id,
        "kb_id": kb_id,
        "concept_count": len(concepts),
        "source_count": len(bundles or {}),
        "concept_with_agent_landing": compute_agent_landing_concept_count(entities),
        "sources": coverage["sources"],
        "columns_total": coverage["columns_total"],
        "columns_with_samples": coverage["columns_with_samples"],
        "coverage_pct": coverage["coverage_pct"],
        "threshold_pct": threshold_pct,
    }


def format_dry_run_summary(summary: dict) -> str:
    """统计结构 → 人读文本（57 上直接粘进台账）。"""
    lines: list[str] = [
        "=== 落点 A/B 预检（只读，不调用任何模型）===",
        f"租户：{summary.get('tenant_id')}",
        f"知识库：{summary.get('kb_id')}",
        f"概念数：{summary.get('concept_count')}",
        f"数据源数：{summary.get('source_count')}",
        (
            "已有 agent 落点的概念："
            f"{summary.get('concept_with_agent_landing')}"
            f"/{summary.get('concept_count')}"
        ),
        "证据覆盖（列级：该列有 samples 才算覆盖）：",
    ]
    sources = summary.get("sources") or []
    if not sources:
        lines.append("  （没有已编译的数据源 schema）")
    for source in sources:
        lines.append(
            f"  - {source.get('name') or '(未命名源)'} ({source.get('ds_id')})："
            f"{source.get('columns_with_samples')}/{source.get('columns_total')} 列"
            f"（{source.get('coverage_pct', 0.0):.1f}%），"
            f"{source.get('entities_with_samples')}/{source.get('entities_total')} 表有样本"
        )

    total = int(summary.get("columns_total") or 0)
    covered = int(summary.get("columns_with_samples") or 0)
    pct = float(summary.get("coverage_pct") or 0.0)
    threshold = float(summary.get("threshold_pct") or EVIDENCE_THRESHOLD_PCT)
    lines.append(f"总体列覆盖：{covered}/{total}（{pct:.1f}%）")
    if total == 0:
        lines.append("判定：没有可统计的列 —— 源 schema 可能还没生成。")
    elif pct < threshold:
        lines.append(
            f"判定：覆盖率 {pct:.1f}% < {threshold:.1f}% —— "
            "先按 runbook 重跑源 schema 生成（generate_ds_schema），再跑 A/B。"
        )
    else:
        lines.append(
            f"判定：覆盖率 {pct:.1f}% >= {threshold:.1f}% —— 可以直接跑 A/B。"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# 纯函数：匿名 / 报告
# ══════════════════════════════════════════════════════════════════

def assign_sides(seed: Optional[int] = None) -> dict[str, str]:
    """随机决定哪条管线是 A、哪条是 B（`seed` 用于复现同一份报告）。"""
    order = [LEGACY, AGENT]
    random.Random(seed).shuffle(order)
    return {SIDE_LABELS[0]: order[0], SIDE_LABELS[1]: order[1]}


def slice_concepts(concepts: Iterable[Any], limit: int) -> list[dict]:
    """按**存储顺序**取前 `limit` 个概念；`limit <= 0` 表示不限制。

    刻意不排序/不随机：评审样本要能被「同一份 schema + 同一个 limit」复现，
    且 operator 能对照界面上的概念顺序定位。
    """
    items = [item for item in (concepts or []) if isinstance(item, dict)]
    if not limit or limit <= 0:
        return items
    return items[:limit]


def compute_side_stats(concepts: Iterable[Any]) -> dict:
    """一侧的机器可算指标（覆盖率、落点数、低置信落点数）。"""
    items = [concept for concept in concepts or [] if isinstance(concept, dict)]
    landed_concepts = 0
    landings = 0
    low_confidence = 0
    for concept in items:
        rows = concept_landings(concept)
        if rows:
            landed_concepts += 1
        landings += len(rows)
        low_confidence += sum(
            1 for row in rows if _fold(row.get("confidence")) == "low")
    return {
        "concepts": len(items),
        "landed_concepts": landed_concepts,
        "landings": landings,
        "low_confidence": low_confidence,
    }


def _provenance_of(concept: Any) -> list[dict]:
    """一个概念的置信度/依据清单（附录用；只收有内容的落点）。"""
    items: list[dict] = []
    for source in concept_landings(concept):
        confidence = str(source.get("confidence") or "").strip()
        rationale = str(source.get("rationale") or "").strip()
        if confidence or rationale:
            items.append({"confidence": confidence, "rationale": rationale})
    return items


def build_report_row(legacy_concept: Any, agent_concept: Any, bundles: Any,
                     sides: dict[str, str],
                     *, excerpt_limit: int = MAX_EXCERPT_CHARS) -> dict:
    """同一概念在两套管线下的输出 → 评审表的一行（A/B 已匿名）。

    同时产出两套单元格：

    - `cells`（盲评，默认用）：只放落点本身；
    - `cells_with_provenance`（`--show-provenance` 调试用）：agent 侧就地带
      置信度/依据。

    另外把原始 `provenance` 也放进行数据，供 `render_report` 生成文末附录。
    两套单元格都由这里生成，`render_report` 只做选择 —— 避免调用方传两个
    不一致的开关时把「盲评」渲染成半盲半明。
    """
    legacy = legacy_concept if isinstance(legacy_concept, dict) else {}
    agent = agent_concept if isinstance(agent_concept, dict) else {}
    by_kind = {LEGACY: legacy, AGENT: agent}

    def _cells(with_provenance: bool) -> dict[str, str]:
        return {
            side: format_landing_cell(
                by_kind.get(kind) or {},
                bundles,
                with_provenance=(kind == AGENT and with_provenance),
            )
            for side, kind in (sides or {}).items()
        }

    return {
        "name": str(legacy.get("name") or agent.get("name") or "(未命名)"),
        "description": str(
            legacy.get("description") or agent.get("description") or ""),
        "cells": _cells(False),
        "cells_with_provenance": _cells(True),
        "evidence": concept_evidence_excerpt(
            [legacy, agent], bundles, excerpt_limit),
        "provenance": {
            side: _provenance_of(by_kind.get(kind) or {})
            for side, kind in (sides or {}).items()
        },
    }


def _render_appendix(rows: Iterable[dict]) -> list[str]:
    """置信度/依据附录：概念 → 侧 → 置信度 + 依据（A/B 仍匿名）。"""
    lines = [
        "## 附录：落点依据（评审完成后再看）",
        "",
        "| 概念 | 侧 | 置信度 | 依据 |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows or []:
        provenance = row.get("provenance") or {}
        for side in SIDE_LABELS:
            items = provenance.get(side) or []
            if not items:
                continue
            confidences = []
            rationales = []
            for item in items:
                confidence = str(item.get("confidence") or "").strip()
                if confidence and confidence not in confidences:
                    confidences.append(confidence)
                rationale = str(item.get("rationale") or "").strip()
                if rationale:
                    rationales.append(rationale)
            lines.append("| " + " | ".join([
                md_cell(row.get("name")),
                side,
                md_cell("/".join(confidences) or "-"),
                md_cell(truncate_text(
                    "；".join(rationales), MAX_RATIONALE_CELL_CHARS)),
            ]) + " |")
    return lines


def render_report(*, tenant_id: str, kb_id: str, generated_at: str,
                  total_concepts: int, rows: Iterable[dict],
                  sides: dict[str, str], side_stats: dict[str, dict],
                  warnings: Optional[list[str]] = None,
                  show_provenance: bool = False) -> str:
    """评审数据 → Markdown 文本。行序 = 概念原序（可复现），A/B 由 `sides` 定。

    默认盲评：主表不含任何 agent 独有信号（置信度/依据/低置信统计），依据统一
    放文末附录；`show_provenance=True` 恢复「就地显示」的调试形态。
    """
    warning_list = [str(item) for item in (warnings or []) if str(item).strip()]
    rows = list(rows or [])
    lines: list[str] = [
        "# 落点 A/B 评审表",
        "",
        f"- 租户：`{tenant_id}`",
        f"- 知识库：`{kb_id}`",
        f"- 生成时间：{generated_at}",
        f"- 概念数：{total_concepts}",
        "- 评审方式：只看 A/B 两列（不知道哪列是哪条管线），"
        "逐条在「判定」列填 `对` / `错` / `说不准`。",
    ]
    if not show_provenance:
        lines.append(
            "- **盲评**：落点依据集中在文末附录，**先判完所有行再打开附录**。"
        )
    lines.extend([
        "",
        "> 口径：两侧都从同一份概念结构出发，只保留人工（`origin=manual`）落点，"
        "其余派生落点清空后重跑 —— 避免上一轮产物污染本次评审。",
        "",
        "| 概念 | 描述 | A 落点 | B 落点 | 证据摘录 | 判定 |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    for row in rows:
        cells = row.get(
            "cells_with_provenance" if show_provenance else "cells") or {}
        lines.append("| " + " | ".join([
            md_cell(row.get("name")),
            md_cell(row.get("description")),
            md_cell(cells.get(SIDE_LABELS[0], "")),
            md_cell(cells.get(SIDE_LABELS[1], "")),
            md_cell(row.get("evidence", "")),
            "",  # 判定：留给评审人
        ]) + " |")

    # 盲评时连「低置信落点」列都不给：只有 agent 落点会带 confidence，这一列
    # 等于直接标出哪侧是新管线。统计留给文末附录/解密之后再看。
    stat_header = "| 侧 | 概念数 | 有落点概念 | 覆盖率 | 落点数 |"
    stat_rule = "| --- | --- | --- | --- | --- |"
    if show_provenance:
        stat_header += " 低置信落点 |"
        stat_rule += " --- |"
    lines.extend(["", "## 机器统计（自动）", "", stat_header, stat_rule])
    for side in SIDE_LABELS:
        kind = (sides or {}).get(side, "")
        stats = (side_stats or {}).get(kind) or {}
        concepts = int(stats.get("concepts") or 0)
        landed = int(stats.get("landed_concepts") or 0)
        pct = (landed / concepts * 100) if concepts else 0.0
        line = (f"| {side} | {concepts} | {landed} | {pct:.1f}% | "
                f"{int(stats.get('landings') or 0)} |")
        if show_provenance:
            line += f" {int(stats.get('low_confidence') or 0)} |"
        lines.append(line)

    lines.extend([
        "",
        "## 人工统计（评审后填写）",
        "",
        "| 侧 | 判定「对」 | 判定「错」 | 精确率 | 需要人工改的条数 |",
        "| --- | --- | --- | --- | --- |",
        "| A |  |  |  |  |",
        "| B |  |  |  |  |",
    ])

    if warning_list:
        lines.extend(["", "## 运行告警", ""])
        lines.extend(f"- {warning}" for warning in warning_list)

    # 顺序按评审动线：先判完行，再看附录（依据），最后才解密。附录放在解密**前**，
    # 避免「从上往下读」时先被身份信息污染。
    if not show_provenance:
        lines.extend(["", *_render_appendix(rows)])

    lines.extend([
        "",
        "## 解密（评审完成前不要看这一节）",
        "",
        f"- A = {_pipeline_label((sides or {}).get(SIDE_LABELS[0]))}",
        f"- B = {_pipeline_label((sides or {}).get(SIDE_LABELS[1]))}",
        "- 结论写法：覆盖率变化、精确率、低置信条数、需要人工改的条数。",
    ])
    lines.append("")
    return "\n".join(lines)


def _pipeline_label(kind: str) -> str:
    if kind == LEGACY:
        return "老管线（align_document_landings）"
    if kind == AGENT:
        return "落点 Agent（LandingAgentRunner）"
    return "(未知)"


# ══════════════════════════════════════════════════════════════════
# DB 读取（全部只读）
# ══════════════════════════════════════════════════════════════════

async def _load_inputs(tenant_id: str, kb_id: str) -> tuple[Any, dict]:
    """读存储的抽象 schema（深拷贝）+ 已编译源 schema → bundles。**不写库**。"""
    from capabilities.knowledge_base.repository.data_source_repository import (
        KnowledgeDataSourceRepository,
    )
    from capabilities.knowledge_base.repository.ontology_abstract_schema_repository import (  # noqa: E501
        OntologyAbstractSchemaRepository,
    )
    from capabilities.knowledge_base.repository.ontology_schema_repository import (
        OntologySchemaRepository,
    )
    from capabilities.knowledge_base.services.abstract_construct import (
        build_source_bundles,
    )
    from jonex_core.common.database import get_db_session

    async with get_db_session() as session:
        stored = await OntologyAbstractSchemaRepository(session).get_by_kb(
            tenant_id, kb_id)
        rows = await OntologySchemaRepository(session).list_compiled_schemas(
            tenant_id, kb_id)
        data_sources = await KnowledgeDataSourceRepository(session).list_by_kb(
            tenant_id, kb_id, offset=0, limit=500)
        ds_names = {source.id: source.name for source in data_sources}
        live = set(ds_names)
        # 与 `AbstractSchemaService._load_source_schemas` 同口径：只读仍然活着的
        # 数据源（残留 schema 行喂给管线只会产生「生下来就 source_gone」的映射）。
        rows = [row for row in rows if row.data_source_id in live]
        bundles = build_source_bundles(rows, ds_names)
        raw_schema = copy.deepcopy(getattr(stored, "schema", None))
    return raw_schema, bundles


def _concepts_of(raw_schema: Any) -> list[dict]:
    if not isinstance(raw_schema, dict):
        return []
    return [entity for entity in (raw_schema.get("entities") or [])
            if isinstance(entity, dict)]


# ══════════════════════════════════════════════════════════════════
# 两条管线（都在内存副本上跑）
# ══════════════════════════════════════════════════════════════════

async def _run_legacy_pass(tenant_id: str, kb_id: str, user_id: str,
                           concepts: list[dict], bundles: dict) -> list[str]:
    """老管线：名字命中 + `align_document_landings` + `apply_llm_landings`。

    顺序与 `AbstractSchemaService._construct_inner` 一致（先名字档，再 LLM 对齐），
    这样 A/B 对比的确实是线上老路径，而不是另行拼装的近似物。
    """
    from capabilities.knowledge_base.services.abstract_construct import (
        apply_llm_landings,
        attach_landings_by_name,
    )
    from capabilities.knowledge_base.services.abstract_schema_service import (
        AbstractSchemaService,
    )
    from jonex_core.common.ontology_llm import align_document_landings

    warnings: list[str] = []
    attach_landings_by_name(concepts, bundles)

    source_catalog, catalog_warnings = (
        AbstractSchemaService._source_landing_catalog(bundles))
    warnings.extend(catalog_warnings)
    document_catalog = AbstractSchemaService._document_landing_catalog(concepts)
    if not source_catalog or not document_catalog:
        warnings.append("老管线跳过 LLM 对齐：概念或源实体为空")
        return warnings

    try:
        alignments = await align_document_landings(
            document_catalog, source_catalog,
            tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 —— 与线上一致：对齐失败 fail-open
        warnings.append(
            f"老管线 LLM 对齐失败（已保留名字档落点）：{str(exc)[:200]}")
        return warnings
    apply_llm_landings(concepts, alignments, bundles)
    return warnings


async def _run_agent_pass(tenant_id: str, kb_id: str, user_id: str,
                          concepts: list[dict], bundles: dict) -> tuple[dict, list[str]]:
    """落点 Agent：`LandingAgentRunner.run`（原地写内存里的 concepts）。"""
    from capabilities.knowledge_base.services.abstract_schema_service import (
        AbstractSchemaService,
    )
    from capabilities.knowledge_base.services.landing_agent.runner import (
        LandingAgentRunner,
    )

    config = dict(AbstractSchemaService._landing_agent_config())
    # `--limit` 已截断概念；同步放宽 runner 上限，避免它再按 max_concepts 截一刀。
    config["max_concepts"] = max(
        int(config.get("max_concepts") or 0), len(concepts))

    async def _progress(progress) -> None:
        total = getattr(progress, "concepts_total", 0) or 0
        done = getattr(progress, "concepts_done", 0) or 0
        # 每 10 个概念报一次，避免输出把终端刷满。
        if done == total or done % 10 == 0:
            print(f"[agent] {done}/{total} 概念（{progress.phase}）",
                  file=sys.stderr)

    runner = LandingAgentRunner(
        tenant_id=tenant_id, kb_id=kb_id, user_id=user_id, config=config)
    result = await runner.run(concepts, bundles, on_progress=_progress)
    return result, list(result.get("warnings") or [])


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

def _limit_arg(value: str) -> int:
    """`--limit` 解析：负数显式报错（而不是悄悄当成不限量）。"""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"不是整数：{value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError(
            f"--limit 不能为负数（收到 {number}）；0 表示不限制")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="落点 A/B 评审：老对齐管线 vs 落点 Agent（只读 DB）")
    parser.add_argument("--tenant-id", required=True, help="合法业务租户")
    parser.add_argument("--kb-id", required=True, help="知识库 ID")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"评审表输出路径（默认 {DEFAULT_OUT}）")
    parser.add_argument(
        "--limit", type=_limit_arg, default=DEFAULT_LIMIT,
        help=("评审**存储顺序**里的前 N 个概念（不排序、不随机）；"
              f"默认 {DEFAULT_LIMIT}；0 表示不限制；负数报错"))
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印统计，不调模型、不跑管线")
    parser.add_argument(
        "--blind", action=argparse.BooleanOptionalAction, default=True,
        help="盲评（默认开）：主表不显示置信度/依据，统一放文末附录")
    parser.add_argument(
        "--show-provenance", action="store_true",
        help="调试用：置信度/依据就地显示在主表（等同 --no-blind）")
    parser.add_argument("--seed", type=int, default=None,
                        help="A/B 匿名分配种子（复现同一份报告时指定）")
    parser.add_argument("--user-id", default="",
                        help="仅写进 LLM 计量/审计，不影响落点")
    return parser


async def _run(args: argparse.Namespace) -> int:
    from jonex_core.common.tenant import require_tenant

    tenant_id = require_tenant(args.tenant_id)
    raw_schema, bundles = await _load_inputs(tenant_id, args.kb_id)
    if raw_schema is None:
        print(f"该知识库还没有抽象 schema 草稿：kb={args.kb_id}", file=sys.stderr)
        return 2
    if not isinstance(raw_schema, dict):
        print("存储的抽象 schema 是 v1 列表形状，脚本只支持 v2 对象形状；"
              "请先在界面上打开/保存一次以完成迁移。", file=sys.stderr)
        return 2

    concepts = _concepts_of(raw_schema)

    if args.dry_run:
        summary = build_dry_run_summary(
            tenant_id=tenant_id, kb_id=args.kb_id,
            entities=concepts, bundles=bundles)
        print(format_dry_run_summary(summary))
        return 0

    concepts = slice_concepts(concepts, args.limit)
    if not concepts:
        print("没有可评审的概念（抽象 schema 为空）。", file=sys.stderr)
        return 2

    # 盲评默认开；`--show-provenance` 与 `--no-blind` 都回退到就地显示。
    show_provenance = bool(args.show_provenance) or not bool(
        getattr(args, "blind", True))

    # 两侧各自一份深拷贝：runner / 老管线都会原地写落点。
    legacy_concepts = reset_derived_landings(concepts)
    agent_concepts = reset_derived_landings(concepts)

    warnings: list[str] = []
    # 两条管线各自兜异常：一侧整 run 级失败（网关挂了/embedding 不可用）不该把
    # 已经跑完的另一侧一起扔掉 —— 评审表照出，失败如实写进「运行告警」。
    try:
        warnings.extend(await _run_legacy_pass(
            tenant_id, args.kb_id, args.user_id, legacy_concepts, bundles))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"老管线运行失败（该列可能只剩名字档落点）：{exc}")
    try:
        agent_result, agent_warnings = await _run_agent_pass(
            tenant_id, args.kb_id, args.user_id, agent_concepts, bundles)
        warnings.extend(agent_warnings)
    except Exception as exc:  # noqa: BLE001
        agent_result = {"stats": {}, "progress": {"error": str(exc)[:200]}}
        warnings.append(f"落点 Agent 运行失败（该列可能只剩人工落点）：{exc}")

    sides = assign_sides(args.seed)
    rows = [
        build_report_row(legacy, agent, bundles, sides)
        for legacy, agent in zip(legacy_concepts, agent_concepts)
    ]
    report = render_report(
        tenant_id=tenant_id, kb_id=args.kb_id,
        generated_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_concepts=len(concepts), rows=rows, sides=sides,
        side_stats={
            LEGACY: compute_side_stats(legacy_concepts),
            AGENT: compute_side_stats(agent_concepts),
        },
        warnings=warnings, show_provenance=show_provenance,
    )

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    print(f"评审表已写入：{out_path}（{len(rows)} 行）")
    print(f"agent stats：{agent_result.get('stats')}")
    print(f"agent progress：{agent_result.get('progress')}")
    if warnings:
        print(f"运行告警 {len(warnings)} 条，见报告「运行告警」一节。")
    if show_provenance:
        print("提醒：本次为调试形态（置信度/依据就地显示）。")
    else:
        print("提醒：盲评模式 —— 先判完所有行，再打开文末附录与解密。")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 —— CLI 边界：把栈留在日志，人看一行
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
