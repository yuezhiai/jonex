"""推理步骤「输入 / 输出」构建器。

设计目标（见 `docs/ai-mcp/reasoning-chain-design.md`）：

1. **集中派生**：各 stage 的 ``detail`` 形状差异很大，展示层不该逐个认识它们；
   这里统一投影成 ``ReasoningIOItem`` 列表，``ReasoningCollector`` 自动调用。
2. **统一脱敏**：禁键、URL 去 query、字符串/列表/表格裁剪都在这一处完成，
   调用点无法绕过。
3. **体积可控**：单步骤 input + output 序列化上限 8KB；检索历史是 JSONB 整份
   落库，不设上限会被大体量 detail 放大。
4. **失败也有输出**：任何步骤至少产出「检索问题 + 知识库范围」作为输入、
   「执行结论」作为输出，前端不会出现空白展开区。

``detail`` 仍然是机器/专用交互数据，本模块只读不写。
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

# ── 体积与裁剪上限 ──────────────────────────────────────────────
IO_MAX_BYTES = 8 * 1024
TEXT_MAX = 500
PREVIEW_MAX = 200
LIST_MAX = 20
TABLE_MAX = 50
DEPTH_MAX = 3

#: 关闭后 ``ReasoningCollector`` 不产出 input/output（回滚开关）。
REASONING_IO_ENABLED = os.getenv("REASONING_IO_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

# ── 脱敏禁键（大小写不敏感）──────────────────────────────────────
_DENY_KEYS = {
    "tenant_id", "raw_url", "api_key", "authorization", "token",
    "cookie", "password", "secret", "prompt", "system_prompt",
}


@dataclass
class ReasoningContext:
    """单次检索请求的展示上下文（由各入口构造，供适配器派生输入）。"""

    query: str = ""
    kb_ids: list[str] = field(default_factory=list)
    mode: Optional[str] = None
    top_k: Optional[int] = None
    #: {kb_id: 显示名}，由 service 每请求解析一次；缺失时回落 kb_id。
    kb_names: dict[str, str] = field(default_factory=dict)
    #: 上下文预算（字符数），用于分流/召回步骤展示。
    budget_chars: Optional[int] = None


# ═══════════════════════════════════════════════════════════════
# 展示项构造 helper
# ═══════════════════════════════════════════════════════════════

def text(label: str, value: Any) -> dict:
    """单值展示项。"""
    return {"label": label, "type": "text", "value": value}


def items(label: str, values: Any) -> dict:
    """列表展示项（前端用「、」连接）。"""
    if values is None:
        values = []
    elif not isinstance(values, (list, tuple, set)):
        values = [values]
    return {"label": label, "type": "list", "value": list(values)}


def kv(label: str, value: Any) -> dict:
    """二级键值对展示项。"""
    return {"label": label, "type": "kv", "value": value or {}}


def table(
    label: str,
    columns: list[dict[str, str]],
    rows: Any,
    *,
    total: Optional[int] = None,
    preview_rows: int = 5,
) -> dict:
    """表格展示项；``rows`` 超过 ``TABLE_MAX`` 时由脱敏层截断并保留真实总数。"""
    rows = list(rows or [])
    return {
        "label": label,
        "type": "table",
        "columns": columns,
        "rows": rows,
        "preview_rows": preview_rows,
        "total": total if total is not None else len(rows),
        "truncated": False,
    }


# ── 展示值规范化（枚举 / 分数 / 布尔 / 知识库名 / Markdown）──────

#: 内部枚举值 → 产品文案。未登记的枚举原样返回，避免吞掉新值。
_ENUM_LABELS = {
    "mixed": "混合检索",
    "hybrid": "混合检索",
    "single": "单通道",
    "ontology": "本体",
    "rag": "知识库",
    "llm-wiki": "LLM Wiki",
    "llm_wiki": "LLM Wiki",
    "jonex-wiki": "Jonex Wiki",
    "jonex_wiki": "Jonex Wiki",
    "none": "无",
    "legacy": "阈值判据",
    "rerank": "重排判据",
    "rag_chunk_qa": "知识库片段作答",
    "rag_chunk_qa_fast": "知识库片段作答（快档）",
    "deep": "深度检索",
    "simple": "简单查询",
    "complex": "复杂查询",
    "fact": "本体事实",
    "unknown": "未分类",
    "high": "高",
    "mid": "中",
    "low": "低",
    # 深度查询的聚合操作
    "compute": "计算",
    "compare": "比较",
    "count": "计数",
    "reason": "推理",
    "convert": "换算",
}

_BAND_LABELS = {"high": "高带", "mid": "中带", "low": "低带", "none": "无"}

_CONF_KEY_LABELS = {
    "has_attrs": "属性",
    "has_desc": "描述",
    "max_hop": "最大跳数",
    "conflict": "存在冲突",
    "scoped_by_citations": "受引用范围限制",
}


def _label(value: Any) -> Any:
    """枚举值转产品文案（只处理字符串，其他类型原样返回）。"""
    if isinstance(value, str):
        return _ENUM_LABELS.get(value, value)
    return value


def _fmt_score(value: Any) -> Any:
    """分数统一两位小数；非数值保持原样（缺失由前端显示「—」）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return None if (math.isnan(value) or math.isinf(value)) else f"{value:.2f}"
    return value


