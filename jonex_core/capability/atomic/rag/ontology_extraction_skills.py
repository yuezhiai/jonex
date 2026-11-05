#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
TBox 构建技能库 + 技能链执行器（P7，skill-action-ontology-enhancement-plan §2.5）。

entity_types[] 条目的 extraction_skills 声明抽取该类型时按序调用的技能，
分 refine（完善）/ constrain（约束）两类；未声明时走默认链（= extractor
现有硬编码后处理路径，P7 阶段行为逐字段一致，锁单测；硬编码→默认链技能
的迁移在灰度验证成功后按 §2.5「切换后的老实现清理」执行）。

调度语义（§2.5）：
- 按声明顺序串行调用，refine 先于 constrain；
- 技能输入 = 候选实体/关系 + 当前 chunk 原文 + 类型 TBox 定义；
  输出 = 完善后的候选，或 reject（丢弃单实体/挂接 + 日志，不整批失败）；
- 技能引用 `builtin:<name>`（内置，在本模块执行）/
  `catalog:<tool_name>`（调用登记的只读工具，本模块不执行，由 knowledge-base 写图前执行，
  格式见 jonex_core/common/catalog_skill_spec.py）；
- 挂接关系口径：关系两端类型或端点名命中该类型实体的，视为该类型的挂接。

一期内置技能全部确定性实现（无 LLM 调用）；LLM 类补全由 procedural prompt
承担（contract_filler/semantic_definition 只做确定性规范化与兜底）。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from jonex_core.capability.atomic.rag.ontology_extractor import (
    ExtractedEntity,
    ExtractedRelation,
    _post_validate,
    merge_entity_attributes,
)

logger = __import__("logging").getLogger(__name__)

# 合法技能分组（声明顺序 = 执行顺序：refine 先于 constrain）
_SKILL_GROUPS = ("refine", "constrain")
# 技能名前缀：builtin 一期实现；catalog 二期 MCP（一期跳过）
_BUILTIN_PREFIX = "builtin:"
_CATALOG_PREFIX = "catalog:"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# ============================================================
# 数据模型
# ============================================================


@dataclass
class SkillCall:
    """一条技能声明（含技能名与声明描述）。"""

    skill: str
    description: str = ""


@dataclass
class SkillReject:
    """技能拒绝记录（丢弃单实体/挂接，不整批失败，D12）。"""

    kind: str   # entity | relation
    name: str
    reason: str
    skill: str


@dataclass
class SkillContext:
    """单类型技能链上下文：该类型候选 + 挂接关系 + chunk 原文 + TBox。"""

    entity_type: str
    entities: List[ExtractedEntity]
    relations: List[ExtractedRelation]
    chunk_texts: List[str]
    tbox_entity: Dict[str, Any] = field(default_factory=dict)
    # chunk_id → 正文。提供时，原文比对限定在实体/关系自己 source_chunks 指向的 chunk；
    # 未提供或对不上时回落 chunk_texts（整篇）
    chunk_text_by_id: Dict[str, str] = field(default_factory=dict)
    schema: Dict[str, Any] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    rejected: List[SkillReject] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)


@dataclass
class SkillOutcome:
    """技能链执行结果。"""

    entities: List[ExtractedEntity] = field(default_factory=list)
    relations: List[ExtractedRelation] = field(default_factory=list)
    rejected: List[SkillReject] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)


# ============================================================
# 声明解析与校验
# ============================================================


