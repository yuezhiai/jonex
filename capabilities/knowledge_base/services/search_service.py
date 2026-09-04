"""Search service for Knowledge Base."""

import asyncio
import hashlib
import json
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
from jonex_core.common.file_source_util import (
    classify_media,
    parse_file_source,
    resolve_page_by_offset,   # [jonex] §block-packing 改动 4：跨页包 pspans 精算
    to_location,
)
from jonex_core.common.neo4j_client import get_neo4j_driver
from jonex_core.common.object_storage import (
    build_asset_key,     # [jonex] §image-refs P2-1: 图片资产对象键
    build_object_key,
    get_object_storage,
)
from jonex_core.common.ontology_embedding import embed
from jonex_core.common.ontology_llm import (
    _scene,               # [jonex] 查询期思考分档（快档派生 `_fast` scene）
    answer_from_chunks,   # [jonex] 方案 A：平台侧基于 chunk 作答
    answer_from_facts,
    arbitrate_answers,    # [jonex] S1+S7 双向校验裁决
    fuse_rag_answers,
)
from jonex_core.common.tenant import require_tenant

from ..dtos import LlmWikiSearchRequest, MixSearchRequest, DeepSearchRequest, DeepSearchResponse, OntologySearchRequest, ReliabilityInfo, SearchHistoryCreateRequest, SearchRequest
from ..dtos.reasoning import (
    STAGE_ARBITRATION,       # [jonex] S1+S7 双向校验裁决
    STAGE_CHUNK_ANSWER,      # [jonex] 方案 A：平台侧基于 chunk 作答
    STAGE_CONTEXT_RETRIEVE,  # [jonex] 方案 A：多 KB 只召回不生成
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

# ── [jonex] §10 L2：跨页打包 chunk 引用页精算（页段打分）──
# 打分方式选型（§10.5③）：关键词命中打分——零外部依赖/延迟；embedding 打分
# 需经 llm-gateway（有计量成本），留待后续迭代。打分失败/无命中时回落
# resolve_page_by_offset(pspans, 0)（chunk 起点页，行为与改造前一致）。
RAG_REF_PAGE_SCORING_ENABLED = os.getenv(
    "RAG_REF_PAGE_SCORING_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")

# ── 本体多跳邻域召回配置 ──
ONTOLOGY_NEIGHBOR_DEPTH_MAX = max(1, int(os.getenv("ONTOLOGY_NEIGHBOR_DEPTH_MAX", "3")))
ONTOLOGY_NEIGHBOR_DEPTH = max(1, min(
    int(os.getenv("ONTOLOGY_NEIGHBOR_DEPTH", "1")), ONTOLOGY_NEIGHBOR_DEPTH_MAX))
ONTOLOGY_NEIGHBOR_LIMIT = max(1, int(os.getenv("ONTOLOGY_NEIGHBOR_LIMIT", "20")))
ONTOLOGY_NEIGHBOR_PER_HOP_LIMIT = max(1, int(os.getenv("ONTOLOGY_NEIGHBOR_PER_HOP_LIMIT", "50")))
ONTOLOGY_NEIGHBOR_TIMEOUT = max(1, int(os.getenv("ONTOLOGY_NEIGHBOR_TIMEOUT", "15")))
# ── [jonex] 多意图邻域取证：单次查询最多并发展开多少个「过线命中」实体 ──
# 查询含多个实体（如「甲实体与乙实体什么关系」）时，对 top-5 中所有过线命中逐一
# neighbors() 展开邻域、合并 facts 去重再作答，避免只取 top1 漏掉其他实体的关系。
# 上限 3 平衡召回完整度与 Neo4j 查询次数（并发 gather，复用 ONTOLOGY_NEIGHBOR_TIMEOUT 整体超时）。
ONTOLOGY_NEIGHBOR_ENTITY_MAX = max(1, int(os.getenv("ONTOLOGY_NEIGHBOR_ENTITY_MAX", "3")))

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

# 升级档位（[jonex] 两档结构，docs/ontology-query-thinking-latency-fix-plan.md §3.3/§3.4）：
#   快档 = 窄召回 + 1 跳 + 禁思考 + 跳过交叉校验 → 查表式查询 1~3s 秒回。
#     禁思考避免思考 token 吃光 max_tokens 导致正文为空（根因 1/2）；
#     跳过交叉校验是因为 desc-only 实体判 mid 会触发 20~96s 的对侧 RAG，
#     必然撞破档超时 → continue → 该档答案被整体丢弃（query_with_ontology_strict
#     的 TimeoutError 分支），白花时间。交叉校验不是被省掉，而是挪到精档。
#   精档 = 全量召回 + 3 跳 + 开思考 + 完整交叉校验 → 快档打分不达标时补齐推理。
# 要不要推理由 _verify_answer 的实测打分决定，不预判查询复杂度。
#
# ⚠️ 超时分层规则：每档「各内层阶段上限之和」必须 < 该档 timeout，
#    否则档超时会丢弃已生成答案（见上）。台账（§3.4）：
#      快档 8(邻域) + 12(作答) + 3(refs/verify) = 23 < 25 ✅
#      精档 15(邻域) + 60(作答) + 20(对侧) + 8(裁决) + 5(refs/verify) = 108 < 120 ✅
#      合计 25 + 120 = 145 < STRICT_TOTAL_BUDGET(150) ✅
#    改任一超时都必须重算本台账，并确认前端/网关 HTTP 超时 > 各档之和。
_STRICT_ESCALATION: list[dict] = [
    {"top_k": 5,  "neighbor_depth": 1, "route_score_min": 1.0,
     "thinking": False, "cross_verify": False,
     "neighbor_timeout": 8, "answer_timeout": 12,
     "timeout": 25,  "label": "快档"},
    {"top_k": 25, "neighbor_depth": 3, "route_score_min": 0.5,
     "thinking": True,  "cross_verify": True,
     "neighbor_timeout": 15, "answer_timeout": 60,
     "timeout": 120, "label": "精档"},
]

# ── P1-5 RAG 召回后处理配置 ──
# 送 LLM 融合前的 chunk 级重排（经 llm-gateway /v1/rerank，与 LightRAG 的 RERANK_BINDING 同源）
RAG_PRELLM_RERANK_ENABLED = os.getenv(
    "RAG_PRELLM_RERANK_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
RAG_PRELLM_RERANK_TOPK = max(1, int(os.getenv("RAG_PRELLM_RERANK_TOPK", "8")))
# 主体一致性信号总开关（[jonex] 方案 B：语义从「硬剔除过滤」改为「软加权」，
# 见 docs/rag-subject-filter-and-answer-source-remediation-plan.md §5）
RAG_SUBJECT_FILTER_ENABLED = os.getenv(
    "RAG_SUBJECT_FILTER_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
# 主体分在最终分里的权重 λ：final = (1-λ)*rerank_relevance + λ*subject_score
RAG_SUBJECT_WEIGHT = float(os.getenv("RAG_SUBJECT_WEIGHT", "0.25"))
# [jonex] 方案 A：平台取回作答权（LightRAG 降为 retriever，答案收归平台侧）。
# 总开关：false 时 _rag_fallback_multi 走现有链路，代码路径完全不变（灰度）。
RAG_PLATFORM_ANSWER_ENABLED = os.getenv(
    "RAG_PLATFORM_ANSWER_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
# 送 answer_from_chunks 的 chunk 文本总长上限（字符，硬截）
RAG_ANSWER_MAX_CONTEXT_CHARS = int(os.getenv("RAG_ANSWER_MAX_CONTEXT_CHARS", "12000"))
# [jonex] S8：chunk 级低质过滤（残缺 HTML/英文摘要/col_ 失效/空壳），
# 插在 doc 聚合之前（先剔垃圾再聚合）；默认开。
RAG_LOWQ_CHUNK_FILTER_ENABLED = os.getenv(
    "RAG_LOWQ_CHUNK_FILTER_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")
# [jonex] S1+S7 双向校验与裁决：总开关 + 超时（超时回退原答案——本体是
# 已有结果，回退成本为 0）。默认开。
ONTOLOGY_ARBITRATION_ENABLED = os.getenv(
    "ONTOLOGY_ARBITRATION_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")
ONTOLOGY_ARBITRATION_TIMEOUT = float(os.getenv("ONTOLOGY_ARBITRATION_TIMEOUT", "8"))
# [jonex] 对侧 RAG 校验的整体超时：超时按「对侧无结果」处理（保留本体原答案），
# 绝不因校验故障丢答案。默认 20s——取值由精档超时台账反推
# （docs/ontology-query-thinking-latency-fix-plan.md §3.4）：
#   15(邻域) + 60(作答) + 20(本项) + 8(裁决) + 5(refs/verify) = 108 < 精档 timeout 120。
# 快档已由 gear.cross_verify=False 跳过对侧，本超时只对精档生效。
# 调大本项须同步重算该台账并上调精档 timeout，否则精档会因超时丢弃已生成答案。
ONTOLOGY_CROSS_RAG_TIMEOUT = float(os.getenv("ONTOLOGY_CROSS_RAG_TIMEOUT", "20"))
# [jonex] L4.2 检索侧：双路召回配额（row : summary ≈ 3 : 1）。
# 表格明细行与摘要各自按配额召回，防止连贯摘要抢占全部候选（T4）。
# 依赖 ctype 存量数据（reparse 后生效；存量 chunk 无 ctype → 不参与配额）。
RAG_DUAL_PATH_QUOTA_ENABLED = os.getenv(
    "RAG_DUAL_PATH_QUOTA_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")

# ── [jonex] §image-refs P4：图片引用配额（先按分数阈值过滤，再取 TopN）──
# 解决图片描述 chunk 语义聚集导致批量误召回、前端展示大量不相关图片的问题。
# 分数阈值：低于此值的图片 location 直接丢弃（基于 final_score/relevance/
# group_final_score/_order_score，语义见 _build_references 图片候选登记段）。
# 分数语义：rerank 开启时为 0~1 相关性分；关闭时为位置衰减分（1.0→0.0，
# 0.5 表示排在后半段——注意 rerank 关闭时阈值会误伤后半段候选，此时应设 0）。
# 默认 0.3：2026-08 以真实文档（岭南画派-10p，62 图）× 6 条真实查询实测，
# rerank 分呈二值化分布——真相关 ≥0.74（四屏 query 下四屏图 0.745~0.991、
# 苗松 query 下目标图 0.990），弱相关/无关 ≤0.03（绝大多数 0.000~0.025）。
# 0.3 位于两带之间（10 倍安全余量），砍噪声带不伤相关带；设 0 关闭过滤。
RAG_IMAGE_REF_SCORE_MIN = float(os.getenv("RAG_IMAGE_REF_SCORE_MIN", "0.3"))
# 通过阈值后每个文档保留的图片 location 上限（按分数降序取 TopN）
RAG_IMAGE_REF_MAX = max(1, int(os.getenv("RAG_IMAGE_REF_MAX", "6")))

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

# [jonex] S5 枚举意图词表：命中即跳过本体作答、强制走 RAG 取原始表格行
# （枚举题要求整行字段一并回传，本体按行取证不可靠）。
# 保守词表，只收明确的多答案/罗列信号词；「一共/共几个」类计数意图
# 归属 P1-6 count_versions 图模板，不在此列。
_ENUM_PATTERNS: list[re.Pattern] = [
    re.compile(r"列出|列举|罗列|枚举"),
    re.compile(r"有哪些|有哪几|哪几个|哪几家|哪些(?:的)?"),
    re.compile(r"分别(?:是|的|为)|各自"),
    re.compile(r"所有(?:的)?|全部"),
]


def _read_gear_overrides(raw: dict) -> tuple[bool, bool, int, int]:
    """[jonex] 查询期思考分档 override 读取（docs/ontology-query-thinking-latency-fix-plan.md §3.6）。

    缺省值 = 现状（非严格模式与所有非 strict 调用方零行为变化）：
      _fast_llm=False（保留思考）、_cross_enabled=True（跑对侧）、
      两个内层超时取原环境变量兜底（ONTOLOGY_NEIGHBOR_TIMEOUT / ONTOLOGY_ANSWER_TIMEOUT）。

    Returns:
        (fast_llm, cross_enabled, neighbor_timeout, answer_timeout)
    """
    return (
        bool(raw.get("_fast_llm_override", False)),
        bool(raw.get("_cross_verify_override", True)),
        int(raw.get("_neighbor_timeout_override", ONTOLOGY_NEIGHBOR_TIMEOUT)),
        int(raw.get("_answer_timeout_override", ONTOLOGY_ANSWER_TIMEOUT)),
    )


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


def _merge_same_row_locations(
    locations: list[dict], row_groups: dict[tuple, list[dict]],
) -> list[dict]:
    """[jonex] §C1-bis §19.5②: 同一 (table_idx, row_start, row_end) 的多个
    行内切分子段合并为一个 location。

    cell 区间取 min(cell_start)/max(cell_end) 并集（硬切子段的重叠区间
    在并集下不丢失覆盖），text 取第一个非空值；子段全部带 cell 锚点时
    才输出合并后的 cell 区间（部分子段被总长防御丢锚点时退回纯行级）。
    """
    if not row_groups:
        return locations
    out: list[dict] = []
    merged_keys: set[tuple] = set()
    for loc in locations:
        if loc.get("type") != "table_row":
            out.append(loc)
            continue
        key = (
            loc.get("table_idx"),
            loc.get("row_start"),
            loc.get("row_end"),
        )
        if key in merged_keys:
            continue
        merged_keys.add(key)
        group = row_groups.get(key) or [loc]
        if len(group) == 1:
            out.append(loc)
            continue
        cell_starts = [
            g["cell_start"] for g in group if g.get("cell_start") is not None
        ]
        cell_ends = [
            g["cell_end"] for g in group if g.get("cell_end") is not None
        ]
        merged = {
            "type": "table_row",
            "row_start": loc["row_start"],
            "row_end": loc["row_end"],
            "table_idx": loc.get("table_idx"),
            "chunk_index": loc.get("chunk_index"),
            "text": next((g.get("text") for g in group if g.get("text")), None),
        }
        if len(cell_starts) == len(group) and len(cell_ends) == len(group):
            merged["cell_start"] = min(cell_starts)
            merged["cell_end"] = max(cell_ends)
        out.append(merged)
    return out


# ── [jonex] §10 L2：跨页打包 chunk 的引用页精算（纯函数）──────────────────

# CJK 统一表意文字基本区（U+4E00–U+9FFF）
_CJK_CHAR_RE = re.compile(r"[一-鿿]")
_ALNUM_WORD_RE = re.compile(r"[0-9A-Za-z_]{2,}")


def _query_terms(query: str) -> set[str]:
    """从 query 提取打分词项：CJK 字符二元组 + 长度 ≥2 的字母数字词。

    CJK 无词边界，二元组是零依赖下最稳的切法；CJK 不足 2 字（无法组成
    二元组）时退回单字，保证短查询仍可打分。
    """
    query = query or ""
    terms = {m.group(0).lower() for m in _ALNUM_WORD_RE.finditer(query)}
    cjk = _CJK_CHAR_RE.findall(query)
    if len(cjk) >= 2:
        terms.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    else:
        terms.update(cjk)
    return terms


def _split_text_by_pspans(text: str, pspans: str) -> list[tuple[int, str]] | None:
    """按 pspans 页边界表把 chunk 全文切成 ``[(页号, 段文本), ...]``。

    完整性防御（§10.5②）：任何坐标越界（content 非整包文本，如入库二次
    切分后被部分召回）、乱序或首 entry 非 0（违反 pspans 格式不变量）都
    返回 None，由调用方回落到 chunk 起点页。
    """
    entries: list[tuple[int, int]] = []
    for seg in (pspans or "").split(";"):
        seg = seg.strip()
        if not seg:
            continue
        a, sep, b = seg.partition("@")
        if not sep or not a.isdigit() or not b.isdigit():
            return None
        entries.append((int(a), int(b)))
    if not entries or entries[0][0] != 0:
        return None
    for (a, _), (b, _) in zip(entries, entries[1:]):
        if a >= b:
            return None
    if any(off > len(text) for off, _ in entries):
        return None
    segs: list[tuple[int, str]] = []
    for i, (off, page) in enumerate(entries):
        end = entries[i + 1][0] if i + 1 < len(entries) else len(text)
        segs.append((page, text[off:end]))
    return segs


def _best_page_by_query(segments: list[tuple[int, str]], query: str) -> int | None:
    """页段打分取最高分页（语义：与 query 最相关，§10.1）。

    同分取先出现的页（包序），保持确定性；无词项或全零分返回 None
    （调用方回落 chunk 起点页）。
    """
    terms = _query_terms(query)
    if not terms:
        return None
    best_page: int | None = None
    best_score = 0
    for page, seg in segments:
        score = sum(1 for t in terms if t in seg)
        if score > best_score:
            best_page, best_score = page, score
    return best_page if best_score else None


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
            query=req.query,   # [jonex] §10 L2 页段打分
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
            history = await self._history.save_history(
                tenant_id,
                user_id,
                SearchHistoryCreateRequest(
                    query=req.query,
                    knowledge_base_id=req.knowledge_base_id,
                    mode=req.mode,
                    top_k=req.top_k,
                    domain_space_id=req.domain_space_id,
                    answer_preview=answer[:300],
                    answer=answer,
                    references=references,
                    duration_ms=duration_ms,
                ),
            )
            result["history_id"] = history.get("id")
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
        query: str | None = None,
    ) -> list[dict]:
        """从 RAG 返回的原始引用片段富化出完整的 references。

        D5（richification in service）+ D6（doc_id 聚合）+ D8（租户过滤）。
        若对象存储可用则生成预签名 URL，否则 raw_url 为 None。

        allowed_kb_ids：纵深防御——仅保留属于本次请求知识库的文档。即便底层
        LightRAG 检索因 workspace 漏配等原因串库，也不会把库外文档泄露给前端
        （None 表示不做 KB 过滤，兼容历史单库调用）。

        doc_map：可选，调用方预查好的 doc 实体映射 {doc_id: KnowledgeDocument}。
        None 时内部自查。

        query：可选，[jonex] §10 L2 页段打分精算用；None（流式 resolve 等
        无 query 场景）时跳过打分、直接回落 chunk 起点页。
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
            # [jonex] §block-packing 改动 4 + §10 L2：跨页打包 chunk 的
            # page_no 精算。默认语义 = chunk 起点页（pspans 首 entry 恒为
            # 0@{起点页}，offset=0 直接解析即得）；开关开启且带 text/query
            # 时升级为页段打分（语义：与 query 最相关，§10.1）。打分失败
            # （完整性防御/零命中/无词项）回落 chunk 起点页，行为与改造前一致。
            if r.get("pspans"):
                refined = None
                # §10.5② 完整性判据（review P1，2026-08-20）：chunk_texts
                # 多于 1 条 = 包被 LightRAG 入库二次切分成子 chunk（事实三：
                # 子 chunk 共享同一 file_source/pspans）。text 是子 chunk
                # 的 join（含 "\n\n" 分隔、可能部分召回/带 overlap），与包
                # 原文逐字符错位，pspans 坐标失准——此时不精算，回落起点页。
                if (
                    RAG_REF_PAGE_SCORING_ENABLED
                    and r.get("text")
                    and len(r.get("chunk_texts") or []) <= 1
                ):
                    segments = _split_text_by_pspans(r["text"], r["pspans"])
                    if segments:
                        refined = _best_page_by_query(segments, query or "")
                if refined is None:
                    refined = resolve_page_by_offset(r["pspans"], 0)
                if refined is not None:
                    r["page_no"] = refined
            ref = agg.setdefault(
                did, {"doc_id": did, "locations": [], "_row_locs": {}}
            )
            loc = to_location(r)
            ref["locations"].append(loc)
            # [jonex] §C1-bis §19.5②: 行内切分子段的分组键（供同行合并）
            if loc.get("type") == "table_row":
                key = (
                    loc.get("table_idx"),
                    loc.get("row_start"),
                    loc.get("row_end"),
                )
                ref["_row_locs"].setdefault(key, []).append(loc)
            # [jonex] §image-refs P2-1: 图片 location 收集资产候选。aext 只在
            # 同一条 flat dict r 上（to_location 不透出 asset_ext），此处
            # 登记 (loc, aext, score)，聚合后统一预签名（见下方资产富化段）。
            # [jonex] §image-refs P4：额外记录检索分数，供图片配额截取使用。
            # [jonex] J4-c：图片阈值优先用原始相关性（relevance / group_relevance），
            # 避免 λ 混合导致的尺度漂移——final_score 已混入 subject_score，
            # 同一阈值在不同组上等效 rel 要求漂移 0.60~0.933（实测见 §13.2）。
            # 此链仅用于图片配额（_asset_cands），不影响 prompt_refs 排序与截断。
            # group_final_score：主体加权路径下同组成员不继承代表分，仅带组代表
            # 的融合分——用它近似成员语义相关性，避免成员只剩召回位置衰减分。
            # _evidence_score：本体路径归一化证据权重兜底（方案 G2，P1）。
            if loc.get("type") == "image":
                _img_score = (
                    r.get("relevance")
                    if r.get("relevance") is not None
                    else r.get("group_relevance")
                    if r.get("group_relevance") is not None
                    else r.get("final_score")
                    if r.get("final_score") is not None
                    else r.get("group_final_score")
                    if r.get("group_final_score") is not None
                    else r.get("_evidence_score")
                    if r.get("_evidence_score") is not None
                    else r.get("_order_score", 0.0)
                )
                # [jonex] §G2：本体路径 raw_refs 只有 _evidence_score（结构分），
                # 无语义相关性分。结构分用于组内排序，但不应被语义阈值误杀。
                _score_source = "structural" if (
                    r.get("_evidence_score") is not None
                    and r.get("relevance") is None
                    and r.get("group_relevance") is None
                    and r.get("final_score") is None
                    and r.get("group_final_score") is None
                ) else "semantic"
                ref.setdefault("_asset_cands", []).append(
                    (loc, r.get("asset_ext") or "", _img_score, _score_source)
                )
                # [jonex] §image-refs E3：登记图片描述原文（配额段超额 rerank
                # 用），键为 loc 对象 id——与 _asset_cands 一一对应，聚合后
                # 用完即弃，不进入输出序列化。
                ref.setdefault("_asset_texts", {})[id(loc)] = r.get("text") or ""

        storage = get_object_storage()
        # [jonex] §image-refs P4：图片引用配额——先按分数阈值过滤，再按分数
        # 降序取 TopN。解决图片描述 chunk 语义聚集导致批量误召回的问题。
        # 此段操作 _asset_cands 和 locations（同步裁剪），确保富化段只处理
        # 存活的图片 location。
        for ref in agg.values():
            cands = ref.get("_asset_cands")
            if not cands:
                continue
            orig_count = len(cands)
            # [jonex] §image-refs E3 + 方案 G3：超额或同分并列时单独 rerank
            # 图片描述，让 TopN 挑选有语义依据——主体加权路径下组成员共享
            # 组代表分（group_final_score / group_relevance），同分并列导致
            # 稳定排序退化为召回顺序，方案 A 的 batch 误召回保护原样复现。
            # 本段每 doc 至多一次；失败返回 None 自动回退原码分链。
            # 尺度注意（§E-6）：E3 分是原始 rerank 相关性（0~1），与代表
            # final_score（λ>0 时 0.75×rel）尺度不同，阈值灰度时需留意。
            _scores = [sc for _loc, _ext, sc, *_ in cands]
            # [jonex] 方案 G3：同分并列（继承组代表分）或全零（无分数来源）
            # → 无判别力，需 E3 逐张打分。单候选不触发（无排序意义）。
            _no_discrimination = len(set(_scores)) <= 1
            # [jonex] §G2：结构分来源（纯 _evidence_score）跳过语义阈值——
            # 证据权重是图谱距离信号，不是 0~1 语义相关性，不应被
            # RAG_IMAGE_REF_SCORE_MIN 误杀。只保留 TopN 截断。
            _structural = any(
                len(c) >= 4 and c[3] == "structural" for c in cands
            )
            if query and RAG_PRELLM_RERANK_ENABLED and (
                orig_count > RAG_IMAGE_REF_MAX
                or (orig_count > 1 and _no_discrimination)
                or (orig_count > 1 and _structural)  # [jonex] I4-a：结构分来源必须 E3 语义打分
            ):
                texts = [
                    ref.get("_asset_texts", {}).get(id(loc), "")
                    for loc, _ext, _sc, *_ in cands
                ]
                d = doc_map[ref["doc_id"]]
                from jonex_core.common.rerank import rerank

                results = await rerank(
                    query, texts, tenant_id=tenant_id,
                    kb_id=d.knowledge_base_id or None,
                )
                if results:
                    score_by_idx = {
                        x.get("index"): x.get("relevance_score", 0.0)
                        for x in results
                    }
                    cands = [
                        (loc, ext, score_by_idx.get(i, sc))
                        for i, (loc, ext, sc, *_rest) in enumerate(cands)
                    ]
                    _structural = False  # E3 rerank 后是真实语义分
                    # [jonex] 临时：E3 段无日志，加一行确认 rerank 分数分布
                    _e3_scores = sorted(
                        [score_by_idx.get(i, 0.0) for i in range(len(cands))],
                        reverse=True,
                    )
                    logger.info(
                        "[image-ref-quota] E3 rerank 完成 doc=%s query=%r "
                        "cands=%d scores=%s",
                        ref["doc_id"], query[:60], len(cands),
                        [round(s, 4) for s in _e3_scores[:6]],
                    )
            # 1) 按分数阈值过滤（结构分豁免）
            if RAG_IMAGE_REF_SCORE_MIN > 0:
                cands = [
                    (loc, ext, sc) for loc, ext, sc, *_rest in cands
                    if sc >= RAG_IMAGE_REF_SCORE_MIN or _structural
                ]
            # 2) 按分数降序取 TopN
            if len(cands) > RAG_IMAGE_REF_MAX:
                cands = sorted(cands, key=lambda x: x[2], reverse=True)[:RAG_IMAGE_REF_MAX]
            # 3) 同步从 locations 中移除被裁剪的图片 location
            survived_locs = {id(loc) for loc, *_ in cands}
            ref["locations"] = [
                loc for loc in ref["locations"]
                if loc.get("type") != "image" or id(loc) in survived_locs
            ]
            ref["_asset_cands"] = cands
            ref.pop("_asset_texts", None)
            if len(cands) < orig_count:
                logger.debug(
                    "[image-ref-quota] doc=%s 图片配额裁剪: %d→%d (阈值=%.2f, topN=%d)",
                    ref["doc_id"], orig_count, len(cands),
                    RAG_IMAGE_REF_SCORE_MIN, RAG_IMAGE_REF_MAX,
                )

        # [jonex] §image-refs P2-1: 图片资产 asset_url 富化。key 派生走
        # build_asset_key（ext 白名单归一，路径穿越免疫）；aext 缺失 =
        # 上传失败/开关关闭，跳过不富化（前端按无 asset_url 降级）。
        # 整段 best-effort：任何异常只影响图片预览，不阻断引用产出。
        asset_jobs: list[tuple[str, dict, str]] = []  # (doc_id, loc, ext)
        for ref in agg.values():
            for loc, ext, _score, *_rest in ref.pop("_asset_cands", []):
                if ext:
                    asset_jobs.append((ref["doc_id"], loc, ext))
        if asset_jobs:
            async def _presign_asset(did: str, loc: dict, ext: str):
                d = doc_map[did]
                key = build_asset_key(
                    tenant_id, d.knowledge_base_id, did, loc["image_idx"], ext,
                )
                return await storage.presigned_url(key, tenant_id)

            results = await asyncio.gather(
                *[_presign_asset(did, loc, ext) for did, loc, ext in asset_jobs],
                return_exceptions=True,
            )
            for (_did, loc, _ext), res in zip(asset_jobs, results):
                if isinstance(res, str):
                    loc["asset_url"] = res
                else:
                    loc["asset_url"] = None
                    # [jonex] I1-b：presign 失败不阻断（best-effort），但不再静默——
                    # SDK 缺失/STS 故障/密钥过期等都会走到这里，需可观测。
                    logger.warning(
                        "[asset-url] presign 失败 doc=%s img=%s: %r",
                        _did, loc.get("image_idx"), res,
                    )

        # [jonex] 批量查询 KB 名称（一次 DB 查询，避免每条引用单独查）
        kb_ids_in_refs = list(dict.fromkeys(
            d.knowledge_base_id for d in doc_map.values() if d.knowledge_base_id
        ))
        kb_names = await self._get_kb_names(tenant_id, kb_ids_in_refs) if kb_ids_in_refs else {}

        out = []
        for did, ref in agg.items():
            d = doc_map[did]
            raw_url = None
            if d.storage_key:
                try:
                    raw_url = await storage.presigned_url(d.storage_key, tenant_id)
                except Exception as exc:
                    # [jonex] I1-b：与 asset_url 同源——STS SDK 缺失/密钥故障时
                    # raw_url 也恒为 None，不再静默（best-effort 不阻断，但要可观测）。
                    logger.warning("[raw-url] presign 失败 doc=%s: %r",
                                   d.id, exc)
            out.append({
                "doc_id": did,
                "kb_id": d.knowledge_base_id,
                "kb_name": kb_names.get(d.knowledge_base_id) if d.knowledge_base_id else None,
                "file_name": d.file_name,
                "mime_type": d.mime_type,
                "file_size": d.file_size,
                "media_type": classify_media(d.mime_type, d.file_name),
                "raw_url": raw_url,
                "locations": _merge_same_row_locations(
                    ref["locations"], ref["_row_locs"],
                ),
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
        # [jonex] 批量查询 KB 名称
        kb_ids_in_refs = list(dict.fromkeys(
            d.knowledge_base_id for d in docs if d.knowledge_base_id
        ))
        kb_names = await self._get_kb_names(tenant_id, kb_ids_in_refs) if kb_ids_in_refs else {}

        out = []
        for d in docs:
            raw_url = None
            if d.storage_key:
                try:
                    raw_url = await storage.presigned_url(d.storage_key, tenant_id)
                except Exception as exc:
                    # [jonex] I1-b：与 asset_url 同源——STS SDK 缺失/密钥故障时
                    # raw_url 也恒为 None，不再静默（best-effort 不阻断，但要可观测）。
                    logger.warning("[raw-url] presign 失败 doc=%s: %r",
                                   d.id, exc)
            out.append({
                "doc_id": d.id,
                "kb_id": d.knowledge_base_id,
                "kb_name": kb_names.get(d.knowledge_base_id) if d.knowledge_base_id else None,
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
    def _detect_enumeration_intent(query: str) -> bool:
        """[jonex] S5：枚举意图检测（列出/有哪些/分别/所有…）。"""
        if not query:
            return False
        return any(p.search(query) for p in _ENUM_PATTERNS)

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
    def _is_stub(entity: dict | None) -> bool:
        """[jonex] S2 真 stub 判定：既无描述、又无结构化属性的空壳。

        **「且」不是「或」**——Row-as-Object 表格对象是纯代码产出、不写
        description，信息全在 attributes 里（全库最可靠的事实来源）；
        只有 desc 空 **且** attrs 空 才是真 stub（如 `Ver1.31`）。
        attributes 兼容 dict 与 JSON 字符串（`"{}"`）两种形态。

        例外：`extraction_method == "manual"` 的用户手动创建实体，即便 desc/attrs
        皆空也不视为 stub（用户有意创建，非编译管线产出的空壳）。
        """
        if not entity:
            return True
        if entity.get("extraction_method") == "manual":
            return False
        has_desc = bool((entity.get("description") or "").strip())
        attrs = entity.get("attributes")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except Exception:
                attrs = None
        has_attrs = bool(attrs)
        return not has_desc and not has_attrs

    @staticmethod
    def _entity_has_content(entity: dict | None) -> bool:
        """实体是否有可作答内容（描述或结构化属性）。

        与 `_is_stub` 的差别：不豁免 `extraction_method == "manual"`——手动实体
        即便有 manual 标记，只要 desc/attrs 皆空，同样「无内容」。用于命中实体
        但无内容时的友好提示兜底（避免误导性「未找到」）。
        attributes 兼容 dict 与 JSON 字符串（`"{}"`）两种形态。
        """
        if not entity:
            return False
        if (entity.get("description") or "").strip():
            return True
        attrs = entity.get("attributes")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except Exception:
                attrs = None
        return bool(attrs)

    @staticmethod
    def _apply_dual_path_quota(
        refs: list[dict],
        row_quota: int | None = None,
    ) -> tuple[list[dict], dict]:
        """[jonex] L4.2 检索侧：双路召回配额（row : summary ≈ 3 : 1）。

        表格明细行（table_row）与表格摘要（table_summary）各自按配额召回——
        摘要是连贯自然语言、向量相似度天然高于 key: value 拼接串（T4 实测
        104/1063 英文摘要抢占召回），不设配额时摘要会吃掉全部候选。
        其余 ctype（text/image/…）不受配额。存量 chunk 无 ctype → 归「其他」。

        row_quota 分档校准（§16.3 影响 2）：
        ``max(8, ⌊RAG_ANSWER_MAX_CONTEXT_CHARS / 900⌋)``（表格 chunk 均长
        ~900 字符，O1 后）。返回 ``(merged, stats)``，merged 保持原相对序。
        """
        quota = row_quota or max(8, RAG_ANSWER_MAX_CONTEXT_CHARS // 900)
        summary_quota = max(1, quota // 3)
        rows: list[dict] = []
        summaries: list[dict] = []
        others: list[dict] = []
        for r in refs:
            try:
                parsed = parse_file_source(r.get("file_path") or "") or {}
            except (NameError, AttributeError, ImportError):
                raise  # 代码 bug，不掩盖
            except Exception as exc:
                logger.debug("L4.2 双路配额解析降级: %s", exc)
                parsed = {}
            ctype = parsed.get("chunk_type") or r.get("chunk_type") or ""
            if ctype == "table_row":
                rows.append(r)
            elif ctype == "table_summary":
                summaries.append(r)
            else:
                others.append(r)
        kept_ids = {id(r) for r in rows[:quota] + summaries[:summary_quota] + others}
        merged = [r for r in refs if id(r) in kept_ids]
        stats = {
            "table_row_total": len(rows),
            "table_row_kept": min(len(rows), quota),
            "table_summary_total": len(summaries),
            "table_summary_kept": min(len(summaries), summary_quota),
            "other_kept": len(others),
        }
        return merged, stats

    @staticmethod
    def _filter_low_quality_chunks(
        refs: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """[jonex] S8：chunk 级低质过滤（§15.3 四类判据，均有实测依据）。

        对应本体侧 S2 的 stub 过滤——RAG 送进 LLM 的 chunk 无质量分层，
        噪声（英文摘要、残缺 HTML 碎片、纯垃圾）与正文同权。

        四类判据：
        1. 残缺 HTML 碎片：以 `>`/`</` 开头，或含表格标签但无闭合且短；
        2. 英文表格摘要（中文 KB）：ctype=table_summary 且 ASCII 占比 >80%；
        3. 列名全失效的表格行：ctype=table_row 且 col_ 键占比 ≥ 一半；
        4. 空壳 chunk：去掉 yx 命名空间标记后正文 < 20 字。

        Returns:
            ``(kept, filtered)``——filtered 带 reason 供 reasoning 可见。
        """
        kept: list[dict] = []
        filtered: list[dict] = []
        for r in refs:
            # LightRAG references 原生字段为 content（file_path 保留）；
            # 平台归一后兼容 text
            text = (r.get("text") or r.get("content") or "").strip()
            reason = SearchService._low_quality_reason(r, text)
            if reason:
                filtered.append({
                    "doc_id": r.get("doc_id"),
                    "chunk_id": r.get("chunk_id") or r.get("id"),
                    "reason": reason,
                    "preview": text[:80],
                })
            else:
                kept.append(r)
        if filtered:
            # [jonex] §block-packing 改动 4：过滤日志附打包开关状态，便于
            # 对照「打包后 empty_shell（去 ns token 后正文 < 20 字）触发率」。
            # 开关读值来自 knowledge-base 容器 env（改动 5 同步注入）；
            # 判据本身（_low_quality_reason）不做任何增删
            reason_counts: dict[str, int] = {}
            for f in filtered:
                reason_counts[f["reason"]] = reason_counts.get(f["reason"], 0) + 1
            logger.info(
                "S8 低质过滤: 剔除 %d/%d 个 chunk，原因分布=%s，"
                "text_block_packing=%s",
                len(filtered), len(refs), reason_counts,
                os.getenv("RAG_TEXT_BLOCK_PACKING"),
            )
        return kept, filtered

    @staticmethod
    def _low_quality_reason(ref: dict, text: str) -> str:
        """S8 单 chunk 判据；返回命中原因（空串=正常）。"""
        try:
            parsed = parse_file_source(ref.get("file_path") or "") or {}
        except (NameError, AttributeError, ImportError):
            raise  # 代码 bug，不掩盖
        except Exception as exc:
            logger.debug("S8 低质判定解析降级: %s", exc)
            parsed = {}
        ctype = parsed.get("chunk_type") or ref.get("chunk_type") or ""

        # 1) 残缺 HTML 碎片（C2 实测：`></td>`、`,190.51</td>`）
        if text.startswith(">") or text.startswith("</"):
            return "html_fragment"
        if (
            ("<td" in text or "<table" in text)
            and "</table>" not in text
            and len(text) < 200
        ):
            return "html_fragment"

        # 4) 空壳 chunk（去 yx 命名空间标记后）
        cleaned = re.sub(r"<!--yx:[0-9a-f]{8}-->", "", text).strip()
        if len(cleaned) < 20:
            return "empty_shell"

        # 2) 英文表格摘要 / 图片描述（中文 KB；T4 实测 104/1063）
        # [jonex] §image-refs P0-4：ctype=image 的英文 VLM 描述同型过滤
        # （纵深防御——RAG_PROMPT_LANG 默认 zh 已根治，见
        # image-reference-chain-execution-plan.md §2.4）
        if ctype in ("table_summary", "image"):
            ascii_count = sum(1 for ch in cleaned if ord(ch) < 128)
            if ascii_count / max(1, len(cleaned)) > 0.8:
                return (
                    "english_image_description"
                    if ctype == "image"
                    else "english_table_summary"
                )

        # 3) 列名全失效的表格行（T2 过渡期兜底；改造后应为 0）
        if ctype == "table_row":
            col_hits = len(re.findall(r"col_\d+\s*:", cleaned))
            if col_hits >= 4:
                named = len(re.findall(r"[^|\n:：]+\s*:", cleaned))
                if named and col_hits / max(1, named) >= 0.5:
                    return "all_col_placeholder"
        return ""

    def _filter_stub_facts(
        self, facts: list[dict], collector=None,
    ) -> list[dict]:
        """[jonex] S2：过滤真 stub 事实（stub 在任何场景都是噪声）。

        过滤结果记入 reasoning（`stub_filtered`），不静默丢弃；全部被过滤
        时自然走 INSUFFICIENT → 降级 RAG，不把 stub 塞回去凑数。
        """
        kept: list[dict] = []
        stub_names: list[str] = []
        for fact in facts:
            entity = fact.get("target_entity") or fact
            if self._is_stub(entity):
                stub_names.append(fact.get("target", "") or "")
            else:
                kept.append(fact)
        if stub_names and collector:
            collector.step(
                STAGE_FACT_LOOKUP, "stub 事实过滤",
                summary=f"过滤 {len(stub_names)} 条 stub 事实",
                detail={"stub_filtered": stub_names},
            )
        elif stub_names:
            logger.info(
                "[ontology] stub 过滤 %d 条：%s", len(stub_names), stub_names,
            )
        return kept

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

    @staticmethod
    def _resolve_query_subjects(
        query: str,
        ontology_instances: list[dict] | None = None,
    ) -> list[str]:
        """解析 query 的主体信号来源（方案 B 的 B1，§5.2）。

        三级回落：
        1. 本体命中实体的 canonical_name + aliases（首选：同源维护、带别名、
           零额外调用成本）；
        2. jieba 词性过滤（保留 n/nz/nr/ns/nt/eng 与 m+q 组合，丢弃动词/疑问/
           代词等无意义片段）；
        3. 无主体 → 返回 []，主体信号整体不参与（不是「全部剔除」）。

        注：`_extract_subject_entity` 保留不动——L1571 深度查询意图分类仍用
        entity_count 参与 simple/complex 判定，语义不可混用（方案风险 R1）。
        """
        subjects: list[str] = []
        if ontology_instances:
            for inst in ontology_instances:
                if not isinstance(inst, dict):
                    continue
                for key in ("canonical_name", "name"):
                    v = inst.get(key)
                    if v and str(v).strip():
                        subjects.append(str(v).strip())
                aliases = inst.get("aliases")
                if isinstance(aliases, (list, tuple)):
                    subjects.extend(str(a).strip() for a in aliases if a)
        if subjects:
            return list(dict.fromkeys(subjects))  # 去重保序

        # jieba 回落
        import jieba.posseg as pseg

        keep_flags = {"n", "nz", "nr", "ns", "nt", "eng"}
        tokens: list[tuple[str, str]] = [
            (w.strip(), flag) for w, flag in pseg.cut(query or "")
        ]
        out: list[str] = []
        i = 0
        while i < len(tokens):
            w, flag = tokens[i]
            if w and len(w) >= 2:
                if flag in keep_flags:
                    out.append(w)
                elif flag == "m" and i + 1 < len(tokens) and tokens[i + 1][1] == "q":
                    nxt = tokens[i + 1][0]
                    if nxt:
                        out.append(f"{w}{nxt}")  # m+q 组合：如「4000笔」
                        i += 1
            i += 1
        return list(dict.fromkeys(out))

    @staticmethod
    def _subject_score(
        ref: dict,
        subjects: list[str],
        doc_map: dict[str, Any],
    ) -> float:
        """多信号主体一致性打分（方案 B 的 B2，§5.3），取三信号最大值。

        | 信号 | 命中得分 | 可得性 |
        |---|---|---|
        | chunk 正文命中 | 1.0 | 现在就有 |
        | entity_hint 命中 | 0.9 | 方案 C 之后 |
        | 文件名命中 | 0.6 | 现在就有 |
        | 全不中 | 0.0 | — |

        doc_map 查不到文档 → 0.0（不参与打分），与「主体不符」通过日志区分。
        """
        if not subjects:
            return 0.0
        score = 0.0
        text = (ref.get("text") or ref.get("content") or "").lower()
        if any(s.lower() in text for s in subjects):
            score = max(score, 1.0)
        hint = (ref.get("entity_hint") or "").lower()
        if hint and any(s.lower() in hint for s in subjects):
            score = max(score, 0.9)
        did = ref.get("doc_id")
        if did:
            d = doc_map.get(did)
            fname = ((d.file_name if d else "") or "").lower()
            if any(s.lower() in fname for s in subjects):
                score = max(score, 0.6)
        return score

    async def _subject_weight_and_rank(
        self,
        query: str,
        raw_refs: list[dict],
        subjects: list[str],
        doc_map: dict[str, Any],
        tenant_id: str,
        kb_id: str = "",
        trace_id: str | None = None,
        user_id: str = "",
    ) -> tuple[list[dict], dict[str, Any]]:
        """方案 B 主流程（§5.4/§5.5）：doc 聚合 → rerank → 主体软融合 → top-K 软截断。

        核心原则：主体信号只影响排序，**永不删除候选**。

        - B4 聚合键为 `(doc_id, chunk_type)`（表格方案改动 63：防止同文档的
          摘要 chunk 吃掉明细行；存量 chunk 无 ctype → 归 (doc_id, None) 组，
          等价于按 doc_id 聚合）。
        - rerank 关闭或失败 → relevance 缺失，final = subject_score，仅排序。
        - 主体全不中 → λ 置 0（信号作废），只按相关性排序，日志 warning 记明。
        - 截断只发生在 rerank 开启且候选组数超过 TOPK 时（组级截断后展开）。

        Returns:
            (排序后的 refs, stats)：stats 供埋点（subject_weighted/lambda_used/
            groups/reranked/subject_top_scores）。
        """
        refs = list(raw_refs)

        # [jonex] review 问题1：相关性兜底——rerank 关闭/失败时 refs 无
        # relevance 分，融合退化为纯主体分四档、「主体全不中时全部同分完全
        # 无序」。LightRAG references 无自带相似度字段，用召回顺序做位置
        # 衰减近似（1.0 → 0.0），保证任何开关组合下排序有相关性维度。
        n_refs = max(1, len(refs))
        for i, r in enumerate(refs):
            r.setdefault("_order_score", 1.0 - i / n_refs)

        # B4：按 (doc_id, chunk_type) 分组，每组取组内相关性最高的代表
        groups: dict[tuple, list[dict]] = {}
        for r in refs:
            key = (r.get("doc_id"), r.get("chunk_type"))
            groups.setdefault(key, []).append(r)
        reps: list[dict] = [
            max(items, key=lambda r: r.get("relevance", r.get("_order_score", 0.0)))
            for items in groups.values()
        ]

        # rerank（仅对代表打分；失败/关闭时返回原列表、无 relevance 分）。
        # truncate=False：截断必须发生在主体融合之后，否则主体分只对
        # rerank 存活的 top-K 生效（P0-1，§5.4 原文语义）。
        rerank_applied = False
        # [jonex] §image-refs E1：组代表 ≥2 即 rerank（truncate=False 只打分
        # 不截断）。旧门槛 >TOPK 是「截断」语义——小结果集（单文档 2~6 组）
        # 被挡在门外，图片组代表无 relevance 分，final 退化为纯主体分/0 分。
        if RAG_PRELLM_RERANK_ENABLED and len(reps) > 1:
            rerank_applied = True
            reps = await self._prellm_rerank_chunks(
                query, reps, tenant_id=tenant_id,
                kb_id=kb_id, trace_id=trace_id, user_id=user_id,
                truncate=False,
            )

        # B3 融合：final = (1-λ)*relevance + λ*subject_score；全不中 → λ=0
        subject_scores = [self._subject_score(r, subjects, doc_map) for r in reps]
        lam = RAG_SUBJECT_WEIGHT
        if all(s == 0.0 for s in subject_scores):
            logger.warning(
                "[subject_weight] 主体信号全部未命中（%d 组候选），本轮 λ=0 只按相关性排序 query=%r",
                len(reps), query[:80],
            )
            lam = 0.0
        for r, s in zip(reps, subject_scores):
            r["subject_score"] = s
            rel = r.get("relevance")
            if rel is None:
                # review 问题1 兜底：无 rerank 分时——
                # · λ>0：保持旧语义（退化为纯主体分排序，主体命中的排前）；
                # · λ=0（主体全不中）：用召回序位置衰减近似相关性，避免
                #   「全部同分完全无序」（预算截断按顺序 break 会随机取 chunk）。
                if lam > 0:
                    r["final_score"] = s
                    continue
                rel = r.get("_order_score", 0.0)
            r["final_score"] = (1.0 - lam) * rel + lam * s

        # 组级排序 → 组级 top-K 截断 → 展开回 chunk 级。
        # P0-2：组内成员**不继承**代表分数——代表分写给全组成员会使组内同分、
        # 稳定排序退化为原始顺序，预算截断按顺序 break 时大表格中后部的
        # 目标行会被丢（如 250 行表切 36 chunk，12000 字符只取前 13 个）。
        # 组内排序键：chunk 级主体分 → 成员自身 relevance → 原始顺序（稳定）。
        reps_sorted = sorted(reps, key=lambda r: r.get("final_score", 0.0), reverse=True)
        reranked = False
        if RAG_PRELLM_RERANK_ENABLED and len(reps_sorted) > RAG_PRELLM_RERANK_TOPK:
            reps_sorted = reps_sorted[:RAG_PRELLM_RERANK_TOPK]
            reranked = True
        ranked: list[dict] = []
        for rep in reps_sorted:
            items = groups.get((rep.get("doc_id"), rep.get("chunk_type")), [])
            for item in items:
                # chunk 级主体分（组内成员逐个算，替代继承代表分）
                item["subject_score"] = self._subject_score(item, subjects, doc_map)
                item["group_final_score"] = rep.get("final_score", 0.0)
                # [jonex] 方案 J4-c：同时下发组代表的**原始 rerank 相关性**
                # （未经 λ 混合）。图片阈值必须比较原始 rel ——
                # group_final_score 已混入 subject_score，同一阈值在不同组上
                # 等效 rel 要求漂移 0.60~0.933（实测见方案文档 §13.2）。
                # rep 无 relevance（rerank 关闭/失败）时不写入，让分数链自然回落。
                if rep.get("relevance") is not None:
                    item["group_relevance"] = rep["relevance"]
                # 不覆盖成员自身 relevance（rerank 只对代表打过分，
                # 成员可能带旧路径或后续链路写入的自身分值）
            items_sorted = sorted(
                items,
                key=lambda r: (
                    -r.get("subject_score", 0.0),
                    -(r.get("relevance")
                      if r.get("relevance") is not None
                      else r.get("_order_score", 0.0)),
                ),
            )
            ranked.extend(items_sorted)

        stats: dict[str, Any] = {
            "subject_weighted": True,
            "lambda_used": lam,
            "groups": len(groups),
            "rerank_applied": rerank_applied,
            "reranked": reranked,
            "subject_top_scores": [round(s, 3) for s in sorted(subject_scores, reverse=True)[:3]],
        }
        return ranked, stats

    async def _prellm_rerank_chunks(
        self,
        query: str,
        raw_refs: list[dict],
        tenant_id: str,
        kb_id: str = "",
        trace_id: str | None = None,
        user_id: str = "",
        truncate: bool = True,
    ) -> list[dict]:
        """送 LLM 融合前的 chunk 级重排：用 reranker 对原始 chunk 文本打分。

        Args:
            query: 用户查询
            raw_refs: LightRAG 返回的原始 reference 列表（每项含 text 字段）
            tenant_id, kb_id, trace_id, user_id: 计量/追踪
            truncate: True（默认，旧路径行为）按 top-K 截断后返回；
                False 只打分排序、不截断——方案 B 的主体融合发生在 rerank
                之后，若在此截断，主体分只对 rerank 存活的 top-K 生效，
                主体信号将无法参与截断决策（P0-1）。

        Returns:
            按 relevance 降序排列的 raw_refs（truncate=True 时 top-K 保留，其余移除）
        """
        from jonex_core.common.rerank import rerank

        # [jonex] §image-refs E2：truncate=False（主体融合路径）的目的是
        # 「打分」而非截断，组代表 ≥2 就值得 rerank；truncate=True（旧
        # 截断路径，1886/2107 调用点）保持 >TOPK 才处理，行为不变。
        _threshold = 1 if not truncate else RAG_PRELLM_RERANK_TOPK
        if not raw_refs or len(raw_refs) <= _threshold:
            return raw_refs

        # 提取每个 ref 的代表文本（chunk 原文）
        texts: list[str] = []
        indices: list[int] = []
        for i, r in enumerate(raw_refs):
            txt = r.get("text") or r.get("content") or ""
            if txt:
                texts.append(txt[:1024])
                indices.append(i)

        if len(texts) <= _threshold:
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
            return ranked[:RAG_PRELLM_RERANK_TOPK] if truncate else ranked
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
        query: str = "",
    ) -> list[dict]:
        """方案⑦：从本体 source_chunks 直接构建 chunk 级引用，不回 LightRAG。

        收集命中实体 + facts 各 target_entity 的 source_chunks[].file_path，
        经 parse_file_source → _build_references 产出完整 references（含 COS 预签名/文件名）。
        source_chunks 为空的实体退化为文档级引用（_build_references_by_doc_ids）。

        query：[jonex] 方案 G1 透传原始查询词，打通 E3 图片描述 rerank 兜底。
        """
        t = time.perf_counter()

        # ── 收集 source_chunks（[jonex] S3：带证据权重收集，末尾按权重重排）──
        weighted_sc: list[tuple[float, str]] = []   # (weight, file_path)
        chunk_doc_ids: list[str] = []
        fallback_doc_ids: list[str] = []
        content_by_key: dict[tuple, str] = {}  # [jonex] I4-a 路线 B：source_chunks[].content → chunk key

        for ent in (ontology_instances or []):
            # 实体权重：命中实体的 confidence 加成（0.1~1.0 → 权重 1.1~2.0）
            w = 1.0 + min(float(ent.get("confidence") or 0.0), 1.0)
            sc = ent.get("source_chunks")
            if isinstance(sc, list) and sc:
                for s in sc:
                    fp = s.get("file_path") if isinstance(s, dict) else None
                    c = (s.get("content") or "").strip() if isinstance(s, dict) else ""
                    if fp:
                        weighted_sc.append((w, fp))
                        # [jonex] I4-a 路线 B：命中实体同口径登记 content
                        if c:
                            for seg in fp.split("<SEP>"):
                                seg = seg.strip()
                                if not seg:
                                    continue
                                parsed = parse_file_source(seg)
                                if parsed and parsed.get("doc_id"):
                                    k = (parsed["doc_id"], parsed.get("chunk_index"))
                                    content_by_key.setdefault(k, c)
            else:
                for did in (ent.get("doc_ids") or []):
                    if did:
                        fallback_doc_ids.append(did)

        for f in (facts or []):
            # 事实权重：hop 越小越直接（hop1=2.0 / hop2=1.0 / hop3=0.67）
            hop = max(int(f.get("hop") or 1), 1)
            w_fact = 2.0 / hop
            te = f.get("target_entity") if isinstance(f, dict) else None
            if isinstance(te, dict):
                sc = te.get("source_chunks")
                if isinstance(sc, list) and sc:
                    for s in sc:
                        fp = s.get("file_path") if isinstance(s, dict) else None
                        c = (s.get("content") or "").strip() if isinstance(s, dict) else ""
                        if fp:
                            weighted_sc.append((w_fact, fp))
                            # [jonex] I4-a 路线 B：本体编译时已把图片 VLM 描述
                            # 存进 source_chunks[].content，查询侧直接读，零额外查询。
                            if c:
                                for seg in fp.split("<SEP>"):
                                    seg = seg.strip()
                                    if not seg:
                                        continue
                                    parsed = parse_file_source(seg)
                                    if parsed and parsed.get("doc_id"):
                                        k = (parsed["doc_id"], parsed.get("chunk_index"))
                                        content_by_key.setdefault(k, c)
                else:
                    for did in (te.get("doc_ids") or []):
                        if did:
                            fallback_doc_ids.append(did)
            # [jonex] 方案⑧：关系边的 source_chunks（覆盖 stub 端点/别名 miss 场景；
            # 权重低于事实本体 chunk）
            rsc = f.get("relation_source_chunks")
            if isinstance(rsc, list) and rsc:
                for s in rsc:
                    fp = s.get("file_path") if isinstance(s, dict) else None
                    if fp:
                        weighted_sc.append((1.0 / hop, fp))

        # ── 解析 source_chunks file_path → 聚合权重 → raw_refs（按权重降序）──
        parsed_by_key: dict = {}
        score: dict = {}
        contrib: dict = {}
        key_order: list = []
        for w, fp in weighted_sc:
            # 单条 file_path 可能是 <SEP> 连接的多值（同一实体跨多 chunk）
            for seg in fp.split("<SEP>"):
                seg = seg.strip()
                if not seg:
                    continue
                parsed = parse_file_source(seg)
                if not (parsed and parsed.get("doc_id")):
                    continue
                # 去重键=语义键（doc+chunk+位置），避免同一 chunk 的 file_path 串变体导致重复
                # [jonex] §block-packing 改动 4：按 chunk 收敛——文本 chunk 判别维度
                # 收敛为 (doc_id, chunk_index)，page_no 不再参与（同一页可多 chunk、
                # 跨页打包 chunk 的展示页随命中片段变化，均不应造成重复引用）；
                # 音视频 chunk 保留 time_start/time_end 维度
                key = (
                    parsed["doc_id"],
                    parsed.get("chunk_index"),
                    None,
                    parsed.get("time_start"),
                    parsed.get("time_end"),
                )
                if key not in parsed_by_key:
                    parsed_by_key[key] = parsed
                    key_order.append(key)
                score[key] = score.get(key, 0.0) + w
                contrib[key] = contrib.get(key, 0) + 1
        # S3 重排：证据权重降序，同权重按贡献事实数降序（多事实共证的 chunk 优先），
        # 其余保持收集顺序（sort 稳定）
        key_order.sort(key=lambda k: (-score[k], -contrib[k]))
        # [jonex] 方案 G2：归一化证据权重写入 _evidence_score。
        # _ontology_refs 产出的 raw_refs 只有 parse_file_source 的结构字段
        # （无 text/final_score/relevance），在 _build_references 分数链中
        # 全部落空 → _img_score=0.0 → 被阈值裁掉。证据权重是本体路径图片唯一
        # 可用的分数信号——归一化到 (0,1] 后写入，让图片拿到非零分通过阈值。
        _max_score = max(score.values()) if score else 0.0
        raw_refs = []
        for k in key_order:
            ref = parsed_by_key[k]
            if _max_score > 0:
                ref["_evidence_score"] = score[k] / _max_score
            # [jonex] I4-a 主路径（路线 B）：图片 chunk 优先读编译时存入
            # source_chunks[].content 的 VLM 描述，零额外查询。
            if ref.get("image_idx") is not None and not ref.get("text"):
                ref["text"] = content_by_key.get(
                    (ref["doc_id"], ref.get("chunk_index")), "")
            raw_refs.append(ref)
        chunk_doc_ids = [parsed_by_key[k]["doc_id"] for k in key_order]

        # ── [jonex] I4-a 兜底（路线 C）：路线 B 未覆盖的图片（存量/content
        # 为空），从 LightRAG 回捞 chunk 正文（VLM 描述）。B 覆盖时零 IO。
        _img_no_text = [r for r in raw_refs
                        if r.get("image_idx") is not None and not r.get("text")]
        if _img_no_text:
            _miss_docs = list(dict.fromkeys(
                r["doc_id"] for r in _img_no_text if r.get("doc_id")))
            _chunk_text_map: dict[tuple, str] = {}
            for did in _miss_docs:
                try:
                    result = await get_rag_client().get_doc_chunks(
                        document_id=did,
                        knowledge_base_id=kb_ids[0] if kb_ids else "",
                        tenant_id=tenant_id,
                    )
                    for c in (result.get("chunks") or []):
                        fp = c.get("file_path") or ""
                        ci = parse_file_source(fp).get("chunk_index") if fp else None
                        content = c.get("content") or ""
                        if ci is not None and content:
                            _chunk_text_map[(did, ci)] = content
                except Exception as exc:
                    logger.warning("[I4-a/C] 回捞图片 chunk 正文失败 doc=%s: %r", did, exc)
            for r in _img_no_text:
                if not r.get("text"):
                    r["text"] = _chunk_text_map.get(
                        (r["doc_id"], r.get("chunk_index")), "")

        # ── chunk 级引用（source_chunks 命中）──
        refs = await self._build_references(
            tenant_id, raw_refs, allowed_kb_ids=kb_ids,
            query=query,       # [jonex] 方案 G1：打通 E3 图片描述 rerank 兜底
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
                    "source_chunks_total": len(weighted_sc),
                    "unique_file_paths": len(raw_refs),
                    "chunk_ref_count": len(refs),
                    "fallback_ref_count": len(fallback),
                    "chunk_doc_ids": chunk_doc_ids,
                    "fallback_doc_ids": fallback_doc_ids,
                    # [jonex] S3：引用按证据权重重排，前 5 权重可见（排查用）
                    "top_ref_scores": [
                        round(score.get(key, 0.0), 2)
                        for key in key_order[:5]
                    ],
                },
                t_start=t,
            )

        return refs + fallback

    async def _rag_platform_answer(
        self,
        tenant_id: str,
        user_id: str,
        req: OntologySearchRequest,
        kb_ids: list[str],
        trace_id: str | None,
        collector: ReasoningCollector | None = None,
        ontology_instances: list[dict] | None = None,
        fast_mode: bool = False,
    ) -> dict:
        """[jonex] 方案 A 新路径（§6.1）：多 KB 只召回（only_need_context）→ 平台侧一次作答。

        答案与引用同源：references 直接取「送进 prompt 的那批 chunk」。
        任一环失败抛异常，由 _rag_fallback_multi 当次回退旧链路（§6.6）。

        Returns:
            {"answer": str, "references": list[dict]}
        """
        rag = get_rag_client()
        t_retrieve = time.perf_counter()

        # ── 1) 多 KB 并行只召回（不调生成 LLM）──
        _sem = asyncio.Semaphore(ONTOLOGY_RAG_MAX_CONCURRENCY)

        async def _query_ctx(kid: str) -> dict | Exception:
            async with _sem:
                try:
                    return await rag.query_detailed(
                        query=req.query, tenant_id=tenant_id, mode=req.mode,
                        top_k=req.top_k, knowledge_base_id=kid,
                        trace_id=trace_id or "", user_id=user_id,
                        only_need_context=True,
                    )
                except Exception as e:
                    return e

        results = await asyncio.gather(*[_query_ctx(kid) for kid in kb_ids])
        ctx_kb: list[str] = []
        kb_failed: list[str] = []
        all_raw_refs: list[dict] = []
        for kid, res in zip(kb_ids, results):
            if isinstance(res, Exception):
                logger.warning("[rag_platform_answer] KB 召回失败 kb=%s: %s", kid, res)
                kb_failed.append(kid)
                continue
            refs = (res or {}).get("references") or []
            if refs:
                ctx_kb.append(kid)
                all_raw_refs.extend(refs)
            else:
                kb_failed.append(kid)

        if not all_raw_refs:
            raise RuntimeError("only_need_context 召回为空（全部 KB 无 references）")

        raw_recall_count = len(all_raw_refs)

        # ── 2) doc_map 预查（与旧路径一致，供打分与 _build_references 共用）──
        recall_doc_ids = [r.get("doc_id") for r in all_raw_refs if r.get("doc_id")]
        doc_map: dict[str, Any] = {}
        if recall_doc_ids:
            async with get_db_session() as session:
                repo = KnowledgeDocumentRepository(session)
                docs = await repo.get_by_ids(list(set(recall_doc_ids)), tenant_id)
            doc_map = {d.id: d for d in docs}

        # ── [jonex] S8：低质 chunk 过滤（doc 聚合之前——先剔垃圾再聚合）──
        if RAG_LOWQ_CHUNK_FILTER_ENABLED and all_raw_refs:
            all_raw_refs, lowq_filtered = self._filter_low_quality_chunks(
                all_raw_refs,
            )
            if collector and lowq_filtered:
                collector.step(
                    STAGE_CONTEXT_RETRIEVE, "低质 chunk 过滤",
                    summary=f"剔除 {len(lowq_filtered)} 个低质 chunk",
                    detail={"lowq_filtered": lowq_filtered},
                )

        # ── [jonex] L4.2：双路召回配额（row : summary ≈ 3 : 1）──
        if RAG_DUAL_PATH_QUOTA_ENABLED and all_raw_refs:
            all_raw_refs, quota_stats = self._apply_dual_path_quota(
                all_raw_refs,
            )
            if collector and (
                quota_stats["table_summary_kept"]
                < quota_stats["table_summary_total"]
                or quota_stats["table_row_kept"]
                < quota_stats["table_row_total"]
            ):
                collector.step(
                    STAGE_CONTEXT_RETRIEVE, "双路召回配额",
                    summary=(
                        f"row 取 {quota_stats['table_row_kept']}/"
                        f"{quota_stats['table_row_total']}、"
                        f"summary 取 {quota_stats['table_summary_kept']}/"
                        f"{quota_stats['table_summary_total']}"
                    ),
                    detail={"dual_path_quota": quota_stats},
                )

        # ── 3) 主体加权 / rerank / top-K（复用方案 B 与 P1-5 逻辑）──
        subject_stats: dict[str, Any] = {}
        if RAG_SUBJECT_FILTER_ENABLED:
            subjects = self._resolve_query_subjects(req.query, ontology_instances)
            if subjects:
                all_raw_refs, subject_stats = await self._subject_weight_and_rank(
                    req.query, all_raw_refs, subjects, doc_map,
                    tenant_id=tenant_id,
                    kb_id=kb_ids[0] if kb_ids else "",
                    trace_id=trace_id, user_id=user_id,
                )
        elif RAG_PRELLM_RERANK_ENABLED and len(all_raw_refs) > RAG_PRELLM_RERANK_TOPK:
            all_raw_refs = await self._prellm_rerank_chunks(
                req.query, all_raw_refs,
                tenant_id=tenant_id,
                kb_id=kb_ids[0] if kb_ids else "",
                trace_id=trace_id, user_id=user_id,
            )

        # ── 4) 预算硬截（按分数从高到低，总长 ≤ RAG_ANSWER_MAX_CONTEXT_CHARS）──
        # [jonex] 软删防御（prompt 层）：doc_map 按 tenant + is_deleted==0 预查，
        # 已删除文档的残留 chunk（LightRAG 清理失败的幽灵）直接跳过，不进预算、
        # 不参与作答——答案与引用同源，prompt_chunk_count 与 recall_count 收敛一致。
        allowed = set(kb_ids)
        prompt_refs: list[dict] = []
        total = 0
        ghost_filtered = 0
        for r in all_raw_refs:
            did = r.get("doc_id")
            d = doc_map.get(did)
            if d is None or d.knowledge_base_id not in allowed:
                ghost_filtered += 1
                continue
            text = (r.get("text") or "").strip()
            if not text:
                continue
            if total + len(text) > RAG_ANSWER_MAX_CONTEXT_CHARS:
                break
            prompt_refs.append(r)
            total += len(text)
        if not prompt_refs:
            raise RuntimeError("预算截断后无可用 chunk")
        if ghost_filtered:
            logger.warning(
                "prompt 层软删防御: 剔除 %d 个已删除文档的残留 chunk，"
                "作答上下文剩 %d 个（答案与引用同源）",
                ghost_filtered, len(prompt_refs),
            )

        # ── 4.5) 召回明细（口径同旧路径 rag_fallback 的 recalls：实际进入
        # prompt 的 chunk，与 references 同源——答案/引用/明细三者一致）──
        recalls: list[dict] = []
        if ONTOLOGY_RAG_RECALL_DETAIL_ENABLED and collector and prompt_refs:
            for r in prompt_refs[:ONTOLOGY_RAG_RECALL_MAX_ITEMS]:
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
                STAGE_CONTEXT_RETRIEVE, "多 KB 只召回不生成",
                summary=(
                    f"{len(ctx_kb)}/{len(kb_ids)} 个知识库返回上下文，"
                    f"召回 {raw_recall_count} 个片段，取 {len(prompt_refs)} 个作答"
                ),
                detail={
                    "kb_ok": ctx_kb,
                    "kb_failed": kb_failed,
                    "raw_recall_count": raw_recall_count,
                    "prompt_chunk_count": len(prompt_refs),
                    "soft_deleted_filtered": ghost_filtered,
                    "recall_count": len(recalls),
                    "recalls": recalls,
                    "subject_weighting": subject_stats,
                    "only_need_context": True,
                },
                t_start=t_retrieve,
            )

        # ── 5) 平台侧一次作答（异常传播，由外层回退旧链路）──
        t_answer = time.perf_counter()
        answer = await answer_from_chunks(
            req.query, prompt_refs,
            tenant_id=tenant_id,
            kb_id=kb_ids[0] if kb_ids else None,
            user_id=user_id,
            trace_id=trace_id,
            fast_mode=fast_mode,
        )
        answer_ms = int((time.perf_counter() - t_answer) * 1000)
        if collector:
            collector.step(
                STAGE_CHUNK_ANSWER, "平台侧基于 chunk 作答",
                summary=f"基于 {len(prompt_refs)} 个 chunk 生成答案（{answer_ms}ms）",
                detail={
                    "chunk_count": len(prompt_refs),
                    "answer_ms": answer_ms,
                    "scene": _scene("rag_chunk_qa", fast_mode),
                },
                t_start=t_answer,
            )
            # 新路径下融合阶段不触发（STAGE_RAG_FALLBACK 同样不发，避免两套语义混用）
            collector.step(
                STAGE_FUSION, "多答案融合", status="skipped",
                summary="平台侧统一作答，无需多答案融合",
            )

        # ── 6) references = 送进 prompt 的同一批 chunk（答案与引用同源）──
        references = await self._build_references(
            tenant_id, prompt_refs,
            allowed_kb_ids=kb_ids, doc_map=doc_map,
            query=req.query,   # [jonex] §10 L2 页段打分
        )
        self._log_rag_timing(
            tenant_id, answer_ms + int((time.perf_counter() - t_retrieve) * 1000),
            None, len(ctx_kb), len(kb_ids), kb_failed,
        )
        return {"answer": answer, "references": references}

    async def _rag_fallback_multi(
        self, tenant_id: str, user_id: str, req: OntologySearchRequest,
        kb_ids: list[str], trace_id: str | None,
        collector: ReasoningCollector | None = None,
        ontology_instances: list[dict] | None = None,
        fast_mode: bool = False,
    ) -> dict:
        """策略 A：并行查询全部 KB 的 RAG → LLM 融合。

        Args:
            collector: 可选，推理链采集器（P0 非流式埋点）。
            ontology_instances: 可选，本体命中实例（方案 B 主体信号首选来源；
                命中分不足 / INSUFFICIENT 等降级场景下仍有值，只有四级均未
                命中才为空）。
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

        # ── [jonex] 方案 A 分叉：平台取回作答权（开关控制，旧路径一行不改）──
        # 新路径任一环失败 → 记 warning 并当次回退旧路径（同一请求内重跑检索）。
        if RAG_PLATFORM_ANSWER_ENABLED:
            try:
                return await self._rag_platform_answer(
                    tenant_id, user_id, req, kb_ids, trace_id,
                    collector=collector,
                    ontology_instances=ontology_instances,
                    fast_mode=fast_mode,
                )
            except Exception as e:
                logger.warning(
                    "[rag_platform_answer] 新路径失败，当次回退旧链路: %s", e
                )

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

        # [jonex] 融合 prompt 用 KB 显示名标识来源（LLM 照抄标识进答案；
        # kb_id UUID 不可读时会产生「知识库 `470a9519...`」这类错误标注）
        _kb_names = await self._get_kb_names(tenant_id, [p["kb_id"] for p in per_kb])
        for p in per_kb:
            p["kb_name"] = _kb_names.get(p["kb_id"], p["kb_id"])

        rag_multi_ms = int((time.perf_counter() - t_rag) * 1000)

        # 埋点口径：过滤/重排前的候选总数（见 rag-subject-filter 方案 §4.2，
        # summary 的「召回数」必须是检索命中数，不是过滤后的残量）
        raw_recall_count = len(all_raw_refs)

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

        # ── [jonex] 方案 B 主体一致性软加权（替代原 P1-5 硬剔除过滤）──
        # 分层局限：本层加权/重排作用在 LightRAG 已生成答案之后的引用上，
        # 可改善展示引用与融合排序，但不改单个 KB 内部已被污染的答案。
        # 若同 KB 内召回了不相关 chunk 并已影响 LightRAG 生成的答案，
        # 需在 LightRAG 检索侧做主体过滤才能真正纠正（方案 A，见 §6）。
        subject_filtered_count = 0
        filter_wiped_all = False
        subject_stats: dict[str, Any] = {}
        prellm_reranked = False
        prellm_rerank_scores: list[float] = []
        if RAG_SUBJECT_FILTER_ENABLED and all_raw_refs:
            subjects = self._resolve_query_subjects(req.query, ontology_instances)
            if subjects:
                all_raw_refs, subject_stats = await self._subject_weight_and_rank(
                    req.query, all_raw_refs, subjects, doc_map,
                    tenant_id=tenant_id,
                    kb_id=kb_ids[0] if kb_ids else "",
                    trace_id=trace_id, user_id=user_id,
                )
                prellm_reranked = bool(subject_stats.get("reranked"))
                prellm_rerank_scores = [
                    round(
                        r.get("final_score")
                        if r.get("final_score") is not None
                        else r.get("group_final_score", r.get("relevance", 0)),
                        4,
                    )
                    for r in all_raw_refs[:5]
                ]
                logger.info(
                    "[subject_weight] query=%r subjects=%s stats=%s",
                    req.query[:80], subjects, subject_stats,
                )
            else:
                logger.info("[subject_weight] 未解析到主体信号，跳过 query=%r", req.query[:80])

        # ── [jonex] P1-5 送 LLM 前 chunk 级重排（主体信号未启用时的独立路径，行为不变）──
        if not subject_stats and RAG_PRELLM_RERANK_ENABLED and all_raw_refs \
                and len(all_raw_refs) > RAG_PRELLM_RERANK_TOPK:
            original_count = len(all_raw_refs)
            all_raw_refs = await self._prellm_rerank_chunks(
                req.query, all_raw_refs,
                tenant_id=tenant_id,
                kb_id=kb_ids[0] if kb_ids else "",
                trace_id=trace_id, user_id=user_id,
            )
            prellm_reranked = len(all_raw_refs) < original_count
            prellm_rerank_scores = [round(r.get("relevance", 0), 4) for r in all_raw_refs[:5]]

        # 埋点口径：过滤/重排后的候选数（rag-subject-filter 方案 §4.2）
        post_filter_count = len(all_raw_refs)

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
                summary=f"{len(per_kb)}/{len(kb_ids)} 个知识库返回有效答案，召回 {raw_recall_count} 个片段"
                        + (f"，重排后取 {post_filter_count} 个" if prellm_reranked else ""),
                detail={
                    "kb_ok": [p["kb_id"] for p in per_kb],
                    "kb_failed": kb_failed,
                    "recall_count": len(recalls),
                    "recalls": recalls,
                    "p1_5": {
                        "subject_filter_enabled": RAG_SUBJECT_FILTER_ENABLED,
                        "subject_weight": RAG_SUBJECT_WEIGHT,
                        # 双数字口径：过滤/重排前的候选总数 vs 过滤/重排后（rag-subject-filter 方案 §4.2）
                        "raw_recall_count": raw_recall_count,
                        "post_filter_count": post_filter_count,
                        "filter_wiped_all": filter_wiped_all,
                        "subject_filtered_count": subject_filtered_count,
                        # 方案 B 软加权统计（未启用主体信号时为空 dict）
                        "subject_weighting": subject_stats,
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
                    "OntoRAG_rerank": {
                        "enabled": RAG_RETRIEVAL_RERANK_ENABLED,
                        "triggered": hit is True,
                    },
                    "subject_filter": {
                        "enabled": RAG_SUBJECT_FILTER_ENABLED,
                        "weight": RAG_SUBJECT_WEIGHT,
                        "filtered": subject_filtered_count,
                        "weighting": subject_stats,
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
                fast_mode=fast_mode,
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
            tenant_id, all_raw_refs, allowed_kb_ids=kb_ids,
            doc_map=doc_map if doc_map else None,
            query=req.query,   # [jonex] §10 L2 页段打分
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
        # [jonex] §block-packing 改动 4：该 2 元组去重键与「按 chunk 收敛」
        # 决策一致（page_no 不参与判别，跨页打包 chunk 不同命中片段不产生
        # 重复引用），无需变更
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

        result = {
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
        if req.save_history:
            history = await self._history.save_history(
                tenant_id, user_id,
                SearchHistoryCreateRequest(
                    query=req.query,
                    knowledge_base_id="" if len(kb_ids) > 1 else (kb_ids[0] if kb_ids else ""),
                    mode=req.mode,
                    top_k=req.top_k,
                    domain_space_id=req.domain_space_id,
                    answer_preview=(answer or "")[:300],
                    answer=answer,
                    references=all_refs or [],
                    reasoning=result["reasoning"],
                    metadata={
                        "knowledge_base_ids": kb_ids,
                        "source": "deep",
                        "pipeline": "deep",
                    },
                ),
            )
            result["history_id"] = history.get("id")
        return result

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
            # [jonex] 查询期思考分档 override（§3.6）：thinking/cross_verify/内层超时
            # 随档位注入，与既有 _route_score_min_override/_neighbor_depth_override 同构。
            sub_req = req.copy(update={
                "top_k": gear["top_k"],
                "with_reasoning": req.with_reasoning,
                "strict_mode": False,
                "save_history": False,  # 内层循环不落库，strict 最外层统一落一条
                "_route_score_min_override": gear.get("route_score_min", ONTOLOGY_ROUTE_SCORE_MIN),
                "_neighbor_depth_override": gear.get("neighbor_depth", ONTOLOGY_NEIGHBOR_DEPTH),
                "_fast_llm_override": not gear.get("thinking", True),
                "_cross_verify_override": gear.get("cross_verify", True),
                "_neighbor_timeout_override": gear.get("neighbor_timeout", ONTOLOGY_NEIGHBOR_TIMEOUT),
                "_answer_timeout_override": gear.get("answer_timeout", ONTOLOGY_ANSWER_TIMEOUT),
            })

            # [jonex] 每档独立超时（§3.4）：原全局 60s 对快档过松、对精档过紧。
            gear_timeout = gear.get("timeout", _STRICT_ATTEMPT_TIMEOUT)
            try:
                result = await asyncio.wait_for(
                    self.query_with_ontology(
                        tenant_id, user_id, sub_req, trace_id,
                    ),
                    timeout=gear_timeout,
                )
            except asyncio.TimeoutError:
                collector.step(
                    STAGE_STRICT_ATTEMPT, f"严格模式·第{i + 1}次尝试（{label}）",
                    status="failed",
                    summary=f"尝试超时（{gear_timeout}s），进入下一档",
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
        if req.save_history:
            kb_ids_for_hist = result.get("knowledge_base_ids") or []
            history = await self._history.save_history(
                tenant_id, user_id,
                SearchHistoryCreateRequest(
                    query=req.query,
                    knowledge_base_id="" if len(kb_ids_for_hist) > 1 else (kb_ids_for_hist[0] if kb_ids_for_hist else ""),
                    mode=req.mode,
                    top_k=req.top_k,
                    domain_space_id=req.domain_space_id,
                    answer_preview=(result.get("answer") or "")[:300],
                    answer=result.get("answer"),
                    references=result.get("references") or [],
                    reasoning=result.get("reasoning"),
                    metadata={
                        "knowledge_base_ids": kb_ids_for_hist,
                        "source": result.get("source"),
                        "pipeline": "ontology-strict",
                    },
                ),
            )
            result["history_id"] = history.get("id")
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
        # ── [jonex] 查询期思考分档 override（docs/ontology-query-thinking-latency-fix-plan.md §3.6）
        # 读取逻辑见 _read_gear_overrides；缺省值 = 现状（零行为变化）。
        _fast_llm, _cross_enabled, _nb_timeout, _answer_timeout = _read_gear_overrides(raw)

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
                        {
                            "name": i.get("name"),
                            "score": i.get("score"),
                            "kb_id": i.get("kb_id"),
                            "type": i.get("type"),
                            "description": i.get("description"),
                            "attributes": i.get("attributes"),
                            "confidence": i.get("confidence"),
                            "doc_ids": i.get("doc_ids"),
                        }
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
            # [jonex] 多意图：收集所有过线命中（上限 ONTOLOGY_NEIGHBOR_ENTITY_MAX），
            # 供阶段 3 逐一展开邻域；matched 仍取首个（向后兼容路由展示/时间线模板）。
            top_n = ontology_instances[:5]
            go_ontology = False
            matched_list: list[dict] = []
            for hit in top_n:
                src = hit.get("source", "")
                vs = hit.get("vscore", 0)
                fs = hit.get("ft_score", hit.get("score", 0))
                if src in ("exact", "prefix") or vs >= ONTOLOGY_VECTOR_SCORE_MIN or fs >= _route_min:
                    go_ontology = True
                    if len(matched_list) < ONTOLOGY_NEIGHBOR_ENTITY_MAX:
                        matched_list.append(hit)
            matched = matched_list[0] if matched_list else None

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
                # [jonex] S5：枚举意图 → 跳过本体取证与作答，强制走 RAG
                # 取原始表格行（整行字段一并回传，覆盖「缺电话列」类召回不全）。
                enum_intent = self._detect_enumeration_intent(req.query)
                if enum_intent:
                    collector.step(
                        STAGE_FACT_LOOKUP, "枚举意图处置", status="skipped",
                        summary="枚举意图：跳过本体作答，强制走 RAG 取原始表格行",
                        detail={"query": req.query},
                    )
                    logger.info("[ontology] 枚举意图 → 强制 RAG query=%r", req.query)
                if timeline and not enum_intent and top_type in (
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
                if facts is None and not enum_intent:  # S5：枚举意图不取邻域，facts=None → 自然降级 RAG
                    try:
                        # [jonex] 多意图：对 matched_list 里所有过线实体并发展开邻域，
                        # 合并 facts 按 (target, relation_type, path) 去重，避免只取
                        # top1 漏掉其他实体的关系。单实体时退化为原单次
                        # neighbors()；单个实体失败不阻断其余，仅记 warning。
                        async def _fetch_neighbors(m: dict):
                            _kb = m.get("kb_id") or kb_ids[0]
                            _name = m.get("name", "")
                            return _kb, _name, await gdao.neighbors(
                                tenant_id, _kb, _name,
                                limit=ONTOLOGY_NEIGHBOR_LIMIT,
                                depth=_nb_depth,
                                per_hop_limit=ONTOLOGY_NEIGHBOR_PER_HOP_LIMIT,
                            )

                        neighbor_results = await asyncio.wait_for(
                            asyncio.gather(
                                *[_fetch_neighbors(m) for m in matched_list],
                                return_exceptions=True,
                            ),
                            timeout=_nb_timeout,
                        )
                        facts = []
                        neighbor_depth = 1
                        hop_distribution: dict[int, int] = {}
                        truncated = False
                        seen_facts: set[tuple] = set()
                        for _kb, _name, res in neighbor_results:
                            if isinstance(res, Exception):
                                logger.warning(
                                    "[ontology] 邻域取证失败 entity=%s kb=%s: %s",
                                    _name, _kb, res,
                                )
                                continue
                            neighbor_depth = max(neighbor_depth, int(res.get("depth", 1)))
                            truncated = truncated or bool(res.get("truncated", False))
                            for h, c in (res.get("hop_distribution") or {}).items():
                                hop_distribution[h] = hop_distribution.get(h, 0) + c
                            for f in res.get("facts", []):
                                key = (
                                    f.get("target", ""),
                                    f.get("relation_type", ""),
                                    tuple(f.get("path") or []),
                                )
                                if key in seen_facts:
                                    continue
                                seen_facts.add(key)
                                facts.append(f)
                        collector.step(
                            STAGE_FACT_LOOKUP, "邻域事实检索",
                            summary=(
                                f"取到 {len(facts)} 条事实"
                                + (f"（{neighbor_depth} 跳）"
                                   if neighbor_depth > 1 else "（1 跳）")
                            ),
                            detail={
                                "entities": [m.get("name", "") for m in matched_list],
                                "kb_id": top_kb_id,
                                "fact_count": len(facts),
                                "depth": neighbor_depth,
                                "hop_distribution": hop_distribution,
                                "truncated": truncated,
                                "facts": facts,
                            },
                            t_start=t,
                        )
                    except asyncio.TimeoutError:
                        collector.step(STAGE_FACT_LOOKUP, "邻域事实检索", status="failed",
                                       summary="邻域查询超时，降级 OntoRAG", t_start=t)
                        logger.warning("[ontology] 邻域查询超时（%ds），降级 RAG", _nb_timeout)
                    except Exception as e:
                        collector.step(STAGE_FACT_LOOKUP, "邻域事实检索", status="failed",
                                       summary="邻域检索失败，降级 OntoRAG", t_start=t)
                        logger.warning("[ontology] 邻域检索失败，降级 RAG: %s", e)

                # ── [jonex] S2：stub 事实过滤（desc 空且 attrs 空）──
                # 在两个 facts 来源（图查询模板 / neighbors）之后统一执行；
                # 全部被过滤时自然走 INSUFFICIENT → 降级 RAG。
                if facts:
                    facts = self._filter_stub_facts(facts, collector=collector)

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
                                fast_mode=_fast_llm,
                            ),
                            timeout=_answer_timeout,   # [jonex] 方案④ 可调超时（两档覆写）
                        )
                        if llm_answer and llm_answer != "INSUFFICIENT":
                            answer = llm_answer
                            source = "ontology"
                            rag_used = False
                            collector.step(STAGE_LLM_ANSWER, "本体事实作答",
                                           summary="基于本体事实生成答案", t_start=t,
                                           detail={"thinking": not _fast_llm,
                                                   "answer_timeout": _answer_timeout})
                        else:
                            collector.step(STAGE_LLM_ANSWER, "本体事实作答", status="skipped",
                                           summary="事实不足（INSUFFICIENT），降级 OntoRAG", t_start=t)
                    except asyncio.TimeoutError:
                        collector.step(STAGE_LLM_ANSWER, "本体事实作答", status="failed",
                                       summary=f"本体 LLM 超时（{_answer_timeout}s），降级 OntoRAG", t_start=t)
                        logger.warning("[ontology] 本体 LLM 回答超时（%ds），降级 RAG", _answer_timeout)
                    except Exception as e:
                        collector.step(STAGE_LLM_ANSWER, "本体事实作答", status="failed",
                                       summary="本体作答失败，降级 OntoRAG", t_start=t)
                        logger.warning("[ontology] 本体问答失败，降级 RAG: %s", e)

                # ── [jonex] 命中实体但无内容兜底（避免误导性「未找到」）──
                # 手动实体只有名字、无 desc/attrs/关系时，answer_from_facts 正确返回
                # INSUFFICIENT。此时不降级 RAG（RAG 无关联文档，必然空手而归），直接
                # 返回「已找到实体但未录入内容」的友好提示。
                # 触发条件须严格：facts 为空列表（neighbors 成功但无关系，而非超时/失败
                # 留下的 None），且命中实体确实无 desc/attrs 才兜底，不误伤有内容实体。
                if (
                    answer is None
                    and matched_list
                    and facts is not None
                    and not facts
                ):
                    empty_names = [
                        h.get("name", "")
                        for h in matched_list
                        if not self._entity_has_content(h)
                    ]
                    if empty_names:
                        names_text = "、".join(
                            f"「{n}」" for n in empty_names[:ONTOLOGY_NEIGHBOR_ENTITY_MAX]
                        )
                        answer = (
                            f"已找到实体 {names_text}，但未录入描述或属性，"
                            "暂时无法回答具体内容。请先为该实体补充描述/属性，"
                            "或尝试换个方式提问。"
                        )
                        source = "ontology"
                        rag_used = False
                        collector.step(
                            STAGE_LLM_ANSWER, "空内容实体兜底",
                            summary="命中实体但无描述/属性/关系，返回友好提示",
                            detail={"entities": empty_names},
                        )
                        logger.info(
                            "[ontology] 命中空内容实体，返回友好提示 entities=%s query=%r",
                            empty_names, req.query,
                        )
            else:
                logger.info(
                    "[ontology] 路由=RAG降级（命中但分数不足）source=%s vscore=%.4f ft_score=%s query=%r",
                    top_source, top_vscore, top_ftscore, req.query,
                )

        # ── 阶段 5/6：RAG Fallback + 融合（采集点⑤⑥在 _rag_fallback_multi 内部）──
        references: list[dict] = []
        if answer is None:
            fallback = await self._rag_fallback_multi(
                tenant_id, user_id, req, kb_ids, trace_id, collector=collector,
                ontology_instances=ontology_instances,
                fast_mode=_fast_llm)
            answer = fallback["answer"]
            references = fallback["references"]
            source = "rag"
        else:
            # 方案⑦：本体路径成功，从 source_chunks 直接构建 chunk 级引用
            references = await self._ontology_refs(
                tenant_id=tenant_id, kb_ids=kb_ids,
                ontology_instances=ontology_instances, facts=facts,
                collector=collector,
                query=req.query,   # [jonex] 方案 G1：打通 E3 图片描述 rerank 兜底
            )

        # ── [jonex] S1+S7：双向校验与裁决（§14.2/§15.4，两路答案就绪后）──
        # 高置信 → 不跑对侧（保持单路延迟）；中/低置信或 RAG 侧命中本体
        # 结构化槽位 → 裁决；异常一律回退现有答案（绝不因裁决故障丢答案）。
        if ONTOLOGY_ARBITRATION_ENABLED and answer and not _cross_enabled:
            # [jonex] 两档升档（§3.3）：快档跳过对侧校验，精档才跑 S1+S7
            collector.step(STAGE_ARBITRATION, "双向校验裁决", status="skipped",
                           summary="快档跳过对侧校验（cross_verify=false）")
        elif ONTOLOGY_ARBITRATION_ENABLED and answer:
            try:
                final = await self._cross_verify(
                    tenant_id, user_id, req, kb_ids, trace_id,
                    answer=answer, source=source, references=references,
                    facts=facts, ontology_instances=ontology_instances,
                    collector=collector,
                    fast_mode=_fast_llm, answer_timeout=_answer_timeout,
                )
                if final is not None:
                    answer = final["answer"]
                    source = final["source"]
                    references = final["references"]
            except Exception as exc:  # noqa: BLE001 — 裁决故障绝不丢已有答案
                logger.warning("[ontology] 裁决失败，回退原答案: %s", exc)
                if collector:
                    collector.step(
                        STAGE_ARBITRATION, "双向校验裁决", status="skipped",
                        summary=f"裁决失败回退原答案（{source}）",
                    )

        result = {
            "answer": answer,
            "source": source,
            "references": references,
            "ontology_instances": ontology_instances,
            "rag_used": rag_used,
            "knowledge_base_ids": kb_ids,
            "reasoning": collector.build(source),
        }
        if req.save_history:
            history = await self._history.save_history(
                tenant_id, user_id,
                SearchHistoryCreateRequest(
                    query=req.query,
                    knowledge_base_id="" if len(kb_ids) > 1 else (kb_ids[0] if kb_ids else ""),
                    mode=req.mode,
                    top_k=req.top_k,
                    domain_space_id=req.domain_space_id,
                    answer_preview=(answer or "")[:300],
                    answer=answer,
                    references=references,
                    reasoning=result["reasoning"],
                    metadata={
                        "knowledge_base_ids": kb_ids,
                        "source": source,
                        "pipeline": "ontology",
                    },
                ),
            )
            result["history_id"] = history.get("id")
        return result

    # ── [jonex] S1+S7 双向校验与裁决 ───────────────────────────────────

    @staticmethod
    def _assess_ontology_confidence(
        facts: list[dict] | None, hits: list[dict] | None,
    ) -> tuple[str, dict]:
        """S1 置信分级（§14.2）：四维判定 → high / mid / low。

        - 结构化属性（attributes 非空）是强信号；
        - 多 hop（>1）是弱信号；
        - 多候选值冲突（多个事实的 attributes 值不一致）直接判 low
          （行10 版本号场景：Ver1.31 vs 1.11版本 并存）。
        """
        if not facts:
            return "low", {"reason": "no_facts"}
        has_attrs = False
        has_desc = False
        max_hop = 1
        attr_by_key: dict[str, set] = {}
        for f in facts:
            te = f.get("target_entity") or {}
            attrs = te.get("attributes")
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except (NameError, AttributeError, ImportError):
                    raise  # 代码 bug，不掩盖
                except Exception as exc:
                    logger.debug("S1 confidence attributes 解析降级: %s", exc)
                    attrs = None
            if attrs:
                has_attrs = True
                for k, v in attrs.items():
                    if isinstance(v, dict):
                        # [jonex] 属性组（§10.2.3）：内层叶子键/值参与冲突检测
                        for k2, v2 in v.items():
                            if v2:
                                attr_by_key.setdefault(str(k2), set()).add(str(v2))
                    elif v:
                        attr_by_key.setdefault(str(k), set()).add(str(v))
            if (te.get("description") or "").strip():
                has_desc = True
            hop = f.get("hop", 1)
            if isinstance(hop, int) and hop > max_hop:
                max_hop = hop
        # 多候选冲突 = 同一属性键在不同事实间值不一致
        # （行10：版本号 Ver1.31 vs 1.11 并存）——同一实体的多属性不算冲突
        conflict = any(len(vals) > 1 for vals in attr_by_key.values())
        detail = {
            "has_attrs": has_attrs, "has_desc": has_desc,
            "max_hop": max_hop, "conflict": conflict,
        }
        if conflict:
            return "low", detail
        if has_attrs and max_hop <= 1:
            return "high", detail
        if has_attrs or has_desc:
            return "mid", detail
        return "low", detail

    @staticmethod
    def _ontology_slot_hit(query: str, ontology_instances: list[dict] | None) -> bool:
        """S7 反向触发（§15.4）：查询槽位在本体侧有结构化 attributes 命中。

        治行19：RAG 答错（主体消歧失败）但本体侧存在
        `武汉分公司（386）.attributes["业务联系人"]="顾雅文"`——attribute
        键或值与 query 词元重叠即触发裁决。
        """
        if not query or not ontology_instances:
            return False
        for hit in ontology_instances[:10]:
            attrs = hit.get("attributes")
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except (NameError, AttributeError, ImportError):
                    raise  # 代码 bug，不掩盖
                except Exception as exc:
                    logger.debug("S7 slot_hit attributes 解析降级: %s", exc)
                    attrs = None
            if not isinstance(attrs, dict):
                continue
            for k, v in attrs.items():
                if k and k in query:
                    return True
                if isinstance(v, dict):
                    # [jonex] §10.2.3 层级关系：属性组（多级表头）的内层
                    # 键/值同样参与槽位命中
                    for k2, v2 in v.items():
                        if (k2 and k2 in query) or (v2 and str(v2) in query):
                            return True
                elif v and str(v) in query:
                    return True
        return False

    @staticmethod
    def _norm_answer_text(s: str) -> str:
        """答案归一化（一致性判定用）：全角数字/点/斜杠转半角、去空白、小写。"""
        table = {ord(c): ord("0") + i for i, c in enumerate("０１２３４５６７８９")}
        table[ord("．")] = ord(".")
        table[ord("／")] = ord("/")
        s = s.translate(table)
        return re.sub(r"\s+", "", s).lower()

    @staticmethod
    def _answers_agree(a: str, b: str) -> bool:
        """[jonex] §15.4 两路一致判定（一致 → 免裁决，省一次 LLM）。

        保守口径，宁裁决不误放：
        - 归一化后完全相同；或
        - 数值经尾零规范化（``40`` 与 ``40.0`` 等价）后集合一致，
          且非数值部分结构一致（仅数字格式差异，如
          ``40 元／手`` vs ``40.0元/手``）。
        同一数值但主体不同的答案（``A 的手续费 40`` vs ``B 的手续费 40``）
        非数值部分不同 → 判不一致，仍进裁决。
        """
        if not a or not b:
            return False
        na = SearchService._norm_answer_text(a)
        nb = SearchService._norm_answer_text(b)
        if na == nb:
            return True

        def _norm_num(m: re.Match) -> str:
            s = m.group(0)
            return s.rstrip("0").rstrip(".") if "." in s else s

        ca = re.sub(r"\d+\.?\d*", _norm_num, na)
        cb = re.sub(r"\d+\.?\d*", _norm_num, nb)
        nums_a = set(re.findall(r"\d+\.?\d*", ca))
        nums_b = set(re.findall(r"\d+\.?\d*", cb))
        if not nums_a or nums_a != nums_b:
            return False
        return re.sub(r"\d+\.?\d*", "#", ca) == re.sub(r"\d+\.?\d*", "#", cb)

    @staticmethod
    def _facts_evidence(facts: list[dict] | None) -> str:
        """渲染本体事实证据（带锚点信息，供裁决对称呈现）。"""
        lines: list[str] = []
        for f in (facts or [])[:12]:
            te = f.get("target_entity") or {}
            attrs = te.get("attributes") or {}
            desc = (te.get("description") or "")[:120]
            anchor = f.get("relation_source_chunks") or te.get("source_chunks")
            lines.append(
                f"- {f.get('target')} (hop={f.get('hop', 1)}) attrs={attrs} "
                f"desc={desc} anchored={bool(anchor)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _refs_evidence(refs: list[dict] | None) -> str:
        """渲染 RAG chunk 证据（带 ctype 锚点，供裁决对称呈现）。"""
        lines: list[str] = []
        for r in (refs or [])[:12]:
            text = (r.get("text") or r.get("content") or "")[:120]
            try:
                parsed = parse_file_source(r.get("file_path") or "") or {}
            except (NameError, AttributeError, ImportError):
                raise  # 代码 bug，不掩盖
            except Exception as exc:
                logger.debug("S7 refs_evidence 解析降级: %s", exc)
                parsed = {}
            ctype = parsed.get("chunk_type") or ""
            row = parsed.get("row_start")
            lines.append(
                f"- [{ctype}] (row={row}) {text}"
                if row is not None else f"- [{ctype}] {text}"
            )
        return "\n".join(lines)

    async def _cross_verify(
        self,
        tenant_id: str,
        user_id: str,
        req: OntologySearchRequest,
        kb_ids: list[str],
        trace_id: str | None,
        *,
        answer: str,
        source: str,
        references: list[dict],
        facts: list[dict] | None,
        ontology_instances: list[dict] | None,
        collector: ReasoningCollector | None,
        fast_mode: bool = False,
        answer_timeout: int = ONTOLOGY_ANSWER_TIMEOUT,
    ) -> dict | None:
        """S1+S7 统一裁决入口（§14.2/§15.4）。

        触发条件（任一满足即跑对侧）：
        - 本体侧为中/低置信（S1 分层）；
        - RAG 侧作答且查询槽位在本体侧有结构化 attributes 命中（S7，治行19）；
        - RAG 侧命中 chunk 存在低质信号（S8 判据命中但未被完全剔除）。

        两侧答案就绪后：一致 → 免裁决直接返回（cross_verified=true）；
        一方无结果 → 返回有结果一方（unverified）；不一致 → LLM 裁决。
        裁决异常/超时返回 None（调用方回退原答案，绝不因校验故障丢答案）。
        """
        onto_answer: str = answer if source == "ontology" else ""
        rag_answer: str = answer if source == "rag" else ""
        onto_refs = references if source == "ontology" else []
        rag_refs = references if source == "rag" else []

        if source == "ontology":
            confidence, conf_detail = self._assess_ontology_confidence(
                facts, ontology_instances,
            )
            if confidence == "high":
                return None  # 高置信不跑对侧（保持单路延迟）
            # 中/低置信 → 跑 RAG 对侧（§4.4：整体超时兜底，超时按对侧无结果处理）
            t = time.perf_counter()
            try:
                fallback = await asyncio.wait_for(
                    self._rag_fallback_multi(
                        tenant_id, user_id, req, kb_ids, trace_id,
                        collector=collector, ontology_instances=ontology_instances,
                        fast_mode=fast_mode,
                    ),
                    timeout=ONTOLOGY_CROSS_RAG_TIMEOUT,
                )
                rag_answer = fallback["answer"]
                rag_refs = fallback["references"]
            except asyncio.TimeoutError:
                # 落到下方 "not rag_answer" 分支返回原答案（unverified）
                logger.warning("[ontology] 对侧 RAG 校验超时（%.0fs），保留本体原答案", ONTOLOGY_CROSS_RAG_TIMEOUT)
                rag_answer, rag_refs = "", []
                if collector:
                    collector.step(
                        STAGE_ARBITRATION, "对侧 RAG 校验超时", status="failed",
                        summary=f"超时 {ONTOLOGY_CROSS_RAG_TIMEOUT:.0f}s，保留本体答案（unverified）",
                    )
            if collector and rag_answer:
                collector.step(
                    STAGE_ARBITRATION, "裁决触发（本体中/低置信）",
                    summary=(
                        f"本体置信={confidence}（{conf_detail}），"
                        f"RAG 对侧耗时 {time.perf_counter() - t:.1f}s"
                    ),
                    detail={"confidence": confidence, **conf_detail},
                )
        else:
            # S7 反向：RAG 作答后反查本体结构化槽位；§15.4 触发条件②——
            # RAG 侧命中 chunk 存在低质信号（S8 判据命中但未被完全剔除，
            # 旧链路 chunk 在 LightRAG 内部作答、平台未过滤）
            slot_hit = self._ontology_slot_hit(req.query, ontology_instances)
            lowq_signals: list[str] = []
            for r in (references or []):
                text = (r.get("text") or r.get("content") or "").strip()
                reason = self._low_quality_reason(r, text)
                if reason:
                    lowq_signals.append(f"{reason}:{text[:20]}")
            if not slot_hit and not lowq_signals:
                return None
            if not facts:
                return None
            try:
                onto_answer = await asyncio.wait_for(
                    answer_from_facts(
                        req.query, ontology_instances, facts,
                        tenant_id=tenant_id,
                        kb_id=kb_ids[0] if kb_ids else None,
                        user_id=user_id, trace_id=trace_id,
                        fast_mode=fast_mode,
                    ),
                    timeout=answer_timeout,
                )
            except Exception:
                return None
            if not onto_answer or onto_answer == "INSUFFICIENT":
                return None
            onto_refs = await self._ontology_refs(
                tenant_id=tenant_id, kb_ids=kb_ids,
                ontology_instances=ontology_instances, facts=facts,
                collector=collector,
                query=req.query,   # [jonex] 方案 G1：打通 E3 图片描述 rerank 兜底
            )
            if collector:
                collector.step(
                    STAGE_ARBITRATION, "裁决触发（RAG 侧信号）",
                    summary=(
                        "触发信号："
                        + ("本体结构化槽位命中；" if slot_hit else "")
                        + (
                            f"低质 chunk 信号 {len(lowq_signals)} 个"
                            f"（{lowq_signals[0] if lowq_signals else ''}…）"
                            if lowq_signals else ""
                        ),
                    ),
                    detail={"slot_hit": slot_hit, "lowq_signals": lowq_signals},
                )

        # §15.4 两路一致 → 直接返回原答案，标注 cross_verified=true
        # （免裁决 LLM，保持延迟）；一方无结果 → 返回有结果的一方，
        # 标注未校验（对侧为空进裁决毫无意义）。
        if onto_answer and rag_answer and self._answers_agree(onto_answer, rag_answer):
            if collector:
                collector.step(
                    STAGE_ARBITRATION, "双向校验裁决", status="skipped",
                    summary="两路答案一致，跳过裁决（cross_verified=true）",
                    detail={"cross_verified": True, "original_source": source},
                )
            return None
        if not onto_answer or not rag_answer:
            if collector:
                collector.step(
                    STAGE_ARBITRATION, "双向校验裁决", status="skipped",
                    summary=(
                        "对侧无结果，保留现有答案（unverified）："
                        f"本体={'有' if onto_answer else '无'}"
                        f"，RAG={'有' if rag_answer else '无'}"
                    ),
                )
            return None

        # 裁决（异常/超时 → None 回退原答案）
        try:
            verdict = await asyncio.wait_for(
                arbitrate_answers(
                    req.query,
                    ontology_answer=onto_answer,
                    ontology_evidence=self._facts_evidence(facts),
                    rag_answer=rag_answer,
                    rag_evidence=self._refs_evidence(rag_refs),
                    tenant_id=tenant_id,
                    kb_id=kb_ids[0] if kb_ids else None,
                    user_id=user_id, trace_id=trace_id,
                ),
                timeout=ONTOLOGY_ARBITRATION_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — 超时/解析失败回退原答案
            logger.warning("[ontology] 裁决调用失败，回退原答案: %s", exc)
            return None

        chosen = verdict.get("verdict", "ontology")
        final_answer = verdict.get("answer") or (
            onto_answer if chosen == "ontology" else rag_answer
        )
        final_refs = onto_refs if chosen == "ontology" else rag_refs
        if collector:
            collector.step(
                STAGE_ARBITRATION, "双向校验裁决",
                summary=(
                    f"采纳 {chosen}（confidence={verdict.get('confidence')}）："
                    f"{verdict.get('reason', '')[:80]}"
                ),
                detail={
                    "verdict": chosen,
                    "confidence": verdict.get("confidence"),
                    "reason": verdict.get("reason"),
                    "original_source": source,
                    "ontology_answer": onto_answer[:200],
                    "rag_answer": rag_answer[:200],
                },
            )
        return {
            "answer": final_answer,
            "source": chosen if chosen == "rag" else "ontology",
            "references": final_refs,
        }

    # ── [jonex] OpenKB 分流 — 批量管线查询、search_llmwiki、search_mix ──

    @staticmethod
    def _merge_references(*groups: list[dict]) -> list[dict]:
        """[jonex] 合并多侧引用，按 doc_id 去重保序。

        混合检索里 OntoRAG 与 llm-wiki 可能引用同一份文档（前者 chunk 级命中，
        后者经 wiki 页溯源）。传入顺序即优先级——先到的保留，因此把证据更强的
        一侧放前面（OntoRAG 的 chunk 级引用带 locations，信息比派生引用多）。
        无 doc_id 的条目按原样保留，不参与去重。
        """
        out: list[dict] = []
        seen: set[str] = set()
        for group in groups:
            for ref in group or []:
                doc_id = str((ref or {}).get("doc_id") or "")
                if not doc_id:
                    out.append(ref)
                    continue
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                out.append(ref)
        return out

    async def _build_openkb_references(
        self, tenant_id: str, per_kb_traces: list[tuple[str, list[dict]]],
    ) -> list[dict]:
        """[jonex] 从 agent 的 wiki 浏览轨迹反解出引用（方案 B，答案定稿后执行）。

        per_kb_traces: [(kb_id, turns), ...] 其中 turns 是 _extract_run_trace 产出。

        溯源口径（决策见 docs/openkb/llmwiki-reasoning/03-references-and-images.md
        §1.4，原选 A「概念/实体页不进引用」，现改选 B）：
          · summaries/<uuid>.md、sources/<uuid>.md → stem 即 document_id，直接命中；
          · concepts/<slug>.md、entities/<slug>.md → 该页 frontmatter 的
            sources: ["summaries/<doc_id>.md"] 就是它的源文档，按此反解；
          · index.md 及其它 → 无 sources，自然落空，不需要黑名单。

        为什么改 B：A 把「页面本身不是用户上传的文档」误当成「页面无法定位到文档」。
        B 产出的引用指向的仍是原始文档，符合「references 只放能点开看原文的文档」
        这条口径。且概念性提问（「XX 是什么」）下 agent 往往只读 concepts/，A 会
        导致引用为零条——实测如此。

        成本：每个涉及概念/实体页的 KB 多一次 list_wiki_contents（页面树元数据，
        need_body=False，不含正文），而非 A 方案评估时假设的「每页一次 read_page
        全文读取」。去重发生在溯源之后、富化之前——多个概念页同源于一个文档时，
        只做一次 PG 查询与一次预签名。
        """
        import uuid as _uuid
        from ..dtos.reference import SourceReference

        def _as_doc_id(stem: str) -> str | None:
            try:
                return str(_uuid.UUID(stem))
            except (ValueError, AttributeError, TypeError):
                return None

        # ① 收集轨迹里的 (kb_id, wiki_path)，保序去重
        #    注意循环层级：路径判定必须在 `for call` 内层。此前误缩进到 `for turn`
        #    层级，导致每轮只看最后一个调用（一轮内先读 summaries 再读 entities 会
        #    丢掉前者），且首轮 calls 为空时 path 未绑定直接 NameError。
        seen_paths: set[tuple[str, str]] = set()
        browsed: list[tuple[str, str]] = []  # [(kb_id, wiki_path), ...] 按浏览顺序
        for kb_id, turns in per_kb_traces:
            for turn in (turns or []):
                for call in (turn.get("calls") or []):
                    path = str((call.get("args") or {}).get("path") or "")
                    if not path:
                        continue
                    key = (kb_id, path)
                    if key in seen_paths:
                        continue
                    seen_paths.add(key)
                    browsed.append(key)

        if not browsed:
            return []

        # ② 直接命中：summaries/ 与 sources/ 的 stem 就是 document_id
        seen: set[str] = set()
        ref_sources: list[tuple[str, str, str]] = []  # (doc_id, wiki_path, kb_id)
        derived_paths: list[tuple[str, str]] = []     # 待反解的概念/实体页
        for kb_id, path in browsed:
            matched = False
            for prefix in ("summaries/", "sources/"):
                if path.startswith(prefix):
                    matched = True
                    doc_id = _as_doc_id(path[len(prefix):].removesuffix(".md"))
                    if doc_id and doc_id not in seen:
                        seen.add(doc_id)
                        ref_sources.append((doc_id, path, kb_id))
                    break
            if not matched and path.startswith(("concepts/", "entities/")):
                derived_paths.append((kb_id, path))

        # ③ 间接溯源：概念/实体页 → frontmatter sources → summaries/<doc_id>.md
        #    每个 KB 只拉一次页面树；失败降级为「只保留直接命中」，绝不让富化故障
        #    冒泡成 500（同 §1.3 第 6 条口径）。
        if derived_paths:
            kbs_needing_map = list(dict.fromkeys(kid for kid, _ in derived_paths))
            page_maps: dict[str, dict[str, list]] = {}
            compiler = KnowledgeCompilerService()

            async def _load_map(kid: str) -> tuple[str, dict[str, list]]:
                contents = await compiler.list_wiki_contents(
                    kb_name=kid, tenant_id=tenant_id, kb_id=kid, document_id="",
                )
                # stem → sources；概念与实体分处两个 section，但 stem 在各自 section
                # 内唯一，故按 section 前缀建键，避免同名 slug 互相覆盖。
                out: dict[str, list] = {}
                for section in ("concepts", "entities"):
                    for entry in (contents or {}).get(section) or []:
                        stem = str(entry.get("stem") or "")
                        if stem:
                            out[f"{section}/{stem}"] = entry.get("sources") or []
                return (kid, out)

            map_results = await asyncio.gather(
                *[_load_map(kid) for kid in kbs_needing_map], return_exceptions=True,
            )
            for item in map_results:
                if isinstance(item, Exception):
                    logger.warning("[openkb] 页面树拉取失败，概念页溯源降级: %s", item)
                    continue
                kid, mapping = item
                page_maps[kid] = mapping

            for kb_id, path in derived_paths:
                mapping = page_maps.get(kb_id)
                if not mapping:
                    continue
                for src in mapping.get(path.removesuffix(".md")) or []:
                    src = str(src)
                    if not src.startswith("summaries/"):
                        continue
                    doc_id = _as_doc_id(src[len("summaries/"):].removesuffix(".md"))
                    if not doc_id or doc_id in seen:
                        continue
                    seen.add(doc_id)
                    # wiki_path 记「实际浏览的那一页」而非反解出的 summaries 页——
                    # 这样前端能区分「直接读了摘要」与「经概念页溯源」，无需新增字段。
                    ref_sources.append((doc_id, path, kb_id))

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
        # [jonex] 批量查询 KB 名称
        kb_ids_in_refs = list(dict.fromkeys(kid for _, _, kid in ref_sources if kid))
        kb_names = await self._get_kb_names(tenant_id, kb_ids_in_refs) if kb_ids_in_refs else {}

        storage = get_object_storage()
        out: list[dict] = []
        for doc_id, wiki_path, kb_id in ref_sources:
            d = doc_map.get(doc_id)
            if d is None:
                continue
            raw_url: str | None = None
            try:
                raw_url = await storage.presigned_url(
                    d.storage_key or build_object_key(kb_id, d.id, d.file_name or ""),
                    tenant_id,
                )
            except Exception:
                raw_url = None
            ref = SourceReference(
                doc_id=doc_id,
                kb_id=kb_id,
                kb_name=kb_names.get(kb_id) if kb_id else None,
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

    async def _get_kb_names(self, tenant_id: str, kb_ids: list[str]) -> dict[str, str]:
        """[jonex] 批量查询 KB 显示名 {kb_id: name}——融合 prompt 用产品名标识
        知识库（否则 LLM 会把 kb_id UUID / source 值照抄进答案，如
        「知识库 `llm-wiki`」）。查询失败返回空映射（降级 kb_id 标识）。"""
        if not kb_ids:
            return {}
        try:
            from sqlalchemy import select as _select
            from ..models.knowledge_info import KnowledgeInfo

            async with get_db_session() as session:
                rows = (
                    await session.execute(
                        _select(KnowledgeInfo.id, KnowledgeInfo.name).where(
                            KnowledgeInfo.tenant_id == tenant_id,
                            KnowledgeInfo.id.in_(kb_ids),
                            KnowledgeInfo.is_deleted == 0,
                        )
                    )
                ).all()
            return {r.id: (r.name or r.id) for r in rows}
        except Exception:
            logger.warning("KB 名批量查询失败 kb_ids=%s", kb_ids, exc_info=True)
            return {}

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
            # [jonex] 融合 prompt 用 KB 显示名（LLM 照抄标识进答案）
            _fkb_names = await self._get_kb_names(
                tenant_id, [p["kb_id"] for p in per_kb_fused])
            for p in per_kb_fused:
                p["kb_name"] = _fkb_names.get(p["kb_id"], p["kb_id"])
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
            history = await self._history.save_history(
                tenant_id, user_id,
                SearchHistoryCreateRequest(
                    query=req.query,
                    knowledge_base_id="" if len(kb_ids) > 1 else kb_ids[0],
                    mode=req.mode,
                    top_k=req.top_k,
                    domain_space_id=req.domain_space_id,
                    answer_preview=answer[:300],
                    answer=answer,
                    references=_references,
                    reasoning=collector.build(source),
                    duration_ms=total_ms,
                    metadata={
                        "knowledge_base_ids": kb_ids,
                        "source": source,
                        "pipeline": "llm-wiki",
                    },
                ),
            )
            result["history_id"] = history.get("id")

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
            summary=f"OntoRAG={len(lightrag_ids)} KB, llm-wiki={len(openkb_ids)} KB",
            detail={
                "pipeline_groups": {"OntoRAG": lightrag_ids, "llm-wiki": openkb_ids},
                "OntoRAG_count": len(lightrag_ids),
                "llm-wiki_count": len(openkb_ids),
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
            # [jonex] 展示名：推理链 title 前缀用产品名（OntoRAG / llm-wiki），
            # 不用内部管线标识（此前 [lightrag] 字样出现在推理链 UI）
            for _side, _res in (("OntoRAG", lr_result), ("llm-wiki", okb_result)):
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
            # [jonex] 融合 prompt 用 KB 显示名标识来源（LLM 会照抄标识进答案；
            # 用 kb_id UUID / source 值会产出「知识库 `llm-wiki`」这类错误标注）。
            # 标签只列「该侧 references 实际命中的 KB」——请求中的无内容/无检索
            # 命中的知识库不应被关联为来源（无关联 KB 标注问题）。某侧引用为空
            # 时标签置空，由 fuse_rag_answers 落为 unspecified（答案中不标注）。
            def _ref_kb_ids(refs) -> list[str]:
                return list(dict.fromkeys(r.get("kb_id") for r in (refs or []) if r.get("kb_id")))

            _lr_ref_kb_ids = _ref_kb_ids(lr_result.get("references") or [])
            _okb_ref_kb_ids = _ref_kb_ids(okb_result.get("references") or [])
            _kb_names = await self._get_kb_names(tenant_id, _lr_ref_kb_ids + _okb_ref_kb_ids)
            _lr_label = "、".join(_kb_names.get(k, k) for k in _lr_ref_kb_ids)
            _okb_label = "、".join(_kb_names.get(k, k) for k in _okb_ref_kb_ids)
            per_kb = [
                {
                    "kb_id": lightrag_ids[0],
                    "kb_name": _lr_label,
                    "answer": lr_answer,
                    "source": lr_result.get("source", "rag"),
                },
                {
                    "kb_id": openkb_ids[0],
                    "kb_name": _okb_label,
                    "answer": okb_answer,
                    "source": "llm-wiki",
                },
            ]
            t_fuse = time.perf_counter()
            answer = await fuse_rag_answers(
                req.query, per_kb,
                tenant_id=tenant_id, user_id=user_id, trace_id=trace_id,
            )
            fusion_ms = int((time.perf_counter() - t_fuse) * 1000)
            collector.step(STAGE_FUSION, "多答案融合",
                           summary=f"融合 OntoRAG + llm-wiki 两侧答案", t_start=t_fuse)
            source = "mixed"
            # [jonex] 两侧引用都要带上。此前只取 lr_result，llm-wiki 侧经
            # _build_openkb_references 溯源出的文档被静默丢弃。按 doc_id 去重，
            # OntoRAG 侧在前（chunk 级证据比 wiki 页派生证据强）。
            references = self._merge_references(
                lr_result.get("references") or [],
                okb_result.get("references") or [],
            )
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
                summary="仅 OntoRAG 侧有效，llm-wiki 侧失败/无答案",
                detail={"partial_failed": True, "failed_pipeline": "llm-wiki",
                        "error": str(okb_result) if not okb_ok else "no_answer"},
            )
        elif okb_effective:
            # 仅 openkb 成功
            answer = okb_answer
            source = okb_result.get("source", "llm-wiki")
            # [jonex] 带上 llm-wiki 侧的引用。此前硬编码为空 + available=False，
            # 与隔壁 lr_effective 分支（取 lr_result["references"]）不对称，
            # 导致「OntoRAG 无答案 + wiki 有答案」时引用必然为 0 条——
            # 而这正是概念性提问的常见组合。
            references = okb_result.get("references") or []
            ontology_instances = []
            rag_used = okb_result.get("rag_used", False)
            references_available = bool(references)
            ontology_instances_available = False
            collector.step(
                STAGE_FUSION, "多答案融合", status="skipped",
                summary="仅 llm-wiki 侧有效，OntoRAG 侧失败/无答案",
                detail={"partial_failed": True, "failed_pipeline": "OntoRAG",
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
            history = await self._history.save_history(
                tenant_id, user_id,
                SearchHistoryCreateRequest(
                    query=req.query,
                    knowledge_base_id="",  # 混合/多 KB
                    mode=req.mode,
                    top_k=req.top_k,
                    domain_space_id=req.domain_space_id,
                    answer_preview=answer[:300],
                    answer=answer,
                    references=references,
                    reasoning=collector.build(source),
                    duration_ms=total_ms,
                    metadata={
                        "knowledge_base_ids": raw_ids,
                        "pipeline_groups": {"OntoRAG": lightrag_ids, "llm-wiki": openkb_ids},
                        "source": source,
                    },
                ),
            )
            result["history_id"] = history.get("id")

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
