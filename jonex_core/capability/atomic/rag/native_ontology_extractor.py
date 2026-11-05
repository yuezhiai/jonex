#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
独立本体抽取器（NativeOntologyExtractor）— 入库 Stage4 唯一抽取入口（P4 接线）。

无状态纯函数组件：不依赖 LightRAG / Neo4j，只依赖 llm-gateway 与
compiled schema（TBox）。输入 chunk 原文（权威 chunk_id 经
GET /documents/chunks 读回，禁止按 content 重算，P0-1），按批 LLM 调用
同出实体 + 关系，采用两层类型体系（§5.8）；每批主抽取后经 gleaning
补抽轮（§5.4，[jonex] P2）回传已抽实体名只输出遗漏：

- 档① TBox 内：领域类型 + 定型关系 → 本体存储（Redis→reconcile→Neo4j）
- 档② TBox 外：兜底粗类型池（默认 ENTITY_TYPES）+ 自由关键词关系
  → 仅回写 LightRAG custom-kg（图覆盖不缩水）

设计文档：docs/ontology/ontology-native-extraction-execution-plan.md §5。
P4 接线为唯一抽取入口；P5 已删除旧 OntologyExtractor 链路。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from jonex_core.capability.atomic.rag.ontology_extractor import (
    ExtractionResult,
    ExtractedEntity,
    ExtractedRelation,
    _extract_concurrency,
    _load_json,
    _openai_chat,
    _post_validate,
    _type_alias_map,
    merge_entity_attributes,
    PROCEDURAL_GATE_KEY,
    _coerce_bool,
    _coerce_int,
    coerce_attribute_types,
    warn_cardinality_conflicts,
)

logger = __import__("logging").getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================


@dataclass
class NativeExtractionResult(ExtractionResult):
    """独立抽取结果：双集交付（§5.2/§5.8）。

    entities/relations        —— 两档全集（后校验前）：回写 LightRAG custom-kg
                                用，含兜底粗类型实体与 TBox 外自由关键词关系；
    ontology_entities/relations —— _post_validate 后的 TBox 档：本体存储侧
                                 （Redis task → reconcile → Neo4j）序列化用，
                                 兜底实体/无 relation_type 关系已被丢弃。
    """

    ontology_entities: List[ExtractedEntity] = field(default_factory=list)
    ontology_relations: List[ExtractedRelation] = field(default_factory=list)


