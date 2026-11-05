"""
本体问答 LLM 客户端：根据图谱事实回答查询，不足时返回 INSUFFICIENT。
"""

import hashlib
import json
import logging
import os
import re
from typing import Any, Optional

from openai import AsyncOpenAI

from jonex_core.common.config import config
from jonex_core.common.model_config import resolve_llm_model

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


# ── 引用标记归一化（答案内联 [n] 引用） ────────────────────────────

_REF_MARKER_RE = re.compile(r"\[(ref[-_]?)?(\d+)\]")

_REFERENCES_HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:References|参考文献|原文引用|引用来源)\s*[:：]?\s*$",
    re.IGNORECASE,
)

# [jonex] 引用范围压缩：相邻引用编号里，连续长度 ≥ 该值的合并为「起始-结束」范围展示。
_RANGE_MIN_RUN = 3

# LLM 生成的引用范围连接符（[a]–[b]）：en dash / em dash / 连字符 / 波浪线（半角 + 全角）
# [jonex] 补充中文范围词「至」「到」——中文答案里 LLM 常用「[ref_5] 至 [ref_5]」
# 表达范围，漏掉会导致 [5] 至 [5] 无法压缩成 [5]、[1] 至 [4] 无法压缩成 [1-4]。
_RANGE_RE = re.compile(r"\[(\d+)\]\s*[-–—~～至到]\s*\[(\d+)\]")

# 相邻引用序列（两个及以上 [n] 相邻，中间仅空白）
_ADJACENT_RE = re.compile(r"\[\d+\](?:\s*\[\d+\])+")


def _norm_range(m: re.Match) -> str:
    """把 LLM 生成的 [a]–[b] 规范化为 [a-b]（升序；退化时 [a]）。"""
    a = int(m.group(1))
    b = int(m.group(2))
    if a == b:
        return f"[{a}]"
    lo, hi = (a, b) if a < b else (b, a)
    return f"[{lo}-{hi}]"


def _compress_adjacent(m: re.Match) -> str:
    """把相邻引用序列 [1][2][3][4][6][8] 压缩为 [1-4][6][8]。

    相邻去重后，连续长度 ≥ _RANGE_MIN_RUN 的编号合并为 [起始-结束]，
    不足的保持单个 [n]。
    """
    nums = [int(x) for x in re.findall(r"\[(\d+)\]", m.group(0))]
    deduped: list[int] = []
    for n in nums:
        if not deduped or deduped[-1] != n:
            deduped.append(n)
    out: list[str] = []
    i = 0
    while i < len(deduped):
        j = i
        while j + 1 < len(deduped) and deduped[j + 1] == deduped[j] + 1:
            j += 1
        if j - i + 1 >= _RANGE_MIN_RUN:
            out.append(f"[{deduped[i]}-{deduped[j]}]")
        else:
            out.extend(f"[{x}]" for x in deduped[i:j + 1])
        i = j + 1
    return "".join(out)


# 单个引用标记 [n] 或范围 [n-m]
_CITE_RE = re.compile(r"\[(\d+)(?:-(\d+))?\]")


def _dedup_global_citations(text: str) -> str:
    """全局去重：每个引用编号在答案里只保留首次出现。

    normalize_citations 按 doc_id 归并后，同一文档被多处引用会产出多个同号角标
    （如「[1] … [1]」或「[1-4] … [3]」）。紧邻去重只覆盖相邻重复，本函数按
    全文首次出现去重：单个 [n] 重复时删除后续整段；范围 [n-m] 里的编号逐个
    登记，已出现过的编号从范围中剔除（连续段按 _RANGE_MIN_RUN 重合并），
    剔空则删除整段。
    """
    seen: set[int] = set()
    out: list[str] = []
    pos = 0
    for m in _CITE_RE.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) is not None else a
        keep = [x for x in range(a, b + 1) if x not in seen]
        if not keep:
            continue
        for x in keep:
            seen.add(x)
        rebuilt: list[str] = []
        i = 0
        while i < len(keep):
            j = i
            while j + 1 < len(keep) and keep[j + 1] == keep[j] + 1:
                j += 1
            if j - i + 1 >= _RANGE_MIN_RUN:
                rebuilt.append(f"[{keep[i]}-{keep[j]}]")
            else:
                rebuilt.extend(f"[{x}]" for x in keep[i:j + 1])
            i = j + 1
        out.append("".join(rebuilt))
    out.append(text[pos:])
    return "".join(out)


def normalize_citations(
    answer: str,
    number_to_doc_id: dict[str, str],
    final_references: list[dict],
) -> str:
    """把答案内的引用标记（`[ref_1]` / `[1]`）重排为最终 references 下标。

    Args:
        answer: 含引用标记的答案文本。
        number_to_doc_id: 标记数字字符串（"1"/"2"…）→ doc_id 的映射。
            路径②（answer_from_chunks）的数字 = `[ref_i]` 中的 i；
            路径①（LightRAG）的数字 = 答案内联 `[n]` 的 n。
        final_references: `_build_references` 产出的聚合引用列表，顺序即卡片顺序。

    规则：doc_id 在 final_references 首次出现的位置 = 编号（1-based）；同一 doc
    去重；`[ref_N]` 形态映射不到时丢弃（防幻觉/越权），纯 `[N]` 形态映射不到时
    保留原样（可能是年份/列表等普通文本，避免误删）。
    """
    if not answer or not number_to_doc_id:
        return answer

    doc_to_idx: dict[str, int] = {}
    for idx, ref in enumerate(final_references):
        did = ref.get("doc_id")
        if did and did not in doc_to_idx:
            doc_to_idx[did] = idx + 1
    if not doc_to_idx:
        return answer

    def _repl(m) -> str:
        is_ref = m.group(1) is not None  # [ref_N] 形态
        did = number_to_doc_id.get(m.group(2))
        idx = doc_to_idx.get(did) if did else None
        if idx is not None:
            return f"[{idx}]"
        return "" if is_ref else m.group(0)

    normalized = _REF_MARKER_RE.sub(_repl, answer)
    # [jonex] 引用范围压缩：
    #   1. LLM 生成的 [a]–[b] 规范化为 [a-b]（见 _norm_range）；
    #   2. 相邻连续编号 [1][2][3][4][6][8] 压缩为 [1-4][6][8]（见 _compress_adjacent）；
    #   3. 相邻重复兜底去重 [1][1] → [1]；
    #   4. 全文级去重：同文档多处引用归并出的同号角标只保留首次出现（见
    #      _dedup_global_citations）。
    # 压缩后前端把 [n] / [n-m] 渲染为圆形角标，连续引用不再逐个罗列。
    normalized = _RANGE_RE.sub(_norm_range, normalized)
    normalized = _ADJACENT_RE.sub(_compress_adjacent, normalized)
    normalized = re.sub(r"(\[\d+\])\s*(?:\1\s*)+", r"\1", normalized)
    normalized = _dedup_global_citations(normalized)
    return normalized


_EVIDENCE_HANDLE_RE = re.compile(r"\[E([1-9][0-9]*)\]")


def normalize_evidence_handles(
    answer: str,
    handle_to_doc_id: dict[str, str],
    final_references: list[dict],
) -> str:
    """把 jonex_wiki 引擎答案里的 [E#] 证据 handle 改写为最终 references 下标 [n]。

    与 ``normalize_citations`` 同构，但输入标记是引擎 synthesis 阶段的证据短句柄
    （``[E1]``/``[E2]``，见 Reference/JonexWiki synthesis.py）。``handle_to_doc_id``
    是 ``"[E1]"`` → ``document_id`` 的映射，由引擎 citations 的 ``handle`` +
    ``document_id`` 字段构建；``final_references`` 顺序即卡片顺序。

    规则：``[E#]`` 经 handle → document_id → 卡片下标两级映射到 ``[n]``；映射不到
    （handle 缺失 / document_id 不在卡片里，如表格句柄）时保留原样，避免误删正文。
    """
    if not answer or not handle_to_doc_id:
        return answer

    doc_to_idx: dict[str, int] = {}
    for idx, ref in enumerate(final_references):
        did = ref.get("doc_id")
        if did and did not in doc_to_idx:
            doc_to_idx[did] = idx + 1
    if not doc_to_idx:
        return answer

    def _repl(m: re.Match) -> str:
        handle = f"[E{m.group(1)}]"
        did = handle_to_doc_id.get(handle)
        idx = doc_to_idx.get(did) if did else None
        return f"[{idx}]" if idx is not None else m.group(0)

    normalized = _EVIDENCE_HANDLE_RE.sub(_repl, answer)
    # 复用范围压缩（与 normalize_citations 一致）
    normalized = _RANGE_RE.sub(_norm_range, normalized)
    normalized = _ADJACENT_RE.sub(_compress_adjacent, normalized)
    normalized = re.sub(r"(\[\d+\])\s*(?:\1\s*)+", r"\1", normalized)
    return normalized


def strip_trailing_references(answer: str) -> str:
    """剥掉答案末尾的「References / 参考文献」引用尾注段（LightRAG 原生答案产物）。

    引用已由 references 卡片单独承载，尾注段与卡片重复，需从答案文本移除，避免
    前端把整段当作正文渲染。仅剥尾部：命中首个 References 标题后，其后（含标题）
    整段截断。
    """
    if not answer:
        return answer
    lines = answer.split("\n")
    for i, line in enumerate(lines):
        if _REFERENCES_HEADING_RE.match(line):
            return "\n".join(lines[:i]).rstrip()
    return answer


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
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

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


# ── 送 LLM 的 payload 瘦身（引用锚点不进 prompt） ──────────────────

# 只用于**回指原文**的字段（`_build_references` / `_facts_number_to_doc_id` 消费），
# 对作答没有信息量。实测它们能占 payload 的 95%——见 `answer_from_facts` 的注释。
_ANCHOR_FIELDS = ("source_chunks", "relation_source_chunks")


def _lean_entity(ent: dict) -> dict:
    """实体的 LLM 视图：浅拷贝并剔除锚点字段。

    ⚠️ 必须**新建 dict**——就地 `del` 会把调用方的 `facts` / `hits` 改坏。
    """
    if not isinstance(ent, dict):
        return ent
    return {k: v for k, v in ent.items() if k not in _ANCHOR_FIELDS}


