"""Search service for Knowledge Base."""

import asyncio
import hashlib
import logging
import os
import re
import time
from typing import Any

from jonex_core.capability.atomic.rag.client import get_rag_client
from .openkb_service import KnowledgeCompilerService  # [jonex]
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.file_source_util import classify_media, parse_file_source, to_location
from jonex_core.common.neo4j_client import get_neo4j_driver
from jonex_core.common.object_storage import build_object_key, get_object_storage
from jonex_core.common.ontology_embedding import embed
from jonex_core.common.ontology_llm import answer_from_facts, fuse_rag_answers
from jonex_core.common.tenant import require_tenant

from ..dtos import LlmWikiSearchRequest, MixSearchRequest, DeepSearchRequest, DeepSearchResponse, OntologySearchRequest, ReliabilityInfo, SearchHistoryCreateRequest, SearchRequest
from ..dtos.reasoning import (
    STAGE_FACT_LOOKUP,
    STAGE_FUSION,
    STAGE_INTENT_CLASSIFY,
    STAGE_LLM_ANSWER,
    STAGE_ONTOLOGY_MATCH,
    STAGE_OPENKB_QUERY,
    STAGE_QUERY_PLAN,
    STAGE_RAG_FALLBACK,
    STAGE_REF_RETRIEVE,
    STAGE_RERANK,
    STAGE_RETRIEVAL_RERANK,
    STAGE_ROUTE_DECISION,
    STAGE_STRICT_ATTEMPT,
    STAGE_STRICT_VERIFY,
    STAGE_SUBQUERY,
    STAGE_SYNTHESIS,
    STAGE_TIMELINE_GRAPH,
)
from ..repository import OntologyGraphRepository
from ..repository.document_repository import KnowledgeDocumentRepository
from ..repository.knowledge_info_repository import KnowledgeInfoRepository
from .document_service import _payload
from .reasoning_trace import ReasoningCollector
from .search_history_service import SearchHistoryService

logger = logging.getLogger(__name__)

ONTOLOGY_ROUTE_SCORE_MIN = float(os.getenv("ONTOLOGY_ROUTE_SCORE_MIN", "1.0"))
ONTOLOGY_VECTOR_SCORE_MIN = float(os.getenv("ONTOLOGY_VECTOR_SCORE_MIN", "0.75"))
_ONTOLOGY_VECTOR_ENABLED = os.getenv("ONTOLOGY_VECTOR_ENABLED", "true").lower() in ("1", "true", "yes", "on")

# RRF 融合参数
_RRF_K = int(os.getenv("ONTOLOGY_RRF_K", "60"))
_RRF_W_FT = float(os.getenv("ONTOLOGY_RRF_W_FT", "1.0"))
_RRF_W_VEC = float(os.getenv("ONTOLOGY_RRF_W_VEC", "1.0"))

# 与 LightRAG QueryRequest.query 的 min_length=3 对齐：短于该长度 LightRAG 必返回 422。
# 可经 RAG_MIN_QUERY_LEN 调整客户端拦截阈值，但下限恒为 3（LightRAG 硬约束，配更小无意义）。
RAG_MIN_QUERY_LEN = max(3, int(os.getenv("RAG_MIN_QUERY_LEN", "3")))

# 多 KB 本体查询上限（单次最多查 20 个知识库）
MAX_KB_PER_QUERY = 20

# RAG fallback 路径最大引用文档数（按 chunk 命中数降序取 top-N，过滤噪声文档）
RAG_FALLBACK_MAX_REFS = max(1, int(os.getenv("ONTOLOGY_RAG_FALLBACK_MAX_REFS", "5")))

# RAG fallback 引用重排开关（方案 B2）：开启后用 reranker 按相关性排序，
# 失败/关闭时兜底回退 len(locations) 频次排序。默认关闭（灰度）。
RAG_FALLBACK_RERANK_ENABLED = os.getenv(
    "ONTOLOGY_RAG_FALLBACK_RERANK_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")

# LightRAG 检索期 rerank 配置态（召回后、送 LLM 前，在 LightRAG 内部执行，平台不感知
# 每次调用）。此标志仅供 reasoning 展示，须与 deploy/.env.rag 的 RERANK_BINDING 保持一致：
# 当 .env.rag 设 RERANK_BINDING=cohere（指向 gateway /v1/rerank）时，这里应设 true。
RAG_RETRIEVAL_RERANK_ENABLED = os.getenv(
    "RAG_RETRIEVAL_RERANK_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")

# RAG fallback 召回明细配置
ONTOLOGY_RAG_RECALL_DETAIL_ENABLED = os.getenv(
    "ONTOLOGY_RAG_RECALL_DETAIL_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")
ONTOLOGY_RAG_RECALL_MAX_ITEMS = max(1, int(os.getenv("ONTOLOGY_RAG_RECALL_MAX_ITEMS", "20")))
ONTOLOGY_RAG_RECALL_TEXT_MAX = max(1, int(os.getenv("ONTOLOGY_RAG_RECALL_TEXT_MAX", "200")))

# 编排推理链进程级总闸（前端再用 with_reasoning 按请求控制）
_REASONING_ENABLED = os.getenv("REASONING_TRACE_ENABLED", "true").lower() in ("1", "true", "yes")

# 查询 embedding TTL 缓存（相同 query 在 TTL 内不重复调 embedding API）
_query_embedding_cache: dict[str, tuple[float, list[float]]] = {}
_QUERY_EMBED_CACHE_TTL = int(os.getenv("ONTOLOGY_QUERY_EMBED_TTL", "300"))
_QUERY_EMBED_CACHE_MAX = int(os.getenv("ONTOLOGY_QUERY_EMBED_CACHE_MAX", "500"))

# 向量召回 embedding 的阶段级超时（秒）：即便 embedding 客户端自身超时/重试失效，
# 也用 asyncio.wait_for 给本体匹配（ontology_match）向量分支一个硬上界，
# 超时即降级为「仅全文召回」，不阻塞整条搜索链路。
_ONTOLOGY_EMBED_TIMEOUT = float(os.getenv("ONTOLOGY_EMBED_TIMEOUT", "5"))

# ── 本体多跳邻域召回配置 ──
ONTOLOGY_NEIGHBOR_DEPTH_MAX = max(1, int(os.getenv("ONTOLOGY_NEIGHBOR_DEPTH_MAX", "3")))
ONTOLOGY_NEIGHBOR_DEPTH = max(1, min(
    int(os.getenv("ONTOLOGY_NEIGHBOR_DEPTH", "1")), ONTOLOGY_NEIGHBOR_DEPTH_MAX))
ONTOLOGY_NEIGHBOR_LIMIT = max(1, int(os.getenv("ONTOLOGY_NEIGHBOR_LIMIT", "20")))
ONTOLOGY_NEIGHBOR_PER_HOP_LIMIT = max(1, int(os.getenv("ONTOLOGY_NEIGHBOR_PER_HOP_LIMIT", "50")))
ONTOLOGY_NEIGHBOR_TIMEOUT = max(1, int(os.getenv("ONTOLOGY_NEIGHBOR_TIMEOUT", "15")))

# ── 方案④：本体作答超时（INSUFFICIENT 快速短路）──
ONTOLOGY_ANSWER_TIMEOUT = max(5, int(os.getenv("ONTOLOGY_ANSWER_TIMEOUT", "30")))
# 方案④b：事实量预判阈值 — facts 数量低于此值时直接跳过本体作答进入 RAG
ONTOLOGY_MIN_FACTS = max(0, int(os.getenv("ONTOLOGY_MIN_FACTS", "0")))

# ── 方案②：融合降本 — 融合宽度上限（2a）──
ONTOLOGY_RAG_FUSION_TOPN = max(1, int(os.getenv("ONTOLOGY_RAG_FUSION_TOPN", "3")))

# ── 方案③：并发限流 — 多 KB 并行查询信号量上限（3a）──
ONTOLOGY_RAG_MAX_CONCURRENCY = max(1, int(os.getenv("ONTOLOGY_RAG_MAX_CONCURRENCY", "7")))

# ── [jonex] OpenKB 多 KB 检索并发上限（D7）──
OPENKB_SEARCH_MAX_CONCURRENCY = max(1, int(os.getenv("OPENKB_SEARCH_MAX_CONCURRENCY", "3")))

# ── [jonex] OpenKB references 不依赖 with_reasoning（D11）──
OPENKB_REFERENCES_ENABLED = os.getenv("OPENKB_REFERENCES_ENABLED", "true").lower() in (
    "1", "true", "yes", "on",
)

# ── [jonex] OpenKB trace 截断上限（D8）──
OPENKB_TRACE_MAX_CALLS = max(1, int(os.getenv("OPENKB_TRACE_MAX_CALLS", "80")))
# ── §10 深度查询配置 ──
_DEEP_QUERY_ENABLED = os.getenv("DEEP_QUERY_ENABLED", "false").lower() in ("1", "true", "yes", "on")
_DEEP_MAX_SUBQUERIES = max(1, min(10, int(os.getenv("DEEP_MAX_SUBQUERIES", "6"))))
_DEEP_SUBQUERY_CONCURRENCY = max(1, min(10, int(os.getenv("DEEP_SUBQUERY_CONCURRENCY", "5"))))
_DEEP_TOTAL_BUDGET = float(os.getenv("DEEP_TOTAL_BUDGET", "180"))

# ── §9 严格模式配置 ──
_STRICT_MODE_ENABLED = os.getenv("STRICT_MODE_ENABLED", "false").lower() in ("1", "true", "yes", "on")
_STRICT_MAX_ATTEMPTS_CAP = max(1, min(3, int(os.getenv("STRICT_MAX_ATTEMPTS_CAP", "3"))))
_STRICT_MIN_SCORE = float(os.getenv("STRICT_MIN_SCORE", "0.8"))
_STRICT_ATTEMPT_TIMEOUT = float(os.getenv("STRICT_ATTEMPT_TIMEOUT", "60"))
_STRICT_TOTAL_BUDGET = float(os.getenv("STRICT_TOTAL_BUDGET", "150"))
# 可靠性分权重
_STRICT_W_NON_REFUSAL = float(os.getenv("STRICT_WEIGHT_NON_REFUSAL", "0.35"))
_STRICT_W_REFERENCE = float(os.getenv("STRICT_WEIGHT_REFERENCE", "0.15"))
_STRICT_W_GROUNDED = float(os.getenv("STRICT_WEIGHT_GROUNDED", "0.30"))
_STRICT_W_CONSISTENCY = float(os.getenv("STRICT_WEIGHT_CONSISTENCY", "0.20"))

# 拒答模板检测（正则）
_REFUSAL_PATTERNS = [
    re.compile(r"无法确定|无法回答|cannot\s+determine|cannot\s+answer", re.IGNORECASE),
    re.compile(r"not\s+enough\s+information|does\s+not\s+contain|没有.*信息|不包含", re.IGNORECASE),
    re.compile(r"^INSUFFICIENT$", re.MULTILINE),
    re.compile(r"没有.*提及|未.*提及|未.*提供|not\s+mentioned|not\s+provided", re.IGNORECASE),
]

# 升级档位（可配，默认 3 档）
_STRICT_ESCALATION: list[dict] = [
    {"top_k": 5, "neighbor_depth": 3, "route_score_min": 1.0, "label": "基线"},
    {"top_k": 15, "neighbor_depth": 3, "route_score_min": 0.7, "label": "加召回"},
    {"top_k": 25, "neighbor_depth": 3, "route_score_min": 0.5, "label": "全量"},
]

# ── P1-5 RAG 召回后处理配置 ──
# 送 LLM 融合前的 chunk 级重排（经 llm-gateway /v1/rerank，与 LightRAG 的 RERANK_BINDING 同源）
RAG_PRELLM_RERANK_ENABLED = os.getenv(
    "RAG_PRELLM_RERANK_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
RAG_PRELLM_RERANK_TOPK = max(1, int(os.getenv("RAG_PRELLM_RERANK_TOPK", "8")))
# 主体一致性过滤：从 query 抽取主体实体，对 doc_id/file_name 做一致性检查
RAG_SUBJECT_FILTER_ENABLED = os.getenv(
    "RAG_SUBJECT_FILTER_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")

# ── P1-6 图查询模板：时间线/枚举/计数意图检测 ──
_TIMELINE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("find_release_for_toolkit", re.compile(r"哪个版本.*(?:支持|适配).*(\d+\.\d+)")),
    ("find_release_for_toolkit", re.compile(r"what\s+(?:version|release).*(?:support|work).*(\d+\.\d+)", re.IGNORECASE)),
    ("earliest", re.compile(r"最早|earliest|first\s+(?:version|release|支持)")),
    ("latest", re.compile(r"最后|latest|last\s+(?:version|release|支持)")),
    ("first_added", re.compile(r"首次(?:支持|加入)|first\s+(?:added|支持|加入)")),
    ("count_versions", re.compile(r"共几个|how\s+many\s+(?:versions|版本)|几个版本", re.IGNORECASE)),
    ("release_year", re.compile(r"哪一年|what\s+year|release\s+year|发布年份|release year", re.IGNORECASE)),
    ("last_before_drop", re.compile(r"最后(?:一个)?支持.*的版本|last\s+(?:version|release).*(?:support|before.*dropped)", re.IGNORECASE)),
]

# 版本号抽取：从 query 里取数字版本号（如 CUDA 12.8 → 12.8）
_VERSION_RE = re.compile(r"\b(\d+\.\d+)\b")


def _evict_stale_entries() -> None:
    """淘汰过期条目；超容量时按时间戳淘汰最旧条目。"""
    now = time.time()
    stale = [k for k, v in _query_embedding_cache.items() if now - v[0] >= _QUERY_EMBED_CACHE_TTL]
    for k in stale:
        del _query_embedding_cache[k]
    over = len(_query_embedding_cache) - _QUERY_EMBED_CACHE_MAX
    if over > 0:
        oldest = sorted(_query_embedding_cache.items(), key=lambda x: x[1][0])[:over]
        for k, _ in oldest:
            del _query_embedding_cache[k]


async def _embed_query_cached(query: str, tenant_id: str) -> list[float] | None:
    key = hashlib.sha256(query.encode("utf-8")).hexdigest()
    now = time.time()
    hit = _query_embedding_cache.get(key)
    if hit and now - hit[0] < _QUERY_EMBED_CACHE_TTL:
        return hit[1]
    vec = await embed(query, tenant_id=tenant_id)
    if vec is not None:
        _query_embedding_cache[key] = (now, vec)
        if len(_query_embedding_cache) > _QUERY_EMBED_CACHE_MAX * 1.2:
            _evict_stale_entries()
    return vec


def _fuse_hits(fulltext_hits: list[dict], vector_hits: list[dict]) -> list[dict]:
    """RRF 融合全文（BM25）与向量（余弦）结果，保留原始分供路由决策。"""
    merged: dict[tuple, dict] = {}

    for rank_idx, hit in enumerate(fulltext_hits):
        key = (hit.get("type", ""), hit.get("name", ""), hit.get("kb_id", ""))
        rrf = _RRF_W_FT / (_RRF_K + rank_idx + 1)
        merged[key] = {**hit, "fused_score": rrf, "ft_score": hit.get("score", 0), "vscore": 0.0}

    for rank_idx, hit in enumerate(vector_hits):
        key = (hit.get("type", ""), hit.get("name", ""), hit.get("kb_id", ""))
        rrf = _RRF_W_VEC / (_RRF_K + rank_idx + 1)
        vscore = hit.get("vscore", hit.get("score", 0))
        if key in merged:
            merged[key]["fused_score"] = merged[key]["fused_score"] + rrf
            merged[key]["vscore"] = vscore
        else:
            merged[key] = {**hit, "fused_score": rrf, "ft_score": 0.0, "vscore": vscore}

    fused = sorted(merged.values(), key=lambda x: x.get("fused_score", 0), reverse=True)
    return fused


