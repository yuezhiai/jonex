#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
本体抽取公共件 — NativeOntologyExtractor 的支撑模块。

P4 起本体抽取唯一入口是 native_ontology_extractor.NativeOntologyExtractor
（chunk 原文独立抽取 + 双层类型归类）；旧 OntologyExtractor（读 LightRAG
候选实体/关系 → 归类/定型链路）已在 P5 清理删除。

本模块保留（§6.1「保留/复用」列）：
- 数据模型 ExtractedEntity / ExtractedRelation / ExtractionResult
- LLM 调用 _openai_chat（llm-gateway 计量头）与并发度 _extract_concurrency
- 解析/校验公共件 _load_json / _post_validate / _endpoint_ok
- compiled schema 三级解析 _resolve_schema / _get_compiled_client
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

from jonex_core.capability.atomic.ontology import OntologyRegistry
from jonex_core.common.model_config import resolve_llm_model

logger = __import__("logging").getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================


@dataclass
class ExtractedEntity:
    canonical_name: str
    entity_type: str
    aliases: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    description: str = ""           # [jonex] 实体描述，answer_from_facts 的核心上下文
    confidence: float = 1.0
    source_chunks: List[Dict[str, Any]] = field(default_factory=list)
    extraction_method: str = "llm_guided"  # [jonex] llm_guided / pre_classified / endpoint_backfill（P2C 兜底）
    table_sig: str = ""             # [jonex] Row-as-Object：表列名签名（改动 18 upsert 键）
    pk_value: str = ""              # [jonex] Row-as-Object：主键值（改动 18 upsert 键）


@dataclass
class ExtractedRelation:
    source_name: str
    source_type: str
    target_name: str
    target_type: str
    relation_type: str
    confidence: float = 1.0
    source_chunks: List[Dict[str, Any]] = field(default_factory=list)   # [jonex] 方案⑧ 关系出处
    keywords: str = ""                      # [jonex] P2：TBox 外关系的自由关键词（relation_type 空串时供 LightRAG 关系检索）
    attributes: Dict[str, Any] = field(default_factory=dict)  # [jonex] P2：关系属性（step_no 等，D6 预留）


@dataclass
class ExtractionResult:
    entities: List[ExtractedEntity] = field(default_factory=list)
    relations: List[ExtractedRelation] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    raw_llm_response: Optional[str] = None
    ok: bool = True                 # [jonex] 产出 ≥1 实体即 True；全批失败/零产出才 False


# ============================================================
# LLM 调用（OpenAI 兼容接口）
# ============================================================


def _extract_concurrency() -> int:
    """本体抽取的 LLM 批次并发度（实体归类/关系定型共用）。

    这些批次经 llm-gateway → 上游 LLM，与入库期 LightRAG 抽取共用同一上游
    并发额度（实测 tokenhub 约 48 并发才触发 429，见 scripts/bench_llm_extract.py）。
    ontology_extract 在 LightRAG 上传阶段之后执行，通常不与同文档的 LightRAG
    抽取同时打满上游；默认 8 保守取值，落在无限流区且给查询期/多文档并行留余量。
    经 ONTOLOGY_EXTRACT_CONCURRENCY 调整（<1 归一）。
    """
    try:
        n = int(os.getenv("ONTOLOGY_EXTRACT_CONCURRENCY", "8"))
    except ValueError:
        n = 8
    return max(1, n)


