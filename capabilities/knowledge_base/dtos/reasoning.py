"""编排推理链 DTO（本体优先检索）"""
from typing import Any, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field

# ── 阶段标识 ──
STAGE_ONTOLOGY_MATCH = "ontology_match"
STAGE_ROUTE_DECISION = "route_decision"
STAGE_FACT_LOOKUP = "fact_lookup"
STAGE_LLM_ANSWER = "llm_answer"
STAGE_RAG_FALLBACK = "rag_fallback"
STAGE_FUSION = "fusion"
STAGE_ARBITRATION = "arbitration"              # [jonex] S1+S7 双向校验裁决（分歧必须可见）
STAGE_RETRIEVAL_RERANK = "retrieval_rerank"   # LightRAG 检索期重排（召回后、送 LLM 前）
STAGE_REF_RETRIEVE = "ref_retrieve"            # 本体成功后取 chunk 引用（RAG 检索，不含生成）
STAGE_RERANK = "rerank"                        # 平台引用期重排（LLM 答完后，多 KB fallback 引用）
STAGE_OPENKB_QUERY = "openkb_query"            # [jonex] OpenKB Wiki 检索
STAGE_TIMELINE_GRAPH = "timeline_graph"       # 图查询模板（时间线/枚举/计数意图走结构化图查询）
STAGE_STRICT_ATTEMPT = "strict_attempt"        # 严格模式·单次尝试
STAGE_STRICT_VERIFY = "strict_verify"           # 严格模式·最终校验
STAGE_INTENT_CLASSIFY = "intent_classify"       # 深度查询·意图分类
STAGE_QUERY_PLAN = "query_plan"                 # 深度查询·查询分解规划
STAGE_SUBQUERY = "subquery"                     # 深度查询·子查询取证
STAGE_SYNTHESIS = "synthesis"                   # 深度查询·汇总与计算
# [jonex] 方案 A：平台取回作答权（rag-subject-filter 方案 §6.7）
STAGE_CONTEXT_RETRIEVE = "context_retrieve"     # 多 KB 只召回不生成（替代 rag_fallback 在新路径下的语义）
STAGE_CHUNK_ANSWER = "chunk_answer"             # 平台侧基于 chunk 一次作答

# ── 状态标识 ──
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


class ReasoningStep(BaseModel):
    """推理链的单个步骤"""
    stage: str
    title: str                                            # 前端展示的中文标题
    status: str = STATUS_DONE
    summary: Optional[str] = None
    detail: Optional[dict[str, Any]] = None               # 结构化明细（已脱敏）
    duration_ms: Optional[int] = None


class ReasoningTrace(BaseModel):
    """完整的推理链轨迹"""
    steps: list[ReasoningStep] = Field(default_factory=list)
    final_source: str = "rag"                             # ontology | rag | llm-wiki | mixed | none
    total_ms: Optional[int] = None
