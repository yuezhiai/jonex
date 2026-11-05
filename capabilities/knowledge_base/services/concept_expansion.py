#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""抽象 schema 的形状（v2 实体/关系 + v1 概念兼容）：形状归一、LLM 输出消毒、查询词匹配、候选展开。

**纯函数，不碰 DB、不碰 LLM、不碰 Neo4j。** 两个理由：
① 检索侧在热路径上用它（`query_with_ontology`），那里每加一次 IO 都是全局代价；
② 这里的规则是「什么算命中」这种最该被测试钉住的语义，脱开 DB 才测得干净。

五件事：

- **归一**（`normalize_concepts`）：把 LLM 或界面来的 concepts 收敛成落库形状。
- **消毒**（`filter_dangling_sources`）：模型只许引用输入里出现过的
  `(data_source_id, entity_name)` 组合，编出来的一律剔除。
- **匹配**（`match_entities`）：查询词 → 命中哪些抽象实体。决策 3：**别名/名称/描述
  的确定性文本匹配**，不走向量、不写 `ontology_synonyms`。
- **展开**（`expand_candidates`）：命中的实体 → N 条带 `data_source_id` 的候选。
  `data_source_id` 是「分源列举 + 标注来源」唯一的信息来源，丢了就没法分源。
- **反向标注**（`annotate_instances`）：命中实例反向标注它属于哪个实体。

**管线顺序是「先消毒、后归一」**：
`normalize_concepts(filter_dangling_sources(raw, allowed))`。所以
`filter_dangling_sources` 必须扛得住**原始、未归一**的输入 —— 它只管源映射，
落库形状（键的增删、长度上限）由 `normalize_concepts` 负责收敛。
"""

from __future__ import annotations

import os

from .metrics.executable import ADDITIVITY_VALUES, normalize_executable

MAX_CONCEPTS = 500
MAX_ALIASES_PER_CONCEPT = 50
MAX_TERM_LENGTH = 128

#: 属性落点语义槽（`filter` / `aggregation` / `grain`）的单槽长度上限。
#: 是「自由文本提示」不是 SQL 片段仓库——超长一律截断，不拒绝（归一化承诺宽容）。
MAX_LANDING_SLOT_CHARS = 240

#: 子串匹配的最短长度。短的别名一律不做子串匹配 —— 「不良」两个字还好，
#: 单个字的别名会把整个查询都吞掉（"缺陷率" 会被别名 "率" 命中）。
MIN_SUBSTRING_LEN = 2

# ── v2 词表（spec: 2026-10-03-concept-matching-v2 §3.2）─────────────────────────

#: 派生核心词时剥离的白名单后缀。**由长到短**排列——「申请值」必须先于「值」匹配，
#: 否则「内存申请值」会剥成「内存申请」。
_CORE_SUFFIXES: tuple[str, ...] = (
    "申请数量", "申请值", "使用值", "数量", "名称", "编码", "大小", "容量", "标志", "单位", "值",
)

#: 派生结果命中这些通用词一律丢弃：它们出现在几十上百个属性里，命中 = 纯噪声。
_CORE_STOPWORDS: frozenset[str] = frozenset({
    "创建", "更新", "删除", "内容", "主键", "外键", "是否", "序号", "类型", "状态",
    "时间", "描述", "备注",
})

#: 命中档位排序（spec §五：exact > alias > attribute > core > description）。
#: ⚠️ `match_entities` 用 `order[match_kind]` 直接索引——新增档位漏改这张表即 KeyError 硬崩。
_MATCH_KIND_ORDER: dict[str, int] = {
    "exact": 0, "alias": 1, "attribute": 2, "core": 3, "description": 4,
}

#: 指标匹配的档位序。**独立于 `_MATCH_KIND_ORDER`** —— 后者被 `_match_entities_v1`
#: 直接索引（新增档位漏改即 KeyError），指标档位面自持一张表、只服务 `match_metrics`。
#: `aggregate` 是聚合词兜底档（T3），词面三档永远排在它前面。
_METRIC_KIND_ORDER = {"exact": 0, "alias": 1, "description": 2, "aggregate": 3}

#: v2 词表/打分开关（spec §七，默认开）。黑名单式「关」：白名单式会把运维最常写的
#: `=on` 判成关，一次「照隔壁抄」的配置就能静默停掉整层改动且无处报错。
ONTOLOGY_CONCEPT_TERMS_V2_ENABLED = (
    os.getenv("ONTOLOGY_CONCEPT_TERMS_V2_ENABLED", "true").lower()
    not in ("0", "false", "no", "off")
)

#: 分级权重（spec §3.3）：L1 名/别名 3；L2 源实体名 2；L3 属性 display_name 2；L4 核心词 1。
_LEVEL_WEIGHTS: dict[str, int] = {"l1": 3, "l2": 2, "l3": 2, "l4": 1}
_LONG_TERM_LEN = 4          # 命中词 ≥4 字 → 长词加成 1（一次，不按词数累计）
MAX_CANDIDATES_PER_KB = 8   # 每库候选封顶（spec §3.3）


def _derive_core_terms(display_name: str) -> list[str]:
    """属性 display_name → 核心词（0 或 1 条）。规则见 spec §3.2。

    剥白名单后缀（由长到短）→ 剩余 ≥2 字且不在通用停用表 → 产出；否则不派生。
    「部署位置」「创建时间」这类后缀不在白名单的**整词不派生**（宁可漏，不造弱词）。
    """
    d = _clean_str(display_name)
    if not d:
        return []
    for suf in _CORE_SUFFIXES:
        if d.endswith(suf) and len(d) > len(suf):
            head = d[: -len(suf)].strip()
            if len(head) >= 2 and head not in _CORE_STOPWORDS:
                return [head]
            return []          # 剥完剩 <2 字（「值」）或属停用词 → 不派生
    return []


def _entity_term_kinds_v2(e: dict) -> list[tuple[str, str]]:
    """v2 词表：`[(词条, 级别)]`，保序去重。L1 名字/别名、L2 源落点实体名、
    L3 属性 display_name、L4 派生核心词（spec §3.2）。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(term, level: str) -> None:
        t = _clean_str(term)
        if not t or t in seen:
            return
        seen.add(t)
        out.append((t, level))

    add(e.get("name"), "l1")
    for a in _as_list(e.get("aliases")):
        add(a, "l1")
    for s in _as_list(e.get("sources")):
        if isinstance(s, dict):
            add(s.get("entity_name"), "l2")
    for attr in _as_list(e.get("attributes")):
        if not isinstance(attr, dict):
            continue
        dn = _clean_str(attr.get("display_name"))
        add(dn, "l3")
        for core in _derive_core_terms(dn):
            add(core, "l4")
    return out


#: 筛选载荷里「属性级证据」的条数上限（spec §3.4 载荷有界：每库 ≤8 候选 × ≤8 证据）。
MAX_ATTR_EVIDENCE = 8


def _matched_attribute_evidence(e: dict, matches: list[dict], *, cap: int = MAX_ATTR_EVIDENCE) -> list[str]:
    """命中的**属性级证据**：哪些属性 display_name 是这次命中的来源。

    - l3（属性 display_name 直接命中）→ 该 display_name 本身；
    - l4（派生核心词命中）→ 派生它的 display_name（如「内存」←「内存申请值」）；
    - l1/l2/exact/description 不产出（不是属性级）。
    保序去重、封顶 cap。**用途**：筛选 LLM 的判定证据——没有它，「服务/内存」
    这类核心词命中会被模型当成弱信号（T9-S1 实证：CRC 因此被误筛）。
    """
    matched_terms = {m.get("term") for m in matches if isinstance(m, dict)}
    if not matched_terms:
        return []
    out: list[str] = []
    for attr in _as_list(e.get("attributes")):
        if not isinstance(attr, dict):
            continue
        dn = _clean_str(attr.get("display_name"))
        if not dn or dn in out:
            continue
        if dn in matched_terms or any(t in matched_terms for t in _derive_core_terms(dn)):
            out.append(dn)
            if len(out) >= cap:
                break
    return out