def _preprocess_query(query: str) -> str:
    """转义 Lucene 特殊字符，保留原始查询文本。

    不再使用 jieba 分词 + OR 拼接：cjk analyzer 自动做中文 bigram 切分，
    查询端 analyzer 与索引端 analyzer 一致，无需前端分词桥接。
    """
    if not query.strip():
        return query
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', query)


class SearchService:
    def __init__(self):
        self._history = SearchHistoryService()

    async def search(self, tenant_id: str, user_id: str, request: SearchRequest | dict, trace_id: str = "") -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchRequest(**_payload(request))
        start = time.perf_counter()

        # ── [jonex] kb_type 分流 ──
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        if kb_type == "openkb":
            return await self._search_openkb(tenant_id, user_id, req)

        detailed = await get_rag_client().query_detailed(
            query=req.query,
            tenant_id=tenant_id,
            mode=req.mode,
            top_k=req.top_k,
            knowledge_base_id=req.knowledge_base_id,
            trace_id=trace_id,          # [jonex] 计量链路追踪
            user_id=user_id,            # [jonex] Gap B3: 透传 user 维度
        )
        answer = detailed.get("answer", "")
        raw_refs = detailed.get("references", [])
        duration_ms = int((time.perf_counter() - start) * 1000)
        references = await self._build_references(
            tenant_id, raw_refs, allowed_kb_ids=[req.knowledge_base_id],
        )
        result = {
            "query": req.query,
            "answer": answer,
            "mode": req.mode,
            "top_k": req.top_k,
            "references": references,
            "metadata": {
                "knowledge_base_id": req.knowledge_base_id,
                "duration_ms": duration_ms,
            },
        }
        if req.save_history:
            await self._history.save_history(
                tenant_id,
                user_id,
                SearchHistoryCreateRequest(
                    query=req.query,
                    knowledge_base_id=req.knowledge_base_id,
                    mode=req.mode,
                    top_k=req.top_k,
                    domain_space_id=req.domain_space_id,
                    answer_preview=answer[:300],
                    duration_ms=duration_ms,
                ),
            )
        return result

    async def enhanced_search(
        self,
        tenant_id: str,
        user_id: str,
        request: SearchRequest | dict,
        trace_id: str = "",          # [jonex] 计量链路追踪
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchRequest(**_payload(request))
        base = await self.search(tenant_id, user_id, req, trace_id=trace_id)
        rag = get_rag_client()
        graph = await rag.get_storage_graph(
            knowledge_base_id=req.knowledge_base_id,
            tenant_id=tenant_id,
            limit=100,
            keyword=req.query,
        )
        entities = graph.get("entities") or graph.get("nodes") or []
        relationships = graph.get("relationships") or graph.get("edges") or []
        return {
            **base,
            "entities": entities,
            "relationships": relationships,
            "graph": graph,
        }

    async def _resolve_kb_ids(self, tenant_id: str, req: OntologySearchRequest) -> list[str]:
        """去重保序 + 数量校验 + 逐 KB 归属校验。"""
        kb_ids = list(dict.fromkeys(k.strip() for k in req.knowledge_base_ids if k and k.strip()))
        if not kb_ids:
            raise InvalidParameterError(message=translate("err.search.kb_required", fallback="请至少指定一个知识库（knowledge_base_ids 不能为空）")  )  # 原消息)
        if len(kb_ids) > MAX_KB_PER_QUERY:
            raise InvalidParameterError(
                message=translate("err.search.max_kb_exceeded", params={"max": str(MAX_KB_PER_QUERY), "n": str(len(kb_ids))}, fallback=f"单次查询最多支持 {MAX_KB_PER_QUERY} 个知识库，当前传入 {len(kb_ids)} 个，请减少后重试")  # 原消息
            )
        await self._assert_kb_ownership(tenant_id, kb_ids)
        return kb_ids

    async def _assert_kb_ownership(self, tenant_id: str, kb_ids: list[str]) -> None:
        """每个 KB 必须属于当前租户（D6 权限模型）。"""
        async with get_db_session() as session:
            repo = KnowledgeInfoRepository(session)
            for kb_id in kb_ids:
                kb = await repo.get_by_id(kb_id, tenant_id)
                if kb is None:
                    raise ResourceNotFoundError(
                        message=translate("err.kb.not_found_or_tenant", params={"kb_id": kb_id}, fallback=f"知识库 {kb_id} 不存在或不属于当前租户")  # 原消息
                    )

    async def _build_references(
        self, tenant_id: str, raw_refs: list[dict],
        allowed_kb_ids: list[str] | None = None,
        doc_map: dict[str, Any] | None = None,
    ) -> list[dict]:
        """从 RAG 返回的原始引用片段富化出完整的 references。

        D5（richification in service）+ D6（doc_id 聚合）+ D8（租户过滤）。
        若对象存储可用则生成预签名 URL，否则 raw_url 为 None。

        allowed_kb_ids：纵深防御——仅保留属于本次请求知识库的文档。即便底层
        LightRAG 检索因 workspace 漏配等原因串库，也不会把库外文档泄露给前端
        （None 表示不做 KB 过滤，兼容历史单库调用）。

        doc_map：可选，调用方预查好的 doc 实体映射 {doc_id: KnowledgeDocument}。
        None 时内部自查。
        """
        doc_ids = [r["doc_id"] for r in raw_refs if r.get("doc_id")]
        if not doc_ids:
            return []

        if doc_map is None:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                docs = await repo.get_by_ids(doc_ids, tenant_id)
            doc_map = {d.id: d for d in docs}
        if allowed_kb_ids is not None:
            allowed = set(allowed_kb_ids)
            cross_kb = {
                did for did, d in doc_map.items()
                if d.knowledge_base_id not in allowed
            }
            if cross_kb:
                logger.warning(
                    "references 富化: %d 个 doc_id 命中库外知识库被剔除（跨库防御）: %s",
                    len(cross_kb), list(cross_kb)[:10],
                )
            doc_map = {
                did: d for did, d in doc_map.items()
                if d.knowledge_base_id in allowed
            }
        filtered = len(set(doc_ids)) - len(doc_map)
        if filtered:
            logger.info(
                "references 富化: %d refs 输入, %d 个 doc_id 被剔除（越权/不存在/库外）",
                len(doc_ids), filtered,
            )

        agg: dict[str, dict] = {}
        for r in raw_refs:
            did = r.get("doc_id")
            if not did or did not in doc_map:
                continue
            ref = agg.setdefault(did, {"doc_id": did, "locations": []})
            ref["locations"].append(to_location(r))

        storage = get_object_storage()
        out = []
        for did, ref in agg.items():
            d = doc_map[did]
            raw_url = None
            if d.storage_key:
                try:
                    raw_url = await storage.presigned_url(d.storage_key, tenant_id)
                except Exception:
                    pass
            out.append({
                "doc_id": did,
                "kb_id": d.knowledge_base_id,
                "file_name": d.file_name,
                "mime_type": d.mime_type,
                "file_size": d.file_size,
                "media_type": classify_media(d.mime_type, d.file_name),
                "raw_url": raw_url,
                "locations": ref["locations"],
            })
        return out

    async def _build_references_by_doc_ids(
        self, tenant_id: str, doc_ids: list[str],
        allowed_kb_ids: list[str] | None = None,
    ) -> list[dict]:
        """文档级 references 富化（本体路径，无 chunk/位置）。

        D8：校验租户归属，越权/不存在自动剔除。
        allowed_kb_ids：纵深防御，仅保留属于本次请求知识库的文档（None 不过滤）。
        """
        deduped = list(dict.fromkeys(d for d in doc_ids if d))
        if not deduped:
            return []
        async with get_db_session() as session:
            docs = await KnowledgeDocumentRepository(session).get_by_ids(deduped, tenant_id)

        if allowed_kb_ids is not None:
            allowed = set(allowed_kb_ids)
            docs = [d for d in docs if d.knowledge_base_id in allowed]

        storage = get_object_storage()
        out = []
        for d in docs:
            raw_url = None
            if d.storage_key:
                try:
                    raw_url = await storage.presigned_url(d.storage_key, tenant_id)
                except Exception:
                    pass
            out.append({
                "doc_id": d.id,
                "kb_id": d.knowledge_base_id,
                "file_name": d.file_name,
                "mime_type": d.mime_type,
                "file_size": d.file_size,
                "media_type": classify_media(d.mime_type, d.file_name),
                "raw_url": raw_url,
                "locations": [{"type": "document"}],
            })
        return out

    async def resolve_references(
        self, tenant_id: str, doc_ids: list[str] | None = None, refs: list[dict] | None = None,
    ) -> list[dict]:
        """引用富化端点（流式 gateway 解析出 doc_ids 后调用此方法）。

        D7：参见 resolve endpoint。支持两种输入：
        - doc_ids：文档级（位置退化为 document）
        - refs：保留位置信息（流式解析出的 ParsedRef 格式）
        """
        if refs:
            return await self._build_references(tenant_id, refs)
        if doc_ids:
            return await self._build_references_by_doc_ids(tenant_id, doc_ids)
        return []

    async def _match_ontology(
        self, gdao: OntologyGraphRepository, tenant_id: str, kb_ids: list[str], query: str,
    ) -> list[dict]:
        """四级递进实体匹配（精确 → 前缀 → 全文+向量并行 → RRF 融合），跨 KB 合并后按 fused_score 降序。"""
        instances: list[dict] = []

        # 1a) 精确匹配旁路（canonical_name 或 alias 完全相等）
        exact = await gdao.exact_match_entities(tenant_id, kb_ids, query)
        if exact:
            for r in exact:
                r["source"] = "exact"
            instances = exact
            logger.info(
                "[ontology] 匹配命中 stage=exact query=%r kb_ids=%s hits=%d",
                query, kb_ids, len(exact),
            )
            return instances

        # 1b) 前缀匹配（"研发"→"研发流程"）
        prefix = await gdao.prefix_match_entities(tenant_id, kb_ids, query, limit=3)
        if prefix:
            for r in prefix:
                r["source"] = "prefix"
            instances = prefix
            logger.info(
                "[ontology] 匹配命中 stage=prefix query=%r kb_ids=%s hits=%d names=%s",
                query, kb_ids, len(prefix), [r.get("name") for r in prefix],
            )
            return instances

        # 1c) 全文 + 1d) 向量：二者无依赖，asyncio.gather 并行
        processed = _preprocess_query(query)

        async def _ft():
            return await gdao.search_entities(tenant_id, kb_ids, processed, limit=10)

        async def _vec():
            if not _ONTOLOGY_VECTOR_ENABLED:
                return []
            try:
                qvec = await asyncio.wait_for(
                    _embed_query_cached(query, tenant_id),
                    timeout=_ONTOLOGY_EMBED_TIMEOUT,
                )
                if qvec is None:
                    return []
                return await gdao.vector_search_entities(tenant_id, kb_ids, qvec, limit=10)
            except asyncio.TimeoutError:
                logger.warning(
                    "[ontology] 向量召回 embedding 超时（%.1fs），降级仅全文 query=%r",
                    _ONTOLOGY_EMBED_TIMEOUT, query,
                )
                return []
            except Exception as e:
                logger.warning("[ontology] 向量召回失败，仅用全文: %s", e)
                return []

        fulltext_hits, vector_hits = await asyncio.gather(_ft(), _vec())

        # 1e) RRF 融合（保留 vscore / ft_score 供路由用）
        instances = _fuse_hits(fulltext_hits, vector_hits)

        if instances:
            logger.info(
                "[ontology] 匹配 stage=hybrid query=%r kb_ids=%s hits=%d "
                "ft_hits=%d vec_hits=%d top_fused=%.4f top_vscore=%.4f top_ftscore=%s",
                query, kb_ids, len(instances), len(fulltext_hits), len(vector_hits),
                instances[0].get("fused_score", 0),
                instances[0].get("vscore", 0),
                instances[0].get("ft_score", 0),
            )
        else:
            logger.info(
                "[ontology] 四级匹配（exact/prefix/fulltext/vector）均未命中 query=%r kb_ids=%s",
                query, kb_ids,
            )
        return instances

    @staticmethod
    def _detect_timeline_intent(query: str) -> dict | None:
        """检测时间线/枚举/计数意图。

        Returns:
            {"intent": str, "params": dict} | None
        """
        if not query:
            return None
        versions = _VERSION_RE.findall(query)
        for intent_key, pattern in _TIMELINE_PATTERNS:
            m = pattern.search(query)
            if m:
                params: dict = {}
                # 提取版本号（如有）
                if versions:
                    params["version"] = versions[0]
                    if len(versions) >= 2:
                        params["version_lo"] = versions[0]
                        params["version_hi"] = versions[1]
                # 对 find_release_for_toolkit 提取 toolkit 版本号
                if intent_key == "find_release_for_toolkit" and m.lastindex is not None:
                    try:
                        params["toolkit_version"] = m.group(1)
                    except IndexError:
                        if versions:
                            params["toolkit_version"] = versions[0]
                return {"intent": intent_key, "params": params}
        # 兜底：仅有版本号但没有明确意图词 → 不匹配（让通用流程处理）
        return None

    async def _execute_graph_query(
        self,
        gdao: OntologyGraphRepository,
        tenant_id: str,
        kb_id: str,
        timeline: dict,
        collector: ReasoningCollector | None = None,
    ) -> list[dict] | None:
        """执行图查询模板，返回兼容 neighbors() 格式的 facts，空则返回 None。

        返回的每条 fact 结构与 neighbors() 兼容：
            {source, target, target_type, target_entity, relation_type,
             relation_chain, relation_source_chunks, path, direction, hop}
        """
        intent = timeline["intent"]
        params = timeline.get("params", {})
        t = time.perf_counter()
        rows: list[dict] = []
        template_name = ""
        cypher_desc = ""

        try:
            if intent == "find_release_for_toolkit":
                tv = params.get("toolkit_version") or params.get("version", "")
                if not tv:
                    return None
                template_name = "find_release_supporting_toolkit"
                cypher_desc = f"MATCH (sw:SoftwareRelease)-[:SUPPORTS_TOOLKIT]->(:SoftwareToolkit{{{tv}}})"
                rows = await gdao.find_release_supporting_toolkit(tenant_id, kb_id, tv)

            elif intent == "earliest":
                target = params.get("version") or _VERSION_RE.sub("", params.get("query_hint", "")).strip()
                if not target:
                    return None
                template_name = "find_earliest_release_for"
                cypher_desc = f"沿 SUPERSEDES 链取最早满足关系的 SoftwareRelease"
                rec = await gdao.find_earliest_release_for(
                    tenant_id, kb_id,
                    relation_types=["SUPPORTS_TOOLKIT", "SUPPORTS"],
                    target_name=target,
                )
                if rec:
                    rows = [rec]

            elif intent == "latest":
                target = params.get("version", "")
                if not target:
                    return None
                template_name = "find_latest_release_for"
                cypher_desc = f"沿 SUPERSEDES 链取最新满足关系的 SoftwareRelease"
                rec = await gdao.find_latest_release_for(
                    tenant_id, kb_id,
                    relation_types=["SUPPORTS_TOOLKIT", "SUPPORTS"],
                    target_name=target,
                )
                if rec:
                    rows = [rec]

            elif intent == "first_added":
                target = params.get("version", "")
                if not target:
                    return None
                template_name = "find_first_release_adding"
                cypher_desc = f"沿 SUPERSEDES 链取首次 ADDED_SUPPORT_FOR({target}) 的版本"
                rec = await gdao.find_first_release_adding(tenant_id, kb_id, target)
                if rec:
                    rows = [rec]

            elif intent == "last_before_drop":
                target = params.get("version", "")
                if not target:
                    return None
                template_name = "find_last_supporting_before_drop"
                cypher_desc = f"MATCH (drop:SoftwareRelease)-[:DROPPED_SUPPORT_FOR]->({target})<-[:SUPERSEDES]-(last)"
                rec = await gdao.find_last_supporting_before_drop(tenant_id, kb_id, target)
                if rec:
                    rows = [rec]

            elif intent == "count_versions":
                lo = params.get("version_lo", "")
                hi = params.get("version_hi", "")
                template_name = "count_toolkit_versions"
                cypher_desc = f"MATCH (t:SoftwareToolkit) WHERE version∈[{lo},{hi}] RETURN count(t)"
                count = await gdao.count_toolkit_versions(tenant_id, kb_id, lo, hi)
                if count > 0:
                    # 构造计数结果 facts（兼容 answer_from_facts）
                    rows = [{
                        "source": "graph_query",
                        "target": f"{lo}→{hi}" if lo and hi else "all",
                        "target_type": "count_result",
                        "target_entity": {"name": "count_result", "type": "count_result",
                                          "aliases": [], "description": f"共 {count} 个版本",
                                          "attributes": {"count": count, "version_lo": lo, "version_hi": hi},
                                          "confidence": 1.0, "kb_id": kb_id,
                                          "doc_ids": [], "source_chunks": []},
                        "relation_type": "COUNT",
                        "relation_chain": ["COUNT"],
                        "relation_source_chunks": [],
                        "path": ["graph_query", f"{count}"],
                        "direction": "outgoing",
                        "hop": 1,
                    }]

            elif intent == "release_year":
                tv = params.get("toolkit_version") or params.get("version", "")
                if not tv:
                    return None
                template_name = "get_toolkit_release_year"
                cypher_desc = f"MATCH (t:SoftwareToolkit{{{tv}}}) RETURN t.release_year"
                rec = await gdao.get_toolkit_release_year(tenant_id, kb_id, tv)
                if rec:
                    rows = [rec]

            else:
                return None

        except Exception as e:
            logger.warning("[ontology] 图查询模板执行失败 intent=%s: %s", intent, e)
            if collector:
                collector.step(
                    STAGE_TIMELINE_GRAPH, "图查询模板",
                    status="failed",
                    summary=f"图查询模板 {template_name} 执行失败: {e}",
                    t_start=t,
                )
            return None

        if not rows:
            if collector:
                collector.step(
                    STAGE_TIMELINE_GRAPH, "图查询模板",
                    status="skipped",
                    summary=f"意图={intent}，图查询模板 {template_name} 未命中，降级通用流程",
                    detail={"intent": intent, "template": template_name, "cypher": cypher_desc, "hits": 0},
                    t_start=t,
                )
            return None

        # 转换成 facts 格式（兼容 answer_from_facts）
        facts: list[dict] = []
        for row in rows:
            name = row.get("name", "")
            etype = row.get("type", "")
            if not name or not etype:
                continue
            fact = {
                "source": "graph_query",
                "target": name,
                "target_type": etype,
                "target_entity": {
                    "name": name,
                    "type": etype,
                    "aliases": row.get("aliases", []),
                    "description": row.get("description", ""),
                    "attributes": row.get("attributes", {}),
                    "confidence": row.get("confidence", 1.0),
                    "kb_id": row.get("kb_id", kb_id),
                    "doc_ids": row.get("doc_ids", []),
                    "source_chunks": row.get("source_chunks", []),
                },
                "relation_type": row.get("relation_type", intent),
                "relation_chain": [row.get("relation_type", intent)],
                "relation_source_chunks": row.get("relation_source_chunks", []),
                "path": ["graph_query", name],
                "direction": "outgoing",
                "hop": 1,
            }
            facts.append(fact)

        if collector:
            collector.step(
                STAGE_TIMELINE_GRAPH, "图查询模板",
                summary=(
                    f"意图「{intent}」命中图查询模板 {template_name}，"
                    f"取到 {len(facts)} 条事实"
                ),
                detail={
                    "intent": intent,
                    "template": template_name,
                    "cypher": cypher_desc,
                    "params": params,
                    "hits": len(facts),
                    "top_fact": facts[0].get("target") if facts else None,
                },
                t_start=t,
            )

        return facts

    @staticmethod
    def _extract_subject_entity(query: str) -> list[str]:
        """从 query 中抽取主体实体名称（产品名/型号/版本名等），用于主体一致性过滤。

        策略：轻量规则（零 LLM 成本），抽取英文专名和中文产品名。
        """
        if not query:
            return []
        subjects: list[str] = []
        # 英文产品名+型号：Go2-W, GH100, B200, Nsight VSE, CUDA 12.8
        model_re = re.compile(
            r"\b([A-Z][a-zA-Z0-9]*(?:\s*[-\s]\s*[A-Z]?[a-zA-Z0-9]+)*)\b"
        )
        for m in model_re.finditer(query):
            token = m.group(1).strip()
            # 过滤掉太短的 token 和常见停用词
            if len(token) >= 2 and token.lower() not in (
                "the", "a", "an", "is", "of", "in", "for", "to", "vs", "or",
                "how", "what", "when", "which", "does", "can", "many", "last",
            ):
                subjects.append(token)
        # 中文产品名
        cn_re = re.compile(r"([一-鿿]{2,8}(?:型号|系列|版本|规格)?)")
        for m in cn_re.finditer(query):
            token = m.group(1).strip()
            if len(token) >= 2:
                subjects.append(token)
        return list(dict.fromkeys(subjects))  # 去重保序

    def _apply_subject_filter(
        self,
        raw_refs: list[dict],
        subjects: list[str],
        doc_map: dict[str, Any],
    ) -> tuple[list[dict], int]:
        """主体一致性过滤：对 raw_refs 按 doc_id 的 file_name 与 query 主体做匹配。

        Returns:
            (filtered_refs, removed_count): 过滤后的 refs 和被移除的数量
        """
        if not subjects or not raw_refs:
            return raw_refs, 0
        # 为每个 doc_id 预计算一致性布尔标记
        doc_match: dict[str, bool] = {}
        removed = 0
        filtered: list[dict] = []
        for r in raw_refs:
            did = r.get("doc_id")
            if not did:
                filtered.append(r)
                continue
            if did not in doc_match:
                d = doc_map.get(did)
                fname = (d.file_name if d else "") or ""
                # 检查文件名是否包含任一主体名（大小写不敏感）
                fname_lower = fname.lower()
                doc_match[did] = any(s.lower() in fname_lower for s in subjects)
            if doc_match[did]:
                filtered.append(r)
            else:
                removed += 1
        return filtered, removed

    async def _prellm_rerank_chunks(
        self,
        query: str,
        raw_refs: list[dict],
        tenant_id: str,
        kb_id: str = "",
        trace_id: str | None = None,
        user_id: str = "",
    ) -> list[dict]:
        """送 LLM 融合前的 chunk 级重排：用 reranker 对原始 chunk 文本打分。

        Args:
            query: 用户查询
            raw_refs: LightRAG 返回的原始 reference 列表（每项含 text 字段）
            tenant_id, kb_id, trace_id, user_id: 计量/追踪

        Returns:
            按 relevance 降序排列的 raw_refs（top-K 保留，其余移除）
        """
        from jonex_core.common.rerank import rerank

        if not raw_refs or len(raw_refs) <= RAG_PRELLM_RERANK_TOPK:
            return raw_refs

        # 提取每个 ref 的代表文本（chunk 原文）
        texts: list[str] = []
        indices: list[int] = []
        for i, r in enumerate(raw_refs):
            txt = r.get("text") or r.get("content") or ""
            if txt:
                texts.append(txt[:1024])
                indices.append(i)

        if len(texts) <= RAG_PRELLM_RERANK_TOPK:
            return raw_refs

        try:
            results = await rerank(
                query, texts, tenant_id=tenant_id,
                kb_id=kb_id or None, trace_id=trace_id, user_id=user_id,
            )
            if not results:
                return raw_refs
            score_by_idx = {x["index"]: x.get("relevance_score", 0.0) for x in results}
            # 标注 relevance 分数
            for i in range(len(raw_refs)):
                raw_refs[i]["relevance"] = score_by_idx.get(
                    next((j for j, idx in enumerate(indices) if idx == i), -1), 0.0
                )
            # 按 relevance 降序排序，取 top-K
            ranked = sorted(raw_refs, key=lambda r: r.get("relevance", 0.0), reverse=True)
            logger.info(
                "[prellm_rerank] query=%s top3_scores=%s total=%d kept=%d",
                query[:80],
                [round(r.get("relevance", 0), 3) for r in ranked[:3]],
                len(raw_refs), min(len(ranked), RAG_PRELLM_RERANK_TOPK),
            )
            return ranked[:RAG_PRELLM_RERANK_TOPK]
        except Exception as e:
            logger.warning("[prellm_rerank] 重排失败（回退原序）: %s", e)
            return raw_refs

    def _log_rag_timing(
        self, tenant_id: str, rag_multi_ms: int | None, fusion_ms: int | None,
        kb_ok: int, kb_total: int, kb_failed: list[str],
    ) -> None:
        """打印 RAG 线路分阶段耗时结构化日志（多库检索 + 多答案融合）。

        无论 with_reasoning 是否开启都会打印，便于按容器 grep；message 内嵌
        数字供人读/grep，extra 保留结构化字段供将来 ELK/JSON 聚合（口径同
        ingest_timing / reconcile_timing，见 docs/ingestion-timing-metrics-design.md §3.4 A）。

        fusion_ms=None 表示未触发融合（无有效答案或仅 1 个答案）。
        查看：make perf-search / docker logs jonex-knowledge-base | findstr ontology_search_timing
        """
        logger.info(
            "ontology_search_timing rag_multi_ms=%s fusion_ms=%s kb_ok=%s kb_total=%s",
            rag_multi_ms, fusion_ms, kb_ok, kb_total,
            extra={
                "event": "ontology_search_timing",
                "tenant_id": tenant_id,
                "rag_multi_ms": rag_multi_ms,
                "fusion_ms": fusion_ms,
                "kb_ok": kb_ok,
                "kb_total": kb_total,
                "kb_failed": kb_failed,
            },
        )

    async def _ontology_refs(
        self,
        tenant_id: str,
        kb_ids: list[str],
        ontology_instances: list[dict],
        facts: list[dict] | None,
        collector: ReasoningCollector | None = None,
    ) -> list[dict]:
        """方案⑦：从本体 source_chunks 直接构建 chunk 级引用，不回 LightRAG。

        收集命中实体 + facts 各 target_entity 的 source_chunks[].file_path，
        经 parse_file_source → _build_references 产出完整 references（含 COS 预签名/文件名）。
        source_chunks 为空的实体退化为文档级引用（_build_references_by_doc_ids）。
        """
        t = time.perf_counter()

        # ── 收集 source_chunks ──
        all_sc: list[dict] = []   # {source_id, file_path}
        chunk_doc_ids: list[str] = []
        fallback_doc_ids: list[str] = []

        for ent in (ontology_instances or []):
            sc = ent.get("source_chunks")
            if isinstance(sc, list) and sc:
                all_sc.extend(sc)
            else:
                for did in (ent.get("doc_ids") or []):
                    if did:
                        fallback_doc_ids.append(did)

        for f in (facts or []):
            te = f.get("target_entity") if isinstance(f, dict) else None
            if isinstance(te, dict):
                sc = te.get("source_chunks")
                if isinstance(sc, list) and sc:
                    all_sc.extend(sc)
                else:
                    for did in (te.get("doc_ids") or []):
                        if did:
                            fallback_doc_ids.append(did)
            # [jonex] 方案⑧：关系边的 source_chunks（覆盖 stub 端点/别名 miss 场景）
            rsc = f.get("relation_source_chunks")
            if isinstance(rsc, list) and rsc:
                all_sc.extend(rsc)

        # ── 解析 source_chunks file_path → raw_refs ──
        raw_refs: list[dict] = []
        seen = set()
        for sc in all_sc:
            fp = sc.get("file_path") if isinstance(sc, dict) else None
            if not fp:
                continue
            # 单条 file_path 可能是 <SEP> 连接的多值（同一实体跨多 chunk）
            for seg in fp.split("<SEP>"):
                seg = seg.strip()
                if not seg:
                    continue
                parsed = parse_file_source(seg)
                if not (parsed and parsed.get("doc_id")):
                    continue
                # 去重键=语义键（doc+chunk+位置），避免同一 chunk 的 file_path 串变体导致重复
                key = (
                    parsed["doc_id"],
                    parsed.get("chunk_index"),
                    parsed.get("page_no"),
                    parsed.get("time_start"),
                    parsed.get("time_end"),
                )
                if key in seen:
                    continue
                seen.add(key)
                raw_refs.append(parsed)
                chunk_doc_ids.append(parsed["doc_id"])

        # ── chunk 级引用（source_chunks 命中）──
        refs = await self._build_references(
            tenant_id, raw_refs, allowed_kb_ids=kb_ids,
        ) if raw_refs else []

        # ── 文档级兜底（source_chunks 为空或 stub 实体）──
        fallback = await self._build_references_by_doc_ids(
            tenant_id,
            list(dict.fromkeys(d for d in fallback_doc_ids if d not in chunk_doc_ids)),
            allowed_kb_ids=kb_ids,
        ) if fallback_doc_ids else []

        # ── 埋点：⑤ 的 STAGE_REF_RETRIEVE 改为记本体引用构建 ──
        if collector:
            collector.step(
                STAGE_REF_RETRIEVE, "本体引用构建",
                summary=(
                    f"source_chunks 命中 {len(raw_refs)} 个 → {len(refs)} 条 chunk 级引用"
                    + (f"；{len(fallback)} 条文档级兜底" if fallback else "")
                ),
                detail={
                    "source_chunks_total": len(all_sc),
                    "unique_file_paths": len(raw_refs),
                    "chunk_ref_count": len(refs),
                    "fallback_ref_count": len(fallback),
                    "chunk_doc_ids": chunk_doc_ids,
                    "fallback_doc_ids": fallback_doc_ids,
                },
                t_start=t,
            )

        return refs + fallback

    async def _rag_fallback_multi(
        self, tenant_id: str, user_id: str, req: OntologySearchRequest,
        kb_ids: list[str], trace_id: str | None,
        collector: ReasoningCollector | None = None,
    ) -> dict:
        """策略 A：并行查询全部 KB 的 RAG → LLM 融合。

        Args:
            collector: 可选，推理链采集器（P0 非流式埋点）。
        Returns:
            {"answer": str, "references": list[dict]}
        """
        empty = {
            "answer": (
                "未在本体图谱中找到与该查询直接相关的信息，"
                "且查询过短无法进行语义检索，请输入更完整的描述。"
            ),
            "references": [],
        }
        if len((req.query or "").strip()) < RAG_MIN_QUERY_LEN:
            return empty

        rag = get_rag_client()
        t_rag = time.perf_counter()
        t_fallback_epoch = time.time()   # 用于回读「检索期 rerank 命中」标记的时间基线

        # 方案③a：并发信号量限流
        _sem = asyncio.Semaphore(ONTOLOGY_RAG_MAX_CONCURRENCY)

        async def _query_one(kid: str) -> dict | Exception:
            async with _sem:
                try:
                    return await rag.query_detailed(
                        query=req.query, tenant_id=tenant_id, mode=req.mode,
                        top_k=req.top_k, knowledge_base_id=kid,
                        trace_id=trace_id or "",
                        user_id=user_id,
                    )
                except Exception as e:
                    return e

        tasks = [_query_one(kid) for kid in kb_ids]
        results = await asyncio.gather(*tasks)

        per_kb: list[dict] = []
        kb_failed: list[str] = []
        all_raw_refs: list[dict] = []
        for kid, res in zip(kb_ids, results):
            if isinstance(res, Exception):
                logger.warning("[ontology] RAG 查询失败 kb=%s: %s", kid, res)
                kb_failed.append(kid)
                continue
            answer = res.get("answer") if isinstance(res, dict) else res
            if not (answer or "").strip():
                logger.info("[ontology] RAG 返回空答案 kb=%s", kid)
                kb_failed.append(kid)
                continue
            per_kb.append({"kb_id": kid, "answer": answer})
            if isinstance(res, dict):
                all_raw_refs.extend(res.get("references", []))

        rag_multi_ms = int((time.perf_counter() - t_rag) * 1000)

        # ── 召回明细：埋点前预查 doc_map（与后续 _build_references 共用，避免重复 DB 查询）──
        recall_doc_ids: list[str] = []
        if all_raw_refs:
            recall_doc_ids = [r.get("doc_id") for r in all_raw_refs if r.get("doc_id")]
        doc_map: dict[str, Any] = {}
        if recall_doc_ids:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                # get_by_ids 全列加载，访问 knowledge_base_id/file_name 均为标量列
                # （非 relationship / 非 deferred），session 关闭后读取安全
                docs = await repo.get_by_ids(list(set(recall_doc_ids)), tenant_id)
            doc_map = {d.id: d for d in docs}

        # ── [jonex] P1-5 主体一致性过滤 ──
        # 分层局限：本层过滤/重排作用在 LightRAG 已生成答案之后的引用上，
        # 可改善展示引用与融合排序，但不改单个 KB 内部已被污染的答案。
        # 若同 KB 内召回了不相关 chunk 并已影响 LightRAG 生成的答案，
        # 需在 LightRAG 检索侧做主体过滤才能真正纠正（见 L3）。
        subject_filtered_count = 0
        if RAG_SUBJECT_FILTER_ENABLED and all_raw_refs:
            subjects = self._extract_subject_entity(req.query)
            if subjects:
                all_raw_refs, subject_filtered_count = self._apply_subject_filter(
                    all_raw_refs, subjects, doc_map,
                )
                logger.info(
                    "[subject_filter] query=%r subjects=%s filtered=%d remaining=%d",
                    req.query[:80], subjects, subject_filtered_count, len(all_raw_refs),
                )

        # ── [jonex] P1-5 送 LLM 前 chunk 级重排 ──
        prellm_reranked = False
        prellm_rerank_scores: list[float] = []
        if RAG_PRELLM_RERANK_ENABLED and all_raw_refs and len(all_raw_refs) > RAG_PRELLM_RERANK_TOPK:
            original_count = len(all_raw_refs)
            all_raw_refs = await self._prellm_rerank_chunks(
                req.query, all_raw_refs,
                tenant_id=tenant_id,
                kb_id=kb_ids[0] if kb_ids else "",
                trace_id=trace_id, user_id=user_id,
            )
            prellm_reranked = len(all_raw_refs) < original_count
            prellm_rerank_scores = [round(r.get("relevance", 0), 4) for r in all_raw_refs[:5]]

        allowed = set(kb_ids)
        recalls: list[dict] = []
        if ONTOLOGY_RAG_RECALL_DETAIL_ENABLED and collector and all_raw_refs:
            for r in all_raw_refs[:ONTOLOGY_RAG_RECALL_MAX_ITEMS]:
                did = r.get("doc_id")
                d = doc_map.get(did)
                # 租户+跨库防御：doc_map 已按 tenant_id 查询；查不到或库外一律剔除
                if d is None or d.knowledge_base_id not in allowed:
                    continue
                text = r.get("text") or ""
                if len(text) > ONTOLOGY_RAG_RECALL_TEXT_MAX:
                    text = text[:ONTOLOGY_RAG_RECALL_TEXT_MAX] + "…"
                recalls.append({
                    "doc_id": did,
                    "file_name": d.file_name,
                    "kb_id": d.knowledge_base_id,
                    "chunk_index": r.get("chunk_index"),
                    "chunk_id": r.get("chunk_id"),
                    "text": text,
                })

        if collector:
            collector.step(
                STAGE_RAG_FALLBACK, "OntoRAG 多库检索",
                summary=f"{len(per_kb)}/{len(kb_ids)} 个知识库返回有效答案，召回 {len(recalls)} 个片段"
                        + (f"，主体过滤移除 {subject_filtered_count} 个" if subject_filtered_count else ""),
                detail={
                    "kb_ok": [p["kb_id"] for p in per_kb],
                    "kb_failed": kb_failed,
                    "recall_count": len(recalls),
                    "recalls": recalls,
                    "p1_5": {
                        "subject_filter_enabled": RAG_SUBJECT_FILTER_ENABLED,
                        "subject_filtered_count": subject_filtered_count,
                        "prellm_rerank_enabled": RAG_PRELLM_RERANK_ENABLED,
                        "prellm_reranked": prellm_reranked,
                        "prellm_rerank_topk": RAG_PRELLM_RERANK_TOPK,
                        "prellm_rerank_top_scores": prellm_rerank_scores[:3] if prellm_rerank_scores else [],
                    },
                },
                t_start=t_rag,
            )
            # 检索期 rerank（LightRAG 内部 + P1-5 平台侧：召回后、送 LLM 前）实测检测
            hit = await self._detect_retrieval_rerank_hit(req.query, since_epoch=t_fallback_epoch)
            rerank_summary_parts: list[str] = []
            if prellm_reranked:
                rerank_summary_parts.append(
                    f"P1-5 平台 chunk 重排（{len(all_raw_refs)} 条经 gateway reranker）"
                )
            if hit is True:
                rerank_summary_parts.append("OntoRAG 内部 rerank 已触发")
            elif RAG_RETRIEVAL_RERANK_ENABLED:
                rerank_summary_parts.append("OntoRAG rerank 已配置但本次未触发")
            else:
                rerank_summary_parts.append("OntoRAG rerank 未配置")

            collector.step(
                STAGE_RETRIEVAL_RERANK, "检索期重排",
                summary="；".join(rerank_summary_parts),
                detail={
                    "triggered": bool(prellm_reranked) or bool(hit),
                    "p1_5_platform_rerank": {
                        "enabled": RAG_PRELLM_RERANK_ENABLED,
                        "triggered": prellm_reranked,
                        "topk": RAG_PRELLM_RERANK_TOPK,
                    },
                    "lightrag_rerank": {
                        "enabled": RAG_RETRIEVAL_RERANK_ENABLED,
                        "triggered": hit is True,
                    },
                    "subject_filter": {
                        "enabled": RAG_SUBJECT_FILTER_ENABLED,
                        "filtered": subject_filtered_count,
                    },
                    "where": "platform_pre_llm",
                    "phase": "retrieval",
                },
            )

        if not per_kb:
            if collector:
                collector.step(STAGE_FUSION, "多答案融合", status="skipped",
                               summary="无有效答案，无需融合")
            self._log_rag_timing(tenant_id, rag_multi_ms, None, len(per_kb), len(kb_ids), kb_failed)
            return {
                "answer": (
                    "抱歉，所有知识库的语义检索均未返回有效结果，"
                    "请尝试调整查询或检查知识库内容。"
                ),
                "references": [],
            }

        fusion_ms = None
        if len(per_kb) == 1:
            answer = per_kb[0]["answer"]
            if collector:
                collector.step(STAGE_FUSION, "多答案融合", status="skipped",
                               summary="仅 1 个有效答案，无需融合")
        else:
            # 方案②a：限制融合宽度 — 只对前 N 个答案做融合（保留 kb_ids 原始顺序）
            per_kb_fused = per_kb[:ONTOLOGY_RAG_FUSION_TOPN]
            fusion_note = ""
            if len(per_kb_fused) < len(per_kb):
                fusion_note = (
                    f"（共 {len(per_kb)} 个，取前 {ONTOLOGY_RAG_FUSION_TOPN} 融合，"
                    f"其余 {len(per_kb) - len(per_kb_fused)} 个仅作引用来源）"
                )
            t_fuse = time.perf_counter()
            answer = await fuse_rag_answers(
                req.query, per_kb_fused,
                tenant_id=tenant_id, user_id=user_id, trace_id=trace_id,
            )
            fusion_ms = int((time.perf_counter() - t_fuse) * 1000)
            if collector:
                collector.step(STAGE_FUSION, "多答案融合",
                               summary=f"融合 {len(per_kb_fused)} 个知识库的答案{fusion_note}",
                               t_start=t_fuse)
        self._log_rag_timing(tenant_id, rag_multi_ms, fusion_ms, len(per_kb), len(kb_ids), kb_failed)

        # 汇集所有 KB 的 references 统一去重富化（按请求 kb_ids 做跨库防御过滤）
        # 传入预查的 doc_map 复用，避免重复 DB 查询
        references = await self._build_references(
            tenant_id, all_raw_refs, allowed_kb_ids=kb_ids, doc_map=doc_map if doc_map else None,
        )

        # 引用排序 + 截断 top-N：优先 reranker 相关性排序，失败/关闭兜底回退 chunk 频次。
        # 各分支都往 reasoning 采集 rerank 阶段，便于前端/排查确认是否触发与打分分布。
        candidate_count = len(references)
        if candidate_count > RAG_FALLBACK_MAX_REFS:
            ranked = None
            t_rr = time.perf_counter()
            if RAG_FALLBACK_RERANK_ENABLED:
                ranked = await self._rerank_references(
                    req.query, references, tenant_id=tenant_id, trace_id=trace_id,
                    kb_id=kb_ids[0] if kb_ids else "", user_id=user_id,
                )
            if ranked is not None:
                references = ranked[:RAG_FALLBACK_MAX_REFS]
                if collector:
                    collector.step(
                        STAGE_RERANK, "引用重排",
                        summary=(f"Reranker 重排 {candidate_count} 个候选文档，按相关性取 "
                                 f"top-{RAG_FALLBACK_MAX_REFS}"),
                        detail={
                            "triggered": True,
                            "candidate_count": candidate_count,
                            "max_refs": RAG_FALLBACK_MAX_REFS,
                            "top_scores": [
                                {"file_name": r.get("file_name"),
                                 "relevance": round(r.get("relevance", 0), 4)}
                                for r in references
                            ],
                        },
                        t_start=t_rr,
                    )
            else:
                # rerank 关闭或调用失败 → 回退 chunk 命中频次排序
                references.sort(key=lambda r: len(r.get("locations", [])), reverse=True)
                references = references[:RAG_FALLBACK_MAX_REFS]
                if collector:
                    if RAG_FALLBACK_RERANK_ENABLED:
                        collector.step(
                            STAGE_RERANK, "引用重排", status="failed",
                            summary="Reranker 调用失败，回退按 chunk 命中频次排序",
                            detail={"triggered": True, "candidate_count": candidate_count,
                                    "max_refs": RAG_FALLBACK_MAX_REFS,
                                    "fallback": "len(locations)"},
                            t_start=t_rr,
                        )
                    else:
                        collector.step(
                            STAGE_RERANK, "引用重排", status="skipped",
                            summary=("未启用 rerank（ONTOLOGY_RAG_FALLBACK_RERANK_ENABLED=false），"
                                     "按 chunk 命中频次排序取 top-N"),
                            detail={"triggered": False, "candidate_count": candidate_count,
                                    "max_refs": RAG_FALLBACK_MAX_REFS,
                                    "fallback": "len(locations)"},
                        )
        elif collector:
            collector.step(
                STAGE_RERANK, "引用重排", status="skipped",
                summary=f"引用文档 {candidate_count} 个 ≤ top-{RAG_FALLBACK_MAX_REFS}，无需重排",
                detail={"triggered": False, "candidate_count": candidate_count,
                        "max_refs": RAG_FALLBACK_MAX_REFS},
            )

        return {"answer": answer, "references": references}

    async def _detect_retrieval_rerank_hit(
        self, query: str, *, since_epoch: float,
    ) -> bool | None:
        """回读 gateway 写入的「检索期 rerank 命中」标记，判断本次查询是否真的触发了
        LightRAG 检索期 rerank 调用。

        Returns:
            True  = 检测到本次查询窗口内的 rerank 调用；
            False = 检测可用但未命中（未调用 / 无召回 / rerank 回退）；
            None  = 检测不可用（Redis 不可达等），无法确认。
        """
        try:
            import hashlib
            from jonex_core.common.cache import CacheUtil
            q = (query or "").strip()
            if not q:
                return False
            qh = hashlib.sha1(q.encode("utf-8")).hexdigest()[:20]
            val = await CacheUtil.get(f"yx:rr:hit:{qh}")
            if val is None:
                return False
            # 标记时间戳需落在本次 fallback 检索开始之后（含 2s 时钟容差），
            # 否则视为上一次相同查询遗留的陈旧标记。
            return float(val) >= since_epoch - 2.0
        except Exception as e:
            logger.warning("[rerank] 检索期命中检测不可用（忽略）: %s", e)
            return None

    async def _rerank_references(
        self, query: str, references: list[dict], *,
        tenant_id: str, trace_id: str | None,
        kb_id: str = "", user_id: str = "",
    ) -> list[dict] | None:
        """用 reranker 对 references 按相关性排序；返回排序后的新列表，失败返回 None。"""
        from jonex_core.common.rerank import rerank

        # 先按 len(locations) 频次粗排，让 gateway 的 MAX_DOCS 截断切掉「频次最低」的
        # 尾部候选（而非 agg 字典近似随机序），避免误杀相关文档，且与兜底排序口径一致。
        references = sorted(references, key=lambda r: len(r.get("locations", [])), reverse=True)

        # 取每个文档代表文本：首个 location 的 chunk 原文 + 文件名兜底。
        # 取舍：仅取 locations[0].text[:1024]；若关键信息在 chunk 后半段会丢信号，
        # 属已知取舍，后续区分度不足时可改为拼接该文档所有 locations 的 text（限总长）。
        docs_text: list[str] = []
        for r in references:
            loc = (r.get("locations") or [{}])[0]
            docs_text.append((loc.get("text") or r.get("file_name") or "")[:1024])

        results = await rerank(
            query, docs_text, tenant_id=tenant_id,
            kb_id=kb_id or None, trace_id=trace_id, user_id=user_id,
        )
        if not results:
            return None

        score_by_idx = {x["index"]: x.get("relevance_score", 0.0) for x in results}
        for i, r in enumerate(references):
            r["relevance"] = score_by_idx.get(i, 0.0)  # 透传给前端用于展示/调试
        sorted_refs = sorted(references, key=lambda r: r.get("relevance", 0.0), reverse=True)

        # 可观测性：灰度期对比「频次排序 vs rerank 排序」，确认 reranker 是否真起作用
        logger.info(
            "[rerank] query=%s top3_scores=%s (共 %d 文档)",
            query[:80],
            [round(r.get("relevance", 0), 3) for r in sorted_refs[:3]],
            len(sorted_refs),
        )
        return sorted_refs

    # ══════════════════════════════════════════════════════════════════
    # §9 严格模式
    # ══════════════════════════════════════════════════════════════════

    def _verify_answer(
        self,
        answer: str,
        references: list[dict],
        query: str,
        ontology_instances: list[dict],
        facts: list[dict] | None,
        prior_answers: list[str] | None = None,
        min_score: float = 0.8,
    ) -> dict:
        """校验单次尝试的答案质量，返回 checks dict 与加权 score。

        Returns:
            {"checks": {...}, "score": float, "passed": bool, "unmet": [...]}
        """
        checks: dict[str, float] = {}

        # 1) non_refusal：命中拒答模板
        refusal = False
        for pat in _REFUSAL_PATTERNS:
            if pat.search(answer):
                refusal = True
                break
        checks["non_refusal"] = 0.0 if refusal else 1.0

        # 2) has_reference
        checks["has_reference"] = 1.0 if references else 0.0

        # 3) grounded：关键数值 token 是否可在证据中找到
        grounded = 0.0
        key_tokens: list[str] = re.findall(
            r"\b\d+\.?\d*\s*(?:m/s²?|km/h|kg|mm|cm|m|g|W|V|A|°C|%|倍)\b", answer,
        )
        if not key_tokens:
            key_tokens = re.findall(r"\b\d+\.?\d+\b", answer)
        if key_tokens:
            # 收集所有证据文本：chunk references + ontology facts + ontology instances
            evidence_texts: list[str] = []
            for r in references:
                for loc in r.get("locations", []):
                    if loc.get("text"):
                        evidence_texts.append(loc["text"])
            if facts:
                for f in facts:
                    te = f.get("target_entity", {})
                    desc = te.get("description", "")
                    attrs = str(te.get("attributes", {}))
                    if desc:
                        evidence_texts.append(desc)
                    if attrs:
                        evidence_texts.append(attrs)
            # [jonex] L5 补上 ontology_instances（query_with_ontology 返回体不含 facts，
            # 但含 ontology_instances——其 description/attributes 同样是有效证据）
            for inst in ontology_instances:
                desc = inst.get("description", "")
                attrs = str(inst.get("attributes", {}))
                if desc:
                    evidence_texts.append(desc)
                if attrs and attrs != "{}":
                    evidence_texts.append(attrs)
            # ── 空格/单位归一化：去除数值与单位之间的空格 ──
            def _norm(s: str) -> str:
                """归一化：去除数值与单位间的空格、多余空白（如 '316 mm'→'316mm'）"""
                s = re.sub(r"(\d)\s+(mm|cm|m|km|kg|g|W|V|A|°C|%|倍)", r"\1\2", s)
                s = re.sub(r"\s+", " ", s)
                return s.strip()
            evidence_normalized = _norm(" ".join(evidence_texts))
            found = sum(1 for t in key_tokens if _norm(t) in evidence_normalized)
            grounded = found / len(key_tokens) if key_tokens else 1.0
        else:
            grounded = 1.0  # 无关键数值则该项满分
        checks["grounded"] = grounded

        # 4) consistency：多次尝试答案的关键结论一致性
        if prior_answers and len(prior_answers) >= 1:
            # 简单归一化：提取所有答案的数值做比较
            all_numbers: list[set[str]] = []
            for ans in [answer] + list(prior_answers):
                all_numbers.append(set(re.findall(r"\b\d+\.?\d*\b", ans)))
            if len(all_numbers) >= 2:
                overlap = len(all_numbers[0] & all_numbers[-1])
                total = max(len(all_numbers[0] | all_numbers[-1]), 1)
                checks["consistency"] = overlap / total
            else:
                checks["consistency"] = 0.5  # single-shot 固定 0.5
        else:
            checks["consistency"] = 0.5

        # 加权求和
        score = (
            _STRICT_W_NON_REFUSAL * checks["non_refusal"]
            + _STRICT_W_REFERENCE * checks["has_reference"]
            + _STRICT_W_GROUNDED * checks["grounded"]
            + _STRICT_W_CONSISTENCY * checks["consistency"]
        )
        score = min(max(score, 0.0), 1.0)
        # [jonex] 使用请求的 strict_min_score 而非进程常量 _STRICT_MIN_SCORE
        passed = score >= min_score
        unmet = [k for k, v in checks.items() if v < 0.6]

        return {
            "checks": checks,
            "score": score,
            "passed": passed,
            "unmet": unmet,
        }

    @staticmethod
    def _build_reliability(verify_result: dict, attempts: int, min_score: float = 0.8) -> dict:
        """构造 ReliabilityInfo 序列化 dict（§9.2、§9.7 文案）。"""
        verdict = "verified" if verify_result["passed"] else "best_effort"
        if verdict == "verified":
            statement = (
                f"本答案经严格校验通过（可靠性 {verify_result['score']:.2f}，"
                f"第 {attempts} 次尝试达标）：非拒答、含原文引用、关键数据可溯源。"
            )
        else:
            unmet_str = "、".join(verify_result.get("unmet", [])) or "无"
            statement = (
                f"本答案为 {attempts} 次严格校验中置信度最高的一次"
                f"（可靠性 {verify_result['score']:.2f}，未达 {min_score} 阈值）。"
                f"未满足项：{unmet_str}。建议结合下方引用人工核对。"
            )
        return {
            "verdict": verdict,
            "score": round(verify_result["score"], 4),
            "attempts": attempts,
            "passed": verify_result["passed"],
            "checks": verify_result["checks"],
            "statement": statement,
            "unmet": verify_result.get("unmet", []),
        }

    # ══════════════════════════════════════════════════════════════════
    # §10 深度查询
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _classify_intent(query: str) -> dict:
        """意图分类器（规则版，零 LLM 成本）。

        Returns:
            {"intent": "simple" | "complex", "ops": [...], "subjects": [...]}
        """
        if not query:
            return {"intent": "simple", "ops": [], "subjects": []}

        # complex 触发词（中文 + 英文）— 注意 "how many" / "共几个" 归类到 count
        # 而非 compute（compute 负责乘除/百分比/换算类）
        complex_triggers = [
            "计算", "多少倍", "百分比", "相差", "对比", "相比",
            "为什么", "意味着", "推断", "共几个", "最早", "最后", "首次",
            "换算", "密度", "占比", "平均", "合计", "哪年",
            "how much", "how many", "compare", "versus", "vs",
            "why", "imply", "derive", "convert", "total", "ratio", "average",
        ]
        ops: list[str] = []
        q_lower = query.lower()
        for t in complex_triggers:
            if t.lower() in q_lower:
                if t in ("计算", "多少倍", "百分比", "换算", "密度", "占比", "平均",
                         "how much", "convert", "total", "ratio", "average"):
                    ops.append("compute")
                elif t in ("对比", "相比", "compare", "versus", "vs"):
                    ops.append("compare")
                elif t in ("共几个", "how many", "count"):
                    ops.append("count")
                elif t in ("为什么", "why"):
                    ops.append("reason")
                elif t in ("最早", "最后", "首次"):
                    ops.append("timeline")
        ops = list(dict.fromkeys(ops))
        subjects = SearchService._extract_subject_entity(query)

        # ── 兜底：无触发词但含多实体/数值 → 可能为复杂对比/分析 ──
        if ops:
            intent = "complex"
        else:
            # 统计 query 中的数值和实体名词数量
            num_count = len(re.findall(r"\b\d+\.?\d*\b", query))
            entity_count = len(subjects)
            if num_count >= 2 and entity_count >= 2:
                # 含多个数值+多实体但无触发词 → 例如 "which shows the smaller variation"
                intent = "complex"
            else:
                intent = "simple"

        return {"intent": intent, "ops": ops, "subjects": subjects}

    async def _plan_subqueries(
        self, query: str, tenant_id: str, user_id: str, trace_id: str | None,
        max_subqueries: int = 6,
    ) -> dict:
        """LLM 规划器：将复杂查询分解为子查询列表（§10.3 ①）。

        Returns:
            {"sub_questions": [{"id": "q1", "ask": "...", "expect": "..."}],
             "aggregation": {"op": "compute|compare|count|reason|none", "instruction": "..."}}
        """
        from jonex_core.common.ontology_llm import _get_client
        import json as _json

        client = _get_client()
        model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

        system_prompt = (
            "You are a query planner for a technical knowledge base. "
            "Decompose a complex question into simple sub-questions, each answerable "
            "by a single fact lookup (no computation within sub-queries). "
            f"At most {max_subqueries} sub-questions.\n\n"
            "Output ONLY valid JSON, no markdown, no explanation:\n"
            '{"sub_questions":[{"id":"q1","ask":"...","expect":"short label"}],'
            '"aggregation":{"op":"...","instruction":"..."}}\n\n'
            'op must be one of: compute | compare | count | reason | convert | none\n\n'
            "=== EXAMPLES ===\n\n"
            "Q: How much energy to heat 1.8L water from 25C to 73C?\n"
            'A: {"sub_questions":[{"id":"q1","ask":"specific heat capacity of water","expect":"water_specific_heat"},{"id":"q2","ask":"heat energy formula Q=mcΔT","expect":"heat_formula"}],"aggregation":{"op":"compute","instruction":"Q = 1.8kg * c * (73-25)K, report in kJ"}}\n\n'
            "Q: compare H100 vs A6000 inference speed\n"
            'A: {"sub_questions":[{"id":"q1","ask":"H100 inference tokens per second","expect":"h100_tokens_per_sec"},{"id":"q2","ask":"A6000 inference tokens per second","expect":"a6000_tokens_per_sec"}],"aggregation":{"op":"compare","instruction":"Calculate ratio H100/A6000 and state which is faster"}}\n\n'
            "Q: Go2-W max distance at top speed over full endurance\n"
            'A: {"sub_questions":[{"id":"q1","ask":"Go2-W top speed","expect":"go2w_top_speed"},{"id":"q2","ask":"Go2-W maximum endurance time","expect":"go2w_endurance_max"}],"aggregation":{"op":"compute","instruction":"distance = speed * endurance_hours, report in km"}}\n\n'
            "Q: how many CUDA versions from 12.4 to 13.3\n"
            'A: {"sub_questions":[{"id":"q1","ask":"list all CUDA Toolkit versions between 12.4 and 13.3","expect":"cuda_versions_list"}],"aggregation":{"op":"count","instruction":"Count distinct minor versions from 12.4 to 13.3 inclusive"}}\n\n'
            "Q: Go2-W rated payload as percentage of self-weight\n"
            'A: {"sub_questions":[{"id":"q1","ask":"Go2-W self weight","expect":"go2w_weight"},{"id":"q2","ask":"Go2-W rated payload capacity","expect":"go2w_payload_rated"}],"aggregation":{"op":"compute","instruction":"percentage = payload/weight * 100, report as %"}}\n\n'
            "Q: Is 10kg within Go2-W payload limit?\n"
            'A: {"sub_questions":[{"id":"q1","ask":"Go2-W rated load and maximum limit load","expect":"go2w_payload_specs"}],"aggregation":{"op":"reason","instruction":"Compare 10kg against rated load and limit load, state if within limits"}}\n\n'
            "Q: convert Go2-W 2.5m/s to km/h\n"
            'A: {"sub_questions":[],"aggregation":{"op":"convert","instruction":"2.5 m/s * 3.6 = 9 km/h"}}\n\n'
            "=== END EXAMPLES ===\n\n"
            "RULES:\n"
            "- Each sub-question MUST be a simple fact lookup (one entity, one attribute)\n"
            "- NEVER include computation/arithmetic in sub-question text\n"
            "- If the question is a simple unit conversion with no fact lookup needed, return empty sub_questions with op=convert\n"
            "- If only one fact is needed (e.g. 'count X' or 'is Y within Z'), use 1 sub-question\n"
            "- Output ONLY the JSON object, no surrounding text, no ``` fences"
        )
        extra_headers = {
            "X-Jonex-Tenant-Id": tenant_id or "unknown",
            "X-Jonex-Scene": "query_plan",
            "X-Jonex-Trace-Id": trace_id or f"plan:{__import__('uuid').uuid4().hex}",
        }
        if user_id:
            extra_headers["X-Jonex-User-Id"] = user_id
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Query: {query}"},
                ],
                temperature=0.1,
                max_tokens=1024,
                extra_headers=extra_headers,
            )
            text = (resp.choices[0].message.content or "").strip()
            # 剥离 markdown 代码块（```json ... ``` 或 ``` ... ```）
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()
            # 提取第一个完整 JSON 对象（匹配最外层花括号）
            json_match = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", text)
            if json_match:
                return _json.loads(json_match.group(0))
            logger.warning("[deep] 规划器返回不可解析的 JSON: %s", text[:200])
            return {"sub_questions": [], "aggregation": {"op": "none", "instruction": ""}}
        except Exception as e:
            logger.warning("[deep] 规划器失败: %s", e)
            return {"sub_questions": [], "aggregation": {"op": "none", "instruction": ""}}

    @staticmethod
    def _extract_fact(sub_result: dict, sub_query: dict) -> dict:
        """从子查询结果中提取关键事实（§10.3 ②）。

        Returns:
            {"sub_id": str, "value": str|None, "source": str, "grounded": bool,
             "answer": str, "ref_count": int, "refs": list[dict]}
        """
        answer = sub_result.get("answer", "")
        source = sub_result.get("source", "rag")
        refs = sub_result.get("references", [])
        value = None
        # 尝试从 answer 中提取结构化值（数值 + 单位）
        val_match = re.search(r"(\d+\.?\d*\s*(?:[A-Za-z%°/²³]+\s*)+)", answer)
        if val_match:
            value = val_match.group(1).strip()
        grounded = source == "ontology" or bool(refs)
        return {
            "sub_id": sub_query.get("id", ""),
            "value": value,
            "source": source,
            "grounded": grounded,
            "answer": answer[:500],
            "ref_count": len(refs),
            "refs": refs,  # 子查询原始引用，供 deep 汇总
        }

    async def _synthesize(
        self, query: str, facts: list[dict], aggregation: dict,
        tenant_id: str, user_id: str, trace_id: str | None,
        allow_common_sense: bool = True,
    ) -> str:
        """LLM 汇总器：基于取证要素 + aggregation 指令生成最终答案（§10.3 ③）。"""
        from jonex_core.common.ontology_llm import _get_client
        import json as _json

        client = _get_client()
        model = os.getenv("ONTOLOGY_LLM_MODEL", "deepseek-v4-flash-202605")

        op = aggregation.get("op", "none")
        instruction = aggregation.get("instruction", "")

        system_prompt = (
            "You are an expert technical analyst. Synthesize a clear, accurate answer "
            "based on the provided facts gathered from sub-queries. "
            f"The aggregation operation is: {op}. Instructions: {instruction}. "
            "Present a step-by-step derivation: list each fact used, the formula or logic, "
            "and the final conclusion."
        )
        if allow_common_sense:
            system_prompt += (
                " You may apply well-known physical constants, math formulas, or standard "
                "conventions on TOP of the provided facts. Explicitly state any assumption "
                "(e.g. 'Assuming c=4186 J/(kg·K)'). NEVER fabricate product parameters."
            )

        extra_headers = {
            "X-Jonex-Tenant-Id": tenant_id or "unknown",
            "X-Jonex-Scene": "deep_synthesis",
            "X-Jonex-Trace-Id": trace_id or f"synth:{__import__('uuid').uuid4().hex}",
        }
        if user_id:
            extra_headers["X-Jonex-User-Id"] = user_id
        # [jonex] L2 剥离 refs（含预签名 URL、chunk 原文）再送 LLM，避免 token 浪费与 URL 泄漏
        facts_lean = [
            {k: v for k, v in f.items() if k != "refs"}
            for f in facts
        ]
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",
                     "content": f"Query: {query}\n\nFacts:\n{_json.dumps(facts_lean, ensure_ascii=False)}\n\nAggregation: {_json.dumps(aggregation)}"},
                ],
                temperature=0.2,
                max_tokens=2048,
                extra_headers=extra_headers,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning("[deep] 汇总器失败: %s", e)
            return f"基于 {len(facts)} 个要素：{', '.join(f.get('value', f.get('answer', '')[:100]) for f in facts)}"

    async def deep_query(
        self,
        tenant_id: str,
        user_id: str,
        request: DeepSearchRequest | dict,
        trace_id: str | None = None,
    ) -> dict:
        """深度查询入口（§10.3）：意图分类 → 规划 → 并发取证 → 汇总。

        simple 问题直接委托 query_with_ontology（复用现有单次核心）。
        complex 问题走「分解-取证-汇总」三段编排。
        可选叠加 strict_mode 做最外层质量门禁。
        """
        req = DeepSearchRequest(**_payload(request))
        # ── 进程总闸 ──
        if not _DEEP_QUERY_ENABLED:
            return {
                "answer": "深度查询功能未启用，请使用 /search/ontology 进行查询。",
                "source": "deep_disabled",
                "references": [],
                "ontology_instances": [],
                "rag_used": False,
                "knowledge_base_ids": [],
                "reasoning": None,
                "reliability": None,
                "plan_summary": None,
                "facts_summary": [],
            }
        kb_ids = await self._resolve_kb_ids(tenant_id, req)
        collector = ReasoningCollector(enabled=req.with_reasoning and _REASONING_ENABLED)

        # ── 意图分类 ──
        intent = self._classify_intent(req.query)
        collector.step(
            STAGE_INTENT_CLASSIFY, "意图识别",
            summary=f"intent={intent['intent']} ops={intent.get('ops', [])}",
            detail=intent,
        )

        # simple → 直连现有核心
        if intent["intent"] == "simple":
            sub_req = req.copy(update={
                "with_reasoning": req.with_reasoning,
                "strict_mode": False,  # 防止复进入 strict 循环
            })
            result = await self.query_with_ontology(
                tenant_id, user_id, sub_req, trace_id,
            )
            # 合并 deep 分类步骤到内层 reasoning
            if result.get("reasoning") and isinstance(result["reasoning"], dict):
                inner_steps = result["reasoning"].get("steps", [])
                result["reasoning"]["steps"] = collector._steps + inner_steps
                result["reasoning"]["deep_intent"] = "simple"
            else:
                result["reasoning"] = collector.build(result.get("source", "unknown"))
            return result

        # ── complex：规划 ──
        t_plan = time.perf_counter()
        plan = await self._plan_subqueries(
            req.query, tenant_id, user_id, trace_id,
            max_subqueries=req.max_subqueries,
        )
        sub_questions = plan.get("sub_questions", [])
        aggregation = plan.get("aggregation", {"op": "none", "instruction": ""})
        collector.step(
            STAGE_QUERY_PLAN, "查询分解",
            summary=f"拆成 {len(sub_questions)} 个子查询，聚合={aggregation.get('op', 'none')}",
            detail={"sub_questions": sub_questions, "aggregation": aggregation},
            t_start=t_plan,
        )

        if not sub_questions:
            # 规划失败 → 降级到普通查询
            fallback = await self.query_with_ontology(
                tenant_id, user_id, req, trace_id,
            )
            fallback["source"] = "deep_degraded"
            return fallback

        # ── 并发取证 ──
        sem = asyncio.Semaphore(_DEEP_SUBQUERY_CONCURRENCY)  # 子查询并发上限

        async def _run_one(sq: dict) -> dict:
            async with sem:
                t_s = time.perf_counter()
                sub = DeepSearchRequest(
                    query=sq.get("ask", ""), mode="hybrid",
                    top_k=max(req.top_k, 10),
                    knowledge_base_ids=req.knowledge_base_ids,
                    save_history=False, with_reasoning=False,
                    strict_mode=False,
                )
                try:
                    r = await self.query_with_ontology(
                        tenant_id, user_id, sub, trace_id,
                    )
                    fact = self._extract_fact(r, sq)
                    collector.step(
                        STAGE_SUBQUERY, f"子查询·{sq.get('id', '?')}",
                        status="done" if fact.get("value") or fact.get("answer") else "skipped",
                        summary=f"{sq.get('ask', '')} → {fact.get('value') or fact.get('answer', '')[:100]}",
                        detail={"value": fact.get("value"), "source": fact.get("source"),
                                "grounded": fact.get("grounded"),
                                "ref_count": fact.get("ref_count")},
                        t_start=t_s,
                    )
                    return fact
                except Exception as e:
                    logger.warning("[deep] 子查询失败 id=%s: %s", sq.get("id"), e)
                    return {"sub_id": sq.get("id", ""), "value": None, "source": "error",
                            "grounded": False, "answer": "", "ref_count": 0}

        t_subq = time.perf_counter()
        tasks = [_run_one(sq) for sq in sub_questions]
        facts = await asyncio.gather(*tasks)
        t_subq_ms = int((time.perf_counter() - t_subq) * 1000)
        logger.info("[deep] 子查询完成 count=%d ms=%d", len(facts), t_subq_ms)

        # ── 汇总 ──
        t_synth = time.perf_counter()
        answer = await self._synthesize(
            req.query, facts, aggregation,
            tenant_id=tenant_id, user_id=user_id, trace_id=trace_id,
            allow_common_sense=req.allow_common_sense,
        )
        collector.step(
            STAGE_SYNTHESIS, "汇总与计算",
            summary=f"基于 {sum(1 for f in facts if f.get('value') or f.get('answer'))}/{len(facts)} 个要素完成 {aggregation.get('op', '?')}",
            detail={"facts": [{"id": f["sub_id"], "value": f.get("value")} for f in facts],
                    "aggregation_op": aggregation.get("op")},
            t_start=t_synth,
        )

        # 合并引用（去重：按 doc_id + chunk_index）
        all_refs: list[dict] = []
        seen_refs: set[tuple] = set()
        all_onto: list[dict] = []
        for f in facts:
            if f.get("source") == "ontology":
                all_onto.append({"name": f.get("value", ""), "source": "subquery"})
            for ref in f.get("refs", []):
                key = (ref.get("doc_id", ""), ref.get("chunk_index", -1))
                if key not in seen_refs:
                    seen_refs.add(key)
                    all_refs.append(ref)

        # ── [jonex] §10.4 deep 专用校验（要素级 grounded）──
        reliability = None
        if req.strict_mode and _STRICT_MODE_ENABLED:
            # deep 模式用要素级 grounded：检查每个子查询取到的要素是否可溯源
            elem_grounded = sum(1 for f in facts if f.get("grounded")) / max(len(facts), 1)
            elem_coverage = sum(1 for f in facts if f.get("value") or f.get("answer")) / max(len(sub_questions), 1)
            # 汇总答案非拒答检测
            refusal = any(pat.search(answer) for pat in _REFUSAL_PATTERNS)
            checks = {
                "non_refusal": 0.0 if refusal else 1.0,
                "has_reference": float(bool(all_refs)),
                "grounded": elem_grounded,
                "element_coverage": elem_coverage,
                "computation_transparent": 1.0 if re.search(r"(?:代入|公式|计算|推导|=|≈)", answer) else 0.5,
                "consistency": 0.5,
            }
            score = (
                _STRICT_W_NON_REFUSAL * checks["non_refusal"]
                + _STRICT_W_REFERENCE * checks["has_reference"]
                + _STRICT_W_GROUNDED * checks["grounded"]
                + 0.1 * checks["element_coverage"]
                + 0.1 * checks["computation_transparent"]
                + 0.15 * checks["consistency"]
            )
            score = min(max(score, 0.0), 1.0)
            verify_result = {"checks": checks, "score": score, "passed": score >= req.strict_min_score, "unmet": [k for k, v in checks.items() if v < 0.6]}
            reliability = self._build_reliability(verify_result, 1, min_score=req.strict_min_score)  # deep 单次编排视为 1 次尝试

        return {
            "answer": answer,
            "source": "deep",
            "references": all_refs or [],
            "ontology_instances": all_onto,
            "rag_used": any(f.get("source") == "rag" for f in facts),
            "knowledge_base_ids": kb_ids,
            "reasoning": collector.build("deep"),
            "reliability": reliability,
            "plan_summary": {
                "sub_queries": len(sub_questions),
                "completed": sum(1 for f in facts if f.get("value") or f.get("answer")),
                "aggregation": aggregation.get("op", "none"),
            },
            "facts_summary": [
                {"id": f["sub_id"], "value": f.get("value"), "source": f.get("source"),
                 "grounded": f.get("grounded")}
                for f in facts
            ],
        }

    async def query_with_ontology_strict(
        self,
        tenant_id: str,
        user_id: str,
        request: OntologySearchRequest | dict,
        trace_id: str | None = None,
    ) -> dict:
        """严格模式：多次升级重试 + 质量校验 + 可靠性说明（§9.6）。"""
        req = OntologySearchRequest(**_payload(request))
        t_all = time.perf_counter()
        cap = min(req.strict_max_attempts, _STRICT_MAX_ATTEMPTS_CAP)

        attempts: list[tuple[dict, dict]] = []  # (result, verify)
        best: tuple[dict, dict] | None = None
        prior_answers: list[str] = []

        # 推理链
        collector = ReasoningCollector(enabled=req.with_reasoning and _REASONING_ENABLED)

        for i in range(cap):
            gear = _STRICT_ESCALATION[min(i, len(_STRICT_ESCALATION) - 1)]
            t_i = time.perf_counter()
            label = gear.get("label", f"第{i + 1}次")

            # 构造升级参数（必须复位 strict_mode=False 防止无限递归）
            sub_req = req.copy(update={
                "top_k": gear["top_k"],
                "with_reasoning": req.with_reasoning,
                "strict_mode": False,
                "_route_score_min_override": gear.get("route_score_min", ONTOLOGY_ROUTE_SCORE_MIN),
                "_neighbor_depth_override": gear.get("neighbor_depth", ONTOLOGY_NEIGHBOR_DEPTH),
            })

            try:
                result = await asyncio.wait_for(
                    self.query_with_ontology(
                        tenant_id, user_id, sub_req, trace_id,
                    ),
                    timeout=_STRICT_ATTEMPT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                collector.step(
                    STAGE_STRICT_ATTEMPT, f"严格模式·第{i + 1}次尝试（{label}）",
                    status="failed",
                    summary=f"尝试超时（{_STRICT_ATTEMPT_TIMEOUT}s），进入下一档",
                    t_start=t_i,
                )
                continue

            # 校验
            chk = self._verify_answer(
                result["answer"], result.get("references", []), req.query,
                result.get("ontology_instances", []),
                result.get("facts") if isinstance(result, dict) else None,
                prior_answers=prior_answers,
                min_score=req.strict_min_score,
            )
            prior_answers.append(result["answer"])

            collector.step(
                STAGE_STRICT_ATTEMPT, f"严格模式·第{i + 1}次尝试（{label}）",
                status="done",
                summary=(
                    f"source={result.get('source', '?')} "
                    f"可靠性={chk['score']:.2f} "
                    f"{'达标' if chk['passed'] else '未达标'}"
                ),
                detail={
                    "gear": gear,
                    "checks": chk["checks"],
                    "score": chk["score"],
                    "answer_preview": result["answer"][:200],
                    "ref_count": len(result.get("references", [])),
                },
                t_start=t_i,
            )

            attempts.append((result, chk))
            if best is None or chk["score"] > best[1]["score"]:
                best = (result, chk)

            if chk["passed"]:
                collector.step(
                    STAGE_STRICT_VERIFY, "严格校验通过",
                    status="done",
                    summary=f"第{i + 1}次达标（{chk['score']:.2f} ≥ {req.strict_min_score}），提前返回",
                )
                break

            if time.perf_counter() - t_all > _STRICT_TOTAL_BUDGET:
                collector.step(
                    STAGE_STRICT_VERIFY, "严格校验中止",
                    status="failed",
                    summary="超总预算，返回当前最优",
                )
                break

        if best is None:
            # 全部超时 → 返回空结果
            return {
                "answer": "严格模式：所有尝试均超时，请稍后重试或关闭 strict_mode。",
                "source": "none",
                "references": [],
                "ontology_instances": [],
                "rag_used": False,
                "knowledge_base_ids": [],
                "reasoning": collector.build("none"),
                "reliability": {
                    "verdict": "best_effort",
                    "score": 0.0,
                    "attempts": cap,
                    "passed": False,
                    "checks": {},
                    "statement": "所有尝试均超时。",
                    "unmet": ["timeout"],
                },
            }

        result, chk = best
        result["reliability"] = self._build_reliability(chk, len(attempts), min_score=req.strict_min_score)
        # 合并 reasoning：strict 外层步骤在前 → 最佳那次的内层步骤在后
        strict_steps = collector._steps  # strict_attempt × N + strict_verify
        inner_reasoning = result.get("reasoning") or {}
        inner_steps = inner_reasoning.get("steps", [])
        merged = {
            "final_source": result.get("source", "unknown"),
            "total_ms": int((time.perf_counter() - t_all) * 1000),
            "strict": {
                "attempts": len(attempts),
                "verdict": result["reliability"]["verdict"],
                "score": chk["score"],
                "budget_ms": int(_STRICT_TOTAL_BUDGET * 1000),
                "used_ms": int((time.perf_counter() - t_all) * 1000),
            },
            "steps": strict_steps + inner_steps,
        }
        result["reasoning"] = merged
        return result

    async def query_with_ontology(
        self,
        tenant_id: str,
        user_id: str,
        request: OntologySearchRequest | dict,
        trace_id: str | None = None,
    ) -> dict:
        """本体优先 → RAG fallback 分流查询（多 KB 并行）。

        匹配策略（四级递进）：
          1a) 精确匹配   canonical_name / alias — 短查询高置信旁路
          1b) 前缀匹配   canonical_name — "研发"→"研发流程" 场景
          1c) cjk 全文检索 ont_entity_ft — BM25 模糊匹配
          1d) 向量语义召回 ont_entity_embedding — 同义/近义查询
          1e) RRF 融合全文+向量结果
        2. 路由判定：exact/prefix 恒走本体；向量余弦 ≥ ONTOLOGY_VECTOR_SCORE_MIN({:.2f})
           或全文 BM25 ≥ ONTOLOGY_ROUTE_SCORE_MIN({:.1f}) 走本体路径
        3. 本体路径：1-hop 邻域 → answer_from_facts → LLM 返回答案或 INSUFFICIENT
        4. 分低或 INSUFFICIENT 时降级 RAG（多 KB 并行 + LLM 融合）
        """.format(ONTOLOGY_VECTOR_SCORE_MIN, ONTOLOGY_ROUTE_SCORE_MIN)
        req = OntologySearchRequest(**_payload(request))
        # ── [jonex] §9 严格模式分派 ──
        if req.strict_mode and _STRICT_MODE_ENABLED:
            return await self.query_with_ontology_strict(
                tenant_id, user_id, request, trace_id,
            )
        # ── [jonex] M3 支持 strict escalation 按请求覆盖检索参数 ──
        raw = request if isinstance(request, dict) else _payload(request)
        _route_override = float(raw.get("_route_score_min_override", ONTOLOGY_ROUTE_SCORE_MIN))
        _neighbor_depth_override = int(raw.get("_neighbor_depth_override", ONTOLOGY_NEIGHBOR_DEPTH))
        _route_min = max(0.0, min(_route_override, ONTOLOGY_ROUTE_SCORE_MIN))
        _nb_depth = max(1, min(_neighbor_depth_override, ONTOLOGY_NEIGHBOR_DEPTH_MAX))

        kb_ids = await self._resolve_kb_ids(tenant_id, req)
        gdao = OntologyGraphRepository(get_neo4j_driver())

        # ── 编排推理链采集器（R1 双闸：请求开关 + 进程总闸）──
        collector = ReasoningCollector(enabled=req.with_reasoning and _REASONING_ENABLED)

        # ── 阶段 1：多 KB 本体实体匹配（采集点①）──
        ontology_instances: list[dict] = []
        t = time.perf_counter()
        try:
            ontology_instances = await self._match_ontology(gdao, tenant_id, kb_ids, req.query)
            collector.step(
                STAGE_ONTOLOGY_MATCH, "本体实体匹配",
                status="done" if ontology_instances else "skipped",
                summary=(f"命中 {len(ontology_instances)} 个实体"
                         if ontology_instances else "三级匹配均未命中"),
                detail={
                    "hits": [
                        {"name": i.get("name"), "score": i.get("score"), "kb_id": i.get("kb_id")}
                        for i in ontology_instances[:5]
                    ],
                    "total_hits": len(ontology_instances),
                    "kb_count": len(kb_ids),
                },
                t_start=t,
            )
        except Exception as e:
            collector.step(STAGE_ONTOLOGY_MATCH, "本体实体匹配",
                           status="failed", summary="本体检索失败，降级 OntoRAG", t_start=t)
            logger.warning("[ontology] 本体检索失败，降级 RAG: %s", e)

        # ── 阶段 2：路由决策（采集点②）──
        answer: str | None = None
        source = "rag"
        rag_used = True
        matched: dict | None = None
        facts: list[dict] | None = None    # [jonex] 方案⑦ _ontology_refs 需要

        if ontology_instances:
            # 路由判定：top-5 内任一命中即走本体（避免高 vscore 候选因全文 rank 落后被埋没）
            top_n = ontology_instances[:5]
            go_ontology = False
            for hit in top_n:
                src = hit.get("source", "")
                vs = hit.get("vscore", 0)
                fs = hit.get("ft_score", hit.get("score", 0))
                if src in ("exact", "prefix") or vs >= ONTOLOGY_VECTOR_SCORE_MIN or fs >= _route_min:
                    go_ontology = True
                    matched = hit
                    break

            top_source = matched.get("source", "") if matched else ""
            top_vscore = matched.get("vscore", 0) if matched else 0.0
            top_ftscore = matched.get("ft_score", matched.get("score", 0)) if matched else 0.0

            route_reason = (
                f"source={top_source}" if top_source in ("exact", "prefix")
                else f"vscore={top_vscore} ≥ {ONTOLOGY_VECTOR_SCORE_MIN}" if top_vscore >= ONTOLOGY_VECTOR_SCORE_MIN
                else f"ft_score={top_ftscore} ≥ {_route_min}" if top_ftscore >= _route_min
                else f"分数均不足（top-{len(top_n)} max_vscore={max((h.get('vscore',0) for h in top_n), default=0):.2f} max_ftscore={max((h.get('ft_score',h.get('score',0)) for h in top_n), default=0):.2f}）"
            )
            collector.step(
                STAGE_ROUTE_DECISION, "路由决策",
                summary=(f"走本体路径（{route_reason}）"
                         if go_ontology else
                         f"降级 OntoRAG（{route_reason}）"),
                detail={"source": top_source, "vscore": top_vscore, "ft_score": top_ftscore,
                        "vscore_threshold": ONTOLOGY_VECTOR_SCORE_MIN,
                        "ftscore_threshold": _route_min,
                        "route": "ontology" if go_ontology else "rag"},
            )
            if go_ontology:
                top_name = matched.get("name", "")
                top_kb_id = matched.get("kb_id") or kb_ids[0]
                logger.info(
                    "[ontology] 路由=本体 query=%r top_name=%s top_kb=%s source=%s vscore=%.4f ft_score=%s",
                    req.query, top_name, top_kb_id, top_source, top_vscore, top_ftscore,
                )

                # ── [jonex] P1-6 时间线/枚举/计数意图 → 图查询模板 ──
                top_type = matched.get("type", "")
                timeline = self._detect_timeline_intent(req.query)
                graph_facts = None
                if timeline and top_type in (
                    "SoftwareRelease", "SoftwareToolkit", "HardwareArchitecture", "Software",
                ):
                    # 把 top_name 作为 fallback 版本号参数注入意图检测
                    if not timeline["params"].get("version"):
                        timeline["params"]["query_hint"] = req.query
                        # 从 top_name 推测版本号（如 "CUDA Toolkit 12.8" → "12.8"）
                        v_match = _VERSION_RE.search(top_name)
                        if v_match:
                            timeline["params"]["version"] = v_match.group(1)
                    graph_facts = await self._execute_graph_query(
                        gdao, tenant_id, top_kb_id, timeline, collector=collector,
                    )

                # ── 阶段 3：邻域取证（采集点③，独立 try）──
                t = time.perf_counter()
                facts = graph_facts  # P1-6 图查询模板已取到 fact 则跳过 neighbors()
                if facts is None:
                    try:
                        neighbor_data = await asyncio.wait_for(
                            gdao.neighbors(
                                tenant_id, top_kb_id, top_name,
                                limit=ONTOLOGY_NEIGHBOR_LIMIT,
                                depth=_nb_depth,
                                per_hop_limit=ONTOLOGY_NEIGHBOR_PER_HOP_LIMIT,
                            ),
                            timeout=ONTOLOGY_NEIGHBOR_TIMEOUT,
                        )
                        facts = neighbor_data.get("facts", [])
                        neighbor_depth = neighbor_data.get("depth", 1)
                        collector.step(
                            STAGE_FACT_LOOKUP, "邻域事实检索",
                            summary=(
                                f"取到 {len(facts)} 条事实"
                                + (f"（{neighbor_depth} 跳）"
                                   if neighbor_depth > 1 else "（1 跳）")
                            ),
                            detail={
                                "entity": top_name,
                                "kb_id": top_kb_id,
                                "fact_count": len(facts),
                                "depth": neighbor_depth,
                                "hop_distribution": neighbor_data.get("hop_distribution", {}),
                                "truncated": neighbor_data.get("truncated", False),
                                "facts": facts,
                            },
                            t_start=t,
                        )
                    except asyncio.TimeoutError:
                        collector.step(STAGE_FACT_LOOKUP, "邻域事实检索", status="failed",
                                       summary="邻域查询超时，降级 OntoRAG", t_start=t)
                        logger.warning("[ontology] 邻域查询超时（%ds），降级 RAG", ONTOLOGY_NEIGHBOR_TIMEOUT)
                    except Exception as e:
                        collector.step(STAGE_FACT_LOOKUP, "邻域事实检索", status="failed",
                                       summary="邻域检索失败，降级 OntoRAG", t_start=t)
                        logger.warning("[ontology] 邻域检索失败，降级 RAG: %s", e)

                # ── 阶段 4：本体作答（采集点④，独立 try）──
                if facts is not None:
                    # 方案④b：事实量预判 — 低于阈值直接跳过本体作答
                    if ONTOLOGY_MIN_FACTS > 0 and len(facts) < ONTOLOGY_MIN_FACTS:
                        collector.step(STAGE_LLM_ANSWER, "本体事实作答", status="skipped",
                                       summary=f"事实不足（{len(facts)} < {ONTOLOGY_MIN_FACTS}），降级 OntoRAG")
                        logger.info(
                            "[ontology] 事实量预判跳过作答 facts=%d min=%d query=%r",
                            len(facts), ONTOLOGY_MIN_FACTS, req.query,
                        )
                        facts = None   # 触发 RAG 降级
                if facts is not None:
                    t = time.perf_counter()
                    try:
                        # [jonex] P2-7: 图查询模板取到的事实 → 放宽常识边界
                        _from_graph_template = bool(
                            graph_facts and facts
                            and any(f.get("source") == "graph_query" for f in facts[:1])
                        )
                        llm_answer = await asyncio.wait_for(
                            answer_from_facts(
                                req.query, ontology_instances, facts,
                                tenant_id=tenant_id,
                                kb_id=top_kb_id,
                                user_id=user_id,
                                trace_id=trace_id,
                                allow_common_sense=_from_graph_template,
                            ),
                            timeout=ONTOLOGY_ANSWER_TIMEOUT,   # [jonex] 方案④ 可调超时
                        )
                        if llm_answer and llm_answer != "INSUFFICIENT":
                            answer = llm_answer
                            source = "ontology"
                            rag_used = False
                            collector.step(STAGE_LLM_ANSWER, "本体事实作答",
                                           summary="基于本体事实生成答案", t_start=t)
                        else:
                            collector.step(STAGE_LLM_ANSWER, "本体事实作答", status="skipped",
                                           summary="事实不足（INSUFFICIENT），降级 OntoRAG", t_start=t)
                    except asyncio.TimeoutError:
                        collector.step(STAGE_LLM_ANSWER, "本体事实作答", status="failed",
                                       summary=f"本体 LLM 超时（{ONTOLOGY_ANSWER_TIMEOUT}s），降级 OntoRAG", t_start=t)
                        logger.warning("[ontology] 本体 LLM 回答超时（%ds），降级 RAG", ONTOLOGY_ANSWER_TIMEOUT)
                    except Exception as e:
                        collector.step(STAGE_LLM_ANSWER, "本体事实作答", status="failed",
                                       summary="本体作答失败，降级 OntoRAG", t_start=t)
                        logger.warning("[ontology] 本体问答失败，降级 RAG: %s", e)
            else:
                logger.info(
                    "[ontology] 路由=RAG降级（命中但分数不足）source=%s vscore=%.4f ft_score=%s query=%r",
                    top_source, top_vscore, top_ftscore, req.query,
                )

        # ── 阶段 5/6：RAG Fallback + 融合（采集点⑤⑥在 _rag_fallback_multi 内部）──
        references: list[dict] = []
        if answer is None:
            fallback = await self._rag_fallback_multi(
                tenant_id, user_id, req, kb_ids, trace_id, collector=collector)
            answer = fallback["answer"]
            references = fallback["references"]
            source = "rag"
        else:
            # 方案⑦：本体路径成功，从 source_chunks 直接构建 chunk 级引用
            references = await self._ontology_refs(
                tenant_id=tenant_id, kb_ids=kb_ids,
                ontology_instances=ontology_instances, facts=facts,
                collector=collector,
            )

        return {
            "answer": answer,
            "source": source,
            "references": references,
            "ontology_instances": ontology_instances,
            "rag_used": rag_used,
            "knowledge_base_ids": kb_ids,
            "reasoning": collector.build(source),
        }

    # ── [jonex] OpenKB 分流 — 批量管线查询、search_llmwiki、search_mix ──

    async def _build_openkb_references(
        self, tenant_id: str, per_kb_traces: list[tuple[str, list[dict]]],
    ) -> list[dict]:
        """[jonex] 从 agent 的 wiki 浏览轨迹反解出引用。

        per_kb_traces: [(kb_id, turns), ...] 其中 turns 是 _extract_run_trace 产出。
        只有 summaries/ 与 sources/ 下的页面能对应到 Jonex 文档。
        entities/concepts 页不做为引用（无对应 PG 文档）。
        """
        import uuid as _uuid
        from ..dtos.reference import SourceReference

        # ① 收集所有可能的 document_id（去重保序）
        seen: set[str] = set()
        ref_sources: list[tuple[str, str, str]] = []  # (doc_id, wiki_path, kb_id)
        for kb_id, turns in per_kb_traces:
            for turn in (turns or []):
                for call in (turn.get("calls") or []):
                    path = (call.get("args") or {}).get("path", "")
                if not path:
                    continue
                # summaries/xxx.md 或 sources/xxx.md → stem 可能是 uuid
                for prefix in ("summaries/", "sources/"):
                    if path.startswith(prefix):
                        stem = path[len(prefix):].removesuffix(".md")
                        try:
                            doc_id = str(_uuid.UUID(stem))
                        except (ValueError, AttributeError):
                            continue
                        if doc_id not in seen:
                            seen.add(doc_id)
                            ref_sources.append((doc_id, path, kb_id))
                        break

        if not ref_sources:
            return []

        # ② 批量查 PG
        doc_ids = [r[0] for r in ref_sources]
        async with get_db_session() as session:
            from ..repository.document_repository import KnowledgeDocumentRepository
            repo = KnowledgeDocumentRepository(session)
            docs = await repo.get_by_ids(doc_ids, tenant_id)
        doc_map = {d.id: d for d in docs}

        # ③ 富化（照 _build_references 的口径，但不走它的 chunk 入参）
        storage = get_object_storage()
        out: list[dict] = []
        for doc_id, wiki_path, kb_id in ref_sources:
            d = doc_map.get(doc_id)
            if d is None:
                continue
            raw_url: str | None = None
            try:
                raw_url = await storage.get_presigned_url(
                    d.storage_key or build_object_key(kb_id, d.id, d.file_name or ""),
                )
            except Exception:
                raw_url = None
            ref = SourceReference(
                doc_id=doc_id,
                kb_id=kb_id,
                file_name=d.file_name or "",
                mime_type=d.mime_type,
                file_size=d.file_size,
                media_type=classify_media(d.mime_type, d.file_name),
                raw_url=raw_url,
                wiki_path=wiki_path,
            )
            out.append(ref.dict())
        return out

    @staticmethod
    def _is_openkb_effective_answer(answer: str | None) -> bool:
        """[jonex] 判断 OpenKB run_query 的返回是否算有效答案（D9）。

        无效：None / 空串 / 仅空白 / 命中 no-answer 文案特征。
        判据故意保守：宁可把兜底文案当成有效答案透出，也不要把正常答案误判成无答案。
        """
        if answer is None:
            return False
        if not answer.strip():
            return False
        # 保守：只把明确的固定 no-answer 文案视为无效，
        # 不包含通用关键词（避免正常答案含"没有找到"被判无效）。
        no_answer_markers = (
            "I'm sorry",
            "I am sorry",
            "Sorry, I'm not able",
            "Sorry, I am not able",
            "[no-context]",
        )
        stripped = answer.strip()
        for marker in no_answer_markers:
            if stripped.startswith(marker):
                return False
        return True

    async def _get_kb_types(self, tenant_id: str, kb_ids: list[str]) -> dict[str, str]:
        """[jonex] 批量查询 KB 的 kb_type（转调公共 helper）。"""
        from .kb_type_service import get_kb_types
        return await get_kb_types(tenant_id, kb_ids)

    async def search_llmwiki(
        self,
        tenant_id: str,
        user_id: str,
        request: LlmWikiSearchRequest | dict,
        trace_id: str | None = None,
    ) -> dict:
        """[jonex] OpenKB Wiki 检索（只查 openkb 管线，D3 异管线拒绝）。"""
        tenant_id = require_tenant(tenant_id)
        req = LlmWikiSearchRequest(**_payload(request))
        t_total = time.perf_counter()

        # 1. KB 解析 + 租户校验
        kb_ids = await self._resolve_kb_ids(tenant_id, req)

        # 2. 管线校验：必须全部为 openkb
        pipeline_map = await self._get_kb_types(tenant_id, kb_ids)
        invalid_ids = [k for k in kb_ids if pipeline_map.get(k) != "openkb"]
        if invalid_ids:
            raise InvalidParameterError(
                message=translate(
                    "err.search.kb_pipeline_mismatch_llmwiki",
                    params={"kb_ids": ", ".join(invalid_ids)},
                    fallback=f"知识库 {', '.join(invalid_ids)} 使用 LightRAG 管线，请改用 /search/ontology 或统一入口 /search/mix",
                ),
                details={"invalid_kb_ids": invalid_ids, "expected_pipeline": "openkb"},
            )

        # 3. 推理链采集器
        collector = ReasoningCollector(enabled=req.with_reasoning and _REASONING_ENABLED)

        # 4. 多 KB 并行查询 OpenKB（信号量限流）
        compiler = KnowledgeCompilerService()
        _sem = asyncio.Semaphore(OPENKB_SEARCH_MAX_CONCURRENCY)
        _trace_by_kb: list[tuple[str, list[dict]]] = []  # [jonex] 收集各 KB 的 wiki 浏览轨迹

        # [jonex] D11: references 依赖 trace，不受 with_reasoning 开关控制
        _want_trace = collector.enabled or OPENKB_REFERENCES_ENABLED

        async def _query_one_okb(kid: str) -> tuple[str, object, int]:
            _t0 = time.perf_counter()
            async with _sem:
                try:
                    _res = await compiler.search_compiled_knowledge(
                        kb_name=kid, tenant_id=tenant_id, kb_id=kid, question=req.query,
                        return_trace=_want_trace,
                    )
                    return (kid, _res, int((time.perf_counter() - _t0) * 1000))
                except Exception as e:
                    return (kid, e, int((time.perf_counter() - _t0) * 1000))

        tasks = [_query_one_okb(kid) for kid in kb_ids]
        results = await asyncio.gather(*tasks)

        per_kb: list[dict] = []
        kb_failed: list[str] = []
        kb_no_answer: list[str] = []
        for kid, res, kb_ms in results:
            if isinstance(res, Exception):
                logger.warning("[openkb] query 失败 kb=%s: %s", kid, res)
                kb_failed.append(kid)
                collector.step(
                    STAGE_OPENKB_QUERY, f"LLM Wiki 检索 · {kid}",
                    status="failed",
                    summary="LLM Wiki 查询异常",
                    detail={"kb_id": kid, "error": str(res)[:200], "duration_ms": kb_ms},
                )
                continue
            answer = res.get("answer") if isinstance(res, dict) else str(res)
            trace_data = res.get("trace") if isinstance(res, dict) else None
            _turns = list((trace_data or {}).get("turns") or [])
            _turn_count = (trace_data or {}).get("turn_count", len(_turns))
            _llm_total_ms = (trace_data or {}).get("llm_total_ms")
            # [jonex] D8 截断：按跨轮 calls 总数截断，保留所有轮及 thinking/llm_ms，只截 calls
            _calls_total = sum(len(t.get("calls", [])) for t in _turns)
            _truncated = _calls_total > OPENKB_TRACE_MAX_CALLS
            if _truncated:
                _remaining = OPENKB_TRACE_MAX_CALLS
                for t in _turns:
                    _tc = t.get("calls", [])
                    if len(_tc) <= _remaining:
                        _remaining -= len(_tc)
                    else:
                        t["calls"] = _tc[:_remaining]
                        _remaining = 0
            if not self._is_openkb_effective_answer(answer):
                logger.info("[openkb] 无效答案 kb=%s answer_preview=%r", kid, (answer or "")[:80])
                kb_no_answer.append(kid)
                collector.step(
                    STAGE_OPENKB_QUERY, f"LLM Wiki 检索 · {kid}",
                    status="skipped",
                    summary="未找到有效答案",
                    detail={
                        "kb_id": kid,
                        "answer_preview": (answer or "")[:200],
                        "turns": _turns,
                        "turn_count": _turn_count,
                        "duration_ms": kb_ms,
                    },
                )
                continue
            per_kb.append({"kb_id": kid, "answer": answer, "source": "llm-wiki"})
            _trace_by_kb.append((kid, _turns))
            _detail: dict = {
                "kb_id": kid,
                "answer_preview": answer[:200],
                "turns": _turns,
                "turn_count": _turn_count,
                "llm_total_ms": _llm_total_ms,
                "duration_ms": kb_ms,
            }
            if _truncated:
                _detail["tool_calls_truncated"] = True
                _detail["tool_calls_total"] = _calls_total
            _summary_calls = sum(len(t.get("calls", [])) for t in _turns)
            collector.step(
                STAGE_OPENKB_QUERY, f"LLM Wiki 检索 · {kid}",
                summary=f"返回有效答案（{_turn_count} 轮，{_summary_calls} 次工具调用）",
                detail=_detail,
            )

        # 6. 无有效答案 → source="none"
        if not per_kb:
            answer = translate(
                "msg.search.openkb_no_answer",
                fallback="未在 Wiki 知识库中找到相关信息，请尝试调整查询。",
            )
            source = "none"
            rag_used = False
        elif len(per_kb) == 1:
            answer = per_kb[0]["answer"]
            source = "llm-wiki"
            rag_used = False
            collector.step(STAGE_FUSION, "多答案融合", status="skipped",
                           summary="仅 1 个有效答案，无需融合")
        else:
            # 多 KB 融合（同 _rag_fallback_multi 的 top-N 策略）
            per_kb_fused = per_kb[:ONTOLOGY_RAG_FUSION_TOPN]
            t_fuse = time.perf_counter()
            answer = await fuse_rag_answers(
                req.query, per_kb_fused,
                tenant_id=tenant_id, user_id=user_id, trace_id=trace_id,
            )
            fusion_ms = int((time.perf_counter() - t_fuse) * 1000)
            collector.step(STAGE_FUSION, "多答案融合",
                           summary=f"融合 {len(per_kb_fused)} 个知识库的 Wiki 答案",
                           t_start=t_fuse)
            source = "llm-wiki"
            rag_used = False

        # 7. 组装响应（references 从 wiki 浏览轨迹反解，ontology_instances 仍空）
        total_ms = int((time.perf_counter() - t_total) * 1000)
        _references = await self._build_openkb_references(tenant_id, _trace_by_kb) if _trace_by_kb else []
        result = {
            "answer": answer,
            "source": source,
            "references": _references,
            "ontology_instances": [],
            "rag_used": rag_used,
            "knowledge_base_ids": kb_ids,
            "reasoning": collector.build(source),
            "references_available": bool(_references),
            "ontology_instances_available": False,
        }

        # 8. 保存检索历史（D8: 多 KB knowledge_base_id=""）
        if req.save_history:
            await self._history.save_history(
                tenant_id, user_id,
                SearchHistoryCreateRequest(
                    query=req.query,
                    knowledge_base_id="" if len(kb_ids) > 1 else kb_ids[0],
                    mode=req.mode,
                    top_k=req.top_k,
                    domain_space_id=req.domain_space_id,
                    answer_preview=answer[:300],
                    duration_ms=total_ms,
                    metadata={
                        "knowledge_base_ids": kb_ids,
                        "source": source,
                        "pipeline": "llm-wiki",
                    },
                ),
            )

        return result

    async def search_mix(
        self,
        tenant_id: str,
        user_id: str,
        request: MixSearchRequest | dict,
        trace_id: str | None = None,
    ) -> dict:
        """[jonex] 混合管线检索统一入口（分流器，D4）。

        按 pipeline_type 分组扇出到 LightRAG / OpenKB，支持混合选库。
        """
        tenant_id = require_tenant(tenant_id)
        req = MixSearchRequest(**_payload(request))
        collector = ReasoningCollector(enabled=req.with_reasoning and _REASONING_ENABLED)
        t_total = time.perf_counter()

        # 1. 用原始 kb_ids 查管线类型（不做去重/上限/租户校验，交给下游）
        raw_ids = list(dict.fromkeys(
            k.strip() for k in req.knowledge_base_ids if k and k.strip()
        ))
        if not raw_ids:
            raise InvalidParameterError(
                message=translate("err.search.kb_required",
                                  fallback="请至少指定一个知识库（knowledge_base_ids 不能为空）"),
            )

        pipeline_map = await self._get_kb_types(tenant_id, raw_ids)
        lightrag_ids = [k for k in raw_ids if pipeline_map.get(k) != "openkb"]
        openkb_ids = [k for k in raw_ids if pipeline_map.get(k) == "openkb"]

        # 2. 路由决策（reasoning 阶段①）
        collector.step(
            STAGE_ROUTE_DECISION, "分流决策",
            summary=f"lightrag={len(lightrag_ids)} KB, openkb={len(openkb_ids)} KB",
            detail={
                "pipeline_groups": {"lightrag": lightrag_ids, "llm-wiki": openkb_ids},
                "lightrag_count": len(lightrag_ids),
                "openkb_count": len(openkb_ids),
            },
        )

        # 3. 全 lightrag → 委托 query_with_ontology
        if not openkb_ids:
            req_ont = OntologySearchRequest(
                query=req.query, mode=req.mode, top_k=req.top_k,
                knowledge_base_ids=lightrag_ids,
                save_history=req.save_history,
                with_reasoning=req.with_reasoning,
                domain_space_id=req.domain_space_id,
                strict_mode=req.strict_mode,
                strict_max_attempts=req.strict_max_attempts,
                strict_min_score=req.strict_min_score,
                strict_require_reference=req.strict_require_reference,
                strict_require_grounded=req.strict_require_grounded,
            )
            return await self.query_with_ontology(tenant_id, user_id, req_ont, trace_id=trace_id)

        # 4. 全 openkb → 委托 search_llmwiki
        if not lightrag_ids:
            req_wiki = LlmWikiSearchRequest(
                query=req.query, mode=req.mode, top_k=req.top_k,
                knowledge_base_ids=openkb_ids,
                save_history=req.save_history,
                with_reasoning=req.with_reasoning,
                domain_space_id=req.domain_space_id,
            )
            return await self.search_llmwiki(tenant_id, user_id, req_wiki, trace_id=trace_id)

        # 5. 混合 → 两侧并行，子调用强制 save_history=False（D8.1）
        async def _run_lightrag() -> dict | Exception:
            try:
                req_ont = OntologySearchRequest(
                    query=req.query, mode=req.mode, top_k=req.top_k,
                    knowledge_base_ids=lightrag_ids,
                    save_history=False,
                    with_reasoning=req.with_reasoning,
                    domain_space_id=req.domain_space_id,
                    strict_mode=req.strict_mode,
                    strict_max_attempts=req.strict_max_attempts,
                    strict_min_score=req.strict_min_score,
                    strict_require_reference=req.strict_require_reference,
                    strict_require_grounded=req.strict_require_grounded,
                )
                return await self.query_with_ontology(
                    tenant_id, user_id, req_ont, trace_id=trace_id,
                )
            except Exception as e:
                return e

        async def _run_openkb() -> dict | Exception:
            try:
                req_wiki = LlmWikiSearchRequest(
                    query=req.query, mode=req.mode, top_k=req.top_k,
                    knowledge_base_ids=openkb_ids,
                    save_history=False,
                    with_reasoning=req.with_reasoning,
                    domain_space_id=req.domain_space_id,
                )
                return await self.search_llmwiki(tenant_id, user_id, req_wiki, trace_id=trace_id)
            except Exception as e:
                return e

        lr_result, okb_result = await asyncio.gather(_run_lightrag(), _run_openkb())

        # 6. 判定两侧成败
        lr_ok = isinstance(lr_result, dict)
        okb_ok = isinstance(okb_result, dict)
        lr_answer = (lr_result.get("answer") or "") if lr_ok else ""
        okb_answer = (okb_result.get("answer") or "") if okb_ok else ""
        # source="rag" 时 references + ontology_instances 双空 = RAG 未找到任何实质内容
        #（"抱歉，所有知识库..."兜底），此时应视为无效，避免把空结果当有效答案参与融合
        lr_effective = lr_ok and (
            lr_result.get("source") == "ontology"
            or bool(lr_result.get("references"))
            or bool(lr_result.get("ontology_instances"))
        )
        okb_effective = okb_ok and okb_result.get("source") not in ("none", "")

        # [jonex] D9: 合并子链路的推理步骤。
        # 子调用已按 with_reasoning 产出 reasoning，不合并则用户混选时看不到 wiki 浏览过程。
        # 按管线分组（lightrag → llm-wiki），不按时间交错。
        if collector.enabled:
            for _side, _res in (("lightrag", lr_result), ("llm-wiki", okb_result)):
                if not isinstance(_res, dict):
                    continue
                _sub = (_res.get("reasoning") or {}).get("steps") or []
                for _s in _sub:
                    collector.step(
                        _s.get("stage", ""),
                        f"[{_side}] {_s.get('title', '')}",
                        status=_s.get("status", "done"),
                        summary=_s.get("summary"),
                        detail=_s.get("detail"),
                    )

        # 7. 融合
        if lr_effective and okb_effective:
            # 两侧都有效 → 融合
            per_kb = [
                {"kb_id": kid, "answer": lr_answer, "source": lr_result.get("source", "rag")}
                for kid in lightrag_ids[:1]  # 融合只取各侧一个代表答案
            ]
            per_kb.append({
                "kb_id": openkb_ids[0], "answer": okb_answer, "source": "llm-wiki",
            })
            t_fuse = time.perf_counter()
            answer = await fuse_rag_answers(
                req.query, per_kb,
                tenant_id=tenant_id, user_id=user_id, trace_id=trace_id,
            )
            fusion_ms = int((time.perf_counter() - t_fuse) * 1000)
            collector.step(STAGE_FUSION, "多答案融合",
                           summary=f"融合 lightrag + openkb 两侧答案", t_start=t_fuse)
            source = "mixed"
            references = lr_result.get("references") or []
            ontology_instances = lr_result.get("ontology_instances") or []
            rag_used = lr_result.get("rag_used", False)
            references_available = bool(references)
            ontology_instances_available = bool(ontology_instances)
        elif lr_effective:
            # 仅 lightrag 成功
            answer = lr_answer
            source = lr_result.get("source", "rag")
            references = lr_result.get("references") or []
            ontology_instances = lr_result.get("ontology_instances") or []
            rag_used = lr_result.get("rag_used", False)
            references_available = bool(references)
            ontology_instances_available = bool(ontology_instances)
            collector.step(
                STAGE_FUSION, "多答案融合", status="skipped",
                summary="仅 lightrag 侧有效，openkb 侧失败/无答案",
                detail={"partial_failed": True, "failed_pipeline": "llm-wiki",
                        "error": str(okb_result) if not okb_ok else "no_answer"},
            )
        elif okb_effective:
            # 仅 openkb 成功
            answer = okb_answer
            source = okb_result.get("source", "llm-wiki")
            references = []
            ontology_instances = []
            rag_used = False
            references_available = False
            ontology_instances_available = False
            collector.step(
                STAGE_FUSION, "多答案融合", status="skipped",
                summary="仅 openkb 侧有效，lightrag 侧失败/无答案",
                detail={"partial_failed": True, "failed_pipeline": "lightrag",
                        "error": str(lr_result) if not lr_ok else "no_answer"},
            )
        else:
            # 两侧都无有效答案
            answer = translate(
                "msg.search.openkb_no_answer",
                fallback="未在知识库中找到相关信息，请尝试调整查询。",
            )
            source = "none"
            references = []
            ontology_instances = []
            rag_used = False
            references_available = False
            ontology_instances_available = False

        total_ms = int((time.perf_counter() - t_total) * 1000)
        result = {
            "answer": answer,
            "source": source,
            "references": references,
            "ontology_instances": ontology_instances,
            "rag_used": rag_used,
            "knowledge_base_ids": raw_ids,
            "reasoning": collector.build(source),
            "references_available": references_available,
            "ontology_instances_available": ontology_instances_available,
        }

        # 8. 统一保存检索历史（D8.1：混合模式只写 1 条）
        if req.save_history:
            await self._history.save_history(
                tenant_id, user_id,
                SearchHistoryCreateRequest(
                    query=req.query,
                    knowledge_base_id="",  # 混合/多 KB
                    mode=req.mode,
                    top_k=req.top_k,
                    domain_space_id=req.domain_space_id,
                    answer_preview=answer[:300],
                    duration_ms=total_ms,
                    metadata={
                        "knowledge_base_ids": raw_ids,
                        "pipeline_groups": {"lightrag": lightrag_ids, "llm-wiki": openkb_ids},
                        "source": source,
                    },
                ),
            )

        return result

    # ── [jonex] OpenKB kb_type 分流（/search 用，单 KB）──

    async def _get_kb_type(self, tenant_id: str, kb_id: str) -> str:
        """[jonex] 查询 KB 的 kb_type（转调公共 helper）。"""
        from .kb_type_service import get_kb_type
        return await get_kb_type(tenant_id, kb_id)

    async def _search_openkb(self, tenant_id: str, user_id: str, req) -> dict:
        """openkb 管线查询 — 调用 OpenKB /query 而非 LightRAG。"""
        compiler = KnowledgeCompilerService()
        result = await compiler.search_compiled_knowledge(
            kb_name=req.knowledge_base_id,
            tenant_id=tenant_id,
            kb_id=req.knowledge_base_id,
            question=req.query,
        )
        return {
            "query": req.query,
            "answer": result.get("answer", ""),
            "mode": req.mode,
            "top_k": req.top_k,
            "references": [],
            "metadata": {
                "knowledge_base_id": req.knowledge_base_id,
                "kb_type": "openkb",
                "duration_ms": 0,
            },
        }


__all__ = ["SearchService"]