def parse_extraction_skills(
    raw: Any,
) -> Tuple[List[SkillCall], List[SkillCall], List[str]]:
    """解析 extraction_skills 声明 → (refine 链, constrain 链, 错误列表)。

    校验规则（§2.5 + kb-service DTO 口径）：
    - 必须是对象；只允许 refine/constrain 分组；
    - 每项必须含 skill 字段；技能名必须以 builtin: / catalog: 为前缀；
    - builtin: 名必须在内置注册表内（未知报错）；
    - catalog: 一期无执行器 → 跳过 + warning（不报错）。
    """
    refine_calls: List[SkillCall] = []
    constrain_calls: List[SkillCall] = []
    errors: List[str] = []
    if raw is None:
        return refine_calls, constrain_calls, errors
    if not isinstance(raw, dict):
        return refine_calls, constrain_calls, ["extraction_skills 必须是对象"]
    for group in raw:
        if group not in _SKILL_GROUPS:
            errors.append(f"extraction_skills 存在未知分组: {group}")
            continue
        items = raw[group]
        if not isinstance(items, list):
            errors.append(f"extraction_skills.{group} 必须是列表")
            continue
        calls = refine_calls if group == "refine" else constrain_calls
        for item in items:
            if not isinstance(item, dict) or not item.get("skill"):
                errors.append(f"extraction_skills.{group} 每项必须含 skill 字段")
                continue
            name = str(item["skill"]).strip()
            if name.startswith(_CATALOG_PREFIX):
                # 调用登记工具的技能由 knowledge-base 在写图前执行（工具定义、凭据、出站校验与
                # 审计都在那边，见 capabilities/knowledge_base/services/extract_tool_service.py）；
                # 抽取器不执行，声明原样留在 schema 里
                logger.debug("catalog 技能交 knowledge-base 写图前执行，抽取期跳过: %s", name)
                continue
            if not name.startswith(_BUILTIN_PREFIX):
                errors.append(
                    f"extraction_skills 技能名必须以 builtin: 或 catalog: 为前缀: {name}"
                )
                continue
            if name[len(_BUILTIN_PREFIX):] not in _BUILTIN_SKILLS:
                errors.append(f"extraction_skills 未知内置技能: {name}")
                continue
            calls.append(SkillCall(skill=name, description=str(item.get("description") or "")))
    return refine_calls, constrain_calls, errors


# ============================================================
# 内置技能（确定性实现，无 LLM 调用）
# ============================================================

# 敬语/口头词（动作名归一剥离）
_COURTESY_PREFIXES = re.compile(
    r"^\s*(?:请|烦请|敬请|劳烦|请先|请您|请勿|无需)\s*",
)
_SEQ_PREFIX = re.compile(
    r"^\s*(?:\d+[.、)）]\s*|(?:第|步骤|step\s*)\s*\d+\s*[步.、)）:]?\s*)",
    re.IGNORECASE,
)
_TRAILING_PUNCT = re.compile(r"\s*[。.!！?？,，;；:：]+$")
# 契约字段值必须出现在 chunk 正文（值或其主体子串）才不算编造
_QUOTE_STRIP = re.compile(r'[「」“”"\'《》]')
# [jonex] P9：归一化比对用——去掉空白与引号类标点后逐字匹配。
# 原文「请点击「挂失冻结」按钮」与端点「挂失冻结按钮」、原文「近 6 个月
# 无逾期记录」与端点「近6个月无逾期记录」，仅标点/空白差异，语义上
# 端点确实出现在原文，不应被 endpoint_verifier 误杀（e2e 暴露）。
_NORM_STRIP = re.compile(r'[\s「」“”"\'《》〈〉]+')
# 拆主体用（「过半主体命中」判定的分界）
_SPLIT_RE = re.compile(r"[，。；、,;.\s]+")


def _prepare_needle(value: str) -> Optional[Tuple[str, str, List[str]]]:
    """[jonex] P2-b：预计算一个 value 的三级匹配因子，返回 ``(v, v_norm, parts)``。

    逐字层与归一化层都要对 value 跑正则、拆主体还要再 split 一次——
    在 ``_backfill_source_chunks`` 的 O(实体×chunk) 内层里，这些因子
    对同一个 value 会被重复计算 M（chunk 数）次。提到这里，让调用方
    对每个 value 只算一次。

    去引号后为空的返回 None（调用方据此跳过该 value）。
    """
    v = _QUOTE_STRIP.sub("", value or "").strip()
    if not v:
        return None
    parts = [p for p in _SPLIT_RE.split(v) if len(p) >= 2]
    return v, _NORM_STRIP.sub("", v), parts


def _mentions_prepared(
    hay: str, hay_norm: str, needle: Tuple[str, str, List[str]],
) -> bool:
    """[jonex] P2-b：``_chunk_mentions`` 的预计算版。

    ``hay`` / ``hay_norm`` / ``needle`` 全部由调用方缓存，本函数只做纯
    子串查找。三级判定口径与 ``_chunk_mentions`` 逐字一致（逐字 →
    归一化 → 拆主体过半），两者必须同步修改。
    """
    v, v_norm, parts = needle
    if v in hay:
        return True
    # 归一化比对：仅标点/空白差异视为原文支撑（e2e：近 6 个月 vs 近6个月）
    if v_norm and v_norm in hay_norm:
        return True
    # 按标点/空白拆主体，过半主体命中即视为原文支撑
    if not parts:
        return False
    return sum(1 for p in parts if p in hay) * 2 >= len(parts)


