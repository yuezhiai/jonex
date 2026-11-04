"""目录清洗与投影纯函数（目录功能下沉）。

从平台 ``capabilities/knowledge_base/services/wiki_outline_service.py`` 平移的
无状态部分，引擎侧去掉平台依赖（translate/i18n/kb_type 校验/KB 名），并新增
目录输入预处理所需的 ``_clean_title``（HTML 表格标题清洗，与平台 adapter 的
展示投影同口径，但服务于「送 LLM 的页面清单」而非展示）。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from typing import Any, Optional

# ── 目录 source 取值（区分自动生成 vs 人工调整）──
SOURCE_DRAFT = "draft"
SOURCE_ADJUST = "adjust"

# ── 结构上限（防 LLM 输出失控；同时也是前端渲染的护栏）──
MAX_TOPICS = 40
MAX_NODES_PER_LEVEL = 60
MAX_TOTAL_NODES = 400
MAX_DEPTH = 3
MAX_ENTRIES_PER_NODE = 500
# 主题地图边上限：主题数本身 ≤40，边再多也读不清；按权重取前 N 条
MAX_TOPIC_EDGES = 120
MAX_TITLE_LEN = 80
MAX_DESCRIPTION_LEN = 240
MAX_NOTE_LEN = 60
# 送进 LLM 的页面数上限：超大库一次列不全，超出部分只保证「不丢」（进未归类）
MAX_PAGES_FOR_LLM = 500
# adjust 覆盖率护栏：新目录覆盖的页面数不足旧的 60% → 视为调整失败
ADJUST_COVERAGE_FLOOR = 0.6

# 日期识别（用于「带日期的条目按时间升序」这一确定性兜底）
_DATE_PATTERNS = (
    re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})"),
    re.compile(r"(\d{4})[-/年](\d{1,2})月?"),
    re.compile(r"(?<!\d)(\d{1,2})[-/月](\d{1,2})(?!\d)"),
)

_UNCLASSIFIED_ID = "unclassified"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TD_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _clean_title(raw: Any, fallback: str, *, limit: int = MAX_TITLE_LEN) -> str:
    """页面标题清洗（目录输入预处理）。

    真实产物里表格页的 frontmatter title 可能是**整段 HTML**（``<table>`` 或整行
    ``<tr><td>…</td>…``），直接送 LLM 会污染目录编排。清洗顺序：
    1. HTML 表格标题 → 取首行单元格文本拼成列名（``修订前 · 修订后``）；
    2. 剥标签后的纯文本；
    3. 页面 path（fallback）。
    最后统一截断到 limit 字符。
    """

    def plain(text: Any) -> str:
        return _WS_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", str(text or "")))).strip()

    raw_text = str(raw or "")
    if "<" in raw_text:
        cells = [c for c in (plain(cell) for cell in _TD_CELL_RE.findall(raw_text)) if c]
        if len(cells) >= 2:
            label = " · ".join(cells[:3])
            return label if len(label) <= limit else label[:limit].rstrip() + "…"
    candidate = plain(raw_text)
    if candidate:
        return candidate if len(candidate) <= limit else candidate[:limit].rstrip() + "…"
    return fallback


def _entry_date(*texts: str) -> Optional[tuple[int, int, int]]:
    """从标题/摘要里抽日期，返回可比较元组；抽不到返回 None。

    只做「确定性兜底」：LLM 被要求自行按时间排序，但日期类条目顺序错了对读者
    伤害最大，因此在 sanitize 阶段再按日期校正一次——仅当一个分组内**全部**
    条目都有日期时才重排，避免打乱混合分组。
    """
    for text in texts:
        t = str(text or "")
        for idx, pattern in enumerate(_DATE_PATTERNS):
            m = pattern.search(t)
            if not m:
                continue
            groups = m.groups()
            try:
                if idx == 0:
                    return (int(groups[0]), int(groups[1]), int(groups[2]))
                if idx == 1:
                    return (int(groups[0]), int(groups[1]), 0)
                return (0, int(groups[0]), int(groups[1]))
            except (TypeError, ValueError):
                continue
    return None


def _fingerprint(pages: list[dict]) -> str:
    """页面清单指纹：path + title 排序后取 sha256（判断目录是否过期）。"""
    blob = "\n".join(
        sorted(f"{p.get('path')}\t{p.get('title')}" for p in pages)
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def scope_outline_to_pages(outline: Any, paths: set) -> Optional[dict]:
    """把知识库级目录**投影**到一批页面（文档级阅读视图用）。

    目录是知识库级的、由 LLM 起草并落库，单文档视图不该也无法为每个文档重新
    起草一份。这里做纯函数过滤：保留含目标页面的主题/小节，条目只留目标页面，
    过滤后没有条目的主题/小节整块丢弃；顺序、标题、描述、note 一律保持原样。

    入参 outline 为 None 或过滤后为空 → 返回 None（前端退化为按分类浏览）。
    """
    if not isinstance(outline, dict):
        return None

    def clean_entries(entries: Any) -> list[dict]:
        out: list[dict] = []
        for entry in (entries or []):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("path") or "") not in paths:
                continue
            item = dict(entry)
            item["order"] = len(out) + 1
            out.append(item)
        return out

    def clean_nodes(nodes: Any) -> list[dict]:
        out: list[dict] = []
        for node in (nodes or []):
            if not isinstance(node, dict):
                continue
            entries = clean_entries(node.get("entries"))
            children = clean_nodes(node.get("children"))
            if not entries and not children:
                continue
            item = dict(node)
            item["entries"] = entries
            item["children"] = children
            item["order"] = len(out) + 1
            out.append(item)
        return out

    topics = clean_nodes(outline.get("topics"))
    if not topics:
        return None
    scoped = dict(outline)
    scoped["topics"] = topics
    scoped["page_count"] = sum(
        len(t.get("entries") or [])
        + sum(len(c.get("entries") or []) for c in (t.get("children") or []))
        for t in topics
    )
    return scoped


def _iter_topic_entries(topic: Any):
    """深度优先遍历主题下的所有 entry（含子分组）。"""
    if not isinstance(topic, dict):
        return
    for entry in (topic.get("entries") or []):
        if isinstance(entry, dict):
            yield entry
    for child in (topic.get("children") or []):
        yield from _iter_topic_entries(child)


def _topic_facets(topic: Any, catalog: dict) -> tuple[set, set]:
    """主题 → (分类集合, 标签集合)：递归收集该主题覆盖页面的分类与标签。"""
    categories: set = set()
    tags: set = set()
    for entry in _iter_topic_entries(topic):
        page = catalog.get(str(entry.get("path") or ""))
        if not page:
            continue
        category = str(page.get("category") or "").strip()
        if category:
            categories.add(category)
        for tag in (page.get("tags") or []):
            tag = str(tag).strip()
            if tag:
                tags.add(tag)
    return categories, tags


def build_topic_edges(outline: Any, pages: list[dict]) -> list[dict]:
    """主题之间的关系边（Wiki 图谱·主题地图）。

    刻意不做全库页面级力导向图。主题级只有几十个节点，边表达「两章共享同一批
    分类/标签」。权重 = 共享标签数 × 2 + 共享分类数；无共享不连边；
    「未归类」是兜底分组，不参与关系推断。
    """
    topics = (outline or {}).get("topics") if isinstance(outline, dict) else None
    if not isinstance(topics, list) or not topics:
        return []
    catalog = {str(p.get("path")): p for p in pages if p.get("path")}
    facets: list[tuple] = []
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        tid = str(topic.get("id") or "")
        if not tid or tid == _UNCLASSIFIED_ID:
            continue
        categories, tags = _topic_facets(topic, catalog)
        facets.append((tid, categories, tags))

    edges: list[dict] = []
    for i in range(len(facets)):
        for j in range(i + 1, len(facets)):
            a_id, a_cats, a_tags = facets[i]
            b_id, b_cats, b_tags = facets[j]
            shared_tags = sorted(a_tags & b_tags)
            shared_cats = sorted(a_cats & b_cats)
            weight = len(shared_tags) * 2 + len(shared_cats)
            if weight <= 0:
                continue
            reasons = [f"标签：{t}" for t in shared_tags[:2]] + \
                      [f"分类：{c}" for c in shared_cats[:2]]
            edges.append({
                "source": a_id,
                "target": b_id,
                "weight": weight,
                "reasons": reasons[:3],
            })
    edges.sort(key=lambda e: (-int(e["weight"]), str(e["source"]), str(e["target"])))
    return edges[:MAX_TOPIC_EDGES]


def sanitize_outline(
    raw: Any, pages: list[dict], *,
    unclassified_title: str = "未归类",
    overview: str = "",
) -> dict:
    """把 LLM 输出规整为可渲染、可校验的目录结构。

    规则：
    - 只保留落在 pages 内的 entry.path；越界/重复一律丢弃；
    - entry.title 一律取引擎页面标题（LLM 不得改名）；
    - 空分组（无 entry 且无子分组）丢弃；
    - 深度/宽度/数量超限截断；
    - 分组内全部条目带日期时按时间升序（确定性兜底）；
    - 未被任何 entry 覆盖的页面 → 「未归类」主题（放最后）；
    - LLM 结果整体不可用 → 按引擎分类目录生成确定性兜底目录。
    """
    catalog = {str(p.get("path")): p for p in pages if p.get("path")}
    used: set[str] = set()
    seq = {"n": 0}

    def clean_entries(raw_entries: Any) -> list[dict]:
        out: list[dict] = []
        if not isinstance(raw_entries, list):
            return out
        for item in raw_entries[:MAX_ENTRIES_PER_NODE]:
            path = ""
            note = ""
            if isinstance(item, str):
                path = item.strip()
            elif isinstance(item, dict):
                path = str(item.get("path") or "").strip()
                note = _clip(item.get("note"), MAX_NOTE_LEN)
            if not path or path not in catalog or path in used:
                continue
            used.add(path)
            page = catalog[path]
            out.append({
                "path": path,
                "title": _clip(page.get("title"), MAX_TITLE_LEN) or path,
                "role": _clip(page.get("type"), 32) or "concept",
                "category": _clip(page.get("category"), 64),
                "note": note,
                "order": len(out) + 1,
            })
        # 日期兜底：整组都是日期条目 → 按时间升序重排（最早在前）
        if len(out) > 1:
            dated = [
                (
                    _entry_date(
                        e["title"], e.get("note"),
                        catalog[e["path"]].get("description"),
                    ),
                    e,
                )
                for e in out
            ]
            if all(d is not None for d, _ in dated):
                dated.sort(key=lambda pair: pair[0])
                out = [e for _, e in dated]
                for i, e in enumerate(out):
                    e["order"] = i + 1
        return out

    def clean_nodes(raw_nodes: Any, depth: int) -> list[dict]:
        out: list[dict] = []
        if not isinstance(raw_nodes, list) or depth > MAX_DEPTH:
            return out
        for item in raw_nodes[:MAX_NODES_PER_LEVEL]:
            if len(out) >= MAX_NODES_PER_LEVEL or seq["n"] >= MAX_TOTAL_NODES:
                break
            if not isinstance(item, dict):
                continue
            entries = clean_entries(item.get("entries"))
            children = clean_nodes(item.get("children"), depth + 1)
            if not entries and not children:
                continue
            seq["n"] += 1
            out.append({
                "id": f"n{seq['n']}",
                "kind": "topic" if depth == 1 else "section",
                "title": _clip(item.get("title"), MAX_TITLE_LEN) or f"分组 {len(out) + 1}",
                "description": _clip(item.get("description"), MAX_DESCRIPTION_LEN),
                "order": len(out) + 1,
                "entries": entries,
                "children": children,
            })
        return out

    raw_topics = (raw or {}).get("topics") if isinstance(raw, dict) else None
    topics = clean_nodes(raw_topics, 1)[:MAX_TOPICS]
    covered = len(used)  # LLM 真正编排进目录的页面数（不含兜底/未归类）

    if pages and covered == 0:
        # LLM 结果不可用（空 / 结构全非法）→ 确定性兜底：按引擎分类目录分组，
        # 保证阅读页永远有目录可看，而不是只剩一个「未归类」大桶。
        topics = _fallback_topics(pages, unclassified_title)
        for topic in topics:
            for entry in topic["entries"]:
                used.add(entry["path"])
    else:
        leftover = [p for p in pages if p.get("path") and p["path"] not in used]
        if leftover:
            entries = clean_entries([{"path": p["path"]} for p in leftover])
            if entries:
                topics.append({
                    "id": _UNCLASSIFIED_ID,
                    "kind": "unclassified",
                    "title": _clip(unclassified_title, MAX_TITLE_LEN) or "未归类",
                    "description": "",
                    "order": len(topics) + 1,
                    "entries": entries,
                    "children": [],
                })

    if not overview and isinstance(raw, dict):
        overview = _clip(raw.get("overview"), MAX_DESCRIPTION_LEN)
    return {
        "version": 1,
        "overview": _clip(overview, MAX_DESCRIPTION_LEN),
        "topics": topics,
        "page_count": len(pages),
        "covered_count": covered,
    }


def _catalog_snapshot(pages: list[dict]) -> list[dict]:
    """留档生成时的页面清单（path/title/type），供排查「目录为什么过期」。"""
    return [
        {"path": p.get("path", ""), "title": p.get("title", ""), "type": p.get("type", "")}
        for p in pages
    ]


def _covered_paths(outline: dict) -> set[str]:
    paths: set[str] = set()

    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for entry in (node.get("entries") or []):
                if isinstance(entry, dict) and entry.get("path"):
                    paths.add(str(entry["path"]))
            walk(node.get("children"))

    walk((outline or {}).get("topics"))
    return paths


def _outline_for_llm(outline: dict) -> dict:
    """当前目录 → 送 LLM 的精简形态（只留结构与路径，省 token）。"""

    def walk(nodes: Any) -> list:
        out = []
        for node in (nodes or []):
            if not isinstance(node, dict):
                continue
            out.append({
                "title": node.get("title", ""),
                "description": node.get("description", ""),
                "entries": [
                    {"path": e.get("path", ""), "note": e.get("note", "")}
                    for e in (node.get("entries") or []) if isinstance(e, dict)
                ],
                "children": walk(node.get("children")),
            })
        return out

    return {
        "overview": (outline or {}).get("overview", ""),
        "topics": walk((outline or {}).get("topics")),
    }


def _fallback_topics(pages: list[dict], unclassified_title: str) -> list[dict]:
    """LLM 结果不可用时的确定性兜底：按引擎分类目录（category）分组。"""
    groups: dict[str, list[dict]] = {}
    for page in pages:
        groups.setdefault(page.get("category") or unclassified_title, []).append(page)
    topics: list[dict] = []
    for i, (name, items) in enumerate(sorted(groups.items()), start=1):
        topics.append({
            "id": f"n{i}",
            "kind": "topic",
            "title": _clip(name, MAX_TITLE_LEN) or f"分组 {i}",
            "description": "",
            "order": i,
            "entries": [
                {"path": p["path"], "title": p["title"], "role": p.get("type", "concept"),
                 "category": p.get("category", ""), "note": "", "order": j + 1}
                for j, p in enumerate(items[:MAX_ENTRIES_PER_NODE])
            ],
            "children": [],
        })
    return topics[:MAX_TOPICS]


def _extract_json_object(text: str) -> Optional[dict]:
    """从 LLM 文本里抠出第一个 JSON 对象（容忍 ```json 围栏与前后解释）。"""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    depth = 0
    start = -1
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(cleaned[start:i + 1])
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    start = -1
    return None


def _draft_max_tokens() -> int:
    """目录生成的输出预算：思考 + JSON 共用（默认 32000，允许环境覆盖）。"""
    raw = os.getenv("WIKI_OUTLINE_MAX_TOKENS", "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 0
    return value if value > 0 else 32000


_DRAFT_SYSTEM_PROMPT = """你是知识库 Wiki 的目录编辑。用户给你一份页面清单，你要把它组织成一份像课本/维基百科那样**有阅读顺序**的目录。

硬性规则：
1. 只能使用清单中出现的 path，一个字符都不能改；绝不发明清单里没有的页面。
2. 每个 path 最多出现一次。
3. 顶层主题 3~12 个，标题依据实际材料主题，用读者能看懂的名称，不要用 concept/table/其他 这类内部词；主题按阅读顺序排列。
4. 主题下可以再分小节（children），最多 3 层。读者先读总览、再读明细、最后读数据与案例。
5. 条目顺序 = 建议阅读顺序：标题或摘要里带日期的页面按时间**升序**（最早在前）；其余按逻辑递进。
6. 页面标题保持原样，不要翻译、不要改写。
7. 同类页面要聚在一起，不要按字母序或路径序机械排列。
8. 只输出 JSON，不要 markdown 代码块，不要任何解释文字。

输出 JSON 形状：
{"overview":"一句话说明这份知识库讲了什么",
 "topics":[{"title":"主题名","description":"一句话说明这章讲什么",
            "entries":[{"path":"清单里的 path","note":"可选：≤20字，为什么先读它"}],
            "children":[{"title":"小节名","description":"","entries":[{"path":"...","note":""}],"children":[]}]}]}"""

_ADJUST_SYSTEM_PROMPT = """你是知识库 Wiki 的目录编辑。用户给你**当前目录**、**可用页面清单**和一条**调整要求**，你要在现有目录上做修改。

硬性规则：
1. 只按调整要求改动；未被要求的部分必须原样保留（同样的主题、同样的顺序、同样的 path 与 note）。
2. 只能使用清单中出现的 path；绝不发明新页面；每个 path 整个目录里最多出现一次。
3. 不要因为"顺手"而重排或重命名未被要求的部分。
4. 页面标题保持清单里的原样。
5. 输出调整后的**完整**目录 JSON（不是差异），形状与当前目录一致。
6. 只输出 JSON，不要 markdown 代码块，不要解释。

输出 JSON 形状：
{"overview":"一句话总览",
 "topics":[{"title":"主题名","description":"","entries":[{"path":"...","note":""}],"children":[]}]}"""
