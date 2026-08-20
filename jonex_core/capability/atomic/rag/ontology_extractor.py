#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
本体抽取器 — Stage4 核心组件。

跑在 atomic-rag 进程内，接收 LightRAG 已抽取的候选实体 + 原文 chunk，
通过 LLM 对其进行本体类型归类、属性补全、关系分类。

输出结构化的 ExtractionResult，经 Redis task 中转后由 knowledge-base
reconcile 循环写入 PostgreSQL。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from jonex_core.capability.atomic.ontology import OntologyRegistry

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
    description: str = ""           # [jonex] 来自 LightRAG NER 的实体描述，answer_from_facts 的核心上下文
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


@dataclass
class ExtractionResult:
    entities: List[ExtractedEntity] = field(default_factory=list)
    relations: List[ExtractedRelation] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    raw_llm_response: Optional[str] = None
    ok: bool = True                 # [jonex] 产出 ≥1 实体即 True；全批失败/零产出才 False


def _is_table_chunk_entity(entity: dict) -> bool:
    """[jonex] O6：判定 LightRAG 实体是否来自表格 chunk。

    实体的 file_path 是 chunk 的 file_source；ctype=table_row/table 即
    表格 chunk（Row-as-Object 已确定性接管表格实体，通用抽取结果丢弃）。
    """
    fp = entity.get("file_path") or ""
    if not fp or "ctype=" not in fp:
        return False
    try:
        from jonex_core.common.file_source_util import parse_file_source

        parsed = parse_file_source(fp)
        return parsed.get("chunk_type") in ("table_row", "table")
    except (NameError, AttributeError, ImportError):
        raise  # 代码 bug，不掩盖
    except Exception as exc:  # noqa: BLE001 — 解析失败按非表格处理
        logger.debug("O6 _is_table_chunk_entity 解析降级: %s", exc)
        return False


# ============================================================
# LLM 调用（OpenAI 兼容接口）
# ============================================================


def _extract_concurrency() -> int:
    """本体抽取的 LLM 批次并发度（实体归类/关系定型共用）。

    这些批次经 llm-gateway → 上游 LLM（deepseek-v4-flash），与入库期 LightRAG
    抽取共用同一上游并发额度（实测 tokenhub 约 48 并发才触发 429，见
    scripts/bench_llm_extract.py）。ontology_extract 在 LightRAG 上传阶段之后执行，
    通常不与同文档的 LightRAG 抽取同时打满上游；默认 8 保守取值，落在无限流区且
    给查询期/多文档并行留余量。经 ONTOLOGY_EXTRACT_CONCURRENCY 调整（<1 归一）。
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
) -> str:
    """调用 OpenAI 兼容接口（deepseek-v4-flash 等）。

    Args:
        json_mode: 开启 response_format=json_object（开关 ONTOLOGY_EXTRACT_JSON_MODE）
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
    model = os.getenv(
        "ONTOLOGY_LLM_MODEL",
        os.getenv("LLM_MODEL", "deepseek-v4-flash-202605"),
    )
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
        return data["choices"][0]["message"]["content"]


# ============================================================
# Prompt 模板
# ============================================================


SYSTEM_PROMPT_TEMPLATE = """你是一个本体抽取助手。根据以下领域本体定义，将输入的候选实体归类到定义的类型、补全属性、为关系指定类型。

## 本体定义（JSON）
{ontology_json}

## 要求
1. 只输出 schema 中定义的实体类型和关系类型
2. 每个实体必须包含 confidence (0.0-1.0) 字段，反映归类正确性的置信度
3. 如果候选实体无法匹配任何类型，使用 "unknown" 类型并降低 confidence
4. attributes 根据 schema 中的属性定义从原文中提取
5. 关系从实体对的上下文推断，relation_type 必须在 schema 中定义
6. 输出必须是合法的 JSON，不能包含 markdown 代码块标记
7. 每个关系必须包含 source_type 和 target_type，值是实体在本体定义中的类型名
8. 若本体定义含 "synonyms" 段（同义词组，每组含 canonical 标准词与 terms 等价词），实体命中某组任一词时，name 统一填该组 canonical"""