def _clean_str(value) -> str:
    """标量 → 去空白字符串；**容器一律当空**。

    `str(["不良", "defect"])` 得到 `"['不良', 'defect']"` —— 一个看起来像名字、
    实际永远匹配不上、界面上只是噪音的字符串。容器不是「值得宽容的怪 input」，
    是 LLM 主动产出的**形状错误**，交给调用方既有的「空 → 跳过」路径处理即可。
    `bytes` 一并挡掉（`str(b"x")` 是 `"b'x'"`，那也不是数据）。
    """
    if value is None or isinstance(
        value, (list, tuple, dict, set, frozenset, bytes, bytearray)
    ):
        return ""
    return str(value).strip()


def _as_list(values) -> list:
    """把「本该是 list」的字段收敛成 list，绝不抛。

    LLM 的形状错误在这里有三个坑：`aliases: "缺陷"` 直接迭代会按**字符**拆成
    「缺」「陷」两个永远匹配不上的别名；`sources: 42` 会 `TypeError` 把一次保存
    打成 500（而本模块承诺的是宽容，不是抛）；`sources: {...}` 会迭代出键名。
    裸字符串按**一个值**处理，其余不可迭代的一律当空。
    """
    if isinstance(values, (list, tuple, set, frozenset)):
        return list(values)
    if values is None or isinstance(values, (dict, bytes, bytearray)):
        return []
    if isinstance(values, str):
        return [values]
    if hasattr(values, "__iter__"):
        return list(values)  # dict_keys / 生成器 等其它可迭代
    return []


def _dedupe_strings(values) -> list[str]:
    """去重、去空白、丢超长项；顺序稳定（先到的留下）。"""
    out: list[str] = []
    seen: set[str] = set()
    for v in _as_list(values):
        s = _clean_str(v)
        if not s or len(s) > MAX_TERM_LENGTH or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _iter_source_pairs(raw_sources):
    """从 sources 里产出合法的 `(ds, entity)` 二元组，跳过畸形条目。"""
    for s in _as_list(raw_sources):
        if not isinstance(s, dict):
            continue
        ds = _clean_str(s.get("data_source_id"))
        ent = _clean_str(s.get("entity_name"))
        if ds and ent:
            yield ds, ent