def _lean_fact(f: dict) -> dict:
    """fact 的 LLM 视图：剔除锚点，并对其 `target_entity` 递归剔除。"""
    if not isinstance(f, dict):
        return f
    out = {k: v for k, v in f.items() if k not in _ANCHOR_FIELDS}
    te = out.get("target_entity")
    if isinstance(te, dict):
        out["target_entity"] = _lean_entity(te)
    return out


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
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

    system_prompt = (
        "You are a knowledge graph query assistant. Answer based solely on the "
        "provided facts. If the facts do not contain enough information to answer "
        'the question, respond with exactly "INSUFFICIENT". Do not make up information. '
        "Facts may include multi-hop relationships. Each fact has a \"hop\" field (1 = direct "
        "neighbor, 2+ = indirect via intermediate entities), a \"path\" listing the entity "
        "chain, and a \"target_entity\" object with the target entity's full content "
        "(description, attributes, aliases). The same target entity may appear more than "
        "once via different relation paths; treat them as distinct evidence. Prefer "
        "lower-hop facts; use higher-hop facts only as supporting context. "
        'Each relation fact carries a "_no" field (its 1-based reference number). '
        'When your answer relies on a specific fact, cite it inline using [ref_i] markers '
        '(e.g. "[ref_1][ref_3]") at the end of the corresponding sentence. Cite only '
        "the facts you actually use; never cite a number you did not rely on."
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

    # [jonex] 本体引用编号：给每个 relation 分配 1-based `_no` 编号，LLM 以
    # [ref_i] 引用；调用方按相同索引提取 doc_id 做 normalize_citations。
    #
    # ⚠️ 送 LLM 的 payload 必须**剔除引用锚点**（`source_chunks`）。线上实测
    # （2026-09-24，火炬库「军用多层瓷介电容器的产品特性是什么」）：命中 18 个实体
    # → 邻域并集 **97 条 fact**，而 `target_entity.source_chunks` 是每个实体命中的
    # **几百条 `{source_id, file_path}`**（单个实体 318 条 / 85,320 字符，且因该实体
    # 在多条 fact 里重复而**被塞了 7 遍**）——占 payload 的 **95%**：
    #
    #     relations 原样（含锚点）= 1,616,040 字符 → 剔除锚点 = 74,437 字符（−95%）
    #
    # 对应网关侧 `prompt=896488` token、上游生成 **40 秒** → 平台
    # `ONTOLOGY_ANSWER_TIMEOUT`（30s）判超时失败 → SDK 重试再来 **21 秒**，大部分
    # 是白跑。锚点对作答没有信息量：system prompt 只声明模型需要 `hop` / `path` /
    # `target_entity` 的 `description, attributes, aliases`；而 doc_id 是**平台侧**
    # 从 `facts` 取的（`_facts_number_to_doc_id`），不依赖模型看见锚点。
    #
    # ⚠️ 用**浅拷贝重建**，不得就地删字段 —— `facts` / `hits` 之后还要被
    # `_facts_number_to_doc_id` / `_cited_doc_ids` / 引用构建读取。
    numbered_relations = [
        {**_lean_fact(f), "_no": i} for i, f in enumerate(facts, 1)
    ]
    facts_text = json.dumps(
        {"entities": [_lean_entity(e) for e in hits],
         "relations": numbered_relations},
        ensure_ascii=False,
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
            # [jonex] 四屏修复 (a)③：0.1→0。两侧答案生成非确定时（temp=0.1 微漂），
            # 裁决输入漂移 → 同 query 裁决结论在 rag/ontology 间反复翻转。
            # 答案温度归零消除输入漂移，裁决（arbitrate_answers 本已 temp=0）收敛。
            temperature=0,
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


async def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    *,
    tool_choice: str = "auto",
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    max_tokens: int = 4096,
):
    """本体查询工具化的单轮调用（scene `ontology_qa_tools`），返回 `(message, finish_reason)`。

    与 `answer_from_facts` 的差别：**异常向上抛**，不吞成 INSUFFICIENT —— 调用方
    （工具循环）据此回落正则路由。scene 要登记进网关 `LLMGW_DISABLE_THINKING_SCENES`：
    工具选择轮不需要思考，思考 token 还会挤占 max_tokens。
    """
    import uuid

    client = _get_client()
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")
    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": "ontology_qa_tools",
        # 同一次查询的多轮共用 trace id：每轮 body 不同，网关的幂等键不会撞
        "X-Jonex-Trace-Id": trace_id or f"ontology_qa_tools:{uuid.uuid4().hex}",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
        temperature=0,
        max_tokens=max_tokens,
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    return choice.message, choice.finish_reason


async def answer_from_chunks(
    query: str,
    chunks: list[dict],
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    fast_mode: bool = False,
) -> tuple[str, list[dict]]:
    """[jonex] 方案 A：基于召回 chunk 原文做一次平台侧作答（场景 rag_chunk_qa）。

    与 answer_from_facts 的关键差异：
    - 送进 prompt 的 chunk 就是答案的证据来源（答案与引用同源）；
    - LLM 异常**向上抛出**，由调用方（_rag_fallback_multi）转为
      `ServiceUnavailableError` 上报，不吞掉异常返回占位文本（避免假答案）。

    Args:
        chunks: 排序/截断后的 chunk 列表，每项含
            doc_id / file_name / kb_id / chunk_id / text / relevance，
            以及 [jonex] 表格方案改动 64 的 ctype / row_start / row_end。
            ⚠️ 检索侧 refs 由 parse_file_source 产出，类型键名是 chunk_type
            （非 ctype）——读取时两键兼容；全量 reparse 前存量 chunk 无类型
            字段，prompt 无类型标签属预期。
        kb_id: 多 KB 场景取主 KB（kb_ids[0]），与检索侧引用重排口径一致。

    Returns:
        (content, budgeted): content 为答案文本（含 `[ref_i]` 内联标记）；budgeted
        为实际送入 prompt 的 chunk 列表（与 `[ref_i]` 一一对应，供调用方做引用
        编号归一化，避免重复复刻预算截断逻辑）。
    """
    client = _get_client()
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

    # [jonex] S4（§6「截断单点化」）：预算截断的唯一权威点是检索侧
    # `_rag_platform_answer` 第 4 步——它已按同一个 `RAG_ANSWER_MAX_CONTEXT_CHARS`
    # 装好箱再把 `prompt_refs` 递进来。本函数**不再做第二道独立截断**，这里的
    # 循环降级为「断言 + 兜底」：口径若被改漂（调用方换了预算、或有人直接调本
    # 函数），也不静默丢证据，而是留痕并保持与检索侧一致的贪心语义。
    #
    # 为什么原实现（`if total + len(text) > max_chars: break`）是错的：`break`
    # 让一个超长 chunk 把**其后全部**候选一并挡在 prompt 外。候选按分数降序，
    # 低分大 chunk 排在前面时，后面所有高分小 chunk 都进不来——检索侧改成贪心
    # 装箱后两边口径也会不一致。`continue` 则只跳过装不下的那一条。
    max_chars = int(os.getenv("RAG_ANSWER_MAX_CONTEXT_CHARS", "12000"))
    budgeted: list[dict] = []
    total = 0
    oversized: list[dict] = []
    for c in chunks:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        if total + len(text) > max_chars:
            oversized.append(c)
            continue
        budgeted.append(c)
        total += len(text)
    if oversized:
        logger.warning(
            "[answer_from_chunks] 收到 %d 条候选、预算 %d 字符装下 %d 条，"
            "跳过 %d 条超长项（首个 %d 字符）——正常情况下调用方应已按同一预算"
            "装箱，出现本告警说明两处口径漂移，请核对 RAG_ANSWER_MAX_CONTEXT_CHARS",
            len(chunks), max_chars, len(budgeted), len(oversized),
            len((oversized[0].get("text") or "")),
        )

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
        "only use them when the user is explicitly asking about visual content. "
        "Chunks labeled 图表数据 are numbers read off charts and figures; they ARE "
        "document content (a datasheet's curve is part of the spec), so use them to "
        "answer — but they are estimated from the axes, not measured. Trust the shape, "
        "trend and order of magnitude; do not quote an exact figure as precise, and say "
        "the value is read from a chart when the precision matters. "
        "Output format: use a GitHub-flavored Markdown table whenever (a) the user "
        "explicitly asks for a table, (b) the answer is mainly cell values taken from "
        "chunks labeled 表格明细 or 表格概览, or (c) the question compares two or more "
        "objects. Table shape: the first column is the compared dimension, each "
        "remaining column is one object named exactly as the chunks name it. Emit the "
        "'| --- | --- |' separator row. Put the [ref_i] markers you rely on at the end "
        "of the cell they support. Leave a cell empty when the chunks have no value for "
        "it and say after the table which cells are uncovered; never fill a cell from a "
        "neighbouring cell. Keep prose outside the table to at most three sentences. "
        "For any other question answer in prose and do not force a table."
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
        # [jonex] `chart` **不借**「图片描述」这个标签：那条规则说「只在用户明确问视觉
        # 内容时才用」——对图表明明太严（数据手册的曲线就是答案本身，用户问「105℃ 时
        # 容值变化多少」正是在问图里的数）。它需要的是另一条：数值是**估读**的。
        elif ctype == "chart":
            label = "图表数据"
        # [jonex] 视频内容的三类来源（见 stages.py 的 _SCENE_SOURCE_CTYPE）——
        # 让作答时能区分引用来自语义章节 / 幻灯片 OCR 原文 / 语音转写。
        elif ctype == "video_chapter":
            label = "视频章节"
        elif ctype == "video_slide":
            label = "幻灯片"
        elif ctype == "audio_segment":
            label = "语音转写"
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
        # [jonex] 四屏修复 (a)③：0.1→0，消除裁决两侧答案输入漂移（同 answer_from_facts）。
        temperature=0,
        max_tokens=8192,
        extra_headers=extra_headers,
    )
    msg = resp.choices[0].message
    finish = resp.choices[0].finish_reason
    content = (msg.content or "").strip()
    if not content:
        # [jonex] 与 answer_from_facts 同源的 reasoning_content 兜底：
        # finish_reason=="length" 时思考链自身被截断（不完整思考文本而非答案，
        # 直接返回会泄露思考链）→ 保持「抛异常、不返回占位文本」的既有语义。
        if finish != "length":
            content = (getattr(msg, "reasoning_content", None) or "").strip()
            if content:
                logger.info("answer_from_chunks content 为空，已用 reasoning_content 兜底（finish_reason=%s）", finish)
    if not content:
        # 空答案视为失败 → 抛出，由调用方转为 ServiceUnavailableError（不返回空答案）
        raise RuntimeError(
            f"answer_from_chunks 返回空内容 finish_reason={finish}"
        )
    return content, budgeted


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
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

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
        "Do not make up information. "
        # [jonex] 方案 B：融合前调用方已把单侧答案内的引用编号重写为「最终合并
        # 引用卡片」的下标（见 search_service _fuse 前 normalize_citations）。
        # 此处要求 LLM 原样保留这些 [n] 标记并紧跟其引用的内容——不丢弃、不
        # 重排、不合并；未带编号的答案片段保持不标注即可。
        "Some input answers carry inline citation markers like [1] or [2] that refer "
        "to numbered source cards. Preserve these markers verbatim and keep each marker "
        "attached to the sentence it cites — do not drop, renumber, merge, or move them. "
        "Answers without markers are fine; leave them unmarked. "
        # [jonex] 表格不能在这里被「融合」掉：两侧都给表格时，重排/合并会产出
        # 表头与单元格错位的假表。要求原样保留，冲突则并列并标 KB 名。
        "Markdown tables inside an input answer must be preserved verbatim — keep the "
        "header row, the '| --- |' separator row and every cell in place; never re-parse "
        "cells, merge two answers' tables into one, or summarise a table into prose. "
        "The markers [n] may appear inside table cells and stay there. If two answers "
        "give different values for the same cell, keep both rows/fragments and label "
        "which knowledge base each came from instead of choosing one."
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
            # [jonex] 四屏修复 (a)③：0.1→0，消除裁决两侧答案输入漂移（同 answer_from_facts）。
            temperature=0,
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


async def anchor_answer_to_references(
    answer: str,
    references: list[dict],
    query: str = "",
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> str:
    """把无内联引用标记的答案锚定到引用卡片：LLM 在答案里插入 [n] 标记。

    用于 openkb（llm-wiki）这类「答案无 [n]/[E#] 内联标记」的管线。references 顺序
    即卡片顺序；锚定素材是文档名 + 知识库名（openkb 引用为文档级，无 excerpt）。
    LLM 只标注能确定来源的句子（不确定不标），不改正文措辞、绝不编造来源。
    LLM 失败或无可锚定素材时原样返回 answer（降级不阻塞检索）。
    """
    if not answer or not references:
        return answer

    doc_lines: list[str] = []
    for i, ref in enumerate(references, 1):
        name = str(ref.get("file_name") or ref.get("doc_id") or f"document {i}")
        kb = str(ref.get("kb_name") or "")
        doc_lines.append(f"{i}. {name}" + (f"（{kb}）" if kb else ""))
    if not doc_lines:
        return answer

    import uuid

    client = _get_client()
    model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

    system_prompt = (
        "You are a citation anchoring assistant. Given an answer and a numbered "
        "list of source documents, insert inline citation markers [n] into the "
        "answer at the claims attributable to document n. Only mark claims you "
        "can confidently attribute; leave the rest unmarked. Do not change the "
        "wording of the answer and never invent an attribution. Return only the "
        "answer text with markers, no extra explanation."
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": _scene("citation_anchor", False),
    }
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id
    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"citation_anchor:{uuid.uuid4().hex}"

    user_content = (
        (f"Query: {query}\n\n" if query else "")
        + "Documents:\n"
        + "\n".join(doc_lines)
        + "\n\nAnswer:\n"
        + answer
    )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_tokens=4096,
            extra_headers=extra_headers,
        )
        content = resp.choices[0].message.content
        return (content or "").strip() or answer
    except Exception as e:
        logger.warning("答案引用锚定 LLM 调用失败: %s", e)
        return answer