USER_PROMPT_TEMPLATE = """## 候选实体列表
{entities_json}

## 原文上下文
{chunks_json}

## 输出格式（JSON）
{{
  "entities": [
    {{"name": "实体名", "type": "实体类型", "aliases": ["别名1"], "attributes": {{"key": "value"}}, "confidence": 0.95}}
  ],
  "relations": [
    {{"source": "源实体", "source_type": "实体类型", "target": "目标实体", "target_type": "实体类型", "relation_type": "关系类型", "confidence": 0.9}}
  ]
}}"""

# ── Phase 2/3：实体专用 prompts（分批、独立调用） ────────────

# [jonex] 要求 5 为 KB 级同义词归一指令：本体定义 synonyms 段命中则 name 统一填 canonical
ENTITY_SYSTEM_PROMPT = """你是本体实体归类助手。根据以下领域本体定义，把候选实体归类到已定义类型并补全属性。
## 本体定义（JSON）
{ontology_json}
## 要求
1. 只输出 schema 中定义的实体类型；无法匹配用 "unknown" 并降低 confidence
2. 每个实体必须含 confidence(0.0-1.0)
3. attributes 仅对 schema 中定义了属性的类型提取；找不到留空，禁止编造
4. 只输出合法 JSON，不要 markdown 代码块
5. 若本体定义含 "synonyms" 段（同义词组，每组含 canonical 标准词与 terms 等价词），候选实体命中某组任一词时，name 统一填该组 canonical"""

ENTITY_USER_PROMPT = """## 候选实体（本批 {n} 个）
{entities_json}
## 输出格式（JSON）
{{"entities":[{{"name":"实体名","original":"输入候选名","type":"实体类型","aliases":["别名"],"attributes":{{}},"confidence":0.95}}]}}"""

# ── Phase 3：关系定型 prompts（对 LightRAG 已有边做类型映射）──

RELATION_SYSTEM_PROMPT = """你是本体关系定型助手。给定已抽取的实体关系边，把每条边映射到 schema 中定义的关系类型。
## 本体关系定义（JSON）
{relations_json}
## 要求
1. relation_type 必须是 schema 中定义的关系名；无法匹配则【不输出】该条
2. 不要臆造 schema 之外的关系，不要新增边
3. 不得改写、不得新增端点名（source/target），端点名仅用于参考语义
4. 只输出合法 JSON，不要 markdown"""

RELATION_USER_PROMPT = """## 候选关系边（本批 {n} 条，来自 LightRAG）
{edges_json}
## 输出格式（JSON）
{{"relations":[{{"source":"源实体","target":"目标实体","relation_type":"关系类型","confidence":0.9}}]}}"""


# ============================================================
# OntologyExtractor
# ============================================================