def _unique_sources(raw_sources) -> list[dict]:
    """收敛成落库形状的源映射并去重：`{data_source_id, entity_name}` 两键，顺序稳定。

    只保留这两个键 —— 别的（`confidence` 之类）不是落库形状的一部分，
    留着会让「存进去的」和「归一后存进去的」长得不一样。
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for pair in _iter_source_pairs(raw_sources):
        if pair in seen:
            continue
        seen.add(pair)
        out.append({"data_source_id": pair[0], "entity_name": pair[1]})
    return out


def normalize_concepts(concepts) -> list[dict]:
    """把 concepts 收敛成落库形状 `{name, description, aliases, sources}`。

    - 概念按 `name` 去重：**同名条目合并**到先收编的那条 —— 源映射与别名并入
      （去重、顺序稳定），描述取先到的那个。合并而不是「先出现的胜出、后来的
      整条丢掉」：丢掉后面那条源映射，正是这个特性要防的事（「接了数据源却用不到
      它那份 schema」），而且丢得无声无息、界面上看不出来；
    - 源映射按 `(data_source_id, entity_name)` 去重，每一项只有这两个键；
    - **没有源映射的概念整条丢弃** —— 它在检索时展开不出任何候选；
    - 对垃圾输入宽容（非 dict / 缺字段 / None / 形状错乱一律跳过，绝不抛），
      因为上游既有 LLM 输出也有人手编辑的 JSONB：一个畸形条目不该让整次保存失败。

    注意：`MAX_TERM_LENGTH` 太长的 `name` 整条丢弃；`MAX_CONCEPTS` 收满即停止
    扫描，其后的条目（含同名合并）不再处理 —— 上限是为了给落库的 JSONB 定界。
    """
    out: list[dict] = []
    by_name: dict[str, dict] = {}
    for raw in _as_list(concepts):
        if not isinstance(raw, dict):
            continue
        name = _clean_str(raw.get("name"))
        if not name or len(name) > MAX_TERM_LENGTH:
            continue

        existing = by_name.get(name)
        if existing is None:
            sources = _unique_sources(raw.get("sources"))
            if not sources:
                continue
            entry = {
                "name": name,
                "description": _clean_str(raw.get("description")),
                "aliases": _dedupe_strings(raw.get("aliases"))[:MAX_ALIASES_PER_CONCEPT],
                "sources": sources,
            }
            by_name[name] = entry
            out.append(entry)
            if len(out) >= MAX_CONCEPTS:
                break
        else:
            existing["sources"] = _unique_sources(
                [*existing["sources"], *_as_list(raw.get("sources"))]
            )
            existing["aliases"] = _dedupe_strings(
                [*existing["aliases"], *_as_list(raw.get("aliases"))]
            )[:MAX_ALIASES_PER_CONCEPT]
    return out


def filter_dangling_sources(concepts, allowed_pairs) -> list[dict]:
    """剔掉模型编出来的源映射：只留 `allowed_pairs` 里真实出现过的 `(ds, entity)`。

    **可以对着原始（未归一）的 concepts 调用** —— 管线就是
    `normalize_concepts(filter_dangling_sources(raw, allowed))`。除了 `sources`
    之外的键原样带过去，落库形状由 `normalize_concepts` 收敛。

    留在 `sources` 里的是**洗干净的两键字典**
    `{"data_source_id": ..., "entity_name": ...}`：必须存「拿来比较的那个值」，
    留原始 dict（可能带空格、带 `confidence` 之类的额外键）等于「验的是 A、存的是
    B」，而带空格的 id 在检索侧按它 join 永远对不上 —— 正是这个函数要消灭的悬挂
    映射，只是换了个形状溜过检查。同一对 `(ds, entity)` 出现多次只留第一次。

    比较口径**两边统一**成 `_clean_str` 之后的字符串（`42` / `"42"` / `" 42 "`
    互相认同）：口径不一致时，`allowed_pairs` 里一个非字符串 id 会让**全部**映射
    落空、概念被整批清空 —— 检索随即什么也不展开，而且哪儿都不报错。

    返回**剔完之后仍然有源**的概念；全被剔掉的整条丢弃（否则界面会多出一个
    空壳概念，人还得手动删）。它只做消毒 **+ 源映射去重**，不管概念去重、
    别名截断、长度上限 —— 那些是 `normalize_concepts` 的事。
    """
    allowed: set[tuple[str, str]] = set()
    for pair in _as_list(allowed_pairs):
        if pair is None or isinstance(pair, (str, bytes, bytearray, dict)):
            continue
        try:
            ds_raw, ent_raw = pair
        except (TypeError, ValueError):
            continue
        ds, ent = _clean_str(ds_raw), _clean_str(ent_raw)
        if ds and ent:
            allowed.add((ds, ent))

    out: list[dict] = []
    for c in _as_list(concepts):
        if not isinstance(c, dict):
            continue
        kept = _unique_sources(
            s
            for s in _as_list(c.get("sources"))
            if isinstance(s, dict)
            and (
                _clean_str(s.get("data_source_id")),
                _clean_str(s.get("entity_name")),
            )
            in allowed
        )
        if not kept:
            continue
        out.append({**c, "sources": kept})
    return out


def _term_matches(query_cf: str, term_cf: str) -> bool:
    """全等，或（够长的）子串。

    子串要求 `MIN_SUBSTRING_LEN` 是有意的：中文单字别名（"率"、"量"）会把几乎
    任何查询都命中，展开出一堆不相干的候选 —— 那比不扩展更糟。
    """
    if not term_cf:
        return False
    if term_cf == query_cf:
        return True
    return len(term_cf) >= MIN_SUBSTRING_LEN and term_cf in query_cf


def _entity_terms(e: dict) -> list[str]:
    """一个实体的全部可匹配词：实体名、别名、**以及它各源落点里的实体名**（同旧版概念口径）。"""
    terms = [_clean_str(e.get("name"))]
    terms.extend(_clean_str(a) for a in _as_list(e.get("aliases")))
    terms.extend(
        _clean_str(s.get("entity_name"))
        for s in _as_list(e.get("sources")) if isinstance(s, dict)
    )
    return [t for t in terms if t]


def _match_entities_v1(query: str, entities) -> list[dict]:
    """查询词 → 命中的抽象实体（`{entity, match_kind, matched_term}`）。

    档位与排序沿用旧版概念匹配口径（exact → alias → description；同档按命中词长
    降序；描述只做「整段是查询子串」）。`included=false` 的落点**参与匹配**
    （它仍是对齐事实，命中后展开阶段会按 included 过滤）。
    """
    q = _clean_str(query)
    if not q:
        return []
    q_cf = q.casefold()

    hits: list[dict] = []
    for e in _as_list(entities):
        if not isinstance(e, dict):
            continue
        name = _clean_str(e.get("name"))
        if not name:
            continue
        matched: tuple[str, str] | None = None
        terms = _entity_terms(e)
        for t in terms:
            if t.casefold() == q_cf:
                matched = ("exact", t)
                break
        if matched is None:
            for t in terms:
                if _term_matches(q_cf, t.casefold()):
                    matched = ("alias", t)
                    break
        if matched is None:
            desc = _clean_str(e.get("description"))
            if len(desc) >= MIN_SUBSTRING_LEN and desc.casefold() in q_cf:
                matched = ("description", desc)
        if matched:
            hits.append({"entity": name, "match_kind": matched[0], "matched_term": matched[1]})

    hits.sort(key=lambda h: (_MATCH_KIND_ORDER[h["match_kind"]], -len(h["matched_term"])))
    return hits


def _match_entities_v2(query: str, entities) -> list[dict]:
    """v2：词表四级、**收集每个实体的全部命中**（供打分求和），档位按来源级别区分。"""
    q = _clean_str(query)
    if not q:
        return []
    q_cf = q.casefold()

    hits: list[dict] = []
    for e in _as_list(entities):
        if not isinstance(e, dict):
            continue
        name = _clean_str(e.get("name"))
        if not name:
            continue
        matched: list[dict] = []
        exact: dict | None = None
        for term, level in _entity_term_kinds_v2(e):
            t_cf = term.casefold()
            if t_cf == q_cf:
                exact = {"term": term, "kind": "exact", "level": level}
                break
            if _term_matches(q_cf, t_cf):
                kind = {"l1": "alias", "l2": "alias", "l3": "attribute", "l4": "core"}[level]
                matched.append({"term": term, "kind": kind, "level": level})
        if exact is not None:
            matched = [exact]           # 与 v1 同语义：整句相等是最强命中，短路
        if not matched:
            desc = _clean_str(e.get("description"))
            if len(desc) >= MIN_SUBSTRING_LEN and desc.casefold() in q_cf:
                matched = [{"term": desc, "kind": "description", "level": "desc"}]
        if matched:
            # 【用户裁定 2026-10-03（T2 开工冲突）】同档多命中取**词池先命中者**（v1 原语义）：
            # 「命中词」展示稳定优先（中文别名不被打成英文表名），对 P2-1 归因/采纳基线扰动最小。
            best = matched[0]
            hits.append({"entity": name, "match_kind": best["kind"],
                         "matched_term": best["term"], "matches": matched})
    hits.sort(key=lambda h: (_MATCH_KIND_ORDER[h["match_kind"]], -len(h["matched_term"])))
    return hits


def match_entities(query: str, entities) -> list[dict]:
    """查询词 → 命中实体。开关分派：ON → v2（四级词表 + `matches`），OFF → v1（逐字今天）。"""
    if ONTOLOGY_CONCEPT_TERMS_V2_ENABLED:
        return _match_entities_v2(query, entities)
    return _match_entities_v1(query, entities)


def match_metrics(query: str, metrics) -> list[dict]:
    """查询词 → 命中的指标（按 **档位序 → 命中词长降序 → 指标池序** 排序）。

    词表 = `name` / `display_name`（同归 exact 档——display_name 是正名的呈现）/
    `aliases` / `description`。子串语义沿 `_term_matches`（全等，或词长 ≥
    `MIN_SUBSTRING_LEN` 且为查询子串）。不掺 LLM 筛选：指标池小，误筛风险大于误召回。

    返回 `[{name, match_kind, matched_term, metric}]`；调用方（`expand_metric_candidates`）
    只取第一条消费。平手回**指标池序**是刻意口径（沿用 anchor-flip 修复）。
    """
    q = _clean_str(query)
    if not q:
        return []
    q_cf = q.casefold()
    out: list[dict] = []
    for idx, m in enumerate(_as_list(metrics)):
        if not isinstance(m, dict):
            continue
        name = _clean_str(m.get("name"))
        if not name:
            continue
        terms: list[tuple[str, str]] = [(name, "exact")]
        display = _clean_str(m.get("display_name"))
        if display:
            terms.append((display, "exact"))
        terms.extend((_clean_str(a), "alias") for a in _as_list(m.get("aliases")))
        desc = _clean_str(m.get("description"))
        if desc:
            terms.append((desc, "description"))
        best: tuple[int, int, str, str] | None = None
        for term, kind in terms:
            if term and _term_matches(q_cf, term.casefold()):
                cur = (_METRIC_KIND_ORDER[kind], -len(term), term, kind)
                if best is None or cur[:2] < best[:2]:
                    best = cur
        if best is not None:
            rank, neg_len, term, kind = best
            out.append({"name": name, "match_kind": kind, "matched_term": term,
                        "metric": m, "_rank": (rank, neg_len, idx)})
    out.sort(key=lambda h: h["_rank"])
    for h in out:
        del h["_rank"]
    return out


def _pick_metric(query: str, metrics, entities) -> dict | None:
    """选定唯一消费的指标：词面三档优先；无词面命中时聚合词兜底。

    兜底三条件（全在本函数内判定）：问句含 `AGGREGATE_WORDS` 任一词 ∧ 实体命中
    （`match_entities`，沿用其开关分派）∧ 该实体被指标落点引用——实体 `sources[]` 里
    `included is not False` 的 `(data_source_id, entity_name)` 与指标 `landings[]` 求交
    非空：落点**只声明源**（无 `entity_name`——DI 指标落点的常态形态）时按源合上、
    引用该源上实体的任意落点；显式写了 `entity_name` 的落点要求两键全等合上。
    多命中取排序第一（档位 → 词长降序 → 池序），其余不消费。
    """
    hits = match_metrics(query, metrics)
    if hits:
        return hits[0]

    q = _clean_str(query)
    word = next((w for w in AGGREGATE_WORDS if w in q), None)
    if word is None:
        return None
    ent_hits = match_entities(query, entities)
    if not ent_hits:
        return None
    matched_names = {str(h.get("entity") or "") for h in ent_hits}
    referenced: set[tuple[str, str]] = set()
    for e in _as_list(entities):
        if not isinstance(e, dict) or _clean_str(e.get("name")) not in matched_names:
            continue
        for s in _as_list(e.get("sources")):
            if not isinstance(s, dict) or s.get("included") is False:
                continue
            ds = _clean_str(s.get("data_source_id"))
            ent = _clean_str(s.get("entity_name"))
            if ds and ent:
                referenced.add((ds, ent))
    if not referenced:
        return None
    fallback: list[tuple] = []
    for idx, m in enumerate(_as_list(metrics)):
        if not isinstance(m, dict) or not _clean_str(m.get("name")):
            continue
        for landing in _as_list(m.get("landings")):
            if not isinstance(landing, dict):
                continue
            ds = _clean_str(landing.get("data_source_id"))
            ent = _clean_str(landing.get("entity_name"))
            if not ds:
                continue
            # 按落点**声明的键**求交：只声明源（`entity_name` 空，DI 指标落点的常态形态）
            # → 引用该源上实体的任意落点；显式写了 `entity_name` → 两键全等才合上。
            if (ds, ent) in referenced or (not ent and any(r[0] == ds for r in referenced)):
                fallback.append((_METRIC_KIND_ORDER["aggregate"], -len(word), idx, m, word))
                break
    if not fallback:
        return None
    fallback.sort(key=lambda t: t[:3])
    _, _, _, metric, word = fallback[0]
    return {"name": _clean_str(metric.get("name")), "match_kind": "aggregate",
            "matched_term": word, "metric": metric}


def expand_metric_candidates(
    query: str, metrics, entities, *, knowledge_base_id: str,
) -> list[dict]:
    """命中指标 → 每落点一条候选（响应对外契约形状，见 spec §5.3）。

    - 识别见 `_pick_metric`（词面优先、聚合词兜底）；
    - **零落点指标 = 等同未命中**（返回 `[]`，调用方自然回落旧路）；
    - 去重键 `(knowledge_base_id, data_source_id, di_metric, entity_name)`；
    - 候选**全键固定输出**（`di_metric`/`entity_name` 空则空串——响应契约与落库形态的
      刻意差别：落库省键、响应全键，下游一律 `or ""` 读）。
    """
    picked = _pick_metric(query, metrics, entities)
    if picked is None:
        return []
    metric = picked["metric"]
    kb = _clean_str(knowledge_base_id)
    additivity = _clean_str(metric.get("additivity")) or "unknown"
    # 落点 → 抽象实体中文名（chip 展示用：只给指标名时看不出落在哪张汇总表上）
    entity_display: dict[tuple[str, str], str] = {}
    for e in _as_list(entities):
        if not isinstance(e, dict):
            continue
        dn = _clean_str(e.get("display_name")) or _clean_str(e.get("name"))
        if not dn:
            continue
        for s in _as_list(e.get("sources")):
            if isinstance(s, dict):
                s_ds = _clean_str(s.get("data_source_id"))
                s_ent = _clean_str(s.get("entity_name"))
                if s_ds and s_ent:
                    entity_display.setdefault((s_ds, s_ent), dn)
    out: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for landing in _as_list(metric.get("landings")):
        if not isinstance(landing, dict):
            continue
        ds = _clean_str(landing.get("data_source_id"))
        if not ds:
            continue
        key = (kb, ds, _clean_str(landing.get("di_metric")),
               _clean_str(landing.get("entity_name")))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "metric": _clean_str(metric.get("name")),
            "display_name": (_clean_str(metric.get("display_name"))
                             or _clean_str(metric.get("name"))),
            "entity_display_name": entity_display.get((ds, key[3]), ""),
            "knowledge_base_id": kb,
            "data_source_id": ds,
            "di_metric": key[2],
            "entity_name": key[3],
            "additivity": additivity,
            "match_kind": picked["match_kind"],
            "matched_term": picked["matched_term"],
        })
    return out


def _gate_supplied(values) -> bool:
    """白名单参数到底有没有「表达」？（说「没表达」= 空闸门 = 不设限）

    只有在这一层判出「表达了」时，`expand_candidates` 才会把「洗不出可用 id」
    当成调用方的错误表达去 fail-closed；判成「没表达」则维持不设限。

    判据沿用本文件的宽容口径（形状而不是类型说了算）：`None` 与空容器（`[]` /
    `()` / `set()` / `{}`）是「没给」，空白裸串（`""` / `"  "`，常见的 `dict.get`
    默认值）也是「没给」；其余一律算「给了」—— 包括形状就不对、`_as_list` 会
    归零的标量（`42`）。
    """
    if values is None or isinstance(values, (bytes, bytearray)):
        return False
    if isinstance(values, (list, tuple, set, frozenset, dict)):
        return len(values) > 0
    return bool(_clean_str(values))


def _expand_candidates_v1(
    query: str,
    entities,
    *,
    knowledge_base_id: str = "",
    allowed_data_source_ids=None,
) -> list[dict]:
    """命中实体 → N 条**带 `data_source_id`** 的候选。

    `data_source_id` 是「分源列举 + 标注来源」唯一的信息来源（spec §六）：
    没有它，下游拿到一堆实体名却不知道各自该去哪个源里查。

    `allowed_data_source_ids` 非空时只展开这些源 —— 验收 §八.4「一个源单独查询
    时结果只带该源标注」就是靠它。去重键是 `(knowledge_base_id, data_source_id,
    entity_name)`：同一条数据源实体被两个抽象实体覆盖时只出一条，避免下游对同一条
    数据源实体查两遍（NL→SQL 那一步每多一条就是多一次真实查询）。

    闸门两侧的口径统一成 `_clean_str` 之后的字符串（同
    `filter_dangling_sources`）：不一致时一个带空格/非字符串的 id 会让候选**全空**，
    而「查询某个源」随即静默地什么也不返回 —— 没有任何一处报错。

    ⚠️ **传进来的 `entities` 必须都属于 `knowledge_base_id`**。这个 id 是**整次
    调用共用的一个标量**：它被原样盖到每条候选上，又同时是去重键的一部分（同一次
    调用里恒为常数，键里的它区分不出任何东西）。本函数因此**无从**判断一个实体
    实际来自哪个库 —— 多库查询时把各库的 entities **合并**成一个池子、只传一个
    kb id 进来，kbB 的实体会被原样标成 `knowledge_base_id="kbA"`，连同它的
    `data_source_id` 一起输出（正是验收 §八.4 要防的**跨库串源**）。
    多库场景请**逐库调用**：同一个实体池，每个库各调一次、id 各传各的。

    三条要写明白的语义：

    - **空闸门 = 不设限**。`None` 与 `[]` 同义（都展开全部源）。`knowledge_base_id`
      同样过 `_clean_str`（它是去重键的一部分，原样收下的容器会 `TypeError`）。
    - **表达坏了的闸门 = 一条不出**。调用方**给了东西**、只是洗不出一个可用 id
      （`["", "  "]` 全是空白、`42` 这种根本不是白名单形状）时 fail-closed —— 那种
      输入表达的是「只看这几个源」，按「不设限」处理会把限制**反转**成「所有源都
      要」，且不报错、不留痕。`None` / `[]` 与它**不是**一回事，前者照旧不设限。
    - **`included=false` 的落点不展开**（决策：included 才生效）—— 它仍参与
      `match_entities` 的匹配，只是产出候选时被这一层滤掉。
    - **候选按 `entities` 的输入顺序产出**，不是按命中强度排。同一个
      `(kb, ds, entity)` 被两个实体覆盖时**先出现的实体**胜出并附着它的
      `match_kind` / `matched_term` —— 想让更强的命中优先，得由调用方把实体池
      按重要性排好（本函数不重排，免得候选顺序在两条调用路径上不一致）。
    """
    kb = _clean_str(knowledge_base_id)
    items = _as_list(entities)
    allowed_ids = {_clean_str(d) for d in _as_list(allowed_data_source_ids)} - {""}
    if not allowed_ids and _gate_supplied(allowed_data_source_ids):
        # 三态：① 没给（None / 空容器）→ 不设限；② 给了且有可用 id → 按它收窄；
        # ③ 给了东西却一个可用 id 都洗不出来 → 调用方**表达了限制**、只是表达坏了。
        # ③ 若按 ① 处理就是 fail-open：闸门失效得无声无息，限制正好反转成「全都要」。
        # 故 ③ fail-closed —— 宁可一条候选都不出，也不能让跨源结果溜过去。
        return []
    allowed = allowed_ids or None

    hits = {h["entity"]: h for h in _match_entities_v1(query, items)}

    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for e in items:
        if not isinstance(e, dict):
            continue
        hit = hits.get(_clean_str(e.get("name")))
        if hit is None:
            continue
        for s in _as_list(e.get("sources")):
            if not isinstance(s, dict) or s.get("included") is False:
                continue                      # 不纳入的落点不展开（决策：included 才生效）
            ds = _clean_str(s.get("data_source_id"))
            ent = _clean_str(s.get("entity_name"))
            if not ds or not ent:
                continue
            if allowed is not None and ds not in allowed:
                continue
            key = (kb, ds, ent)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "concept": _clean_str(e.get("name")),   # 键名历史包袱：值 = 抽象实体名
                "display_name": (_clean_str(e.get("display_name"))
                                 or _clean_str(e.get("name"))),
                "knowledge_base_id": kb,
                "data_source_id": ds,
                "entity_name": ent,
                "match_kind": hit["match_kind"],
                "matched_term": hit["matched_term"],
            })
    return out


def _score_matches(matches: list[dict]) -> int:
    """整数分 = Σ（不同命中词按级权重）+ 长词加成 1。无浮点：可逐字钉测试。

    同词去重按 `casefold`（终审收口 M-2）：`Job` 与 `job` 是同一个词，只计一次；
    级别取**首个命中**（插入序），不是跨大小写各按级别累加。
    ⚠️ 不能写成字典推导式 —— 重复键是「后者覆盖前者」（值层面的 last-wins），
    首个命中的级别会被后面的弱级别盖掉；词池序里强档在前，先到者胜才合口径。
    """
    terms: dict[str, str] = {}
    for m in matches:
        terms.setdefault(m["term"].casefold(), m["level"])
    score = sum(_LEVEL_WEIGHTS.get(lv, 1) for lv in terms.values())
    if any(len(t) >= _LONG_TERM_LEN for t in terms):
        score += 1
    return score


def _expand_candidates_v2(query, entities, *, knowledge_base_id="", allowed_data_source_ids=None) -> list[dict]:
    """v2 展开：与 v1 同构，增量为 —— 每条候选附三个私有键：
    `_score`（筛选触发与排序）、`_matches`（全部命中词+级别）、`_attr_hits`
    （属性级证据，供筛选 LLM 判定；T9-S1 修复），按**分数降序**排序（同分保持
    实体池序，2026-10-07 修①）、每库封顶 `MAX_CANDIDATES_PER_KB`。

    ⚠️ 三个 `_` 前缀键都是**私有键**：消费方必须经 `concept_filter.filter_candidates`
    取最终形状（它负责剥掉；`concept_candidates` 的形状契约不变）。"""
    kb = _clean_str(knowledge_base_id)
    items = _as_list(entities)
    allowed_ids = {_clean_str(d) for d in _as_list(allowed_data_source_ids)} - {""}
    if not allowed_ids and _gate_supplied(allowed_data_source_ids):
        return []
    allowed = allowed_ids or None

    hits = {h["entity"]: h for h in _match_entities_v2(query, items)}
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for e in items:
        if not isinstance(e, dict):
            continue
        hit = hits.get(_clean_str(e.get("name")))
        if hit is None:
            continue
        score = _score_matches(hit.get("matches") or [])
        for s in _as_list(e.get("sources")):
            if not isinstance(s, dict) or s.get("included") is False:
                continue
            ds = _clean_str(s.get("data_source_id"))
            ent = _clean_str(s.get("entity_name"))
            if not ds or not ent:
                continue
            if allowed is not None and ds not in allowed:
                continue
            key = (kb, ds, ent)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "concept": _clean_str(e.get("name")),
                "display_name": (_clean_str(e.get("display_name"))
                                 or _clean_str(e.get("name"))),
                "knowledge_base_id": kb,
                "data_source_id": ds,
                "entity_name": ent,
                "match_kind": hit["match_kind"],
                "matched_term": hit["matched_term"],
                "_score": score,
                "_matches": hit.get("matches") or [],          # 全部命中词+级别（筛选证据）
                "_attr_hits": _matched_attribute_evidence(e, hit.get("matches") or []),
            })
    # 排序：分数降序；**同分保持实体池序**（稳定排序天然兑现）——2026-10-07 修①，
    # 详见 `expand_candidates` docstring。原次级键 `-命中词长` / `实体名` 已删。
    out.sort(key=lambda c: -c["_score"])
    return out[:MAX_CANDIDATES_PER_KB]


def expand_candidates(query, entities, *, knowledge_base_id="", allowed_data_source_ids=None) -> list[dict]:
    """命中实体 → N 条**带 `data_source_id`** 的候选。开关分派：ON → v2，OFF → v1（逐字今天）。

    v2 的增量只在**形状之外**多三个私有键（`_score` / `_matches` / `_attr_hits`）
    与一段排序：

    - 每条候选附 `_score`（整数命中分：分级权重求和 + 长词加成，见 `_score_matches`）、
      `_matches`（全部命中词+级别）与 `_attr_hits`（属性级证据，T9-S1 修复）——
      它们是**私有键**，消费方必须经 `concept_filter.filter_candidates` 取最终形状
      （剥掉全部 `_` 前缀键后 `concept_candidates` 的公共形状契约不变）；
    - 候选按 `_score` 降序排序、每库封顶 `MAX_CANDIDATES_PER_KB` —— v1 的「按输入
      顺序产出、不重排」在 v2 只对**同分档**成立。⚠️ 2026-10-07 修①：原次级键
      `-命中词长` / `实体名` 已删，**同分回落实体池序**。事故出处：`jobs_by_working_life`
      问句中 Job 与 WorkingLife 6 分打平，`-命中词长` 以展示词长（`工作年限`4 > `职位`2）
      把 WorkingLife 反超成候选序第一 → ② `_pick` 取第一 = 锚点翻转 → 该实体载荷不含
      FK 列名 → 模型猜 `working_life_id`（真名 `working_lifes_id`）→ 207 确定性失败
      （2026-10-04 起；此前池序时该题 26/26）。`matched_term` 是**展示词**（词池先命中
      者），不是该实体最长命中词，不够格当排序键。spec 修订见
      `docs/superpowers/specs/2026-10-03-concept-matching-v2-design.md` §3.3。

    其余语义（闸门三态 fail-closed、`(kb, ds, entity)` 去重、`included=false` 不展开、
    「传进来的 entities 必须都属于同一个 knowledge_base_id」）与 v1 逐字相同，
    逐条见 `_expand_candidates_v1`。`ONTOLOGY_CONCEPT_TERMS_V2_ENABLED=false` 时
    走 v1 直连（不经 v2 词表/打分），兑现「关 = 逐字回到今天」。
    """
    if ONTOLOGY_CONCEPT_TERMS_V2_ENABLED:
        return _expand_candidates_v2(query, entities,
                                     knowledge_base_id=knowledge_base_id,
                                     allowed_data_source_ids=allowed_data_source_ids)
    return _expand_candidates_v1(query, entities,
                                 knowledge_base_id=knowledge_base_id,
                                 allowed_data_source_ids=allowed_data_source_ids)


def annotate_instances(instances, entities) -> list[dict]:
    """给命中的实体实例反向标注「它属于哪个抽象概念、这个概念还挂在哪些源上」。

    这是「补上实体这一半」（spec §六）的落点：现有 `_build_inverse_map` 只翻
    relation 的 inverse，实体侧一直没人管。

    **只标注、不加候选** —— 不改实例集合，检索结果与改造前逐条相同。
    匹配键优先是 `entity_name`（概念里的源映射用的就是它），大小写不敏感；
    文档抽取实例的 `name` 往往是业务叫法（如「B厂综合吨垃圾发电量」），而 `type`
    才是抽象实体名（如 `m12_plant_daily`）—— 所以 `name` 没命中时按 `type` 再兜一次。
    没有这层兜底，文档实例永远拿不到 `concept_sources`，答案里的血缘块也就永远不出现
    （2026-10-21 真机：所有问句 source=ontology、concept_candidates 有、`data_lineage`
    恒为 null）。

    `concept_sources` 带的是**这个概念名下的全部源映射**，而不是「这个实体出现在
    哪些源」：价值恰好不在后者 —— 从 ds_uc3 查到的 `EmployeeDefectRate` 要能看出
    同一个「不良」概念在 ds_assets 上还挂着 `t_defect`，那才谈得上跨源对齐。

    ⚠️ **实体池必须与实例同库**。实体 dict 里没有 `knowledge_base_id`（落库形状
    只有 name/description/aliases/sources 等），本函数无从判断一个实体属于哪个库 ——
    多库查询时把各库的 entities **合并**成一个池子传进来，B 库的同名实体会被
    标上 A 库的实体名与 A 库的 `data_source_id`（正是验收 §八.4 要防的串源）。
    多库场景请**逐库调用**（或按库把实例分开）。

    返回新列表、**不就地修改入参**：这个列表在下游还要走路由/作答/引用，
    就地加字段会让「这次检索为什么多了个字段」变得难查。
    """
    index: dict[str, dict] = {}
    by_concept_name: dict[str, dict] = {}
    for e in _as_list(entities):
        if not isinstance(e, dict):
            continue
        name = _clean_str(e.get("name"))
        if not name:
            continue
        # `max_items=None`：`concept_sources` 是下游可见字段，不受落库上限约束
        # （上限只归落库/展示形状，见 `_unique_landings`）。
        sources = [
            {"data_source_id": s["data_source_id"], "entity_name": s["entity_name"]}
            for s in _unique_landings(e.get("sources"), max_items=None) if s["included"]
        ]
        if not sources:
            continue
        slot = {"concept": name, "sources": sources}
        for s in sources:
            # 先到先得：同一实体名被两个抽象实体引用是数据问题（正常一实体只会对齐成
            # 一个抽象实体）。覆盖会让同一份数据两次查询标注出不同的概念名。
            index.setdefault(s["entity_name"].casefold(), slot)
        # 文档抽取实例的 type 就是抽象实体名（如 m12_plant_daily）：name 没命中时用它兜底。
        by_concept_name.setdefault(name.casefold(), slot)

    out: list[dict] = []
    for inst in _as_list(instances):
        if not isinstance(inst, dict):
            out.append(inst)
            continue
        slot = index.get(_clean_str(inst.get("name")).casefold())
        if not slot:
            # name 是业务叫法（「B厂综合吨垃圾发电量」），type 才是抽象实体名。
            slot = by_concept_name.get(_clean_str(inst.get("type")).casefold())
        if not slot:
            out.append(inst)
            continue
        out.append({
            **inst,
            "concept": slot["concept"],
            # 每个实例各拿一份自己的列表：同一个实体名命中多条实例时，几份输出会
            # 共享同一个 list 对象 —— 下游一处 append 就同时改到另外几条。
            "concept_sources": [dict(s) for s in slot["sources"]],
        })
    return out


MAX_ENTITIES = 500
MAX_RELATIONS = 1000
MAX_ATTRIBUTES_PER_ENTITY = 200
MAX_LANDINGS_PER_ENTITY = 50
MAX_ENUM_VALUES = 50
MAX_METRICS = 500
MAX_LANDINGS_PER_METRIC = 50

#: `tools` 覆盖块（演化台 Tool-Schema 线）：条数上限 + 单条描述长度上限。
#: 名称必填、同名去重（先到者胜）；**未知键一律丢弃**（只认 name/description/enabled）。
TOOLS_MAX = 20
TOOL_DESC_MAX = 256

#: 可加性白名单（与 DI `/v1/metrics` 官方值域对齐：additive/semi_additive/non_additive）；
#: 缺省与非法值一律归 `unknown`（宽容，不做拒绝——unknown 在合计门上按「不重叠直接相加」放行）。
#: 定义收在 `metrics.executable`（W3.3 起口径卡、编译器共用一处），此处保留名字兼容旧 import。

#: 聚合/数量词表：聚合词兜底（问句含词 ∧ 实体命中 ∧ 实体被指标落点引用 → 产指标候选）。
#: 是 onto `fanout.py::_AGGREGATE_WORDS`（六个合计触发词）的**超集**——本地多收四个数量词：
#: 「有多少用户」是常态问句，onto 的合计触发不该收它们，两处用途不同、不强行同源。
#: ⚠️ 本表同时是 `nl2sql/diagnosis.py` 归因文案（指标缺口档）的判据来源（同仓一处定义）——
#: 改词先查这两个消费点。
AGGREGATE_WORDS = ("加起来", "一共", "总共", "合计", "总计", "共计", "多少", "几个", "数量", "总数")

#: 属性类型的白名单（与编译 schema / 画布口径一致；未知类型一律回落 `string`）
_ATTRIBUTE_TYPES = ("string", "text", "number", "integer", "boolean", "date", "datetime", "enum")


def _unique_landings(raw_sources, max_items: int | None = MAX_LANDINGS_PER_ENTITY) -> list[dict]:
    """v2 落点收敛：`{data_source_id, entity_name, included}` 三键，按 (ds, entity) 去重。

    `included` 缺省（非 False）视为 True——「缺省即纳入」是宽口径：迁移与生成
    都显式写值，缺省只会在人手拼的 JSON 里出现，那种情况下「不纳入」会是更令
    人意外的解读。同 (ds, entity) 重复出现时**先到者胜**（含它的 included）。

    `max_items` 是条数上限，缺省取落库/展示共用的 `MAX_LANDINGS_PER_ENTITY`；
    只有**落库/展示形状**该受它约束 —— `annotate_instances` 显式传 `None`
    （无上限）：它产出的 `concept_sources` 是下游可见字段，被落库上限顺手截掉会
    让「这个概念还挂在哪些源」**静默**变少（旧 `_unique_sources` 没有上限）。
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for s in _as_list(raw_sources):
        if not isinstance(s, dict):
            continue
        ds, ent = _clean_str(s.get("data_source_id")), _clean_str(s.get("entity_name"))
        if not (ds and ent):
            # 落点必须两键齐：手工自建实体是「没有落点」，不是「空落点」。
            continue
        if (ds, ent) in seen:
            continue
        seen.add((ds, ent))
        out.append({
            "data_source_id": ds,
            "entity_name": ent,
            "included": s.get("included") is not False,
        })
        if max_items is not None and len(out) >= max_items:
            break
    return out