def _chunk_mentions(texts: List[str], value: str) -> bool:
    """value 或其核心子串是否出现在任一 chunk 正文（摘句口径）。

    [jonex] P9：标点/空白差异不作为「不在原文」依据——先逐字比对，
    再归一化（去空白与引号类标点）比对，最后按标点/空白拆主体
    过半命中判定。

    单次调用口径；批量场景（O(实体×chunk)）请改用
    ``_prepare_needle`` + ``_mentions_prepared`` 预计算。
    """
    needle = _prepare_needle(value)
    if needle is None:
        return False
    hay = "\n".join(texts)
    return _mentions_prepared(hay, _NORM_STRIP.sub("", hay), needle)


def _scoped_texts(ctx: SkillContext, source_chunks: List[Dict[str, Any]]) -> List[str]:
    """按 source_chunks 取比对用原文；取不到（无映射 / 无出处 / id 对不上）回落整篇。

    回落而不是返回空：空列表会让所有校验判「不在原文」，把没有出处信息的实体整批误杀。
    """
    if ctx.chunk_text_by_id and source_chunks:
        ids = list(dict.fromkeys(
            str(sc.get("source_id") or "") for sc in source_chunks
        ))
        texts = [ctx.chunk_text_by_id[i] for i in ids if i and i in ctx.chunk_text_by_id]
        if texts:
            return texts
    return ctx.chunk_texts


def _texts_for_entity(ctx: SkillContext, e: ExtractedEntity) -> List[str]:
    return _scoped_texts(ctx, e.source_chunks or [])


def _texts_for_relation(ctx: SkillContext, r: ExtractedRelation) -> List[str]:
    """关系优先用自己的出处；没有出处时用源端实体的出处（挂接关系与源实体同 chunk 抽出）。"""
    if r.source_chunks:
        return _scoped_texts(ctx, r.source_chunks)
    src = next((e for e in ctx.entities if e.canonical_name == r.source_name), None)
    if src is not None:
        return _texts_for_entity(ctx, src)
    return ctx.chunk_texts


def _skill_name_normalizer(ctx: SkillContext) -> None:
    """动作名「动词+宾语」归一：去敬语/序号前缀/句末标点；超长截断进 description。"""
    maxlen = int(ctx.params.get("name_maxlen")
                 or _env_int("ONTOLOGY_PROCEDURAL_NAME_MAXLEN", 30))
    for e in list(ctx.entities):
        name = e.canonical_name
        norm = _COURTESY_PREFIXES.sub("", name)
        norm = _SEQ_PREFIX.sub("", norm)
        norm = _TRAILING_PUNCT.sub("", norm).strip()
        if not norm:
            ctx.rejected.append(SkillReject(
                "entity", name, "归一后名称为空", "builtin:name_normalizer"))
            ctx.entities.remove(e)
            continue
        if len(norm) > maxlen:
            full = norm
            norm = norm[:maxlen]
            # 超长截断：全文进 description（保留检索召回）
            if full not in (e.description or ""):
                e.description = f"{full}（{e.description}）" if e.description else full
        if norm != name:
            if name not in e.aliases:
                e.aliases.append(name)
            e.canonical_name = norm
            # 挂接关系端点同步改名（防 endpoint_verifier/schema_validator 误杀）
            for r in ctx.relations:
                if r.source_name == name:
                    r.source_name = norm
                if r.target_name == name:
                    r.target_name = norm
            ctx.logs.append(f"name_normalizer: {name} → {norm}")


def _skill_endpoint_verifier(ctx: SkillContext) -> None:
    """挂接端点必须存在于 chunk 正文，否则丢弃该挂接（防 stub 节点）。"""
    cand_names = {e.canonical_name for e in ctx.entities}
    for r in list(ctx.relations):
        texts = _texts_for_relation(ctx, r)
        for endpoint in (r.source_name, r.target_name):
            if endpoint in cand_names:
                continue
            if not _chunk_mentions(texts, endpoint):
                ctx.rejected.append(SkillReject(
                    "relation", f"{r.source_name}→{r.target_name}",
                    f"端点 {endpoint} 不在 chunk 正文", "builtin:endpoint_verifier",
                ))
                ctx.relations.remove(r)
                break


# [jonex] P10 批 2.5：生成式字段——definition 由 semantic_definition 技能
# 兜底摘句，值可来自 LLM 归纳而非逐字摘句，不参与 no_fabrication 逐字校验。
_NO_FABRICATION_GENERATIVE = {"definition"}


