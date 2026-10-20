# [jonex] 新增文件 — Jonex Wiki Atomic Capability（进程内 import 引擎，B1 单容器）
import asyncio
import hashlib
import html
import json
import re
import time
from typing import Any

from jonex_core.capability.base import BaseCapability
from jonex_core.capability.models import (
    CapabilityMetadata,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityType,
)

# apply_schema 投影:平台 AGENTS.md → 引擎 PUBLIC_PROMPT_KEYS.compile_main
_SCHEMA_PROMPT_KEY = "compile_main"

# [jonex] 引擎任务终态词汇 → 平台(OpenKB 对账)词汇。引擎 TaskStatus 是 done/failed/cancelled，
# 而 knowledge_base 的 patrol_*_compile 只认 completed/failed/running。不归一化会让
# 「编译成功(done)」永远卡在 llm_wiki_compile_status=compiling。其余 queued/compiling/parsing/
# running 原样透传（巡逻按 in-flight 跳过，不误判）。
_TASK_STATUS_ALIAS = {
    "done": "completed",
    "cancelled": "failed",
}

# [jonex] Wiki 页面树投影：引擎页面类型 → 前端(OpenKB 词汇) section。
# 前端 WikiPageBrowser 只认 summaries/concepts/entities 三类 section。
_SECTION_BY_TYPE = {"source": "summaries", "table": "entities", "concept": "concepts"}
# section → 引擎页面目录前缀（concept 无固定目录前缀，留空）。
_SECTION_PREFIX = {"summaries": "sources/", "entities": "tables/", "concepts": ""}
# 链入本页（What links here）单次返回的反向链接条数上限。
_BACKLINK_LIMIT = 30
# 页面级 References 摘录截断长度（展示侧清洗用）。
_MAX_EXCERPT_LEN = 240
# claim 级审计（列式视图「溯源审计」）单次返回上限
_MAX_CLAIMS_PER_QUERY = 500
_MAX_CLAIM_TEXT_LEN = 400

# [jonex] 引擎 query 是异步任务(app.query → task_id)，同步等待完成时的轮询参数。
# 等待须覆盖引擎180s运行预算和冷加载，且小于 RemoteJonexWikiClient 的300s。
# Sidecar/Gateway外层须依次晚于本层截止，不得在引擎正常运行时先断开。
_QUERY_POLL_INTERVAL_S = 0.5
_QUERY_POLL_TIMEOUT_S = 240.0


def _page_stem(path: str, page_type: str) -> str:
    """引擎页面路径 → 前端 stem（剥 section 目录前缀 + .md）。"""
    stem = path[:-3] if path.endswith(".md") else path
    prefix = _SECTION_PREFIX.get(_SECTION_BY_TYPE.get(page_type, "concepts"), "")
    if prefix and stem.startswith(prefix):
        stem = stem[len(prefix):]
    return stem


def _section_to_engine_path(section: str, stem: str) -> str:
    """前端 {section}/{stem} → 引擎页面路径（相对 wiki/，含 .md）。

    stem 允许带 .md（页面 markdown 内的 wikilink 就是 canonical 路径形态），
    这里统一剥一次，避免出现 xxx.md.md 的历史坑。
    """
    if stem.endswith(".md"):
        stem = stem[:-3]
    return _SECTION_PREFIX.get(section, "") + stem + ".md"


def _target_stem(target: str) -> str:
    """链接 target_id（canonical 路径，含 .md）→ 实体 stem（与 _page_stem 同口径）。

    供图谱关系匹配：关系 target 必须与实体 id 用同一套 stem 口径，否则
    前端 _openkb_graph_view 的 ``source/target in keep`` 会裁掉边。
    """
    target = (target or "").split("#", 1)[0].strip()
    if not target:
        return ""
    if target.startswith("sources/"):
        return _page_stem(target, "source")
    if target.startswith("tables/"):
        return _page_stem(target, "table")
    return _page_stem(target, "concept")


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_excerpt(text: str) -> str:
    """把引擎摘录清成可读文本：剥 HTML 标签（表格来源页的摘录是 <td> 片段）、
    反转义实体、压空白，再截断到 _MAX_EXCERPT_LEN。"""
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub(" ", str(text))
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return cleaned[:_MAX_EXCERPT_LEN]


_TD_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)