def _unique_attribute_sources(raw_sources) -> list[dict]:
    """属性来源收敛：`{data_source_id, entity_name, attribute_name, role?, filter?,
    aggregation?, grain?, enabled?}`（后五个可选——role 是选列提示，其余是 P1-5
    mapping 语义槽），按**三元组**去重（role 不进去重键：同一物理列至多一条；
    同键重复时**先到者胜**，含它的 role）。

    P1-5 mapping 语义槽（可选键，**带键才带**，绝不物化缺省值）：

    - `filter` / `aggregation` / `grain`：strip 后非空才带，超 `MAX_LANDING_SLOT_CHARS`
      截断——它们是给 ② 生成提示的自由文本，不是 SQL 权威；
    - `enabled`：**入参带该键才带**，布尔化（只有显式 `False` 是停用，与
      `_carry_over_constraint_enabled` / 读取侧同口径）。缺键 = 启用，不回写。
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for s in _as_list(raw_sources):
        if not isinstance(s, dict):
            continue
        ds = _clean_str(s.get("data_source_id"))
        ent = _clean_str(s.get("entity_name"))
        col = _clean_str(s.get("attribute_name"))
        if not (ds and ent and col) or (ds, ent, col) in seen:
            continue
        seen.add((ds, ent, col))
        item: dict = {"data_source_id": ds, "entity_name": ent, "attribute_name": col}
        role = _clean_str(s.get("role"))
        if role:
            item["role"] = role
        for slot in ("filter", "aggregation", "grain"):
            value = _clean_str(s.get(slot))
            if value:
                item[slot] = value[:MAX_LANDING_SLOT_CHARS]
        if "enabled" in s:
            item["enabled"] = s.get("enabled") is not False
        out.append(item)
    return out


def _normalize_attribute(raw) -> dict | None:
    """单个属性收敛；无名整条丢。

    返回 `{name, type, primary_key, required, sources}`，另有可选键
    `enum_values` / `display_name` / `description`（洗得出才带）。
    """
    if not isinstance(raw, dict):
        return None
    name = _clean_str(raw.get("name"))
    if not name or len(name) > MAX_TERM_LENGTH:
        return None
    attr_type = _clean_str(raw.get("type"))
    out = {
        "name": name,
        "type": attr_type if attr_type in _ATTRIBUTE_TYPES else "string",
        "primary_key": raw.get("primary_key") is True,
        "required": raw.get("required") is True,
        "sources": _unique_attribute_sources(raw.get("sources")),
    }
    enum_values = _dedupe_strings(raw.get("enum_values"))[:MAX_ENUM_VALUES]
    if enum_values:
        out["enum_values"] = enum_values
    if _clean_str(raw.get("display_name")):
        out["display_name"] = _clean_str(raw.get("display_name"))
    if _clean_str(raw.get("description")):
        out["description"] = _clean_str(raw.get("description"))
    return out


def _merge_attributes(existing: list[dict], raw_attributes) -> None:
    """把原始属性并入 `existing`（**初始归一与同名实体合并共用同一条路径**）。

    每个原始属性先过 `_normalize_attribute`（垃圾照旧丢），再按属性名并入：
    名字已存在 → 只把 `sources` 并入（按三元组去重）；名字不存在 → 追加，整体受
    `MAX_ATTRIBUTES_PER_ENTITY` 上限约束（**已达上限仍允许并入既存属性名** ——
    上限挡的是属性条数，不是来源）。标量（display_name/description/type/
    primary_key/required/enum_values）**先到者胜**，与「描述取先到」同口径。

    初始归一也走这里：同一份输入里自带两条同名属性（LLM 重复吐字段）同样要收编成
    一条，否则「一个属性一条、来源多挂」的不变量会被从入口处破坏。
    """
    by_name: dict[str, dict] = {}
    for a in existing:
        by_name.setdefault(a["name"], a)
    for raw in _as_list(raw_attributes):
        attr = _normalize_attribute(raw)
        if attr is None:
            continue
        current = by_name.get(attr["name"])
        if current is None:
            if len(existing) >= MAX_ATTRIBUTES_PER_ENTITY:
                continue
            existing.append(attr)
            by_name[attr["name"]] = attr
        else:
            current["sources"] = _unique_attribute_sources(
                [*current["sources"], *attr["sources"]]
            )


def _unique_metric_landings(raw_landings) -> list[dict]:
    """指标落点收敛：`data_source_id` 必填；`di_metric` 与 `entity_name` **至少其一非空**
    （两个都空 = 没有通道可执行，整条丢）；按三键 `(ds, di_metric, entity_name)` 去重
    （同键先到者胜）；受 `MAX_LANDINGS_PER_METRIC` 约束。空通道**省键**（读侧统一 `or ""`）。

    与 `_unique_landings`（实体落点）的差别：无 `included` 开关（v1 不引入）。
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for s in _as_list(raw_landings):
        if not isinstance(s, dict):
            continue
        ds = _clean_str(s.get("data_source_id"))
        di_metric = _clean_str(s.get("di_metric"))
        entity = _clean_str(s.get("entity_name"))
        if not ds or not (di_metric or entity):
            continue
        if (ds, di_metric, entity) in seen:
            continue
        seen.add((ds, di_metric, entity))
        entry: dict = {"data_source_id": ds}
        if di_metric:
            entry["di_metric"] = di_metric
        if entity:
            entry["entity_name"] = entity
        out.append(entry)
        if len(out) >= MAX_LANDINGS_PER_METRIC:
            break
    return out


