# [jonex] 悦溪新增文件 — OpenKB Atomic Capability 实现
import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from jonex_core.capability.base import BaseCapability
from jonex_core.capability.models import (
    CapabilityRequest,
    CapabilityResponse,
    CapabilityMetadata,
    CapabilityType,
)

OPENKB_CAPABILITY_ID = "atomic.openkb.v1"


class _OpenKBError(Exception):
    """带稳定 error_code 的 adapter 异常（方案 §1.2 错误码契约）。"""

    def __init__(self, message: str, *, error_code: str = "INTERNAL_ERROR", http_code: int = 500):
        super().__init__(message)
        self.error_code = error_code
        self.http_code = http_code


def _h1_of(body: str) -> str:
    """[jonex] 提取正文首个 `# ` 标题（H1）；无则返回空串。

    供 _handle_read_page / _iter_wiki_pages 提取页面显示名
    （OpenKB 页面文件名是英文 slug，中文标题在正文 H1）。
    """
    import re
    m = re.search(r"^#\s+(.+?)\s*$", body, re.M)
    return m.group(1).strip() if m else ""


def _now_iso() -> str:
    """[jonex] 当前 UTC 时间 ISO 串（任务文件时间戳，与 PG requested_at 同口径）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# [jonex] 模块级：per-KB asyncio.Lock（key = kb_dir 绝对路径）——KB 级 mutation 锁。
# 覆盖 compile / remove_document / delete_kb / recompile 四条改 KB 路径。
# kb_ingest_lock（openkb.locks）的重入判定是线程级，asyncio 同线程并发下不互斥，
# 只能作跨进程二次防线；真正的协程级串行化靠这里的 asyncio.Lock。
_kb_mutation_locks: dict[str, asyncio.Lock] = {}
_kb_mutation_locks_guard = asyncio.Lock()   # 字典创建互斥

# 后台编译任务集合（防 GC 回收导致任务丢失）
_pending_compile_tasks: set[asyncio.Task] = set()


class OpenkbCapability(BaseCapability):
    """OpenKB 知识编译能力。

    由 deploy/start_capability.py 加载（CAPABILITY_KIND=atomic, CAPABILITY_NAME=openkb）。
    class_name 自动推导为 "OpenkbCapability"。
    """

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="openkb",
            capability_name="OpenKB Knowledge Compiler",
            capability_type=CapabilityType.ATOMIC,
            version="v1",
            description="OpenKB Wiki 编译引擎 — 消费 Jonex parsed artifact -> LLM 编译为结构化 Wiki",
        )

    async def validate_input(self, request: CapabilityRequest) -> bool:
        action = request.payload.get("action", "")
        if action not in {
            "init_kb", "compile_parsed_document", "query",
            "status", "list_contents", "recompile",
            "remove_document", "delete_kb", "list_graph",
            "read_page", "list_wiki_contents",
            "get_compile_status", "list_compile_tasks",
        }:
            return False

        if action == "compile_parsed_document":
            data = request.payload.get("data") or {}
            return bool(data.get("document_id") and data.get("parsed_markdown_path"))

        if action == "remove_document":
            data = request.payload.get("data") or {}
            return bool(data.get("document_id"))

        if action == "read_page":
            data = request.payload.get("data") or {}
            return bool(str(data.get("path") or "").strip())

        if action == "get_compile_status":
            data = request.payload.get("data") or {}
            return bool(str(data.get("task_id") or "").strip())

        # list_wiki_contents / list_graph 的 document_id、list_compile_tasks 的
        # document_id 不进必填（服务层可选，缺省 = KB 级，兼容既有消费者）

        return True

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        action = request.payload["action"]
        kb_id = request.payload.get("knowledge_base_id")
        data = request.payload.get("data", {})
        kb_name = data.get("kb", f"kb_{kb_id}" if kb_id else "default")

        workspace = f"{request.tenant_id}__{kb_id}" if kb_id else request.tenant_id
        # [jonex] 口径与 initialize() 统一（get + 默认值，缺失不 KeyError）
        kb_dir = Path(os.environ.get("OPENKB_KB_ROOT", "/app/data/kb_storage")) / workspace / kb_name

        # [jonex] 注入 metering context（per-request，不污染全局）
        from openkb.jonex_metering import set_jonex_context

        set_jonex_context(
            tenant_id=request.tenant_id,
            kb_id=kb_id,
            scene=f"openkb_{action}",
            trace_id=request.request_id,
        )

        try:
            handlers = {
                "init_kb": self._handle_init,
                "compile_parsed_document": self._handle_compile_parsed_document,
                "query": self._handle_query,
                "status": self._handle_status,
                "list_contents": self._handle_list,
                "recompile": self._handle_recompile,
                "remove_document": self._handle_remove_document,
                "delete_kb": self._handle_delete_kb,
                "list_graph": self._handle_list_graph,
                "read_page": self._handle_read_page,
                "list_wiki_contents": self._handle_list_contents,
                "get_compile_status": self._handle_get_compile_status,
                "list_compile_tasks": self._handle_list_compile_tasks,
            }
            handler = handlers[action]
            result_data = await handler(kb_dir, kb_name, data)
            return CapabilityResponse.ok(
                request_id=request.request_id,
                data=result_data,
            )
        except _OpenKBError as exc:
            # [jonex] 稳定 error_code 透传给 knowledge_base
            return CapabilityResponse.error(
                request_id=request.request_id,
                code=exc.http_code,
                message=str(exc),
                details={"error_code": exc.error_code},
            )
        except FileExistsError as exc:
            return CapabilityResponse.error(
                request_id=request.request_id,
                code=409,
                message=str(exc),
                details={"error_code": "KB_ALREADY_EXISTS"},
            )
        except Exception as exc:
            return CapabilityResponse.error(
                request_id=request.request_id,
                code=500,
                message=str(exc),
                details={"error_code": "COMPILE_FAILED" if action == "compile_parsed_document" else "INTERNAL_ERROR"},
            )

    # ── [jonex] 启动钩子：孤儿编译任务判死（崩溃恢复） ──

    async def initialize(self) -> None:
        """[jonex] 启动时扫描各 KB tasks/，把 pending/running 判死（容器重启恢复）。

        被 start_capability.py:105 自动调用（BaseCapability.initialize 钩子）。
        """
        from openkb.locks import atomic_write_json

        root = Path(os.environ.get("OPENKB_KB_ROOT", "/app/data/kb_storage"))
        for workspace in root.glob("*/*"):
            tasks_dir = workspace / ".openkb" / "tasks"
            if not tasks_dir.is_dir():
                continue
            for tf in tasks_dir.glob("*.json"):
                try:
                    task = json.loads(tf.read_text(encoding="utf-8"))
                    if task.get("status") in ("pending", "running"):
                        task["status"] = "failed"
                        task["error"] = "task lost after restart"
                        task["updated_at"] = _now_iso()
                        atomic_write_json(tf, task)
                except Exception:
                    continue

    async def _mutation_lock_for(self, kb_dir) -> asyncio.Lock:
        """[jonex] per-KB mutation 锁（协程级串行化，覆盖四条改 KB 路径）。"""
        key = str(Path(kb_dir).resolve())
        async with _kb_mutation_locks_guard:
            if key not in _kb_mutation_locks:
                _kb_mutation_locks[key] = asyncio.Lock()
            return _kb_mutation_locks[key]

    # ── handler ──

    async def _handle_init(self, kb_dir, kb_name, data):
        import os
        from openkb.cli import initialize_kb
        from openkb.config import load_config, save_config

        # [jonex] 幂等 init：已初始化则跳过 initialize_kb，避免 FileExistsError
        created = not (kb_dir / ".openkb").is_dir()
        if created:
            initialize_kb(kb_dir, model=data.get("model") or os.getenv("OPENKB_LLM_MODEL"))

        config_path = kb_dir / ".openkb" / "config.yaml"
        if lang := os.getenv("OPENKB_LANGUAGE"):
            config = load_config(config_path)
            config["language"] = lang
            save_config(config_path, config)

        return {"kb": kb_name, "created": created, "message": "KB initialized"}

    async def _handle_compile_parsed_document(self, kb_dir, kb_name, data):
        """[jonex] 任务化：校验产物 → 登记任务 → 后台编译，立即返回 {task_id}。

        ⚠️ 提交阶段只校验、不落盘——写 wiki/sources/{doc_id}.md 与 assets 由
        _run_compile_task 在 mutation 锁内统一做（_compile_parsed_markdown），
        避免在锁外覆盖其他并发任务的 source/assets。
        """
        import asyncio
        import json
        from uuid import uuid4

        from openkb.cli import initialize_kb
        from openkb.config import load_config, save_config

        document_id = data["document_id"]
        parsed_markdown_path = Path(data["parsed_markdown_path"])

        # ① 只校验产物（不落盘）
        self._validate_input_path(parsed_markdown_path, must_be_file=True)
        if assets_dir := data.get("assets_dir"):
            try:
                self._validate_input_path(Path(assets_dir), must_be_dir=True)
            except _OpenKBError:
                # assets 缺失不阻断编译（编译侧降级为无图片）
                pass

        if not (kb_dir / ".openkb").is_dir():
            initialize_kb(kb_dir, model=os.getenv("OPENKB_LLM_MODEL"))

        # [jonex] 确保 language 与 OPENKB_LANGUAGE 一致（compile 路径也可能首次 init）
        config_path = kb_dir / ".openkb" / "config.yaml"
        if lang := os.getenv("OPENKB_LANGUAGE"):
            config = load_config(config_path)
            if config.get("language") != lang:
                config["language"] = lang
                save_config(config_path, config)

        # ② 登记任务（KB 级 tasks/ 目录，原子写）
        task_id = f"t_{uuid4().hex}"
        self._write_task(kb_dir, task_id, {
            "task_id": task_id, "document_id": document_id,
            "status": "pending", "error": None, "warnings": [],
            "created_at": _now_iso(), "started_at": None, "updated_at": _now_iso(),
        })

        # ③ 后台执行（事件循环内 create_task；防 GC 用模块级集合持有）
        task = asyncio.get_running_loop().create_task(
            self._run_compile_task(kb_dir, kb_name, task_id, data)
        )
        _pending_compile_tasks.add(task)
        task.add_done_callback(_pending_compile_tasks.discard)

        return {"task_id": task_id, "status": "pending"}

    # ── [jonex] 编译任务（任务化：文件持久化 + per-KB mutation 锁串行） ──

    async def _run_compile_task(self, kb_dir, kb_name, task_id, data):
        """后台执行编译：mutation 锁内跑 _compile_parsed_markdown，回写任务状态。"""
        async with await self._mutation_lock_for(kb_dir):   # ← 协程级串行化
            try:
                self._update_task(kb_dir, task_id, status="running",
                                  started_at=_now_iso())
                bundle = self._build_metering_bundle(kb_dir)
                result = await self._compile_parsed_markdown(
                    kb_dir=kb_dir,
                    document_id=data["document_id"],
                    parsed_markdown_path=Path(data["parsed_markdown_path"]),
                    assets_dir=Path(data["assets_dir"]) if data.get("assets_dir") else None,
                    metadata=data.get("metadata") or {},
                    bundle=bundle,
                )
                self._update_task(kb_dir, task_id, status="completed",
                                  warnings=result.get("warnings", []))
            except Exception as exc:  # noqa: BLE001
                self._update_task(kb_dir, task_id, status="failed",
                                  error=str(exc)[:1000])

    async def _handle_get_compile_status(self, kb_dir, kb_name, data):
        """[jonex] 查询单个编译任务状态；未知 task → 404。"""
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise _OpenKBError(error_code="MISSING_PARAM", message="get_compile_status: missing task_id",
                               http_code=400)
        path = self._task_path(kb_dir, task_id)
        if not path.is_file():
            raise _OpenKBError(error_code="PAGE_NOT_FOUND", message=f"get_compile_status: task not found: {task_id}",
                               http_code=404)
        task = json.loads(path.read_text(encoding="utf-8"))
        return {
            "task_id": task.get("task_id"),
            "document_id": task.get("document_id"),
            "status": task.get("status"),
            "error": task.get("error"),
            "warnings": task.get("warnings") or [],
            "updated_at": task.get("updated_at"),
        }

    async def _handle_list_compile_tasks(self, kb_dir, kb_name, data):
        """[jonex] 列出该 KB 的编译任务（对账巡检批量拉取；document_id 可选过滤）。"""
        import json as _json
        tasks_dir = kb_dir / ".openkb" / "tasks"
        document_id = str(data.get("document_id") or "").strip()
        out = []
        if tasks_dir.is_dir():
            for tf in sorted(tasks_dir.glob("*.json")):
                try:
                    task = _json.loads(tf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if document_id and task.get("document_id") != document_id:
                    continue
                out.append({
                    "task_id": task.get("task_id"),
                    "document_id": task.get("document_id"),
                    "status": task.get("status"),
                    "error": task.get("error"),
                    "warnings": task.get("warnings") or [],
                    "updated_at": task.get("updated_at"),
                })
        return {"tasks": out}

    @staticmethod
    def _task_path(kb_dir, task_id) -> Path:
        return kb_dir / ".openkb" / "tasks" / f"{task_id}.json"

    def _write_task(self, kb_dir, task_id: str, data: dict) -> None:
        """登记任务（原子写）。"""
        from openkb.locks import atomic_write_json
        path = self._task_path(kb_dir, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)

    def _update_task(self, kb_dir, task_id: str, **fields) -> None:
        """读改写任务文件。

        每个 task 一个独立文件、无跨任务共享状态，故无并发覆盖问题
        （mutation 锁只保护 KB 文件树写入，不保护任务文件本身）。
        """
        from openkb.locks import atomic_write_json
        path = self._task_path(kb_dir, task_id)
        if not path.is_file():
            return
        task = json.loads(path.read_text(encoding="utf-8"))
        task.update(fields)
        task["updated_at"] = _now_iso()
        atomic_write_json(path, task)

    async def _handle_query(self, kb_dir, kb_name, data):
        from openkb.agent.query import build_run_config_from_bundle, run_query
        from openkb.config import resolve_effective_config

        config = resolve_effective_config(kb_dir)[0]
        bundle = self._build_metering_bundle(kb_dir)
        model = self._litellm_model(config["model"])
        run_config = build_run_config_from_bundle(model, bundle)
        # [jonex] 调用方（knowledge_base）按 with_reasoning 决定是否要轨迹。
        want_trace = bool(data.get("return_trace"))
        result = await run_query(
            data["question"], kb_dir, model, stream=False,
            bundle=bundle, run_config=run_config,
            return_trace=want_trace,
        )
        if want_trace:
            answer, trace = result
            return {"answer": answer, "trace": trace}
        return {"answer": result}

    async def _handle_status(self, kb_dir, kb_name, data):
        from openkb.cli import get_kb_status
        return get_kb_status(kb_dir)

    async def _handle_list(self, kb_dir, kb_name, data):
        from openkb.cli import get_kb_list
        return get_kb_list(kb_dir)

    # ── [jonex] Wiki 阅读模式（编译结果页）：read_page / list_wiki_contents ──

    async def _handle_read_page(self, kb_dir, kb_name, data):
        """[jonex] 读取 Wiki 页面 markdown 内容（供编译结果 Wiki 阅读模式）。

        路径校验：resolve 后必须位于 wiki/ 内（防穿越），且文件存在。
        path 形如 `entities/xxx` / `concepts/xxx` / `summaries/xxx`（.md 可省略）。
        ⚠️ 故意不做文档归属校验：同 KB 内任意页面可读（wikilink 天然跨文档，
        从文档 A 的正文可跳到文档 B 贡献的实体页）。这是设计，不是漏洞。
        """
        import re
        from openkb import frontmatter as fm

        path = str(data.get("path") or "").strip()
        if not path:
            raise _OpenKBError(error_code="MISSING_PARAM", message="read_page: missing path",
                               http_code=400)
        # 归一：剥 .md 后缀统一处理，只允许 wiki 页面目录前缀，禁止 .. / 绝对路径
        # 注：sources/ 不放行（整篇解析产物可达数 MB，不穿 JSON 链路）
        # ⚠️ 按段校验：每段只允许 [\w\-]+——字符类含 `.`/`/` 会让
        # `entities/../..` 通过正则并在 resolve 后仍落在 wiki 内（→ 404 而非
        # 400）；按段限制后 `..` 直接 400。
        rel = path[:-3] if path.endswith(".md") else path
        if not re.fullmatch(r"(summaries|concepts|entities)/[\w\-]+(/[\w\-]+)*", rel):
            raise _OpenKBError(error_code="PATH_OUT_OF_SCOPE", message=f"read_page: invalid page path: {path}",
                               http_code=400)
        wiki_dir = (kb_dir / "wiki").resolve()
        # ⚠️ 统一补 .md 再 resolve：实际文件是 wiki/{rel}.md（前端传的 path 不带后缀）
        target = (wiki_dir / f"{rel}.md").resolve()
        if not target.is_relative_to(wiki_dir):
            raise _OpenKBError(error_code="PATH_OUT_OF_SCOPE", message=f"read_page: path escapes wiki: {path}",
                               http_code=400)
        if not target.is_file():
            raise _OpenKBError(error_code="PAGE_NOT_FOUND", message=f"read_page: page not found: {path}",
                               http_code=404)

        content = target.read_text(encoding="utf-8")
        meta = fm.parse(content) or {}
        # 剥离 frontmatter，正文供前端渲染
        parts = fm.split(content)
        body = parts[1] if parts else content
        return {
            "path": rel,
            "section": rel.split("/", 1)[0],
            "name": target.stem,
            "title": str(meta.get("name") or "") or _h1_of(body) or target.stem,
            "type": str(meta.get("type") or ""),
            "description": str(meta.get("description") or ""),
            "sources": meta.get("sources") or [],
            "content": body,
        }

    @staticmethod
    def _read_page_head(path, limit: int = 64 * 1024) -> str:
        """[jonex] 只读文件头部 limit 字节（frontmatter + 正文开头 H1 区域）。

        frontmatter 解析（openkb.frontmatter.split）只依赖文件开头到第二个
        行首 ``---`` 的区域；编译器写出的 frontmatter 是单行 JSON 风格 kv，
        64KB 覆盖绝大多数页面。⚠️ 头部若无完整 frontmatter（超长 sources
        数组超出 limit），调用方必须回退整读，见 _iter_wiki_pages。
        """
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read(limit)

    def _iter_wiki_pages(self, kb_dir, need_body: bool = True) -> list[dict]:
        """[jonex] 遍历 wiki/ 的 summaries/concepts/entities，返回结构化条目。

        条目含 {section, id, name, stem, type, description, sources, body}——
        id=stem（slug，关系/图匹配用），name=title（显示名：frontmatter name →
        正文 H1 → stem 兜底）。
        need_body=False（页面树场景）走 **_read_page_head 只读头部**：
        frontmatter 解析 + H1 提取所需区域全部在头部，不整读正文；仅当
        头部 limit 内无完整 frontmatter（超长 sources 数组）时回退整读。
        """
        from openkb import frontmatter as fm

        out: list[dict] = []
        for section in ("summaries", "concepts", "entities"):
            sect_dir = kb_dir / "wiki" / section
            if not sect_dir.is_dir():
                continue
            for f in sorted(sect_dir.glob("*.md")):
                try:
                    if need_body:
                        text = f.read_text(encoding="utf-8")
                    else:
                        text = self._read_page_head(f)
                        parts = fm.split(text)
                        if parts is not None:
                            meta_head = fm.parse(text) or {}
                            body_head = parts[1] if len(parts) > 1 else ""
                            # 回退整读的两条件（任一命中）：
                            # ① 头部无完整 frontmatter（超长 sources 数组超出 limit）
                            # ② frontmatter 无 name 且头部内无 H1（H1 可能在 64KB
                            #    之外——长 sources + 长前言）→ 否则 title 兜底成
                            #    stem，左树退回英文 slug/UUID，且不报错
                            if not meta_head.get("name") and not _h1_of(body_head):
                                text = f.read_text(encoding="utf-8")
                        else:
                            text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                meta = fm.parse(text) or {}
                parts = fm.split(text)
                body = parts[1] if parts else text
                stem = f.stem
                title = str(meta.get("name") or "").strip() or _h1_of(body) or stem
                # type 兜底与旧 _handle_list_graph 一致：概念页 → Concept，实体页 → Entity
                etype = str(meta.get("type") or ("concept" if section == "concepts" else "entity"))
                out.append({
                    "section": section,
                    "id": stem,
                    "stem": stem,
                    "name": title,
                    "title": title,
                    "type": etype,
                    "description": str(meta.get("description") or ""),
                    "sources": meta.get("sources") or [],
                    "body": body if need_body else "",
                })
        return out

    @staticmethod
    def _page_belongs_to(sources: list | str, document_id: str) -> bool:
        """[jonex] 页面是否属于某文档：sources 数组含 summaries/{doc_id}.md。

        传 frontmatter 的 sources 值（不是整条页面 dict）；仅概念/实体页用
        sources 判定（摘要页另走文件名 1:1）。
        """
        wanted = f"summaries/{document_id}.md"
        if isinstance(sources, list):
            return any(wanted == str(s) for s in sources)
        return str(sources or "") == wanted

    def _document_pages(self, pages: list[dict], document_id: str) -> list[dict]:
        """[jonex] 文档级页面集合（唯一过滤入口）。

        页面树（list_wiki_contents）/ 图谱（list_graph）/ 前端 wikilink 判定
        三处共用的同一归属口径：摘要 1:1（文件名 == document_id），
        entities/concepts 按 sources 归属（_page_belongs_to）。
        """
        kept = []
        for p in pages:
            if p["section"] == "summaries":
                if p["stem"] == document_id:
                    kept.append(p)
            elif self._page_belongs_to(p.get("sources"), document_id):
                kept.append(p)
        return kept

    @staticmethod
    def _group_pages(pages: list[dict], document_id: str) -> dict:
        """按 section 分组为 {summaries, concepts, entities} 结构化列表。

        树条目输出 **stem/title**（页面路径用 stem、显示用 title）——页面树
        消费的是 stem/title 语义，不是图节点的 id/name 语义；下游
        `_handle_list_graph` 的 id/name 两分只在图谱契约里出现。
        """
        grouped: dict[str, list[dict]] = {"summaries": [], "concepts": [], "entities": []}
        for p in pages:
            grouped[p["section"]].append({
                "stem": p["stem"],
                "title": p["title"],
                "type": p["type"],
                "description": p["description"],
            })
        return {
            "summaries": grouped["summaries"],
            "concepts": grouped["concepts"],
            "entities": grouped["entities"],
            "document_id": document_id,
        }

    async def _handle_list_contents(self, kb_dir, kb_name, data):
        """[jonex] Wiki 页面树（新 action list_wiki_contents 的 handler）。

        与原生 list_contents / _handle_list 无关（那是 OpenKB get_kb_list 透传）。
        document_id 可选：给则文档级过滤，缺省 KB 级（兼容既有消费者）。
        """
        document_id = str(data.get("document_id") or "").strip()
        pages = self._iter_wiki_pages(kb_dir, need_body=False)
        if not document_id:
            # KB 级：按 section 分组返回全量
            return self._group_pages(pages, document_id="")

        # 文档级：单一入口 _document_pages（与 list_graph / wikilink 判定同源）
        return self._group_pages(self._document_pages(pages, document_id),
                                 document_id=document_id)

    async def _handle_recompile(self, kb_dir, kb_name, data):
        # [jonex] mutation 锁：与后台编译任务互斥（iter_recompile 内部 kb_ingest_lock）
        async with await self._mutation_lock_for(kb_dir):
            from openkb.cli import iter_recompile

            bundle = self._build_metering_bundle(kb_dir)
            results = []
            async for event in iter_recompile(
                kb_dir, data.get("doc_name"),
                all_docs=data.get("all_docs", False), bundle=bundle,
            ):
                if event.get("event") == "final":
                    results.append(event)
            return {"results": results}

    async def _handle_remove_document(self, kb_dir, kb_name, data):
        # [jonex] mutation 锁：与后台编译任务互斥（删除时编译可能正写概念页）
        async with await self._mutation_lock_for(kb_dir):
            document_id = data["document_id"]
            result = await self._remove_compiled_document(kb_dir=kb_dir, document_id=document_id)
            return {
                "document_id": document_id,
                "status": result.get("status"),
                "removed": result.get("removed", False),
                "warnings": result.get("warnings", []),
                "result": result.get("detail", {}),
            }

    async def _handle_list_graph(self, kb_dir, kb_name, data):
        """[jonex] 列出 Wiki 的 entities/concepts 及其关系（文档级过滤可选）。

        基于 _iter_wiki_pages 单次遍历（need_body=True：关系从正文抽）。
        document_id 可选：给则走 _document_pages 文档级过滤（与页面树同一
        归属口径），缺省 KB 级全量。
        🔴 红线：必须排除 summaries——摘要页不入图！依赖方
        knowledge_info_service.get()（KB 详情页头部实体数/关系数）直接以
        entities_count/relationships_count 为权威计数；摘要页混入会导致
        entity_count 按文档数虚高、关系数跳变（01 步骤修过的回归）。
        """
        import re

        document_id = str(data.get("document_id") or "").strip()
        pages = self._iter_wiki_pages(kb_dir)          # need_body=True：关系从正文抽
        # 文档级归属走 _document_pages（与页面树同一口径）
        if document_id:
            pages = self._document_pages(pages, document_id)
        # 🔴🔴 红线（不可删改）：必须排除 summaries —— 摘要页不入图！
        # 依赖方：knowledge_info_service.get()（KB 详情页头部实体数/关系数）
        # 直接以本接口 entities_count/relationships_count 为权威计数。
        # 若摘要页混入 entities，entity_count 会按文档数虚高（KB 级时每篇
        # 文档 +1），且摘要页正文挂满全文档 wikilink，关系数跳变。
        # 这是 01 步骤「openkb 计数改读 Wiki」修复过的显示问题，回归即返工。
        nodes = [p for p in pages if p["section"] in ("entities", "concepts")]

        entities: list[dict] = []
        relationships: list[dict] = []
        seen_rel: set[tuple[str, str]] = set()
        for p in nodes:
            entities.append({
                "id": p["id"],                       # slug，关系/图匹配用
                "name": p["name"],                   # 显示名（H1 中文标题）
                "type": p["type"],
                "description": p["description"],
                "section": p["section"],
                "sources": p["sources"],
            })
            for raw in re.findall(r"\[\[([^\]]+)\]\]", p["body"]):
                target = raw.split("|", 1)[0].strip()      # 去别名 [[t|alias]]
                target = target.split("/")[-1].strip()      # section/name → name
                target = target.split("#", 1)[0].strip()    # 去锚点
                if not target or target == p["id"]:
                    continue
                key = (p["id"], target)
                if key in seen_rel:
                    continue
                seen_rel.add(key)
                relationships.append({"source": p["id"], "target": target})

        return {
            "entities": entities,
            "relationships": relationships,
            "entities_count": len(entities),
            "relationships_count": len(relationships),
        }

    async def _handle_delete_kb(self, kb_dir, kb_name, data):
        # [jonex] mutation 锁：与后台编译任务互斥（编译写文件时 rmtree 目录的极端场景）
        async with await self._mutation_lock_for(kb_dir):
            result = await self._delete_kb_dir(kb_dir=kb_dir)
            return {
                "kb": kb_name,
                "status": result.get("status"),
                "deleted": result.get("deleted", False),
            }

    # ── Adapter-local helpers（复用 OpenKB compiler/registry/lock）──

    @staticmethod
    def _litellm_model(model: str | None) -> str | None:
        """[jonex] 给 litellm 的模型名补 provider 前缀。

        OpenKB 经 litellm 调用 LLM；对 OpenAI 兼容端点（llm-gateway），litellm 需要
        `openai/<model>` 形式才能确定 provider，否则报 "LLM Provider NOT provided"。
        已带 provider 前缀（含 `/`）则原样返回。可用 OPENKB_LLM_PROVIDER 覆盖默认 openai。
        """
        if not model:
            return model
        if "/" in model:
            return model
        provider = os.getenv("OPENKB_LLM_PROVIDER", "openai").strip() or "openai"
        return f"{provider}/{model}"

    def _validate_input_path(self, path: Path, *, must_be_file: bool = False, must_be_dir: bool = False) -> Path:
        """路径安全校验（方案 §1.3）：解析后必须位于 OPENKB_INPUT_ROOT 内。

        [jonex] 跨容器挂载点无关：contract 传入的应是「相对 inputs 卷根」的路径
        （如 parsed/{document_id}/content.md）。相对路径按 OPENKB_INPUT_ROOT 解析；
        绝对路径仍按原样校验（须落在 root 内），从而兼容不同容器的挂载点。
        """
        input_root = Path(os.getenv("OPENKB_INPUT_ROOT", "/app/data/inputs")).resolve()
        p = Path(path)
        resolved = (p.resolve() if p.is_absolute() else (input_root / p).resolve())
        try:
            resolved.relative_to(input_root)
        except ValueError:
            raise _OpenKBError(
                f"路径越权：{path} 不在允许根目录 {input_root} 内",
                error_code="PATH_OUT_OF_SCOPE",
                http_code=400,
            )
        if must_be_file and not resolved.is_file():
            raise _OpenKBError(
                f"markdown 文件不存在：{path}", error_code="ARTIFACT_NOT_FOUND", http_code=404,
            )
        if must_be_dir and not resolved.is_dir():
            raise _OpenKBError(
                f"assets 目录不存在：{path}", error_code="ARTIFACT_NOT_FOUND", http_code=404,
            )
        return resolved

    async def _compile_parsed_markdown(self, kb_dir, document_id, parsed_markdown_path, assets_dir, metadata, bundle):
        """将 Jonex parsed markdown 写入 wiki/sources 并复用 compile_short_doc 编译。

        以 document_id 为稳定键；重复提交是 update/overwrite。全程在 KB ingest 锁内完成，
        避免 source/assets/registry 半更新。不经过 converter/indexer，不触发 PageIndex。
        """
        import shutil

        from openkb.locks import kb_ingest_lock, atomic_write_text
        from openkb.state import HashRegistry
        from openkb.converter import _registry_path
        from openkb.config import resolve_effective_config, resolve_concurrency
        from openkb.agent.compiler import compile_short_doc, DEFAULT_COMPILE_CONCURRENCY

        warnings: list[str] = []

        md_path = self._validate_input_path(parsed_markdown_path, must_be_file=True)
        if assets_dir is not None:
            try:
                assets_dir = self._validate_input_path(assets_dir, must_be_dir=True)
            except _OpenKBError as exc:
                warnings.append(str(exc))
                assets_dir = None

        config = resolve_effective_config(kb_dir)[0]
        model = self._litellm_model(config.get("model") or os.getenv("OPENKB_LLM_MODEL"))
        if not model:
            raise _OpenKBError("KB 未配置 LLM model", error_code="COMPILE_FAILED", http_code=500)

        markdown_text = md_path.read_text(encoding="utf-8")
        openkb_dir = kb_dir / ".openkb"
        source_path = kb_dir / "wiki" / "sources" / f"{document_id}.md"

        # KB 级 ingest 锁：同一 KB 内 compile/remove 串行化
        with kb_ingest_lock(openkb_dir):
            registry = HashRegistry(openkb_dir / "hashes.json")
            # 幂等覆盖：先清理旧 registry 记录与旧 assets
            registry.remove_by_doc_name(document_id)
            atomic_write_text(source_path, markdown_text)

            if assets_dir is not None:
                dest_assets = kb_dir / "wiki" / "sources" / "images" / document_id
                if dest_assets.exists():
                    shutil.rmtree(dest_assets, ignore_errors=True)
                shutil.copytree(assets_dir, dest_assets)
                # [jonex] content.md 内图片引用由 atomic-rag 侧按 images/{document_id}/<name>
                # 生成，正好相对 wiki/sources/{document_id}.md 指向此处，无需再重写。

            # 直接 await（不用 CLI 的 asyncio.run 包装，避免事件循环冲突）
            await compile_short_doc(
                document_id,
                source_path,
                kb_dir,
                model,
                max_concurrency=resolve_concurrency(config) or DEFAULT_COMPILE_CONCURRENCY,
                bundle=bundle,
            )

            # 编译成功后登记 hash，使 status/list/recompile/remove 可识别
            file_hash = HashRegistry.hash_file(source_path)
            registry.remove_by_hash(file_hash)
            registry.add(
                file_hash,
                {
                    "name": f"{document_id}.md",
                    "doc_name": document_id,
                    "type": "md",
                    "path": _registry_path(source_path, kb_dir),
                    "source_path": _registry_path(source_path, kb_dir),
                },
            )

        return {"compiled": True, "warnings": warnings, "source": str(source_path)}

    async def _remove_compiled_document(self, kb_dir, document_id):
        """复用 run_remove_for_api 按 document_id 删除 source/assets/registry/派生页。"""
        from openkb.cli import run_remove_for_api

        if not (kb_dir / ".openkb").is_dir():
            return {"removed": False, "status": "not_found", "warnings": []}

        # run_remove_for_api 自身在 kb_ingest_lock 内执行 resolve+plan+execute
        result = run_remove_for_api(kb_dir, document_id, keep_raw=True)
        status = result.get("status")
        warnings: list[str] = []
        if status == "multiple":
            warnings.append(f"document_id={document_id} 匹配到多个候选，未删除")
        return {
            "removed": status in ("removed", "partial"),
            "status": status,
            "warnings": warnings,
            "detail": result,
        }

    async def _delete_kb_dir(self, kb_dir):
        """加锁删除当前 KB 目录（幂等）。"""
        import shutil

        from openkb.locks import kb_ingest_lock

        if not kb_dir.exists():
            return {"deleted": False, "status": "not_found"}

        openkb_dir = kb_dir / ".openkb"
        if openkb_dir.is_dir():
            # KB 级独占锁；删除锁文件所在目录，锁通过已打开 fd 释放（POSIX 安全）
            with kb_ingest_lock(openkb_dir):
                shutil.rmtree(kb_dir, ignore_errors=True)
        else:
            shutil.rmtree(kb_dir, ignore_errors=True)
        return {"deleted": True, "status": "deleted"}

    def _build_metering_bundle(self, kb_dir):
        """构造 per-request metering bundle（独立对象，无并发覆盖风险）。"""
        from openkb.config import resolve_credential_bundle
        from openkb.jonex_metering import build_metering_headers

        bundle = resolve_credential_bundle(kb_dir)
        jonex_headers = build_metering_headers()
        return replace(
            bundle,
            extra_headers={**(bundle.extra_headers or {}), **jonex_headers},
        )