async def _openai_chat(
    messages: List[Dict[str, str]],
    temperature: float = 0.1,
    max_tokens: Optional[int] = None,
    scope: Optional[Dict[str, Any]] = None,
    json_mode: bool = False,
    return_meta: bool = False,
    model: Optional[str] = None,
):
    """调用 OpenAI 兼容接口（经 llm-gateway 计量，上游 deepseek-v4-flash 等）。

    Args:
        json_mode: 开启 response_format=json_object（开关 ONTOLOGY_EXTRACT_JSON_MODE）
        return_meta: [jonex] P2 True 时返回 (content, meta) 元组，meta 含
            finish_reason 供截断检测（§5.4）；默认 False 保持旧调用方行为不变。
        model: [jonex] P3a 模型覆盖（KB 级配置下发）。三级优先级：
            **显式入参 > `scope["model"]` > `ONTOLOGY_LLM_MODEL`**。

            为什么也认 `scope`：本体抽取的 4 个 LLM 出口（主抽取、表格补跑、
            表级判定、列名映射）只有前两个自带 `options`，后两个在
            `table_object_extractor` 里、签名只有 `scope` 一个通道。把模型挂在
            scope 上，四处共用同一个值、也免掉「给两个方法各加形参再逐层透传」
            那种改一半就静默失效的接线。空值一律视为"没覆盖"。
    """
    import httpx

    host = os.getenv(
        "ONTOLOGY_LLM_BINDING_HOST",
        "http://llm-gateway:8787/v1",
    ).rstrip("/")
    api_key = os.getenv(
        "ONTOLOGY_LLM_BINDING_API_KEY",
        "",  # 改为网关内部 token，不持云端 key
    )
    # [jonex] P3a：显式入参 > scope 携带 > env 默认（见 docstring 的三级优先级）
    model = (model or "").strip() or str((scope or {}).get("model") or "").strip() \
        or resolve_llm_model("ONTOLOGY_LLM_MODEL")
    timeout = float(os.getenv("ONTOLOGY_LLM_TIMEOUT", "60"))

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # 注入计量上下文头（G3a：随 base_url 改指网关同步注入，避免 unknown 窗口）
    if scope:
        tenant_id = scope.get("tenant_id", "unknown")
        kb_id = scope.get("knowledge_base_id", "")
        doc_id = scope.get("document_id", "")
        headers["X-Jonex-Tenant-Id"] = tenant_id
        headers["X-Jonex-Kb-Id"] = kb_id
        headers["X-Jonex-Doc-Id"] = doc_id
        headers["X-Jonex-Scene"] = "ontology_extract"
        # 链路追踪 ID：优先用调用方透传的 trace_id；缺失时按 (tenant/kb/doc) 派生稳定值，
        # 使同一文档的重抽取在 body 不变时去重，不同文档/内容如实记录。
        # 不再注入随机 X-Jonex-Request-Id（交给网关按 trace + body 哈希派生幂等键）。
        trace_id = scope.get("trace_id") or f"ontology_extract:{tenant_id}:{kb_id}:{doc_id}"
        headers["X-Jonex-Trace-Id"] = trace_id

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    # [jonex] JSON 模式（deepseek-v4-flash 已确认支持；网关透传）
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{host}/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if return_meta:
            # [jonex] P2：finish_reason 供截断检测（length = 输出被 max_tokens 截断）
            return content, {
                "finish_reason": data["choices"][0].get("finish_reason"),
            }
        return content


# ============================================================
# compiled schema 三级解析（P5 从旧类提升为模块级）
# ============================================================


_compiled_client: Optional[Any] = None


def _get_compiled_client():
    """CompiledSchemaClient 惰性单例（_resolve_schema 第二级用）。"""
    global _compiled_client
    if _compiled_client is None:
        from jonex_core.capability.atomic.ontology.compiled_schema_client import (
            CompiledSchemaClient,
        )
        _compiled_client = CompiledSchemaClient()
    return _compiled_client


async def _resolve_schema(
    compiled_schema: Optional[dict],
    scope: Dict[str, Any],
    registry: Optional[OntologyRegistry] = None,
) -> tuple[Optional[Any], Optional[str]]:
    """解析 schema 来源，返回 (schema_obj, ontology_json)。

    优先级：入参 compiled_schema > CompiledSchemaClient > OntologyRegistry
    """
    # 1) 调用方传入的 compiled_schema
    if compiled_schema is not None:
        prompt = compiled_schema.get("prompt_schema")
        if prompt:
            ontology_json = json.dumps(prompt, ensure_ascii=False)
            logger.info(
                "Using push compiled schema for %s/%s (version=%s)",
                scope.get("tenant_id", "?"), scope.get("knowledge_base_id", "?"),
                compiled_schema.get("schema_version", "?"),
            )
            return compiled_schema, ontology_json

    # 2) CompiledSchemaClient 拉取
    tenant_id = scope.get("tenant_id")
    knowledge_base_id = scope.get("knowledge_base_id")
    if tenant_id and knowledge_base_id:
        try:
            client = _get_compiled_client()
            raw = await client.get_schema(tenant_id, knowledge_base_id)
            if raw and raw.get("prompt_schema"):
                ontology_json = json.dumps(raw["prompt_schema"], ensure_ascii=False, indent=2)
                logger.info(
                    "Using client-fetched compiled schema for %s/%s (source=%s)",
                    tenant_id, knowledge_base_id, raw.get("source_type", "?"),
                )
                return raw, ontology_json
        except Exception as e:
            logger.warning("Compiled schema load failed: %s", e)

    # 3) OntologyRegistry fallback
    if registry is not None:
        domain = scope.get("domain", "default")
        ontology_json = registry.to_prompt_json(domain)
        logger.info("Fallback to OntologyRegistry domain=%s", domain)
        return None, ontology_json

    return None, None