def _normalize_metric(raw) -> dict | None:
    """单个指标收敛；无名整条丢（与 `_normalize_attribute` 同款）。

    返回 `{name, aliases, additivity, landings}`，另有可选键 `display_name` /
    `description`（洗得出才带）。`additivity` **恒写**（白名单外归 `unknown`）。
    """
    if not isinstance(raw, dict):
        return None
    name = _clean_str(raw.get("name"))
    if not name or len(name) > MAX_TERM_LENGTH:
        return None
    additivity = _clean_str(raw.get("additivity"))
    out: dict = {
        "name": name,
        "aliases": _dedupe_strings(raw.get("aliases")),
        "additivity": additivity if additivity in ADDITIVITY_VALUES else "unknown",
        "landings": _unique_metric_landings(raw.get("landings")),
    }
    if _clean_str(raw.get("display_name")):
        out["display_name"] = _clean_str(raw.get("display_name"))
    if _clean_str(raw.get("description")):
        out["description"] = _clean_str(raw.get("description"))
    # `executable`（W3.3）：形状透传、不做语义校验（spec D7）。`null` = 显式清空 ——
    # 归一化后不留键；非 dict 一律丢弃（宽容口径，绝不抛）。枚举值统一小写（D8）：
    # 同一语义的大小写变体必须得到同一个 hash，否则漂移判定会把写法差异当变更。
    executable = raw.get("executable")
    if isinstance(executable, dict):
        out["executable"] = normalize_executable(executable)
    return out


