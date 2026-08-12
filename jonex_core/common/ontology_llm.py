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


async def answer_from_facts(
    query: str,
    hits: list[dict],
    facts: list[dict],
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    allow_common_sense: bool = False,
) -> str:
    """根据本体实体 + 邻居事实回答，不足返回 "INSUFFICIENT"。

    Args:
        allow_common_sense: [jonex] P2-7 放宽常识边界。
            为 True 时允许模型在已取到确定领域事实的基础上，叠加通用常识/数学
            （物理常量、semver 语义、单位换算），但必须标注假设。
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
        "X-Jonex-Scene": "ontology_qa",
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
            max_tokens=2048,
            extra_headers=extra_headers,
        )
        content = resp.choices[0].message.content
        if not content or not content.strip():
            logger.warning(
                "本体 LLM 返回空内容 finish_reason=%s",
                resp.choices[0].finish_reason,
            )
            return "INSUFFICIENT"
        return content.strip()
    except Exception as e:
        logger.warning("本体 LLM 调用失败: %s", e)
        return "INSUFFICIENT"


async def fuse_rag_answers(
    query: str,
    per_kb_answers: list[dict],
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
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
        "answer. Resolve factual conflicts. When possible, note which knowledge base each "
        "piece of information comes from. Do not make up information."
    )

    import json, uuid
    kb_texts = json.dumps(per_kb_answers, ensure_ascii=False)

    extra_headers = {
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": "rag_fusion",
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
            max_tokens=1000,
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