def _clean_title(raw: Any, fallback: str, *, limit: int = 60, alt: Any = None) -> str:
    """页面标题清洗（阅读侧统一入口）。

    真实产物里表格页的 frontmatter title 可能是**整段 HTML**（`<table>`、或整行
    `<tr><td>修订前</td><td>修订后</td>…`），直接上屏会撑爆目录栏、条目页标题与
    图谱节点标签。清洗顺序：

    1. HTML 表格标题 → 取首行单元格文本拼成列名（`修订前 · 修订后`）——这才是
       这张表在语义上的名字；
    2. 剥标签后的纯文本；
    3. 备用标题（navigation.json 里的 title，同样是 HTML 时无用）；
    4. 页面 stem。
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
    for candidate in (plain(raw_text), plain(alt)):
        if candidate:
            return candidate if len(candidate) <= limit else candidate[:limit].rstrip() + "…"
    return fallback


def _normalize_task_status(task: dict) -> dict:
    """把引擎任务字典的 status 字段归一为平台(OpenKB)词汇。"""
    t = dict(task)
    status = t.get("status")
    if status in _TASK_STATUS_ALIAS:
        t["status"] = _TASK_STATUS_ALIAS[status]
    return t


def _as_int(value) -> int:
    """[jonex] 宽松取非负整数（引擎计数/耗时缺失时归 0，不抛错）。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _outline_request(data: dict) -> dict:
    """[jonex] 目录请求体：剥掉 client 包装字段（kb/user_id），只留引擎 draft/adjust 认识的字段。"""
    return {k: v for k, v in (data or {}).items() if k not in ("kb", "user_id")}


def _normalize_citations(raw) -> list:
    """[jonex] 引擎 citation → 平台引用条目。

    引擎已核验的引用带六字段 + page_path（承载该证据的 Wiki 页路径）。这里只做
    形状归一：缺字段补空串、非 dict / 无 document_id 的条目丢弃（无 document_id 在
    平台上点不开原文），绝不让形状问题冒泡成 500。
    """
    out: list = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get("document_id") or "").strip()
        if not doc_id:
            continue
        out.append({
            "document_id": doc_id,
            "handle": str(item.get("handle") or ""),
            "chunk_id": str(item.get("chunk_id") or ""),
            "passage_id": str(item.get("passage_id") or ""),
            "source_id": str(item.get("source_id") or ""),
            "claim_id": str(item.get("claim_id") or ""),
            "excerpt": str(item.get("excerpt") or ""),
            # 平台侧「引用 ↔ Wiki 页」定位依据
            "page_path": str(item.get("page_path") or ""),
            "evidence_ref": item.get("evidence_ref") or None,
        })
    return out


_ACTIONS = {
    "init_kb", "compile_parsed_document", "compile_parsed_documents", "query", "status", "list_contents",
    "recompile", "remove_document", "delete_kb", "list_graph", "read_page",
    "list_wiki_contents", "get_compile_status", "list_compile_tasks", "get_document_chunks",
    "apply_schema", "reparse", "cancel_task", "cancel_query", "resolve_evidence",
    "compilation_state", "pause_compilation", "resume_compilation",
    # [jonex] 阅读侧：反向链接（链入本页）与 claim 级溯源审计
    "get_wiki_backlinks", "get_wiki_claims",
    # [jonex] Wiki 阅读目录（读/起草/调整，目录功能下沉到引擎）
    "list_wiki_outline", "draft_wiki_outline", "adjust_wiki_outline",
}


