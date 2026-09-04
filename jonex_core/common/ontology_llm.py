"""
本体问答 LLM 客户端：根据图谱事实回答查询，不足时返回 INSUFFICIENT。
"""

import json
import logging
import os
from typing import Any, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _scene(base: str, fast_mode: bool) -> str:
    """[jonex] 查询期思考分档：fast_mode 时派生 `<base>_fast` 变体 scene。

    网关据 LLMGW_DISABLE_THINKING_SCENES 白名单对 `_fast` 变体注入
    thinking={"type":"disabled"}（见 llm_gateway/upstream.py::_maybe_disable_thinking）。
    语义是「这次调用走快档」——精档与非严格模式使用原始 scene 名，天然保留思考。
    计量维度随之可分：`_fast` 占比 = 被快档解决的查询比例。
    """
    return f"{base}_fast" if fast_mode else base


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=os.getenv(
                "ONTOLOGY_LLM_BINDING_HOST",
                "http://llm-gateway:8787/v1",
            ),
            api_key=os.getenv("ONTOLOGY_LLM_BINDING_API_KEY", ""),
        )
    return _client


async def arbitrate_answers(
    query: str,
    ontology_answer: str,
    ontology_evidence: str,
    rag_answer: str,
    rag_evidence: str,
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict:
    """[jonex] S1+S7 双向校验裁决（改动 48）。

    对称呈现两侧答案与证据，输出严格 JSON
    ``{verdict, confidence, reason, answer}``：
    - verdict: "ontology" / "rag"（采纳哪一侧）
    - answer: 采纳的答案文本（可做小幅订正）
    - confidence: 0~1
    - reason: 裁决理由（分歧必须可见）

    裁决偏向按**证据类型**（结构化属性 > 自由文本 > 摘要；带锚点 > 无锚点），
    不按来源类型。解析失败抛异常，由调用方回退原答案（绝不因裁决故障丢答案）。
    计量场景头 ``ontology_arbitration``。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You are an answer arbitration expert. Compare two candidate answers "
        "to the same question and decide which one to adopt.\n"
        "Evidence hierarchy (most reliable first): structured attributes "
        "(key=value fields) > free-text descriptions > table summaries; "
        "evidence with location anchors (row/page/timestamp) beats evidence "
        "without. Prefer the answer backed by stronger evidence type, NOT by "
        "its source.\n"
        "If both sides agree, adopt the more specific one. If they conflict, "
        "pick the side with stronger evidence. Never fabricate facts; you may "
        "only choose between the two candidates (optionally trimming obviously "
        "wrong fragments).\n"
        "Output strict JSON: {\"verdict\": \"ontology\"|\"rag\", "
        "\"confidence\": 0.0-1.0, \"reason\": \"short reason\", "
        "\"answer\": \"adopted answer text\"}"
    )
    user_prompt = json.dumps(
        {
            "query": query,
            "ontology_answer": ontology_answer,
            "ontology_evidence": ontology_evidence[:3000],
            "rag_answer": rag_answer,
            "rag_evidence": rag_evidence[:3000],
        },
        ensure_ascii=False,
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": "ontology_arbitration",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id
    import uuid

    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"arbitration:{uuid.uuid4().hex}"

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    raw = resp.choices[0].message.content or ""
    result = json.loads(raw)
    result.setdefault("verdict", "ontology")
    result.setdefault("answer", ontology_answer)
    result.setdefault("reason", "")
    result.setdefault("confidence", 0.5)
    return result


async def answer_from_facts(
    query: str,
    hits: list[dict],
    facts: list[dict],
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    allow_common_sense: bool = False,
    fast_mode: bool = False,
) -> str:
    """根据本体实体 + 邻居事实回答，不足返回 "INSUFFICIENT"。

    Args:
        allow_common_sense: [jonex] P2-7 放宽常识边界。
            为 True 时允许模型在已取到确定领域事实的基础上，叠加通用常识/数学
            （物理常量、semver 语义、单位换算），但必须标注假设。
        fast_mode: [jonex] 查询期思考分档（docs/ontology-query-thinking-latency-fix-plan.md §3）。
            为 True 时 scene 派生 `ontology_qa_fast`，由网关白名单注入禁思考
            （严格模式快档）；False 保留原 scene 与思考（精档/非严格模式）。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You are a knowledge graph query assistant. Answer based solely on the "
        "provided facts. If the facts do not contain enough information to answer "
        'the question, respond with exactly "INSUFFICIENT". Do not make up information. '
        "Facts may include multi-hop relationships. Each fact has a \"hop\" field (1 = direct "
        "neighbor, 2+ = indirect via intermediate entities), a \"path\" listing the entity "
        "chain, and a \"target_entity\" object with the target entity's full content "
        "(description, attributes, aliases). The same target entity may appear more than "
        "once via different relation paths; treat them as distinct evidence. Prefer "
        "lower-hop facts; use higher-hop facts only as supporting context."
    )
    # [jonex] P2-7 放宽常识边界：允许叠加通用常识/数学，但必须标注假设
    if allow_common_sense:
        common_sense_append = os.getenv("ONTOLOGY_ANSWER_COMMON_SENSE_APPEND", "")
        if not common_sense_append:
            common_sense_append = (
                "\n\nIf the question can be answered by applying well-known physical constants "
                "(e.g. specific heat of water ≈ 4186 J/(kg·K), boiling point of water ≈ 100°C), "
                "basic math, or standard semantics (e.g. semver version ordering), you may do so "
                "on top of the provided domain facts. HOWEVER, you MUST: "
                "1) explicitly state the assumption (e.g. \"Assuming c≈4186 J/(kg·K)\"), "
                "2) never fabricate product parameters, hardware specs, or domain facts, "
                "3) clearly separate which parts of the answer come from provided facts "
                "and which come from general knowledge."
            )
        system_prompt += common_sense_append

    facts_text = json.dumps(
        {"entities": hits, "relations": facts}, ensure_ascii=False,
    )

    # 注入计量上下文头
    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": _scene("ontology_qa", fast_mode),
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    # 链路追踪 ID：本次逻辑问答生成一次，SDK 内部重试会复用同一头 + 同一 body，
    # 网关据此 + body 哈希派生稳定的计量幂等键，实现重试去重。
    # 不再注入 X-Jonex-Request-Id（避免每次随机值导致重试被重复计量）。
    import uuid
    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"ontology_qa:{uuid.uuid4().hex}"

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Facts:\n{facts_text}\n\nQuestion: {query}",
                },
            ],
            temperature=0.1,
            max_tokens=8192,
            extra_headers=extra_headers,
        )
        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason
        content = (msg.content or "").strip()

        if not content:
            # [jonex] 思考模式下正文可能落在 reasoning_content、content 为空
            # （上游 tokenhub 行为，Reference/Rag-anything 侧 _metered_llm() 已同源修复过一次）。
            # ⚠️ 仅在**正常结束**时兜底：finish_reason=="length" 说明思考链自身被截断，
            #    此时 reasoning_content 是不完整的思考文本而非答案，且直接返回会泄露思考链。
            if finish == "length":
                logger.warning(
                    "本体 LLM 输出预算耗尽（finish_reason=length）且正文为空——"
                    "非事实不足，而是 max_tokens 被思考 token 吃光；返回 INSUFFICIENT 交由升档重跑"
                )
                return "INSUFFICIENT"
            content = (getattr(msg, "reasoning_content", None) or "").strip()
            if content:
                logger.info("本体 LLM content 为空，已用 reasoning_content 兜底（finish_reason=%s）", finish)

        if not content:
            logger.warning("本体 LLM 返回空内容 finish_reason=%s（兜底后仍为空）", finish)
            return "INSUFFICIENT"
        return content
    except Exception as e:
        logger.warning("本体 LLM 调用失败: %s", e)
        return "INSUFFICIENT"