def _fmt_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return "是" if value else "否"
    return value


def _kb_name(ctx: ReasoningContext, kb_id: Any) -> Any:
    if kb_id is None:
        return None
    return ctx.kb_names.get(str(kb_id)) or kb_id


def _kb_scope(ctx: ReasoningContext) -> Optional[str]:
    """知识库范围展示：单库用名称，多库用「N 个知识库」。"""
    if not ctx.kb_ids:
        return None
    if len(ctx.kb_ids) == 1:
        return str(_kb_name(ctx, ctx.kb_ids[0]))
    return f"{len(ctx.kb_ids)} 个知识库"


def _plain_preview(value: Any, limit: int = PREVIEW_MAX) -> str:
    """把 Markdown / 引用标清洗成单段纯文本预览（答案、chunk 片段共用）。"""
    s = str(value or "")
    if not s:
        return ""
    s = re.sub(r"```.*?```", " ", s, flags=re.S)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"(\*\*|__)(.*?)\1", r"\2", s)
    s = re.sub(r"(?<!\*)\*(?!\*)([^*]+)(?<!\*)\*(?!\*)", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\[ref_\d+\]", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s).strip()
    return _clip_text(s, limit) if s else ""


# ═══════════════════════════════════════════════════════════════
# 各 stage 适配器
# ═══════════════════════════════════════════════════════════════

def _io_intent_classify(d: dict, ctx: ReasoningContext):
    return (
        [text("检索问题", ctx.query)],
        [
            text("意图", _label(d.get("intent"))),
            items("操作类型", [_label(v) for v in (d.get("ops") or [])]),
            items("主体词", d.get("subjects") or []),
        ],
    )


def _io_query_plan(d: dict, ctx: ReasoningContext):
    subs = d.get("sub_questions") or []
    agg = d.get("aggregation") or {}
    rows = [
        {
            "id": s.get("id"),
            "ask": s.get("ask"),
            "expect": s.get("expect"),
        }
        for s in subs if isinstance(s, dict)
    ]
    out = [
        text("子查询数量", len(rows)),
        table(
            "子查询",
            [
                {"key": "id", "label": "编号"},
                {"key": "ask", "label": "子问题"},
                {"key": "expect", "label": "期望字段"},
            ],
            rows,
            preview_rows=5,
        ),
        text("聚合方式", _label(agg.get("op"))),
        text("聚合说明", agg.get("instruction")),
    ]
    return ([text("检索问题", ctx.query)], out)


def _io_subquery(d: dict, ctx: ReasoningContext):
    ask = d.get("ask") or (ctx.query)
    inp = [
        text("子问题", ask),
        text("期望字段", d.get("expect")),
        kv("检索参数", {
            "返回数量": ctx.top_k,
            "检索方式": _label(ctx.mode or "hybrid"),
        }),
    ]
    out = [
        text("取值", d.get("value")),
        text("答案预览", d.get("answer_preview")),
        text("来源", _label(d.get("source"))),
        text("可溯源", "是" if d.get("grounded") else "否"),
        text("引用数", d.get("ref_count")),
    ]
    return inp, out


def _io_synthesis(d: dict, ctx: ReasoningContext):
    facts = d.get("facts") or []
    inp = [
        text("检索问题", ctx.query),
        text("聚合方式", _label(d.get("aggregation_op"))),
        table(
            "取证要素",
            [
                {"key": "id", "label": "子查询"},
                {"key": "value", "label": "取值"},
            ],
            facts,
            preview_rows=10,
        ),
    ]
    out = [text("聚合方式", _label(d.get("aggregation_op")))]
    return inp, out


def _io_strict_attempt(d: dict, ctx: ReasoningContext):
    gear = d.get("gear") or {}
    inp = [
        text("检索问题", ctx.query),
        kv("档位参数", gear),
    ]
    out = [
        text("来源", _label(d.get("source"))),
        text("可靠性评分", _fmt_score(d.get("score"))),
        text("是否达标", "是" if d.get("passed") else "否"),
        text("答案预览", d.get("answer_preview")),
        text("引用数", d.get("ref_count")),
        kv("校验项", d.get("checks") or {}),
    ]
    return inp, out


def _io_strict_verify(d: dict, ctx: ReasoningContext):
    inp = [text("检索问题", ctx.query), kv("校验参数", d)]
    return inp, []


def _io_ontology_match(d: dict, ctx: ReasoningContext):
    hits = [h for h in (d.get("hits") or []) if isinstance(h, dict)]
    names: list[str] = []
    reranked = False
    for h in hits:
        parts = []
        if h.get("rerank_score") is not None:
            reranked = True
            parts.append(f"重排 {_fmt_score(h.get('rerank_score'))}")
        if h.get("ft_score") is not None:
            parts.append(f"全文 {_fmt_score(h.get('ft_score'))}")
        if h.get("vscore") is not None:
            parts.append(f"向量 {_fmt_score(h.get('vscore'))}")
        name = h.get("name") or "未命名实体"
        names.append(f"{name}（{' / '.join(parts)}）" if parts else name)
    inp = [
        text("检索问题", ctx.query),
        text("来源对象", _kb_scope(ctx)),
        kv("关键配置", {
            "返回数量": ctx.top_k or 10,
            "结果重排": "是" if reranked else "否",
        }),
    ]
    out = [
        text("命中实体数量", d.get("total_hits", len(hits))),
        text("命中知识库数", d.get("kb_count", len(ctx.kb_ids))),
        items("匹配实体", names),
    ]
    return inp, out


def _io_route_decision(d: dict, ctx: ReasoningContext):
    groups = d.get("pipeline_groups")
    if isinstance(groups, dict):
        # mix 入口「分流决策」：只报通道与各通道 KB 数，不铺开 KB ID 清单。
        selected = [name for name, kids in groups.items() if kids]
        routed = "single" if len(selected) == 1 else "mixed"
        counts = {
            _label(name): len(kids or [])
            for name, kids in groups.items()
        }
        budget = (
            f"{ctx.budget_chars} 字符" if ctx.budget_chars else None
        )
        inp = [
            text("检索问题", ctx.query),
            items("候选检索通道", [_label(n) for n in groups.keys()]),
            kv("候选知识库", counts),
            kv("检索参数", {
                "检索方式": _label(ctx.mode or "mixed"),
                "返回数量": ctx.top_k,
            }),
        ]
        if budget:
            inp.append(text("上下文预算", budget))
        out = [
            text("分流结果", _label(routed)),
            items("选中通道", [_label(n) for n in selected]),
        ]
        return inp, out

    # 本体链「路由决策」
    threshold_source = d.get("threshold_source") or "legacy"
    criteria = {"判据": _label(threshold_source)}
    if threshold_source == "rerank":
        criteria.update({
            "重排分": _fmt_score(d.get("rerank_score")),
            "重排阈值": _fmt_score(d.get("rerank_threshold")),
            "重排失败": _fmt_bool(d.get("rerank_failed")),
        })
    else:
        criteria.update({
            "全文分": _fmt_score(d.get("ft_score")),
            "全文阈值": _fmt_score(d.get("ftscore_threshold")),
            "向量分": _fmt_score(d.get("vscore")),
            "向量阈值": _fmt_score(d.get("vscore_threshold")),
        })
    inp = [
        text("检索问题", ctx.query),
        kv("路由判据", criteria),
    ]
    out = [
        text("选中路径", _label(d.get("route"))),
        text("命中来源", _label(d.get("source")) or None),
        kv("生效分数", {
            "全文分": _fmt_score(d.get("ft_score")),
            "向量分": _fmt_score(d.get("vscore")),
            "重排分": _fmt_score(d.get("rerank_score")),
        }),
    ]
    return inp, out


def _io_fact_lookup(d: dict, ctx: ReasoningContext):
    # stub 过滤
    if "stub_filtered" in d:
        names = d.get("stub_filtered") or []
        return (
            [text("过滤规则", "描述为空且属性为空"), text("检索问题", ctx.query)],
            [
                text("过滤数量", len(names)),
                items("被过滤实体", names),
            ],
        )
    # inverse 翻译
    if "inverse_map" in d:
        imap = d.get("inverse_map") or {}
        rows = [{"from": k, "to": v} for k, v in imap.items()]
        return (
            [text("映射来源", "compiled schema inverse_of 声明")],
            [
                text("映射条数", len(rows)),
                table(
                    "关系映射",
                    [
                        {"key": "from", "label": "原关系"},
                        {"key": "to", "label": "翻译后关系"},
                    ],
                    rows,
                    preview_rows=10,
                ),
            ],
        )
    # 邻域事实检索
    if "fact_count" in d:
        facts = d.get("facts") or []
        rows = [
            {
                "relation": f.get("relation_type"),
                "target": f.get("target"),
                "target_type": _label(f.get("target_type")),
                "hop": f.get("hop"),
            }
            for f in facts if isinstance(f, dict)
        ]
        kb_id = d.get("kb_id")
        inp = [
            text("检索问题", ctx.query),
            items("起始实体", d.get("entities") or []),
            text("知识库", _kb_name(ctx, kb_id) if kb_id else _kb_scope(ctx)),
            text("跳数", d.get("depth")),
        ]
        out = [
            text("事实数量", d.get("fact_count", len(rows))),
            kv("跳数分布", d.get("hop_distribution") or {}),
            text("是否截断", "是" if d.get("truncated") else "否"),
            table(
                "事实明细",
                [
                    {"key": "relation", "label": "关系"},
                    {"key": "target", "label": "目标实体"},
                    {"key": "target_type", "label": "类型"},
                    {"key": "hop", "label": "跳数"},
                ],
                rows,
                preview_rows=5,
            ),
        ]
        return inp, out
    # procedural / 枚举意图处置等：输入问题，输出执行结论（由 fallback 补）
    return ([text("检索问题", d.get("query") or ctx.query)], [])


def _io_llm_answer(d: dict, ctx: ReasoningContext):
    if "entities" in d:
        return (
            [items("命中实体", d.get("entities") or [])],
            [text("处置", "命中实体但无描述/属性/关系，返回友好提示")],
        )
    facts = [f for f in (d.get("facts_preview") or []) if isinstance(f, dict)]
    rows = [
        {
            "source": f.get("source") or f.get("source_entity"),
            "relation": f.get("relation_type") or f.get("relation"),
            "target": f.get("target") or f.get("target_entity"),
            "kb": _kb_name(ctx, f.get("kb_id") or (ctx.kb_ids[0] if ctx.kb_ids else None)),
        }
        for f in facts
    ]
    inp = [
        text("检索问题", ctx.query),
        table(
            "输入事实",
            [
                {"key": "source", "label": "来源实体"},
                {"key": "relation", "label": "关系"},
                {"key": "target", "label": "事实"},
                {"key": "kb", "label": "知识库"},
            ],
            rows,
            total=d.get("fact_count", len(rows)),
            preview_rows=5,
        ),
        kv("作答参数", {
            "思考模式": "开启" if d.get("thinking") else "关闭",
            "超时(秒)": d.get("answer_timeout"),
        }),
    ]
    return inp, []


def _io_arbitration(d: dict, ctx: ReasoningContext):
    # L2 证据校验
    if "threshold_hi" in d:
        inp = [
            text("检索问题", ctx.query),
            kv("支持度阈值", {
                "高带": _fmt_score(d.get("threshold_hi")),
                "低带": _fmt_score(d.get("threshold_lo")),
            }),
            text("结构对齐", _fmt_bool(d.get("struct_align"))),
        ]
        out = [
            text("证据带", _BAND_LABELS.get(str(d.get("band")), d.get("band"))),
            text("支持度", _fmt_score(d.get("support"))),
            items("Top 支持分", [_fmt_score(v) for v in (d.get("top_scores") or [])]),
            text("独立证据数", d.get("doc_n")),
            kv("池构成", {
                _label(k): v for k, v in (d.get("src_counts") or {}).items()
            }),
        ]
        if d.get("reason"):
            out.append(text("跳过原因", d.get("reason")))
        return inp, out
    # 最终裁决
    if "verdict" in d:
        inp = [
            text("检索问题", ctx.query),
            text("本体答案预览", _plain_preview(d.get("ontology_answer"))),
            text("RAG 答案预览", _plain_preview(d.get("rag_answer"))),
            text("原答案来源", _label(d.get("original_source"))),
        ]
        out = [
            text("裁决结果", _label(d.get("verdict"))),
            text("置信度", _fmt_score(d.get("confidence"))),
            text("理由", d.get("reason")),
        ]
        return inp, out
    # 触发信号 / 跳过
    if "confidence" in d:
        criteria = {
            _CONF_KEY_LABELS.get(k, k): _fmt_bool(v)
            for k, v in d.items() if k != "confidence"
        }
        return (
            [text("检索问题", ctx.query)],
            [text("本体置信度", _label(d.get("confidence"))),
             kv("置信判据", criteria)],
        )
    if "slot_hit" in d or "lowq_signals" in d:
        return (
            [text("检索问题", ctx.query)],
            [
                text("本体结构化槽位命中", "是" if d.get("slot_hit") else "否"),
                items("低质 chunk 信号", d.get("lowq_signals") or []),
            ],
        )
    return ([text("检索问题", ctx.query)], [])


def _io_ref_retrieve(d: dict, ctx: ReasoningContext):
    if "pool_size" in d:
        return (
            [text("检索问题", ctx.query), text("知识库", _kb_scope(ctx))],
            [text("候选池大小", d.get("pool_size")),
             text("处理", "已合并三路召回，并完成 rerank + 统一分")],
        )
    refs = [r for r in (d.get("refs_preview") or []) if isinstance(r, dict)]
    chunk_rows = [
        {
            "content": _plain_preview(r.get("text") or r.get("content"), 120),
            "doc": r.get("file_name") or r.get("file_path"),
            "score": _fmt_score(r.get("score")),
        }
        for r in refs
    ]
    inp = [
        text("检索问题", ctx.query),
        text("知识库", _kb_scope(ctx)),
        text("候选 chunk 总数", d.get("source_chunks_total")),
        text("唯一文件数", d.get("unique_file_paths")),
    ]
    out = [
        text("chunk 级引用数", d.get("chunk_ref_count")),
        text("文档级兜底引用数", d.get("fallback_ref_count")),
        text("裁剪数量", d.get("pruned_count")),
        text("保留最低相关度", _fmt_score(d.get("min_kept_relevance"))),
        text("按答案引用标保留", (
            f"{d.get('cited_filter_kept')}/{d.get('cited_filter_total')}"
            if d.get("cited_filter_kept") is not None else None
        )),
        items("Top 权重", d.get("top_ref_scores") or []),
        table(
            "引用片段",
            [
                {"key": "content", "label": "内容"},
                {"key": "doc", "label": "文档"},
                {"key": "score", "label": "评分"},
            ],
            chunk_rows,
            total=d.get("refs_preview_total", d.get("chunk_ref_count", len(chunk_rows))),
            preview_rows=5,
        ),
    ]
    return inp, out


def _io_context_retrieve(d: dict, ctx: ReasoningContext):
    if "lowq_filtered" in d:
        return (
            [text("检索问题", ctx.query)],
            [text("剔除数量", len(d.get("lowq_filtered") or [])),
             items("被剔除 chunk", d.get("lowq_filtered") or [])],
        )
    if "dual_path_quota" in d:
        return (
            [text("检索问题", ctx.query)],
            [kv("双路配额", d.get("dual_path_quota") or {})],
        )
    recalls = d.get("recalls") or []
    rows = [
        {
            "file_name": r.get("file_name"),
            "kb": _kb_name(ctx, r.get("kb_id")),
            "chunk_index": r.get("chunk_index"),
            "score": _fmt_score(r.get("score")),
        }
        for r in recalls if isinstance(r, dict)
    ]
    inp = [
        text("检索问题", ctx.query),
        text("知识库", _kb_scope(ctx)),
        kv("召回参数", {
            "返回数量": ctx.top_k,
            "预算字符": ctx.budget_chars or d.get("budget_chars"),
        }),
    ]
    out = [
        items("成功知识库", [_kb_name(ctx, k) for k in (d.get("kb_ok") or [])]),
        items("失败知识库", [_kb_name(ctx, k) for k in (d.get("kb_failed") or [])]),
        text("原始召回数", d.get("raw_recall_count")),
        text("进入 prompt 数", d.get("prompt_chunk_count")),
        text("软删剔除数", d.get("soft_deleted_filtered")),
        text("超预算数", d.get("oversized_count")),
        table(
            "召回片段",
            [
                {"key": "file_name", "label": "文件"},
                {"key": "kb", "label": "知识库"},
                {"key": "chunk_index", "label": "Chunk"},
                {"key": "score", "label": "评分"},
            ],
            rows,
            total=d.get("recall_count", len(rows)),
            preview_rows=10,
        ),
    ]
    return inp, out


def _io_chunk_answer(d: dict, ctx: ReasoningContext):
    return (
        [
            text("检索问题", ctx.query),
            text("使用 chunk 数", d.get("chunk_count")),
            text("作答场景", _label(d.get("scene"))),
        ],
        (
            [text("作答耗时(ms)", d.get("answer_ms"))]
            if d.get("answer_ms") is not None else []
        ),
    )


def _io_openkb_query(d: dict, ctx: ReasoningContext):
    inp = [
        text("知识库", _kb_name(ctx, d.get("kb_id"))),
        text("检索问题", ctx.query),
        text("引擎", "LLM Wiki"),
    ]
    if d.get("reason") == "empty_kb":
        return inp, [text("结果", "知识库暂无文档，未调用引擎")]
    if d.get("error"):
        return inp, [
            text("结果", "查询异常"),
            text("错误", d.get("error")),
            text("耗时", d.get("duration_ms")),
        ]
    turns = d.get("turns") or []
    turn_rows = [
        {
            "turn": i,
            "calls": len(t.get("calls") or []) if isinstance(t, dict) else 0,
        }
        for i, t in enumerate(turns, 1)
    ]
    effective = d.get("effective")
    if effective is None:
        effective = bool(d.get("answer_preview"))
    out = [
        text("是否有效答案", "是" if effective else "否"),
        text("答案预览", d.get("answer_preview")),
        text("轮数", d.get("turn_count", len(turns))),
        text("工具调用次数", sum(r["calls"] for r in turn_rows)),
        text("耗时", d.get("duration_ms")),
        table(
            "检索轮次",
            [
                {"key": "turn", "label": "轮次"},
                {"key": "calls", "label": "工具调用数"},
            ],
            turn_rows,
            preview_rows=5,
        ),
    ]
    if d.get("tool_calls_truncated"):
        out.append(text("轨迹截断", "是"))
    return inp, out


def _io_jonex_wiki_query(d: dict, ctx: ReasoningContext):
    inp = [
        text("知识库", _kb_name(ctx, d.get("kb_id"))),
        text("检索问题", ctx.query),
        text("引擎", "Jonex Wiki"),
    ]
    if d.get("reason") == "empty_kb":
        return inp, [text("结果", "知识库暂无文档，未调用引擎")]
    if d.get("error"):
        return inp, [
            text("结果", "查询异常"),
            text("错误", d.get("error")),
            text("耗时", d.get("duration_ms")),
        ]
    calls = d.get("calls") or []
    call_rows = [
        {
            "tool": c.get("tool"),
            "query": c.get("query") or c.get("from_query"),
            "paths": "、".join((c.get("paths") or [])[:5]),
            "count": c.get("count"),
        }
        for c in calls if isinstance(c, dict)
    ]
    effective = d.get("effective")
    if effective is None:
        effective = bool(d.get("answer_preview"))
    out = [
        text("是否有效答案", "是" if effective else "否"),
        text("答案预览", d.get("answer_preview")),
        text("检索/阅读调用次数", d.get("tool_calls_total")),
        text("引用数", d.get("citation_count")),
        text("耗时", d.get("duration_ms")),
        table(
            "调用链",
            [
                {"key": "tool", "label": "工具"},
                {"key": "query", "label": "检索词"},
                {"key": "paths", "label": "页面"},
                {"key": "count", "label": "计数"},
            ],
            call_rows,
            preview_rows=5,
        ),
    ]
    if d.get("tool_calls_truncated"):
        out.append(text("轨迹截断", "是"))
    return inp, out


def _io_fusion(d: dict, ctx: ReasoningContext):
    if d.get("partial_failed"):
        return (
            [text("检索问题", ctx.query)],
            [
                text("融合结果", "仅单侧有效，无需融合"),
                items("失败/无答案侧", d.get("failed_pipelines") or []),
            ],
        )
    if "valid_answer_count" in d:
        return (
            [text("检索问题", ctx.query)],
            [text("有效答案数", d.get("valid_answer_count")),
             text("融合结果", "无需融合")],
        )
    return ([text("检索问题", ctx.query)], [])


def _io_timeline_graph(d: dict, ctx: ReasoningContext):
    inp = [
        text("检索问题", ctx.query),
        text("意图", d.get("intent")),
        text("模板", d.get("template")),
        text("查询描述", d.get("cypher")),
        kv("模板参数", d.get("params") or {}),
    ]
    out = [
        text("命中事实数", d.get("hits")),
        text("首条事实", d.get("top_fact")),
    ]
    return inp, out


_IO_BUILDERS: dict[str, Callable[[dict, ReasoningContext], tuple[list, list]]] = {
    "intent_classify": _io_intent_classify,
    "query_plan": _io_query_plan,
    "subquery": _io_subquery,
    "synthesis": _io_synthesis,
    "strict_attempt": _io_strict_attempt,
    "strict_verify": _io_strict_verify,
    "ontology_match": _io_ontology_match,
    "route_decision": _io_route_decision,
    "fact_lookup": _io_fact_lookup,
    "llm_answer": _io_llm_answer,
    "arbitration": _io_arbitration,
    "ref_retrieve": _io_ref_retrieve,
    "context_retrieve": _io_context_retrieve,
    "chunk_answer": _io_chunk_answer,
    "openkb_query": _io_openkb_query,
    "jonex_wiki_query": _io_jonex_wiki_query,
    "fusion": _io_fusion,
    "timeline_graph": _io_timeline_graph,
}


# ═══════════════════════════════════════════════════════════════
# 脱敏 / 裁剪
# ═══════════════════════════════════════════════════════════════

def _is_denied_key(key: str) -> bool:
    k = key.strip().lower()
    if not k:
        return False
    if k in _DENY_KEYS:
        return True
    if "prompt" in k:
        return True
    return k.endswith("_token") or k.endswith("_secret") or k.endswith("_password")


def _strip_url(value: str) -> str:
    """预签名 URL 一律剥离 query（签名/凭据都在 query 里）。"""
    if not (value.startswith("http://") or value.startswith("https://")):
        return value
    try:
        p = urlsplit(value)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except Exception:  # noqa: BLE001 — 脱敏失败时宁可丢弃到字符串截断
        return value.split("?", 1)[0]


def _clip_text(value: str, limit: int = TEXT_MAX) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _sanitize_value(value: Any, depth: int = 0, text_limit: int = TEXT_MAX) -> Any:
    """递归脱敏：禁键、URL 去 query、非有限浮点置 None、深度/条数限制。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, str):
        return _clip_text(_strip_url(value), text_limit)
    if depth >= DEPTH_MAX:
        # 超出深度：容器折叠成字符串，避免 detail 里的深层结构被整体带出
        try:
            return _clip_text(json.dumps(value, ensure_ascii=False, default=str), text_limit)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, (list, tuple, set)):
        return [
            _sanitize_value(v, depth + 1, text_limit)
            for v in list(value)[:LIST_MAX]
        ]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if len(out) >= LIST_MAX:
                break
            key = str(k)
            if _is_denied_key(key):
                continue
            out[key] = _sanitize_value(v, depth + 1, text_limit)
        return out
    return _clip_text(str(value), text_limit)


def sanitize_item(item: Any) -> Optional[dict]:
    """把单个展示项裁剪成契约形状；非法输入返回 None。"""
    if not isinstance(item, dict) or not item.get("label"):
        return None
    label = _clip_text(str(item.get("label")), 100)
    typ = str(item.get("type") or "text")
    out: dict[str, Any] = {"label": label, "type": typ}

    if typ == "table":
        columns = []
        for c in (item.get("columns") or [])[:20]:
            if isinstance(c, dict) and c.get("key"):
                columns.append({
                    "key": str(c.get("key")),
                    "label": str(c.get("label") or c.get("key")),
                })
        rows = []
        for r in list(item.get("rows") or [])[:TABLE_MAX]:
            if isinstance(r, dict):
                row = {}
                for k, v in r.items():
                    if len(row) >= 20:
                        break
                    if _is_denied_key(str(k)):
                        continue
                    row[str(k)] = _sanitize_value(v, depth=1)
                rows.append(row)
        total = item.get("total")
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(rows)
        preview = item.get("preview_rows")
        if not isinstance(preview, int) or isinstance(preview, bool) or preview < 1:
            preview = 5
        out.update({
            "columns": columns,
            "rows": rows,
            "total": total,
            "preview_rows": min(preview, TABLE_MAX),
            "truncated": bool(item.get("truncated")) or total > len(rows),
        })
    elif typ == "list":
        out["value"] = [
            _sanitize_value(v, depth=1)
            for v in (item.get("value") or [])[:LIST_MAX]
        ]
    elif typ == "kv":
        val = item.get("value")
        out["value"] = _sanitize_value(val if isinstance(val, dict) else {"值": val}, depth=1)
    elif typ == "json":
        out["value"] = _sanitize_value(item.get("value"), depth=0)
    else:
        out["type"] = "text"
        out["value"] = _sanitize_value(item.get("value"), depth=1)

    if item.get("note"):
        out["note"] = _clip_text(str(item["note"]), PREVIEW_MAX)
    return out


def _pack(raw: Optional[list]) -> list[dict]:
    return [x for x in (sanitize_item(i) for i in (raw or [])) if x is not None]


def _measure(inp: list[dict], out: list[dict]) -> int:
    try:
        return len(json.dumps(
            {"input": inp, "output": out},
            ensure_ascii=False, default=str, allow_nan=False,
        ).encode("utf-8"))
    except Exception:  # noqa: BLE001 — 只要有非 JSON 值就按超限处理
        return IO_MAX_BYTES + 1


def _shrink_tables(items: list[dict]) -> None:
    for it in items:
        if it.get("type") != "table":
            continue
        rows = it.get("rows") or []
        preview = int(it.get("preview_rows") or 5)
        if len(rows) > preview:
            it["rows"] = rows[:preview]
            it["truncated"] = True


def _minimize(items: list[dict], text_limit: int = 120) -> None:
    for it in items:
        typ = it.get("type")
        if typ == "table":
            it["rows"] = (it.get("rows") or [])[:3]
            it["truncated"] = True
        elif typ == "list":
            it["value"] = (it.get("value") or [])[:10]
        elif typ == "kv":
            val = it.get("value") or {}
            if isinstance(val, dict):
                it["value"] = {
                    k: _sanitize_value(v, depth=1, text_limit=text_limit)
                    for k, v in list(val.items())[:10]
                }
        elif typ == "json":
            try:
                it["value"] = _clip_text(
                    json.dumps(it.get("value"), ensure_ascii=False, default=str),
                    text_limit,
                )
            except Exception:  # noqa: BLE001
                it["value"] = None
        else:
            it["value"] = _sanitize_value(it.get("value"), depth=1, text_limit=text_limit)


def sanitize_step_io(
    raw_input: Optional[list], raw_output: Optional[list],
) -> tuple[list[dict], list[dict], bool]:
    """脱敏 + 按 8KB 上限逐级裁剪。

    Returns: ``(input_items, output_items, truncated)``。
    """
    inp, out = _pack(raw_input), _pack(raw_output)
    if _measure(inp, out) <= IO_MAX_BYTES:
        return inp, out, False

    # ① 表格退到默认预览行数
    _shrink_tables(inp)
    _shrink_tables(out)
    if _measure(inp, out) <= IO_MAX_BYTES:
        return inp, out, True

    # ② 丢弃无法表格化的 json 项
    inp = [i for i in inp if i.get("type") != "json"]
    out = [i for i in out if i.get("type") != "json"]
    if _measure(inp, out) <= IO_MAX_BYTES:
        return inp, out, True

    # ③ 极端收缩：表格 3 行、列表 10 项、文本 120 字
    _minimize(inp)
    _minimize(out)
    if _measure(inp, out) <= IO_MAX_BYTES:
        return inp, out, True

    # ④ 仍超限：保留前 6 项
    inp, out = inp[:6], out[:6]
    if _measure(inp, out) <= IO_MAX_BYTES:
        return inp, out, True

    # ⑤ 最后兜底：各留 1 项，再逐项丢弃直到装得下
    inp, out = inp[:1], out[:1]
    while _measure(inp, out) > IO_MAX_BYTES and (inp or out):
        if len(out) > len(inp):
            out.pop()
        elif inp:
            inp.pop()
        else:
            out.pop()
    return inp, out, True


# ═══════════════════════════════════════════════════════════════
# 对外入口
# ═══════════════════════════════════════════════════════════════

def _fallback_io(
    summary: Optional[str], ctx: ReasoningContext,
) -> tuple[list, list]:
    inp: list[dict] = [text("检索问题", ctx.query)]
    scope = _kb_scope(ctx)
    if scope:
        inp.append(text("知识库", scope))
    params = {
        "检索方式": _label(ctx.mode),
        "返回数量": ctx.top_k,
    }
    params = {k: v for k, v in params.items() if v is not None}
    if params:
        inp.append(kv("检索参数", params))
    out: list[dict] = [text("执行结论", summary or "—")]
    return inp, out


def build_step_io(
    stage: str,
    *,
    summary: Optional[str],
    detail: Optional[dict],
    ctx: ReasoningContext,
) -> tuple[list[dict], list[dict], bool]:
    """按 stage 派生并脱敏单步 I/O。未知 stage 走通用兜底。"""
    d = detail if isinstance(detail, dict) else {}
    builder = _IO_BUILDERS.get(stage)
    inp: list[dict] = []
    out: list[dict] = []
    if builder is not None:
        try:
            inp, out = builder(d, ctx)
        except Exception:  # noqa: BLE001 — 展示派生失败绝不能影响检索
            inp, out = [], []
    if not inp or not out:
        f_in, f_out = _fallback_io(summary, ctx)
        inp = inp or f_in
        out = out or f_out
    return sanitize_step_io(inp, out)


# ── 显式 I/O 便捷构造（供 search_service 的关键作答步骤使用）──

def answer_output(label: str, answer: Optional[str], **extra: Any) -> list[dict]:
    """答案预览输出（清洗 Markdown / 引用标并截断到 200 字）。"""
    out = [text(label, _plain_preview(answer))]
    for key, val in extra.items():
        out.append(text(key, val))
    return out


def fusion_input(per_kb: list[dict]) -> list[dict]:
    """多答案融合的输入：各侧/各 KB 答案预览表。"""
    rows = []
    for p in per_kb or []:
        if not isinstance(p, dict):
            continue
        rows.append({
            "kb": p.get("kb_name") or p.get("kb_id"),
            "source": _label(p.get("source")),
            "answer": _plain_preview(p.get("answer")),
        })
    return [
        table(
            "待融合答案",
            [
                {"key": "kb", "label": "来源"},
                {"key": "source", "label": "管线"},
                {"key": "answer", "label": "答案预览"},
            ],
            rows,
            preview_rows=5,
        ),
    ]


__all__ = [
    "ReasoningContext",
    "REASONING_IO_ENABLED",
    "IO_MAX_BYTES",
    "text",
    "items",
    "kv",
    "table",
    "sanitize_item",
    "sanitize_step_io",
    "build_step_io",
    "answer_output",
    "fusion_input",
]