def _merge_metrics(existing: list[dict], raw_metrics) -> None:
    """把原始指标并入 `existing`（初始归一与同名合并共用一条路径，同 `_merge_attributes` 纪律）。

    同名 → `aliases` 并集（去重保序）、`landings` 并集（三键去重）；标量
    （display_name/description/additivity）**先到者胜**。新名 → 追加，整体受
    `MAX_METRICS` 上限约束（**已达上限仍允许并入既存指标名**——上限挡条数不挡来源）。
    """
    by_name: dict[str, dict] = {}
    for m in existing:
        by_name.setdefault(m["name"], m)
    for raw in _as_list(raw_metrics):
        metric = _normalize_metric(raw)
        if metric is None:
            continue
        current = by_name.get(metric["name"])
        if current is None:
            if len(existing) >= MAX_METRICS:
                continue
            existing.append(metric)
            by_name[metric["name"]] = metric
        else:
            current["aliases"] = _dedupe_strings([*current["aliases"], *metric["aliases"]])
            current["landings"] = _unique_metric_landings(
                [*current["landings"], *metric["landings"]]
            )
            # 同名合并：`executable` 视作标量，**先到者非空者胜**。缺键不等于声明，
            # 所以第一条没写定义时允许后到条目补上（否则重复条目里的定义会被静默丢）。
            if "executable" not in current and "executable" in metric:
                current["executable"] = metric["executable"]


