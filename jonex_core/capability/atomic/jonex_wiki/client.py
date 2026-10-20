# [jonex] 新增文件 — Jonex Wiki 客户端（knowledge_base 侧接口）
#
# 注：B1 单容器后，引擎侧 HTTP 客户端（原 JonexWikiEngineClient）已移除——
# JonexWikiCapability 进程内 import jonex_wiki 直接调 WikiApplication，不经 HTTP 转调。
# 本文件只保留 knowledge_base → Sidecar → capability /invoke 的调用侧接口。
from abc import ABC, abstractmethod


class JonexWikiClient(ABC):
    """knowledge_base 调 jonex_wiki 的抽象接口。"""

    @abstractmethod
    async def init_kb(self, kb_name: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def compile_parsed_document(self, kb_name: str, parsed_artifact: dict,
                                      tenant_id: str, kb_id: str) -> dict: ...

    async def compile_parsed_documents(self, kb_name: str, documents: list[dict],
                                       tenant_id: str, kb_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def apply_schema(self, kb_name: str, tenant_id: str, kb_id: str, *,
                           schema_version: int, config: dict, agents_md: str) -> dict: ...

    @abstractmethod
    async def remove_document(self, kb_name: str, document_id: str,
                              tenant_id: str, kb_id: str) -> dict: ...

    async def get_document_chunks(self, document_id: str, tenant_id: str, kb_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def delete_kb(self, kb_name: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def query(self, kb_name: str, question: str, tenant_id: str, kb_id: str,
                    return_trace: bool = False, thinking_mode: str | None = None) -> dict: ...

    @abstractmethod
    async def status(self, kb_name: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def list_graph(self, kb_name: str, tenant_id: str, kb_id: str,
                         document_id: str = "") -> dict: ...

    @abstractmethod
    async def read_page(self, kb_name: str, path: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def list_wiki_contents(self, kb_name: str, tenant_id: str, kb_id: str,
                                 document_id: str = "", view: str = "") -> dict: ...

    @abstractmethod
    async def get_wiki_backlinks(self, kb_name: str, path: str, tenant_id: str, kb_id: str,
                                 document_id: str = "") -> dict: ...

    @abstractmethod
    async def get_wiki_claims(self, kb_name: str, tenant_id: str, kb_id: str, *,
                              path: str = "", document_id: str = "",
                              limit: int = 500) -> dict: ...

    @abstractmethod
    async def get_outline(self, kb_name: str, tenant_id: str, kb_id: str,
                          document_id: str = "") -> dict: ...

    async def draft_outline(self, kb_name: str, tenant_id: str, kb_id: str,
                            request: dict, user_id=None) -> dict: ...

    async def adjust_outline(self, kb_name: str, tenant_id: str, kb_id: str,
                             request: dict, user_id=None) -> dict: ...

    async def get_compile_status(self, kb_name: str, task_id: str,
                                 tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def list_compile_tasks(self, kb_name: str, tenant_id: str, kb_id: str,
                                 document_id: str = "") -> dict: ...

    @abstractmethod
    async def recompile(self, kb_name: str, tenant_id: str, kb_id: str,
                        doc_name=None, all_docs: bool = False) -> dict: ...


class RemoteJonexWikiClient(JonexWikiClient):
    """通过 Sidecar /invoke 调用 jonex_wiki 适配器容器（生产用）。"""

    def __init__(self, endpoint: str = None, capability_id: str = "atomic.jonex_wiki.v1"):
        import httpx
        from jonex_core.common.config import get_config

        cfg = get_config()
        # [jonex] 内部鉴权必须用配置的网关 key：Sidecar 侧 _gateway_api_key_matches
        # 是**精确比对** GATEWAY_API_KEY，任何硬编码字面量都会 401（曾因硬编码
        # "jonex_test_gateway" 导致 jonex_wiki 检索全量失败，被 search_service
        # 误报为「未找到相关信息」）。与 openkb / rag 客户端保持一致。
        self._client = httpx.AsyncClient(
            base_url=(endpoint or cfg.SIDECAR_URL or "http://sidecar:8000").rstrip("/"),
            headers={"X-API-Key": cfg.GATEWAY_API_KEY},
            timeout=300,
        )
        self._capability_id = capability_id

    async def _invoke(self, action: str, data: dict, tenant_id: str, kb_id: str) -> dict:
        from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError

        body = {
            "capability_id": self._capability_id,
            "payload": {"action": action, "knowledge_base_id": kb_id, "data": data},
            "tenant_id": tenant_id,
        }
        from jonex_core.common.sidecar_client import post_sidecar
        resp = await post_sidecar(self._client, body, tenant_id)
        result = resp.json()
        if not result.get("success"):
            err_code = (result.get("details") or {}).get("error_code", "")
            message = str(result.get("message") or "Jonex Wiki invoke failed")
            # [jonex] 引擎 err_code（conflict / not_found / validation_failed / …）是**稳定且
            # 不翻译**的机器信号，必须随异常带出去：调用方若靠 message 文案分支，会在引擎或
            # 适配器任何一次本地化/改词之后**静默失效**（自愈变死代码，比不写更糟）。
            # message 只作给人看的说明。
            machine = {"engine_error_code": err_code}
            if err_code in ("not_found",):
                raise ResourceNotFoundError(message=message, details=machine)
            if err_code in ("conflict", "validation", "missing_tenant", "MISSING_PARAM"):
                raise InvalidParameterError(message=message, details=machine)
            raise RuntimeError(f"Jonex Wiki invoke failed: {message}")
        return result["data"]

    async def init_kb(self, kb_name, tenant_id, kb_id):
        return await self._invoke("init_kb", {"kb": kb_name}, tenant_id, kb_id)

    async def compile_parsed_document(self, kb_name, parsed_artifact, tenant_id, kb_id):
        """入库平台已解析产物。parsed_artifact = {source_path, source_name, document_id}。"""
        return await self._invoke("compile_parsed_document", {"kb": kb_name, **parsed_artifact},
                                  tenant_id, kb_id)

    async def compile_parsed_documents(self, kb_name, documents, tenant_id, kb_id):
        return await self._invoke("compile_parsed_documents", {"kb": kb_name, "documents": documents},
                                  tenant_id, kb_id)

    async def apply_schema(self, kb_name, tenant_id, kb_id, *, schema_version, config, agents_md):
        return await self._invoke("apply_schema", {
            "kb": kb_name, "schema_version": schema_version,
            "config": config, "agents_md": agents_md,
        }, tenant_id, kb_id)

    async def remove_document(self, kb_name, document_id, tenant_id, kb_id):
        return await self._invoke("remove_document", {"kb": kb_name, "document_id": document_id},
                                  tenant_id, kb_id)

    async def get_document_chunks(self, document_id, tenant_id, kb_id):
        return await self._invoke("get_document_chunks", {"document_id": document_id}, tenant_id, kb_id)

    async def delete_kb(self, kb_name, tenant_id, kb_id):
        return await self._invoke("delete_kb", {"kb": kb_name}, tenant_id, kb_id)

    async def query(self, kb_name, question, tenant_id, kb_id, return_trace: bool = False,
                    thinking_mode: str | None = None, progress_session: str | None = None):
        return await self._invoke("query", {
            "kb": kb_name, "question": question, "return_trace": return_trace,
            **({"progress_session": progress_session} if progress_session else {}),
            **({"thinking_mode": thinking_mode} if thinking_mode is not None else {}),
        }, tenant_id, kb_id)

    async def status(self, kb_name, tenant_id, kb_id):
        return await self._invoke("status", {"kb": kb_name}, tenant_id, kb_id)

    async def compilation_state(self, tenant_id, kb_id, paused=None):
        action = ("compilation_state" if paused is None else
                  "pause_compilation" if paused else "resume_compilation")
        return await self._invoke(action, {}, tenant_id, kb_id)

    async def list_graph(self, kb_name, tenant_id, kb_id, document_id: str = ""):
        return await self._invoke("list_graph", {"kb": kb_name, "document_id": document_id},
                                  tenant_id, kb_id)

    async def read_page(self, kb_name, path, tenant_id, kb_id):
        return await self._invoke("read_page", {"kb": kb_name, "path": path}, tenant_id, kb_id)

    async def list_wiki_contents(self, kb_name, tenant_id, kb_id, document_id: str = "",
                                 view: str = ""):
        """[jonex] view="counts" 只要三类页数（资产 tab 统计卡用），缺省返回页面条目。"""
        payload: dict = {"kb": kb_name, "document_id": document_id}
        if view:
            payload["view"] = view
        return await self._invoke("list_wiki_contents", payload, tenant_id, kb_id)

    async def get_wiki_backlinks(self, kb_name, path, tenant_id, kb_id, document_id: str = ""):
        return await self._invoke("get_wiki_backlinks",
                                  {"kb": kb_name, "path": path, "document_id": document_id},
                                  tenant_id, kb_id)

    async def get_wiki_claims(self, kb_name, tenant_id, kb_id, *, path: str = "",
                              document_id: str = "", limit: int = 500):
        return await self._invoke("get_wiki_claims",
                                  {"kb": kb_name, "path": path,
                                   "document_id": document_id, "limit": limit},
                                  tenant_id, kb_id)

    async def get_outline(self, kb_name, tenant_id, kb_id, document_id: str = ""):
        return await self._invoke("list_wiki_outline", {"kb": kb_name, "document_id": document_id},
                                  tenant_id, kb_id)

    async def draft_outline(self, kb_name, tenant_id, kb_id, request, user_id=None):
        return await self._invoke("draft_wiki_outline",
                                  {"kb": kb_name, **request, "user_id": user_id},
                                  tenant_id, kb_id)

    async def adjust_outline(self, kb_name, tenant_id, kb_id, request, user_id=None):
        return await self._invoke("adjust_wiki_outline",
                                  {"kb": kb_name, **request, "user_id": user_id},
                                  tenant_id, kb_id)

    async def get_compile_status(self, kb_name, task_id, tenant_id, kb_id):
        return await self._invoke("get_compile_status", {"kb": kb_name, "task_id": task_id},
                                  tenant_id, kb_id)

    async def list_compile_tasks(self, kb_name, tenant_id, kb_id, document_id: str = ""):
        return await self._invoke("list_compile_tasks", {"kb": kb_name, "document_id": document_id},
                                  tenant_id, kb_id)

    async def recompile(self, kb_name, tenant_id, kb_id, doc_name=None, all_docs=False):
        return await self._invoke("recompile", {
            "kb": kb_name, "doc_name": doc_name, "all_docs": all_docs,
        }, tenant_id, kb_id)


class MockJonexWikiClient(JonexWikiClient):
    """测试桩（与 MockOpenKBClient 对齐）。"""

    async def init_kb(self, *a, **kw): return {"kb": "mock", "created": True}
    async def compile_parsed_document(self, *a, **kw): return {"status": "compiled"}
    async def apply_schema(self, *a, **kw): return {"applied": True, "schema_version": kw.get("schema_version", 1)}
    async def remove_document(self, *a, **kw): return {"status": "removed"}
    async def get_document_chunks(self, document_id, *a, **kw):
        return {"doc_id": document_id, "total": 0, "chunks": [], "kb_type": "jonex_wiki"}
    async def delete_kb(self, *a, **kw): return {"status": "deleted"}
    async def query(self, *a, **kw): return {"answer": "mock answer"}
    async def status(self, *a, **kw): return {"documents": 0}
    async def list_graph(self, *a, **kw): return {"entities": [], "relationships": []}
    async def read_page(self, *a, **kw): return {"path": "", "content": ""}
    async def list_wiki_contents(self, *a, view: str = "", **kw):
        # [jonex] counts 视图与条目视图的返回形状不同，桩按 view 分流
        if view == "counts":
            return {"summaries": 0, "concepts": 0, "entities": 0, "document_id": ""}
        return {"summaries": [], "concepts": [], "entities": [], "document_id": ""}
    async def get_wiki_backlinks(self, *a, **kw): return {"total": 0, "items": []}
    async def get_wiki_claims(self, *a, **kw): return {"total": 0, "items": []}
    async def get_outline(self, *a, **kw): return {"outline": None, "pages": [], "topic_edges": []}
    async def draft_outline(self, *a, **kw): return {"outline": None, "pages": [], "topic_edges": []}
    async def adjust_outline(self, *a, **kw): return {"outline": None, "pages": [], "topic_edges": []}
    async def get_compile_status(self, *a, **kw): return {"status": "completed"}
    async def list_compile_tasks(self, *a, **kw): return {"tasks": []}
    async def recompile(self, *a, **kw): return {"results": []}
    async def compilation_state(self, *a, **kw):
        return {"kb_id": kw.get("kb_id", ""), "paused": False,
                "parsed_source_count": 0, "published_source_count": 0, "tasks": []}


def get_jonex_wiki_client(capability_id: str = "atomic.jonex_wiki.v1") -> JonexWikiClient:
    """获取 Jonex Wiki Client。通过 CapabilityLocator 读取模式，一期只支持 remote。"""
    from jonex_core.capability.locator import get_locator, CapabilityMode

    spec = get_locator().get_spec(capability_id)

    if spec.mode == CapabilityMode.MOCK:
        return MockJonexWikiClient()

    if spec.mode == CapabilityMode.REMOTE:
        from jonex_core.common.config import get_config

        cfg = get_config()
        return RemoteJonexWikiClient(
            endpoint=spec.endpoint or cfg.SIDECAR_URL or "http://sidecar:8000",
            capability_id=capability_id,
        )

    raise ValueError(f"不支持的 Jonex Wiki 能力模式：{spec.mode}")
