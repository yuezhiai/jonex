"""Knowledge Base search DTOs."""

from typing import Any, Optional

try:
    from pydantic.v1 import BaseModel, Field, validator
except ImportError:
    from pydantic import BaseModel, Field, validator

_VALID_MODES = frozenset({"naive", "local", "global", "hybrid", "mix"})
# mix → hybrid 别名兼容（前端历史遗留，per no-frontend-change 规则在 backend 侧兜底）
_MODE_ALIASES = {"mix": "hybrid"}


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    mode: str = Field(default="hybrid", min_length=1, max_length=32)

    @validator("mode")
    def validate_mode(cls, v):
        v = v.lower()
        v = _MODE_ALIASES.get(v, v)
        if v not in _VALID_MODES:
            raise ValueError(f"mode 必须为 {sorted(_VALID_MODES)} 之一，当前值: {v}")
        return v
    top_k: int = Field(default=15, ge=1, le=50)
    knowledge_base_id: str = Field(default="", min_length=0, max_length=128)
    save_history: bool = True
    domain_space_id: Optional[str] = Field(default=None, max_length=64)


class SearchResponse(BaseModel):
    query: str
    answer: str
    mode: str
    top_k: int
    references: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EnhancedSearchResponse(SearchResponse):
    entities: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    graph: dict[str, Any] = Field(default_factory=dict)


class OntologySearchRequest(BaseModel):
    """本体优先检索请求（多 KB）"""
    query: str = Field(..., min_length=1, max_length=2000)
    mode: str = Field(default="hybrid", min_length=1, max_length=32)

    @validator("mode")
    def validate_mode(cls, v):
        v = v.lower()
        v = _MODE_ALIASES.get(v, v)
        if v not in _VALID_MODES:
            raise ValueError(f"mode 必须为 {sorted(_VALID_MODES)} 之一，当前值: {v}")
        return v
    top_k: int = Field(default=15, ge=1, le=50)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    save_history: bool = True
    with_reasoning: bool = False          # 是否返回编排推理链（P0 非流式）
    domain_space_id: Optional[str] = Field(default=None, max_length=64)
    # ── [jonex] §9 严格模式字段（全部可选，默认关，向后兼容）──
    strict_mode: bool = False             # 开启严格模式
    strict_max_attempts: int = Field(3, ge=1, le=3)   # 循环上限
    strict_min_score: float = Field(0.8, ge=0.0, le=1.0)  # 校验通过阈值
    strict_require_reference: bool = True  # 要求答案有支撑引用
    strict_require_grounded: bool = True   # 要求关键论断可溯源


class ReliabilityInfo(BaseModel):
    """严格模式可靠性信息（[jonex] §9.2）"""
    verdict: str = "best_effort"           # "verified" | "best_effort"
    score: float = 0.0                     # 可靠性分 0.0~1.0
    attempts: int = 1                      # 实际尝试次数
    passed: bool = False                   # 是否达到 strict_min_score
    checks: dict = Field(default_factory=dict)  # 逐项校验结果
    statement: str = ""                    # 面向用户的中文可靠性说明
    unmet: list[str] = Field(default_factory=list)  # 未满足的校验项


class OntologySearchResponse(BaseModel):
    """本体优先检索响应（多 KB）"""
    answer: str
    source: str
    references: list[dict[str, Any]] = Field(default_factory=list)
    ontology_instances: list[dict[str, Any]] = Field(default_factory=list)
    rag_used: bool
    knowledge_base_ids: list[str] = Field(default_factory=list)
    reasoning: Optional[dict[str, Any]] = None    # 编排推理链（默认 None，仅契约对齐）
    # [jonex] openkb 检索标记：references/ontology_instances 是否可用（D6）
    references_available: bool = True
    ontology_instances_available: bool = True


# ── [jonex] openkb 分流 — 新增 DTO ──

class LlmWikiSearchRequest(BaseModel):
    """OpenKB Wiki 检索请求（只查 openkb 管线）"""
    query: str = Field(..., min_length=1, max_length=2000)
    mode: str = Field(default="hybrid", min_length=1, max_length=32)

    @validator("mode")
    def validate_mode(cls, v):
        v = v.lower()
        v = _MODE_ALIASES.get(v, v)
        if v not in _VALID_MODES:
            raise ValueError(f"mode 必须为 {sorted(_VALID_MODES)} 之一，当前值: {v}")
        return v
    top_k: int = Field(default=5, ge=1, le=50)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    save_history: bool = True
    with_reasoning: bool = False
    domain_space_id: Optional[str] = Field(default=None, max_length=64)


class MixSearchRequest(BaseModel):
    """混合管线检索请求（统一入口，可接受任意 pipeline_type 组合）

    注意：此处的 "Mix" 指「混合管线」（lightrag + openkb），
    与 mode 字段的 mix 值（hybrid 的别名，`_MODE_ALIASES`）无关。
    """
    query: str = Field(..., min_length=1, max_length=2000)
    mode: str = Field(default="hybrid", min_length=1, max_length=32)

    @validator("mode")
    def validate_mode(cls, v):
        v = v.lower()
        v = _MODE_ALIASES.get(v, v)
        if v not in _VALID_MODES:
            raise ValueError(f"mode 必须为 {sorted(_VALID_MODES)} 之一，当前值: {v}")
        return v
    top_k: int = Field(default=5, ge=1, le=50)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    save_history: bool = True
    with_reasoning: bool = False
    domain_space_id: Optional[str] = Field(default=None, max_length=64)
    # ── [jonex] 严格模式字段（与 OntologySearchRequest 对齐，透传给 lightrag 侧）──
    strict_mode: bool = False
    strict_max_attempts: int = Field(3, ge=1, le=3)
    strict_min_score: float = Field(0.8, ge=0.0, le=1.0)
    strict_require_reference: bool = True
    strict_require_grounded: bool = True
    # ── [jonex] §9 严格模式可靠性字段（输出侧）──
    reliability: Optional[dict[str, Any]] = None  # ReliabilityInfo 序列化


class DeepSearchRequest(OntologySearchRequest):
    """深度查询请求（[jonex] §10），继承 OntologySearchRequest + strict 字段。

    新增：
    - max_subqueries: 子查询数上限覆盖
    - allow_common_sense: 是否在汇总阶段放宽常识边界
    """
    max_subqueries: int = Field(6, ge=1, le=10)
    allow_common_sense: bool = True


class DeepSearchResponse(BaseModel):
    """深度查询响应（[jonex] §10）"""
    answer: str
    source: str = "deep"
    references: list[dict[str, Any]] = Field(default_factory=list)
    ontology_instances: list[dict[str, Any]] = Field(default_factory=list)
    rag_used: bool = False
    knowledge_base_ids: list[str] = Field(default_factory=list)
    reasoning: Optional[dict[str, Any]] = None
    reliability: Optional[dict[str, Any]] = None
    plan_summary: Optional[dict[str, Any]] = None   # 子查询规划摘要
    facts_summary: list[dict[str, Any]] = Field(default_factory=list)  # 取证要素摘要