class OntologyExtractor:
    """本体抽取器 — 用 LLM 对候选实体做本体类型归类。

    支持 compiled schema 优先读取策略（优先级从高到低）：
    1. 调用方传入的 compiled_schema 参数
    2. scope 含 tenant_id + knowledge_base_id 时使用 CompiledSchemaClient
    3. 否则 fallback 到 OntologyRegistry（domain="default"）
    """

    def __init__(self, registry: Optional[OntologyRegistry] = None):
        self._registry = registry
        self._compiled_client: Optional[Any] = None

    def _get_compiled_client(self):
        if self._compiled_client is None:
            from jonex_core.capability.atomic.ontology.compiled_schema_client import (
                CompiledSchemaClient,
            )
            self._compiled_client = CompiledSchemaClient()
        return self._compiled_client

    async def _resolve_schema(
        self,
        compiled_schema: Optional[dict],
        scope: Dict[str, Any],
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
                client = self._get_compiled_client()
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
        if self._registry is not None:
            domain = scope.get("domain", "default")
            ontology_json = self._registry.to_prompt_json(domain)
            logger.info("Fallback to OntologyRegistry domain=%s", domain)
            return None, ontology_json

        return None, None

    async def extract(
        self,
        content_list: List[Dict[str, Any]],
        lightrag_entities: List[Dict[str, Any]],
        scope: Dict[str, Any],
        compiled_schema: Optional[dict] = None,
        lightrag_relations: Optional[List[dict]] = None,
        edge_based: Optional[bool] = None,
    ) -> ExtractionResult:
        """执行本体抽取。

        Args:
            content_list: 原文 chunks。
            lightrag_entities: LightRAG 已抽取的候选实体列表。
            scope: 任务上下文 {"tenant_id", "knowledge_base_id", "document_id"}。
            compiled_schema: per-KB compiled schema dict（可选，push 模式）。
            lightrag_relations: LightRAG 已抽关系边列表（Phase 3，去 chunk 定型）。
            edge_based: 显式强制边定型开关（None=读环境开关；True/False=覆盖环境）。
                [jonex] P0-A.6/P2：ontology-only / reparse 传 True 强制走"对已抽边做类型
                映射"分支，不受 ONTOLOGY_EXTRACT_DROP_CHUNKS 环境态影响；关系列表为空允许成功。

        Returns:
            ExtractionResult: 结构化抽取结果。
        """
        schema, ontology_json = await self._resolve_schema(compiled_schema, scope)
        if not ontology_json:
            return ExtractionResult(ok=False, errors=["无可用 ontology schema，跳过本体抽取"])

        # [jonex] O6（§11.2）：表格 chunk 来源的 LightRAG 实体丢弃——表格实体
        # 由 Row-as-Object 确定性生成（attributes 结构化、按行独立实例），
        # 通用文本抽取对表格的低质结果不占 200 截断名额、不造成同名污染。
        _drop_table_entities = os.getenv(
            "TABLE_OBJECT_FILTER_LIGHTRAG_TABLE_ENTITIES", "true",
        ).lower() in ("1", "true", "yes", "on")
        if _drop_table_entities and lightrag_entities:
            lightrag_entities = [
                e for e in lightrag_entities
                if not _is_table_chunk_entity(e)
            ]

        # Phase 4：过滤 + 排序 + 裁剪
        max_entities = int(os.getenv("ONTOLOGY_EXTRACT_MAX_ENTITIES", "200"))
        filtered = self._filter_and_sort(lightrag_entities)[:max_entities]
        if not filtered:
            return ExtractionResult(ok=False, errors=["无候选实体，跳过本体抽取"])

        # Phase 4：代码预分类（命中 aliases 的实体不进 LLM）
        type_index, case_insensitive = self._build_type_index(schema)
        pre_classified: List[ExtractedEntity] = []
        ambiguous: List[dict] = []
        for e in filtered:
            code = self._match_type(e, type_index, case_insensitive)
            if code:
                name = e.get("name", e.get("entity_name", ""))
                pre_classified.append(ExtractedEntity(
                    canonical_name=name,
                    entity_type=code,
                    aliases=[name],
                    confidence=0.9,
                ))
            else:
                ambiguous.append({
                    "name": e.get("name", e.get("entity_name", "")),
                    "original_type": e.get("type", ""),
                    "description": (e.get("description", "") or "")[:300],
                })

        # 仅歧义实体进分批 LLM
        llm_entities, ent_errors, any_batch_ok = await self._classify_entities(
            ambiguous, ontology_json, scope,
        )
        entities = pre_classified + llm_entities

        # 2) 关系定型（Phase 3）：实体最终类型 map（含预分类 + LLM 分类并集）
        entity_type_map = {e.canonical_name: e.entity_type for e in entities}

        # [jonex] edge_based 显式参数优先于环境开关：
        #   None  → 读 ONTOLOGY_EXTRACT_DROP_CHUNKS 且要求 lightrag_relations 非 None（保持旧语义）
        #   True  → 强制边定型（ontology-only / reparse），关系为空也允许成功
        #   False → 强制走 legacy chunk 抽取
        if edge_based is None:
            use_edge = (
                os.getenv("ONTOLOGY_EXTRACT_DROP_CHUNKS", "true").lower() in ("1", "true", "yes", "on")
                and lightrag_relations is not None
            )
        else:
            use_edge = edge_based
        if use_edge:
            # Phase 3：对 LightRAG 已有边做类型映射，去 chunk
            relations_json = json.dumps(
                schema.get("prompt_schema", {}).get("relation_types", [])
                if schema else [],
                ensure_ascii=False,
            ) if ontology_json else "[]"
            # 如果 schema 来自 OntologyRegistry fallback，relation_types 在 ontology_json 内
            if not relations_json or relations_json == "[]":
                try:
                    parsed = json.loads(ontology_json)
                    relations_json = json.dumps(parsed.get("relation_types", []), ensure_ascii=False)
                except Exception:
                    relations_json = "[]"
            relations, rel_errors = await self._type_relations(
                lightrag_relations or [], entity_type_map, relations_json, scope,
            )
        else:
            # 回退：Phase 2 的 chunk 单次抽取
            relations, rel_errors = await self._extract_relations_legacy(
                filtered, content_list, ontology_json, scope,
            )

        # Phase 5：溯源回填（按 name + aliases 匹配候选实体的 source_id/file_path）
        # [jonex] 方案⑧ D：索引覆盖 canonical_name 与 aliases，降低归一化/翻译改名 miss
        prov: dict[str, dict] = {}
        for e in filtered:
            p = {
                "source_id": e.get("source_id", ""), "file_path": e.get("file_path", "")
            }
            if p["source_id"] or p["file_path"]:
                prov[e.get("name", e.get("entity_name", ""))] = p
                for alias in (e.get("aliases") or []):
                    if alias and alias not in prov:
                        prov[alias] = p
        for ent in entities:
            p = prov.get(ent.canonical_name)
            if not p:
                for alias in (ent.aliases or []):
                    p = prov.get(alias)
                    if p:
                        break
            if p and (p["source_id"] or p["file_path"]):
                ent.source_chunks = [p]

        # [jonex] 描述回填：LightRAG NER 的 description 是 answer_from_facts 的核心上下文，
        # 预分类分支与 LLM 分类分支都不带描述，统一在此按 name 从候选实体回填。
        desc_maxlen = int(os.getenv("ONTOLOGY_ENTITY_DESC_MAXLEN", "1000"))
        desc_map = {
            e.get("name", e.get("entity_name", "")): (e.get("description", "") or "")
            for e in filtered
        }
        for ent in entities:
            if not ent.description:
                ent.description = desc_map.get(ent.canonical_name, "")[:desc_maxlen]

        # P2C：关系端点实体兜底补齐 —— 对"已保留关系"的端点名，凡不在正式实体集合中则补建
        # entity_type="unknown" 的正式实体（非 stub），保证关系两端在写库时不因找不到节点而捏造 stub。
        if os.getenv("ONTOLOGY_ENTITY_BACKFILL_ENDPOINTS", "true").lower() in ("1", "true", "yes", "on"):
            known_names = {e.canonical_name for e in entities}
            endpoint_names = {r.source_name for r in relations} | {r.target_name for r in relations}
            for nm in endpoint_names - known_names:
                entities.append(ExtractedEntity(
                    canonical_name=nm,
                    entity_type="unknown",
                    aliases=[nm],
                    confidence=0.3,
                    extraction_method="endpoint_backfill",
                ))

        # Phase 5：后校验（丢弃 TBox 之外的类型/关系）
        entities, relations = self._post_validate(entities, relations, schema)

        # [jonex] 边兜底（决策点①，默认关）：schema relation_types 覆盖稀疏时，
        # 对 LightRAG 已有边、未被 typed 关系覆盖的实体对，落通用 RELATED_TO 边保证图非空。
        if os.getenv("ONTOLOGY_KEEP_UNTYPED_EDGES", "false").lower() in ("1", "true", "yes", "on"):
            relations.extend(
                self._fallback_untyped_edges(lightrag_relations or [], relations, entity_type_map)
            )

        ok = bool(entities) or any_batch_ok
        return ExtractionResult(
            entities=entities,
            relations=relations,
            errors=ent_errors + rel_errors,
            ok=ok,
        )

    # ── Phase 5：后校验 ──────────────────────────

    @staticmethod
    def _post_validate(
        entities: List[ExtractedEntity],
        relations: List[ExtractedRelation],
        schema: Optional[dict],
    ) -> tuple[List[ExtractedEntity], List[ExtractedRelation]]:
        """丢弃 TBox 之外的实体类型 / 关系类型 / 端点类型不符的关系。"""
        if not schema:
            return entities, relations

        valid_etypes = {et.get("name", "") for et in schema.get("entity_types", [])}
        rel_constraint = {
            rt.get("name", ""): (rt.get("source", ""), rt.get("target", ""))
            for rt in schema.get("relation_types", [])
        }

        kept_e = [e for e in entities if e.entity_type in valid_etypes or e.entity_type == "unknown"]
        kept_r = []
        for r in relations:
            st = rel_constraint.get(r.relation_type)
            if st is None:
                continue
            # [jonex] 端点类型为空或 unknown 时视为通配，避免未归类实体把整条边丢光（孤立节点根因）。
            src_ok = OntologyExtractor._endpoint_ok(r.source_type, st[0])
            tgt_ok = OntologyExtractor._endpoint_ok(r.target_type, st[1])
            if src_ok and tgt_ok:
                kept_r.append(r)
        return kept_e, kept_r

    @staticmethod
    def _endpoint_ok(actual: str, expected: str) -> bool:
        """关系端点类型校验：空 / unknown 视为通配。"""
        return (not actual) or (actual == "unknown") or (actual == expected)

    @staticmethod
    def _rel_source_chunks(lr: dict) -> list:
        """从一条 lightrag 关系边提取 source_chunks。

        graph_reader._map_relation 已带出 file_path/source_id，
        此处组装为与实体 source_chunks 同构的 [{source_id, file_path}]。
        """
        sid = lr.get("source_id", "") or ""
        fp = lr.get("file_path", "") or ""
        if sid or fp:
            return [{"source_id": sid, "file_path": fp}]
        return []

    @staticmethod
    def _fallback_untyped_edges(
        lightrag_relations: List[dict],
        typed_relations: List[ExtractedRelation],
        entity_type_map: Dict[str, str],
    ) -> List[ExtractedRelation]:
        """决策点①：对未被 typed 关系覆盖的 LightRAG 边补通用 RELATED_TO 边。"""
        typed_pairs = {(r.source_name, r.target_name) for r in typed_relations}
        typed_pairs |= {(r.target_name, r.source_name) for r in typed_relations}
        extra: List[ExtractedRelation] = []
        for lr in lightrag_relations:
            s = lr.get("source_entity", "")
            t = lr.get("target_entity", "")
            if not s or not t or (s, t) in typed_pairs:
                continue
            typed_pairs.add((s, t))
            typed_pairs.add((t, s))
            try:
                conf = float(lr.get("weight", 0.5))
            except (ValueError, TypeError):
                conf = 0.5
            extra.append(ExtractedRelation(
                source_name=s, target_name=t,
                source_type=entity_type_map.get(s, ""),
                target_type=entity_type_map.get(t, ""),
                relation_type="RELATED_TO", confidence=conf,
                source_chunks=OntologyExtractor._rel_source_chunks(lr),
            ))
        return extra

    # ── 解析 ────────────────────────────────────

    @staticmethod
    def _load_json(raw: str) -> Optional[dict]:
        """通用 JSON 解析：去 markdown 包裹 + 花括号兜底。"""
        import re

        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            s, e = cleaned.find("{"), cleaned.rfind("}")
            if s != -1 and e > s:
                try:
                    return json.loads(cleaned[s:e + 1])
                except json.JSONDecodeError:
                    return None
        return None

    @staticmethod
    def _parse_llm_response(raw: str) -> ExtractionResult:
        """解析 LLM 返回的 JSON（旧风格单次调用，兼容保留）。"""
        data = OntologyExtractor._load_json(raw)
        if data is None:
            return ExtractionResult(errors=["LLM 输出不是合法 JSON"])

        entities = []
        relations = []
        errors = []

        for ent in data.get("entities", []):
            try:
                entities.append(
                    ExtractedEntity(
                        canonical_name=ent.get("name", ""),
                        entity_type=ent.get("type", "unknown"),
                        aliases=ent.get("aliases", []),
                        attributes=ent.get("attributes", {}),
                        confidence=float(ent.get("confidence", 1.0)),
                    )
                )
            except (ValueError, TypeError) as e:
                errors.append(f"实体解析失败: {ent.get('name', '?')} → {e}")

        for rel in data.get("relations", []):
            try:
                relations.append(
                    ExtractedRelation(
                        source_name=rel.get("source", ""),
                        source_type=rel.get("source_type", ""),
                        target_name=rel.get("target", ""),
                        target_type=rel.get("target_type", ""),
                        relation_type=rel.get("relation_type", ""),
                        confidence=float(rel.get("confidence", 1.0)),
                    )
                )
            except (ValueError, TypeError) as e:
                errors.append(f"关系解析失败: {rel.get('source', '?')}→{rel.get('target', '?')} → {e}")

        return ExtractionResult(
            entities=entities,
            relations=relations,
            errors=errors,
        )

    # ── Phase 4：预分类 + 过滤 ────────────────────

    @staticmethod
    def _build_type_index(schema: Optional[dict]) -> tuple[dict, bool]:
        """从 compiled schema 的 entity_types aliases 构 {归一化别名 -> entity_code} 索引。"""
        if not schema:
            return {}, True
        case_insensitive = (schema.get("disambiguation") or {}).get("case_insensitive", True)
        index: dict[str, str] = {}
        for et in schema.get("entity_types", []):
            code = et.get("name", "")
            if not code:
                continue
            keys = [code, et.get("display_name", "")] + (et.get("aliases") or [])
            for k in keys:
                k = (k or "").strip()
                if not k:
                    continue
                index[k.lower() if case_insensitive else k] = code
        return index, case_insensitive

    @staticmethod
    def _match_type(raw_entity: dict, index: dict, case_insensitive: bool) -> Optional[str]:
        """仅用 LightRAG 的 type 标签匹配领域 entity_code；命中返回 code，否则 None。"""
        t = (raw_entity.get("type", "") or "").strip()
        if not t:
            return None
        return index.get(t.lower() if case_insensitive else t)

    @staticmethod
    def _filter_and_sort(lightrag_entities: List[dict]) -> List[dict]:
        """丢弃孤立（度=0 且 描述为空）并按 relations_count 降序。"""
        drop_orphan = os.getenv("ONTOLOGY_EXTRACT_DROP_ORPHAN", "true").lower() in ("1", "true", "yes", "on")
        items = lightrag_entities
        if drop_orphan:
            items = [
                e for e in items
                if not (e.get("relations_count", 0) == 0 and not (e.get("description", "") or "").strip())
            ]
        return sorted(items, key=lambda e: e.get("relations_count", 0), reverse=True)

    # ── Phase 2：实体分批归类 ────────────────────

    async def _classify_entities(
        self,
        entities_snapshot: List[dict],
        ontology_json: str,
        scope: Optional[Dict[str, Any]],
    ) -> tuple[List[ExtractedEntity], List[str], bool]:
        """实体分批归类，返回 (entities, errors, any_batch_ok)。"""
        batch_size = int(os.getenv("ONTOLOGY_EXTRACT_BATCH_SIZE", "35"))
        max_tokens = int(os.getenv("ONTOLOGY_EXTRACT_MAX_TOKENS", "4096"))
        json_mode = os.getenv("ONTOLOGY_EXTRACT_JSON_MODE", "true").lower() in ("1", "true", "yes", "on")
        system_prompt = ENTITY_SYSTEM_PROMPT.format(ontology_json=ontology_json)

        batches = [
            entities_snapshot[i:i + batch_size]
            for i in range(0, len(entities_snapshot), batch_size)
        ]
        sem = asyncio.Semaphore(_extract_concurrency())

        async def _run_batch(batch_idx: int, batch: List[dict]) -> tuple[List[ExtractedEntity], List[str], bool]:
            """处理单批实体归类，返回 (entities, errors, any_ok)。批间无依赖，可并行。"""
            b_entities: List[ExtractedEntity] = []
            b_errors: List[str] = []
            user_prompt = ENTITY_USER_PROMPT.format(n=len(batch), entities_json=json.dumps(batch, ensure_ascii=False))
            async with sem:  # 仅限制并发 LLM 调用；JSON 解析开销小，放锁外也可，这里从简
                try:
                    raw = await _openai_chat(
                        messages=[{"role": "system", "content": system_prompt},
                                  {"role": "user", "content": user_prompt}],
                        temperature=0, max_tokens=max_tokens, scope=scope, json_mode=json_mode,
                    )
                except Exception as e:
                    b_errors.append(f"实体批#{batch_idx} 调用失败: {e}")
                    return b_entities, b_errors, False

            data = self._load_json(raw)
            if data is None:
                b_errors.append(f"实体批#{batch_idx} 输出非合法 JSON")
                return b_entities, b_errors, False

            for ent in data.get("entities", []):
                try:
                    name = ent.get("name", "")
                    original = ent.get("original", "")
                    aliases = list({*ent.get("aliases", []), name, original} - {""})
                    b_entities.append(ExtractedEntity(
                        canonical_name=name, entity_type=ent.get("type", "unknown"),
                        aliases=aliases, attributes=ent.get("attributes", {}),
                        confidence=float(ent.get("confidence", 1.0)),
                    ))
                except (ValueError, TypeError) as e:
                    b_errors.append(f"实体解析失败 {ent.get('name','?')}: {e}")
            return b_entities, b_errors, True

        # 并发跑各批，按批序聚合（保持结果与错误信息的确定性顺序）
        results = await asyncio.gather(*[_run_batch(i, b) for i, b in enumerate(batches)])
        entities: List[ExtractedEntity] = []
        errors: List[str] = []
        any_ok = False
        for b_entities, b_errors, b_ok in results:
            entities.extend(b_entities)
            errors.extend(b_errors)
            any_ok = any_ok or b_ok

        return entities, errors, any_ok

    # ── Phase 2 临时：关系抽取（保留 chunk，Phase 3 替换为边定型）──

    async def _extract_relations_legacy(
        self,
        entities_snapshot: List[dict],
        content_list: List[Dict[str, Any]],
        ontology_json: str,
        scope: Optional[Dict[str, Any]],
    ) -> tuple[List[ExtractedRelation], List[str]]:
        """占位：Phase 3 用 _type_relations 整体替换此方法并去掉 chunk。"""
        # TODO(Phase3): replaced by _type_relations
        max_chunks = int(os.getenv("ONTOLOGY_EXTRACT_MAX_CHUNKS", "50"))
        max_tokens = int(os.getenv("ONTOLOGY_EXTRACT_MAX_TOKENS", "4096"))
        json_mode = os.getenv("ONTOLOGY_EXTRACT_JSON_MODE", "true").lower() in ("1", "true", "yes", "on")

        chunks_snapshot = [
            {"content": (c.get("content", "") or "")[:500]}
            for c in (content_list or [])[:max_chunks]
        ]

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(ontology_json=ontology_json)
        user_prompt = USER_PROMPT_TEMPLATE.format(
            entities_json=json.dumps(entities_snapshot, ensure_ascii=False),
            chunks_json=json.dumps(chunks_snapshot, ensure_ascii=False),
        )

        try:
            raw = await _openai_chat(
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user_prompt}],
                temperature=0, max_tokens=max_tokens, scope=scope, json_mode=json_mode,
            )
        except Exception as e:
            return [], [f"关系抽取调用失败: {e}"]

        data = self._load_json(raw)
        if data is None:
            return [], ["关系抽取输出非合法 JSON"]

        relations, errors = [], []
        for rel in data.get("relations", []):
            try:
                relations.append(ExtractedRelation(
                    source_name=rel.get("source", ""),
                    source_type=rel.get("source_type", ""),
                    target_name=rel.get("target", ""),
                    target_type=rel.get("target_type", ""),
                    relation_type=rel.get("relation_type", ""),
                    confidence=float(rel.get("confidence", 1.0)),
                ))
            except (ValueError, TypeError) as e:
                errors.append(f"关系解析失败: {rel.get('source', '?')}→{rel.get('target', '?')} → {e}")

        return relations, errors

    # ── Phase 3：关系定型（去 chunk，对 LightRAG 已有边做映射）──

    async def _type_relations(
        self,
        lightrag_relations: List[dict],
        entity_type_map: Dict[str, str],
        relations_json: str,
        scope: Optional[Dict[str, Any]],
    ) -> tuple[List[ExtractedRelation], List[str]]:
        """对 LightRAG 已有边做类型映射（分批、无 chunk、端点类型用 entity_type_map 回填）。"""
        rel_batch = int(os.getenv("ONTOLOGY_EXTRACT_REL_BATCH_SIZE", "50"))
        max_tokens = int(os.getenv("ONTOLOGY_EXTRACT_MAX_TOKENS", "4096"))
        json_mode = os.getenv("ONTOLOGY_EXTRACT_JSON_MODE", "true").lower() in ("1", "true", "yes", "on")
        use_original_endpoint = os.getenv("ONTOLOGY_REL_USE_ORIGINAL_ENDPOINT", "true").lower() in ("1", "true", "yes", "on")

        edges = [
            {"source": r.get("source_entity", ""), "target": r.get("target_entity", ""),
             "description": (r.get("description", "") or "")[:200]}
            for r in lightrag_relations
            if r.get("source_entity") and r.get("target_entity")
        ]
        if not edges:
            return [], []

        # [jonex] 方案⑧：预建 source_chunks 索引（不改 LLM payload）
        # sc_by_idx 与 edges 同过滤口径，保证 idx 严格对齐
        _filtered_lr = [lr for lr in lightrag_relations
                        if lr.get("source_entity") and lr.get("target_entity")]
        sc_by_idx = {j: OntologyExtractor._rel_source_chunks(lr)
                     for j, lr in enumerate(_filtered_lr)}
        sc_by_pair = {(lr.get("source_entity", ""), lr.get("target_entity", "")):
                      OntologyExtractor._rel_source_chunks(lr)
                      for lr in _filtered_lr}

        system_prompt = RELATION_SYSTEM_PROMPT.format(relations_json=relations_json)
        batches = [edges[i:i + rel_batch] for i in range(0, len(edges), rel_batch)]
        sem = asyncio.Semaphore(_extract_concurrency())

        async def _run_batch(batch_idx: int, batch: List[dict]) -> tuple[List[ExtractedRelation], List[str]]:
            """处理单批关系定型，返回 (relations, errors)。批间无依赖，可并行。"""
            b_relations: List[ExtractedRelation] = []
            b_errors: List[str] = []
            if use_original_endpoint:
                # P2A: 边带 idx，LLM 只回 idx→relation_type，端点名取原始边
                indexed_batch = [
                    {"idx": j, "source": e["source"], "target": e["target"],
                     "description": e["description"]}
                    for j, e in enumerate(batch)
                ]
                edge_by_idx = {j: e for j, e in enumerate(batch)}
                user_prompt = (
                    "## 候选关系边（本批 {n} 条，来自 LightRAG）\n"
                    "{edges_json}\n"
                    "## 输出格式（JSON）\n"
                    '{{"relations":[{{"idx":0,"relation_type":"关系类型","confidence":0.9}}]}}'
                ).format(n=len(indexed_batch), edges_json=json.dumps(indexed_batch, ensure_ascii=False))
                async with sem:
                    try:
                        raw = await _openai_chat(
                            messages=[{"role": "system", "content": system_prompt},
                                      {"role": "user", "content": user_prompt}],
                            temperature=0, max_tokens=max_tokens, scope=scope, json_mode=json_mode,
                        )
                    except Exception as e:
                        b_errors.append(f"关系批#{batch_idx} 调用失败: {e}")
                        return b_relations, b_errors
                data = self._load_json(raw)
                if data is None:
                    b_errors.append(f"关系批#{batch_idx} 输出非合法 JSON")
                    return b_relations, b_errors
                for rel in data.get("relations", []):
                    idx = rel.get("idx")
                    if idx is None or idx not in edge_by_idx:
                        continue
                    e = edge_by_idx[idx]
                    src, tgt = e["source"], e["target"]
                    b_relations.append(ExtractedRelation(
                        source_name=src, target_name=tgt,
                        source_type=entity_type_map.get(src, ""),
                        target_type=entity_type_map.get(tgt, ""),
                        relation_type=rel.get("relation_type", ""),
                        confidence=float(rel.get("confidence", 1.0)),
                        source_chunks=sc_by_idx.get(
                            batch_idx * rel_batch + idx, []),   # [jonex] 方案⑧
                    ))
            else:
                user_prompt = RELATION_USER_PROMPT.format(n=len(batch), edges_json=json.dumps(batch, ensure_ascii=False))
                async with sem:
                    try:
                        raw = await _openai_chat(
                            messages=[{"role": "system", "content": system_prompt},
                                      {"role": "user", "content": user_prompt}],
                            temperature=0, max_tokens=max_tokens, scope=scope, json_mode=json_mode,
                        )
                    except Exception as e:
                        b_errors.append(f"关系批#{batch_idx} 调用失败: {e}")
                        return b_relations, b_errors
                data = self._load_json(raw)
                if data is None:
                    b_errors.append(f"关系批#{batch_idx} 输出非合法 JSON")
                    return b_relations, b_errors
                for rel in data.get("relations", []):
                    src, tgt = rel.get("source", ""), rel.get("target", "")
                    b_relations.append(ExtractedRelation(
                        source_name=src, target_name=tgt,
                        source_type=entity_type_map.get(src, ""),
                        target_type=entity_type_map.get(tgt, ""),
                        relation_type=rel.get("relation_type", ""),
                        confidence=float(rel.get("confidence", 1.0)),
                        source_chunks=sc_by_pair.get((src, tgt), []),   # [jonex] 方案⑧
                    ))
            return b_relations, b_errors

        # 并发跑各批，按批序聚合（保持结果与错误信息的确定性顺序）
        results = await asyncio.gather(*[_run_batch(i, b) for i, b in enumerate(batches)])
        relations: List[ExtractedRelation] = []
        errors: List[str] = []
        for b_relations, b_errors in results:
            relations.extend(b_relations)
            errors.extend(b_errors)
        return relations, errors