def _no_fabrication_fields(tbox_entity: Dict[str, Any]) -> List[str]:
    """no_fabrication 校验字段范围（P10 批 2.5 泛化）。

    - Action 校验 authorization / failure_policy / preconditions / atomic_effects /
      follow_ups 五个审查与执行边界会直接消费的契约字段；parameters 等纯归纳字段
      逐字校验误杀率高，不纳入。比对走三级口径（逐字 → 去标点空白 → 主体过半命中），
      容忍归纳式改写；audit_required 是布尔值，不走摘句，由 ``_clear_unstated_audit``
      按段落措辞判断；
    - 其余类型校验 TBox 声明的全部 string/text 属性（声明即应摘句），
      排除生成式字段 definition；number/boolean/enum 非文本，摘句口径
      不适用。
    """
    if (tbox_entity or {}).get("name") == "Action":
        # preconditions 是审查维度之一（「执行前必须核验什么」），编造的前置条件会直接
        # 进审查结论，故与 authorization / failure_policy 一样要求原文支撑；
        # atomic_effects / follow_ups 决定执行侧「什么要回滚、什么只联动」，编造同样会落错
        return ["authorization", "failure_policy", "preconditions", "atomic_effects", "follow_ups"]
    fields = []
    for attr in tbox_entity.get("attributes") or []:
        name = str(attr.get("name", "")).strip()
        if not name or name in _NO_FABRICATION_GENERATIVE:
            continue
        if str(attr.get("type", "string")) not in ("string", "text"):
            continue
        fields.append(name)
    return fields


def _skill_no_fabrication(ctx: SkillContext) -> None:
    """属性禁编造：校验字段的取值必须可在 chunk 原文摘句（宁缺毋假）。

    [jonex] P10 批 2.5 泛化：旧实现硬编码 authorization/failure_policy 两
    字段——Organization/Product 等类型的属性编造（行业、参数量等无原文
    支撑也落 attributes）完全漏网。现按 TBox 声明取校验字段（见
    _no_fabrication_fields）；未声明属性的类型无字段可校验，直接跳过。
    """
    fields = _no_fabrication_fields(ctx.tbox_entity)
    if not fields:
        return
    for e in ctx.entities:
        attrs = dict(e.attributes or {})
        texts = _texts_for_entity(ctx, e)
        for field in fields:
            value = attrs.get(field)
            if not value:
                continue
            if isinstance(value, str):
                ok = _chunk_mentions(texts, value)
            elif isinstance(value, list):
                ok = bool(value) and all(
                    _chunk_mentions(texts, str(v)) for v in value
                )
            else:
                ok = False
            if not ok:
                ctx.rejected.append(SkillReject(
                    "entity", e.canonical_name,
                    f"{field} 无原文支撑，置空（宁缺毋假）", "builtin:no_fabrication",
                ))
                ctx.logs.append(
                    f"no_fabrication: {e.canonical_name}.{field} 置空: {str(value)[:80]}"
                )
                attrs[field] = "" if isinstance(value, str) else []
        e.attributes = attrs
    # 逐字比对之外的语义兜底（只对 Action 生效）：执行人被误填成权限要求、
    # 段落没提审计却给了 audit_required
    _clear_unstated_authorization(ctx)
    _clear_unstated_audit(ctx)


# 权限类措辞：原文出现这些才算「写明了权限要求」。刻意不收裸「审批」——
# 「止付审批通过后方可办理」是前置条件，不是本动作的权限要求。
_AUTHORIZATION_MARKERS = re.compile(
    r"权限|可执行|须经|需经|经[^。；\n]{0,12}审批|授权|仅限|不得|级及以上|以上人员|有权"
)


def _action_segment(ctx: SkillContext, e: ExtractedEntity) -> Optional[str]:
    """本动作「自己的」原文段落：优先「第N步：<动作名>」开头的段落，否则第一个提到动作名的段落。

    按换行切段。找不到（动作名经归一后与原文对不上）返回 None，由调用方按「无法判断」处理。
    """
    names = sorted({n for n in [e.canonical_name, *(e.aliases or [])] if n}, key=len, reverse=True)
    if not names:
        return None
    paragraphs = [p for t in _texts_for_entity(ctx, e) for p in str(t).splitlines() if p.strip()]
    step_title = re.compile(
        r"第\s*[一二三四五六七八九十百\d]+\s*步\s*[：:]?\s*(?:" + "|".join(re.escape(n) for n in names) + ")"
    )
    for p in paragraphs:
        if step_title.search(p):
            return p
    return next((p for p in paragraphs if any(n in p for n in names)), None)