class JonexWikiCapability(BaseCapability):
    """Jonex Wiki 知识编译能力（进程内引擎，对齐 OpenKB）。

    由 deploy/start_capability.py 加载（CAPABILITY_KIND=atomic, CAPABILITY_NAME=jonex_wiki）。
    B1 单容器：与 OpenKB 一致，进程内 ``import jonex_wiki`` 直接调 ``WikiApplication``，
    不经 FastAPI 路由 / HTTP 转调；引擎与能力共享同一 pydantic v2 进程。
    """

    def __init__(self):
        super().__init__()
        self._app = None

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="jonex_wiki",
            capability_name="Jonex Wiki Compiler",
            capability_type=CapabilityType.ATOMIC,
            version="v1",
            description="Jonex Wiki 编译引擎 — 消费平台解析产物,LLM 编译为结构化 Wiki（进程内 import 引擎）",
        )

    async def initialize(self) -> None:
        """进程内启动引擎运行时（DB 接线 + 任务 executor + 恢复），与独立引擎 lifespan 同源。"""
        from jonex_wiki.application import WikiApplication
        from jonex_wiki.infra.settings import Settings

        settings = Settings()   # 缺必填 env 在此 fail-fast（api_key / llm_binding / embedding）
        self._app = WikiApplication(settings)
        await self._app.start()

    async def shutdown(self) -> None:
        if self._app is not None:
            await self._app.stop()

    async def validate_input(self, request: CapabilityRequest) -> bool:
        action = request.payload.get("action", "")
        if action not in _ACTIONS:
            return False
        data = request.payload.get("data") or {}
        if action == "compile_parsed_documents":
            documents = data.get("documents")
            return (isinstance(documents, list) and bool(documents) and all(
                isinstance(d, dict) and d.get("source_path") and d.get("source_name")
                and d.get("document_id") for d in documents))
        if action == "compile_parsed_document":
            return bool(data.get("source_path") and data.get("source_name"))
        if action == "query":
            return bool(str(data.get("question") or "").strip())
        if action in ("remove_document", "reparse", "recompile", "get_document_chunks"):
            return bool(data.get("document_id"))
        if action == "read_page":
            return bool(str(data.get("path") or "").strip())
        if action == "get_wiki_backlinks":
            return bool(str(data.get("path") or "").strip())
        if action == "get_wiki_claims":
            # 审计视图必须限定范围（按页面或按来源文档），否则一次全库 claim 没意义
            return bool(str(data.get("path") or "").strip()
                        or str(data.get("document_id") or "").strip())
        if action == "adjust_wiki_outline":
            return bool(str(data.get("instruction") or "").strip())
        if action in ("get_compile_status", "cancel_task"):
            return bool(str(data.get("task_id") or "").strip())
        if action == "apply_schema":
            # agents_md 是「编译偏好 suffix」，空串合法（= 无偏好 = 引擎纯固定骨架）。
            return "agents_md" in data
        return True

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        from jonex_wiki.kernel.errors import DomainError

        action = request.payload["action"]
        data = request.payload.get("data") or {}
        kb_id = request.payload.get("knowledge_base_id") or data.get("kb_id")
        tenant_id = request.tenant_id
        if not kb_id:
            return CapabilityResponse.error(request.request_id, 400, "缺少 knowledge_base_id",
                                            details={"error_code": "MISSING_PARAM"})
        try:
            result = await self._dispatch(action, tenant_id, kb_id, data)
            return CapabilityResponse.ok(request.request_id, data=result)
        except DomainError as exc:
            # 引擎业务错误:稳定 error_code 透传给 knowledge_base（与原 HTTP 信封口径一致）
            return CapabilityResponse.error(request.request_id, exc.http_status, str(exc),
                                            details={"error_code": exc.code})
        except Exception as exc:  # noqa: BLE001
            return CapabilityResponse.error(request.request_id, 500, str(exc),
                                            details={"error_code": "JONEX_WIKI_INTERNAL"})

    async def _dispatch(self, action, tenant_id, kb_id, data) -> dict:
        app = self._app
        if action == "init_kb":
            # jonex_wiki KB 隐式:首次 ingest 即建,无显式 init
            return {"initialized": True}
        if action == "compile_parsed_documents":
            return await app.ingest_parsed_documents(tenant_id, kb_id, data["documents"])
        if action == "compile_parsed_document":
            return await app.ingest_parsed_document(tenant_id, kb_id, data["source_path"],
                                                    data["source_name"], data.get("document_id"))
        if action == "query":
            return await self._query_sync(app, tenant_id, kb_id,
                                          str(data.get("question") or ""),
                                          bool(data.get("return_trace")),
                                          thinking_mode=data.get("thinking_mode", "fast"),
                                          progress_session=data.get("progress_session"))
        if action == "resolve_evidence":
            from jonex_wiki.adapters.evidence_view import resolve_evidence
            return await resolve_evidence(app, tenant_id, kb_id, data["query_id"], data["handle"])
        if action == "cancel_query":
            state = await app.get_task(tenant_id, data["query_id"])
            if state.get("kb_id") != kb_id:
                raise ValueError("Query does not belong to this knowledge base")
            return await app.cancel_task(tenant_id, data["query_id"])
        if action == "remove_document":
            return await app.remove_document(tenant_id, kb_id, data["document_id"])
        if action == "get_document_chunks":
            return await app.get_document_chunks(tenant_id, kb_id, data["document_id"])
        if action == "reparse":
            return await app.reparse_document(tenant_id, kb_id, data["document_id"])
        if action == "recompile":
            return await app.recompile_document(tenant_id, kb_id, data["document_id"])
        if action == "list_wiki_contents":
            # [jonex] view=counts：只要三类页数（编译资产 tab 的统计卡/tab 计数
            # 只用到数字，没必要把上千个页面对象搬回去）。缺省仍返回页面条目。
            if str(data.get("view") or "") == "counts":
                return await self._count_wiki_pages(app, tenant_id, kb_id)
            return await self._list_wiki_contents(app, tenant_id, kb_id,
                                                  str(data.get("document_id") or ""))
        if action in ("status", "list_contents"):
            # 内容清单 = 已发布 Wiki 页面清单
            return await app.list_pages(tenant_id, kb_id)
        if action == "read_page":
            return await self._read_page(app, tenant_id, kb_id, str(data.get("path") or ""))
        if action == "list_graph":
            return await self._list_graph(app, tenant_id, kb_id,
                                          str(data.get("document_id") or ""))
        if action == "get_wiki_backlinks":
            return await self._get_backlinks(app, tenant_id, kb_id,
                                             str(data.get("path") or ""),
                                             str(data.get("document_id") or ""))
        if action == "get_wiki_claims":
            return await self._get_claims(app, tenant_id, kb_id,
                                          str(data.get("path") or ""),
                                          str(data.get("document_id") or ""),
                                          _as_int(data.get("limit")) or _MAX_CLAIMS_PER_QUERY)
        if action == "list_wiki_outline":
            return await app.get_outline(tenant_id, kb_id, str(data.get("document_id") or ""))
        if action == "draft_wiki_outline":
            return await app.draft_outline(
                tenant_id, kb_id, _outline_request(data), user_id=data.get("user_id"))
        if action == "adjust_wiki_outline":
            return await app.adjust_outline(
                tenant_id, kb_id, _outline_request(data), user_id=data.get("user_id"))
        if action == "get_compile_status":
            return _normalize_task_status(await app.get_task(tenant_id, data["task_id"]))
        if action == "list_compile_tasks":
            res = await app.list_tasks(tenant_id, kb_id, kind="compile")
            return {"tasks": [_normalize_task_status(t) for t in (res or {}).get("tasks", [])]}
        if action == "cancel_task":
            return await app.cancel_task(tenant_id, data["task_id"])
        if action == "compilation_state":
            return await app.compilation_state(tenant_id, kb_id)
        if action in ("pause_compilation", "resume_compilation"):
            return await app.set_compilation_paused(tenant_id, kb_id, action == "pause_compilation")
        if action == "apply_schema":
            return await self._apply_schema(tenant_id, kb_id, data)
        if action == "delete_kb":
            return await app.delete_kb(tenant_id, kb_id)
        return {"error": f"未实现 action {action}"}

    async def _query_sync(self, app, tenant_id: str, kb_id: str,
                          question: str, want_trace: bool,
                          *, thinking_mode: str | None = None, progress_session: str | None = None) -> dict:
        """[jonex] 引擎 query 是异步任务(app.query 返回 task_id)，这里提交后同步等待完成。

        投影为平台契约，服务三条消费线（平台 /search/jonex_wiki）：
          · answer    —— 始终透出；
          · citations —— 引擎已核验的引用（六字段 + page_path），是平台 references 的
                        唯一来源。**不受 want_trace 影响**：引用是答案的证据，不是调试信息；
          · trace / spans / counts / latency_ms —— 仅 want_trace 时透出。trace 是引擎侧
                        已做白名单投影的检索/阅读轨迹（检索词、候选页、实读页），
                        spans 是 CR-TRACE-002 公开时间线（工具/模型/证据/合成 + 耗时）。
        """
        task = (await app.query(tenant_id, kb_id, question) if thinking_mode is None else
                await app.query(tenant_id, kb_id, question, thinking_mode=thinking_mode))
        task_id = str(task.get("task_id") or "") if isinstance(task, dict) else ""
        if not task_id:
            raise RuntimeError("jonex_wiki query: missing task_id")
        try:
            redis = key = None
            last_progress = None
            if progress_session:
                from jonex_core.common.cache import get_redis_client
                from jonex_core.common.wiki_search_session import session_key
                redis = get_redis_client()
                key = session_key(tenant_id, progress_session)
                await redis.hset(key, f"query:{kb_id}", task_id)
            deadline = time.monotonic() + _QUERY_POLL_TIMEOUT_S
            while True:
                st = await app.get_task(tenant_id, task_id)
                if not isinstance(st, dict):
                    raise RuntimeError("jonex_wiki query: invalid task state")
                status = str(st.get("status") or "")
                if redis is not None:
                    session_status = await redis.hget(key, "status")
                    if session_status not in ("running", b"running"):
                        await app.cancel_task(tenant_id, task_id)
                        raise RuntimeError("Wiki search cancelled")
                    progress = st.get("progress") or {}
                    if progress.get("seq") != last_progress:
                        import json
                        await redis.hset(key, f"progress:{kb_id}", json.dumps(progress, ensure_ascii=False))
                        last_progress = progress.get("seq")
                if status == "done":
                    result = st.get("result") or {}
                    out: dict = {
                        "answer": str(result.get("answer") or ""),
                        "citations": [dict(c, query_id=task_id, bundle_sha=result.get("bundle_sha"))
                                      for c in _normalize_citations(result.get("citations"))],
                    }
                    if want_trace:
                        out["trace"] = list(result.get("trace") or [])
                        out["spans"] = list(result.get("spans") or [])
                        out["counts"] = {
                            "search_calls": _as_int(result.get("search_calls")),
                            "read_calls": _as_int(result.get("read_calls")),
                            "tool_calls": _as_int(result.get("tool_calls")),
                            "model_calls": _as_int(result.get("model_calls")),
                        }
                        out["latency_ms"] = _as_int(result.get("latency_ms"))
                    return out
                if status in ("failed", "cancelled"):
                    # 任务失败是可诊断错误，抛异常走 execute→error response→client→
                    # search_jonex_wiki 的 except 分支，避免被静默吞成「无答案」。
                    raise RuntimeError(f"jonex_wiki query {status}: {st.get('error') or 'no detail'}")
                if time.monotonic() >= deadline:
                    raise RuntimeError("jonex_wiki query timeout")
                await asyncio.sleep(_QUERY_POLL_INTERVAL_S)
        except BaseException:
            # A stopped/disconnected streaming search must not leave model work running.
            if progress_session:
                from contextlib import suppress
                with suppress(Exception):
                    await app.cancel_task(tenant_id, task_id)
            raise

    async def _list_wiki_contents(self, app, tenant_id: str, kb_id: str,
                                  document_id: str) -> dict:
        """[jonex] 把引擎 list_pages 投影为 OpenKB 词汇的 Wiki 页面树。

        jonex_wiki 页面类型 {source, table, concept} → section {summaries,
        entities, concepts}。document_id 非空则按文档过滤：source 页按
        slug（source_id == document_id 的 sha256 前缀）匹配，table/concept
        页按 claims.jsonl 的 page→document 归属匹配（frontmatter source_ids
        存的是 passage_id hash，不是 document_id）。

        **字段面与 openkb 侧 ``_group_pages`` 对齐**：每个条目只出
        ``{stem, title, type, description}``。本接口的三个消费方都只要这些——
        前端 WikiPageBrowser 用 stem/title/type/description；编译资产 tab 只
        数条数（已改走 ``view=counts``）；检索侧概念页溯源只要 stem。
        此前每个条目还带了 canonical path / section / category / aliases /
        tags / why_visit / fields / scope / reference_count，并额外复制一份
        扁平 ``pages`` 数组（与三个 section 完全同源，序列化两遍）——实测
        6 文档的库响应体 1.6MB，其中一半是那份重复、另一半大部分是这些无人
        消费的字段。这些字段真正被消费的地方是**阅读目录接口**
        （``/wiki/outline`` 的 ``pages[]``）与 ``read_page``，不在本接口。
        """
        pages_res = await app.list_pages(tenant_id, kb_id)
        pages = (pages_res or {}).get("pages", []) or []
        doc_slug = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:16] if document_id else None
        # [jonex] claim→document 归属映射只在**文档级过滤**时有用；KB 级请求
        # （document_id 为空，正是检索侧溯源与资产 tab 的调用形态）拿它没有
        # 任何用处——此前无条件拉取，等于每次白扫一遍 claims.jsonl
        # （实测该 KB 5.9MB / 3727 条 / 约 130ms）。
        doc_pages: dict = {}
        if document_id:
            doc_pages = (await app.list_page_documents(tenant_id, kb_id)).get("documents", {})

        grouped = {"summaries": [], "concepts": [], "entities": []}
        for p in pages:
            path = str(p.get("path") or "")
            ptype = str(p.get("type") or "concept")
            if document_id and not self._page_belongs_to_doc(
                    doc_pages, path, ptype, document_id, doc_slug):
                continue
            section = _SECTION_BY_TYPE.get(ptype, "concepts")
            stem = _page_stem(path, ptype)
            # 标题清洗：表格页 title 可能是整段 HTML（真实产物里见过 `<table>`、
            # 整行 `<tr><td>修订前</td>…`），直接上屏会撑爆目录栏与条目页标题；
            # HTML 表格标题会退化成「首行列名 · 首行列名」。
            title = _clean_title(p.get("title"), stem, alt=p.get("title"))
            grouped[section].append({
                "stem": stem,          # 旧口径：剥 section 前缀与 .md
                "title": title,
                "type": ptype,
                "description": str(p.get("description") or ""),
            })
        return {
            "summaries": grouped["summaries"],
            "concepts": grouped["concepts"],
            "entities": grouped["entities"],
            "document_id": document_id,
        }

    async def _count_wiki_pages(self, app, tenant_id: str, kb_id: str) -> dict:
        """[jonex] 只返回三类页数（OpenKB 词汇），不构造页面对象。

        编译资产 tab 的统计卡与 tab 计数只消费长度，此前却走完整页面树：
        实测 6 文档的库传 1.6MB、50 文档的库传 4.75MB（约 3.5s），服务端还要
        把同一批对象序列化两遍。引擎侧 ``count_pages`` 用与 ``list_pages``
        完全一致的过滤/归并口径直接计数，响应体从 MB 级降到字节级。
        """
        counts = (await app.count_pages(tenant_id, kb_id) or {}).get("counts", {}) or {}
        out = {"summaries": 0, "concepts": 0, "entities": 0}
        for page_type, n in counts.items():
            out[_SECTION_BY_TYPE.get(str(page_type), "concepts")] += int(n or 0)
        return {**out, "document_id": ""}

    @staticmethod
    def _page_belongs_to_doc(doc_pages, path: str, ptype: str,
                             document_id: str, doc_slug: str) -> bool:
        """文档级过滤：source 页按 slug 匹配；table/concept 页查 claims 归属映射。"""
        if ptype == "source":
            return _page_stem(path, "source") == "source-" + doc_slug
        return document_id in (doc_pages.get(path) or set()) if doc_pages is not None else True

    async def _list_graph(self, app, tenant_id: str, kb_id: str,
                          document_id: str) -> dict:
        """[jonex] 把 Wiki 页面 + 页间 links 投影为 OpenKB 词汇的图谱。

        实体 = concept/table 页（排除 source 页与 raw 页——与 openkb
        「摘要页不入图」红线一致）；关系 = 页面 frontmatter links[].target_id
        （页间导航/支撑关系，与实体 id 同一 stem 口径）。document_id 非空则按
        claims.jsonl 的 page→document 归属过滤（frontmatter source_ids 是
        passage_id hash，不能当 document_id 用）。
        """
        pages_res = await app.list_pages(tenant_id, kb_id)
        pages = (pages_res or {}).get("pages", []) or []
        doc_pages = (await app.list_page_documents(tenant_id, kb_id)).get("documents", {})

        entities: list[dict] = []
        relationships: list[dict] = []
        seen_rel: set[tuple[str, str]] = set()
        for p in pages:
            path = str(p.get("path") or "")
            ptype = str(p.get("type") or "concept")
            if ptype == "source":
                continue  # 源页(→summaries)不入图
            if document_id and document_id not in (doc_pages.get(path) or ()):
                continue
            page = p.get("page") or {}
            stem = _page_stem(path, ptype)
            entities.append({
                "id": stem,
                "name": str(p.get("title") or stem),
                "type": ptype,
                "description": str(p.get("description") or ""),
                "section": _SECTION_BY_TYPE.get(ptype, "concepts"),
                "sources": [str(s) for s in (page.get("source_ids") or [])],
            })
            for link in (page.get("links") or []):
                if not isinstance(link, dict):
                    continue
                target = _target_stem(str(link.get("target_id") or ""))
                if not target or target == stem:
                    continue
                key = (stem, target)
                if key in seen_rel:
                    continue
                seen_rel.add(key)
                relationships.append({"source": stem, "target": target})
        return {
            "entities": entities,
            "relationships": relationships,
            "entities_count": len(entities),
            "relationships_count": len(relationships),
        }

    async def _read_page(self, app, tenant_id: str, kb_id: str, path: str) -> dict:
        """读取 Wiki 页面并投影为阅读侧形状（正文 + 元数据 + 页面级 References）。

        路径口径两套并存：
        - **canonical 引擎路径**（`canteen/lunch-dishes.md`，页面 markdown 内
          wikilink 的形态）：Wiki 阅读界面用这套，页面身份唯一、不依赖 section 映射；
        - **旧前端口径** `{section}/{stem}`（summaries|concepts|entities）：历史
          WikiPageBrowser 仍按此调用，继续支持。

        返回体里两条线都有：`path`/`engine_path`（canonical）与
        `section`/`name`/`stem`（旧口径），老组件读旧字段，新阅读页读 canonical。

        `references` 是**页面级溯源**：该页每个 claim 的 evidence/source_spans
        指向的源文档（去重、带摘录与字符区间），对应维基百科条目底部的
        References —— 让读者能从结论回到原始文档。
        """
        from jonex_wiki.kernel.errors import ValidationError

        raw = (path or "").strip()
        section, sep, stem = raw.partition("/")
        if raw.endswith(".md"):
            engine_path = raw
        elif section in _SECTION_PREFIX and sep and stem:
            engine_path = _section_to_engine_path(section, stem)
        else:
            raise ValidationError(f"无效页面路径: {path}")

        res = await app.read_page(tenant_id, kb_id, engine_path)
        index = res.get("index") or {}
        page = res.get("page") or {}
        ptype = str(res.get("type") or _SECTION_BY_TYPE.get(section, "concept"))
        stem_out = _page_stem(engine_path, ptype)
        section_out = _SECTION_BY_TYPE.get(ptype)
        if section_out is None:
            section_out = section if section in _SECTION_PREFIX else "concepts"
        # 标题清洗（表格页 title 可能是整段 HTML）——与 list_wiki_contents 同口径
        title = _clean_title(index.get("title"), stem_out, alt=index.get("title"))
        # 页面级 References：引擎给原始摘录，这里做展示清洗（剥 HTML/截断）
        references = [
            {
                "document_id": ref.get("document_id"),
                "excerpts": [_clean_excerpt(e) for e in (ref.get("excerpts") or [])],
                "claim_count": int(ref.get("claim_count") or 0),
                "chunk_id": ref.get("chunk_id") or "",
                "start_char": ref.get("start_char"),
                "end_char": ref.get("end_char"),
            }
            for ref in (res.get("references") or [])
        ]
        return {
            # ── canonical 身份（Wiki 阅读界面）──
            "path": engine_path,
            "engine_path": engine_path,
            "category": engine_path.split("/", 1)[0] if "/" in engine_path else "",
            # ── 旧口径（WikiPageBrowser 兼容）──
            "section": section_out,
            "name": stem_out,
            "stem": stem_out,
            # ── 阅读元数据 ──
            "title": title,
            "type": ptype,
            "description": str(index.get("description") or ""),
            "aliases": [str(a) for a in (index.get("aliases") or [])][:10],
            "tags": [str(t) for t in (index.get("tags") or [])][:10],
            "why_visit": str(index.get("why_visit") or ""),
            "limitations": [str(x) for x in (index.get("limitations") or [])][:10],
            "interpretation_status": index.get("interpretation_status"),
            "source_ids": page.get("source_ids") or [],
            # 列式视图（时间轴矩阵）：表格页的列名 + 所在工作表范围
            "fields": [str(f) for f in (index.get("fields") or [])][:40],
            "scope": index.get("scope") if isinstance(index.get("scope"), dict) else {},
            # 兼容旧字段名：历史组件用它显示「来源数」（值本身是 passage 指纹）
            "sources": page.get("source_ids") or [],
            # 页间链接（canonical 路径），前端据此渲染「相关页面」并做链接有效性校验
            "related": [str(r) for r in (page.get("related_pages") or [])][:100],
            "links": [
                str(link.get("target_id") or "")
                for link in (page.get("links") or [])
                if isinstance(link, dict) and link.get("target_id")
            ][:100],
            # 链接明细：kind（navigation/evidence/…）+ why_visit（引擎给的「为什么点它」），
            # 图谱边标签与「相关页面」的悬停说明用它；links 保持字符串数组不变。
            "link_meta": [
                {
                    "path": str(link.get("target_id") or ""),
                    "kind": str(link.get("kind") or ""),
                    "why_visit": _clean_excerpt(link.get("why_visit") or "")[:200],
                }
                for link in (page.get("links") or [])
                if isinstance(link, dict) and link.get("target_id")
            ][:100],
            # ── 页面级 References（源文档溯源）──
            "references": references,
            "content": res.get("body") or "",
        }

    async def _get_backlinks(self, app, tenant_id: str, kb_id: str, path: str,
                             document_id: str = "") -> dict:
        """「链入本页」：谁链接到了这一页（维基百科 What links here）。

        数据来自引擎 ``get_page_backlinks``（编译期 links 反向 + 正文提及，识别口径
        与前端正文蓝链完全一致），这里只补展示标题与 document 过滤。
        """
        raw = (path or "").strip()
        if not raw:
            from jonex_wiki.kernel.errors import ValidationError
            raise ValidationError("无效页面路径: 空")
        engine_path = raw if raw.endswith(".md") else (
            _section_to_engine_path(raw.partition("/")[0], raw.partition("/")[2])
            if raw.partition("/")[0] in _SECTION_PREFIX and raw.partition("/")[1] else raw)
        backlinks = await app.get_page_backlinks(tenant_id, kb_id, engine_path)
        docs_index = (await app.list_page_documents(tenant_id, kb_id)).get("documents", {})
        page_docs = docs_index.get(engine_path) or ()
        # 反向链接 source 的展示标题来自引擎 list_pages（PageIndexEntry.title）
        pages = (await app.list_pages(tenant_id, kb_id)).get("pages", []) or []
        titles: dict[str, str] = {}
        for p in pages:
            ppath = str(p.get("path") or "")
            ptype = str(p.get("type") or "concept")
            titles[ppath] = _clean_title(p.get("title"), _page_stem(ppath, ptype),
                                         alt=p.get("title"))

        items: list[dict] = []
        mention_count = 0
        for entry in backlinks.get("items", []):
            source = str(entry.get("path") or "")
            if document_id and document_id not in (docs_index.get(source) or ()):
                continue
            kind = str(entry.get("kind") or "link")
            items.append({
                "path": source,
                "title": titles.get(source) or _page_stem(source, "concept"),
                "kind": kind,
            })
            if "mention" in kind:
                mention_count += 1
        items.sort(key=lambda i: (0 if "link" in i["kind"] else 1, i["title"]))
        return {
            "knowledge_base_id": kb_id,
            "path": engine_path,
            "total": len(items),
            "mention_count": mention_count,
            "document_count": len(page_docs),
            "items": items[:_BACKLINK_LIMIT],
        }

    async def _get_claims(self, app, tenant_id: str, kb_id: str, path: str,
                          document_id: str = "", limit: int = _MAX_CLAIMS_PER_QUERY) -> dict:
        """claim 级溯源明细（列式视图「溯源审计」）。

        按页面或按来源文档过滤，二者至少给一个；返回体带页面标题与文档名，
        让审计表可以直接渲染（前端不必再补一次查询）。
        """
        raw = (path or "").strip()
        engine_path = ""
        if raw:
            engine_path = raw if raw.endswith(".md") else (
                _section_to_engine_path(raw.partition("/")[0], raw.partition("/")[2])
                if raw.partition("/")[0] in _SECTION_PREFIX and raw.partition("/")[1] else raw)
        result = await app.get_page_claims(tenant_id, kb_id, path=engine_path,
                                           document_id=document_id, limit=limit)
        pages = (await app.list_pages(tenant_id, kb_id)).get("pages", []) or []
        raw_titles = {str(p.get("path") or ""): p.get("title") for p in pages}
        for item in result["items"]:
            page = str(item.get("page_path") or "")
            # 展示清洗：引擎给原始 text/excerpt，这里剥 HTML + 截断（与旧口径一致）
            item["text"] = _clean_excerpt(item.get("text") or "")[:_MAX_CLAIM_TEXT_LEN]
            item["excerpt"] = _clean_excerpt(item.get("excerpt") or "")
            item["page_title"] = _clean_title(raw_titles.get(page),
                                              _page_stem(page, "concept"),
                                              alt=raw_titles.get(page))
        result.update({"knowledge_base_id": kb_id, "path": engine_path,
                       "document_id": document_id})
        return result

    async def _apply_schema(self, tenant_id, kb_id, data) -> dict:
        """schema 投影:AGENTS.md → 引擎 compile_main prompt。

        expected_version 不能直接透传平台 schema_version:引擎 PromptRepo.upsert 的 CAS
        语义是「新建传 0、更新传当前 version」，而平台 schema_version(1,2,3…)与引擎
        prompt version(首写为 1)是两套独立版本空间。这里先读引擎当前 prompt——
        source=default(version=None)时按新建传 0,否则传实际 version。
        """
        schema_version = int(data.get("schema_version") or 0)
        current = await self._app.get_prompt(tenant_id, kb_id, _SCHEMA_PROMPT_KEY)
        expected_version = 0 if current["version"] is None else current["version"]
        prompt = await self._app.put_prompt(tenant_id, kb_id, _SCHEMA_PROMPT_KEY,
                                            data["agents_md"], expected_version)
        return {"applied": True, "schema_version": schema_version,
                "prompt_key": _SCHEMA_PROMPT_KEY, "prompt": prompt}