async def propose_concept_clusters(
    source_entities: list[dict],
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> list[dict]:
    """把各数据源的实体类型提成候选概念簇（决策 4：LLM 提候选，人确认后才生效）。

    Args:
        source_entities: `[{"data_source_id", "source_name", "entity_name",
            "description"}]` —— 调用方已按源铺平。**只传结构，不传任何凭证**：
            这里吃的就是 `ontology_compiled_schemas.entity_types` 里的名称与描述，
            跟数据库密码没有关系。

    Returns:
        `[{"name", "description", "aliases", "sources":[{"data_source_id",
        "entity_name"}]}]`。

        ⚠️ 这里**不保证** sources 里的组合真实存在 —— 模型会把实体名配到一个
        它没见过的 `data_source_id` 上，而且看起来完全正常。落库前必须过调用方
        的 `filter_dangling_sources`。

    Raises:
        RuntimeError: 以下三种情形**都抛**，绝不返回空列表：
            1. 模型返回空正文（`content` 缺失或全是空白）；
            2. JSON 里没有 `concepts` 数组；
            3. `concepts` 是空数组。
            与 `answer_from_chunks` 同口径：**抛**而不是回空列表 —— 调用方要的是
            「生成失败、不落库、不覆盖」，而一个空列表在调用方眼里等于「模型认为
            没有概念」，会静默把已有对齐清空。
        RuntimeError（续）: 输出被 `max_tokens` **截断**（`finish_reason ==
            "length"`）—— 消息里带实体数。它是「输入太大」这一种，与上面三种
            「模型回了个完整的坏答案」不同：实体数在知识库里没有上限，所以大库
            **每次都会撞**，而改造前它表现为 `json.loads` 的 `Unterminated string`
            （调用方只包成 5003「概念聚类失败」），操作员看不出真因。
        json.JSONDecodeError: 正文在**合法 JSON 之前**就断了，但 `finish_reason`
            没说截断（模型自己没写全）。与上面几种不同：那几种是「拿到了一个完整
            的坏答案」，这一种是「根本没拿到完整答案」，所以不兜成 RuntimeError、
            直接透出解码错误 —— 调用方不必区分，但排查时这个异常名是「答案不完整」
            的信号（预算见下方 `max_tokens` 注释，已按实体数放大）。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You are an ontology alignment assistant. You are given entity types "
        "extracted from several different data sources of one knowledge base. "
        "Group entity types that denote THE SAME real-world concept into one "
        "concept, and output that concept together with all of its source mappings.\n"
        "IMPORTANT: most entity types are NOT related to each other. Only merge "
        "when you are confident they denote the same concept (for example a metric "
        "`EmployeeDefectRate` and a table `DefectRecord` that are both about 不良). "
        "When in doubt, keep them as separate concepts. Never invent entities.\n"
        # 命名风格不算差异 —— 这是最常见的一类假阴性。实测两个库之间唯一的重叠是
        # `Users` 与 `User`，模型把「差一个字母」当成了不同概念（两边的库还都是英文表名、
        # 都没有描述，除名字以外没有别的信号可看）。
        "Naming style alone does NOT make two entity types different: singular vs "
        "plural (`User` / `Users`), `snake_case` vs `PascalCase`, and the same word "
        "with a `Sys` / `Tb` / `T_` prefix denote the SAME concept — merge them when "
        "the underlying thing matches.\n"
        # 没描述的实体由调用方补了一串列名（见 `_source_entities`）—— 那是它们唯一的信号
        "Some entries carry `columns` (a sample of that entity's column names) "
        "instead of a description. Where a description is missing, treat those "
        "column names as the semantic signal: two entities whose columns describe "
        "the same kind of record (e.g. both keyed by a user id and a phone number) "
        "are likely the same concept.\n"
        "Every source mapping MUST reuse the exact `data_source_id` and "
        "`entity_name` strings given in the input — never paraphrase, translate, "
        "or invent them.\n"
        "For each concept give a short `name`, a one-sentence `description` in the "
        "same language as the input descriptions, and `aliases` (alternative terms "
        "a user might type when searching).\n"
        'Output strict JSON: {"concepts": [{"name": str, "description": str, '
        '"aliases": [str], "sources": [{"data_source_id": str, '
        '"entity_name": str}]}]}'
    )
    user_prompt = json.dumps({"entity_types": source_entities}, ensure_ascii=False)

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 这个 scene 恒禁思考（网关 LLMGW_DISABLE_THINKING_SCENES 白名单）：输入可达
        # 17.5k prompt token，思考 token 会先把输出预算吃光、正文落不下来（历史故障：
        # reasoning 6853/8192，正文被截断成非法 JSON）。**改名要同步改白名单**，否则
        # 网关静默不注入，故障表现为「JSON 解析失败/空内容」。
        # 这里不自己塞 `thinking` 字段：那是腾讯 tokenhub 的专有格式，属网关的适配职责。
        "X-Jonex-Scene": "abstract_schema_align",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    import uuid

    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"abstract_schema:{uuid.uuid4().hex}"

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # 与 answer_from_facts / arbitrate_answers 同口径：temp=0 让同一份
        # schema 重生成得到同一份候选（人确认过的东西不该每次变样）
        temperature=0,
        # ⚠️ 本文件里唯一一个**输出规模与输入规模同阶**的调用，预算不能照抄兄弟函数：
        # 它要给喂进去的每个实体类型都落一条 `sources` 映射（prompt 明说了大部分实体
        # 互不相关、应当各占一个概念），所以 token 数 = O(概念数) = O(实体数)，而不是
        # answer_from_facts / answer_from_chunks / fuse_rag_answers /
        # anchor_answer_to_references 那种一段短散文。实测（真实 prompt）：178 个实体
        # → 17531 completion token / 48713 字符正文。8192 会在 JSON 字符串中途截断，
        # `json.loads` 抛 `Unterminated string`，调用方只看到 5003。
        # 另：本 scene 经网关白名单恒禁思考（见下方 X-Jonex-Scene），所以这 32768
        # **全是正文预算**，不会有推理链来抢。
        # 改这里时**不要**顺手动其他几个调用的 max_tokens —— 它们口径不同。
        max_tokens=32768,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    # ⚠️ 截断诊断**必须先于解析**：正文被 `max_tokens` 切断时 `json.loads` 抛的是
    # `Unterminated string`，调用方（generate）只把它包成 5003，与一次网络故障长得
    # 一模一样 —— 操作员看不出「是输入太大」。而实体数没有上限，大库**每次都撞**。
    # 消息开头就是真因、并带上实体数（那是唯一的处置线索），且整条短到不会被
    # 调用方截断（`str(exc)[:200]`）—— 诊断排在前面才切不掉。
    # 用 getattr 取：桩 / 老客户端可能压根没有这个字段，「没填」与「填了别的值」
    # 同义（都不是 length），不该为此抛 AttributeError。
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError(
            f"输出被截断（finish_reason=length）：本次输入实体类型 "
            f"{len(source_entities)} 个，超出单次调用的输出预算（max_tokens=32768），"
            "候选概念只生成了一部分。请减少参与对齐的数据源/实体后重试。"
        )
    raw = (choice.message.content or "").strip()
    if not raw:
        raise RuntimeError("概念聚类返回空内容")
    data = json.loads(raw)
    concepts = data.get("concepts") if isinstance(data, dict) else data
    if not isinstance(concepts, list):
        raise RuntimeError("概念聚类返回的 JSON 里没有 concepts 数组")
    if not concepts:
        # 空数组与「解析失败」同档：调用方看到 [] 只会当成「模型认为没有概念」，
        # 落库就把已有对齐清空了 —— 宁可抛出去让人重生成。
        raise RuntimeError("概念聚类返回的 concepts 是空数组")
    return concepts


async def resolve_focus_entities(
    prompt: str,
    source_entities: list[dict],
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> list[dict]:
    """聚焦提示词 → 种子组（「这次重点看哪些实体」）。

    Args:
        prompt: 用户输入的聚焦提示词（调用方已去空白/限长）。
        source_entities: 同 `propose_concept_clusters` 的铺平输入。

    Returns:
        `[{"concept": str, "members": [{"data_source_id", "entity_name"}]}]`。
        **空数组是合法结果**（模型认为没有相关实体）—— 与
        `propose_concept_clusters` 的空数组口径**相反**：那边空数组意味着
        「答案坏掉」（落库会清空已有对齐）；这边不落库，空是「提示词没命中」，
        调用方（preview）把它转成明确的用户错误。

    Raises:
        RuntimeError: 空正文 / 没有 groups 数组 / 输出被 max_tokens 截断
            （消息里带诊断，同聚类先例）。
        json.JSONDecodeError: 正文在合法 JSON 之前断了且 finish_reason 非 length。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You are an ontology focus assistant. You are given a user's focus "
        "prompt and entity types extracted from several different data sources "
        "of one knowledge base. Select the entity types the focus prompt is "
        "about, and group each selected entity type together with entity types "
        "that denote THE SAME real-world concept elsewhere (another data source, "
        "or another table in the same source under a different naming style).\n"
        "Rules:\n"
        "- Put the entity type that most directly matches the focus prompt FIRST "
        "in its group's members; the remaining members are its same-concept "
        "counterparts. Singular vs plural (`Job` / `Jobs`), `snake_case` vs "
        "`PascalCase`, and `Sys` / `Tb` / `T_` prefixes do NOT make two entity "
        "types different concepts.\n"
        "- Only select entity types clearly related to the focus prompt. When in "
        "doubt, do not select. Never invent entities.\n"
        "- Entity types unrelated to the focus prompt must not be selected at all.\n"
        "- Every member MUST reuse the exact `data_source_id` and `entity_name` "
        "strings given in the input — never paraphrase, translate, or invent "
        "them.\n"
        "- Some entries carry `columns` instead of a description; treat those "
        "column names as the semantic signal.\n"
        'Output strict JSON: {"groups": [{"concept": str, "members": '
        '[{"data_source_id": str, "entity_name": str}]}]}'
    )
    user_prompt = json.dumps(
        {"focus_prompt": prompt, "entity_types": source_entities},
        ensure_ascii=False,
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 恒禁思考（网关 LLMGW_DISABLE_THINKING_SCENES 白名单，与
        # abstract_schema_align 同档）：大输入抽取类调用，思考 token 会先把输出
        # 预算吃光、正文落不下来。**改名要同步改白名单**，否则网关静默不注入，
        # 故障表现为「JSON 解析失败 / 空内容」。
        "X-Jonex-Scene": "abstract_schema_focus",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    import uuid

    extra_headers["X-Jonex-Trace-Id"] = (
        trace_id or f"abstract_schema_focus:{uuid.uuid4().hex}"
    )

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        # 输出规模 = O(选中的种子数)，远小于聚类的 O(全部实体)（那边 32768 下界
        # 是 178 实体 → 17.5k 输出）；8192 对种子清单留足 partner 组余量。
        max_tokens=8192,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    # 截断诊断先于解析（同聚类先例）：截断时 json.loads 抛的是 Unterminated
    # string，调用方只包成 5003，操作员看不出「是输出被切了」。
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError(
            "输出被截断（finish_reason=length）：聚焦解析超出单次调用的输出预算"
            "（max_tokens=8192），种子清单只生成了一部分。请缩小关注范围后重试。"
        )
    raw = (choice.message.content or "").strip()
    if not raw:
        raise RuntimeError("聚焦解析返回空内容")
    data = json.loads(raw)
    groups = data.get("groups") if isinstance(data, dict) else data
    if not isinstance(groups, list):
        raise RuntimeError("聚焦解析返回的 JSON 里没有 groups 数组")
    return groups


async def suggest_attribute_display_names(
    entity_groups: list[dict],
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> list[dict]:
    """批量为抽象属性提中文 display_name（「自动构造」的名字档）。

    Args:
        entity_groups: `[{"entity_name", "entity_display_name", "entity_description",
            "attributes": [{"name", "type", "description"}]}]` —— 只传结构，不传凭证。

    Returns:
        `[{"entity_name", "attribute_name", "display_name"}]`。

        ⚠️ **未校验**：可能含未知实体/属性名、非中文值、超长值 —— 落库前必须过
        调用方的 `apply_name_patches` 收口（那里判「只认既有属性名 + 必须含中文 + 限长」）。

    Raises:
        RuntimeError: 空正文 / 无 `names` 数组 / `names` 空数组（与
            `propose_concept_clusters` 同口径：抛，不静默回空 —— 空列表在调用方
            眼里等于「模型认为都不需要名字」，会静默跳过整档）。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You are an ontology naming assistant. You are given entities of an "
        "abstract schema (merged from several data sources) and their attributes "
        "(column name, type, description). For each attribute, propose a SHORT "
        "Chinese display name (2-8 Chinese characters) that a Chinese-speaking "
        "business user would understand.\n"
        "Rules:\n"
        "- Use the entity name/description and the attribute description as context.\n"
        "- Never invent attributes: only return names for the exact attribute "
        "`name` strings given in the input.\n"
        "- The display_name MUST contain Chinese characters (no pure English).\n"
        "- Keep it idiomatic (e.g. `user_id` -> 用户编号, `created_at` -> 创建时间).\n"
        'Output strict JSON: {"names": [{"entity_name": str, '
        '"attribute_name": str, "display_name": str}]}'
    )
    user_prompt = json.dumps({"entities": entity_groups}, ensure_ascii=False)

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 与 abstract_schema_align 同档恒禁思考（网关 LLMGW_DISABLE_THINKING_SCENES
        # 白名单）：一次性结构化 JSON 输出，输入规模大 —— 漏登记时故障是静默的
        # （思考 token 吃光输出预算 → 截断成非法 JSON），改名要同步改白名单。
        "X-Jonex-Scene": "abstract_schema_attr_names",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    import uuid

    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"abstract_names:{uuid.uuid4().hex}"

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # 与兄弟函数同口径：temp=0，同一份 draft 重跑得到同一批名字
        temperature=0,
        # 批大小 10 实体 × ~15 属性 ≈ 150 个名字 ≈ ~2k 输出 token；8192 留足余量且
        # 与「名字是短词」的事实匹配（不需要 propose_concept_clusters 的 32768）。
        max_tokens=8192,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError(
            f"输出被截断（finish_reason=length）：本批实体 {len(entity_groups)} 个，"
            "超出单次调用的输出预算，请缩小批次后重试。"
        )
    raw = (choice.message.content or "").strip()
    if not raw:
        raise RuntimeError("属性命名返回空内容")
    data = json.loads(raw)
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list):
        raise RuntimeError("属性命名返回的 JSON 里没有 names 数组")
    if not names:
        raise RuntimeError("属性命名返回的 names 是空数组")
    return names


# 规则 4 的**共享词干**：两支只差尾巴一句（单次档「不在给定清单里」/ 多轮档「没给过也没
# resolve 过」）。词干只写一份 —— 两支各写一份的话，改单侧词干时哈希冻结用例（只钉单次档）
# 与多轮差异护栏（只数行数、允许规则 4 不同）**都看不见**，是两条护栏之间的缝。
_RULE_4_STEM = ("4. Quote identifiers the way this dialect needs; never invent "
                "tables or columns that are ")

#: 4c 的**共享文案**：列样本两档都随**列清单**进开局 payload（`pick_samples` 只挑「像枚举」的列；
#: gap1 同族修复，2026-10-02 —— 样本一直在多轮 payload 里、提示词却一度没有这条，「样本只是样本」
#: 是防「照着 3 个样本答『只有这三种』」的那半句，缺了它样本的注入面就带着这个坑）。
_RULE_SAMPLES_4C = (
    "4c. Some columns list `samples` — a few real values observed in that column. Prefer "
    "them when you write filter literals (`WHERE col = …`), but treat them as **examples "
    "only**: the column may hold other values too.\n"
)

#: 4d 的**共享文案**：口径约束现在**两档都在开局 payload 里**（P1-2 续档 gap1 修复，2026-10-02 ——
#: 多轮档「按需取」对口径结构性失效：口径是「模型不知道自己不知道」，码表方向猜不出、不会去问，
#: 试点实证 before==after 3/3、审计 tools=0）。与规则 4 词干同一条理由只写一份：各写一份的话，
#: 单侧漂移串不过护栏（单次档哈希冻结用例看不见多轮支）。
_RULE_CONSTRAINTS_4D = (
    "4d. Some payloads list `constraints` — the library's own conventions for this table "
    "(for example how a status code is meant to be read). When a convention bears on the "
    "question, you MUST follow it.\n"
)

#: 4e（2026-10-13 TBox 落点线）：**仅在 payload 带 `concept_fields` 时渲染**（与
#: 4b/4c/4d「无条件常驻」不同——它描述的是「这一次给了你概念列映射」这一事实本身，
#: 没给就不能说）。语义：问句提到某概念时，从该概念的 columns 里按语义选列，别对
#: 概念下的每一列做同一件事。
_RULE_CONCEPTS_4E = (
    "4e. Some question concepts map to several physical columns — the payload's "
    "`concept_fields` lists them (each column may carry a `role`). When the question "
    "refers to such a concept, pick the column(s) whose meaning matches the "
    "question's intent (e.g. a location named in the question matches the column "
    "whose role is the city) instead of matching every column of the concept.\n"
)

#: 4f（2026-10-25 P1-5「Mapping 对象化」）：**仅当本次注入的 concept_fields 里真带
#: 语义槽时渲染**（`with_mapping_slots`）。文案逐字取自 spec §5.3：槽是映射库约定的
#: 物理实现（过滤谓词 / 聚合 / 粒度），不是可选装饰；问句没明说相反就照用。
_RULE_CONCEPTS_4F = (
    "4f. Some `concept_fields` entries carry `filter`, `aggregation` or `grain` — the\n"
    "library's agreed physical realization of that concept. When the question refers\n"
    "to such a concept, treat a `filter` as a predicate over that table's rows, use\n"
    "the stated `aggregation` for the concept's measure, and honor `grain` as the\n"
    "grouping the value is defined at. These are part of the concept's definition, not\n"
    "optional decoration; apply them unless the question explicitly asks otherwise.\n"
)


#: 4g（2026-10-28 P0-A1 投影契约）：**仅在开关开时**渲染。BIRD 实测 71 条列数不符里
#: 62 条是模型多给了列；这条把「过滤/排序/join 用的列不要进 SELECT」说死。
_RULE_PROJECTION_4G = (
    "4g. SELECT exactly the columns the question asks to output. Columns used only for "
    "filtering, joining, ordering, or computing must stay in WHERE/JOIN/ORDER BY and must "
    "NOT appear in SELECT. For a rate/count/average/max/min/top-k question, return only the "
    "requested scalar (plus the ranking key only if the question asks for a ranking).\n"
)


#: 4h（2026-10-28 P0-A2 evidence/hints）：**仅在这次真给了 hints 时**渲染。
#: BIRD 的 evidence 是官方输入（公式/值映射），不注入模型就会猜公式、猜列。
_RULE_PLAN_4I = (
    "4i. A `plan` key is a planning note. Follow it for joins, derived formulas, "
    "grouping and output cardinality; the question and given schema always take "
    "precedence, and never use tables or columns not listed.\n"
)

_RULE_PREVIEW_4J = (
    "4j. `schema_preview` lists other tables and their column names in this data "
    "source. You may join them when the question needs them; use only listed "
    "tables/columns, and prefer the detailed anchor/related columns when present.\n"
)

#: 4j 明细版（2026-10-30）：预览列带 `{name, type, description}`。
_RULE_PREVIEW_4J_DETAILS = (
    "4j. `schema_preview` lists other tables; each column is an object with "
    "`name`, `type` and optionally `description`. Use the descriptions to choose "
    "the right column when several look similar. You may join these tables when "
    "the question needs them; use only listed tables/columns.\n"
)

#: 4k（P2-4b）：**仅当预览表真带了采样行时**渲染。采样行只作格式/枚举参考，
#: 不是全量数据 —— 不让人把样例值当过滤条件、也不让人据此断言「没有这个值」。
_RULE_PREVIEW_ROWS_4K = (
    "4k. `sample_rows` are a few real rows from the preview tables (cells truncated). "
    "Use them only to understand value formats and common enums; never copy them as "
    "filters, and never conclude that a value absent from the samples does not exist.\n"
)

#: 4l（2026-10-29）：**仅当问题里出现 rank** 时渲染。BIRD 有「Rank …」题要输出
#: 排名列；模型默认只 ORDER BY、不产出 RANK()，官方口径按列比对时必挂。
_RULE_RANK_4L = (
    "4l. The question asks to rank. Include the rank as an output column using "
    "`RANK() OVER (ORDER BY <metric> DESC NULLS LAST)` (on engines without "
    "`NULLS LAST`, sort nulls last explicitly); project only the columns the "
    "question names plus that rank column.\n"
)

#: 4m（2026-10-29）：**仅当问题问「平均每 Y 有多少 X」时**渲染。
#: 「平均数量」是对**每组计数**再平均（COUNT per group → AVG），不是对 0/1 指标求平均。
_RULE_PER_GROUP_AVG_4M = (
    "4m. The question asks for an average count per group (for example, the "
    "average number of X per Y). Write it as `SELECT AVG(cnt) FROM (SELECT "
    "<group key>, COUNT(*) AS cnt FROM <table> WHERE <conditions> GROUP BY "
    "<group key>) t`. Never write `AVG(CASE WHEN ... THEN 1 ELSE 0 END)` for "
    "this shape.\n"
)

#: 4n（2026-10-29）：输出列精确性。问句点名了哪些字段就只投影哪些；
#: 「list all X」但没点名字段时只投影实体的标识列 —— 多选列在官方口径下直接算错。
_RULE_EXACT_FIELDS_4N = (
    "4n. Project exactly the output fields the question asks for. If it names "
    "fields (for example names and addresses), select only those columns and do "
    "not add other columns from the table. If it asks to list rows without naming "
    "fields, project only the identifier column(s) of the requested entity.\n"
)

#: 4o（2026-10-29）：hint 忠实度。evidence/hints 给出公式或值映射时必须原样使用；
#: 不要换成名字相近的列，也不要对计算结果自作主张 ROUND。
_RULE_HINT_EXACTNESS_4O = (
    "4o. Follow the given hints literally when they state a formula, calculation "
    "or value mapping: use that exact expression and those exact values; never "
    "replace them with a similarly named column. Do not apply ROUND to computed "
    "values unless the question explicitly asks for rounding.\n"
)

#: 4p（2026-10-29）：百分比/比例的分母 = 实体总量，不是过滤后的子集。
#: 「among X / of those X」才用过滤后子集作分母。
_RULE_PERCENTAGE_BASE_4P = (
    "4p. For a 'percentage of X' or 'proportion of X' question, the denominator is "
    "the total number of X in X's base table, independent of the question's "
    "filters; compute it with a scalar subquery. Use the filtered subset as the "
    "denominator only when the question says 'among X' or 'of those X'.\n"
)

#: 4q（2026-10-30）：多步/多 join 题的 CTE 提示。**仅当 join 图 ≥2 条且非 sqlserver**。
_RULE_CTE_4Q = (
    "4q. This question needs multiple steps. Use ONE `WITH` clause to compute the "
    "intermediate result (for example per-group counts or a filtered subset), then "
    "a final SELECT over it. Keep it a single statement and do not nest more than "
    "one CTE level.\n"
)

#: 4r（2026-10-30）：除法分母 NULLIF。PG 的整数/数值除零会直接抛错。
_RULE_NULLIF_4R = (
    "4r. For every division or percentage, wrap the denominator with "
    "`NULLIF(<denominator>, 0)` so a zero denominator yields NULL instead of a "
    "division-by-zero error.\n"
)

#: 4s（2026-10-30）：单属性问句只投影一列。BIRD 里「Which are the cards…」类
#: 金标只要一个标识/描述列，多带一列 name/status/enrollment 在官方口径下直接算错。
_RULE_SINGLE_COLUMN_4S = (
    "4s. The question asks for a single attribute or a list of entities and does "
    "not ask for several attributes. Project exactly ONE column: do not add an id "
    "next to a name, a status next to a format, or an enrollment next to a school.\n"
)

#: 4t（2026-10-30）：时间戳列按日历日期比较时先转 date。
#: 直接 `timestamp > '2014-09-01'` 会漏掉当天 00:00 之后的记录。
_RULE_DATE_CAST_4T = (
    "4t. When a column stores a timestamp/datetime and the question gives a "
    "calendar date, cast the column to a date before comparing (for example "
    "`col::date = 'YYYY-MM-DD'` or `DATE(col) = ...`), so rows later that day are "
    "included.\n"
)

#: 4u（S1，2026-10-30）：格式预测给出的输出列/形状。只作提示，不覆盖 schema 校验。
_RULE_ANSWER_FORMAT_4U = (
    "4u. `answer_format` lists the output columns the question expects, in order. "
    "Project exactly those columns when they exist in the schema; never invent a "
    "column that is not in the schema. If it conflicts with the question or with "
    "`hints`, the question and `hints` win.\n"
)

#: 4w（2026-10-30）：ORDER BY 的 NULL 排序显式化。BIRD 金标大量使用
#: `DESC NULLS LAST` / `ASC NULLS FIRST`；PG 默认相反，NULL 会改变行序/取到的行。
_RULE_NULL_ORDER_4W = (
    "4w. When you use ORDER BY (including inside RANK()/ROW_NUMBER() window "
    "ORDER BY), make NULL ordering explicit: for a descending 'highest/most/"
    "latest' sort add NULLS LAST; for an ascending 'lowest/least/oldest' sort "
    "add NULLS FIRST. Keep the requested sort direction. On SQL Server, which "
    "does not support NULLS LAST/FIRST, emulate it with an extra sort key "
    "`CASE WHEN <col> IS NULL THEN 1 ELSE 0 END`.\n"
)

#: 4y（2026-10-31）：CTE 选中表之间的结构化 join 边。
_RULE_CTE_JOINS_4Y = (
    "4y. `cte_joins` lists join edges among the tables in `schema_preview`. Use "
    "them when combining those tables; do not replace a listed join key with a "
    "guessed one.\n"
)

#: 4x（2026-10-31）：大 schema 分块选择生效时，跨表过滤/字段必须 join，不能丢条件。
_RULE_CTE_JOIN_4X = (
    "4x. `schema_preview` lists every table available in this source. When a "
    "filter, attribute, or output field the question needs lives in a table other "
    "than the anchor, JOIN that table instead of dropping the condition. Use join "
    "keys that appear in both tables' column lists (same column name, e.g. country "
    "or city) or the provided join edges. Never silently answer from the anchor "
    "table alone when the question needs another table.\n"
)

#: 4x（2026-11-04 值同义词演化线 Task 2）：**仅在这次真给了 `value_aliases` 时**渲染。
#: 别名是 KB 级「people say A, the data stores B」的登记：问句命中 `term` 时过滤必须用
#: `canonical`，把字面 `term` 写进 SQL 会 0 行（真机：「华东环保」→「华东环保集团」）。
_RULE_VALUE_ALIASES_4X = (
    "4x. Some payloads list `value_aliases` — pairs of a `term` (a spelling people use) "
    "and the `canonical` value actually stored in the data. When the question contains "
    "a `term`, use the full `canonical` value verbatim (even though it contains the "
    "term); do not filter on the bare term.\n"
)

#: 4v（2026-10-30）：最高级问句只返回单行。BIRD 里 “Which X has the highest Y”
#: 的金标是 1 行；我们常给 LIMIT 10/100，官方 EX 直接判错。
_RULE_SUPERLATIVE_LIMIT_4V = (
    "4v. This question asks for THE single top entity/record (a superlative such "
    "as highest/lowest/oldest/youngest). Apply LIMIT 1 (PostgreSQL/MySQL) or "
    "SELECT TOP 1 (SQL Server) to the final result so it returns exactly one row. "
    "Do NOT change which columns you project or how you compute them because of "
    "this rule; only limit the number of rows.\n"
)

_RULE_GRAIN_4AA = (
    "4aa. This question asks for a list of entities or a count of entities. "
    "Decide the entity grain before writing SQL: when you project entity labels "
    "for a list/which/who question, use SELECT DISTINCT on those labels; when you "
    "count entities, use COUNT(DISTINCT <entity key>) rather than COUNT(*). Do not "
    "let joins multiply entity rows; do not return duplicate entity rows for "
    "all/list questions.\n"
)

_RULE_LEFT_JOIN_4AB = (
    "4ab. The question asks a yes/no or conditional question (Did ...? If yes / "
    "Whether / Is there ...). Preserve the negative answer: use a LEFT JOIN for the "
    "optional relation and a CASE expression to output the negative branch (for "
    "example NO) instead of INNER JOIN, which would drop those rows.\n"
)

_RULE_DIMENSION_JOINS_4Z = (
    "4z. `dimension_joins` are explicit fact-to-dimension joins. When the question "
    "asks for a human-readable name or label (team, country, city, province, "
    "continent, player, customer, vendor, department, etc.), JOIN the listed "
    "dimension table with those keys and project its label_columns (for example "
    "name/title/firstName+lastName). Do not answer with the fact table code column "
    "when the question asks for the name.\n"
)

_RULE_HINTS_4H = (
    "4h. `hints` are authoritative domain rules for this question (definitions, formulas, "
    "value mappings). Apply them; quote literals exactly as given. If a hint conflicts "
    "with the schema, follow the hint's intent but never invent tables or columns.\n"
)


def _format_few_shots(rows: Optional[list[dict]]) -> str:
    """同库 few-shot 提示块；空列表 = 一个字节都不加。"""
    if not rows:
        return ""
    parts = ["\nReference examples from the same database (question → SQL):\n"]
    for index, row in enumerate(rows, 1):
        question = str(row.get("question") or "").strip()
        sql = str(row.get("sql") or "").strip()
        if not question or not sql:
            continue
        cot = str(row.get("cot") or "").strip()
        if cot:
            parts.append(
                f"Q{index}: {question}\nReasoning{index}: {cot}\nSQL{index}: {sql}\n")
        else:
            parts.append(f"Q{index}: {question}\nSQL{index}: {sql}\n")
    if len(parts) == 1:
        return ""
    parts.append(
        "Use them only to understand column semantics and SQL style; do not copy "
        "their filters or assume their tables are relevant.\n")
    return "".join(parts)


def preview_has_rows(schema_preview: Optional[list[dict]]) -> bool:
    """预览里是否有任一表带了采样行（P2-4b 规则 4k 的渲染条件）。"""
    return any(isinstance(entry, dict) and entry.get("sample_rows")
               for entry in (schema_preview or []))


def has_mapping_slots(concept_fields: Optional[list[dict]]) -> bool:
    """`concept_fields` 里是否带任何语义槽（`filter`/`aggregation`/`grain`）。

    4f 的渲染判据（与 4e 的「有 concept_fields 才渲染」同族，但更细：只有槽才需要
    解释槽怎么用）。宽容收敛：坏输入一律按 False，不抛。
    """
    for group in concept_fields or []:
        if not isinstance(group, dict):
            continue
        columns = group.get("columns")
        for col in (columns if isinstance(columns, list) else []):
            if not isinstance(col, dict):
                continue
            if any(col.get(key) for key in ("filter", "aggregation", "grain")):
                return True
    return False

#: 4b 的**共享文案**（E1，2026-10-07：多轮档开局也注入 1 跳 join 图后，两档同文）。
#:
#: 4b 里 `origin` 那半句是**语义**、不是装饰：值传到 payload 里只解决一半 ——
#: 模型不知道 `derived` 是「猜的」，就会把命名推导的边当真外键用（spec §九.3
#: 的「prompt 提示」、§四 的「让模型知道这条边是声明的还是猜的」）。
#: `cardinality` 不解释（它只影响写法风格，不影响这条边能不能信）。
_RULE_JOINS_4B = (
    "4b. When `joins` are given you may join those tables in the same query: each "
    "entry means `from_table.from_column` matches `to_table.to_column`, and `origin` "
    "says where that edge came from — `declared` = a real foreign key in the "
    "database, `comment` = stated in a column comment, `value` = confirmed by value "
    "containment against the data, `derived` = inferred from naming conventions, "
    "so treat it as a hint rather than a fact. `related_tables` "
    "carries their columns. Never join tables that are not listed; "
    "if `joins_truncated` is true the graph is incomplete, so prefer saying "
    "`not_answerable` over assuming an edge that is not listed.\n"
)


def _tool_summary(description: str, *, limit: int = 120) -> str:
    """工具描述 → 提示词里的一句摘要（首句、压空白、超长截断）。"""
    text = " ".join(str(description or "").split())
    if not text:
        return "no description"
    first = text.split(". ")[0].rstrip(".")
    if len(first) > limit:
        first = first[:limit - 1].rstrip() + "…"
    return first


def build_sql_system_prompt(*, engine: str, table_name: str, with_tools: bool = False,
                            with_concept_fields: bool = False,
                            with_mapping_slots: bool = False,
                            with_projection_contract: bool = False,
                            with_hints: bool = False,
                            with_plan: bool = False,
                            with_schema_preview: bool = False,
                            with_preview_details: bool = False,
                            with_preview_rows: bool = False,
                            with_rank_hint: bool = False,
                            with_per_group_avg: bool = False,
                            with_exact_fields: bool = False,
                            with_hint_exactness: bool = False,
                            with_percentage_base: bool = False,
                            with_cte_hint: bool = False,
                            with_nullif_hint: bool = False,
                            with_single_column: bool = False,
                            with_date_cast: bool = False,
                            with_answer_format: bool = False,
                            with_superlative_limit: bool = False,
                            with_null_order: bool = False,
                             with_cte_join_hint: bool = False,
                             with_cte_joins: bool = False,
                             with_grain_hint: bool = False,
                             with_left_join_hint: bool = False,
                             with_dimension_joins: bool = False,
                             with_value_aliases: bool = False,
                            few_shots: Optional[list[dict]] = None,
                            with_exec_tool: bool = False,
                            tool_specs: list[tuple[str, str]] | None = None) -> str:
    """单次/多轮共用的 SQL 生成提示词。

    `with_tools=False` ⇒ 与抽出前 `generate_sql` 里那份**逐字节相同**（有用例钉）；
    `with_tools=True` ⇒ **保留 4b**（E1，2026-10-07：多轮开局现在也注入 1 跳 join 图 ——
    锚错现场实证「模型不知道自己缺」型缺口按需取兜不住，与 4c/4d 同族）+ **保留 4c/4d**
    （样本随列清单注入、约束补回开局 payload —— 2026-10-02 gap1 同族修复）+ **改写规则 4**
    （「不许编列」→「不许编没给过也没 resolve 过的列」）
    + 2026-10-07「锚错兜底」A 给规则 4 追加跨实体引导句（join 先查工具、别照表名猜列名）
    + **改写规则 5**（「答不了就 not_answerable」→「先用工具、再拒答」，续档 2026-10-02）
    + 追加规则 6（工具说明 + 实体名指引）。

    `with_concept_fields=True` ⇒ slot 规则后追加 4e（只在本次真给了 concept_fields 时由
    调用方传 True；默认 False 的返回与今天**逐字节相同**，sha256 冻结用例钉着）。
    `with_mapping_slots=True` ⇒ 再追加 4f（只在本次注入的列真带 `filter`/`aggregation`/
    `grain` 时由调用方传 True；默认 False ⇒ 提示词逐字节不变）。

    `tool_specs`（演化台 `tool_schema`，2026-10-26）：**有效工具名 + 有效描述**的清单。
    默认 `None` ⇒ 与今天逐字节相同（sha 冻结用例钉着）；只有多轮档在**真有覆盖/停用**
    时才传 —— 规则 4/5/6 按**实际存在的工具**动态渲染，停用的工具绝不点名（否则模型
    会去调一个不存在的工具、写出 DSML 文本 → `llm_failed`）。0 个工具时规则 6 明确
    「本轮没有可用工具，直接作答」。

    ⚠️ `Table:` 后面拼的就是调用方给的那个标识 —— **可能是限定名**（`public.t_asset`），
    也可能是实体名（存量 schema 行的回退档）。限定名那一档是**目标形态**：
    表不在默认 schema 时只有限定名查得到，而模型照抄这个字符串比自己补 schema 可靠。
    这里只原样拼、不做任何加工（加工过就对不上调用方要去查的那张表了）。
    """
    if with_tools:
        # ⚠️ **续档 2（2026-10-02 傍晚，同窗口交错 A/B）**：句尾补「标识符逐字照抄」——
        # 多轮档实测手滑（`dbo.jobs` 写成 `dbo.job`）只在**工具轮之后**出现（单次臂探针 6/6 不犯），
        # 加行后同窗口 A/B：拼错 2/10 → 0/10（两批合计 4/20 → 0/20）。
        # A（锚错兜底，2026-10-07）：句尾加**跨实体引导** —— 锚在任一端时，join 列名都
        # 必须先 `list_related`/`resolve_entity` 核对、不许照表名直觉猜（真机实证：锚=
        # WorkingLifes 时模型 3/3 猜 `working_life_id`（真名 `working_lifes_id`）→ 207）。
        # 只动 with_tools 分支；单次档提示词逐字节冻结（`test_single_shot_prompt_sha256_is_pinned`）。
        rule_4_head = (
            "neither given to you nor returned by a tool call. Confirm the entity "
            "is unknown before giving up. Copy table and column names exactly as "
            "they appear (the table is `dbo.jobs`, never `dbo.job`). ")
        join_tail_default = (
            "If you need to join a table that is not in the column list you were "
            "given, call `list_related` or `resolve_entity` first — never guess a "
            "join column from the table name alone (the real name often differs: "
            "`working_lifes_id`, not `working_life_id`).\n")
        join_tail_dynamic = (
            "If you need to join a table that is not in the column list you were "
            "given, call an available tool (rule 6) first — never guess a join "
            "column from the table name alone (the real name often differs: "
            "`working_lifes_id`, not `working_life_id`).\n")
        join_tail_no_tools = (
            "If you need to join a table that is not in the column list you were "
            "given, never guess a join column from the table name alone (the real "
            "name often differs: `working_lifes_id`, not `working_life_id`).\n")
        if tool_specs is None:
            rule_4 = _RULE_4_STEM + rule_4_head + join_tail_default
        else:
            rule_4 = _RULE_4_STEM + rule_4_head + (
                join_tail_dynamic if tool_specs else join_tail_no_tools)
        # **E1（2026-10-07）：4b 返场** —— 锚错现场实证（锚=WorkingLifes 时模型 3/3
        # 猜 `working_life_id` 撞 207；单次档同现场带图 3/3 过）：join 图是「模型不知道
        # 自己缺」型缺口，按需取兜不住，与 4c/4d 同族、须铺到眼前。**4c/4d 保留**：
        # 样本随列清单、约束补回开局 payload（2026-10-02 gap1 同族修复，文案见共享常量）。
        #
        # ⚠️ **续档改动（2026-10-02 真机实测：78/78 零工具调用）**：原稿规则 5 与单次档
        # 共用（「答不了就 not_answerable」），且它排在规则 6 之前 —— 模型把它当**第一出口**
        # （「company 表没有职位数」型拒答全走它，工具一次没动）。工具档改成「**先用工具、
        # 再拒答**」；配套三件（同一续档）：规则 6 加实体名指引、工具结果回带实体名
        # （`multiturn._list_related`）、末轮接住工具调用（`generate_sql_multiturn`）。
        no_tool_rule_5 = ('5. If the question cannot be answered with these columns, '
                          'return `{"not_answerable": true, "reason": "<short reason>"}` '
                          'instead.\n')
        if tool_specs is None or tool_specs:
            rule_5 = ("5. If the given columns alone cannot answer the question, do NOT give "
                      "up: use the tools (rule 6) FIRST to look for the missing tables, "
                      "columns or values. Return `{\"not_answerable\": true, \"reason\": "
                      "\"<short reason>\"}` only after tool calls have not given you what "
                      "you need.\n")
        else:
            rule_5 = no_tool_rule_5
        slot_rules = (_RULE_JOINS_4B + _RULE_SAMPLES_4C + _RULE_CONSTRAINTS_4D
                      + (_RULE_CONCEPTS_4E if with_concept_fields else "")
                      + (_RULE_CONCEPTS_4F if with_mapping_slots else "")
                      + (_RULE_PROJECTION_4G if with_projection_contract else "")
                      + (_RULE_HINTS_4H if with_hints else "")
                      + (_RULE_PLAN_4I if with_plan else "")
                      + ((_RULE_PREVIEW_4J_DETAILS if with_preview_details
                          else _RULE_PREVIEW_4J) if with_schema_preview else "")
                      + (_RULE_PREVIEW_ROWS_4K if with_preview_rows else "")
                      + (_RULE_RANK_4L if with_rank_hint else "")
                      + (_RULE_PER_GROUP_AVG_4M if with_per_group_avg else "")
                      + (_RULE_EXACT_FIELDS_4N if with_exact_fields else "")
                      + (_RULE_HINT_EXACTNESS_4O if with_hint_exactness else "")
                      + (_RULE_PERCENTAGE_BASE_4P if with_percentage_base else "")
                      + (_RULE_CTE_4Q if with_cte_hint else "")
                      + (_RULE_NULLIF_4R if with_nullif_hint else "")
                      + (_RULE_SINGLE_COLUMN_4S if with_single_column else "")
                      + (_RULE_DATE_CAST_4T if with_date_cast else "")
                      + (_RULE_ANSWER_FORMAT_4U if with_answer_format else "")
                      + (_RULE_SUPERLATIVE_LIMIT_4V if with_superlative_limit else "")
                      + (_RULE_NULL_ORDER_4W if with_null_order else "")
                       + (_RULE_CTE_JOIN_4X if with_cte_join_hint else "")
                       + (_RULE_CTE_JOINS_4Y if with_cte_joins else "")
                       + (_RULE_GRAIN_4AA if with_grain_hint else "")
                       + (_RULE_LEFT_JOIN_4AB if with_left_join_hint else "")
                       + (_RULE_DIMENSION_JOINS_4Z if with_dimension_joins else "")
                       + (_RULE_VALUE_ALIASES_4X if with_value_aliases else ""))
        if tool_specs is None and not with_exec_tool:
            tool_rules = (
                "6. You may call the provided tools to look things up before answering: "
                "`list_related` (join edges of an entity — call it again on a related table to go one more hop), "
                "`sample_values` (real value samples of a column), "
                "`resolve_entity` (an entity's columns and library conventions). "
                "Use them when the given columns are not enough; do not guess values or join edges you have not seen. "
                "When calling a tool, pass the ENTITY name — for table `dbo.company` the entity is "
                "`Company` — never a raw table name; related tables come with their entity names.\n"
            )
        elif tool_specs:
            listing = ", ".join(
                f"`{name}` ({_tool_summary(description)})"
                for name, description in tool_specs)
            if with_exec_tool:
                # 执行工具吃的是完整 SQL；「传实体名」那句只对三只实体工具成立。
                entity_hint = (
                    "When calling an ENTITY tool (`list_related`, `sample_values`, "
                    "`resolve_entity`), pass the ENTITY name — for table `dbo.company` the "
                    "entity is `Company` — never a raw table name; related tables come with "
                    "their entity names. `execute_sql` instead takes a complete SQL statement.\n")
            else:
                entity_hint = (
                    "When calling a tool, pass the ENTITY name — for table `dbo.company` the "
                    "entity is `Company` — never a raw table name; related tables come with "
                    "their entity names.\n")
            tool_rules = (
                "6. You may call the provided tools to look things up before answering: "
                f"{listing}. "
                "Use them when the given columns are not enough; do not guess values or join edges you have not seen. "
                f"{entity_hint}"
            )
            if with_exec_tool:
                tool_rules += (
                    "6b. `execute_sql` runs ONE read-only SELECT you write to inspect real "
                    "data (row counts, a few rows, value formats). Use it to validate "
                    "filters, joins and literals before submitting. Only tables already "
                    "given or returned by tools are allowed; it is not the final answer. "
                    "If it returns an error, fix the SQL or proceed with what you have.\n")
        else:
            # 0 个有效工具（全被 KB 覆盖停用）：绝不点名任何工具，否则模型会写出
            # 对不存在的工具的 DSML 文本 → 解析失败 → `llm_failed`。
            tool_rules = (
                "6. No tools are available this round — answer directly with the "
                "information you already have; do not ask for tool calls.\n")
    else:
        rule_4 = _RULE_4_STEM + "not in the given list.\n"
        rule_5 = ('5. If the question cannot be answered with these columns, return '
                  '`{"not_answerable": true, "reason": "<short reason>"}` instead.\n')
        # 4c 与 4b 同例：**无条件常驻** —— 不提「这一次有没有给 samples」，否则同一份提示词
        # 会有两种形态，而「样本只是样本」那半句正是防止模型照着 3 个样本答「只有这三种」。
        #
        # ⚠️ **别删**：它一度看着像死文本（② 曾经短期不注入 samples），但实测它**本身就会
        # 改变答案形状** —— 真机矩阵里「无 4c」那档 `jobs_by_working_life` 稳定多带
        # `working_lifes_id`（5/5、每次 SQL 一字不差，不是模型抖动，是这条把答案推歪了）。
        # 那次测量在问句钉死形状**之前**，钉死后它的边际作用没再单独量过 —— 留着零成本。
        # 依据与台账：`.superpowers/sdd/progress-evidence.md`（回退报告 ev-revert-report.md、
        # 重放依据 ev-task-7-report.md）。
        #
        # 4d 与 4b/4c 同例：**无条件常驻** —— 提示词不因「这次有没有给约束」变形；
        # 约束本体在 payload 的 `constraints` 键里（没给时该键不出现，payload 逐字节不变）。
        # ⚠️ **别当它没用就删**：它把「库自己的口径」抬成必须遵守的规则（如 status 码怎么读）。
        # 真机实测（2026-10-01）：模型拿恒为 'False' 的 `is_enable` 当过滤条件，把「正在招聘中」
        # 答成 0 行；那一列的**值样例就在 payload 里**（4c 一档）也救不了 —— 值样例证明不了
        # 「别拿它当过滤」，只有这条规则能。依据：规格
        # `docs/superpowers/specs/2026-10-01-nl2sql-constraint-query-rule-design.md` §一
        # （两条真机题）与本线报告 `.superpowers/sdd/p12-task-2-report.md`。
        slot_rules = (_RULE_JOINS_4B + _RULE_SAMPLES_4C + _RULE_CONSTRAINTS_4D
                      + (_RULE_CONCEPTS_4E if with_concept_fields else "")
                      + (_RULE_CONCEPTS_4F if with_mapping_slots else "")
                      + (_RULE_PROJECTION_4G if with_projection_contract else "")
                      + (_RULE_HINTS_4H if with_hints else "")
                      + (_RULE_PLAN_4I if with_plan else "")
                      + ((_RULE_PREVIEW_4J_DETAILS if with_preview_details
                          else _RULE_PREVIEW_4J) if with_schema_preview else "")
                      + (_RULE_PREVIEW_ROWS_4K if with_preview_rows else "")
                      + (_RULE_RANK_4L if with_rank_hint else "")
                      + (_RULE_PER_GROUP_AVG_4M if with_per_group_avg else "")
                      + (_RULE_EXACT_FIELDS_4N if with_exact_fields else "")
                      + (_RULE_HINT_EXACTNESS_4O if with_hint_exactness else "")
                      + (_RULE_PERCENTAGE_BASE_4P if with_percentage_base else "")
                      + (_RULE_CTE_4Q if with_cte_hint else "")
                      + (_RULE_NULLIF_4R if with_nullif_hint else "")
                      + (_RULE_SINGLE_COLUMN_4S if with_single_column else "")
                      + (_RULE_DATE_CAST_4T if with_date_cast else "")
                      + (_RULE_ANSWER_FORMAT_4U if with_answer_format else "")
                      + (_RULE_SUPERLATIVE_LIMIT_4V if with_superlative_limit else "")
                      + (_RULE_NULL_ORDER_4W if with_null_order else "")
                       + (_RULE_CTE_JOIN_4X if with_cte_join_hint else "")
                       + (_RULE_CTE_JOINS_4Y if with_cte_joins else "")
                       + (_RULE_GRAIN_4AA if with_grain_hint else "")
                       + (_RULE_LEFT_JOIN_4AB if with_left_join_hint else "")
                       + (_RULE_DIMENSION_JOINS_4Z if with_dimension_joins else "")
                       + (_RULE_VALUE_ALIASES_4X if with_value_aliases else ""))
        tool_rules = ""

    return "".join([
        "You translate a question into ONE read-only SQL query against the given table(s).\n",
        f"Dialect: {engine}. Table: {table_name}.\n",
        "Rules:\n",
        "1. Output exactly one statement, SELECT only. Never write, drop, alter, or "
        "call procedures; never use SELECT INTO.\n",
        "2. Never use comments.\n",
        "3. Always cap the rows returned: `LIMIT n` on postgresql/mysql, "
        f"`SELECT TOP n …` on sqlserver (n <= {config.NL2SQL_MAX_ROWS}).\n",
        # SQL Server 那条是硬约束，不是风格偏好：guard 只认**行首**的 TOP，
        # 藏在 CTE 里的 TOP 它既认不出、也没法核对超没超上限，于是 `WITH …` 一律被拒
        # （Task 4 的裁决：与其猜，不如让模型重写成带 TOP 的平铺 SELECT）。
        "3b. On sqlserver do NOT use `WITH` CTEs — write a flat `SELECT TOP n …` "
        "instead (a CTE plus no leading TOP is rejected outright).\n",
        rule_4,
        slot_rules,
        _format_few_shots(few_shots),
        rule_5,
        tool_rules,
        'Output strict JSON: {"sql": str} or {"not_answerable": true, "reason": str}.',
    ])


#: SQL 生成 payload 的**规则版本**（Task 4：payload 版本进观测）。提示词规则 / payload
#: 组装口径的语义变了就改这里 —— 同一问句、同一份 schema 在不同规则下写出的 SQL 可能
#: 不同，指纹必须能区分。日期+语义后缀（如 `2026-10-26-tool-schema`），语义变化必须 bump。
PAYLOAD_RULES_VERSION = "2026-11-04-value-aliases"


def payload_fingerprint(payload: dict) -> str:
    """payload 的可复现指纹：`{rules 版本, payload}` 规范化 JSON → sha256（64 位 hex）。

    规范化 = `sort_keys=True`（字典插入序不影响结果：同内容恒同指纹）+ 紧凑分隔符 +
    `ensure_ascii=False`（中文按 UTF-8 直接编码）。**只做标识、不做校验/去重**：同一份
    payload + 同一版规则 ⇒ 同一指纹；payload 任一值或 `PAYLOAD_RULES_VERSION` 变了必变。
    """
    blob = json.dumps({"rules": PAYLOAD_RULES_VERSION, "payload": payload},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _clean_value_aliases(value_aliases: Optional[list[dict]]) -> list[dict]:
    """归一 `value_aliases` 成 payload 形状：只留 `term` / `canonical` 两键。

    空词面 / 空 canonical / 非 dict 条目一律丢弃（脏数据不该把整条 payload 带歪）；
    两个字段都 `strip()`，与 `value_aliases.py` 的 `resolve_for_question` 出口同口径。
    """
    cleaned: list[dict] = []
    for item in value_aliases or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        canonical = str(item.get("canonical") or "").strip()
        if term and canonical:
            cleaned.append({"term": term, "canonical": canonical})
    return cleaned


def build_sql_payload(question: str, *, engine: str, table_name: str, columns: list[dict],
                      joins: Optional[list[dict]] = None,
                      related_tables: Optional[list[dict]] = None,
                      joins_truncated: bool = False,
                      constraints: Optional[list[dict]] = None,
                      concept_fields: Optional[list[dict]] = None,
                      join_paths: Optional[dict] = None,
                      forbidden_note: Optional[dict] = None,
                      today: Optional[str] = None,
                      hints: Optional[list[str]] = None,
                      plan: Optional[str] = None,
                      schema_preview: Optional[list[dict]] = None,
                       cte_joins: Optional[list[dict]] = None,
                       dimension_joins: Optional[list[dict]] = None,
                       answer_format: Optional[list[str]] = None,
                       value_aliases: Optional[list[dict]] = None) -> dict:
    """单次/多轮共用的 user payload（**键序即契约**：`json.dumps` 按插入序输出）。

    ⚠️ 没有可用 join 时 payload 必须**与不带 join 的版本逐字节相同**（向后兼容的硬
    要求，`test_payload_is_unchanged_when_no_joins` 钉着）：所以三个 join 键只在**真
    给了边**时追加，`related_tables` / `joins_truncated` 不预先占位（给了 `[]` /
    `false` 都是多出来的字节）。**键的插入顺序也不能动** —— `json.dumps` 按插入序
    输出，前四个键的次序就是「逐字节相同」这句话本身。`constraints` 守**同一套规矩**
    （`test_payload_is_unchanged_when_no_constraints` 钉着）：只在真给了约束时追加
    （`[]` / `None` 都不追加），且必须排在 joins 那一段**之后**。

    ⚠️ 前提（调用方保证）：`joins_truncated` 只在 `joins` 非空时才有意义 —— `joins.py`
    的截断只在候选数超上限时发生（`MAX_JOINS ≥ 1` ⇒ kept 必非空）；本函数**不校验**，
    `joins` 为空时截断信号会被**静默丢弃**（整个 join 段不进 —— 与「没给边就与旧版
    逐字节相同」是同一条硬要求的推论，不是缺陷）。

    `join_paths`（多跳 join 路径化线，2026-10-16）：**只补 1 跳边给不出的东西** ——
    多轮档**搜到 ≥2 跳路径时**额外传它，`joins`（1 跳边）**照发**；`related_tables`
    （邻表**列清单**，不是边）也照给。其余调用方不传 ⇒ 键不出现，与旧版逐字节相同。

    ⚠️ **2026-10-16 更正**：本段原写「**路径替代 1 跳边**、搜到路径就**不再传 `joins`**」
    （号称 spec §七「换成」）—— **已被 A/B 门实测推翻**。长度 1 的「路径」只是把同一条边
    换个写法渲染，却让模型改了投影（多选一列 `COUNT`），而判分器是**行宽严格匹配** ⇒
    把一道**本来通过**的 1 跳题判红（确定性复现 3/3）。现语义 = **追加 ≥2 跳路径 + 保留 `joins`**。

    `value_aliases`（2026-11-04 值同义词演化线）：**非空才追加、恒在末位**，形如
    `[{"term": ..., "canonical": ...}]`（`_clean_value_aliases` 先归一）；空/`None`
    ⇒ 键不出现，payload 与引入前**逐字节相同**（与 constraints / hints 同款纪律）。
    """
    payload: dict = {
        "question": question, "engine": engine, "table": table_name, "columns": columns,
    }
    if joins:
        payload["joins"] = joins
    # related_tables 承载**邻表的列清单**（不是「边」）：只要「边以任一形式给了」
    # （`joins` 或 `join_paths`）就要给列清单。单次档 / 关档 / 无路径时 `join_paths` 为
    # None ⇒ 该键仍只在 `joins` 非空时出现，**与旧版逐字节相同**（同一条硬要求的推论）。
    # ⚠️ `joins` 与 `join_paths` **可同时存在**（2026-10-16 更正：路径不再「替代」1 跳边，
    # 而是**追加** ≥2 跳路径）—— 判据必须是 `joins or join_paths`：只给了路径、没给边时
    # 仍要发列清单，否则模型看不到邻表有哪些列，是真回归。
    if joins or join_paths:
        payload["related_tables"] = related_tables or []
    if joins and joins_truncated:
        payload["joins_truncated"] = True
    if constraints:
        payload["constraints"] = constraints
    # concept_fields（TBox 落点线）：一个组都没有时入参是 None/[]，payload 与旧版
    # **逐字节相同**（有用例钉）。**键序**：恒排在 constraints 之后——新增键只许
    # 追加在尾部，不许插队（「键序即契约」，json.dumps 按插入序输出）。
    if concept_fields:
        payload["concept_fields"] = concept_fields
    # join_paths（多跳 join 路径化线，2026-10-16）：**只有搜到路径时才传**（出参为
    # `{"paths": [...], "truncated": ..., "explored": ...}`），None / 空 ⇒ 不追加，
    # payload 与旧版**逐字节相同**（与 concept_fields / constraints 同款纪律）。
    # **键序**：恒排在 concept_fields 之后——新增键只许追加在尾部。
    if join_paths:
        payload["join_paths"] = join_paths
    # forbidden_note（禁止同查线）：只在锚点邻域真有 active 禁止对时传；None / 空
    # ⇒ 键不出现，payload 与旧版**逐字节相同**。**键序**：恒排在 join_paths 之后。
    if forbidden_note:
        payload["forbidden_pairs"] = forbidden_note
    # today（相对时间线，2026-10-22）：只在**问句含相对时间词**时由调用方传入；
    # 不给 ⇒ 键不出现，payload 与旧版**逐字节相同**（同 constraints / join_paths 纪律）。
    # 模型据此把「上周 / 本月 / 最近 7 天」落到真实日历，而不是拿数据最大日期当今天
    # （真机：「上周」被解成数据最大日期所在周的**前一周** → 0 行）。
    if today:
        payload["today"] = today
    # hints（2026-10-28 P0-A2）：只在真有内容时追加，且**恒在末位**；
    # 空/None ⇒ 键不出现，payload 与引入前逐字节相同。
    if hints:
        payload["hints"] = hints
    # plan（P2-1）：只在 planner 真产出时追加，恒在末位；None/空 ⇒ 键不出现。
    if plan:
        payload["plan"] = str(plan)
    # schema_preview（P2-2）：只在真有小 schema 预览时追加，恒在末位。
    if schema_preview:
        payload["schema_preview"] = schema_preview
    # cte_joins（2026-10-31）：CTE 选中表之间的 join 边，非空才追加。
    if cte_joins:
        payload["cte_joins"] = cte_joins
    # dimension_joins（2026-11-01）：事实→维度标签 join；非空才追加。
    if dimension_joins:
        payload["dimension_joins"] = dimension_joins
    # answer_format（S1，2026-10-30）：格式预测非空才追加，恒在末位；
    # 空/None ⇒ 键不出现，payload 与引入前逐字节相同。
    if answer_format:
        payload["answer_format"] = answer_format
    # value_aliases（2026-11-04 值同义词演化线）：问句真命中 KB 级同义词才传，
    # 恒在末位；空/None ⇒ 键不出现，payload 与引入前**逐字节相同**（有用例钉）。
    aliases = _clean_value_aliases(value_aliases)
    if aliases:
        payload["value_aliases"] = aliases
    return payload


async def call_llm_with_tools(
    messages: list[dict],
    *,
    tools: Optional[list[dict]] = None,
    tool_choice: str = "auto",
    model: Optional[str] = None,
    scene: str = "nl2sql_multiturn",
    max_tokens: int = 2048,
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict:
    """一次带工具的 chat 往返 → `{"content", "tool_calls": [{id,name,arguments,arguments_raw}], "finish_reason"}`。

    - 与 `generate_sql` 同一 `_get_client()`；`tool_calls[].arguments_raw` 是**原样 JSON 串**
      （回喂时 `function.arguments` 必须是字符串，见 multiturn 的循环）；
    - `arguments` 的类型契约：**通常是 dict**；坏 JSON 时回 `{}`（此时 `arguments_raw` 保留
      原文，调用方可以原样回喂给模型让它自己纠）；合法但**非对象**的 JSON（如 `[1, 2]`）
      解析出来就是 list —— 本函数**不做形状校验**，那是工具自己的入参校验（`run_tool`）；
    - **不含循环、不解析 SQL、不做重试** —— 那是 `nl2sql/multiturn.py` 的事；
    - **不看 `finish_reason`**：截断（`"length"`）**不在这里抛**。`generate_sql` 遇截断是抛的，
      但多轮档一轮被截断未必致命（还能在下一轮重来），**调用方必须自己判** —— 这是有意的分工；
    - `max_tokens=2048` 的依据（**每轮**预算，不是整轮）：一轮的产出要么是 ≤3 个工具调用的
      参数（短 JSON）、要么是与单次档同形的终答（一条 SQL / 一句 not_answerable），
      与 `generate_sql` 的 1024 同量级、留一倍余量；
    - `tools` 为空（`None` / `[]`）时 **`tools` 与 `tool_choice` 一并发不出去** ——
      `tool_choice` 不是独立开关，它只为 `tools` 而传（今天等价，但别指望靠它收口）；
    - `scene` 默认 `nl2sql_multiturn`，**要求网关白名单登记**（否则思考 token 吃预算）。
    """
    client = _get_client()
    model = model or os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    import uuid

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 与其它 scene 同口径：**改名要同步改网关白名单**（LLMGW_DISABLE_THINKING_SCENES），
        # 否则网关静默不注入、思考 token 先把预算吃光（历史故障见 propose_concept_clusters）。
        "X-Jonex-Scene": scene,
        "X-Jonex-Trace-Id": trace_id or f"{scene}:{uuid.uuid4().hex}",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    kwargs: dict = {"model": model, "messages": messages, "max_tokens": max_tokens,
                    "temperature": 0, "extra_headers": extra_headers}
    # 没给工具时 `tools` / `tool_choice` **一个都不出现**：空列表在某些实现里会被当成
    # 「传了工具但没人能调」而报错，且多出来的键会让桩/回放比对不上（有用例钉住）。
    # ⚠️ `tool_choice` 在这里是被**静默丢弃**的、不是独立开关：传了 `tool_choice` 但
    # `tools` 为空，它压根不会进请求 —— 想靠它收口的人会扑空。
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
    resp = await client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    message = choice.message
    calls = []
    for tc in (message.tool_calls or []):
        raw = tc.function.arguments or "{}"
        # 参数不是合法 JSON 时**不抛**：回 `{}` 让调用方（multiturn 循环）看得见这次工具
        # 调用、按自己的策略处置（回一条报错消息给模型 / 判失败）—— 抛在这里等于把一次
        # 模型的小失误升级成整轮问答失败。
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": tc.id, "name": tc.function.name,
                      "arguments": args, "arguments_raw": raw})
    return {"content": (message.content or "").strip(), "tool_calls": calls,
            "finish_reason": getattr(choice, "finish_reason", None)}


async def generate_sql(
    question: str,
    *,
    engine: str,
    table_name: str,
    columns: list[dict],
    joins: Optional[list[dict]] = None,
    related_tables: Optional[list[dict]] = None,
    joins_truncated: bool = False,
    constraints: Optional[list[dict]] = None,
    concept_fields: Optional[list[dict]] = None,
    projection_contract: bool = False,
    hints: Optional[list[str]] = None,
    value_aliases: Optional[list[dict]] = None,
    plan: Optional[str] = None,
    schema_preview: Optional[list[dict]] = None,
    preview_details: bool = False,
    rank_hint: bool = False,
    per_group_avg: bool = False,
    exact_fields: bool = False,
    hint_exactness: bool = False,
    percentage_base: bool = False,
    cte_hint: bool = False,
    nullif_hint: bool = False,
    single_column: bool = False,
    date_cast: bool = False,
    answer_format: Optional[list[str]] = None,
    superlative_limit: bool = False,
    null_order: bool = False,
    cte_join_hint: bool = False,
    cte_joins: Optional[list[dict]] = None,
    grain_hint: bool = False,
    left_join_hint: bool = False,
    dimension_joins: Optional[list[dict]] = None,
    few_shots: Optional[list[dict]] = None,
    forbidden_note: Optional[dict] = None,
    today: Optional[str] = None,
    retry_note: Optional[str] = None,
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict:
    """把一个问句落成**一条**只读 SQL，或者明说「用这些列答不了」。

    分诊不做成单独一步：「这不是数据问题」「这些列不够」由**这一次调用直接说**
    （返回 `not_answerable`），省一次往返、也少一处判据分叉。

    Args:
        engine: 方言。**必须给** —— SQL Server 要 `TOP`、PG/MySQL 要 `LIMIT`，
            不说清楚模型就会串档。
        table_name: **物理表标识**，调用方取自编译 schema 的 `source_object_id`。
            **它可以是（也通常就是）限定名 `<schema>.<table>`** —— PG `public.t_asset`、
            MSSQL `dbo.Company`、MySQL `<db>.<table>`：表不在默认 schema 时，光给表名
            照样查不到（真机实测 `relation "knowledge_data_sources" does not exist`）。
            它**不是**① 的实体编码：实体名是概念层的名字，真源上未必有同名的表。
            调用方在 `source_object_id` 缺失（存量 schema 行）时回退传实体名 ——
            本函数对两种形态一视同仁，原样拼进提示词，不在这里做任何判定。
        columns: `[{"name": str, "type": str, "samples": list?}]`，来自该实体的 compiled schema。
            `samples` 是**取样得到的**真实取值（只给取值域小的列，见 `nl2sql/evidence.pick_samples`），
            模型照着它写 `WHERE` 的字面量 —— 真机实测能修「列名给的信息不够、猜错列/猜错取值形态」
            那一类错（2026-10-01 全量 A/B：有样本 **22/23**、无样本 21/23，逐题差异只有证据依赖题）。
            **它是可选键**：没探过证据 / 该列取值域大（>10）/ 超出字符预算时就缺失，
            缺失时 payload 与不带样本的版本**逐字节相同**（有用例钉住）。
        joins: 锚点实体**同源 1 跳内、已校验可用**的 join 边
            （`[{from_table, from_column, to_table, to_column, cardinality, origin}]`，
            由 `nl2sql/joins.py` 选好；本函数**不校验**，只原样注入）。
            **为空/不传时 payload 与不带 join 的版本逐字节相同**。
        related_tables: 被 join 表的列清单（`[{table, columns}]`）。不给列，模型写不出
            `GROUP BY chan.name` 这类跨表取列。
        joins_truncated: 边被上限截断过 —— 注入里会明确告诉模型「这份图不全」。
        constraints: `[{name, target_type, target_code, constraint_type, expression}]` ——
            **人审确认过的库口径**（如「在招」= `status='1' AND is_del='False'`），由
            调用方 `nl2sql/constraints.filter_constraints` 挑成「只属于锚点实体」的那几条
            （≤8 条、expression 合计 ≤1500 字符；2026-10-12 起）。**不传 / `None` / `[]` 三者等效**：
            一条约束都没有时 `constraints` 键不出现，payload 与不带约束的版本**逐字节
            相同**（有用例把这三档都钉住）。判据是下面 `if constraints:` 的**真值判断**，
            **别改成 `is not None`** —— 那会让线上主路径（`filter_constraints` 一条都没
            挑中时传的是 `[]`）静默多出一个空键、逐字节契约当场破掉。
        concept_fields: `[{name, display_name?, columns: [{name, role?, filter?,
            aggregation?, grain?}]}]` —— 抽象字段落点（「概念 → 多列」映射，由 service 层
            `_collect_concept_fields` 组装）。**非空才加 `concept_fields` 键，且恒排在
            `constraints` 之后；空/`None` 时 payload 逐字节不变**。它同时开关提示词的
            4e 段（有它才渲染）；列真带 `filter`/`aggregation`/`grain` 时再渲染 4f
            （槽的用法说明，见 `has_mapping_slots`）。
        value_aliases: `[{"term": str, "canonical": str}]` —— KB 级值同义词（问句词面
            命中 `term` 时，筛选要用数据里真实存的 `canonical`）。由 `value_aliases.
            resolve_for_question` 选好；本函数只做归一（丢弃空项/多余键），**非空才**
            追加 `value_aliases` payload 键与提示词 4x 规则；`None` / `[]` ⇒ payload 与
            提示词均与引入前**逐字节相同**（有用例钉）。
        retry_note: 重试轮的整段英文反馈（由 `service` 层 `_build_retry_note` 拼好，含内部
            换行）。**附在 user prompt 尾部、与 payload 隔一个空行**；`None` / 空串时不加，
            首轮 user_prompt 与本参数引入前**逐字节相同**。只动 user 侧，**system prompt
            一个字节不动**（单次档提示词逐字节冻结，见 `test_single_shot_prompt_sha256_is_pinned`）。

    Returns:
        `{"sql": str, "payload_version": str}` 或
        `{"not_answerable": True, "reason": str, "payload_version": str}`。
        `payload_version` = 本次 payload（+ `PAYLOAD_RULES_VERSION`）的 sha256 指纹，
        可复现：同一问句 + 同一 schema + 同一规则版本 ⇒ 同一指纹。**调用方不得把它
        透给终端用户**（service 只送进观测 scene）。

    Raises:
        RuntimeError: 模型回了空正文 / 不是 JSON / 两者都没有 / 被 `max_tokens` 截断。
            与 `propose_concept_clusters` 同口径：**抛**而不是回空值 ——
            回空值在调用方眼里是「模型说没问题」，会把错数字答出去。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    # 值同义词（2026-11-04）：先归一，payload 的键与提示词 4x 规则共用同一判据
    # （若逐处判 `bool(value_aliases)`，全脏条目时会「提示词说有条目、payload 却没有」）。
    aliases = _clean_value_aliases(value_aliases)

    # 提示词抽成 `build_sql_system_prompt` 共用（多轮档传 `with_tools=True`）；
    # 这里不传 = `with_tools=False`，与抽出前那份**逐字节相同**（有用例钉）。
    # `with_concept_fields` 只在**本次真给了 concept_fields**时为 True（4e 段按需渲染，
    # 默认 False 的返回与今天逐字节相同、sha256 冻结用例钉着）。
    system_prompt = build_sql_system_prompt(
        engine=engine, table_name=table_name,
        with_concept_fields=bool(concept_fields),
        with_mapping_slots=has_mapping_slots(concept_fields),
        with_projection_contract=projection_contract,
        with_hints=bool(hints), with_plan=bool(plan),
        with_schema_preview=bool(schema_preview),
        with_preview_details=bool(preview_details),
        with_preview_rows=preview_has_rows(schema_preview),
        with_rank_hint=bool(rank_hint),
        with_per_group_avg=bool(per_group_avg),
        with_exact_fields=bool(exact_fields),
        with_hint_exactness=bool(hint_exactness),
        with_percentage_base=bool(percentage_base),
        with_cte_hint=bool(cte_hint),
        with_nullif_hint=bool(nullif_hint),
        with_single_column=bool(single_column),
        with_date_cast=bool(date_cast),
        with_answer_format=bool(answer_format),
        with_superlative_limit=bool(superlative_limit),
        with_null_order=bool(null_order),
         with_cte_join_hint=bool(cte_join_hint),
         with_cte_joins=bool(cte_joins),
         with_grain_hint=bool(grain_hint),
         with_left_join_hint=bool(left_join_hint),
         with_dimension_joins=bool(dimension_joins),
         with_value_aliases=bool(aliases),
        few_shots=few_shots)
    # ⚠️ payload 组装抽到 `build_sql_payload`（E1，2026-10-07：多轮档共用，键序契约
    # 收成单点）——传参与搬移前逐项相同，输出逐字节不变（有用例钉）。
    payload: dict = build_sql_payload(
        question, engine=engine, table_name=table_name, columns=columns,
        joins=joins, related_tables=related_tables, joins_truncated=joins_truncated,
        constraints=constraints, concept_fields=concept_fields,
        forbidden_note=forbidden_note, today=today, hints=hints, plan=plan,
         schema_preview=schema_preview, cte_joins=cte_joins,
         dimension_joins=dimension_joins,
         answer_format=answer_format,
         value_aliases=aliases)
    # payload 版本指纹（Task 4）：这次生成的可复现标识，随结果回带（service 层只把它
    # 送进观测 scene，不进 HTTP 响应）。算在 payload 组装之后、发请求之前。
    version = payload_fingerprint(payload)
    user_prompt = json.dumps(payload, ensure_ascii=False)
    # 重试轮才非空：service 层拼好的整段反馈附在 user prompt 尾部（与 payload 隔一个空行）。
    # ⚠️ `None` / 空串时**一个字节都不追加** —— 首轮 user_prompt 必须与本参数引入前逐字节
    # 相同（`test_payload_is_unchanged_when_no_retry_note` 钉着）。判据是**真值判断**，
    # 别改成 `is not None`（空串会多拼出一个 `"\n\n"`，同 payload 那几条契约的同一课）。
    if retry_note:
        user_prompt = f"{user_prompt}\n\n{retry_note}"

    import uuid

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 与其它 scene 同口径：**改名要同步改网关白名单**（LLMGW_DISABLE_THINKING_SCENES），
        # 否则网关静默不注入、思考 token 先把预算吃光（历史故障见 propose_concept_clusters）。
        "X-Jonex-Scene": "nl2sql_generate",
        "X-Jonex-Trace-Id": trace_id or f"nl2sql:{uuid.uuid4().hex}",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # temp=0：同一问句 + 同一份列，重跑得到的 SQL 应当一样（便于复盘与复现）
        temperature=0,
        # ⚠️ 不要照抄 propose_concept_clusters 的 32768：那个的输出规模与输入同阶
        # （每个实体一条映射），而这里只出一条 SQL。默认 1024 足够，且能尽早暴露失控输出；
        # **开思考时必须放大**（reasoning 先吃预算），由 `NL2SQL_LLM_MAX_TOKENS` 控制。
        max_tokens=int(config.NL2SQL_LLM_MAX_TOKENS),
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError(
            "SQL 生成被 max_tokens 截断（finish_reason=length）——预算要放大或问题要收敛"
        )
    content = (choice.message.content or "").strip()
    if not content:
        raise RuntimeError("SQL 生成：模型返回空正文")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"SQL 生成：正文不是合法 JSON（{exc}）") from exc
    if not isinstance(data, dict):
        raise RuntimeError("SQL 生成：正文不是 JSON 对象")
    if data.get("not_answerable"):
        return {"not_answerable": True, "reason": str(data.get("reason") or "用这些列答不了"),
                "payload_version": version}
    sql = str(data.get("sql") or "").strip()
    if not sql:
        raise RuntimeError("SQL 生成：既没有 sql 也没有 not_answerable")
    return {"sql": sql, "payload_version": version}


async def filter_relevant_concepts(
    question: str,
    candidates: list[dict],
    *,
    tenant_id: str | None = None,
    kb_id: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
) -> list[int]:
    """问句 vs 编号候选 → **保留的候选下标**（保序、去重）。

    坏答案抛（空正文 / 非 JSON / 回包非对象 / 无 `keep` / 非数组 / 下标全不可用 /
    截断），与 `propose_attribute_display_names` 同口径：抛给调用方去「原样放行」。
    空列表是**合法筛空**（"一条都不相关"），与"没答出来"必须区分 —— 后者靠抛。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You filter candidate entities for a data question. You are given a "
        "question and a numbered list of candidate entities (entity name, the "
        "concept it belongs to, and the words that matched). Return ONLY the "
        "indices of the candidates the question is actually asking about.\n"
        "Rules:\n"
        "- Return every candidate that is plausibly the subject of the question.\n"
        "- If none of the candidates is relevant, return an empty list.\n"
        "- Never invent indices; indices must come from the input list.\n"
        "- `matched_terms` are the exact words from the question that hit this "
        "entity. level l1/l2 = entity name/alias or source table name; l3 = an "
        "attribute display name; l4 = a short core word derived from an attribute "
        "display name.\n"
        "- `matched_attributes` are the business (Chinese) attribute names behind "
        "those hits — the columns this entity can answer questions about.\n"
        "- Keep a candidate when the question's subject OR any condition it asks "
        "about (e.g. a memory/CPU/status condition) plausibly refers to the entity "
        "or to any of its `matched_attributes`.\n"
        'Output strict JSON: {"keep": [int, ...]}'
    )
    # 载荷对齐 spec §3.4「实体名 / KB / 源名 / 命中词与级别 / 分数」：`score`、
    # `matched_terms`（全部命中词+级别）、`matched_attributes`（属性级证据）是
    # T9-S1 的修复面 —— 旧载荷只给单个 `matched_term`，筛选 LLM 看不到候选被
    # 召回的依据，把正确候选误筛。私有键（`_score` / `_matches` / `_attr_hits`）
    # 由调用方（`concept_filter.filter_candidates`）原样送进来，此处按读数取值。
    candidates_payload: list[dict] = []
    for i, c in enumerate(candidates):
        # `_matches` 缺席（TERMS_V2=OFF 的 v1 候选没有它）→ 回落单个 `matched_term`
        # （level 未知记 null）。没有这条回落，回退路径上筛选看到的证据比改造前还少。
        matched = c.get("_matches") or (
            [{"term": c.get("matched_term"), "kind": c.get("match_kind"), "level": None}]
            if c.get("matched_term") else []
        )
        candidates_payload.append({
            "idx": i, "entity": c.get("entity_name"), "concept": c.get("concept"),
            # spec §3.4 字段列：跨库合并送筛时每个候选自报家门
            "kb": c.get("knowledge_base_id"),
            # 源名以 data_source_id 代（两个调用点均无名字上下文；spec §3.4 的「源名」降级为 id）
            "source": c.get("data_source_id"),
            "score": c.get("_score"),
            "matched_terms": [
                {"term": m.get("term"), "match": m.get("kind"), "level": m.get("level")}
                for m in matched
            ],
            "matched_attributes": c.get("_attr_hits") or [],
        })
    user_prompt = json.dumps(
        {"question": question, "candidates": candidates_payload},
        ensure_ascii=False,
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 抽取档恒禁思考（网关 LLMGW_DISABLE_THINKING_SCENES 白名单）：一次性结构化
        # JSON 输出，漏登记时故障是静默的（思考 token 吃光输出预算 → 截断成非法 JSON）。
        # 改名要同步改白名单（config.py 默认值 + .env 示例 ×2 + deploy/README）。
        "X-Jonex-Scene": "concept_relevance_filter",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    import uuid

    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"concept_filter:{uuid.uuid4().hex}"

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # 与兄弟函数同口径：temp=0，同一输入同一筛选
        temperature=0,
        # 输出只是下标数组；1024 留足余量（与「答案是短数组」的事实匹配）
        max_tokens=1024,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError("concept_relevance_filter 回包被截断（finish_reason=length）")
    content = (choice.message.content or "").strip()
    if not content:
        raise RuntimeError("concept_relevance_filter 回包为空")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"concept_relevance_filter 回包不是 JSON: {content[:200]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"concept_relevance_filter 回包不是对象: {content[:200]}")
    keep = payload.get("keep")
    if not isinstance(keep, list):
        raise RuntimeError(f"concept_relevance_filter 回包缺 keep 数组: {content[:200]}")

    out: list[int] = []
    for v in keep:
        if isinstance(v, bool) or not isinstance(v, int):
            continue
        if 0 <= v < len(candidates) and v not in out:
            out.append(v)
    if keep and not out:
        raise RuntimeError(f"concept_relevance_filter 的 keep 无一个可用下标: {content[:200]}")
    return out


async def suggest_concept_aliases(
    questions: list[str],
    concepts: list[dict],
    *,
    tenant_id: str | None = None,
    kb_id: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
) -> list[dict]:
    """失败问句 × 概念清单 → 别名候选 `[{"q", "concept", "alias"}]`（半自动闭环的草稿源）。

    坏答案抛（空正文 / 非 JSON / 回包非对象 / 缺 `suggestions` / 非数组 / 有元素但零条可用），
    与 `filter_relevant_concepts` / `propose_attribute_display_names` 同口径。
    **空列表是合法「想不出候选」**，与"没答出来"必须区分 —— 后者靠抛。
    被丢弃的脏条目：q 越界/非 int/bool、概念不在入参清单、别名空/超 32 字符、整条重复。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You propose aliases that map a user's question wording to an existing "
        "domain concept. You are given failing questions and the concept list "
        "(name, aliases, description).\n"
        "Rules:\n"
        "- `concept` MUST be exactly one name from the given concept list; never invent one.\n"
        "- `alias` is a short term (<= 32 chars) taken from / closely paraphrasing the "
        "question that refers to that concept — the word users say, not a sentence.\n"
        "- Skip a question if no concept clearly matches; skip an alias that already "
        "appears in that concept's aliases.\n"
        "- `q` is the 0-based index of the question in the input list.\n"
        'Output strict JSON: {"suggestions": [{"q": int, "concept": str, "alias": str}]}'
    )
    concept_names = {str(c.get("name") or "") for c in concepts or [] if isinstance(c, dict)}
    concepts_payload = [
        {"name": c.get("name"),
         "aliases": [str(a) for a in (c.get("aliases") or [])][:20],
         "description": str(c.get("description") or "")[:120]}
        for c in concepts or [] if isinstance(c, dict)
    ]
    user_prompt = json.dumps(
        {"questions": list(questions or [])[:20], "concepts": concepts_payload},
        ensure_ascii=False,
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 抽取档恒禁思考（网关 LLMGW_DISABLE_THINKING_SCENES 白名单）：一次性结构化
        # JSON 输出，漏登记时故障是静默的（思考 token 吃光输出预算 → 截断成非法 JSON）。
        # 改名要同步改白名单（config.py 默认值 + .env 示例 ×2 + deploy/README）。
        "X-Jonex-Scene": "nl2sql_alias_suggest",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    import uuid

    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"alias_suggest:{uuid.uuid4().hex}"

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        max_tokens=1024,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError("nl2sql_alias_suggest 回包被截断（finish_reason=length）")
    content = (choice.message.content or "").strip()
    if not content:
        raise RuntimeError("nl2sql_alias_suggest 回包为空")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"nl2sql_alias_suggest 回包不是 JSON: {content[:200]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"nl2sql_alias_suggest 回包不是对象: {content[:200]}")
    raw_items = payload.get("suggestions")
    if not isinstance(raw_items, list):
        raise RuntimeError(f"nl2sql_alias_suggest 回包缺 suggestions 数组: {content[:200]}")

    out: list[dict] = []
    seen: set[tuple] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        q = item.get("q")
        concept = str(item.get("concept") or "").strip()
        alias = str(item.get("alias") or "").strip()
        if isinstance(q, bool) or not isinstance(q, int) or not 0 <= q < len(questions or []):
            continue
        if concept not in concept_names or not alias or len(alias) > 32:
            continue
        if (q, concept, alias) in seen:
            continue
        seen.add((q, concept, alias))
        out.append({"q": q, "concept": concept, "alias": alias})
    if raw_items and not out:
        raise RuntimeError(f"nl2sql_alias_suggest 的 suggestions 无一条可用: {content[:200]}")
    return out


async def suggest_value_synonyms(
    questions: list[str],
    concept_names: list[str],
    *,
    tenant_id: str | None = None,
    kb_id: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
) -> list[dict]:
    """失败问句 × 实体/属性名 → 值同义词候选 `[{"terms": [...], "canonical": ...}]`。

    「值同义词」是 KB 级 `terms → canonical` 登记（people say A, the data stores B）。
    这里只让模型从**问句词面**里挑出疑似被数据值卡住的术语，并给出它认为的规范值；
    **不喂任何数据行**，模型也不知道库里真有哪些值 —— canonical 只是候选，必须经
    演化台现场验真后才会写库（Task 8）。

    坏答案抛（截断 / 空正文 / 非 JSON / 回包非对象 / 缺 `suggestions` / 非数组 /
    有元素但零条可用），与 `suggest_concept_aliases` 同口径。**空列表是合法
    「想不出候选」**，与「没答出来」必须区分 —— 后者靠抛。

    被丢弃的脏条目：非 dict、`terms` 非数组、terms 里空/非字符串项（逐项丢弃）、
    `canonical` 空。长度（≤32）与**跨条目 term 去重**在调用方
    （`builders.synonym_entries_from_suggestions`）做，以便进 `skipped` 让用户看见。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You propose VALUE synonyms: a short term that users type in their question, "
        "paired with the canonical value that is likely stored in the data. You are "
        "given failing questions and the knowledge base's entity / attribute names "
        "for domain context. You do NOT see any data rows.\n"
        "Rules:\n"
        "- `terms` are short wordings taken from / closely paraphrasing the questions "
        "(for example an organization, product or place name). Prefer the shortest "
        "unambiguous wording; <= 32 characters each.\n"
        "- `canonical` is the full value you believe the data stores (for example a "
        "company's full registered name). This is a CANDIDATE, not a verified fact.\n"
        "- Only propose a pair when the questions suggest a value-spelling mismatch. "
        "Skip questions where the failure is clearly about a missing column, a missing "
        "table or a permission problem.\n"
        "- Never invent facts that the questions and concept names do not support.\n"
        'Output strict JSON: {"suggestions": [{"terms": [str], "canonical": str}]}'
    )
    user_prompt = json.dumps(
        {"questions": list(questions or [])[:20],
         "concepts": [str(c) for c in (concept_names or [])][:200]},
        ensure_ascii=False,
    )

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 抽取档恒禁思考（网关 LLMGW_DISABLE_THINKING_SCENES 白名单）：一次性结构化
        # JSON 输出，漏登记时故障是静默的（思考 token 吃光输出预算 → 截断成非法 JSON）。
        # 改名要同步改白名单（config.py 默认值 + .env 示例 ×2 + deploy/README）。
        "X-Jonex-Scene": "nl2sql_value_synonym_suggest",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    import uuid

    extra_headers["X-Jonex-Trace-Id"] = (
        trace_id or f"value_synonym_suggest:{uuid.uuid4().hex}")

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        max_tokens=1024,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError("nl2sql_value_synonym_suggest 回包被截断（finish_reason=length）")
    content = (choice.message.content or "").strip()
    if not content:
        raise RuntimeError("nl2sql_value_synonym_suggest 回包为空")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"nl2sql_value_synonym_suggest 回包不是 JSON: {content[:200]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"nl2sql_value_synonym_suggest 回包不是对象: {content[:200]}")
    raw_items = payload.get("suggestions")
    if not isinstance(raw_items, list):
        raise RuntimeError(
            f"nl2sql_value_synonym_suggest 回包缺 suggestions 数组: {content[:200]}")

    out: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_terms = item.get("terms")
        if not isinstance(raw_terms, list):
            continue
        terms: list[str] = []
        seen: set[str] = set()
        for raw in raw_terms:
            if not isinstance(raw, str):
                continue
            term = raw.strip()
            key = term.casefold()
            if term and key not in seen:
                seen.add(key)
                terms.append(term)
        raw_canonical = item.get("canonical")
        canonical = (raw_canonical.strip()
                     if isinstance(raw_canonical, str) else "")
        if not terms or not canonical:
            continue
        out.append({"terms": terms, "canonical": canonical})
    if raw_items and not out:
        raise RuntimeError(
            f"nl2sql_value_synonym_suggest 的 suggestions 无一条可用: {content[:200]}")
    return out


async def revise_evolution_entry(
    messages: list[dict],
    *,
    tenant_id: str | None = None,
    kb_id: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
) -> dict:
    """演化条目「人工调整」一轮：messages（CLI 侧 `build_revise_messages` 组装，system 已含
    修订原则与条目现状）→ `{"revised": str, "rationale": str}`。

    坏答案抛（截断 / 空正文 / 非 JSON / 回包非对象 / 缺 revised 或空串）——与
    `suggest_concept_aliases` 同口径：**不静默降级**。rationale 非字符串时回落空串（理由缺省宽松）。
    """
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")
    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 一轮修订恒禁思考（网关 LLMGW_DISABLE_THINKING_SCENES 白名单，config.py 默认值）：
        # 一次性结构化 JSON 输出，漏登记时故障是**静默的** —— 思考 token 吃光输出预算 →
        # 回包截断成非法 JSON，弹窗只看到「修订失败」，看不出根因在网关配置。
        # 改名要同步改白名单（config.py 默认值 + .env 示例 ×2 + deploy/README）。
        "X-Jonex-Scene": "nl2sql_entry_revise",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    import uuid

    extra_headers["X-Jonex-Trace-Id"] = trace_id or f"entry_revise:{uuid.uuid4().hex}"

    resp = await client.chat.completions.create(
        model=model,
        messages=list(messages or []),
        temperature=0,
        max_tokens=1024,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError("nl2sql_entry_revise 回包被截断（finish_reason=length）")
    content = (choice.message.content or "").strip()
    if not content:
        raise RuntimeError("nl2sql_entry_revise 回包为空")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"nl2sql_entry_revise 回包不是 JSON: {content[:200]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"nl2sql_entry_revise 回包不是对象: {content[:200]}")
    revised = payload.get("revised")
    if not isinstance(revised, str) or not revised.strip():
        raise RuntimeError(f"nl2sql_entry_revise 回包缺 revised: {content[:200]}")
    rationale = payload.get("rationale")
    return {"revised": revised.strip(),
            "rationale": rationale.strip() if isinstance(rationale, str) else ""}


# ══════════════════════════════════════════════════════════════
# 文档本体 ↔ 结构化源：LLM 自动落点对齐
# ══════════════════════════════════════════════════════════════

LANDING_ALIGN_SCENE = "abstract_schema_landing_align"
LANDING_ALIGN_BATCH_SIZE = 20


async def align_document_landings(
    document_entities: list[dict],
    source_entities: list[dict],
    *,
    batch_size: int = LANDING_ALIGN_BATCH_SIZE,
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> list[dict]:
    """让 LLM 把文档本体实体/属性对齐到结构化源的实体/列（自动落点）。

    Args:
        document_entities: `[{"name","display_name","description","aliases",
            "attributes":[{"name","display_name","description","type"}]}]`
        source_entities: `[{"data_source_id","source_name","entity_name",
            "description","attributes":[{"name","description","type"}]}]`

    Returns:
        `[{"entity": str, "mappings": [{"data_source_id": str,
        "source_entity": str, "attributes": [{"attribute": str,
        "source_attribute": str}]}]}]`。

        **不保证返回的实体/列真实存在**：模型可能拼错或编造。调用方必须按
        `(data_source_id, source_entity)` 与源列白名单过滤后再落库。

    Raises:
        RuntimeError: 空正文 / 没有 alignments 数组 / 输出被 max_tokens 截断。
            空数组是合法结果（模型认为没有足够把握的映射），不抛。
    """
    if not document_entities or not source_entities:
        return []
    size = max(1, int(batch_size or LANDING_ALIGN_BATCH_SIZE))
    alignments: list[dict] = []
    for start in range(0, len(document_entities), size):
        alignments.extend(
            await _align_landings_batch(
                document_entities[start:start + size],
                source_entities,
                tenant_id=tenant_id,
                kb_id=kb_id,
                user_id=user_id,
                trace_id=trace_id,
            )
        )
    return alignments


async def _align_landings_batch(
    document_entities: list[dict],
    source_entities: list[dict],
    *,
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> list[dict]:
    client = _get_client()
    model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

    system_prompt = (
        "You are an ontology landing alignment assistant. You are given entity "
        "types extracted from documents (canonical document ontology) and entity "
        "types extracted from structured data sources (tables) of the same "
        "knowledge base. Align each document entity to the structured source "
        "entities that denote THE SAME real-world concept, and align its "
        "attributes to semantically equivalent source columns.\n"
        "Rules:\n"
        "- A document entity may map to zero, one, or several source entities "
        "(the same concept can live in multiple data sources).\n"
        "- Naming style alone does NOT make two entities different: singular vs "
        "plural (`Company` / `Companies`), `snake_case` vs `PascalCase`, and "
        "`Sys` / `Tb` / `T_` prefixes are the same concept when semantics match.\n"
        "- Only map attributes when they denote the same field. Do NOT force a "
        "mapping: omit uncertain entities/attributes entirely rather than guess.\n"
        "- Every `data_source_id`, `source_entity`, `entity`, `attribute` and "
        "`source_attribute` MUST reuse the exact strings given in the input. "
        "Never paraphrase, translate, or invent them.\n"
        "- `attributes` may be an empty list when only the entity matches.\n"
        'Output strict JSON: {"alignments": [{"entity": str, "mappings": '
        '[{"data_source_id": str, "source_entity": str, "attributes": '
        '[{"attribute": str, "source_attribute": str}]}]}]}'
    )
    user_prompt = json.dumps(
        {"document_entities": document_entities, "source_entities": source_entities},
        ensure_ascii=False,
    )

    import uuid

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        # 恒禁思考（网关 LLMGW_DISABLE_THINKING_SCENES 白名单）：输出规模与输入
        # 实体数同阶，思考 token 会先吃光输出预算。**改名要同步改白名单**。
        "X-Jonex-Scene": LANDING_ALIGN_SCENE,
        "X-Jonex-Trace-Id": trace_id or f"abstract_schema_landing:{uuid.uuid4().hex}",
    }
    if kb_id:
        extra_headers["X-Jonex-Kb-Id"] = kb_id
    if user_id:
        extra_headers["X-Jonex-User-Id"] = user_id

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        max_tokens=16384,
        response_format={"type": "json_object"},
        extra_headers=extra_headers,
    )
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError(
            f"自动落点对齐输出被截断（finish_reason=length）：本批文档实体 "
            f"{len(document_entities)} 个，超出单次调用的输出预算（max_tokens=16384）。"
            "请减少参与对齐的实体后重试。"
        )
    raw = (choice.message.content or "").strip()
    if not raw:
        raise RuntimeError("自动落点对齐返回空内容")
    data = json.loads(raw)
    alignments = data.get("alignments") if isinstance(data, dict) else None
    if not isinstance(alignments, list):
        raise RuntimeError("自动落点对齐返回的 JSON 里没有 alignments 数组")
    return alignments
