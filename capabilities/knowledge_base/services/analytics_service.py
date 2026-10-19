#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""数据运营（Data Operations）聚合服务。

二期口径。下面三条是结构性约定，改本文件前先读：

- **事件表是唯一数据源**：`search_request_event` / `search_step_event` /
  `search_hit_event` / `search_adoption_event` / `processing_task_event`。
  对 `KnowledgeSearchHistory` 的聚合**已全部删除**（历史行由
  `scripts/backfill_search_events.py` 一次性回填成事件行），不做双源合并。
- 「数据处理流水 / 异常资产」两块的数据源是 `processing_task_event`；
  四级漏斗 = **状态列 ∪ 事件表**，取并集保证单调，且埋点缺失时自然退化为纯状态列口径。
- 全部聚合方法都套 `_resilient` 降级层：非预期异常 → ERROR 日志 → 返回等结构空壳，
  **绝不 5xx**；`JonexException`（租户非法 / 权限不足等 4xx 语义）继续向上抛。

调用链：Gateway → Sidecar /invoke → capability → AnalyticsService → PG / Neo4j。
权限判定（传 domain_space_id 则空间成员、不传则仅租户管理员）在 `capability.execute` 里，
本服务只按 domain_space_id + kb_id 过滤数据范围 —— 两个参数都只能「收窄」范围。

⚠️ asyncpg 三条硬约束（改本文件前先读）：

1. `= ANY(:kb_ids)` 传 list，**不是** `IN :kb_ids` —— asyncpg 只认 `$N` 位置占位符，
   不做 psycopg2 那种元组字面量展开，`IN :kb_ids` 会渲染成 `IN $3` 直接语法错；
2. 原生 `text()` 的绑定参数**必须显式 `CAST`**（无类型信息时 PG 推断会撞
   `UndefinedFunctionError`）；时间窗口一律用「结束日 +1 天」的开区间，
   写成 `< 当天` 会把当天整段截掉；
3. **不往 TIMESTAMPTZ 写 naive datetime**（asyncpg 按 UTC 编码 → 本地值差 8 小时）；
   本文件只读不写，写侧口径见 `repository/processing_task_repository.py` 的模块注释。
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from functools import wraps

from sqlalchemy import func, select, text

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import JonexException, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.neo4j_client import get_neo4j_driver
from jonex_core.common.quota import classify_quota_category
from jonex_core.common.tenant import require_tenant

from ..dtos.reasoning import (
    STAGE_ARBITRATION,
    STAGE_CHUNK_ANSWER,
    STAGE_CONTEXT_RETRIEVE,
    STAGE_FACT_LOOKUP,
    STAGE_JONEX_WIKI_QUERY,
    STAGE_LLM_ANSWER,
    STAGE_ONTOLOGY_MATCH,
    STAGE_OPENKB_QUERY,
    STAGE_ROUTE_DECISION,
    STAGE_TIMELINE_GRAPH,
    STATUS_DONE,
    STATUS_FAILED,
)
from ..models import (
    DomainService,
    KnowledgeDataSource,
    KnowledgeDocument,
    KnowledgeInfo,
)
from ..models.document import DocStatus, OntologyStatus
from ..repository import (
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TASK_TYPE_COMPILE,
    TASK_TYPE_EXTRACT,
    TASK_TYPE_PARSE,
    OntologyGraphRepository,
)
from .kb_type_service import LLM_WIKI_TYPES
from .knowledge_info_service import KnowledgeInfoService
from .search_event_writer import (
    TARGET_DOCUMENT,
    TARGET_ONTOLOGY_INSTANCE,
    ontology_object_id,
)

logger = logging.getLogger(__name__)

