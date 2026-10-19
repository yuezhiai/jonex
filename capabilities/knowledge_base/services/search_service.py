"""Search service for Knowledge Base."""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from typing import Any, Awaitable, Callable

from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common.model_config import resolve_llm_model
from .openkb_service import KnowledgeCompilerService  # [jonex]
from .jonex_wiki_service import JonexWikiService  # [jonex]
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
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
    anchor_answer_to_references,  # [jonex] openkb 无标记答案 → [n] 引用锚定
    answer_from_chunks,   # [jonex] 方案 A：平台侧基于 chunk 作答
    answer_from_facts,
    arbitrate_answers,    # [jonex] S1+S7 双向校验裁决
    fuse_rag_answers,
    normalize_citations,          # [jonex] 引用编号归一化（[ref_i] → [n]）
    normalize_evidence_handles,   # [jonex] jonex_wiki 证据句柄归一化（[E#] → [n]）
    strip_trailing_references,    # [jonex] 剥掉 LightRAG 答案末尾 References 尾注段
)
from jonex_core.common.tenant import require_tenant

from ..dtos import JonexWikiSearchRequest, LlmWikiSearchRequest, MixSearchRequest, DeepSearchRequest, DeepSearchResponse, OntologySearchRequest, ReliabilityInfo, SearchHistoryCreateRequest, SearchRequest
from ..dtos.reasoning import (
    STAGE_ARBITRATION,       # [jonex] S1+S7 双向校验裁决
    STAGE_CHUNK_ANSWER,      # [jonex] 方案 A：平台侧基于 chunk 作答
    STAGE_CONTEXT_RETRIEVE,  # [jonex] 方案 A：多 KB 只召回不生成
    STAGE_FACT_LOOKUP,
    STAGE_FUSION,
    STAGE_INTENT_CLASSIFY,
    STAGE_JONEX_WIKI_QUERY,  # [jonex] jonex_wiki 引擎检索链路
    STAGE_LLM_ANSWER,
    STAGE_ONTOLOGY_MATCH,
    STAGE_OPENKB_QUERY,
    STAGE_QUERY_PLAN,
    STAGE_REF_RETRIEVE,
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
from .kb_type_service import LLM_WIKI_TYPES  # [jonex] llm-wiki 子集 {openkb, jonex_wiki}
from .ontology_compiler import OntologyCompiler   # [jonex] P8（M3-a）：inverse 映射表
from .reasoning_trace import ReasoningCollector
from .search_event_writer import write_trace_events   # [jonex] 数据运营二期：检索轨迹埋点
from .search_history_service import SearchHistoryService

logger = logging.getLogger(__name__)

ONTOLOGY_ROUTE_SCORE_MIN = float(os.getenv("ONTOLOGY_ROUTE_SCORE_MIN", "1.0"))
ONTOLOGY_VECTOR_SCORE_MIN = float(os.getenv("ONTOLOGY_VECTOR_SCORE_MIN", "0.75"))
# [jonex] S6（§4.3）：L1 路由判据统一量纲——把候选实体的 description+attributes
# 当 document、query 当 query 送 rerank，得 0~1 分，替代 `ft_score >= 1.0` 这个
# 无量纲阈值（BM25 分本身无上界，两套分不可比）。L1 与 L2 共用同一把尺。
#
# ⚠️ 默认 false（灰度）：开启后**每条查询在路由路径上多一次 rerank 调用**，
# 且判据随之改变。`ONTOLOGY_ROUTE_RERANK_MIN` 的值须按方案 §4.5 同法实测标定
# （≥30 条标注样本 + 可分性判定），当前 0.5 是**未标定的占位默认**。
ONTOLOGY_ROUTE_RERANK_ENABLED = os.getenv(
    "ONTOLOGY_ROUTE_RERANK_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
ONTOLOGY_ROUTE_RERANK_MIN = float(os.getenv("ONTOLOGY_ROUTE_RERANK_MIN", "0.5"))
# 送 rerank 的候选文本上限（description + attributes 拼接后截断），防超长拖慢路由。
_ROUTE_DOC_MAX = int(os.getenv("ONTOLOGY_ROUTE_DOC_MAX", "800"))
# [jonex] S6（§4.3）：`extraction_method` 硬门开关。开启后 stub 实体不进 L1 候选
# （纵深防御——四级匹配的 Cypher 已带 `coalesce(stub,false)=false` 过滤）。
#
# ⚠️ [jonex] 2026-09-18 修复：本常量在 B2 修复（先过门再取前 5）时只写了使用点
# `:5206`、**漏了定义**，于是 `ontology_instances` 非空——即**恰好是本体命中路径**
# ——必然 `NameError` 整条查询崩。单测只覆盖 `_route_method_gate()` 函数本身、
# 不碰该开关，故 14 个用例全绿而真实路径 100% 失败。此处补上定义。
#
# ✅ 默认 **true**（2026-09-18 实测后定）：本门在真实数据上**几乎是空操作**——
# 本地 14,366 个本体实体里，Cypher 放行（`stub` 属性为 false）但本门会剔的
# **只有 1 条**（`extraction_method='stub'` 而 `stub` 属性为 false 的错标实体）。
# 即：`coalesce(e.stub,false)=false` 已经把该做的事做完了，本门是**没有代价的
# 纵深防御**。原先默认 false 是没有依据的反射性保守。
#
# 回滚：置 false → 回到「Cypher 单层过滤」。
#
# ⚠️ 另一半硬门（`_route_only_backfill`，:5349）**不在本开关内、无条件生效**——
# 同一个 §4.3 硬门被拆成两半、只有一半可回滚。是否收进本开关待定（见方案
# §4.3 缺口记录）。
ONTOLOGY_ROUTE_METHOD_GATE_ENABLED = os.getenv(
    "ONTOLOGY_ROUTE_METHOD_GATE_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")

# [jonex] L0 统一候选池（§3.3 #2 的欠账 / §4.2 的 L0）：把**本体候选**并入
# RAG 侧候选池，两路共用一次 rerank + 一次统一分。
#
# 为什么需要：现在两条路径的池是**互斥**的——本体路径用 `_ontology_refs` 的池
# （有 `_evidence_score`），降级路径用 `_rag_platform_answer` 的池（LightRAG 召回，
# **无该字段**）。实测降级池的 `_evidence_score` 有值率 **0/16** → `_unified_score`
# 按「缺项权重重分配」剔除 `w_e` 项 → `P2 ≡ P1`。合并后 `w_e` 才有作用面。
#
# ⚠️ 默认 false（灰度）。开着才合并；关掉即回到「两路互斥」的现状，零残留。
RAG_L0_UNIFIED_POOL_ENABLED = os.getenv(
    "RAG_L0_UNIFIED_POOL_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")


def _route_doc_of(hit: dict) -> str:
    """[jonex] S6：候选实体 → 送 rerank 的 document（§4.3）。

    ⚠️ [jonex] B（2026-09-23）：**实体名放首行**。

    旧口径只有 `description` + `attributes`，而描述里**常不含主体名**——线上
    「李冬芸」的 description 是「绵阳办事处主任，联系电话17761259793。」，query
    里全是"李冬芸"、doc 里一个都没有，rerank 无从判断"这段描述说的是不是他"，
    只得 **0.2327**（< 阈值）→ 判不过线 → 降级 RAG；**前置实体名后 0.9885**。

    这正是原 docstring 那句自述的成因——它把「只有名字的粗实体拿不到分」归因于
    「空 document 无意义」，于是选择**不送 rerank**（退回旧判据）；真因是**名字
    没进 doc**，正解是送进去。

    实测（本地栈 4 query × 5 真实候选，`scripts/_archive` 外的一次性实验）：

    | 候选性质 | 例（old → name） |
    |---|---|
    | 名字与 query 有重叠 → **大涨** | `设备` 0.0298 → 0.3972；`油温` 0.4055 → 0.6783 |
    | 名字与 query 无重叠 → 小幅**降** | `电力变压器日常维护与巡检` 0.8905 → 0.8147 |
    | 噪声 | `设备负载(%)` 0.0033 → 0.0055 |

    → **非单调提升**（给 doc 加无关文本会稀释相关性），但降幅 ~0.08 且全落在 0.8+
    高分区，不改变过线判定。⚠️ 因此**阈值必须重标**：判据已从「描述与 query 相关」
    变成「名字+描述与 query 相关」。

    ⚠️ **别名（`aliases`）不纳入**：实测 20 个数据点升 8 / 降 10 / 平 2（`name`
    相对 `name+alias`），`避雷器*` 一组已在天花板附近反被拉低（0.999 → 0.988）。
    要加须先重做分离度实验，别顺手带上。

    三者（name / description / attributes）皆空时仍返回空串——**空串不送 rerank**，
    调用方据此走「无可判文本」的兜底分支（加名字后该分支近乎不可达，留作防御）。
    """
    parts: list[str] = []
    # 实体名在前：它是「这个候选是什么」的第一身份，也是 query 主体与 doc 之间
    # **必然共享**的那个语素（用户问的就是它）。两路召回都返 `name`，
    # `canonical_name` 是入库侧/测试口径的兜底。
    name = str(hit.get("name") or hit.get("canonical_name") or "").strip()
    if name:
        parts.append(name)
    desc = str(hit.get("description") or "").strip()
    if desc:
        parts.append(desc)
    attrs = hit.get("attributes")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except (json.JSONDecodeError, TypeError):
            attrs = None
    if isinstance(attrs, dict):
        for k, v in attrs.items():
            sv = str(v or "").strip()
            if sv:
                parts.append(f"{k}: {sv}")
    return "\n".join(parts)[:_ROUTE_DOC_MAX]


def _route_method_gate(hit: dict) -> bool:
    """[jonex] S6：`extraction_method` 硬门（§4.3）。True = 可进候选。

    | method | 处置 |
    |---|---|
    | `stub` | **不进候选**——`confidence=0.1`、只有名字的空壳。四级匹配的 Cypher 已带 `coalesce(stub,false)=false` 过滤，此处是**纵深防御** |
    | `endpoint_backfill` | **放行**——「不得单独触发路由」由调用方按 `matched_list` 是否全为 backfill 判定（见 `_route_only_backfill`），不是逐条排除 |
    | `manual` | **放行**——`_is_stub`（`:1536`）对人工录入实体特判豁免，本门须与之对齐，否则人工实体会被两套判据打成矛盾 |
    | `row_as_object` | **放行**——枚举域门另用 §3.4 的真行对象判据（`table_sig` 非空），不能用 `extraction_method` 本身 |
    | 其余 | 放行 |
    """
    return str(hit.get("extraction_method") or "").strip() != "stub"


def _route_only_backfill(matched: list[dict]) -> bool:
    """[jonex] S6：命中集是否**全部**由 `endpoint_backfill` 构成。

    `endpoint_backfill` 是「被当关系端点补出来的」实体（`confidence=0.3`、
    无描述无属性），它单独命中不足以支撑走本体路径；但作为多实体命中里的
    辅助证据是合理的。故判据是「全为 backfill」而非「含 backfill」。
    """
    if not matched:
        return False
    for h in matched:
        if str(h.get("extraction_method") or "").strip() != "endpoint_backfill":
            return False
        # ⚠️ [jonex] B3 修复：**有内容的不算「不足以支撑路由」**。
        # `extraction_method` 是「首写者胜」——实体可能先被 endpoint_backfill
        # 建出、后被 native_llm 补全内容而标签仍是旧的。S2 的 `_method_weight`
        # 为这种过期标签专门设了 `_METHOD_WEIGHT_CONTENT_FLOOR = 0.8` 缓解，
        # 路由侧若无对应逻辑，两套判据会打架（正是 §4.3 为 `manual` 特意对齐
        # `_is_stub` 时要避免的情形）。
        if _has_entity_content(h):
            return False
    return True


def _route_trace(
    top_n: list[dict],
    matched: "dict | None",
    *,
    rerank_enabled: bool,
    rerank_failed: bool,
    rr_scores: dict[int, float],
    route_min: float,
    rerank_min: float,
) -> "tuple[str, dict]":
    """[jonex] C：路由决策的**展示唯一出处**——返回 (原因串, collector detail)。

    2026-09-23 线上实锤：`李冬芸是哪个机构下的，什么岗位` 本体命中 4 个实体、其中
    `李冬芸` 的 ft_score=14.6462，面板却写「全文分 **0.00** / 阈值 1.00」「向量分
    **0.00** / 阈值 0.75」。两个 0.00 是 `matched=None` 的**占位值**（与检索结果
    无关），且这两个阈值在 rerank 生效时**根本不参与判定**——排查据此先怀疑"没检到"，
    最后只能绕到线上手工复现 rerank 打分，才定位到真因（doc 缺实体名 → 0.2327 < 0.5）。

    三条口径，分别对应当时的三个坑：

    | 字段 | 口径 | 坑 |
    |---|---|---|
    | `vscore` / `ft_score` | **top_n 的真实 max**（无命中时也报真值） | 不再拿 `matched=None` 填 0.00 |
    | `threshold_source` | 生效判据（`rerank` / `legacy`），**只出该套阈值** | 旧阈值不参与判定却照展示 |
    | 原因串 | 不过线时报 `max_rerank=<分> < <阈值>` | 「无命中过线」四个字零信息量 |

    开关 OFF（默认）时原因串与旧文案**逐字相同**，灰度期不产生 diff。
    """
    max_vscore = max((float(h.get("vscore") or 0) for h in top_n), default=0.0)
    max_ftscore = max(
        (float(h.get("ft_score", h.get("score", 0)) or 0) for h in top_n), default=0.0,
    )

    # ⚠️ 判据自述用 **matched 的** source（旁路判定），展示用 top_n[0] 兜底
    # （无命中时也要能看出「检没检到、走的哪路召回」）。
    matched_source = matched.get("source", "") if matched else ""
    display_source = matched_source or (top_n[0].get("source", "") if top_n else "")

    # 旧判据文案——**保持原样**（仅把 `_route_min` 换成入参）。
    m_vscore = matched.get("vscore", 0) if matched else 0.0
    m_ftscore = matched.get("ft_score", matched.get("score", 0)) if matched else 0.0
    legacy_reason = (
        f"source={matched_source}" if matched_source in ("exact", "prefix")
        else f"vscore={m_vscore} ≥ {ONTOLOGY_VECTOR_SCORE_MIN}" if m_vscore >= ONTOLOGY_VECTOR_SCORE_MIN
        else f"ft_score={m_ftscore} ≥ {route_min}" if m_ftscore >= route_min
        else f"分数均不足（top-{len(top_n)} max_vscore={max_vscore:.2f} max_ftscore={max_ftscore:.2f}）"
    )

    detail = {
        "source": display_source,
        # 真实 max：`matched=None` 只表示「没有候选过线」，**不表示检索为空**。
        "vscore": max_vscore,
        "ft_score": max_ftscore,
        "route": "ontology" if matched else "rag",
    }

    if rerank_enabled and not rerank_failed:
        rr_max = max(rr_scores.values()) if rr_scores else None
        if matched:
            reason = f"[rerank 生效] {matched.get('_route_why')}"
        elif rr_max is None:
            # 未被判失败却一条分都没有——不编造数字。
            reason = f"[rerank 生效] 无命中过线（top-{len(top_n)} 无候选分）"
        else:
            reason = (f"[rerank 生效] 无命中过线"
                      f"（top-{len(top_n)} max_rerank={rr_max:.4f} < {rerank_min}）")
        detail.update({
            "threshold_source": "rerank",
            "rerank_score": rr_max,
            "rerank_threshold": rerank_min,
            "rerank_failed": False,
        })
        return reason, detail

    if rerank_enabled:
        # 失败**必须标出来**——否则这份读数与「开关压根没开」在界面上无法区分。
        detail["rerank_failed"] = True
        reason = f"[rerank 失败→退回旧判据] {legacy_reason}"
    else:
        reason = legacy_reason

    detail.update({
        "threshold_source": "legacy",
        "vscore_threshold": ONTOLOGY_VECTOR_SCORE_MIN,
        "ftscore_threshold": route_min,
    })
    return reason, detail


def _hit_trace_of(hit: dict) -> dict:
    """[jonex] 命中实体 → 展示用 detail（`STAGE_ONTOLOGY_MATCH` 的 hits 项）。

    2026-09-23 线上实例：面板列出 5 个候选，其中 2 个显示 **score 0.00**，被读成
    「召回了但打分是 0」。真相是**字段缺失**——两条召回路径返回的键不同：

    | 召回路径 | 返回 | 融合产物 |
    |---|---|---|
    | 全文（BM25） | `score` | `ft_score=score`、`vscore=0.0` |
    | 向量（余弦） | **`vscore`**（`score AS vscore`） | `ft_score=0.0`、`vscore`，**无 `score`** |

    实测那批候选：李冬芸 `ft_score=14.657 / vscore=0`（只有全文）、
    「智能知识问答助手」`ft_score=0 / vscore=0.796`（只有向量）——**常常只有一个非零**。
    故这里**双分并列**交出，由前端按哪个有值显示哪个并标明种类。

    ⚠️ 刻意**不传 `score`**：它只有全文分支有，纯向量命中必缺 → 前端
    `Number(null) === 0` 会把它渲染成 `"0.00"`（与 `_route_trace` 修的是同一类假 0）。
    """
    return {
        "name": hit.get("name"),
        "ft_score": hit.get("ft_score"),
        "vscore": hit.get("vscore"),
        "kb_id": hit.get("kb_id"),
        "type": hit.get("type"),
        "description": hit.get("description"),
        "attributes": hit.get("attributes"),
        "confidence": hit.get("confidence"),
        "doc_ids": hit.get("doc_ids"),
    }


def _attach_rerank_to_hits(
    hits_detail: list[dict], hits_src: list[dict],
    top_n: list[dict], rr_scores: dict[int, float],
) -> None:
    """把 rerank **判据分**按**对象身份**回填进展示用 hits（原地改 `hits_detail`）。

    rerank 发生在匹配**之后**，而展示项在匹配时就记录了——不回填，面板上就只有
    检索分（rerank 生效时它并不参与判定），看不到真正决定路由的那个分。

    ⚠️ **按 `id()` 而非按下标，也不能按 `name`**（2026-09-23 评审 F1）：

      · 按下标不行——展示的是 `ontology_instances[:5]`（**未过 method gate**），算
        rerank 的却是 `_gated[:5]`：前 5 里若有 stub 被剔，两者下标即错位；
      · 按名也不行——**同一 KB 内跨 `entity_type` 重名是常态**（唯一约束含
        `entity_type`：同 KB 同 type 唯一、跨 type 可重名；实测 `013fd0d4` 里
        「表格明细」「其它」「粉面」「粥面」「咸菜」等**各 2 条**）。dict 构造后写覆盖
        先写 → 同名另一条的分盖上来。这是纯展示层，可它存在的全部意义就是让路由判据
        可见 —— **报错的分比不报更糟**（上一轮排查正是被 detail 里的假 0.00 带偏的）。

    `_gated` 的元素是 `ontology_instances` 里**同一批 dict 的引用**，故 `id()` 稳定可
    对齐；而 `_hit_trace_of(i)` 新建 dict、不持有原引用，所以要并行传一份 `hits_src`
    保存源引用。

    Args:
        hits_detail: 展示项（`_hit_trace_of` 产出），与 `hits_src` **同序同长**。
        hits_src: 与 `hits_detail` 一一对应的源实体引用（`zip` 依赖**同序同长**）。
        top_n: 参与 rerank 的候选（`_gated[:5]`）。
        rr_scores: `top_n` 下标 → rerank 分。
    """
    by_id = {id(top_n[i]): rr_scores.get(i) for i in range(len(top_n))}
    for hd, src in zip(hits_detail, hits_src):
        v = by_id.get(id(src))
        if v is not None:
            hd["rerank_score"] = v


def _append_gate_hits(
    hits_detail: list[dict], hits_src: list[dict], top_n: list[dict],
) -> None:
    """把 `top_n` 里**不在展示列表**的候选补进展示列表（原地改两个列表）。

    [jonex] 2026-09-23 评审 F2。展示列表取自 `ontology_instances[:5]`（**未过 method
    gate**），路由实际判的却是 `_gated[:5]`——gate 剔掉靠前候选时，`_gated[:5]` 会含
    `ontology_instances[5]`、`[6]`…，这些**本来不在展示列表里** → 判据分回填不上，
    面板会呈现「这 5 条都没有判据分」，而真正决定路由的是另外几条。

    ⚠️ **不删任何一类**：展示列表里过不了 gate 的（召回证据，供展示）与这里补进来的
    （过 gate，供解释路由）**并存是对的**，只是补全。补进来的那条打标
    `_from_gate=True`，让前端 / 排查者看得出「这条是过 gate 后补进来的」。

    同源前提同 `_attach_rerank_to_hits`：`hits_detail` 与 `hits_src` **同序同长**，
    故两个 append 必须成对。
    """
    seen = {id(s) for s in hits_src}
    for h in top_n:
        if id(h) in seen:
            continue
        _d = _hit_trace_of(h)
        _d["_from_gate"] = True
        hits_detail.append(_d)
        hits_src.append(h)
        seen.add(id(h))


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

# ── [jonex] search_mix 中 jonex_wiki 侧超时降级（Phase 5）：最慢一路决定整体延迟，
# 超时返回空 + 标记 source 降级，不拖垮 lightrag/openkb（留 30s 给 Gateway 缓冲）──
JONEX_WIKI_SEARCH_TIMEOUT_SEC = float(os.getenv("JONEX_WIKI_SEARCH_TIMEOUT_SEC", "90"))
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
# [jonex] S3（§3.3 #3）转正：默认由 false 改 true。统一分（score = w_r*rerank
# + …）的**物质基础**——关闭时所有语义分退化为位次衰减 `_order_score`，
# 后续阈值/权重全建在流沙上。实机 `deploy/.env:252` 早已为 true（§12.1），
# 本改动只是把已验证取值固化进代码默认值，新环境漏配 env 时不再回退到未验证的旧默认。
# 回滚：环境变量显式设 false 即回改造前口径。
RAG_PRELLM_RERANK_ENABLED = os.getenv(
    "RAG_PRELLM_RERANK_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")
RAG_PRELLM_RERANK_TOPK = max(1, int(os.getenv("RAG_PRELLM_RERANK_TOPK", "8")))
# 主体一致性信号总开关（[jonex] 方案 B：语义从「硬剔除过滤」改为「软加权」，
# 见 docs/rag-subject-filter-and-answer-source-remediation-plan.md §5）
RAG_SUBJECT_FILTER_ENABLED = os.getenv(
    "RAG_SUBJECT_FILTER_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
# 主体分在最终分里的权重 λ：final = (1-λ)*rerank_relevance + λ*subject_score
RAG_SUBJECT_WEIGHT = float(os.getenv("RAG_SUBJECT_WEIGHT", "0.25"))
# [jonex] 2026-09-22：平台取回答作权（LightRAG 降为 retriever，答案收归平台侧）
# 已是**唯一路径**，原灰度开关已删除。S5 转正依据：实测新路径命中 13/14（93%）、
# 回退 1（7%，`finish_reason=length` 内容长度而非链路缺陷）。原「失败回退旧链路」
# 语义作废 —— 失败现抛 ServiceUnavailableError，空命中返回软空结果。
# 送 answer_from_chunks 的 chunk 文本总长上限（字符，硬截）
RAG_ANSWER_MAX_CONTEXT_CHARS = int(os.getenv("RAG_ANSWER_MAX_CONTEXT_CHARS", "12000"))
# [jonex] S4（§3.3 #2 / §6）：统一 rerank 的载荷上限——**只防池子爆炸，不做择优**。
#
# 为什么不能沿用 `RAG_PRELLM_RERANK_TOPK`（默认 8）当截断值：那是「rerank 后挑
# 前 8 条」，等于把候选池在打分**之前**砍到 8。2026-09-15 实测（本地靶场，
# `.env.local` 指向服务器网关）：
#   `[prellm_rerank] total=19 kept=8` / `total=18 kept=8` —— 19/18 条候选被硬砍到 8；
#   走主体路径时池子 19~20 条，其中带真 rerank 分的只有 2~6 条，其余全是
#   `order_decay` 占位分，`_unified_score` 让两种量纲直接比大小（§3.2 的失真根源）。
# 截断只能有一个点（§6「截断单点化」），就是 `RAG_ANSWER_MAX_CONTEXT_CHARS`
# 那道预算。本值只保证「rerank 一次打全池」时载荷不失控。
#
# 目标是与 `CHUNK_TOP_K=60`（deploy/.env.rag，LightRAG 侧候选池上限）**对齐**
# ——实测预算容量分布为 20~162 条（取决于 KB 的 chunk 大小），60 覆盖 20/33/44
# 三档。⚠️ `.env.rag` 侧尚未设（LightRAG 默认 20），须与 `RERANK_BINDING=null`
# 一并改（方案 §9.2 决策落定段）；未改前上游池更小，本值只是不发生作用。
RAG_RERANK_POOL_MAX = max(1, int(os.getenv("RAG_RERANK_POOL_MAX", "60")))
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
# ⛔ 已删除（2026-09-24）：S1 置信判据曾有一条口径回滚阀
#    `ONTOLOGY_CONF_SCOPE_BY_CITATIONS_ENABLED`（关掉即逐字回到「全量 facts」旧
#    口径）。**观察期结束、确认新口径更优后已连同开关一起删除**，不再留回退路径
#    —— 留着只会是「漏配即静默退回有缺陷口径」的失效面（同
#    `ONTOLOGY_L2_EVIDENCE_VERIFY_ENABLED` 当年的处置）。
#    现口径：`max_hop`/`has_attrs`/`has_desc` 只覆盖**答案实际引用的那批 fact**
#    （依据 = 答案正文的 `[ref_N]`），解析不出引用时回落全量。详见
#    `_assess_ontology_confidence` 的 docstring。
# [jonex] S7（§4.4）：L2 证据校验——用 **rerank 当判别器**替换 `_cross_verify` 的
# 对侧完整 RAG 调用。「不用 LLM」不等于「不用模型」：reranker 是 cross-encoder，
# 能感知结构关系（这是它与 token 匹配、与 bi-encoder 的本质差别），单次是
# encoder 前向，比 LLM 生成便宜一到两个数量级，且与 S6 路由判据**同一出口、
# 同一 0~1 量纲**。
#
#     support = topk_mean( rerank(query=本体答案, documents=L0 池中 RAG 侧 chunk) )
#     support >= T_hi        → 短路返回本体答案，标 cross_verified   【0 次 LLM】
#     T_lo < support < T_hi  → 落 LLM 裁决（L4）
#     support <= T_lo        → RAG 证据与本体答案不是一回事 → RAG 兜底
#
# ✅ 2026-09-22：原「要不要跑 L2」的总开关已删除，L2 **固定执行**（标定已完成、
# 实机长期为 true，开关只剩「漏配即静默退回旧行为」的失效面）。T_hi/T_lo 仍可配。
# ✅ 2026-09-20 标定完成（§8-5 决策 a 的 1~3 步；30 条真实 query / 17 条走到判带，
# 采集见 `scripts/_archive/probes/_calib_l2_threshold.py`）。**前置修正**：证据集由 `rag` 改为
# `rag`+`both`（排除 `onto`，理由见 `_l2_evidence_verify` 的 C1 注释）——修正后
# both 口径的 support 分布出现两个清晰间隙：
#     0.018 0.029 0.067 │ 0.386 0.514 │ 0.754 ... 0.998
# `T_hi` 取上间隙上侧、`T_lo` 取下间隙上侧，各留余量。因间隙宽（0.24 / 0.32），
# 阈值在 0.6~0.8 / 0.1~0.3 区间内取值结果完全相同，鲁棒性良好。
ONTOLOGY_L2_SUPPORT_T_HI = float(os.getenv("ONTOLOGY_L2_SUPPORT_T_HI", "0.65"))
# ⚠️ [jonex] A3 修复（2026-09-18）：T_lo 默认由 0.3 降为 **0.0**——即**标定前
# 关闭低带短路**，只保留高带短路。原因：`support` 是 top-K 均值（K=5，不随池
# 规模自适应），而实测 rerank 分「top1 0.96~1.0，其余多在 0.001~0.04」——代入
# `[0.97, 0.05, 0.02, 0.01, 0.0]` 得 0.21 ≤ 0.3 → 判低带 → **直接采纳 RAG 并
# 跳过裁决**。即「只有一条 chunk 强支持」这种最理想的证据形态，反而被规则判成
# 「证据与答案不是一回事」，把正确的本体答案静默降级。
# ✅ 2026-09-20 标定后由 0.0 恢复为 **0.15**：A3 的顾虑建立在**旧证据集**
# （`rag`-only，中位 0.129）之上；修正证据集后低分簇（≤0.067）的语义已明确是
# 「语料确实没答案」（人工核查 3 条，答案均自述信息不足），与 A3 描述的
# 「单条强支持被长尾拉低」形态可区分。
ONTOLOGY_L2_SUPPORT_T_LO = float(os.getenv("ONTOLOGY_L2_SUPPORT_T_LO", "0.15"))
# support 取 top-K 均值（K 与 §4.4 的 `topk_mean` 一致）。
ONTOLOGY_L2_SUPPORT_TOPK = max(1, int(os.getenv("ONTOLOGY_L2_SUPPORT_TOPK", "5")))
# ⛔ [jonex] 已删除（2026-09-24）：曾有一条「两路一致判定的 rerank 补充通道」
# （`ONTOLOGY_ANSWER_AGREE_RERANK_ENABLED` / `_MIN` + `_answers_agree_with_rerank`，
# 用 `rerank(onto_answer, [rag_answer])` 过线即判一致）。**标定结论是「没有可用
# 阈值」，已整体删除**——不要重新加回来，除非换掉判据的性质：
#
#   取数 `scripts/bench/calibrate_answer_agree_rerank.py`，语料 = 186 对真实同
#   query 两路答案，标签由 LLM 判官给。176 可判（一致 123 / 不一致 53）：
#       一致最低分 **0.6271**   不一致最高分 **0.9999**   间隙 **−0.3727（不可分）**
#   最致命一条：本体「现藏于广州艺术博物院」vs RAG「现藏香港中文大学文物馆」
#   ——收藏地直接矛盾、且冲突就在开头 60 字（与 trace 的 200 字截断无关），
#   reranker 给了 **0.9999（全库最高分）**。FP=0 需 `MIN=1.000`，那时 FN=123。
#
#   根因：Qwen3-Reranker 是 **topical relevance** 模型，同一问题的两个答案天然
#   互相「高度相关」，它测的从来不是事实一致性 → 分数在 1.0 附近饱和。
#   **任何「重合度/相似度」型判据在本问题上都不可分**（结构化覆盖率扫描同样
#   全程 FP≥21，见 `_calib_structural_agree.py`）。要检测矛盾，得做 slot 级的
#   取值比对或换成能做 NLI 的模型，不是换个阈值能解决的。
#
# 现役判据是纯结构的 `_structural_agree`（零模型调用），标定见其 docstring。
# [jonex] L2 证据池诊断埋点（**只记日志，不参与判定**）。默认 false = 零额外成本。
#
# 动机（§8-5 标定的前置取数）：A2 把两路共有的 chunk 标 `both` 并从 L2 证据池
# 排除，而两路高度重叠是常态——被排除的那批恰是最相关的。本埋点直接量
# 「被排除的那批分是不是更高」，为此前的标定提供原料。
#
# ✅ 2026-09-20 已用于标定并得出结论（见 `_l2_evidence_verify` 的 C1 注释）：
# `both` 中位 0.934 vs `rag` 中位 0.129，A2 排除过度 → 证据集已修正为 rag+both。
# 保留本开关供**后续复核**（换语料 / 换 reranker / 调 K 后重标）。
#
# 开启后**额外**对 `both` / `onto` 各补一次 rerank（判定只用 rag+both 那批），
# 结果只进 `detail` 与日志，不改变 band。
ONTOLOGY_L2_DIAG_ENABLED = os.getenv(
    "ONTOLOGY_L2_DIAG_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
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
# [jonex] 四屏修复 (b)：结构分来源（本体路径，纯 _evidence_score）经 E3 rerank
# 后的图片阈值。本体路径图片候选强制 E3（I4-a），E3 分数已是 0~1 语义相关性，
# 不应再用语义路径的 0.7 裁人（四屏案例 56 号图 0.6915 压线被误杀）；但也不能
# 纯豁免（会漏出 E3 分 0.22 的噪声图）。实测分布：相关带 ≥0.69、噪声带 ≤0.22，
# 0.5 留双侧余量。设 0 = 关闭结构分阈值（回退纯豁免，I4-a 之前行为）。
# E3 未跑/失败时结构分候选不受此阈值约束（分数仍是证据权重，非语义分）。
RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL = float(
    os.getenv("RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL", "0.5")
)
# 通过阈值后每个文档保留的图片 location 上限（按分数降序取 TopN）
RAG_IMAGE_REF_MAX = max(1, int(os.getenv("RAG_IMAGE_REF_MAX", "6")))

# ── S1 本体侧 chunk 正文批量回捞 ──
# 背景：独立本体抽取后 source_chunks[].content 恒空转，本体路径 raw_refs 只有
# parse_file_source 的结构锚点、没有正文 → 统一 rerank 无输入。开关开启后，
# 在候选池构建阶段一次性并发回捞**全部**候选 chunk 正文（不再只补图片）。
# 关闭时回退改造前行为：只回捞「路线 B 未覆盖的图片」，逐 doc 串行。
# 详见 docs/ontology/ontology-rag-scoring-unification-and-routing-plan.md §3.3 #1、§12.4。
ONTOLOGY_CHUNK_TEXT_PREFETCH_ENABLED = os.getenv(
    "ONTOLOGY_CHUNK_TEXT_PREFETCH_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
# 单 doc 回捞超时（秒）。本体引用是「已有结果」的富化，超时按无正文降级、不阻断。
ONTOLOGY_CHUNK_TEXT_PREFETCH_TIMEOUT = float(
    os.getenv("ONTOLOGY_CHUNK_TEXT_PREFETCH_TIMEOUT", "10")
)
# 回捞 doc 数上限与并发度：防止一次查询扇出过多 HTTP 打爆 atomic-rag。
# 超出上限按证据权重的 key_order 截断（已有序），并**显式记日志**不做静默丢弃。
ONTOLOGY_CHUNK_TEXT_PREFETCH_MAX_DOCS = max(
    1, int(os.getenv("ONTOLOGY_CHUNK_TEXT_PREFETCH_MAX_DOCS", "32"))
)
ONTOLOGY_CHUNK_TEXT_PREFETCH_CONCURRENCY = max(
    1, int(os.getenv("ONTOLOGY_CHUNK_TEXT_PREFETCH_CONCURRENCY", "4"))
)

# ── S2 evidence_prior：证据先验权重（§3.2 / §8-1）──
# 背景：`_ontology_refs` 原有的实体权重 `1.0 + min(confidence, 1.0)` 已失去
# 区分度——confidence 是 `extraction_method` 的分档常数编码（§1.4），且写图侧
# 「只升不降」（`REPO` 的 ON MATCH `CASE WHEN $confidence > coalesce(...)`），
# 会随重复抽取单向爬升。S2 改为按 `extraction_method` 派生 method_weight
# （编码更稳、语义可读），并叠加两个与 method **独立**的维度：
#   `rank`（本体召回位次）、`contrib`（共证事实数，收集期已在算）。
# 区分度收益来自这两者，method_weight 本身只是换了个更稳的编码（§1.4 订正）。
# 灰度：`confidence` 逐字保留改造前口径，可即时回滚。
# 详见 docs/ontology/ontology-rag-scoring-unification-and-routing-plan.md
#     §3.2、§1.3、§1.4、§1.8、§8-1。
RAG_EVIDENCE_PRIOR_MODE = os.getenv(
    "RAG_EVIDENCE_PRIOR_MODE", "confidence"
).strip().lower()

# `extraction_method` → 质量先验（§1.3 全 7 种取值）。
_METHOD_WEIGHT: dict[str, float] = {
    "native_llm": 1.0,
    "procedural_llm": 1.0,
    "manual": 1.0,
    # 真行对象（table_sig 非空）才是 1.0；分组聚合节点见 _method_weight
    "row_as_object": 1.0,
    "llm_guided": 0.8,        # 存量，无 live 生产者
    "endpoint_backfill": 0.3,
    "stub": 0.0,
}
# 未登记的 method：保守取「低权威但不为零」，避免新编码上线即被一票否决。
_METHOD_WEIGHT_UNKNOWN = 0.8
# 过期标签缓解下限（§3.2）：merge_entity 的 ON MATCH 不写 extraction_method
# （「首写者胜」），实体可能先被低质路径建成空壳、后被 native_llm 补全内容而
# 标签仍是旧的。标签只用于锦上添花，不用于一票否决——有实质内容则不低于 0.8。
_METHOD_WEIGHT_CONTENT_FLOOR = 0.8
# 分组聚合节点：`TM` 的 build_group_relations 产物也打 row_as_object，但它们
# 没有 table_sig / pk_value / 整行 attributes（§1.3 ⚠️）。语义等同「只有名字
# 的结构节点」→ 与 endpoint_backfill 同档。判据唯一化为 table_sig 非空
# （§3.4：实测 name_source 恒为 null 是死判据、attributes 键数会误伤 379 个
# 真行对象）。
_METHOD_WEIGHT_GROUP_NODE = 0.3

# chunk 模态加成（§3.2：把 `neighbors()` 的三档 tier 归并进统一公式，
# 见 §1.8——Cypher 侧 tier 只承担截断，语义必须同步进查询侧打分）。
_MODALITY_BOOST_IMAGE = 1.2
_MODALITY_BOOST_TEXT = 1.0
# 「无溯源」档：在 chunk 级不出现（无溯源的实体产不出 chunk 引用，走文档级
# 兜底），保留常量供 S3 对文档级引用计分。
_MODALITY_BOOST_NONE = 0.6

# 关系边种类权重（§3.2，按 2026-09-13 实测 ~101k 条边的构成定义）。
_REL_WEIGHT_GROUP = 0.3        # 包含（分组关系）占 71.1%：纯代码规则产物、无溯源
_REL_WEIGHT_UNTYPED = 0.4      # RELATES_TO：KEEP_UNTYPED_EDGES 兜底降级
# KEEP_UNTYPED_EDGES 兜底边的类型名：现行落图用 RELATES_TO（TBox 已声明的通用
# 关联），历史图里有 133bb0fa 改名前的 RELATED_TO——两者同一档权重，都收。
_UNTYPED_EDGE_TYPES = ("RELATES_TO", "RELATED_TO")
_REL_WEIGHT_TABLE_REF = 0.5    # 表格合成行引用边：列值共现，不是抽出的语义关系
_REL_WEIGHT_EXTRACTED = 1.0    # TBox 定型关系 / 带 file_path 的抽取边

# ── S3 统一分（§3.2 / §6 S3）──
# 背景：`/search` 与本体路径此前各有各的分数量纲（LightRAG relevance /
# `final_score`（λ 混合）/ `group_final_score` / `_order_score` / `_evidence_score`），
# 互不可比、阈值只能按路径各打补丁（图片双阈值即其产物，§2.3 ⑤）。
# 唯一跨路径天然可通约的是 reranker 相关性（0~1，两路共用 llm-gateway
# `/v1/rerank` 出口），故以它为主轴：
#
#     score = w_r * rerank + w_e * evidence_prior + w_s * subject_prior
#
# 其余分量全部降级为**先验权重**，不再直接充当分数。
#
# 权重按 §8-4 决策 (b) 分三步引入（P1→P2→P3），不一次到位：
#   P1（默认）：1.0 / 0.0 / 0.0 —— 纯 reranker 相关性
#   P2        ：0.8 / 0.2 / 0.0 —— 叠加 evidence_prior（S2 产出）
#   P3        ：0.7 / 0.2 / 0.1 —— 叠加 subject_prior（收敛到目标配比）
# 每阶段独立观察、前阶段对照数据留作后阶段基线；任一阶段未达预期即停在该阶段。
# 阶段切换只改 env，代码零改动（权重全部走环境变量灰度）。
#
# ⚠️ P2/P3 的**判别力依赖两路候选池各自都有真分**。实测（2026-09-15）：
# 统一分整条链能否生效，取决于 `relevance` 覆盖率——它**不是**由
# `RAG_PRELLM_RERANK_ENABLED` 单独决定的，还要本体候选真有正文：
#
#   ONTOLOGY_CHUNK_TEXT_PREFETCH_ENABLED=false（默认）
#     → 本体候选只有「缺正文的图片」被回捞，文本/表格候选全空
#     → `_prellm_rerank_chunks` 收到 texts<=1 → 提前返回，一条都不打分
#     → 全池 `order_decay` → `score` 就是池位次
#     → 而池位次又由 `_evidence_score` 排出来 → `w_e` 与位次共线 → **P1=P2=P3**
#
# 实测 4 组（S1 关→开）：真分覆盖率 0/46→43/46、0/53→47/53、6/21→16/21、
# 9/34→24/34；`_evidence_score` 与池位次 ρ 从 -0.88/-0.96 解耦到 -0.61/-0.37；
# P1 置换 0%（空步）→ P2/P3 置换 59~85%。**S1 是 P1/P2/P3 生效的前置条件。**
#
# 下述覆盖率护栏就是为这条链兜底：覆盖率不足时跳过重排并**出声**，避免
# 「全 order_decay 却装作排了序」的静默失真（该失真正是上述排查困难的成因）。
# [jonex] A1 修复（2026-09-18）：默认由 false 改为 true（用户定）。
# 与 §8-11「保持 false」的冲突以此为准——那句是 S1 未开时的旧决策，已被后续
# 「先开 S1 再重测 P1/P2/P3」取代。实机 env 早已显式置 true，本步把「依赖每个
# 部署点记得配」改成「代码默认即正确」。
# ⚠️ 开启后必须有覆盖率护栏（`RAG_UNIFIED_SCORE_MIN_COVERAGE`）配套：低覆盖率下
# 照写 `score`/`score_kind` 会让下游误判分数有效——该护栏只在工作区、未随代码
# 提交（review C2），**提交时二者必须同批**。回滚：显式置 false。
RAG_UNIFIED_SCORE_ENABLED = os.getenv(
    "RAG_UNIFIED_SCORE_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")
# P1 初值：纯 rerank。P2 改 0.8/0.2/0.0，P3 改 0.7/0.2/0.1。
RAG_SCORE_W_RERANK = float(os.getenv("RAG_SCORE_W_RERANK", "1.0"))
RAG_SCORE_W_EVIDENCE = float(os.getenv("RAG_SCORE_W_EVIDENCE", "0.0"))
RAG_SCORE_W_SUBJECT = float(os.getenv("RAG_SCORE_W_SUBJECT", "0.0"))
# 统一分的最低 rerank 真分覆盖率（0~1）。低于此值时统一分退化为「位次与位次
# 自比」——排序结果与不排序等价，却会写出 `score`/`score_kind` 让下游误以为
# 分数有效。实测基线：S1 关闭时本体路径 0%~29%，S1 开启后 70%~94%；RAG 预算
# 路径恒 100%。默认 0.5 落在两者中间，两侧都不误伤。
RAG_UNIFIED_SCORE_MIN_COVERAGE = min(
    max(float(os.getenv("RAG_UNIFIED_SCORE_MIN_COVERAGE", "0.5")), 0.0), 1.0
)

# `score_kind` 取值（§3.2）：候选上必须显式标记分数语义。
_SCORE_KIND_RERANK = "rerank"            # rerank 分可用，是真相关性
_SCORE_KIND_ORDER_DECAY = "order_decay"  # rerank 缺项 → 位次衰减，**只排序不判阈值**


def _img_structural(cand: tuple) -> bool:
    """图片候选是否「无真语义分」（§3.3 #5 ①）。

    两个用途：是否该给它跑 E3 补分、是否该豁免语义阈值。

    判据按**统一分跑没跑过**分流：
      · 跑过（`score_kind` 非 None）→ `kind != rerank` 即为无真分。这比旧判据
        严格：旧判据要同时排除 `relevance`/`group_relevance`/`final_score`/
        `group_final_score` 四个键，漏掉一个就把「无真分」误判成「语义来源」，
        于是拿一个非语义量纲的数去比语义阈值。
      · 没跑过（`score_kind` 为 None）→ 回退旧 `_score_source == "structural"`
        标签，逐字保留改造前行为（可即时回滚）。

    候选元组：`(loc, asset_ext, score, score_source, score_kind)`；`score_kind`
    是 S4 追加的第五位，老调用点只产出四元组时按未跑统一分处理。
    """
    kind = cand[4] if len(cand) >= 5 else None
    if kind is not None:
        return kind != _SCORE_KIND_RERANK
    return len(cand) >= 4 and cand[3] == "structural"


def _img_min_score(src: str, kind: str | None, reranked: bool) -> float:
    """图片候选的分数阈值（§3.3 #5 ①「双阈值收敛为一个」）。

    返回值 `<= 0` 表示**不判阈值**（只走 TopN 截断）。

    统一分口径（`kind is not None`）：收敛为**一个**阈值
    `RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL`。因为此时所有图片分都来自
    `rerank(图片描述 × query)`——E3 与主池 rerank 是同一个模型、同一个函数，
    实测相关带 ≥0.69 / 噪声带 ≤0.22，0.5 切在两带之间。

    为什么不能用 `RAG_IMAGE_REF_SCORE_MIN`（0.7）：那个值标定在另一条量纲上
    （LightRAG `relevance` 混入 λ 权重后的 `final_score`），正是当年把 0.6915
    的同批图整批误杀的值（四屏修复 (b)）。

    旧口径（`kind is None`，统一分未开启）：逐字保留改造前的双阈值双轨——
    E3 已跑用 0.5、未跑时语义来源用 0.7 / 结构分来源豁免。该分支只做回滚兜底，
    改动即等于改旧口径行为，勿「顺手统一」。
    """
    # 评审 P3：RAG_IMAGE_REF_SCORE_MIN=0 保持总开关语义
    # （.env.example「设 0 关闭过滤」）——结构分阈值不得绕过。
    if RAG_IMAGE_REF_SCORE_MIN <= 0:
        return 0.0
    if kind is not None:
        # `order_decay` 是位次占位分，S3 契约明令全链路不得据此做阈值判定
        # ——它只是「排在这」，不是「相关性低」。旧口径的「结构分来源豁免」
        # 在统一分下等价于此，两支在此合流。
        return (
            RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL
            if kind == _SCORE_KIND_RERANK else 0.0
        )
    if reranked:
        return RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL
    if src == "structural":
        return 0.0
    return RAG_IMAGE_REF_SCORE_MIN


# [jonex] 本体引用按相关性裁剪（2026-09-23）。
#
# 背景：`_ontology_refs` 的打分链早已跑完（chunk 级 rerank → 统一分），但最后
# `_build_references` **一刀不裁**——查询「电力变压器 有哪些组件」答案只引用 1 条，
# 面板却列出 6 条关联原文。同一条链上**图片那条支路一直在裁**（`_img_min_score`
# 按 `RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL` 判 `score_kind == rerank`），缺的只是
# 非图片模态那一半。
#
# 实测（同一 reranker，上述 6 条的 chunk 级 relevance）：
#   0.9937 / 0.9770 / 0.8111 / 0.6848 / 0.6526 / 0.0697
# 最大间隙 0.583 落在 0.0697~0.6526 之间 → 0.5 切在缝里。
#
# ⚠️ 与 `RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL`（同为 0.5）**值相同但量纲不同**：
# 那个标定在「图片 VLM 描述 × query」，本值标定在「chunk 正文 × query」。
# 不合并成同一个变量——以后调图片阈值不该连带动文本。
#
# ⚠️ **默认 0.0 = 关闭**：与同文件同类机制的灰度惯例一致（`ONTOLOGY_ROUTE_RERANK_ENABLED`
# / `RAG_L0_UNIFIED_POOL_ENABLED` 均默认 false）。且本值只标定在 n=6 上，不足以支撑
# 默认开启——线上 `.env` 显式配 0.5 灰度，出问题改回 0 即可（代码零改动）。
ONTOLOGY_REF_MIN_RELEVANCE = float(os.getenv("ONTOLOGY_REF_MIN_RELEVANCE", "0.0"))

# [jonex] 引用按**答案引用标**过滤（2026-09-23，Task 6）。
#
# 与上面的 `ONTOLOGY_REF_MIN_RELEVANCE` 是**两种独立语义**、两个独立开关：
#   · 那个问「与问题相关吗」——按分数阈值裁（与答案无关的元信息表）；
#   · 这个问「答案用到了吗」——按答案正文的 `[ref_i]` 裁（答案只引用 1 条，
#     面板却列 6 条）。可叠加：本体路径先按分数裁、再按引用标滤。
#
# ⚠️ **过滤必须先于 `normalize_citations`**（两条路径一致）：该函数遍历**传进来的**
# 引用列表建 `doc_to_idx`（`ontology_llm.py:160-166`）。先滤后归一 → 答案里的
# `[ref_i]` 自动落到**新下标**，答案文本一个字都不用改；反过来（先归一后过滤）
# 会打乱下标，答案里的 `[n]` 全部错位甚至悬空。
#
# ⚠️ **默认 false = 关闭**（与 `ONTOLOGY_ROUTE_RERANK_ENABLED` / `RAG_L0_UNIFIED_POOL_ENABLED`
# / `ONTOLOGY_REF_MIN_RELEVANCE` 同一灰度惯例）：部署即零行为变化，线上显式置 true。
RAG_REF_CITED_ONLY_ENABLED = os.getenv(
    "RAG_REF_CITED_ONLY_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")


def _prune_low_relevance_refs(
    refs: list[dict], min_score: float,
    protect_doc_ids: set[str] | None = None,
) -> tuple[list[dict], dict]:
    """按**裸 `relevance`** 裁掉低相关的候选（本体引用专用，纯函数、保序）。

    四条口径，每条都对应一个已知的坑：

    | 口径 | 为什么 |
    |---|---|
    | 只认 `relevance is not None` | `order_decay` 是位次占位分，不是"相关性低"。契约（`:1304-1307`）明令全链路不得据此做阈值判定——**没打上分的一律放行** |
    | `protect_doc_ids` 无条件放行 | 答案引用过的 doc 一旦被裁，`normalize_citations` 的 `doc_to_idx` 里就没它，答案里引用它的 `[ref_N]` 会被**静默抹成空串**（`ontology_llm.py:169-176`）。这是答案质量回归，不是展示问题 |
    | 按 `_order_group` **整组**裁 | 组内是行序 / 步序，裁一半会让枚举题字段缺列（§3.4 排序豁免要防的正是这个）。组的判据取**组内真实分最大值** |
    | 全低于阈值时保底留 1 条 | 相关带整体偏低的 query 不能出现「一条引用都没有」。⚠️ 但池里若有 `order_decay` 残留，它们天然留下 → 保底不触发（见 `test_real_scores_all_pruned_decay_remains`） |

    ⚠️ 判据是 `relevance is not None`，**不是** `score_kind == "rerank"`（2026-09-23
    评审 I2 订正）。两者只在 `_apply_unified_score` **跑过**时才等价——统一分里
    `relevance is None` ⟺ `kind = order_decay`（`_unified_score:1421-1427`）。而统一分
    **没跑**时（开关关闭、或真分覆盖率 < `RAG_UNIFIED_SCORE_MIN_COVERAGE` 触发护栏，
    该函数 `:1584`/`:1592` 两处早退）整池**不写 `score_kind`**，可
    `_prellm_rerank_chunks` 独立写入的 `relevance`（`:3305`）照常有值。此时拿
    `score_kind` 当门 = 全池被判「无真分」→ 一条不裁，且 detail 报
    `pruned_count=0`，与「本来就没有低于阈值的候选」字面不可区分（静默失效）。

    判据取裸 `relevance` 而非统一分 `score`：线上 `w_r=0.8 / w_e=0.2`，`score` 混了
    证据先验，高先验的**无关**文档会被抬过线——筛相关性就该只用相关性那一维。

    保序：只做减法，不重排（排序是 `_order_aware_sort` 的职责）。

    Returns:
        `(kept, stats)`；`stats` 供 `STAGE_REF_RETRIEVE` 埋点用。
    """
    _empty = {"pruned": 0, "kept": len(refs), "decay_passed": 0, "protected": 0,
              "pruned_relevance": [], "min_kept_relevance": None}
    if not refs:
        return [], {**_empty, "kept": 0}
    if min_score <= 0:
        return list(refs), _empty

    protect = protect_doc_ids or set()

    def _is_real(r: dict) -> bool:
        # [jonex] 2026-09-23 评审 I2：**不**依赖 `score_kind`（理由见 docstring）。
        # `relevance is not None` 即「有真 rerank 分」；统一分未跑时 `score_kind`
        # 整池缺失，靠它当门会让裁剪静默失效。
        return r.get("relevance") is not None

    decay_passed = sum(1 for r in refs if not _is_real(r))
    # ⚠️ 口径与 `_passes` 的 protect 分支**逐字对齐**（2026-09-23 评审 F3）：那边是
    # `doc_id in protect` 就放行、**不看** `_is_real`。统计这边原多判了一道 `_is_real`
    # → 白名单里的 `order_decay` / 无真分候选**被保护却不计入** `protected`，detail
    # 报数偏小（行为无差——它们本来也放行，但排查时会误判「白名单没生效」）。
    protected = sum(1 for r in refs
                    if r.get("doc_id") and r["doc_id"] in protect)

    # 组 → 组内真实分最大值（无真分的组不参与判定，一律放行）
    group_best: dict[str, float] = {}
    for r in refs:
        g = r.get("_order_group")
        if not g or not _is_real(r):
            continue
        v = float(r["relevance"])
        if v > group_best.get(g, float("-inf")):
            group_best[g] = v

    def _passes(r: dict) -> bool:
        if r.get("doc_id") and r["doc_id"] in protect:
            return True                       # 答案引用过的 doc，无条件放行
        g = r.get("_order_group")
        if g and g in group_best:
            return group_best[g] >= min_score
        if not _is_real(r):
            return True                       # order_decay / 无真分：不判阈值
        return float(r["relevance"]) >= min_score

    kept = [r for r in refs if _passes(r)]
    pruned = [r for r in refs if not _passes(r)]

    if not kept and pruned:                   # 保底：全裁光时留分最高的一条
        best = max(pruned, key=lambda r: float(r.get("relevance") or 0.0))
        kept, pruned = [best], [r for r in pruned if r is not best]

    kept_rel = [float(r["relevance"]) for r in kept if r.get("relevance") is not None]
    return kept, {
        "pruned": len(pruned),
        "kept": len(kept),
        "decay_passed": decay_passed,
        "protected": protected,
        "pruned_relevance": sorted(
            (float(r["relevance"]) for r in pruned
             if r.get("relevance") is not None), reverse=True)[:6],
        "min_kept_relevance": min(kept_rel) if kept_rel else None,
    }


def _rebuild_after_e3(cands: list[tuple], score_by_idx: dict) -> list[tuple]:
    """E3 rerank 返回后重建候选元组，**逐条**决定分数与 `score_kind`。

    修的是什么：原实现写
    `(loc, ext, score_by_idx.get(i, sc), src, _SCORE_KIND_RERANK)`——用
    `.get(i, sc)` 兜底，却把**整批**一律标成 `rerank`。rerank 响应残缺
    （只返回部分 `index`）时，未被返回的候选会带着**原分数**被贴上真分标签，
    再拿去比 `RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL`。原分数在统一分口径下可能
    正是 `order_decay` 位次占位分——S3 契约明令「全链路不得据此做阈值判定」，
    这恰好是本次改造要消灭的那类误判。

    现在按「这次 rerank 到底有没有返回它」分流：
      · 返回了 → 用 rerank 分，`score_kind` 置 `rerank`（此刻确实是真分）；
      · 没返回 → **保留原分数与原 `score_kind`**，语义不被篡改。

    候选元组形状 `(loc, asset_ext, score, score_source, score_kind)`；老调用点
    只产出四元组时，`score_kind` 按缺失处理（补 `None`，即未跑统一分）。
    """
    rebuilt = []
    for i, (loc, ext, sc, src, *_rest) in enumerate(cands):
        kind = _rest[0] if _rest else None
        if i in score_by_idx:
            rebuilt.append(
                (loc, ext, score_by_idx[i], src, _SCORE_KIND_RERANK))
        else:
            rebuilt.append((loc, ext, sc, src, kind))
    return rebuilt

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

# [jonex] M3-b procedural 意图词表（docs/ontology/skill-action-ontology-enhancement-plan.md M3-b）：
# 「如何/怎么/步骤/流程」类问法优先判 procedural，枚举处置不生效——
# 「配置流程的所有步骤」这类步骤问法与枚举词表冲突时以 procedural 为准。
_PROCEDURAL_PATTERNS: list[re.Pattern] = [
    re.compile(r"如何|怎么|怎样"),
    re.compile(r"步骤|流程|操作方法|操作步骤|下一步"),
    re.compile(r"第.步"),
    re.compile(r"先.*然后"),
]

# [jonex] P8（M3-a）inverse 语义翻译总开关（默认 true；无 inverse_of 声明时空转零变化）
ONTOLOGY_INVERSE_EXPAND_ENABLED = (
    os.getenv("ONTOLOGY_INVERSE_EXPAND_ENABLED", "true").lower()
    not in ("0", "false", "no", "off")
)

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


async def _filter_kbs_by_classification(
    tenant_id: str, user_id: str | None, kb_ids: list[str],
) -> list[str]:
    """按用户可读最高等级过滤 KB（B6 §5.1 多 KB 检索）。

    管理员原样返回；保持输入顺序与重复项。

    ⚠ 无 user / "anonymous" **不再原样放行**（B6 收口）：MCP 等机器调用没有
    用户身份，早退会让检索完全绕过密级。机器身份按最低启用等级处理
    （fail closed），与文档级守卫对空用户的口径一致。
    """
    kb_ids = list(kb_ids or [])
    if not kb_ids:
        return kb_ids

    from jonex_core.security import classification_access as policy
    from .space_permission_service import is_tenant_admin

    if user_id and user_id != "anonymous":
        if await is_tenant_admin(tenant_id, user_id):
            return kb_ids
    async with get_db_session() as session:
        max_ranks = await policy.kb_max_level_rank(session, tenant_id, kb_ids)
        user_rank = await policy.get_user_max_rank(
            session, tenant_id, user_id, is_tenant_admin=False,
        )
    if user_rank is None:
        return kb_ids
    allowed = [kb_id for kb_id in kb_ids if max_ranks.get(kb_id, 0) <= user_rank]
    if len(allowed) < len(kb_ids):
        logger.info(
            "检索批量跳过：%d 个知识库超密级（user=%s）",
            len(kb_ids) - len(allowed), user_id,
        )
    return allowed


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
        # [jonex] S3：合并后显式字典会丢掉子段上落的统一分，按「组内最高分」
        # 补回（与 doc 级同口径）。开关关闭时全为 None，不写该键。
        _best = max(
            (
                (g.get("score"), g.get("score_kind"))
                for g in group if g.get("score") is not None
            ),
            key=lambda t: t[0], default=None,
        )
        if _best is not None:
            merged["score"], merged["score_kind"] = _best
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


# ── S2 evidence_prior 分量（纯函数，§3.2）──────────────────────────


def _has_entity_content(ent: dict) -> bool:
    """实体是否有实质内容（描述非空或属性非空）。

    只用于「过期标签缓解」（§3.2）：标签低但内容在的实体不该被一票否决。
    attributes 经 Neo4j 往返可能是 JSON 字符串（`merge_entity` 用
    `apoc.convert.toJson`），两种形态都要认。
    """
    if str(ent.get("description") or "").strip():
        return True
    attrs = ent.get("attributes")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except (json.JSONDecodeError, TypeError):
            return False
    return bool(attrs) if isinstance(attrs, dict) else False


def _is_true_row_object(ent: dict) -> bool:
    """真行对象判据（§3.4 / §1.3）：`extraction_method == "row_as_object"`
    **且** `table_sig` 非空。

    分组聚合节点（`TM:3443-3453`）同样带 `row_as_object` 标记，但没有
    `table_sig`/`pk_value`、没有整行 `attributes`——单看 `extraction_method`
    会把它们一并纳入判据。2026-09-13 探针实测（§3.4 表）另两个候选判据均不可用：
    `name_source` 全量实体恒为 `null`（抽取侧定义了却没写进图，死判据）、
    `attributes` 键数 > 1 会误伤 379 个真行对象。故判据唯一化为 `table_sig` 非空。

    实机规模（2026-09-15 复测，非 stub 实体 117,117 条）：真行对象 90,272 /
    分组聚合节点 12,490，两档分离干净。
    """
    return (
        str(ent.get("extraction_method") or "").strip() == "row_as_object"
        and bool(str(ent.get("table_sig") or "").strip())
    )


def _mark_order_lock(
    lock_map: dict[str, tuple[str, str]], fp: str, tag: tuple[str, str],
) -> None:
    """把一个 `file_path` 串（可能 `<SEP>` 连接多值）登记为排序豁免片段（§3.4）。

    登记的是**原始片段字符串**，与聚合循环里 `fp.split("<SEP>")` 得到的 `seg`
    逐字一致——这样豁免判定不需要第二次 parse。`setdefault`：同一 chunk 被多
    个实体共证时**任一来源锁定即锁定**（表行的行序 / 步骤链的步序都不能被打乱）。
    """
    for seg in (fp or "").split("<SEP>"):
        seg = seg.strip()
        if seg:
            lock_map.setdefault(seg, tag)


def _method_weight(ent: dict) -> float:
    """按 `extraction_method` 派生实体质量先验（§3.2）。

    ⚠️ 不能用 `extraction_method == "row_as_object"` 判「真行对象」——分组
    聚合节点同标记但无 table_sig（§1.3）。判据唯一化为 table_sig 非空
    （§3.4：name_source 恒 null 是死判据、attributes 键数会误伤真行对象）。
    """
    method = str(ent.get("extraction_method") or "").strip()
    if method == "row_as_object" and not str(ent.get("table_sig") or "").strip():
        return _METHOD_WEIGHT_GROUP_NODE
    w = _METHOD_WEIGHT.get(method, _METHOD_WEIGHT_UNKNOWN)
    if w <= 0.0:
        # stub = 显式剔除口径（§3.2「直接剔除」），不参与下方的「有内容抬升」
        # 缓解——那条例外的目的是救「标签过期」的低质档，不是复活空壳。
        return 0.0
    if w < _METHOD_WEIGHT_CONTENT_FLOOR and _has_entity_content(ent):
        return _METHOD_WEIGHT_CONTENT_FLOOR
    return w


def _rank_decay(rank: int) -> float:
    """本体召回位次衰减（§8-1 叠加的独立维度之一）。

    位次 0/1/2 → 1.0/0.50/0.33，与 `_hop_decay` 同族（1/x 递减）。
    精确/前缀匹配命中时列表只有 1~3 条，位次是强信号；混合召回（RRF）串越
    长位次越弱，恰好对应「后排命中可信度低」。
    """
    return 1.0 / (1.0 + max(int(rank), 0))


def _hop_decay(hop: int) -> float:
    """事实跳数衰减（§3.2：沿用现有 2.0/hop 的**相对**关系，归一化后等价）。"""
    return 1.0 / max(int(hop or 1), 1)


def _modality_boost(ref: dict) -> float:
    """chunk 模态加成（§1.8 三档 tier 归并进统一公式）。

    图片锚点 1.2 / 普通 chunk 1.0。「无溯源 0.6」档在本函数不出现——无溯源的
    实体产不出 chunk 级引用，走文档级兜底（`_MODALITY_BOOST_NONE` 留给 S3 对
    文档级引用计分）。
    """
    return (
        _MODALITY_BOOST_IMAGE
        if ref.get("image_idx") is not None
        else _MODALITY_BOOST_TEXT
    )


def _rel_kind_weight(relation_type: str, rel_chunks: list) -> float:
    """关系边种类权重（§3.2 实测边构成表）。

    判据次序有意如此：**先判行锚点再判 file_path**。表格合成行引用边
    （`reconciliation_service._build_table_reference_relations`）的
    source_chunks 沿用行对象的锚点；S1b 之后行对象自身也带上了 chunk 的
    `file_path`，这类边会同时具备两种锚点——按 file_path 判会把它误升到 1.0，
    与 §3.2「必须显式降权」相反。
    """
    entries = [e for e in rel_chunks if isinstance(e, dict)]
    if any(
        e.get("table_sig") or e.get("row_start") is not None for e in entries
    ):
        return _REL_WEIGHT_TABLE_REF
    if any(e.get("file_path") for e in entries):
        return _REL_WEIGHT_EXTRACTED
    rt = str(relation_type or "").strip()
    if rt == "包含":
        return _REL_WEIGHT_GROUP
    if rt in _UNTYPED_EDGE_TYPES:
        return _REL_WEIGHT_UNTYPED
    return _REL_WEIGHT_EXTRACTED


def _evidence_prior(best_weight: float, contrib: int, ref: dict) -> float:
    """把收集期的权重折算成 0~1 的 `evidence_prior`（§3.2）。

    `evidence_prior = modality_boost(chunk) * max(分量权重) * log1p(contrib)`

    取 **`max`（最强单条贡献）而非 `Σw`（累加）**——2026-09-18 修正，实测见
    `scripts/_archive/probes/_diag_b1_b6_b7.py`。原实现传 `Σw`，而 `Σw` 每条贡献加一次，与
    `contrib` 计的是**同一个 n**，于是 `n` 进了两次：等权时实际是
    `n·w̄·log1p(n)`。`log1p` 的用意是「共证是存在性证据，线性会放大刷量」，
    而 `n·log1p(n)` **比线性还猛**（n=42 时放大 160×，纯 `log1p` 只有 3.8×），
    与该注释自相矛盾。改 `max` 后 `contrib` 只剩 `log1p(contrib)` 一条通道。

    **为什么不是 `Σw/contrib`（均值）**：均值不单调——新增一条弱贡献会把分
    **压低**。n=1、Σw=1.0 时 `(1.0/1)·log1p(1)=0.693`；再加一条 w=0.05 得
    `(1.05/2)·log1p(2)=0.577 < 0.693`。「证据变多、分反而降」对证据先验是硬伤。
    `max` 只受 `log1p(contrib)` 推高，单调不减；且它是文档公式**最忠实的聚合**
    ——§3.2 写的就是「一条贡献的 `method_weight × hop_decay`」，取最强那条即
    逐字读法。

    归一化留到有全局最大值时再做（调用方按批归一），此处只保证单调。
    """
    return float(best_weight) * _modality_boost(ref) * math.log1p(max(contrib, 0))


def _as_score_float(value: Any, default: float = 0.0) -> float:
    """宽容地把 ref 上的分值转成 float。

    分数来自多条链路（LightRAG JSON / Neo4j 属性往返 / 本模块改写），可能是
    字符串、None 或 NaN/Inf。统一分是**排序与阈值**的依据，一处脏值就会让
    整条候选池的排序崩掉，故此处不放任 `float()` 抛异常。
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _unified_score(
    ref: dict,
    *,
    w_r: float,
    w_e: float,
    w_s: float,
    order_decay: float = 0.0,
) -> tuple[float, str]:
    """按 §3.2 公式给单个候选项算统一分，返回 `(score, score_kind)`。

    三个分量：

    | 分量 | 取值 | 来源 | 缺项条件 |
    |---|---|---|---|
    | `rerank` | 0~1 | `ref["relevance"]`（reranker 原始相关性） | 未跑/失败 |
    | `evidence_prior` | 0~1 | `ref["_evidence_score"]`（S2 归一化证据先验） | 非本体路径 |
    | `subject_prior` | 0~1 | `ref["subject_score"]`（主体三信号取 max） | 未解析到主体 |

    **缺项时权重重分配，不置 0**（§3.2）：只对「可用分量」的权重求和做分母，
    等价于把缺失分量的权重按比例摊给其余分量。改造前 `rel is None` 时直接把
    `final_score` 切到主体分（量纲跳变），正是分数漂移的根源之一。

    `rerank` 分量特殊：它缺项时用**位次衰减**顶上并标 `score_kind="order_decay"`
    ——这是「主轴不可用」的显式降级，不是把它当成 0 相关性。调用方（及 S4 的
    阈值判定）必须据此**不做任何阈值判定**，只用于排序。

    Args:
        order_decay: 调用方按**当前**列表位次算出的衰减值（`1.0 - i/n`）。
            `rerank` 缺项时**一律取它**，不读 ref 上残留的 `_order_score`。

            为什么不能用 `_order_score`：那是 `_subject_weight_and_rank` 在
            **上游召回序**上 `setdefault` 的中间量，而本函数要排的是**当前**
            候选池——池序经过分组 / 组级重排 / 组员展开之后已与召回序彻底脱钩。
            读它等于把主体加权的排序成果整个推翻。实测（2026-09-15，4 个样本）：
            P1 下 `image` / `table_row` 被顶上第 1 位、唯一的正文命中被打到第 17 位，
            与现状排序的逆序对 73~94；改取当前位次后降到 16~30 且 top1 全部回到正文。
            取当前位次则自洽：分数严格随当前序递减，稳定排序下即保序。
    """
    rerank = ref.get("relevance")
    if rerank is None:
        kind = _SCORE_KIND_ORDER_DECAY
        rerank = _as_score_float(order_decay, 0.0)
    else:
        kind = _SCORE_KIND_RERANK
        rerank = _as_score_float(rerank)

    parts: list[tuple[float, float]] = [(w_r, rerank)]
    if w_e > 0:
        ev = ref.get("_evidence_score")
        if ev is not None:
            parts.append((w_e, _as_score_float(ev)))
    if w_s > 0:
        subj = ref.get("subject_score")
        if subj is not None:
            parts.append((w_s, _as_score_float(subj)))

    total = sum(w for w, _ in parts)
    if total <= 0:
        # 三个权重全 0（配置错误）：退化为 rerank 主轴，避免全池同分。
        return rerank, kind
    return sum(w * v for w, v in parts) / total, kind


def _budget_pack(
    refs: list[dict], max_chars: int,
) -> tuple[list[dict], int, int]:
    """按字符预算**贪心装箱**：跳着装不下的，继续用后面的填（§6 S4「截断单点化」）。

    调用方必须已按分数降序排好 `refs`——本函数只负责装箱，不排序。

    为什么不是「超预算即停」（改造前的 `break`）：
    候选按统一分降序，一个低分大 chunk 排在前面时，`break` 会把它**其后全部**
    候选一并挡在 prompt 外。实测（2026-09-15，`kb_demo_hw_inet_finance`）单个
    chunk 最大 2875 字符，而预算只有 12000 ——「上市公司有哪些」这一问 19 条
    候选里第 1 条就是 2875 字符，只要再有一条大的排在前面，尾部的高分小 chunk
    （实测最小 120 字符）就全部进不来。`continue` 只跳过装不下的那一条。

    Returns:
        (kept, total_chars, skipped)：skipped 为因装不下被跳过的**条数**，
        供调用方判定是否要告警（正常情况应为 0 或个位数）。
    """
    kept: list[dict] = []
    total = 0
    skipped = 0
    for r in refs:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        if total + len(text) > max_chars:
            skipped += 1
            continue
        kept.append(r)
        total += len(text)
    return kept, total, skipped


def _order_aware_sort(refs: list[dict]) -> list[dict]:
    """按统一分降序排序。**真分与位次分分段**，锁序候选以**组**为单位参与竞争。

    ── 为什么要分段（2026-09-15 实测修正）──
    `_unified_score` 里 `rerank` 缺项时用位次衰减顶上（`score_kind="order_decay"`），
    本意是「主轴不可用时的显式降级，只排序不判阈值」。**全池**都缺真分时这是对
    的（位次分严格递减 + 稳定排序 = 保原序）。但**混池**时两类分数会直接比大小，
    而二者的值域分布完全不同：

      · `order_decay = 1 - i/n` 铺满整个 (0, 1]，排在第 3 位就是 0.93；
      · 真 rerank 分实际极度集中在 0 附近（实测 top1 0.96，其余多在 0.001~0.04）。

    于是一条排在第 3 位、**根本没被 rerank 评过**的候选，会碾压一条 rel=0.46 的
    真分候选。统一分自己犯了它要治的「量纲不通约」病（§2.3 ④）。

    混池不是罕见情形——它是本体路径的**常态**：`_ontology_refs` 的候选里只有
    S1 正文回捞拿到正文的那部分才评得了 rerank。实测（`kb_demo_hw_inet_finance`）：

      · `华为电力企业数字化平台包含什么`  池 21 条，6 条带真分 → P1 顺序置换 **86%**
      · `南方电网数字电网蓝图讲了什么`     池 34 条，9 条带真分 → P1 顺序置换 **97%**
      · `设备运行维护规程有哪些要求`       池 46 条，0 条带真分 → 顺序置换 **0%**（对照）

    修正口径：**真分候选整体在前（段内按分降序），位次候选保持原序整体在后**。
    三种退化情形都与改造前逐字等价：
      · 全真分 → `sorted(score, reverse=True)`；
      · 全位次 → 保原序；
      · 无锁序混池（唯一的行为变化点）→ 真分上浮，位次段不再参与比大小。

    ── 排序豁免（§3.4）──
    分组键是 `_order_group`（`_ontology_refs` 在收集阶段打的标记）：同键候选构成
    一组，**组内保持产出序**，组的排位取**组内最高分**。

    为什么锁的是「组内相对序」而不是「这些候选整个不动」：豁免要保护的是行序 /
    步序不被重排（枚举题整行缺失、步骤链顺序错乱），不是让它们永远沉底或顶头。
    组作为一个整体与普通候选比大小，才是「统一分只用于**组间**排序」的字面含义。

    组跨段时**整组归真分段**（组内只要有任一成员带真分）：锁序组被拆到两段之间
    会破坏「组内保序」要保护的连续性，行序 / 步序照样会乱。
    """
    _scored: list[dict] = []
    _decayed: list[dict] = []
    for r in refs:
        if r.get("score_kind") == _SCORE_KIND_RERANK:
            _scored.append(r)
        else:
            _decayed.append(r)

    if not any(r.get("_order_group") for r in refs):
        _scored.sort(key=lambda r: r.get("score", 0.0), reverse=True)
        return _scored + _decayed

    # blocks: (组分数, 组首在原列表的下标, 成员列表, 是否真分段)；按组首出现序
    # 追加，末段稳定排序即可保证「同分保原序」与原实现一致。
    blocks: list[tuple[float, int, list[dict], bool]] = []
    group_at: dict[str, int] = {}
    for i, r in enumerate(refs):
        g = r.get("_order_group")
        s = r.get("score", 0.0)
        is_scored = r.get("score_kind") == _SCORE_KIND_RERANK
        if not g:
            blocks.append((s, i, [r], is_scored))
            continue
        at = group_at.get(g)
        if at is None:
            group_at[g] = len(blocks)
            blocks.append((s, i, [r], is_scored))
        else:
            best, first, items, scored = blocks[at]
            items.append(r)
            blocks[at] = (max(best, s), first, items, scored or is_scored)
    # True > False → reverse 下真分段整体在前；段内按组分数降序，同分保原序
    blocks.sort(key=lambda b: (b[3], b[0]), reverse=True)
    return [r for _best, _first, items, _scored in blocks for r in items]


def _rerank_coverage(refs: list[dict]) -> float:
    """候选池里带 rerank 真分的比例（§3.2 统一分的前置条件）。

    ⚠️ 判据是 `relevance` **字段是否存在**，不是取值大小：`_prellm_rerank_chunks`
    只给真正送去打分的候选写该字段（S4 起，见其内注释），`relevance == 0.0` 是
    「打了分且真不相关」的可信真分，绝不能算作缺项。
    """
    if not refs:
        return 0.0
    return sum(1 for r in refs if r.get("relevance") is not None) / len(refs)


def _apply_unified_score(
    raw_refs: list[dict],
    *,
    w_r: float | None = None,
    w_e: float | None = None,
    w_s: float | None = None,
) -> list[dict]:
    """给整条候选池算统一分并按分数降序重排；开关关闭时**原样返回**。

    落两个字段：`score`（统一分）与 `score_kind`（`rerank` / `order_decay`）。
    旧分数链（`final_score` / `group_final_score` / `_order_score` /
    `_evidence_score`）**并行输出、一字不改**——S3 要求保留旧字段跑一版做对照
    （P1/P2/P3 每一阶段都要有可比基线），清理留给 S9（单独提交）。

    排序见 `_order_aware_sort`：**真分与位次分分段**——全池同档时退化为
    「全真分按分降序 / 全位次保原序」，混池时真分段整体在前、位次段保原序
    沉底（避免位次分与真分直接比大小，§2.3 ④ 量纲不通约）。
    """
    if not RAG_UNIFIED_SCORE_ENABLED or not raw_refs:
        return raw_refs

    # ── rerank 真分覆盖率护栏（见 `RAG_UNIFIED_SCORE_MIN_COVERAGE` 处长注释）──
    # 覆盖率不足时统一分只剩 `order_decay`（= 池位次本身）可排，排序结果与不排
    # 等价；此时**跳过**并保原池序，且必须出声——这条链过去是全静默的（无正文
    # →不打分→退化为位次），排查时从日志里完全看不出来。
    _cov = _rerank_coverage(raw_refs)
    if _cov < RAG_UNIFIED_SCORE_MIN_COVERAGE:
        logger.warning(
            "[unified_score] 跳过重排：rerank 真分覆盖率 %.1f%% < 阈值 %.1f%%"
            "（%d/%d 条有 relevance），本池只剩位次分可排、重排与不排等价。"
            " 最常见原因：ONTOLOGY_CHUNK_TEXT_PREFETCH_ENABLED=false 致本体候选"
            "无正文，`_prellm_rerank_chunks` 在 texts<=1 时提前返回未打分",
            _cov * 100, RAG_UNIFIED_SCORE_MIN_COVERAGE * 100,
            sum(1 for r in raw_refs if r.get("relevance") is not None),
            len(raw_refs),
        )
        return raw_refs

    _w_r = RAG_SCORE_W_RERANK if w_r is None else w_r
    _w_e = RAG_SCORE_W_EVIDENCE if w_e is None else w_e
    _w_s = RAG_SCORE_W_SUBJECT if w_s is None else w_s

    n = max(1, len(raw_refs))
    for i, r in enumerate(raw_refs):
        r["score"], r["score_kind"] = _unified_score(
            r, w_r=_w_r, w_e=_w_e, w_s=_w_s, order_decay=1.0 - i / n,
        )
    ranked = _order_aware_sort(raw_refs)
    _locked = sum(1 for r in ranked if r.get("_order_group"))
    _n_scored = sum(1 for r in ranked if r.get("score_kind") == _SCORE_KIND_RERANK)
    logger.info(
        "[unified_score] %d 条候选按统一分重排 (w_r=%.2f w_e=%.2f w_s=%.2f) "
        "top3=%s kind=%s 真分/位次=%d/%d%s",
        len(ranked), _w_r, _w_e, _w_s,
        [round(r.get("score", 0.0), 3) for r in ranked[:3]],
        ranked[0].get("score_kind") if ranked else None,
        _n_scored, len(ranked) - _n_scored,
        f" 锁序豁免 {_locked} 条（§3.4：组内保序、组间比大小）" if _locked else "",
    )
    return ranked


def _first_fact_doc_id(fact: dict) -> str | None:
    """取单个 fact 的首个 doc_id（target_entity.source_chunks[].file_path 优先，
    退化为 target_entity.doc_ids）。

    与 `_ontology_refs` 的收集口径一致：file_path 可能是 `<SEP>` 连接的多值，
    逐段 parse_file_source 取第一个能解析出 doc_id 的段。
    """
    te = fact.get("target_entity") if isinstance(fact, dict) else None
    if isinstance(te, dict):
        sc = te.get("source_chunks")
        if isinstance(sc, list) and sc:
            for s in sc:
                fp = s.get("file_path") if isinstance(s, dict) else None
                if fp:
                    for seg in fp.split("<SEP>"):
                        seg = seg.strip()
                        if not seg:
                            continue
                        parsed = parse_file_source(seg)
                        if parsed and parsed.get("doc_id"):
                            return parsed["doc_id"]
        for did in te.get("doc_ids") or []:
            if did:
                return did
    return None


def _facts_number_to_doc_id(facts: list[dict] | None) -> dict[str, str]:
    """从本体 facts 构建 编号 → doc_id 映射，与 answer_from_facts 的 `_no` 编号对齐。

    answer_from_facts 给每个 relation fact 分配 1-based `_no` 并让 LLM 以 [ref_i]
    引用；本函数按相同索引提取每个 fact 的首个 doc_id，供 normalize_citations
    把 [ref_i] 重排为 references 下标 [n]。
    """
    mapping: dict[str, str] = {}
    for i, f in enumerate(facts or [], 1):
        did = _first_fact_doc_id(f)
        if did:
            mapping[str(i)] = did
    return mapping


# `[ref_N]` 形态（answer_from_facts 分配、normalize_citations 消费）。与纯 `[N]`
# 区分：后者可能是年份/列表等普通文本，`normalize_citations` 对两者处理不同
# （`[ref_N]` 映射不到会删、纯 `[N]` 保留原样），白名单也必须按同一口径解析。
#
# ⚠️ [jonex] 2026-09-23 评审 I1：**必须与消费方的正则逐字对齐**
# （`jonex_core/common/ontology_llm.py:44` 的 `\[(ref[-_]?)?(\d+)\]`）。消费方把
# `ref` 后缀的三种写法 `[ref_1]` / `[ref-1]` / `[ref1]` 一律判成 `[ref_N]` 形态
# （该文件 `:170` 只判 `group(1) is not None`），映射不到就**删**。白名单若窄于此
# （旧写法 `\[ref_(\d+)\]` 只认下划线一种），LLM 写出 `[ref-1]` / `[ref1]` 时
# 保护不到 → 该 doc 可能被裁 → 引用标被静默抹空，**正是白名单要防的那条回归**。
# 仍不匹配纯 `[1]` / `[2024]`（无 `ref` 前缀），与消费方的 `is_ref` 判定一致。
_REF_MARKER_RE = re.compile(r"\[ref[-_]?(\d+)\]")

# 答案正文里的数值（`_answers_agree` / `_answer_numbers` 共用口径）。
_NUM_RE = re.compile(r"\d+\.?\d*")


def _cited_doc_ids_from_map(
    answer: str, number_to_doc: dict[str, str],
) -> set[str]:
    """从答案解析 `[ref_N]` → 用给定映射 → doc_id 集合（纯函数）。

    `number_to_doc` 的两种来路（两条路径同构，2026-09-23 Task 6）：
      · 本体：`_facts_number_to_doc_id(facts)`（answer_from_facts 的 `[ref_i]` 编号）；
      · RAG：`{str(i): chunk["doc_id"] for i, chunk in enumerate(budgeted, 1)}`
        （answer_from_chunks 的 `[ref_i]` 编号 = budgeted 位次）。

    ⚠️ 只认 `[ref_N]`（含 `[ref-1]` / `[ref1]` 两种变体，见 `_REF_MARKER_RE`），
    不认纯 `[N]`：后者可能是年份/列表，混进来会误保护/误保留一大批无关 doc。
    """
    if not answer or not number_to_doc:
        return set()
    return {number_to_doc[n] for n in _REF_MARKER_RE.findall(answer)
            if n in number_to_doc}


def _cited_doc_ids(answer: str, facts: list[dict] | None) -> set[str]:
    """从答案正文解析 `[ref_N]` → fact 编号 → doc_id 白名单（纯函数）。

    用途：`_prune_low_relevance_refs` 的 `protect_doc_ids` —— 答案引用过的 doc
    **必须保留**，否则它在 `normalize_citations` 的 `doc_to_idx` 里消失，答案里
    引用它的 `[ref_N]` 会被静默抹成空串。

    ⚠️ 只认 `[ref_N]`，不认纯 `[N]`：后者由 LightRAG 路径产生（数字即 `[n]`），
    且可能是年份/列表，混进来会误保护一大批无关 doc、把裁剪废掉。

    本函数只是 `_cited_doc_ids_from_map` + facts 编号映射的封装（Task 6 抽公共
    实现）——**行为逐字不变**，Task 2-5 的白名单语义不变。
    """
    return _cited_doc_ids_from_map(answer, _facts_number_to_doc_id(facts))


def _cited_fact_numbers(answer: str) -> set[int]:
    """从答案正文解析 `[ref_N]` → fact 编号集合（1-based，纯函数）。

    与 `_cited_doc_ids` 同源（同一个 `_REF_MARKER_RE`），区别只在**返回编号本身
    而不是映射到的 doc_id**：置信判据要的是「答案依据了哪几条 fact」，doc 粒度
    会把它碾平（多条 fact 可同属一个 doc）。

    ⚠️ 必须在 `normalize_citations` **之前**取：归一化会把 `[ref_3]` 重写成
    `[2]`（references 下标），fact 编号语义随之丢失，事后无法还原。
    """
    if not answer:
        return set()
    return {int(n) for n in _REF_MARKER_RE.findall(answer)}


def _filter_refs_to_cited(
    answer: str, refs: list[dict], number_to_doc: dict[str, str],
) -> tuple[list[dict], set[str]]:
    """只保留答案引用到的 doc 对应的 references（**保序**、纯函数）。

    Task 6（2026-09-23）：「答案没用到的别列」。与按分数裁的
    `_prune_low_relevance_refs` 独立、可叠加。**必须先于 `normalize_citations`**
    —— 先滤后归一，答案里的 `[ref_i]` 自动落到新下标，**不改写答案文本**
    （见 `RAG_REF_CITED_ONLY_ENABLED` 的注释）。

    **不做开关判断**：开关放在调用点（`RAG_REF_CITED_ONLY_ENABLED`），
    本函数才能独立测试，也避免「开关忘了传 → 看起来永远是开的」。

    两条兜底（都要）：LLM 偶尔不标引用，此时把引用列表整块清空是比「多列几条」
    严重得多的退化。
      · `cited` 为空（答案没引用标 / 标号映射不到任何 doc）→ **全留**；
      · 全滤光（`kept` 为空但 `refs` 非空）→ **全留**。

    ⚠️ 无 `doc_id` 的条目判为「未被引用」（会被滤掉）：`normalize_citations` 的
    `doc_to_idx` 同样收不到它（`if did and ...`）→ 答案根本引用不到它。

    Args:
        answer: 答案正文（含 `[ref_N]` 引用标）。
        refs: 聚合后的引用列表（保序输入、保序输出）。
        number_to_doc: `[ref_N]` 的 N → doc_id 映射。

    Returns:
        `(kept, cited)`；`cited` 供调用点判「兜底是否触发」与埋点用。
    """
    cited = _cited_doc_ids_from_map(answer, number_to_doc)
    if not refs or not cited:
        return list(refs), cited
    kept = [r for r in refs if r.get("doc_id") in cited]
    if not kept:
        # 答案引用的 doc 在引用列表里一条都没有（文档级兜底缺失 / 已被上游裁掉）
        # —— 全滤光就全留，绝不返回空列表。
        return list(refs), cited
    return kept, cited


class SearchService:
    def __init__(self):
        self._history = SearchHistoryService()

    async def search(self, tenant_id: str, user_id: str, request: SearchRequest | dict, trace_id: str = "") -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchRequest(**_payload(request))
        start = time.perf_counter()

        # ── [jonex] kb_type 分流 ──
        kb_type = await self._get_kb_type(tenant_id, req.knowledge_base_id)
        if kb_type in LLM_WIKI_TYPES:
            if kb_type == "openkb":
                return await self._search_openkb(tenant_id, user_id, req)
            # [jonex] jonex_wiki 走专用链路（与 /search/jonex_wiki 端点、search_mix 的 jw 侧同源）。
            # 原先这里直接抛「即将上线」——但 DomainService.search 是**逐个 KB 调本方法**的，
            # 于是任何含 jonex_wiki KB 的领域服务检索都被判失败；纯 jonex_wiki 的服务更是
            # 全 KB 失败、返回「成功但全空」（无 answer 字段），前端渲染成「服务暂时不可用」。
            return await self.search_jonex_wiki(
                tenant_id, user_id,
                JonexWikiSearchRequest(
                    query=req.query, mode=req.mode, top_k=req.top_k,
                    knowledge_base_ids=[req.knowledge_base_id],
                    save_history=req.save_history,
                    domain_space_id=req.domain_space_id,
                ),
                trace_id=trace_id or None,
            )

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
        # [jonex] 引用归一化（LightRAG 原生路径）：剥掉答案末尾 References 尾注段，
        # 与 references 卡片去重。内联 [n] 暂不做重排（LightRAG 的 [n] 对应其自身
        # top-5 尾注、raw_refs 不携带 reference_id，无法可靠映射到聚合卡片）。
        answer = strip_trailing_references(answer)
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
            # [jonex] 数据运营二期：检索轨迹埋点。
            # 必须落在 save_history **之后** —— `answer_id` 取 `history.id`，
            # 那是事件表与检索历史之间唯一的关联键。本路径（纯 LightRAG）
            # 没有 ReasoningCollector，步骤事件自然为空，请求/命中/采用照写。
            await write_trace_events(
                tenant_id=tenant_id,
                request_id=trace_id or "",
                answer_id=result["history_id"],
                result=result,
                kb_ids=[req.knowledge_base_id] if req.knowledge_base_id else [],
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
            # [jonex] S3（§3.3 #4）：把统一分从 flat ref 落到 location 上，成为
            # DTO 的显式字段（`SourceLocation.score` / `score_kind`），不再靠
            # service 返回裸 dict 漏出的影子字段（`relevance` 等）。开关关闭时
            # `score` 键根本不存在，`SourceLocation.score` 保持 None——旧响应
            # 形状逐字不变。
            if r.get("score") is not None:
                loc["score"] = r["score"]
                loc["score_kind"] = r.get("score_kind")
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
                # [jonex] S4：统一分是真分（`rerank`）时直接拿它当 TopN 键——
                # 它才是整条候选池的排序依据，P2/P3 起还含证据/主体分量，比裸
                # `relevance` 更能代表「该不该留下这张图」。`order_decay` 时
                # 不走这里：位次占位分只是「排在这」，不如旧梯里的
                # `final_score`/`_evidence_score` 有语义，留给下面的旧梯处理。
                _unified = (
                    r.get("score")
                    if r.get("score_kind") == _SCORE_KIND_RERANK
                    else None
                )
                _img_score = (
                    _unified
                    if _unified is not None
                    else r.get("relevance")
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
                # [jonex] S4（§3.3 #5 ①）：第五条元素 = 统一分的 `score_kind`。
                # 统一分跑过时它**精确**表达旧 `_score_source` 想表达的事：
                #   rerank      = 有真语义相关性分（旧「语义来源」）
                #   order_decay = 只有位次占位分（旧「结构来源」）→ 不得判阈值
                # 且比旧判据更准——旧判据要同时排除四个分数键，漏一个就把
                # 「无真分」误判成「语义来源」。无统一分（键缺失）时为 None，
                # 阈值判定逐字回退旧双轨。
                ref.setdefault("_asset_cands", []).append(
                    (loc, r.get("asset_ext") or "", _img_score, _score_source,
                     r.get("score_kind"))
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
            # [jonex] 四屏修复 (b)：E3 是否真跑出语义分（供下方逐条阈值判定）——
            # 未跑/失败时结构分候选的分数仍是归一化证据权重（非 0~1 语义分），
            # 语义阈值对其无意义，必须豁免。
            _reranked = False
            # [jonex] 方案 G3：同分并列（继承组代表分）或全零（无分数来源）
            # → 无判别力，需 E3 逐张打分。单候选不触发（无排序意义）。
            _no_discrimination = len(set(_scores)) <= 1
            # [jonex] §G2：结构分来源（纯 _evidence_score）跳过语义阈值——
            # 证据权重是图谱距离信号，不是 0~1 语义相关性，不应被
            # RAG_IMAGE_REF_SCORE_MIN 误杀。只保留 TopN 截断。
            # [jonex] S4：判据统一走 `_img_structural`——统一分跑过时以
            # `score_kind` 为准（更准，见注册处注释），否则回退旧 `_score_source`。
            _structural = any(_img_structural(c) for c in cands)
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
                    # [jonex] 四屏修复 (b)：重建时保留来源标记 c[3]（评审 b1）——
                    # 逐条阈值判定需要知道候选是语义分还是结构分来源，不能像
                    # 之前那样重建 3 元组丢标记。
                    # [jonex] S4：E3 分同样是「图片描述 × query」的 reranker
                    # 相关性，与统一分主轴**同一个模型、同一个函数**，故重建时
                    # 把 `score_kind` 一并置为 `rerank`——它此刻确实是真分，
                    # 而下方的单阈值正是为该量纲标定的（见 `_img_min`）。
                    # ⚠️ 逐条判「这次 rerank 有没有返回它」，未返回的不得被
                    # 贴上真分标签——见 `_rebuild_after_e3` 的说明。
                    cands = _rebuild_after_e3(cands, score_by_idx)
                    _reranked = True
                    # [jonex] E3 段日志：分数分布 + 候选最低分 + 双阈值，
                    # 供灰度期复核阈值切在相关带还是噪声带（评审 b2/P4）。
                    # e3_lowest 是候选最低分（非阈值）；存活/被裁条数由下方
                    # 「图片配额裁剪 %d→%d」日志给出。
                    _e3_scores = sorted(
                        [score_by_idx.get(i, 0.0) for i in range(len(cands))],
                        reverse=True,
                    )
                    logger.info(
                        "[image-ref-quota] E3 rerank 完成 doc=%s query=%r "
                        "cands=%d scores=%s e3_lowest=%.4f sem_min=%.2f "
                        "struct_min=%.2f",
                        ref["doc_id"], query[:60], len(cands),
                        [round(s, 4) for s in _e3_scores[:6]],
                        _e3_scores[-1] if _e3_scores else 0.0,
                        RAG_IMAGE_REF_SCORE_MIN,
                        RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL,
                    )
            # 1) 按分数阈值过滤——逐条按来源取值（评审 b1：不再 doc 级连坐）：
            #    E3 已跑（_reranked）→ 所有候选分数都是 E3 分（图片描述×query
            #    相关性，系统性低于文本语义分），来源标签已无意义，统一用
            #    RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL（四屏修复 (b) 补丁：RAG
            #    路径语义来源 0.6915 被 0.7 误杀，与本体路径同批图同分却
            #    两路径阈值不一致；0.5 可切 0.22 噪声带——实测相关带 ≥0.69、
            #    噪声带 ≤0.22）；
            #    E3 未跑/失败 → 语义来源仍用 RAG_IMAGE_REF_SCORE_MIN（分数
            #    是 LightRAG relevance/final_score，分布不同：旧实测真相关
            #    ≥0.74，0.7 线有依据）；结构分来源豁免（分数仍是归一化证据
            #    权重，非语义分）——此豁免分支是 I4-a 之前的
            #    "结构分排序 + TopN"行为回退，勿删。
            #    [jonex] S4（§3.3 #5 ①「双阈值收敛为一个」）——统一分口径下
            #    上面这套双轨消失：候选自己的 `score_kind` 就是唯一的判据，
            #    阈值收敛为**一个** `RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL`。
            #
            #    为什么 0.5 而不是 0.7：统一分路径下所有图片分都来自
            #    rerank(图片描述 × query)——E3 与主池 rerank 是同一个模型同一个
            #    函数，实测相关带 ≥0.69 / 噪声带 ≤0.22（四屏修复 (b)）。
            #    0.7 来自另一条量纲（LightRAG relevance 混 λ 后的 final_score），
            #    正是当年把 0.6915 的同批图整批误杀的那个值；0.5 切两带之间，
            #    对统一分与 E3 分同时成立。全量切换后 `RAG_IMAGE_REF_SCORE_MIN`
            #    在图片判定上退役（S9 清理），届时 `_STRUCTURAL` 这个名字也
            #    该一并改掉——此处保留旧名只为不在灰度期同时改值又改名。
            def _img_min(src: str, kind: str | None) -> float:
                return _img_min_score(src, kind, _reranked)

            cands = [
                (loc, ext, sc) for loc, ext, sc, src, *rest in cands
                if _img_min(src, rest[0] if rest else None) <= 0
                or sc >= _img_min(src, rest[0] if rest else None)
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
                # [jonex] 评审 b2：升 info 并带双阈值，供灰度期复核
                # 「结构分阈值切在哪一档、语义阈值裁了多少」有据可依。
                logger.info(
                    "[image-ref-quota] doc=%s 图片配额裁剪: %d→%d "
                    "(语义阈值=%.2f, 结构分阈值=%.2f, topN=%d)",
                    ref["doc_id"], orig_count, len(cands),
                    RAG_IMAGE_REF_SCORE_MIN, RAG_IMAGE_REF_SCORE_MIN_STRUCTURAL,
                    RAG_IMAGE_REF_MAX,
                )

        # [jonex] §image-refs P2-1: 图片资产 asset_url 富化。key 派生走
        # build_asset_key（ext 白名单归一，路径穿越免疫）；aext 缺失 =
        # 上传失败/开关关闭，跳过不富化（前端按无 asset_url 降级）。
        # 整段 best-effort：任何异常只影响图片预览，不阻断引用产出。
        asset_jobs: list[tuple[str, dict, str]] = []  # (doc_id, loc, ext)
        for ref in agg.values():
            for loc, ext, _score, *_rest in ref.pop("_asset_cands", []):
                # [jonex] §image-refs B2：把 asset_ext 透出到 image location，
                # 随引用响应序列化并持久化到历史快照，供前端 resolve 回传后
                # 重新推导对象键、生成新的 asset_url（见 resolve_references）。
                if ext:
                    loc["asset_ext"] = ext
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
            # [jonex] S3：doc 级统一分取**组内 location 最高分**（§3.3 #4）。
            # 取最高而非代表文本打分：已删除的旧引用重排只拿
            # `locations[0].text[:1024]` 当代表（自承是已知取舍），统一到
            # chunk 级打分后该取舍消失——一个 doc 的分数就是它最好那条证据的
            # 分数。图片配额已在上方裁剪过 locations，此处读到的是存活集合。
            _loc_scored = [
                (loc.get("score"), loc.get("score_kind"))
                for loc in ref["locations"] if loc.get("score") is not None
            ]
            _best = max(_loc_scored, key=lambda t: t[0], default=None)
            out.append({
                "doc_id": did,
                "kb_id": d.knowledge_base_id,
                "kb_name": kb_names.get(d.knowledge_base_id) if d.knowledge_base_id else None,
                "file_name": d.file_name,
                "mime_type": d.mime_type,
                "file_size": d.file_size,
                "media_type": classify_media(d.mime_type, d.file_name),
                "raw_url": raw_url,
                "score": _best[0] if _best else None,
                "score_kind": _best[1] if _best else None,
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
    def _detect_procedural_intent(query: str) -> bool:
        """[jonex] P8（M3-b）：procedural 意图检测（如何/步骤/流程/下一步…）。"""
        if not query:
            return False
        return any(p.search(query) for p in _PROCEDURAL_PATTERNS)

    @staticmethod
    def _resolve_intent_priority(
        procedural_intent: bool, enum_intent: bool,
    ) -> tuple[bool, bool]:
        """[jonex] P8（M3-b）：procedural 优先于 enum。

        「配置流程的所有步骤」同时命中 enum 第 4 条（所有）与 procedural
        （步骤）——步骤类问法以步骤链模板为准，enum 判定不生效
        （collector 在调用处记录原因）。
        """
        if procedural_intent:
            return True, False
        return procedural_intent, enum_intent

    @staticmethod
    def _build_inverse_map(schema: dict | None) -> dict[str, str]:
        """[jonex] P8（M3-a）：从 compiled schema 建 relation_name → inverse_of 映射。

        只收有 inverse_of 声明的关系；空 schema / 无声明 → {}（零变化）。
        """
        rels = (schema or {}).get("relation_types") or []
        return {
            rt["name"]: rt["inverse_of"]
            for rt in rels
            if isinstance(rt, dict) and rt.get("inverse_of") and rt.get("name")
        }

    @staticmethod
    def _expand_inverse_relations(
        facts: list[dict], inverse_map: dict[str, str],
    ) -> list[dict]:
        """[jonex] P8（M3-a）：direction=="incoming" 的 fact 按 inverse 映射
        把 relation_type 翻译成逆名并同步 relation_chain 最后一跳。

        无映射时返回原列表引用（零变化）；不改 direction（遍历方向是
        既成事实，与 hop/path 保持一致）。
        """
        if not inverse_map or not facts:
            return facts
        out: list[dict] = []
        for f in facts:
            if f.get("direction") != "incoming":
                out.append(f)
                continue
            rel = f.get("relation_type", "")
            inv = inverse_map.get(rel)
            if not inv:
                out.append(f)
                continue
            nf = dict(f)
            nf["relation_type"] = inv
            chain = list(f.get("relation_chain") or [])
            if chain:
                chain[-1] = inv
                nf["relation_chain"] = chain
            out.append(nf)
        return out

    @staticmethod
    def _should_exempt_min_facts(procedural_intent: bool, root_type: str) -> bool:
        """[jonex] P8（M3-f）：procedural 意图且 root 命中单个 Action 时
        豁免 ONTOLOGY_MIN_FACTS（单 Action 常只有 2-3 条边，低于运维阈值）。
        """
        return bool(procedural_intent) and root_type == "Action"

    @staticmethod
    def _enumeration_applies(query: str, matched_entities: list[dict]) -> bool:
        """[jonex] S5 域限定：枚举处置仅针对表格行实体（extraction_method=row_as_object）。

        S5 设计源头是表格解析枚举题（整行字段召回不全 → 强制走 RAG 取原始表格行），
        因此非表格问题（如「Skill 有哪些步骤」）不做枚举降级，正常走本体作答；
        M3-b：procedural 问法优先，命中「如何/步骤/流程」时 enum 不生效。
        """
        if not query or not matched_entities:
            return False
        if SearchService._detect_procedural_intent(query):
            return False
        if not SearchService._detect_enumeration_intent(query):
            return False
        return any(
            m.get("extraction_method") == "row_as_object" for m in matched_entities
        )

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
        # [jonex] P8（M3-c）：procedural 分支直接产出 facts（跳过通用行转换）
        prebuilt_facts: list[dict] | None = None

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

            elif intent == "procedural":
                # [jonex] P8（M3-c）步骤链专用模板：repository 侧已产出
                # 与 neighbors() 同构的 facts（step_no 有序），此处透传。
                root_name = params.get("root_name", "")
                root_type = params.get("root_type", "")
                if not root_name or root_type not in ("Skill", "Action"):
                    return None
                template_name = "find_action_chain"
                cypher_desc = (
                    f"MATCH {root_type} 步骤链"
                    + (" HAS_STEP（step_no 升序）" if root_type == "Skill"
                       else " NEXT_STEP（深度序号）")
                    + " + 5 挂接展开"
                )
                prebuilt_facts = await gdao.find_action_chain(
                    tenant_id, kb_id, root_name, root_type,
                )

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

        if prebuilt_facts is not None:
            # [jonex] P8（M3-c）：procedural 分支 facts 已在构造侧有序产出
            if collector:
                collector.step(
                    STAGE_TIMELINE_GRAPH, "图查询模板",
                    summary=(
                        f"意图「{intent}」命中步骤链模板 {template_name}，"
                        f"取到 {len(prebuilt_facts)} 条事实"
                    ),
                    detail={
                        "intent": intent,
                        "template": template_name,
                        "cypher": cypher_desc,
                        "params": params,
                        "hits": len(prebuilt_facts),
                        "top_fact": (prebuilt_facts[0].get("target")
                                     if prebuilt_facts else None),
                    },
                    t_start=t,
                )
            return prebuilt_facts

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

        # [jonex] 四屏修复 (c)：图片锚点 chunk（带 image_idx）价值在锚点而非
        # 正文——正文是 MinerU JSON 结构（高 ASCII 占比触发 english 误杀、
        # 版面主题可能 <20 字触发 empty_shell 误杀），不参与文本质量判定，
        # 直接放行。无锚点的图片/表格 chunk 仍走原判据（纯英文 VLM 描述的
        # 纵深防御保留，RAG_PROMPT_LANG 配错场景）。
        # 锚点来源二选一：file_source 串解析（本体路径 refs），或结构化
        # refs 的 image_idx 字段（query_detailed DTO——file_path 是纯文件名，
        # 解析不出锚点；评审后复测发现 4 个四屏图片描述仍被 english 误杀）。
        img_idx = parsed.get("image_idx")
        if img_idx in (None, ""):
            img_idx = ref.get("image_idx")
        # [jonex] `chart` 与 `image` 同为**带图资产的契约类**（引擎侧 2026-09-26 起共用
        # `image_idx` 序数空间，见 `chunker.IMAGE_ASSET_CTYPES`）：正文都是 VLM 产出、
        # 价值都在锚点。只认 image 会让 chart 正文重新落回下面的判据（过短/纯英文都杀）。
        if img_idx not in (None, "") and ctype in ("image", "chart"):
            return ""

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
        # [jonex] `chart` 同属「VLM 产的图片描述」（它就是那张图的描述），同型过滤。
        # 原因码仍归 `english_image_description` —— 别让它落成 `english_table_summary`，
        # 那会把「图表描述」误记成「表格摘要」，日志里分不出来。
        if ctype in ("table_summary", "image", "chart"):
            ascii_count = sum(1 for ch in cleaned if ord(ch) < 128)
            if ascii_count / max(1, len(cleaned)) > 0.8:
                return (
                    "english_table_summary"
                    if ctype == "table_summary"
                    else "english_image_description"
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
        already_reranked: bool = False,
        group_cap: int | None = None,
    ) -> tuple[list[dict], dict[str, Any]]:
        """方案 B 主流程（§5.4/§5.5）：doc 聚合 → rerank → 主体软融合 → top-K 软截断。

        核心原则：主体信号只影响排序，**永不删除候选**。

        - B4 聚合键为 `(doc_id, chunk_type)`（表格方案改动 63：防止同文档的
          摘要 chunk 吃掉明细行；存量 chunk 无 ctype → 归 (doc_id, None) 组，
          等价于按 doc_id 聚合）。
        - rerank 关闭或失败 → relevance 缺失，final = subject_score，仅排序。
        - 主体全不中 → λ 置 0（信号作废），只按相关性排序，日志 warning 记明。
        - 截断只发生在候选组数超过 `group_cap` 时（组级截断后展开）。

        [jonex] S4 新增两个入参——`_rag_platform_answer` 改为「先整池 rerank、
        再做主体融合」后，本方法不再是 rerank 的唯一入口：

        Args:
            already_reranked: 调用方已对**全池**跑过 rerank，每条 ref 都带自己的
                `relevance`。此时本方法跳过内部 rerank（否则重复调用一次网关），
                且组代表由其成员中 `relevance` 最高者充当，组员因带真分而不再
                退化成 `order_decay` 占位（P0-2「组员不继承代表分」在**全池已
                打分的口径下不再需要兜底**——它当年是为了防止组内同分，现在每条
                组员有自己的分，同分问题自然消失）。
            group_cap: 组级截断上限。`None` 时**调用时**读 `RAG_PRELLM_RERANK_TOPK`
                （旧调用点行为逐字不变，且保持可被 env/monkeypatch 覆盖——写成
                默认参数值会在导入时绑死、后续改模块常量不再生效）；S4 的调用
                点显式传 `RAG_RERANK_POOL_MAX`，使截断在正常池规模下不触发——
                截断单点化到预算那一步。

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
        # [jonex] S4：调用方已整池 rerank 过就跳过——重复调用网关是纯浪费，
        # 且会把本来带真分的组员再覆盖一遍（无害但无谓）。
        if already_reranked:
            rerank_applied = any(r.get("relevance") is not None for r in reps)
        elif RAG_PRELLM_RERANK_ENABLED and len(reps) > 1:
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
        # [jonex] S4：截断上限改由调用方给定（`None` 维持旧行为）。S4 调用点传
        # `RAG_RERANK_POOL_MAX`，正常池规模下这条 `if` 不成立 —— 组级择优截断
        # 让位给预算单点截断（§6「截断单点化」）。
        _cap = RAG_PRELLM_RERANK_TOPK if group_cap is None else group_cap
        if RAG_PRELLM_RERANK_ENABLED and len(reps_sorted) > _cap:
            reps_sorted = reps_sorted[:_cap]
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
        prescored: dict | None = None,
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
            # [jonex] C7 修复（2026-09-18）：**不得静默**。这两处提前返回与
            # `RAG_UNIFIED_SCORE_ENABLED` 的覆盖率护栏（§8-13）是一对——护栏
            # 只能在「跑过本函数」之后才发现全池无真分；在此提前返回则连护栏
            # 都不触发，池里 `relevance` 全缺 → 下游 `_unified_score` 整池落
            # `order_decay`，而调用方看不出为什么 P1/P2/P3 无差别（§8.3 吃过
            # 一次：`ONTOLOGY_CHUNK_TEXT_PREFETCH_ENABLED=false` 致覆盖率 0，
            # 归因绕了三轮）。
            if RAG_UNIFIED_SCORE_ENABLED:
                logger.warning(
                    "[prellm_rerank] 提前返回（候选 %d ≤ 阈值 %d）→ 本次无 rerank "
                    "真分，统一分将整池落 order_decay（P1/P2/P3 无差别）；query=%r",
                    len(raw_refs), _threshold, (query or "")[:80],
                )
            return raw_refs

        # [jonex] 分数复用（§ 同查询内本体候选被打两遍 rerank）：
        # `_ontology_refs` 的 S4 已经给本体那批打过一次分，L0 统一池把它们并进
        # 更大的池后又打一次。两次输入**完全相同**——同一个 query、同一份正文
        # （实测 RAG 侧 `text` 与 `get_doc_chunks` 的 `content` 60/60 逐字相同）、
        # 同一个模型；网关又是纯透传（只分批 + `index` 重编号，无归一化、无跨批
        # 聚合）→ 同一篇文档的分数必然相同。
        # 故命中的只补 `relevance`、**不再送去上游**，下游排序/统一分照常。
        # 「跳过」按**位次**精确记，判据是「这条是本次从 `prescored` 填进来的」，
        # 不是「这条有没有 relevance」。后者把正确性押在「没人会带 relevance 进
        # 函数」这个**未被断言**的前提上：今天成立（`relevance` 全仓只有 rerank
        # 结果与 prescore 两处写），但一旦有 ref 自带（例如 LightRAG 原生分混入），
        # 它就会冒充 reranker 分——正是 S4 改造要消灭的量纲越位，且只在本体路径
        # 触发、极隐蔽。
        _reused: set[int] = set()
        if prescored:
            for _i, r in enumerate(raw_refs):
                if r.get("relevance") is not None:
                    continue
                _k = (r.get("doc_id"), r.get("chunk_index"))
                if _k in prescored:
                    r["relevance"] = prescored[_k]
                    _reused.add(_i)
        _prescored_hits = len(_reused)

        # 提取每个 ref 的代表文本（chunk 原文）——已复用到分数的不再送上游
        texts: list[str] = []
        indices: list[int] = []
        for i, r in enumerate(raw_refs):
            if i in _reused:
                continue
            txt = r.get("text") or r.get("content") or ""
            if txt:
                texts.append(txt[:1024])
                indices.append(i)

        # ⚠️ 有复用命中时**不套用**「候选太少不值得调一次 rerank」的阈值：池里
        # 115 条只剩 1 条待打分，正是**该**调的那种情形（其余分数已在手）。
        _skip_rerank = (not texts) if _prescored_hits else (len(texts) <= _threshold)
        if _skip_rerank:
            if _prescored_hits:
                # 全部命中复用分数 → 不打分，但**仍须按 relevance 排序返回**
                # （调用方依赖这个顺序，不能退回原序）。
                ranked = sorted(
                    raw_refs, key=lambda r: r.get("relevance", 0.0), reverse=True)
                kept = ranked[:RAG_PRELLM_RERANK_TOPK] if truncate else ranked
                logger.info(
                    "[prellm_rerank] query=%s total=%d 复用分数=%d 送去打分=%d "
                    "kept=%d truncate=%s（零上游调用）",
                    query[:80], len(raw_refs), _prescored_hits, len(texts),
                    len(kept), truncate,
                )
                return kept
            # 走到这里且无复用命中 ⟺ 「有正文者 ≤ 阈值」（见 `_skip_rerank` 的算式）
            if RAG_UNIFIED_SCORE_ENABLED:
                logger.warning(
                    "[prellm_rerank] 提前返回（有正文者 %d ≤ 阈值 %d，候选 %d 条）"
                    "→ 无 rerank 真分；常见原因是 S1 正文回捞未开/未命中"
                    "（`ONTOLOGY_CHUNK_TEXT_PREFETCH_ENABLED`）；query=%r",
                    len(texts), _threshold, len(raw_refs), (query or "")[:80],
                )
            return raw_refs

        try:
            results = await rerank(
                query, texts, tenant_id=tenant_id,
                kb_id=kb_id or None, trace_id=trace_id, user_id=user_id,
            )
            if not results:
                # [jonex] rerank 静默失效是「本批 relevance 全部缺失 → 统一分
                # 退化成 order_decay」的首要原因，且没有异常可查（模型名不符时
                # 网关返回 200 + 空 results，见 jonex_core/common/rerank.py）。
                # 没有这行日志，`score_kind` 的失真在链路上是不可见的。
                logger.warning(
                    "[prellm_rerank] reranker 返回空结果（%d 条候选）→ "
                    "本批 relevance 全部缺失，统一分退化为 order_decay；query=%r",
                    len(raw_refs), query[:80],
                )
                return raw_refs
            score_by_idx = {x["index"]: x.get("relevance_score", 0.0) for x in results}
            # [jonex] S4：`relevance` **只写给真正参与 rerank 的条目**。
            #
            # 改造前是无差别赋值 `raw_refs[i]["relevance"] = score_by_idx.get(pos, 0.0)`，
            # 无正文的候选（不在 `indices` 里、压根没送去打分）会被写成 `0.0`。
            # 后果有两个，且都伤及统一分：
            #   1. `_unified_score` 把 `0.0` 当**真分**（见 S3 单测
            #      `test_zero_relevance_is_real_score_not_missing`）——「缺项」
            #      被当成「真不相关」，正是 §3.2 明令禁止的「缺失即 0」；
            #   2. `score_kind` 被谎报成 `rerank`，下游无法区分「打过分的低分」
            #      与「没打分的占位」。
            # 留空则自然走缺项分支：主权重分配、`kind="order_decay"`。
            # （顺带把原来 `next(...)` 的 O(n²) 查找换成一次建表。）
            _pos_of = {idx: pos for pos, idx in enumerate(indices)}
            for i in range(len(raw_refs)):
                pos = _pos_of.get(i)
                if pos is None:
                    continue
                raw_refs[i]["relevance"] = score_by_idx.get(pos, 0.0)
            # 按 relevance 降序排序，取 top-K
            ranked = sorted(raw_refs, key=lambda r: r.get("relevance", 0.0), reverse=True)
            kept = ranked[:RAG_PRELLM_RERANK_TOPK] if truncate else ranked
            logger.info(
                "[prellm_rerank] query=%s top3_scores=%s total=%d scored=%d kept=%d truncate=%s",
                query[:80],
                [round(r.get("relevance", 0), 3) for r in ranked[:3]],
                len(raw_refs), len(texts), len(kept), truncate,
            )
            return kept
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

    async def _load_doc_map(self, tenant_id: str, doc_ids: list[str]) -> dict:
        """[S1] 批量预查 doc_id → KnowledgeDocument 映射。

        两处用途，共用一次查询：
        1. 解析每个 doc 各自真实的 kb_id（回捞 chunk 正文时要用，见
           `_prefetch_chunk_texts` 的 docstring）；
        2. 回传给 `_build_references(doc_map=...)`，免去它内部再查一次。

        失败不阻断——返回空 dict，`_build_references` 会自行回退自查。
        """
        ids = list(dict.fromkeys(d for d in (doc_ids or []) if d))
        if not ids:
            return {}
        try:
            async with get_db_session() as session:
                docs = await KnowledgeDocumentRepository(session).get_by_ids(
                    ids, tenant_id,
                )
            return {d.id: d for d in docs}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[S1] doc_map 预查失败（回退 _build_references 自查）: %r", exc)
            return {}

    async def _prefetch_chunk_texts(
        self,
        tenant_id: str,
        doc_ids: list[str],
        kb_of_doc: dict,
        fallback_kb_ids: list[str],
        cache: dict | None = None,
    ) -> dict:
        """[S1] 并发回捞多个文档的全部 chunk 正文。

        返回 ``{(doc_id, chunk_index): content}``——与 `_build_references` 的
        语义键口径一致（doc + chunk_index）。

        `cache`：**请求作用域**的复用缓存，键 ``(tenant_id, kb_id, doc_id)`` →
        ``{chunk_index: content}``。同一次查询里 `_ontology_refs` 会被调两次
        （主线 + L0 池的 `pool_only=True`），两次都把同一个 doc 的全部 chunk
        捞回来 —— 实测逐字相同，纯重复。传 `None`（旧调用方）行为逐字不变。
        ⚠️ 缓存只在**取成功**时写入，且**不得跨请求复用**（文档重解析后 chunk
        会变）；键含 kb_id 是因为 kb 走三级回落，同一 doc 可能解析出不同 kb。

        `knowledge_base_id` 解析：改造前硬编码 ``kb_ids[0]``，多 KB 查询时会把
        非首个库的文档拿错库 id 去查；此处按 doc_map 取各 doc 的真实 kb_id，
        解析不到才回落 `fallback_kb_ids[0]`。

        单 doc 失败/超时只丢弃该 doc，不影响其余——本体引用是「已有结果」的
        富化，正文缺失只降级为无 rerank 输入，绝不阻断引用产出。
        """
        if not doc_ids:
            return {}
        sem = asyncio.Semaphore(ONTOLOGY_CHUNK_TEXT_PREFETCH_CONCURRENCY)
        text_map: dict[tuple, str] = {}
        stats = {"ok": 0, "empty": 0, "failed": 0, "timeout": 0, "cached": 0}

        async def _one(did: str) -> None:
            async with sem:
                kb_id = (
                    kb_of_doc.get(did)
                    or (fallback_kb_ids[0] if fallback_kb_ids else "")
                )
                # [jonex] 请求作用域缓存：同一次查询里 `_ontology_refs` 会被调两次
                # （主线 + L0 池的 `pool_only=True`），两次都把**同一个 doc 的全部
                # chunk** 捞回来（线上实测 1 doc → 3443 个 chunk、逐字相同）。
                # 复用安全性的论证见调用方 `_ontology_refs` 的 docstring。
                # ⚠️ 只在**取成功**时写缓存 —— 失败/超时是瞬时的，缓存空结果会把
                # 一次抖动固化成本次查询的最终结论。
                _ck = (tenant_id, kb_id, did)
                if cache is not None and _ck in cache:
                    for _ci, _content in cache[_ck].items():
                        text_map[(did, _ci)] = _content
                    stats["cached"] += 1
                    return
                try:
                    result = await asyncio.wait_for(
                        get_rag_client().get_doc_chunks(
                            document_id=did,
                            knowledge_base_id=kb_id,
                            tenant_id=tenant_id,
                        ),
                        timeout=ONTOLOGY_CHUNK_TEXT_PREFETCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    stats["timeout"] += 1
                    logger.warning(
                        "[S1] 回捞 chunk 正文超时 doc=%s（>%.1fs）",
                        did, ONTOLOGY_CHUNK_TEXT_PREFETCH_TIMEOUT,
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    stats["failed"] += 1
                    logger.warning("[S1] 回捞 chunk 正文失败 doc=%s: %r", did, exc)
                    return
                n = 0
                _per_doc: dict[int, str] = {}
                for c in (result.get("chunks") or []):
                    fp = c.get("file_path") or ""
                    ci = parse_file_source(fp).get("chunk_index") if fp else None
                    content = c.get("content") or ""
                    if ci is not None and content:
                        text_map[(did, ci)] = content
                        _per_doc[ci] = content
                        n += 1
                if cache is not None:
                    cache[_ck] = _per_doc
                stats["ok" if n else "empty"] += 1

        await asyncio.gather(*(_one(d) for d in doc_ids))
        logger.info(
            "[S1] chunk 正文回捞：请求 %d doc（并发上限 %d）→ 有正文 %d / 空 %d / "
            "失败 %d / 超时 %d / 命中缓存 %d，回填 %d 个 chunk",
            len(doc_ids), ONTOLOGY_CHUNK_TEXT_PREFETCH_CONCURRENCY,
            stats["ok"], stats["empty"], stats["failed"], stats["timeout"],
            stats["cached"], len(text_map),
        )
        return text_map

    async def _ontology_refs(
        self,
        tenant_id: str,
        kb_ids: list[str],
        ontology_instances: list[dict],
        facts: list[dict] | None,
        collector: ReasoningCollector | None = None,
        query: str = "",
        trace_id: str | None = None,
        user_id: str = "",
        pool_only: bool = False,
        protect_doc_ids: set[str] | None = None,
        answer: str | None = None,
        chunk_text_cache: dict | None = None,
        rerank_score_cache: dict | None = None,
    ) -> list[dict]:
        """方案⑦：从本体 source_chunks 直接构建 chunk 级引用，不回 LightRAG。

        收集命中实体 + facts 各 target_entity 的 source_chunks[].file_path，
        经 parse_file_source → _build_references 产出完整 references（含 COS 预签名/文件名）。
        source_chunks 为空的实体退化为文档级引用（_build_references_by_doc_ids）。

        query：[jonex] 方案 G1 透传原始查询词，打通 E3 图片描述 rerank 兜底。

        pool_only：[jonex] L0 专用。True 时**只构建候选池就返回**（在 S1 正文回捞
            之后、S4 rerank 之前早退），供 `_rag_platform_answer` 把它并入 RAG 侧
            池、两路共用一次 rerank 与一次统一分。

            ⚠️ **这是权宜之计，留了一笔结构债**：函数名是 `_ontology_refs` 却可能
            只返回池，职责混杂。之所以不现在抽干净，是因为「L0 有没有用」尚未
            验证——先花 5 行拿数据，验证有价值再抽成独立的 `_ontology_candidate_pool()`。

            返回的池**已带 `_evidence_score`**（该字段在 S1 回捞之前就已写入），
            这正是 L0 要的：让 `w_e` 在进 prompt 的池里有值。

        protect_doc_ids：[jonex] 2026-09-23。答案正文 `[ref_N]` 引用到的 doc_id
            白名单 —— 这些 doc 的候选**无条件不被裁剪**。理由见
            `_prune_low_relevance_refs` 的口径表：裁掉它们会让
            `normalize_citations` 丢掉映射、答案里的引用标被静默抹空。

        answer：[jonex] 2026-09-23 Task 6。已生成的答案正文，供
            `RAG_REF_CITED_ONLY_ENABLED` 开启时**按答案引用标**裁掉「答案没用到的」
            引用（与 `protect_doc_ids` 是同一份信息，用途相反：那边防误裁、这边
            主动裁）。`None`（不传）时不过滤 —— 池构建等无答案的调用路径零变化。
        """
        t = time.perf_counter()

        # ── 收集 source_chunks（[jonex] S3：带证据权重收集，末尾按权重重排）──
        weighted_sc: list[tuple[float, str]] = []   # (weight, file_path)
        chunk_doc_ids: list[str] = []
        fallback_doc_ids: list[str] = []
        content_by_key: dict[tuple, str] = {}  # [jonex] I4-a 路线 B：source_chunks[].content → chunk key

        # [jonex] S2：`method` 口径下实体权重 = method_weight × rank_decay。
        # `rank` 是本体四级召回列表内的位次（exact/prefix 命中时列表只有
        # 1~3 条 → 位次强；RRF 混合召回串越长 → 后排越弱），与 method 独立。
        _prior_mode = RAG_EVIDENCE_PRIOR_MODE == "method"

        # [jonex] S4（§3.4 排序豁免）：本次收集里哪些 file_path 片段来自
        # 「顺序即答案」的产出源。`seg → (组类别, 组内标识)`，聚合阶段据此给
        # 候选打 `_order_locked`/`_order_group`，统一分只用于**组间**排序。
        # 两类来源：① 真行对象的证据 chunk（整行要按行序一并回传，枚举题
        # 按相关性重排会导致字段缺失）；② procedural 步骤链（`step_no` 深度序
        # 是答案正确性的一部分）。fact 级顺序本身由 Cypher 产出侧锁定（§1.8），
        # 此处只管 chunk 级引用。
        _seg_lock: dict[str, tuple[str, str]] = {}

        for rank, ent in enumerate(ontology_instances or []):
            if _prior_mode:
                w = _method_weight(ent) * _rank_decay(rank)
            else:
                # 改造前口径：命中实体的 confidence 加成（0.1~1.0 → 1.1~2.0）
                w = 1.0 + min(float(ent.get("confidence") or 0.0), 1.0)
            sc = ent.get("source_chunks")
            if isinstance(sc, list) and sc:
                for s in sc:
                    fp = s.get("file_path") if isinstance(s, dict) else None
                    c = (s.get("content") or "").strip() if isinstance(s, dict) else ""
                    if fp:
                        # [jonex] S2：stub（method_weight=0）直接剔除，不贡献
                        # 引用；confidence 口径不做该剔除，逐字保留旧行为。
                        if not _prior_mode or w > 0.0:
                            weighted_sc.append((w, fp))
                        # [jonex] S4（§3.4）：真行对象的证据 chunk 锁行序
                        if _is_true_row_object(ent):
                            _mark_order_lock(
                                _seg_lock, fp,
                                ("table", str(ent.get("table_sig") or "").strip()),
                            )
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
            # 事实权重：hop 越小越直接。改造前 = 2.0/hop（hop1=2.0 / hop2=1.0 /
            # hop3=0.67）；S2 = method_weight × 1/hop——与 2.0/hop 成比例、
            # 归一化后等价（§3.2），额外叠加目标实体的质量先验（§8-1 的 rank
            # 维度不适用于 fact：facts 的序由 Cypher 产出侧锁定，见 §3.4）。
            hop = max(int(f.get("hop") or 1), 1)
            te = f.get("target_entity") if isinstance(f, dict) else None
            # 目标实体字典：`method` 口径用于算权重，`confidence` 口径用于判行对象
            # ——两侧都要，故在分支**之外**统一取一次（S4 起初只在 method 分支里
            # 定义 `_te`，实机默认走 confidence，直接 UnboundLocalError）。
            _te = te if isinstance(te, dict) else {}
            if _prior_mode:
                w_fact = _method_weight(_te) * _hop_decay(hop)
            else:
                w_fact = 2.0 / hop
            # [jonex] S4（§3.4）：procedural 步骤链——`step_no` 由构造侧
            # （`_facts_from_chain_rows`）按链内步序写入，是答案正确性的一部分。
            # 步骤链的 chunk 引用一旦被统一分重排，答案里的步骤顺序就乱了。
            _chain_locked = f.get("step_no") is not None
            if isinstance(te, dict):
                sc = te.get("source_chunks")
                if isinstance(sc, list) and sc:
                    for s in sc:
                        fp = s.get("file_path") if isinstance(s, dict) else None
                        c = (s.get("content") or "").strip() if isinstance(s, dict) else ""
                        if fp:
                            # [jonex] S2：stub 目标实体直接剔除（§3.2）
                            if not _prior_mode or w_fact > 0.0:
                                weighted_sc.append((w_fact, fp))
                            # [jonex] S4（§3.4）：步骤链目标实体的证据锁步序
                            if _chain_locked:
                                _mark_order_lock(_seg_lock, fp, ("chain", ""))
                            elif _is_true_row_object(_te):
                                _mark_order_lock(
                                    _seg_lock, fp,
                                    ("table",
                                     str(_te.get("table_sig") or "").strip()),
                                )
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
            # 权重低于事实本体 chunk）。S2 追加 rel_kind_weight——关系边上**没有**
            # extraction_method（§1.3 ⚠️），method_weight 不适用，按实测的边构成
            # 单独定义（§3.2）。
            rsc = f.get("relation_source_chunks")
            rel_chunks = [s for s in rsc if isinstance(s, dict)] if isinstance(rsc, list) else []
            if rel_chunks:
                w_rel = (
                    _rel_kind_weight(f.get("relation_type"), rel_chunks)
                    * _hop_decay(hop)
                    if _prior_mode
                    else 1.0 / hop
                )
                for s in rel_chunks:
                    fp = s.get("file_path")
                    if fp:
                        weighted_sc.append((w_rel, fp))
                        # [jonex] S4（§3.4）：步骤链关系边的证据同样锁步序
                        if _chain_locked:
                            _mark_order_lock(_seg_lock, fp, ("chain", ""))

        # ── 解析 source_chunks file_path → 聚合权重 → raw_refs（按权重降序）──
        parsed_by_key: dict = {}
        score: dict = {}
        contrib: dict = {}
        wmax: dict = {}          # [jonex] B1：按 key 取最强单条贡献权重
        key_order: list = []
        # [jonex] S4（§3.4）：chunk 语义键 → 排序豁免组。组键带 doc_id——
        # `table_sig` 是表头 md5，不同文档的同名表会撞；步骤链按单次调用成组
        # （一次查询至多一条链），故 doc 段留空。
        lock_of_key: dict = {}
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
                # [jonex] B1 修正（2026-09-18）：最强单条贡献权重。`method` 口径
                # 用它做 `evidence_prior` 的质量项——**不用 `Σw`**，因为 `Σw` 每次
                # 贡献加一次、与 `contrib` 计同一个 n，会让 n 进两次（详见
                # `_evidence_prior` 的 docstring）。`confidence` 口径仍用 `Σw`。
                wmax[key] = max(wmax.get(key, 0.0), w)
                # [jonex] S4（§3.4）：该片段若来自锁序产出源，登记豁免组。
                # `setdefault` 与收集侧的 `_mark_order_lock` 同口径：任一来源
                # 锁定即锁定。
                _lk = _seg_lock.get(seg)
                if _lk is not None:
                    lock_of_key.setdefault(
                        key, f"{_lk[0]}:{parsed['doc_id']}:{_lk[1]}",
                    )
        # S3 重排：证据权重降序，同权重按贡献事实数降序（多事实共证的 chunk 优先），
        # 其余保持收集顺序（sort 稳定）。
        # [jonex] S2：method 口径下排序键换成 evidence_prior（补上 chunk 模态与
        # 共证事实数两个只能按 key 聚合后才能算的因子）；confidence 口径逐字保留
        # 旧的 (score, contrib) 两级排序，回滚即回到改造前。
        prior: dict = (
            {
                # [jonex] B1：传 `wmax`（最强单条）而非 `score`（Σw）——`contrib`
                # 的唯一通道是 `log1p(contrib)`，不重复计入。
                k: _evidence_prior(wmax[k], contrib[k], parsed_by_key[k])
                for k in key_order
            }
            if _prior_mode
            else score
        )
        key_order.sort(key=lambda k: (-prior[k], -contrib[k]))
        # [jonex] 方案 G2：归一化证据权重写入 _evidence_score。
        # _ontology_refs 产出的 raw_refs 只有 parse_file_source 的结构字段
        # （无 text/final_score/relevance），在 _build_references 分数链中
        # 全部落空 → _img_score=0.0 → 被阈值裁掉。证据权重是本体路径图片唯一
        # 可用的分数信号——归一化到 (0,1] 后写入，让图片拿到非零分通过阈值。
        _max_score = max(prior.values()) if prior else 0.0
        raw_refs = []
        for k in key_order:
            ref = parsed_by_key[k]
            if _max_score > 0:
                ref["_evidence_score"] = prior[k] / _max_score
            # [jonex] I4-a 主路径（路线 B）：图片 chunk 优先读编译时存入
            # source_chunks[].content 的 VLM 描述，零额外查询。
            if ref.get("image_idx") is not None and not ref.get("text"):
                ref["text"] = content_by_key.get(
                    (ref["doc_id"], ref.get("chunk_index")), "")
            # [jonex] S4（§3.4）：锁序候选——统一分只用于**组间**排序，组内保持
            # 产出序（行序 / 步序）。`_order_locked` 只为可读性与排查，判据统一
            # 取 `_order_group` 是否存在。
            _lg = lock_of_key.get(k)
            if _lg:
                ref["_order_locked"] = True
                ref["_order_group"] = _lg
            raw_refs.append(ref)
        # ⚠️ [jonex] 必须取在**引用裁剪之前**（2026-09-23 评审 M3）：本列表是文档级
        # 兜底的**排除名单**（下游 `_build_references_by_doc_ids` 只兜
        # `d not in chunk_doc_ids` 的 doc）。若取在裁剪之后，「chunk 全被裁光」的 doc
        # 就不在名单里 → 会经文档级兜底**整篇复活**，把裁剪又抵消回去。
        # 单测 `test_chunk_doc_ids_computed_before_prune` 锁着这个位次。
        chunk_doc_ids = [parsed_by_key[k]["doc_id"] for k in key_order]

        # ── [jonex] I4-a 兜底（路线 C）：回捞 chunk 正文 ──
        # 改造前只补「路线 B 未覆盖的图片」（存量 content 为空），逐 doc 串行 HTTP。
        # S1（§3.3 #1）开关开启后扩大到**全部缺正文的候选**（含文本 chunk——这是
        # 统一 rerank 的输入前提），改为受控并发 + 单 doc 超时 + doc 数上限；
        # 开关关闭时挑选口径与改造前逐字一致。
        if ONTOLOGY_CHUNK_TEXT_PREFETCH_ENABLED:
            _no_text = [r for r in raw_refs if not r.get("text")]
        else:
            _no_text = [r for r in raw_refs
                        if r.get("image_idx") is not None and not r.get("text")]
        _doc_map: dict | None = None
        if _no_text:
            # raw_refs 按 key_order（证据权重降序）产出 → 超限时截掉的是低权重 doc
            _miss_docs = list(dict.fromkeys(
                r["doc_id"] for r in _no_text if r.get("doc_id")))
            if len(_miss_docs) > ONTOLOGY_CHUNK_TEXT_PREFETCH_MAX_DOCS:
                logger.info(
                    "[S1] 待回捞 doc 数 %d 超上限 %d，按证据权重截断 %d 个低权重 doc",
                    len(_miss_docs), ONTOLOGY_CHUNK_TEXT_PREFETCH_MAX_DOCS,
                    len(_miss_docs) - ONTOLOGY_CHUNK_TEXT_PREFETCH_MAX_DOCS,
                )
                _miss_docs = _miss_docs[:ONTOLOGY_CHUNK_TEXT_PREFETCH_MAX_DOCS]
            # doc_map 预查一次，两用：解析各 doc 的真实 kb_id（改造前硬编码
            # kb_ids[0]，多 KB 查询会拿错库）+ 回传 _build_references 复用，
            # 总查询次数与改造前持平（它原本也要自查一次）。
            _doc_map = await self._load_doc_map(tenant_id, chunk_doc_ids)
            _kb_of_doc = {
                _did: _kb
                for _did, _doc in (_doc_map or {}).items()
                if (_kb := getattr(_doc, "knowledge_base_id", None))
            }
            # 兜底：doc_map 未覆盖（跨租户/已删除/预查失败）时用 file_source
            # 自带的 kb= 段，最后才回落 kb_ids[0]。
            for _r in _no_text:
                _did, _kb = _r.get("doc_id"), _r.get("kb_id")
                if _did and _kb and _did not in _kb_of_doc:
                    _kb_of_doc[_did] = _kb
            _chunk_text_map = await self._prefetch_chunk_texts(
                tenant_id, _miss_docs, _kb_of_doc, kb_ids,
                cache=chunk_text_cache,
            )
            _hit = 0
            for r in _no_text:
                if not r.get("text"):
                    _t = _chunk_text_map.get(
                        (r["doc_id"], r.get("chunk_index")), "")
                    if _t:
                        r["text"] = _t
                        _hit += 1
            if ONTOLOGY_CHUNK_TEXT_PREFETCH_ENABLED:
                logger.info(
                    "[S1] 本体候选正文回填 %d/%d 条（候选池共 %d 条）",
                    _hit, len(_no_text), len(raw_refs),
                )

        # [jonex] L0：池构建到此为止——`_evidence_score` 已写（段 2）、正文已回捞
        # （S1），但 rerank 与统一分**留给调用方**去做（两路共用一次）。
        if pool_only:
            logger.info(
                "[L0] 本体候选池早退：%d 条（已带 _evidence_score，未 rerank/未打统一分）",
                len(raw_refs),
            )
            return raw_refs

        # ── [jonex] S4（§3.3 #2「两路径共用候选池 + 单次 rerank」）──
        # 本体候选过去**完全不参与 rerank**：实测池里 23 条一条真分都没有，只有
        # 证据权重（`_evidence_score`）。而同一时刻 RAG 侧拿到的是同网关、同
        # query、同模型的 reranker 分——两侧候选一旦要横向比较（§4 的 L1 路由、
        # S7 的 L2 证据校验），比的就成了「真相关性」vs「证据权重」两种量纲，
        # 与 §3.2 要消灭的问题同源。
        #
        # 这里复用与 RAG 侧**同一个** reranker、同一个 `truncate=False` 口径，
        # 使两侧 `relevance` 同量纲可加。S1 的正文回捞（上方）已把输入备齐；
        # 没有正文的候选不会被送去打分，`_prellm_rerank_chunks` 也不给它们写
        # `relevance`（见该方法的 S4 注释）——缺项自然走权重重分配，不是「0 分」。
        if RAG_PRELLM_RERANK_ENABLED and len(raw_refs) > 1:
            raw_refs = await self._prellm_rerank_chunks(
                query or "", raw_refs,
                tenant_id=tenant_id,
                kb_id=kb_ids[0] if kb_ids else "",
                trace_id=trace_id, user_id=user_id,
                truncate=False,
            )
            # [jonex] 把本次打出的分数留给统一池复用——池里并进来的正是这批候选，
            # 输入逐字相同（同 query / 同正文 / 同模型），没必要再打一次。
            if rerank_score_cache is not None:
                for r in raw_refs:
                    _rel = r.get("relevance")
                    _did, _ci = r.get("doc_id"), r.get("chunk_index")
                    if _rel is not None and _did is not None and _ci is not None:
                        rerank_score_cache[(_did, _ci)] = _rel

        # ── [jonex] S3 统一分（§3.2）──
        # 注：rerank 未覆盖到的候选（无正文）落 `kind="order_decay"`：分数取位次
        # 衰减、仅供排序，下游不得据此做阈值判定。
        #
        # ⚠️ 本路径是**混池常态**（实测 6 组：2 组有真分、4 组真分为 0）。旧
        # `_order_aware_sort` 让位次分与真分直接比大小（位次分铺满 (0,1]、真分挤在
        # 0 附近），把真分候选压下去——实测 top15 里的带真分候选 6→5、**9→2**。
        # 现按 `score_kind` 分段（真分段在前、位次段保原序）。因本路径真分本就产出
        # 于池首（`truncate=False` 不重排），实测 6/6 组零扰动——这是**实测结论**，
        # 不是结构性保证。详见方案 §8.2。
        #
        # ⚠️ `w_e`（`_evidence_score`）在本路径**无排序增量**：它是按 `prior` 排完序
        # 之后才回写的（`:3106-3117`），与池序共线是**同义反复**；`subject_score`
        # 本路径恒缺项。故 §3.2 的 P2 步（`w_e`）在本路径是空步。
        raw_refs = _apply_unified_score(raw_refs)

        # ── [jonex] 引用按相关性裁剪（2026-09-23）──
        # 打分链到此已完整（chunk 级 rerank + 统一分），而 `_build_references`
        # 只做富化、不裁。图片支路早就在裁（`_img_min_score`），这里补上非图片
        # 模态：查询「电力变压器 有哪些组件」答案只引用 1 条，面板却列 6 条。
        # ⚠️ 判据用裸 `relevance`——统一分 `score` 混了证据先验，会把无关文档抬过线。
        # ⚠️ 答案引用过的 doc 走白名单无条件放行，否则会丢引用标（见函数 docstring）。
        raw_refs, _prune_stats = _prune_low_relevance_refs(
            raw_refs, ONTOLOGY_REF_MIN_RELEVANCE,
            protect_doc_ids=protect_doc_ids,
        )
        if _prune_stats["pruned"]:
            logger.info(
                "[ref-prune] 本体引用裁剪 query=%r 裁 %d/%d（阈值 %.2f，"
                "被裁 relevance=%s，保留最低 %.4f，order_decay 放行 %d，白名单保护 %d）",
                query[:60], _prune_stats["pruned"],
                _prune_stats["pruned"] + _prune_stats["kept"],
                ONTOLOGY_REF_MIN_RELEVANCE, _prune_stats["pruned_relevance"],
                _prune_stats["min_kept_relevance"] or 0.0,
                _prune_stats["decay_passed"], _prune_stats["protected"],
            )

        # ── chunk 级引用（source_chunks 命中）──
        refs = await self._build_references(
            tenant_id, raw_refs, allowed_kb_ids=kb_ids,
            query=query,       # [jonex] 方案 G1：打通 E3 图片描述 rerank 兜底
            doc_map=_doc_map,  # [S1] 复用上面的预查；None 时其内部自查
        ) if raw_refs else []

        # ── 文档级兜底（source_chunks 为空或 stub 实体）──
        fallback = await self._build_references_by_doc_ids(
            tenant_id,
            list(dict.fromkeys(d for d in fallback_doc_ids if d not in chunk_doc_ids)),
            allowed_kb_ids=kb_ids,
        ) if fallback_doc_ids else []

        # ── [jonex] 引用按**答案引用标**过滤（2026-09-23，Task 6）──
        # 「答案没用到的别列」——与上面的分数阈值裁独立、可叠加。两个本体入口
        # （正向 + `_cross_verify` 反向）都经这里，故过滤收在本函数内一处。
        #
        # ⚠️ **必须排在调用方的 `normalize_citations` 之前**：答案 `[ref_i]` 随后按
        # **过滤后的**列表重排下标，答案文本不用改写（见 `RAG_REF_CITED_ONLY_ENABLED`）。
        # ⚠️ 滤的是**合并后**的列表（chunk 级 + 文档级兜底）：答案引用的 doc 若只有
        # 文档级条目，只滤 chunk 级会让它从 `doc_to_idx` 里消失 → 引用标被静默抹空，
        # 正是 `protect_doc_ids` 要防的那条回归。
        all_refs = refs + fallback
        cited_stats = {"enabled": RAG_REF_CITED_ONLY_ENABLED,
                       "kept": len(all_refs), "total": len(all_refs)}
        if RAG_REF_CITED_ONLY_ENABLED and answer:
            all_refs, _cited_docs = _filter_refs_to_cited(
                answer, all_refs, _facts_number_to_doc_id(facts))
            cited_stats["kept"] = len(all_refs)
            if cited_stats["kept"] < cited_stats["total"]:
                logger.info(
                    "[ref-cited] 本体引用按答案引用标过滤 query=%r 留 %d/%d"
                    "（答案引用 %d 个 doc）",
                    query[:60], cited_stats["kept"], cited_stats["total"],
                    len(_cited_docs),
                )

        # ── 埋点：⑤ 的 STAGE_REF_RETRIEVE 改为记本体引用构建 ──
        if collector:
            collector.step(
                STAGE_REF_RETRIEVE, "本体引用构建",
                summary=(
                    f"source_chunks 命中 {len(raw_refs)} 个 → {len(refs)} 条 chunk 级引用"
                    + (f"；{len(fallback)} 条文档级兜底" if fallback else "")
                    # [jonex] Task 6：真发生过滤时补一句 —— 否则上面「N 条」
                    # 与 detail 里的 `cited_filter_kept` 读起来自相矛盾。
                    + (f"；按答案引用标留 {cited_stats['kept']}/{cited_stats['total']}"
                       if cited_stats["kept"] < cited_stats["total"] else "")
                ),
                detail={
                    "source_chunks_total": len(weighted_sc),
                    "unique_file_paths": len(raw_refs),
                    "chunk_ref_count": len(refs),
                    "fallback_ref_count": len(fallback),
                    "chunk_doc_ids": chunk_doc_ids,
                    "fallback_doc_ids": fallback_doc_ids,
                    # [jonex] S3：引用按证据权重重排，前 5 权重可见（排查用）
                    # [jonex] S2：method 口径下这里报的就是 evidence_prior
                    "top_ref_scores": [
                        round(prior.get(key, 0.0), 2)
                        for key in key_order[:5]
                    ],
                    # [jonex] 引用裁剪可见性（2026-09-23）：不报这个，
                    # 「为什么这条没列出来」就又变成不可见的。
                    "pruned_count": _prune_stats["pruned"],
                    "pruned_relevance": _prune_stats["pruned_relevance"],
                    "min_kept_relevance": _prune_stats["min_kept_relevance"],
                    "ref_threshold": ONTOLOGY_REF_MIN_RELEVANCE,
                    "decay_passed": _prune_stats["decay_passed"],
                    "protected_by_citation": _prune_stats["protected"],
                    # [jonex] 引用按答案引用标过滤的可见性（2026-09-23 Task 6）：
                    # 关闭态也报（kept == total）——否则排查时分不清「开关没开」
                    # 与「开关开了但一条没裁」。
                    "cited_only_enabled": cited_stats["enabled"],
                    "cited_filter_kept": cited_stats["kept"],
                    "cited_filter_total": cited_stats["total"],
                },
                t_start=t,
            )

        return all_refs

    async def _rag_recall_chunks(
        self,
        tenant_id: str,
        kb_ids: list[str],
        req: OntologySearchRequest,
        trace_id: str | None,
        user_id: str,
    ) -> tuple[list[dict], list[str], list[str]]:
        """[jonex] L0：LightRAG 侧**只召回**（only_need_context），不调生成 LLM。

        从 `_rag_platform_answer` 的召回段抽出，供 L0 在**路由之前**复用——
        这是「统一召回」的第三路（另两路：Neo4j 实体召回、本体侧 chunk 回捞）。

        Returns: `(refs, 命中的 kb 列表, 失败的 kb 列表)`
        """
        rag = get_rag_client()
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
        refs_out: list[dict] = []
        for kid, res in zip(kb_ids, results):
            if isinstance(res, Exception):
                logger.warning("[L0] KB 召回失败 kb=%s: %s", kid, res)
                kb_failed.append(kid)
                continue
            refs = (res or {}).get("references") or []
            if refs:
                ctx_kb.append(kid)
                refs_out.extend(refs)
            else:
                kb_failed.append(kid)
        return refs_out, ctx_kb, kb_failed

    async def _build_unified_pool(
        self,
        tenant_id: str,
        user_id: str,
        req: OntologySearchRequest,
        kb_ids: list[str],
        ontology_instances: list[dict] | None,
        trace_id: str | None,
        *,
        collector: ReasoningCollector | None = None,
        chunk_text_cache: dict | None = None,
        rerank_score_cache: dict | None = None,
    ) -> list[dict]:
        """[jonex] L0 统一召回（§4.2）：三路并行 → 合并 → 一次 rerank 打统一分。

        ```
        L0 统一召回（两路 chunk 并行，零 LLM 生成）
           ├─ Neo4j 实体召回       ← 已在 _match_ontology 完成（本体侧 candidates），
           │                          是下方「本体侧回捞」的**前置输入**，不进池
           ├─ LightRAG only_need_context 多 KB 召回   ← _rag_recall_chunks
           └─ 本体侧 chunk 正文批量回捞                ← _ontology_refs(pool_only=True)
           → 按 (doc_id, chunk_index) 合并候选池 → 一次 rerank 打统一分
        ```

        **按需构建（B7，§8-15）——不在 L1 路由之前无条件建**：本方法是惰性闭包
        `_unified_pool()` 的被调方，同查询只建一次（`_pool_cache`）。本体成功路径
        只在真进 L2 校验时才建，RAG 降级路径在作答前建。两路复用同一份池，
        rerank 只跑一次。返回的池**已带 `_evidence_score` 与 `relevance`**。

        ⚠️ 已知范围限制：本条链路在本体**邻域取证之前**，故本体候选只来自命中
        实体的 `source_chunks`，**不含 facts 的**（要含需把 L0 挪到邻域取证之后，
        而那会让 RAG 降级路径也白等一次邻域查询）。
        """
        # ① LightRAG 侧召回 + ② 本体侧 chunk 回捞，两路并行。
        _rag_task = self._rag_recall_chunks(
            tenant_id, kb_ids, req, trace_id, user_id,
        )

        async def _onto() -> list[dict]:
            if not ontology_instances:
                return []
            try:
                return await self._ontology_refs(
                    tenant_id, kb_ids, list(ontology_instances), None,
                    collector=collector, query=req.query,
                    trace_id=trace_id, user_id=user_id, pool_only=True,
                    # 与主线那次 `_ontology_refs` 共享回捞缓存——两次捞的是同一个
                    # doc 的同一批 chunk，实测逐字相同（见 `_prefetch_chunk_texts`）。
                    chunk_text_cache=chunk_text_cache,
                )
            except Exception as e:  # noqa: BLE001 — 本体侧失败不拖垮 L0
                logger.warning("[L0] 本体候选池构建失败，仅用 RAG 召回: %s", e)
                return []

        (rag_refs, _ctx_kb, _kb_failed), onto_refs = await asyncio.gather(
            _rag_task, _onto(),
        )

        # ③ 按 (doc_id, chunk_index) 合并（§3.3 #2 指定的语义键）。
        # ⚠️ 冲突时**不是丢弃本体那条**——`_evidence_score` **只由本体侧写**，
        # 丢弃等于 L0 白做。正确做法是把本体侧特有字段**补齐**到已有的 RAG ref
        # （RAG 侧 `text` 更全，本体侧的先验分是增量）。实测两路高度重叠
        # （首测 3 条本体候选全部与 RAG 池同键），故这一步是 L0 的主要价值所在。
        pool: list[dict] = []
        by_key: dict[tuple, dict] = {}
        for r in rag_refs:
            k = (r.get("doc_id"), r.get("chunk_index"))
            if k not in by_key:
                # [jonex] S7：打来源标记——L2 证据校验必须只拿 **RAG 侧** chunk
                # 当证据（本体侧 chunk 是本体答案自己的出处，拿来印证是自我循环）。
                r["_l0_src"] = "rag"
                by_key[k] = r
                pool.append(r)
        _added = _enriched = 0
        for r in onto_refs:
            k = (r.get("doc_id"), r.get("chunk_index"))
            ex = by_key.get(k)
            if ex is not None:
                # [jonex] A2 修复：同键 = **两路都召回了它**。此前这里只补字段、
                # 保留先入池的 `_l0_src="rag"`，于是「本体答案自己的出处 chunk」
                # 被 L2 当成**独立证据**（自证循环）——实测两路高度重叠，这是常态。
                # 改标 `"both"`。
                # ⚠️ [jonex] C1（2026-09-20）：L2 的证据集此后收 `rag`+`both`，
                # **只排除 `onto`**——`both` 里的 chunk 是 RAG 侧独立召回的产物，
                # 不是纯自证。原「L2 严格只收 rag」的排除面过大，实测 support
                # 中位被压到 0.129（both 口径 0.934），造成系统性误杀。
                ex["_l0_src"] = "both"
                if ex.get("_evidence_score") is None and r.get("_evidence_score") is not None:
                    ex["_evidence_score"] = r["_evidence_score"]
                    _enriched += 1
                continue
            r["_l0_src"] = "onto"
            by_key[k] = r
            pool.append(r)
            _added += 1

        # ④ 一次 rerank（truncate=False：只打分不截断，截断归预算）+ 统一分。
        if RAG_PRELLM_RERANK_ENABLED and len(pool) > 1:
            pool = await self._prellm_rerank_chunks(
                req.query, pool, tenant_id=tenant_id,
                kb_id=kb_ids[0] if kb_ids else "",
                trace_id=trace_id, user_id=user_id, truncate=False,
                # 本体侧那批在 `_ontology_refs` 的 S4 已打过分，这里只补还没分的。
                prescored=rerank_score_cache,
            )
        # [jonex] A5 修复：主体融合必须在统一分**之前**补上——否则 L0 一开，
        # `_rag_platform_answer` 的 `if unified_pool is None and RAG_SUBJECT_FILTER_ENABLED`
        # 会跳过它，而本函数原本又没做 → `subject_score` 恒缺项 → §8.4 实测
        # 「三权重里只有 `w_s` 是活的」里**唯一活的那个也死了**（L0 与 P3 互斥）。
        # 顺序与 `_rag_platform_answer` 一致：rerank → 主体融合 → 统一分。
        if RAG_SUBJECT_FILTER_ENABLED:
            _subjects = self._resolve_query_subjects(req.query, ontology_instances)
            if _subjects:
                _doc_ids = list(dict.fromkeys(
                    r.get("doc_id") for r in pool if r.get("doc_id")))
                _l0_doc_map = await self._load_doc_map(tenant_id, _doc_ids)
                pool, _ = await self._subject_weight_and_rank(
                    req.query, pool, _subjects, _l0_doc_map,
                    tenant_id=tenant_id, kb_id=kb_ids[0] if kb_ids else "",
                    trace_id=trace_id, user_id=user_id,
                    already_reranked=True, group_cap=RAG_RERANK_POOL_MAX,
                )
        pool = _apply_unified_score(pool)
        logger.info(
            "[L0] 统一池：RAG %d 条 + 本体 %d 条 → 新增 %d / 补齐先验分 %d，池共 %d 条（已 rerank + 主体融合 + 统一分）",
            len(rag_refs), len(onto_refs), _added, _enriched, len(pool),
        )
        return pool

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
        unified_pool: list[dict] | None = None,
    ) -> dict:
        """[jonex] 方案 A 新路径（§6.1）：多 KB 只召回（only_need_context）→ 平台侧一次作答。

        unified_pool：[jonex] L0。非空时**直接复用统一候选池**（三路召回已合并 +
            已 rerank + 已打统一分），跳过本函数自己的召回与 rerank——这正是
            §4.2「检索一次、多处复用」。为空时保持原行为。

            [jonex] B7 后语义微调：池不再「路由前无条件构建」，而是在**调用方
            真要用它时**才建（本函数是 RAG 降级路径，池在这里必被用上）。

        答案与引用同源：references 直接取「送进 prompt 的那批 chunk」。
        任一环失败抛异常，由 `_rag_fallback_multi` 转为 `ServiceUnavailableError` 上报
        （2026-09-22：原「回退旧链路」语义已作废，旧链路已删除）。
        两处**合法空命中**（召回为空 / 预算后无 chunk）在本函数内软返回，不抛。

        Returns:
            {"answer": str, "references": list[dict]}
        """
        rag = get_rag_client()
        t_retrieve = time.perf_counter()

        if unified_pool is not None:
            # [jonex] L0/B7：复用已构建的统一池（三路召回已合并、已 rerank、
            # 已打统一分）——§4.2「检索一次、多处复用」。跳过本函数自己的召回、
            # 合并与后续的 rerank/统一分。
            all_raw_refs = list(unified_pool)
            ctx_kb, kb_failed = list(kb_ids), []
            raw_recall_count = len(all_raw_refs)
            logger.info(
                "[L0] 复用统一池：%d 条（跳过本函数的召回与 rerank）",
                len(all_raw_refs),
            )
        else:
            # ── 1) 多 KB 并行只召回（不调生成 LLM）──
            # 抽成 `_rag_recall_chunks`，供 L0 在**路由之前**复用（§4.2 L0 统一召回）。
            all_raw_refs, ctx_kb, kb_failed = await self._rag_recall_chunks(
                tenant_id, kb_ids, req, trace_id, user_id,
            )

        # [jonex] 2026-09-22：**合法空命中，不是故障**——KB 都查通了，只是没有命中
        # 内容。旧实现抛异常让外层回退旧链路（由 LightRAG 生成一句软答案）；旧链路
        # 删除后改为直接返回软空结果。
        # ⚠️ 这道检查必须在 if/else **之外**：L0 开着时 `unified_pool` 可能是**空列表**
        # （非 None），走上面的复用分支后 `all_raw_refs` 为空却绕过检查，最后掉到预算
        # 检查里、打出「全部为空文本或超预算」的误导性 warning（2026-09-22 运行时实测）。
        # 【风险·推断未实测】「旧链路软答案 ↔ 本软结果」的语义等价是推断，因不做 E2E
        # 且旧链路已删、无对照可能；若线上反馈文案不当，回归手法是 git revert。
        if not all_raw_refs:
            if collector:
                collector.step(
                    STAGE_CONTEXT_RETRIEVE, "多 KB 只召回不生成",
                    status="done",
                    summary=f"0/{len(kb_ids)} 个知识库返回上下文（召回为空）",
                    # ⚠️ kb_ok / kb_failed 语义必须准确：前端
                    # `case 'context_retrieve'` 会把 kb_failed 渲染成
                    # `t('knowledgeSearch.failedKbs')` + tone="danger" 的**红色
                    # chip**（SessionCard.tsx:419-428）。「召回为空」是 KB 调用
                    # 成功但无内容，塞进 kb_failed 会让用户看到「失败的知识库：
                    # kb1」，语义错位。故 kb_ok 全置、kb_failed 留空。
                    detail={"kb_ok": list(kb_ids), "kb_failed": [],
                            "raw_recall_count": 0, "prompt_chunk_count": 0,
                            "recalls": [], "only_need_context": True},
                )
            return {
                "answer": translate(
                    "msg.search.rag_no_answer",
                    fallback="未在知识库中检索到与该查询相关的内容，请尝试更换关键词。",
                ),
                "references": [],
            }

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

        # ── 3) 统一 rerank：**整池打分、不做截断**（§6 S4「单次 rerank」「截断单点化」）──
        #
        # 改造前这里是个二选一，**两条路都只让 ≤8 条拿到真分**（2026-09-15 实测）：
        #   · `RAG_SUBJECT_FILTER_ENABLED=true` → `_subject_weight_and_rank` 内部
        #     `reps_sorted[:RAG_PRELLM_RERANK_TOPK]`（组级 top-8），组员还不继承 rep 的
        #     `relevance`（P0-2）→ 池里 19~20 条只有 2~6 条带真分；
        #   · `false` → `_prellm_rerank_chunks(truncate=True)` 直接
        #     `ranked[:RAG_PRELLM_RERANK_TOPK]`（`[prellm_rerank] total=19 kept=8`）。
        # 两种口径下 `_unified_score` 都在拿「8 条真分」和「十几条 `order_decay`
        # 占位分」直接比大小 —— 这正是 §3.2 要消灭的量纲越位。
        #
        # 现在：rerank 打全池（`truncate=False`），每条候选都带自己的 `relevance`；
        # 唯一的截断点是下面的预算（第 4 步）。`RAG_RERANK_POOL_MAX` 只兜底防爆。
        subject_stats: dict[str, Any] = {}
        if len(all_raw_refs) > RAG_RERANK_POOL_MAX:
            logger.warning(
                "[S4] 候选池 %d 条超 rerank 上限 %d，按召回序尾部截断（防载荷失控）；"
                "query=%r",
                len(all_raw_refs), RAG_RERANK_POOL_MAX, req.query[:80],
            )
            all_raw_refs = all_raw_refs[:RAG_RERANK_POOL_MAX]

        # [jonex] L0：复用统一池时，下列「截断 / rerank / 主体融合 / 统一分」四步
        # 全部跳过——池已经过同一套处理，重跑只是白花一次 rerank。
        if unified_pool is None and RAG_PRELLM_RERANK_ENABLED and len(all_raw_refs) > 1:
            all_raw_refs = await self._prellm_rerank_chunks(
                req.query, all_raw_refs,
                tenant_id=tenant_id,
                kb_id=kb_ids[0] if kb_ids else "",
                trace_id=trace_id, user_id=user_id,
                truncate=False,          # 只打分，不截断——截断归预算
            )

        # 主体融合：在**已有全池真分**的基础上做软加权（不再自己 rerank、不再截断）。
        if unified_pool is None and RAG_SUBJECT_FILTER_ENABLED:
            subjects = self._resolve_query_subjects(req.query, ontology_instances)
            if subjects:
                all_raw_refs, subject_stats = await self._subject_weight_and_rank(
                    req.query, all_raw_refs, subjects, doc_map,
                    tenant_id=tenant_id,
                    kb_id=kb_ids[0] if kb_ids else "",
                    trace_id=trace_id, user_id=user_id,
                    already_reranked=True,
                    group_cap=RAG_RERANK_POOL_MAX,
                )

        # ── 3.5) [jonex] S3 统一分（§3.2）：算 score / score_kind 并按统一分重排 ──
        # 位置在预算截断**之前**——截断取的就是统一分序（P1 的验证目标：
        # 纯 reranker 相关性 vs 现状的位次衰减，二者截断出的候选集不同）。
        # 开关关闭时 `_apply_unified_score` 原样返回，本段零行为变化。
        if unified_pool is None:
            all_raw_refs = _apply_unified_score(all_raw_refs)

        # ── 4) 预算硬截（按分数从高到低，总长 ≤ RAG_ANSWER_MAX_CONTEXT_CHARS）──
        # [jonex] 软删防御（prompt 层）：doc_map 按 tenant + is_deleted==0 预查，
        # 已删除文档的残留 chunk（LightRAG 清理失败的幽灵）直接跳过，不进预算、
        # 不参与作答——答案与引用同源，prompt_chunk_count 与 recall_count 收敛一致。
        # [jonex] S4：`break` → 贪心装箱。候选按统一分降序，「超预算即停」会让
        # 一个低分大 chunk（实测最大 2875 字符）把其后**全部**高分小 chunk 挡在
        # prompt 外；`continue` 只跳过装不下的那一条，继续用后面的小 chunk 填满
        # 预算。这是§6 S4「截断单点化」的落点——本循环是全链路唯一权威截断点
        # （`answer_from_chunks` 内的同名循环已降级为断言）。
        allowed = set(kb_ids)
        eligible: list[dict] = []
        ghost_filtered = 0
        for r in all_raw_refs:
            did = r.get("doc_id")
            d = doc_map.get(did)
            if d is None or d.knowledge_base_id not in allowed:
                ghost_filtered += 1
                continue
            eligible.append(r)
        prompt_refs, total, oversized = _budget_pack(
            eligible, RAG_ANSWER_MAX_CONTEXT_CHARS,
        )
        if not prompt_refs:
            # [jonex] 2026-09-22：同上一处——候选存在但全为空文本或全超预算，仍是
            # **合法空命中**，不是故障。改为软返回。
            # ⚠️ 这里**必须发 collector.step**：否则用户只看到一句空提示、推理链上
            # 什么都没有，而这恰恰是最难归因的一种空命中（候选有、就是装不进预算）。
            # 只打 logger.warning 是服务端可见、前端与用户都不可见的。
            logger.warning(
                "[S4] 预算装箱后无可用 chunk（候选 %d 条，eligible=%d，全部为空文本或超预算 %d 字符）query=%r",
                len(all_raw_refs), len(eligible), RAG_ANSWER_MAX_CONTEXT_CHARS, req.query[:80],
            )
            if collector:
                collector.step(
                    STAGE_CONTEXT_RETRIEVE, "多 KB 只召回不生成",
                    status="done",
                    summary=(
                        f"召回 {len(all_raw_refs)} 个片段，软删剔除 {ghost_filtered} 个，"
                        f"预算装箱后 0 条可用（上限 {RAG_ANSWER_MAX_CONTEXT_CHARS} 字符）"
                    ),
                    detail={"kb_ok": list(kb_ids), "kb_failed": [],
                            "raw_recall_count": len(all_raw_refs),
                            "eligible_count": len(eligible),
                            "ghost_filtered": ghost_filtered,
                            "oversized_count": oversized,
                            "budget_chars": RAG_ANSWER_MAX_CONTEXT_CHARS,
                            "prompt_chunk_count": 0,
                            "recalls": [], "only_need_context": True},
                )
            return {
                "answer": translate(
                    "msg.search.rag_no_answer",
                    fallback="未在知识库中检索到与该查询相关的内容，请尝试更换关键词。",
                ),
                "references": [],
            }
        if oversized:
            logger.info(
                "[S4] 预算装箱：候选 %d 条 → 装入 %d 条（%d 字符 / 上限 %d），"
                "跳过 %d 条超长项",
                len(eligible), len(prompt_refs), total,
                RAG_ANSWER_MAX_CONTEXT_CHARS, oversized,
            )
        if ghost_filtered:
            logger.warning(
                "prompt 层软删防御: 剔除 %d 个已删除文档的残留 chunk，"
                "作答上下文剩 %d 个（答案与引用同源）",
                ghost_filtered, len(prompt_refs),
            )

        # ── 4.5) 召回明细：实际进入 prompt 的 chunk，与 references 同源
        # ——答案/引用/明细三者一致。前端 `case 'context_retrieve'` 消费。──
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
                    # [jonex] 2026-09-22：带自己的分，供前端 chip 展示。旧路径是按位次
                    # 对齐另一份 scores 数组，这里改为「每条带自己的分」——更准。
                    # 统一分优先，缺失时退回 rerank 相关性（`RAG_UNIFIED_SCORE_ENABLED`
                    # 关闭时 `_apply_unified_score` 原样返回，此时只有 relevance）。
                    "score": r.get("score") if r.get("score") is not None
                             else r.get("relevance"),
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

        # ── 5) 平台侧一次作答（异常传播，由外层转为 ServiceUnavailableError）──
        t_answer = time.perf_counter()
        answer, budgeted = await answer_from_chunks(
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
            # 平台侧统一作答，融合阶段不触发（`fusion` 标 skipped 保留为进度可见性）
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

        # [jonex] 引用编号归一化：把答案里的 [ref_i] 重排为 references 下标 [n]。
        # budgeted 与 [ref_i] 一一对应（answer_from_chunks 返回），doc_id 在聚合
        # references 中的首次出现位置即卡片编号，保证答案 [n] 与卡片严格对应。
        number_to_doc = {}
        for i, chunk in enumerate(budgeted, 1):
            did = chunk.get("doc_id")
            if did:
                number_to_doc[str(i)] = did

        # ── [jonex] 引用按**答案引用标**过滤（2026-09-23，Task 6）──
        # ⚠️ **必须排在下面的 `normalize_citations` 之前**：先滤后归一，答案里的
        # `[ref_i]` 会按过滤后的列表重排到新下标（答案文本不用改写）；反过来会把
        # 答案里的 `[n]` 全部打乱/悬空。过滤映射与归一映射**同源**（同一个
        # `number_to_doc`），故两条路径的编号口径永远一致。
        #
        # 本路径没有 `STAGE_REF_RETRIEVE` 那样的 collector step（答案与引用同源，
        # 不经过候选池），可视性靠日志留痕。
        if RAG_REF_CITED_ONLY_ENABLED and references:
            _ref_total = len(references)
            references, _cited_docs = _filter_refs_to_cited(
                answer, references, number_to_doc)
            if len(references) < _ref_total:
                logger.info(
                    "[ref-cited] RAG 引用按答案引用标过滤 query=%r 留 %d/%d"
                    "（答案引用 %d 个 doc）",
                    req.query[:60], len(references), _ref_total,
                    len(_cited_docs),
                )
        if number_to_doc:
            answer = normalize_citations(answer, number_to_doc, references)

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
        unified_pool: list[dict] | None = None,
    ) -> dict:
        """RAG 降级入口：长度校验 + 转 `_rag_platform_answer`。

        ⚠️ 函数名是历史遗留（`_multi` 指旧的「每 KB 生成 + LLM 融合」多库策略，
        已于 2026-09-22 整体删除）。**保留该名不合并进调用方**：它是 4 处单测
        （`test_cross_verify_pool_reuse` / `test_ontology_arbitration` /
        `test_ontology_thinking_scene`）的 monkeypatch 接缝，改名会扩大爆炸半径
        而无功能收益。

        unified_pool：[jonex] L0。非空时透传给 `_rag_platform_answer` 复用
            已构建的统一候选池（不再自己召回）。

        Args:
            collector: 可选，推理链采集器（P0 非流式埋点）。
            ontology_instances: 可选，本体命中实例（方案 B 主体信号首选来源；
                命中分不足 / INSUFFICIENT 等降级场景下仍有值，只有四级均未
                命中才为空）。
        Returns:
            {"answer": str, "references": list[dict]}
        Raises:
            ServiceUnavailableError: 真故障（上游 LLM 异常 / 空答案 / 超时）。
                **合法空命中不抛**——那两种在 `_rag_platform_answer` 内软返回。
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

        try:
            return await self._rag_platform_answer(
                tenant_id, user_id, req, kb_ids, trace_id,
                collector=collector,
                ontology_instances=ontology_instances,
                fast_mode=fast_mode,
                unified_pool=unified_pool,
            )
        except ServiceUnavailableError:
            raise
        except Exception as e:
            # [jonex] 2026-09-22：新路径失败 = **真故障**（LLM 异常 /
            # answer_from_chunks 空内容 / 超时）。不再静默回退旧链路——显式上报，
            # 让失败可见（原 §6.6 回退语义作废；旧链路主体已删除）。
            # 注意：两处「合法空命中」（召回为空 / 预算后无 chunk）已在
            # `_rag_platform_answer` 内软返回，不会走到这里。
            logger.error(
                "[rag_platform_answer] 新路径失败，显式报错（不再回退旧链路）: %s", e
            )
            raise ServiceUnavailableError(
                message=translate(
                    "err.search.rag_answer_unavailable",
                    fallback="检索已命中但生成答案失败（上游服务不可用），请稍后重试",
                ),
                details={"reason": str(e)},
            ) from e

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
        model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

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
        model = resolve_llm_model("ONTOLOGY_LLM_MODEL")

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
            # [jonex] 数据运营二期：检索轨迹埋点（deep 管线）。
            await write_trace_events(
                tenant_id=tenant_id,
                request_id=trace_id or "",
                answer_id=result["history_id"],
                result=result,
                collector=collector,
                kb_ids=kb_ids,
            )
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
            # [jonex] 数据运营二期：检索轨迹埋点（严格模式）。
            # 步骤取自 `result["reasoning"]["steps"]`（strict_steps + 内层全链的
            # 合并结果，比 collector 里只有外层几条更完整 —— §D2）。
            await write_trace_events(
                tenant_id=tenant_id,
                request_id=trace_id or "",
                answer_id=result["history_id"],
                result=result,
                collector=collector,
                kb_ids=kb_ids_for_hist,
            )
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
        # 展示用 hits：路由块算完 rerank 后**按身份回填**判据分（rerank 在匹配之后）。
        # `_hits_src` 与 `_hits_detail` **同序同长**：前者存源实体引用（供 `id()` 对齐、
        # 以及 F2 的 gate 补全），后者存展示项（`_hit_trace_of` 新建、不留原引用）。
        _hits_src: list[dict] = []
        _hits_detail: list[dict] = []
        t = time.perf_counter()
        try:
            ontology_instances = await self._match_ontology(gdao, tenant_id, kb_ids, req.query)
            _hits_src = list(ontology_instances[:5])
            _hits_detail = [_hit_trace_of(i) for i in _hits_src]
            collector.step(
                STAGE_ONTOLOGY_MATCH, "本体实体匹配",
                status="done" if ontology_instances else "skipped",
                summary=(f"命中 {len(ontology_instances)} 个实体"
                         if ontology_instances else "三级匹配均未命中"),
                detail={
                    "hits": _hits_detail,
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

        # ── [jonex] L0 统一召回（§4.2）：三路召回 → 合并 → 一次 rerank 打统一分 ──
        # 这是 §3.3 #2 的欠账：此前两条路径的池是**互斥**的（本体池有
        # `_evidence_score`、RAG 池没有），合并后 `w_e` 才有作用面（P2 才可评估）。
        #
        # ⚠️ [jonex] B7 修复（2026-09-18）：**由「路由之前无条件构建」改为按需**。
        # 原实现把池建在路由前，但实测（`scripts/_archive/probes/_diag_b1_b6_b7.py`，L0 强开、
        # 10 条真实查询）本体作答成功的 6/6 条**一次都没消费它**：
        #   · 池在本体成功路径上的**唯一**消费者是 `_l2_evidence_verify`；
        #   · 而 `_cross_verify` 对高置信**在 `_assess_ontology_confidence` 之后就
        #     early return**（`:6104`），根本走不到 L2；
        #   · 该 S7 开关彼时默认 false 且实机未配 → `_l2_evidence_verify` 实测 0 次。
        #     （2026-09-22：该开关已删除，L2 固定执行，此条历史成因不再适用；
        #     但「按需构建」的结论仍然成立——高置信仍在进 L2 之前 early return。）
        # 代价实测 **937ms/条**（L0 整体 5622ms / 6 条，其中 RAG 召回 424ms、
        # rerank 视池大小 130~911ms），随池规模线性放大，全部白付。
        #
        # 改为按需后：RAG 降级路径照旧构建（那是池的主用途），本体路径只在
        # **真的进 L2** 时才建（高置信永远不会）。同一次查询只建一次（闭包缓存）。
        _pool_cache: dict[str, list[dict] | None] = {}
        # [jonex] 请求作用域的 chunk 正文缓存：本体引用构建会被调两次（主线 +
        # L0 池的 `pool_only=True`），两次都把同一个 doc 的**全部** chunk 捞回来
        # （线上实测 1 doc → 3443 个 chunk、逐字相同）。见 `_prefetch_chunk_texts`。
        # ⚠️ 生命周期**只到本次查询结束**——不能上提到模块级（重解析后 chunk 会变）。
        _chunk_text_cache: dict = {}
        # [jonex] 同上：本体侧候选的 rerank 分数缓存，供统一池跳过重复打分
        # （(`doc_id`, `chunk_index`) → relevance）。同样只活在本次查询内。
        _rerank_score_cache: dict = {}

        async def _unified_pool() -> list[dict] | None:
            """惰性构建统一池；开关关闭或已建过直接返回缓存。"""
            if not RAG_L0_UNIFIED_POOL_ENABLED:
                return None
            if "pool" not in _pool_cache:
                _t_l0 = time.perf_counter()
                _p = await self._build_unified_pool(
                    tenant_id, user_id, req, kb_ids, ontology_instances,
                    trace_id, collector=collector,
                    chunk_text_cache=_chunk_text_cache,
                    rerank_score_cache=_rerank_score_cache,
                )
                _pool_cache["pool"] = _p
                if collector:
                    collector.step(
                        STAGE_REF_RETRIEVE, "L0 统一召回",
                        summary=f"三路召回合并 → {len(_p)} 条候选（已 rerank + 统一分）",
                        detail={"pool_size": len(_p)},
                        t_start=_t_l0,
                    )
            return _pool_cache["pool"]

        if ontology_instances:
            # 路由判定：top-5 内任一命中即走本体（避免高 vscore 候选因全文 rank 落后被埋没）
            # [jonex] 多意图：收集所有过线命中（上限 ONTOLOGY_NEIGHBOR_ENTITY_MAX），
            # 供阶段 3 逐一展开邻域；matched 仍取首个（向后兼容路由展示/时间线模板）。
            # [jonex] S6（§4.3）：L1 判据统一量纲 + extraction_method 硬门。
            # ⚠️ [jonex] B4 修复：**先过门再取前 5**。原先 `[:5]` 在前、过门在后，
            # 被门挡掉的候选不从第 6 位补 → 名额净缩小。且 Cypher 侧过滤的是
            # `coalesce(stub,false)=false`、门判的是 `extraction_method=='stub'`，
            # 在「先 stub 建、后被补全内容」的实体上两者不等价，可能白丢名额。
            _gated = (
                [h for h in ontology_instances if _route_method_gate(h)]
                if ONTOLOGY_ROUTE_METHOD_GATE_ENABLED else list(ontology_instances)
            )
            top_n = _gated[:5]

            # rerank 判据（开关控制，默认关）：候选实体的 description+attributes
            # 当 document、query 当 query，得 0~1 分，替代无量纲的 ft_score 阈值。
            _rr_scores: dict[int, float] = {}
            _rr_failed = False
            if ONTOLOGY_ROUTE_RERANK_ENABLED and top_n:
                _docs = [_route_doc_of(h) for h in top_n]
                if any(_docs):
                    try:
                        from jonex_core.common.rerank import rerank as _rerank
                        _res = await _rerank(
                            req.query, _docs,
                            tenant_id=tenant_id,
                            kb_id=kb_ids[0] if kb_ids else None,
                            trace_id=trace_id, user_id=user_id,
                        )
                    except Exception as e:  # noqa: BLE001 — 路由不得因 reranker 故障中断
                        _res = None
                        logger.warning("[S6-route] rerank 调用异常，退回旧判据: %s", e)
                    for _x in (_res or []):
                        _idx = _x.get("index")
                        if isinstance(_idx, int) and 0 <= _idx < len(top_n):
                            _rr_scores[_idx] = float(_x.get("relevance_score") or 0.0)
                    if not _rr_scores:
                        _rr_failed = True
                        logger.warning(
                            "[S6-route] reranker 无有效结果（%d 条候选）→ 路由退回旧判据"
                            "（exact/prefix 旁路 + vscore/ft_score 阈值）；query=%r",
                            len(top_n), req.query[:80],
                        )
                else:
                    # [jonex] B 后近乎不可达：`_route_doc_of` 现在至少产出实体名，
                    # 而候选实体必然有名字。留作防御（真触发说明 hit 连 name 都没有）。
                    _rr_failed = True
                    logger.warning(
                        "[S6-route] 候选实体 name/description/attributes 全空，"
                        "无可判文本 → 路由退回旧判据；query=%r", req.query[:80],
                    )

            go_ontology = False
            matched_list: list[dict] = []
            for _i, hit in enumerate(top_n):
                src = hit.get("source", "")
                vs = hit.get("vscore", 0)
                fs = hit.get("ft_score", hit.get("score", 0))
                if src == "exact":
                    # exact 语义明确，保留旁路（§4.3）。
                    _pass, _why = True, "exact 旁路"
                elif ONTOLOGY_ROUTE_RERANK_ENABLED and not _rr_failed:
                    # rerank 生效：prefix 与其余一视同仁，都要过 rerank 阈值
                    # （不再凭硬编码 1.0 直接过线）。
                    _s = _rr_scores.get(_i)
                    _pass = _s is not None and _s >= ONTOLOGY_ROUTE_RERANK_MIN
                    _why = (f"rerank={_s:.4f}≥{ONTOLOGY_ROUTE_RERANK_MIN}"
                            if _s is not None else "rerank 无该候选分")
                else:
                    # 开关关 / rerank 失败：**旧判据原样保留**（含 prefix 旁路）。
                    _pass = (src in ("exact", "prefix")
                             or vs >= ONTOLOGY_VECTOR_SCORE_MIN
                             or fs >= _route_min)
                    _why = f"旧判据(source={src},vscore={vs},ft_score={fs})"
                if _pass:
                    hit["_route_why"] = _why
                    if len(matched_list) < ONTOLOGY_NEIGHBOR_ENTITY_MAX:
                        matched_list.append(hit)

            # [jonex] S6：endpoint_backfill 不得**单独**触发路由（§4.3）——
            # 它是被当关系端点补出来的实体（confidence=0.3、无描述无属性），
            # 单独命中不足以支撑走本体路径；作多实体命中里的辅助证据则合理。
            if _route_only_backfill(matched_list):
                logger.warning(
                    "[S6-route] 过线命中全为 endpoint_backfill（%d 条），"
                    "不足以单独触发本体路由 → 降级 RAG；query=%r",
                    len(matched_list), req.query[:80],
                )
                matched_list = []
            go_ontology = bool(matched_list)
            matched = matched_list[0] if matched_list else None

            # [jonex] C：原因串与 detail 统一由 `_route_trace` 产出——**展示的唯一
            # 出处**。改造前这两处内联在本函数里，`matched=None` 时把 vscore/ft_score
            # 写成 0.00（与检索结果无关），且永远挂着旧判据的阈值（rerank 生效时它们
            # 不参与判定）——2026-09-23 线上排查正是被这两点带偏。
            # 判据分回填进「本体实体匹配」那一步的展示项（rerank 发生在它之后）。
            # [jonex] F2：先补全——gate 剔掉靠前候选时 `_gated[:5]` 会含
            # `ontology_instances[5]`/`[6]`，它们不在展示列表里、分会回填不上；
            # 补完再统一按 `id()` 对齐回填（两个列表同序同长）。
            _append_gate_hits(_hits_detail, _hits_src, top_n)
            _attach_rerank_to_hits(_hits_detail, _hits_src, top_n, _rr_scores)
            route_reason, route_detail = _route_trace(
                top_n, matched,
                rerank_enabled=ONTOLOGY_ROUTE_RERANK_ENABLED,
                rerank_failed=_rr_failed,
                rr_scores=_rr_scores,
                route_min=_route_min,
                rerank_min=ONTOLOGY_ROUTE_RERANK_MIN,
            )
            collector.step(
                STAGE_ROUTE_DECISION, "路由决策",
                summary=(f"走本体路径（{route_reason}）"
                         if go_ontology else
                         f"降级 OntoRAG（{route_reason}）"),
                detail=route_detail,
            )
            if go_ontology:
                top_name = matched.get("name", "")
                top_kb_id = matched.get("kb_id") or kb_ids[0]
                logger.info(
                    "[ontology] 路由=本体 query=%r top_name=%s top_kb=%s source=%s vscore=%.4f ft_score=%s",
                    req.query, top_name, top_kb_id,
                    route_detail["source"], route_detail["vscore"], route_detail["ft_score"],
                )

                # ── [jonex] P1-6 时间线/枚举/计数意图 → 图查询模板 ──
                top_type = matched.get("type", "")
                timeline = self._detect_timeline_intent(req.query)
                graph_facts = None
                # [jonex] S5：枚举意图 → 跳过本体取证与作答，强制走 RAG
                # 取原始表格行（整行字段一并回传，覆盖「缺电话列」类召回不全）。
                # 域限定：仅命中表格行实体（extraction_method=row_as_object）才处置，
                # 非表格枚举问法（如「Skill 有哪些步骤」）正常走本体作答；
                # M3-b：procedural 问法优先，enum 不生效。
                enum_intent = self._enumeration_applies(req.query, matched_list)
                # [jonex] P8（M3-b）：procedural 优先于 enum——
                # 「配置流程的所有步骤」同时命中 enum 第 4 条（所有）与
                # procedural（步骤），步骤类问法以步骤链模板为准。
                procedural_intent = self._detect_procedural_intent(req.query)
                procedural_intent, enum_intent = self._resolve_intent_priority(
                    procedural_intent, enum_intent,
                )
                if procedural_intent and not enum_intent:
                    collector.step(
                        STAGE_FACT_LOOKUP, "procedural 意图优先", status="skipped",
                        summary="procedural 意图命中：枚举判定不生效，按步骤类问法处理",
                        detail={"query": req.query},
                    )
                    logger.info(
                        "[ontology] procedural 优先于 enum query=%r", req.query,
                    )
                if enum_intent:
                    collector.step(
                        STAGE_FACT_LOOKUP, "枚举意图处置", status="skipped",
                        summary="枚举意图：跳过本体作答，强制走 RAG 取原始表格行",
                        detail={"query": req.query},
                    )
                    logger.info("[ontology] 枚举意图 → 强制 RAG query=%r", req.query)
                elif self._detect_enumeration_intent(req.query):
                    logger.info(
                        "[ontology] 枚举词表命中但无表格行实体（procedural 优先或非 row_as_object），"
                        "不强制 RAG query=%r",
                        req.query,
                    )
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
                elif procedural_intent and top_type in ("Skill", "Action"):
                    # [jonex] P8（M3-c）：procedural 意图 + Skill/Action root
                    # → 步骤链专用模板（HAS_STEP/NEXT_STEP，不走 neighbors 自环）
                    graph_facts = await self._execute_graph_query(
                        gdao, tenant_id, top_kb_id,
                        {"intent": "procedural",
                         "params": {"root_name": top_name, "root_type": top_type}},
                        collector=collector,
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

                # ── [jonex] P8（M3-a）：inverse 语义翻译 ──
                # incoming 边按 inverse_of 映射翻译 relation_type（作答 LLM 看到
                # 语义正确方向）；无声明/开关关时零变化。compiled schema 走
                # Redis 缓存，无额外 IO。
                if ONTOLOGY_INVERSE_EXPAND_ENABLED and facts:
                    try:
                        _compiled = await OntologyCompiler().get_compiled_schema(
                            tenant_id, top_kb_id,
                        )
                        _inverse_map = self._build_inverse_map(_compiled)
                        if _inverse_map:
                            facts = self._expand_inverse_relations(facts, _inverse_map)
                            collector.step(
                                STAGE_FACT_LOOKUP, "inverse 语义翻译",
                                summary=(f"incoming 边按 inverse 映射翻译"
                                         f"（{len(_inverse_map)} 条声明）"),
                                detail={"inverse_map": _inverse_map},
                            )
                    except Exception as e:
                        logger.warning("[ontology] inverse 展开失败，跳过: %s", e)

                # ── [jonex] S2：stub 事实过滤（desc 空且 attrs 空）──
                # 在两个 facts 来源（图查询模板 / neighbors）之后统一执行；
                # 全部被过滤时自然走 INSUFFICIENT → 降级 RAG。
                if facts:
                    facts = self._filter_stub_facts(facts, collector=collector)

                # ── 阶段 4：本体作答（采集点④，独立 try）──
                if facts is not None:
                    # 方案④b：事实量预判 — 低于阈值直接跳过本体作答
                    # [jonex] P8（M3-f）：procedural 意图 + 单 Action root 豁免
                    #（单 Action 常只有 2-3 条边，低于运维调高的阈值）
                    if (
                        ONTOLOGY_MIN_FACTS > 0
                        and len(facts) < ONTOLOGY_MIN_FACTS
                        and not self._should_exempt_min_facts(procedural_intent, top_type)
                    ):
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
                # reason 里带**生效判据的实际分与阈值**（如
                # `[rerank 生效] 无命中过线（top-4 max_rerank=0.2327 < 0.5）`）——
                # 改造前这条只写死「命中但分数不足」，且 vscore/ft_score 恒为 0.0000。
                logger.info(
                    "[ontology] 路由=RAG降级（命中但分数不足）query=%r reason=%s "
                    "source=%s vscore=%.4f ft_score=%s",
                    req.query, route_reason, route_detail["source"],
                    route_detail["vscore"], route_detail["ft_score"],
                )

        # ── 阶段 5/6：RAG Fallback + 融合（采集点⑤⑥在 _rag_fallback_multi 内部）──
        references: list[dict] = []
        # [jonex] P0（2026-09-24）：本体作答**归一化之前**的原文，供 S1 置信判据
        # 定位「答案依据了哪几条 fact」。`normalize_citations` 会把 `[ref_i]` 重排成
        # references 下标（`[ref_3]` → `[2]`），事后无法还原，故必须在这里留一份。
        # RAG 路径不产生本体事实引用，恒为 None（判据回落全量 facts，行为不变）。
        _answer_fact_refs: str | None = None
        if answer is None:
            fallback = await self._rag_fallback_multi(
                tenant_id, user_id, req, kb_ids, trace_id, collector=collector,
                ontology_instances=ontology_instances,
                fast_mode=_fast_llm,
                # [jonex] L0/B7：**这条路径才是池的主用途**——复用统一池，
                # `_rag_platform_answer` 不再自己召回 + rerank，且合并了本体侧
                # `_evidence_score` 使 `w_e` 有作用面。按需构建（同查询只建一次）。
                unified_pool=await _unified_pool())
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
                # [jonex] S4：本体候选也过 rerank，计量按同一条链路归因
                trace_id=trace_id, user_id=user_id,
                # [jonex] 答案已生成：把 `[ref_N]` 引用到的 doc 保护起来，
                # 否则裁剪会让 normalize_citations 丢掉映射、引用标被抹空。
                protect_doc_ids=_cited_doc_ids(answer, facts),
                # [jonex] 同一份信息反向用（Task 6）：开启时**只留**答案引用到的
                # 引用条目。必须早于下面的 normalize_citations。
                answer=answer,
                # 与 L0 池那次共享回捞缓存（主线先跑，池侧命中）。
                chunk_text_cache=_chunk_text_cache,
            )
            # [jonex] 本体引用编号：把答案里的 [ref_i] 重排为 references 下标 [n]。
            # 与 RAG 路径（answer_from_chunks → normalize_citations）同源对齐，
            # 保证本体作答的答案正文同样带紫色上标引用编号。
            _answer_fact_refs = answer   # P0：必须早于下面这次重写
            if answer and references and facts:
                number_to_doc = _facts_number_to_doc_id(facts)
                if number_to_doc:
                    answer = normalize_citations(answer, number_to_doc, references)

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
                    # [jonex] S7 / B7：L2 证据校验要用 L0 池里的 **RAG 侧** chunk
                    # 当证据。传的是**构建器**不是池本身——高置信在 `_cross_verify`
                    # 内就 early return 了，传池等于把「按需」又变回「无条件」。
                    unified_pool_fn=_unified_pool,
                    # [jonex] P0：`answer` 此刻已是归一化文本（fact 编号被重排成
                    # references 下标），判据要吃的是它归一化前的那一份。
                    raw_answer=_answer_fact_refs,
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
            # [jonex] 数据运营二期：检索轨迹埋点（本体优先）。
            await write_trace_events(
                tenant_id=tenant_id,
                request_id=trace_id or "",
                answer_id=result["history_id"],
                result=result,
                collector=collector,
                kb_ids=kb_ids,
            )
        return result

    # ── [jonex] S1+S7 双向校验与裁决 ───────────────────────────────────

    @staticmethod
    def _fact_entity_attrs(f: dict) -> dict | None:
        """取 fact 目标实体的 attributes（JSON 字符串就地解析）。

        `attributes` 在部分写入路径下是 JSON 字符串；解析失败降级为 `None`
        （该 fact 不参与任何判据），但不掩盖代码 bug。
        """
        attrs = (f.get("target_entity") or {}).get("attributes")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except (NameError, AttributeError, ImportError):
                raise  # 代码 bug，不掩盖
            except Exception as exc:
                logger.debug("S1 confidence attributes 解析降级: %s", exc)
                attrs = None
        return attrs

    @staticmethod
    def _assess_ontology_confidence(
        facts: list[dict] | None,
        answer: str | None = None,
    ) -> tuple[str, dict]:
        """S1 置信分级（§14.2）：判定 → high / mid / low。

        ⚠️ 第二个位置参数曾是 `hits`（`ontology_instances`），**从未被函数体读过**
        ——留着一个不生效的参数会让人误以为它在参与判定，2026-09-24 已删除。
        真要判「问的是谁」（命中实体自身的 attributes 作为强信号），另开补全。

        **判据（三个信号，看 `answer` 实际引用的那批 fact）**：
        - 结构化属性（attributes 非空）是强信号；
        - 多 hop（>1）是弱信号；
        - `high` = 有结构化属性 **且** 引用的事实全在 1 跳内。

        **[jonex] F（2026-09-24）：`conflict` 不再参与判决，只作观测信号。**

        原先「多候选值冲突 → 直接判 low」（行10：`Ver1.31` vs `1.11版本` 并存）。
        实测把它拿掉的依据（30 条真实查询 / 154 个冲突键，见
        `scripts/_archive/artifacts/_conflict_key_census.md`）：全域 conflict 触发的
        键几乎全是「表行对象的列」（`id`/`code`/`bin1`/`create_time`/`quantity`…）与
        「多个实体各自的描述属性」（`definition`/`industry`/`collection_holder`…）
        ——**不是矛盾，是多个并列实体各有各的值**。它把 `high` 压得不可达（11 条可
        观测样本里 **10 条**被判 low → 本体命中必跑对侧/L2），而它为之而设的行10 型
        歧义，30 条里只认出 2 处（`teacher: 居古泉/居廉`、
        `legal_name: 株洲科能有限责任公司/股份有限公司`）。

        两次收窄口径的尝试都被同一份数据否掉，故没有再试第三种：
          · 键形态白名单（只对"身份型键"判冲突）——同一个 `collection_holder` 在
            「蟹爪水仙写生」上是真冲突（广州艺术博物院 vs 香港中文大学文物馆）、
            在「陈树人」上是 10 幅画各自的收藏地，**键形态分不开**；
          · 收窄到命中实体集合——命中候选同样是多个并列实体（含表头错位产生的
            实体：键「时间」的值竟是「环境温度(°C)」），实测只翻 1/10，还让一条
            原本 mid 的掉成 low。

        代价：行10 型不再被拦。冲突**照常计算并写进 `detail["conflict"]` /
        `near_dup_exempted`**（近重复豁免那套逻辑保留，它决定的是**上报的值**），
        供排查与后续消费；只是不再单独压低判决。

        `answer` 为 `None` 或解析不出任何 `[ref_N]`（LLM 没照做引用标 / 快档输出
        过简 / 反向路径）→ **回落全量 facts**（`scoped_by_citations=False`）。
        绝不能让「没引用标」变成「子集为空 → 判 low」的新失败模式。（原先这里还有
        一个回滚开关，观察期结束后已删除，见上方 `ONTOLOGY_CROSS_RAG_TIMEOUT` 处的注记。）
        """
        if not facts:
            return "low", {"reason": "no_facts"}

        # 口径（见 docstring）：**两类判据的遍历范围不同，别一起收窄**。
        #   · 「答案的依据强不强」三维 → 答案引用的 fact 子集（`scoped`）；
        #   · 「语料里有没有互斥值」冲突 → **全域**（`facts`）。
        scoped: list[dict] = facts
        _scoped_by_citations = False
        if answer:
            _cited = _cited_fact_numbers(answer)
            if _cited:
                _sub = [f for i, f in enumerate(facts, 1) if i in _cited]
                if _sub:
                    scoped, _scoped_by_citations = _sub, True

        # ── 遍历①：冲突检测的取数，**全域 facts** ──
        # 答案只引用了 `Ver1.31`，不代表 `1.11版本` 不存在。恰恰相反：LLM 在两个
        # 冲突值里挑了一个、且没声明存在冲突，是最该被校验的情形——把这一维收窄
        # 到引用集，等于让它挑哪个就以哪个为准（自证清白）。
        # [jonex] 四屏修复 (a)②：`attr_owners` 跟踪每个 (键, 值) 的贡献实体
        # （名字 + doc_ids），供近重复实体组豁免判定。
        attr_by_key: dict[str, set] = {}
        attr_owners: dict[str, dict[str, list[tuple[str, list[str]]]]] = {}
        for f in facts:
            attrs = SearchService._fact_entity_attrs(f)
            if not attrs:
                continue
            ent_name = f.get("target") or ""
            ent_docs = (f.get("target_entity") or {}).get("doc_ids") or []
            for k, v in attrs.items():
                if isinstance(v, dict):
                    # [jonex] 属性组（§10.2.3）：内层叶子键/值参与冲突检测
                    for k2, v2 in v.items():
                        if v2:
                            _k, _v = str(k2), str(v2)
                            attr_by_key.setdefault(_k, set()).add(_v)
                            attr_owners.setdefault(_k, {}).setdefault(
                                _v, []).append((ent_name, list(ent_docs)))
                elif v:
                    _k, _v = str(k), str(v)
                    attr_by_key.setdefault(_k, set()).add(_v)
                    attr_owners.setdefault(_k, {}).setdefault(
                        _v, []).append((ent_name, list(ent_docs)))

        # ── 遍历②：依据强度的三维，**答案引用子集** ──
        has_attrs = False
        has_desc = False
        max_hop = 1
        for f in scoped:
            if SearchService._fact_entity_attrs(f):
                has_attrs = True
            te = f.get("target_entity") or {}
            if (te.get("description") or "").strip():
                has_desc = True
            hop = f.get("hop", 1)
            if isinstance(hop, int) and hop > max_hop:
                max_hop = hop
        # 多候选冲突 = 同一属性键在不同事实间值不一致
        # （行10：版本号 Ver1.31 vs 1.11 并存）——同一实体的多属性不算冲突。
        # 四屏修复 (a)②：豁免仅限「分类学键 × 近重复实体组」的组合——
        # 值不一致但贡献实体互为近重复（不同名、名字互为子串、doc_ids 有
        # 交集，如「花瓜鱼蟹四屏」vs「高剑父花瓜鱼蟹四屏」）且键是分类学
        # 形态（artwork_category 绘画/花鸟画）时，是同主题泛化/特化实体的
        # 分类学差异，不算冲突；版本号型子串（CUDA Toolkit 12.8 vs 12.8.1）
        # 键非分类学形态、无关实体、同名多值均仍判冲突（评审 P2 收窄）。
        conflict = False
        near_dup_exempted: list[str] = []
        for k, vals in attr_by_key.items():
            if len(vals) <= 1:
                continue
            if not SearchService._is_taxonomy_attr(k):
                conflict = True
                continue
            names: set[str] = set()
            doc_ids_by_name: dict[str, set[str]] = {}
            for v in vals:
                for nm, docs in attr_owners.get(k, {}).get(v, []):
                    if nm:
                        names.add(nm)
                        doc_ids_by_name.setdefault(nm, set()).update(docs)
            if SearchService._is_near_duplicate_owner_group(
                names, doc_ids_by_name,
            ):
                near_dup_exempted.append(k)
            else:
                conflict = True
        detail = {
            "has_attrs": has_attrs, "has_desc": has_desc,
            "max_hop": max_hop, "conflict": conflict,
            # [jonex] P0：`max_hop`/`has_attrs`/`has_desc` 三维走的是「答案引用
            # 子集」还是「回落全量」。这是灰度期唯一能分辨「口径生效」与「静默
            # 回落」的信号，别降级为 debug。**不含 `conflict`**——它恒为全域。
            "scoped_by_citations": _scoped_by_citations,
        }
        if near_dup_exempted:
            detail["near_dup_exempted"] = near_dup_exempted
        # [jonex] F（2026-09-24）：`conflict` **不再否决判决**——照常算、照常进
        # detail，但不把结论压成 low。理由见 `_assess_ontology_confidence` 的 docstring。
        # 原先这里是 `if conflict: return "low", detail`。
        if has_attrs and max_hop <= 1:
            return "high", detail
        if has_attrs or has_desc:
            return "mid", detail
        return "low", detail

    @staticmethod
    def _is_taxonomy_attr(attr_key: str) -> bool:
        """评审 P2：键是否为「分类学形态」——category / 类别 / 分类 / genre /
        题材 / 品类 / 门类（大小写不敏感）。

        近重复豁免只对分类学键开放：版本号键（version、版本号）上的子串型
        同名近重复（CUDA Toolkit 12.8 vs 12.8.1）是精确数据差异，不是分类
        层级差异，绝不能豁免；不认识的键一律不豁免（宁可多跑一次裁决）。
        """
        if not attr_key:
            return False
        return bool(
            re.search(
                r"category|类别|分类|genre|题材|品类|门类", attr_key, re.IGNORECASE,
            )
        )

    @staticmethod
    def _is_near_duplicate_owner_group(
        names: set[str], doc_ids_by_name: dict[str, set[str]],
    ) -> bool:
        """四屏修复 (a)②：同键冲突值的贡献实体是否互为「近重复实体组」。

        近重复 = 不同名、名字互为子串（「花瓜鱼蟹四屏」⊂「高剑父花瓜鱼蟹四屏」）、
        且 doc_ids 有交集（同源文档）——这类实体是同主题的泛化/特化提取物，
        其分类学属性差异（artwork_category 绘画 vs 花鸟画）非语义互斥。

        调用方须先经 `_is_taxonomy_attr` 键形态把关（评审 P2 收窄），本方法
        只判实体组形态：全部贡献实体两两满足才算组（要求全部成团，避免
        「绘画/花鸟画/版画」中独立第三实体被误豁免）；同名实体多值（数据质量
        矛盾）不豁免；doc_ids 缺失/无交集时不豁免（保守——信息不足时宁可判
        冲突多跑一次裁决）。
        """
        nl = sorted(n for n in names if n)
        if len(nl) < 2:
            return False
        for i in range(len(nl)):
            for j in range(i + 1, len(nl)):
                a, b = nl[i], nl[j]
                if a == b:
                    continue
                if a in b or b in a:
                    da = doc_ids_by_name.get(a) or set()
                    db = doc_ids_by_name.get(b) or set()
                    if da and db and (da & db):
                        continue
                return False
        return True

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
    def _answer_numbers(text: str) -> set[str]:
        """答案里的数值集合（全角→半角 + 尾零归一：「40.0」与「40」等价）。"""
        out: set[str] = set()
        for s in _NUM_RE.findall(SearchService._norm_answer_text(text or "")):
            out.add(s.rstrip("0").rstrip(".") if "." in s else s)
        return out

    @staticmethod
    def _fact_entity_names(
        facts: list[dict] | None, ontology_instances: list[dict] | None,
    ) -> set[str]:
        """本体侧「依据的实体名」集合（含别名）——结构化一致判据的名称来源。

        只取 `facts` 的 target 与 `target_entity.name/aliases`、命中实体的
        `name/aliases`。**刻意不查全库实体名**：那要每次查询付一次图查询
        （单库 2k~8k 个名字），为一个命中率约 8% 的判据不值得。长度 <2 的丢弃
        ——单字名会匹配到任何文本，等于没检查。
        """
        out: set[str] = set()

        def _add(v) -> None:
            if isinstance(v, str) and len(v) >= 2:
                out.add(v)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    _add(x)

        for f in (facts or []):
            if not isinstance(f, dict):
                continue
            _add(f.get("target"))
            te = f.get("target_entity")
            if isinstance(te, dict):
                _add(te.get("name"))
                _add(te.get("aliases"))
        for h in (ontology_instances or []):
            if isinstance(h, dict):
                _add(h.get("name"))
                _add(h.get("aliases"))
        return out

    @staticmethod
    def _structural_agree(
        onto_answer: str,
        rag_answer: str,
        names: set[str],
    ) -> bool:
        """[jonex] 结构化一致性判据：本体答案的**数值与实体**是否全能在 RAG 答案里找到。

        口径标定见 `scripts/bench/_calib_structural_agree.py`（182 对真实同 query
        两路答案，标签由 LLM 判官给，与 `calibrate_answer_agree_rerank.py` 同源）：

            仅数值包含（onto 数值 ⊆ rag 数值）      命中 139/182，**FP=26**
            仅实体包含（onto 实体 ⊆ rag 实体）      命中  19/182，FP=1
            数值且实体（本判据）                    命中  15/182，**FP=1**

        → **判别力全部来自实体包含**；数值单独用是危险的（同主题答案的数值几乎全同，
        两段完全不同的描述也能共享全部数字）。数值只在实体之后当附加闸。
        唯一那条 FP 经人工核验是判官误标（本体说「信用卡挂失办理」技能、RAG 说
        「…规程」，说的是同一件事），非判据错误。

        为什么是「包含」而不是「重合度」：一致时 `_cross_verify` 返回 None、**保留
        本体答案**——只要本体答案的断言都能在 RAG 答案里找到，保留它就不算错。
        反向（RAG 多出的内容）不影响该结论。重合度类判据在同语料上不可分
        （阈值 0.55~1.0 全程 FP ≥21；rerank 通道同型失败，见其标定结论）。

        ⚠️ **已知局限**（宁漏不放，故命中率只有约 8%）：
          · 名称来源是 `facts`/`ontology_instances`（见 `_fact_entity_names`），比
            标定用的**全库实体名**窄 → 包含更易成立 → **FP 可能高于标定值**；
          · 实体靠字面匹配，别名/异写造成的假阴性是 FN 的主要来源（标定里 113 条）。

        本方法**纯字符串运算、零模型调用**——这是它相对被删除的 rerank 通道的主要优点。
        """
        if not onto_answer or not rag_answer or not names:
            return False
        _onto_nums = SearchService._answer_numbers(onto_answer)
        if not _onto_nums <= SearchService._answer_numbers(rag_answer):
            return False
        _onto_ents = {n for n in names if n in onto_answer}
        if not _onto_ents:
            return False   # 无可核对的依据 → 保守判不一致
        return _onto_ents <= {n for n in names if n in rag_answer}

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

    async def _l2_evidence_verify(
        self,
        tenant_id: str,
        kb_ids: list[str],
        req: OntologySearchRequest,
        trace_id: str | None,
        user_id: str,
        *,
        answer: str,
        facts: list[dict] | None,
        unified_pool_fn: Callable[[], Awaitable[list[dict] | None]] | None = None,
    ) -> tuple[str, float, dict]:
        """[jonex] S7：L2 证据校验（§4.4）——**纯 rerank 判别，0 次 LLM 生成**。

        池是**按需建**的（B7）：本方法在 `_cross_verify` 里位于「高置信 early
        return」之后，所以拿 `unified_pool_fn` 现调现建——高置信路径一次都不建。
        建不出来（开关关/构建失败）→ 池为空 → 走 `skip`（保持原行为）。


        为什么不用规则判据当放行闸门：`_verify_answer` 的 `grounded` 把证据 join
        成一个大串做子串包含，**假阳性致命**——「甲公司的联系人是张三」，证据里
        同时出现「甲公司」和「张三」（但张三属于乙公司）→ 全 token 命中 → 判
        grounded → 返回错答案。join 成大串后连「同一条证据内」这个最弱约束都没了。

        Returns: `(band, support, detail)`，band ∈ `{"high","mid","low","skip"}`。
        `skip` = 判不了（无池/无正文/rerank 失败），调用方应保持原行为。
        """
        detail: dict = {
            "threshold_hi": ONTOLOGY_L2_SUPPORT_T_HI,
            "threshold_lo": ONTOLOGY_L2_SUPPORT_T_LO,
        }

        # ── 前置门①结构对齐（零成本预筛，不承担放行责任）──
        # 检查答案关键值是否出现在**它自己的 facts** 里。两边都是结构化同源数据，
        # 不涉跨文档关系歧义；检的是「LLM 相对自己的输入有没有幻觉」。
        # 不通过直接判 low，省掉后面的 rerank。
        _facts_text = " ".join(json.dumps(f, ensure_ascii=False) for f in (facts or []))
        _keys = re.findall(r"\d+\.?\d+", answer or "")
        if _keys:
            _miss = [k for k in _keys if k not in _facts_text]
            detail["struct_align"] = f"数值 {len(_keys)-len(_miss)}/{len(_keys)} 可在 facts 溯源"
            if len(_miss) * 2 > len(_keys):
                # ⚠️ [jonex] A4 修复（2026-09-18）：门不过**不再判 low**。
                # 原先 `return "low"` 会让一条正则直接决定路径归属（低带 → 跳过
                # LLM 裁决、直接采纳 RAG），而该正则要求「≥2 位数字」（`\d+\.?\d+`
                # 与 §5.4.2 记的 `grounded` 后门同一缺陷）：「共 5 个部门」不匹配、
                # 纯文字答案一律跳过。单位/千分位/LLM 改写造成的假阴性都会静默把
                # 正确的本体答案降级。现改为 `skip`——保持原行为（走对侧 + 裁决）。
                detail["struct_align_miss"] = _miss[:8]
                detail["band"] = "skip"
                return "skip", 0.0, detail
        else:
            detail["struct_align"] = "无数值关键值，跳过该门"

        # ── 主判据：rerank(本体答案, L0 池中**独立召回过的** chunk) ──
        #
        # ⚠️ [jonex] C1（2026-09-20 标定后修正证据集口径）：收 `rag` + `both`，
        # **排除 `onto`**。三类来源的性质不同：
        #   · `rag`  = 只被 RAG 侧召回                      → 独立证据
        #   · `both` = 本体侧回捞 ∩ RAG 侧召回              → **同样是独立证据**
        #              （RAG 侧确实独立召回了它），只是恰好与本体实体同源
        #   · `onto` = 只被本体侧回捞（命中实体的 source_chunks）→ **纯自证**
        #
        # A2 原判「排除 both 防自证」**过度**：被排除的恰是 RAG 命中的核心 chunk
        # （与本体侧高度重叠是常态），留下的 `rag`-only 是召回边缘结果——拿边缘
        # 结果测「本体答案可信度」等于测错了样本。实测（30 条真实 query，17 条
        # 走到判带）：`sup_rag` 中位 **0.129** vs `sup_both` 中位 **0.934**（差
        # 7.2 倍）；旧口径下 high=**0 条**（S7 短路从未生效过）、low=11 条，其中
        # 6 条经逐条人工核查是带引用的合格本体答案——被系统性误杀。
        # 排除 `onto` 仍然正确：它才是纯自证，且实测其分值两极分化（13 条里
        # 7 条 <0.04），单独用会引入「答案与自身出处文本形式差异」的噪声。
        #
        # [jonex] B7：池**在这里才建**（而不是路由前）。结构对齐门不过时已
        # `return "skip"`，故那种情况连池都不建——省得比「路由前无条件建」还多。
        unified_pool = await unified_pool_fn() if unified_pool_fn else None
        if not unified_pool:
            detail["reason"] = "无统一池（L0 未开或无候选）"
            logger.warning(
                "[S7] L2 无统一池可判 → 跳过校验、保持原行为。"
                "S7 依赖 RAG_L0_UNIFIED_POOL_ENABLED=true 才有池；query=%r",
                req.query[:80],
            )
            return "skip", 0.0, detail
        # 池构成统计（零成本、无条件）：三类一起记——判带用 `rag`+`both`，
        # 但 `both`/`onto` 的占比是回溯判据口径的关键（both 高 = 两路重叠常态）。
        _src_n = {"rag": 0, "both": 0, "onto": 0}
        for r in unified_pool:
            _s = r.get("_l0_src")
            if _s in _src_n:
                _src_n[_s] += 1
        detail["src_counts"] = _src_n

        _docs = [
            str(r.get("text") or "").strip()
            for r in unified_pool
            if r.get("_l0_src") in ("rag", "both")
        ]
        _docs = [d for d in _docs if d][:RAG_RERANK_POOL_MAX]
        if not _docs:
            # ⚠️ [jonex] B5 修复：原先只写 detail 就返回，**全程静默**——与 §8.3
            # 刚吃过的教训同型。S7 单开（L0 未开）时这里必然命中，必须出声。
            detail["reason"] = "统一池中无独立召回证据（L0 未开，或候选全为 onto）"
            logger.warning(
                "[S7] L2 无可判证据（rag+both 均无正文）→ 跳过校验、保持原行为；"
                "若 L0 未开属预期，若已开则说明候选全被标为 onto（纯本体自证）；query=%r",
                req.query[:80],
            )
            return "skip", 0.0, detail

        from jonex_core.common.rerank import rerank as _rerank

        try:
            _res = await _rerank(
                answer, _docs, tenant_id=tenant_id,
                kb_id=kb_ids[0] if kb_ids else None,
                trace_id=trace_id, user_id=user_id,
            )
        except Exception as e:  # noqa: BLE001 — 校验故障不改变行为
            detail["reason"] = f"rerank 失败: {e}"
            return "skip", 0.0, detail
        _scores = sorted(
            (float(x.get("relevance_score") or 0.0) for x in (_res or [])), reverse=True,
        )
        if not _scores:
            detail["reason"] = "rerank 无结果"
            return "skip", 0.0, detail
        _topk = _scores[:ONTOLOGY_L2_SUPPORT_TOPK]
        support = sum(_topk) / len(_topk)
        detail["support"] = round(support, 4)
        detail["top_scores"] = [round(s, 4) for s in _topk]
        detail["doc_n"] = len(_docs)

        # ── 诊断取数（开关控制，只记日志不判带）──
        # 对 `both` / `onto` 各补一次同样口径的 rerank，直接回答「被排除的那批
        # 分是不是更高」。判定仍只用上面 `rag` 的 support，故本段不改变 band。
        if ONTOLOGY_L2_DIAG_ENABLED:
            for _src in ("both", "onto"):
                _ds = [
                    str(r.get("text") or "").strip()
                    for r in unified_pool
                    if r.get("_l0_src") == _src
                ]
                _ds = [d for d in _ds if d][:RAG_RERANK_POOL_MAX]
                if not _ds:
                    continue
                try:
                    _r2 = await _rerank(
                        answer, _ds, tenant_id=tenant_id,
                        kb_id=kb_ids[0] if kb_ids else None,
                        trace_id=trace_id, user_id=user_id,
                    )
                    _sc = sorted(
                        (float(x.get("relevance_score") or 0.0)
                         for x in (_r2 or [])),
                        reverse=True,
                    )
                    _k = _sc[:ONTOLOGY_L2_SUPPORT_TOPK]
                    detail[f"diag_{_src}_n"] = len(_ds)
                    detail[f"diag_{_src}_support"] = (
                        round(sum(_k) / len(_k), 4) if _k else None
                    )
                except Exception as e:  # noqa: BLE001 — 诊断不影响判定
                    detail[f"diag_{_src}_err"] = str(e)
            logger.info(
                "[S7-diag] L2 池构成 rag=%d both=%d onto=%d ｜ support: "
                "rag=%.4f both=%s onto=%s ｜ T_lo=%.2f query=%r",
                _src_n["rag"], _src_n["both"], _src_n["onto"], support,
                detail.get("diag_both_support"),
                detail.get("diag_onto_support"),
                ONTOLOGY_L2_SUPPORT_T_LO, req.query[:60],
            )

        band = (
            "high" if support >= ONTOLOGY_L2_SUPPORT_T_HI
            else "low" if support <= ONTOLOGY_L2_SUPPORT_T_LO
            else "mid"
        )
        detail["band"] = band
        return band, support, detail

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
        unified_pool_fn: Callable[[], Awaitable[list[dict] | None]] | None = None,
        raw_answer: str | None = None,
    ) -> dict | None:
        """S1+S7 统一裁决入口（§14.2/§15.4）。

        触发条件（任一满足即跑对侧）：
        - 本体侧为中/低置信（S1 分层）；
        - RAG 侧作答且查询槽位在本体侧有结构化 attributes 命中（S7，治行19）；
        - RAG 侧命中 chunk 存在低质信号（S8 判据命中但未被完全剔除）。

        两侧答案就绪后：一致 → 免裁决直接返回（cross_verified=true）；
        一方无结果 → 返回有结果一方（unverified）；不一致 → LLM 裁决。
        裁决异常/超时返回 None（调用方回退原答案，绝不因校验故障丢答案）。

        raw_answer：**[jonex] P0 专用**——本体作答在 `normalize_citations` **之前**的
            原文（含 `[ref_i]` fact 编号）。`answer` 是归一化**之后**的文本，编号已被
            重排为 references 下标，fact 语义不可还原；置信判据要定位「答案依据了
            哪几条 fact」，只能吃这一份。`None`（反向路径 / 旧调用方）→ 判据回落
            全量 facts，零行为变化。
        """
        onto_answer: str = answer if source == "ontology" else ""
        rag_answer: str = answer if source == "rag" else ""
        onto_refs = references if source == "ontology" else []
        rag_refs = references if source == "rag" else []

        _l2_low = False   # [jonex] S7：L2 判为低带 → 直接采纳 RAG，跳过 LLM 裁决
        if source == "ontology":
            confidence, conf_detail = self._assess_ontology_confidence(
                facts, answer=raw_answer,
            )
            if confidence == "high":
                return None  # 高置信不跑对侧（保持单路延迟）

            # [jonex] S7（§4.4）：L2 证据校验——**高支持度短路，0 次 LLM**。
            # 这是 S7 相对现状的全部收益所在：现状无论支持度多高都要跑一遍
            # 对侧完整 RAG（召回 + 作答），而 L2 用一次 rerank 就能在证据
            # 明确支持时直接放行。中/低带仍走原有对侧 + LLM 裁决路径。
            # [jonex] 2026-09-22：原总开关已删除，L2 校验固定执行。
            _band, _sup, _det = await self._l2_evidence_verify(
                tenant_id, kb_ids, req, trace_id, user_id,
                answer=answer, facts=facts,
                unified_pool_fn=unified_pool_fn,
            )
            if collector:
                collector.step(
                    STAGE_ARBITRATION, "L2 证据校验",
                    summary=(f"band={_band} support={_det.get('support', 'n/a')}"
                             f"（{_det.get('struct_align', '')}）"),
                    detail=_det,
                )
            if _band == "high":
                logger.info(
                    "[S7] L2 高支持度短路（0 次 LLM）support=%.4f query=%r",
                    _sup, req.query[:60],
                )
                return {
                    "answer": answer, "source": "ontology",
                    "references": references, "cross_verified": True,
                }
            # §4.4 低带 = 「RAG 证据与本体答案不是一回事」→ 走 RAG 兜底。
            # 记下来，待对侧拿到 RAG 答案后**直接采纳、跳过 LLM 裁决**。
            _l2_low = _band == "low"
            # 中/低置信 → 跑 RAG 对侧（§4.4：整体超时兜底，超时按对侧无结果处理）
            t = time.perf_counter()
            try:
                fallback = await asyncio.wait_for(
                    self._rag_fallback_multi(
                        tenant_id, user_id, req, kb_ids, trace_id,
                        collector=collector, ontology_instances=ontology_instances,
                        fast_mode=fast_mode,
                        # [jonex] §4.2「L3 复用 L0 候选池」：对侧只需**作答**，
                        # 不必重新召回 + rerank。池在上方 L2 校验时已建好并缓存
                        # （`_pool_cache`，同查询只建一次），此处现调为缓存命中、
                        # 零额外成本。开关关闭 / 未走 L2 时 `unified_pool_fn` 返回
                        # None，保持原行为（自行召回）。
                        unified_pool=(
                            await unified_pool_fn() if unified_pool_fn else None
                        ),
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
            except Exception as e:  # noqa: BLE001 — 对侧失败绝不丢已有本体答案
                # [jonex] 2026-09-22：`_rag_fallback_multi` 现在会把真故障抛成
                # `ServiceUnavailableError`（不再静默回退）。这条路径上**本体答案已产出**，
                # 必须降级为「对侧无结果」而非把 503 抛给用户。
                # 注：即使不写这一段，`:5782` 的外层 except 也会兜住并保留答案——
                # 本段是为了让日志/trace 精确指向「对侧检索失败」而非笼统的「裁决失败」。
                logger.warning(
                    "[ontology] 对侧 RAG 查询失败（%s），保留本体原答案（unverified）", e,
                )
                rag_answer, rag_refs = "", []
                if collector:
                    collector.step(
                        STAGE_ARBITRATION, "对侧 RAG 查询失败", status="failed",
                        summary="对侧生成失败（上游不可用），保留本体答案（unverified）",
                    )
            # [jonex] S7：低带直接采纳 RAG，跳过 LLM 裁决——裁决是给「两边都有理」
            # 的中间带用的；低带已经判定「本体答案与 RAG 证据不是一回事」，
            # 再花一次 LLM 去裁是浪费。
            if _l2_low and rag_answer:
                logger.info(
                    "[S7] L2 低支持度 → 直接采纳 RAG 答案（跳过 LLM 裁决）query=%r",
                    req.query[:60],
                )
                if collector:
                    collector.step(
                        STAGE_ARBITRATION, "L2 低带→RAG 兜底",
                        summary="support ≤ T_lo：证据与本体答案不是一回事，直接采纳 RAG",
                    )
                return {"answer": rag_answer, "source": "rag", "references": rag_refs}
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
            # 旧口径下 chunk 由 LightRAG 内部作答、平台未过滤）
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
                # [jonex] S4：本体候选也过 rerank，计量按同一条链路归因
                trace_id=trace_id, user_id=user_id,
                protect_doc_ids=_cited_doc_ids(onto_answer, facts),
                # [jonex] Task 6：反向路径同样按引用标过滤（漏了这处，裁决采纳
                # 本体答案时 references 不收敛——那条路更少走、更难复现）。
                answer=onto_answer,
            )
            # [jonex] 本体引用编号：反向路径的 onto_answer 同样把 [ref_i] 重排为
            # onto_refs 下标 [n]，保证裁决采纳本体答案时引用编号仍正确。
            if onto_answer and onto_refs and facts:
                number_to_doc = _facts_number_to_doc_id(facts)
                if number_to_doc:
                    onto_answer = normalize_citations(onto_answer, number_to_doc, onto_refs)
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
        # [jonex] 免裁决判定 = 数值集合精确判据 **OR** 结构化包含判据。
        # 前者对数值型答案是充分条件（改动为零）；后者覆盖「短本体答案 vs 长 RAG
        # 答案」这类纯文字形态。两者都**宁漏不放**：命中率低但零模型调用，
        # 标定与局限见 `_structural_agree` 的 docstring。
        if onto_answer and rag_answer and (
            self._answers_agree(onto_answer, rag_answer)
            or self._structural_agree(
                onto_answer, rag_answer,
                self._fact_entity_names(facts, ontology_instances),
            )
        ):
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
        compiler: Any | None = None,
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
            # [jonex] jonex_wiki 复用同一套溯源逻辑，但页面树经 jonex_wiki client 拉取
            compiler = compiler or KnowledgeCompilerService()

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

    # ── [jonex] jonex_wiki 引擎检索链路（引擎公开 trace / span 投影）──

    @staticmethod
    def _jonex_wiki_chain_calls(trace: list[dict]) -> list[dict]:
        """[jonex] 引擎公开轨迹 → 检索链路调用序列（保序，不截断；截断由调用方做）。

        引擎已在 query_task 里按事件/字段白名单投影（Reference/JonexWiki
        domain/query/query_task.py 的 _PUBLIC_TRACE_FIELDS），这里只做平台侧形状归一：
        wiki_search 带检索词 + 候选页/前沿页，wiki_read 带实读页，其余工具保留工具名与计数。
        """
        calls: list[dict] = []
        for entry in trace or []:
            if not isinstance(entry, dict):
                continue
            event = str(entry.get("event") or "")
            detail = entry.get("detail")
            if not isinstance(detail, dict):
                detail = {}
            tool = str(detail.get("tool") or "")
            if event == "tool_result" and tool == "wiki_search":
                calls.append({
                    "tool": "wiki_search",
                    "query": str(detail.get("query") or ""),
                    "paths": [str(p) for p in (detail.get("candidate_paths") or [])],
                    "frontier_paths": [str(p) for p in (detail.get("frontier_paths") or [])],
                    "count": detail.get("count"),
                    "cached": bool(detail.get("cached")),
                })
                continue
            if event == "tool_result" and tool == "wiki_read":
                paths = detail.get("paths")
                if isinstance(paths, str):
                    paths = [paths]
                calls.append({
                    "tool": "wiki_read",
                    "paths": [str(p) for p in (paths or [])],
                    "count": detail.get("count"),
                    "success": detail.get("success"),
                    "cached": bool(detail.get("cached")),
                })
                continue
            if event == "tool_result":
                calls.append({
                    "tool": tool or "tool",
                    "paths": [],
                    "count": detail.get("count"),
                    "success": detail.get("success"),
                })
                continue
            if event == "search_rewritten":
                calls.append({
                    "tool": "query_rewrite",
                    "query": str(detail.get("to") or ""),
                    "from_query": str(detail.get("from") or ""),
                    "paths": [],
                })
                continue
            if event == "search_constraints_preserved":
                calls.append({
                    "tool": "query_constraints",
                    "query": str(detail.get("query") or ""),
                    "terms": [str(t) for t in (detail.get("terms") or [])],
                    "paths": [],
                })
        return calls

    @staticmethod
    def _jonex_wiki_chain_spans(spans: list[dict], limit: int) -> list[dict]:
        """[jonex] 引擎公开 span → 链路耗时条目。

        只留 tool / evidence / synthesis 三类（model span 不投：链路展示不需要模型调用细节），
        且只取公开摘要与耗时，条数按 OPENKB_TRACE_MAX_CALLS 同口径封顶。
        """
        out: list[dict] = []
        for span in spans or []:
            if not isinstance(span, dict):
                continue
            if str(span.get("type") or "") not in ("tool", "evidence", "synthesis"):
                continue
            out.append({
                "type": str(span.get("type") or ""),
                "tool": span.get("tool"),
                "title": str(span.get("title") or ""),
                "duration_ms": span.get("duration_ms"),
                "result_summary": span.get("result_summary"),
            })
            if len(out) >= limit:
                break
        return out

    async def _build_jonex_wiki_references(
        self, tenant_id: str,
        per_kb_citations: list[tuple[str, list[dict]]],
        allowed_kb_ids: list[str] | None = None,
    ) -> list[dict]:
        """Keep each verified evidence handle distinct; document enrichment is batched."""
        from ..dtos.reference import SourceLocation, SourceReference
        entries = [(kid, c) for kid, citations in per_kb_citations for c in citations
                   if isinstance(c, dict) and c.get("document_id")]
        if not entries:
            return []
        async with get_db_session() as session:
            docs = await KnowledgeDocumentRepository(session).get_by_ids(
                list(dict.fromkeys(c["document_id"] for _, c in entries)), tenant_id)
        allowed = set(allowed_kb_ids) if allowed_kb_ids is not None else None
        doc_map = {d.id: d for d in docs if allowed is None or d.knowledge_base_id in allowed}
        kb_names = await self._get_kb_names(tenant_id, list(dict.fromkeys(k for k, _ in entries)))
        storage = get_object_storage()
        urls = {}
        out = []
        seen = set()
        for kid, c in entries:
            d = doc_map.get(c["document_id"])
            if d is None or d.knowledge_base_id != kid:
                continue
            key = (kid, c.get("query_id"), c.get("handle"), c.get("chunk_id"), c.get("excerpt"))
            if key in seen:
                continue
            seen.add(key)
            if d.id not in urls:
                try:
                    urls[d.id] = await storage.presigned_url(
                        d.storage_key or build_object_key(kid, d.id, d.file_name or ""), tenant_id)
                except Exception:
                    urls[d.id] = None
            evidence = c.get("evidence_ref") or {}
            spans = evidence.get("spans") or []
            location = SourceLocation(
                type="char" if spans else "table_row" if evidence.get("table_id") else "chunk",
                text=c.get("excerpt") or "", evidence_ref=evidence or None,
                char_start=spans[0][0] if spans else None,
                char_end=spans[0][1] if spans else None,
                row_start=evidence.get("row_start", evidence.get("row")),
                row_end=evidence.get("row_end"),
            )
            out.append(SourceReference(
                doc_id=d.id, kb_id=kid, kb_name=kb_names.get(kid),
                file_name=d.file_name or "", mime_type=d.mime_type, file_size=d.file_size,
                media_type=classify_media(d.mime_type, d.file_name), raw_url=urls[d.id],
                wiki_path=c.get("page_path"), query_id=c.get("query_id"),
                evidence_handle=c.get("handle"), bundle_sha=c.get("bundle_sha"),
                locations=[location],
            ).dict())
        return out

    @staticmethod
    def _normalize_wiki_handles(answer: str, kb_id: str, references: list[dict]) -> str:
        # Evidence indexes, not document indexes: two citations in one PDF may differ.
        import re
        indexes = {ref["evidence_handle"]: i + 1 for i, ref in enumerate(references)
                   if ref.get("kb_id") == kb_id and ref.get("evidence_handle")}
        return re.sub(r"\[E[0-9]+\]", lambda m: f"[{indexes[m[0]]}]" if m[0] in indexes else m[0], answer)

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
            # [jonex] 中文：OpenKB 空知识库时 LLM 生成的兜底表达，
            # 样本见 knowledge_search_history（「知识库中暂无相关文档内容…」、
            # 「现有知识库中未收录…」）。判据保守：只匹配明确的无内容前缀，
            # 宁可漏判空答案（回到旧行为），也不误伤正常答案。
            "知识库中暂无",
            "知识库暂无",
            "现有知识库中未收录",
            "现有知识库未收录",
            "知识库中未收录",
            "知识库未收录",
            "当前知识库中暂无",
            "当前知识库未收录",
            "未在知识库中找到",
            "知识库中未找到",
        )
        stripped = answer.strip()
        # 剥掉前导礼貌语，避免「抱歉，知识库中暂无…」漏判
        stripped = stripped.lstrip("抱歉，抱歉!，。！、 ")
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

    async def _count_llm_wiki_docs(self, tenant_id: str, kb_ids: list[str]) -> dict[str, int]:
        """[jonex] 批量统计各 KB 的有效文档数（is_deleted=0），供空库前置拦截。

        空库（0 文档）的 openkb 引擎没有 wiki 产物，agent 会反复读页直到
        MaxTurnsExceeded，把「没内容」误报成「引擎异常」；这里提前短路，直接判
        「无答案」。查询失败时不拦截（返回假计数 1），宁可靠后报错也不误伤正常库。
        """
        if not kb_ids:
            return {}
        try:
            from sqlalchemy import func, select as _select
            from ..models.document import KnowledgeDocument

            async with get_db_session() as session:
                rows = (
                    await session.execute(
                        _select(
                            KnowledgeDocument.knowledge_base_id,
                            func.count(),
                        )
                        .where(
                            KnowledgeDocument.tenant_id == tenant_id,
                            KnowledgeDocument.knowledge_base_id.in_(kb_ids),
                            KnowledgeDocument.is_deleted == 0,
                        )
                        .group_by(KnowledgeDocument.knowledge_base_id)
                    )
                ).all()
            return {r[0]: int(r[1] or 0) for r in rows}
        except Exception:
            logger.warning("有效文档数批量查询失败 kb_ids=%s", kb_ids, exc_info=True)
            return {k: 1 for k in kb_ids}

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

        # 2. 管线校验：三引擎显式——jonex_wiki 报错指路 /search/jonex_wiki，lightrag 才报 LightRAG
        pipeline_map = await self._get_kb_types(tenant_id, kb_ids)
        jonex_wiki_ids = [k for k in kb_ids if pipeline_map.get(k) == "jonex_wiki"]
        invalid_ids = [k for k in kb_ids if pipeline_map.get(k) not in LLM_WIKI_TYPES]
        if jonex_wiki_ids:
            raise InvalidParameterError(
                message=translate(
                    "err.search.kb_pipeline_mismatch_jonex_wiki",
                    params={"kb_ids": ", ".join(jonex_wiki_ids)},
                    fallback=f"知识库 {', '.join(jonex_wiki_ids)} 使用 jonex_wiki 管线，请改用 /search/jonex_wiki",
                ),
                details={"invalid_kb_ids": jonex_wiki_ids, "expected_pipeline": "jonex_wiki"},
            )
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

        # 4a. 空库前置拦截：0 文档的 KB 直接判「无答案」，不烧 openkb agent 的 50 轮。
        #     空库没有 wiki 产物，agent 会反复读页直到 MaxTurnsExceeded，误报成「引擎异常」。
        _doc_counts = await self._count_llm_wiki_docs(tenant_id, kb_ids)
        _query_ids = [k for k in kb_ids if _doc_counts.get(k, 0) > 0]
        _empty_ids = [k for k in kb_ids if _doc_counts.get(k, 0) == 0]

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

        tasks = [_query_one_okb(kid) for kid in _query_ids]
        results = await asyncio.gather(*tasks)

        per_kb: list[dict] = []
        kb_failed: list[str] = []
        kb_no_answer: list[str] = []
        # [jonex] 空库直接记「无答案」：不进引擎、不进 kb_failed，前端显示「未找到」而非「引擎异常」
        for _kid in _empty_ids:
            kb_no_answer.append(_kid)
            collector.step(
                STAGE_OPENKB_QUERY, f"LLM Wiki 检索 · {_kid}",
                status="skipped",
                summary="知识库暂无文档",
                detail={"kb_id": _kid, "reason": "empty_kb"},
            )
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

        # 6a. 先构建平台 references 并按 KB 拆分（openkb 答案无内联标记，需 LLM 锚定；
        #     每个 KB 的 answer 用该 KB 自己的引用卡片锚定，故按 kb_id 拆分 _references）。
        _references = await self._build_openkb_references(
            tenant_id, _trace_by_kb, compiler=compiler,
        ) if _trace_by_kb else []
        _refs_by_kb: dict[str, list[dict]] = {kid: [] for kid, _ in _trace_by_kb}
        for _r in _references:
            _kb = _r.get("kb_id")
            if _kb in _refs_by_kb:
                _refs_by_kb[_kb].append(_r)

        # 6. 无有效答案 → source="none"
        # [jonex] 同 jonex_wiki 侧：区分「全部 KB 调用失败」与「确实无答案」，
        # 避免把引擎故障（401/超时/500）误报成「请调整查询」。
        if not per_kb:
            if kb_failed and not kb_no_answer:
                logger.error("[openkb] 全部 KB 查询失败（非无答案）kb=%s", kb_failed)
                answer = translate(
                    "msg.search.openkb_unavailable",
                    params={"kb_ids": ", ".join(kb_failed)},
                    fallback=(
                        f"知识库检索服务暂时不可用（{', '.join(kb_failed)}），"
                        "请稍后重试；若持续出现请联系管理员。"
                    ),
                )
            else:
                answer = translate(
                    "msg.search.openkb_no_answer",
                    fallback="未在 Wiki 知识库中找到相关信息，请尝试调整查询。",
                )
            source = "none"
            rag_used = False
        elif len(per_kb) == 1:
            answer = per_kb[0]["answer"]
            # [jonex] openkb 答案无内联标记 → LLM 锚定插入 [n]（对应侧内引用卡片）
            answer = await anchor_answer_to_references(
                answer, _refs_by_kb.get(per_kb[0]["kb_id"], []),
                query=req.query, tenant_id=tenant_id, user_id=user_id, trace_id=trace_id,
            )
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
                # [jonex] 融合前先 LLM 锚定：每个 KB 的答案用自己的引用卡片插入 [n]，
                # 融合 LLM 按 prompt 原样保留 [n]，融合后编号即最终卡片下标。
                p["answer"] = await anchor_answer_to_references(
                    p["answer"], _refs_by_kb.get(p["kb_id"], []),
                    query=req.query, tenant_id=tenant_id, user_id=user_id, trace_id=trace_id,
                )
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

        # 7. 组装响应（references 已在 6a 构建；ontology_instances 仍空）
        total_ms = int((time.perf_counter() - t_total) * 1000)
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
            # [jonex] 数据运营二期：检索轨迹埋点（llm-wiki 管线）。
            await write_trace_events(
                tenant_id=tenant_id,
                request_id=trace_id or "",
                answer_id=result["history_id"],
                result=result,
                collector=collector,
                kb_ids=kb_ids,
            )

        return result

    async def search_jonex_wiki(
        self,
        tenant_id: str,
        user_id: str,
        request: JonexWikiSearchRequest | dict,
        trace_id: str | None = None,
        progress_session: str | None = None,
    ) -> dict:
        """[jonex] jonex_wiki 管线检索（只查 jonex_wiki 管线，D3 异管线拒绝）。"""
        tenant_id = require_tenant(tenant_id)
        req = JonexWikiSearchRequest(**_payload(request))
        t_total = time.perf_counter()

        # 1. KB 解析 + 租户校验
        kb_ids = await self._resolve_kb_ids(tenant_id, req)

        # 2. 管线校验：三引擎显式——openkb 报错指路 /search/llmwiki，lightrag 指路 /search/mix
        pipeline_map = await self._get_kb_types(tenant_id, kb_ids)
        openkb_ids = [k for k in kb_ids if pipeline_map.get(k) == "openkb"]
        invalid_ids = [k for k in kb_ids if pipeline_map.get(k) not in LLM_WIKI_TYPES]
        if openkb_ids:
            raise InvalidParameterError(
                message=translate(
                    "err.search.kb_pipeline_mismatch_openkb",
                    params={"kb_ids": ", ".join(openkb_ids)},
                    fallback=f"知识库 {', '.join(openkb_ids)} 使用 openkb 管线，请改用 /search/llmwiki",
                ),
                details={"invalid_kb_ids": openkb_ids, "expected_pipeline": "jonex_wiki"},
            )
        if invalid_ids:
            raise InvalidParameterError(
                message=translate(
                    "err.search.kb_pipeline_mismatch_llmwiki",
                    params={"kb_ids": ", ".join(invalid_ids)},
                    fallback=f"知识库 {', '.join(invalid_ids)} 使用 LightRAG 管线，请改用 /search/ontology 或统一入口 /search/mix",
                ),
                details={"invalid_kb_ids": invalid_ids, "expected_pipeline": "jonex_wiki"},
            )

        # 3. 推理链采集器
        collector = ReasoningCollector(enabled=req.with_reasoning and _REASONING_ENABLED)

        # 4. 多 KB 并行查询 jonex_wiki（信号量限流）
        compiler = JonexWikiService()
        _sem = asyncio.Semaphore(OPENKB_SEARCH_MAX_CONCURRENCY)
        # [jonex] 各 KB 的引擎核验引用 —— 平台 references 的唯一来源（引用是答案的证据，
        # 不受 with_reasoning 开关影响）；检索链路则直接从引擎公开 trace 现算，不留中间态。
        _citations_by_kb: list[tuple[str, list[dict]]] = []

        # [jonex] D11: references 依赖 trace，不受 with_reasoning 开关控制
        _want_trace = collector.enabled or OPENKB_REFERENCES_ENABLED

        # 4a. 空库前置拦截：0 文档的 KB 直接判「无答案」，不调 jonex_wiki 引擎。
        #     空库没有已发布 bundle，引擎会报「不存在或无可查询的已发布 bundle」，
        #     把「没内容」误报成「引擎异常」；这里提前短路，直接判「无答案」。
        _doc_counts = await self._count_llm_wiki_docs(tenant_id, kb_ids)
        _query_ids = [k for k in kb_ids if _doc_counts.get(k, 0) > 0]
        _empty_ids = [k for k in kb_ids if _doc_counts.get(k, 0) == 0]

        async def _query_one_jw(kid: str) -> tuple[str, object, int]:
            _t0 = time.perf_counter()
            async with _sem:
                try:
                    _res = await compiler.search_compiled_knowledge(
                        kb_name=kid, tenant_id=tenant_id, kb_id=kid, question=req.query,
                        return_trace=_want_trace,
                        thinking_mode=req.thinking_mode,
                        **({"progress_session": progress_session} if progress_session else {}),
                    )
                    return (kid, _res, int((time.perf_counter() - _t0) * 1000))
                except Exception as e:
                    return (kid, e, int((time.perf_counter() - _t0) * 1000))

        tasks = [_query_one_jw(kid) for kid in _query_ids]
        results = await asyncio.gather(*tasks)

        per_kb: list[dict] = []
        kb_failed: list[str] = []
        kb_no_answer: list[str] = []
        # [jonex] 空库直接记「无答案」：不进引擎、不进 kb_failed，前端显示「未找到」而非「引擎异常」
        for _kid in _empty_ids:
            kb_no_answer.append(_kid)
            collector.step(
                STAGE_JONEX_WIKI_QUERY, f"jonex_wiki 检索 · {_kid}",
                status="skipped",
                summary="知识库暂无文档",
                detail={"kb_id": _kid, "reason": "empty_kb"},
            )
        for kid, res, kb_ms in results:
            if isinstance(res, Exception):
                logger.warning("[jonex_wiki] query 失败 kb=%s: %s", kid, res)
                kb_failed.append(kid)
                collector.step(
                    STAGE_JONEX_WIKI_QUERY, f"jonex_wiki 检索 · {kid}",
                    status="failed",
                    summary="jonex_wiki 查询异常",
                    detail={"kb_id": kid, "error": str(res)[:200], "duration_ms": kb_ms},
                )
                continue
            answer = res.get("answer") if isinstance(res, dict) else str(res)
            # [jonex] 引擎返回三样东西：答案、已核验引用（六字段 + page_path）、
            # 公开检索轨迹（白名单后的 trace + CR-TRACE-002 span）。
            _citations = list(res.get("citations") or []) if isinstance(res, dict) else []
            _trace_events = list(res.get("trace") or []) if isinstance(res, dict) else []
            _spans = list(res.get("spans") or []) if isinstance(res, dict) else []
            _counts = (res.get("counts") or {}) if isinstance(res, dict) else {}
            _calls_all = self._jonex_wiki_chain_calls(_trace_events)
            # [jonex] D8 截断：按调用数截断（保留前 N 次，链路顺序不变）
            _calls_total = len(_calls_all)
            _truncated = _calls_total > OPENKB_TRACE_MAX_CALLS
            _calls = _calls_all[:OPENKB_TRACE_MAX_CALLS] if _truncated else _calls_all
            if not self._is_openkb_effective_answer(answer):
                logger.info("[jonex_wiki] 无效答案 kb=%s answer_preview=%r", kid, (answer or "")[:80])
                kb_no_answer.append(kid)
                collector.step(
                    STAGE_JONEX_WIKI_QUERY, f"jonex_wiki 检索 · {kid}",
                    status="skipped",
                    summary="未找到有效答案",
                    detail={
                        "kb_id": kid,
                        "answer_preview": (answer or "")[:200],
                        "calls": _calls,
                        "tool_calls_total": _calls_total,
                        "duration_ms": kb_ms,
                    },
                )
                continue
            per_kb.append({"kb_id": kid, "answer": answer, "source": "jonex_wiki"})
            if _citations:
                _citations_by_kb.append((kid, _citations))
            _detail: dict = {
                "kb_id": kid,
                "answer_preview": answer[:200],
                "calls": _calls,
                "tool_calls_total": _calls_total,
                "citation_count": len(_citations),
                "duration_ms": kb_ms,
            }
            _chain_spans = self._jonex_wiki_chain_spans(_spans, OPENKB_TRACE_MAX_CALLS)
            if _chain_spans:
                _detail["spans"] = _chain_spans
            if _counts:
                _detail["counts"] = _counts
            if _truncated:
                _detail["tool_calls_truncated"] = True
            collector.step(
                STAGE_JONEX_WIKI_QUERY, f"jonex_wiki 检索 · {kid}",
                summary=f"返回有效答案（{_calls_total} 次检索/阅读调用，{len(_citations)} 条引用）",
                detail=_detail,
            )

        # 6a. 先构建平台 references + 各 KB 的 evidence handle 映射（[E#]→[n] 改写需要）。
        #     handle 是引擎侧的 KB 内编号（每个 KB 都从 [E1] 起），跨 KB 会冲突，故按 KB
        #     分别建映射；改写只发生在融合前（单 KB 直接改 answer，多 KB 每个答案先改再融合）。
        _references = await self._build_jonex_wiki_references(
            tenant_id, _citations_by_kb, allowed_kb_ids=kb_ids,
        ) if _citations_by_kb else []

        # 6. 无有效答案 → source="none"
        # [jonex] 必须先区分「全部调用失败」与「确实无答案」：两者都进 not per_kb，
        # 但前者是服务故障（401/超时/500），沿用 no_answer 文案会把故障误报成
        # 「请调整查询」，把用户和排障的人都引向错误方向（曾掩盖 jonex_wiki
        # 全量 401 鉴权失败）。kb_no_answer 非空则说明确有引擎正常应答，只是没内容。
        if not per_kb:
            if kb_failed and not kb_no_answer:
                logger.error("[jonex_wiki] 全部 KB 查询失败（非无答案）kb=%s", kb_failed)
                answer = translate(
                    "msg.search.jonex_wiki_unavailable",
                    params={"kb_ids": ", ".join(kb_failed)},
                    fallback=(
                        f"jonex_wiki 检索服务暂时不可用（{', '.join(kb_failed)}），"
                        "请稍后重试；若持续出现请联系管理员。"
                    ),
                )
            else:
                answer = translate(
                    "msg.search.jonex_wiki_no_answer",
                    fallback="未在 jonex_wiki 知识库中找到相关信息，请尝试调整查询。",
                )
            source = "none"
            rag_used = False
        elif len(per_kb) == 1:
            answer = per_kb[0]["answer"]
            # [jonex] 引擎答案里的 [E#] 证据句柄 → 平台引用卡片下标 [n]
            answer = self._normalize_wiki_handles(answer, per_kb[0]["kb_id"], _references)
            # [jonex] 与 kb_type 同拼写（前端直接展示 final_source，别混两种写法）
            source = "jonex_wiki"
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
                # [jonex] 融合前先把 [E#] → [n]（handle 是 KB 内编号，须按 KB 映射），
                # 融合 LLM 按 prompt 原样保留 [n]，融合后编号即最终卡片下标。
                p["answer"] = self._normalize_wiki_handles(p["answer"], p["kb_id"], _references)
            t_fuse = time.perf_counter()
            answer = await fuse_rag_answers(
                req.query, per_kb_fused,
                tenant_id=tenant_id, user_id=user_id, trace_id=trace_id,
            )
            fusion_ms = int((time.perf_counter() - t_fuse) * 1000)
            collector.step(STAGE_FUSION, "多答案融合",
                           summary=f"融合 {len(per_kb_fused)} 个知识库的 jonex_wiki 答案",
                           t_start=t_fuse)
            source = "jonex_wiki"
            rag_used = False

        # 7. 组装响应（references 已在 6a 构建；ontology_instances 仍空）
        total_ms = int((time.perf_counter() - t_total) * 1000)
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
                        "pipeline": "jonex_wiki",
                    },
                ),
            )
            result["history_id"] = history.get("id")
            # [jonex] 数据运营二期：检索轨迹埋点（jonex-wiki 管线）。
            await write_trace_events(
                tenant_id=tenant_id,
                request_id=trace_id or "",
                answer_id=result["history_id"],
                result=result,
                collector=collector,
                kb_ids=kb_ids,
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

        按 pipeline_type 分组扇出到 LightRAG / OpenKB / jonex_wiki，支持混合选库。
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

        # [jonex] B6 §5.1：多 KB 检索按用户可读最高等级过滤；全部被过滤 ⇒ 空结果
        # （不报错，前端已有空答案容错）。管理员/无 user/anonymous 原样放行。
        raw_ids = await _filter_kbs_by_classification(tenant_id, user_id, raw_ids)
        if not raw_ids:
            return {
                "answer": "",
                "source": "none",
                "references": [],
                "ontology_instances": [],
                "rag_used": False,
                "knowledge_base_ids": [],
                "reasoning": {},
                "references_available": False,
                "ontology_instances_available": False,
            }

        pipeline_map = await self._get_kb_types(tenant_id, raw_ids)
        # [jonex] 三引擎显式分桶：jonex_wiki 不兜进 lightrag 桶（Phase 5 /search/mix 三路接入）
        lightrag_ids = [k for k in raw_ids if pipeline_map.get(k) == "lightrag"]
        openkb_ids = [k for k in raw_ids if pipeline_map.get(k) == "openkb"]
        jonex_wiki_ids = [k for k in raw_ids if pipeline_map.get(k) == "jonex_wiki"]

        # 2. 路由决策（reasoning 阶段①）
        collector.step(
            STAGE_ROUTE_DECISION, "分流决策",
            summary=(
                f"OntoRAG={len(lightrag_ids)} KB, llm-wiki={len(openkb_ids)} KB, "
                f"jonex-wiki={len(jonex_wiki_ids)} KB"
            ),
            detail={
                "pipeline_groups": {
                    "OntoRAG": lightrag_ids,
                    "llm-wiki": openkb_ids,
                    "jonex-wiki": jonex_wiki_ids,
                },
                "OntoRAG_count": len(lightrag_ids),
                "llm-wiki_count": len(openkb_ids),
                "jonex-wiki_count": len(jonex_wiki_ids),
            },
        )

        # 3. 全 lightrag → 委托 query_with_ontology
        if not openkb_ids and not jonex_wiki_ids:
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
        if not lightrag_ids and not jonex_wiki_ids:
            req_wiki = LlmWikiSearchRequest(
                query=req.query, mode=req.mode, top_k=req.top_k,
                knowledge_base_ids=openkb_ids,
                save_history=req.save_history,
                with_reasoning=req.with_reasoning,
                domain_space_id=req.domain_space_id,
            )
            return await self.search_llmwiki(tenant_id, user_id, req_wiki, trace_id=trace_id)

        # 4b. 全 jonex_wiki → 委托 search_jonex_wiki
        if not lightrag_ids and not openkb_ids:
            req_jw = JonexWikiSearchRequest(
                query=req.query, mode=req.mode, top_k=req.top_k,
                knowledge_base_ids=jonex_wiki_ids,
                save_history=req.save_history,
                with_reasoning=req.with_reasoning,
                domain_space_id=req.domain_space_id,
            )
            return await self.search_jonex_wiki(tenant_id, user_id, req_jw, trace_id=trace_id)

        # 5. 混合 → 并行（只跑非空桶），子调用强制 save_history=False（D8.1）
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

        # [jonex] Phase 5 降级：jonex_wiki 侧 90s 超时返回空 + 标记 source 降级，
        # 不拖垮 lightrag/openkb。超时后 cancel 任务（asyncio.wait_for 内建取消），
        # 返回降级 dict，不阻塞整体。
        def _jw_degraded(reason: str) -> dict:
            return {
                "source": "none",
                "degraded": True,
                "degraded_reason": reason,
                "answer": "",
                "references": [],
                "ontology_instances": [],
                "rag_used": False,
                "knowledge_base_ids": jonex_wiki_ids,
                "reasoning": {},
            }

        async def _run_jonex_wiki() -> dict:
            req_jw = JonexWikiSearchRequest(
                query=req.query, mode=req.mode, top_k=req.top_k,
                knowledge_base_ids=jonex_wiki_ids,
                save_history=False,
                with_reasoning=req.with_reasoning,
                domain_space_id=req.domain_space_id,
            )
            try:
                return await asyncio.wait_for(
                    self.search_jonex_wiki(tenant_id, user_id, req_jw, trace_id=trace_id),
                    timeout=JONEX_WIKI_SEARCH_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.warning("[jonex_wiki] search_mix 超时降级 kb=%s timeout=%ss",
                               jonex_wiki_ids, JONEX_WIKI_SEARCH_TIMEOUT_SEC)
                return _jw_degraded("timeout")
            except Exception as exc:
                logger.warning("[jonex_wiki] search_mix 失败降级 kb=%s: %s", jonex_wiki_ids, exc)
                return _jw_degraded(f"error: {exc}"[:200])

        _coros: list[tuple[str, Any]] = []
        if lightrag_ids:
            _coros.append(("lr", _run_lightrag()))
        if openkb_ids:
            _coros.append(("okb", _run_openkb()))
        if jonex_wiki_ids:
            _coros.append(("jw", _run_jonex_wiki()))
        _gathered = await asyncio.gather(*[c for _, c in _coros])
        _by_side = {key: res for (key, _), res in zip(_coros, _gathered)}
        lr_result = _by_side.get("lr")
        okb_result = _by_side.get("okb")
        jw_result = _by_side.get("jw")

        # 6. 判定三侧成败
        lr_ok = isinstance(lr_result, dict)
        okb_ok = isinstance(okb_result, dict)
        jw_ok = isinstance(jw_result, dict)
        lr_answer = (lr_result.get("answer") or "") if lr_ok else ""
        okb_answer = (okb_result.get("answer") or "") if okb_ok else ""
        jw_answer = (jw_result.get("answer") or "") if jw_ok else ""
        # source="rag" 时 references + ontology_instances 双空 = RAG 未找到任何实质内容
        #（"抱歉，所有知识库..."兜底），此时应视为无效，避免把空结果当有效答案参与融合
        lr_effective = lr_ok and (
            lr_result.get("source") == "ontology"
            or bool(lr_result.get("references"))
            or bool(lr_result.get("ontology_instances"))
        )
        okb_effective = okb_ok and okb_result.get("source") not in ("none", "")
        jw_effective = jw_ok and jw_result.get("source") not in ("none", "")

        # [jonex] D9: 合并子链路的推理步骤。
        # 子调用已按 with_reasoning 产出 reasoning，不合并则用户混选时看不到 wiki 浏览过程。
        # 按管线分组（lightrag → llm-wiki → jonex-wiki），不按时间交错。
        if collector.enabled:
            # [jonex] 展示名：推理链 title 前缀用产品名（OntoRAG / llm-wiki / jonex-wiki），
            # 不用内部管线标识（此前 [lightrag] 字样出现在推理链 UI）
            for _side, _res in (("OntoRAG", lr_result), ("llm-wiki", okb_result),
                                ("jonex-wiki", jw_result)):
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
        # [jonex] Phase 5 降级标记：jonex_wiki 超时/失败在推理链显式标注，不静默
        if isinstance(jw_result, dict) and jw_result.get("degraded"):
            collector.step(
                STAGE_JONEX_WIKI_QUERY, "jonex_wiki 检索",
                status="degraded",
                summary="jonex_wiki 侧降级（超时/失败），不参与融合",
                detail={"kb_ids": jonex_wiki_ids,
                        "reason": jw_result.get("degraded_reason")},
            )

        # 7. 融合（三侧）
        def _ref_kb_ids(refs) -> list[str]:
            return list(dict.fromkeys(r.get("kb_id") for r in (refs or []) if r.get("kb_id")))

        _side_label = {"lr": "OntoRAG", "okb": "llm-wiki", "jw": "jonex-wiki"}
        _side_source = {"lr": None, "okb": "llm-wiki", "jw": "jonex-wiki"}
        _side_kb = {"lr": lightrag_ids, "okb": openkb_ids, "jw": jonex_wiki_ids}

        # 有效侧（保序：OntoRAG → llm-wiki → jonex-wiki）
        _effective: list[tuple[str, dict, str]] = []
        if lr_effective:
            _effective.append(("lr", lr_result, lr_answer))
        if okb_effective:
            _effective.append(("okb", okb_result, okb_answer))
        if jw_effective:
            _effective.append(("jw", jw_result, jw_answer))

        if len(_effective) >= 2:
            # 多侧融合
            # [jonex] 方案 B：先合并引用（OntoRAG 在前），再把各侧答案里的「侧内编号 [n]」
            # 重写为「合并引用卡片」的下标，使融合答案保留的编号与下方卡片严格对应。
            lr_refs = (lr_result.get("references") or []) if lr_ok else []
            okb_refs = (okb_result.get("references") or []) if okb_ok else []
            jw_refs = (jw_result.get("references") or []) if jw_ok else []
            references = self._merge_references(lr_refs, okb_refs, jw_refs)
            # [jonex] 各侧 answer 的「侧内编号」→「合并卡片下标」重写；无侧内编号的侧不重写。
            # 重写结果须回填进 per_kb（此前直接改局部 lr_answer，_effective 里的旧快照没更新，
            # 融合实际吃的是重写前 answer——OntoRAG 编号错位；jonex_wiki/openkb 侧同理）。
            _rewritten_answer = {"lr": lr_answer, "okb": okb_answer, "jw": jw_answer}
            if lr_refs:
                lr_number_to_doc = {
                    str(i): (r.get("doc_id") or "")
                    for i, r in enumerate(lr_refs, 1)
                }
                _rewritten_answer["lr"] = normalize_citations(
                    lr_answer, lr_number_to_doc, references,
                )
            if okb_refs:
                okb_number_to_doc = {
                    str(i): (r.get("doc_id") or "")
                    for i, r in enumerate(okb_refs, 1)
                }
                _rewritten_answer["okb"] = normalize_citations(
                    okb_answer, okb_number_to_doc, references,
                )
            if jw_refs:
                jw_number_to_doc = {
                    str(i): (r.get("doc_id") or "")
                    for i, r in enumerate(jw_refs, 1)
                }
                _rewritten_answer["jw"] = normalize_citations(
                    jw_answer, jw_number_to_doc, references,
                )
            # [jonex] 融合 prompt 用 KB 显示名标识来源（LLM 会照抄标识进答案；
            # 用 kb_id UUID / source 值会产出「知识库 `llm-wiki`」这类错误标注）。
            # 标签只列「该侧 references 实际命中的 KB」——请求中的无内容/无检索
            # 命中的知识库不应被关联为来源（无关联 KB 标注问题）。某侧引用为空
            # 时标签置空，由 fuse_rag_answers 落为 unspecified（答案中不标注）。
            _all_ref_kb_ids: list[str] = []
            for _k, _r, _a in _effective:
                _all_ref_kb_ids += _ref_kb_ids(_r.get("references") or [])
            _kb_names = await self._get_kb_names(tenant_id, _all_ref_kb_ids)
            per_kb = []
            for _k, _r, _a in _effective:
                _ref_ids = _ref_kb_ids(_r.get("references") or [])
                _label = "、".join(_kb_names.get(x, x) for x in _ref_ids)
                _src = _side_source[_k] or _r.get("source", "rag")
                _kb_id = _side_kb[_k][0]
                per_kb.append({"kb_id": _kb_id, "kb_name": _label,
                               "answer": _rewritten_answer.get(_k, _a), "source": _src})
            t_fuse = time.perf_counter()
            answer = await fuse_rag_answers(
                req.query, per_kb,
                tenant_id=tenant_id, user_id=user_id, trace_id=trace_id,
            )
            fusion_ms = int((time.perf_counter() - t_fuse) * 1000)
            collector.step(STAGE_FUSION, "多答案融合",
                           summary=f"融合 {len(per_kb)} 侧答案", t_start=t_fuse)
            source = "mixed"
            ontology_instances = (lr_result.get("ontology_instances") or []) if lr_ok else []
            rag_used = lr_result.get("rag_used", False) if lr_ok else False
            references_available = bool(references)
            ontology_instances_available = bool(ontology_instances)
        elif len(_effective) == 1:
            # 仅单侧有效
            _k, _r, _a = _effective[0]
            if _k == "lr":
                answer = _a
                source = _r.get("source", "rag")
                references = _r.get("references") or []
                ontology_instances = _r.get("ontology_instances") or []
                rag_used = _r.get("rag_used", False)
                references_available = bool(references)
                ontology_instances_available = bool(ontology_instances)
            else:
                # llm-wiki 型（openkb / jonex_wiki）：带上该侧引用。与 OntoRAG 无答案
                # + wiki 有答案的常见组合对齐——概念性提问引用不为 0 条。
                answer = _a
                source = _r.get("source") or _side_source[_k]
                references = _r.get("references") or []
                ontology_instances = []
                rag_used = _r.get("rag_used", False)
                references_available = bool(references)
                ontology_instances_available = False
            _failed_labels = [
                _side_label[fk]
                for fk in ("lr", "okb", "jw")
                if fk != _k and fk in _by_side
            ]
            collector.step(
                STAGE_FUSION, "多答案融合", status="skipped",
                summary=f"仅 {_side_label[_k]} 侧有效，其余侧失败/无答案",
                detail={"partial_failed": True, "failed_pipelines": _failed_labels},
            )
        else:
            # 三侧都无有效答案
            # [jonex] 再区分「被请求侧全部失败/降级」与「确实无答案」：混选时某一侧
            # 故障会退化成 degraded/Exception，若一律报 no_answer，用户看到的是
            # 「请调整查询」，而真实原因是服务不可用（排障方向被带偏）。
            _requested = [k for k in ("lr", "okb", "jw") if k in _by_side]
            _all_failed = bool(_requested) and all(
                (not isinstance(_by_side[k], dict)) or bool(_by_side[k].get("degraded"))
                for k in _requested
            )
            if _all_failed:
                logger.error("[search_mix] 全部管线查询失败（非无答案）sides=%s", _requested)
                answer = translate(
                    "msg.search.mix_unavailable",
                    fallback="知识库检索服务暂时不可用，请稍后重试；若持续出现请联系管理员。",
                )
            else:
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
                        "pipeline_groups": {
                            "OntoRAG": lightrag_ids,
                            "llm-wiki": openkb_ids,
                            "jonex-wiki": jonex_wiki_ids,
                        },
                        "source": source,
                    },
                ),
            )
            result["history_id"] = history.get("id")
            # [jonex] 数据运营二期：检索轨迹埋点（混合管线：
            # 只写 1 条 history → 只落 1 组事件，kb 取扇出前的 raw_ids）。
            await write_trace_events(
                tenant_id=tenant_id,
                request_id=trace_id or "",
                answer_id=result["history_id"],
                result=result,
                collector=collector,
                kb_ids=raw_ids,
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
