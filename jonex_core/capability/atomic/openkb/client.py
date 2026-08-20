# [jonex] 悦溪新增文件 — OpenKB 客户端
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

OPENKB_CAPABILITY_ID = "atomic.openkb.v1"


# ── 抽象接口 ──

class OpenKBClient(ABC):
    @abstractmethod
    async def init_kb(self, kb_name: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def compile_parsed_document(
        self, kb_name: str, parsed_artifact: dict, tenant_id: str, kb_id: str,
    ) -> dict: ...

    @abstractmethod
    async def remove_document(
        self, kb_name: str, document_id: str, tenant_id: str, kb_id: str,
    ) -> dict: ...

    @abstractmethod
    async def delete_kb(self, kb_name: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def query(
        self, kb_name: str, question: str, tenant_id: str, kb_id: str,
        return_trace: bool = False,  # [jonex]
    ) -> dict: ...

    @abstractmethod
    async def status(self, kb_name: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def list_graph(self, kb_name: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def read_page(self, kb_name: str, path: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def list_wiki_contents(self, kb_name: str, tenant_id: str, kb_id: str,
                                 document_id: str = "") -> dict: ...

    @abstractmethod
    async def get_compile_status(self, kb_name: str, task_id: str, tenant_id: str, kb_id: str) -> dict: ...

    @abstractmethod
    async def list_compile_tasks(self, kb_name: str, tenant_id: str, kb_id: str,
                                 document_id: str = "") -> dict: ...


# ── Remote：通过 Sidecar /invoke ──

class RemoteOpenKBClient(OpenKBClient):
    """通过 Sidecar /invoke 调用 OpenKB 容器（生产用）。

    镜像 RemoteRAGClient 的调用模式：
    组装 {capability_id, payload, tenant_id} 的 body，POST Sidecar /invoke。
    """

    def __init__(self, endpoint: str = None, capability_id: str = OPENKB_CAPABILITY_ID):
        import httpx
        from jonex_core.common.config import get_config
        cfg = get_config()
        self._client = httpx.AsyncClient(
            base_url=(endpoint or cfg.SIDECAR_URL or "http://sidecar:8000").rstrip("/"),
            headers={"X-API-Key": "jonex_test_gateway"},
            timeout=300,
        )
        self._capability_id = capability_id

    def _workspace(self, tenant_id: str, kb_id: str) -> str:
        return f"{tenant_id}__{kb_id}"

    async def _invoke(self, action: str, data: dict, tenant_id: str, kb_id: str) -> dict:
        from jonex_core.common.exceptions import (
            InvalidParameterError,
            ResourceNotFoundError,
        )

        body = {
            "capability_id": self._capability_id,
            "payload": {"action": action, "knowledge_base_id": kb_id, "data": data},
            "tenant_id": tenant_id,
        }
        resp = await self._client.post(
            "/invoke",
            json=body,
            headers={"X-Tenant-ID": tenant_id},
        )
        resp.raise_for_status()
        result = resp.json()
        if not result.get("success"):
            # [jonex] 还原 adapter 侧 _OpenKBError 的语义（跨进程边界映射）
            err_code = (result.get("details") or {}).get("error_code", "")
            message = str(result.get("message") or "OpenKB invoke failed")
            if err_code in ("PATH_OUT_OF_SCOPE", "MISSING_PARAM"):
                raise InvalidParameterError(message=message)
            if err_code == "PAGE_NOT_FOUND":
                raise ResourceNotFoundError(message=message)
            raise RuntimeError(f"OpenKB invoke failed: {message}")
        return result["data"]

    async def init_kb(self, kb_name, tenant_id, kb_id):
        return await self._invoke("init_kb", {"kb": kb_name}, tenant_id, kb_id)

    async def compile_parsed_document(self, kb_name, parsed_artifact, tenant_id, kb_id):
        """编译 Jonex parsed artifact — 只消费 markdown/assets/metadata。"""
        return await self._invoke("compile_parsed_document", {
            "kb": kb_name,
            **parsed_artifact,
        }, tenant_id, kb_id)

    async def apply_schema(self, kb_name, tenant_id, kb_id, *,
                           schema_version: int, config: dict, agents_md: str):
        """[jonex] 投影 LLM-Wiki Schema 到 OpenKB（方案 §8）。"""
        return await self._invoke("apply_schema", {
            "kb": kb_name,
            "schema_version": schema_version,
            "config": config,
            "agents_md": agents_md,
        }, tenant_id, kb_id)

    async def remove_document(self, kb_name, document_id, tenant_id, kb_id):
        """删除 OpenKB 中指定 document_id 的编译输入和派生索引记录。"""
        return await self._invoke("remove_document", {
            "kb": kb_name,
            "document_id": document_id,
        }, tenant_id, kb_id)

    async def delete_kb(self, kb_name, tenant_id, kb_id):
        """删除当前 tenant + kb 对应的 OpenKB KB 目录。"""
        return await self._invoke("delete_kb", {"kb": kb_name}, tenant_id, kb_id)

    async def query(self, kb_name, question, tenant_id, kb_id, return_trace: bool = False):  # [jonex]
        return await self._invoke("query", {
            "kb": kb_name, "question": question, "return_trace": return_trace,
        }, tenant_id, kb_id)

    async def status(self, kb_name, tenant_id, kb_id):
        return await self._invoke("status", {"kb": kb_name}, tenant_id, kb_id)

    async def list_graph(self, kb_name, tenant_id, kb_id, document_id: str = ""):
        """列出 Wiki 的 entities/concepts 及其关系（document_id 可选：给则文档级）。"""
        return await self._invoke("list_graph", {"kb": kb_name, "document_id": document_id},
                                  tenant_id, kb_id)

    async def list_contents(self, kb_name, tenant_id, kb_id):
        """OpenKB 原生 list（get_kb_list 透传）——保持原样，不用于 Wiki 页面树。"""
        return await self._invoke("list_contents", {"kb": kb_name}, tenant_id, kb_id)

    async def read_page(self, kb_name: str, path: str, tenant_id: str, kb_id: str) -> dict:
        """读取 Wiki 页面 markdown（编译结果 Wiki 阅读模式）。"""
        return await self._invoke("read_page", {"kb": kb_name, "path": path},
                                  tenant_id, kb_id)

    async def list_wiki_contents(self, kb_name, tenant_id, kb_id, document_id: str = ""):
        """列出 Wiki 页面树（新 action；document_id 可选：给则文档级过滤）。

        与 list_contents（OpenKB 原生 get_kb_list 透传）是两个 action——
        Wiki 页面树不替换原生 list 语义。
        """
        return await self._invoke("list_wiki_contents", {"kb": kb_name, "document_id": document_id},
                                  tenant_id, kb_id)

    async def get_compile_status(self, kb_name, task_id, tenant_id, kb_id):
        """查询单个编译任务状态（排障/手工验证用）。"""
        return await self._invoke("get_compile_status", {"kb": kb_name, "task_id": task_id},
                                  tenant_id, kb_id)

    async def list_compile_tasks(self, kb_name, tenant_id, kb_id, document_id: str = ""):
        """列出该 KB 的编译任务（对账巡检批量拉取；document_id 可选过滤）。"""
        return await self._invoke("list_compile_tasks", {"kb": kb_name, "document_id": document_id},
                                  tenant_id, kb_id)

    async def recompile(self, kb_name, tenant_id, kb_id, doc_name=None, all_docs=False):
        return await self._invoke("recompile", {
            "kb": kb_name, "doc_name": doc_name, "all_docs": all_docs,
        }, tenant_id, kb_id)


# ── Mock（测试桩）──

# [jonex] 样例 wiki 浏览轨迹，供前端在 mock 模式下联调 openkb_query 推理链渲染。
_MOCK_TRACE = [
    {"tool": "read_file", "args": {"path": "index.md"},
     "output_preview": "# Knowledge Base Index ...", "output_chars": 512},
    {"tool": "read_file",
     "args": {"path": "summaries/c832fb46-9e33-4292-ac24-c27339be7405.md"},
     "output_preview": "本文档规定了医疗器械供应商审核的基本要求 ...", "output_chars": 4231},
    {"tool": "read_file", "args": {"path": "entities/cfda.md"},
     "output_preview": "CFDA 是 ...", "output_chars": 320},
    {"tool": "get_image",
     "args": {"image_path": "images/c832fb46-9e33-4292-ac24-c27339be7405/p1_img1.png"},
     "output_preview": "<image>"},
]


class MockOpenKBClient(OpenKBClient):
    async def init_kb(self, *a, **kw): return {"kb": "mock", "created": True}
    async def compile_parsed_document(self, *a, **kw): return {"status": "compiled"}
    async def apply_schema(self, *a, **kw): return {"applied": True, "schema_version": kw.get("schema_version", 1)}
    async def remove_document(self, *a, **kw): return {"status": "removed"}
    async def delete_kb(self, *a, **kw): return {"status": "deleted"}
    async def query(self, *a, **kw):
        if kw.get("return_trace"):
            return {"answer": "mock answer", "trace": list(_MOCK_TRACE)}
        return {"answer": "mock answer"}
    async def status(self, *a, **kw): return {"documents": 0}
    async def list_graph(self, *a, **kw): return {"entities": [], "relationships": [], "entities_count": 0, "relationships_count": 0}
    async def list_contents(self, *a, **kw): return {"documents": [], "summaries": [], "concepts": [], "entities": []}
    async def read_page(self, *a, **kw): return {"path": "", "content": ""}
    async def list_wiki_contents(self, *a, **kw): return {"summaries": [], "concepts": [], "entities": [], "document_id": ""}
    async def get_compile_status(self, *a, **kw): return {"status": "completed"}
    async def list_compile_tasks(self, *a, **kw): return {"tasks": []}
    async def recompile(self, *a, **kw): return {"results": []}


# ── 工厂 ──

def get_openkb_client(capability_id: str = OPENKB_CAPABILITY_ID) -> OpenKBClient:
    """获取 OpenKB Client。通过 CapabilityLocator 读取模式，一期只支持 remote。"""
    from jonex_core.capability.locator import get_locator, CapabilityMode

    spec = get_locator().get_spec(capability_id)

    if spec.mode == CapabilityMode.MOCK:
        return MockOpenKBClient()

    if spec.mode == CapabilityMode.REMOTE:
        from jonex_core.common.config import get_config
        cfg = get_config()
        return RemoteOpenKBClient(
            endpoint=spec.endpoint or cfg.SIDECAR_URL or "http://sidecar:8000",
            capability_id=capability_id,
        )

    raise ValueError(f"不支持的 OpenKB 能力模式：{spec.mode}")