def _clear_unstated_authorization(ctx: SkillContext) -> None:
    """D15：本动作自己的原文段落里没有写权限要求时，清空 authorization。

    LLM 常把「由谁来做」误填成「谁有权做」（试点 e2e：5/8 个动作的 authorization 只是
    执行人）。逐字比对拦不住——执行人的名字确实出现在原文；也不能靠「authorization
    等于 PERFORMED_BY 端点」判断，LLM 有时只填了 authorization、不输出执行人关系。
    判据：本动作段落（``_action_segment``）中没有任何权限类措辞（``_AUTHORIZATION_MARKERS``）。

    找不到本动作段落时不动（无法判断，宁可保留也不误清）；chunk 没有换行时整块是一段，
    只要出现过权限措辞也不动——两种情况都退化为不做这一步。
    口径：Skill 级的权限声明（写在章节开头）不下放到各步骤，未在步骤段落内重申 = 未声明。
    清空 = 「权限未声明」，下游按 D15 视为未知（执行侧拒绝或转人工，审查侧 unknown）。
    """
    for e in ctx.entities:
        if e.entity_type != "Action":
            continue
        attrs = dict(e.attributes or {})
        auth = attrs.get("authorization")
        values = [auth] if isinstance(auth, str) else list(auth or [])
        if not any(str(v).strip() for v in values):
            continue
        segment = _action_segment(ctx, e)
        if segment is None or _AUTHORIZATION_MARKERS.search(segment):
            continue
        ctx.rejected.append(SkillReject(
            "entity", e.canonical_name,
            "authorization 在本动作段落中无权限措辞（执行人≠权限），置空（D15）",
            "builtin:no_fabrication",
        ))
        ctx.logs.append(
            f"no_fabrication: {e.canonical_name}.authorization 置空（段落未写权限）: {auth}"
        )
        attrs["authorization"] = "" if isinstance(auth, str) else []
        e.attributes = attrs


# 审计类措辞：原文出现这些才算「说到了审计留痕」（含「无需留痕」这类否定说法，
# 否定同样是写明了，audit_required=false 也要有它撑着）。
# 刻意不收裸「记录」：「生成挂失记录」「争议记录保留」说的是业务数据，不是审计要求，
# 收进来会让 LLM 凭常识补的 audit_required=true 借这类句子过关。
_AUDIT_MARKERS = re.compile(r"审计|留痕|日志|存档|归档|备案")


def _clear_unstated_audit(ctx: SkillContext) -> None:
    """本动作段落里没说审计留痕时，去掉 audit_required（回到「未提及」）。

    audit_required 是三态：true / false / 缺键。LLM 容易按常识补一个 true（「挂失当然要留痕」）
    或 false（没看到就填否），两种都是编造。布尔值没法逐字摘句，改按段落措辞判断：
    本动作段落（``_action_segment``）没有审计类措辞（``_AUDIT_MARKERS``）就删键。
    找不到本动作段落时不动（无法判断，宁可保留也不误删）。
    """
    for e in ctx.entities:
        if e.entity_type != "Action":
            continue
        attrs = dict(e.attributes or {})
        if "audit_required" not in attrs:
            continue
        segment = _action_segment(ctx, e)
        if segment is None or _AUDIT_MARKERS.search(segment):
            continue
        ctx.rejected.append(SkillReject(
            "entity", e.canonical_name,
            "audit_required 在本动作段落中无审计措辞，置为未提及",
            "builtin:no_fabrication",
        ))
        ctx.logs.append(
            f"no_fabrication: {e.canonical_name}.audit_required 去掉（段落未提审计）: "
            f"{attrs['audit_required']}"
        )
        attrs.pop("audit_required")
        e.attributes = attrs


def _skill_synonym_merger(ctx: SkillContext) -> None:
    """KB 级同义词归一：声明技能，实际执行在 kb-service 写图前 reconcile 的
    _apply_synonyms（跨服务不通，抽取期 no-op，§5 P7 要点）。"""
    ctx.logs.append("synonym_merger: 抽取期 no-op（执行在 kb-service 写图前）")