# ============================================================
# 解析 / 校验公共件
# ============================================================


def _load_json(raw: str) -> Optional[dict]:
    """通用 JSON 解析：去 markdown 包裹 + 花括号兜底 + [jonex] P4 截断部分恢复。

    截断输出（finish_reason=length 且拆批后仍截断）按对象级大括号平衡
    扫描逐项恢复：保住已完整输出的实体/关系，替代旧行为「整批清零」。
    """
    import re

    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(cleaned[s:e + 1])
        except json.JSONDecodeError:
            return _recover_truncated_objects(cleaned[s:e + 1])
    return None


def _recover_truncated_objects(core: str) -> Optional[dict]:
    """[jonex] P4 截断 JSON 逐对象恢复：提取 entities/relations 数组中
    大括号平衡的完整对象；一个对象都恢复不出则返回 None（整批清零）。"""
    out: dict = {"entities": [], "relations": []}
    for key in ("entities", "relations"):
        idx = core.find(f'"{key}"')
        if idx == -1:
            continue
        arr = core.find("[", idx)
        if arr == -1:
            continue
        pos = arr + 1
        while pos < len(core):
            while pos < len(core) and core[pos] in " \t\r\n,":
                pos += 1
            if pos >= len(core) or core[pos] == "]":
                break
            if core[pos] != "{":
                pos += 1
                continue
            # 大括号平衡扫描（含字符串转义）
            depth, i, in_str, esc = 0, pos, False, False
            while i < len(core):
                ch = core[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            break
                i += 1
            if depth != 0 or i >= len(core):
                break  # 截断处对象未闭合
            try:
                out[key].append(json.loads(core[pos:i + 1]))
            except json.JSONDecodeError:
                pass
            pos = i + 1
    return out if (out["entities"] or out["relations"]) else None


#: procedural 子管线的产出标记（native_ontology_extractor 的 procedural 解析写入）
PROCEDURAL_EXTRACTION_METHOD = "procedural_llm"
#: 以 procedural 结果为准的类型：procedural prompt 专门约束了这两类的契约字段与禁编造，
#: 主抽取对这些字段没有约束
PROCEDURAL_PRIORITY_TYPES = frozenset({"Action", "Skill"})
#: 库级 procedural 开关在 prompt_schema（即抽取器收到的 tbox）顶层的键名。
#: 由 kb-service 派单时按 knowledge_info.procedural_extract 写入；抽取器只认值为 True。
#: 两个执行方（atomic-rag / ontology-compile）都把 prompt_schema 原样作为 tbox 传给
#: NativeOntologyExtractor.extract()，故挂在这里不需要改任何派单 payload 结构。
PROCEDURAL_GATE_KEY = "procedural_extract"


def _non_empty_attrs(attrs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        k: v for k, v in (attrs or {}).items()
        if v is not None and v != "" and v != [] and v != {}
    }


def merge_entity_attributes(kept: "ExtractedEntity", incoming: "ExtractedEntity") -> None:
    """同名实体合并时的属性合并（原地修改 ``kept``）。

    - 任一方是 procedural 产出、且该 procedural 实体属于 Action/Skill：
      以 procedural 一方的属性为准，缺失字段用另一方补（字段级并集，同字段冲突取 procedural）。
      procedural 一方后到时，类型与 extraction_method 也改用 procedural 的——
      主抽取可能把同名动作判成 Concept 等泛化类型，留下它会让 Action 整个丢失。
      双方都是 procedural 时，先到者为准、后到者补缺。
    - 其余情况维持先到者优先：先到者属性为空时才取后到者的整份属性。
    """
    kept_proc = (
        kept.extraction_method == PROCEDURAL_EXTRACTION_METHOD
        and kept.entity_type in PROCEDURAL_PRIORITY_TYPES
    )
    incoming_proc = (
        incoming.extraction_method == PROCEDURAL_EXTRACTION_METHOD
        and incoming.entity_type in PROCEDURAL_PRIORITY_TYPES
    )
    if not (kept_proc or incoming_proc):
        if incoming.attributes and not kept.attributes:
            kept.attributes = dict(incoming.attributes)
        return

    if incoming_proc and not kept_proc:
        primary, secondary = incoming.attributes, kept.attributes
        kept.entity_type = incoming.entity_type
        kept.extraction_method = incoming.extraction_method
    else:
        primary, secondary = kept.attributes, incoming.attributes
    merged = _non_empty_attrs(secondary)
    merged.update(_non_empty_attrs(primary))
    kept.attributes = merged


_TRUE_WORDS = frozenset({"true", "yes", "y", "1", "是", "需要", "要", "必须", "需", "有"})
_FALSE_WORDS = frozenset({"false", "no", "n", "0", "否", "不需要", "不要", "无需", "不必", "无"})


def _coerce_bool(value: Any) -> Optional[bool]:
    """布尔属性归一：认得出是 / 否就返回 bool，认不出（含 None、空串）返回 None。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value) if value in (0, 1) else None
    s = str(value).strip().lower()
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    return None


def _coerce_int(value: Any) -> Optional[int]:
    """整数归一（step_no 等序号）：接受 int、整数值 float、纯数字字符串，其余返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    s = str(value).strip()
    return int(s) if s.lstrip("-").isdigit() else None


def _coerce_number(value: Any) -> Optional[float | int]:
    """数值归一：只认不带单位的纯数字（含小数、千分位逗号）。

    带单位的值（「1.2亿元」「70%」）在抽取侧**不换算**：换算口径属于消费方
    （审查项目的精确取值接口按 TBox 类型归一并保留原值），这里猜单位会把错数落进图。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip().replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() and "." not in s else f


def coerce_attribute_types(entities: List["ExtractedEntity"], schema: Optional[dict]) -> Dict[str, int]:
    """按 TBox 声明的属性类型归一实体属性值（原地修改），返回 {"coerced": n, "unparsed": m}。

    - boolean：认得出是 / 否就转 bool；认不出**删键**——布尔字段落一个说不清的字符串，
      下游（动作执行器 / 审查规则）会把它当成「有值」。
    - number：纯数字字符串转 int / float；带单位等认不出的**保留原值**，由消费方按自己的
      口径解析（删掉会丢信息，猜单位会落错数）。
    - 其它类型不动。
    """
    stats = {"coerced": 0, "unparsed": 0}
    if not schema:
        return stats
    types: Dict[str, Dict[str, str]] = {}
    for et in schema.get("entity_types") or []:
        name = et.get("name") or ""
        types[name] = {
            str(a.get("name")): str(a.get("type") or "string").lower()
            for a in (et.get("attributes") or []) if a.get("name")
        }
    for e in entities:
        decl = types.get(e.entity_type)
        if not decl or not e.attributes:
            continue
        attrs = dict(e.attributes)
        changed = False
        for key, value in list(attrs.items()):
            t = decl.get(key)
            if t == "boolean" and not isinstance(value, bool):
                b = _coerce_bool(value)
                if b is None:
                    attrs.pop(key)
                    stats["unparsed"] += 1
                else:
                    attrs[key] = b
                    stats["coerced"] += 1
                changed = True
            elif t == "number" and (isinstance(value, str) or isinstance(value, bool)):
                n = _coerce_number(value)
                if n is None:
                    stats["unparsed"] += 1
                else:
                    attrs[key] = n
                    stats["coerced"] += 1
                    changed = True
        if changed:
            e.attributes = attrs
    return stats


def warn_cardinality_conflicts(relations: List["ExtractedRelation"], schema: Optional[dict]) -> List[str]:
    """基数与步骤序冲突**只告警不删**，返回告警文本列表。

    - cardinality=one_to_one 的关系：同一源实体出现多条出边；
    - HAS_STEP：同一技能下多个动作共用同一 step_no。
    图上保留全部边（LLM 抖动丢数据比冗余边更伤），查询侧按 confidence 取首条。
    """
    warnings: List[str] = []
    if not schema:
        return warnings
    one_to_one = {
        rt.get("name") for rt in (schema.get("relation_types") or [])
        if (rt.get("cardinality") or "") == "one_to_one" and rt.get("name")
    }
    out_edges: Dict[tuple, set] = {}
    step_slots: Dict[tuple, set] = {}
    for r in relations:
        if r.relation_type in one_to_one:
            out_edges.setdefault((r.relation_type, r.source_name), set()).add(r.target_name)
        if r.relation_type == "HAS_STEP":
            n = _coerce_int((r.attributes or {}).get("step_no"))
            if n is not None:
                step_slots.setdefault((r.source_name, n), set()).add(r.target_name)
    for (rel, src), targets in out_edges.items():
        if len(targets) > 1:
            warnings.append(f"{rel} 声明 one_to_one，但 {src} 有 {len(targets)} 条出边: {sorted(targets)}")
    for (skill, n), actions in step_slots.items():
        if len(actions) > 1:
            warnings.append(f"技能 {skill} 的第 {n} 步对应多个动作: {sorted(actions)}")
    for w in warnings:
        logger.warning("ontology 基数/步骤序冲突（只告警不删）: %s", w)
    return warnings


def _endpoint_ok(actual: str, expected) -> bool:
    """关系端点类型校验：空 / unknown 视为通配；expected 为集合时按包含判断。"""
    if isinstance(expected, (set, frozenset)):
        return (not actual) or (actual == "unknown") or (actual in expected)
    return (not actual) or (actual == "unknown") or (actual == expected)


# [jonex] 批 6 C-1：无约束关系降级置信度折扣（保留信号但低于正式关系）
_UNTYPED_EDGE_CONF_DISCOUNT = 0.7


def _keep_untyped_edges_enabled() -> bool:
    """[jonex] 批 6 C-1 兜底通道开关。

    与 57 老代码同名变量、同语义（生产 true、代码默认 false）——为
    no_constraint 且两端类型都在 TBox 内的关系降级映射到通用关系
    RELATES_TO 落图（实体多关系少的根因 L3 兜底；C-2 补 TBox 关系
    定义后剩余 no_constraint 经此通道保留，不再直接丢弃）。

    兜底类型名 = TBox 里已声明的 RELATES_TO（各 TBox 把它声明为「全类型端点」的
    通用关联）—— 此前用 RELATED_TO，与 TBox 的 RELATES_TO 仅一字之差却未被任何
    TBox 声明，落出来的边在编译 schema 里查无此类型（2026-09-11 对齐全仓四份 TBox 时修正）。
    """
    return os.getenv("ONTOLOGY_KEEP_UNTYPED_EDGES", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _keep_fallback_entities_enabled() -> bool:
    """[jonex] 实体侧兜底通道开关（与关系侧 ONTOLOGY_KEEP_UNTYPED_EDGES 对称）。

    §5.8 两层类型设计：TBox 档进本体库、兜底粗类型档只回写 LightRAG 不进本体库。
    对语料与 TBox 完全不对口的文档（实测 kb_demo_internet/歌词 mp3），LLM 按
    「无条件出口」落兜底粗类型池（作品/人物/概念/场景…），结果是本体库 0 实体。

    本开关放行兜底档实体入本体库，默认关（安全优先：兜底粗类型是泛化名词，
    入库会带噪声与偏移的类型标签，属产品语义决策）。

    ⚠️ 兜底类型集合由调用方注入（native_ontology_extractor.load_fallback_types），
    不在本模块导入——native 侧已 import 本模块，反向 import 会成环。
    开关开但未注入集合 → 等同关（见 _post_validate），不静默放行全部类型。
    """
    return os.getenv("ONTOLOGY_KEEP_FALLBACK_ENTITIES", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _type_alias_map(schema: Optional[dict]) -> Dict[str, str]:
    """entity_types 的 aliases → {alias: canonical} 类型归一映射。

    为什么需要：aliases 是中文语料的语义入口，prompt 里如实渲染给 LLM
    （native 侧 _render_type_def），但 valid_etypes 只认 name 字段。LLM 一旦
    把别名当 type 输出（实测：业务模板 Feature.aliases 含 "概念"/"Concept"），
    整批实体被判为 TBox 外类型丢弃。

    策略（显式，三选一必须说清）：
    - canonical names **先占位** → canonical 永远优先于 alias；
    - 同一别名命中多个类型 → **不映射**并记 warning（不做任意映射：
      实测 technology.yaml「任务」→ Event/Mission/Skill 三重冲突）；
    - 大小写敏感，与实体名归一 _alias_map 同口径（避免扩大行为变化面）。

    定义在本模块（而非 native）以便 _post_validate 与 _parse_results 共用；
    native 侧已 import 本模块，反向则成环。
    """
    m: Dict[str, str] = {}
    if not schema:
        return m
    entity_types = schema.get("entity_types") or []

    # ① canonical 先占位
    for et in entity_types:
        name = str(et.get("name") or "").strip()
        if name:
            m[name] = name

    # ② 先扫全量再判定：避免「先到先得」引入遍历顺序依赖
    owners: Dict[str, set] = {}
    for et in entity_types:
        name = str(et.get("name") or "").strip()
        if not name:
            continue
        for a in et.get("aliases") or []:
            key = str(a).strip()
            if not key or key == name:
                continue
            owners.setdefault(key, set()).add(name)

    # ③ 只填「未占用 且 无冲突」的别名
    for alias, alias_owners in owners.items():
        if len(alias_owners) > 1:
            logger.warning(
                "ontology type alias 冲突，跳过映射: alias=%s owners=%s",
                alias, sorted(alias_owners),
            )
            continue
        if alias in m:  # canonical 已占位，别名不覆盖
            continue
        m[alias] = next(iter(alias_owners))
    return m


def _post_validate(
    entities: List[ExtractedEntity],
    relations: List[ExtractedRelation],
    schema: Optional[dict],
    fallback_types: Optional[set] = None,
) -> tuple[List[ExtractedEntity], List[ExtractedRelation]]:
    """丢弃 TBox 之外的实体类型 / 关系类型 / 端点类型不符的关系。

    [jonex] P2（M1）：端点校验从「单值相等」升级为「集合包含」
    （source_types/target_types 为空时回落 source/target 单值）；
    丢弃 schema 外关系前先查 inverse 映射——LLM 输出逆名时换成
    canonical 名并交换 source/target，再走端点校验（物理只落 canonical 方向）。
    """
    if not schema:
        return entities, relations

    valid_etypes = {et.get("name", "") for et in schema.get("entity_types", [])}
    # [jonex] 实体侧兜底通道（ONTOLOGY_KEEP_FALLBACK_ENTITIES，默认关）：
    # 开关开且调用方注入了兜底类型集合 → 并入白名单，兜底档实体得以入库；
    # 同时让「两端类型都在 valid_etypes 内」的 no_constraint 降级条件对兜底档成立。
    # 开关开但未注入集合 → 等同关（安全降级，不静默放行全部类型）。
    _fb_types = (
        set(fallback_types or ()) if _keep_fallback_entities_enabled() else set()
    )
    if _fb_types:
        valid_etypes = valid_etypes | _fb_types
    rel_constraint: Dict[str, dict] = {}
    inverse_map: Dict[str, str] = {}
    for rt in schema.get("relation_types", []):
        name = rt.get("name", "")
        if not name:
            continue
        rel_constraint[name] = {
            "source": rt.get("source", ""),
            "target": rt.get("target", ""),
            "source_types": rt.get("source_types") or [],
            "target_types": rt.get("target_types") or [],
        }
        inverse_of = rt.get("inverse_of")
        if inverse_of:
            inverse_map[inverse_of] = name

    # [jonex] 第二道防线：类型别名归一（解析期 _parse_results 已做，此处幂等无害）。
    # _post_validate 是公开校验入口，直接以别名类型构造的实体也应被接纳并改写成
    # canonical；用 replace 而非原地赋值，保持本函数的纯函数契约。
    _type_alias = _type_alias_map(schema)

    kept_e = []
    dropped_e_by_type: Dict[str, int] = {}
    for e in entities:
        etype = _type_alias.get(e.entity_type, e.entity_type)
        if etype != e.entity_type:
            e = replace(e, entity_type=etype)
        if e.entity_type in valid_etypes or e.entity_type == "unknown":
            kept_e.append(e)
        else:
            dropped_e_by_type[e.entity_type] = dropped_e_by_type.get(e.entity_type, 0) + 1
    kept_r = []
    dropped_r_by_reason: Dict[str, int] = {}
    for r in relations:
        constraint = rel_constraint.get(r.relation_type)
        if constraint is None:
            # [jonex] P2：丢弃前先查 inverse 映射（逆名是虚拟名，物理只落 canonical）
            canonical_name = inverse_map.get(r.relation_type)
            if canonical_name:
                r = ExtractedRelation(
                    source_name=r.target_name,
                    source_type=r.target_type,
                    target_name=r.source_name,
                    target_type=r.source_type,
                    relation_type=canonical_name,
                    confidence=r.confidence,
                    source_chunks=r.source_chunks,
                    keywords=r.keywords,          # [jonex] P7：翻转不丢关键词
                    attributes=r.attributes,      # [jonex] P7：翻转不丢关系属性（step_no 等）
                )
                constraint = rel_constraint.get(canonical_name)
            if constraint is None:
                # [jonex] 批 6 C-1 兜底通道：两端类型都在 TBox 内的无约束
                # 关系不直接丢——降级映射到通用关系 RELATES_TO 落图（与 57
                # 老代码 KEEP_UNTYPED_EDGES 放宽形态一致），原始关系名存
                # attributes.raw_relation_type 保留语义，confidence 打折标记
                # 降级。开关默认关；一端不在 TBox 或开关关闭时维持丢弃口径。
                # 类型名取 RELATES_TO：四份 TBox 均按「全类型端点」声明了它，
                # 落图类型因此始终可在编译 schema 里解析（详见开关函数 docstring）。
                if (
                    _keep_untyped_edges_enabled()
                    and r.source_type in valid_etypes
                    and r.target_type in valid_etypes
                ):
                    kept_r.append(ExtractedRelation(
                        source_name=r.source_name,
                        source_type=r.source_type,
                        target_name=r.target_name,
                        target_type=r.target_type,
                        relation_type="RELATES_TO",
                        confidence=r.confidence * _UNTYPED_EDGE_CONF_DISCOUNT,
                        source_chunks=r.source_chunks,
                        keywords=r.keywords,
                        attributes={
                            **(r.attributes or {}),
                            "raw_relation_type": r.relation_type,
                        },
                    ))
                    dropped_r_by_reason["downgraded_relates_to"] = (
                        dropped_r_by_reason.get("downgraded_relates_to", 0) + 1
                    )
                else:
                    dropped_r_by_reason["no_constraint"] = (
                        dropped_r_by_reason.get("no_constraint", 0) + 1
                    )
                continue
        allowed_src = set(constraint["source_types"]) or {constraint["source"]}
        allowed_tgt = set(constraint["target_types"]) or {constraint["target"]}
        # 兜底档不属于任何 TBox 类型，端点约束对其必然 mismatch → 随开关同步放宽，
        # 否则兜底实体入了库但边全丢，图上只剩一堆孤立节点。
        if _fb_types:
            allowed_src = allowed_src | _fb_types
            allowed_tgt = allowed_tgt | _fb_types
        # 端点类型同走别名归一（第二道防线，与实体侧一致）——否则实体被归一
        # 留下来了，边却因端点仍是别名而 mismatch 丢光，图上只剩孤立节点。
        _stype = _type_alias.get(r.source_type, r.source_type)
        _ttype = _type_alias.get(r.target_type, r.target_type)
        if _stype != r.source_type or _ttype != r.target_type:
            r = replace(r, source_type=_stype, target_type=_ttype)
        # [jonex] 端点类型为空或 unknown 时视为通配，避免未归类实体把整条边丢光（孤立节点根因）。
        src_ok = _endpoint_ok(r.source_type, allowed_src)
        tgt_ok = _endpoint_ok(r.target_type, allowed_tgt)
        if src_ok and tgt_ok:
            kept_r.append(r)
        else:
            dropped_r_by_reason["endpoint_mismatch"] = (
                dropped_r_by_reason.get("endpoint_mismatch", 0) + 1
            )

    # [jonex] P5-var 观测：reject 计数与原因分桶——TBox 档相对两档全集的
    # 丢损可见（类型超出 TBox / 关系无约束 / 端点不符），静默丢弃无法
    # 区分「LLM 没抽出来」与「抽出来被校验丢了」。
    if dropped_e_by_type or dropped_r_by_reason:
        logger.info(
            "ontology _post_validate reject: entities=%s relations=%s",
            dict(dropped_e_by_type),
            dict(dropped_r_by_reason),
        )
    return kept_e, kept_r