#: 背书 `origin` 的信任档（与 `nl2sql/joins.py` 的排序表同口径，数值越大越可信）：
#: declared > comment > value > derived。**未知/缺失一律按最低档（derived）**处理。
_ORIGIN_TRUST = {"declared": 3, "comment": 2, "value": 1, "derived": 0}


def _origin_trust(origin) -> int:
    """`origin` 的信任档；未知/缺失视最低档（derived）。"""
    return _ORIGIN_TRUST.get(_clean_str(origin), _ORIGIN_TRUST["derived"])


def _unique_backings(raw_backings) -> list[dict]:
    """背书收敛：`{data_source_id, relation_name, source_column, target_column, origin}`。

    只有 `(ds, relation_name)` 必备；join 键可空（逻辑关系没有背书、背书也可以
    只有来源关系名）。按 (ds, relation_name, source_column, target_column) 去重 ——
    键**不含 `origin`**：同一物理 FK 被多个来源背书时只显示一条（重复显示没有
    信息量，界面上还会像两条边）。

    同键多条时 **`origin` 取信任序最高档**（declared > comment > value > derived，
    未知/缺失视最低档，只有更高档才升级）：`origin` 是证据强度的载体，按「先到者胜」
    并掉会把「先 value 后 declared」的情形**低估**成 value。其余键的取值仍先到者胜。
    """
    out: list[dict] = []
    by_key: dict[tuple, dict] = {}
    for b in _as_list(raw_backings):
        if not isinstance(b, dict):
            continue
        ds = _clean_str(b.get("data_source_id"))
        rel = _clean_str(b.get("relation_name"))
        if not (ds and rel):
            continue
        key = (ds, rel, _clean_str(b.get("source_column")), _clean_str(b.get("target_column")))
        origin = _clean_str(b.get("origin"))
        existing = by_key.get(key)
        if existing is None:
            item: dict = {"data_source_id": ds, "relation_name": rel}
            if _clean_str(b.get("source_column")):
                item["source_column"] = _clean_str(b.get("source_column"))
            if _clean_str(b.get("target_column")):
                item["target_column"] = _clean_str(b.get("target_column"))
            if origin:
                item["origin"] = origin
            by_key[key] = item
            out.append(item)
        elif origin and _origin_trust(origin) > _origin_trust(existing.get("origin")):
            existing["origin"] = origin
    return out