def _skill_dedup_merger(ctx: SkillContext) -> None:
    """同名合并（大小写不敏感）：描述取长、aliases 并集、source_chunks 聚合。"""
    merged: Dict[str, ExtractedEntity] = {}
    for e in ctx.entities:
        key = e.canonical_name.casefold()
        if key not in merged:
            merged[key] = ExtractedEntity(
                canonical_name=e.canonical_name, entity_type=e.entity_type,
                aliases=list(e.aliases), attributes=dict(e.attributes or {}),
                description=e.description, confidence=e.confidence,
                source_chunks=[dict(sc) for sc in e.source_chunks],
                extraction_method=e.extraction_method,
            )
            continue
        m = merged[key]
        if len(e.description) > len(m.description):
            m.description = e.description
        if e.confidence > m.confidence:
            m.confidence = e.confidence
        # 与 NativeOntologyExtractor._merge_entities 同一合并口径（Action/Skill 以 procedural 为准）
        merge_entity_attributes(m, e)
        for alias in e.aliases:
            if alias and alias not in m.aliases:
                m.aliases.append(alias)
        seen = {(sc.get("source_id"), sc.get("file_path")) for sc in m.source_chunks}
        for sc in e.source_chunks:
            key2 = (sc.get("source_id"), sc.get("file_path"))
            if key2 not in seen:
                seen.add(key2)
                m.source_chunks.append(dict(sc))
    ctx.entities = list(merged.values())


def _skill_schema_validator(ctx: SkillContext) -> None:
    """_post_validate：端点集合校验 + inverse 归一翻转（schema 外丢弃）。"""
    kept_e, kept_r = _post_validate(ctx.entities, ctx.relations, ctx.schema)
    for e in ctx.entities:
        if e not in kept_e:
            ctx.rejected.append(SkillReject(
                "entity", e.canonical_name, "schema 外类型",
                "builtin:schema_validator",
            ))
    for r in ctx.relations:
        if r not in kept_r:
            ctx.rejected.append(SkillReject(
                "relation", f"{r.source_name}→{r.target_name}", "schema 外/端点越界",
                "builtin:schema_validator",
            ))
    ctx.entities = kept_e
    ctx.relations = kept_r


def _skill_step_serializer(ctx: SkillContext) -> None:
    """无序清单编 step_no、补 NEXT_STEP：同 Skill 的 Action 按 HAS_STEP 关系
    出现顺序编号（缺失时），连续 step_no 间补 NEXT_STEP 关系。"""
    # 同 skill 分组：HAS_STEP 关系 (Skill→Action)
    groups: Dict[str, List[ExtractedEntity]] = {}
    for r in ctx.relations:
        if r.relation_type != "HAS_STEP":
            continue
        target = next(
            (e for e in ctx.entities if e.canonical_name == r.target_name), None,
        )
        if target is not None:
            groups.setdefault(r.source_name, []).append(target)
    next_rel_exists = {
        (r.source_name, r.target_name)
        for r in ctx.relations if r.relation_type == "NEXT_STEP"
    }
    for skill_name, actions in groups.items():
        # 现有序号
        nums: List[int] = []
        for a in actions:
            try:
                nums.append(int((a.attributes or {}).get("step_no", 0)))
            except (ValueError, TypeError):
                nums.append(0)
        # 缺失序号按出现顺序补齐（从现有最大值+1 或 1 开始）
        missing = [i for i, n in enumerate(nums) if n <= 0]
        if missing:
            nxt = max(nums, default=0) + 1
            for i in missing:
                nums[i] = nxt
                nxt += 1
                attrs = dict(actions[i].attributes or {})
                attrs["step_no"] = nums[i]
                actions[i].attributes = attrs
        # HAS_STEP 关系属性补权威 step_no（D3）
        for r in ctx.relations:
            if r.relation_type != "HAS_STEP":
                continue
            tgt = next(
                (a for a in actions if a.canonical_name == r.target_name), None,
            )
            if tgt is not None:
                r.attributes = {**(r.attributes or {}),
                                "step_no": (tgt.attributes or {}).get("step_no")}
        # 连续 step_no 间补 NEXT_STEP
        ordered = sorted(zip(nums, actions), key=lambda t: t[0])
        for (n1, a1), (n2, a2) in zip(ordered, ordered[1:]):
            if n1 <= 0 or n2 <= 0 or n2 != n1 + 1:
                continue
            if (a1.canonical_name, a2.canonical_name) in next_rel_exists:
                continue
            ctx.relations.append(ExtractedRelation(
                source_name=a1.canonical_name, source_type="Action",
                target_name=a2.canonical_name, target_type="Action",
                relation_type="NEXT_STEP", confidence=0.9,
                attributes={"step_no": n2},
                source_chunks=[dict(sc) for sc in (a2.source_chunks or [])[:1]],
            ))
            ctx.logs.append(
                f"step_serializer: 补 NEXT_STEP {a1.canonical_name}→{a2.canonical_name}"
            )