# ============================================================
# 环境变量与默认值
# ============================================================


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in (
        "1", "true", "yes", "on",
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# 兜底粗类型池（§5.8 档②）：ONTOLOGY_NATIVE_FALLBACK_TYPES 优先，
# 默认读 ENTITY_TYPES（JSON 数组，与旧自抽粗类型口径一致；现网为 37 类）。
def load_fallback_types() -> List[str]:
    raw = os.getenv("ONTOLOGY_NATIVE_FALLBACK_TYPES") or os.getenv("ENTITY_TYPES", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except json.JSONDecodeError:
            logger.warning(
                "ONTOLOGY_NATIVE_FALLBACK_TYPES 非法 JSON，忽略: %.80s", raw,
            )
    # 环境缺省：读 LightRAG 默认粗类型（constants.DEFAULT_ENTITY_TYPES 子集，
    # 无环境变量时保持抽取可用；现网部署由 ENTITY_TYPES 覆盖为 37 中文粗类型）。
    return [
        "Person", "Organization", "Location", "Event", "Concept",
        "Method", "Content", "Data", "Artifact", "NaturalObject",
    ]


# ============================================================
# Prompt 模板（双层类型，§5.8）
# ============================================================
#
# [jonex] 2026-09-17 根因修复：补「无条件出口」规则（下方 rule 0）。
#
# 现象：TBox 外文档（歌词 mp3 等）整篇 0 实体，ok=False 而 errors=[]。
#       同 chunk 换旧链路（LightRAG 自抽）或开放式 prompt 则稳定 12+ 实体。
# 根因：类型体系只给了**条件出口**——「领域类型（优先）」+「兜底类型（次选，
#       无法归入时选最接近的）」+ 禁止 unknown。当原文与 TBox 及其关系定义
#       **完全不对口**时，这些条件无一被满足，而 prompt 又从未要求「无条件继续
#       抽取」，模型遂以返回空图收场：既满足「领域类型优先」，也不违反
#       「禁止 unknown」。是**遗漏**，不是某句约束写得太重。
# 证据（mp3 歌词 chunk，N=20，nonce 隔离防上游去重）：
#       · 本 prompt 0/20 全空；同 chunk 开放 prompt 20/20（均值 12.4 实体）。
#       · 逐条删除候选中和源——关系定义段 / TBox 的 attributes / aliases /
#         rule1 / rule3 / rule7 / TBox 退化裸名——**全部仍是 0/12**：删任何
#         一句都不会补上缺失的出口，故单点消融无效。
#       · 单加本规则即恢复：池版 12/12（均值 8.1）。删掉兜底池反而降到
#         12/12（均值 4.9）→ 两层设计本身是对的，**只缺这条出口**。
# 端到端验证（真实 NativeOntologyExtractor.extract()，非裸调 LLM）：
#       · 域外（kb_demo_internet / 李学仕-直觉.mp3）：
#         修复前 ok=False 实体 0 关系 0 errors=[]（复现线上症状）
#         → 修复后 ok=True 实体 10 关系 6（类型落兜底池：人物/地点/概念/物料；
#         ontology_entities=0 符合 §5.8 设计——兜底档不进本体库）。
#       · 域内回归（生产经验数据 / AOI检验记录.xlsx，375 chunk，两臂交错 ×3）：
#         修复前实体 [48,42,35] 均值 41.7 → 修复后 [49,46,49] 均值 48.0。
#         **无回归**。
#
# ⚠️ 测量陷阱（本项目踩过两次，后续做 A/B 必看）：llm-gateway 的幂等键只用于
#    计量去重，但**上游会按请求体去重**——同一 prompt 连发 N 次，N-1 次拿到的是
#    同一条响应，N=10 实为 N=1，会读出「10/10 vs 0/10」这种零方差的假干净分离。
#    凡重复采样必须给 prompt 追加唯一 nonce（见 scripts/_diag_ab_n20.py）。
#    同理，整篇文档的单次运行方差极大（实测实体数带宽 35~49），单次对照不可作数。
# 参照实现：旧链路 LightRAG prompt.py 有语义完全对应的一句——
#       "If none of the provided entity types apply, do not add new entity
#        type and classify it as `Other`."（无条件终点类型）。
#       本修复不引入 "Other" 这类两层体系外的新类型（避免污染下游类型口径），
#       而是把无条件出口指向既有兜底池，效果等同（8.1 vs 8.2）。
# 验证脚本：scripts/_diag_ab_n20.py / _diag_ablation.py / _diag_fix_isolate.py
#       / _diag_indomain.py。


NATIVE_SYSTEM_PROMPT = """你是本体抽取助手。从输入的原文 chunk 中抽取实体与关系，一次同出。

## 实体类型体系（两层，必须遵守）
1. 领域类型（优先）：{tbox_types}
   每项含 name（类型名）、aliases（中文同义表述：原文用词命中 aliases
   即归入该类型，不得落兜底类型）、attributes（该类型可提取的属性表：
   属性名 → 语义说明；未列出的属性禁止编造）。
   实体能归入领域类型时，type 必须填领域类型名。
2. 兜底类型（次选）：{fallback_types}
   无法归入任何领域类型时，从兜底类型中选择最接近的一个。
   禁止输出 "unknown" 作为实体类型。

## 领域关系定义（JSON）
{relations_json}

## 抽取要求
0. 无条件出口：若原文内容与上述领域类型及其关系定义完全不对口（如歌词、
   文学、闲聊、口语对话），领域类型与领域关系名一概不适用时，**不得以此
   为由返回空结果**——实体仍必须照常抽取，type 从「兜底类型」中选最接近
   的一个，relation_type 一律填空串 ""，并在 keywords 中输出自由关键词。
1. 关系：两端实体类型符合领域关系定义约束（source/target）的，relation_type
   填领域关系名；TBox 外的关系，relation_type 填空串 ""，并在 keywords 中
   输出 1-4 个自由关键词（逗号分隔）。
2. 每个实体必须输出 description（一句话概括，供检索使用）与 confidence (0.0-1.0)。
3. attributes 仅对领域类型定义过的属性提取；未定义属性留空，禁止编造。
4. 每个实体/关系必须输出 chunks：支持该结论的 chunk 编号数组（从输入的 [C编号] 引用）。
5. 同义词归一：{synonym_rule}
6. 只输出合法 JSON，不要 markdown 代码块。
7. 数值负例（[jonex] P10 批 2.6）：纯数字/规格值本身（如「309B」「256K」
   「12.3%」「v2.1」）不单独抽成实体——它们是属性值，应填入所属实体
   （如 Product）的 attributes（params/context_length 等）；除非该数值串
   是产品的正式名称且被原文作为独立对象指称（如「小米14」「GPT-4」）。"""

NATIVE_USER_PROMPT = """## 原文 chunk（本批 {n} 个，[C编号] 开头）
{chunks_json}

## 输出格式（JSON）
{{
  "entities": [
    {{"name": "实体名", "type": "实体类型", "aliases": ["别名"], "attributes": {{}},
      "description": "一句话描述", "confidence": 0.95, "chunks": [0, 2]}}
  ],
  "relations": [
    {{"source": "源实体名", "source_type": "实体类型", "target": "目标实体名",
      "target_type": "实体类型", "relation_type": "领域关系名或空串",
      "keywords": "关键词1,关键词2", "confidence": 0.9, "chunks": [0]}}
  ]
}}"""

# [jonex] P2 gleaning 补抽用户提示词（§5.4 定义，P2 实现）：
# 回传已抽实体名，只输出遗漏实体与至少一端为新实体的关系。
NATIVE_GLEANING_USER_PROMPT = """## 原文 chunk（本批 {n} 个，[C编号] 开头）
{chunks_json}

## 本轮已抽取实体（不得重复输出，也不得换别名重复）
{known_names}

## 任务：补抽上一轮遗漏的内容
1. 只输出 name 不在「本轮已抽取实体」清单中的实体；清单内实体一律跳过。
2. 关系只输出至少一端是新实体的；两端都是清单内实体的关系不输出。
3. 实体/关系的格式与 chunks 引用规则同主抽取（[C编号] 引用原文）。

## 输出格式（JSON）
{{
  "entities": [
    {{"name": "实体名", "type": "实体类型", "aliases": ["别名"], "attributes": {{}},
      "description": "一句话描述", "confidence": 0.95, "chunks": [0, 2]}}
  ],
  "relations": [
    {{"source": "源实体名", "source_type": "实体类型", "target": "目标实体名",
      "target_type": "实体类型", "relation_type": "领域关系名或空串",
      "keywords": "关键词1,关键词2", "confidence": 0.9, "chunks": [0]}}
  ]
}}"""


def _build_synonym_rule(tbox: dict) -> str:
    synonyms = tbox.get("synonyms") or []
    if not synonyms:
        return "本体定义未提供同义词组，实体名按原文规范化输出。"
    groups = json.dumps(synonyms, ensure_ascii=False)
    return (
        "本体定义含 synonyms 段（同义词组，每组含 canonical 标准词与 terms 等价词）："
        f"{groups}。实体命中某组任一词时，name 必须填该组 canonical。"
    )


# ============================================================
# Procedural 抽取子管线（P7，M2）
# ============================================================
#
# 触发条件（gate 双 AND，§5 P2-12）：
#   ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED（进程级总闸，默认关）
#   AND options.procedural（任务层按「schema 含 Action」组装，每文档条件）。
# 未声明 Action 的 KB 完全不进这条路径，现有行为零变化。

# 操作类信号词（预筛零成本跳过；M2 口径）
_PROCEDURAL_SIGNAL_RE = re.compile(
    r"步骤|第\s*[一二三四五六七八九十\d]+\s*[步条项]|"
    r"(?:^|\n)[ \t]*\d+[.、)）]\s*\S|"          # 有序列表结构
    r"操作|配置|执行|点击|输入|设置|请先|然后|接着|最后|"
    r"办理|申请|提交|审批|核验|验证|通知|回滚",
)

PROCEDURAL_SYSTEM_PROMPT = """你是规程知识抽取助手，从输入的操作规程 chunk 中抽取业务技能（Skill）与动作（Action）及挂接关系。

## 核心字段（宁缺毋假，D7/D15）
1. definition：动作的精确业务含义，**必须与相近动作区分**（如「冻结」vs「止付」的差异）。
2. preconditions：执行前必须满足/核验什么。
3. authorization：谁能执行（角色/职级/审批）。
4. failure_policy：失败处理边界——什么失败回滚、什么失败不回滚。
5. atomic_effects：本动作在同一事务内完成的主状态变更（失败会整体回滚的部分）。
6. follow_ups：提交后触发的联动（通知、审计、调用其他系统等，失败不撤销主操作）。
7. audit_required：原文**明确要求**审计、留痕、记录日志或存档时填 true，明确写不需要时填 false，
   原文没提到时填 null。
   **以上 7 个字段文档未写时必须留空 / 空数组 / null，禁止自行推断编造。**
   尽量摘原文短句，不要改写成原文没有的说法。

## authorization 与 performed_by 必须分开
- authorization 只填原文**明确写出的权限要求**：如「XX级及以上可执行」「须经XX审批」
  「权限要求：XX」「仅限XX」「XX不得自行……」「授权XX」。
- 原文只说明「由谁来做」（如「客服专员提交申请」「风险专员执行止付」），这是执行人，
  只填 performed_by，**authorization 留空数组**。
- 执行人不等于有权限的人：不能因为某人执行了该动作，就推断他是权限要求。

## 挂接端点（防臆造）
acts_on / uses_tool / performed_by / requires / results_in 的端点名**必须逐字出现在 chunk 原文**；
原文没有对应端点时留空数组。

## 输出要求
- 只输出合法 JSON，不要 markdown 代码块。
- skills/actions 均为数组；本 chunk 无规程内容时输出空数组。
- step_no 为步骤序号（原文编号优先）；part_of_skill 为所属技能名（与 skills 中 name 一致）。
- next_action 为紧邻下一步动作名；没有则留空字符串。
- action_type 取 operation|decision|interaction|validation 之一。"""

PROCEDURAL_USER_PROMPT = """## 原文 chunk
{chunk_text}

## 输出格式（JSON）
{{
  "skills": [
    {{"name": "技能名", "goal": "技能目标", "attributes": {{
        "definition": "语义定义", "skill_domain": "所属领域/模块", "prerequisites": "技能级前置条件"}}}}
  ],
  "actions": [
    {{"name": "动作名（动词+宾语）", "step_no": 1, "action_type": "operation",
      "definition": "精确业务含义，与相近动作区分",
      "part_of_skill": "所属技能名", "preconditions": [], "authorization": [],
      "failure_policy": "", "atomic_effects": [], "follow_ups": [], "audit_required": null,
      "acts_on": [], "uses_tool": [], "performed_by": [],
      "requires": [], "results_in": [], "next_action": ""}}
  ]
}}"""


def _prefilter_procedural_chunks(
    chunks: List[Dict[str, Any]],
    chunk_max: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """信号词预筛（M2 零成本跳过）：只取文本 chunk、正文命中操作类信号。

    按信号命中次数降序取前 chunk_max（默认 ONTOLOGY_PROCEDURAL_CHUNK_MAX=30）。
    表格/图片 chunk 一律不进（表格走 Row-as-Object，图片无操作语义）。
    """
    if chunk_max is None:
        chunk_max = _env_int("ONTOLOGY_PROCEDURAL_CHUNK_MAX", 30)
    hits: List[Tuple[int, Dict[str, Any]]] = []
    for c in chunks:
        if NativeOntologyExtractor._is_table_chunk(c):
            continue
        text = str(c.get("content") or "")
        n = len(_PROCEDURAL_SIGNAL_RE.findall(text))
        if n > 0:
            hits.append((n, c))
    hits.sort(key=lambda t: t[0], reverse=True)
    return [c for _n, c in hits[:chunk_max]]


def _procedural_relations_from_item(
    item: Dict[str, Any],
    skill_names: set,
    action_names: set,
) -> List[ExtractedRelation]:
    """M2 输出格式的动作对象 → 挂接关系列表（端点类型 unknown，_post_validate
    端点集合校验按通配保留；端点存在性由 endpoint_verifier 技能校验）。"""
    relations: List[ExtractedRelation] = []
    src = str(item.get("name") or "").strip()
    if not src:
        return relations

    def _endpoints(key: str) -> List[str]:
        raw = item.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(x).strip() for x in raw if str(x).strip()]

    skill = str(item.get("part_of_skill") or "").strip()
    if skill and skill in skill_names:
        step_no = _coerce_int(item.get("step_no"))
        relations.append(ExtractedRelation(
            source_name=skill, source_type="Skill",
            target_name=src, target_type="Action",
            relation_type="HAS_STEP", confidence=0.9,
            # 边属性是 step_no 的权威来源（D3），落整数，检索侧排序不受字符串序影响
            attributes={"step_no": step_no} if step_no is not None else {},
        ))
    nxt = str(item.get("next_action") or "").strip()
    if nxt:
        relations.append(ExtractedRelation(
            source_name=src, source_type="Action",
            target_name=nxt, target_type="Action",
            relation_type="NEXT_STEP", confidence=0.9,
        ))
    # 挂接关系类型 → M2 输出键
    hook_map = [
        ("ACTS_ON", "acts_on"), ("USES_TOOL", "uses_tool"),
        ("PERFORMED_BY", "performed_by"), ("REQUIRES", "requires"),
        ("RESULTS_IN", "results_in"),
    ]
    for rel_type, key in hook_map:
        for endpoint in _endpoints(key):
            relations.append(ExtractedRelation(
                source_name=src, source_type="Action",
                target_name=endpoint, target_type="unknown",
                relation_type=rel_type, confidence=0.9,
            ))
    return relations


def _norm_contract_field(value: Any) -> Any:
    """契约字段规范化：list 逐项 strip 去空保持数组；标量 str 化 strip。

    LLM 输出 contract 字段按 prompt 是数组（authorization/preconditions 等），
    不能整体 str() 字符串化；空串/空列表由调用方过滤（宁缺毋假）。
    """
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return str(value or "").strip()


def _parse_procedural_item(
    item: Dict[str, Any],
    kind: str,
) -> Optional[ExtractedEntity]:
    """procedural LLM 输出的 skills/actions 条目 → 实体（属性名与 §2.1 对齐）。"""
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    if kind == "skill":
        attrs = dict(item.get("attributes") or {})
        out = {
            "definition": _norm_contract_field(attrs.get("definition")),
            "skill_domain": _norm_contract_field(attrs.get("skill_domain")),
            "goal": _norm_contract_field(item.get("goal")),
            "prerequisites": _norm_contract_field(attrs.get("prerequisites")),
        }
        return ExtractedEntity(
            canonical_name=name, entity_type="Skill",
            attributes={k: v for k, v in out.items() if v},
            description=str(attrs.get("definition") or "")[:2000],
            confidence=0.9, extraction_method="procedural_llm",
        )
    # action
    attrs = {
        "definition": _norm_contract_field(item.get("definition")),
        "action_type": _norm_contract_field(item.get("action_type")),
        "preconditions": _norm_contract_field(item.get("preconditions")),
        "authorization": _norm_contract_field(item.get("authorization")),
        "failure_policy": _norm_contract_field(item.get("failure_policy")),
        "atomic_effects": _norm_contract_field(item.get("atomic_effects")),
        "follow_ups": _norm_contract_field(item.get("follow_ups")),
    }
    # audit_required 是三态：true / false / 未提及（null 不落键）。不能走 _norm_contract_field，
    # 否则 False 会被 str 化成 "False" 再被当成非空值保留。
    audit = _coerce_bool(item.get("audit_required"))
    if audit is not None:
        attrs["audit_required"] = audit
    step_no = _coerce_int(item.get("step_no"))
    if step_no is not None:
        attrs["step_no"] = step_no
    return ExtractedEntity(
        canonical_name=name, entity_type="Action",
        attributes={k: v for k, v in attrs.items()
                    if v is False or (v not in ("", None) and v != [])},
        description=str(item.get("definition") or "")[:2000],
        confidence=0.9, extraction_method="procedural_llm",
    )


def _procedural_enabled(procedural_opt: Any) -> bool:
    """procedural gate 任务层条件：options.procedural 非 None 即启用。

    task_manager 按「schema 含 Action」组装空 dict 占位——空 dict 是 falsy，
    不能用 truthiness 判定（会永远关闸）。总闸 env 由调用方另行 AND。
    """
    return procedural_opt is not None


def _kb_procedural_gate(schema: Optional[dict]) -> bool:
    """库级 procedural 开关：kb-service 派单时写进 prompt_schema 顶层，只认值为 True。

    缺键（老任务 / 执行方自行回落 CompiledSchemaClient 取到的 schema）一律按关处理 ——
    内置 TBox 都含 Action，没有这一道就等于总闸一开全量开。
    """
    return bool(schema) and schema.get(PROCEDURAL_GATE_KEY) is True


def procedural_gate_open(procedural_opt: Any, schema: Optional[dict]) -> bool:
    """procedural 是否真正执行 = 每文档条件（schema 含 Action）AND 库级开关 AND 进程级总闸。"""
    return (
        _procedural_enabled(procedural_opt)
        and _kb_procedural_gate(schema)
        and _env_flag("ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED", False)
    )


def _parse_procedural_results(
    raw_items: List[Tuple[Optional[str], Optional[str], Optional[Exception], Dict[str, Any]]],
    schema: dict,
) -> Tuple[List[ExtractedEntity], List[ExtractedRelation], List[str]]:
    """解析 procedural 各 chunk 的 LLM 输出 → Skill/Action 实体 + 挂接关系。"""
    entities: List[ExtractedEntity] = []
    relations: List[ExtractedRelation] = []
    errors: List[str] = []
    for i, (raw, _fr, err, chunk) in enumerate(raw_items):
        if err is not None:
            errors.append(f"procedural chunk#{i} 调用失败: {err}")
            continue
        data = _load_json(raw or "")
        if data is None:
            errors.append(f"procedural chunk#{i} 输出非合法 JSON")
            continue
        # procedural 每 chunk 一次调用，产出的出处就是这个 chunk：直接记上，
        # 不等后面按名字回填（回填按名字匹配，可能挂到别的 chunk 上）
        chunk_id = str((chunk or {}).get("chunk_id") or "")
        origin = (
            [{"source_id": chunk_id, "file_path": str(chunk.get("file_path") or "")}]
            if chunk_id else []
        )
        skill_names: set = set()
        action_names: set = set()
        for s in data.get("skills", []):
            e = _parse_procedural_item(s, "skill")
            if e is not None:
                e.source_chunks = [dict(sc) for sc in origin]
                entities.append(e)
                skill_names.add(e.canonical_name)
        for a in data.get("actions", []):
            e = _parse_procedural_item(a, "action")
            if e is not None:
                e.source_chunks = [dict(sc) for sc in origin]
                entities.append(e)
                action_names.add(e.canonical_name)
        for a in data.get("actions", []):
            for r in _procedural_relations_from_item(a, skill_names, action_names):
                r.source_chunks = [dict(sc) for sc in origin]
                relations.append(r)
    # M2 上限：每文档 Action 数截断（超出按出现顺序丢弃实体与其挂接）
    max_actions = _env_int("ONTOLOGY_PROCEDURAL_MAX_ACTIONS", 100)
    action_names_all = {
        e.canonical_name for e in entities if e.entity_type == "Action"
    }
    if len(action_names_all) > max_actions:
        keep = list(action_names_all)[:max_actions]
        dropped = action_names_all - set(keep)
        entities = [
            e for e in entities
            if e.entity_type != "Action" or e.canonical_name not in dropped
        ]
        relations = [
            r for r in relations
            if not (
                (r.source_type == "Action" and r.source_name in dropped)
                or (r.target_type == "Action" and r.target_name in dropped)
            )
        ]
        errors.append(
            f"procedural Action 超上限 {max_actions}，截断 {len(dropped)} 个"
        )
    return entities, relations, errors


# ============================================================
# NativeOntologyExtractor
# ============================================================


class NativeOntologyExtractor:
    """独立本体抽取器（进程内调用；P4 起 Stage4 唯一抽取入口）。

    不依赖 LightRAG 图抽取结果，只依赖 llm-gateway 与 compiled schema。
    复用 ontology_extractor 的 `_openai_chat`（计量头）、`_load_json`、
    `_post_validate`（本体存储侧过滤）。
    """

    def __init__(self, tbox: Optional[dict] = None):
        # P2 纯新增：tbox 经 extract() 参数传入；构造参数仅便于单测注入。
        self._tbox = tbox

    # ── 主流程 ──────────────────────────────

    async def extract(
        self,
        chunks: List[Dict[str, Any]],
        tbox: Optional[dict] = None,
        scope: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> NativeExtractionResult:
        """执行独立本体抽取（§5.3 七步流程）。

        Args:
            chunks: 原文片段（GET /documents/chunks 读回）：
                [{"chunk_id": "chunk-<md5>", "content": "...", "file_path": "...",
                  "chunk_type": "text|image|table_row|table"}]
                chunk_id 为权威 ID，禁止按 content 重算（§5.6 P0-1）。
            tbox: compiled schema 的 prompt_schema（entity_types/relation_types/
                synonyms/disambiguation）。为空/不可用 → ok=false 整体不抽取
                （§5.8 P1-4：不得「空集全丢弃」静默清空本体）。
            scope: {"tenant_id", "knowledge_base_id", "document_id", "trace_id"}。
            options: 见 §5.4（batch_size/gleaning/json_mode/max_tokens/
                temperature/procedural；procedural 为 P7 预留，本阶段恒 null）。

        Returns:
            NativeExtractionResult: entities/relations = 两档全集（LightRAG 回写）；
                ontology_entities/ontology_relations = TBox 档（本体存储）。
        """
        opts = dict(options or {})
        # [jonex] P5：保留环境变量接线（§6.2）——opts 显式值优先，缺失时读 env。
        # env 未设时默认值 = P4 已验证的 native 现状（行为零变化）。
        if opts.get("batch_size") is None:
            opts["batch_size"] = _env_int("ONTOLOGY_EXTRACT_BATCH_SIZE", 35)  # 语义：每批 chunk 数
        if opts.get("max_tokens") is None:
            opts["max_tokens"] = _env_int("ONTOLOGY_EXTRACT_MAX_TOKENS", 8192)
        if opts.get("json_mode") is None:
            opts["json_mode"] = _env_flag("ONTOLOGY_EXTRACT_JSON_MODE", True)
        schema = tbox if tbox is not None else self._tbox
        if not schema or not (schema.get("entity_types") or schema.get("relation_types")):
            return NativeExtractionResult(
                ok=False, errors=["TBox 不可用，跳过独立本体抽取"],
            )

        # ① 预处理：表格 chunk 剔除（O6，Row-as-Object 确定性接管）。
        # [jonex] P5-var（方案 B）：仅当任务层确认 Row-as-Object 已接管表格
        # （content_list 存在 type=="table" item，由 options.table_objects_handled
        # 告知）才剔除表格 chunk；无接管时表格 chunk 进 native 文本抽取
        # （LLM 兜底，修本体召回）。无该键 = 旧行为（恒剔除）。
        table_handled = bool(opts.get("table_objects_handled", True))
        usable = [
            c for c in chunks
            if not (table_handled and self._is_table_chunk(c))
        ]
        if not usable:
            # [jonex] P0-b（2026-09-13 线上 OOM 复盘）：这条早退原先只把原因塞进
            # errors 字符串——不落日志，调用方也不检查 result.ok，于是「主抽取
            # 一个 chunk 都没跑」在日志里完全隐形。线上那轮 financial_metric.md
            # 的全部 46290 个 chunk 都是表格 chunk，本函数在此直接返回、0 次 LLM
            # 调用；4.6 小时 / 46290 chunk 的消耗全部发生在调用方随后的「零产出
            # 兜底」里。事后只能从「attrs 観測 entities=...」这类**只在有产出时
            # 才打印**的日志反推，排障成本极高。此处补显式告警，把「主抽取空跑」
            # 变成可 grep 的信号（与调用方 P0-a 的兜底规模闸门配套）。
            table_chunk_count = sum(
                1 for c in chunks if self._is_table_chunk(c)
            )
            if chunks:
                reason = f"全部为表格 chunk（{table_chunk_count}/{len(chunks)}）"
            else:
                reason = "输入 chunk 为空"
            logger.warning(
                "[jonex][P0-b] 主抽取无可用 chunk，整体跳过（本次 0 次 LLM 调用）: "
                "chunks=%d 表格 chunk=%d table_objects_handled=%s 原因=%s ——"
                "若表格确定性映射同时零产出，调用方的零产出兜底将接管表格内容",
                len(chunks), table_chunk_count, table_handled, reason,
            )
            return NativeExtractionResult(
                ok=False,
                errors=[
                    f"无可用 chunk（{reason}），跳过本体抽取"
                ],
            )

        # ② 分批：[jonex] P1 输出预算反推每批 chunk 上限——
        #   out_cap = max_tokens // ONTOLOGY_EXTRACT_PER_CHUNK_OUTPUT_RESERVE
        # （默认 8192//1000≈8 chunk/批，每 chunk 预留 1000 token 产出预算，
        # 缓解大批判注意力稀释的自然少输出），与 batch_size 取小；
        #   输入侧 token 预算保留为 AND 上限（_pack_batches 双条件切批）。
        budget = _env_int("ONTOLOGY_EXTRACT_BATCH_BUDGET_TOKENS", 24000)
        batch_size = max(1, int(opts.get("batch_size", 35)))
        per_chunk_reserve = max(1, _env_int("ONTOLOGY_EXTRACT_PER_CHUNK_OUTPUT_RESERVE", 1000))
        out_cap = max(1, min(int(opts.get("max_tokens", 8192)) // per_chunk_reserve, batch_size))
        batches = self._pack_batches(usable, budget, out_cap)

        # ③ 单次 LLM 调用/批（JSON mode，经 llm-gateway 计量头）
        # [jonex] P5-var：多次独立采样取并集（self-consistency）——上游
        # temperature=0 非位精确，同输入 4 次实测 25/27/31/36 个实体。
        # 主批跑 ONTOLOGY_NATIVE_SAMPLE_RUNS 次（默认 2，成本 ≈2x），
        # 各次产出全量解析后由 ⑤ _merge_entities 按 canonical_name
        # 并集合并（描述取长、aliases/chunks 聚合，语义已具备）。
        sample_runs = self._effective_sample_runs(opts, len(usable))
        if sample_runs < self._sample_runs(opts):
            logger.info(
                "大文档降采样: usable=%d ≥ %s，采样次数 %d → %d（省一轮主批）",
                len(usable), os.getenv("ONTOLOGY_EXTRACT_BIG_DOC_CHUNKS", "500"),
                self._sample_runs(opts), sample_runs,
            )
        raw_items = await self._run_batches(batches, schema, scope, opts)
        for _ in range(sample_runs - 1):
            raw_items.extend(
                await self._run_batches(batches, schema, scope, opts)
            )

        # ④ 解析 + 别名归一（synonyms canonical）
        # 先于 ③′ 执行：procedural 的错误需 extend 到 errors，且 ④ 的解析结果
        # 与 ③′ 互不依赖（procedural 关闸时此顺序与旧版行为完全一致）。
        entities, relations, errors, any_batch_ok = self._parse_results(
            raw_items, schema,
        )

        # ④′ [jonex] P2 gleaning 补抽（§5.4）：每批回传已抽实体名只输出遗漏，
        # 默认 1 轮（ONTOLOGY_EXTRACT_GLEANING）。补齐大批判注意力稀释的
        # 自然少输出；产出并入主抽取，走 ⑤ 合并 / ⑦ 端点兜底同路径。
        glean_rounds = self._gleaning_rounds(opts)
        if glean_rounds > 0:
            g_entities, g_relations, g_errors = await self._run_gleaning(
                raw_items, schema, scope, opts, glean_rounds,
            )
            entities.extend(g_entities)
            relations.extend(g_relations)
            errors.extend(g_errors)

        # ③′ procedural 子管线（P7 M2）：总闸 env AND 每文档条件（options.procedural）
        # 双 AND 生效，未声明 Action 的 KB 完全不进这条路径（§5 P2-12 gate 关系）。
        # 任务层条件用「非 None」（task_manager 传空 dict 占位，空 dict falsy，
        # 用 truthiness 会永远关闸——见 _procedural_enabled）。
        # 三道 AND：每文档条件 options.procedural（schema 含 Action）、库级开关
        # （prompt_schema.procedural_extract，kb-service 派单写入）、进程级总闸 env
        procedural_opt = opts.get("procedural")
        procedural_enabled = procedural_gate_open(procedural_opt, schema)
        proc_entities: List[ExtractedEntity] = []
        proc_relations: List[ExtractedRelation] = []
        if procedural_enabled:
            proc_chunks = _prefilter_procedural_chunks(usable)
            if proc_chunks:
                proc_items = await self._run_procedural(
                    proc_chunks, schema, scope, opts,
                )
                proc_entities, proc_relations, proc_errors = _parse_procedural_results(
                    proc_items, schema,
                )
                errors.extend(proc_errors)
                logger.info(
                    "procedural: chunks=%d actions=%d skills=%d relations=%d",
                    len(proc_chunks),
                    sum(1 for e in proc_entities if e.entity_type == "Action"),
                    sum(1 for e in proc_entities if e.entity_type == "Skill"),
                    len(proc_relations),
                )
                # [jonex] P9（批 5）契约字段空置率：authorization/failure_policy
                # 为空的 Action 占比。空置率异常高说明语料本就不含契约信息，
                # 或预筛信号词过于宽松（宁缺毋假：置空≠无约束，M4 按 D15 消费）。
                proc_actions = [e for e in proc_entities if e.entity_type == "Action"]
                if proc_actions:
                    def _vacant(key: str) -> int:
                        return sum(
                            1 for e in proc_actions
                            if not (e.attributes or {}).get(key)
                        )
                    # audit_required 是布尔：False 也是「写明了」，只有缺键才算空置
                    audit_vacant = sum(
                        1 for e in proc_actions
                        if "audit_required" not in (e.attributes or {})
                    )
                    n_act = len(proc_actions)
                    logger.info(
                        "procedural 契约空置率: actions=%d "
                        "authorization_vacant=%.0f%% failure_policy_vacant=%.0f%% "
                        "atomic_effects_vacant=%.0f%% follow_ups_vacant=%.0f%% "
                        "audit_required_vacant=%.0f%%",
                        n_act,
                        100 * _vacant("authorization") / n_act,
                        100 * _vacant("failure_policy") / n_act,
                        100 * _vacant("atomic_effects") / n_act,
                        100 * _vacant("follow_ups") / n_act,
                        100 * audit_vacant / n_act,
                    )
            else:
                proc_entities, proc_relations = [], []

        # procedural 产出并入主抽取
        if procedural_enabled:
            entities.extend(proc_entities)
            relations.extend(proc_relations)

        # ⑤ 跨批/跨 chunk 合并：同名实体描述取长、aliases 并集、source_chunks 聚合；
        # [jonex] P5-var 同名边合并（多次采样同一条边重复出现，不合并会翻倍）
        entities = self._merge_entities(entities)
        relations = self._merge_relations(relations)

        # ⑥ [jonex] P3 chunk 引用确定性回填：canonical_name + aliases 归一化
        # 匹配 chunk 正文（LLM 批量抽取的 chunks 引用常失真——22 chunk 仅
        # 4 个被引用且集中于首尾），命中并集追加，不删 LLM 声明。
        # [jonex] P2：await —— 内部是 O(实体×chunk) 纯计算，必须让出点
        # 才能保持服务可响应（详见该方法 docstring）。
        await self._backfill_source_chunks(entities, relations, usable)

        # ⑥′ [jonex] P10 批 2.7：漏抽 chunk 单 chunk 补跑——批量抽取下
        # 注意力稀释，短 chunk/靠后 chunk 常零引用（实测 22 chunk 中
        # chunk=2 判「合理空置」实为 MTP 类内容漏抽）。回填后仍未被任何
        # 实体/关系 source_chunks 引用的 chunk 单独成批补跑一轮（单 chunk
        # 批 = 满注意力，不乘采样次数控制成本），产出并入走 ⑦/④ 同路径。
        if _env_flag("ONTOLOGY_EXTRACT_MISSING_CHUNK_RERUN", True) and len(usable) > 1:
            referenced = {
                sc.get("source_id") or ""
                for e in entities
                for sc in e.source_chunks
            } | {
                sc.get("source_id") or ""
                for r in relations
                for sc in r.source_chunks
            }
            missing = [
                c for c in usable
                if (c.get("chunk_id") or "") and c.get("chunk_id") not in referenced
            ]
            # [jonex] P0-3 配套闸门：单 chunk 补跑 = 每个零引用 chunk 一次 LLM
            # 调用。source_chunks 配额（ONTOLOGY_SOURCE_CHUNKS_MAX）会收窄
            # referenced 的覆盖广度，超大文档上缺失数可从数千涨到数万——不拦
            # 就成了「修内存换烧 LLM 调用 + 又一轮长尾」。小文档实测 missing
            # 个位数，默认阈值不对其生效；0 = 关闭闸门（旧行为）。
            max_missing = max(0, _env_int("ONTOLOGY_MISSING_CHUNK_RERUN_MAX", 1000))
            if missing and max_missing and len(missing) > max_missing:
                logger.warning(
                    "missing-chunk rerun 跳过: 零引用 chunk %d 个 > 上限 %d "
                    "（补跑需 %d 次单 chunk LLM 调用）；置 "
                    "ONTOLOGY_MISSING_CHUNK_RERUN_MAX=0 关闭该闸门",
                    len(missing), max_missing, len(missing),
                )
            elif missing:
                rerun_items = await self._run_batches(
                    [[c] for c in missing], schema, scope, opts,
                )
                r_entities, r_relations, r_errors, _r_ok = self._parse_results(
                    rerun_items, schema,
                )
                errors.extend(r_errors)
                entities.extend(r_entities)
                relations.extend(r_relations)
                entities = self._merge_entities(entities)
                relations = self._merge_relations(relations)
                logger.info(
                    "missing-chunk rerun: %d/%d chunk 零引用，单批补跑 "
                    "补得 %d 实体 / %d 关系",
                    len(missing), len(usable),
                    len(r_entities), len(r_relations),
                )

        # ⑥″ TBox 声明技能链（§2.5）：声明了 extraction_skills 的类型按声明
        # 顺序串行（refine 先于 constrain）；未声明类型不跑技能。reject 只丢单实体/挂接（D12）。
        # 放在 ⑥ 回填与 ⑥′ 漏抽补跑之后：① 补跑产出的实体同样要经过约束技能；
        # ② 此时每个实体已带 source_chunks，原文比对可限定在它自己的 chunk 内
        #   （chunk_text_by_id），而不是整篇文档。
        if procedural_enabled or any(
            et.get("extraction_skills") for et in schema.get("entity_types", [])
        ):
            from jonex_core.capability.atomic.rag.ontology_extraction_skills import (
                run_entity_skill_chains,
            )

            entities, relations, rejects, skill_logs = await run_entity_skill_chains(
                entities, relations, schema,
                chunk_texts=[str(c.get("content") or "") for c in usable],
                params={"name_maxlen": _env_int("ONTOLOGY_PROCEDURAL_NAME_MAXLEN", 30)},
                chunk_text_by_id={
                    str(c.get("chunk_id")): str(c.get("content") or "")
                    for c in usable if c.get("chunk_id")
                },
            )
            for rj in rejects:
                logger.info(
                    "ontology skill reject: skill=%s kind=%s name=%s reason=%s",
                    rj.skill, rj.kind, rj.name, rj.reason,
                )
            for sl in skill_logs:
                logger.debug("ontology skill log: %s", sl)

        # ⑦ 端点兜底：关系端点缺失 → 补 entity_type="unknown" 正式实体；
        # [jonex] P4 继承来源关系 source_chunks（旧实现恒空 → 图节点
        # source_id/file_path 空串脏数据）。
        if _env_flag("ONTOLOGY_ENTITY_BACKFILL_ENDPOINTS", True):
            known = {e.canonical_name for e in entities}
            endpoints = {r.source_name for r in relations} | {r.target_name for r in relations}
            for nm in endpoints - known:
                if nm:
                    seen_sc = set()
                    rel_chunks = []
                    for r in relations:
                        if r.source_name != nm and r.target_name != nm:
                            continue
                        for sc in r.source_chunks:
                            key = (sc.get("source_id"), sc.get("file_path"))
                            if key not in seen_sc:
                                seen_sc.add(key)
                                rel_chunks.append(dict(sc))
                    entities.append(ExtractedEntity(
                        canonical_name=nm, entity_type="unknown", aliases=[nm],
                        confidence=0.3, extraction_method="endpoint_backfill",
                        source_chunks=rel_chunks,
                    ))

        # ④ 后校验：本体存储侧只落 TBox 档（丢兜底粗类型/无 relation_type 关系）。
        # [jonex] 兜底类型集合在此注入而非由 _post_validate 自行导入——本模块已
        # import ontology_extractor，反向 import 会成环。开关 ONTOLOGY_KEEP_FALLBACK_ENTITIES
        # 默认关；开时 _post_validate 放行落在这批兜底类型上的实体与关系端点。
        # 属性按 TBox 类型归一（boolean 认不出删键、number 只转纯数字），
        # 放在后校验之前：_post_validate 之后的观测日志看到的已是归一后的值。
        coerce_stats = coerce_attribute_types(entities, schema)
        if coerce_stats["coerced"] or coerce_stats["unparsed"]:
            logger.info(
                "ontology attrs 类型归一: coerced=%d unparsed=%d",
                coerce_stats["coerced"], coerce_stats["unparsed"],
            )

        ont_entities, ont_relations = _post_validate(
            entities, relations, schema,
            fallback_types=set(load_fallback_types()),
        )

        # one_to_one 多出边、同技能 step_no 撞号：只告警不删（函数内已逐条打 warning）
        warn_cardinality_conflicts(ont_relations, schema)

        # [jonex] P5-var 观测：with_attrs 比例 + TBox 属性零命中清单。
        # 区分两类零命中：语料无对应值（TBox 有定义但文档没提）与
        # 结构上不可能（该类型本就无属性定义，attributes 恒空）。
        if ont_entities:
            tbox_attr_names = {
                a.get("name", "")
                for et in schema.get("entity_types", [])
                for a in et.get("attributes", [])
                if a.get("name")
            }
            used_attr_names = {
                an
                for e in ont_entities
                for an in (e.attributes or {}).keys()
            }
            with_attrs = sum(1 for e in ont_entities if e.attributes)
            logger.info(
                "ontology attrs 观测: entities=%d with_attrs=%d (%.0f%%) "
                "used_attr_names=%d tbox_attr_names=%d zero_hit=%s",
                len(ont_entities),
                with_attrs,
                100 * with_attrs / len(ont_entities),
                len(used_attr_names),
                len(tbox_attr_names),
                sorted(tbox_attr_names - used_attr_names),
            )

        ok = any_batch_ok and bool(entities)
        if not any_batch_ok:
            errors.append("所有批次 LLM 调用失败")
        return NativeExtractionResult(
            entities=entities,
            relations=relations,
            ontology_entities=ont_entities,
            ontology_relations=ont_relations,
            errors=errors,
            ok=ok,
        )

    # ── ① 表格 chunk 剔除 ─────────────────────

    @staticmethod
    def _is_table_chunk(chunk: Dict[str, Any]) -> bool:
        """O6：chunk_type 或 file_path ctype 锚点为表格则剔除。"""
        ctype = chunk.get("chunk_type", "")
        if ctype in ("table_row", "table"):
            return True
        fp = chunk.get("file_path") or ""
        if not fp or "ctype=" not in fp:
            return False
        try:
            from jonex_core.common.file_source_util import parse_file_source

            return parse_file_source(fp).get("chunk_type") in ("table_row", "table")
        except (ImportError, AttributeError, NameError):
            raise  # 代码 bug，不掩盖
        except Exception as exc:  # noqa: BLE001 — 解析失败按非表格处理
            logger.debug("native O6 file_source 解析降级: %s", exc)
            return False

    # ── ② token 预算装箱 ──────────────────────

    _TOKEN_SPLIT_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        r"""无依赖 token 估算：\w+ 词元 + 独立标点 ≈ 英文 token 数。

        中文按单字计（\w+ 会把整段中文视为一个词元），需额外按字符补偿：
        估算值 = 词元数 + 非 ASCII 字符数（近似中文逐字 token）。
        """
        if not text:
            return 0
        words = len(cls._TOKEN_SPLIT_RE.findall(text))
        non_ascii = sum(1 for ch in text if ord(ch) > 127)
        return words + non_ascii

    @classmethod
    def _pack_batches(
        cls,
        chunks: List[Dict[str, Any]],
        budget_tokens: int,
        batch_size: int,
    ) -> List[List[Dict[str, Any]]]:
        """按 token 预算贪心装箱；单 chunk 超预算则单 chunk 成批。

        双上限取小（§5.4 + [jonex] P1）：batch_size 传输出预算反推值
        （max_tokens // PER_CHUNK_OUTPUT_RESERVE 与配置上限取小），
        输入侧 budget_tokens 保留为 AND 上限。
        """
        batches: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        current_tokens = 0
        for chunk in chunks:
            cost = cls._estimate_tokens(str(chunk.get("content") or ""))
            if current and (
                current_tokens + cost > budget_tokens or len(current) >= batch_size
            ):
                batches.append(current)
                current, current_tokens = [], 0
            current.append(chunk)
            current_tokens += cost
        if current:
            batches.append(current)
        return batches

    # ── ③ LLM 分批调用（含截断检测） ──────────

    # 属性说明渲染上限（P0）：属性行只保留 name + description，截断控制
    # system prompt 体积（default.yaml Action 12 属性全量渲染会反挤输出空间）。
    _ATTR_DESC_MAX = 40

    @staticmethod
    def _render_type_def(et: Dict[str, Any]) -> Dict[str, Any]:
        """[jonex] P0 类型定义渲染：name + 中文 aliases + 裁剪属性表。

        - aliases 是中文语料的语义入口（丢 → 中文实体被兜底中文粗类型池
          吸引、档位判出本体，P6 后 30 vs 107 的独立主因之一）；
        - attributes 不进 prompt 则属性 schema 对 LLM 不可见，属性必然全空；
        - 属性行只保留 name + description（截断 _ATTR_DESC_MAX 字），
          且仅属性非空的类型携带 attributes 键。
        """
        item: Dict[str, Any] = {
            "name": et.get("name", ""),
            "aliases": [str(a).strip() for a in et.get("aliases", []) if str(a).strip()],
        }
        rendered: Dict[str, str] = {}
        for a in et.get("attributes") or []:
            name = str(a.get("name") or "").strip()
            if not name:
                continue
            desc = str(a.get("description") or "").strip() or name
            rendered[name] = desc[:NativeOntologyExtractor._ATTR_DESC_MAX]
        if rendered:
            item["attributes"] = rendered
        return item

    def _build_prompts(
        self,
        batch: List[Dict[str, Any]],
        schema: dict,
    ) -> Tuple[str, str]:
        # [jonex] P0：渲染完整类型定义（旧实现只传 name，aliases/attributes
        # 均不进 prompt——与 P1 缩批同批上线，见 _pack_batches 输出预算反推）。
        tbox_types = [self._render_type_def(et) for et in schema.get("entity_types", [])]
        relations_json = json.dumps(
            schema.get("relation_types", []), ensure_ascii=False,
        )
        system = NATIVE_SYSTEM_PROMPT.format(
            tbox_types=json.dumps(tbox_types, ensure_ascii=False),
            fallback_types=json.dumps(load_fallback_types(), ensure_ascii=False),
            relations_json=relations_json,
            synonym_rule=_build_synonym_rule(schema),
        )
        chunk_items = [
            f"[C{i}] {c.get('content') or ''}"
            for i, c in enumerate(batch)
        ]
        user = NATIVE_USER_PROMPT.format(
            n=len(chunk_items), chunks_json=json.dumps(chunk_items, ensure_ascii=False),
        )
        return system, user

    async def _call_llm_once(
        self,
        system: str,
        user: str,
        scope: Optional[Dict[str, Any]],
        opts: Dict[str, Any],
        max_tokens: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[Exception]]:
        """单次 LLM 调用，返回 (content, finish_reason, error)。

        §5.4 截断检测：finish_reason == "length" → 调用方降半/拆批重试。
        """
        try:
            content, meta = await _openai_chat(
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=float(opts.get("temperature", 0)),
                max_tokens=max_tokens or int(opts.get("max_tokens", 8192)),
                scope=scope,
                json_mode=bool(opts.get("json_mode", True)),
                return_meta=True,
                # [jonex] P3a：KB 级本体抽取模型（None ⇒ 由 _openai_chat 再按
                # scope / env 回落，见那里的三级优先级）
                model=opts.get("model") or None,
            )
            return content, (meta or {}).get("finish_reason"), None
        except Exception as exc:  # noqa: BLE001 — 批级错误聚合
            return None, None, exc

    async def _run_batches(
        self,
        batches: List[List[Dict[str, Any]]],
        schema: dict,
        scope: Optional[Dict[str, Any]],
        opts: Dict[str, Any],
    ) -> List[Tuple[Optional[str], Optional[str], Optional[Exception], List[Dict[str, Any]]]]:
        """并发跑各批；截断批先二分，半批再截断才拆单 chunk（[jonex] P4b，§5.4）。

        temperature=0 下同 prompt 降半重试必然再 length（纯浪费一次调用），
        故截断即拆批。拆批后单 chunk 仍截断 → 保留截断输出，由
        _load_json 截断部分恢复解析（不再整批清零）。

        [jonex] P4b 二分优先：原实现截断即拆单 chunk，一个 8 chunk 批顶格
        要付 1+8=9 次调用。实测单 chunk 输出 p50=1625 / p95=3400 token，
        4 chunk 半批多数落在 8192 以内，故先二分：半批正常则只需 1+2=3 次。
        半批仍顶格说明该半批确实重，直接拆单 chunk 而**不再二分到 2+2**——
        全顶格时递归二分要 1+2+4+8=15 次，反而劣于原来的 1+8=9；「二分一级
        后即拆单」把最坏压到 1+2+8=11，期望值仍显著优于原实现。

        返回按批序的 (raw, finish_reason, error, 批内 chunk 列表)；拆批产物
        以子批形式占据原批位置（顺序确定，解析期按批序聚合）。
        """
        sem = asyncio.Semaphore(_extract_concurrency())
        max_tokens = int(opts.get("max_tokens", 8192))

        async def _one(batch: List[Dict[str, Any]], mt: int):
            async with sem:
                system, user = self._build_prompts(batch, schema)
                return await self._call_llm_once(system, user, scope, opts, mt)

        async def _singles(batch, tag, why):
            logger.warning("批%s %s，按 chunk 拆批（%d 个）", tag, why, len(batch))
            subs = await asyncio.gather(*[_one([c], max_tokens) for c in batch])
            return [(s[0], s[1], s[2], [batch[j]]) for j, s in enumerate(subs)]

        async def _split(batch, tag, allow_half: bool):
            """跑一批；顶格则降级。allow_half=False 时不再二分，直接拆单。"""
            raw, fr, err = await _one(batch, max_tokens)
            if err is not None or fr != "length" or len(batch) <= 1:
                return [(raw, fr, err, batch)]
            if allow_half and len(batch) > 2:
                mid = len(batch) // 2
                logger.warning(
                    "批%s 截断，二分拆批（%d → %d+%d）", tag, len(batch), mid, len(batch) - mid,
                )
                halves = await asyncio.gather(
                    _split(batch[:mid], tag + "a", False),
                    _split(batch[mid:], tag + "b", False),
                )
                return halves[0] + halves[1]
            why = "半批仍顶格" if allow_half is False and len(batch) > 1 else "截断"
            return await _singles(batch, tag, why)

        # 主批并发
        results = await asyncio.gather(
            *[_split(b, "#%d" % i, True) for i, b in enumerate(batches)]
        )
        outputs: List[Tuple[Optional[str], Optional[str], Optional[Exception], List[Dict[str, Any]]]] = []
        for group in results:
            outputs.extend(group)
        return outputs

    # ── ④′ gleaning 补抽（[jonex] P2，§5.4） ──────

    @staticmethod
    def _gleaning_rounds(opts: Dict[str, Any]) -> int:
        """gleaning 轮数：options 显式值优先，缺失读 env。

        [jonex] P5-var：默认 2 轮（原 1）——上游 temperature=0 非位精确，
        单次抽取实体数波动 25~39，多轮 gleaning 逐轮累加已知名单、
        只收遗漏，是收敛方差的第一个抓手（成本 = 轮数×批数）。
        """
        v = opts.get("gleaning")
        if v is not None:
            try:
                return max(0, int(v))
            except (ValueError, TypeError):
                pass
        return max(0, _env_int("ONTOLOGY_EXTRACT_GLEANING", 2))

    @staticmethod
    def _sample_runs(opts: Dict[str, Any]) -> int:
        """主批独立采样次数：options 显式值优先，缺失读 env（默认 2）。

        [jonex] P5-var：采样并集收敛上游方差（1 次实测 25~39 波动）。
        设 1 或 0 关闭（单次采样，成本最低但方差不回补）。
        """
        v = opts.get("sample_runs")
        if v is not None:
            try:
                return max(1, int(v))
            except (ValueError, TypeError):
                pass
        return max(1, _env_int("ONTOLOGY_NATIVE_SAMPLE_RUNS", 2))

    @staticmethod
    def _effective_sample_runs(opts: Dict[str, Any], n_chunks: int) -> int:
        """[jonex] P11 砍遍数：大文档降为单次采样。

        self-consistency 采样把主批成本翻倍，收敛的是上游方差（±25%）；
        chunk 数越大的文档，方差对整体召回的影响越小（大数定律），而成本
        线性翻倍。故 ≥ ONTOLOGY_EXTRACT_BIG_DOC_CHUNKS 时降为 1 次。
        opts 显式给 sample_runs 时以调用方为准；阈值设 0 = 关闸（永不降级）。
        """
        runs = NativeOntologyExtractor._sample_runs(opts)
        if opts.get("sample_runs") is not None:
            return runs
        threshold = _env_int("ONTOLOGY_EXTRACT_BIG_DOC_CHUNKS", 500)
        if threshold > 0 and n_chunks >= threshold:
            return 1
        return runs

    @staticmethod
    def _glean_selected(known: set, n_chunks: int) -> bool:
        """[jonex] P11 砍遍数：只有低产批才值得补抽。

        gleaning 的立论是「大批判注意力稀释 → 自然少输出」。若一批已按
        每 chunk ≥ ONTOLOGY_GLEAN_MIN_YIELD_RATIO 个实体产出，说明注意力
        没稀释，再补抽只是重复劳动。阈值设 0 = 关闸（全部批都补抽，旧行为）。

        默认 2.0（每 chunk 2 个实体）——一个 ~1200 token 的 chunk 抽出 2 个
        实体算抽干净了；1 个/chunk 仍偏疏，留待补抽。线上实测 gleaning 每批
        每轮还能捞回 ~9 个漏抽实体，说明低产批是真实存在的大头。
        """
        ratio = _env_float("ONTOLOGY_GLEAN_MIN_YIELD_RATIO", 2.0)
        if ratio <= 0:
            return True
        return len(known) < n_chunks * ratio

    def _build_gleaning_prompts(
        self,
        batch: List[Dict[str, Any]],
        schema: dict,
        known_names: List[str],
    ) -> Tuple[str, str]:
        system, _ = self._build_prompts(batch, schema)
        chunk_items = [
            f"[C{i}] {c.get('content') or ''}"
            for i, c in enumerate(batch)
        ]
        user = NATIVE_GLEANING_USER_PROMPT.format(
            n=len(chunk_items),
            chunks_json=json.dumps(chunk_items, ensure_ascii=False),
            known_names=json.dumps(known_names, ensure_ascii=False),
        )
        return system, user

    async def _run_gleaning(
        self,
        raw_items: List[Tuple[Optional[str], Optional[str], Optional[Exception], List[Dict[str, Any]]]],
        schema: dict,
        scope: Optional[Dict[str, Any]],
        opts: Dict[str, Any],
        rounds: int,
    ) -> Tuple[List[ExtractedEntity], List[ExtractedRelation], List[str]]:
        """主抽取后的补抽（P2）：每批回传已抽实体名，只输出遗漏内容。

        多轮逐轮累加已知名单；某批一轮无新实体则停止该批后续轮
        （temperature=0 下同 prompt 必然同输出）。只收新实体与至少
        一端为新实体的关系（防御 LLM 不遵守指令）。
        """
        sem = asyncio.Semaphore(_extract_concurrency())
        max_tokens = int(opts.get("max_tokens", 8192))
        glean_entities: List[ExtractedEntity] = []
        glean_relations: List[ExtractedRelation] = []
        errors: List[str] = []

        # [jonex] P5-var：多次采样下 raw_items 含同批多份（各采样一次），
        # 按批内 chunk_id 集合聚合——同批各份的 known 并集回传，且每批只
        # 补抽一次（重复补抽必然无新增，纯浪费调用）。
        # [jonex] P10：旧实现去重保留第一份、known 只 parse 第一份——
        # 后续采样份独有的实体不在 known 里，gleaning 把它们当「遗漏」
        # 重复输出，挤占真实遗漏的输出预算。并集后 known 完整。
        grouped: Dict[tuple, List[Tuple[Optional[str], Optional[str], Optional[Exception], List[Dict[str, Any]]]]] = {}
        order: List[tuple] = []
        for item in raw_items:
            key = tuple(c.get("chunk_id") for c in item[3])
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(item)
        raw_items = [grouped[key][0] for key in order]

        # 各批已知名单 = 该批所有采样份实体名并集（失败份跳过；全部失败
        # 或均无产出则 None 不补抽）
        per_batch_known: List[Optional[set]] = []
        skipped = 0
        for raw, _fr, err, batch in raw_items:
            key = tuple(c.get("chunk_id") for c in batch)
            known: set = set()
            any_ok = False
            for s_raw, s_fr, s_err, s_batch in grouped[key]:
                if s_err is not None or not s_raw:
                    continue
                parsed_e, _, _, _ = self._parse_results(
                    [(s_raw, s_fr, s_err, s_batch)], schema,
                )
                known |= {e.canonical_name for e in parsed_e}
                any_ok = True
            # [jonex] P11：高产批（每 chunk ≥ ratio 个实体）跳过补抽——
            # 其注意力未稀释，再补抽只是重复劳动。
            selectable = any_ok and known and self._glean_selected(
                known, len(batch),
            )
            per_batch_known.append(known if selectable else None)
            if any_ok and known and not selectable:
                skipped += 1
        if skipped:
            logger.info(
                "gleaning 跳过高产批 %d/%d（阈值 %.2f 实体/chunk；置 "
                "ONTOLOGY_GLEAN_MIN_YIELD_RATIO=0 关闭该优化）",
                skipped, len(per_batch_known),
                _env_float("ONTOLOGY_GLEAN_MIN_YIELD_RATIO", 2.0),
            )

        for rnd in range(rounds):
            idxs = [i for i, known in enumerate(per_batch_known) if known]
            if not idxs:
                break

            async def _glean_one(i: int):
                batch = raw_items[i][3]
                async with sem:
                    system, user = self._build_gleaning_prompts(
                        batch, schema, sorted(per_batch_known[i]),
                    )
                    return await self._call_llm_once(system, user, scope, opts, max_tokens)

            results = await asyncio.gather(*[_glean_one(i) for i in idxs])
            for i, (raw, fr, err) in zip(idxs, results):
                if err is not None:
                    errors.append(f"gleaning 轮#{rnd} 批#{i} 调用失败: {err}")
                    continue
                data = _load_json(raw or "")
                if data is None:
                    errors.append(f"gleaning 轮#{rnd} 批#{i} 输出非合法 JSON")
                    continue
                g_e, g_r, g_errors, _ = self._parse_results(
                    [(raw, fr, err, raw_items[i][3])], schema,
                )
                errors.extend(g_errors)
                known = per_batch_known[i]
                new_e = [e for e in g_e if e.canonical_name not in known]
                if not new_e:
                    # 无新增：后续轮同 prompt 必然同输出，停止该批
                    per_batch_known[i] = None
                    continue
                new_names = {e.canonical_name for e in new_e}
                new_r = [
                    rel for rel in g_r
                    if rel.source_name not in known or rel.target_name not in known
                ]
                glean_entities.extend(new_e)
                glean_relations.extend(new_r)
                known |= new_names
                logger.info(
                    "gleaning 轮#%d 批#%d 新增实体 %d 关系 %d",
                    rnd, i, len(new_e), len(new_r),
                )
        return glean_entities, glean_relations, errors

    # ── ③′ procedural LLM 调用（P7，M2） ──────

    def _build_procedural_prompts(self, chunk: Dict[str, Any]) -> Tuple[str, str]:
        """procedural 提示词（D7：核心 4 字段 + 挂接端点，不抽 duration/frequency）。"""
        system = PROCEDURAL_SYSTEM_PROMPT
        user = PROCEDURAL_USER_PROMPT.format(
            chunk_text=str(chunk.get("content") or ""),
        )
        return system, user

    async def _run_procedural(
        self,
        chunks: List[Dict[str, Any]],
        schema: dict,
        scope: Optional[Dict[str, Any]],
        opts: Dict[str, Any],
    ) -> List[Tuple[Optional[str], Optional[str], Optional[Exception], Dict[str, Any]]]:
        """procedural 每 chunk 一次 LLM 调用（并发复用 _extract_concurrency）。

        [jonex] P4：截断不再降半重试（temperature=0 同 prompt 必然再
        length，纯浪费调用）；保留首次截断输出，由 _load_json 截断部分
        恢复解析。单 chunk 不可拆批。
        """
        sem = asyncio.Semaphore(_extract_concurrency())
        max_tokens = int(opts.get("max_tokens", 8192))

        async def _one(chunk: Dict[str, Any], mt: int):
            async with sem:
                system, user = self._build_procedural_prompts(chunk)
                return await self._call_llm_once(system, user, scope, opts, mt)

        first = await asyncio.gather(*[_one(c, max_tokens) for c in chunks])
        outputs = []
        for i, chunk in enumerate(chunks):
            raw, fr, err = first[i]
            if err is None and fr == "length":
                logger.warning("procedural chunk#%d 截断，按部分恢复解析", i)
            outputs.append((raw, fr, err, chunk))
        return outputs

    # ── ④ 解析 + 别名归一 ─────────────────────

    @staticmethod
    def _alias_map(schema: dict) -> Dict[str, str]:
        """synonyms 段 → {term: canonical} 归一映射（大小写敏感按原文）。"""
        m: Dict[str, str] = {}
        for group in schema.get("synonyms") or []:
            canonical = group.get("canonical") or ""
            if not canonical:
                continue
            for term in group.get("terms") or []:
                t = str(term).strip()
                if t and t != canonical:
                    m.setdefault(t, canonical)
        return m

    @classmethod
    def _parse_results(
        cls,
        raw_items: List[Tuple[Optional[str], Optional[str], Optional[Exception], List[Dict[str, Any]]]],
        schema: dict,
    ) -> Tuple[List[ExtractedEntity], List[ExtractedRelation], List[str], bool]:
        """解析各批 LLM 输出：实体/关系 + chunk 索引溯源（§5.3 ④⑥）。"""
        alias_map = cls._alias_map(schema)
        # [jonex] 类型别名归一（与实体名 alias_map 同形，定义在 ontology_extractor
        # 以便与 _post_validate 共用）：LLM 输出别名当 type 时归一为 canonical，
        # 否则 _post_validate 按 valid_etypes 整批丢弃。
        type_alias_map = _type_alias_map(schema)
        # [jonex] P5：实体描述截断接 env（§6.2 保留变量；默认 2000 = P4 现状）
        desc_maxlen = _env_int("ONTOLOGY_ENTITY_DESC_MAXLEN", 2000)
        entities: List[ExtractedEntity] = []
        relations: List[ExtractedRelation] = []
        errors: List[str] = []
        any_ok = False
        for bi, (raw, _fr, err, batch) in enumerate(raw_items):
            if err is not None:
                errors.append(f"批#{bi} 调用失败: {err}")
                continue
            data = _load_json(raw or "")
            if data is None:
                errors.append(f"批#{bi} 输出非合法 JSON")
                continue
            any_ok = True
            chunk_by_idx = {i: c for i, c in enumerate(batch)}
            for ent in data.get("entities", []):
                try:
                    name = str(ent.get("name") or "").strip()
                    if not name:
                        continue
                    name = alias_map.get(name, name)
                    aliases = [str(a).strip() for a in ent.get("aliases", []) if str(a).strip()]
                    # aliases 保留原词（检索召回用），仅剔除与 canonical 同名的项
                    aliases = [a for a in aliases if a != name]
                    etype = str(ent.get("type") or "").strip() or "unknown"
                    entities.append(ExtractedEntity(
                        canonical_name=name,
                        entity_type=type_alias_map.get(etype, etype),
                        aliases=aliases,
                        attributes=ent.get("attributes") or {},
                        description=str(ent.get("description") or "")[:desc_maxlen],
                        confidence=cls._to_float(ent.get("confidence"), 1.0),
                        source_chunks=cls._resolve_chunks(ent.get("chunks"), chunk_by_idx),
                        extraction_method="native_llm",
                    ))
                except (ValueError, TypeError) as exc:  # noqa: BLE001 — 单条容错
                    errors.append(f"批#{bi} 实体解析失败 {ent.get('name', '?')}: {exc}")
            for rel in data.get("relations", []):
                try:
                    src = str(rel.get("source") or "").strip()
                    tgt = str(rel.get("target") or "").strip()
                    if not src or not tgt:
                        continue
                    src = alias_map.get(src, src)
                    tgt = alias_map.get(tgt, tgt)
                    # 关系端点用的就是实体类型词表，走同一套类型别名归一
                    stype = str(rel.get("source_type") or "").strip()
                    ttype = str(rel.get("target_type") or "").strip()
                    relations.append(ExtractedRelation(
                        source_name=src,
                        source_type=type_alias_map.get(stype, stype),
                        target_name=tgt,
                        target_type=type_alias_map.get(ttype, ttype),
                        relation_type=str(rel.get("relation_type") or "").strip(),
                        keywords=str(rel.get("keywords") or "").strip(),
                        confidence=cls._to_float(rel.get("confidence"), 1.0),
                        source_chunks=cls._resolve_chunks(rel.get("chunks"), chunk_by_idx),
                    ))
                except (ValueError, TypeError) as exc:  # noqa: BLE001 — 单条容错
                    errors.append(f"批#{bi} 关系解析失败 {rel.get('source', '?')}→{rel.get('target', '?')}: {exc}")
        return entities, relations, errors, any_ok

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _resolve_chunks(
        idxs: Any,
        chunk_by_idx: Dict[int, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """chunk 索引 → [{source_id: 权威 chunk_id, file_path}]。

        索引缺失/越界时回落批内全量 chunk（保引用可用，记 debug）。
        """
        out: List[Dict[str, Any]] = []
        seen = set()
        if isinstance(idxs, list) and idxs:
            for idx in idxs:
                try:
                    i = int(idx)
                except (ValueError, TypeError):
                    continue
                c = chunk_by_idx.get(i)
                if c is None:
                    continue
                key = (c.get("chunk_id") or "", c.get("file_path") or "")
                if key in seen:
                    continue
                seen.add(key)
                out.append({"source_id": c.get("chunk_id") or "", "file_path": c.get("file_path") or ""})
            if out:
                return out
            logger.debug("native 溯源 chunk 索引无效，回落批内全量 chunk")
        for c in chunk_by_idx.values():
            out.append({"source_id": c.get("chunk_id") or "", "file_path": c.get("file_path") or ""})
        return out

    # ── ⑤ 跨批合并 ────────────────────────────

    @staticmethod
    def _merge_entities(entities: List[ExtractedEntity]) -> List[ExtractedEntity]:
        """同名实体合并：描述取长、aliases 并集、source_chunks 按 source_id 去重聚合。"""
        merged: Dict[str, ExtractedEntity] = {}
        for e in entities:
            name = e.canonical_name
            if name not in merged:
                merged[name] = ExtractedEntity(
                    canonical_name=e.canonical_name,
                    entity_type=e.entity_type,
                    aliases=list(e.aliases),
                    attributes=dict(e.attributes),
                    description=e.description,
                    confidence=e.confidence,
                    source_chunks=[dict(sc) for sc in e.source_chunks],
                    extraction_method=e.extraction_method,
                )
                continue
            m = merged[name]
            if len(e.description) > len(m.description):
                m.description = e.description
            if e.confidence > m.confidence:
                m.confidence = e.confidence
            # Action/Skill 以 procedural 属性为准、字段级并集；其余类型先到者优先
            merge_entity_attributes(m, e)
            for alias in e.aliases:
                if alias and alias not in m.aliases:
                    m.aliases.append(alias)
            seen = {(sc.get("source_id"), sc.get("file_path")) for sc in m.source_chunks}
            for sc in e.source_chunks:
                key = (sc.get("source_id"), sc.get("file_path"))
                if key not in seen:
                    seen.add(key)
                    m.source_chunks.append(dict(sc))
        return list(merged.values())

    @staticmethod
    def _merge_relations(relations: List[ExtractedRelation]) -> List[ExtractedRelation]:
        """同名边合并：主键 = (source_name, target_name, relation_type)。

        [jonex] P5-var：多次采样下同一条边在各采样中重复出现，不合并会
        翻倍污染关系数（reconcile 写图 + 引用回写都会重复消费）。合并
        语义与实体一致：confidence 取大、chunks 去重聚合、keywords 首条
        非空优先、attributes 首见优先。
        """
        merged: Dict[Tuple[str, str, str], ExtractedRelation] = {}
        for r in relations:
            key = (r.source_name, r.target_name, r.relation_type)
            prev = merged.get(key)
            if prev is None:
                merged[key] = ExtractedRelation(
                    source_name=r.source_name,
                    source_type=r.source_type,
                    target_name=r.target_name,
                    target_type=r.target_type,
                    relation_type=r.relation_type,
                    confidence=r.confidence,
                    source_chunks=[dict(sc) for sc in r.source_chunks],
                    keywords=r.keywords,
                    attributes=dict(r.attributes or {}),
                )
                continue
            if r.confidence > prev.confidence:
                prev.confidence = r.confidence
            if r.keywords and not prev.keywords:
                prev.keywords = r.keywords
            for k2, v2 in (r.attributes or {}).items():
                if k2 not in prev.attributes:
                    prev.attributes[k2] = v2
            seen = {(sc.get("source_id"), sc.get("file_path")) for sc in prev.source_chunks}
            for sc in r.source_chunks:
                key_sc = (sc.get("source_id"), sc.get("file_path"))
                if key_sc not in seen:
                    seen.add(key_sc)
                    prev.source_chunks.append(dict(sc))
        return list(merged.values())

    # ── ⑥ chunk 引用确定性回填（[jonex] P3） ──────

    # [jonex] P2-a：本函数内层是 O(实体×chunk) 纯 CPU 计算且无 await，
    # 会整段占死事件循环；每累计这么多次「内层比对」让出一次。
    #
    # 按次数计而非按实体数计：单次比对成本固定，按实体数计会让两次让出
    # 之间的耗时随 chunk 数 M 线性增长（M=24212 时 16×24212≈38.7 万次
    # ≈0.8~1.9s；M 再大一个量级就顶到 10s healthcheck 超时，重演同一
    # 冻结）。按次数计与 M、N 都解耦，可被 ONTOLOGY_BACKFILL_YIELD_OPS
    # 覆盖（越小越可响应、吞吐略降）。
    _BACKFILL_YIELD_OPS_DEFAULT = 200_000

    @staticmethod
    async def _backfill_source_chunks(
        entities: List[ExtractedEntity],
        relations: List[ExtractedRelation],
        usable: List[Dict[str, Any]],
    ) -> None:
        """canonical_name + aliases 归一化匹配 chunk 正文，命中并集追加 source_chunks。

        LLM 批量抽取的 chunks 引用常失真（有效但不准的索引无法在
        _resolve_chunks 识别）——确定性回填保证实体与真正提及它的 chunk
        关联完整。仅并集追加（不删 LLM 声明，覆盖「索引有效但不准」）。
        关系取两端命中集合的交集（两端同现的 chunk 才是关系支撑），
        交集为空回落并集。归一化匹配复用 P9 技能层的三级判定
        （逐字 → 去标点/空白归一 → 拆主体过半命中）。

        [jonex] P2 性能与让出（2026-09-12 线上事故修复）：
        原实现对每个 (实体, chunk) 对重算 chunk 正文的归一化正则，并对
        每个 value 重跑拆主体正则——规模是 O(N×M) 次全串正则。线上
        29433 实体 × 24212 chunk ≈ 21 亿次，实测 100% 单核烧 9.7~19.5
        小时；且本函数原为同步实现、由 async 调用方直接调用，没有让出点，
        把 asyncio 事件循环整个占死——atomic-rag 全服务离线（连 /health
        都不响应，心跳停摆），只能重启容器。

        现改为：chunk 侧归一化一次（O(M)）、value 侧因子一次（O(N)），
        内层只剩纯子串查找；并按「内层比对次数」让出事件循环。注意这是
        **常数优化**，双重循环仍是 O(N×M)——M=24212 量级实测仍需分钟级
        CPU（预估 20~70 分钟，取决于 aliases 数）；要再降一档需索引化
        （AC 自动机/倒排，O(N+M)），本次未做。
        判定口径与原实现逐字一致（复用 _mentions_prepared）。
        """
        from jonex_core.capability.atomic.rag.ontology_extraction_skills import (
            _NORM_STRIP,
            _mentions_prepared,
            _prepare_needle,
        )

        key_map: Dict[Tuple[str, str], Dict[str, str]] = {}
        content_map: Dict[Tuple[str, str], str] = {}
        norm_map: Dict[Tuple[str, str], str] = {}
        for c in usable:
            key = (c.get("chunk_id") or "", c.get("file_path") or "")
            key_map[key] = {"source_id": key[0], "file_path": key[1]}
            txt = str(c.get("content") or "")
            content_map[key] = txt
            norm_map[key] = _NORM_STRIP.sub("", txt)

        # [jonex] P0-3：每个实体/关系的 source_chunks 数量上限（0 = 不限）。
        # P2-b 把本函数从「O(N×M) 次正则」压成「纯子串查找」后 CPU 不再是
        # 瓶颈，**产出规模**随即成为瓶颈：实体循环命中即 append、
        # 关系循环按两端并集/交集 append，线上 17MB 文档实测 19647 实体 ×
        # 46290 chunk，最坏产出上亿个 {"source_id","file_path"} dict，
        # 序列化再翻一倍，直接把宿主内存吃到 23.7GB 触发 oom-killer
        # （同一根因换个出口：P2-b 之前是 CPU 卡死跑不完，之后是内存 OOM）。
        # 业务口径：引用回溯展示「这条事实来自哪几个片段」，几百上千个引用
        # 无使用价值，纯是内存与回写 payload 的负担。
        cap = max(0, _env_int("ONTOLOGY_SOURCE_CHUNKS_MAX", 32))

        # 实体：命中 chunk 并集 + 既有 LLM 引用
        # 让出点按累计内层比对次数（实体循环 + 关系循环共用一个计数）
        yield_ops = max(1, _env_int(
            "ONTOLOGY_BACKFILL_YIELD_OPS",
            NativeOntologyExtractor._BACKFILL_YIELD_OPS_DEFAULT,
        ))
        ops = 0
        for e in entities:
            existing = {(sc.get("source_id"), sc.get("file_path")) for sc in e.source_chunks}
            # cname 与 aliases 的判定因子预先算好（原 _hit 为 cname 命中
            # 即短路，any() 保持同样的短路语义；顺序不影响布尔结果）
            needles = [
                n
                for n in (
                    _prepare_needle(e.canonical_name),
                    *(_prepare_needle(a) for a in e.aliases),
                )
                if n is not None
            ]
            if not needles:
                continue
            for key, sc in key_map.items():
                if key in existing:
                    continue
                if cap and len(e.source_chunks) >= cap:
                    # 达上限即停：既封住内存，也让 O(N×M) 的内层循环提前收敛
                    # （本函数的 CPU 大头就是这层遍历）。
                    break
                ops += 1
                if ops >= yield_ops:
                    ops = 0
                    await asyncio.sleep(0)
                if any(
                    _mentions_prepared(content_map[key], norm_map[key], n)
                    for n in needles
                ):
                    e.source_chunks.append(dict(sc))
        # 关系：两端命中交集（同现支撑），空则并集；既有 LLM 引用保留
        entity_chunks: Dict[str, set] = {
            e.canonical_name: {
                (sc.get("source_id"), sc.get("file_path")) for sc in e.source_chunks
            }
            for e in entities
        }
        for r in relations:
            src_set = entity_chunks.get(r.source_name) or set()
            tgt_set = entity_chunks.get(r.target_name) or set()
            inter = src_set & tgt_set
            base = inter if inter else (src_set | tgt_set)
            existing = {(sc.get("source_id"), sc.get("file_path")) for sc in r.source_chunks}
            pending = base - existing
            # 集合交/并/差与排序同样占 CPU，按元素数计入让出预算
            if pending:
                ops += len(pending)
                if ops >= yield_ops:
                    ops = 0
                    await asyncio.sleep(0)
            for key in sorted(pending):
                if cap and len(r.source_chunks) >= cap:
                    # 关系侧两端并集动辄上万（线上实例实测单条最多 769 个），
                    # 达配额即停。
                    break
                if key in key_map:
                    r.source_chunks.append(dict(key_map[key]))