def _normalize_tools(values) -> list[dict]:
    """`tools` 覆盖块归一：名称必填、同名去重（**先到者胜**）、描述截 `TOOL_DESC_MAX`、
    `enabled` **只有显式布尔 false** 才落键、未知键丢弃、条数受 `TOOLS_MAX` 约束。

    宽容口径同族：非 dict 条目 / 无名条目 / 垃圾容器一律跳过，绝不抛。
    「只有显式 false」与约束的 `enabled` 同口径（缺字段 = 启用，怪值也不当停用）。
    """
    tools: list[dict] = []
    seen: set[str] = set()
    for raw in _as_list(values):
        if not isinstance(raw, dict):
            continue
        name = _clean_str(raw.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        item: dict = {"name": name}
        desc = _clean_str(raw.get("description"))
        if desc:
            item["description"] = desc[:TOOL_DESC_MAX]
        if raw.get("enabled") is False:
            item["enabled"] = False
        tools.append(item)
        if len(tools) >= TOOLS_MAX:
            break
    return tools


def normalize_schema(schema) -> dict:
    """把 v2 schema 收敛成落库形状 `{entities, relations, metrics}`（可选 `tools`）。

    与 `normalize_concepts` 同族的宽容口径（垃圾输入绝不抛）：
    - 实体按 `name` 去重：同名**合并**（落点/别名/属性并入、标量取先到）；
    - **零落点实体保留**（手工自建实体；生成管线的剔除由 `filter_dangling_sources`
      在概念阶段完成）；
    - 关系：`source`/`target` 必须指向已收编的实体名，否则**整条丢弃**（画不出、
      也过不了校验）；多态端点三键（`source_types`/`target_types` 实体名列表、长度
      >1 才带；`inverse_of` 非空才带）**先到者胜**（同名合并分支不动）；同名关系
      合并背书；条数受 `MAX_RELATIONS` 约束（到顶即停）；
    - 属性：无名丢、未知类型回落 `string`、来源按三元组去重、同名属性并入一条
      （初始归一与同名实体合并走同一条 `_merge_attributes` 路径）。
    - 指标：无名丢、`additivity` 白名单外归 `unknown`、落点三键去重（`di_metric`/`entity_name` 至少其一）、同名合并（别名/落点并集、标量先到）、条数受 `MAX_METRICS` 约束。
    - `tools`（演化台 Tool-Schema 线，**可选**）：仅在有非空覆盖时出现该键
      （无/空 → 输出**不含** `tools` 键，与既有三键形状逐字节相同）；归一细节见
      `_normalize_tools`。
    """
    if not isinstance(schema, dict):
        return {"entities": [], "relations": [], "metrics": []}

    entities: list[dict] = []
    by_name: dict[str, dict] = {}
    for raw in _as_list(schema.get("entities")):
        if not isinstance(raw, dict):
            continue
        name = _clean_str(raw.get("name"))
        if not name or len(name) > MAX_TERM_LENGTH:
            continue
        existing = by_name.get(name)
        if existing is None:
            attributes: list[dict] = []
            _merge_attributes(attributes, raw.get("attributes"))
            entry: dict = {
                "name": name,
                "description": _clean_str(raw.get("description")),
                "aliases": _dedupe_strings(raw.get("aliases"))[:MAX_ALIASES_PER_CONCEPT],
                "sources": _unique_landings(raw.get("sources")),
                "attributes": attributes,
            }
            if _clean_str(raw.get("display_name")):
                entry["display_name"] = _clean_str(raw.get("display_name"))
            by_name[name] = entry
            entities.append(entry)
            if len(entities) >= MAX_ENTITIES:
                break
        else:
            existing["sources"] = _unique_landings([*existing["sources"], *_as_list(raw.get("sources"))])
            existing["aliases"] = _dedupe_strings(
                [*existing["aliases"], *_as_list(raw.get("aliases"))]
            )[:MAX_ALIASES_PER_CONCEPT]
            _merge_attributes(existing["attributes"], raw.get("attributes"))

    known = {e["name"] for e in entities}
    relations: list[dict] = []
    by_rel: dict[str, dict] = {}
    for raw in _as_list(schema.get("relations")):
        if not isinstance(raw, dict):
            continue
        name = _clean_str(raw.get("name"))
        source, target = _clean_str(raw.get("source")), _clean_str(raw.get("target"))
        if not name or source not in known or target not in known:
            continue
        existing = by_rel.get(name)
        if existing is None:
            entry = {
                "name": name,
                "source": source,
                "target": target,
                "description": _clean_str(raw.get("description")),
                "backings": _unique_backings(raw.get("backings")),
            }
            if _clean_str(raw.get("cardinality")):
                entry["cardinality"] = _clean_str(raw.get("cardinality"))
            # 多态端点三键（2026-10-13 TBox 导入线）：types 数组收敛为实体名列表
            # （去重、保序、超长/脏值 `_dedupe_strings` 已丢）；**单值=未设置**
            # （「集合必含 source/target」的语义校验在**导入器**做——normalize 无告警
            # 通道，只管形状）。`inverse_of` 纯声明字符串。
            # ⚠️ 同名合并分支（下面的 else）**不动**这三键：先到者（= 反序合并里的
            # 导入方）权威——一期 UI 不提供三键编辑，不存在「人工编辑被冲掉」。
            source_types = _dedupe_strings(raw.get("source_types"))
            if len(source_types) > 1:
                entry["source_types"] = source_types
            target_types = _dedupe_strings(raw.get("target_types"))
            if len(target_types) > 1:
                entry["target_types"] = target_types
            if _clean_str(raw.get("inverse_of")):
                entry["inverse_of"] = _clean_str(raw.get("inverse_of"))
            by_rel[name] = entry
            relations.append(entry)
            if len(relations) >= MAX_RELATIONS:
                break
        else:
            existing["backings"] = _unique_backings(
                [*existing["backings"], *_as_list(raw.get("backings"))]
            )
    metrics: list[dict] = []
    _merge_metrics(metrics, schema.get("metrics"))
    out = {"entities": entities, "relations": relations, "metrics": metrics}
    tools = _normalize_tools(schema.get("tools"))
    if tools:
        out["tools"] = tools
    return out


def migrate_v1_concepts(concepts) -> dict:
    """v1 概念列表 → v2 schema：每个概念成一个实体（落点补 `included: true`），关系为空。

    只做形状搬运（name/description/aliases/sources 原样），消毒交给
    `normalize_concepts`（迁移脚本里先 v1 归一、再本函数）。
    """
    entities: list[dict] = []
    for c in _as_list(concepts):
        if not isinstance(c, dict):
            continue
        name = _clean_str(c.get("name"))
        if not name:
            continue
        landings = [
            {"data_source_id": ds, "entity_name": ent, "included": True}
            for ds, ent in _iter_source_pairs(c.get("sources"))
        ]
        if not landings:
            continue
        entry: dict = {
            "name": name,
            "description": _clean_str(c.get("description")),
            "aliases": _dedupe_strings(c.get("aliases"))[:MAX_ALIASES_PER_CONCEPT],
            "sources": landings,
            "attributes": [],
        }
        entities.append(entry)
        if len(entities) >= MAX_ENTITIES:
            break
    return {"entities": entities, "relations": [], "metrics": []}


__all__ = [
    "ADDITIVITY_VALUES",
    "AGGREGATE_WORDS",
    "MAX_ALIASES_PER_CONCEPT",
    "MAX_CANDIDATES_PER_KB",
    "MAX_CONCEPTS",
    "MAX_LANDINGS_PER_METRIC",
    "MAX_LANDING_SLOT_CHARS",
    "MAX_METRICS",
    "MAX_TERM_LENGTH",
    "MIN_SUBSTRING_LEN",
    "ONTOLOGY_CONCEPT_TERMS_V2_ENABLED",
    "TOOLS_MAX",
    "TOOL_DESC_MAX",
    "annotate_instances",
    "expand_candidates",
    "expand_metric_candidates",
    "filter_dangling_sources",
    "match_entities",
    "match_metrics",
    "migrate_v1_concepts",
    "normalize_concepts",
    "normalize_schema",
]