def _skill_contract_filler(ctx: SkillContext) -> None:
    """契约字段规范化：字符串→列表、剥离空项（真实填充由 procedural prompt
    承担，禁编造由 no_fabrication 约束技能保证）。"""
    for e in ctx.entities:
        attrs = dict(e.attributes or {})
        for field in ("authorization", "preconditions",
                      "atomic_effects", "follow_ups"):
            # 字段缺失时不写键：图上「没抽到」必须与「抽到了但为空」可区分
            # （D15：authorization 缺失 = 未知，不能被一个空数组伪装成「已声明无约束」）
            if field not in attrs:
                continue
            value = attrs[field]
            if isinstance(value, str):
                items = [line.strip() for line in value.splitlines() if line.strip()]
            elif isinstance(value, list):
                items = [str(v).strip() for v in value if str(v).strip()]
            else:
                items = []
            if items:
                attrs[field] = items
            else:
                attrs.pop(field)
        e.attributes = attrs


def _skill_semantic_definition(ctx: SkillContext) -> None:
    """definition 兜底：LLM 未产出时从 chunk 找「{name}（是指/即/用于）…」句式；
    找不到留空（真实生成由 procedural prompt 承担，宁缺毋假）。"""
    for e in ctx.entities:
        attrs = dict(e.attributes or {})
        if str(attrs.get("definition") or "").strip():
            continue
        # 每个实体只按自己的名字（及别名）匹配：共用一个 alternation 会让所有实体
        # 拿到全文第一处匹配，把别人的定义错配给自己
        names = sorted(
            {n for n in [e.canonical_name, *(e.aliases or [])] if n},
            key=len, reverse=True,
        )
        if not names:
            continue
        alternation = "|".join(re.escape(n) for n in names)
        pattern = re.compile(
            rf"(?:所谓)?(?:{alternation})(?:，|,)?\s*(?:是指|即|用于|指)\s*([^\n。；]{{4,}})"
        )
        m = pattern.search("\n".join(_texts_for_entity(ctx, e)))
        if not m:
            continue
        attrs["definition"] = m.group(1).strip()
        e.attributes = attrs
        ctx.logs.append(
            f"semantic_definition: {e.canonical_name} 兜底摘句 "
            f"{m.group(1)[:40]}..."
        )


# 内置技能注册表（builtin:<name> → 技能函数）
_BUILTIN_SKILLS: Dict[str, Callable[[SkillContext], None]] = {
    # refine 类
    "step_serializer": _skill_step_serializer,
    "contract_filler": _skill_contract_filler,
    "semantic_definition": _skill_semantic_definition,
    # constrain 类
    "name_normalizer": _skill_name_normalizer,
    "endpoint_verifier": _skill_endpoint_verifier,
    "no_fabrication": _skill_no_fabrication,
    "synonym_merger": _skill_synonym_merger,
    "dedup_merger": _skill_dedup_merger,
    "schema_validator": _skill_schema_validator,
}


# ============================================================
# 技能链执行器
# ============================================================


def collect_attached_relations(
    entity_type: str,
    entities: List[ExtractedEntity],
    relations: List[ExtractedRelation],
) -> List[ExtractedRelation]:
    """收集该类型的挂接关系：两端类型命中，或端点名命中该类型实体名。"""
    names = {e.canonical_name for e in entities}
    out = []
    for r in relations:
        if r.source_type == entity_type or r.target_type == entity_type:
            out.append(r)
        elif r.source_name in names or r.target_name in names:
            out.append(r)
    return out