async def answer_from_chunks(
    query: str,
    chunks: list[dict],
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    fast_mode: bool = False,
) -> str:
    """[jonex] 方案 A：基于召回 chunk 原文做一次平台侧作答（场景 rag_chunk_qa）。

    与 answer_from_facts 的关键差异：
    - 送进 prompt 的 chunk 就是答案的证据来源（答案与引用同源）；
    - LLM 异常**向上抛出**，由调用方（_rag_fallback_multi 新路径）回退旧链路，
      不吞掉异常返回占位文本（避免假答案）。

    Args:
        chunks: 排序/截断后的 chunk 列表，每项含
            doc_id / file_name / kb_id / chunk_id / text / relevance，
            以及 [jonex] 表格方案改动 64 的 ctype / row_start / row_end。
            ⚠️ 检索侧 refs 由 parse_file_source 产出，类型键名是 chunk_type
            （非 ctype）——读取时两键兼容；全量 reparse 前存量 chunk 无类型
            字段，prompt 无类型标签属预期。
        kb_id: 多 KB 场景取主 KB（kb_ids[0]），与 _rerank_references 口径一致。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    # token 预算硬截：按分数从高到低丢 chunk，直至总长 ≤ 上限
    max_chars = int(os.getenv("RAG_ANSWER_MAX_CONTEXT_CHARS", "12000"))
    budgeted: list[dict] = []
    total = 0
    for c in chunks:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        if total + len(text) > max_chars:
            break
        budgeted.append(c)
        total += len(text)

    system_prompt = (
        "You are a retrieval-augmented QA assistant. Answer the question based "
        "solely on the provided reference chunks. Each chunk is prefixed with a "
        "[ref_i] marker; cite the markers you rely on (e.g. \"[ref_1][ref_3]\") at "
        "the end of the corresponding sentences. Do not make up information that is "
        "not in the chunks. If the chunks are insufficient, say so explicitly and "
        "list what is missing. Chunks may include table rows (labeled 表格明细) and "
        "table overviews (labeled 表格概览) — for precise value lookups prefer 表格明细; "
        "for structural summaries prefer 表格概览. "
        "Chunks labeled 图片描述 are visual descriptions of images in the document — "
        "they describe what appears in a picture, not the document's textual content. "
        "Do not treat image descriptions as factual statements from the document; "
        "only use them when the user is explicitly asking about visual content."
    )

    lines: list[str] = []
    for i, c in enumerate(budgeted, 1):
        fname = c.get("file_name") or c.get("doc_id") or ""
        # 检索侧 chunk 的类型键是 chunk_type（parse_file_source 产物）；
        # ctype 仅为历史/其他调用方兜底（见 answer_from_chunks docstring）。
        ctype = c.get("ctype") or c.get("chunk_type")
        label = ""
        if ctype == "table_row":
            rs, re_ = c.get("row_start"), c.get("row_end")
            label = f"表格明细（行 {rs}-{re_}）" if rs is not None and re_ is not None else "表格明细"
        elif ctype == "table_summary":
            label = "表格概览"
        elif ctype == "image":
            label = "图片描述"
        header = f"[ref_{i}] {fname}" + (f"（{label}）" if label else "")
        lines.append(f"{header}\n{c.get('text', '')}")

    # 注入计量上下文头（新场景 rag_chunk_qa，与 ontology_qa / rag_fusion 分离统计）
    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": _scene("rag_chunk_qa", fast_mode),
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    # 链路追踪 ID：与 answer_from_facts / fuse_rag_answers 同一幂等去重机制
    import uuid
    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"rag_chunk_qa:{uuid.uuid4().hex}"

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Reference chunks:\n\n" + "\n\n".join(lines)
                           + f"\n\nQuestion: {query}",
            },
        ],
        temperature=0.1,
        max_tokens=8192,
        extra_headers=extra_headers,
    )
    msg = resp.choices[0].message
    finish = resp.choices[0].finish_reason
    content = (msg.content or "").strip()
    if not content:
        # [jonex] 与 answer_from_facts 同源的 reasoning_content 兜底：
        # finish_reason=="length" 时思考链自身被截断（不完整思考文本而非答案，
        # 直接返回会泄露思考链）→ 保持「抛异常回退旧链路」的既有语义。
        if finish != "length":
            content = (getattr(msg, "reasoning_content", None) or "").strip()
            if content:
                logger.info("answer_from_chunks content 为空，已用 reasoning_content 兜底（finish_reason=%s）", finish)
    if not content:
        # 空答案视为失败 → 抛出，由调用方回退旧链路（不返回空答案）
        raise RuntimeError(
            f"answer_from_chunks 返回空内容 finish_reason={finish}"
        )
    return content


async def fuse_rag_answers(
    query: str,
    per_kb_answers: list[dict],
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    fast_mode: bool = False,
) -> str:
    """将多个 KB 的 RAG 答案融合为一个准确、无重复、标注来源的统一回答。

    Args:
        query: 原始查询
        per_kb_answers: [{"kb_id": ..., "answer": ...}, ...] 至少 2 条

    Returns:
        融合后的回答字符串
    """
    if len(per_kb_answers) < 2:
        return per_kb_answers[0]["answer"] if per_kb_answers else ""

    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You are a RAG answer fusion assistant. Given multiple answers from different "
        "knowledge bases for the same question, produce a single, accurate, non-redundant "
        "answer. Resolve factual conflicts. When noting which knowledge base a piece of "
        "information comes from, you MUST ONLY use the exact knowledge base names provided "
        "in the input — a name may list multiple knowledge bases separated by a Chinese "
        "enumeration comma '、', in which case attribute the information to that group "
        "rather than picking one of them. If you cannot attribute a piece of information "
        "confidently, do not label it. The placeholder \"unspecified\" means the source "
        "knowledge bases could not be located — never print \"unspecified\" in your "
        "answer and do not label such information. Never invent knowledge base names. "
        "Do not make up information."
    )

    import uuid

    # [jonex] 只把「知识库显示名 + 答案」喂给 LLM——此前 json.dumps(per_kb) 会把
    # kb_id（UUID 不可读）和内部 source 字段（如 "llm-wiki"）直接暴露给 LLM，
    # 产出「知识库 `llm-wiki`」「知识库 `470a9519...`」这类错误标注。
    # 标签为空（该侧引用未定位到 KB）时落 unspecified，prompt 已指示不输出该词。
    def _kb_label(a: dict) -> str:
        return str(a.get("kb_name") or "unspecified")

    kb_texts = "\n\n".join(
        f"[Knowledge base: {_kb_label(a)}]\n{a.get('answer', '')}"
        for a in per_kb_answers
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": _scene("rag_fusion", fast_mode),
    }
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id
    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"rag_fusion:{uuid.uuid4().hex}"

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Query: {query}\n\n"
                        f"Answers from different knowledge bases:\n{kb_texts}\n\n"
                        "Please produce a single fused answer."
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=4096,
            extra_headers=extra_headers,
        )
        content = resp.choices[0].message.content
        return (content or "").strip() or "\n\n---\n\n".join(
            [a["answer"] for a in per_kb_answers]
        )
    except Exception as e:
        logger.warning("RAG 答案融合 LLM 调用失败: %s", e)
        # 降级：直接拼接各 KB 答案
        parts = [a["answer"] for a in per_kb_answers]
        return "\n\n---\n\n".join(parts)