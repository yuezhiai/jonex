"""抽象 schema「聚焦生成」的确定性构件：关系图 / 1 跳闭包 / 种子校验。

纯函数、不连库、不出网（LLM 解析在
`jonex_core.common.ontology_llm.resolve_focus_entities`）。

为什么不把「选哪些表」一步交给 LLM：模型只做它最擅长的**语义判断**（提示词 →
重点实体）；「沿关系扩一跳」是图操作，确定性算出来同输入同输出、可测、可复现。
让模型顺手选邻居，每次结果都可能不一样，还没法测。
"""

from __future__ import annotations

# 提交清单的硬上限（正常一次几十条；上限只为挡住畸形请求）
FOCUS_SEEDS_MAX = 500
# 种子数软警告线：超过只提示、不拦截（解析可能确实命中很多实体）
FOCUS_SEED_SOFT_WARN = 100


def _clean(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def build_relation_graph(rows) -> dict[str, dict[str, dict[str, set[str]]]]:
    """每个源一行 compiled schema → 无向关系图 `{ds: {entity: {neighbor: {关系名}}}}`。

    - **全部 origin 都收**（declared/comment/value/derived）：实测高信任边太少
      （jinpin declared 仅 7/75），只算它们几乎扩不动；低信任边的代价只是「多带
      一张表进来」，比「该带的没带上」轻。
    - 端点不在该行 `entity_types` 里的边丢掉（脏边）；自环丢（否则种子会把自己
      扩成邻居）。
    - **同一 ds 出现多行时实体取并集、每行只收本行声明端点之间的边**
      （畸形数据硬化）：只按第一行建节点的话，后出行新引入的实体会静默缺节点、
      其参与的关系被整体丢弃。
    """
    graph: dict[str, dict[str, dict[str, set[str]]]] = {}
    for r in rows:
        ds = r.data_source_id
        names = {
            str(e.get("name") or "").strip()
            for e in (r.entity_types or [])
            if isinstance(e, dict)
        }
        names.discard("")
        node = graph.setdefault(ds, {})
        for n in names:
            node.setdefault(n, {})
        for rel in (r.relation_types or []):
            if not isinstance(rel, dict):
                continue
            src = str(rel.get("source") or "").strip()
            tgt = str(rel.get("target") or "").strip()
            name = str(rel.get("name") or "").strip()
            if not name or not src or not tgt or src == tgt:
                continue
            if src not in names or tgt not in names:
                continue
            node[src].setdefault(tgt, set()).add(name)
            node[tgt].setdefault(src, set()).add(name)
    return graph


def expand_focus_closure(seeds, graph) -> tuple[set, list[dict]]:
    """种子 →（闭包集合, 邻居清单）。**双向 1 跳**。

    seeds 项只读 `data_source_id` / `entity_name`。neighbors 每项：
    `{"data_source_id", "entity_name", "via": [{"seed_data_source_id",
    "seed_entity_name", "relation_name"}]}` —— 同一邻居被多枚种子/多条关系拉进来
    时全部记下（UI 展示「经由谁」）；**种子自身不算邻居**（它是「重点」，UI 分两组）。
    输出按 (ds, entity) / (seed, relation) 稳定排序，同输入同输出。
    """
    seed_pairs = {(s["data_source_id"], s["entity_name"]) for s in seeds}
    included = set(seed_pairs)
    via_map: dict[tuple[str, str], list[dict]] = {}
    for s in seeds:
        sds, sent = s["data_source_id"], s["entity_name"]
        for neighbor, rels in (graph.get(sds, {}).get(sent) or {}).items():
            npair = (sds, neighbor)
            included.add(npair)
            if npair in seed_pairs:
                continue
            entries = via_map.setdefault(npair, [])
            for rel in sorted(rels):
                entry = {"seed_data_source_id": sds, "seed_entity_name": sent,
                         "relation_name": rel}
                if entry not in entries:
                    entries.append(entry)
    neighbors = [
        {"data_source_id": ds, "entity_name": ent,
         "via": sorted(
             via_map[(ds, ent)],
             key=lambda v: (v["seed_data_source_id"], v["seed_entity_name"],
                            v["relation_name"]),
         )}
        for ds, ent in sorted(via_map)
    ]
    return included, neighbors


def validate_focus_seeds(groups, allowed: set) -> tuple[list[dict], int]:
    """LLM 的种子组 → 校验后的种子清单（模型编造一律丢弃，全部确定性）。

    - 成员必须逐字命中 `allowed`（调用方给的 (ds, entity) 全集），否则丢；
    - 组内**第一个存活成员**是 `focus`（提示词直接点名的那个），其后是 `partner`
      （同概念对应，合并的另一半）；整组成员全悬空的组直接消失；
    - 同 (ds, entity) 跨组去重，先到者胜。

    Returns `(seeds, dropped)`；seeds 项 = `{"data_source_id", "entity_name",
    "role", "concept"}`，dropped = 被丢弃的成员条数（日志/诊断用，含重复）。
    """
    seeds: list[dict] = []
    dropped = 0
    seen: set[tuple[str, str]] = set()
    for g in (groups or []):
        if not isinstance(g, dict):
            continue
        concept = str(g.get("concept") or "").strip()
        group_first = True
        for m in (g.get("members") if isinstance(g.get("members"), list) else []):
            if not isinstance(m, dict):
                dropped += 1
                continue
            ds = _clean(m.get("data_source_id"))
            ent = _clean(m.get("entity_name"))
            if not ds or not ent or (ds, ent) not in allowed or (ds, ent) in seen:
                dropped += 1
                continue
            seen.add((ds, ent))
            seeds.append({
                "data_source_id": ds, "entity_name": ent,
                "role": "focus" if group_first else "partner",
                "concept": concept,
            })
            group_first = False
    return seeds, dropped


def filter_focus_seeds(seeds, allowed: set) -> tuple[list[dict], int]:
    """提交清单（前端删减后）→ 过滤悬空 / 畸形 / 重复，**保留 role**。

    与 `validate_focus_seeds` 的分工：那份管「LLM 组 → 种子（role 由组序推导）」，
    这份管「用户确认后的清单 → 存活种子（role 是用户看到的，原样带回）」。
    源侧在预览之后被改（源删了/表没了）时这里落空 —— 服务层据此报「请重新解析」。
    role 只认 `focus` / `partner`，其余（缺省、脏值）按 `focus` 收。

    Returns `(alive, dropped)`。
    """
    alive: list[dict] = []
    dropped = 0
    seen: set[tuple[str, str]] = set()
    for s in (seeds or []):
        if not isinstance(s, dict):
            dropped += 1
            continue
        ds = _clean(s.get("data_source_id"))
        ent = _clean(s.get("entity_name"))
        if not ds or not ent or (ds, ent) not in allowed or (ds, ent) in seen:
            dropped += 1
            continue
        seen.add((ds, ent))
        alive.append({
            "data_source_id": ds, "entity_name": ent,
            "role": "partner" if s.get("role") == "partner" else "focus",
        })
    return alive, dropped