async def apply_declared_chain(
    ctx: SkillContext,
    refine_calls: List[SkillCall],
    constrain_calls: List[SkillCall],
) -> SkillOutcome:
    """按声明顺序串行执行技能链（refine 先于 constrain）。

    技能抛异常 → 该技能失败记日志跳过（管线容错），不整批失败；
    reject 只丢弃单实体/挂接（D12），由技能自行写入 ctx.rejected。
    """
    for call in refine_calls + constrain_calls:
        fn = _BUILTIN_SKILLS[call.skill[len(_BUILTIN_PREFIX):]]
        # [jonex] P9（批 5）技能链执行日志：refined=执行后实体数（该技能可见
        # 规模）、rejected=本次新增拒绝数、elapsed_ms=耗时。reject 率异常高
        # 说明该约束技能与语料风格不匹配（技能链可调，§2.5 可追踪性）。
        before_rej = len(ctx.rejected)
        before_ent = len(ctx.entities)
        t0 = time.perf_counter()
        try:
            fn(ctx)
        except Exception as exc:  # noqa: BLE001 — 技能级容错
            logger.warning("技能 %s 执行失败: %s", call.skill, exc)
            ctx.logs.append(f"{call.skill} 执行失败: {exc}")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "技能链 %s: skill=%s refined=%d rejected=%d elapsed_ms=%.1f",
            ctx.entity_type, call.skill,
            len(ctx.entities) - before_ent, len(ctx.rejected) - before_rej,
            elapsed_ms,
        )
    return SkillOutcome(
        entities=ctx.entities,
        relations=ctx.relations,
        rejected=ctx.rejected,
        logs=ctx.logs,
    )


async def run_entity_skill_chains(
    entities: List[ExtractedEntity],
    relations: List[ExtractedRelation],
    schema: Dict[str, Any],
    chunk_texts: List[str],
    params: Optional[Dict[str, Any]] = None,
    chunk_text_by_id: Optional[Dict[str, str]] = None,
) -> Tuple[List[ExtractedEntity], List[ExtractedRelation], List[SkillReject], List[str]]:
    """对声明了 extraction_skills 的类型逐个跑技能链（P7 接线点）。

    ``chunk_text_by_id`` 提供时，原文比对限定在每个实体/关系自己 source_chunks 指向的
    chunk（见 ``_scoped_texts``）；不提供则沿用整篇 ``chunk_texts``。

    未声明类型走默认链 = extractor 现有硬编码后处理路径（本函数不动它们，
    行为逐字段一致）。声明链作用于该类型实体（对象原地修改/替换）与其挂接
    关系（增删改按对象身份 diff 同步回共享列表）。

    Returns:
        (entities, relations, rejected, logs) —— entities/relations 为更新后全集。
    """
    decl_map: Dict[str, Tuple[List[SkillCall], List[SkillCall]]] = {}
    errors: List[str] = []
    for et in schema.get("entity_types", []):
        raw = et.get("extraction_skills")
        if not raw:
            continue
        refine_calls, constrain_calls, errs = parse_extraction_skills(raw)
        errors.extend(errs)
        if refine_calls or constrain_calls:
            decl_map[et.get("name", "")] = (refine_calls, constrain_calls)
    if errors:
        logger.warning("extraction_skills 声明解析错误: %s", "; ".join(errors))
    if not decl_map:
        return entities, relations, [], []

    all_rejected: List[SkillReject] = []
    all_logs: List[str] = []
    for etype, (refine_calls, constrain_calls) in decl_map.items():
        type_entities = [e for e in entities if e.entity_type == etype]
        if not type_entities:
            continue
        attached = collect_attached_relations(etype, type_entities, relations)
        tbox_entity = next(
            (et for et in schema.get("entity_types", [])
             if et.get("name") == etype),
            {},
        )
        ctx = SkillContext(
            entity_type=etype,
            entities=type_entities,
            relations=list(attached),  # 拷贝：技能原地 remove 不影响 attached 快照
            chunk_texts=chunk_texts,
            tbox_entity=tbox_entity,
            schema=schema,
            params=params or {},
            chunk_text_by_id=dict(chunk_text_by_id or {}),
        )
        outcome = await apply_declared_chain(ctx, refine_calls, constrain_calls)

        # 实体：整类替换（对象可能被改名/合并/拒绝）
        entities = [e for e in entities if e.entity_type != etype] + outcome.entities

        # 挂接关系：按对象身份 diff 同步
        kept_ids = {id(r) for r in outcome.relations}
        attached_ids = {id(r) for r in attached}
        relations = [r for r in relations if id(r) not in (attached_ids - kept_ids)]
        relations.extend(
            r for r in outcome.relations if id(r) not in attached_ids
        )
        all_rejected.extend(outcome.rejected)
        all_logs.extend(outcome.logs)
    return entities, relations, all_rejected, all_logs