# ── 五个「解析器类目」的后缀表 ──────────────────────────────────
# 这是「文档/文本/图像/音频/视频」五类在平台里的**唯一权威定义**，直接抄自 KB 详情页
# 「解析配置 → 文件类解析配置」那张表（即 `business_domain.parser_configs.file_types`，
# 种子数据见 `deploy/postgres/migrations/006_seed_data.sql`）。
# 顺序即判定优先级（五类后缀互不重叠，实际无所谓，但保持与展示顺序一致）。
#
# ⚠️ 与 `quota.py` 的配额类别**刻意有两处不同**，改回去就是口径漂移：
#   · `.xls/.xlsx` 在这里属**文档**（解析配置的文档解析器就管 Excel），
#     而配额轴把它们算作 spreadsheet（表格）；
#   · `.json/.jsonl` 在这里属**文本**（文本解析器的 file_types 里有 JSON/JSONL），
#     而配额轴把 `.json` 算 document、`.jsonl` 算 document。
# 两条轴各管各的：配额轴管「放不放行、算多少量」（上传准入第一道门，别动），
# 这条管「构成图上算哪一类、标签显示什么」。
_PARSER_CATEGORY_EXT: tuple[tuple[str, frozenset[str]], ...] = (
    ("document", frozenset({".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx"})),
    ("text", frozenset({".json", ".jsonl", ".markdown", ".md", ".txt"})),
    ("image", frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})),
    ("audio", frozenset({".aac", ".amr", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"})),
    ("video", frozenset({".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm", ".wmv"})),
)

# 结构化数据分类：五类之外的补充档（json 已随解析配置归「文本」，不在这里）。
# csv/ods 仍归 spreadsheet（表格，页面上是隐藏类）。
_STRUCTURED_MIME = frozenset({
    "application/xml", "text/xml",
    "application/x-yaml", "text/yaml", "application/yaml",
    "application/x-sql", "application/sql",
    "application/x-parquet", "application/vnd.apache.parquet",
})
_STRUCTURED_EXT = frozenset({
    ".xml", ".yaml", ".yml", ".parquet", ".sql", ".sqlite", ".db", ".ndjson",
})

# 无扩展名时的纯文本补充判据（解析配置按后缀分档，没有后缀就只剩 mime 可看）。
# ⚠️ 不要用 `text/` 前缀兜底 —— `text/csv` 属表格，一刀切会把 csv 变成「文本」。
_TEXT_MIME = frozenset({
    "text/plain", "text/x-markdown", "application/x-markdown",
})

# 固定 8 类顺序（structured 恒在 image 之后、video 之前；text 紧随 document）
_COMPOSITION_CATEGORIES = (
    "document", "text", "spreadsheet", "image", "structured", "video", "audio", "other",
)

# [jonex] 二期：对外直出 `asset_type`。前端 `DoAssetType` 只有 7 类，
# 与后端的唯一差异是表格：后端按 mime 归为 `spreadsheet`，前端叫 `table`
# （`api/dataOperations.ts` 的 TO_API_CATEGORY 已固化这个映射）。
# 这里直接输出前端口径 —— 否则前端要再写一遍分类逻辑，而且两处必然判得不一样。
_ASSET_TYPE_ALIAS = {"spreadsheet": "table"}

# 长期未使用清单上限：原型「长期未使用资产 / 知识」两张表都是概览卡
# （`pagination={false}`），给全量会把页面撑爆 —— 长期未使用天然是长尾。
_UNUSED_LIST_LIMIT = 20
# 高频引用 / 高频使用榜单长度（原型 Top10）
# 排序契约：次数 DESC → 最近一次引用/命中时间 DESC → 稳定兜底键（资产 id / 实体三元组）。
# 第二键是需求方明确要求的「同次数按最新引用排前」，**不要**改回按 id / 名称定序。
_TOP_LIST_LIMIT = 10
# 带 Neo4j 存活回捞的榜单多取候选：回捞被丢弃的名额由后续候选补上
_TOP_CANDIDATE_FACTOR = 6
# Neo4j 实体全集有界拉取条数：长期未使用 = 全集 − 用过的，是补集，
# 无法像 active_entities 那样反转取数方向，只能有界拉取后在 Python 侧求差。
# ⚠️ 拉取顺序见 `_neo4j_entities_head`（按创建时间**升序**）：样本要尽量落在
# 「创建满 N 天」那一侧，否则年龄闸会把样本滤空、榜内条数系统性偏少。
# 仍然是有界的**近似**：库里实体多于这个数时，榜只反映其中最老的一批。
_NEO4J_ENTITY_HEAD_LIMIT = 500
# 抽屉 B「主要关系」最多展开多少条邻居事实。抽屉只展示一组组目标名，200 已远超
# 一屏；真正的总数由 `neighbors_by_entity` 单独 count 返回（不靠这个上限凑），
# 被截断时 `relation_truncated=true`，前端提示「仅显示前 N 条」。
_NEO4J_RELATION_LIMIT = 200
# 关系类型缺失时的兜底组名（Neo4j 的 `r.relation_type` 理论上必填，但历史数据
# 有写空串的，分组时给出 `None` 会让前端渲染出 `undefined`）
_RELATION_TYPE_FALLBACK = "未分类"

# ⚠️ `degraded` 当前**没有生产者**（reasoning_trace 只产出 running/done/skipped/failed）。
# 保留它是把「参与率分子」的口径做成超集：将来真出现「跑过但降级」的步骤无需再改这里，
# 而现在多这一个字面量不会误判任何行（库里没有行是 degraded）。
_STATUS_DEGRADED = "degraded"
# 参与率分子计入的步骤状态：`skipped` 不计入
_COUNTED_STEP_STATUSES = (STATUS_DONE, STATUS_FAILED, _STATUS_DEGRADED)

# ── 参与率三栏的 stage 白名单（下面逐条写了理由，不要自行增删）──
# ① `ref_retrieve` 三栏都不计：它是「本体成功后取 chunk 引用」，算进 RAG 会把
#    本体成功的请求重复计入 RAG 栏，这个重叠没有业务含义；
# ② 全仓没有 `rag_fallback` / `rerank` 这两个 stage 常量（`rerank` 只是
#    `_SCORE_KIND_RERANK` 这个分数类型），不得写进代码；
# ③ `route_decision` 一般不算（它是路由判定），但 `detail.route = 'rag'` 是
#    「本体路径真降级到 RAG」的唯一结构化信号，故作为 RAG 的附加判据。
_STAGES_ONTOLOGY = (STAGE_ONTOLOGY_MATCH, STAGE_FACT_LOOKUP, STAGE_TIMELINE_GRAPH, STAGE_ARBITRATION)
_STAGES_WIKI = (STAGE_OPENKB_QUERY, STAGE_JONEX_WIKI_QUERY)
_STAGES_RAG = (STAGE_CONTEXT_RETRIEVE, STAGE_CHUNK_ANSWER, STAGE_LLM_ANSWER)

# ── 窗口与分页限幅（模块级：正常路径与降级路径共用同一套规则）──
_DAYS_DEFAULT = 7
_DAYS_MIN = 1
_DAYS_MAX = 365
# 自定义区间（start_date ~ end_date，含首尾）的跨度上限。与 `_DAYS_MAX` 同量级：
# 再长的话日粒度趋势点太密，而且 `_neo4j_stats` 要把图内**全部**实体的 created_at
# 拉回 Python 再按日期分桶，区间越大这步越贵。
_RANGE_MAX_DAYS = 366
_UNUSED_DAYS_DEFAULT = 30
_PAGE_DEFAULT = 1
_PAGE_SIZE_DEFAULT = 20
_PAGE_SIZE_MAX = 100

# 异常资产下钻的 stage 值域与展示顺序（对齐前端 DoStage）
_STAGE_VALUES = (TASK_TYPE_PARSE, TASK_TYPE_EXTRACT, TASK_TYPE_COMPILE)
_STAGE_ORDER = (TASK_TYPE_PARSE, TASK_TYPE_EXTRACT, TASK_TYPE_COMPILE)

# LLM-Wiki 编译状态列（`llm_wiki_compile_status`）的「已编译」值：裸字符串列，
# 无 Enum 可引用，与 `processing_task_repository.COMPILE_STATUS_MAP` 的键保持一致。
_WIKI_COMPILED = "compiled"

# 失败原因兜底文案：`processing_task_event.error` 可空
_REASON_FALLBACK = "未提供失败原因"


def classify_asset_category(mime_type: str | None, file_name: str | None) -> str:
    """资产构成分类：五个「解析器类目」优先，其余落回配额分类（structured / 表格 / other）。

    ⚠️ `file_name` **必须传**：`.docx`/`.json`/`.yaml` 这类文件常常只靠扩展名
    才能识别（mime 是 `application/octet-stream`），只传 mime 会让它们永远进不了
    正确类别（一期就是把 `file_name` 传了 `None`）。另注意**必须是完整文件名**：
    `os.path.splitext('.docx')` 返回 `('.docx', '')`（前导点被当成隐藏文件名），
    只给「含点后缀」会解析出空扩展名 —— 见 `_composition` 的调用点。

    判定顺序是**契约**（五类后缀表 → structured → text(mime) → 配额兜底），不要调换：
    ① 先查 `_PARSER_CATEGORY_EXT`，因为 `.json` 既在解析配置的文本解析器里、
       又长得像结构化数据，必须先被「文本」拿走；
    ② 剩下 `_STRUCTURED_EXT` 里的 `.xml/.yaml/.sql/...` 才归 structured（隐藏类）；
    ③ 无扩展名时用 mime 兜一层纯文本；
    ④ 其余（`.csv/.ods/...`）交给配额分类，保持一期行为不变。
    """
    mt = (mime_type or "").strip().lower()
    ext = os.path.splitext(file_name or "")[1].lower()
    for category, suffixes in _PARSER_CATEGORY_EXT:
        if ext in suffixes:
            return category
    if mt in _STRUCTURED_MIME or ext in _STRUCTURED_EXT:
        return "structured"
    if mt in _TEXT_MIME:
        return "text"
    return classify_quota_category(mime_type, file_name)


def to_asset_type(category: str) -> str:
    """后端 8 类 `category` → 前端 7 类 `asset_type`（表格：spreadsheet → table）。

    `other` 照直输出：前端 `DoAssetType` 没有这一类，由前端做兜底展示位。
    """
    return _ASSET_TYPE_ALIAS.get(category, category)


def _file_format(mime_type: str | None, file_name: str | None) -> str | None:
    """抽屉 A「文件格式」展示串：优先扩展名，取不到才回落 mime 子类型。

    为什么在后端算：① 扩展名大小写规范（`pdf` → `PDF`）是全站展示细节，前端按
    mime 猜会各处不一致；② `application/vnd.openxmlformats-...` 这类 mime 直接显示
    没法看；③ 前端 `file_name` 可能已被截断（只用来展示），不能反推扩展名。

    两者都取不到返回 `None`（前端渲染 `—`），**不要**回落 `"未知"` 这类字面量 ——
    文案归前端。
    """
    ext = os.path.splitext(file_name or "")[1].lstrip(".").strip()
    if ext:
        # `.tar.gz` 这类复合扩展名只取最后一段（`GZ`），够用且不会长得没法看
        return ext.upper()[:16]
    main = (mime_type or "").split(";")[0].strip()
    if "/" in main:
        # `application/vnd.ms-excel` → `VND.MS-EXCEL`，难看但比空白有信息量
        sub = main.split("/", 1)[1].strip()
        return sub.upper()[:32] or None
    return None


def _attr_pairs(raw) -> list:
    """Neo4j 实例 `attributes` → `[{"key", "value"}]`（抽屉 B「核心属性」）。

    `value` 一律转字符串：图里存的类型不定（数字、bool、嵌套 dict 都有），前端那一列
    只是文本；`None` 给**空串**（不给 `"None"`，那会被当成真值显示出来）。

    ⚠️ **不补任何默认键**。原型对缺属性的实例显示「业务编码 = —、负责人 = —」，
    那是**前端**的兜底模板 —— 后端补进去就再也分不清「图里确实没有」和「后端编的」。
    """
    if not isinstance(raw, dict):
        return []
    pairs = []
    for key, value in raw.items():
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value
        elif isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        else:
            text = json.dumps(value, ensure_ascii=False)
        pairs.append({"key": str(key), "value": text})
    return pairs


def _relation_groups(facts: list) -> list:
    """一跳邻居事实 → 按 `relation_type` 归组（抽屉 B「主要关系」）。

    正反向边**合并成同一组**：原型那几组（发行方 / 适用客户 / 关联风险）不分方向，
    拆成两组会出现两个「发行方」。去重由仓储层负责（同一 (类型, 目标) 只留一条），
    这里只做归组。

    分组顺序 = 事实的到达顺序（仓储侧按 `relation_type` 排过），目标同理；
    不再按名称重排 —— 重排会把「高频/高置信的目标」挤到后面。
    """
    groups: dict = {}
    for fact in facts:
        relation_type = str(fact.get("relation_type") or _RELATION_TYPE_FALLBACK)
        target = fact.get("target")
        if not target:
            continue
        group = groups.get(relation_type)
        if group is None:
            group = {"relation_type": relation_type, "targets": []}
            groups[relation_type] = group
        group["targets"].append({
            "canonical_name": target,
            "entity_type": fact.get("target_type"),
        })
    return list(groups.values())


# ══════════════════════════════════════════════════════════════
# 参数解析（模块级函数：正常路径与降级路径共用，避免两处限幅规则漂移）
# ══════════════════════════════════════════════════════════════


def _clamp_int(raw, default: int, lo: int, hi: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _parse_stages(raw) -> list:
    """解析 `stage` 筛选：接受 list / 逗号分隔串 / 单值，只保留合法值（保序去重）。"""
    if raw is None or raw == "":
        return []
    items = []
    if isinstance(raw, (list, tuple, set, frozenset)):
        for part in raw:
            items.extend(str(part or "").split(","))
    else:
        items = str(raw).split(",")
    out = []
    for item in items:
        value = item.strip()
        if value in _STAGE_VALUES and value not in out:
            out.append(value)
    return out


def _parse_bool(raw, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_date(raw):
    """解析 `start_date` / `end_date`：接受 `date` / `datetime` 对象或 ISO `YYYY-MM-DD` 串。

    非法值返回 `None`（**不抛**）—— 「参数不合法要 400」是网关的职责（那儿面向客户端）；
    能力层还会被内部调用方经 invoke 打进来，不该因为一个新参数给不出就 500。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_range(data: dict):
    """解析自定义区间，返回 `(start, end)`；不成对就给 `(None, None)`。

    **成对才有意义**：只给一边、或给了个反的区间（start > end），都当没传 ——
    退回 `days` 的相对窗口。这里只判「能不能用」，不判「该不该拒」（那是网关的事）。

    跨度超过 `_RANGE_MAX_DAYS` 时把右界夹到上限并留 WARNING：正常入口（网关）会在
    更早的地方 400 掉，能走到这儿的只有内部调用方 —— 夹一下保证 `window_dates` 有界，
    比返回一个几千点的时间轴安全。
    """
    start = _parse_date(data.get("start_date"))
    end = _parse_date(data.get("end_date"))
    if start is None or end is None or start > end:
        return None, None
    if (end - start).days + 1 > _RANGE_MAX_DAYS:
        logger.warning(
            "[jonex] 数据运营自定义区间超上限（%s ~ %s），右界夹到 %s 天",
            start, end, _RANGE_MAX_DAYS,
        )
        end = start + timedelta(days=_RANGE_MAX_DAYS - 1)
    return start, end


def _parse_scope(data: dict) -> dict:
    """解析「范围 + 窗口 + 分页 + 筛选」全部入参，一次解析、全链路复用。

    返回 dict 而不是元组：这些字段要一路透传到降级路径的空壳构造器，
    位置元组在加字段时极易错位 —— 一期 `_parse` 返回 4 元组、三个公开方法都用 `_`
    把 `unused_threshold_days` 接掉，前端那个 30/90 天切换器因此一直是死的。
    """
    data = data or {}
    start_date, end_date = _parse_range(data)
    return {
        "domain_space_id": data.get("domain_space_id") or None,
        "kb_id": data.get("kb_id") or None,
        # ── 三期钻取详情（抽屉 A/B）的定位键 ──
        # 与 `kb_id` 同类：一路透传到降级空壳构造器（空壳也要把定位键原样回显，
        # 前端拿到的响应结构在降级/正常两条路上逐字段一致）。
        # 抽屉 C 不在这里 —— 它的定位键就是 `kb_id`；抽屉 D 的定位键是 `service_id`。
        "asset_id": data.get("asset_id") or None,
        "entity_type": data.get("entity_type") or None,
        "canonical_name": data.get("canonical_name") or None,
        "service_id": data.get("service_id") or None,
        "days": _clamp_int(data.get("days"), _DAYS_DEFAULT, _DAYS_MIN, _DAYS_MAX),
        # 自定义区间：成对给出时**覆盖** `days`（窗口右界不再等于今天）。
        # 只给一边 / 给反了 → 这里已经是 (None, None)，自然退回 `days`。
        "start_date": start_date,
        "end_date": end_date,
        "unused_threshold_days": _clamp_int(
            data.get("unused_threshold_days"), _UNUSED_DAYS_DEFAULT, _DAYS_MIN, _DAYS_MAX
        ),
        "page": _clamp_int(data.get("page"), _PAGE_DEFAULT, 1, 10**6),
        "page_size": _clamp_int(data.get("page_size"), _PAGE_SIZE_DEFAULT, 1, _PAGE_SIZE_MAX),
        "stages": _parse_stages(data.get("stage")),
        # 异常资产下钻的 handled 筛选：默认只看未处置的
        "handled": _parse_bool(data.get("handled"), default=False),
    }


def _resolve_window(today: date, days: int, start, end):
    """把「相对窗口 `days`」与「绝对区间 `start`~`end`」归一成同一个三元组。

    返回 `(win_start, win_end, window_dates)`：`win_end` 是**最后一天 +1 天的开区间右界**
    （SQL 一律写 `>= win_start AND < win_end`，写成 `<= 末日` 会把末日整段截掉）；
    `window_dates` 是升序日期串，供趋势补零。

    自定义区间优先：`start`/`end` 成对给出时完全按它算、`days` 被忽略 ——
    「右界不等于今天」正是本次扩展要支持的能力，`days` 表达不了。

    正常路径（`_scope_window`）与降级路径（`_scope_window_dates`）共用本函数，
    日期轴口径只有一处 —— 否则降级壳的点数与真数据对不上，前端要为此单写分支。
    """
    if start is not None and end is not None:
        return (
            start,
            end + timedelta(days=1),
            [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)],
        )
    return (
        today - timedelta(days=days - 1),
        today + timedelta(days=1),
        [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)],
    )


def _scope_window_dates(scope: dict) -> list:
    """降级空壳用的日期轴 —— **降级路径专用**：正常路径的日期取自 DB `current_date()`。

    降级空壳也要给出「日期点数正确」的趋势（自定义区间下点数同样得对），
    前端就不必为降级单写分支；容器与 DB 同为 `Asia/Shanghai`，
    本地 `date.today()` 与 DB `current_date()` 同一天。
    """
    _, _, window_dates = _resolve_window(
        date.today(),
        scope.get("days", _DAYS_DEFAULT),
        scope.get("start_date"),
        scope.get("end_date"),
    )
    return window_dates


# ══════════════════════════════════════════════════════════════
# SQL 片段（模块级常量：多处复用，口径只有一处）
# ══════════════════════════════════════════════════════════════


def _sql_list(values) -> str:
    """把**代码内常量**渲染成 SQL 字面量列表（如 `'ready', 'failed'`）。

    只用于本文件写死的状态/阶段常量，**绝不用于外部输入**
    （租户、kb_ids、查询词一律走绑定参数）。
    """
    return ", ".join(f"'{v}'" for v in values)


# ── 请求级 CTE（检索分析的核心：空间过滤 / searched_kbs / kb_usage /
#    服务归因 / 参与率分母都复用它）──
# `knowledge_base_ids` 为空时按 `entry_service_id` 反查 `service_knowledge_bases`
# 回填 —— 领域服务形态的入口只有 service_id，不补这一步服务检索会被整段丢掉。
_REQ_CTE = """
WITH req AS (
    SELECT re.request_id, re.tenant_id, re.user_id, re.query_hash, re.query,
           re.received_at, re.entry_service_id,
           CASE WHEN jsonb_array_length(re.knowledge_base_ids) > 0
                THEN re.knowledge_base_ids
                ELSE coalesce((
                    SELECT jsonb_agg(skb.kb_id)
                      FROM knowledge_base.service_knowledge_bases skb
                     WHERE skb.service_id = re.entry_service_id
                       AND skb.tenant_id  = re.tenant_id
                       AND skb.is_deleted = 0
                ), '[]'::jsonb)
           END AS kb_ids
      FROM knowledge_base.search_request_event re
     WHERE re.tenant_id = :tenant_id
       AND re.is_deleted = 0
       AND re.received_at >= CAST(:win_start AS DATE)
       AND re.received_at <  CAST(:win_end AS DATE)
)
"""

# 请求级范围谓词：① 至少命中范围内一个库；② 排除匿名检索。
# `user_id` 可空（MCP / API Key 调用），`NULL <> 'anonymous'` 求值为 NULL 会把这些行
# 整段丢掉，故必须显式写成 `IS NULL OR <>` —— 最容易漏、且漏了不报错的一处。
_REQ_IN_SCOPE = """
       EXISTS (
           SELECT 1 FROM jsonb_array_elements_text(r.kb_ids) AS x(kb)
            WHERE x.kb = ANY(:kb_ids)
       )
       AND (r.user_id IS NULL OR r.user_id <> 'anonymous')
"""

# ── 领域服务归因（请求级）：把「一次检索经哪个服务发起」展开成 (request_id, service_id) ──
# **列表与抽屉 D 共用这一份**（原先内联在 `_search_agg` 里，抽屉要复用时抽到模块级）：
# 两个方向都要算，缺一不可 ——
#   ① kb → service 映射（一个库可能挂在多个服务下）；
#   ② 请求自带的 `entry_service_id`（领域服务形态的入口只有它，无 kb 的请求
#      也只剩这一条归因线索）。
# UNION 自带去重；上层一律再套 `count(DISTINCT request_id)`
# （`search_request_event` 无唯一约束，重复行会虚增）。
# ⚠️ 少写一支就会让列表与抽屉同时少算（口径同源的好处），但**改这里等于同时改列表**。
_PAIR_CTE = _REQ_CTE + f"""
, pair AS (
    SELECT r.request_id, r.tenant_id, skb.service_id
      FROM req r
      CROSS JOIN LATERAL jsonb_array_elements_text(r.kb_ids) AS x(kb)
      JOIN knowledge_base.service_knowledge_bases skb
        ON skb.kb_id = x.kb
       AND skb.tenant_id = r.tenant_id
       AND skb.is_deleted = 0
     WHERE x.kb = ANY(:kb_ids)
       AND (r.user_id IS NULL OR r.user_id <> 'anonymous')
    UNION
    SELECT r.request_id, r.tenant_id, r.entry_service_id
      FROM req r
     WHERE {_REQ_IN_SCOPE}
)
"""

# ── 文档级 CTE（处理线三块复用）──
# 租户 + 未删除 + kb 范围一个都不能少：这三列是 `knowledge_documents` 的列，
# 写原生 SQL 时最容易漏掉租户与 is_deleted（`BaseRepository.count()` 会统一加，
# 手写 SQL 没有这层保护）。
_SCOPED_DOC_CTE = """
WITH scoped AS (
    SELECT d.id, d.knowledge_base_id, d.file_name, d.mime_type,
           d.status, d.ontology_status, d.llm_wiki_compile_status,
           d.data_source_type
      FROM knowledge_base.knowledge_documents d
     WHERE d.tenant_id = :tenant_id
       AND d.is_deleted = 0
       AND d.knowledge_base_id = ANY(:kb_ids)
)
"""

# ── 四级漏斗（状态列 ∪ 事件表）──
# `parsed/extracted/compiled` 逐级 AND 上一级，用构造保证单调（不会出现
# 「已编译 > 已抽取」的凹陷）。事件表提供「曾经成功过」这个 append-only 维度
# （reparse 重置状态列也抹不掉），状态列兜底覆盖；事件表为空时自动退化为纯状态列口径。
_PIPELINE_SQL = _SCOPED_DOC_CTE + f"""
, ok AS (
    SELECT p.asset_id,
           bool_or(p.task_type = '{TASK_TYPE_PARSE}')   AS ok_parse,
           bool_or(p.task_type = '{TASK_TYPE_EXTRACT}') AS ok_extract,
           bool_or(p.task_type = '{TASK_TYPE_COMPILE}') AS ok_compile
      FROM knowledge_base.processing_task_event p
     WHERE p.tenant_id = :tenant_id
       AND p.is_deleted = 0
       AND p.status = '{TASK_STATUS_SUCCESS}'
       AND p.asset_id IN (SELECT id FROM scoped)
     GROUP BY p.asset_id
), joined AS (
    SELECT s.knowledge_base_id,
           (s.status IN ({_sql_list((DocStatus.INGESTING.value, DocStatus.READY.value))})) AS parsed,
           s.ontology_status,
           s.llm_wiki_compile_status,
           coalesce(ok.ok_extract, false) AS ok_extract,
           coalesce(ok.ok_compile, false) AS ok_compile
      FROM scoped s
      LEFT JOIN ok ON ok.asset_id = s.id
)
SELECT knowledge_base_id AS kb_id,
       count(*) AS total,
       count(*) FILTER (WHERE parsed) AS parsed,
       count(*) FILTER (
           WHERE parsed AND (ontology_status = '{OntologyStatus.READY.value}' OR ok_extract)
       ) AS extracted,
       count(*) FILTER (
           WHERE parsed AND (ontology_status = '{OntologyStatus.READY.value}' OR ok_extract)
             AND (llm_wiki_compile_status = '{_WIKI_COMPILED}' OR ok_compile)
       ) AS compiled
  FROM joined
 GROUP BY knowledge_base_id
"""

# ── 流水任务状态分布 + 平均耗时（后端预计算，不让前端从被截断的列表里算）──
# 平均耗时**只统计 success**（前后端约定死的口径）：耗时只有成功的任务有可比性，
# 失败任务的耗时取决于它多久才炸、会把这个数往下拉。用 `FILTER` 而不是「调用方挑
# success 那一行」—— GROUP BY status 后没有 ORDER BY，非 success 分组同样会算出 avg，
# 谁排在前面由 PG 决定，`next(非空)` 那样写在数据分布变化时会**静默换成别的状态的口径**。
# ⚠️ 用 FILTER 后非 success 分组的 `avg_sec` 恒 NULL，这正是调用方 `next(非空)` 能取到
# success 那一行的原因 —— 两处是配套的，不要单独改其中一处。
# `pending` 恒 0，在 Python 侧补（见 _pipeline_agg）。
_TASK_STATS_SQL = _SCOPED_DOC_CTE + f"""
SELECT p.status AS status,
       count(*) AS cnt,
       avg(EXTRACT(EPOCH FROM (p.finished_at - p.started_at)))
           FILTER (WHERE p.status = '{TASK_STATUS_SUCCESS}') AS avg_sec
  FROM knowledge_base.processing_task_event p
  JOIN scoped s ON s.id = p.asset_id
 WHERE p.tenant_id = :tenant_id
   AND p.is_deleted = 0
 GROUP BY p.status
"""

# ── 任务明细（按开始时间倒序，≤ limit 条）──
# 事件表没有 duration 列，`duration_ms` 由两端时间现算（只对拿到明细的这几十行算）。
_TASKS_SQL = _SCOPED_DOC_CTE + """
SELECT p.processing_task_id, p.asset_id, p.task_type, p.status, p.error,
       p.started_at, p.finished_at,
       s.file_name, s.mime_type, s.knowledge_base_id, s.data_source_type,
       CASE WHEN p.started_at IS NOT NULL AND p.finished_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM (p.finished_at - p.started_at)) * 1000
       END AS duration_ms
  FROM knowledge_base.processing_task_event p
  JOIN scoped s ON s.id = p.asset_id
 WHERE p.tenant_id = :tenant_id
   AND p.is_deleted = 0
 ORDER BY coalesce(p.started_at, p.finished_at, p.created_at) DESC, p.processing_task_id DESC
 LIMIT :limit
"""

# ── 异常资产（按失败**任务**计数，不是按资产）──
# `handled` = 同一 `(asset_id, task_type)` 之后存在更新的成功任务。
# ⚠️ 必须按 `(asset_id, task_type)` 分区 —— 写成「该资产存在任何成功任务」的话，
# parse 失败后 compile 成功会被误判成「已处置」，与那次失败毫无关系。
# 窗口函数在 `fails` CTE 内算好，外层再按 `status='failed'` 过滤，
# 所以成功任务也参与分区计算（不会被这一步过滤掉）。
_ABNORMAL_CTE = _SCOPED_DOC_CTE + f"""
, fails AS (
    SELECT p.processing_task_id, p.asset_id, p.task_type, p.status, p.error,
           s.file_name, s.mime_type, s.knowledge_base_id, s.data_source_type,
           coalesce(p.finished_at, p.started_at, p.created_at) AS occurred_at,
           max(CASE WHEN p.status = '{TASK_STATUS_SUCCESS}'
                    THEN coalesce(p.finished_at, p.started_at, p.created_at) END)
               OVER (PARTITION BY p.asset_id, p.task_type) AS last_success_at,
           count(*) FILTER (WHERE p.status = '{TASK_STATUS_FAILED}')
               OVER (PARTITION BY p.asset_id, p.task_type) AS retry_count
      FROM knowledge_base.processing_task_event p
      JOIN scoped s ON s.id = p.asset_id
     WHERE p.tenant_id = :tenant_id
       AND p.is_deleted = 0
)
"""

# 已处置判据（明细与汇总共用，口径只有一处）
_HANDLED_TRUE = "(last_success_at IS NOT NULL AND last_success_at > occurred_at)"
_HANDLED_FALSE = "(last_success_at IS NULL OR last_success_at <= occurred_at)"
_ABNORMAL_WHERE_FAILED = f" WHERE status = '{TASK_STATUS_FAILED}'"

# 汇总：按 stage 分组，只数**未处置**的
_ABNORMAL_SUMMARY_SQL = _ABNORMAL_CTE + f"""
SELECT task_type,
       count(*) AS total_failed,
       count(*) FILTER (WHERE {_HANDLED_FALSE}) AS unhandled
  FROM fails
{_ABNORMAL_WHERE_FAILED}
 GROUP BY task_type
"""

# 文档级「最近一次被采用」子查询（长期未使用资产用）。
# ⚠️ 这里**不能**加时间窗口：`last_cited_at = NULL` 的语义是「从未使用」，
# 若按窗口裁剪，一个 40 天前被引用过的资产会变成「从未使用」。
_LAST_CITED_SUB = f"""
        (SELECT a.target_id AS doc_id, max(a.adopted_at) AS last_cited_at
           FROM knowledge_base.search_adoption_event a
          WHERE a.tenant_id = :tenant_id
            AND a.is_deleted = 0
            AND a.target_type = '{TARGET_DOCUMENT}'
            AND a.kb_id = ANY(:kb_ids)
          GROUP BY a.target_id)
"""

# 长期未使用资产清单。
#
# 判据是**两半取交集**（缺任意一半都会出假阳性）：
#   ① 年龄：`d.created_at`（上传时间）早于「今天 − threshold_days」；
#   ② 未被使用：`last_cited_at` 为空（从未引用）或早于同一个阈值。
# 只判 ② 的后果是「今天刚上传、一次没被引用过」的文档当场进榜；只判 ① 则把
# 「天天被引用的老资产」也算成未使用。只判 ② 时 30/90 两档对「从未引用」那半
# 还是同一个空判（`last_cited_at IS NULL` 与阈值无关），所以 ① 是它真生效的前提。
#
# ⚠️ 年龄那半边**用应用进程的时钟**（`:created_before`，Python 侧
# `datetime.now() - timedelta(days=...)` 算的 naive 本地时间），**不要**写成
# SQL 的 `created_at < now() - INTERVAL ...`：
# `d.created_at` 是 `TimestampMixin` 写的**无时区** `TIMESTAMP`（本地墙钟），
# `now()` 是 `timestamptz`，PG 会按**会话 `TimeZone`** 把左边隐式转成 timestamptz
# 再比 —— 只在「PG 会话时区 == 写入方时区」时才正确（本机靠 compose 的 `TZ` 撑着，
# 官方 postgres 镜像默认 UTC，换一套部署就**静默差 8 小时**）。
# 这与 `_local_naive_to_iso` 立的是同一条规矩（见该函数注释）：naive 列的
# 还原/比较都必须用**写入方（应用进程）的时区**。同理 **CAST 目标是 `timestamp`
# 而不是 `timestamptz`**，写成后者等于把时区又塞回来。
# 注：`last_cited_at` 那半边仍是 timestamptz，用 DB 的 `now()` 是对的 ——
# 两条比较用两个「此刻」（应用进程 vs DB），差在毫秒级，阈值是 30/90 天，可忽略。
#
# ⚠️ 这里**不**做时区换算（列保持裸的），换算在 Python 侧由 `_local_naive_to_iso`
# 负责 —— 理由同上。
#
# `ORDER BY` 用裸列：它只在库内比较，与时区无关。
_UNUSED_ASSETS_SQL = f"""
SELECT d.id AS asset_id, d.file_name, d.mime_type, d.knowledge_base_id,
       d.data_source_type,
       d.updated_at, d.created_at,
       m.last_cited_at
  FROM knowledge_base.knowledge_documents d
  LEFT JOIN {_LAST_CITED_SUB} m ON m.doc_id = d.id
 WHERE d.tenant_id = :tenant_id
   AND d.is_deleted = 0
   AND d.status = '{DocStatus.READY.value}'
   AND d.knowledge_base_id = ANY(:kb_ids)
   AND d.created_at < CAST(:created_before AS timestamp)
   AND (m.last_cited_at IS NULL
        OR m.last_cited_at < (now() - CAST(:threshold_days AS int) * INTERVAL '1 day'))
 ORDER BY m.last_cited_at ASC NULLS FIRST, d.updated_at DESC, d.id ASC
 LIMIT {_UNUSED_LIST_LIMIT}
"""


def _local_naive_to_iso(value):
    """**应用本地时间**的 naive datetime → 带时区偏移的 ISO 8601 字符串。

    ⚠️ 这不是「顺手格式化一下」，是修一个**静默、前端可见**的 bug：

    `knowledge_documents.created_at/updated_at` 由 `TimestampMixin` 用
    `datetime.now()` 写入，是**无时区**的本地 `TIMESTAMP`。asyncpg 原样返回
    naive datetime → `.isoformat()` 得到 `2026-09-09T17:20:53.363655` 这种
    **没有偏移量的裸串**。前端 `new Date(...)` 对裸串按**浏览器本地时区**解析，
    而同一个响应里其余 `*_at` 都带偏移（DB 的 `timestamptz` 是 `+00:00`、
    Neo4j 毫秒是 `+08:00`）—— 偏移量一旦不是浏览器时区，这个字段就**显示错**，
    而接口形状不变、不报错、日志干净。

    **为什么是 `.astimezone()` 而不是 SQL 里的 `AT TIME ZONE current_setting('TimeZone')`**：
    值的产生方是**应用进程**（`datetime.now()`），要还原就必须用**应用进程的**时区
    —— 那正是 `.astimezone()` 的语义（naive 视为系统本地时间）。SQL 那个写法把还原
    时区寄托在**PG 会话的 `TimeZone`** 上，只在「PG 默认时区恰好等于应用时区」时才
    成立（本机两者都是 `Asia/Shanghai`，靠的是 compose 里设了 `TZ`；官方 postgres 镜像
    默认 UTC，换一套部署就会**静默差 8 小时**）。⚠️ 不要为了「统一风格」把它改回 SQL。

    另注：本函数**只适用于 `TimestampMixin` 这类本地 naive 列**。事件表
    （`processing_task_event` 等）的 `*_at` 是 `TIMESTAMPTZ`、由 DB 的 `NOW()` 填
    （见 `processing_task_repository.py` 的时间戳口径注释），asyncpg 返回的就是
    带 UTC 偏移的 aware datetime，**不要**再过这个函数。
    """
    return value.astimezone().isoformat() if value else None


# ══════════════════════════════════════════════════════════════
# 降级保护层（非预期异常 → 等结构空壳，绝不 5xx）
# ══════════════════════════════════════════════════════════════


def _resilient(empty_factory):
    """聚合方法降级装饰器。

    - **结构完整**：降级返回值与正常返回逐字段对齐（空壳构造器负责），
      前端不需要为降级单独写分支；
    - **绝不 5xx**：非预期异常 → ERROR 日志（带堆栈）→ 返回零值空壳；
    - **不吞 4xx**：`JonexException` 继续向上抛 —— 租户非法、权限不足、参数非法
      都是 4xx 语义，被降级掩盖会变成「静默返回空数据」，比报错更难排查。
    """

    def decorator(method):
        @wraps(method)
        async def wrapper(self, tenant_id: str, user_id, data: dict):
            try:
                return await method(self, tenant_id, user_id, data)
            except JonexException:
                raise
            except Exception:  # noqa: BLE001 — 聚合失败不得变成 5xx，但必须留日志
                logger.error(
                    "[jonex] 数据运营聚合降级（返回空壳）：method=%s tenant=%s",
                    method.__name__, tenant_id, exc_info=True,
                )
                try:
                    scope = _parse_scope(data)
                except Exception:  # noqa: BLE001 — 连参数都解析不了时给最保守的空壳
                    scope = {"days": _DAYS_DEFAULT, "page": _PAGE_DEFAULT,
                             "page_size": _PAGE_SIZE_DEFAULT}
                return empty_factory(scope)

        return wrapper

    return decorator


def _empty_pipeline_usage() -> dict:
    return {
        "valid_requests": 0,
        "ontology": {"count": 0, "rate": 0.0},
        "rag": {"count": 0, "rate": 0.0},
        "wiki": {"count": 0, "rate": 0.0},
    }


def _empty_search(data: dict) -> dict:
    return {
        "total_requests": 0,
        "active_users": 0,
        "avg_requests_per_user": None,
        "searched_kbs": 0,
        "total_kbs": 0,
        "unused_kbs": 0,
        "used_services": 0,
        "request_trend": [{"date": d, "count": 0} for d in _scope_window_dates(data)],
        "pipeline_usage": _empty_pipeline_usage(),
        "top_questions": [],
        "question_topics": [],
        "kb_usage": [],
        "service_usage": [],
    }


def _empty_overview(data: dict) -> dict:
    return {
        "data_assets": {
            "total_assets": 0,
            "total_data_sources": 0,
            "cited_assets": 0,
            "usage_rate": 0.0,
        },
        "knowledge_assets": {
            "ontology_entities": 0,
            "ontology_relations": 0,
            "wiki_entities": 0,
            "wiki_concepts": 0,
        },
        "search_analytics": {
            "total_requests": 0,
            "searched_kbs": 0,
            "total_kbs": 0,
            "used_services": 0,
            "unused_kbs": 0,
            "active_users": 0,
        },
    }


def _empty_data_assets(data: dict) -> dict:
    return {
        "total_assets": 0,
        "total_data_sources": 0,
        "cited_assets": 0,
        "usage_rate": 0.0,
        "new_assets_in_window": 0,
        "new_data_sources_in_window": 0,
        "access_trend": [{"date": d, "count": 0} for d in _scope_window_dates(data)],
        "composition": [
            {"category": c, "asset_type": to_asset_type(c), "count": 0}
            for c in _COMPOSITION_CATEGORIES
        ],
        "top_cited_assets": [],
        "unused_assets": [],
        "processing_pipeline": {"total": 0, "parsed": 0, "extracted": 0, "compiled": 0},
        "pipeline_task_stats": {
            "success": 0, "failed": 0, "running": 0, "pending": 0, "avg_duration_ms": None,
        },
        "processing_tasks": [],
        "abnormal_summary": {
            "total": 0,
            "by_stage": [{"stage": s, "count": 0} for s in _STAGE_ORDER],
        },
        "abnormal_assets": [],
    }


def _empty_knowledge_assets(data: dict) -> dict:
    return {
        "ontology": {"entities": 0, "relations": 0, "active_entities": 0, "activity_rate": 0.0},
        "wiki": {"entities": 0, "concepts": 0, "active_pages": 0, "activity_rate": 0.0},
        "entity_type_distribution": [],
        "kb_entity_distribution": [],
        "entity_growth_trend": [{"date": d, "count": 0} for d in _scope_window_dates(data)],
        "top_used_entities": [],
        "unused_entities": [],
    }


def _empty_abnormal_page(data: dict) -> dict:
    return {
        "items": [],
        "total": 0,
        "page": data.get("page", _PAGE_DEFAULT),
        "page_size": data.get("page_size", _PAGE_SIZE_DEFAULT),
    }


def _empty_quality_anomalies(data: dict) -> dict:
    """解析质量异常（观测档）的降级空壳。"""
    return {"items": [], "total": 0}


# 三期三个钻取空壳：**定位键原样回显**（`asset_id` / `kb_id` + 三元组），
# 名称为空串而不是 `None` —— 前端拿到的是「结构完整但内容为空」，
# 与其它空壳一样不必为降级单写分支。⚠️ 只有「非预期异常」会走到这里；
# 「资产/实例/知识库不存在」走 404（`JonexException` 不被降级层吞掉）。


def _empty_asset_detail(data: dict) -> dict:
    return {
        "asset_id": data.get("asset_id") or "",
        "file_name": "",
        "asset_type": None,
        "file_format": None,
        "file_size": None,
        "source": None,
        "kb_id": data.get("kb_id") or None,
        "kb_name": None,
        "uploaded_at": None,
        "last_processed_at": None,
        "status": None,
        "ontology_status": None,
        "llm_wiki_compile_status": None,
        "cite_count": 0,
        "last_cited_at": None,
    }


def _empty_ontology_entity_detail(data: dict) -> dict:
    return {
        "kb_id": data.get("kb_id") or "",
        "kb_name": None,
        "canonical_name": data.get("canonical_name") or "",
        "entity_type": data.get("entity_type") or "",
        "aliases": [],
        "description": None,
        "created_at": None,
        "updated_at": None,
        "attributes": [],
        "relation_total": 0,
        "relation_groups": [],
        "relation_truncated": False,
        "hit_count": 0,
        "adopted_count": 0,
        "last_used_at": None,
        "degraded": True,
    }


def _empty_kb_detail(data: dict) -> dict:
    return {
        "kb_id": data.get("kb_id") or "",
        "kb_name": "",
        "kb_type": None,
        "ontology": {"object_instances": 0, "relation_instances": 0, "degraded": True},
        "asset_total": 0,
        "assets_by_type": [],
        "services": [],
        "updated_at": None,
        "request_count": 0,
        "last_request_at": None,
    }


def _empty_service_detail(data: dict) -> dict:
    # 定位键照常回显：降级壳也要能和列表那一行对得上位（同 `_empty_kb_detail`）
    sid = data.get("service_id") or ""
    return {
        "service_id": sid,
        "service_name": sid,
        "status": None,
        "enabled": None,
        "knowledge_bases": [],
        "request_count": 0,
        "last_request_at": None,
    }


class AnalyticsService:
    """数据运营聚合服务（二期：事件表口径）。"""

    # ── 范围与窗口 ──────────────────────────────────────────────

    async def _kb_scope(self, session, tenant_id: str, domain_space_id, kb_id=None):
        """返回 (kb_ids, kb_map)：本次统计范围内的库 id 列表与 {id: KnowledgeInfo}。

        `kb_id` 是在空间范围之上再收窄一层（单库筛选），**不是**范围之外的白名单：
        传了不属于本租户/本空间的 id 只会得到空范围，各统计项按空范围出 0 ——
        与「库里没有这个库」同一个表现，不泄露别的租户是否有这个 id。
        """
        conditions = [
            KnowledgeInfo.tenant_id == tenant_id,
            KnowledgeInfo.is_deleted == 0,
        ]
        if domain_space_id:
            conditions.append(KnowledgeInfo.space_id == domain_space_id)
        if kb_id:
            conditions.append(KnowledgeInfo.id == kb_id)
        result = await session.execute(select(KnowledgeInfo).where(*conditions))
        kbs = list(result.scalars())
        return [kb.id for kb in kbs], {kb.id: kb for kb in kbs}

    async def _scope_window(self, session, scope: dict):
        """按 `scope` 解析出本次统计的窗口三元组 (win_start, win_end, window_dates)。

        `win_end` 是最后一天 +1 天的开区间右界；`window_dates` 为升序日期串（趋势补零用）。

        相对窗口（`days`）的日期一律取自 DB `current_date()`，不与容器本地时钟各算一遍
        —— 两处各算一次就会出现「趋势最后一个点跟今天差一天」这类只有跨时区才暴露的问题。
        **自定义区间例外**：右界由调用方指定、本来就不是今天，此时不去（也不该去）DB
        问一次 `current_date()` —— 既少一次查询，语义上也没有「今天」这个锚点。

        ⚠️ 窗口**只在本方法里算一次**，再往下游聚合函数传：`_search_agg` / `_access_trend`
        以前各自调一遍 `_window`，加自定义区间时漏传一个就会出现「趋势按自定义、同页其他
        指标按 7 天」的错位 —— 而且不报错，只是数字对不上。
        """
        start, end = scope.get("start_date"), scope.get("end_date")
        if start is not None and end is not None:
            return _resolve_window(date.today(), scope["days"], start, end)
        today = (await session.execute(select(func.current_date()))).scalar_one()
        return _resolve_window(today, scope["days"], None, None)

    @staticmethod
    def _fill_trend(raw: dict, window_dates: list) -> list:
        return [{"date": d, "count": raw.get(d, 0)} for d in window_dates]

    @staticmethod
    def _window_params(win_start: date, win_end: date) -> dict:
        """窗口绑定参数：绑真实 `date` 对象并显式 CAST。

        不写 `current_date - :since_days` 那种减法：原生 `text()` 的绑定参数无类型
        信息，asyncpg 交给 PG 推断时会解析成 `timestamptz >= integer` 而报
        `UndefinedFunctionError`（一期踩过）。
        """
        return {"win_start": win_start, "win_end": win_end}

    # ── 检索线聚合（事件表口径）────────────────────────

    async def _search_agg(self, session, tenant_id: str, kb_ids: list,
                          win_start: date, win_end: date, window_dates: list) -> dict:
        """从检索事件表聚合「检索分析」页签的全部指标。kb_ids 为空时短路。

        窗口由调用方算好传进来（`_scope_window`），本方法不再自己推 —— 见那里的说明。
        """
        if not kb_ids:
            return {
                "total_requests": 0,
                "active_users": 0,
                "searched_kbs": 0,
                "request_trend": self._fill_trend({}, window_dates),
                "top_questions": [],
                "kb_usage": [],
                "pipeline_usage": _empty_pipeline_usage(),
                "service_usage": [],
                "used_services": 0,
            }

        base = {"tenant_id": tenant_id, "kb_ids": list(kb_ids),
                **self._window_params(win_start, win_end)}

        # ① 请求总数 / 活跃用户。`count(DISTINCT user_id)` 天然忽略 NULL ——
        #    MCP / API Key 请求（user_id 为 NULL）计入 total_requests、不计入
        #    active_users，这是有意为之：服务调用不等于用户活跃。
        totals = (
            await session.execute(
                text(_REQ_CTE + f"""
SELECT count(*) AS total_requests,
       count(DISTINCT r.user_id) AS active_users
  FROM req r
 WHERE {_REQ_IN_SCOPE}
"""),
                base,
            )
        ).first()
        total_requests = int(totals.total_requests) if totals else 0
        active_users = int(totals.active_users) if totals else 0

        # ② 请求趋势：按天分组。表达式抽成变量复用到 GROUP BY —— 写成相同的字面量
        #    文本一样能跑（原生 SQL 里相同文本就是同一个表达式），复用变量读起来
        #    只有一处口径。
        day_expr = "to_char(r.received_at, 'YYYY-MM-DD')"
        trend_rows = (
            await session.execute(
                text(_REQ_CTE + f"""
SELECT {day_expr} AS day, count(*) AS cnt
  FROM req r
 WHERE {_REQ_IN_SCOPE}
 GROUP BY {day_expr}
"""),
                base,
            )
        ).all()
        request_trend = self._fill_trend({r.day: int(r.cnt) for r in trend_rows}, window_dates)

        # ③ 高频提问：按 `query_hash` 归并同一句话。同分时必须再按 query 定序 ——
        #    只按 count 排时 PG 对并列行的次序不作保证，卡在第 10 名上的同分提问
        #    每次刷新都会换人，榜单看起来像在跳。
        #    `query_hash IS NOT NULL`：没有 query 的请求（MCP 等）不构成「提问」。
        top_rows = (
            await session.execute(
                text(_REQ_CTE + f"""
SELECT r.query_hash AS query_hash, max(r.query) AS query, count(*) AS cnt
  FROM req r
 WHERE {_REQ_IN_SCOPE}
   AND r.query_hash IS NOT NULL
 GROUP BY r.query_hash
 ORDER BY count(*) DESC, max(r.query) ASC
 LIMIT {_TOP_LIST_LIMIT}
"""),
                base,
            )
        ).all()
        top_questions = [{"query": r.query, "count": int(r.cnt)} for r in top_rows]

        # ④ 库使用情况：把一次检索的 kb 列表展开成行（同一库在一条请求里只算一次）。
        #    `searched_kbs` **等于本结果行数**，而不是另做一次 count —— 两处各写一遍
        #    「用过几个库」迟早会算出两个数，页面上顶部 KPI 与表格行数就对不上。
        kb_rows = (
            await session.execute(
                text(_REQ_CTE + f"""
SELECT x.kb AS kb_id,
       count(*) AS request_count,
       max(r.received_at) AS last_used_at
  FROM req r
  CROSS JOIN LATERAL jsonb_array_elements_text(r.kb_ids) AS x(kb)
 WHERE x.kb = ANY(:kb_ids)
   AND (r.user_id IS NULL OR r.user_id <> 'anonymous')
 GROUP BY x.kb
 ORDER BY count(*) DESC, x.kb ASC
 LIMIT {_TOP_LIST_LIMIT * _TOP_CANDIDATE_FACTOR}
"""),
                base,
            )
        ).all()
        searched_kbs = len(kb_rows)

        # ⑤ 参与率：分母 = 窗口内请求去重数（= total_requests），
        #    分子 = 有该类实质步骤的去重请求数（`skipped` 不计入分子）。
        step_rows = (
            await session.execute(
                text(_REQ_CTE + f"""
SELECT count(DISTINCT s.request_id) FILTER (
           WHERE s.stage = ANY(ARRAY[{_sql_list(_STAGES_ONTOLOGY)}])
       ) AS ontology_cnt,
       count(DISTINCT s.request_id) FILTER (
           WHERE s.stage = ANY(ARRAY[{_sql_list(_STAGES_WIKI)}])
       ) AS wiki_cnt,
       count(DISTINCT s.request_id) FILTER (
           WHERE s.stage = ANY(ARRAY[{_sql_list(_STAGES_RAG)}])
              OR (s.stage = '{STAGE_ROUTE_DECISION}' AND s.detail->>'route' = 'rag')
       ) AS rag_cnt
  FROM knowledge_base.search_step_event s
 WHERE s.tenant_id = :tenant_id
   AND s.is_deleted = 0
   AND s.status = ANY(ARRAY[{_sql_list(_COUNTED_STEP_STATUSES)}])
   AND s.request_id IN (SELECT r.request_id FROM req r WHERE {_REQ_IN_SCOPE})
"""),
                base,
            )
        ).first()
        valid_requests = total_requests
        onto_cnt = int(step_rows.ontology_cnt or 0) if step_rows else 0
        wiki_cnt = int(step_rows.wiki_cnt or 0) if step_rows else 0
        rag_cnt = int(step_rows.rag_cnt or 0) if step_rows else 0

        def _rate(cnt: int) -> float:
            return round(cnt / valid_requests, 4) if valid_requests else 0.0

        pipeline_usage = {
            "valid_requests": valid_requests,
            "ontology": {"count": onto_cnt, "rate": _rate(onto_cnt)},
            "rag": {"count": rag_cnt, "rate": _rate(rag_cnt)},
            "wiki": {"count": wiki_cnt, "rate": _rate(wiki_cnt)},
        }

        # ⑥ 领域服务归因：请求经哪个服务发起。CTE 见模块级 `_PAIR_CTE`
        #    （抽屉 D 复用同一份，口径只有一处）。
        pair_cte = _PAIR_CTE
        used_services = int(
            (
                await session.execute(
                    text(pair_cte + """
SELECT count(DISTINCT p.service_id) AS cnt
  FROM pair p
 WHERE p.service_id IS NOT NULL
"""),
                    base,
                )
            ).scalar_one()
            or 0
        )

        service_rows = (
            await session.execute(
                text(pair_cte + f"""
SELECT p.service_id AS service_id,
       ds.name AS service_name,
       count(DISTINCT p.request_id) AS request_count,
       count(DISTINCT skb.kb_id) AS kb_count,
       max(rq.received_at) AS last_used_at
  FROM pair p
  LEFT JOIN knowledge_base.services ds
         ON ds.id = p.service_id
        AND ds.tenant_id = p.tenant_id
        AND ds.is_deleted = 0
  LEFT JOIN knowledge_base.service_knowledge_bases skb
         ON skb.service_id = p.service_id
        AND skb.tenant_id = p.tenant_id
        AND skb.is_deleted = 0
        AND skb.kb_id = ANY(:kb_ids)
  LEFT JOIN req rq ON rq.request_id = p.request_id
 WHERE p.service_id IS NOT NULL
 GROUP BY p.service_id, ds.name
 ORDER BY count(DISTINCT p.request_id) DESC, p.service_id ASC
 LIMIT {_TOP_LIST_LIMIT}
"""),
                base,
            )
        ).all()

        return {
            "total_requests": total_requests,
            "active_users": active_users,
            "searched_kbs": searched_kbs,
            "request_trend": request_trend,
            "top_questions": top_questions,
            "kb_usage": kb_rows,
            "pipeline_usage": pipeline_usage,
            "service_usage": service_rows,
            "used_services": used_services,
        }

    async def _cited_agg(self, session, tenant_id: str, kb_ids: list,
                         win_start: date, win_end: date):
        """被引用 / 采用（`search_adoption_event`）。

        返回 (cited_assets, top_cited_raw)；kb_ids 为空时 (0, [])。

        ⚠️ **必须过滤 `target_type='document'`**：命中/采用表里 `ontology_instance`
        的 `target_id` 是实体 sha 值，不过滤会混进 `count(DISTINCT target_id)`，
        使 `usage_rate = cited_assets / total_assets` 大于 1。
        """
        if not kb_ids:
            return 0, []
        params = {"tenant_id": tenant_id, "kb_ids": list(kb_ids),
                  **self._window_params(win_start, win_end)}
        cited_assets = int(
            (
                await session.execute(
                    text(f"""
SELECT count(DISTINCT a.target_id) AS cnt
  FROM knowledge_base.search_adoption_event a
 WHERE a.tenant_id = :tenant_id
   AND a.is_deleted = 0
   AND a.target_type = '{TARGET_DOCUMENT}'
   AND a.kb_id = ANY(:kb_ids)
   AND a.adopted_at >= CAST(:win_start AS DATE)
   AND a.adopted_at <  CAST(:win_end AS DATE)
"""),
                    params,
                )
            ).scalar_one()
            or 0
        )

        # 同一 answer_id 只计 1 次（同一次回答里同资产重复出现不该虚增引用数）
        #
        # 排序契约（需求方口径）：引用次数 DESC → 最近一次引用时间 DESC → `target_id` 兜底。
        # 第二键**不能**省：并列行若退回按 `target_id`（随机 UUID）定序，同分资产在榜内
        # 的先后与「谁最近被引用过」无关，等于伪随机。第三键只为把并列行定成全序 ——
        # 同一批埋点写入的 `adopted_at` 可能逐位相同，只留两个键时 PG 对并列行次序不作
        # 保证（同 `_search_agg` 里「高频提问」那处的顾虑）。
        # `HAVING >= 1` 在 `answer_id NOT NULL` 下是 no-op，写出来是为了让「至少被引用
        # 过一次」这条口径不隐式依赖 DDL 约束。
        top_rows = (
            await session.execute(
                text(f"""
SELECT a.target_id AS doc_id,
       count(DISTINCT a.answer_id) AS cite_count,
       max(a.adopted_at) AS last_cited_at
  FROM knowledge_base.search_adoption_event a
 WHERE a.tenant_id = :tenant_id
   AND a.is_deleted = 0
   AND a.target_type = '{TARGET_DOCUMENT}'
   AND a.kb_id = ANY(:kb_ids)
   AND a.adopted_at >= CAST(:win_start AS DATE)
   AND a.adopted_at <  CAST(:win_end AS DATE)
 GROUP BY a.target_id
HAVING count(DISTINCT a.answer_id) >= 1
 ORDER BY count(DISTINCT a.answer_id) DESC,
          max(a.adopted_at) DESC,
          a.target_id ASC
 LIMIT {_TOP_LIST_LIMIT}
"""),
                params,
            )
        ).all()
        top_cited = [
            {"doc_id": r.doc_id, "cite_count": int(r.cite_count), "last_cited_at": r.last_cited_at}
            for r in top_rows
        ]
        return cited_assets, top_cited

    async def _unused_assets_agg(self, session, tenant_id: str, kb_ids: list,
                                 threshold_days: int, kb_map=None) -> list:
        """长期未使用资产：READY 文档中**上传满 `threshold_days` 天**且「最近一次被采用」
        为空或早于同一个阈值的。

        `unused_threshold_days` 一期解析完就被丢掉了（`_parse` 的 4 元组三个调用方
        都用 `_` 接掉），前端那个 30/90 天切换器一直是死的 —— 这里真的用上它。

        判据是**两半取交集**，理由与取舍见 `_UNUSED_ASSETS_SQL` 上方注释：
        「年龄」那半边必须用**应用进程的时钟**（这里算的 `created_before`），
        不能交给 SQL 的 `now()`，否则会静默依赖 PG 会话时区。

        `kb_map` 不传时（兼容老调用点）`kb_name` 回落 `kb_id`，与 `_enrich_cited` 同口径。
        """
        if not kb_ids:
            return []
        # 年龄下限：与 `d.created_at` 是同一个钟（都是应用进程的 naive 本地时间），
        # 故不受 PG 会话时区影响 —— 见 `_UNUSED_ASSETS_SQL` 上方注释，别改成 SQL 表达式。
        created_before = datetime.now() - timedelta(days=threshold_days)
        rows = (
            await session.execute(
                text(_UNUSED_ASSETS_SQL),
                {
                    "tenant_id": tenant_id,
                    "kb_ids": list(kb_ids),
                    "threshold_days": threshold_days,
                    "created_before": created_before,
                },
            )
        ).all()
        kb_map = kb_map or {}
        return [
            {
                "asset_id": r.asset_id,
                "file_name": r.file_name,
                # None = 从未被引用过（LEFT JOIN 未命中），前端渲染「从未使用」
                "last_cited_at": r.last_cited_at.isoformat() if r.last_cited_at else None,
                "asset_type": to_asset_type(classify_asset_category(r.mime_type, r.file_name)),
                "kb_id": r.knowledge_base_id,
                # 原型表格「所属知识库」列；富化不到时回落 kb_id（与 `_enrich_cited` 同口径）
                "kb_name": (
                    kb_map[r.knowledge_base_id].name
                    if r.knowledge_base_id in kb_map
                    else r.knowledge_base_id
                ),
                # ⚠️ 必须过 `_local_naive_to_iso`（裸 isoformat 会丢掉时区，见该函数注释）
                "updated_at": _local_naive_to_iso(r.updated_at),
                # 上传时间（= 判据里「年龄」那一半的可视化依据，语义同 `AssetDetail.uploaded_at`）。
                # 没有它的话，表上只有「更新时间」—— 一份 100 天前上传、3 天前重新解析过的
                # 文档会显示「更新 3 天前」，用户看不出它凭什么叫「长期未使用」。
                "created_at": _local_naive_to_iso(r.created_at),
            }
            for r in rows
        ]

    # ── 处理线聚合 ─────────────────────────────

    async def _pipeline_agg(self, session, tenant_id: str, kb_ids: list, kb_map: dict):
        """四级漏斗 + 任务状态分布。

        「不适用的阶段 ⇒ 该级 = 上一级」：lightrag 资产没有文档级编译阶段，
        `llm_wiki_compile_status` 恒 NULL，无法用状态列区分「不适用」与「尚未编译」，
        只能靠 kb_type。锚在**已抽取**上而不是已解析 —— 锚在已解析的话，一个 lightrag
        文档解析成功但抽取失败时会「不在已抽取」却「在已编译」，漏斗出现凹陷
        （已编译 > 已抽取），页面看起来就是坏的。
        """
        empty = (
            {"total": 0, "parsed": 0, "extracted": 0, "compiled": 0},
            {"success": 0, "failed": 0, "running": 0, "pending": 0, "avg_duration_ms": None},
        )
        if not kb_ids:
            return empty

        params = {"tenant_id": tenant_id, "kb_ids": list(kb_ids)}
        rows = (await session.execute(text(_PIPELINE_SQL), params)).all()

        total = parsed = extracted = compiled = 0
        for row in rows:
            kb = kb_map.get(row.kb_id)
            if kb is None:
                # 不在 kb_map 里说明两次范围查询不一致（理论不会发生）：
                # 按 lightrag 口径（编译阶段不适用）计入，并留日志。
                logger.warning(
                    "[jonex] 数据运营漏斗：文档引用了范围外的 kb，按「编译不适用」计入 kb=%s",
                    row.kb_id,
                )
            is_wiki = bool(kb is not None and getattr(kb, "kb_type", None) in LLM_WIKI_TYPES)
            total += int(row.total)
            parsed += int(row.parsed)
            extracted += int(row.extracted)
            # 编译阶段对非 wiki 库不适用 → 等于上一级（不适用 ⇒ 通过）
            compiled += int(row.compiled) if is_wiki else int(row.extracted)

        stats_rows = (await session.execute(text(_TASK_STATS_SQL), params)).all()
        by_status = {r.status: int(r.cnt) for r in stats_rows}
        # ⚠️ 取到的**必须是 success 那一行**：SQL 里用 `FILTER (WHERE status='success')`
        # 保证只有该分组的 avg_sec 非空（配套见 _TASK_STATS_SQL 上方注释）。
        # 没有成功任务、或成功任务两端时间不齐 → 这里拿到 None → 前端显示「—」。
        avg_sec = next((r.avg_sec for r in stats_rows if r.avg_sec is not None), None)
        task_stats = {
            "success": by_status.get(TASK_STATUS_SUCCESS, 0),
            "failed": by_status.get(TASK_STATUS_FAILED, 0),
            "running": by_status.get(TASK_STATUS_RUNNING, 0),
            # 恒 0：资产建档即进入 parsing，没有等待调度的中间态
            "pending": 0,
            "avg_duration_ms": int(round(float(avg_sec) * 1000)) if avg_sec is not None else None,
        }
        return (
            {"total": total, "parsed": parsed, "extracted": extracted, "compiled": compiled},
            task_stats,
        )

    async def _processing_tasks_agg(self, session, tenant_id: str, kb_ids: list,
                                    limit: int = _UNUSED_LIST_LIMIT) -> list:
        """处理任务明细（按开始时间倒序，≤ limit 条）。kb_ids 为空时短路。"""
        if not kb_ids:
            return []
        rows = (
            await session.execute(
                text(_TASKS_SQL),
                {"tenant_id": tenant_id, "kb_ids": list(kb_ids), "limit": limit},
            )
        ).all()
        return [
            {
                "task_id": r.processing_task_id,
                "asset_id": r.asset_id,
                "file_name": r.file_name,
                "task_type": r.task_type,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "duration_ms": int(round(float(r.duration_ms))) if r.duration_ms is not None else None,
                "error": r.error,
                "asset_type": to_asset_type(classify_asset_category(r.mime_type, r.file_name)),
            }
            for r in rows
        ]

    async def _abnormal_summary(self, session, tenant_id: str, kb_ids: list) -> dict:
        """异常汇总：未处置的失败**任务**数，按 stage 分组，`total` 恒等于三档之和。"""
        counts = {stage: 0 for stage in _STAGE_ORDER}
        if kb_ids:
            rows = (
                await session.execute(
                    text(_ABNORMAL_SUMMARY_SQL),
                    {"tenant_id": tenant_id, "kb_ids": list(kb_ids)},
                )
            ).all()
            for row in rows:
                if row.task_type in counts:
                    counts[row.task_type] = int(row.unhandled)
        return {
            "total": sum(counts.values()),
            "by_stage": [{"stage": stage, "count": counts[stage]} for stage in _STAGE_ORDER],
        }

    async def _abnormal_items(self, session, tenant_id: str, kb_ids: list, kb_map: dict,
                              *, stages=None, handled: bool = False,
                              limit: int = _UNUSED_LIST_LIMIT, offset: int = 0):
        """异常明细，返回 (items, total)。

        `handled` 是**筛选**：`False`（默认）只看未处置的，`True` 只看已处置的。
        `total` 是同一筛选条件下的全量条数（分页要它，不能拿 `len(items)` 充数）。
        """
        if not kb_ids:
            return [], 0
        params = {"tenant_id": tenant_id, "kb_ids": list(kb_ids)}
        extra = ""
        if stages:
            params["stages"] = list(stages)
            extra += " AND task_type = ANY(:stages)"
        extra += " AND " + (_HANDLED_TRUE if handled else _HANDLED_FALSE)

        total = int(
            (
                await session.execute(
                    text(_ABNORMAL_CTE
                         + f"SELECT count(*) AS cnt FROM fails{_ABNORMAL_WHERE_FAILED}{extra}"),
                    params,
                )
            ).scalar_one()
            or 0
        )

        rows = (
            await session.execute(
                text(_ABNORMAL_CTE + f"""
SELECT processing_task_id, asset_id, task_type, error, occurred_at,
       file_name, mime_type, knowledge_base_id, data_source_type, retry_count,
       {_HANDLED_TRUE} AS handled
  FROM fails
{_ABNORMAL_WHERE_FAILED}{extra}
 ORDER BY occurred_at DESC, processing_task_id DESC
 LIMIT :limit OFFSET :offset
"""),
                {**params, "limit": limit, "offset": offset},
            )
        ).all()

        items = []
        for r in rows:
            asset_type = to_asset_type(classify_asset_category(r.mime_type, r.file_name))
            kb = kb_map.get(r.knowledge_base_id)
            kb_name = kb.name if kb is not None else None
            reason = (r.error or "").strip() or _REASON_FALLBACK
            occurred_at = r.occurred_at.isoformat() if r.occurred_at else None
            retry_count = int(r.retry_count or 0)
            items.append({
                "asset_id": r.asset_id,
                "file_name": r.file_name,
                "task_type": r.task_type,
                "status": TASK_STATUS_FAILED,
                "error": r.error,
                # 一期字段保留（值 = occurred_at），别删
                "last_failed_at": occurred_at,
                "asset_type": asset_type,
                "kb_name": kb_name,
                "handled": bool(r.handled),
                "retry_count": retry_count,
                "occurred_at": occurred_at,
                # 前端 `DoAnomalyAsset.detail` 是 Record<string,string>，键固定对齐
                # `dataOperations.anomaly.field.*` 的 i18n 键。
                # `source` 取文档的来源方式（`knowledge_documents.data_source_type`
                # 是真实列）：后端 file/api/storage/api_push ↔ 前端
                # upload/api/storage/apiPush。
                "detail": {
                    "assetName": r.file_name or r.asset_id,
                    "assetType": asset_type,
                    "kbName": kb_name or "",
                    "stage": r.task_type,
                    "reason": reason,
                    "occurredAt": occurred_at or "",
                    "source": r.data_source_type or "file",
                    "retryCount": str(retry_count),
                },
            })
        return items, total

    # ── Neo4j 本体聚合 ──────────────────────────────────────────

    async def _neo4j_stats(self, tenant_id: str, kb_ids: list) -> dict:
        """跨 KB 聚合本体指标；Neo4j 不可用降级全 0，不阻塞基础能力。

        ⚠️ **不带窗口参数**：`growth` 把图内**全部**实体的 `created_at` 拉回 Python
        （Cypher 侧无时间过滤，见下方 `growth_rec`），调用方再用 `_fill_trend(growth,
        window_dates)` 按窗口补零 —— 所以自定义区间天然生效，代价是区间越大这步越贵。
        """
        empty = {
            "entities": 0,
            "relations": 0,
            "type_distribution": {},
            "kb_distribution": {},
            "growth": {},
        }
        if not kb_ids:
            return empty
        try:
            driver = get_neo4j_driver()
            if driver is None:
                return empty
            async with driver.session() as ndb:
                entities = await self._neo4j_count(
                    ndb, tenant_id, kb_ids,
                    "MATCH (e:OntologyEntity {tenant_id:$t}) WHERE e.kb_id IN $kbs "
                    "AND coalesce(e.stub,false)=false RETURN count(e) AS c",
                )
                relations = await self._neo4j_count(
                    ndb, tenant_id, kb_ids,
                    "MATCH (s:OntologyEntity {tenant_id:$t})-[r:ONT_REL]->(o:OntologyEntity {tenant_id:$t}) "
                    "WHERE s.kb_id IN $kbs AND o.kb_id IN $kbs "
                    "AND coalesce(s.stub,false)=false AND coalesce(o.stub,false)=false "
                    "RETURN count(r) AS c",
                )
                type_rec = await ndb.run(
                    "MATCH (e:OntologyEntity {tenant_id:$t}) WHERE e.kb_id IN $kbs "
                    "AND coalesce(e.stub,false)=false "
                    "RETURN e.entity_type AS type, count(e) AS c",
                    {"t": tenant_id, "kbs": kb_ids},
                )
                type_distribution = {r["type"]: r["c"] async for r in type_rec}
                kb_rec = await ndb.run(
                    "MATCH (e:OntologyEntity {tenant_id:$t}) WHERE e.kb_id IN $kbs "
                    "AND coalesce(e.stub,false)=false "
                    "RETURN e.kb_id AS kb_id, count(e) AS c",
                    {"t": tenant_id, "kbs": kb_ids},
                )
                kb_distribution = {r["kb_id"]: r["c"] async for r in kb_rec}
                growth_rec = await ndb.run(
                    "MATCH (e:OntologyEntity {tenant_id:$t}) WHERE e.kb_id IN $kbs "
                    "AND coalesce(e.stub,false)=false "
                    "RETURN e.created_at AS created_at",
                    {"t": tenant_id, "kbs": kb_ids},
                )
                growth = {}
                async for r in growth_rec:
                    day = self._ts_to_day(r["created_at"])
                    if day:
                        growth[day] = growth.get(day, 0) + 1
            return {
                "entities": entities,
                "relations": relations,
                "type_distribution": type_distribution,
                "kb_distribution": kb_distribution,
                "growth": growth,
            }
        except Exception:  # noqa: BLE001 — Neo4j 降级，不抛
            logger.warning("[jonex] Neo4j 本体统计降级（返回空值）tenant=%s", tenant_id, exc_info=True)
            return empty

    async def _neo4j_count(self, ndb, tenant_id, kb_ids, cypher) -> int:
        rec = await ndb.run(cypher, {"t": tenant_id, "kbs": kb_ids})
        record = await rec.single()
        return record["c"] if record else 0

    async def _hit_candidates(self, session, tenant_id: str, kb_ids: list,
                              win_start: date, win_end: date) -> list:
        """PG 出「窗口内被命中过」的本体实例三元组候选（取数方向反转：PG 出候选，Neo4j 只做存活校验）。

        收益：候选量级 = 近 N 天**真实命中过**的实体（走命中表自己的时间索引），
        而不是图内全部实体（大库上万、空间级到十万级）—— 后者要把整个图跨进程
        拉回 Python 再算 sha256，而且把 Neo4j 变成硬依赖。

        ⚠️ join 键一律用 `(kb_id, entity_type, canonical_name)` 三元组，**不是**
        `target_id`：`canonical_name` 可被改名（实例更新与对账的别名对齐都会改），
        改名后历史行的 `target_id` 指向一个不再存在的键，拿它 join 会把该实体判成
        「已失效」→ `active_entities` 系统性少算。
        """
        if not kb_ids:
            return []
        rows = (
            await session.execute(
                text(f"""
SELECT DISTINCT s.kb_id, s.entity_type, s.canonical_name
  FROM knowledge_base.search_hit_event s
 WHERE s.tenant_id = :tenant_id
   AND s.is_deleted = 0
   AND s.target_type = '{TARGET_ONTOLOGY_INSTANCE}'
   AND s.kb_id = ANY(:kb_ids)
   AND s.hit_at >= CAST(:win_start AS DATE)
   AND s.hit_at <  CAST(:win_end AS DATE)
"""),
                {
                    "tenant_id": tenant_id,
                    "kb_ids": list(kb_ids),
                    **self._window_params(win_start, win_end),
                },
            )
        ).all()
        return [
            (r.kb_id, r.entity_type, r.canonical_name)
            for r in rows
            if r.kb_id and r.entity_type and r.canonical_name
        ]

    async def _neo4j_alive(self, tenant_id: str, triples: list):
        """候选三元组回捞 Neo4j 存活项。

        返回存活三元组集合；**Neo4j 不可用返回 None** —— 与「回捞结果为空集」区分开：
        前者是降级（调用方按归 0 处理），后者是真实的「一个都不存在了」。
        """
        if not triples:
            return set()
        try:
            driver = get_neo4j_driver()
            if driver is None:
                return None
            async with driver.session() as ndb:
                rec = await ndb.run(
                    "UNWIND $rows AS row "
                    "MATCH (e:OntologyEntity {tenant_id:$t, kb_id:row.kb_id, "
                    "       entity_type:row.entity_type, canonical_name:row.canonical_name}) "
                    "WHERE coalesce(e.stub,false) = false "
                    "RETURN row.kb_id AS kb_id, row.entity_type AS et, row.canonical_name AS cn",
                    {
                        "t": tenant_id,
                        "rows": [
                            {"kb_id": kb, "entity_type": et, "canonical_name": cn}
                            for kb, et, cn in triples
                        ],
                    },
                )
                return {(r["kb_id"], r["et"], r["cn"]) async for r in rec}
        except Exception:  # noqa: BLE001 — Neo4j 降级
            logger.warning("[jonex] Neo4j 实体存活回捞降级 tenant=%s", tenant_id, exc_info=True)
            return None

    async def _top_hits(self, session, tenant_id: str, kb_ids: list,
                        win_start: date, win_end: date) -> list:
        """高频使用本体候选榜单（按三元组 group by）。

        排序契约（需求方口径）：命中次数 DESC → 最近一次命中时间 DESC → 三元组兜底。
        第二键**不能**退回 `canonical_name`（按名字节序定序，与「谁最近被命中过」无关）。

        ⚠️ 候选池（`LIMIT _TOP_LIST_LIMIT * _TOP_CANDIDATE_FACTOR`）也是这条 `ORDER BY`
        选出来的，而调用方随后只做 Neo4j 存活过滤、**只删不排**（见 `knowledge_assets`）
        —— 所以排序必须在 SQL 里定死，不能挪到 Python 侧补排。
        """
        if not kb_ids:
            return []
        rows = (
            await session.execute(
                text(f"""
SELECT s.kb_id, s.entity_type, s.canonical_name,
       count(DISTINCT s.request_id) AS hit_count,
       max(s.hit_at) AS last_hit_at
  FROM knowledge_base.search_hit_event s
 WHERE s.tenant_id = :tenant_id
   AND s.is_deleted = 0
   AND s.target_type = '{TARGET_ONTOLOGY_INSTANCE}'
   AND s.kb_id = ANY(:kb_ids)
   AND s.hit_at >= CAST(:win_start AS DATE)
   AND s.hit_at <  CAST(:win_end AS DATE)
 GROUP BY s.kb_id, s.entity_type, s.canonical_name
HAVING count(DISTINCT s.request_id) >= 1
 ORDER BY count(DISTINCT s.request_id) DESC,
          max(s.hit_at) DESC,
          s.kb_id ASC, s.entity_type ASC, s.canonical_name ASC
 LIMIT {_TOP_LIST_LIMIT * _TOP_CANDIDATE_FACTOR}
"""),
                {
                    "tenant_id": tenant_id,
                    "kb_ids": list(kb_ids),
                    **self._window_params(win_start, win_end),
                },
            )
        ).all()
        return [
            {
                "kb_id": r.kb_id,
                "entity_type": r.entity_type,
                "canonical_name": r.canonical_name,
                "hit_count": int(r.hit_count),
                "last_used_at": r.last_hit_at.isoformat() if r.last_hit_at else None,
            }
            for r in rows
            if r.kb_id and r.entity_type and r.canonical_name
        ]

    async def _neo4j_entities_head(self, tenant_id: str, kb_ids: list, limit: int):
        """有界拉取 Neo4j 实体全集样本（**按创建时间升序，最老的在前**）；不可用返回 None。

        ⚠️ 排序方向是**为「长期未使用」这个唯一调用点选的**：它在 Python 侧要减掉
        「近 N 天命中过的」再取前 20，而这批候选现在还要过「创建满 N 天」这道闸。
        按「最近改动倒序」取头部样本的话，样本会被**刚创建/刚改过**的实体占满，
        年龄闸一卡几乎全被滤掉，榜内条数系统性偏少；按创建时间升序取，样本恰好就是
        「最可能长期未使用」的那一批。全仓只有 `knowledge_assets` 一处调用它。
        """
        if not kb_ids:
            return []
        try:
            driver = get_neo4j_driver()
            if driver is None:
                return None
            async with driver.session() as ndb:
                rec = await ndb.run(
                    "MATCH (e:OntologyEntity {tenant_id:$t}) "
                    "WHERE e.kb_id IN $kbs AND coalesce(e.stub,false) = false "
                    "RETURN e.kb_id AS kb_id, e.entity_type AS entity_type, "
                    "       e.canonical_name AS canonical_name, "
                    "       e.created_at AS created_at, "
                    "       coalesce(e.updated_at, e.created_at) AS updated_at "
                    "ORDER BY coalesce(e.created_at, e.updated_at) ASC "
                    "LIMIT $limit",
                    {"t": tenant_id, "kbs": kb_ids, "limit": limit},
                )
                return [
                    {
                        "kb_id": r["kb_id"],
                        "entity_type": r["entity_type"],
                        "canonical_name": r["canonical_name"],
                        "created_at": r["created_at"],
                        "updated_at": r["updated_at"],
                    }
                    async for r in rec
                ]
        except Exception:  # noqa: BLE001 — Neo4j 降级
            logger.warning("[jonex] Neo4j 实体全集拉取降级 tenant=%s", tenant_id, exc_info=True)
            return None

    async def _last_hit_at(self, session, tenant_id: str, triples: list) -> dict:
        """给少量三元组取**全时段**最近命中时间（「最近使用时间」列）。

        只对最终要展示的那 ≤20 条查询，参数量可控（每条 3 个绑定参数），
        不会退化成「全表按三元组 group by」。
        """
        out = {}
        triples = [t for t in triples if t and all(t)]
        if not triples:
            return out
        clauses = []
        params = {"tenant_id": tenant_id}
        for idx, (kb_id, entity_type, canonical_name) in enumerate(triples):
            clauses.append(
                f"(s.kb_id = :kb{idx} AND s.entity_type = :et{idx} AND s.canonical_name = :cn{idx})"
            )
            params[f"kb{idx}"] = kb_id
            params[f"et{idx}"] = entity_type
            params[f"cn{idx}"] = canonical_name
        rows = (
            await session.execute(
                text(f"""
SELECT s.kb_id, s.entity_type, s.canonical_name, max(s.hit_at) AS last_hit_at
  FROM knowledge_base.search_hit_event s
 WHERE s.tenant_id = :tenant_id
   AND s.is_deleted = 0
   AND s.target_type = '{TARGET_ONTOLOGY_INSTANCE}'
   AND ({' OR '.join(clauses)})
 GROUP BY s.kb_id, s.entity_type, s.canonical_name
"""),
                params,
            )
        ).all()
        for r in rows:
            out[(r.kb_id, r.entity_type, r.canonical_name)] = r.last_hit_at
        return out

    @staticmethod
    def _ts_to_day(created_at):
        """Neo4j `timestamp()` 毫秒 → 本地日期串；None / 非法值返回 None。"""
        if created_at is None:
            return None
        try:
            return datetime.fromtimestamp(int(created_at) / 1000).date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    @staticmethod
    def _ts_to_iso(created_at):
        """Neo4j `timestamp()` 毫秒 → 带本地时区偏移的 ISO 串（与模型 `_iso` 口径一致）。"""
        if created_at is None:
            return None
        try:
            return datetime.fromtimestamp(int(created_at) / 1000).astimezone().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    # ── 公开方法 ────────────────────────────────────────────────

    @_resilient(_empty_overview)
    async def overview(self, tenant_id: str, user_id, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        async with get_db_session() as session:
            kb_ids, _ = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"], scope["kb_id"]
            )
            total_kbs = len(kb_ids)
            win_start, win_end, window_dates = await self._scope_window(session, scope)
            total_assets = await self._count_docs_ready(session, tenant_id, kb_ids)
            total_data_sources = await self._count_sources(session, tenant_id, kb_ids)
            cited_assets, _ = await self._cited_agg(session, tenant_id, kb_ids, win_start, win_end)
            search = await self._search_agg(
                session, tenant_id, kb_ids, win_start, win_end, window_dates
            )

        usage_rate = (cited_assets / total_assets) if total_assets else 0.0
        neo = await self._neo4j_stats(tenant_id, kb_ids)
        return {
            "data_assets": {
                "total_assets": total_assets,
                "total_data_sources": total_data_sources,
                "cited_assets": cited_assets,
                "usage_rate": round(usage_rate, 4),
            },
            "knowledge_assets": {
                "ontology_entities": neo["entities"],
                "ontology_relations": neo["relations"],
                # Wiki 侧三期做（原型标注 ⏸️），占位零值
                "wiki_entities": 0,
                "wiki_concepts": 0,
            },
            "search_analytics": {
                "total_requests": search["total_requests"],
                "searched_kbs": search["searched_kbs"],
                "total_kbs": total_kbs,
                "used_services": search["used_services"],
                "unused_kbs": max(0, total_kbs - search["searched_kbs"]),
                "active_users": search["active_users"],
            },
        }

    @_resilient(_empty_data_assets)
    async def data_assets(self, tenant_id: str, user_id, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        async with get_db_session() as session:
            kb_ids, kb_map = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"], scope["kb_id"]
            )
            win_start, win_end, window_dates = await self._scope_window(session, scope)

            total_assets = await self._count_docs_ready(session, tenant_id, kb_ids)
            total_data_sources = await self._count_sources(session, tenant_id, kb_ids)
            cited_assets, top_cited = await self._cited_agg(
                session, tenant_id, kb_ids, win_start, win_end
            )
            access_trend = await self._access_trend(
                session, tenant_id, kb_ids, win_start, window_dates
            )
            composition = await self._composition(session, tenant_id, kb_ids)
            top_cited_assets = await self._enrich_cited(session, tenant_id, top_cited, kb_map)
            unused_assets = await self._unused_assets_agg(
                session, tenant_id, kb_ids, scope["unused_threshold_days"], kb_map
            )
            processing_pipeline, pipeline_task_stats = await self._pipeline_agg(
                session, tenant_id, kb_ids, kb_map
            )
            processing_tasks = await self._processing_tasks_agg(session, tenant_id, kb_ids)
            abnormal_summary = await self._abnormal_summary(session, tenant_id, kb_ids)
            # 页 1 内联的异常清单**只给未处置的**：与同一页的 `abnormal_summary.total`
            # 口径一致（否则汇总说待处理 3、下面列 5 条，用户第一眼就发现对不上）；
            # 已处置的走 abnormal-assets 下钻（handled 筛选）。
            abnormal_assets, _ = await self._abnormal_items(
                session, tenant_id, kb_ids, kb_map, handled=False
            )
            new_data_sources = await self._count_new_sources(session, tenant_id, kb_ids, win_start)

        usage_rate = (cited_assets / total_assets) if total_assets else 0.0
        return {
            "total_assets": total_assets,
            "total_data_sources": total_data_sources,
            "cited_assets": cited_assets,
            "usage_rate": round(usage_rate, 4),
            # 近 N 天新增资产 = 接入趋势之和（同一份日粒度数据，两处口径必然一致）
            "new_assets_in_window": sum(point["count"] for point in access_trend),
            "new_data_sources_in_window": new_data_sources,
            "access_trend": access_trend,
            "composition": composition,
            "top_cited_assets": top_cited_assets,
            "unused_assets": unused_assets,
            "processing_pipeline": processing_pipeline,
            "pipeline_task_stats": pipeline_task_stats,
            "processing_tasks": processing_tasks,
            "abnormal_summary": abnormal_summary,
            "abnormal_assets": abnormal_assets,
        }

    @_resilient(_empty_knowledge_assets)
    async def knowledge_assets(self, tenant_id: str, user_id, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        async with get_db_session() as session:
            kb_ids, kb_map = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"], scope["kb_id"]
            )
            win_start, win_end, window_dates = await self._scope_window(session, scope)

            # 活跃本体：PG 出候选 → Neo4j 回捞存活项。分母与 `_neo4j_stats`
            # 的 entities 对齐（两处都带 `coalesce(stub,false)=false`）。
            candidates = await self._hit_candidates(session, tenant_id, kb_ids, win_start, win_end)
            alive = await self._neo4j_alive(tenant_id, candidates) if candidates else set()
            top_hits = await self._top_hits(session, tenant_id, kb_ids, win_start, win_end)
            top_alive = (
                await self._neo4j_alive(
                    tenant_id,
                    [(h["kb_id"], h["entity_type"], h["canonical_name"]) for h in top_hits],
                )
                if top_hits else set()
            )
            entities_all = await self._neo4j_entities_head(
                tenant_id, kb_ids, _NEO4J_ENTITY_HEAD_LIMIT
            )

            # 长期未使用本体 = 全集 − 阈值窗口内命中过的，**再按年龄收窄**。
            #
            # 判据与资产侧同构、也是**两半取交集**：① 实体创建满 threshold 天；
            # ② 阈值窗口内 0 次命中。只判 ② 的话，昨天刚编译出来的实体（一次没被
            # 检索命中过）当天就进榜 —— 那不是「长期未使用」，是「还没来得及用」。
            #
            # `win_end` 是「今天 +1 天」的开区间右界，故 `win_end - threshold`
            # 正是「最近 threshold 天」的左界。
            # ⚠️ 该等式在**相对窗口**（`days`）下成立；若将来这条链路支持自定义区间
            # （`start_date`/`end_date`），阈值窗口会变成「以区间末日结尾」—— 那是
            # 独立的口径问题，登记在方案文档 §6.3，不在本处兜底。
            threshold_days = scope["unused_threshold_days"]
            th_start = win_end - timedelta(days=threshold_days)
            used_in_threshold = set(
                await self._hit_candidates(session, tenant_id, kb_ids, th_start, win_end)
            )
            # 年龄下限（纪元毫秒）。⚠️ 与资产侧那个 naive 列不是一回事：Neo4j 的
            # `created_at` 是 `timestamp()` 毫秒，`datetime.now().timestamp()` 与
            # `_ts_to_day` / `_ts_to_iso` 里的 `fromtimestamp(ms/1000)` 用的是同一套
            # 本机时区，相减时自动抵消 —— 这里**不存在**会话时区那个坑，不要照搬
            # 资产侧「必须用应用进程本地 naive 时间」的写法去改它。
            age_cutoff_ms = int(
                (datetime.now() - timedelta(days=threshold_days)).timestamp() * 1000
            )
            unused_candidates = []
            if entities_all is not None:
                for row in entities_all:
                    triple = (row["kb_id"], row["entity_type"], row["canonical_name"])
                    if not all(triple) or triple in used_in_threshold:
                        continue
                    # 太年轻 / 没有创建时间 → 不构成「长期未使用」。
                    # `created_at` 缺失时**排除而不是放进榜**：两半判据里「年龄」这半
                    # 拿不到，放进榜等于用「未知」冒充「长期未使用」，与列表文案
                    # 「创建 ≥N 天」直接矛盾。
                    created_ms = row.get("created_at")
                    if created_ms is None or int(created_ms) >= age_cutoff_ms:
                        continue
                    unused_candidates.append(row)
                    if len(unused_candidates) >= _UNUSED_LIST_LIMIT:
                        break
            # 「最近使用时间」列必须**全时段**取，不能拿「用过的」窗口数据凑
            last_hits = await self._last_hit_at(
                session, tenant_id,
                [(r["kb_id"], r["entity_type"], r["canonical_name"]) for r in unused_candidates],
            )

            neo = await self._neo4j_stats(tenant_id, kb_ids)
            entities = int(neo["entities"])
            type_distribution = [
                {"entity_type": t, "count": int(c)}
                for t, c in sorted(neo["type_distribution"].items(), key=lambda kv: -kv[1])
            ]
            kb_entity_distribution = self._kb_distribution(
                neo["kb_distribution"], kb_map, entities
            )
            growth_trend = self._fill_trend(neo["growth"], window_dates)

            # Neo4j 不可用（alive is None）→ 活跃数归 0；`top_alive` 同理 → 榜单给空
            active_entities = len(alive) if alive is not None else 0
            top_used_entities = []
            if top_alive is not None:
                for h in top_hits:
                    triple = (h["kb_id"], h["entity_type"], h["canonical_name"])
                    if triple not in top_alive:
                        continue
                    kb = kb_map.get(h["kb_id"])
                    top_used_entities.append({
                        # object_id 只作展示 id：返回时现算，
                        # **绝不**作为聚合 join 键（改名会失配）
                        "object_id": ontology_object_id(
                            tenant_id, h["kb_id"], h["entity_type"], h["canonical_name"]
                        ),
                        "canonical_name": h["canonical_name"],
                        "entity_type": h["entity_type"],
                        "hit_count": h["hit_count"],
                        # 结构性恒 0：`references` 里没有实体信息，采用事件只有
                        # document 一类（不要为了「有值」去从 references 造实体采用行）
                        "adopted_count": 0,
                        "last_used_at": h["last_used_at"],
                        "kb_id": h["kb_id"],
                        "kb_name": kb.name if kb is not None else None,
                    })
                    if len(top_used_entities) >= _TOP_LIST_LIMIT:
                        break

            unused_entities = []
            for r in unused_candidates:
                triple = (r["kb_id"], r["entity_type"], r["canonical_name"])
                last_used = last_hits.get(triple)
                kb = kb_map.get(r["kb_id"])
                unused_entities.append({
                    "object_id": ontology_object_id(
                        tenant_id, r["kb_id"], r["entity_type"], r["canonical_name"]
                    ),
                    "canonical_name": r["canonical_name"],
                    "entity_type": r["entity_type"],
                    "created_at": self._ts_to_iso(r["created_at"]),
                    "updated_at": self._ts_to_iso(r["updated_at"]),
                    "last_used_at": last_used.isoformat() if last_used else None,
                    "kb_id": r["kb_id"],
                    "kb_name": kb.name if kb is not None else None,
                })

        return {
            "ontology": {
                "entities": entities,
                "relations": int(neo["relations"]),
                "active_entities": active_entities,
                "activity_rate": round(active_entities / entities, 4) if entities else 0.0,
            },
            # Wiki 侧全部指标三期做（原型标注 ⏸️），保持占位零值
            "wiki": {"entities": 0, "concepts": 0, "active_pages": 0, "activity_rate": 0.0},
            "entity_type_distribution": type_distribution,
            "kb_entity_distribution": kb_entity_distribution,
            "entity_growth_trend": growth_trend,
            "top_used_entities": top_used_entities,
            "unused_entities": unused_entities,
        }

    @_resilient(_empty_search)
    async def search_analytics(self, tenant_id: str, user_id, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        async with get_db_session() as session:
            kb_ids, kb_map = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"], scope["kb_id"]
            )
            total_kbs = len(kb_ids)
            win_start, win_end, window_dates = await self._scope_window(session, scope)
            search = await self._search_agg(
                session, tenant_id, kb_ids, win_start, win_end, window_dates
            )

        total_requests = search["total_requests"]
        active_users = search["active_users"]
        kb_usage = []
        for r in search["kb_usage"]:
            kb = kb_map.get(r.kb_id)
            kb_usage.append({
                "kb_id": r.kb_id,
                "kb_name": kb.name if kb is not None else r.kb_id,
                "request_count": int(r.request_count),
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "kb_type": kb.kb_type if kb is not None else None,
            })
        service_usage = [
            {
                "service_id": r.service_id,
                # 服务可能已删（`services` LEFT JOIN 未命中）：回落 id，不留空白列
                "service_name": r.service_name or r.service_id,
                "request_count": int(r.request_count),
                "kb_count": int(r.kb_count or 0),
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            }
            for r in search["service_usage"]
        ]

        return {
            "total_requests": total_requests,
            "active_users": active_users,
            # 分母 0 时给 None（原型显示「—」）而不是 0 —— 0 看起来像「有人提问但人均 0」
            "avg_requests_per_user": round(total_requests / active_users, 1) if active_users else None,
            "searched_kbs": search["searched_kbs"],
            "total_kbs": total_kbs,
            "unused_kbs": max(0, total_kbs - search["searched_kbs"]),
            "used_services": search["used_services"],
            "request_trend": search["request_trend"],
            "pipeline_usage": search["pipeline_usage"],
            "top_questions": search["top_questions"],
            "question_topics": [],      # 三期（离线聚类，原型标注「每日批量更新」）
            "kb_usage": kb_usage,
            "service_usage": service_usage,
        }

    @_resilient(_empty_abnormal_page)
    async def abnormal_assets(self, tenant_id: str, user_id, data: dict) -> dict:
        """异常资产下钻（本期唯一新增端点）。

        ⚠️ **没有时间窗口**：这是「待人工复核」的队列，按 `days` 过滤会让久未处置的
        失败从默认视图消失（一条 19 天前的抽取失败会被 7 天默认值藏掉），
        也会与页 1 的 `abnormal_summary`（全时段口径）自相矛盾 ——
        汇总说待处理 1、下钻却 0 条。
        """
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        async with get_db_session() as session:
            kb_ids, kb_map = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"], scope["kb_id"]
            )
            items, total = await self._abnormal_items(
                session, tenant_id, kb_ids, kb_map,
                stages=scope["stages"], handled=scope["handled"],
                limit=scope["page_size"],
                offset=(scope["page"] - 1) * scope["page_size"],
            )
        return {
            "items": items,
            "total": total,
            "page": scope["page"],
            "page_size": scope["page_size"],
        }

    @_resilient(_empty_quality_anomalies)
    async def quality_anomalies(self, tenant_id: str, user_id, data: dict) -> dict:
        """解析质量异常（观测档）：最近 `quality_report.verdict='bad'` 的任务。

        本版本不接入解析质量观测，恒返回空列表。
        **观测档**：档位差不拦截检索、不改变任务结论。
        """
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        async with get_db_session() as session:
            kb_ids, kb_map = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"], scope["kb_id"]
            )
        if not kb_ids:
            return {"items": [], "total": 0}

        result = {"items": []}
        raw_items = [
            raw for raw in ((result or {}).get("items") or [])
            # 范围兜底：引擎已按 kb_ids 过滤，这里再挡一次跨范围泄露。
            if (raw.get("kb_id") or "") in kb_ids
        ]
        name_map: dict[str, str] = {}
        doc_ids = [raw.get("document_id") for raw in raw_items if raw.get("document_id")]
        if doc_ids:
            async with get_db_session() as session:
                rows = (
                    await session.execute(
                        select(
                            KnowledgeDocument.id,
                            KnowledgeDocument.file_name,
                        ).where(
                            KnowledgeDocument.tenant_id == tenant_id,
                            KnowledgeDocument.id.in_(doc_ids),
                            KnowledgeDocument.is_deleted == 0,
                        )
                    )
                ).all()
                name_map = {r.id: r.file_name for r in rows}
        items = []
        for raw in raw_items:
            kb_id = raw.get("kb_id") or ""
            kb = kb_map.get(kb_id)
            doc_id = raw.get("document_id")
            items.append({
                "document_id": doc_id,
                "document_name": name_map.get(doc_id) or doc_id,
                "kb_id": kb_id,
                "kb_name": getattr(kb, "name", "") if kb is not None else "",
                "task_id": raw.get("task_id"),
                "verdict": raw.get("verdict"),
                "reasons": raw.get("reasons") or [],
                "occurred_at": raw.get("occurred_at"),
            })
        return {"items": items, "total": len(items)}

    # ── 三期：列表钻取详情（原型「点名称进抽屉」）───────────────
    #
    # 三个方法共用的两条约定（改之前先读 `dtos/analytics.py` 三期段注释）：
    # ① 窗口/范围参数与打开列表时**同一组**（`days` 或 `start_date`+`end_date`，
    #    加 `domain_space_id`）—— 抽屉里的次数才等于列表里那个数字；
    #    前端不传不会报错，只会按默认 7 天算而「数字对不上」。
    # ② 定位键是**真实键**：A 用 `asset_id`、B 用 `kb_id`+`entity_type`+`canonical_name`
    #    三元组、C 用 `kb_id`。列表里的本体 `object_id` 是单向 sha 值，**查不了详情**。
    #
    # 「不存在」一律走 404（`ResourceNotFoundError`，`_resilient` 不吞 `JonexException`），
    # 而不是返回空壳 —— 空壳的语义是「降级」，两者混在一起前端就分不清要不要提示
    # 「内容已变更」。

    @_resilient(_empty_asset_detail)
    async def asset_detail(self, tenant_id: str, user_id, data: dict) -> dict:
        """抽屉 A · 数据资产详情。

        范围判据：文档所属的库必须在「本次范围」内 —— 与列表同源（`domain_space_id`
        传了就是空间范围，没传就是租户级全量）。这层校验挡住「拿空间 A 的凭证读
        空间 B 的资产」，`execute` 里的空间成员判定只保证「能进这个空间」。
        """
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        asset_id = scope["asset_id"]
        async with get_db_session() as session:
            _, kb_map = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"], scope["kb_id"]
            )
            doc = (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id == asset_id,
                        KnowledgeDocument.tenant_id == tenant_id,
                        KnowledgeDocument.is_deleted == 0,
                    )
                )
            ).scalars().first()
            # 「不存在 / 已被删 / 不在本次范围」合并成同一个 404：把后两者区分出来
            # 等于确认了「有这条资产，只是不给你看」（与 KB 详情的防探测同口径）。
            if doc is None or doc.knowledge_base_id not in kb_map:
                raise ResourceNotFoundError(
                    message=translate(
                        "err.kb.asset_not_found",
                        params={"asset_id": asset_id},
                        fallback=f"资产不存在: {asset_id}",
                    )
                )
            win_start, win_end, _ = await self._scope_window(session, scope)
            cite_count, last_cited_at = await self._cited_agg_asset(
                session, tenant_id, asset_id, win_start, win_end
            )
            kb = kb_map.get(doc.knowledge_base_id)

        return {
            "asset_id": doc.id,
            "file_name": doc.file_name,
            "asset_type": to_asset_type(classify_asset_category(doc.mime_type, doc.file_name)),
            "file_format": _file_format(doc.mime_type, doc.file_name),
            "file_size": int(doc.file_size) if doc.file_size is not None else None,
            # 数据来源：与异常抽屉同口径（`data_source_type` 为空的老数据回落 `file`）
            "source": doc.data_source_type or "file",
            "kb_id": doc.knowledge_base_id,
            # 富化不到（库已删）时回落 kb_id：非空 id 也比空白列强（与 `_enrich_cited` 一致）
            "kb_name": kb.name if kb is not None else doc.knowledge_base_id,
            # ⚠️ 这两列是 `TimestampMixin` 的**本地 naive** 时间，必须过
            # `_local_naive_to_iso`（裸 `isoformat()` 会丢时区，见该函数注释）
            "uploaded_at": _local_naive_to_iso(doc.created_at),
            "last_processed_at": _local_naive_to_iso(doc.updated_at),
            # 三条处理链的状态**各自透出原始码**，不在后端合成「当前处理状态」：
            # 同一个文档可能解析已 ready、本体抽取还在跑，合成一个词必然丢掉一半事实。
            "status": doc.status,
            "ontology_status": doc.ontology_status,
            "llm_wiki_compile_status": doc.llm_wiki_compile_status,
            "cite_count": cite_count,
            "last_cited_at": last_cited_at.isoformat() if last_cited_at else None,
        }

    @_resilient(_empty_ontology_entity_detail)
    async def ontology_entity_detail(self, tenant_id: str, user_id, data: dict) -> dict:
        """抽屉 B · Ontology 对象详情。

        ⚠️ 两块数据来自两个库，**降级口径不同**：属性/关系在 Neo4j（不可用 → 空 +
        `degraded=true`），命中次数在 PG（**照常返回**，没理由跟着降）。

        这直接决定 404 怎么判：只有「图在、但查不到这个三元组」才是 404。
        `degraded=true` 时问不出「实例还在不在」，只能出空壳 —— 把它当 404 会让
        Neo4j 一挂，整个知识资产页签点谁都报「对象不存在」。
        """
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        kb_id, entity_type, canonical_name = (
            scope["kb_id"], scope["entity_type"], scope["canonical_name"]
        )
        async with get_db_session() as session:
            _, kb_map = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"], kb_id
            )
            if kb_id not in kb_map:
                raise ResourceNotFoundError(
                    message=translate(
                        "err.kb.not_found",
                        params={"kb_id": kb_id},
                        fallback=f"知识库不存在: {kb_id}",
                    )
                )
            win_start, win_end, _ = await self._scope_window(session, scope)
            hit_count, last_used_at = await self._hit_agg_entity(
                session, tenant_id, kb_id, entity_type, canonical_name, win_start, win_end
            )
            kb_name = kb_map[kb_id].name

        detail, facts, relation_total, relation_truncated, degraded = (
            await self._entity_graph_detail(tenant_id, kb_id, entity_type, canonical_name)
        )
        # 图在、但没这个三元组 → 实例不存在，或**已被改名**（列表里的旧名字查不到了）。
        # 前端按「对象已变更，请刷新列表」处理；不要重试，重试还是这个名字。
        if detail is None and not degraded:
            raise ResourceNotFoundError(
                message=translate(
                    "err.kb.ontology_entity_not_found",
                    params={"canonical_name": canonical_name},
                    fallback=f"Ontology 对象不存在: {canonical_name}",
                )
            )
        detail = detail or {}

        return {
            "kb_id": kb_id,
            "kb_name": kb_name,
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "aliases": list(detail.get("aliases") or []),
            "description": detail.get("description"),
            "created_at": self._ts_to_iso(detail.get("created_at")),
            "updated_at": self._ts_to_iso(detail.get("updated_at")),
            "attributes": _attr_pairs(detail.get("attributes")),
            # `relation_total` 是**全部**关系数，不是 `relation_groups` 里目标的数量 ——
            # 后者受 `_NEO4J_RELATION_LIMIT` 和去重影响，拿它凑总数会把截断说成「就这么多」
            "relation_total": relation_total,
            "relation_groups": _relation_groups(facts),
            # 明细被上限截断：前端提示「仅显示前 N 条」，不要静默少列
            "relation_truncated": relation_truncated,
            "hit_count": hit_count,
            # 结构性恒 0：采用事件（`search_adoption_event`）只记 document，
            # **没有实体采用这一类埋点**（同列表 `UsedEntityItem.adopted_count`）
            "adopted_count": 0,
            "last_used_at": last_used_at.isoformat() if last_used_at else None,
            "degraded": degraded,
        }

    @_resilient(_empty_kb_detail)
    async def kb_detail(self, tenant_id: str, user_id, data: dict) -> dict:
        """抽屉 C · 知识库详情。

        ⚠️ **权限前置是 `KnowledgeInfoService.get()` 的 404 判据**，不另写一套：
        它会挡掉「非空间管理者且无 KB 授权」的访问（防探测 404）。数据运营页的空间
        成员本来就能检索到这个库的内容，但**不该**从这里读到库结构 —— 对象实例数、
        资产类型拆解、关联服务都是这个页面上新增的信息面。权限只有一份实现。

        本方法只补三块 `get()` 没有的：资产类型拆解、窗口内检索次数、关联领域服务。

        `object_instances` / `relation_instances` 直接取 `get(with_stats=True)` 算好的值：
        它已经处理了 llm-wiki 库要走 Wiki 图的问题（自己查 Neo4j 会把它们误报 0）。
        ⚠️ 代价：llm-wiki 库的 `with_stats=True` 会触发跨容器全图扫描（~2.6s）。
        抽屉是「点开一次」的请求，接受；**若将来把它塞进列表批量调用，必须先改这里**。
        """
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        kb_id = scope["kb_id"]
        async with get_db_session() as session:
            # 与 A/B 同一层范围校验：`domain_space_id` 传了就得对得上，
            # 否则会出现「按空间 A 过滤、却返回空间 B 的库」。
            _, kb_map = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"], kb_id
            )
            if kb_id not in kb_map:
                raise ResourceNotFoundError(
                    message=translate(
                        "err.kb.not_found",
                        params={"kb_id": kb_id},
                        fallback=f"知识库不存在: {kb_id}",
                    )
                )
            win_start, win_end, _ = await self._scope_window(session, scope)
            composition = await self._composition(session, tenant_id, [kb_id])
            services = await self._kb_services(session, tenant_id, [kb_id])
            request_count, last_request_at = await self._kb_request_usage(
                session, tenant_id, kb_id, win_start, win_end
            )
            # `KnowledgeInfo.updated_at` 同为本机 naive 列，取原始值自己转 ——
            # `to_dict()` 里是裸 `isoformat()`（不带时区偏移），直接用它会让这一个
            # 字段跟同一响应里其余 `*_at` 的口径不一致（见 `_local_naive_to_iso`）。
            kb_updated_at = (
                await session.execute(
                    select(KnowledgeInfo.updated_at).where(
                        KnowledgeInfo.id == kb_id,
                        KnowledgeInfo.tenant_id == tenant_id,
                    )
                )
            ).scalar()

        # ② 真正的权限门槛（404 防探测）。放在范围校验之后：先判「在不在范围内」
        #    再判「有没有权限」，两种情况对外都是 404，但先跑便宜的那步。
        info = await KnowledgeInfoService().get(
            kb_id, tenant_id, user_id=user_id, with_stats=True
        )

        # 只返回非零项：抽屉要列的是「这个库有什么」，把 7 类里恒为 0 的那几类也铺出来
        # 只是噪声（前端要拼的是标签组，不是固定列）。`asset_total` 仍是全量口径，
        # 与「数据资产」页签的 READY 文档数同一个数。
        by_type = [
            {"asset_type": c["asset_type"], "count": c["count"]}
            for c in composition
            if c["count"] > 0
        ]
        # ⚠️ 降级时两个计数**必须是 None，不能透明成 0**：`get(with_stats=True)` 在
        # Neo4j / 编译服务不可用时会把计数**填 0** 并置 `ontology_degraded=True`
        # （`knowledge_info_service.py:274-276` / `290-293`）。那个 0 是「没取到」的
        # 占位值，直接透出会让抽屉显示「编译资产：0 个对象、0 条关系」—— 库里明明有
        # 上千个对象，这是**对用户陈述一件假事**。转成 None 后前端渲染「—」，
        # 由 `degraded` 提供「暂不可用」文案（DTO 的 `Optional[int]` 就是这个语义）。
        degraded = bool(info.get("ontology_degraded", False))
        return {
            "kb_id": kb_id,
            "kb_name": info.get("name") or kb_id,
            "kb_type": info.get("kb_type"),
            "ontology": {
                "object_instances": None if degraded else int(info.get("entity_count") or 0),
                "relation_instances": None if degraded else int(info.get("relation_count") or 0),
                "degraded": degraded,
            },
            "asset_total": sum(c["count"] for c in composition),
            "assets_by_type": by_type,
            "services": services,
            "updated_at": _local_naive_to_iso(kb_updated_at),
            "request_count": request_count,
            "last_request_at": last_request_at.isoformat() if last_request_at else None,
        }

    @_resilient(_empty_service_detail)
    async def service_detail(self, tenant_id: str, user_id, data: dict) -> dict:
        """抽屉 D · 领域服务详情。

        ⚠️ **本方法刻意不抛 404**（与 A/B/C 不同，理由见 `ServiceDetail` 的 DTO 注释）：
        抽屉的主体是列表那一行**归因记录**，服务实体只是装饰。列表在 `services` 查不到
        时本来就回落 `service_id` 显示，所以「行在、实体不在」是列表已承认的合法状态
        （软删服务、历史回填把空间 id 写进 `entry_service_id` 的遗留数据）。
        取不到就留空属性块、**使用情况照常返回** —— 使用情况来自事件表，与服务实体无关。

        范围外服务的属性**同样不填**（回落 id），两种情形对外完全同形 ——
        否则「404 / 200」或「真名 / 机器 id」的差别就成了「这个 id 在别的空间存不存在」
        的探测口子。
        """
        tenant_id = require_tenant(tenant_id)
        scope = _parse_scope(data)
        service_id = scope["service_id"]
        async with get_db_session() as session:
            kb_ids, kb_map = await self._kb_scope(
                session, tenant_id, scope["domain_space_id"]
            )
            win_start, win_end, _ = await self._scope_window(session, scope)
            svc = (
                await session.execute(
                    select(DomainService).where(
                        DomainService.id == service_id,
                        DomainService.tenant_id == tenant_id,
                        DomainService.is_deleted == 0,
                    )
                )
            ).scalars().first()
            # 属性块只认「本空间的服务」：跨空间的同 id 服务不得从这里透出名称与状态。
            # 判空后与「本来就没有这条服务」走同一条路（回落 id），不留可观测差异。
            if (
                svc is not None
                and scope["domain_space_id"]
                and svc.space_id != scope["domain_space_id"]
            ):
                svc = None
            bound_kb_ids = await self._service_kbs(session, tenant_id, service_id, kb_ids)
            request_count, last_request_at = await self._service_request_usage(
                session, tenant_id, kb_ids, service_id, win_start, win_end
            )

        return {
            "service_id": service_id,
            "service_name": (svc.name if svc is not None else None) or service_id,
            "status": svc.status if svc is not None else None,
            # `enabled` 列是 SmallInteger（1/0），DTO 是 bool|None：
            # 实体取不到 → None（前端渲染「—」），不要把它当成「已停用」
            "enabled": (
                bool(svc.enabled)
                if svc is not None and svc.enabled is not None
                else None
            ),
            "knowledge_bases": [
                {"kb_id": kid, "kb_name": kb_map[kid].name if kid in kb_map else kid}
                for kid in bound_kb_ids
            ],
            "request_count": request_count,
            "last_request_at": last_request_at.isoformat() if last_request_at else None,
        }

    # ── 其余统计辅助 ────────────────────────────────────────────

    async def _count_docs_ready(self, session, tenant_id: str, kb_ids: list) -> int:
        if not kb_ids:
            return 0
        result = await session.execute(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.is_deleted == 0,
                KnowledgeDocument.status == DocStatus.READY.value,
                KnowledgeDocument.knowledge_base_id.in_(kb_ids),
            )
        )
        return int(result.scalar_one())

    async def _count_sources(self, session, tenant_id: str, kb_ids: list) -> int:
        if not kb_ids:
            return 0
        result = await session.execute(
            select(func.count())
            .select_from(KnowledgeDataSource)
            .where(
                KnowledgeDataSource.tenant_id == tenant_id,
                KnowledgeDataSource.is_deleted == 0,
                KnowledgeDataSource.status == "active",
                KnowledgeDataSource.knowledge_base_id.in_(kb_ids),
            )
        )
        return int(result.scalar_one())

    async def _count_new_sources(self, session, tenant_id: str, kb_ids: list,
                                 win_start: date) -> int:
        """窗口内新增数据源（KPI 卡副文案）。口径与 `_count_sources` 一致（只算 active）。"""
        if not kb_ids:
            return 0
        result = await session.execute(
            select(func.count())
            .select_from(KnowledgeDataSource)
            .where(
                KnowledgeDataSource.tenant_id == tenant_id,
                KnowledgeDataSource.is_deleted == 0,
                KnowledgeDataSource.status == "active",
                KnowledgeDataSource.knowledge_base_id.in_(kb_ids),
                KnowledgeDataSource.created_at >= win_start,
            )
        )
        return int(result.scalar_one())

    async def _access_trend(self, session, tenant_id: str, kb_ids: list,
                            win_start: date, window_dates: list) -> list:
        """数据接入趋势：用 `documents.created_at` 近似（上传时刻，非可用时刻）。

        `asset_status_event` 本期默认不写，所以拿不到「真实 ready 时刻」；
        切到它是可选项，不是本期的默认口径。
        窗口由调用方算好传进来（`_scope_window`），本方法不再自己推。
        """
        if not kb_ids:
            return self._fill_trend({}, window_dates)
        # ⚠️ `to_char` 表达式必须在 select 与 group_by 里复用同一个**表达式对象**：
        # 分别写两次会被渲染成两个绑定参数（`to_char(created_at, $1)` 与
        # `to_char(created_at, $2)`），PG 判定两者不相等 → GroupingError。
        day_expr = func.to_char(KnowledgeDocument.created_at, "YYYY-MM-DD")
        rows = (
            await session.execute(
                select(day_expr, func.count())
                .select_from(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.is_deleted == 0,
                    KnowledgeDocument.status == DocStatus.READY.value,
                    KnowledgeDocument.knowledge_base_id.in_(kb_ids),
                    KnowledgeDocument.created_at >= win_start,
                )
                .group_by(day_expr)
            )
        ).all()
        return self._fill_trend({r[0]: int(r[1]) for r in rows}, window_dates)

    async def _composition(self, session, tenant_id: str, kb_ids: list) -> list:
        """资产构成：按 (mime_type, 扩展名) 分组，Python 侧归 7 类。

        ⚠️ **必须带上扩展名**：`.json`/`.yaml`/`.parquet` 这类文件常常只靠
        扩展名才能识别（mime 是 `application/octet-stream`），只按 mime_type 分组
        会让它们永远进不了 structured 类（一期就是把 `file_name` 传了 `None`）。
        按 (mime, 扩展名) 分组而不是逐行取回，是为了结果集有界。
        """
        counts = {c: 0 for c in _COMPOSITION_CATEGORIES}
        if not kb_ids:
            return [
                {"category": c, "asset_type": to_asset_type(c), "count": 0}
                for c in _COMPOSITION_CATEGORIES
            ]
        # `substring(... from '(\.[^.]*)$')` 取出「含点的扩展名」
        rows = (
            await session.execute(
                text("""
SELECT d.mime_type,
       lower(substring(d.file_name from '(\\.[^.]*)$')) AS ext,
       count(*) AS cnt
  FROM knowledge_base.knowledge_documents d
 WHERE d.tenant_id = :tenant_id
   AND d.is_deleted = 0
   AND d.status = :ready
   AND d.knowledge_base_id = ANY(:kb_ids)
 GROUP BY d.mime_type, lower(substring(d.file_name from '(\\.[^.]*)$'))
"""),
                {
                    "tenant_id": tenant_id,
                    "kb_ids": list(kb_ids),
                    "ready": DocStatus.READY.value,
                },
            )
        ).all()
        for mime_type, ext, cnt in rows:
            # ⚠️ `ext` 是**含点的扩展名**（`.docx`），不能直接当文件名喂进去：
            # `os.path.splitext('.docx')` 返回 `('.docx', '')` —— 前导点被当成隐藏文件名，
            # 扩展名解析为空 → 整批落进 `other`。docx/pptx/xlsx/csv 只有扩展名这一条判据
            # （mime 是 `application/vnd.openxmlformats-…` 或 `application/octet-stream`），
            # 所以「其他」会凭空吃掉所有 Office 与表格文件。补一个基名规避。
            category = classify_asset_category(mime_type, f"f{ext}" if ext else None)
            counts[category] = counts.get(category, 0) + int(cnt)
        return [
            {"category": c, "asset_type": to_asset_type(c), "count": counts[c]}
            for c in _COMPOSITION_CATEGORIES
        ]

    async def _enrich_cited(self, session, tenant_id: str, top_cited: list, kb_map=None) -> list:
        """高频引用 Top10 富化。

        `kb_map` 不传时（兼容老调用点）`kb_name` 回落 `kb_id`，不会报错。
        """
        if not top_cited:
            return []
        doc_ids = [c["doc_id"] for c in top_cited]
        rows = (
            await session.execute(
                select(
                    KnowledgeDocument.id,
                    KnowledgeDocument.file_name,
                    KnowledgeDocument.mime_type,
                    KnowledgeDocument.knowledge_base_id,
                )
                .where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.is_deleted == 0,
                    KnowledgeDocument.id.in_(doc_ids),
                )
            )
        ).all()
        doc_map = {r[0]: (r[1], r[2], r[3]) for r in rows}
        kb_map = kb_map or {}
        result = []
        for c in top_cited:
            file_name, mime_type, kb_id = doc_map.get(c["doc_id"], (c["doc_id"], None, None))
            kb = kb_map.get(kb_id) if kb_id else None
            last_cited = c.get("last_cited_at")
            result.append({
                "asset_id": c["doc_id"],
                "file_name": file_name,
                "mime_type": mime_type,
                # 前端「前往知识库」靠它拼目标地址；没有 kb_id 就只能原地不动
                "kb_id": kb_id,
                # 原型表格「所属知识库」列；富化不到时回落 kb_id（非空 id 也比空白强）
                "kb_name": kb.name if kb is not None else kb_id,
                "asset_type": to_asset_type(classify_asset_category(mime_type, file_name)),
                "cite_count": c["cite_count"],
                # 原型表格「最近引用时间」列
                "last_cited_at": (
                    last_cited.isoformat() if hasattr(last_cited, "isoformat") else last_cited
                ),
            })
        return result

    # ── 三期钻取详情的取数辅助 ──────────────────────────────────

    async def _cited_agg_asset(self, session, tenant_id: str, asset_id: str,
                               win_start: date, win_end: date):
        """单个数据资产在窗口内的被采用次数，返回 `(cite_count, last_cited_at)`。

        与「高频引用资产 Top10」**逐字同口径**：`count(DISTINCT answer_id)`
        （同一次回答里同一资产重复出现只算 1 次）、窗口边界 `>= win_start AND < win_end`。

        ⚠️ **不按 `kb_id` 过滤**：`target_id` 就是 `knowledge_documents.id`（全表主键、
        全局唯一），再叠一层 `a.kb_id = 文档的 kb` 在数据正常时等价、在事件行 kb_id
        异常时反而与列表算出的数**不一致**。租户边界仍由 `tenant_id` 守住。
        """
        row = (
            await session.execute(
                text(f"""
SELECT count(DISTINCT a.answer_id) AS cite_count,
       max(a.adopted_at) AS last_cited_at
  FROM knowledge_base.search_adoption_event a
 WHERE a.tenant_id = :tenant_id
   AND a.is_deleted = 0
   AND a.target_type = '{TARGET_DOCUMENT}'
   AND a.target_id = :asset_id
   AND a.adopted_at >= CAST(:win_start AS DATE)
   AND a.adopted_at <  CAST(:win_end AS DATE)
"""),
                {
                    "tenant_id": tenant_id,
                    "asset_id": asset_id,
                    **self._window_params(win_start, win_end),
                },
            )
        ).first()
        if row is None:
            return 0, None
        return int(row.cite_count or 0), row.last_cited_at

    async def _hit_agg_entity(self, session, tenant_id: str, kb_id: str, entity_type: str,
                              canonical_name: str, win_start: date, win_end: date):
        """单个本体实例在窗口内的命中次数，返回 `(hit_count, last_hit_at)`。

        判据与「高频使用本体实例 Top10」完全一致（`_top_hits`）：按三元组定位、
        `count(DISTINCT request_id)`、同窗口边界。⚠️ 用 `canonical_name` 而**不是**
        列表里的 `object_id` —— 后者是 sha 值，不在事件表里做 join 键。
        """
        row = (
            await session.execute(
                text(f"""
SELECT count(DISTINCT s.request_id) AS hit_count,
       max(s.hit_at) AS last_hit_at
  FROM knowledge_base.search_hit_event s
 WHERE s.tenant_id = :tenant_id
   AND s.is_deleted = 0
   AND s.target_type = '{TARGET_ONTOLOGY_INSTANCE}'
   AND s.kb_id = :kb_id
   AND s.entity_type = :entity_type
   AND s.canonical_name = :canonical_name
   AND s.hit_at >= CAST(:win_start AS DATE)
   AND s.hit_at <  CAST(:win_end AS DATE)
"""),
                {
                    "tenant_id": tenant_id,
                    "kb_id": kb_id,
                    "entity_type": entity_type,
                    "canonical_name": canonical_name,
                    **self._window_params(win_start, win_end),
                },
            )
        ).first()
        if row is None:
            return 0, None
        return int(row.hit_count or 0), row.last_hit_at

    async def _kb_request_usage(self, session, tenant_id: str, kb_id: str,
                                win_start: date, win_end: date):
        """单个知识库在窗口内的检索请求数 / 最近一次检索时间。

        ⚠️ `count(*)` 而**不是** `count(DISTINCT request_id)`：必须与「知识库使用情况」
        表里那一行**逐字对上**（`_search_agg` 的 kb_usage CTE 用的就是 `count(*)`）。
        这里换个更正确的判据，页面上立刻出现「列表 38,620 / 抽屉 38,611」这种
        对不上的数字 —— 要改就列表与抽屉两处一起改（`search_request_event` 无唯一
        约束，重复行确实会虚增，那是个独立的口径问题）。
        """
        row = (
            await session.execute(
                text(_REQ_CTE + """
SELECT count(*) AS request_count,
       max(r.received_at) AS last_request_at
  FROM req r
  CROSS JOIN LATERAL jsonb_array_elements_text(r.kb_ids) AS x(kb)
 WHERE x.kb = :kb_id
   AND (r.user_id IS NULL OR r.user_id <> 'anonymous')
"""),
                {
                    "tenant_id": tenant_id,
                    "kb_id": kb_id,
                    **self._window_params(win_start, win_end),
                },
            )
        ).first()
        if row is None:
            return 0, None
        return int(row.request_count or 0), row.last_request_at

    async def _kb_services(self, session, tenant_id: str, kb_ids: list) -> list:
        """知识库绑定的领域服务（抽屉 C「关联领域服务」）。

        **只看绑定关系、不看检索事件**：抽屉要回答的是「这个库挂在哪些服务下」，
        与「近 7 天有谁经服务搜过它」是两件事（后者是 `service_usage`）。

        服务可能已被删（`services` LEFT JOIN 未命中）→ 回落 `service_id` 当名字，
        与 `_search_agg` 的服务归因同口径，不留空白列。
        """
        if not kb_ids:
            return []
        rows = (
            await session.execute(
                text("""
SELECT DISTINCT skb.service_id AS service_id, ds.name AS service_name
  FROM knowledge_base.service_knowledge_bases skb
  LEFT JOIN knowledge_base.services ds
         ON ds.id = skb.service_id
        AND ds.tenant_id = skb.tenant_id
        AND ds.is_deleted = 0
 WHERE skb.tenant_id = :tenant_id
   AND skb.is_deleted = 0
   AND skb.kb_id = ANY(:kb_ids)
 ORDER BY skb.service_id ASC
"""),
                {"tenant_id": tenant_id, "kb_ids": list(kb_ids)},
            )
        ).all()
        return [
            {"service_id": r.service_id, "service_name": r.service_name or r.service_id}
            for r in rows
        ]

    async def _service_kbs(self, session, tenant_id: str, service_id: str,
                           kb_ids: list) -> list:
        """领域服务在**本次范围内**绑定的知识库 id（抽屉 D「关联知识库」）。

        过滤条件与列表行 `kb_count`（`_search_agg` 的 `service_knowledge_bases` join）
        **逐字相同** —— 少一个条件，`len(knowledge_bases)` 就和列表那一行的 `kb_count`
        对不上，而这种错位不报错、只是数字不同。

        名字由调用方用 `_kb_scope` 的 `kb_map` 富化（同一份范围，不再查一次）。
        """
        if not kb_ids:
            return []
        rows = (
            await session.execute(
                text("""
SELECT DISTINCT skb.kb_id AS kb_id
  FROM knowledge_base.service_knowledge_bases skb
 WHERE skb.tenant_id = :tenant_id
   AND skb.is_deleted = 0
   AND skb.service_id = :service_id
   AND skb.kb_id = ANY(:kb_ids)
 ORDER BY skb.kb_id ASC
"""),
                {"tenant_id": tenant_id, "service_id": service_id, "kb_ids": list(kb_ids)},
            )
        ).all()
        return [r.kb_id for r in rows]

    async def _service_request_usage(self, session, tenant_id: str, kb_ids: list,
                                     service_id: str, win_start: date, win_end: date):
        """单个领域服务在窗口内的调用次数 / 最近调用时间（抽屉 D）。

        ⚠️ 必须与「领域服务使用情况」表那一行**逐字对上**，所以复用 `_PAIR_CTE`
        同款 `count(DISTINCT request_id)`（`UNION` 已去重，这里再 DISTINCT 一层是为了
        与列表的写法保持一模一样）。把 `entry_service_id` 那一支省掉、或改成
        `count(*)`，页面上立刻出现「列表 87 / 抽屉 79」这种对不上的数字。
        """
        row = (
            await session.execute(
                text(_PAIR_CTE + """
SELECT count(DISTINCT p.request_id) AS request_count,
       max(rq.received_at) AS last_used_at
  FROM pair p
  LEFT JOIN req rq ON rq.request_id = p.request_id
 WHERE p.service_id = :service_id
"""),
                {
                    "tenant_id": tenant_id,
                    "kb_ids": list(kb_ids),
                    "service_id": service_id,
                    **self._window_params(win_start, win_end),
                },
            )
        ).first()
        if row is None:
            return 0, None
        return int(row.request_count or 0), row.last_used_at

    async def _entity_graph_detail(self, tenant_id: str, kb_id: str, entity_type: str,
                                   canonical_name: str):
        """取实例本体（别名/属性/时间）+ 一跳邻居事实。

        返回 `(detail | None, facts, relation_total, relation_truncated, degraded)`：

        - `degraded=True` → driver 取不到或查询报错，detail 恒为 `None`，
          **不代表实例不存在**（调用方据此不判 404）；
        - `degraded=False, detail is None` → 图是通的，就是没有这个三元组
          （不存在 / 已改名），调用方判 404。

        与 `_neo4j_alive` / `_neo4j_stats` 同款降级：Neo4j 挂了不影响基础能力。
        """
        try:
            driver = get_neo4j_driver()
            if driver is None:
                return None, [], 0, False, True
            gdao = OntologyGraphRepository(driver)
            detail = await gdao.get_entity(tenant_id, kb_id, entity_type, canonical_name)
            if detail is None:
                return None, [], 0, False, False
            facts, total, truncated = await gdao.neighbors_by_entity(
                tenant_id, kb_id, entity_type, canonical_name,
                limit=_NEO4J_RELATION_LIMIT,
            )
            return detail, facts, total, truncated, False
        except Exception:  # noqa: BLE001 — Neo4j 降级：属性/关系给空，命中次数照常（在 PG）
            logger.warning(
                "[jonex] 数据运营 Ontology 对象详情降级 tenant=%s kb=%s name=%s",
                tenant_id, kb_id, canonical_name, exc_info=True,
            )
            return None, [], 0, False, True

    @staticmethod
    def _kb_distribution(kb_distribution: dict, kb_map: dict, total: int) -> list:
        """知识库本体实例分布：Top5 + 其他（kb_id=null），ratio 相对全部实体总数。"""
        items = sorted(kb_distribution.items(), key=lambda kv: -kv[1])
        top = items[:5]
        rest = sum(c for _, c in items[5:])
        result = []
        for kb_id, count in top:
            kb = kb_map.get(kb_id)
            result.append({
                "kb_id": kb_id,
                "kb_name": kb.name if kb is not None else kb_id,
                "count": int(count),
                "ratio": round(count / total, 4) if total else 0.0,
            })
        if rest:
            result.append({
                "kb_id": None,
                "kb_name": "其他",
                "count": int(rest),
                "ratio": round(rest / total, 4) if total else 0.0,
            })
        return result


__all__ = ["AnalyticsService", "classify_asset_category", "to_asset_type"]
